"""Stats and time-series routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Query

from dashboard.routers import read_models


def register_stats_routes(
    app: FastAPI,
    *,
    stats_cache: Any,
    build_where: Callable[..., tuple[str, list[Any]]],
    db_read_sync: Callable[[Callable[[Any], Any]], Any],
    period_minutes: Callable[..., float | None],
    period_rates: Callable[[int, float | None], tuple[float | None, float | None]],
    timestamp_format: str,
) -> None:
    @app.get("/api/stats")
    async def get_stats(
        from_date: str = None,
        to_date: str = None,
        station: int = None,
        from_: str = Query(None, alias="from"),
        to_: str = Query(None, alias="to"),
    ):
        from_date = from_date or from_
        to_date = to_date or to_
        where, params = build_where(from_date, to_date, station=station)
        cache_key = f"stats:{where}:{tuple(params)}"

        cached = stats_cache.get(cache_key)
        if cached is not None:
            return cached

        now = datetime.now(UTC)
        now_s = now.strftime(timestamp_format)
        b1m = (now - timedelta(seconds=60)).strftime(timestamp_format)
        b1h = (now - timedelta(hours=1)).strftime(timestamp_format)
        rates_key = f"rates:{station}:{int(now.timestamp()) // 5 * 5}"
        rates = stats_cache.get(rates_key)

        station_cond, station_params = "", []
        if station is not None and str(station).strip() != "":
            try:
                station_cond, station_params = " AND station = ?", [int(station)]
            except (TypeError, ValueError):
                pass

        def _query_stats(db):
            row, rows, acct_rows, c1m, c1h = read_models.stats_aggregates(
                db,
                where=where,
                params=params,
                station_cond=station_cond,
                station_params=station_params,
                b1m=b1m,
                b1h=b1h,
                now_s=now_s,
                need_rates=rates is None,
            )

            total_input = row[0]
            total_cache = row[2]
            total_count = row[3]
            total_success = row[4]
            total_fail = row[5]
            cache_hit_rate = (
                (total_cache / total_input * 100) if total_input > 0 else 0.0
            )
            success_rate = (
                (total_success / total_count * 100) if total_count > 0 else None
            )

            totals = {
                "input": total_input,
                "output": row[1],
                "cache": total_cache,
                "total": total_input + row[1] + total_cache,
                "count": total_count,
                "success_count": total_success,
                "fail_count": total_fail,
                "avg_duration_ms": int(row[6]),
                "cache_hit_rate": round(cache_hit_rate, 1),
                "success_rate": round(success_rate, 1)
                if success_rate is not None
                else None,
            }

            return totals, rows, acct_rows, c1m, c1h

        totals, rows, acct_rows, c1m, c1h = await db_read_sync(_query_stats)

        if rates is None:
            period = period_minutes(from_date, to_date, now)
            p_min, avg_per_min = period_rates(totals["count"], period)
            rates = {
                "c1m": c1m,
                "rpm_1m": round(c1m / 1.0, 1),
                "c1h": c1h,
                "rpm_1h": round(c1h / 60.0, 2),
                "period_minutes": p_min,
                "avg_per_min": avg_per_min,
            }
            stats_cache.set(rates_key, rates, ttl=5)
        else:
            period = period_minutes(from_date, to_date, now)
            p_min, avg_per_min = period_rates(totals["count"], period)
            rates = {
                **rates,
                "period_minutes": p_min,
                "avg_per_min": avg_per_min,
            }

        sum_total = totals["total"]
        models = {}
        for row in rows:
            total = row[1] + row[2] + row[3]
            model_cache_rate = (row[3] / row[1] * 100) if row[1] > 0 else 0.0
            model_success_rate = (row[5] / row[4] * 100) if row[4] > 0 else None
            models[row[0]] = {
                "input": row[1],
                "output": row[2],
                "cache": row[3],
                "total": total,
                "pct": f"{total / sum_total * 100:.1f}%" if sum_total else "0%",
                "count": row[4],
                "success_count": row[5],
                "fail_count": row[6],
                "avg_duration_ms": int(row[7]),
                "cache_hit_rate": round(model_cache_rate, 1),
                "success_rate": round(model_success_rate, 1)
                if model_success_rate is not None
                else None,
            }

        accounts = {}
        for row in acct_rows:
            total = row[1] + row[2] + row[3]
            label = row[0] if row[0] else "(default)"
            account_cache_rate = (row[3] / row[1] * 100) if row[1] > 0 else 0.0
            account_success_rate = (row[5] / row[4] * 100) if row[4] > 0 else None
            accounts[label] = {
                "input": row[1],
                "output": row[2],
                "cache": row[3],
                "total": total,
                "pct": f"{total / sum_total * 100:.1f}%" if sum_total else "0%",
                "count": row[4],
                "success_count": row[5],
                "fail_count": row[6],
                "avg_duration_ms": int(row[7]),
                "cache_hit_rate": round(account_cache_rate, 1),
                "success_rate": round(account_success_rate, 1)
                if account_success_rate is not None
                else None,
            }

        result = {
            "models": models,
            "accounts": accounts,
            "totals": totals,
            "rates": rates,
        }
        stats_cache.set(cache_key, result, ttl=10)
        return result

    @app.get("/api/stats/timeseries")
    async def get_stats_timeseries(
        from_date: str = None,
        to_date: str = None,
        granularity: str = "hour",
        station: int = None,
    ):
        where, params = build_where(from_date, to_date, station=station)

        if granularity == "hour" and from_date and to_date:
            try:
                start = datetime.fromisoformat(str(from_date).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(to_date).replace("Z", "+00:00"))
                if start.tzinfo is not None:
                    start = start.astimezone(UTC).replace(tzinfo=None)
                if end.tzinfo is not None:
                    end = end.astimezone(UTC).replace(tzinfo=None)
                if (end - start).total_seconds() > 7 * 86400:
                    granularity = "day"
            except Exception:
                pass

        cache_key = f"timeseries:{granularity}:{where}:{tuple(params)}"
        cached = stats_cache.get(cache_key)
        if cached is not None:
            return cached

        def _query_timeseries(db):
            return read_models.timeseries_rows(
                db,
                where=where,
                params=params,
                granularity=granularity,
            )

        rows = await db_read_sync(_query_timeseries)

        result = {
            "series": [
                {
                    "period": row["period"],
                    "count": row["count"],
                    "success": row["success_count"],
                    "fail": row["fail_count"],
                    "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"],
                    "cache_tokens": row["cache_tokens"],
                    "avg_duration_ms": int(row["avg_duration"]),
                }
                for row in rows
            ],
            "granularity": granularity,
        }
        stats_cache.set(cache_key, result, ttl=10)
        return result


__all__ = ["register_stats_routes"]
