"""Principe XVI : aucun sous-processus runtime ne doit ouvrir de fenêtre.

Échoue si un module du service lance ``subprocess.run/Popen`` sans
``creationflags=CREATE_NO_WINDOW`` (Windows) ni ``start_new_session`` (POSIX).
``core/process.py`` est le wrapper autorisé ; ``scripts/app_supervisor.py``
construit ses flags dans un dict avant l'appel.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RUNTIME_ROOTS = [
    ROOT / "opencode.py",
    ROOT / "scripts" / "fresh_chain.py",
    ROOT / "scripts" / "fresh_chain_supervisor.py",
    ROOT / "free",
    ROOT / "vpn",
    ROOT / "dashboard",
    ROOT / "observability",
]
EXCLUDED = {
    ROOT / "core" / "process.py",
    ROOT / "scripts" / "app_supervisor.py",
}
CALLS = {
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_output",
    "subprocess.check_call",
}


def _runtime_files() -> list[Path]:
    files: list[Path] = []
    for root in RUNTIME_ROOTS:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("*.py"))
    return [p for p in files if p not in EXCLUDED and "tests" not in p.parts]


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return f"{getattr(func.value, 'id', '')}.{func.attr}"
    if isinstance(func, ast.Name):
        return func.id
    return ""


def test_no_visible_subprocess_in_runtime() -> None:
    offenders: list[str] = []
    for path in _runtime_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) not in CALLS:
                continue
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            if "creationflags" not in kwargs and "start_new_session" not in kwargs:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, (
        "Sous-processus sans CREATE_NO_WINDOW/start_new_session : "
        + ", ".join(offenders)
    )
