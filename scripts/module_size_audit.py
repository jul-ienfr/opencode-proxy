#!/usr/bin/env python3
"""Module size audit and legacy ratchet.

- New files must be <= ``--max-lines`` (default 1500).
- With ``--ratchet``, every baseline file must not grow.
- ``--write-baseline`` records the current sizes (used to freeze legacy).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_MAX_LINES = 1500
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


def _lines(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def scan(root: Path, max_lines: int) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for path in _iter_python(root):
        rel = path.relative_to(root).as_posix()
        size = _lines(path)
        if size > 0:
            sizes[rel] = size
    return sizes


def check(
    sizes: dict[str, int],
    *,
    max_lines: int,
    baseline: dict[str, int] | None = None,
) -> list[dict[str, object]]:
    violations: list[dict[str, object]] = []
    for path, lines in sorted(sizes.items()):
        base = baseline.get(path) if baseline is not None else None
        if base is None:
            if lines > max_lines:
                violations.append({"file": path, "lines": lines, "limit": max_lines, "reason": "over-max"})
            continue
        if base > max_lines:
            # Legacy monolith: frozen at its baseline, no growth.
            if lines > base:
                violations.append({"file": path, "lines": lines, "baseline": base, "reason": "grew"})
            continue
        if lines > max_lines:
            violations.append({"file": path, "lines": lines, "limit": max_lines, "reason": "over-max"})
    return violations


def _load_baseline(path: Path) -> dict[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    files = data.get("files") if isinstance(data, dict) else None
    return {str(key): int(value) for key, value in (files or {}).items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    parser.add_argument("--ratchet", default="", help="baseline JSON path")
    parser.add_argument("--write-baseline", default="", help="write current sizes to JSON")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    sizes = scan(root, args.max_lines)
    if args.write_baseline:
        target = Path(args.write_baseline)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "max_lines": int(args.max_lines),
            "generated_from": str(root),
            "files": sizes,
        }
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if args.json:
            print(json.dumps({"written": str(target), "files": len(sizes)}, indent=2))
        else:
            print(f"wrote {target} ({len(sizes)} files)")
        return 0

    baseline = _load_baseline(Path(args.ratchet)) if args.ratchet else None
    violations = check(sizes, max_lines=args.max_lines, baseline=baseline)
    payload = {
        "files": len(sizes),
        "max_lines": int(args.max_lines),
        "ratchet": bool(baseline),
        "violations": violations,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"files={len(sizes)} max={args.max_lines} ratchet={bool(baseline)} "
            f"violations={len(violations)}"
        )
        for item in violations[:20]:
            print(f"  {item}")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
