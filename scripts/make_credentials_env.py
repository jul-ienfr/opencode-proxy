#!/usr/bin/env python3
"""LEGACY migration helper: generate credentials.env from vpn_configs/credentials.txt.

The dashboard now writes credentials.env directly ([24] single source);
this one-shot script exists only to migrate an old credentials.txt once.
Reads the two-line NordVPN service credentials file and writes a
docker-compose env_file (OPENVPN_USER / OPENVPN_PASSWORD) with owner-only
permissions. NEVER prints the values.

Usage: python scripts/make_credentials_env.py
"""

import os
import stat

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "vpn_configs", "credentials.txt")
DST = os.path.join(ROOT, "credentials.env")


def main() -> None:
    if not os.path.exists(SRC):
        raise SystemExit(f"ERROR: credentials file not found: {SRC}")
    with open(SRC, "r", encoding="utf-8") as f:
        lines = f.read().strip().splitlines()
    if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
        raise SystemExit(f"ERROR: {SRC} has no valid user/password (2 non-empty lines)")
    with open(DST, "w", encoding="utf-8") as f:
        f.write(f"OPENVPN_USER={lines[0].strip()}\n")
        f.write(f"OPENVPN_PASSWORD={lines[1].strip()}\n")
    os.chmod(DST, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    print(f"OK: {DST} written (permissions 0600). Values never displayed.")


if __name__ == "__main__":
    main()
