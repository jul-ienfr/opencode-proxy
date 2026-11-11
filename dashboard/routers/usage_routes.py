"""Free-model usage aggregation extracted from ``dashboard/api.py``."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI

from dashboard.routers import read_models


def build_usage_payload(
    *,
    days: int,
    grouped_rows: list[Any],
    recent_rows: list[Any],
    total_count: int,
) -> dict[str, Any]:
    by_model: dict = {}
    by_key: dict = {}
    by_workspace: dict = {}
    by_ip: dict = {}
    for row in grouped_rows:
        _paid, free, key, ws, ip, n, ok_n, fail_n, tok_in, tok_out, first_seen, last_seen = (
            row[:12] if len(row) >= 12 else (*row, "")[:12]
        )
        model = by_model.setdefault(
            free,
            {
                "requests": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "success": 0,
                "fail": 0,
            },
        )
        model["requests"] += n
        model["tokens_in"] += tok_in or 0
        model["tokens_out"] += tok_out or 0
        model["success"] += ok_n or 0
        model["fail"] += fail_n or 0
        api_key = by_key.setdefault(
            key,
            {"requests": 0, "tokens_in": 0, "tokens_out": 0},
        )
        api_key["requests"] += n
        api_key["tokens_in"] += tok_in or 0
        api_key["tokens_out"] += tok_out or 0
        workspace = by_workspace.setdefault(
            ws,
            {"requests": 0, "tokens_in": 0, "tokens_out": 0},
        )
        workspace["requests"] += n
        workspace["tokens_in"] += tok_in or 0
        workspace["tokens_out"] += tok_out or 0
        if ip:
            ip_data = by_ip.setdefault(
                ip,
                {
                    "requests": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                    "success": 0,
                    "fail": 0,
                    "reset_at": None,
                    "available": False,
                },
            )
            ip_data["requests"] += n
            ip_data["tokens_in"] += tok_in or 0
            ip_data["tokens_out"] += tok_out or 0
            ip_data["success"] += ok_n or 0
            ip_data["fail"] += fail_n or 0

    timeline = [
        {
            "ts": ts,
            "model": free,
            "status": status,
            "tokens": (tok_in or 0) + (tok_out or 0),
            "ip": ip,
        }
        for ts, free, status, tok_in, tok_out, ip in recent_rows
    ]

    quota_window_hours = 48
    now = datetime.now(UTC).replace(tzinfo=None)
    for ip_data in by_ip.values():
        try:
            last = datetime.fromisoformat(ip_data["last_seen"])
            if last.tzinfo is not None:
                last = last.astimezone(UTC).replace(tzinfo=None)
            reset_at = last + timedelta(hours=quota_window_hours)
            ip_data["reset_at"] = reset_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            ip_data["available"] = now >= reset_at
            ip_data["reset_in_sec"] = max(0, int((reset_at - now).total_seconds()))
        except Exception:
            ip_data["reset_at"] = None
            ip_data["available"] = True
            ip_data["reset_in_sec"] = 0

    return {
        "days": days,
        "total_requests": total_count,
        "by_model": by_model,
        "by_key": by_key,
        "by_workspace": by_workspace,
        "by_ip": by_ip,
        "recent": timeline[:100],
    }


def register_usage_routes(
    app: FastAPI,
    *,
    db_read_sync: Callable[..., Any],
) -> None:
    @app.get("/api/free-model-usage")
    async def get_free_model_usage(days: int = 7):
        def _query(db):
            return read_models.free_usage_rows(db, days=days)

        grouped_rows, recent_rows, total_count = await db_read_sync(_query)
        return build_usage_payload(
            days=days,
            grouped_rows=grouped_rows,
            recent_rows=recent_rows,
            total_count=total_count,
        )


__all__ = ["build_usage_payload", "register_usage_routes"]
