#!/usr/bin/env python3
"""[019 T321] Borner le repli WARP à 2–3 conteneurs et arrêter les sweeps.

Le repli WARP a été dimensionné à 12 conteneurs (image 1,03 Go) et n'était
jamais réduit : ils partagent le CPU/RAM de l'hôte avec les six tunnels
Gluetun. Ce script est idempotent :

- garde les premiers conteneurs WARP référencés par ``socks5_proxies``
  (ordre de la config), dans la limite de ``WARP_MAX_CONTAINERS`` (2..3) ;
- supprime les autres conteneurs WARP gérés ;
- supprime les conteneurs de sweep laissés par des scripts one-shot
  (``wgsweep``, ``udpsweep``) ;
- mesure le CPU hôte avant/après et écrit la preuve dans
  ``logs/warp_bound_<date>.json``.

Défaut : dry-run. ``--apply`` exécute les suppressions. La config WARP de
``config.yaml`` n'est jamais réécrite : les lignes excédentaires restent
présentes mais le pool les désactive à la construction des endpoints.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import yaml_get  # noqa: E402
from free.warp_provisioner import (  # noqa: E402
    is_warp_container_name,
    list_warp_containers,
    warp_container_cap,
)

LEGACY_SWEEPS = ("wgsweep", "udpsweep")


def _run(args: list[str], *, timeout: int = 60) -> str:
    proc = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "docker failed").strip()[:300])
    return (proc.stdout or "").strip()


def _warp_rows() -> list[dict[str, Any]]:
    rows = yaml_get("ip_rotation", "socks5_proxies", []) or []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _configured_containers(rows: list[dict[str, Any]]) -> list[str]:
    ordered: list[str] = []
    for row in rows:
        managed = bool(row.get("managed", False))
        provider = str(row.get("provider") or "").strip().lower()
        if not (managed or provider == "warp"):
            continue
        container = str(row.get("container") or "").strip()
        if container and container not in ordered:
            ordered.append(container)
    return ordered


def plan_bound(
    *,
    cap: int | None = None,
    existing: list[str] | None = None,
    configured: list[str] | None = None,
) -> dict[str, Any]:
    """Calcule gardés/supprimés sans toucher Docker."""

    limit = max(2, min(3, int(cap or warp_container_cap())))
    containers = list(existing if existing is not None else list_warp_containers())
    preferred = list(configured if configured is not None else
                     _configured_containers(_warp_rows()))
    keep: list[str] = []
    for name in preferred:
        if name in containers and name not in keep:
            keep.append(name)
        if len(keep) >= limit:
            break
    # Si la config référence des conteneurs absents, compléter avec les
    # conteneurs réels les plus anciens (tri numérique stable) pour ne pas
    # tomber sous la borne.
    for name in sorted(containers, key=_container_index):
        if len(keep) >= limit:
            break
        if name not in keep:
            keep.append(name)
    remove = [name for name in containers if name not in keep]
    return {
        "cap": limit,
        "existing": sorted(containers, key=_container_index),
        "configured": preferred,
        "keep": keep,
        "remove": remove,
    }


def _container_index(name: str) -> tuple[int, str]:
    try:
        return (int(str(name).rsplit("-", 1)[-1]), str(name))
    except (TypeError, ValueError):
        return (10_000, str(name))


def host_cpu_percent(*, sample_s: float = 3.0) -> float | None:
    try:
        import psutil

        return float(psutil.cpu_percent(interval=max(0.5, float(sample_s))))
    except Exception:
        return None


def _docker_state(names: list[str]) -> dict[str, str]:
    if not names:
        return {}
    try:
        output = _run(
            [
                "ps",
                "-a",
                "--filter",
                "name=^(" + "|".join(names) + ")$",
                "--format",
                "{{.Names}}\t{{.State}}",
            ],
            timeout=30,
        )
    except Exception:
        return {}
    state: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            state[parts[0].strip()] = parts[1].strip()
    return state


def apply_bound(plan: dict[str, Any], *, remove_sweeps: bool = True) -> dict[str, Any]:
    removed: list[str] = []
    errors: list[str] = []
    for name in plan.get("remove", []):
        try:
            _run(["rm", "-f", name], timeout=60)
            removed.append(name)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    sweeps_removed: list[str] = []
    if remove_sweeps:
        for name in LEGACY_SWEEPS:
            try:
                _run(["rm", "-f", name], timeout=60)
                sweeps_removed.append(name)
            except Exception as exc:
                # Un sweep déjà absent est un succès idempotent.
                if "no such" not in str(exc).lower():
                    errors.append(f"{name}: {type(exc).__name__}: {exc}")
    return {
        "removed": removed,
        "sweeps_removed": sweeps_removed,
        "errors": errors,
        "remaining": list(plan.get("keep", [])),
    }


def run(*, apply: bool, cap: int | None = None, remove_sweeps: bool = True) -> dict[str, Any]:
    started = time.time()
    cpu_before = host_cpu_percent()
    existing = list_warp_containers()
    plan = plan_bound(cap=cap, existing=existing)
    result: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "dry_run": not apply,
        "cpu_before_pct": cpu_before,
        "warp_states_before": _docker_state(existing),
        "plan": plan,
    }
    if apply:
        result["applied"] = apply_bound(plan, remove_sweeps=remove_sweeps)
        time.sleep(2.0)
        result["warp_states_after"] = _docker_state(plan["keep"])
        result["cpu_after_pct"] = host_cpu_percent()
    result["duration_s"] = round(time.time() - started, 2)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="supprimer réellement")
    parser.add_argument(
        "--keep",
        type=int,
        default=None,
        help="conteneurs WARP à garder (borné 2..3, défaut WARP_MAX_CONTAINERS)",
    )
    parser.add_argument(
        "--keep-sweeps",
        action="store_true",
        help="ne pas supprimer wgsweep/udpsweep",
    )
    parser.add_argument("--json", action="store_true", help="sortie JSON seule")
    args = parser.parse_args(argv)

    result = run(
        apply=bool(args.apply),
        cap=args.keep,
        remove_sweeps=not bool(args.keep_sweeps),
    )
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = logs / f"warp_bound_{stamp}.json"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        plan = result["plan"]
        print(
            f"WARP cap={plan['cap']} existing={len(plan['existing'])} "
            f"keep={plan['keep']} remove={plan['remove']}"
            f" dry_run={result['dry_run']}"
        )
        print(f"CPU before={result['cpu_before_pct']}%", end="")
        if "cpu_after_pct" in result:
            print(f" after={result['cpu_after_pct']}%")
            print(f"removed={result['applied']['removed']}")
            print(f"sweeps_removed={result['applied']['sweeps_removed']}")
            if result["applied"]["errors"]:
                print(f"errors={result['applied']['errors']}")
        else:
            print()
        print(f"proof={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
