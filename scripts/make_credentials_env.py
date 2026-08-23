#!/usr/bin/env python3
"""Generate compose env files from single sources.

1. LEGACY migration: credentials.txt (2 lines) → credentials.env
   (OPENVPN_USER / OPENVPN_PASSWORD, 0600). Dashboard now writes
   credentials.env directly ([24] single source); this exists only to
   migrate an old credentials.txt once.

2. F-H5 single source: config.yaml ip_rotation.server_countries →
   .env SERVER_COUNTRIES (docker-compose interpolation via
   ${SERVER_COUNTRIES:?…} — no inline fallback, fail-closed).

Usage: python scripts/make_credentials_env.py
NEVER prints credential values.
"""

import os
import re
import stat

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "vpn_configs", "credentials.txt")
DST = os.path.join(ROOT, "credentials.env")
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
ENV_PATH = os.path.join(ROOT, ".env")


def _load_server_countries() -> str | None:
    """Read ip_rotation.server_countries from config.yaml (single source)."""
    if not os.path.exists(CONFIG_PATH):
        return None
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(open(CONFIG_PATH, encoding="utf-8"))
        val = (data or {}).get("ip_rotation", {}).get("server_countries")
        if isinstance(val, list):
            return ",".join(str(x).strip() for x in val if str(x).strip())
        if isinstance(val, str):
            return val.strip()
        return None
    except Exception:
        # Fallback: raw parse without yaml (fresh clone without deps)
        try:
            text = open(CONFIG_PATH, encoding="utf-8").read()
            # Capture folded scalar after 'server_countries:' up to next key at same indent
            m = re.search(r"server_countries:\s*(.+?)(?:\n\s{2}\w|\n\w|\Z)", text, re.S)
            if not m:
                return None
            raw = m.group(1).strip()
            # Collapse YAML folded/plain lines into one comma list
            raw = re.sub(r"\s*\n\s*", "", raw)
            return raw.strip().strip('"').strip("'")
        except Exception:
            return None


def _upsert_env_var(path: str, key: str, value: str) -> None:
    """Upsert KEY=VALUE in an env file, preserving other lines and comments."""
    lines: list[str] = []
    found = False
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                # Match KEY= or KEY = or commented #KEY= — only replace active line
                if re.match(rf"^{re.escape(key)}\s*=", stripped):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass  # Windows may not support chmod 0600


def _sync_server_countries() -> bool:
    countries = _load_server_countries()
    if not countries:
        print(f"WARN: could not read ip_rotation.server_countries from {CONFIG_PATH} — .env not updated")
        return False
    _upsert_env_var(ENV_PATH, "SERVER_COUNTRIES", countries)
    print(f"OK: {ENV_PATH} SERVER_COUNTRIES synced from config.yaml ({len(countries.split(','))} countries)")
    return True


def main() -> None:
    did_credentials = False
    if os.path.exists(SRC):
        with open(SRC, encoding="utf-8") as f:
            lines = f.read().strip().splitlines()
        if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
            raise SystemExit(f"ERROR: {SRC} has no valid user/password (2 non-empty lines)")
        with open(DST, "w", encoding="utf-8") as f:
            f.write(f"OPENVPN_USER={lines[0].strip()}\n")
            f.write(f"OPENVPN_PASSWORD={lines[1].strip()}\n")
        os.chmod(DST, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        print(f"OK: {DST} written (permissions 0600). Values never displayed.")
        did_credentials = True
    else:
        print(f"INFO: {SRC} not found — skipping credentials.env generation (already migrated)")

    synced = _sync_server_countries()
    if not did_credentials and not synced:
        raise SystemExit(f"ERROR: nothing to do — no {SRC} and no server_countries in {CONFIG_PATH}")


if __name__ == "__main__":
    main()
