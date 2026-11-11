"""SQL projections used by dashboard routes.

Route modules keep HTTP validation and payload shaping; every SQL statement
lives here so the handlers stay free of storage details (T164/T165).
"""

from __future__ import annotations

import json
from typing import Any

STATS_TOTALS_SQL = (
    "SELECT COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0),"
    "       COALESCE(SUM(tokens_cache), 0), COUNT(*),"
    "       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),"
    "       SUM(CASE WHEN success = 0 OR success IS NULL THEN 1 ELSE 0 END),"
    "       COALESCE(AVG(duration_ms), 0)"
    " FROM requests "
)
STATS_MODELS_SQL = (
    "SELECT model, COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0),"
    "       COALESCE(SUM(tokens_cache), 0), COUNT(*),"
    "       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),"
    "       SUM(CASE WHEN success = 0 OR success IS NULL THEN 1 ELSE 0 END),"
    "       COALESCE(AVG(duration_ms), 0)"
    " FROM requests "
)
STATS_ACCOUNTS_SQL = (
    "SELECT COALESCE(NULLIF(free_model_ip, ''), account_alias, ''), COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0),"
    "       COALESCE(SUM(tokens_cache), 0), COUNT(*),"
    "       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),"
    "       SUM(CASE WHEN success = 0 OR success IS NULL THEN 1 ELSE 0 END),"
    "       COALESCE(AVG(duration_ms), 0)"
    " FROM requests "
)
RATE_COUNT_SQL = (
    "SELECT COUNT(*) FROM requests WHERE timestamp >= ? AND timestamp <= ?"
)


def stats_aggregates(
    db: Any,
    *,
    where: str,
    params: list[Any],
    station_cond: str,
    station_params: list[Any],
    b1m: str,
    b1h: str,
    now_s: str,
    need_rates: bool,
) -> tuple[Any, list[Any], list[Any], int | None, int | None]:
    totals = db.execute(STATS_TOTALS_SQL + where, params).fetchone()
    rows = db.execute(
        STATS_MODELS_SQL + where + " GROUP BY model",
        params,
    ).fetchall()
    account_rows = db.execute(
        STATS_ACCOUNTS_SQL
        + where
        + " GROUP BY COALESCE(NULLIF(free_model_ip, ''), account_alias, '')",
        params,
    ).fetchall()
    c1m = c1h = None
    if need_rates:
        c1m = db.execute(
            RATE_COUNT_SQL + station_cond,
            [b1m, now_s] + station_params,
        ).fetchone()[0]
        c1h = db.execute(
            RATE_COUNT_SQL + station_cond,
            [b1h, now_s] + station_params,
        ).fetchone()[0]
    return totals, rows, account_rows, c1m, c1h


def timeseries_rows(
    db: Any,
    *,
    where: str,
    params: list[Any],
    granularity: str,
) -> list[Any]:
    if granularity == "day":
        trunc_expr = "substr(timestamp, 1, 10)"
    elif granularity == "week":
        trunc_expr = (
            "strftime('%G', timestamp) || '-W' || strftime('%V', timestamp)"
        )
    else:
        trunc_expr = "substr(timestamp, 1, 13) || ':00'"
    return db.execute(
        f"SELECT {trunc_expr} as period, "
        "COUNT(*) as count, "
        "SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count, "
        "SUM(CASE WHEN success = 0 OR success IS NULL THEN 1 ELSE 0 END) as fail_count, "
        "COALESCE(SUM(tokens_input), 0) as input_tokens, "
        "COALESCE(SUM(tokens_output), 0) as output_tokens, "
        "COALESCE(SUM(tokens_cache), 0) as cache_tokens, "
        "COALESCE(AVG(duration_ms), 0) as avg_duration "
        "FROM requests " + where + " GROUP BY period ORDER BY period",
        params,
    ).fetchall()


def history_rows(db: Any, *, query: str, params: list[Any]) -> list[Any]:
    return db.execute(query, params).fetchall()


def history_count(db: Any, *, where: str, params: list[Any]) -> int:
    row = db.execute(
        "SELECT COUNT(*) FROM requests " + where,
        params,
    ).fetchone()
    return int(row[0]) if row else 0


def request_row(db: Any, request_id: str) -> Any:
    return db.execute(
        "SELECT * FROM requests WHERE id = ?",
        (request_id,),
    ).fetchone()


def history_filter_rows(db: Any) -> tuple[list[Any], list[Any], list[Any], list[Any]]:
    models = [
        row[0]
        for row in db.execute(
            "SELECT DISTINCT model FROM requests WHERE model IS NOT NULL ORDER BY model"
        ).fetchall()
    ]
    original_models = [
        row[0]
        for row in db.execute(
            "SELECT DISTINCT original_model FROM requests WHERE original_model IS NOT NULL ORDER BY original_model"
        ).fetchall()
    ]
    accounts = [
        row[0]
        for row in db.execute(
            "SELECT DISTINCT account_alias FROM requests WHERE account_alias IS NOT NULL ORDER BY account_alias"
        ).fetchall()
    ]
    tools_rows = db.execute(
        "SELECT tools_used FROM requests WHERE tools_used IS NOT NULL AND tools_used != '[]'"
    ).fetchall()
    return models, original_models, accounts, tools_rows


def merge_tool_names(rows: list[Any], provider: Any) -> list[str]:
    tool_set: set[str] = set()
    if callable(provider):
        try:
            tool_set.update(provider() or ())
        except Exception:
            pass
    for row in rows:
        try:
            tools = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(tools, list):
            tool_set.update(tools)
    return sorted(tool_set)


def delete_history_rows(
    connection: Any,
    *,
    before: str | None,
    delete_all: bool,
    model: str | None,
    token_usage: Any,
    token_lock: Any,
    normalize_date_bound: Any,
) -> None:
    if delete_all:
        connection.execute("DELETE FROM requests")
        if token_usage:
            with token_lock:
                for entry in token_usage.values():
                    entry["input"] = entry["output"] = entry["cache"] = 0
    elif model:
        connection.execute(
            "DELETE FROM requests WHERE model = ?",
            (model,),
        )
        if token_usage and model in token_usage:
            with token_lock:
                token_usage[model]["input"] = token_usage[model]["output"] = 0
                token_usage[model]["cache"] = 0
    elif before:
        connection.execute(
            "DELETE FROM requests WHERE timestamp < ?",
            (normalize_date_bound(before, end_of_day=True),),
        )
        if token_usage:
            rows = connection.execute(
                "SELECT model, COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0),"
                " COALESCE(SUM(tokens_cache), 0) FROM requests GROUP BY model"
            ).fetchall()
            with token_lock:
                for entry in token_usage.values():
                    entry["input"] = entry["output"] = entry["cache"] = 0
                for row in rows:
                    name = row[0]
                    if name in token_usage:
                        token_usage[name]["input"] = row[1]
                        token_usage[name]["output"] = row[2]
                        token_usage[name]["cache"] = row[3]
    connection.commit()


def cost_rows(db: Any, *, where: str, params: list[Any]) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT model, SUM(tokens_input) AS tokens_input, "
        "SUM(tokens_output) AS tokens_output FROM requests "
        + where
        + " GROUP BY model",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def free_usage_rows(
    db: Any,
    *,
    days: int,
) -> tuple[list[Any], list[Any], int]:
    where = "WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)"
    params = [f"-{days} days"]
    grouped = db.execute(
        "SELECT paid_model, free_model, api_key, workspace_id, ip,"
        "       COUNT(*) AS n,"
        "       SUM(CASE WHEN status = 200 THEN 1 ELSE 0 END) AS ok_n,"
        "       SUM(CASE WHEN status != 200 OR status IS NULL THEN 1 ELSE 0 END) AS fail_n,"
        "       COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0),"
        "       MIN(timestamp), MAX(timestamp)"
        " FROM free_model_usage " + where + " GROUP BY free_model, api_key, workspace_id, ip",
        params,
    ).fetchall()
    recent = db.execute(
        "SELECT timestamp, free_model, status, tokens_input, tokens_output, ip"
        " FROM free_model_usage " + where + " ORDER BY timestamp DESC LIMIT 100",
        params,
    ).fetchall()
    total = db.execute(
        "SELECT COUNT(*) FROM free_model_usage " + where,
        params,
    ).fetchone()
    return grouped, recent, int(total[0]) if total else 0


__all__ = [
    "cost_rows",
    "delete_history_rows",
    "free_usage_rows",
    "history_count",
    "history_filter_rows",
    "history_rows",
    "merge_tool_names",
    "request_row",
    "stats_aggregates",
    "timeseries_rows",
]
