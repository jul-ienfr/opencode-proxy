"""Recrée les endpoints WARP en loopback et réconcilie leur egress (011).

Usage :
    python scripts/warp_recreate_loopback.py --dry-run
    python scripts/warp_recreate_loopback.py --apply

Le script :
1. lit `ip_rotation.socks5_proxies` (provider=warp, managed, enabled) ;
2. recrée chaque conteneur avec `-p 127.0.0.1:<port>:1080` et les options 011 ;
3. attend le healthcheck ;
4. mesure l'egress IPv6/IPv4 et écrit `egress_ip`/`block` dans config.yaml ;
5. vérifie l'unicité des `/64` et signale les doublons.

Aucune action si `--apply` n'est pas fourni.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import yaml_get, yaml_set  # noqa: E402
from free.egress_probe import resolve_egress  # noqa: E402
from free.quota_ledger import quota_block  # noqa: E402
from free.warp_provisioner import WarpEndpointSpec, WarpProvisioner  # noqa: E402


def _load_rows() -> list[dict]:
    raw = yaml_get("ip_rotation", "socks5_proxies", []) or []
    return [dict(row) for row in raw if isinstance(row, dict)]


def _save_rows(rows: list[dict]) -> None:
    yaml_set("ip_rotation", "socks5_proxies", rows)


def _wait_healthy(container: str, timeout_s: float = 120.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        proc = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                container,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        state = (proc.stdout or "").strip()
        if state == "healthy":
            return True
        time.sleep(3)
    return False


def _plan(rows: list[dict]) -> list[WarpEndpointSpec]:
    """Une spec par conteneur physique (IPv4 et IPv6 partagent le tunnel)."""
    specs: list[WarpEndpointSpec] = []
    seen: set[str] = set()
    for row in rows:
        if str(row.get("provider") or "").lower() != "warp":
            continue
        if not bool(row.get("managed")) or not bool(row.get("enabled", True)):
            continue
        container = str(row.get("container") or "")
        if not container or container in seen:
            continue
        seen.add(container)
        specs.append(
            WarpEndpointSpec(
                label=str(row.get("label") or ""),
                container=container,
                port=int(row.get("port") or 0),
                image=str(row.get("image") or "caomingjun/warp:latest"),
                ipv6=bool(row.get("ipv6", True)),
                managed=True,
                provider="warp",
                block=str(row.get("block") or "") or None,
                enabled=True,
            )
        )
    return specs


def _rows_for_container(rows: list[dict], container: str) -> list[dict]:
    return [
        row
        for row in rows
        if str(row.get("provider") or "").lower() == "warp"
        and bool(row.get("managed"))
        and bool(row.get("enabled", True))
        and str(row.get("container") or "") == container
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    rows = _load_rows()
    specs = _plan(rows)
    if not specs:
        print(json.dumps({"ok": False, "error": "no enabled managed WARP endpoint"}))
        return 2
    if args.dry_run or not args.apply:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "endpoints": [
                        {"label": spec.label, "port": spec.port, "container": spec.container}
                        for spec in specs
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0

    provisioner = WarpProvisioner()
    results: list[dict] = []
    for spec in specs:
        result: dict = {
            "container": spec.container,
            "port": spec.port,
            "families": [],
            "ok": False,
        }
        try:
            provisioner._runner(["rm", "-f", spec.container], 60)
            provisioner._runner(provisioner._run_args(spec), 120)
            healthy = _wait_healthy(spec.container)
            result["healthy"] = healthy
            if not healthy:
                result["reason"] = "health_timeout"
                results.append(result)
                continue
            all_ok = True
            for row in _rows_for_container(rows, spec.container):
                family = "ipv6" if bool(row.get("ipv6")) else "ipv4"
                ip = resolve_egress(
                    proxy_url=f"socks5h://127.0.0.1:{spec.port}",
                    ipv6=bool(row.get("ipv6")),
                    timeout_s=15.0,
                )
                entry = {"label": row.get("label"), "family": family, "ok": False}
                if not ip:
                    entry["reason"] = "egress_unresolved"
                    all_ok = False
                    result["families"].append(entry)
                    continue
                block = quota_block(ip)
                row["egress_ip"] = ip
                row["block"] = block or ""
                entry.update({"ok": True, "egress_ip": ip, "block": block})
                result["families"].append(entry)
            result["ok"] = all_ok
            if not all_ok:
                result["reason"] = "family_unresolved"
        except Exception as exc:
            result["reason"] = f"{type(exc).__name__}: {exc}"
        results.append(result)

    _save_rows(rows)
    blocks = [
        str(family.get("block") or "")
        for item in results
        for family in item.get("families", [])
        if family.get("block")
    ]
    duplicates = sorted({block for block in blocks if blocks.count(block) > 1})
    print(
        json.dumps(
            {
                "ok": all(item.get("ok") for item in results),
                "results": results,
                "duplicate_blocks": duplicates,
                "unique_blocks": len(set(blocks)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all(item.get("ok") for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
