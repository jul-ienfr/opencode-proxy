#!/usr/bin/env python3
"""ARCHIVÉ — autoscaler one-shot, remplacé par le catalogue d'egress (013).

Ce script n'est **pas** planifié et ne doit pas l'être. Le scheduler
`free/egress_scheduler.py` est la seule automatisation : il découvre, valide
et rotationne sans créer de stations en boucle. Conservé uniquement comme
outil de diagnostic manuel ; `--loop` est explicitement déconseillé.

Principes historiques :
- WARP : crée les conteneurs manquants jusqu'à la cible (IPv6 = bucket /64),
  sonde l'egress réel et n'active la ligne IPv6 que si elle répond.
- NordVPN : monte ``station_count`` UNE station à la fois, attend l'API, et
  rollback immédiat si la nouvelle station n'est pas connectée/saine. Ne
  dépasse jamais la limite ``--nordvpn-target`` (10 services compose).
- Public : élève ``public_proxies.max_active`` et laisse le scheduler
  revalider le feed. Aucun proxy non validé n'est activé par ce script.

Usage :
    python scripts/archive/autoscale_max_ips.py --dry-run
    python scripts/archive/autoscale_max_ips.py --apply --warp-target 12 --nordvpn-target 10 --public-max 64
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import yaml_get, yaml_set  # noqa: E402
from free.egress_probe import resolve_egress  # noqa: E402
from free.quota_ledger import quota_block  # noqa: E402
from free.warp_provisioner import WarpEndpointSpec, WarpProvisioner  # noqa: E402

API = "http://127.0.0.1:4000"
WARP_IMAGE = "caomingjun/warp:latest"
MAX_STATIONS = 10


def _log(msg: str) -> None:
    print(f"[autoscale] {msg}", flush=True)


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
        if (proc.stdout or "").strip() == "healthy":
            return True
        time.sleep(3)
    return False


def _api_json(path: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(f"{API}{path}", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _warp_rows() -> list[dict]:
    rows = yaml_get("ip_rotation", "socks5_proxies", []) or []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _save_warp_rows(rows: list[dict]) -> None:
    yaml_set("ip_rotation", "socks5_proxies", rows)


def _warp_containers(rows: list[dict]) -> set[str]:
    return {
        str(row.get("container") or "")
        for row in rows
        if str(row.get("provider") or "").lower() == "warp"
        and bool(row.get("managed"))
        and str(row.get("container") or "")
    }


def _next_warp_index(rows: list[dict]) -> int:
    indexes: list[int] = []
    for container in _warp_containers(rows):
        try:
            indexes.append(int(container.rsplit("-", 1)[-1]))
        except ValueError:
            continue
    return max(indexes, default=0) + 1


def scale_warp(target: int, *, apply: bool) -> dict:
    target = max(1, min(64, int(target or 12)))
    rows = _warp_rows()
    existing = _warp_containers(rows)
    indexes = sorted(
        int(name.rsplit("-", 1)[-1])
        for name in existing
        if name.rsplit("-", 1)[-1].isdigit()
    )
    next_index = _next_warp_index(rows)
    next_port = max((int(row.get("port") or 0) for row in rows), default=11080) + 1
    plan = []
    current = len(indexes)
    while current < target:
        plan.append({"index": next_index, "container": f"warp-{next_index}", "port": next_port})
        next_index += 1
        next_port += 1
        current += 1
    if not apply:
        return {"ok": True, "dry_run": True, "target": target, "current": len(existing), "plan": plan}

    provisioner = WarpProvisioner()
    created: list[dict] = []
    for item in plan:
        index = int(item["index"])
        container = str(item["container"])
        port = int(item["port"])
        result: dict = {"container": container, "port": port, "ok": False}
        try:
            spec = WarpEndpointSpec(
                label=f"WARP {index}",
                container=container,
                port=port,
                image=WARP_IMAGE,
                ipv6=True,
                managed=True,
                provider="warp",
                enabled=True,
            )
            provisioner._runner(provisioner._run_args(spec), 120)
            healthy = _wait_healthy(container)
            result["healthy"] = healthy
            if not healthy:
                result["reason"] = "health_timeout"
                created.append(result)
                continue
            ip = resolve_egress(
                proxy_url=f"socks5h://127.0.0.1:{port}",
                ipv6=True,
                timeout_s=20.0,
            )
            if not ip:
                result["reason"] = "egress_unresolved"
                created.append(result)
                continue
            block = quota_block(ip) or ""
            rows.append(
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "username": None,
                    "password": None,
                    "enabled": False,
                    "label": f"WARP {index}",
                    "ipv6": False,
                    "egress_ip": "",
                    "managed": True,
                    "container": container,
                    "image": WARP_IMAGE,
                    "block": "",
                    "provider": "warp",
                }
            )
            rows.append(
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "username": None,
                    "password": None,
                    "enabled": True,
                    "label": f"WARP {index} IPv6",
                    "ipv6": True,
                    "egress_ip": ip,
                    "managed": True,
                    "container": container,
                    "image": WARP_IMAGE,
                    "block": block,
                    "provider": "warp",
                }
            )
            _save_warp_rows(rows)
            result.update({"ok": True, "egress_ip": ip, "block": block})
        except Exception as exc:
            result["reason"] = f"{type(exc).__name__}: {exc}"
        created.append(result)
    return {"ok": all(row.get("ok") for row in created), "target": target, "created": created}


def scale_public(max_active: int, *, apply: bool) -> dict:
    target = max(1, min(256, int(max_active or 64)))
    cfg = dict(yaml_get("ip_rotation", "public_proxies", {}) or {})
    plan = {
        "max_active": target,
        "feed_path": str(cfg.get("feed_path") or "logs/proxy_lists/working.jsonl"),
        "enabled": bool(cfg.get("enabled", False)),
        "isolation": str(cfg.get("isolation") or "process"),
    }
    if not apply:
        return {"ok": True, "dry_run": True, "plan": plan}
    cfg["max_active"] = target
    cfg["feed_path"] = "logs/proxy_lists/working.jsonl"
    cfg.setdefault("enabled", True)
    yaml_set("ip_rotation", "public_proxies", cfg)
    return {"ok": True, "applied": plan}


def _station_ok(station: dict) -> bool:
    vpn = station.get("vpn") or {}
    if vpn.get("auth_failed"):
        return False
    if str(vpn.get("status") or "") not in ("connected", "running"):
        return False
    return bool(str(station.get("current_ip") or "").strip())


def scale_nordvpn(target: int, *, apply: bool) -> dict:
    target = max(1, min(MAX_STATIONS, int(target or MAX_STATIONS)))
    current = int(yaml_get("ip_rotation", "station_count", 6) or 6)
    plan = {"current": current, "target": target, "steps": list(range(current + 1, target + 1))}
    if not apply or target <= current:
        return {"ok": True, "dry_run": not apply, "plan": plan}
    applied: list[dict] = []
    for wanted in range(current + 1, target + 1):
        yaml_set("ip_rotation", "station_count", wanted)
        deadline = time.monotonic() + 180.0
        healthy = False
        last: dict = {}
        while time.monotonic() < deadline:
            time.sleep(5)
            try:
                payload = _api_json("/api/vpn-status")
                stations = payload.get("stations") or []
                if len(stations) >= wanted:
                    last = stations[wanted - 1]
                    if _station_ok(last):
                        healthy = True
                        break
            except Exception:
                continue
        if healthy:
            applied.append({"station_count": wanted, "ok": True, "ip": last.get("current_ip")})
            _log(f"NordVPN {wanted}/{target} connecté ({last.get('current_ip')})")
            continue
        # Rollback : ne jamais laisser une station AUTH/quota casser les autres.
        yaml_set("ip_rotation", "station_count", wanted - 1)
        applied.append(
            {
                "station_count": wanted,
                "ok": False,
                "reason": "auth_or_health_failed",
                "rollback_to": wanted - 1,
            }
        )
        _log(f"NordVPN {wanted}/{target} non sain — rollback à {wanted - 1}")
        break
    return {"ok": all(item.get("ok") for item in applied), "applied": applied, "target": target}


def run_once(args) -> dict:
    out: dict = {"ts": time.time()}
    if not args.skip_warp:
        out["warp"] = scale_warp(args.warp_target, apply=args.apply)
    if not args.skip_public:
        out["public"] = scale_public(args.public_max, apply=args.apply)
    if not args.skip_nordvpn:
        out["nordvpn"] = scale_nordvpn(args.nordvpn_target, apply=args.apply)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--warp-target", type=int, default=12)
    parser.add_argument("--nordvpn-target", type=int, default=10)
    parser.add_argument("--public-max", type=int, default=64)
    parser.add_argument("--skip-warp", action="store_true")
    parser.add_argument("--skip-nordvpn", action="store_true")
    parser.add_argument("--skip-public", action="store_true")
    args = parser.parse_args(argv)
    args.apply = bool(args.apply and not args.dry_run)

    # One-shot uniquement : aucune boucle autonome. Le scheduler du catalogue
    # est la seule automatisation autorisée.
    print(json.dumps(run_once(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
