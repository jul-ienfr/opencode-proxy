#!/usr/bin/env python3
"""Audit de configuration : la clé est-elle lue par le code, et existe-t-elle ?

**Le problème que ça attaque.** `config.yaml` contient des centaines de clés. Une
faute de frappe (`watchdog_intervall`) ne casse rien : le code lit
`yaml_get("ip_rotation.watchdog_interval", 30)`, prend le défaut, et le réglage
de l'utilisateur est ignoré **en silence**. Inversement, une clé restée après un
refactor ne sert plus à rien mais continue d'être crue active.

**Deux rapports, deux gestes :**

* `dead` — présentes dans `config.yaml`, jamais lues (ou lues nulle part sous ce
  nom) : soit un vestige à supprimer, soit une faute de frappe à corriger ;
* `missing` — lues par `yaml_get("<clé>")` mais absentes du fichier : le défaut
  s'applique, et l'utilisateur croit avoir réglé quelque chose.

**Ce que ce script ne fait pas.** Il ne juge pas les sections **dynamiques**
(`models.*`, `custom_routes.*`, `free_model_map.*`, `pricing.*`…) : leurs clés
sont des identifiants de données, pas des réglages. Les ignorer est une décision
assumée, écrite ici pour ne pas être redécouverte.

Usage :
    python scripts/config_audit.py              # rapport lisible
    python scripts/config_audit.py --json       # machine
    python scripts/config_audit.py --strict     # code de sortie 1 si dead > seuil
    python scripts/config_audit.py --update     # réécrit _research/config_audit_baseline.json
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
CONFIG = RACINE / "config.yaml"
BASELINE = RACINE / "_research" / "config_audit_baseline.json"

# Sections dont les clés sont des DONNÉES (ids de modèles, routes nommées),
# pas des réglages : les auditer produirait du bruit permanent.
SECTIONS_DYNAMIQUES = {"models", "custom_routes", "free_model_map", "pricing"}

# Cartes dont les CLÉS ENFANTS sont des identifiants (modèle, pays, cause) :
# `effort_caps.mimo-v2.6` n'est pas un réglage nommé, c'est une entrée de table.
# On ne les audite pas — sinon chaque nouveau modèle apparaît comme « clé morte ».
CARTES_PAR_IDENTIFIANT = {
    "effort_caps",
    "min_tokens",
    "free_model_candidates",
    "policies",
    "per_model",
    "defaults",
    "bad_ttl_by_cause",
    "free_parallel",
    "latency_rotation",
    "socks5_proxies",
    "identity_profiles",
    "ip_check_urls",
    "shims",
    "effort_order",
    "reasoning_options",
    "max_completion_models",
    "aliases",
    "go_only_ids",
    "purged_ids",
    "probe",
    "overflow_markers",
    "state_file_2",
}

# Fichiers à ignorer dans la recherche d'usage.
EXCLUS = ("_research", "tests", "Lib", "node_modules", ".git", "site-packages")


def _fichiers_python() -> list[Path]:
    sortie = []
    for chemin in RACINE.rglob("*.py"):
        relatif = str(chemin.relative_to(RACINE))
        if any(partie in relatif for partie in EXCLUS):
            continue
        sortie.append(chemin)
    return sortie


def cles_du_fichier(chemin: Path | None = None) -> dict[str, object]:
    """Aplatit `config.yaml` en `section.sous_cle` → valeur (les dynamiques exclues).

    `chemin=None` plutôt que `chemin=CONFIG` : un défaut est lié à la définition,
    donc le monkeypatch de `CONFIG` dans les tests ne l'aurait pas atteint.
    """
    import yaml

    chemin = chemin or CONFIG
    data = yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}
    aplaties: dict[str, object] = {}

    def _descend(noeud: object, prefixe: str) -> None:
        if isinstance(noeud, dict):
            for cle, valeur in noeud.items():
                chemin_complet = f"{prefixe}.{cle}" if prefixe else str(cle)
                if isinstance(valeur, dict):
                    if not prefixe and str(cle) in SECTIONS_DYNAMIQUES:
                        continue
                    if str(cle) in CARTES_PAR_IDENTIFIANT:
                        continue  # table d'identifiants : pas un réglage
                    _descend(valeur, chemin_complet)
                else:
                    aplaties[chemin_complet] = valeur
        else:
            aplaties[prefixe] = noeud

    _descend(data, "")
    return aplaties


def _litteraux_yaml_get() -> set[str]:
    """Clés lues via `yaml_get("...")` — extraites par l'AST, pas par regex.

    La regex comptait les docstrings : une explication contenant
    `yaml_get("<clé>")` créait une fausse clé « manquante ». L'AST ne voit que
    les appels réels.

    Résultat mis en cache : parser ~200 fichiers coûtait l'essentiel des 28 s
    du gate, pour une donnée qui ne change pas pendant un run.
    """
    global _CLES_LUES_CACHE
    if _CLES_LUES_CACHE is not None:
        return _CLES_LUES_CACHE
    cles: set[str] = set()
    for chemin in _fichiers_python():
        if chemin.name == "config_audit.py":
            continue
        try:
            arbre = ast.parse(chemin.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for noeud in ast.walk(arbre):
            if not isinstance(noeud, ast.Call):
                continue
            fonction = noeud.func
            nom = getattr(fonction, "id", None) or getattr(fonction, "attr", None) or ""
            if nom not in ("yaml_get", "_cfg_get", "cfg_get"):
                continue
            if not noeud.args:
                continue
            premier = noeud.args[0]
            if isinstance(premier, ast.Constant) and isinstance(premier.value, str):
                cles.add(premier.value)
    _CLES_LUES_CACHE = cles
    return cles


_CLES_LUES_CACHE: set[str] | None = None


def _source_complete() -> str:
    """Source concaténée de tous les .py audités — calculée **une fois**.

    L'audit est appelé une dizaine de fois par les tests ; rescanner plusieurs
    mégaoctets à chaque fois coûtait 55 s au lieu de 5. La source ne change pas
    pendant un run : la mettre en cache est sans risque.
    """
    global _SOURCE_CACHE
    if _SOURCE_CACHE is None:
        morceaux = []
        for chemin in _fichiers_python():
            try:
                morceaux.append(chemin.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        _SOURCE_CACHE = "\n".join(morceaux)
    return _SOURCE_CACHE


_SOURCE_CACHE: str | None = None
_AUDIT_CACHE: dict[tuple[str, float], dict[str, object]] = {}


def auditer(chemin: Path | None = None) -> dict[str, object]:
    chemin = chemin or CONFIG
    try:
        signature = (str(chemin), chemin.stat().st_mtime)
    except OSError:
        signature = (str(chemin), 0.0)
    if signature in _AUDIT_CACHE:
        return _AUDIT_CACHE[signature]

    aplaties = cles_du_fichier(chemin)
    lues_exactement = _litteraux_yaml_get()
    source = _source_complete()

    mortes: list[str] = []
    for cle in aplaties:
        feuille = cle.rsplit(".", 1)[-1]
        # « utilisée » = son nom apparaît quelque part dans le code, ou elle est
        # lue via yaml_get sous sa forme complète. Volontairement permissif :
        # on cherche le silence, pas la pureté.
        if cle in lues_exactement:
            continue
        if re.search(rf"[\"']{re.escape(feuille)}[\"']", source):
            continue
        mortes.append(cle)

    manquantes = sorted(
        cle
        for cle in lues_exactement
        # une clé sans point est une SECTION entière (`yaml_get("server")`) :
        # légitime, ce n'est pas un réglage manquant.
        if "." in cle and cle not in aplaties
    )
    resultat: dict[str, object] = {
        "total_cles": len(aplaties),
        "cles_lues_par_yaml_get": len(lues_exactement),
        "dead": sorted(mortes),
        "missing": manquantes,
    }
    _AUDIT_CACHE[signature] = resultat
    return resultat


def _rapport(r: dict[str, object]) -> None:
    dead = r["dead"]
    missing = r["missing"]
    print(f"clés de configuration (hors sections dynamiques) : {r['total_cles']}")
    print(f"clés lues via yaml_get                          : {r['cles_lues_par_yaml_get']}")
    print(f"clés jamais lues (dead)                         : {len(dead)}")
    print(f"clés lues mais absentes du fichier (missing)    : {len(missing)}")
    if dead:
        print("\n--- dead (vestige ou faute de frappe) ---")
        for cle in dead:
            print("  -", cle)
    if missing:
        print("\n--- missing (le défaut s'applique, l'utilisateur croit régler) ---")
        for cle in missing:
            print("  -", cle)


def main(argv: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(description="Audit des clés de config.yaml")
    parseur.add_argument("--json", action="store_true")
    parseur.add_argument("--strict", action="store_true", help="échec si le nombre de dead dépasse la référence")
    parseur.add_argument("--update", action="store_true", help="enregistre la référence actuelle")
    args = parseur.parse_args(argv)

    resultat = auditer()
    if args.json:
        print(json.dumps(resultat, indent=2, ensure_ascii=False))
    else:
        _rapport(resultat)

    if args.update:
        BASELINE.parent.mkdir(exist_ok=True)
        BASELINE.write_text(
            json.dumps({"dead": resultat["dead"], "missing": resultat["missing"]}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nréférence écrite : {BASELINE.relative_to(RACINE)}")
        return 0

    if args.strict:
        reference = {"dead": [], "missing": []}
        if BASELINE.exists():
            reference = json.loads(BASELINE.read_text(encoding="utf-8"))
        nouvelles_dead = sorted(set(resultat["dead"]) - set(reference.get("dead", [])))
        if nouvelles_dead:
            print(
                f"\nÉCHEC : {len(nouvelles_dead)} clé(s) morte(s) de plus que la référence — "
                "faute de frappe probable dans config.yaml",
                file=sys.stderr,
            )
            for cle in nouvelles_dead:
                print("  +", cle, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
