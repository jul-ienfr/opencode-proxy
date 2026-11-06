#!/usr/bin/env python3
"""Apply, prune and back up the target registry.

Usage:
    python scripts/registry_maintenance.py --once
    python scripts/registry_maintenance.py --db logs/registry.db --backup-dir logs/backups
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rotation.infrastructure.db.maintenance import run_once  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "logs" / "registry.db"))
    parser.add_argument(
        "--backup-dir", default=str(ROOT / "logs" / "backups")
    )
    parser.add_argument("--once", action="store_true", help="run once and exit")
    args = parser.parse_args()
    result = run_once(args.db, backup_dir=args.backup_dir)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("integrity_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
