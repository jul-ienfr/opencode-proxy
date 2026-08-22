"""
Dashboard API endpoints: stats, logs, history, config, static files.
"""

import json
import os
import time
import asyncio
import socket
from fastapi import Request, Response
from fastapi.responses import StreamingResponse, JSONResponse
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
        self._cleanup_counter = 0

    def get(self, key: str):
        entry = self._store.get(key)
        if entry and (time.monotonic() - entry[0]) < self._ttl:
            return entry[1]
        return None

    def set(self, key: str, value):
        self._store[key] = (time.monotonic(), value)
        self._cleanup_counter += 1
        if self._cleanup_counter >= 50:
            self._cleanup_counter = 0
            self._evict_expired()

    def _evict_expired(self):
        now = time.monotonic()
        expired = [k for k, (ts, _) in self._store.items() if (now - ts) >= self._ttl]
        for k in expired:
            del self._store[k]

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


def _build_where(from_date=None, to_date=None, status=None, model=None, original_model=None, account=None, tool=None, search=None, is_stream=None):
    conditions, params = [], []
    if from_date:
        conditions.append("timestamp >= ?")
        params.append(from_date)
    if to_date:
        conditions.append("timestamp <= ?")
        params.append(to_date + "T23:59:59")
    if status == "success":
        conditions.append("success = 1")
    elif status == "error":
        conditions.append("success = 0")
    if model:
        conditions.append("model = ?")
        params.append(model)
    if original_model:
        conditions.append("original_model = ?")
        params.append(original_model)
    if account:
        conditions.append("account_alias = ?")
        params.append(account)
    if tool:
        conditions.append("tools_used LIKE ?")
        params.append(f'%"{tool}"%')
    if search:
        conditions.append("(error LIKE ? OR model LIKE ? OR original_model LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    if is_stream is not None and str(is_stream).strip() != "":
        v = str(is_stream).strip().lower()
        if v in ("true", "1", "yes"):
            conditions.append("is_stream = 1")
        elif v in ("false", "0", "no"):
            conditions.append("is_stream = 0")
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    return where, params


def daysAgo(n: int) -> str:
    """Return date string for N days ago (en-CA format YYYY-MM-DD)."""
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def _get_canonical_vpn_paths():
    """Return canonical (configs_dir, auth_file) from config.yaml with fallback defaults."""
    try:
        import config.settings as cfg
        openvpn_cfg = cfg.yaml_get("ip_rotation", "openvpn", {}) or {}
        if not isinstance(openvpn_cfg, dict):
            openvpn_cfg = {}
        configs_dir = openvpn_cfg.get("configs_dir") or "vpn/configs"
        auth_file = openvpn_cfg.get("auth_file") or "vpn/credentials.txt"
        # Resolve relative to project root
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if not os.path.isabs(configs_dir):
            configs_dir = os.path.join(root, configs_dir)
        if not os.path.isabs(auth_file):
            auth_file = os.path.join(root, auth_file)
        return configs_dir, auth_file
    except Exception:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(root, "vpn", "configs"), os.path.join(root, "vpn", "credentials.txt")


def _persist_vpn_config(updates: dict):
    """Persist VPN config changes to config.yaml (hot-reload, best-effort).

    Uses config.settings YAML layer so _yaml_data stays in sync,
    then hot-reloads vpn_manager in-memory and emits SSE events.
    Supports alias keys (proxy_port ↔ vpn_proxy_port, vpn-api-cache ↔ api_cache_ttl).
    """
    try:
        import config.settings as cfg
        # Map API keys (incl. frontend aliases) → yaml keys in ip_rotation
        key_map = {
            "enabled": "enabled",
            "mode": "mode",
            "proxy_mode": "proxy_mode",
            "docker_image": "docker_image",
            "docker_image_custom": "docker_image_custom",
            "free_only": "free_only",
            "quota_per_ip": "quota_per_ip",
            "switch_delay": "switch_delay",
            "vpn_proxy_port": "vpn_proxy_port",
            "proxy_port": "vpn_proxy_port",
            "vpn-proxy-port": "vpn_proxy_port",
            "nordvpn_token": "nordvpn_token",
            "nordvpn_country": "nordvpn_country",
            "nordvpn_group": "nordvpn_group",
            "nordvpn_technology": "nordvpn_technology",
            "nordvpn_exe": "nordvpn_exe",
            "docker_killswitch": "docker_killswitch",
            "docker_dns_over_tls": "docker_dns_over_tls",
            "use_nordvpn_api": "use_nordvpn_api",
            "api_cache_ttl": "api_cache_ttl",
            "vpn-api-cache": "api_cache_ttl",
            "circuit_breaker_threshold": "circuit_breaker_threshold",
            "circuit_breaker_recovery": "circuit_breaker_recovery",
            "backoff_max_delay": "backoff_max_delay",
            "watchdog_interval": "watchdog_interval",
        }

        ip_rot = cfg.yaml_get("ip_rotation", default={}) or {}
        if not isinstance(ip_rot, dict):
            ip_rot = {}

        changed_keys: dict = {}
        for api_key, yaml_key in key_map.items():
            if api_key in updates:
                new_val = updates[api_key]
                if ip_rot.get(yaml_key) != new_val:
                    ip_rot[yaml_key] = new_val
                    changed_keys[yaml_key] = new_val

        if changed_keys:
            cfg._yaml_data["ip_rotation"] = ip_rot
            cfg.save_yaml_config()
            # Keep module-level IP_ROTATION in sync (opencode.py imports it at startup)
            try:
                cfg.IP_ROTATION.clear()
                cfg.IP_ROTATION.update(ip_rot)
            except Exception:
                pass
            # Hot-reload vpn_manager in-memory (no restart)
            try:
                import shared_state
                if shared_state.vpn_manager:
                    mgr_updates: dict = {}
                    for yk, val in changed_keys.items():
                        if yk == "vpn_proxy_port":
                            mgr_updates["proxy_port"] = val
                            mgr_updates["vpn_proxy_port"] = val
                        else:
                            mgr_updates[yk] = val
                    # Also forward original alias values for manager keys that use different naming
                    for k in ("api_cache_ttl", "docker_image_custom", "free_only", "use_nordvpn_api"):
                        if k in updates and k not in mgr_updates:
                            mgr_updates[k] = updates[k]
                    shared_state.vpn_manager.update_config(mgr_updates)
            except Exception as e:
                _debug(f"  [vpn] hot-reload after persist failed: {e}")
            # SSE — notify GUI without restart/polling
            try:
                mgr = get_event_manager()
                mgr.publish("vpn_config_changed", {"changed": list(changed_keys.keys())})
                mgr.publish("stats_updated", {"time": time.strftime("%Y-%m-%dT%H:%M:%S")})
            except Exception:
                pass
            _debug(f"  [vpn] config persisted (hot-reload): {list(changed_keys.keys())}")

    except Exception as e:
        _debug(f"  [vpn] failed to persist config: {e}")


def _persist_vpn_servers(servers: list):
    """Persist full openvpn.servers list to config.yaml (hot-reload in-memory)."""
    try:
        import config.settings as cfg
        ip_rot = cfg.yaml_get("ip_rotation", default={}) or {}
        if not isinstance(ip_rot, dict):
            ip_rot = {}
        openvpn_cfg = ip_rot.get("openvpn") or {}
        if not isinstance(openvpn_cfg, dict):
            openvpn_cfg = {}
        openvpn_cfg["servers"] = servers
        # Ensure canonical paths are set
        if "configs_dir" not in openvpn_cfg:
            openvpn_cfg["configs_dir"] = "vpn/configs"
        if "auth_file" not in openvpn_cfg:
            openvpn_cfg["auth_file"] = "vpn/credentials.txt"
        ip_rot["openvpn"] = openvpn_cfg
        cfg._yaml_data["ip_rotation"] = ip_rot
        cfg.save_yaml_config()
        try:
            cfg.IP_ROTATION.clear()
            cfg.IP_ROTATION.update(ip_rot)
        except Exception:
            pass
        # Hot-reload manager servers
        try:
            import shared_state
            if shared_state.vpn_manager:
                shared_state.vpn_manager.update_config({"servers": servers})
        except Exception as e:
            _debug(f"  [vpn] persist servers hot-reload failed: {e}")
        try:
            get_event_manager().publish("vpn_config_changed", {"changed": ["openvpn.servers"]})
        except Exception:
            pass
        _debug(f"  [vpn] servers persisted: {len(servers)} entries")
    except Exception as e:
        _debug(f"  [vpn] failed to persist servers: {e}")


def register_dashboard(app, static_dir, conn, server_manager_getter=None, token_usage=None, token_lock=None):
    # Add Cache-Control headers for static assets (JS/CSS/HTML)
    from starlette.middleware.base import BaseHTTPMiddleware
    class _StaticCacheMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            if request.url.path.startswith("/static/"):
                response.headers["Cache-Control"] = "public, max-age=3600"
            return response
    app.add_middleware(_StaticCacheMiddleware)

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Cache index.html in memory — avoids re-reading from disk on every request
    _index_html_cache: bytes | None = None
    try:
        with open(os.path.join(static_dir, "index.html"), "rb") as _f:
            _index_html_cache = _f.read()
    except Exception:
        pass

    @app.get("/")
    async def root():
        if _index_html_cache is not None:
            return Response(content=_index_html_cache, media_type="text/html; charset=utf-8")
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
            "free_model_map": config_settings.FREE_MODEL_MAP,
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

    @app.get("/api/config/tool-capabilities")
    async def get_tool_capabilities():
        from config import TOOL_CAPABILITIES, MODELS
        # Get all known tools from the database
        where, params = _build_where(daysAgo(30), None)
        def _query_all_tools():
            return conn.execute(
                "SELECT tools FROM requests " + where + " AND tools IS NOT NULL AND tools != '[]'",
                params,
            ).fetchall()
        rows = await asyncio.to_thread(_query_all_tools)
        all_tools = set()
        for row in rows:
            try:
                tools = json.loads(row["tools"]) if isinstance(row["tools"], str) else []
                for t in tools:
                    if isinstance(t, str):
                        all_tools.add(t)
            except (json.JSONDecodeError, TypeError):
                continue
        return {
            "capabilities": TOOL_CAPABILITIES,
            "all_tools": sorted(all_tools),
            "all_models": sorted(MODELS.keys()),
        }

    @app.post("/api/config/tool-capabilities")
    async def update_tool_capabilities(request: Request):
        body = await request.json()
        from config import save_tool_capabilities
        _debug(f"  [config] tool capabilities updated ({len(body)} entries)")
        save_tool_capabilities(body)
        return {"status": "ok", "message": "Tool capabilities updated."}

    @app.get("/api/config/web-search")
    async def get_web_search_config():
        from config import yaml_get
        mode = yaml_get("web_search", "mode", "duckduckgo")
        target_model = yaml_get("web_search", "target_model", None)
        max_results = yaml_get("web_search", "max_results", 5)
        timeout = yaml_get("web_search", "timeout", 10)
        return {
            "mode": mode,
            "target_model": target_model,
            "max_results": max_results,
            "timeout": timeout,
            "available_models": sorted(MODELS.keys()),
            "modes": ["duckduckgo", "model", "ddg_then_model", "model_then_ddg"],
        }

    @app.post("/api/config/web-search")
    async def update_web_search_config(request: Request):
        body = await request.json()
        from config import yaml_set
        if "mode" in body:
            yaml_set("web_search", "mode", body["mode"])
        if "target_model" in body:
            yaml_set("web_search", "target_model", body["target_model"])
        if "max_results" in body:
            yaml_set("web_search", "max_results", int(body["max_results"]))
        if "timeout" in body:
            yaml_set("web_search", "timeout", int(body["timeout"]))
        _debug(f"  [config] web search updated: mode={body.get('mode')}, model={body.get('target_model')}")
        return {"status": "ok", "message": "Web search config updated."}

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

    @app.get("/api/config/free-model-map")
    async def get_free_model_map():
        """Get free model mapping (paid → free)."""
        import config.settings as cfg
        fmap = cfg.yaml_get("free_model_map", default={}) or {}
        if not fmap:
            # Fallback to in-memory global
            try:
                import opencode as _oc
                fmap = getattr(_oc, "FREE_MODEL_MAP", {}) or cfg.FREE_MODEL_MAP
            except Exception:
                fmap = cfg.FREE_MODEL_MAP
        return {"free_model_map": fmap, "available_models": sorted(MODELS.keys())}

    @app.post("/api/config/free-model-map")
    async def update_free_model_map(request: Request):
        """Update free model mapping (hot-reload, no restart)."""
        body = await request.json()
        # Accept either {free_model_map: {...}} or raw dict
        if "free_model_map" in body and isinstance(body["free_model_map"], dict):
            new_map = body["free_model_map"]
        elif isinstance(body, dict) and all(isinstance(v, str) for v in body.values()):
            # Raw map passed directly
            new_map = {k: v for k, v in body.items() if k not in ("status", "message")}
            # If body was wrapper with other keys, treat accordingly
            if not new_map and "free_model_map" not in body:
                new_map = body
        else:
            new_map = body.get("free_model_map", body)
            if not isinstance(new_map, dict):
                return JSONResponse(status_code=400, content={"error": "free_model_map must be an object"})
        # Validate: values should be known models or free variants
        # Allow empty string to delete entry (handled below)
        cleaned = {}
        for k, v in new_map.items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            k = k.strip()
            v = v.strip()
            if not k:
                continue
            if v == "":
                continue  # skip empty = deletion
            cleaned[k] = v
        # Persist to YAML (top-level key)
        try:
            import config.settings as cfg
            cfg._yaml_data["free_model_map"] = cleaned
            cfg.save_yaml_config()
            # Hot-reload in-memory globals (config + opencode)
            cfg.FREE_MODEL_MAP.clear()
            cfg.FREE_MODEL_MAP.update(cleaned)
            try:
                import opencode as _oc
                # Update opencode's imported reference in-place if same object, else reassign
                if hasattr(_oc, "FREE_MODEL_MAP"):
                    # If it's same dict object, already updated via cfg; else replace
                    if _oc.FREE_MODEL_MAP is not cfg.FREE_MODEL_MAP:
                        _oc.FREE_MODEL_MAP.clear()
                        _oc.FREE_MODEL_MAP.update(cleaned)
            except Exception as e:
                _debug(f"  [config] free_model_map opencode hot-reload failed: {e}")
            try:
                get_event_manager().publish("config_changed", {"changed": ["free_model_map"]})
            except Exception:
                pass
            _debug(f"  [config] free_model_map updated (hot-reload): {len(cleaned)} entries")
        except Exception as e:
            _debug(f"  [config] free_model_map persist failed: {e}")
            return JSONResponse(status_code=500, content={"error": str(e)})
        return {"status": "ok", "free_model_map": cleaned, "message": "Free model map updated (hot-reload)."}

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
                "SELECT COALESCE(NULLIF(free_model_ip, ''), account_alias, ''), COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0),"
                "       COALESCE(SUM(tokens_cache), 0), COUNT(*),"
                "       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),"
                "       SUM(CASE WHEN success = 0 OR success IS NULL THEN 1 ELSE 0 END),"
                "       COALESCE(AVG(duration_ms), 0)"
                " FROM requests " + where +
                " GROUP BY COALESCE(NULLIF(free_model_ip, ''), account_alias, '')",
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

    @app.get("/api/stats/timeseries")
    async def get_stats_timeseries(from_date: str = None, to_date: str = None, granularity: str = "hour"):
        """Return time-series data for charts: requests count, tokens, avg duration per time bucket."""
        where, params = _build_where(from_date, to_date)

        # Determine time grouping format
        if granularity == "day":
            time_fmt = "%Y-%m-%d"
        elif granularity == "week":
            time_fmt = "%Y-W%W"
        else:  # hour
            time_fmt = "%Y-%m-%d %H:00"

        def _query_timeseries():
            # Group by truncated timestamp
            if granularity == "day":
                trunc_expr = "substr(timestamp, 1, 10)"
            elif granularity == "week":
                trunc_expr = "substr(timestamp, 1, 4) || '-W' || printf('%02d', ((cast(substr(timestamp, 9, 2) as integer) - 1) / 7 + 1))"
            else:  # hour
                trunc_expr = "substr(timestamp, 1, 13) || ':00'"

            rows = conn.execute(
                f"SELECT {trunc_expr} as period, "
                "COUNT(*) as count, "
                "SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count, "
                "SUM(CASE WHEN success = 0 OR success IS NULL THEN 1 ELSE 0 END) as fail_count, "
                "COALESCE(SUM(tokens_input), 0) as input_tokens, "
                "COALESCE(SUM(tokens_output), 0) as output_tokens, "
                "COALESCE(SUM(tokens_cache), 0) as cache_tokens, "
                "COALESCE(AVG(duration_ms), 0) as avg_duration "
                "FROM requests " + where +
                f" GROUP BY period ORDER BY period",
                params,
            ).fetchall()
            return rows

        rows = await asyncio.to_thread(_query_timeseries)

        return {
            "series": [
                {
                    "period": r["period"],
                    "count": r["count"],
                    "success": r["success_count"],
                    "fail": r["fail_count"],
                    "input_tokens": r["input_tokens"],
                    "output_tokens": r["output_tokens"],
                    "cache_tokens": r["cache_tokens"],
                    "avg_duration_ms": int(r["avg_duration"]),
                }
                for r in rows
            ],
            "granularity": granularity,
        }

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
        """Return lines from logs/debug.log, most recent first.
        Auto-rotation keeps the file ≤50MB so full-read is the normal path;
        falls back to tail-reading for files that somehow exceed that."""
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        debug_log_path = os.path.join(log_dir, "debug.log")
        try:
            if not os.path.exists(debug_log_path):
                return {"logs": [], "total": 0, "has_more": False}

            file_size = os.path.getsize(debug_log_path)
            if file_size == 0:
                return {"logs": [], "total": 0, "has_more": False}

            MAX_FULL_READ = 50 * 1024 * 1024  # 50 MB — matches rotation threshold

            def _read_all():
                """Read the whole file (fine for files up to 50 MB)."""
                with open(debug_log_path, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
                total = len(all_lines)
                # Most-recent-first, paginated
                reversed_lines = [line.rstrip("\n") for line in reversed(all_lines)]
                page = reversed_lines[offset:offset + limit]
                return page, total

            def _read_tail():
                """Read from the end of a large file.
                Reads up to 10 MB from EOF — enough for thousands of lines."""
                with open(debug_log_path, "rb") as f:
                    f.seek(0, 2)
                    file_size = f.tell()

                    # Read up to 10 MB from end
                    read_size = min(file_size, 10 * 1024 * 1024)
                    f.seek(file_size - read_size)
                    data = f.read().decode("utf-8", errors="replace")

                lines = data.split("\n")
                # Discard partial first line (continuation from before our window)
                if file_size > read_size and len(lines) > 1:
                    lines = lines[1:]
                # Discard trailing empty from final newline
                if lines and lines[-1] == "":
                    lines = lines[:-1]

                total = len(lines)
                reversed_lines = list(reversed(lines))
                page = reversed_lines[offset:offset + limit]
                return page, total

            if file_size <= MAX_FULL_READ:
                page, total = await asyncio.to_thread(_read_all)
            else:
                page, total = await asyncio.to_thread(_read_tail)

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

    @app.get("/api/free-model-usage")
    async def get_free_model_usage(days: int = 7):
        """Free model usage stats for quota analysis.

        Returns per-model, per-key, per-workspace, per-IP aggregates with
        total requests, tokens, and success/failure counts.
        Also calculates quota reset times per IP.
        """
        def _query():
            where = "WHERE timestamp >= datetime('now', ?)"
            params = [f"-{days} days"]
            rows = conn.execute(
                "SELECT timestamp, paid_model, free_model, api_key, workspace_id, "
                "       status, tokens_input, tokens_output, duration_ms, ip "
                "FROM free_model_usage " + where + " ORDER BY timestamp DESC LIMIT 5000",
                params,
            ).fetchall()
            return rows

        rows = await asyncio.to_thread(_query)

        # Aggregate by free_model
        by_model = {}
        by_key = {}
        by_workspace = {}
        by_ip = {}
        timeline = []
        for r in rows:
            ts, paid, free, key, ws, status, tok_in, tok_out, dur, ip = r if len(r) > 9 else (*r, "")
            # By model
            m = by_model.setdefault(free, {"requests": 0, "tokens_in": 0, "tokens_out": 0, "success": 0, "fail": 0})
            m["requests"] += 1
            m["tokens_in"] += tok_in or 0
            m["tokens_out"] += tok_out or 0
            if status == 200:
                m["success"] += 1
            else:
                m["fail"] += 1
            # By key
            k = by_key.setdefault(key, {"requests": 0, "tokens_in": 0, "tokens_out": 0})
            k["requests"] += 1
            k["tokens_in"] += tok_in or 0
            k["tokens_out"] += tok_out or 0
            # By workspace
            w = by_workspace.setdefault(ws, {"requests": 0, "tokens_in": 0, "tokens_out": 0})
            w["requests"] += 1
            w["tokens_in"] += tok_in or 0
            w["tokens_out"] += tok_out or 0
            # By IP (track quota usage and reset time)
            if ip:
                ip_data = by_ip.setdefault(ip, {
                    "requests": 0, "tokens_in": 0, "tokens_out": 0,
                    "first_seen": ts, "last_seen": ts, "success": 0, "fail": 0,
                    "reset_at": None, "available": False,
                })
                ip_data["requests"] += 1
                ip_data["tokens_in"] += tok_in or 0
                ip_data["tokens_out"] += tok_out or 0
                if status == 200:
                    ip_data["success"] += 1
                else:
                    ip_data["fail"] += 1
                # Track time range
                if ts > ip_data["last_seen"]:
                    ip_data["last_seen"] = ts
                if ts < ip_data["first_seen"]:
                    ip_data["first_seen"] = ts
            # Timeline
            timeline.append({"ts": ts, "model": free, "status": status, "tokens": (tok_in or 0) + (tok_out or 0), "ip": ip})

        # Calculate reset times for each IP (quota window = 48h from last request)
        from datetime import datetime, timedelta
        QUOTA_WINDOW_HOURS = 48
        now = datetime.utcnow()
        for ip_addr, ip_data in by_ip.items():
            try:
                last = datetime.fromisoformat(ip_data["last_seen"])
                reset_at = last + timedelta(hours=QUOTA_WINDOW_HOURS)
                ip_data["reset_at"] = reset_at.strftime("%Y-%m-%dT%H:%M:%S")
                ip_data["available"] = now >= reset_at
                ip_data["reset_in_sec"] = max(0, int((reset_at - now).total_seconds()))
            except Exception:
                ip_data["reset_at"] = None
                ip_data["available"] = True
                ip_data["reset_in_sec"] = 0

        return {
            "days": days,
            "total_requests": len(rows),
            "by_model": by_model,
            "by_key": by_key,
            "by_workspace": by_workspace,
            "by_ip": by_ip,
            "recent": timeline[:100],
        }

    # ── VPN / IP rotation endpoints ──

    @app.get("/api/vpn-status")
    async def get_vpn_status():
        """Get current VPN status, IP, server, and usage stats."""
        import shared_state
        if shared_state.vpn_manager:
            # Check for existing Docker container
            shared_state.vpn_manager._check_existing_docker()
            if shared_state.free_ip_pool:
                return shared_state.free_ip_pool.get_status()
            return shared_state.vpn_manager.get_status()
        return {"enabled": False, "status": "not_configured"}

    @app.get("/api/vpn-config")
    async def get_vpn_config():
        """Get current VPN configuration."""
        import shared_state
        if shared_state.vpn_manager:
            return shared_state.vpn_manager.get_config()
        return {"enabled": False, "servers": [], "auth_file": "", "protocol": "udp"}

    @app.post("/api/vpn-config")
    async def update_vpn_config(request: Request):
        """Update VPN configuration (hot-reload + persist to config.yaml, reboot-safe)."""
        import shared_state
        import os
        body = await request.json()

        # Handle credentials - save to canonical auth_file (no VPN manager needed)
        if "credentials" in body:
            creds = body.pop("credentials")
            username = creds.get("username", "")
            password = creds.get("password", "")
            if username and password:
                # Canonical path from config.yaml
                _, cred_path = _get_canonical_vpn_paths()
                os.makedirs(os.path.dirname(cred_path), exist_ok=True)
                with open(cred_path, "w", newline='\n', encoding="utf-8") as f:
                    f.write(f"{username}\n{password}\n")
                try:
                    os.chmod(cred_path, 0o600)
                except Exception:
                    pass
                # Also mirror to legacy path for compat
                try:
                    legacy_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vpn_configs")
                    os.makedirs(legacy_dir, exist_ok=True)
                    legacy_path = os.path.join(legacy_dir, "credentials.txt")
                    if os.path.abspath(legacy_path) != os.path.abspath(cred_path):
                        with open(legacy_path, "w", newline='\n', encoding="utf-8") as lf:
                            lf.write(f"{username}\n{password}\n")
                        try:
                            os.chmod(legacy_path, 0o600)
                        except Exception:
                            pass
                except Exception:
                    pass
                if shared_state.vpn_manager:
                    shared_state.vpn_manager._auth_file = cred_path
                    # Persist auth_file if changed? not needed — canonical already

        # Handle server removal with YAML persistence + file unlink (reboot-safe, hot-reload)
        if "remove_server" in body:
            name_to_remove = body.pop("remove_server")
            # Remove from YAML first
            try:
                import config.settings as cfg
                ip_rot = cfg.yaml_get("ip_rotation", default={}) or {}
                openvpn_cfg = ip_rot.get("openvpn") or {}
                servers = list(openvpn_cfg.get("servers", []) or [])
                new_servers = [s for s in servers if s.get("name") != name_to_remove]
                _debug(f"  [vpn] remove debug: {name_to_remove} servers {len(servers)} -> {len(new_servers)} list={servers} new={new_servers}")
                if len(new_servers) != len(servers):
                    _persist_vpn_servers(new_servers)
                    _debug(f"  [vpn] _persist_vpn_servers called for remove {name_to_remove}")
                else:
                    _debug(f"  [vpn] remove no diff, fallback to manager remove")
                    # Fallback: at least update manager
                    if shared_state.vpn_manager:
                        shared_state.vpn_manager.remove_server(name_to_remove)
                # Unlink .ovpn file (canonical + legacy)
                for base_dir in [_get_canonical_vpn_paths()[0],
                                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vpn_configs")]:
                    try:
                        ovpn_path = os.path.join(base_dir, f"{name_to_remove}.ovpn")
                        if os.path.exists(ovpn_path):
                            os.remove(ovpn_path)
                            _debug(f"  [vpn] removed ovpn file: {ovpn_path}")
                    except Exception as e:
                        _debug(f"  [vpn] failed to remove {name_to_remove}.ovpn: {e}")
                # Ensure manager in sync even if YAML path already handled
                if shared_state.vpn_manager and len(new_servers) == len(servers):
                    # already called remove above
                    pass
                elif shared_state.vpn_manager:
                    # _persist_vpn_servers already hot-reloaded, ensure cycle rebuilt
                    pass
            except Exception as e:
                _debug(f"  [vpn] remove_server error: {e}")
                if shared_state.vpn_manager:
                    try:
                        shared_state.vpn_manager.remove_server(name_to_remove)
                    except Exception:
                        pass

        # Handle explicit add_server via JSON (rare, but keep)
        if "add_server" in body:
            srv = body.pop("add_server")
            try:
                import config.settings as cfg
                ip_rot = cfg.yaml_get("ip_rotation", default={}) or {}
                openvpn_cfg = ip_rot.get("openvpn") or {}
                servers = list(openvpn_cfg.get("servers", []) or [])
                # Deduplicate by name
                servers = [s for s in servers if s.get("name") != srv.get("name")]
                # Ensure config path is canonical
                raw_cfg = srv.get("config", "")
                if raw_cfg:
                    # Normalize to canonical relative path
                    fname = os.path.basename(raw_cfg)
                    canonical_path = f"vpn/configs/{fname}"
                    srv["config"] = canonical_path
                servers.append(srv)
                _persist_vpn_servers(servers)
                if shared_state.vpn_manager:
                    # add_server will cycle rebuild; but _persist already updated manager
                    # Ensure file exists check not needed
                    pass
            except Exception as e:
                _debug(f"  [vpn] add_server error: {e}")
                if shared_state.vpn_manager:
                    shared_state.vpn_manager.add_server(srv["name"], srv["config"])

        # Handle remaining generic keys with hot-reload + persist (with validation per Phase3)
        if body and shared_state.vpn_manager:
            # Normalize aliases before persist/hot-reload
            normalized: dict = {}
            for k, v in body.items():
                if k == "proxy_port":
                    normalized["vpn_proxy_port"] = v
                    normalized["proxy_port"] = v
                elif k == "vpn-proxy-port":
                    normalized["vpn_proxy_port"] = v
                elif k == "vpn-api-cache":
                    normalized["api_cache_ttl"] = v
                else:
                    normalized[k] = v
            # Validation (Phase3: clamp app.js + 400 api.py)
            def _bad(msg): return JSONResponse(status_code=400, content={"error": msg})
            if "vpn_proxy_port" in normalized:
                try:
                    p = int(normalized["vpn_proxy_port"])
                    if not (1 <= p <= 65535): return _bad("vpn_proxy_port must be 1-65535")
                    normalized["vpn_proxy_port"] = p
                    normalized["proxy_port"] = p
                except (ValueError, TypeError): return _bad("Invalid vpn_proxy_port")
            if "proxy_port" in normalized and "vpn_proxy_port" not in normalized:
                try:
                    p = int(normalized["proxy_port"])
                    if not (1 <= p <= 65535): return _bad("proxy_port must be 1-65535")
                except (ValueError, TypeError): return _bad("Invalid proxy_port")
            if "quota_per_ip" in normalized:
                try:
                    q = int(normalized["quota_per_ip"])
                    if not (1 <= q <= 10000): return _bad("quota_per_ip must be 1-10000")
                    normalized["quota_per_ip"] = q
                except (ValueError, TypeError): return _bad("Invalid quota_per_ip")
            if "switch_delay" in normalized:
                try:
                    sd = int(normalized["switch_delay"])
                    if not (1 <= sd <= 60): return _bad("switch_delay must be 1-60")
                    normalized["switch_delay"] = sd
                except (ValueError, TypeError): return _bad("Invalid switch_delay")
            if "circuit_breaker_threshold" in normalized:
                try:
                    cb = int(normalized["circuit_breaker_threshold"])
                    if not (1 <= cb <= 10): return _bad("circuit_breaker_threshold must be 1-10")
                    normalized["circuit_breaker_threshold"] = cb
                except (ValueError, TypeError): return _bad("Invalid circuit_breaker_threshold")
            if "circuit_breaker_recovery" in normalized:
                try:
                    cr = int(normalized["circuit_breaker_recovery"])
                    if not (30 <= cr <= 3600): return _bad("circuit_breaker_recovery must be 30-3600")
                    normalized["circuit_breaker_recovery"] = cr
                except (ValueError, TypeError): return _bad("Invalid circuit_breaker_recovery")
            if "backoff_max_delay" in normalized:
                try:
                    bd = int(normalized["backoff_max_delay"])
                    if not (10 <= bd <= 300): return _bad("backoff_max_delay must be 10-300")
                    normalized["backoff_max_delay"] = bd
                except (ValueError, TypeError): return _bad("Invalid backoff_max_delay")
            if "watchdog_interval" in normalized:
                try:
                    wd = int(normalized["watchdog_interval"])
                    if not (0 <= wd <= 600): return _bad("watchdog_interval must be 0-600")
                    normalized["watchdog_interval"] = wd
                except (ValueError, TypeError): return _bad("Invalid watchdog_interval")
            if "api_cache_ttl" in normalized:
                try:
                    ac = int(normalized["api_cache_ttl"])
                    if not (60 <= ac <= 7200): return _bad("api_cache_ttl must be 60-7200")
                    normalized["api_cache_ttl"] = ac
                except (ValueError, TypeError): return _bad("Invalid api_cache_ttl")
            # Persist first (writes YAML + hot-reloads manager inside)
            _persist_vpn_config(normalized)
            # Ensure manager also gets any keys not covered by _persist (e.g., socks5, geo_filter fallback)
            try:
                shared_state.vpn_manager.update_config(normalized)
            except Exception as e:
                _debug(f"  [vpn] update_config fallback failed: {e}")
        elif body:
            # No manager but still persist (validate as above for api_cache etc if present)
            _persist_vpn_config(body)

        config = shared_state.vpn_manager.get_config() if shared_state.vpn_manager else {}
        return {"ok": True, "config": config}

    @app.post("/api/vpn/toggle")
    async def toggle_vpn(request: Request):
        """Enable or disable VPN rotation."""
        import shared_state
        body = await request.json()
        enabled = body.get("enabled", True)
        if shared_state.vpn_manager:
            shared_state.vpn_manager.enabled = enabled
            if shared_state.free_ip_pool:
                shared_state.free_ip_pool._vpn = shared_state.vpn_manager
            return {"ok": True, "enabled": enabled}
        return {"error": "VPN manager not initialized"}

    @app.post("/api/vpn/connect")
    async def connect_vpn():
        """Connect VPN — checks for existing container first, then tries connection."""
        import shared_state
        if not shared_state.vpn_manager:
            return {"error": "VPN manager not initialized"}

        # Check if Docker container is already running
        shared_state.vpn_manager._check_existing_docker()
        if shared_state.vpn_manager.status == "connected":
            return {"ok": True, "ip": shared_state.vpn_manager.current_ip, "server": {"name": "Docker VPN"}}

        try:
            ip = await shared_state.vpn_manager.connect_next()
            return {"ok": True, "ip": ip, "server": shared_state.vpn_manager.current_server}
        except Exception as e:
            error_msg = str(e)
            if "AUTH_FAILED" in error_msg or "lockout" in error_msg.lower():
                _debug("[vpn] OpenVPN failed, switching to NordVPN app detection mode")
                try:
                    ip = await shared_state.vpn_manager.connect_wait()
                    return {"ok": True, "ip": ip, "server": {"name": "NordVPN App"}}
                except Exception as e2:
                    return {"error": f"OpenVPN failed: {error_msg}. NordVPN app detection: {e2}"}
            return {"error": error_msg}

    @app.post("/api/vpn/disconnect")
    async def disconnect_vpn():
        """Disconnect VPN."""
        import shared_state
        if not shared_state.vpn_manager:
            return {"error": "VPN manager not initialized"}
        await shared_state.vpn_manager.disconnect()
        return {"ok": True}

    @app.post("/api/vpn/health-check")
    async def vpn_health_check():
        """Run a health check on the current VPN connection."""
        import shared_state
        if not shared_state.vpn_manager:
            return {"error": "VPN manager not initialized"}
        result = await shared_state.vpn_manager.health_check()
        return result

    @app.post("/api/vpn/next")
    async def next_vpn():
        """Switch to next VPN server."""
        import shared_state
        if not shared_state.vpn_manager:
            return {"error": "VPN manager not initialized"}
        try:
            if shared_state.free_ip_pool:
                await shared_state.free_ip_pool.switch_ip()
            else:
                await shared_state.vpn_manager.connect_next()
            return {"ok": True, "ip": shared_state.vpn_manager.current_ip, "server": shared_state.vpn_manager.current_server}
        except Exception as e:
            return {"error": str(e)}

    @app.post("/api/vpn/upload-config")
    async def upload_vpn_config(request: Request):
        """Upload an OpenVPN .ovpn config file (reboot-safe + hot-reload)."""
        import shared_state
        import os

        form = await request.form()
        name = form.get("name", "")
        file = form.get("config")

        if not name:
            return JSONResponse(status_code=400, content={"error": "Server name required"})
        if not file:
            return JSONResponse(status_code=400, content={"error": "Config file required"})

        # Basic name sanitization (alphanum, dash, underscore)
        import re
        if not re.match(r'^[a-zA-Z0-9._-]+$', name):
            return JSONResponse(status_code=400, content={"error": "Invalid server name (use letters, numbers, - _ .)"})
        # Validate file extension/content
        filename = getattr(file, 'filename', '') or ''
        if filename and not filename.lower().endswith('.ovpn'):
            # still allow but warn
            _debug(f"  [vpn] upload warning: filename {filename!r} not .ovpn")

        # Canonical directory from config.yaml
        configs_dir, _ = _get_canonical_vpn_paths()
        os.makedirs(configs_dir, exist_ok=True)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # Relative path stored in YAML (portable)
        rel_path = f"vpn/configs/{name}.ovpn"
        file_path = os.path.join(configs_dir, f"{name}.ovpn")
        content = await file.read()
        # Basic ovpn validation — must contain 'remote' or 'client'
        try:
            text = content.decode('utf-8', errors='ignore')
            if 'remote' not in text.lower() and 'client' not in text.lower():
                _debug(f"  [vpn] upload warning: {name}.ovpn may be invalid (no 'remote' found)")
        except Exception:
            pass
        # Prevent path traversal via name already sanitized
        with open(file_path, "wb") as f:
            f.write(content)
        # Mirror to legacy dir for compat (best-effort)
        try:
            legacy_dir = os.path.join(root, "vpn_configs")
            os.makedirs(legacy_dir, exist_ok=True)
            legacy_path = os.path.join(legacy_dir, f"{name}.ovpn")
            if os.path.abspath(legacy_path) != os.path.abspath(file_path):
                with open(legacy_path, "wb") as lf:
                    lf.write(content)
        except Exception as e:
            _debug(f"  [vpn] legacy mirror failed: {e}")

        # Persist to config.yaml openvpn.servers (reboot-safe) + hot-reload manager
        try:
            import config.settings as cfg
            ip_rot = cfg.yaml_get("ip_rotation", default={}) or {}
            openvpn_cfg = ip_rot.get("openvpn") or {}
            servers = list(openvpn_cfg.get("servers", []) or [])
            # Deduplicate by name — replace existing
            servers = [s for s in servers if s.get("name") != name]
            servers.append({"name": name, "config": rel_path})
            _persist_vpn_servers(servers)
        except Exception as e:
            _debug(f"  [vpn] persist after upload failed: {e}")
            if shared_state.vpn_manager:
                shared_state.vpn_manager.add_server(name, rel_path)

        # Also ensure manager has it (persist already did, but double-ensure)
        if shared_state.vpn_manager:
            try:
                # If persist succeeded, manager already has new list; ensure file path accessible
                # No extra add needed; but if manager list missing, add
                if not any(s.get("name") == name for s in shared_state.vpn_manager._servers):
                    shared_state.vpn_manager.add_server(name, rel_path)
            except Exception:
                pass

        return {"ok": True, "path": rel_path, "name": name, "servers": len(servers) if 'servers' in locals() else 0}

    @app.get("/api/vpn/credentials")
    async def get_vpn_credentials():
        """Check if VPN credentials exist (does not return actual values). Supports canonical + legacy fallback."""
        import os
        _, canonical = _get_canonical_vpn_paths()
        legacy = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vpn_configs", "credentials.txt")
        cred_path = canonical if os.path.exists(canonical) and os.path.getsize(canonical) > 0 else legacy
        # If neither has content, still check canonical first
        if not os.path.exists(cred_path):
            cred_path = canonical
        exists = os.path.exists(cred_path) and os.path.getsize(cred_path) > 0
        # Fallback: check alternative if primary empty
        if not exists:
            alt = legacy if cred_path == canonical else canonical
            if os.path.exists(alt) and os.path.getsize(alt) > 0:
                cred_path = alt
                exists = True
        username_saved = ""
        if exists:
            try:
                with open(cred_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.read().strip().split("\n")
                    if lines:
                        username_saved = lines[0]
            except Exception:
                pass
        return {"exists": exists, "username_preview": username_saved[:4] + "****" if username_saved else "", "path": os.path.basename(cred_path)}

    @app.post("/api/vpn/credentials")
    async def save_vpn_credentials(request: Request):
        """Save NordVPN credentials (canonical + legacy mirror, hot-reload)."""
        import shared_state
        body = await request.json()
        username = body.get("username", "")
        password = body.get("password", "")

        if not username or not password:
            return JSONResponse(status_code=400, content={"error": "Username and password required"})

        import os
        _, cred_path = _get_canonical_vpn_paths()
        os.makedirs(os.path.dirname(cred_path), exist_ok=True)
        with open(cred_path, "w", newline='\n', encoding="utf-8") as f:
            f.write(f"{username}\n{password}\n")
        try:
            os.chmod(cred_path, 0o600)
        except Exception:
            pass
        # Mirror to legacy
        try:
            legacy_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vpn_configs")
            os.makedirs(legacy_dir, exist_ok=True)
            legacy_path = os.path.join(legacy_dir, "credentials.txt")
            if os.path.abspath(legacy_path) != os.path.abspath(cred_path):
                with open(legacy_path, "w", newline='\n', encoding="utf-8") as lf:
                    lf.write(f"{username}\n{password}\n")
                try:
                    os.chmod(legacy_path, 0o600)
                except Exception:
                    pass
        except Exception as e:
            _debug(f"  [vpn] legacy cred mirror failed: {e}")

        if shared_state.vpn_manager:
            shared_state.vpn_manager._auth_file = cred_path

        return {"ok": True}

    # ── SOCKS5 proxy endpoints ──

    @app.get("/api/vpn/socks5")
    async def get_socks5_proxies():
        """Get list of SOCKS5 proxies."""
        import shared_state
        if shared_state.vpn_manager:
            return {
                "proxies": shared_state.vpn_manager.get_socks5_proxies(),
                "rotate": shared_state.vpn_manager.socks5_rotate,
            }
        return {"proxies": [], "rotate": True}

    @app.post("/api/vpn/socks5")
    async def add_socks5_proxy(request: Request):
        """Add a SOCKS5 proxy."""
        import shared_state
        body = await request.json()
        host = body.get("host", "").strip()
        port = int(body.get("port", 1080))
        username = body.get("username", "").strip()
        password = body.get("password", "").strip()

        if not host:
            return {"error": "Host required"}

        if shared_state.vpn_manager:
            shared_state.vpn_manager.add_socks5_proxy(host, port, username, password)
            return {"ok": True, "proxies": shared_state.vpn_manager.get_socks5_proxies()}
        return {"error": "VPN manager not initialized"}

    @app.post("/api/vpn/socks5/remove")
    async def remove_socks5_proxy(request: Request):
        """Remove a SOCKS5 proxy by index."""
        import shared_state
        body = await request.json()
        index = body.get("index")

        if index is None:
            return {"error": "Index required"}

        if shared_state.vpn_manager:
            shared_state.vpn_manager.remove_socks5_proxy(int(index))
            return {"ok": True, "proxies": shared_state.vpn_manager.get_socks5_proxies()}
        return {"error": "VPN manager not initialized"}

    @app.post("/api/vpn/socks5/toggle")
    async def toggle_socks5_proxy(request: Request):
        """Enable or disable a SOCKS5 proxy."""
        import shared_state
        body = await request.json()
        index = body.get("index")
        enabled = body.get("enabled", True)

        if index is None:
            return {"error": "Index required"}

        if shared_state.vpn_manager:
            shared_state.vpn_manager.toggle_socks5_proxy(int(index), enabled)
            return {"ok": True, "proxies": shared_state.vpn_manager.get_socks5_proxies()}
        return {"error": "VPN manager not initialized"}

    @app.post("/api/vpn/socks5/test")
    async def test_socks5_proxy(request: Request):
        """Test a SOCKS5 proxy connection."""
        import shared_state
        body = await request.json()
        host = body.get("host", "").strip()
        port = int(body.get("port", 1080))

        if not host:
            return {"error": "Host required"}

        if shared_state.vpn_manager:
            result = await shared_state.vpn_manager.test_socks5_proxy(host, port)
            return result
        return {"error": "VPN manager not initialized"}

    @app.post("/api/vpn/proxy-mode")
    async def set_proxy_mode(request: Request):
        """Change the proxy mode (vpn, socks5, direct)."""
        import shared_state
        body = await request.json()
        mode = body.get("mode", "vpn")

        if mode not in ("vpn", "socks5", "direct"):
            return {"error": "Invalid mode. Must be vpn, socks5, or direct"}

        if shared_state.vpn_manager:
            shared_state.vpn_manager.proxy_mode = mode
            return {"ok": True, "proxy_mode": mode}
        return {"error": "VPN manager not initialized"}

    @app.post("/api/vpn/socks5/rotate")
    async def set_socks5_rotation(request: Request):
        """Enable or disable SOCKS5 proxy rotation."""
        import shared_state
        body = await request.json()
        rotate = body.get("rotate", True)

        if shared_state.vpn_manager:
            shared_state.vpn_manager.socks5_rotate = rotate
            return {"ok": True, "rotate": rotate}
        return {"error": "VPN manager not initialized"}

    # ── NordVPN API endpoints ──

    @app.get("/api/vpn/countries")
    async def get_vpn_countries():
        """Get available countries from NordVPN API."""
        import shared_state
        if shared_state.vpn_manager:
            countries = await shared_state.vpn_manager.get_countries()
            return {"countries": countries}
        return {"countries": []}

    @app.post("/api/vpn/discover")
    async def discover_vpn_servers(request: Request):
        """Discover servers via NordVPN API."""
        import shared_state
        if not shared_state.vpn_manager:
            return {"error": "VPN manager not initialized"}
        body = await request.json()
        servers = await shared_state.vpn_manager.discover_servers(
            country_code=body.get("country"),
            group=body.get("group"),
            protocol=body.get("protocol", "openvpn_udp"),
            limit=body.get("limit", 5),
        )
        return {"servers": servers}

    @app.post("/api/vpn/discover-and-add")
    async def discover_and_add_servers(request: Request):
        """Discover servers and add them to rotation."""
        import shared_state
        if not shared_state.vpn_manager:
            return {"error": "VPN manager not initialized"}
        body = await request.json()
        added = await shared_state.vpn_manager.discover_and_add_servers(
            country_code=body.get("country"),
            group=body.get("group"),
            protocol=body.get("protocol", "openvpn_udp"),
            limit=body.get("limit", 5),
        )
        return {"added": added, "count": len(added)}

    @app.post("/api/vpn/save-state")
    async def save_vpn_state():
        """Persist VPN state to disk."""
        import shared_state
        if shared_state.vpn_manager:
            shared_state.vpn_manager.save_state()
            return {"ok": True}
        return {"error": "VPN manager not initialized"}

    @app.get("/api/vpn/scorer-status")
    async def get_scorer_status():
        """Get server scorer status."""
        import shared_state
        if shared_state.vpn_manager and shared_state.vpn_manager._server_scorer:
            return shared_state.vpn_manager._server_scorer.get_status()
        return {"total_servers": 0, "top_5": []}

    @app.get("/api/vpn/export")
    async def export_vpn_config():
        """Export VPN configuration as JSON."""
        import shared_state
        import json
        if not shared_state.vpn_manager:
            return {"error": "VPN manager not initialized"}

        config = shared_state.vpn_manager.get_config()
        status = shared_state.vpn_manager.get_status()
        state = {
            "ip_history": shared_state.vpn_manager._ip_history,
            "total_switches": shared_state.vpn_manager._total_switches,
        }

        export = {
            "config": config,
            "state": state,
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "version": "1.0",
        }
        return export

    @app.post("/api/vpn/import")
    async def import_vpn_config(request: Request):
        """Import VPN configuration from JSON."""
        import shared_state
        body = await request.json()

        if "config" in body:
            config = body["config"]
            if shared_state.vpn_manager:
                shared_state.vpn_manager.update_config(config)

        if "state" in body:
            state = body["state"]
            if shared_state.vpn_manager:
                if "ip_history" in state:
                    shared_state.vpn_manager._ip_history = state["ip_history"]
                if "total_switches" in state:
                    shared_state.vpn_manager._total_switches = state["total_switches"]

        return {"ok": True}

    @app.get("/api/vpn/rotation-rules")
    async def get_rotation_rules():
        """Get rotation rules configuration (hot-reload aware)."""
        import shared_state
        try:
            import config.settings as cfg
            ip_rot = cfg.yaml_get("ip_rotation", default={}) or {}
            rules = ip_rot.get("rotation_rules", [])
            # Prefer in-memory manager if available and non-empty, else YAML
            if shared_state.vpn_manager and shared_state.vpn_manager._rotation_rules:
                rules = shared_state.vpn_manager._rotation_rules
            return {"rules": rules}
        except Exception:
            if shared_state.vpn_manager:
                return {"rules": shared_state.vpn_manager._rotation_rules}
            return {"rules": []}

    @app.post("/api/vpn/rotation-rules")
    async def set_rotation_rules(request: Request):
        """Update rotation rules configuration (hot-reload + persist)."""
        import shared_state
        body = await request.json()
        rules = body.get("rules", [])
        if not isinstance(rules, list):
            return JSONResponse(status_code=400, content={"error": "rules must be a list"})
        # Validate each rule has at least model_pattern or strategy
        for r in rules:
            if not isinstance(r, dict):
                return JSONResponse(status_code=400, content={"error": "each rule must be an object"})
        # Persist to YAML (hot-reload)
        try:
            import config.settings as cfg
            ip_rot = cfg.yaml_get("ip_rotation", default={}) or {}
            ip_rot["rotation_rules"] = rules
            cfg._yaml_data["ip_rotation"] = ip_rot
            cfg.save_yaml_config()
            try:
                cfg.IP_ROTATION["rotation_rules"] = rules
            except Exception:
                pass
        except Exception as e:
            _debug(f"  [vpn] persist rotation_rules failed: {e}")
        if shared_state.vpn_manager:
            shared_state.vpn_manager.set_rotation_rules(rules)
            # also update_config for generic path
            try:
                shared_state.vpn_manager.update_config({"rotation_rules": rules})
            except Exception:
                pass
            try:
                get_event_manager().publish("vpn_config_changed", {"changed": ["rotation_rules"]})
            except Exception:
                pass
            return {"ok": True, "rules": rules}
        return {"ok": True, "rules": rules, "persisted": True}

    @app.get("/api/vpn/schedule")
    async def get_vpn_schedule():
        """Get VPN rotation schedule configuration (hot-reload aware)."""
        import shared_state
        try:
            import config.settings as cfg
            ip_rot = cfg.yaml_get("ip_rotation", default={}) or {}
            schedule = ip_rot.get("schedule", {"enabled": False, "rules": []})
            # Merge with manager config if present
            if shared_state.vpn_manager:
                mgr_sched = shared_state.vpn_manager._config.get("schedule")
                if mgr_sched:
                    schedule = mgr_sched
            return schedule
        except Exception:
            if shared_state.vpn_manager:
                from config.settings import IP_ROTATION
                schedule = IP_ROTATION.get("schedule", {"enabled": False, "rules": []})
                return schedule
            return {"enabled": False, "rules": []}

    @app.post("/api/vpn/schedule")
    async def set_vpn_schedule(request: Request):
        """Update VPN rotation schedule configuration (hot-reload + persist)."""
        import shared_state
        body = await request.json()
        # Accept either {enabled, rules} or raw schedule dict
        schedule = body if isinstance(body, dict) else {}
        if "enabled" not in schedule:
            schedule["enabled"] = False
        if "rules" not in schedule:
            schedule["rules"] = []
        if not isinstance(schedule["rules"], list):
            return JSONResponse(status_code=400, content={"error": "rules must be a list"})
        # Persist to YAML
        try:
            import config.settings as cfg
            ip_rot = cfg.yaml_get("ip_rotation", default={}) or {}
            ip_rot["schedule"] = schedule
            cfg._yaml_data["ip_rotation"] = ip_rot
            cfg.save_yaml_config()
            try:
                cfg.IP_ROTATION["schedule"] = schedule
            except Exception:
                pass
        except Exception as e:
            _debug(f"  [vpn] persist schedule failed: {e}")
        if shared_state.vpn_manager:
            shared_state.vpn_manager._schedule_config = schedule
            try:
                shared_state.vpn_manager.update_config({"schedule": schedule})
            except Exception:
                pass
            try:
                get_event_manager().publish("vpn_config_changed", {"changed": ["schedule"]})
            except Exception:
                pass
            return {"ok": True, "schedule": schedule}
        return {"ok": True, "schedule": schedule, "persisted": True}

    # ── NordVPN App endpoints ──

    @app.get("/api/vpn/nordvpn-available")
    async def check_nordvpn_available():
        """Check if NordVPN desktop app is installed."""
        import shared_state
        if shared_state.vpn_manager:
            from vpn_manager import NordVPNAppController
            controller = NordVPNAppController()
            return {"available": controller.available, "exe": controller._exe}
        return {"available": False, "exe": None}

    @app.get("/api/vpn/nordvpn-countries")
    async def get_nordvpn_app_countries():
        """List available countries from NordVPN app."""
        import shared_state
        if shared_state.vpn_manager:
            from vpn_manager import NordVPNAppController
            controller = NordVPNAppController()
            countries = controller.list_countries()
            return {"countries": countries}
        return {"countries": []}

    @app.get("/api/vpn/nordvpn-cities")
    async def get_nordvpn_app_cities(country: str = ""):
        """List cities in a country from NordVPN app."""
        import shared_state
        if not country:
            return {"error": "Country parameter required"}
        if shared_state.vpn_manager:
            from vpn_manager import NordVPNAppController
            controller = NordVPNAppController()
            cities = controller.list_cities(country)
            return {"cities": cities}
        return {"cities": []}

    @app.get("/api/vpn/nordvpn-status")
    async def get_nordvpn_app_status():
        """Get detailed NordVPN app status."""
        import shared_state
        if shared_state.vpn_manager:
            from vpn_manager import NordVPNAppController
            controller = NordVPNAppController()
            return controller.get_status()
        return {"connected": False, "error": "Not initialized"}

    @app.get("/api/vpn/diagnostic")
    async def vpn_diagnostic():
        """Full diagnostic of the VPN system — check all modes, Docker, dependencies."""
        import shared_state
        import shutil
        import os

        result = {
            "current_mode": None,
            "modes": {},
            "docker": {"available": False, "running": False},
            "nordvpn_app": {"available": False, "exe": None, "status": None},
            "wsl2": {"available": False},
            "openvpn": {"available": False},
            "recommendation": None,
        }

        # Current mode
        if shared_state.vpn_manager:
            result["current_mode"] = shared_state.vpn_manager._mode
            result["status"] = shared_state.vpn_manager.status
            result["ip"] = shared_state.vpn_manager.current_ip

        # Check Docker
        docker_cmd = shutil.which("docker")
        if docker_cmd:
            result["docker"]["available"] = True
            try:
                import subprocess
                r = subprocess.run(
                    [docker_cmd, "info", "--format", "{{.ServerVersion}}"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=0x08000000 if __import__('sys').platform == 'win32' else 0,
                )
                if r.returncode == 0:
                    result["docker"]["version"] = r.stdout.strip()
                    result["docker"]["running"] = True
            except Exception:
                pass

        # Check NordVPN App
        from vpn_manager import NordVPNAppController
        try:
            controller = NordVPNAppController()
            result["nordvpn_app"]["available"] = controller.available
            result["nordvpn_app"]["exe"] = controller._exe
            if controller.available:
                status = controller.get_status()
                result["nordvpn_app"]["status"] = status
        except Exception as e:
            result["nordvpn_app"]["error"] = str(e)

        # Check WSL2
        try:
            import subprocess
            r = subprocess.run(
                ["wsl", "-d", "Ubuntu-22.04", "--", "which", "openvpn"],
                capture_output=True, text=True, timeout=10,
                creationflags=0x08000000 if __import__('sys').platform == 'win32' else 0,
            )
            result["wsl2"]["available"] = r.returncode == 0
        except Exception:
            pass

        # Check OpenVPN native
        openvpn_path = shutil.which("openvpn")
        if openvpn_path:
            result["openvpn"]["available"] = True
            result["openvpn"]["path"] = openvpn_path

        # Recommendation
        if result["nordvpn_app"]["available"]:
            result["recommendation"] = "nordvpn-app"
        elif result["docker"]["running"]:
            result["recommendation"] = "docker"
        elif result["wsl2"]["available"]:
            result["recommendation"] = "wsl2"
        elif result["openvpn"]["available"]:
            result["recommendation"] = "native"
        else:
            result["recommendation"] = "none — install NordVPN or Docker"

        return result

    @app.get("/api/tools")
    async def get_tools(days: int = 7, all: bool = False):
        """Aggregate tool names from recent requests.
        Set ?all=true to include universal tools (Read, Write, etc.)."""
        cache_key = f"tools:{days}:{all}"
        cached = _tools_cache.get(cache_key)
        if cached is not None:
            return cached

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
        _tools_cache.set(cache_key, result)
        return result

    # ── Stats & history ──

    @app.get("/api/history")
    async def get_history(from_date: str = None, to_date: str = None, limit: int = 20, offset: int = 0,
                          status: str = None, model: str = None, original_model: str = None,
                          account: str = None, tool: str = None, search: str = None, is_stream: str = None):
        where, params = _build_where(from_date, to_date, status, model, original_model, account, tool, search, is_stream)
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
                    "client_ip": r["client_ip"] if "client_ip" in r.keys() else None,
                    "account_alias": r["account_alias"] if "account_alias" in r.keys() else None,
                    "is_free_model": "-free" in (r["model"] or ""),
                    "free_ip": r["free_model_ip"] if "free_model_ip" in r.keys() and r["free_model_ip"] else "",
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

    @app.get("/api/requests/{req_id}")
    async def get_request_detail(req_id: str):
        """Return full request details including request/response bodies."""
        def _query_request():
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
            return row

        row = await asyncio.to_thread(_query_request)
        if not row:
            return JSONResponse(status_code=404, content={"error": "Request not found"})

        def _parse_json_field(val):
            if not val or val == "[]" or val == "null":
                return None
            try:
                return json.loads(val)
            except Exception:
                return val

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
            "is_stream": bool(row["is_stream"]) if "is_stream" in row.keys() else False,
            "thinking": row["thinking"] if "thinking" in row.keys() else None,
            "effort": row["effort"] if "effort" in row.keys() else None,
            "client_ip": row["client_ip"] if "client_ip" in row.keys() else None,
            "client_user_agent": row["client_user_agent"] if "client_user_agent" in row.keys() else None,
            "account_alias": row["account_alias"] if "account_alias" in row.keys() else None,
            "tools": _parse_json_field(row["tools"]) if "tools" in row.keys() else [],
            "tools_used": _parse_json_field(row["tools_used"]) if "tools_used" in row.keys() else [],
            "request_body": _parse_json_field(row["request_body"]) if "request_body" in row.keys() else None,
            "response_body": _parse_json_field(row["response_body"]) if "response_body" in row.keys() else None,
        }

    @app.get("/api/history/filters")
    async def get_history_filters():
        """Return unique values for history filter dropdowns."""
        def _query_filters():
            models = [r[0] for r in conn.execute(
                "SELECT DISTINCT model FROM requests WHERE model IS NOT NULL ORDER BY model").fetchall()]
            orig_models = [r[0] for r in conn.execute(
                "SELECT DISTINCT original_model FROM requests WHERE original_model IS NOT NULL ORDER BY original_model").fetchall()]
            accounts = [r[0] for r in conn.execute(
                "SELECT DISTINCT account_alias FROM requests WHERE account_alias IS NOT NULL ORDER BY account_alias").fetchall()]
            # Extract all unique tool names from tools_used JSON arrays
            tool_set = set()
            for row in conn.execute("SELECT tools_used FROM requests WHERE tools_used IS NOT NULL AND tools_used != '[]'"):
                try:
                    tools = json.loads(row[0])
                    if isinstance(tools, list):
                        tool_set.update(tools)
                except (json.JSONDecodeError, TypeError):
                    pass
            return {
                "models": models,
                "original_models": orig_models,
                "accounts": accounts,
                "tools_used": sorted(tool_set),
            }
        return await asyncio.to_thread(_query_filters)

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