#!/usr/bin/env python3
"""Rapport de fiabilité des requêtes free (feature 012, T027/T035).

Lecture seule de ``logs/requests.db`` et ``logs/registry.db``. Calcule les
seuils SC-001..SC-007 sur une fenêtre glissante :

- tentatives et succès ;
- requêtes client finales (via ``client_request_id`` + ``attempt_no``) ;
- latence p50/p95 des succès ;
- 429 station et IP sans succès ;
- rotations terminées (``rotation_jobs.finished_at``).

Usage :
    python scripts/request_reliability_report.py --hours 24
    python scripts/request_reliability_report.py --hours 24 --json
"""

from __future__ import annotations

import argparse
import calendar
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REQUESTS = ROOT / "logs" / "requests.db"
REGISTRY = ROOT / "logs" / "registry.db"


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * p))], 1)


def _requests_stats(con: sqlite3.Connection, since: str) -> dict[str, Any]:
    rows = list(
        con.execute(
            "SELECT id, timestamp, model, success, duration_ms, ttfb_ms, error, "
            "free_status, paid_status, client_request_id, attempt_no, final_status "
            "FROM requests WHERE timestamp >= ?",
            (since,),
        )
    )
    attempts = len(rows)
    successes = sum(1 for row in rows if int(row["success"] or 0) == 1)
    durations = [float(row["duration_ms"]) for row in rows if row["success"] and row["duration_ms"]]
    ttfb = [float(row["ttfb_ms"]) for row in rows if row["success"] and row["ttfb_ms"]]
    by_model: dict[str, dict[str, Any]] = {}
    for row in rows:
        model = str(row["model"] or "?")
        item = by_model.setdefault(model, {"attempts": 0, "success": 0})
        item["attempts"] += 1
        item["success"] += 1 if int(row["success"] or 0) == 1 else 0

    # Requêtes client finales : dernière tentative par client_request_id.
    finals: dict[str, sqlite3.Row] = {}
    for row in rows:
        cid = str(row["client_request_id"] or "").strip()
        if not cid:
            continue
        prev = finals.get(cid)
        if prev is None or int(row["attempt_no"] or 0) >= int(prev["attempt_no"] or 0):
            finals[cid] = row
    final_rows = list(finals.values())
    final_ok = sum(
        1
        for row in final_rows
        if 200 <= int(row["final_status"] or 0) < 400
        or int(row["success"] or 0) == 1
    )
    status_counts: dict[str, int] = {}
    for row in rows:
        key = str(row["free_status"] or row["paid_status"] or ("ok" if row["success"] else "?"))
        status_counts[key] = status_counts.get(key, 0) + 1
    return {
        "attempts": attempts,
        "attempt_success": successes,
        "attempt_success_pct": round(100.0 * successes / attempts, 1) if attempts else None,
        "client_requests": len(final_rows),
        "client_success": final_ok,
        "client_success_pct": round(100.0 * final_ok / len(final_rows), 1) if final_rows else None,
        "p50_success_ms": _pct(durations, 0.50),
        "p95_success_ms": _pct(durations, 0.95),
        "p50_ttfb_ms": _pct(ttfb, 0.50),
        "p95_ttfb_ms": _pct(ttfb, 0.95),
        "ttfb_samples": len(ttfb),
        "statuses": status_counts,
        "by_model": by_model,
    }


def _station_stats(con: sqlite3.Connection, since: str) -> dict[str, Any]:
    rows = list(
        con.execute(
            "SELECT ip, COUNT(*) n, SUM(status=200) ok, SUM(status=429) r429 "
            "FROM free_model_usage WHERE timestamp >= ? GROUP BY ip",
            (since,),
        )
    )
    attempts = sum(int(row["n"] or 0) for row in rows)
    ok = sum(int(row["ok"] or 0) for row in rows)
    r429 = sum(int(row["r429"] or 0) for row in rows)
    dead = [row for row in rows if int(row["ok"] or 0) == 0 and int(row["n"] or 0) >= 10]
    return {
        "attempts": attempts,
        "success": ok,
        "success_pct": round(100.0 * ok / attempts, 1) if attempts else None,
        "r429": r429,
        "r429_pct": round(100.0 * r429 / attempts, 1) if attempts else None,
        "dead_ips": len(dead),
        "dead_ip_attempts": sum(int(row["n"] or 0) for row in dead),
    }


def _rotation_stats(con: sqlite3.Connection, since_epoch: float) -> dict[str, Any]:
    try:
        rows = list(
            con.execute(
                "SELECT result, COUNT(*) n FROM rotation_jobs "
                "WHERE (finished_at IS NOT NULL AND finished_at >= ?) "
                "   OR (finished_at IS NULL AND started_at >= ?) "
                "GROUP BY result",
                (since_epoch, since_epoch),
            )
        )
    except sqlite3.Error:
        rows = []
    return {str(row["result"] or "?"): int(row["n"] or 0) for row in rows}


def _parse_utc(value: str) -> float:
    return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))


def build_report(hours: float, clean_start: str | None = None) -> dict[str, Any]:
    now = time.time()
    since_epoch = now - hours * 3600.0
    if clean_start:
        since_epoch = max(since_epoch, _parse_utc(clean_start))
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since_epoch))
    elapsed_hours = round(max(0.0, (now - since_epoch) / 3600.0), 2)
    full_window_ready = elapsed_hours >= hours - 0.01
    with _connect(REQUESTS) as con:
        requests = _requests_stats(con, since)
        stations = _station_stats(con, since)
    try:
        with _connect(REGISTRY) as con:
            rotations = _rotation_stats(con, since_epoch)
    except sqlite3.Error:
        rotations = {}
    pool_503 = int(requests["statuses"].get("503", 0))
    checks = {
        "SC-001_pool_503_zero": pool_503 == 0,
        "SC-002_attempt_success_ge_80": (requests["attempt_success_pct"] or 0) >= 80.0,
        "SC-003_dead_ip_attempts_lt_500": stations["dead_ip_attempts"] < 500,
        "SC-004_rotations_lt_150": sum(rotations.values()) < 150,
        "SC-005_ttfb_p95_lt_35s": (requests["p95_ttfb_ms"] or 0) < 35000.0,
    }
    return {
        "window_hours": hours,
        "since": since,
        "clean_start": clean_start,
        "elapsed_hours": elapsed_hours,
        "full_window_ready": full_window_ready,
        "requests": requests,
        "stations": stations,
        "rotations": rotations,
        "checks": checks,
        "verdict": "ok" if all(checks.values()) else "degraded",
    }


def _print_markdown(report: dict[str, Any]) -> None:
    req = report["requests"]
    st = report["stations"]
    print(f"# Fiabilité free — {report['window_hours']} h (depuis {report['since']})")
    print()
    print(f"- Tentatives : {req['attempts']} — succès {req['attempt_success']} "
          f"({req['attempt_success_pct']} %)")
    print(f"- Requêtes client finales : {req['client_requests']} — succès "
          f"{req['client_success']} ({req['client_success_pct']} %)")
    print(f"- Latence succès p50 / p95 : {req['p50_success_ms']} ms / "
          f"{req['p95_success_ms']} ms")
    print(f"- TTFB succès p50 / p95 : {req['p50_ttfb_ms']} ms / "
          f"{req['p95_ttfb_ms']} ms ({req['ttfb_samples']} mesures)")
    print(f"- Stations : {st['attempts']} essais — {st['success_pct']} % 200 — "
          f"{st['r429_pct']} % 429")
    print(f"- IP mortes (>=10 essais) : {st['dead_ips']} — "
          f"{st['dead_ip_attempts']} essais")
    print(f"- Rotations : {report['rotations']}")
    print(f"- Fenêtre propre : {report['elapsed_hours']} h écoulées — "
          f"{'24 h complètes' if report['full_window_ready'] else 'en cours'}")
    print()
    print("| Seuil | Verdict |")
    print("|---|---|")
    for key, ok in report["checks"].items():
        print(f"| {key} | {'OK' if ok else 'KO'} |")
    print()
    print(f"**Verdict : {report['verdict']}**")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--clean-start", default=None,
                        help="Ne pas compter avant cet instant UTC (YYYY-MM-DDTHH:MM:SSZ)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = build_report(max(0.1, float(args.hours)), clean_start=args.clean_start)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_markdown(report)
    return 0 if report["verdict"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
