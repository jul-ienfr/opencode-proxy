"""config_coverage.py — couverture configuration (PC-14, audit 2026-09-08, O1).

But : chaque clé de ``config.yaml`` choisie par l'opérateur DOIT être lue
par le code ET changer observablement le comportement. Toute clé ignorée
est un bug. Ce script produit le tableau clé → statut → preuves :

  * CONSOMMÉE  : lue par le code prod ET référencée par ≥ 1 test
                 (l'effet est prouvé — exigence O1).
  * PARTIELLE  : lue par le code mais AUCUN test ne la référence
                 (effet non prouvé — ajouter un test).
  * MORTE      : aucune lecture dans le code prod (implémenter l'effet
                 OU supprimer la clé avec ADR — jamais de zombie).

Portée de recherche (note d'écart vs le plan : le plan listait
``app/ core/ vpn/ free/ config/ shared_state.py opencode.py`` ; on cherche
dans TOUS les .py prod pour éviter les faux MORTE des clés consommées par
dashboard/, ops/, protocol/… — le rapport affiche la portée exacte) :
tous les ``*.py`` sous la racine sauf tests/, docs/, scripts/ (ce script),
vendored (Lib/, .venv/, __pycache__/). Les tests cherchés sous tests/.

Sous-arbres DONNÉES exclus (pas des knobs : validés ailleurs) :
models, custom_routes, free_model_map.

Usage :
  python scripts/config_coverage.py [--config config.yaml] [--root .]
  python scripts/config_coverage.py --strict        # exit 1 si MORTE
  python scripts/config_coverage.py --baseline FILE # exit 1 si MORTE hors baseline (gate CI)

La baseline est un JSON {"morte_connue": [...], "legacy": [...]},
clés en dotted-path (ex. "ip_rotation.dual_station").
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SKIP_SUBTREES = {"models", "custom_routes", "free_model_map"}
SKIP_DIRS = {
    "docs",
    "scripts",
    ".git",
    ".venv",
    "venv",
    "Lib",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".kilo",
    "logs",
    "node_modules",
}  # NOTE : "tests" volontairement ABSENT (filtre tests_only ci-dessous).


def flatten(node, prefix=""):
    """config.yaml → [(dotted_key, value)] (feuilles + dicts vides)."""
    out = []
    if isinstance(node, dict):
        if not node and prefix:
            out.append((prefix, node))
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if path.split(".")[0] in SKIP_SUBTREES:
                continue
            out.extend(flatten(v, path))
    elif isinstance(node, list):
        if not node and prefix:
            out.append((prefix, node))
        else:
            for i, v in enumerate(node):
                if isinstance(v, (dict, list)):
                    out.extend(flatten(v, f"{prefix}[{i}]"))
                elif prefix:
                    out.append((prefix, v))
    elif prefix:
        out.append((prefix, node))
    return out


def leaf_of(dotted: str) -> str:
    return dotted.split(".")[-1].split("[")[0]


# Feuilles trop génériques pour une recherche par mot : on cherche le
# PARENT (le knob, ex. "bad_ttl_by_cause") au lieu de la feuille ("auth").
GENERIC_LEAVES = {
    "auth",
    "tls",
    "mode",
    "enabled",
    "timeout",
    "window",
    "interval",
    "retries",
    "key",
    "port",
    "host",
    "ttl",
    "delay",
    "limit",
}


def needle_for(dotted: str) -> str:
    leaf = leaf_of(dotted)
    if len(leaf) <= 4 or leaf in GENERIC_LEAVES:
        parent = dotted.rsplit(".", 1)[0]
        return leaf_of(parent) if parent else leaf
    return leaf


def iter_py_files(root: Path, tests_only: bool):
    for p in root.rglob("*.py"):
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        is_test = (
            rel.parts[0] == "tests"
            or "/tests/" in str(rel).replace("\\", "/")
            or p.name.startswith("test_")
            or p.name.endswith("_test.py")
        )
        # tests/ est exclu de SKIP_DIRS : on le filtre ici explicitement.
        if tests_only != is_test:
            continue
        yield p


def search(needle: str, files: list[Path]) -> list[str]:
    rx = re.compile(r"\b" + re.escape(needle) + r"\b")
    hits = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if rx.search(text):
            try:
                hits.append(str(f.relative_to(Path.cwd())))
            except ValueError:
                hits.append(str(f))
            if len(hits) >= 4:
                break
    return hits


def analyze(config_path: Path, root: Path):
    import yaml

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    keys = [(k, v) for k, v in flatten(cfg) if leaf_of(k)]
    # Déduplique sur le leaf (même knob à deux endroits = une ligne).
    seen: dict[str, str] = {}
    for dotted, _v in keys:
        seen.setdefault(leaf_of(dotted), dotted)
    prod_files = list(iter_py_files(root, tests_only=False))
    test_files = list(iter_py_files(root, tests_only=True))
    rows = []
    for leaf in sorted(seen):
        needle = needle_for(seen[leaf])
        code_hits = search(needle, prod_files)
        if not code_hits:
            rows.append((seen[leaf], "MORTE", [], []))
            continue
        test_hits = search(needle, test_files)
        status = "CONSOMMÉE" if test_hits else "PARTIELLE"
        rows.append((seen[leaf], status, code_hits[:3], test_hits[:3]))
    return rows, [str(p) for p in prod_files][:0]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--root", default=".")
    ap.add_argument("--strict", action="store_true", help="exit 1 si ≥ 1 clé MORTE")
    ap.add_argument(
        "--baseline", default=None, help="JSON {morte_connue|legacy: [...]} : exit 1 si MORTE hors baseline"
    )
    args = ap.parse_args(argv)
    root = Path(args.root)
    rows, _ = analyze(Path(args.config), root)
    print("| clé | statut | code | tests |")
    print("|---|---|---|---|")
    counts = {"CONSOMMÉE": 0, "PARTIELLE": 0, "MORTE": 0}
    mortes = []
    for dotted, status, code_hits, test_hits in rows:
        counts[status] += 1
        if status == "MORTE":
            mortes.append(dotted)
        print(f"| `{dotted}` | {status} | {', '.join(code_hits)} | {', '.join(test_hits)} |")
    print(f"\nTotal: {len(rows)} clés — " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    allowed: set[str] = set()
    if args.baseline:
        try:
            bl = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            allowed = set(bl.get("morte_connue", []) or []) | set(bl.get("legacy", []) or [])
        except (OSError, ValueError) as e:
            print(f"baseline illisible: {e}", file=sys.stderr)
            return 2
    unexpected = [m for m in mortes if m not in allowed and leaf_of(m) not in allowed]
    if args.strict and mortes:
        print(f"\nSTRICT: {len(mortes)} clé(s) MORTE(s)", file=sys.stderr)
        return 1
    if args.baseline and unexpected:
        print(f"\nBASELINE: {len(unexpected)} MORTE(s) hors baseline: {unexpected}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
