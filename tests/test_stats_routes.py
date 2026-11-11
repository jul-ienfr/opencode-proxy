"""Stats route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.stats_routes import register_stats_routes


class FakeCache:
    def __init__(self):
        self.data = {}
        self.sets = []

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ttl=None):
        self.data[key] = value
        self.sets.append((key, ttl))


class FakeDB:
    def __init__(self, *, totals=None, rows=None, accounts=None, counts=(0, 0)):
        self.totals = totals or (0, 0, 0, 0, 0, 0, 0)
        self.rows = rows or []
        self.accounts = accounts or []
        self.counts = counts
        self.queries = []
        self.count_calls = 0

    def execute(self, sql, params):
        self.queries.append(sql)
        if "COALESCE(AVG(duration_ms), 0) FROM requests" in sql and "GROUP BY" not in sql:
            return FakeCursor([self.totals])
        if "GROUP BY model" in sql:
            return FakeCursor(self.rows)
        if "GROUP BY COALESCE" in sql:
            return FakeCursor(self.accounts)
        if "COUNT(*) FROM requests WHERE timestamp" in sql:
            value = self.counts[self.count_calls]
            self.count_calls += 1
            return FakeCursor([(value,)])
        raise AssertionError(f"unexpected SQL: {sql}")


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return self.rows


def _client(*, cached=None, db=None, cache=None):
    cache = cache or FakeCache()
    if cached is not None:
        cache.data.update(cached)
    calls = []

    async def db_read_sync(fn):
        calls.append(True)
        return fn(db or FakeDB())

    app = FastAPI()
    register_stats_routes(
        app,
        stats_cache=cache,
        build_where=lambda from_date, to_date, station=None: (
            " WHERE station = ?" if station is not None else "",
            [station] if station is not None else [],
        ),
        db_read_sync=db_read_sync,
        period_minutes=lambda from_date, to_date, now=None: 10.0,
        period_rates=lambda count, minutes: (minutes, round(count / minutes, 2)),
        timestamp_format="%Y-%m-%dT%H:%M:%SZ",
    )
    return TestClient(app), cache, calls


def test_stats_aggregates_models_and_accounts():
    db = FakeDB(
        totals=(100, 50, 10, 4, 3, 1, 120),
        rows=[("model-a", 100, 50, 10, 4, 3, 1, 120)],
        accounts=[("ip-1", 100, 50, 10, 4, 3, 1, 120)],
        counts=(2, 5),
    )
    client, cache, calls = _client(db=db)
    payload = client.get("/api/stats", params={"station": "2"}).json()
    assert payload["totals"]["total"] == 160
    assert payload["totals"]["success_rate"] == 75.0
    assert payload["models"]["model-a"]["pct"] == "100.0%"
    assert payload["accounts"]["ip-1"]["count"] == 4
    assert payload["rates"]["c1m"] == 2
    assert payload["rates"]["rpm_1h"] == 0.08
    assert calls == [True]
    assert any(key.startswith("stats:") for key, ttl in cache.sets if ttl == 10)
    assert any(key.startswith("rates:") for key, ttl in cache.sets if ttl == 5)


def test_stats_empty_and_cached_paths():
    client, cache, calls = _client()
    payload = client.get("/api/stats").json()
    assert payload["totals"]["success_rate"] is None
    assert payload["totals"]["cache_hit_rate"] == 0.0
    assert calls == [True]

    cached_client, _, cached_calls = _client(
        cached={"stats: WHERE station = ?:(1,)": {"cached": True}}
    )
    assert cached_client.get(
        "/api/stats", params={"station": "1"}
    ).json() == {"cached": True}
    assert cached_calls == []


def test_timeseries_forces_day_over_seven_days():
    class SeriesDB:
        def __init__(self):
            self.sql = None

        def execute(self, sql, params):
            self.sql = sql
            return FakeCursor(
                [
                    {
                        "period": "2026-01-01",
                        "count": 2,
                        "success_count": 2,
                        "fail_count": 0,
                        "input_tokens": 4,
                        "output_tokens": 5,
                        "cache_tokens": 1,
                        "avg_duration": 30,
                    }
                ]
            )

    db = SeriesDB()
    cache = FakeCache()

    async def run(fn):
        return fn(db)

    app = FastAPI()
    register_stats_routes(
        app,
        stats_cache=cache,
        build_where=lambda *args, **kwargs: ("", []),
        db_read_sync=run,
        period_minutes=lambda *args, **kwargs: None,
        period_rates=lambda count, minutes: (None, None),
        timestamp_format="%Y-%m-%dT%H:%M:%SZ",
    )
    payload = TestClient(app).get(
        "/api/stats/timeseries",
        params={
            "from_date": "2026-01-01T00:00:00Z",
            "to_date": "2026-02-01T00:00:00Z",
        },
    ).json()
    assert payload["granularity"] == "day"
    assert payload["series"][0]["avg_duration_ms"] == 30
    assert "substr(timestamp, 1, 10)" in db.sql
    assert cache.sets[-1][1] == 10
