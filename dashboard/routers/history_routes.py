"""History, request-detail and filter routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from dashboard.routers import read_models


def register_history_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    build_where: Callable[..., tuple[str, list[Any]]],
    db_read_sync: Callable[[Callable[[Any], Any]], Any],
    db_query_sync: Callable[[Callable[[], Any]], Any],
    stats_cache: Any,
    free_model_id_set: Callable[[], set[str]],
    filters_cache_getter: Callable[[], dict[str, Any] | None],
    filters_cache_setter: Callable[[dict[str, Any] | None], Any],
    tools_provider_getter: Callable[[], Any],
    connection_getter: Callable[[], Any],
    token_usage_getter: Callable[[], Any],
    token_lock_getter: Callable[[], Any],
    normalize_date_bound: Callable[..., Any],
    debug: Callable[[str], Any],
) -> None:
    def _parse_json_field(value: Any) -> Any:
        if not value or value == "[]" or value == "null":
            return None
        try:
            return json.loads(value)
        except Exception:
            return value

    @app.get("/api/history")
    async def get_history(
        from_date: str = None,
        to_date: str = None,
        limit: int = 20,
        offset: int = 0,
        status: str = None,
        model: str = None,
        original_model: str = None,
        account: str = None,
        tool: str = None,
        search: str = None,
        station: int = None,
    ):
        limit = max(1, min(int(limit or 20), 200))
        offset = max(0, int(offset or 0))
        where, params = build_where(
            from_date,
            to_date,
            status,
            model,
            original_model,
            account,
            tool,
            search,
            station=station,
        )
        query = (
            "SELECT id, timestamp, model, original_model, duration_ms, tokens_input, tokens_output, "
            "tokens_cache, success, error, protocol, is_stream, thinking, effort, client_ip, "
            "account_alias, station, free_model_ip, geo_country, geo_blocked, geo_direct_country, "
            "geo_direct_ip, geo_via_vpn, geo_allowed, tools, tools_used, free_status, paid_status "
            "FROM requests " + where + " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        def _query_history(db):
            rows = read_models.history_rows(db, query=query, params=params)
            count_key = f"histcount:{where}:{tuple(params[:-2])}"
            total_count = stats_cache.get(count_key)
            if total_count is None:
                total_count = read_models.history_count(
                    db,
                    where=where,
                    params=params[:-2],
                )
                stats_cache.set(count_key, total_count)
            return rows, total_count

        rows, total_count = await db_read_sync(_query_history)
        free_ids = free_model_id_set()

        return {
            "logs": [
                {
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "model": row["model"],
                    "original_model": row["original_model"],
                    "duration_ms": row["duration_ms"],
                    "tokens_input": row["tokens_input"],
                    "tokens_output": row["tokens_output"],
                    "tokens_cache": row["tokens_cache"],
                    "success": bool(row["success"]),
                    "error": row["error"],
                    "protocol": row["protocol"]
                    if "protocol" in row.keys()
                    else None,
                    "is_stream": bool(row["is_stream"])
                    if "is_stream" in row.keys()
                    else False,
                    "thinking": row["thinking"]
                    if "thinking" in row.keys()
                    else None,
                    "effort": row["effort"] if "effort" in row.keys() else None,
                    "client_ip": row["client_ip"]
                    if "client_ip" in row.keys()
                    else None,
                    "account_alias": row["account_alias"]
                    if "account_alias" in row.keys()
                    else None,
                    "is_free_model": (row["model"] or "") in free_ids
                    or "-free" in (row["model"] or ""),
                    "station": row["station"]
                    if "station" in row.keys()
                    else None,
                    "free_ip": row["free_model_ip"]
                    if "free_model_ip" in row.keys() and row["free_model_ip"]
                    else "",
                    "geo_country": row["geo_country"]
                    if "geo_country" in row.keys()
                    else None,
                    "geo_blocked": bool(row["geo_blocked"])
                    if "geo_blocked" in row.keys() and row["geo_blocked"]
                    else False,
                    "geo_direct_country": row["geo_direct_country"]
                    if "geo_direct_country" in row.keys()
                    else None,
                    "geo_direct_ip": row["geo_direct_ip"]
                    if "geo_direct_ip" in row.keys()
                    else None,
                    "geo_via_vpn": bool(row["geo_via_vpn"])
                    if "geo_via_vpn" in row.keys() and row["geo_via_vpn"]
                    else False,
                    "geo_allowed": row["geo_allowed"]
                    if "geo_allowed" in row.keys()
                    else None,
                    "free_status": row["free_status"]
                    if "free_status" in row.keys()
                    else None,
                    "paid_status": row["paid_status"]
                    if "paid_status" in row.keys()
                    else None,
                    "tools": json.loads(row["tools"])
                    if "tools" in row.keys()
                    and row["tools"]
                    and row["tools"] != "[]"
                    else [],
                    "tools_used": json.loads(row["tools_used"])
                    if "tools_used" in row.keys()
                    and row["tools_used"]
                    and row["tools_used"] != "[]"
                    else [],
                }
                for row in rows
            ],
            "total": total_count,
            "page": offset // limit + 1,
            "per_page": limit,
            "has_more": offset + limit < total_count,
        }

    @app.get("/api/requests/{req_id}")
    async def get_request_detail(req_id: str, request: Request):
        err = token_checker(request)
        if err:
            return err

        def _query_request(db):
            return read_models.request_row(db, req_id)

        row = await db_read_sync(_query_request)
        if not row:
            return JSONResponse(
                status_code=404,
                content={"error": "Requête non trouvée"},
            )

        return {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "model": row["model"],
            "original_model": row["original_model"],
            "duration_ms": row["duration_ms"],
            "tokens_input": row["tokens_input"],
            "tokens_output": row["tokens_output"],
            "tokens_cache": row["tokens_cache"],
            "success": bool(row["success"]),
            "error": row["error"],
            "protocol": row["protocol"] if "protocol" in row.keys() else None,
            "is_stream": bool(row["is_stream"])
            if "is_stream" in row.keys()
            else False,
            "thinking": row["thinking"] if "thinking" in row.keys() else None,
            "effort": row["effort"] if "effort" in row.keys() else None,
            "client_ip": row["client_ip"]
            if "client_ip" in row.keys()
            else None,
            "client_user_agent": row["client_user_agent"]
            if "client_user_agent" in row.keys()
            else None,
            "account_alias": row["account_alias"]
            if "account_alias" in row.keys()
            else None,
            "tools": _parse_json_field(row["tools"])
            if "tools" in row.keys()
            else [],
            "tools_used": _parse_json_field(row["tools_used"])
            if "tools_used" in row.keys()
            else [],
            "request_body": _parse_json_field(row["request_body"])
            if "request_body" in row.keys()
            else None,
            "response_body": _parse_json_field(row["response_body"])
            if "response_body" in row.keys()
            else None,
            "free_status": row["free_status"]
            if "free_status" in row.keys()
            else None,
            "paid_status": row["paid_status"]
            if "paid_status" in row.keys()
            else None,
        }

    @app.get("/api/history/filters")
    async def get_history_filters():
        cached = filters_cache_getter()
        if cached is not None:
            return cached

        def _query_filters(db):
            models, original_models, accounts, tools_rows = (
                read_models.history_filter_rows(db)
            )
            return {
                "models": models,
                "original_models": original_models,
                "accounts": accounts,
                "tools_used": read_models.merge_tool_names(
                    tools_rows,
                    tools_provider_getter(),
                ),
            }

        result = await db_read_sync(_query_filters)
        filters_cache_setter(result)
        return result

    @app.delete("/api/history")
    async def delete_history(
        before: str = None,
        all: bool = False,
        model: str = None,
    ):
        debug(f"  [history] delete: all={all} model={model} before={before}")
        if not all and not before and not model:
            return {
                "error": "Spécifiez une date 'before', un nom de 'model' ou 'all=true'"
            }

        def _delete():
            read_models.delete_history_rows(
                connection_getter(),
                before=before,
                delete_all=all,
                model=model,
                token_usage=token_usage_getter(),
                token_lock=token_lock_getter(),
                normalize_date_bound=normalize_date_bound,
            )

        await db_query_sync(_delete)
        filters_cache_setter(None)
        stats_cache.invalidate()
        return {"status": "deleted"}


__all__ = ["register_history_routes"]
