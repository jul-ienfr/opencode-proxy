"""Architecture guard: domain has no I/O, application never imports adapters."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOMAIN = ROOT / "rotation" / "domain"
FORBIDDEN_DOMAIN = {
    "asyncio",
    "sqlite3",
    "subprocess",
    "requests",
    "httpx",
    "aiohttp",
    "docker",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_domain_has_no_io_imports():
    offenders: list[tuple[str, set[str]]] = []
    for path in DOMAIN.rglob("*.py"):
        bad = _imports(path) & FORBIDDEN_DOMAIN
        if bad:
            offenders.append((path.name, bad))
    assert offenders == [], f"domain I/O imports: {offenders}"


def test_domain_does_not_import_infrastructure():
    offenders: list[str] = []
    for path in DOMAIN.rglob("*.py"):
        source = path.read_text(encoding="utf-8", errors="replace")
        if "rotation.infrastructure" in source or "free.ip_registry.adapters" in source:
            offenders.append(path.name)
    assert offenders == [], f"domain imports adapters: {offenders}"
