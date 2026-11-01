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

import requests

ROOT = Path(__file__).resolve().parent.parent
SPARES = (5, 6)
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
                timeout=10,
            ).stdout
            return str(pid).encode() in out
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


def _disable_station(station: int) -> None:
    """Add a station to ip_rotation.disabled_stations during a repair chain."""
    path = ROOT / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return
    match = re.search(r"^  disabled_stations:\s*\[([^\]]*)\]\s*$", text, flags=re.MULTILINE)
    if match:
        ids = [part.strip() for part in match.group(1).split(",") if part.strip()]
        if str(station) in ids:
            return
        ids.append(str(station))
        replacement = f"  disabled_stations: [{', '.join(ids)}]"
        text = text[: match.start()] + replacement + text[match.end() :]
    else:
        block = re.search(
            r"^  disabled_stations:\s*\n((?:  - \d+\s*\n)+)",
            text,
            flags=re.MULTILINE,
        )
        if block:
            ids = [
                line.strip()[2:].strip()
                for line in block.group(1).splitlines()
                if line.strip().startswith("- ")
            ]
            if str(station) in ids:
                return
            ids.append(str(station))
            replacement = "  disabled_stations: [" + ", ".join(ids) + "]\n"
            text = text[: block.start()] + replacement + text[block.end() :]
        else:
            return
    try:
        path.write_text(text, encoding="utf-8", newline="")
        log(f"station {station} disabled during repair chain")
    except Exception as exc:
        log(f"station {station} disable failed: {type(exc).__name__}: {exc}")


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
        [
            sys.executable,
            "-X",
            "utf8",
            str(ROOT / "scripts" / "fresh_chain.py"),
            "--station",
            str(station),
            "--claims",
            "--delay",
            "8",
        ],
        cwd=str(ROOT),
        stdout=handle,
        stderr=handle,
    )
    log(f"station {station} chain started pid={proc.pid}")


def _status() -> dict:
    try:
        return requests.get("http://127.0.0.1:4000/api/vpn-status", timeout=20).json()
    except Exception:
        return {}


def _needs_chain(station: dict) -> bool:
    """A station with a real quota/bad-IP failure should be rebound."""
    if station.get("model_usable") and station.get("station_usable"):
        return False
    reason = str(station.get("model_usable_reason") or "")
    nonrout = str(station.get("non_routable_reason") or "")
    if reason in ("daily-bucket-exhausted", "bad-ip", "station-unusable"):
        return True
    if nonrout.startswith(("quota=", "bad_until", "egress=", "status=error")):
        return True
    return False


def _stop_chain(station: int) -> None:
    path, pid = _lock_state(station)
    if not pid or not _pid_alive(pid):
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                timeout=20,
            )
        else:
            os.kill(pid, 15)
        log(f"station {station} chain stopped (station healthy)")
    except Exception as exc:
        log(f"station {station} chain stop failed: {type(exc).__name__}: {exc}")
    try:
        path.unlink()
    except Exception:
        pass


def _set_env(key: str, value: str) -> None:
    path = ROOT / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        lines = []
    found = False
    for index, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[index] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    except Exception as exc:
        log(f"env update {key} failed: {type(exc).__name__}: {exc}")


def _persist_pin(station: int, winner: dict) -> None:
    host = str(winner.get("host") or "").strip()
    country = str(winner.get("country") or "").strip()
    if not host:
        return
    _set_env(f"SERVER_HOSTNAMES_STATION{station}", host)
    if country:
        _set_env(f"SERVER_COUNTRIES_STATION{station}", country)
    _enable_station(station)
    log(f"station {station} pinned to {host} ({country}) egress={winner.get('ip')}")


def main() -> None:
    log("supervisor started")
    handled: set[int] = set()
    repairing: set[int] = set()
    while True:
        try:
            data = _status()
            stations = {int(s.get("station") or 0): s for s in data.get("stations", [])}
            healthy = {
                sid
                for sid, station in stations.items()
                if station.get("model_usable") and station.get("station_usable")
            }
            # A live chain lock means the station is being repaired, even if
            # its status now says "disabled" (we disable it while rebinding).
            for sid in range(1, 7):
                _path, _pid = _lock_state(sid)
                if _pid and _pid_alive(_pid):
                    repairing.add(sid)
            for sid in list(repairing):
                if sid in healthy:
                    repairing.discard(sid)

            broken = [
                sid
                for sid, station in stations.items()
                if sid and sid not in handled and _needs_chain(station)
            ]
            # The NordVPN account drops every tunnel when several stations
            # re-authenticate at once. Repair one station at a time; the
            # others keep serving. The claims catalog prevents duplicate work.
            target = min(broken) if broken else (min(repairing) if repairing else None)
            wanted = [target] if target is not None else []

            # Every broken station searches at the same time. Chains that are
            # no longer wanted (station recovered/winner) are stopped.
            for sid in list(stations):
                if sid not in wanted:
                    _stop_chain(sid)

            for station in wanted:
                if station in handled:
                    continue
                winner = _winner(station)
                if winner:
                    _persist_pin(station, winner)
                    handled.add(station)
                    repairing.discard(station)
                    continue
                path, pid = _lock_state(station)
                if pid and _pid_alive(pid):
                    repairing.add(station)
                    continue
                if path.exists():
                    try:
                        path.unlink()
                    except Exception:
                        pass
                _start_chain(station)
                repairing.add(station)
        except Exception as exc:  # noqa: BLE001
            log(f"supervisor error: {type(exc).__name__}: {exc}")
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
