#!/usr/bin/env python3
"""Déplace `control_api_key` de config.yaml vers .env (chmod 600).

Usage :
    python scripts/migrate_control_key.py --dry-run
    python scripts/migrate_control_key.py --apply

Le fichier credentials.env (utilisé par les conteneurs gluetun) n'est pas
touché : la clé doit déjà y être identique. Le script refuse d'écrire si
config.yaml et credentials.env divergent.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read_yaml_key(text: str) -> tuple[str | None, str | None]:
    # clé sous ip_rotation, indentée de 2 espaces.
    m = re.search(r"^  control_api_key:\s*(\S+)\s*$", text, re.MULTILINE)
    return (m.group(1), m.group(0)) if m else (None, None)


def _credentials_key() -> str | None:
    try:
        for line in (ROOT / "credentials.env").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("VPN_CONTROL_API_KEY="):
                return line.strip().split("=", 1)[1]
    except Exception:
        pass
    return None


def _env_has_key() -> bool:
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("VPN_CONTROL_API_KEY="):
                return True
    except Exception:
        pass
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.apply and not args.dry_run:
        ap.error("choisir --dry-run ou --apply")

    cfg_path = ROOT / "config.yaml"
    text = cfg_path.read_text(encoding="utf-8")
    key, line = _read_yaml_key(text)
    if not key:
        print("déjà migré : aucune control_api_key dans config.yaml")
        return 0
    # la paire (key, line) est toujours les deux nulles ou les deux fournies
    assert line is not None
    cred = _credentials_key()
    if cred and cred != key:
        print("REFUS : config.yaml et credentials.env divergent — corriger d'abord")
        return 2
    if _env_has_key():
        print("REFUS : VPN_CONTROL_API_KEY déjà présent dans .env — vérifier la valeur")
        return 2
    print(f"clé trouvée (len={len(key)}), credentials.env={'identique' if cred else 'absente'}")
    if args.dry_run:
        print("dry-run : aucune écriture")
        return 0

    # 1) .env
    env_path = ROOT / ".env"
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    env_path.write_text(existing + f"VPN_CONTROL_API_KEY={key}\n", encoding="utf-8")
    try:
        os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    # 2) retirer la ligne du YAML
    new_text = text.replace(line + "\n", "", 1)
    tmp = cfg_path.with_suffix(".yaml.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, cfg_path)
    print("migré : clé dans .env, retirée de config.yaml ; redémarrer le proxy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
