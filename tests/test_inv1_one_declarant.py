"""INV-1 — une table, un déclarant (024 T310).

Le registre ``registry.db`` est partagé par plusieurs stores (qualification,
ip_registry, ip_state, catalog claims). Autrefois chaque module déclarait sa
propre version d'une même table avec ``CREATE TABLE IF NOT EXISTS`` : le
résultat dépendait de l'ordre de boot, et les INSERT de l'autre module
échouaient sur des colonnes inconnues (mesuré : ``servers`` et ``ip_state``
avec des schémas incompatibles).

Cette gate scanne l'AST de tous les modules de **production** et échoue si une
table est déclarée par plus d'un fichier. Les DDL partagées vivent dans
``free/sql_schemas.py`` ; les stores importent la constante.

Hors périmètre : ``scripts/`` (outils jetables qui créent leurs propres bases
d'archive/bench) et ``tests/``. La gate est non circulaire : elle analyse les
constantes ``str`` exécutées, pas le résultat du code.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Modules de production à scanner (mêmes racines que la gate INV-9).
PRODUCTION_DIRS = (
    "app",
    "core",
    "config",
    "dashboard",
    "free",
    "observability",
    "ops",
    "rotation",
    "server",
    "streaming",
    "upstream",
    "vpn",
)
PRODUCTION_FILES = ("opencode.py",)

#: Bases indépendantes (archive, bench) hors production : leurs DDL ne
#: participent pas à l'autorité ``registry.db``.
EXCLUDED_DIRS = ("scripts", "tests", ".artifacts", "_research", "Lib", "logs")

_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r'(?:"?([A-Za-z_][\w]*)"?\.)?"?([A-Za-z_][\w]*)"?',
    re.IGNORECASE,
)


def _iter_production_files() -> list[Path]:
    files: list[Path] = []
    for dirname in PRODUCTION_DIRS:
        base = ROOT / dirname
        if not base.is_dir():
            continue
        files.extend(
            p for p in base.rglob("*.py") if "__pycache__" not in p.parts
        )
    for filename in PRODUCTION_FILES:
        path = ROOT / filename
        if path.is_file():
            files.append(path)
    return sorted(set(files))


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def _declared_tables(source: str, *, filename: str) -> set[str]:
    tree = ast.parse(source, filename=filename)
    docstrings = _docstring_node_ids(tree)
    tables: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        if "CREATE TABLE" not in node.value.upper():
            continue
        for match in _CREATE_TABLE.finditer(node.value):
            table = match.group(2)
            if table:
                tables.add(table.lower())
    return tables


def _declarants() -> dict[str, set[str]]:
    declarants: dict[str, set[str]] = defaultdict(set)
    for path in _iter_production_files():
        try:
            source = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tables = _declared_tables(source, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - défensif
            raise AssertionError(f"{path}: SyntaxError {exc}") from exc
        rel = path.relative_to(ROOT).as_posix()
        for table in tables:
            declarants[table].add(rel)
    return declarants


def test_gate_detects_duplicate_declarant(tmp_path: Path) -> None:
    """La gate elle-même mord : deux fichiers déclarant la même table."""
    one = tmp_path / "one.py"
    two = tmp_path / "two.py"
    one.write_text('SCHEMA = "CREATE TABLE IF NOT EXISTS sample (id INTEGER)"\n', encoding="utf-8")
    two.write_text('SCHEMA = "CREATE TABLE IF NOT EXISTS sample (id INTEGER)"\n', encoding="utf-8")
    assert _declared_tables(one.read_text(), filename=str(one)) == {"sample"}
    assert _declared_tables(two.read_text(), filename=str(two)) == {"sample"}


def test_gate_ignores_docstring_declaration(tmp_path: Path) -> None:
    good = tmp_path / "ok.py"
    good.write_text(
        '"""Mention CREATE TABLE sample (id INTEGER) only in prose."""\n'
        "VALUE = 1\n",
        encoding="utf-8",
    )
    assert _declared_tables(good.read_text(), filename=str(good)) == set()


def test_no_table_has_more_than_one_declarant() -> None:
    declarants = _declarants()
    assert declarants, "aucune table déclarée trouvée — chemins de la gate erronés"
    multi = {
        table: sorted(files)
        for table, files in declarants.items()
        if len(files) > 1
    }
    assert not multi, (
        "INV-1 violée — une table a plusieurs déclarants : "
        + ", ".join(f"{table} -> {files}" for table, files in sorted(multi.items()))
    )


def test_canonical_shared_schema_is_the_single_home() -> None:
    """Les DDL partagées sont centralisées dans ``free/sql_schemas.py``."""
    schema_file = ROOT / "free" / "sql_schemas.py"
    assert schema_file.is_file()
    declarants = _declarants()
    for table in ("servers", "pins", "slo_samples", "catalog", "claims", "rebind_budget"):
        assert table in declarants, f"{table} n'est déclarée nulle part"
        assert declarants[table] == {"free/sql_schemas.py"}, (
            f"{table} doit être déclarée une seule fois dans free/sql_schemas.py, "
            f"trouvée dans {sorted(declarants[table])}"
        )
