#!/usr/bin/env python3
"""Snapshot read-only de la flotte stations (baseline audit/roadmap).

Usage :
    python scripts/station_snapshot.py --out logs/station_snapshot.json
    python scripts/station_snapshot.py --api http://127.0.0.1:4000

Ne modifie rien : lit les endpoints dashboard, docker inspect/ps et le .env
(station keys uniquement). Aucun secret n'est écrit.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _get(url: str, timeout: float = 8.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _docker(args: list[str], timeout: float = 15.0) -> str:
    try:
        p = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        return (p.stdout or "").strip()
    except Exception as e:
        return f"ERR:{type(e).__name__}:{e}"


def _env_station_keys() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith(("VPN_TYPE_STATION", "OPENVPN_PROTOCOL_STATION", "SERVER_COUNTRIES=")):
                if "=" in s:
                    k, v = s.split("=", 1)
                    out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:4000")
    ap.add_argument("--out", default="logs/station_snapshot.json")
    ap.add_argument(
        "--containers",
        default="opencode-vpn,opencode-vpn-2,opencode-vpn-3,opencode-vpn-4,opencode-vpn-5,opencode-vpn-6",
    )
    args = ap.parse_args()

    containers = [c.strip() for c in args.containers.split(",") if c.strip()]
    snap = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "api": args.api,
        "pool_status": _get(f"{args.api.rstrip('/')}/api/pool-status"),
        "vpn_status": _get(f"{args.api.rstrip('/')}/api/vpn-status"),
        "containers": {},
        "env_station_keys": _env_station_keys(),
        "docker_ps": _docker(["ps", "--format", "{{.Names}}\t{{.Status}}"]).splitlines(),
    }
    for c in containers:
        inspect = _docker(["inspect", "-f", "{{json .State}}", c])
        try:
            state = json.loads(inspect) if inspect and not inspect.startswith("ERR:") else inspect
        except Exception:
            state = inspect
        snap["containers"][c] = {
            "inspect": state,
            "vpn_type": _docker(["exec", c, "printenv", "VPN_TYPE"]),
            "ovpn_protocol": _docker(["exec", c, "printenv", "OPENVPN_PROTOCOL"]),
        }

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
