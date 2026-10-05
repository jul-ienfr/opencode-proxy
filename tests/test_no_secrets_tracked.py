"""Garde-fou secrets : aucun .ovpn / credentials / clé ne doit être tracké.

B3 audit : vpn_configs/*.ovpn et vpn/configs/*.ovpn étaient versionnés
malgré .gitignore (*.ovpn). gitignore ne dé-tracke rien — ce test échoue
si un secret refait surface dans l'index.
"""

import subprocess


def _tracked_files() -> list[str]:
    out = subprocess.check_output(["git", "ls-files"], text=True)
    return out.splitlines()


def test_no_ovpn_tracked():
    tracked = _tracked_files()
    ovpns = [f for f in tracked if f.lower().endswith(".ovpn")]
    assert ovpns == [], f".ovpn trackés (fuite secret) : {ovpns}"


def test_no_credentials_tracked():
    tracked = _tracked_files()
    banned = [
        f
        for f in tracked
        if f in {"credentials.env", "api_keys.json", ".env"}
        or f.endswith("credentials.txt")
        or f.endswith("wireguard.env")
    ]
    assert banned == [], f"fichiers secrets trackés : {banned}"
