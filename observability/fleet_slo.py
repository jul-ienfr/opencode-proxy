"""SLO de flotte free — les neuf métriques du plan 019.

Lecture seule de ``logs/registry.db`` (+ journal superviseur pour les
compteurs qui n'y sont pas persistés). Aucune décision de routage n'est prise
ici : le module ne fait que mesurer, écrire un échantillon par minute dans
``slo_samples`` et rendre les lignes Prometheus.

Définitions (audit 019 §9.2) :

* ``station_client_usable`` : stations ``client_usable=1`` avec un 200 dans la
  fenêtre de 120 s — c'est la seule définition qui vaut pour le 6/6.
* ``warm_pool_live`` : IP distinctes dont ``expires_at`` est futur.
* ``rotate_committed_rate`` : jobs terminés ``ok`` / tous les jobs terminés
  sur la fenêtre (les ``started`` orphelins sont exclus après T324).
* ``resets_respected`` : 1 si aucune qualification n'a démarré entre un 429 et
  son ``reset_at``, 0 sinon.
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any

__all__ = [
    "SLO_METRICS",
    "collect",
    "collect_cached",
    "ensure_schema",
    "render_prometheus",
    "write_sample",
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "logs", "registry.db")
DEFAULT_SUPERVISOR_LOG = os.path.join(ROOT, "logs", "fresh_chain_supervisor.log")
SLO_METRICS = (
    "station_client_usable",
    "warm_pool_live",
    "ip_200_rate",
    "rotate_latency_p95",
    "app_up",
    "code_reload_total",
    "auth_attempts_total",
    "incidents_open",
    "rotation_jobs_orphans",
    "rotate_committed_rate",
    "resets_respected",
)

_CACHE: tuple[float, dict[str, float]] | None = None


def _connect(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3.0)


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _count_lines(path: str, needle: str, *, tail_bytes: int = 8_000_000) -> int:
    """Compte les lignes contenant ``needle`` dans la fin du fichier."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()  # ne pas compter une ligne tronquée
            return sum(
                1 for line in fh if needle.encode("utf-8", "ignore") in line
            )
    except OSError:
        return 0


def _app_up(host: str = "127.0.0.1", port: int = 4000) -> float:
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.5):
            return 1.0
    except OSError:
        return 0.0


def collect(
    db_path: str = DEFAULT_DB,
    supervisor_log: str = DEFAULT_SUPERVISOR_LOG,
    window_s: float = 120.0,
) -> dict[str, float]:
    """Rend les métriques mesurées. Ne lève jamais : une erreur vaut 0."""
    now = time.time()
    metrics: dict[str, float] = {name: 0.0 for name in SLO_METRICS}
    metrics["app_up"] = _app_up()
    try:
        conn = _connect(db_path)
    except sqlite3.Error:
        return metrics
    try:
        usable = _scalar(
            conn,
            "SELECT COUNT(*) FROM station_state "
            "WHERE client_usable=1 AND last_200_at>?",
            (now - window_s,),
        )
        metrics["station_client_usable"] = float(usable or 0)
        metrics["warm_pool_live"] = float(
            _scalar(
                conn,
                "SELECT COUNT(DISTINCT ip) FROM warm_pool WHERE expires_at>?",
                (now,),
            )
            or 0
        )
        metrics["incidents_open"] = float(
            _scalar(conn, "SELECT COUNT(*) FROM incidents WHERE ended_at IS NULL")
            or 0
        )
        metrics["rotation_jobs_orphans"] = float(
            _scalar(
                conn,
                "SELECT COUNT(*) FROM rotation_jobs "
                "WHERE result='started' AND started_at<?",
                (now - 300.0,),
            )
            or 0
        )
        total = _scalar(
            conn,
            "SELECT COUNT(*) FROM qualification_attempts WHERE finished_at>?",
            (now - 3600.0,),
        ) or 0
        ok = _scalar(
            conn,
            "SELECT COUNT(*) FROM qualification_attempts "
            "WHERE finished_at>? AND status='200'",
            (now - 3600.0,),
        ) or 0
        metrics["ip_200_rate"] = float(ok) / float(total) if total else 0.0
        done_ok = _scalar(
            conn,
            "SELECT COUNT(*) FROM rotation_jobs "
            "WHERE result='ok' AND finished_at>?",
            (now - 86400.0,),
        ) or 0
        done_all = _scalar(
            conn,
            "SELECT COUNT(*) FROM rotation_jobs "
            "WHERE result IN ('ok','failed','abandoned') AND finished_at>?",
            (now - 86400.0,),
        ) or 0
        metrics["rotate_committed_rate"] = (
            float(done_ok) / float(done_all) if done_all else 0.0
        )
        rows = conn.execute(
            "SELECT (finished_at-started_at) FROM rotation_jobs "
            "WHERE result IN ('ok','failed') AND finished_at IS NOT NULL "
            "AND finished_at>? AND finished_at>=started_at",
            (now - 86400.0,),
        ).fetchall()
        latencies = sorted(float(r[0]) for r in rows if r[0] is not None)
        if latencies:
            idx = min(len(latencies) - 1, int(round(0.95 * (len(latencies) - 1))))
            metrics["rotate_latency_p95"] = latencies[idx]
        violations = _scalar(
            conn,
            "SELECT COUNT(*) FROM qualification_attempts a "
            "JOIN quota_buckets b ON b.ip=a.egress_ip "
            "WHERE b.last_status='429' AND a.started_at>b.last_seen_at "
            "AND a.started_at<b.reset_at",
        )
        metrics["resets_respected"] = 0.0 if violations else 1.0
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    metrics["code_reload_total"] = float(
        _count_lines(supervisor_log, "code reload")
    )
    metrics["auth_attempts_total"] = float(
        _count_lines(supervisor_log, " AUTH")
    )
    return metrics


def collect_cached(ttl_s: float = 30.0, **kwargs) -> dict[str, float]:
    """Cache court : ``/metrics`` peut être scrapé toutes les 15 s."""
    global _CACHE
    now = time.monotonic()
    if _CACHE is not None and now - _CACHE[0] < ttl_s:
        return dict(_CACHE[1])
    values = collect(**kwargs)
    _CACHE = (now, dict(values))
    return values


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS slo_samples (
            sampled_at REAL PRIMARY KEY,
            station_client_usable INTEGER NOT NULL DEFAULT 0,
            warm_pool_live INTEGER NOT NULL DEFAULT 0,
            ip_200_rate REAL NOT NULL DEFAULT 0,
            rotate_latency_p95 REAL NOT NULL DEFAULT 0,
            app_up INTEGER NOT NULL DEFAULT 0,
            code_reload_total INTEGER NOT NULL DEFAULT 0,
            auth_attempts_total INTEGER NOT NULL DEFAULT 0,
            incidents_open INTEGER NOT NULL DEFAULT 0,
            rotation_jobs_orphans INTEGER NOT NULL DEFAULT 0,
            rotate_committed_rate REAL NOT NULL DEFAULT 0,
            resets_respected INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_slo_samples_time "
        "ON slo_samples(sampled_at)"
    )


def write_sample(db_path: str, metrics: dict[str, float], *, now: float | None = None) -> bool:
    """Écrit un échantillon (un par minute). Retourne False sur erreur."""
    ts = float(now if now is not None else time.time())
    bucket = ts - (ts % 60.0)
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            ensure_schema(conn)
            conn.execute(
                "INSERT OR REPLACE INTO slo_samples "
                "(sampled_at, station_client_usable, warm_pool_live, ip_200_rate, "
                "rotate_latency_p95, app_up, code_reload_total, auth_attempts_total, "
                "incidents_open, rotation_jobs_orphans, rotate_committed_rate, "
                "resets_respected) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bucket,
                    int(metrics.get("station_client_usable", 0)),
                    int(metrics.get("warm_pool_live", 0)),
                    float(metrics.get("ip_200_rate", 0.0)),
                    float(metrics.get("rotate_latency_p95", 0.0)),
                    int(metrics.get("app_up", 0)),
                    int(metrics.get("code_reload_total", 0)),
                    int(metrics.get("auth_attempts_total", 0)),
                    int(metrics.get("incidents_open", 0)),
                    int(metrics.get("rotation_jobs_orphans", 0)),
                    float(metrics.get("rotate_committed_rate", 0.0)),
                    int(metrics.get("resets_respected", 0)),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except sqlite3.Error:
        return False


def render_prometheus(metrics: dict[str, float]) -> str:
    lines = [
        "# HELP opencode_fleet_station_client_usable Stations client_usable in the same 120s window.",
        "# TYPE opencode_fleet_station_client_usable gauge",
    ]
    for name, value in metrics.items():
        metric = f"opencode_fleet_{name}"
        lines.append(f"{metric} {float(value):.6g}")
    return "\n".join(lines) + "\n"
