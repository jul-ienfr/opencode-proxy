#!/usr/bin/env python3
"""Check or refresh NordVPN service credentials from the local access token.

The script never prints secret values. It reads ``NORDVPN_TOKEN`` from
``credentials.env``, calls the official NordVPN endpoint with Basic auth
``token:<token>``, and can update ``credentials.env`` and
``vpn_configs/credentials.txt`` with the returned service username/password.

Examples:
    python scripts/refresh_nord_credentials.py --check
    python scripts/refresh_nord_credentials.py --apply
    python scripts/refresh_nord_credentials.py --apply --token-file .env

A 401 means the token itself is invalid/expired; no local credential can be
derived from it. Obtain a fresh access token from the NordVPN manual setup
page of the existing account, then re-run ``--apply``.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / "credentials.env"
SRC = ROOT / "vpn_configs" / "credentials.txt"
URL = "https://api.nordvpn.com/v1/users/services/credentials"


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def fetch(token: str) -> dict:
    raw = base64.b64encode(f"token:{token}".encode()).decode()
    request = urllib.request.Request(URL, headers={"Authorization": f"Basic {raw}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def report(data: dict, status: int) -> dict:
    user = str(data.get("username") or "")
    password = str(data.get("password") or "")
    wireguard = str(data.get("nordlynx_private_key") or "")
    return {
        "ok": status == 200 and bool(user) and bool(password),
        "status": status,
        "fields": sorted(data) if isinstance(data, dict) else [],
        "has_username": bool(user),
        "has_password": bool(password),
        "has_wireguard_key": bool(wireguard),
        "username_len": len(user),
        "password_len": len(password),
        "wireguard_key_len": len(wireguard),
    }


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def apply_credentials(user: str, password: str, wireguard_key: str = "") -> dict:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    env_backup = ENV.with_name(f"credentials.env.bak-nord-{stamp}")
    if ENV.exists():
        shutil.copy2(ENV, env_backup)
    lines = []
    seen_user = False
    seen_password = False
    for line in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("OPENVPN_USER="):
            lines.append(f"OPENVPN_USER={user}")
            seen_user = True
        elif line.startswith("OPENVPN_PASSWORD="):
            lines.append(f"OPENVPN_PASSWORD={password}")
            seen_password = True
        else:
            lines.append(line)
    if not seen_user:
        lines.append(f"OPENVPN_USER={user}")
    if not seen_password:
        lines.append(f"OPENVPN_PASSWORD={password}")
    _atomic_write(ENV, "\n".join(lines) + "\n")
    src_backup = ""
    if SRC.exists():
        src_backup = str(SRC.with_name(f"credentials.txt.bak-nord-{stamp}"))
        shutil.copy2(SRC, src_backup)
    _atomic_write(SRC, f"{user}\n{password}\n")
    wg_result: dict[str, str] = {}
    if wireguard_key:
        wg = ROOT / "vpn_configs" / "wireguard.env"
        wg_backup = ""
        if wg.exists():
            wg_backup = str(wg.with_name(f"wireguard.env.bak-nord-{stamp}"))
            shutil.copy2(wg, wg_backup)
        wg_lines: list[str] = []
        seen_key = False
        for line in (
            wg.read_text(encoding="utf-8", errors="replace").splitlines()
            if wg.exists()
            else []
        ):
            if line.startswith("WIREGUARD_PRIVATE_KEY="):
                wg_lines.append(f"WIREGUARD_PRIVATE_KEY={wireguard_key}")
                seen_key = True
            else:
                wg_lines.append(line)
        if not seen_key:
            wg_lines.append(f"WIREGUARD_PRIVATE_KEY={wireguard_key}")
        _atomic_write(wg, "\n".join(wg_lines) + "\n")
        wg_result = {"wireguard": str(wg), "wireguard_backup": wg_backup}
    return {
        "env": str(ENV),
        "env_backup": str(env_backup),
        "source": str(SRC),
        "source_backup": src_backup,
        **wg_result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="only verify the token (default)")
    parser.add_argument("--apply", action="store_true", help="write the returned service credentials")
    parser.add_argument("--token-file", default=str(ENV), help="file containing NORDVPN_TOKEN")
    args = parser.parse_args()
    env = load_env(Path(args.token_file))
    token = env.get("NORDVPN_TOKEN", "")
    if not token:
        print(json.dumps({"ok": False, "error": "NORDVPN_TOKEN absent"}))
        return 2
    try:
        data = fetch(token)
        status = 200
    except urllib.error.HTTPError as exc:
        print(json.dumps({"ok": False, "status": exc.code, "error": "token refusé (401)" if exc.code == 401 else str(exc)}))
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "status": 0, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    result = report(data if isinstance(data, dict) else {}, status)
    print(json.dumps(result, ensure_ascii=False))
    if not result["ok"]:
        return 1
    if args.apply and not args.check:
        print(
            json.dumps(
                {
                    "applied": apply_credentials(
                        str(data["username"]),
                        str(data["password"]),
                        str(data.get("nordlynx_private_key") or ""),
                    )
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
