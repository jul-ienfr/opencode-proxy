"""
Test du hot-reload : vérifie que custom_routes.json est rechargé sans redémarrage.
Usage : lance le proxy dans un terminal, puis ce script dans un autre.
"""
import json
import sys
import os
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

from config.settings import ROUTES, CUSTOM_ROUTES, maybe_reload_custom_routes

ROUTES_FILE = "custom_routes.json"


def read_routes():
    with open(ROUTES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def write_routes(routes):
    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(routes, f, indent=2)


print("Test hot-reload de custom_routes.json")
print("=" * 60)

original = read_routes()
print(f"\n[1] État initial: mimov25 = {ROUTES.get('mimov25', {}).get('model')}")

# Modifier : mimo-v2.5 → mimo-v2.5 (changement de contenu)
new_routes = {
    "mimov25": {"match": ["mimo-v2.5"], "model": "mimo-v2.5"},
    "*": {"match": ["*"], "model": "mimo-v2.5"}
}
write_routes(new_routes)
time.sleep(1)
maybe_reload_custom_routes()
print(f"[2] Après modification: mimov25 = {ROUTES.get('mimov25', {}).get('model')}")
print(f"    {'OK' if ROUTES.get('mimov25', {}).get('model') == 'mimo-v2.5' else 'ECHEC'}")

# Restaurer : mimo-v2.5 → deepseek-v4-flash
write_routes(original)
time.sleep(1)
maybe_reload_custom_routes()
print(f"[3] Après restauration: mimov25 = {ROUTES.get('mimov25', {}).get('model')}")
print(f"    {'OK' if ROUTES.get('mimov25', {}).get('model') == 'deepseek-v4-flash' else 'ECHEC'}")

print(f"\n{'=' * 60}")
