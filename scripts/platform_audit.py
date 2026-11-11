#!/usr/bin/env python3
"""Audit multiplateforme et exécution silencieuse (constitution XV/XVI).

Vérifie que le code runtime ne peut pas :

- lancer un processus sans politique silencieuse (``CREATE_NO_WINDOW`` sur
  Windows, pas de TTY sur POSIX) ;
- utiliser ``os.system``/``os.popen``/``shell=True`` ;
- coder en dur un chemin Windows ou un exécutable ``.exe`` hors adapter.

Les scripts interactifs (``scripts/``) ne sont pas scannés : ils sont lancés
par l'opérateur dans un terminal. Le service, lui, doit rester muet.

Une exception ponctuelle se justifie sur la ligne concernée avec
``# platform-audit: allow <raison>`` ; elle est visible en revue.

Usage :
    python scripts/platform_audit.py
    python scripts/platform_audit.py --strict
    python scripts/platform_audit.py --json
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIRS = (
    "core",
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
ROOT_MODULES = (
    "docker_events.py",
    "opencode.py",
    "vpn_manager.py",
)
# Le helper lui-même appelle subprocess : c'est le seul endroit autorisé.
PROCESS_HELPER = "core/process.py"
SUBPROCESS_NAMES = {"run", "Popen", "call", "check_output", "check_call"}
WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:\\")
EXE_RE = re.compile(r"\.exe\b", re.IGNORECASE)
ALLOW_MARKER = "# platform-audit: allow"


@dataclass
class Finding:
    path: str
    line: int
    code: str
    message: str


@dataclass
class AuditResult:
    findings: list[Finding] = field(default_factory=list)
    files: int = 0

    @property
    def errors(self) -> list[Finding]:
        return self.findings

    def as_dict(self) -> dict:
        return {
            "files": self.files,
            "errors": [finding.__dict__ for finding in self.findings],
            "ok": not self.findings,
        }


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory in RUNTIME_DIRS:
        base = root / directory
        if base.is_dir():
            files.extend(
                path
                for path in base.rglob("*.py")
                if "__pycache__" not in path.parts
            )
    for name in ROOT_MODULES:
        path = root / name
        if path.is_file():
            files.append(path)
    return sorted(set(files))


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _keyword(call: ast.Call, name: str) -> ast.keyword | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword
    return None


def _is_true(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _windows_literal(value: str) -> str:
    text = str(value or "")
    if WINDOWS_PATH_RE.match(text):
        return "chemin Windows en dur"
    lowered = text.lower()
    if "python.exe" in lowered:
        return ""
    if EXE_RE.search(text):
        return "exécutable .exe en dur"
    if "cmd /c start" in lowered or lowered.startswith("start "):
        return "commande qui ouvre une fenêtre"
    if "powershell.exe" in lowered and "-windowstyle hidden" not in lowered:
        return "PowerShell visible"
    return ""


def audit(root: str | Path | None = None) -> AuditResult:
    base = Path(root or ROOT).resolve()
    result = AuditResult()
    for path in _iter_files(base):
        relative = path.relative_to(base).as_posix()
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        result.files += 1
        is_helper = relative == PROCESS_HELPER
        docstrings = _docstrings(tree)
        allowed_lines = {
            index
            for index, line in enumerate(source.splitlines(), start=1)
            if ALLOW_MARKER in line
        }
        for node in ast.walk(tree):
            line = int(getattr(node, "lineno", 0) or 0)
            if line in allowed_lines:
                continue
            if isinstance(node, ast.Call):
                name = _dotted(node.func)
                if name in {f"subprocess.{item}" for item in SUBPROCESS_NAMES}:
                    if is_helper:
                        continue
                    if (
                        _keyword(node, "creationflags") is None
                        and _keyword(node, "start_new_session") is None
                    ):
                        result.findings.append(
                            Finding(
                                relative,
                                line,
                                "process-visible",
                                f"{name} sans création silencieuse (utiliser core.process.run_hidden)",
                            )
                        )
                    shell = _keyword(node, "shell")
                    if shell is not None and _is_true(shell.value):
                        result.findings.append(
                            Finding(
                                relative,
                                line,
                                "shell-true",
                                f"{name} avec shell=True interdit pour le service",
                            )
                        )
                if name in ("os.system", "os.popen"):
                    result.findings.append(
                        Finding(
                            relative,
                            line,
                            "os-shell",
                            f"{name} interdit (utiliser core.process.run_hidden)",
                        )
                    )
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                reason = _windows_literal(node.value)
                if reason:
                    result.findings.append(
                        Finding(
                            relative,
                            line,
                            "platform-hardcode",
                            f"{reason}: {node.value[:80]!r}",
                        )
                    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="compatibilité gate CI")
    args = parser.parse_args(argv)
    result = audit(args.root)
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"fichiers runtime scannés : {result.files}")
        print(f"violations                 : {len(result.findings)}")
        for finding in result.findings:
            print(f"  {finding.path}:{finding.line}: [{finding.code}] {finding.message}")
        if not result.findings:
            print("platform_audit: OK (aucune fenêtre, aucun shell, aucun chemin en dur)")
    return 1 if result.findings else 0


if __name__ == "__main__":
    sys.exit(main())
