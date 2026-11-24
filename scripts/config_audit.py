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
from typing import Any

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
    "quota_per_ip_by_model",
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

# [024 T224] Toutes les sections sont strictes : une feuille n'est vivante que
# si le code la lit explicitement. L'ancien filet « le nom de la feuille
# apparaît entre guillemets » est retiré (il retenait 62 % des clés par
# coïncidence ; `enabled` apparaît 1 110 fois).
STRICT_SECTIONS: set[str] | None = None

_READER_NAMES = {
    "yaml_get",
    "_cfg_get",
    "cfg_get",
    "_cfg",
    "_cfg_int",
    "_cfg_float",
    "_cfg_bool",
    "_cfg_str",
    "_cfg_list",
    "_cfg_dict",
    "_cfg_num",
    "_yaml_get",
    "yaml_get_fn",
    "_yaml_int",
    "_yaml_float",
    "_yaml_bool",
    "_yaml_str",
    "_yaml_list",
    "_yaml_dict",
}

_CONFIG_RECEIVER_MARKERS = (
    "cfg",
    "config",
    "settings",
    "yaml",
    "section",
    "_data",
)


def _receiver_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _receiver_name(node.func)
    return ""


def _is_config_receiver(name: str) -> bool:
    lowered = name.lower()
    if any(marker in lowered for marker in _CONFIG_RECEIVER_MARKERS):
        return True
    return name.isupper() and len(name) > 3


def _is_config_expression(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and _is_config_receiver(child.id):
            return True
        if isinstance(child, ast.Attribute) and _is_config_receiver(child.attr):
            return True
    return False


def _literal_str(node: ast.AST, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


_CONST_MAP_MARKERS = (
    "CONFIG",
    "DEFAULT",
    "BOUND",
    "THRESHOLD",
    "KEY",
    "MAP",
    "SECTION",
    "FIELDS",
)


def _collect_consts(arbre: ast.AST) -> dict[str, str]:
    consts: dict[str, str] = {}
    for node in ast.walk(arbre):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                consts[target.id] = value.value
    return consts


def _collect_config_map_keys(arbre: ast.AST) -> set[str]:
    keys: set[str] = set()
    for node in ast.walk(arbre):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not isinstance(value, ast.Dict):
            continue
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not any(
            marker in name.upper()
            for name in names
            for marker in _CONST_MAP_MARKERS
        ):
            continue
        for key in value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
    return keys

# Secrets lus depuis l'environnement/credentials.env, volontairement absents
# du config.yaml suivi (scrub au chargement). Ne pas les compter « missing ».
SECRET_KEYS = {"ip_rotation.control_api_key"}

# Fichiers à ignorer dans la recherche d'usage.
EXCLUS = (
    "_research",
    "tests",
    "Lib",
    "node_modules",
    ".git",
    "site-packages",
    ".audit-wt",
    ".audit-logs-wt",
    ".kilo",
)


def _est_dynamique(cle: str) -> bool:
    """Vraie pour une clé sous une section/map d'identifiants.

    Ces clés sont volontairement absentes de l'aplatissement (`CARTES_PAR_
    IDENTIFIANT`), donc les compter comme « lues mais absentes » serait un
    faux positif.
    """

    parties = cle.split(".")
    if parties and parties[0] in SECTIONS_DYNAMIQUES:
        return True
    return any(partie in CARTES_PAR_IDENTIFIANT for partie in parties)


def _fichiers_python() -> list[Path]:
    sortie = []
    for chemin in RACINE.rglob("*.py"):
        relatif = str(chemin.relative_to(RACINE))
        if any(partie in relatif for partie in EXCLUS):
            continue
        sortie.append(chemin)
    return sortie


def _lire_source(chemin: Path) -> str:
    """Lit un source Python en neutralisant le BOM UTF-8.

    L'interpréteur Python accepte un BOM en tête de fichier, mais
    ``ast.parse()`` sur une **chaîne** le refuse (``invalid non-printable
    character U+FEFF``). Sans ce décodage, tout fichier avec BOM était
    silencieusement ignoré par l'audit — c'était le cas de
    ``scripts/fresh_chain_supervisor.py`` et de ses clés ``ip_registry.*``.
    """
    return chemin.read_text(encoding="utf-8-sig", errors="replace")


def cles_du_fichier(chemin: Path | None = None) -> dict[str, Any]:
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
    """Chemins de configuration lus explicitement par le code.

    Reconnaît ``yaml_get`` et ses variantes typées, les accès dictionnaire
    (``cfg.get("clé")``, ``section["clé"]``), les constantes module et les
    dictionnaires de bornes/défauts. Le nom-de-feuille permissif est supprimé.
    """
    global _CLES_LUES_CACHE, _LEAF_READS, _DICT_SECTIONS, _READS_CACHE
    if (
        _CLES_LUES_CACHE is not None
        and _READS_CACHE is not None
        and _READS_CACHE[0] is _CLES_LUES_CACHE
    ):
        return _CLES_LUES_CACHE
    cles: set[str] = set()
    leaves: set[str] = set()
    dict_sections: set[str] = set()
    for chemin in _fichiers_python():
        if chemin.name == "config_audit.py":
            continue
        try:
            arbre = ast.parse(_lire_source(chemin))
        except (OSError, SyntaxError):
            continue
        consts = _collect_consts(arbre)
        leaves.update(_collect_config_map_keys(arbre))
        for noeud in ast.walk(arbre):
            if isinstance(noeud, ast.Call):
                fonction = noeud.func
                nom = getattr(fonction, "id", None) or getattr(fonction, "attr", None) or ""
                if nom == "get" and isinstance(fonction, ast.Attribute):
                    if noeud.args:
                        first = _literal_str(noeud.args[0], consts)
                        if first:
                            leaves.add(first)
                    continue
                if len(noeud.args) > 1 and _is_config_expression(noeud.args[0]):
                    key = _literal_str(noeud.args[1], consts)
                    if key:
                        leaves.add(key)
                if nom not in _READER_NAMES:
                    continue
                args = list(noeud.args)
                if not args:
                    continue
                section = _literal_str(args[0], consts)
                if not section:
                    continue
                if len(args) == 1:
                    cles.add(section)
                    continue
                if isinstance(args[1], ast.Constant) and args[1].value is None:
                    dict_sections.add(section)
                    continue
                key = _literal_str(args[1], consts)
                if not key:
                    continue
                chemin_complet = f"{section}.{key}"
                cles.add(chemin_complet)
                if len(args) > 2 and isinstance(args[2], ast.Dict):
                    dict_sections.add(chemin_complet)
            elif isinstance(noeud, ast.Subscript):
                if _is_config_expression(noeud.value):
                    key = _literal_str(noeud.slice, consts)
                    if key:
                        leaves.add(key)
    _LEAF_READS = leaves
    _DICT_SECTIONS = dict_sections
    _CLES_LUES_CACHE = cles
    _READS_CACHE = (cles, leaves, dict_sections)
    return cles


_CLES_LUES_CACHE: set[str] | None = None
_LEAF_READS: set[str] | None = None
_DICT_SECTIONS: set[str] | None = None
_READS_CACHE: tuple[set[str], set[str], set[str]] | None = None


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
                morceaux.append(_lire_source(chemin))
            except OSError:
                continue
        _SOURCE_CACHE = "\n".join(morceaux)
    return _SOURCE_CACHE


_SOURCE_CACHE: str | None = None
_AUDIT_CACHE: dict[tuple[str, float], dict[str, object]] = {}


def _cle_est_lue(cle: str, lues: set[str]) -> bool:
    """Vrai si la clé est lue explicitement (ou via un dict d'ancêtre)."""
    feuille = cle.rsplit(".", 1)[-1]
    if cle in lues or (feuille and _LEAF_READS and feuille in _LEAF_READS):
        return True
    parties = cle.split(".")
    return any(
        ".".join(parties[:i]) in (_DICT_SECTIONS or set())
        for i in range(len(parties) - 1, 0, -1)
    )


def auditer(chemin: Path | None = None) -> dict[str, Any]:
    chemin = chemin or CONFIG
    try:
        signature = (str(chemin), chemin.stat().st_mtime)
    except OSError:
        signature = (str(chemin), 0.0)
    if signature in _AUDIT_CACHE:
        return _AUDIT_CACHE[signature]

    aplaties = cles_du_fichier(chemin)
    lues_exactement = _litteraux_yaml_get()

    # [024 T224] Audit strict sur toutes les sections.
    mortes: list[str] = []
    for cle in aplaties:
        if _cle_est_lue(cle, lues_exactement):
            continue
        mortes.append(cle)

    manquantes = sorted(
        cle
        for cle in lues_exactement
        # une clé sans point est une SECTION entière (`yaml_get("server")`) :
        # légitime, ce n'est pas un réglage manquant.
        if "." in cle
        and cle not in aplaties
        and cle not in SECRET_KEYS
        and not _est_dynamique(cle)
        # une clé qui est un SOUS-ARBRE (`upstream.timeout`) est lue comme
        # section entière, pas comme feuille : légitime.
        and not any(k.startswith(cle + ".") for k in aplaties)
    )
    resultat: dict[str, object] = {
        "total_cles": len(aplaties),
        "cles_lues_par_yaml_get": len(lues_exactement),
        "dead": sorted(mortes),
        "missing": manquantes,
    }
    _AUDIT_CACHE[signature] = resultat
    return resultat


def _rapport(r: dict[str, Any]) -> None:
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
        reference: dict[str, list[str]] = {"dead": [], "missing": []}
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
