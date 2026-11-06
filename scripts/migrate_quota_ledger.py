#!/usr/bin/env python3
"""Import the legacy quota_ledger.json into registry.db (once).

The JSON file stays as a `.migrated` backup; the runtime QuotaLedger now uses
the SQLite quota_buckets table.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from free.quota_ledger import QuotaLedger  # noqa: E402


def main() -> int:
    source = ROOT / "logs" / "quota_ledger.json"
    target = ROOT / "logs" / "registry.db"
    if not source.exists():
        print(f"no legacy ledger: {source}")
        return 0
    raw = json.loads(source.read_text(encoding="utf-8"))
    items = raw.get("items") if isinstance(raw, dict) else raw
    ledger = QuotaLedger(path=str(target), persist=True)
    ledger.restore_state(items or [])
    ledger.flush()
    print(f"imported {len(items or [])} buckets into {target}")
    backup = source.with_suffix(".json.migrated")
    source.replace(backup)
    print(f"legacy ledger moved to {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
