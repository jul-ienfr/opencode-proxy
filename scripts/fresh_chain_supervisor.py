#!/usr/bin/env python3
"""Supervise the fresh-IP chains for the spare stations.

Runs one chain per spare station (5/6). A chain is started only when its lock
is absent or stale; it stops by itself on a real 200. On a winner the station
is removed from ``ip_rotation.disabled_stations`` so the proxy can serve it.

Usage:
    python scripts/fresh_chain_supervisor.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIONS = (5, 6)
INTERVAL_S = 60.0
LOG = ROOT / "logs" / "fresh_chain_supervisor.log"


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _lock_state(station: int) -> tuple[Path, int]:
    path = ROOT / "logs" / f"fresh_chain_{station}.lock"
    if not path.exists():
        return path, 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return path, int(data.get("pid") or 0)
    except Exception:
        return path, 0


def _enable_station(station: int) -> None:
    """Remove a station from ip_rotation.disabled_stations (hot reload)."""
    path = ROOT / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return
    match = re.search(r"^  disabled_stations:\s*\[([^\]]*)\]\s*$", text, flags=re.MULTILINE)
    if match:
        ids = [part.strip() for part in match.group(1).split(",") if part.strip()]
        ids = [part for part in ids if part != str(station)]
        replacement = f"  disabled_stations: [{', '.join(ids)}]"
        text = text[: match.start()] + replacement + text[match.end() :]
    else:
        block = re.search(
            r"^  disabled_stations:\s*\n((?:  - \d+\s*\n)+)",
            text,
            flags=re.MULTILINE,
        )
        if not block:
            return
        ids = [
            line.strip()[2:].strip()
            for line in block.group(1).splitlines()
            if line.strip().startswith("- ")
        ]
        ids = [part for part in ids if part != str(station)]
        replacement = "  disabled_stations: [" + ", ".join(ids) + "]\n"
        text = text[: block.start()] + replacement + text[block.end() :]
    try:
        path.write_text(text, encoding="utf-8", newline="")
        log(f"station {station} re-enabled in config.yaml")
    except Exception as exc:
        log(f"station {station} re-enable failed: {type(exc).__name__}: {exc}")


def _winner(station: int) -> dict | None:
    path = ROOT / "logs" / f"fresh_chain_state_{station}.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    winner = state.get("winner")
    return winner if isinstance(winner, dict) and winner.get("ip") else None


def _start_chain(station: int) -> None:
    log_path = ROOT / "logs" / f"fresh_chain_{station}.log"
    handle = open(log_path, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(ROOT / "scripts" / "fresh_chain.py"), "--station", str(station)],
        cwd=str(ROOT),
        stdout=handle,
        stderr=handle,
    )
    log(f"station {station} chain started pid={proc.pid}")


def main() -> None:
    log("supervisor started")
    while True:
        try:
            for station in STATIONS:
                winner = _winner(station)
                if winner:
                    log(f"station {station} winner {winner.get('host')} egress={winner.get('ip')}")
                    _enable_station(station)
                    continue
                path, pid = _lock_state(station)
                if pid and _pid_alive(pid):
                    continue
                if path.exists():
                    try:
                        path.unlink()
                    except Exception:
                        pass
                _start_chain(station)
        except Exception as exc:  # noqa: BLE001
            log(f"supervisor error: {type(exc).__name__}: {exc}")
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
