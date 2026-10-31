"""Garde-fou **source** : aucun test ne doit écrire le `config.yaml` du dépôt.

L'incident qu'il verrouille (2026-10-10) — deux tests de persistance
(`tests/test_free_model_map_http.py` et `tests/test_lot6_config.py`)
détournaient le chemin en réaffectant `api.__file__`, ce qui arrangeait
l'époque où `_persist_free_model_map` recalculait son chemin depuis
`os.path.dirname(os.path.dirname(__file__))`. Le module porte depuis une
constante unique, `dashboard.api.__CONFIG_YAML_PATH` (:208) : le détour est
devenu **inopérant** et les deux écrivains ont visé le VRAI `config.yaml`.

Le coup de grâce est le silence : **verts en exécution isolée** (les
assertions relisent ce que le test vient d'écrire), **rouges en suite
complète** (la copie `tmp_path` n'a jamais été créée) — et dans les deux cas
le fichier du dépot perdait son `free_model_map` (12 entrées → 1).

La vérification est donc **statique** (AST), et volontairement pas à
l'horloge : le `pythonw` vivant réécrit légitimement `config.yaml` (thread de
découverte, `free_discovery.interval`), donc surveiller le fichier pendant la
suite produirait des faux positifs. On vérifie la **cause** — qu'aucun test
n'adresse le chemin du dépôt.

Deux règles :

1. **Interdit** — réaffecter `__file__` d'un module `dashboard` : le détour
   historique, inopérant depuis la constante unique.
2. **Obligatoire** — tout test qui appelle un écrivain YAML réel doit le
   neutraliser, soit en remplaçant l'écrivain lui-même, soit en détournant la
   constante de chemin qu'il lit. Le détour peut vivre dans le corps du test,
   dans un helper du même fichier, ou dans une fixture qu'il demande.
"""

import ast
import os

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

# Écrivain YAML réel → constante de chemin qu'il faut détourner pour l'isoler.
WRITER_PATH_CONST = {
    "save_yaml_config": "CONFIG_PATH",
    "save_custom_routes": "CONFIG_PATH",
    "yaml_set": "CONFIG_PATH",
    "_persist_vpn_config": "__CONFIG_YAML_PATH",
    "_persist_free_model_map": "__CONFIG_YAML_PATH",
}
PATH_CONSTS = set(WRITER_PATH_CONST.values())
WRITER_NAMES = set(WRITER_PATH_CONST)

# Noms sous lesquels les tests importent `dashboard.api`.
DASHBOARD_MODULES = {"api", "api_module", "dapi", "_api"}


def _iter_test_files():
    for name in sorted(os.listdir(TESTS_DIR)):
        if name.startswith("test_") and name.endswith(".py"):
            yield name


def _scan_fn(fn):
    """(appels + params, sites setattr) d'une fonction, helpers imbriqués inclus."""
    names = set()
    sites = []
    for a in (
        list(fn.args.posonlyargs)
        + list(fn.args.args)
        + list(fn.args.kwonlyargs)
        + ([fn.args.vararg] if fn.args.vararg else [])
        + ([fn.args.kwarg] if fn.args.kwarg else [])
    ):
        names.add(a.arg)
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)
        # monkeypatch.setattr(<module>, "<const>", ...) — le 2e argument nomme
        # ce qui est remplacé (une constante de chemin ou un écrivain).
        if isinstance(func, ast.Attribute) and func.attr == "setattr":
            if len(node.args) >= 3 and isinstance(node.args[1], ast.Constant):
                mod = node.args[0]
                mod_name = getattr(mod, "id", None) or getattr(mod, "attr", None)
                sites.append((mod_name, node.args[1].value))
    return names, sites


def _resolve(funcs):
    """Propage les détours : un helper/fixture qui redirige protège ses appelants."""
    names_of, targets_of = {}, {}
    for name, fn in funcs.items():
        names, sites = _scan_fn(fn)
        names_of[name] = names
        targets_of[name] = {const for _, const in sites}
    # Borné par le nombre de fonctions : pas de point fixe infini.
    for _ in range(len(names_of) + 1):
        for name in names_of:
            after = set(targets_of[name])
            for other in names_of[name]:
                if other != name and other in targets_of:
                    after |= targets_of[other]
            targets_of[name] = after
    return names_of, targets_of


def _violations(rel, src):
    tree = ast.parse(src, filename=rel)
    funcs = {node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    names_of, targets_of = _resolve(funcs)

    bad = []
    for name in sorted(funcs):
        fn = funcs[name]
        _, sites = _scan_fn(fn)
        for mod_name, const in sites:
            if const == "__file__" and mod_name in DASHBOARD_MODULES:
                bad.append(
                    f"{rel}:{fn.lineno} — {name}() réaffecte {mod_name}.__file__ : "
                    "détour historique INOPÉRANT depuis `api.__CONFIG_YAML_PATH`"
                )
        inherited = targets_of[name]
        neutralized = inherited & WRITER_NAMES
        for writer, const in sorted(WRITER_PATH_CONST.items()):
            if writer not in names_of[name]:
                continue
            if writer in neutralized:
                continue  # l'écrivain lui-même est remplacé par un faux
            if const in inherited:
                continue  # la constante de chemin est détournée (ici, helper ou fixture)
            bad.append(
                f"{rel}:{fn.lineno} — {name}() appelle {writer}() sans détourner {const} ni remplacer l'écrivain"
            )
    return bad


@pytest.mark.parametrize("rel", list(_iter_test_files()))
def test_aucun_test_ne_crit_le_config_yaml_du_depot(rel):
    src_path = os.path.join(TESTS_DIR, rel)
    # utf-8-sig : certains fichiers de test portent une BOM (ast.parse la
    # refuse en utf-8 nu).
    with open(src_path, encoding="utf-8-sig") as f:
        src = f.read()
    bad = _violations(rel, src)
    assert not bad, "écriture réelle du config.yaml du dépôt :\n  " + "\n  ".join(bad)


def test_le_garde_fou_voit_bien_les_deux_regles():
    """Auto-contrôle : le détecteur n'est pas vide, il mord sur les deux motifs."""
    inert = (
        "def test_x(monkeypatch):\n"
        "    import dashboard.api as api\n"
        "    monkeypatch.setattr(api, '__file__', '/tmp/dashboard/api.py')\n"
        "    api._persist_free_model_map({'a': 'b'})\n"
    )
    bare = (
        "def test_y(monkeypatch):\n    import dashboard.api as api\n    api._persist_vpn_config({'quota_per_ip': 9})\n"
    )
    clean = (
        "def test_z(monkeypatch, tmp_path):\n"
        "    import dashboard.api as api\n"
        "    monkeypatch.setattr(api, '__CONFIG_YAML_PATH', str(tmp_path / 'config.yaml'))\n"
        "    api._persist_vpn_config({'quota_per_ip': 9})\n"
    )
    inert_bad = _violations("inert.py", inert)
    bare_bad = _violations("bare.py", bare)
    clean_bad = _violations("clean.py", clean)

    assert any("INOPÉRANT" in b for b in inert_bad), "le détour `__file__` doit être vu"
    assert any("sans détourner" in b for b in bare_bad), "l'écrivain nu doit être vu"
    assert clean_bad == [], f"un détour correct ne doit rien déclencher : {clean_bad}"
