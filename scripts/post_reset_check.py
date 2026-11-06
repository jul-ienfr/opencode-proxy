#!/usr/bin/env python3
"""Wait for the next egress quota reset and log station recovery.

Diagnostic only: it never binds, probes, or changes a station. It polls the
proxy status after the earliest ``egress_ips.reset_at`` and writes a timestamped
line to ``logs/post_reset_check.log`` so the operator can verify that the
revived IPs actually became usable.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "logs" / "registry.db"
LOG = ROOT / "logs" / "post_reset_check.log"
STATUS = "http://127.0.0.1:4000/api/vpn-status"


def next_reset_at(
    db_path: str | Path = DB, *, now: float | None = None
) -> float | None:
    ts = time.time() if now is None else float(now)
    path = Path(db_path)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            row = conn.execute(
                "SELECT MIN(reset_at) FROM egress_ips "
                "WHERE last_429_at IS NOT NULL AND reset_at > ?",
                (ts,),
            ).fetchone()
        finally:
            conn.close()
        return float(row[0]) if row and row[0] else None
    except Exception:
        return None


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def snapshot() -> tuple[int, list[dict]]:
    try:
        with urllib.request.urlopen(STATUS, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        log(f"status unavailable: {type(exc).__name__}: {exc}")
        return 0, []
    stations = data.get("stations") or []
    usable = sum(1 for item in stations if item.get("model_usable"))
    return usable, stations


def wait_until(target: float) -> None:
    while True:
        remaining = target - time.time()
        if remaining <= 0:
            return
        time.sleep(min(300.0, remaining))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", action="store_true", help="check immediately")
    parser.add_argument("--window-s", type=float, default=1800.0)
    args = parser.parse_args()
    reset = next_reset_at()
    if reset is None:
        log("no future egress reset found — checking current state")
    else:
        log(
            "armed for reset at "
            + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset))
        )
        if not args.now:
            wait_until(reset + 30.0)
    deadline = time.time() + max(60.0, float(args.window_s))
    while time.time() < deadline:
        usable, stations = snapshot()
        summary = [
            (
                int(item.get("station") or 0),
                str(item.get("vpn_status") or ""),
                bool(item.get("model_usable")),
                str(item.get("current_ip") or ""),
            )
            for item in stations
        ]
        log(f"model_usable={usable}/{len(stations)} {summary}")
        if usable >= 6:
            log("recovery complete: 6/6 model_usable")
            return 0
        time.sleep(30.0)
    log("recovery window ended without 6/6")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
