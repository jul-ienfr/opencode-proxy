"""
Dashboard API endpoints: stats, logs, history, config, static files.
"""

import json
import os
import time
import asyncio
import hmac
import re
import socket
import threading
from datetime import datetime, timedelta, timezone
from fastapi import Request, Response
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import MODELS, HOST, PORT, WEB_PORT, PROXY, API_KEY, CONFIG_KEYS, save_env, apply_server_changes, CUSTOM_ROUTES, save_custom_routes, API_KEYS, save_api_keys, API_KEY_ROUTING
import config.settings as config_settings
from .display import log_lines, debug as _debug
from .events import get_event_manager
from .quota import get_quota_snapshot, get_available_models, get_model_limits_for_all, get_model_capabilities_for_all

from traffic_capture import capture as _traffic_capture

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

# ── Dashboard auth (opt-in via DASHBOARD_TOKEN env) ──
# When DASHBOARD_TOKEN is set, sensitive endpoints require the header
# `X-Dashboard-Token` (constant-time comparison). Unset → legacy open access
# (documented: set DASHBOARD_TOKEN when the server is exposed beyond localhost).

_DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()


def _check_dashboard_token(request: Request):
    """Return a 401 response if the token is configured and not provided."""
    if not _DASHBOARD_TOKEN:
        return None
    provided = request.headers.get("X-Dashboard-Token", "")
    if provided and hmac.compare_digest(provided, _DASHBOARD_TOKEN):
        return None
    return JSONResponse(status_code=401, content={
        "error": "unauthorized",
        "message": "Valid X-Dashboard-Token header required (DASHBOARD_TOKEN env).",
    })


# Secret fields that are never shipped to the browser (masked instead) and
# must be preserved on write when the POST carries an empty value (the UI
# posts '' for unchanged secret inputs — see static/app.js).
_SECRET_FIELDS = ("api_key", "go_workspace_id", "go_auth_cookie")


def _merge_preserved_api_keys(rows: list) -> list:
    """Merge posted api-key rows over the stored ones, KEEPING the stored
    secret when a posted secret field is empty (finding i).

    Keys by ``alias`` (the UI's stable identity for a row). A row that only
    carries masked placeholders + alias will therefore leave its secrets
    untouched instead of blanking them.
    """
    if not isinstance(rows, list):
        return rows if isinstance(rows, list) else []
    stored = {str(k.get("alias", "")): k for k in API_KEYS if k.get("alias")}
    out = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        merged = dict(row)
        old = stored.get(str(row.get("alias", "")))
        if old:
            for field in _SECRET_FIELDS:
                if not merged.get(field) and old.get(field):
                    merged[field] = old[field]
        out.append(merged)
    return out


# Serialization lock for the dashboard's reads on the SHARED sqlite connection.
# opencode.py passes its own `_db_commit_lock` (register_dashboard db_lock arg)
# so dashboard reads and the proxy's writers (inserts, vacuum, cleanup) can
# never execute concurrently on one connection — that race produced
# sqlite3.InterfaceError aborts observed in the traffic capture (GET frames
# dying with "request aborted: InterfaceError", dur=0ms).
_db_lock = threading.Lock()


def _run_db_locked(fn):
    """Run fn while holding the serialization lock (from the worker thread)."""
    with _db_lock:
        return fn()


async def _db_query_sync(fn):
    """Run a synchronous DB function in a thread pool to avoid blocking the event loop.

    The lock is acquired inside the worker thread — same lock the proxy
    writers hold — so every read is atomic w.r.t. inserts/commits.
    """
    return await asyncio.to_thread(_run_db_locked, fn)


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


def _build_where(from_date=None, to_date=None, status=None, model=None, original_model=None, account=None, tool=None, search=None):
    conditions, params = [], []
    if from_date:
        conditions.append("timestamp >= ?")
        params.append(_normalize_date_bound(from_date, end_of_day=False))
    if to_date:
        conditions.append("timestamp <= ?")
        params.append(_normalize_date_bound(to_date, end_of_day=True))
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
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    return where, params


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date_bound_to_utc(date_str: str, end_of_day: bool) -> str:
    """Jour calendaire local → borne UTC+Z, via le fuseau système (DST-correct)."""
    local_midnight = datetime.fromisoformat(date_str + "T00:00:00")  # naive = heure locale
    local = local_midnight + timedelta(days=1) - timedelta(seconds=1) if end_of_day else local_midnight
    return local.astimezone().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_date_bound(value, end_of_day: bool):
    """YYYY-MM-DD nu (sémantique locale) → borne UTC+Z ; timestamps complets passent tels quels."""
    if isinstance(value, str) and _DATE_ONLY_RE.match(value):
        return _date_bound_to_utc(value, end_of_day)
    return value


def daysAgo(n: int) -> str:
    """Instant UTC exact à J-n (timestamp complet avec Z ; passe _build_where inchangé)."""
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _persist_vpn_config(updates: dict):
    """Persist VPN config changes to config.yaml (non-blocking, best-effort)."""
    try:
        import yaml
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
        if not os.path.exists(config_path):
            return

        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        ip_rot = config.get("ip_rotation", {})

        # Map update keys to config.yaml paths
        key_map = {
            "enabled": "enabled",
            "proxy_mode": "proxy_mode",
            "dual_station": "dual_station",
            "strict_free": "strict_free",
            "quota_per_ip": "quota_per_ip",
            "switch_delay": "switch_delay",
            "docker_container": "docker_container",
            "docker_compose_file": "docker_compose_file",
            "vpn_proxy_port": "vpn_proxy_port",
            "socks5_proxy_port": "socks5_proxy_port",
            "credentials_file": "credentials_file",
            "server_countries": "server_countries",
            "ip_check_url": "ip_check_url",
            "circuit_breaker_threshold": "circuit_breaker_threshold",
            "circuit_breaker_recovery": "circuit_breaker_recovery",
            "backoff_max_delay": "backoff_max_delay",
            "watchdog_interval": "watchdog_interval",
            "identity_rotation": "identity_rotation",
            "server_provider": "server_provider",
            "identity_profiles": "identity_profiles",
            # Identity pool ("un profil par IP") + freshness windows + watchdog
            "identity_diversity": "identity_diversity",
            "identity_max_profiles": "identity_max_profiles",
            "recent_ip_window": "recent_ip_window",
            "recent_ip_max_age": "recent_ip_max_age",
            "watchdog_backoff_base": "watchdog_backoff_base",
            "watchdog_backoff_max": "watchdog_backoff_max",
            "shared_rotation_file": "shared_rotation_file",
            # Station 2 ("double embrayage")
            "socks5_proxy_port_2": "socks5_proxy_port_2",
            "vpn_proxy_port_2": "vpn_proxy_port_2",
            "docker_container_2": "docker_container_2",
            "compose_service_2": "compose_service_2",
            "state_file_2": "state_file_2",
            # [plan 18/08 §4] N-station selector (GUI dropdown 1-10, hot
            # reload) — persisted so the runtime count survives a restart.
            # `dual_station` remains mapped (legacy toggle, inoffensive).
            "station_count": "station_count",
            # [plan 18/08 §3d] VPN technology selector (auto/wireguard/openvpn)
            # + auto-mode thresholds — persisted here so the selection is a
            # first-class config.yaml key (hot-reloaded via the mirror).
            "vpn_stack": "vpn_stack",
            "auto_ov_fail_threshold": "auto_ov_fail_threshold",
            "auto_ov_return_min": "auto_ov_return_min",
            "auto_wg_egress_ticks": "auto_wg_egress_ticks",
            "auto_flip_cooldown_min": "auto_flip_cooldown_min",
        }

        changed = False
        for key, yaml_key in key_map.items():
            if key in updates:
                old_val = ip_rot.get(yaml_key)
                new_val = updates[key]
                if old_val != new_val:
                    ip_rot[yaml_key] = new_val
                    changed = True

        if changed:
            config["ip_rotation"] = ip_rot
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            # [32] keep the in-memory mirror in sync — otherwise the next
            # settings.yaml_set() re-dumps the stale _yaml_data and reverts
            # what we just wrote to disk.
            try:
                from config import settings as _st
                cur = _st._yaml_data.get("ip_rotation")
                if isinstance(cur, dict):
                    # [33] in-place update: settings.IP_ROTATION is a live
                    # reference to this dict — replacing it would orphan
                    # opencode.py's copy and break strict_free hot-reload.
                    cur.update(ip_rot)
                else:
                    _st._yaml_data["ip_rotation"] = dict(ip_rot)
            except Exception:
                pass
            _debug(f"  [vpn] config persisted to {config_path}")

    except Exception as e:
        _debug(f"  [vpn] failed to persist config: {e}")


def _write_credentials_env(username: str, password: str):
    """Write NordVPN credentials to credentials.env — the single source
    gluetun reads via docker-compose env_file ([24]).

    Plaintext on disk by design (docker compose env_file format); the file
    is .gitignored. A container restart is needed for gluetun to pick up
    the new values.
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cred_path = os.path.join(root, "credentials.env")
    with open(cred_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"OPENVPN_USER={username}\nOPENVPN_PASSWORD={password}\n")
    try:
        os.chmod(cred_path, 0o600)
    except Exception:
        pass
    _debug(f"  [vpn] credentials written to {cred_path}")


def register_dashboard(app, static_dir, conn, server_manager_getter=None, token_usage=None, token_lock=None, db_lock=None):
    # Serialize dashboard DB reads with the proxy's writers: opencode.py passes
    # its `_db_commit_lock`; without it a private lock still protects the
    # dashboard's own concurrent reads.
    global _db_lock
    if db_lock is not None:
        _db_lock = db_lock
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
    async def get_config(request: Request):
        err = _check_dashboard_token(request)
        if err:
            return err
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
                    "go_workspace_id_masked": (k.get("go_workspace_id", "")[:4] + "****") if len(k.get("go_workspace_id", "")) > 4 else "",
                    "go_auth_cookie_masked": (k.get("go_auth_cookie", "")[:6] + "****") if len(k.get("go_auth_cookie", "")) > 6 else "",
                    # Finding i: never ship full secrets to the browser. The UI
                    # (static/app.js) shows *_masked placeholders and posts ''
                    # for unchanged fields — the merge in update_api_keys_config
                    # keeps the stored value when a posted secret is empty.
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
        err = _check_dashboard_token(request)
        if err:
            return err
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
        rows = await _db_query_sync(_query_all_tools)
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
        err = _check_dashboard_token(request)
        if err:
            return err
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
        err = _check_dashboard_token(request)
        if err:
            return err
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
    async def get_api_keys_config(request: Request):
        err = _check_dashboard_token(request)
        if err:
            return err
        return {
            "api_keys": [
                {
                    "api_key_masked": (k["api_key"][:4] + "****" + k["api_key"][-4:]) if len(k.get("api_key", "")) > 8 else "****",
                    "has_go_workspace": bool(k.get("go_workspace_id")),
                    "has_go_cookie": bool(k.get("go_auth_cookie")),
                    # Finding i: never ship full secrets to the browser —
                    # only presence flags + masked values. The UI posts ''
                    # for unchanged secret fields; the merge below keeps the
                    # stored value in that case.
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
        err = _check_dashboard_token(request)
        if err:
            return err
        body = await request.json()
        if "api_keys" in body:
            _debug(f"  [config] API keys updated ({len(body['api_keys'])} keys)")
            merged = _merge_preserved_api_keys(body["api_keys"])
            save_api_keys(merged)
        if "routing" in body:
            save_env({"API_KEY_ROUTING": body["routing"]})
        return {"status": "ok", "message": "API keys saved."}

    @app.post("/api/config")
    async def update_config(request: Request):
        err = _check_dashboard_token(request)
        if err:
            return err
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
            # [36] null (not 100%) when there are no requests — the UI shows « — »
            success_rate = (total_success / total_count * 100) if total_count > 0 else None

            totals = {
                "input": total_input, "output": row[1], "cache": total_cache,
                "total": total_input + row[1] + total_cache,
                "count": total_count,
                "success_count": total_success, "fail_count": total_fail,
                "avg_duration_ms": int(row[6]),
                "cache_hit_rate": round(cache_hit_rate, 1),
                "success_rate": round(success_rate, 1) if success_rate is not None else None,
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

        totals, rows, acct_rows = await _db_query_sync(_query_stats)

        sum_total = totals["total"]
        models = {}
        for r in rows:
            t = r[1] + r[2] + r[3]
            m_cache_rate = (r[3] / r[1] * 100) if r[1] > 0 else 0.0
            # [36] null (not 100%) when no requests — the UI shows « — »
            m_success_rate = (r[5] / r[4] * 100) if r[4] > 0 else None
            models[r[0]] = {
                "input": r[1], "output": r[2], "cache": r[3], "total": t,
                "pct": f"{t/sum_total*100:.1f}%" if sum_total else "0%",
                "count": r[4], "success_count": r[5], "fail_count": r[6],
                "avg_duration_ms": int(r[7]),
                "cache_hit_rate": round(m_cache_rate, 1),
                "success_rate": round(m_success_rate, 1) if m_success_rate is not None else None,
            }

        # Per-account stats
        accounts = {}
        for r in acct_rows:
            t = r[1] + r[2] + r[3]
            label = r[0] if r[0] else "(default)"
            a_cache_rate = (r[3] / r[1] * 100) if r[1] > 0 else 0.0
            # [36] null (not 100%) when no requests — the UI shows « — »
            a_success_rate = (r[5] / r[4] * 100) if r[4] > 0 else None
            accounts[label] = {
                "input": r[1], "output": r[2], "cache": r[3], "total": t,
                "pct": f"{t/sum_total*100:.1f}%" if sum_total else "0%",
                "count": r[4], "success_count": r[5], "fail_count": r[6],
                "avg_duration_ms": int(r[7]),
                "cache_hit_rate": round(a_cache_rate, 1),
                "success_rate": round(a_success_rate, 1) if a_success_rate is not None else None,
            }

        result = {"models": models, "accounts": accounts, "totals": totals}
        _stats_cache.set(cache_key, result)
        return result

    @app.get("/api/stats/timeseries")
    async def get_stats_timeseries(from_date: str = None, to_date: str = None, granularity: str = "hour"):
        """Return time-series data for charts: requests count, tokens, avg duration per time bucket."""
        where, params = _build_where(from_date, to_date)

        def _query_timeseries():
            # Group by truncated timestamp
            if granularity == "day":
                trunc_expr = "substr(timestamp, 1, 10)"
            elif granularity == "week":
                # [29] real ISO 8601 week grouping (%G ISO year + %V week 01-53);
                # the old day-of-month / 7 heuristic merged adjacent months.
                trunc_expr = "strftime('%G', timestamp) || '-W' || strftime('%V', timestamp)"
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

        rows = await _db_query_sync(_query_timeseries)

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
                        # [31] Real event (not a comment): comments are invisible
                        # to JS EventSource, so the client's silence watchdog
                        # (app.js) can observe this ping and detect a stalled
                        # stream instead of waiting forever.
                        yield "event: ping\ndata: {}\n\n"
            except (asyncio.CancelledError, RuntimeError):
                pass
            finally:
                await manager.unsubscribe(queue)

        return StreamingResponse(event_generator(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

    # ── Quota endpoints ──

    @app.get("/api/quotas")
    async def get_quotas():
        return await get_quota_snapshot()

    @app.get("/api/free-model-usage")
    async def get_free_model_usage(days: int = 7):
        """Free model usage stats for quota analysis.

        Returns per-model, per-key, per-workspace, per-IP aggregates with
        total requests, tokens, and success/failure counts.
        Also calculates quota reset times per IP.
        """
        def _query():
            where = "WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)"
            params = [f"-{days} days"]
            rows = conn.execute(
                "SELECT timestamp, paid_model, free_model, api_key, workspace_id, "
                "       status, tokens_input, tokens_output, duration_ms, ip "
                "FROM free_model_usage " + where + " ORDER BY timestamp DESC LIMIT 5000",
                params,
            ).fetchall()
            return rows

        rows = await _db_query_sync(_query)

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
                if last.tzinfo is not None:
                    # [30] UTC timestamps carry a Z suffix → normalize to naive UTC
                    # so the comparison below stays naive-vs-naive.
                    last = last.astimezone(datetime.timezone.utc).replace(tzinfo=None)
                reset_at = last + timedelta(hours=QUOTA_WINDOW_HOURS)
                ip_data["reset_at"] = reset_at.strftime("%Y-%m-%dT%H:%M:%SZ")
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

    async def _ip_stats_db(vpn_manager=None) -> dict:
        """Per-IP usage stats for the IP History block (free egress IPs).

        Reads the requests table — free/paid split via the `-free` model
        suffix (the proxy is the only producer of `-free` models), identity
        = fingerprint profile of the most recent request on that IP,
        rotation info (server + time) merged from the _ip_history of BOTH
        stations (dual station — the table only records free_model_ip, not
        the owning tunnel). Cached 10s (_stats_cache), bounded to the top
        100 IPs by request count.
        """
        import shared_state
        cached = _stats_cache.get("ip_stats")
        if cached is not None:
            return cached
        stats: dict = {}
        try:
            def _query_grouped():
                return conn.execute(
                    "SELECT free_model_ip, COUNT(*) AS total,"
                    "       SUM(CASE WHEN model LIKE '%-free' THEN 1 ELSE 0 END),"
                    "       SUM(CASE WHEN model NOT LIKE '%-free' THEN 1 ELSE 0 END),"
                    "       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),"
                    "       SUM(CASE WHEN success = 0 OR success IS NULL THEN 1 ELSE 0 END),"
                    "       MIN(timestamp), MAX(timestamp)"
                    " FROM requests"
                    " WHERE free_model_ip IS NOT NULL AND free_model_ip != ''"
                    " GROUP BY free_model_ip"
                    " ORDER BY COUNT(*) DESC, MAX(timestamp) DESC"
                    " LIMIT 100"
                ).fetchall()

            def _query_identity():
                return conn.execute(
                    "SELECT free_model_ip, identity FROM ("
                    "  SELECT free_model_ip, identity,"
                    "         ROW_NUMBER() OVER (PARTITION BY free_model_ip"
                    "                            ORDER BY timestamp DESC) AS rn"
                    "  FROM requests"
                    "  WHERE free_model_ip IS NOT NULL AND free_model_ip != ''"
                    "    AND identity IS NOT NULL AND identity != ''"
                    "  ORDER BY timestamp DESC LIMIT 1000"
                    ") WHERE rn = 1"
                ).fetchall()

            rows = await _db_query_sync(_query_grouped)
            id_rows = await _db_query_sync(_query_identity)
            # Both stations' _ip_history merged — the newer entry wins
            # (dedup by IP so the free_ip_model table's per-IP rotation data
            # reflects whichever tunnel last served that IP).
            def _merged_history():
                merged: dict = {}
                # [plan 18/08 §4] N-station: merge history from EVERY
                # active station (was mgr1+mgr2 fixed pair); falls back to
                # the caller's manager when no registry is present.
                for mgr in (list(getattr(shared_state, "vpn_managers", None) or [])
                            or [vpn_manager]):
                    if mgr is None:
                        continue
                    for h in mgr._ip_history:
                        if not h.get("ip"):
                            continue
                        old = merged.get(h["ip"])
                        if old is None or (h.get("time") or "") >= (old.get("time") or ""):
                            merged[h["ip"]] = h
                return merged

            rotation_by_ip = _merged_history() if vpn_manager else {}
            identity_by_ip = {r[0]: r[1] for r in id_rows}
            for ip, total, free_c, paid_c, ok_c, fail_c, first_seen, last_seen in rows:
                rot = rotation_by_ip.get(ip) or {}
                stats[ip] = {
                    "total": total,
                    "free_count": free_c,
                    "paid_count": paid_c,
                    "success_count": ok_c,
                    "fail_count": fail_c,
                    "identity": identity_by_ip.get(ip),
                    "server": rot.get("server"),
                    "last_rotation": rot.get("time"),
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                }
        except Exception as e:
            _debug(f"  [db] ip_stats query error: {type(e).__name__}: {e}")
            return {}
        _stats_cache.set("ip_stats", stats)
        return stats

    @app.get("/api/vpn-status")
    async def get_vpn_status():
        """Get current VPN status, IP, server, and usage stats."""
        import shared_state
        managers = getattr(shared_state, "vpn_managers", None) or []
        if managers:
            # [plan 18/08 §4] N-station: refresh every active tunnel so the
            # IPs / servers shown in the VPN tab stay live (was mgr1+mgr2).
            for mgr in managers:
                await mgr.refresh_status()
            if shared_state.free_ip_pool:
                data = shared_state.free_ip_pool.get_status()
            else:
                data = managers[0].get_status()
        else:
            data = {"enabled": False, "status": "not_configured"}
        # Per-IP usage stats — injected unconditionally (works in direct mode
        # too: free_ip is then the residential IP). Frontend polls this
        # endpoint every 10s while the vpn tab is active.
        if isinstance(data, dict):
            data["ip_stats"] = await _ip_stats_db(shared_state.vpn_manager)
            # [plan] F: cross-station shared state (recent-IP registry +
            # identity cursor) — lets the VPN tab show why an IP is skipped.
            rot = getattr(shared_state, "shared_rotation", None)
            if rot is not None:
                data["shared_rotation"] = rot.get_status()
            # [plan] C: real-time extras for the VPN panel — docker event
            # watcher state (stream alive, last container event) and the
            # per-station country (current / next / pinned-at / rotation on)
            # overlaid from the managers: the pool status does not carry
            # country fields.
            watcher = getattr(shared_state, "docker_event_watcher", None)
            if watcher is not None:
                data["watch_events"] = watcher.get_status()
            countries = {}
            # [plan 18/08 §4] N-station: per-station country overlay for
            # every active tunnel (was mgr1+mgr2 fixed pair).
            for mgr in managers:
                if mgr is None:
                    continue
                st = mgr.get_status()
                countries[mgr._station] = {
                    "current_country": st.get("current_country"),
                    "next_country": st.get("next_country"),
                    "country_pinned_at": st.get("country_pinned_at"),
                    "country_rotation": st.get("country_rotation"),
                }
            if countries:
                data["countries"] = countries
        return data

    @app.get("/api/vpn-config")
    async def get_vpn_config():
        """Get current VPN configuration."""
        import shared_state
        if shared_state.vpn_manager:
            return shared_state.vpn_manager.get_config()
        return {"enabled": False, "servers": [], "auth_file": "", "protocol": "udp"}

    @app.get("/api/vpn-stack-info")
    async def get_vpn_stack_info():
        """[plan 18/08 §3d] VPN technology selector state — per-station
        effective stack, key presence, reliability counters, flip journal."""
        import shared_state
        # [plan 18/08 §4] N-station: one entry per active station (was a
        # fixed mgr1+mgr2 pair).
        info = {"stations": {}}
        for mgr in getattr(shared_state, "vpn_managers", None) or []:
            info["stations"][str(mgr._station)] = mgr.stack_info()
        return info

    @app.post("/api/vpn-config")
    async def update_vpn_config(request: Request):
        """Update VPN configuration (hot-reload + persist to config.yaml)."""
        err = _check_dashboard_token(request)
        if err:
            return err
        import shared_state
        import os
        body = await request.json()

        # Handle credentials — [24] write the single source gluetun actually
        # reads (credentials.env via docker-compose env_file).
        if "credentials" in body:
            creds = body.pop("credentials")
            username = creds.get("username", "")
            password = creds.get("password", "")
            if username and password:
                _write_credentials_env(username, password)

        # Handle config updates (need VPN manager). [plan] F: fan-out to
        # ALL stations — identity pool, watchdog backoff and freshness
        # windows must stay symmetric on every station (a single shared
        # registry and one absolute identity cursor globalise them).
        managers = getattr(shared_state, "vpn_managers", None) or []
        if managers and body:
            # [plan 18/08 §4] N-station hot-reload — change the number of
            # parallel tunnels at runtime (start/stop compose containers,
            # no proxy restart). Short-circuits when the count did not
            # actually change; _apply_station_count persists config.yaml
            # LAST (coherent on mid-failure).
            if "station_count" in body:
                _new_n = body["station_count"]
                try:
                    _new_n = int(_new_n)
                except (TypeError, ValueError):
                    _new_n = 0
                body.pop("station_count")  # consumed — never fanned out
                if _new_n and _new_n != len(managers):
                    try:
                        from opencode import _apply_station_count
                        await _apply_station_count(_new_n)
                    except Exception as e:
                        _debug(f"  [vpn] station_count hot-reload failed: {e}")
                        return {"error": f"station_count hot-reload failed: {e}"}
                    managers = getattr(shared_state, "vpn_managers", None) or []
            for mgr in managers:
                await mgr.update_config(body)
            # [plan] E: new ip_rotation timings drive the free-IP pool too
            # (rotation_threshold stagger, connect retry, bad TTL, ...).
            pool = getattr(shared_state, "free_ip_pool", None)
            if pool is not None:
                pool.update_config(body)

            # Persist changes to config.yaml. [audit 18/08] vpn_stack is
            # excluded here and persisted AFTER the stack application
            # succeeded — persisting first would leave config.yaml saying
            # wireguard while the effective stack stayed openvpn when
            # set_stack refuses (no vpn_configs/wireguard.env): incoherent
            # persistent state that would also kill auto-flips on reboot
            # (stack != auto wins). The other keys are unconditional.
            _persist_body = {k: v for k, v in body.items() if k != "vpn_stack"}
            _persist_vpn_config(_persist_body)

            # [plan 18/08 §3d] stack selection — applied AFTER the config
            # fan-out (set_stack persists the mode itself into the manager;
            # config.yaml holds the selection via the conditional persist
            # above). _apply_stack recreates ALL stations, so every station
            # beyond the first only mirrors state (propagate=False) — no
            # second compose — and only on success (a refused flip must not
            # desync the other stations from station 1).
            if "vpn_stack" in body:
                _stack_ok = False
                try:
                    _stack_res = await managers[0].set_stack(
                        str(body["vpn_stack"]))
                    _stack_ok = bool(_stack_res.get("ok"))
                    if _stack_ok:
                        _persist_vpn_config({"vpn_stack": str(body["vpn_stack"])})
                    else:
                        _debug(f"  [vpn] set_stack refused: {_stack_res.get('error')} — config.yaml keeps the previous stack")
                except Exception as e:
                    _debug(f"  [vpn] set_stack failed: {e}")
                # only on success — a refused flip must not desync the other
                # stations from station 1 (their mirror would claim a stack
                # the primary never applied).
                if _stack_ok:
                    for _m in managers[1:]:
                        try:
                            await _m.set_stack(str(body["vpn_stack"]), propagate=False)
                        except Exception as e:
                            _debug(f"  [vpn] set_stack (station {_m._station}) failed: {e}")

        config = managers[0].get_config() if managers else {}
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
        """Connect VPN — reconcile status, then connect via compose-managed gluetun."""
        import shared_state
        if not shared_state.vpn_manager:
            return {"error": "VPN manager not initialized"}

        await shared_state.vpn_manager.refresh_status()
        if shared_state.vpn_manager.status == "connected":
            return {"ok": True, "ip": shared_state.vpn_manager.current_ip,
                    "server": shared_state.vpn_manager.current_server}

        try:
            await shared_state.vpn_manager.connect()
            return {"ok": True, "ip": shared_state.vpn_manager.current_ip,
                    "server": shared_state.vpn_manager.current_server}
        except Exception as e:
            return {"error": str(e)}

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
    async def next_vpn(request: Request):
        """Switch to next VPN server.

        Body may carry ``station`` (0 = active station, default; n = station
        n — the latter only within the resolved station_count).
        """
        import shared_state
        managers = getattr(shared_state, "vpn_managers", None) or []
        if not managers:
            return {"error": "VPN manager not initialized"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        station = body.get("station", 0)
        try:
            station = int(station)
        except (TypeError, ValueError):
            station = 0
        if station:
            # [plan 18/08 §4] N-station: 1-indexed lookup in the registry.
            if not (1 <= station <= len(managers)):
                return {"error": f"station {station} not configured "
                                f"(station_count={len(managers)})"}
            mgr = managers[station - 1]
        else:
            # 0 → the station the pool currently routes through.
            mgr = None
        try:
            if shared_state.free_ip_pool:
                if mgr is None:
                    # 0 → the station the pool actually routes through.
                    # switch_ip() rotates the pool's ACTIVE station (which may
                    # be station 2); if we read station 1 here, a successful
                    # station-2 rotation would be reported ok:false with the
                    # wrong station_out — a false negative for the operator.
                    mgr = shared_state.free_ip_pool.active_station or shared_state.vpn_manager
                ip_before = mgr.current_ip
                await shared_state.free_ip_pool.switch_ip(station=mgr)
                ip_after = mgr.current_ip
                server = mgr.current_server
                station_out = mgr._station
            else:
                mgr = mgr or shared_state.vpn_manager
                ip_before = mgr.current_ip
                await mgr.connect_next()
                ip_after = mgr.current_ip
                server = mgr.current_server
                station_out = mgr._station
            ok = bool(ip_after) and (ip_before is None or ip_after != ip_before)
            return {"ok": ok, "ip": ip_after, "server": server, "station": station_out}
        except Exception as e:
            return {"error": str(e)}

    @app.post("/api/vpn/update")
    async def update_vpn():
        """Force-check and apply a pending gluetun image update."""
        import shared_state
        if not shared_state.vpn_manager:
            return {"error": "VPN manager not initialized"}
        try:
            available = await shared_state.vpn_manager.check_update()
            if not available:
                return {"ok": False, "error": "no update available",
                        "update": shared_state.vpn_manager.get_status()["update"]}
            # check_opportune=True ([21]): the manual endpoint must not cut
            # live free streams either — the check runs inside the lock.
            result = await shared_state.vpn_manager.apply_update(check_opportune=True)
            result["update"] = shared_state.vpn_manager.get_status()["update"]
            return result
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/vpn/credentials")
    async def get_vpn_credentials():
        """Check if VPN credentials exist (does not return actual values)."""
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cred_path = os.path.join(root, "credentials.env")
        exists = os.path.exists(cred_path) and os.path.getsize(cred_path) > 0
        username_saved = ""
        if exists:
            try:
                with open(cred_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("OPENVPN_USER="):
                            username_saved = line.split("=", 1)[1].strip()
                            break
            except Exception:
                pass
        return {"exists": exists, "username_preview": username_saved[:4] + "****" if username_saved else ""}

    @app.post("/api/vpn/credentials")
    async def save_vpn_credentials(request: Request):
        """Save NordVPN credentials — writes credentials.env (gluetun env_file)."""
        err = _check_dashboard_token(request)
        if err:
            return err
        body = await request.json()
        username = body.get("username", "")
        password = body.get("password", "")

        if not username or not password:
            return {"error": "Username and password required"}

        _write_credentials_env(username, password)
        return {"ok": True, "note": "restart the VPN container (docker compose up -d) for gluetun to pick up new credentials"}

    @app.post("/api/vpn/save-state")
    async def save_vpn_state():
        """Persist VPN state to disk."""
        import shared_state
        if shared_state.vpn_manager:
            shared_state.vpn_manager.save_state()
            return {"ok": True}
        return {"error": "VPN manager not initialized"}

    @app.get("/api/vpn/export")
    async def export_vpn_config(request: Request):
        """Export VPN configuration as JSON."""
        err = _check_dashboard_token(request)
        if err:
            return err
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
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "version": "1.0",
        }
        return export

    @app.post("/api/vpn/import")
    async def import_vpn_config(request: Request):
        """Import VPN configuration from JSON."""
        err = _check_dashboard_token(request)
        if err:
            return err
        import shared_state
        body = await request.json()

        if "config" in body:
            config = body["config"]
            if shared_state.vpn_manager:
                await shared_state.vpn_manager.update_config(config)

        if "state" in body:
            state = body["state"]
            if shared_state.vpn_manager:
                if "ip_history" in state:
                    shared_state.vpn_manager._ip_history = state["ip_history"]
                if "total_switches" in state:
                    shared_state.vpn_manager._total_switches = state["total_switches"]

        return {"ok": True}

    # ── Traffic capture (Wireshark-like raw request view) ──

    @app.get("/api/traffic/status")
    async def get_traffic_status(request: Request):
        err = _check_dashboard_token(request)
        if err:
            return err
        return _traffic_capture.status()

    @app.post("/api/traffic/config")
    async def set_traffic_config(request: Request):
        err = _check_dashboard_token(request)
        if err:
            return err
        body = await request.json()
        _traffic_capture.configure(
            enabled=body.get("enabled"),
            max_frames=body.get("max_frames"),
            body_cap=body.get("body_cap"),
            max_bytes=body.get("max_bytes"),
        )
        _debug(f"  [traffic] config updated -> {json.dumps(body)}")
        return {"ok": True, **(_traffic_capture.status())}

    @app.get("/api/traffic/frames")
    async def get_traffic_frames(request: Request, limit: int = 200, offset: int = 0,
                                 method: str = None, path: str = None,
                                 status: int = None, aborted: bool = None,
                                 since: float = None):
        err = _check_dashboard_token(request)
        if err:
            return err
        frames = _traffic_capture.frames(limit=limit, offset=offset,
                                         method=method, path=path,
                                         status=status, aborted=aborted,
                                         since=since)
        return {"frames": frames, "total": len(frames), "status": _traffic_capture.status()}

    @app.get("/api/traffic/frames/{frame_id}")
    async def get_traffic_frame_detail(request: Request, frame_id: int):
        err = _check_dashboard_token(request)
        if err:
            return err
        frame = _traffic_capture.frame_detail(frame_id)
        if frame is None:
            return JSONResponse(status_code=404, content={"error": "frame not found"})
        return frame

    @app.post("/api/traffic/clear")
    async def clear_traffic(request: Request):
        err = _check_dashboard_token(request)
        if err:
            return err
        n = _traffic_capture.clear()
        _debug(f"  [traffic] capture cleared ({n} frames)")
        return {"ok": True, "cleared": n}

    @app.get("/api/traffic/stats")
    async def get_traffic_stats(request: Request, window: float = 60.0):
        err = _check_dashboard_token(request)
        if err:
            return err
        return _traffic_capture.stats(window=max(1.0, window))

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
        rows = await _db_query_sync(_query_tools)

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
                          account: str = None, tool: str = None, search: str = None):
        where, params = _build_where(from_date, to_date, status, model, original_model, account, tool, search)
        query = "SELECT * FROM requests " + where + " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        def _query_history():
            rows = conn.execute(query, params).fetchall()
            count_query = "SELECT COUNT(*) FROM requests " + where
            total_row = conn.execute(count_query, params[:-2]).fetchone()
            total_count = total_row[0] if total_row else 0
            return rows, total_count

        rows, total_count = await _db_query_sync(_query_history)

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
    async def get_request_detail(req_id: str, request: Request):
        """Return full request details including request/response bodies."""
        err = _check_dashboard_token(request)
        if err:
            return err

        def _query_request():
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
            return row

        row = await _db_query_sync(_query_request)
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
        return await _db_query_sync(_query_filters)

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
                conn.execute("DELETE FROM requests WHERE timestamp < ?", (_normalize_date_bound(before, end_of_day=True),))
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

        await _db_query_sync(_delete)
        return {"status": "deleted"}