#!/usr/bin/env python3
"""Import graph and cycle detector for the monolith rewrite inventory.

Scans Python files under a root, resolves internal imports, reports module
sizes, edges and import cycles.  Output is JSON or a short text summary.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "Lib",
    "logs",
    "static",
    "tests",
    ".kilo",
    ".audit-wt",
    ".audit-logs-wt",
    "node_modules",
}


def _iter_python(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*.py"):
        parts = set(path.relative_to(root).parts)
        if parts & EXCLUDED_PARTS:
            continue
        if any(part.startswith(".trou") for part in parts):
            continue
        files.append(path)
    return sorted(files)


def module_name(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative(current: str, module: str, level: int) -> str:
    package = current.split(".")[:-1] if not current.endswith(".__init__") else current.split(".")[:-1]
    # ``from .`` uses the current package; each extra level moves one parent up.
    base = package[: max(0, len(package) - (level - 1))]
    if module:
        base = [*base, module]
    return ".".join(part for part in base if part)


def imports_of(path: Path, current: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found.add(_resolve_relative(current, node.module or "", node.level))
            elif node.module:
                found.add(node.module)
    return found


def build_graph(root: Path) -> tuple[dict[str, int], dict[str, set[str]]]:
    files = _iter_python(root)
    names = {module_name(path, root): path for path in files}
    sizes = {name: len(path.read_text(encoding="utf-8-sig", errors="replace").splitlines()) for name, path in names.items()}
    graph: dict[str, set[str]] = {}
    for name, path in names.items():
        graph[name] = {item for item in imports_of(path, name) if item in names}
    return sizes, graph


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    cycles: list[list[str]] = []
    visiting: list[str] = []
    done: set[str] = set()

    def visit(node: str) -> None:
        if node in done:
            return
        if node in visiting:
            start = visiting.index(node)
            cycle = visiting[start:] + [node]
            if cycle not in cycles:
                cycles.append(cycle)
            return
        visiting.append(node)
        for child in sorted(graph.get(node, set())):
            visit(child)
        visiting.pop()
        done.add(node)

    for node in sorted(graph):
        visit(node)
    return cycles


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--cycles-only", action="store_true")
    parser.add_argument("--fail-on-cycles", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    sizes, graph = build_graph(root)
    cycles = find_cycles(graph)
    payload = {
        "files": len(sizes),
        "edges": sum(len(values) for values in graph.values()),
        "cycles": cycles,
        "largest": sorted(sizes.items(), key=lambda item: item[1], reverse=True)[:20],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif args.cycles_only:
        for cycle in cycles:
            print(" -> ".join(cycle))
    else:
        print(f"files={payload['files']} edges={payload['edges']} cycles={len(cycles)}")
        for name, lines in payload["largest"]:
            print(f"  {lines:>7}  {name}")
    return 1 if cycles and args.fail_on_cycles else 0


if __name__ == "__main__":
    raise SystemExit(main())
