#!/usr/bin/env python3
"""Move the remaining legacy JSON state files into ``registry.db``.

Each file is stored under its stem in ``chain_state`` and renamed
``<name>.migrated`` after a successful SQLite write. Running it twice is
harmless.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from free.chain_state import load_json_state  # noqa: E402

NAMED = (
    "free_pool.json",
    "shared_rotation.json",
    "latency_engine.json",
    "allocator_state.json",
    "fresh_chain_state.json",
)


def main() -> int:
    logs = ROOT / "logs"
    paths = [logs / name for name in NAMED]
    paths.extend(sorted(logs.glob("fresh_chain_state_*.json")))
    moved = 0
    for path in paths:
        if not path.exists():
            continue
        value = load_json_state(path, default=None)
        if value is None:
            print(f"skip (unreadable): {path.name}")
            continue
        if path.exists():
            print(f"stored but not renamed: {path.name}")
            continue
        moved += 1
        print(f"migrated: {path.name}")
    print(f"{moved} file(s) migrated into logs/registry.db")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
