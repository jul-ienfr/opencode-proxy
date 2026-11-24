"""INV-9 — « chemin testé = chemin livré » (024 T309).

Le défaut corrigé par T109/T309 : des modules de PRODUCTION lisaient
directement ``PYTEST_CURRENT_TEST`` pour SAUTER des chemins de production
sous pytest. Le code testé n'était alors pas le code livré.

Désormais la détection est centralisée dans ``core.testing.under_test()``.
Cette gate garantit qu'AUCUNE constante ``str`` exécutée des répertoires de
production ne contient plus ``PYTEST_CURRENT_TEST``. Seul
``core/testing.py`` (l'implémentation du point unique) est exempté.

Notes :
* Les commentaires et docstrings ne sont pas des constantes exécutées ; les
  docstrings (``ast.Constant`` premier statement d'un module/classe/fonction)
  sont donc explicitement exclus du scan.
* La gate est non circulaire : elle analyse l'AST des fichiers, pas le
  résultat de ``under_test()``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Répertoires de production à scanner.
PRODUCTION_DIRS = (
    "app",
    "scripts",
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

# Implémentation du point unique : seule exception autorisée.
EXEMPT_FILES = {(ROOT / "core" / "testing.py").resolve()}

MARKER = "PYTEST_CURRENT_TEST"


def _iter_production_files() -> list[Path]:
    """Tous les ``.py`` de production (dossiers + fichiers racine listés)."""
    files: list[Path] = []
    for dirname in PRODUCTION_DIRS:
        base = ROOT / dirname
        if not base.is_dir():
            continue
        files.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    for filename in PRODUCTION_FILES:
        path = ROOT / filename
        if path.is_file():
            files.append(path)
    # Déduplique en gardant un ordre stable et résout pour l'exemption.
    seen: set[Path] = set()
    out: list[Path] = []
    for p in sorted(files):
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        out.append(p)
    return out


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """Ids des ``ast.Constant`` qui sont des docstrings (exclus du scan)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
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


def _scan_source(source: str, *, filename: str = "<string>") -> list[tuple[int, str]]:
    """Constantes str exécutées contenant ``PYTEST_CURRENT_TEST``.

    Retourne ``[(lineno, value)]``. Les docstrings sont ignorées (elles ne
    sont pas exécutées comme des constantes de détection).
    """
    tree = ast.parse(source, filename=filename)
    docstrings = _docstring_node_ids(tree)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if id(node) in docstrings:
            continue
        value = node.value
        if isinstance(value, str) and MARKER in value:
            hits.append((getattr(node, "lineno", 0), value))
    return hits


def test_gate_scanner_catches_executable_constant(tmp_path: Path) -> None:
    """La gate elle-même mord : une constante exécutée est détectée."""
    bad = tmp_path / "bad_module.py"
    bad.write_text(
        'import os\n\nif os.getenv("PYTEST_CURRENT_TEST"):\n    pass\n',
        encoding="utf-8",
    )
    hits = _scan_source(bad.read_text(encoding="utf-8"), filename=str(bad))
    assert hits, "la gate doit détecter une lecture runtime de PYTEST_CURRENT_TEST"
    assert any(MARKER in value for _, value in hits)


def test_gate_scanner_ignores_docstring(tmp_path: Path) -> None:
    """Un docstring qui mentionne le marqueur n'est PAS une violation."""
    good = tmp_path / "ok_module.py"
    good.write_text(
        '"""Module docstring mentions PYTEST_CURRENT_TEST only."""\n\n\n'
        "def f():\n"
        '    """Also PYTEST_CURRENT_TEST in a docstring."""\n'
        "    return 1\n",
        encoding="utf-8",
    )
    hits = _scan_source(good.read_text(encoding="utf-8"), filename=str(good))
    assert hits == []


def test_no_production_module_reads_pytest_current_test_directly() -> None:
    """INV-9 : aucune constante exécutée de prod ne lit PYTEST_CURRENT_TEST."""
    violations: list[str] = []
    scanned = 0
    for path in _iter_production_files():
        if path.resolve() in EXEMPT_FILES:
            continue
        try:
            source = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - défensif
            continue
        scanned += 1
        try:
            hits = _scan_source(source, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - défensif
            violations.append(f"{path}: SyntaxError {exc}")
            continue
        for lineno, _value in hits:
            rel = path.relative_to(ROOT)
            violations.append(
                f"{rel}:{lineno} — lecture directe de {MARKER} "
                f"(utiliser core.testing.under_test())"
            )
    assert scanned > 0, "aucun fichier de production scanné — chemins de la gate erronés"
    assert not violations, (
        "INV-9 violée — chemin testé != chemin livré. "
        "Remplacer les lectures directes par core.testing.under_test() :\n"
        + "\n".join(violations)
    )


# ── Comportement de core.testing.under_test() ─────────────────────────────


def _reload_core_testing():
    """Import frais : les tests de comportement ne dépendent d'aucun cache."""
    import importlib

    import core.testing as core_testing

    return importlib.reload(core_testing)


def test_under_test_false_without_pytest_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hors pytest (variable absente) → production → False."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("OPENCODE_FORCE_PRODUCTION_PATHS", raising=False)
    assert _reload_core_testing().under_test() is False


def test_under_test_true_inside_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sous pytest sans échappatoire → True."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::test_y")
    monkeypatch.delenv("OPENCODE_FORCE_PRODUCTION_PATHS", raising=False)
    assert _reload_core_testing().under_test() is True


def test_under_test_false_when_env_forces_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OPENCODE_FORCE_PRODUCTION_PATHS=1`` → chemins de production → False."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::test_y")
    monkeypatch.setenv("OPENCODE_FORCE_PRODUCTION_PATHS", "1")
    assert _reload_core_testing().under_test() is False


def test_under_test_false_when_yaml_forces_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``debug.force_production_paths`` vrai dans la config → False."""
    import config.settings as settings

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::test_y")
    monkeypatch.delenv("OPENCODE_FORCE_PRODUCTION_PATHS", raising=False)

    original = settings.yaml_get

    def _yaml_get(section: str, key: str | None = None, default=None):
        if section == "debug" and key == "force_production_paths":
            return True
        return original(section, key, default)

    monkeypatch.setattr(settings, "yaml_get", _yaml_get)
    assert _reload_core_testing().under_test() is False


def test_under_test_never_recurses(monkeypatch: pytest.MonkeyPatch) -> None:
    """``under_test`` ne doit pas s'appeler elle-même (pas de RecursionError)."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::test_y")
    monkeypatch.delenv("OPENCODE_FORCE_PRODUCTION_PATHS", raising=False)
    result = _reload_core_testing().under_test()
    assert isinstance(result, bool)