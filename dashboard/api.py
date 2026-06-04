"""
Dashboard API endpoints: stats, logs, history, config, static files.
"""

import json
import os
import time
import asyncio
import socket
from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from config import MODELS, HOST, PORT, WEB_PORT, PROXY, API_KEY, CONFIG_KEYS, save_env, apply_server_changes, CUSTOM_ROUTES, save_custom_routes, API_KEYS, save_api_keys, API_KEY_ROUTING
import config.settings as config_settings
from .display import log_lines, debug as _debug
from .events import get_event_manager
from .quota import get_quota_snapshot, get_available_models, get_model_limits_for_all, get_model_capabilities_for_all

# Tools that work on all models — hidden from routing UI by default
UNIVERSAL_TOOLS = {"Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebSearch"}


# ── Simple TTL cache for expensive dashboard queries ──

class _TTLCache:
    """In-memory cache with TTL for reducing redundant DB scans."""
    def __init__(self, ttl: float = 10.0):
        self._ttl = ttl
        self._store: dict[str, tuple[float, any]] = {}

    def get(self, key: str):
        entry = self._store.get(key)
        if entry and (time.monotonic() - entry[0]) < self._ttl:
            return entry[1]
        return None

    def set(self, key: str, value):
        self._store[key] = (time.monotonic(), value)

    def invalidate(self, key: str | None = None):
        if key:
            self._store.pop(key, None)
        else:
            self._store.clear()

_stats_cache = _TTLCache(ttl=10.0)
_tools_cache = _TTLCache(ttl=30.0)

# Cache for local IP resolution (rarely changes)
_local_ips_cache: tuple[float, list] | None = None


async def _db_query_sync(fn):
    """Run a synchronous DB function in a thread pool to avoid blocking the event loop."""
    return await asyncio.to_thread(fn)


def _get_local_ips() -> list:
    """Get all local network IP addresses (excluding loopback). Cached 60s."""
    global _local_ips_cache
    now = time.monotonic()
    if _local_ips_cache and (now - _local_ips_cache[0]) < 60.0:
        return _local_ips_cache[1]
    ips = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            if addr and not addr.startswith("127.") and '.' in addr:
                if addr not in ips:
                    ips.append(addr)
    except Exception:
        pass
    # Fallback: try to determine primary IP via connection
    if not ips:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.254.254.254", 1))
            ips.append(s.getsockname()[0])
        except Exception:
            pass
        finally:
            s.close()
    _local_ips_cache = (now, ips)
    return ips


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


def daysAgo(n: int) -> str:
    """Return date string for N days ago (en-CA format YYYY-MM-DD)."""
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def register_dashboard(app, static_dir, conn, server_manager_getter=None, token_usage=None, token_lock=None):
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
            "local_ips": await asyncio.to_thread(_get_local_ips),
            "web_port": WEB_PORT,
            "routes": routes_info,
            "models": models_info,
            "model_limits": get_model_limits_for_all(models_info),
            "model_capabilities": get_model_capabilities_for_all(models_info),
            "proxy_running": server_manager_getter().is_running if server_manager_getter and server_manager_getter() else True,
            "disable_mapping": config_settings.DISABLE_MAPPING,
            "custom_routes": config_settings.CUSTOM_ROUTES,
            "go_workspace_id_set": bool(config_settings.OPENCODE_GO_WORKSPACE_ID),
            "go_workspace_id_masked": (config_settings.OPENCODE_GO_WORKSPACE_ID[:4] + "****") if config_settings.OPENCODE_GO_WORKSPACE_ID and len(config_settings.OPENCODE_GO_WORKSPACE_ID) > 4 else (config_settings.OPENCODE_GO_WORKSPACE_ID or ""),
            "go_auth_cookie_set": bool(config_settings.OPENCODE_GO_AUTH_COOKIE),
            "go_auth_cookie_masked": (config_settings.OPENCODE_GO_AUTH_COOKIE[:6] + "****") if config_settings.OPENCODE_GO_AUTH_COOKIE and len(config_settings.OPENCODE_GO_AUTH_COOKIE) > 6 else (""),
            "api_keys": [
                {
                    "api_key_masked": (k["api_key"][:4] + "****" + k["api_key"][-4:]) if len(k.get("api_key", "")) > 8 else "****",
                    "api_key": k.get("api_key", ""),
                    "go_workspace_id_masked": (k.get("go_workspace_id", "")[:4] + "****") if len(k.get("go_workspace_id", "")) > 4 else "",
                    "go_auth_cookie_masked": (k.get("go_auth_cookie", "")[:6] + "****") if len(k.get("go_auth_cookie", "")) > 6 else "",
                    "go_workspace_id": k.get("go_workspace_id", ""),
                    "go_auth_cookie": k.get("go_auth_cookie", ""),
                    "enabled": k.get("enabled", True),
                    "alias": k.get("alias", ""),
                }
                for k in API_KEYS
            ],
            "routing": API_KEY_ROUTING,
            "lang": os.getenv("PROXY_LANG", "en"),
        }

    @app.get("/api/config/custom-routes")
    async def get_custom_routes():
        return config_settings.CUSTOM_ROUTES

    @app.post("/api/config/custom-routes")
    async def update_custom_routes(request: Request):
        body = await request.json()
        _debug(f"  [config] custom routes updated ({len(body)} routes)")
        save_custom_routes(body)
        return {"status": "ok", "message": "Custom routes updated."}

    @app.get("/api/config/api-keys")
    async def get_api_keys_config():
        return {
            "api_keys": [
                {
                    "api_key_masked": (k["api_key"][:4] + "****" + k["api_key"][-4:]) if len(k.get("api_key", "")) > 8 else "****",
                    "api_key": k.get("api_key", ""),
                    "has_go_workspace": bool(k.get("go_workspace_id")),
                    "has_go_cookie": bool(k.get("go_auth_cookie")),
                    "go_workspace_id": k.get("go_workspace_id", ""),
                    "go_auth_cookie": k.get("go_auth_cookie", ""),
                    "enabled": k.get("enabled", True),
                    "alias": k.get("alias", ""),
                }
                for k in API_KEYS
            ],
            "count": len(API_KEYS),
            "routing": API_KEY_ROUTING,
        }

    @app.post("/api/config/api-keys")
    async def update_api_keys_config(request: Request):
        body = await request.json()
        if "api_keys" in body:
            _debug(f"  [config] API keys updated ({len(body['api_keys'])} keys)")
            save_api_keys(body["api_keys"])
        if "routing" in body:
            save_env({"API_KEY_ROUTING": body["routing"]})
        return {"status": "ok", "message": "API keys saved."}

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

        if "routing" in body:
            env_updates["API_KEY_ROUTING"] = body["routing"]

        if "lang" in body:
            env_updates["PROXY_LANG"] = body["lang"]

        restart_needed = False

        if "port" in body:
            try:
                new_port = int(body["port"])
            except (ValueError, TypeError):
                return {"status": "error", "message": "Invalid port value"}
            if not (1 <= new_port <= 65535):
                return {"status": "error", "message": "Port must be between 1 and 65535"}
            if new_port != PORT:
                env_updates["OPENCODE_PORT"] = str(new_port)
                restart_needed = True

        if "web_port" in body:
            try:
                new_web_port = int(body["web_port"])
            except (ValueError, TypeError):
                return {"status": "error", "message": "Invalid web port value"}
            if not (1 <= new_web_port <= 65535):
                return {"status": "error", "message": "Web port must be between 1 and 65535"}
            if new_web_port != WEB_PORT:
                env_updates["OPENCODE_WEB_PORT"] = str(new_web_port)
                restart_needed = True

        if "host" in body:
            new_host = body["host"]
            if new_host != HOST:
                env_updates["OPENCODE_HOST"] = new_host
                restart_needed = True

        if env_updates:
            save_env(env_updates)

        if restart_needed:
            apply_server_changes(
                port=body.get("port"),
                web_port=body.get("web_port"),
                host=body.get("host"),
            )
            mgr = server_manager_getter() if server_manager_getter else None
            if mgr:
                try:
                    mgr.restart(
                        port=body.get("port"),
                        web_port=body.get("web_port"),
                        host=body.get("host"),
                    )
                except Exception as e:
                    return {
                        "status": "error",
                        "needs_restart": True,
                        "message": f"Hot-restart failed: {e}. Manual restart required.",
                    }

        return {
            "status": "ok",
            "needs_restart": False,
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

    @app.post("/api/proxy/restart")
    async def proxy_restart(full: bool = False):
        mgr = server_manager_getter() if server_manager_getter else None
        if not mgr:
            return {"status": "error", "message": "No server manager available"}
        if full:
            # Schedule full restart after response is sent
            loop = asyncio.get_event_loop()
            loop.call_soon(mgr.full_restart)
            return {"status": "ok", "message": "Full restart triggered"}
        mgr.restart()
        return {"status": "ok", "message": "Proxy restarted"}

    # ── Stats & history ──

    @app.get("/api/stats")
    async def get_stats(from_date: str = None, to_date: str = None):
        where, params = _build_where(from_date, to_date)
        cache_key = f"stats:{where}:{tuple(params)}"

        # Return cached result if fresh (< 10s old)
        cached = _stats_cache.get(cache_key)
        if cached is not None:
            return cached

        def _query_stats():
            row = conn.execute(
                "SELECT COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0),"
                "       COALESCE(SUM(tokens_cache), 0), COUNT(*),"
                "       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),"
                "       SUM(CASE WHEN success = 0 OR success IS NULL THEN 1 ELSE 0 END),"
                "       COALESCE(AVG(duration_ms), 0)"
                " FROM requests " + where,
                params,
            ).fetchone()

            total_input = row[0]
            total_cache = row[2]
            total_count = row[3]
            total_success = row[4]
            total_fail = row[5]
            cache_hit_rate = (total_cache / total_input * 100) if total_input > 0 else 0.0
            success_rate = (total_success / total_count * 100) if total_count > 0 else 100.0

            totals = {
                "input": total_input, "output": row[1], "cache": total_cache,
                "total": total_input + row[1] + total_cache,
                "count": total_count,
                "success_count": total_success, "fail_count": total_fail,
                "avg_duration_ms": int(row[6]),
                "cache_hit_rate": round(cache_hit_rate, 1),
                "success_rate": round(success_rate, 1),
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

            acct_rows = conn.execute(
                "SELECT COALESCE(account_alias, ''), COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0),"
                "       COALESCE(SUM(tokens_cache), 0), COUNT(*),"
                "       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),"
                "       SUM(CASE WHEN success = 0 OR success IS NULL THEN 1 ELSE 0 END),"
                "       COALESCE(AVG(duration_ms), 0)"
                " FROM requests " + where +
                " GROUP BY account_alias",
                params,
            ).fetchall()

            return totals, rows, acct_rows

        totals, rows, acct_rows = await asyncio.to_thread(_query_stats)

        sum_total = totals["total"]
        models = {}
        for r in rows:
            t = r[1] + r[2] + r[3]
            m_cache_rate = (r[3] / r[1] * 100) if r[1] > 0 else 0.0
            m_success_rate = (r[5] / r[4] * 100) if r[4] > 0 else 100.0
            models[r[0]] = {
                "input": r[1], "output": r[2], "cache": r[3], "total": t,
                "pct": f"{t/sum_total*100:.1f}%" if sum_total else "0%",
                "count": r[4], "success_count": r[5], "fail_count": r[6],
                "avg_duration_ms": int(r[7]),
                "cache_hit_rate": round(m_cache_rate, 1),
                "success_rate": round(m_success_rate, 1),
            }

        # Per-account stats
        accounts = {}
        for r in acct_rows:
            t = r[1] + r[2] + r[3]
            label = r[0] if r[0] else "(default)"
            a_cache_rate = (r[3] / r[1] * 100) if r[1] > 0 else 0.0
            a_success_rate = (r[5] / r[4] * 100) if r[4] > 0 else 100.0
            accounts[label] = {
                "input": r[1], "output": r[2], "cache": r[3], "total": t,
                "pct": f"{t/sum_total*100:.1f}%" if sum_total else "0%",
                "count": r[4], "success_count": r[5], "fail_count": r[6],
                "avg_duration_ms": int(r[7]),
                "cache_hit_rate": round(a_cache_rate, 1),
                "success_rate": round(a_success_rate, 1),
            }

        result = {"models": models, "accounts": accounts, "totals": totals}
        _stats_cache.set(cache_key, result)
        return result

    @app.get("/api/logs")
    async def get_logs(limit: int = 100, offset: int = 0):
        lines = list(log_lines)
        return {
            "logs": lines[offset:offset+limit],
            "total": len(lines),
            "has_more": offset + limit < len(lines)
        }

    # ── Debug toggle ──

    @app.get("/api/debug")
    async def get_debug():
        return {"enabled": config_settings.DEBUG}

    @app.post("/api/debug")
    async def set_debug(request: Request):
        body = await request.json()
        enabled = body.get("enabled", False)
        config_settings.DEBUG = bool(enabled)
        # Persist to .env so debug survives restarts
        save_env({"OPENCODE_DEBUG": "1" if enabled else "0"})
        # Update display module's debug function too
        from dashboard.display import set_debug_log_file
        if enabled:
            from config import settings
            log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
            debug_log_path = os.path.join(log_dir, "debug.log")
            set_debug_log_file(debug_log_path)
        from dashboard.display import debug as _debug_fn
        _debug_fn(f"Debug mode {'ENABLED' if enabled else 'DISABLED'} via API")
        return {"enabled": config_settings.DEBUG}

    @app.get("/api/debug/logs")
    async def get_debug_logs(limit: int = 500, offset: int = 0):
        """Return lines from logs/debug.log, most recent first."""
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        debug_log_path = os.path.join(log_dir, "debug.log")
        try:
            if not os.path.exists(debug_log_path):
                return {"logs": [], "total": 0, "has_more": False}
            with open(debug_log_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            total = len(all_lines)
            # Most recent first
            all_lines = list(reversed(all_lines))
            page = [line.rstrip("\n") for line in all_lines[offset:offset + limit]]
            return {
                "logs": page,
                "total": total,
                "has_more": offset + limit < total,
            }
        except Exception as e:
            return {"logs": [], "total": 0, "has_more": False, "error": str(e)}

    @app.delete("/api/debug/logs")
    async def clear_debug_logs():
        """Truncate logs/debug.log."""
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        debug_log_path = os.path.join(log_dir, "debug.log")
        try:
            if os.path.exists(debug_log_path):
                with open(debug_log_path, "w", encoding="utf-8") as f:
                    f.truncate(0)
            from dashboard.display import debug as _debug_fn
            _debug_fn("Debug log cleared via API")
            return {"status": "ok", "message": "Debug log cleared."}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── SSE events ──

    @app.get("/api/events")
    async def event_stream(request: Request):
        manager = get_event_manager()
        queue = await manager.subscribe()
        _debug(f"  [sse] new SSE subscriber")

        async def event_generator():
            try:
                yield f"event: connected\ndata: {json.dumps({'status': 'ok'})}\n\n"
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=30)
                        yield payload
                    except asyncio.TimeoutError:
                        # Check if client disconnected during idle period
                        if await request.is_disconnected():
                            break
                        yield ": keepalive\n\n"
            except (asyncio.CancelledError, RuntimeError):
                pass
            finally:
                await manager.unsubscribe(queue)

        return StreamingResponse(event_generator(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

    # ── Quota endpoints ──

    @app.get("/api/quotas")
    async def get_quotas():
        return get_quota_snapshot()

    @app.get("/api/tools")
    async def get_tools(days: int = 7, all: bool = False):
        """Aggregate tool names from recent requests.
        Set ?all=true to include universal tools (Read, Write, etc.)."""
        where, params = _build_where(daysAgo(days), None)
        def _query_tools():
            return conn.execute(
                "SELECT tools FROM requests " + where + " AND tools IS NOT NULL AND tools != '[]'",
                params,
            ).fetchall()
        rows = await asyncio.to_thread(_query_tools)

        # Aggregate tool names and count occurrences
        tool_counts = {}
        for row in rows:
            try:
                tools = json.loads(row["tools"]) if isinstance(row["tools"], str) else []
                for tool in tools:
                    if isinstance(tool, str):
                        tool_counts[tool] = tool_counts.get(tool, 0) + 1
            except (json.JSONDecodeError, TypeError):
                continue

        # Sort by frequency descending
        sorted_tools = sorted(tool_counts.items(), key=lambda x: -x[1])

        # Filter out universal tools unless ?all=true
        if not all:
            sorted_tools = [(n, c) for n, c in sorted_tools if n not in UNIVERSAL_TOOLS]

        # Check if any tool matches an existing custom route
        custom_routes = CUSTOM_ROUTES
        result = []
        for name, count in sorted_tools:
            routed_to = None
            for route_key, route_info in custom_routes.items():
                if route_info.get("match") and name in route_info["match"]:
                    routed_to = route_info.get("model")
                    break
            result.append({"name": name, "count": count, "routed_to": routed_to})
        return result

    # ── Stats & history ──

    @app.get("/api/history")
    async def get_history(from_date: str = None, to_date: str = None, limit: int = 20, offset: int = 0):
        where, params = _build_where(from_date, to_date)
        query = "SELECT * FROM requests " + where + " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        def _query_history():
            rows = conn.execute(query, params).fetchall()
            count_query = "SELECT COUNT(*) FROM requests " + where
            total_row = conn.execute(count_query, params[:-2]).fetchone()
            total_count = total_row[0] if total_row else 0
            return rows, total_count

        rows, total_count = await asyncio.to_thread(_query_history)

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
                    "account_alias": r["account_alias"] if "account_alias" in r.keys() else None,
                    "tools": json.loads(r["tools"]) if "tools" in r.keys() and r["tools"] and r["tools"] != "[]" else [],
                    "tools_used": json.loads(r["tools_used"]) if "tools_used" in r.keys() and r["tools_used"] and r["tools_used"] != "[]" else [],
                }
                for r in rows
            ],
            "total": total_count,
            "page": offset // limit + 1,
            "per_page": limit,
            "has_more": offset + limit < total_count
        }

    @app.delete("/api/history")
    async def delete_history(before: str = None, all: bool = False, model: str = None):
        _debug(f"  [history] delete: all={all} model={model} before={before}")
        if not all and not before and not model:
            return {"error": "Specify 'before' date, 'model' name, or 'all=true'"}

        def _delete():
            if all:
                conn.execute("DELETE FROM requests")
                if token_usage:
                    with token_lock:
                        for d in token_usage.values():
                            d["input"] = d["output"] = d["cache"] = 0
            elif model:
                conn.execute("DELETE FROM requests WHERE model = ?", (model,))
                if token_usage and model in token_usage:
                    with token_lock:
                        token_usage[model]["input"] = token_usage[model]["output"] = token_usage[model]["cache"] = 0
            elif before:
                conn.execute("DELETE FROM requests WHERE timestamp < ?", (before + "T23:59:59",))
                # Recalculate all counters from remaining rows
                if token_usage:
                    rows = conn.execute(
                        "SELECT model, COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0),"
                        " COALESCE(SUM(tokens_cache), 0) FROM requests GROUP BY model"
                    ).fetchall()
                    with token_lock:
                        for d in token_usage.values():
                            d["input"] = d["output"] = d["cache"] = 0
                        for row in rows:
                            m = row[0]
                            if m in token_usage:
                                token_usage[m]["input"] = row[1]
                                token_usage[m]["output"] = row[2]
                                token_usage[m]["cache"] = row[3]
            conn.commit()

        await asyncio.to_thread(_delete)
        return {"status": "deleted"}