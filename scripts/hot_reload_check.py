"""
Test du hot-reload du mapping personnalisé : vérifie que la section
`custom_routes` de config.yaml (source VIVANTE) est rechargée sans redémarrage.

Usage : lance le proxy dans un terminal, puis ce script dans un autre.
       (ou sans proxy : le reload fonctionne in-process aussi)

Le script :
  1. sauvegarde config.yaml,
  2. modifie la section custom_routes (changement de cible puis restauration),
  3. vérifie que _route_for() sert la nouvelle cible à chaque étape,
  4. restaure le fichier original (backup/restore intégral).

Note historique : l'ancienne version de ce script modifiait custom_routes.json,
qui n'est qu'un FALLBACK legacy depuis que load_custom_routes() privilégie
config.yaml (section custom_routes). Son verdict OK/ÉCHEC ne disait donc rien
de la configuration réelle.
"""

import os
import shutil
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from config.settings import CONFIG_PATH, CUSTOM_ROUTES_PATH, yaml_get

CONFIG_FILE = CONFIG_PATH
TEST_KEY = "zz-hotreload-check"
TEST_MATCH = "zz-hotreload-probe-model"
TARGET_A = "mimo-v2.5-free"
TARGET_B = "deepseek-v4-flash-free"
# Le reload est throttlé : forcer un intervalle de check court pour ce test.
_CHECK_INTERVAL = float(yaml_get("background", "custom_routes_check_interval", 5))
_SLEEP = _CHECK_INTERVAL + 1


def read_yaml_text():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return f.read()


def write_yaml_text(text):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(text)


def inject_test_route(text, target):
    """Insère/écrase une route de test dans la section custom_routes."""
    marker = "\ncustom_routes:\n"
    if marker not in text:
        text += f"\ncustom_routes:\n  {TEST_KEY}:\n    match:\n    - {TEST_MATCH}\n    model: {target}\n"
        return text
    block = (
        f"  {TEST_KEY}:\n    match:\n    - {TEST_MATCH}\n    model: {target}\n"
    )
    # Retire une éventuelle route de test précédente (run interrompu)
    lines = text.splitlines(keepends=True)
    out, skipping = [], False
    for line in lines:
        if line.rstrip("\r\n") == f"  {TEST_KEY}:":
            skipping = True
            continue
        if skipping:
            # fin du bloc : première ligne indentée "  " qui n'est PAS un enfant
            if line.startswith("    ") or line.startswith("      "):
                continue
            if line.strip() == "" :
                continue
            skipping = False
        out.append(line)
    text = "".join(out)
    idx = text.index(marker) + len(marker)
    return text[:idx] + block + text[idx:]


def main():
    import opencode  # après settings : _route_for dépend des deux modules

    print("Test hot-reload du mapping personnalisé (config.yaml:custom_routes)")
    print("=" * 60)

    if os.path.exists(CUSTOM_ROUTES_PATH):
        print(
            f"\n[!] ATTENTION : {CUSTOM_ROUTES_PATH} existe encore (source legacy "
            "fallback). Il est IGNORÉ tant que config.yaml:custom_routes est non vide."
        )

    backup = CONFIG_FILE + ".bak-hotreload-check"
    shutil.copy2(CONFIG_FILE, backup)
    try:
        original_text = read_yaml_text()

        # État initial : le modèle de sonde ne doit pas router
        opencode._route_cache.clear()
        r0 = opencode._route_for(TEST_MATCH)
        print(f"\n[1] État initial (pas de route): {'OK' if r0 is None else 'ECHEC'}")

        # Injection route → TARGET_A
        write_yaml_text(inject_test_route(original_text, TARGET_A))
        time.sleep(_SLEEP)
        opencode._route_cache.clear()
        ra = opencode._route_for(TEST_MATCH)
        ok_a = ra is not None and ra.get("model") == TARGET_A
        print(f"[2] Après injection → {TARGET_A}: {'OK' if ok_a else 'ECHEC'} (got {ra and ra.get('model')})")

        # Changement de cible → TARGET_B
        write_yaml_text(inject_test_route(original_text, TARGET_B))
        time.sleep(_SLEEP)
        opencode._route_cache.clear()
        rb = opencode._route_for(TEST_MATCH)
        ok_b = rb is not None and rb.get("model") == TARGET_B
        print(f"[3] Après changement → {TARGET_B}: {'OK' if ok_b else 'ECHEC'} (got {rb and rb.get('model')})")

        print(f"\n{'=' * 60}")
        if ok_a and ok_b:
            print("VERDICT : hot-reload FONCTIONNEL")
            return 0
        print("VERDICT : ECHEC — le reload n'a pas pris effet")
        return 1
    finally:
        write_yaml_text(read_yaml_text())  # no-op safety si backup absent
        shutil.copy2(backup, CONFIG_FILE)
        os.remove(backup)
        print("\nconfig.yaml restauré depuis le backup.")


if __name__ == "__main__":
    sys.exit(main())
