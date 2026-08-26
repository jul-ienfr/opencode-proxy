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


def _load_control_api_key() -> str | None:
    """Read ip_rotation.control_api_key from config.yaml (single source)."""
    if not os.path.exists(CONFIG_PATH):
        return None
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(open(CONFIG_PATH, encoding="utf-8"))
        val = (data or {}).get("ip_rotation", {}).get("control_api_key")
        if isinstance(val, str) and val.strip():
            return val.strip()
        return None
    except Exception:
        try:
            text = open(CONFIG_PATH, encoding="utf-8").read()
            m = re.search(r"control_api_key:\s*['\"]?([^\n'\"\s]+)['\"]?", text)
            if m:
                return m.group(1).strip()
            return None
        except Exception:
            return None


def _sync_server_countries() -> bool:
    countries = _load_server_countries()
    if not countries:
        print(f"WARN: could not read ip_rotation.server_countries from {CONFIG_PATH} — .env not updated")
        return False
    _upsert_env_var(ENV_PATH, "SERVER_COUNTRIES", countries)
    print(f"OK: {ENV_PATH} SERVER_COUNTRIES synced from config.yaml ({len(countries.split(','))} countries)")
    return True


def _sync_control_api_key() -> bool:
    key = _load_control_api_key()
    if not key:
        print(f"WARN: could not read ip_rotation.control_api_key from {CONFIG_PATH} — credentials.env not updated")
        return False
    _upsert_env_var(DST, "VPN_CONTROL_API_KEY", key)
    # [fix SEC-1 26/08] le rôle d'auth du control server gluetun embarque la
    # MÊME clé dans son JSON (HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE) —
    # l'ancien code ne mettait à jour que VPN_CONTROL_API_KEY : après
    # rotation + recréation des conteneurs, le control API répondait 401
    # (rôle encore sur l'ancienne clé). Les deux variables sont syncd.
    import json as _json

    role = {"name": "normal", "auth": "apikey", "apikey": key}
    _upsert_env_var(DST, "HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE", _json.dumps(role))
    print(f"OK: {DST} VPN_CONTROL_API_KEY + HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE synced from config.yaml")
    return True


def main() -> None:
    did_credentials = False
    if os.path.exists(SRC):
        with open(SRC, encoding="utf-8") as f:
            lines = f.read().strip().splitlines()
        if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
            raise SystemExit(f"ERROR: {SRC} has no valid user/password (2 non-empty lines)")
        # [plan v10 Lot 6 — incident P0] UPSERT et plus OVERWRITE : l'ancien
        # `open(DST,"w")` réécrivait credentials.env avec 2 lignes, effaçant
        # VPN_CONTROL_API_KEY ET HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE — les
        # conteneurs recréés ensuite rejetaient leur PROPRE healthcheck en
        # 401 (boucle unhealthy/churn infinie). Upsert = préserve tout le reste.
        _upsert_env_var(DST, "OPENVPN_USER", lines[0].strip())
        _upsert_env_var(DST, "OPENVPN_PASSWORD", lines[1].strip())
        try:
            os.chmod(DST, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        except Exception:
            pass  # Windows may not support chmod 0600
        print(f"OK: {DST} upserted (other keys preserved). Values never displayed.")
        did_credentials = True
    else:
        print(f"INFO: {SRC} not found — skipping credentials.env generation (already migrated)")

    synced = _sync_server_countries()
    synced_key = _sync_control_api_key()
    if not did_credentials and not synced and not synced_key:
        raise SystemExit(f"ERROR: nothing to do — no {SRC} and no server_countries/control_api_key in {CONFIG_PATH}")


if __name__ == "__main__":
    main()
