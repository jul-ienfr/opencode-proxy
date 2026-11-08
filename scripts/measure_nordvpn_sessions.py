"""Mesure la limite de sessions NordVPN, station par station (011).

Usage :
    python scripts/measure_nordvpn_sessions.py --dry-run
    python scripts/measure_nordvpn_sessions.py --apply

Arrête toutes les stations, attend la libération des sessions, puis démarre
1, 2, ... N stations et probe le SOCKS de chacune. Le premier échec indique la
limite observée. Aucun secret n'est affiché.

Le proxy et le superviseur doivent être arrêtés avant `--apply`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIONS = [
    ("opencode-vpn", 1080),
    ("opencode-vpn-2", 1081),
    ("opencode-vpn-3", 1082),
    ("opencode-vpn-4", 1083),
    ("opencode-vpn-5", 1084),
    ("opencode-vpn-6", 1085),
]
COOLDOWN_S = 90.0
SETTLE_S = 45.0


def _docker(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _probe(port: int, timeout_s: float = 10.0) -> dict:
    proc = subprocess.run(
        [
            "curl",
            "-s",
            "--max-time",
            str(int(timeout_s)),
            "--socks5-hostname",
            f"127.0.0.1:{port}",
            "https://api.ipify.org",
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s + 5,
        check=False,
    )
    ip = (proc.stdout or "").strip()
    return {"port": port, "ok": bool(ip), "ip": ip or None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cooldown", type=float, default=COOLDOWN_S)
    parser.add_argument("--settle", type=float, default=SETTLE_S)
    args = parser.parse_args(argv)
    if not args.apply or args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "stations": STATIONS}))
        return 0

    results = []
    _docker(["stop", *[name for name, _ in STATIONS]], timeout=120)
    time.sleep(max(0.0, args.cooldown))
    started: list[str] = []
    for index, (name, port) in enumerate(STATIONS, start=1):
        start = _docker(["start", name], timeout=60)
        started.append(name)
        if start.returncode != 0:
            results.append(
                {
                    "count": index,
                    "station": name,
                    "started": False,
                    "error": (start.stderr or "").strip()[:200],
                }
            )
            break
        time.sleep(max(0.0, args.settle))
        probes = [_probe(p) for _, p in STATIONS[:index]]
        result = {
            "count": index,
            "station": name,
            "started": True,
            "probes": probes,
            "ok": sum(1 for item in probes if item["ok"]),
        }
        results.append(result)
        if result["ok"] < index:
            break
    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
