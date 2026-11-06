#!/usr/bin/env python3
"""Échantillonne /api/vpn-status (+ /metrics) et écrit un JSONL SLO.

Chaque ligne contient l'état flotte, la qualité par station (TTFB/probe,
succès, 429) et les compteurs Prometheus utiles (rotations, fallback payant,
statuts free). `station_slo_report.py` agrège le tout.

Depuis le 2026-11-04, l'échantillonneur tient aussi un journal d'incidents
SQLite : chaque transition client-utilisable -> indisponible ouvre un incident,
chaque retour ferme l'incident avec sa durée. C'est la base de calcul du SLO
de disponibilité par station.

Usage :
    python scripts/station_slo_watch.py --duration 3600
    python scripts/station_slo_watch.py --duration 0   # infini
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_QUALITY_KEYS = (
    "score",
    "ewma_ms",
    "p95_ms",
    "probe_ms",
    "probe_ok",
    "probe_age_s",
    "recent_429",
    "success",
    "errors",
    "count",
)

_INCIDENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    station       INTEGER NOT NULL,
    started_at    TEXT NOT NULL,
    started_epoch REAL NOT NULL,
    ended_at      TEXT,
    ended_epoch   REAL,
    duration_s    REAL,
    cause         TEXT,
    ip            TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_open
    ON incidents(station, ended_at);
"""


def _get_text(url: str, timeout: float = 10.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _get_json(url: str, timeout: float = 10.0) -> dict:
    return json.loads(_get_text(url, timeout=timeout))


def _metric_value(line: str) -> float:
    try:
        return float(line.rsplit(" ", 1)[1])
    except Exception:
        return 0.0


def _parse_metrics(text: str) -> dict:
    """Extrait les compteurs utiles, sans dépendance Prometheus."""
    out: dict = {
        "rotations": {},
        "fallback": {},
        "free_status": {},
        "flap": {},
    }
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        try:
            if line.startswith("vpn_rotations_total"):
                m = re.search(r'kind="([^"]+)"', line)
                if m:
                    out["rotations"][m.group(1)] = _metric_value(line)
            elif line.startswith("proxy_fallback_total"):
                leg = re.search(r'leg="([^"]+)"', line)
                cause = re.search(r'cause="([^"]+)"', line)
                if leg and cause:
                    out["fallback"][f"{leg.group(1)}:{cause.group(1)}"] = _metric_value(line)
            elif line.startswith("proxy_misc_total"):
                name = re.search(r'name="([^"]+)"', line)
                if name and name.group(1).startswith("leg_status:"):
                    out["free_status"][name.group(1)] = _metric_value(line)
            elif line.startswith("vpn_station_flap_1h"):
                sid = re.search(r'station="([^"]+)"', line)
                if sid:
                    out["flap"][sid.group(1)] = _metric_value(line)
        except Exception:
            continue
    return out


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=0x08000000,
            )
            return str(pid) in (out.stdout or "")
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _acquire_lock(path: Path) -> bool:
    """Un seul échantillonneur par machine ; un lock périmé est repris."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = int(data.get("pid") or 0)
        except Exception:
            pid = 0
        if pid and _pid_alive(pid):
            return False
        try:
            path.unlink()
        except OSError:
            return False
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        ),
        encoding="utf-8",
    )
    return True


def _open_incidents(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute(
        "SELECT id, station FROM incidents WHERE ended_at IS NULL"
    ).fetchall()
    return {int(row[1]): int(row[0]) for row in rows}


def _update_incidents(
    conn: sqlite3.Connection,
    open_ids: dict[int, int],
    stations: list[dict],
    now_epoch: float,
    at_iso: str,
) -> None:
    """Ouvre/ferme les incidents sur la transition client-utilisable."""
    for station in stations:
        try:
            sid = int(station.get("station") or 0)
        except (TypeError, ValueError):
            continue
        if sid <= 0:
            continue
        client_usable = bool(station.get("station_usable")) and bool(
            station.get("model_usable")
        )
        if not client_usable and sid not in open_ids:
            cursor = conn.execute(
                """
                INSERT INTO incidents(station, started_at, started_epoch, cause, ip)
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    sid,
                    at_iso,
                    now_epoch,
                    str(station.get("reason") or "")[:300],
                    str(station.get("ip") or ""),
                ),
            )
            open_ids[sid] = int(cursor.lastrowid)
        elif client_usable and sid in open_ids:
            row = conn.execute(
                "SELECT started_epoch FROM incidents WHERE id=?",
                (open_ids[sid],),
            ).fetchone()
            started = float(row[0]) if row else now_epoch
            conn.execute(
                """
                UPDATE incidents
                   SET ended_at=?, ended_epoch=?, duration_s=?
                 WHERE id=?
                """,
                (at_iso, now_epoch, max(0.0, now_epoch - started), open_ids[sid]),
            )
            open_ids.pop(sid, None)
    conn.commit()


def _build_row(base: str, no_metrics: bool) -> dict:
    _t0 = time.monotonic()
    s = _get_json(f"{base}/api/vpn-status")
    ms = round((time.monotonic() - _t0) * 1000.0, 1)
    q = s.get("quality") or {}
    quality = {
        "enabled": bool(q.get("enabled")),
        "stations": {
            str(sid): {k: v.get(k) for k in _QUALITY_KEYS}
            for sid, v in (q.get("stations") or {}).items()
            if isinstance(v, dict)
        },
    }
    metrics = None
    if not no_metrics:
        try:
            metrics = _parse_metrics(_get_text(f"{base}/metrics", timeout=5.0))
        except Exception as e:
            metrics = {"error": f"{type(e).__name__}: {e}"}
    return {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ms": ms,
        "status": s.get("status"),
        "healthy": s.get("healthy"),
        "total": s.get("total"),
        "routable": s.get("routable"),
        "boot_error": s.get("boot_error"),
        "stale": bool(s.get("stale")),
        "status_age_s": s.get("status_age_s"),
        "refresh_error": s.get("refresh_error"),
        "quality_enabled": bool(q.get("enabled")),
        "quality": quality,
        "metrics": metrics,
        "stations": [
            {
                "station": st.get("station"),
                "status": st.get("vpn_status"),
                "ip": st.get("current_ip"),
                "station_usable": bool(st.get("station_usable")),
                "model_usable": bool(st.get("model_usable")),
                "client_usable": bool(st.get("station_usable"))
                and bool(st.get("model_usable")),
                "flap_1h": (st.get("vpn") or {}).get("flap_count_1h"),
                "secondary": (st.get("vpn") or {}).get("secondary_path_issue"),
                "auth_failed": (st.get("vpn") or {}).get("auth_failed"),
                "total_switches": (st.get("vpn") or {}).get("total_switches"),
                "reason": st.get("non_routable_reason"),
            }
            for st in (s.get("stations") or [])
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:4000")
    ap.add_argument("--duration", type=float, default=86400.0, help="0 = infini")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--out", default="logs/station_slo.jsonl")
    ap.add_argument("--incidents-db", default="logs/station_incidents.db")
    ap.add_argument("--lock", default="logs/station_slo_watch.lock")
    ap.add_argument("--no-metrics", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    incidents_path = Path(args.incidents_db)
    if not incidents_path.is_absolute():
        incidents_path = ROOT / incidents_path
    incidents_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(args.lock)
    if not lock_path.is_absolute():
        lock_path = ROOT / lock_path
    if not _acquire_lock(lock_path):
        print(f"station_slo_watch already running ({lock_path})", flush=True)
        return 0
    base = args.api.rstrip("/")
    started = time.monotonic()
    conn = sqlite3.connect(str(incidents_path))
    try:
        conn.executescript(_INCIDENTS_SCHEMA)
        conn.commit()
        open_ids = _open_incidents(conn)
        with out.open("a", encoding="utf-8") as f:
            while True:
                try:
                    row = _build_row(base, args.no_metrics)
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    f.flush()
                    _update_incidents(
                        conn,
                        open_ids,
                        list(row.get("stations") or []),
                        time.time(),
                        str(row["at"]),
                    )
                except Exception as e:
                    f.write(
                        json.dumps(
                            {
                                "at": time.strftime(
                                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                ),
                                "error": f"{type(e).__name__}: {e}",
                            }
                        )
                        + "\n"
                    )
                    f.flush()
                if args.duration and (time.monotonic() - started) >= args.duration:
                    return 0
                time.sleep(max(5.0, float(args.interval)))
    finally:
        conn.close()
        try:
            lock_path.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
