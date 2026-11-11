"""Regression guard: no country/hostname/IP range is hardcoded in selection."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    ROOT / "scripts" / "fresh_chain.py",
    ROOT / "scripts" / "fresh_chain_supervisor.py",
)
FORBIDDEN = re.compile(
    r"(al\d+\.nordvpn\.com|87\.120\.102\.|Albania|albania)",
    re.IGNORECASE,
)


def _string_literals(path: Path) -> list[str]:
    # utf-8-sig : `ast.parse` refuse le BOM quand il parse une chaîne, alors
    # que l'interpréteur Python l'accepte au chargement du fichier
    # (`fresh_chain_supervisor.py` en porte un).
    tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_no_hardcoded_albania_hosts_or_ip_range():
    offenders: list[tuple[str, str]] = []
    for path in FILES:
        for value in _string_literals(path):
            if FORBIDDEN.search(value):
                offenders.append((path.name, value))
    assert offenders == [], f"hardcoded geo values: {offenders}"


def test_no_country_quota_in_candidate_key():
    """Country must not be a hard quota in the candidate selector."""
    source = (ROOT / "scripts" / "fresh_chain.py").read_text(encoding="utf-8")
    assert "max_per_country" not in source.split("def _candidate_key", 1)[-1].split(
        "rows.sort", 1
    )[0]
