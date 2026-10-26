#!/usr/bin/env python3
"""Réconcilie les VPN_TYPE_STATIONn du .env avec les conteneurs réellement actifs.

Usage :
    python scripts/station_env_reconcile.py           # dry-run
    python scripts/station_env_reconcile.py --apply

Ne redémarre aucun conteneur. Corrige uniquement la source `.env` pour éviter
qu'un futur `compose up` ne convertisse une station OV en WG (ou inversement).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CONTAINERS = {
    1: "opencode-vpn",
    2: "opencode-vpn-2",
    3: "opencode-vpn-3",
    4: "opencode-vpn-4",
    5: "opencode-vpn-5",
    6: "opencode-vpn-6",
}


def _docker(args: list[str], timeout: int = 15) -> str:
    p = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=0x08000000 if sys.platform == "win32" else 0,
    )
    return (p.stdout or "").strip()


def _container_stack(sid: int) -> str | None:
    try:
        v = _docker(["exec", CONTAINERS[sid], "printenv", "VPN_TYPE"]).strip().lower()
    except Exception:
        return None
    return v if v in ("wireguard", "openvpn") else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    desired: dict[int, str] = {}
    for sid in sorted(CONTAINERS):
        stack = _container_stack(sid)
        if stack:
            desired[sid] = stack
        else:
            print(f"s{sid}: conteneur absent/stack inconnu — ignoré")

    if not desired:
        print("aucune stack active détectée")
        return 2

    print("stacks actives :", {f"s{k}": v for k, v in desired.items()})
    if not args.apply:
        print("dry-run : aucune écriture (--apply pour corriger .env)")
        return 0

    from vpn.manager import VPNManager

    mgr = object.__new__(VPNManager)
    mgr._compose_file_path = lambda: str(ROOT / "docker-compose.yml")  # type: ignore[method-assign]
    ok = mgr._sync_stack_env(desired)
    print("écrit" if ok else "échec écriture")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
