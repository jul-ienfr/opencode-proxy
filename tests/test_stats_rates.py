"""test_stats_rates.py — Débits req/min dans Statistiques / Aperçu.

Couvre, sur DB SQLite temporaire (jamais logs/requests.db, cf. CLAUDE.md) :
  * _period_minutes : bornes absentes/inversées, clamp du futur (to=today →
    end == now, donc durée du jour = temps écoulé depuis minuit).
  * get_stats().rates : c1m/c1h glissants identiques quelle que soit la période,
    rpm_1h == c1h/60, avg_per_min == count/durée, filtre station propagé,
    période vide → avg_per_min == 0.0.
"""

import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard.api as api
from dashboard.api import _period_minutes, register_dashboard

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _ts(dt: datetime) -> str:
    return dt.strftime(_TS_FMT)


@pytest.fixture
def rates_db(tmp_path):
    """DB fichier avec schéma canonique (station + index) + lignes datées."""
    db_path = tmp_path / "requests.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS requests ("
        " id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, model TEXT NOT NULL,"
        " original_model TEXT, duration_ms INTEGER, tokens_input INTEGER,"
        " tokens_output INTEGER, tokens_cache INTEGER, success INTEGER,"
        " account_alias TEXT, tools_used TEXT, free_model_ip TEXT, station INTEGER)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON requests(timestamp)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_requests_station_ts ON requests(station, timestamp)"
    )
    now = datetime.now(UTC)
    rows = []
    # N lignes récentes (dans la minute) — station 1 sauf une (station 2).
    for i in range(3):
        rows.append((f"m{i}", _ts(now - timedelta(seconds=30)), "glm", None, 10, 5, 5, 0, 1, "a", None, 1))
    rows.append(("m3", _ts(now - timedelta(seconds=20)), "glm", None, 10, 5, 5, 0, 1, "a", None, 2))
    # M lignes dans l'heure mais hors minute.
    for i in range(5):
        rows.append(
            (f"h{i}", _ts(now - timedelta(minutes=30)), "glm", None, 10, 5, 5, 0, 1, "a", None, 1)
        )
    # Vieilles lignes hors fenêtre (dans la période 7j, hors 1h).
    for i in range(2):
        rows.append(
            (f"o{i}", _ts(now - timedelta(days=1)), "glm", None, 10, 5, 5, 0, 1, "a", None, 1)
        )
    conn.executemany(
        "INSERT INTO requests(id, timestamp, model, original_model, duration_ms,"
        " tokens_input, tokens_output, tokens_cache, success, account_alias, tools_used, station)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    yield conn, str(db_path), now
    conn.close()


@pytest.fixture
def rates_app(rates_db, tmp_path):
    conn, _db_path, _now = rates_db
    fast = FastAPI()
    (tmp_path / "index.html").write_text("<html></html>")
    register_dashboard(fast, str(tmp_path), conn)
    # Vide le cache stats entre tests (TTL 10 s sinon).
    api._stats_cache.invalidate()
    try:
        yield fast
    finally:
        api._stats_cache.invalidate()
        api._ro_db_path = ""
        api._shared_conn = None


async def _get_stats(app, **params):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/stats", params=params)
        assert r.status_code == 200, r.text
        return r.json()


class TestPeriodMinutes:
    """Durée période : clamp futur, gardes, bornes absentes."""

    def test_missing_bounds_returns_none(self):
        assert _period_minutes(None, None) is None
        assert _period_minutes("2026-09-01", None) is None
        assert _period_minutes(None, "2026-09-08") is None

    def test_inverted_custom_returns_none(self):
        assert _period_minutes("2026-09-08", "2026-09-01") is None

    def test_today_clamps_to_now(self):
        # to=today normalisé = 23:59:59 local (futur) → end_eff == now.
        now = datetime.now(UTC)
        today = now.astimezone().strftime("%Y-%m-%d")
        minutes = _period_minutes(today, today, now)
        assert minutes is not None
        elapsed = (now - now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 60.0
        assert abs(minutes - elapsed) < 1.0

    def test_full_past_day_is_1440_minutes(self):
        assert _period_minutes("2026-09-01", "2026-09-01") == pytest.approx(1440.0, abs=1.0)

    def test_invalid_strings_return_none(self):
        assert _period_minutes("nawak", "2026-09-08") is None


@pytest.mark.asyncio
async def test_sliding_counts_and_rpm(rates_app, rates_db):
    """c1m == N récent, c1h == N+M, rpm_1h == c1h/60."""
    _conn, _path, _now = rates_db
    data = await _get_stats(rates_app)
    rates = data["rates"]
    assert rates["c1m"] == 4
    assert rates["c1h"] == 9
    assert rates["rpm_1m"] == pytest.approx(4.0)
    assert rates["rpm_1h"] == pytest.approx(9 / 60.0)


@pytest.mark.asyncio
async def test_sliding_independent_of_period(rates_app, rates_db):
    """Glissant identique today vs 7j ; avg_per_min suit la période."""
    _conn, _path, now = rates_db
    today = now.astimezone().strftime("%Y-%m-%d")
    week_ago = (now - timedelta(days=7)).astimezone().strftime("%Y-%m-%d")
    d_today = await _get_stats(rates_app, from_date=today, to_date=today)
    d_week = await _get_stats(rates_app, from_date=week_ago, to_date=today)
    assert d_today["rates"]["c1m"] == d_week["rates"]["c1m"] == 4
    assert d_today["rates"]["c1h"] == d_week["rates"]["c1h"] == 9
    assert d_week["rates"]["avg_per_min"] < d_today["rates"]["avg_per_min"]
    # avg_per_min arrondi à 2 décimales côté API : tolérance absolue 0.01.
    assert d_today["rates"]["avg_per_min"] == pytest.approx(
        d_today["totals"]["count"] / d_today["rates"]["period_minutes"], abs=0.011
    )
    assert d_week["rates"]["avg_per_min"] == pytest.approx(
        d_week["totals"]["count"] / d_week["rates"]["period_minutes"], abs=0.011
    )


@pytest.mark.asyncio
async def test_station_filter_propagated(rates_app):
    """?station=1 exclut la ligne récente station 2 des comptages glissants."""
    data = await _get_stats(rates_app, station=1)
    assert data["rates"]["c1m"] == 3
    assert data["rates"]["c1h"] == 8


@pytest.mark.asyncio
async def test_empty_period_avg_is_zero(rates_app):
    """Période sans requêtes → avg_per_min == 0.0 (pas None, pas d'exception)."""
    data = await _get_stats(rates_app, from_date="2020-01-01", to_date="2020-01-02")
    # Glissant temps réel inchangé (lignes récentes toujours là)...
    assert data["rates"]["c1m"] == 4
    # ...mais la période est vide.
    assert data["totals"]["count"] == 0
    assert data["rates"]["avg_per_min"] == 0.0


@pytest.mark.asyncio
async def test_no_bounds_avg_is_none(rates_app):
    """Sans from/to → avg_per_min None (la tuile affiche —)."""
    data = await _get_stats(rates_app)
    assert data["rates"]["avg_per_min"] is None
    assert data["rates"]["period_minutes"] is None


def test_sliding_uses_timestamp_index(rates_db):
    """Les probes glissantes passent par idx_timestamp (range-scan, pas full-scan)."""
    conn, _path, _now = rates_db
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM requests"
        " WHERE timestamp >= ? AND timestamp <= ?",
        ("2026-09-08T00:00:00Z", "2026-09-08T23:59:59Z"),
    ).fetchall()
    assert any("idx_timestamp" in str(tuple(row)) for row in plan), [tuple(r) for r in plan]
