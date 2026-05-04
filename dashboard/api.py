"""
Dashboard API endpoints: stats, logs, history, config, static files.
"""

import json
import os
import asyncio
import socket
from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from config import MODELS, HOST, PORT, WEB_PORT, PROXY, API_KEY, CONFIG_KEYS, save_env, apply_port_changes, DISABLE_MAPPING, CUSTOM_ROUTES, save_custom_routes
import config.settings as config_settings
from .display import log_lines
from .events import get_event_manager
from .quota import get_quota_snapshot, get_model_limits, get_available_models, MODEL_CAPABILITIES


def _get_local_ip() -> str:
    """Get the local network IP address (not 127.0.0.1)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.254.254.254", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _build_where(from_date=None, to_date=None):
    conditions, params = [], []
    if from_date:
        conditions.append("timestamp >= ?")
        params.append(from_date)
    if to_date:
        conditions.append("timestamp <= ?")
        params.append(to_date + "T23:59:59")
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    return where, params


def register_dashboard(app, static_dir, conn, db_lock, server_manager_getter=None):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    async def root():
        from fastapi.responses import FileResponse
        return FileResponse(os.path.join(static_dir, "index.html"))

    # ── Config endpoints ──

    @app.get("/api/config")
    async def get_config():
        routes_info = {}
        for name, info in config_settings.ROUTES.items():
            routes_info[name] = {
                "match": info["match"],
                "model": info["model"],
            }
        models_info = get_available_models()
        return {
            "proxy": PROXY or "",
            "api_key_set": bool(API_KEY),
            "api_key_masked": (API_KEY[:4] + "****" + API_KEY[-4:]) if API_KEY and len(API_KEY) > 8 else ("****" if API_KEY else ""),
            "host": HOST,
            "port": PORT,
            "local_ip": _get_local_ip(),
            "web_port": WEB_PORT,
            "routes": routes_info,
            "models": models_info,
            "model_limits": get_model_limits(),
            "model_capabilities": MODEL_CAPABILITIES,
            "proxy_running": server_manager_getter().is_running if server_manager_getter and server_manager_getter() else True,
            "disable_mapping": DISABLE_MAPPING,
            "custom_routes": config_settings.CUSTOM_ROUTES,
            "go_workspace_id_set": bool(config_settings.OPENCODE_GO_WORKSPACE_ID),
            "go_workspace_id_masked": (config_settings.OPENCODE_GO_WORKSPACE_ID[:4] + "****") if config_settings.OPENCODE_GO_WORKSPACE_ID and len(config_settings.OPENCODE_GO_WORKSPACE_ID) > 4 else (config_settings.OPENCODE_GO_WORKSPACE_ID or ""),
            "go_auth_cookie_set": bool(config_settings.OPENCODE_GO_AUTH_COOKIE),
            "go_auth_cookie_masked": (config_settings.OPENCODE_GO_AUTH_COOKIE[:6] + "****") if config_settings.OPENCODE_GO_AUTH_COOKIE and len(config_settings.OPENCODE_GO_AUTH_COOKIE) > 6 else (""),
        }

    @app.get("/api/config/custom-routes")
    async def get_custom_routes():
        return config_settings.CUSTOM_ROUTES

    @app.post("/api/config/custom-routes")
    async def update_custom_routes(request: Request):
        body = await request.json()
        save_custom_routes(body)
        return {"status": "ok", "message": "Custom routes updated."}

    @app.post("/api/config")
    async def update_config(request: Request):
        body = await request.json()
        env_updates = {}

        if "routes" in body:
            for route_name, model in body["routes"].items():
                if route_name == "opus":
                    env_updates["OPUS_MAP_MODEL"] = model
                elif route_name == "sonnet":
                    env_updates["SONNET_MAP_MODEL"] = model
                elif route_name == "haiku":
                    env_updates["HAIKU_MAP_MODEL"] = model

        if "proxy" in body:
            env_updates["OPENCODE_PROXY"] = body["proxy"]

        if "disable_mapping" in body:
            env_updates["DISABLE_MAPPING"] = "1" if body["disable_mapping"] else "0"

        if "api_key" in body and body["api_key"]:
            env_updates["OPENCODE_API_KEY"] = body["api_key"]

        if "go_workspace_id" in body and body["go_workspace_id"]:
            env_updates["OPENCODE_GO_WORKSPACE_ID"] = body["go_workspace_id"]

        if "go_auth_cookie" in body and body["go_auth_cookie"]:
            env_updates["OPENCODE_GO_AUTH_COOKIE"] = body["go_auth_cookie"]

        port_changed = False
        web_port_changed = False

        if "port" in body:
            new_port = int(body["port"])
            if new_port != PORT:
                env_updates["OPENCODE_PORT"] = str(new_port)
                port_changed = True

        if "web_port" in body:
            new_web_port = int(body["web_port"])
            if new_web_port != WEB_PORT:
                env_updates["OPENCODE_WEB_PORT"] = str(new_web_port)
                web_port_changed = True

        if env_updates:
            save_env(env_updates)

        needs_restart = False
        if port_changed or web_port_changed:
            apply_port_changes(port=body.get("port"), web_port=body.get("web_port"))
            mgr = server_manager_getter() if server_manager_getter else None
            if mgr:
                try:
                    mgr.restart(port=body.get("port"), web_port=body.get("web_port"))
                except Exception as e:
                    return {
                        "status": "error",
                        "needs_restart": True,
                        "message": f"Hot-restart failed: {e}. Manual restart required.",
                    }

        return {
            "status": "ok",
            "needs_restart": needs_restart,
            "message": "Configuration updated.",
        }

    # ── Proxy control ──

    @app.get("/api/proxy/status")
    async def proxy_status():
        mgr = server_manager_getter() if server_manager_getter else None
        if mgr:
            return {"running": mgr.is_running, "port": PORT, "web_port": WEB_PORT}
        return {"running": True, "port": PORT, "web_port": WEB_PORT}

    @app.post("/api/proxy/start")
    async def proxy_start():
        mgr = server_manager_getter() if server_manager_getter else None
        if not mgr:
            return {"status": "ok", "message": "No server manager available"}
        if mgr.is_running:
            return {"status": "ok", "message": "Already running"}
        mgr.start()
        return {"status": "ok", "message": "Proxy started"}

    @app.post("/api/proxy/stop")
    async def proxy_stop():
        mgr = server_manager_getter() if server_manager_getter else None
        if not mgr:
            return {"status": "ok", "message": "No server manager available"}
        if not mgr.is_running:
            return {"status": "ok", "message": "Already stopped"}
        mgr.stop()
        return {"status": "ok", "message": "Proxy stopped"}

    # ── Stats & history ──

    @app.get("/api/stats")
    async def get_stats(from_date: str = None, to_date: str = None):
        where, params = _build_where(from_date, to_date)

        with db_lock:
            row = conn.execute(
                "SELECT COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0),"
                "       COALESCE(SUM(tokens_cache), 0), COUNT(*),"
                "       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),"
                "       SUM(CASE WHEN success = 0 OR success IS NULL THEN 1 ELSE 0 END),"
                "       COALESCE(AVG(duration_ms), 0)"
                " FROM requests " + where,
                params,
            ).fetchone()

            totals = {
                "input": row[0], "output": row[1], "cache": row[2],
                "total": row[0] + row[1] + row[2],
                "count": row[3],
                "success_count": row[4], "fail_count": row[5],
                "avg_duration_ms": int(row[6])
            }

            rows = conn.execute(
                "SELECT model, COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0),"
                "       COALESCE(SUM(tokens_cache), 0), COUNT(*),"
                "       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),"
                "       SUM(CASE WHEN success = 0 OR success IS NULL THEN 1 ELSE 0 END),"
                "       COALESCE(AVG(duration_ms), 0)"
                " FROM requests " + where +
                " GROUP BY model",
                params,
            ).fetchall()

        sum_total = totals["total"]
        models = {}
        for r in rows:
            t = r[1] + r[2] + r[3]
            models[r[0]] = {
                "input": r[1], "output": r[2], "cache": r[3], "total": t,
                "pct": f"{t/sum_total*100:.1f}%" if sum_total else "0%",
                "count": r[4], "success_count": r[5], "fail_count": r[6],
                "avg_duration_ms": int(r[7])
            }

        return {"models": models, "totals": totals}

    @app.get("/api/logs")
    async def get_logs(limit: int = 100, offset: int = 0):
        lines = list(log_lines)
        return {
            "logs": lines[offset:offset+limit],
            "total": len(lines),
            "has_more": offset + limit < len(lines)
        }

    # ── SSE events ──

    @app.get("/api/events")
    async def event_stream(request: Request):
        manager = get_event_manager()
        queue = await manager.subscribe()

        async def event_generator():
            try:
                yield f"event: connected\ndata: {json.dumps({'status': 'ok'})}\n\n"
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=30)
                        yield payload
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                await manager.unsubscribe(queue)

        return StreamingResponse(event_generator(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

    # ── Quota endpoints ──

    @app.get("/api/quotas")
    async def get_quotas():
        return get_quota_snapshot()

    # ── Stats & history ──

    @app.get("/api/history")
    async def get_history(from_date: str = None, to_date: str = None, limit: int = 20, offset: int = 0):
        where, params = _build_where(from_date, to_date)
        query = "SELECT * FROM requests " + where + " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with db_lock:
            rows = conn.execute(query, params).fetchall()
            count_query = "SELECT COUNT(*) FROM requests " + where
            total_row = conn.execute(count_query, params[:-2]).fetchone()
            total_count = total_row[0] if total_row else 0

        return {
            "logs": [
                {
                    "id": r["id"],
                    "timestamp": r["timestamp"],
                    "model": r["model"],
                    "original_model": r["original_model"],
                    "duration_ms": r["duration_ms"],
                    "tokens_input": r["tokens_input"],
                    "tokens_output": r["tokens_output"],
                    "tokens_cache": r["tokens_cache"],
                    "success": bool(r["success"]),
                    "error": r["error"],
                    "protocol": r["protocol"] if "protocol" in r.keys() else None,
                    "is_stream": bool(r["is_stream"]) if "is_stream" in r.keys() else False,
                    "thinking": r["thinking"] if "thinking" in r.keys() else None,
                    "effort": r["effort"] if "effort" in r.keys() else None,
                }
                for r in rows
            ],
            "total": total_count,
            "page": offset // limit + 1,
            "per_page": limit,
            "has_more": offset + limit < total_count
        }

    @app.delete("/api/history")
    async def delete_history(before: str = None, all: bool = False):
        with db_lock:
            if all:
                conn.execute("DELETE FROM requests")
            elif before:
                conn.execute("DELETE FROM requests WHERE timestamp < ?", (before + "T23:59:59",))
            else:
                return {"error": "Specify 'before' date or 'all=true'"}
            conn.commit()
        return {"status": "deleted"}