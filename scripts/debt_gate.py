#!/usr/bin/env python3
"""Debt gate: fail when a P0 debt is still open.

The debt registry is `specs/002-vpn-catalog-rotation/debt.md`; statuses live
next to it in `debt_status.json` so the markdown table stays readable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEBT = ROOT / "specs" / "002-vpn-catalog-rotation" / "debt.md"
DEFAULT_STATUS = ROOT / "specs" / "002-vpn-catalog-rotation" / "debt_status.json"
_ROW = re.compile(r"^\|\s*(D-\d+)\s*\|.*\|\s*(P[0-2])\s*\|", re.MULTILINE)


def read_debts(path: Path = DEFAULT_DEBT) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return {match.group(1): match.group(2) for match in _ROW.finditer(text)}


def read_statuses(path: Path = DEFAULT_STATUS) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {str(k): str(v).lower() for k, v in (data or {}).items()}


def open_p0(
    debts: dict[str, str], statuses: dict[str, str]
) -> list[tuple[str, str]]:
    return [
        (debt_id, severity)
        for debt_id, severity in debts.items()
        if severity == "P0" and statuses.get(debt_id, "open") != "closed"
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="exit 1 on open P0")
    parser.add_argument("--debt", type=Path, default=DEFAULT_DEBT)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    args = parser.parse_args()
    debts = read_debts(args.debt)
    statuses = read_statuses(args.status)
    opened = open_p0(debts, statuses)
    if opened:
        print("P0 debt still open:")
        for debt_id, _severity in opened:
            print(f"  - {debt_id}")
        return 1 if args.strict else 0
    print(f"debt gate: {len(debts)} debts, 0 P0 open")
    return 0


if __name__ == "__main__":
    sys.exit(main())
