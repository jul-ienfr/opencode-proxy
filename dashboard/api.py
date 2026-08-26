"""
Dashboard API endpoints: stats, logs, history, config, static files.
"""

import asyncio
import hmac
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import config.settings as config_settings
from config import (
    API_KEY,
    API_KEY_ROUTING,
    API_KEYS,
    HOST,
    MODELS,
    PORT,
    PROXY,
    apply_server_changes,
    save_api_keys,
    save_custom_routes,
    save_env,
)
from traffic_capture import capture as _traffic_capture

from .display import debug as _debug
from .display import log_lines
from .events import get_event_manager
from .quota import (
    get_available_models,
    get_model_capabilities_for_all,
    get_model_limits_for_all,
    get_quota_snapshot,
)

# Windows: masquer la fenêtre console des subprocess (évite le flash noir 1s)
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
if hasattr(subprocess, "CREATE_NO_WINDOW"):
    _CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ── Simple TTL cache for expensive dashboard queries ──


class _TTLCache:
    """In-memory cache with TTL for reducing redundant DB scans."""

    def __init__(self, ttl: float = 2.0):
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


_stats_cache = _TTLCache(ttl=2.0)

# [P2 perf] caches dérivés : filtres historique (scan lourd DISTINCT + JSON),
# COUNT de pagination — invalidés par DELETE /api/history.
_filters_cache: dict | None = None
_tools_provider = None  # set[str] des tools utilisés, maintenu côté writer proxy
_shared_conn = None  # connexion sqlite partagée du proxy (écritures)

# Cache for local IP resolution (rarely changes)
_local_ips_cache: tuple[float, list] | None = None

# ── [P1 perf] Traffic capture lazy (on-demand) ──
# La capture ne tourne que si un onglet Traffic regarde réellement (poll auto
# ≤ 3 s < TTL). Zéro viewer → enabled=False → le middleware est un pur
# passthrough sans coût (traffic_capture.py chemin disabled). Le toggle
# utilisateur reste mémorisé : la capture reprend dès qu'un viewer revient.
_TRAFFIC_VIEWER_TTL = 15.0
_traffic_viewer_until = 0.0  # monotonic deadline du dernier viewer
_traffic_user_enabled: bool | None = None  # None = pas encore lu du YAML


def _traffic_mark_viewer() -> None:
    """Un endpoint Traffic vient d'être consulté : garder la capture vivante."""
    global _traffic_viewer_until
    _traffic_viewer_until = time.monotonic() + _TRAFFIC_VIEWER_TTL
    _traffic_apply_lazy()


def _traffic_apply_lazy() -> None:
    global _traffic_user_enabled
    if _traffic_user_enabled is None:
        # Première lecture : respecter le réglage boot de config.yaml
        # (même clé/défaut que opencode.py §Traffic Capture).
        try:
            _traffic_user_enabled = bool(config_settings.yaml_get("traffic", "enabled", True))
        except Exception:
            _traffic_user_enabled = True
    watched = time.monotonic() < _traffic_viewer_until
    wanted = bool(_traffic_user_enabled and watched)
    if _traffic_capture.enabled != wanted:
        _traffic_capture.configure(enabled=wanted)

# ── Dashboard auth (opt-in via DASHBOARD_TOKEN env) ──
# When DASHBOARD_TOKEN is set, sensitive endpoints require the header
# `X-Dashboard-Token` (constant-time comparison). Unset → legacy open access
# (documented: set DASHBOARD_TOKEN when the server is exposed beyond localhost).
# [F-H6] Fail-closed when DASHBOARD_REQUIRE_TOKEN=true and host 0.0.0.0 without token.

_DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()
_DASHBOARD_REQUIRE_TOKEN = os.getenv("DASHBOARD_REQUIRE_TOKEN", "").strip().lower() in (
    "1",
    "true",
    "yes",
)
_host_for_warning = "0.0.0.0"
try:
    _host_for_warning = (os.getenv("OPENCODE_HOST", "") or "").strip()
    if not _host_for_warning:
        import yaml as _yaml_warn

        try:
            with open(
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"
                ),
                encoding="utf-8",
            ) as _f:
                _yw = _yaml_warn.safe_load(_f) or {}
                _host_for_warning = str(_yw.get("server", {}).get("host", "0.0.0.0"))
        except Exception:
            _host_for_warning = "0.0.0.0"
    if _host_for_warning == "0.0.0.0" and not _DASHBOARD_TOKEN:
        import logging as _logging_warn

        _msg = (
            "DASHBOARD_TOKEN not set while host is 0.0.0.0 — dashboard is open to LAN. "
            "Set DASHBOARD_TOKEN in .env (header X-Dashboard-Token) for any non-localhost deployment."
        )
        if _DASHBOARD_REQUIRE_TOKEN:
            _msg += " DASHBOARD_REQUIRE_TOKEN=true — dashboard will deny sensitive endpoints (403)."
        _logging_warn.getLogger("dashboard").warning(_msg)
except Exception:
    pass

# [Axe 3.4] config.yaml manual-edit detector. Hot-reload is push-only BY
# DESIGN (no file watcher auto-reload) — this only tracks whether the file
# changed on disk since the last dashboard write, and exposes it as
# ``config_yaml_dirty`` in /api/vpn-status so the GUI can banner.
#  Never auto-reload: a user editing config.yaml by hand must restart or
# re-push to get a consistent state.
__CONFIG_YAML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"
)
_config_yaml_known_mtime: float = 0.0


def _config_yaml_mtime() -> float:
    try:
        return os.stat(__CONFIG_YAML_PATH).st_mtime
    except OSError:
        return 0.0


_config_yaml_known_mtime = _config_yaml_mtime()


def _check_dashboard_token(request: Request):
    """Return a 401/403 response if the token is configured (or required) and not provided."""
    # [plan v10 §14.0.3 — v9 zéro friction] loopback + LAN de confiance passent
    # SANS token : le réseau est le seul identifiant. Le token reste exigé pour
    # les réseaux non approuvés et quand DASHBOARD_REQUIRE_TOKEN est actif.
    try:
        _client = getattr(request, "client", None)
        _ip = str(getattr(_client, "host", "") or "") if _client else ""
        from trust import ip_trusted, is_loopback

        if (_ip and (is_loopback(_ip) or ip_trusted(_ip))) and not _DASHBOARD_REQUIRE_TOKEN:
            return None
    except Exception:
        pass  # décision réseau indisponible → comportement historique ci-dessous
    if not _DASHBOARD_TOKEN:
        if _DASHBOARD_REQUIRE_TOKEN and _host_for_warning == "0.0.0.0":
            return JSONResponse(
                status_code=403,
                content={
                    "error": "forbidden",
                    "message": "DASHBOARD_TOKEN requis quand host est 0.0.0.0 (définissez DASHBOARD_TOKEN env ou DASHBOARD_REQUIRE_TOKEN=false).",
                },
            )
        return None
    provided = request.headers.get("X-Dashboard-Token", "")
    if provided and hmac.compare_digest(provided, _DASHBOARD_TOKEN):
        return None
    return JSONResponse(
        status_code=401,
        content={
            "error": "unauthorized",
            "message": "En-tête X-Dashboard-Token valide requis (env DASHBOARD_TOKEN).",
        },
    )


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


# ── [P2 perf] Connexion READ-ONLY dédiée pour les lectures dashboard ──
# WAL autorise des lecteurs concurrents du writer : une agrégation lourde ne
# doit plus bloquer les inserts du proxy jusqu'à 30 s (le lock partagé reste
# réservé aux écritures/maintenance via _db_query_sync). Une connexion par
# thread worker (jamais partagée entre threads), fallback sur la connexion
# partagée sous lock si la connexion RO est indisponible (tests :memory:, DB
# absente) ou en échec transitoire.
_ro_conns = threading.local()
_ro_db_path: str = ""


def _resolve_ro_path(conn) -> str:
    try:
        for row in conn.execute("PRAGMA database_list"):
            # row = (seq, name, file) — file vide pour :memory:
            if row[1] == "main" and row[2]:
                return row[2]
    except Exception:
        pass
    return ""


def _get_ro_conn():
    """Connexion sqlite read-only du thread courant (ou None si indisponible)."""
    c = getattr(_ro_conns, "conn", None)
    cur_path = getattr(_ro_conns, "path", "")
    if c is not None and cur_path == _ro_db_path:
        try:
            c.execute("SELECT 1").fetchone()
            return c
        except sqlite3.Error:
            try:
                c.close()
            except Exception:
                pass
            _ro_conns.conn = None
    else:
        try:
            if c is not None:
                c.close()
        except Exception:
            pass
        _ro_conns.conn = None
    if not _ro_db_path:
        return None
    try:
        from pathlib import PurePath

        uri = PurePath(_ro_db_path).as_uri() + "?mode=ro"
        c = sqlite3.connect(uri, uri=True, timeout=5.0)
        c.row_factory = sqlite3.Row
        _ro_conns.conn = c
        _ro_conns.path = _ro_db_path
        return c
    except Exception:
        return None


async def _db_read_sync(fn):
    """Exécute fn(db) hors loop sur la connexion RO dédiée — SANS le lock writer.

    fn prend la connexion en paramètre. Toute erreur de lecture RO (busy bref,
    schema drift) retombe sur la voie sûre : connexion partagée sous _db_lock.
    """
    ro = _get_ro_conn()

    def _run():
        if ro is not None:
            try:
                return fn(ro)
            except sqlite3.Error:
                pass  # busy/drift → retry court puis fallback sous lock
            try:
                time.sleep(0.05)
                return fn(ro)
            except sqlite3.Error:
                pass
        return _run_db_locked(lambda: fn(_shared_conn))

    return await asyncio.to_thread(_run)


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
            if addr and not addr.startswith("127.") and "." in addr:
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


def _build_where(
    from_date=None,
    to_date=None,
    status=None,
    model=None,
    original_model=None,
    account=None,
    tool=None,
    search=None,
    station=None,
):
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
    # [plan v10 §4 Lot 4] filtre per-station (?station=N) — colonne ajoutée
    # au Lot 4 (NULL = requêtes payées/directes).
    if station is not None and str(station).strip() != "":
        try:
            conditions.append("station = ?")
            params.append(int(station))
        except (TypeError, ValueError):
            pass
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
    local = (
        local_midnight + timedelta(days=1) - timedelta(seconds=1) if end_of_day else local_midnight
    )
    return local.astimezone().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_date_bound(value, end_of_day: bool):
    """YYYY-MM-DD nu (sémantique locale) → borne UTC+Z ; timestamps complets passent tels quels."""
    if isinstance(value, str) and _DATE_ONLY_RE.match(value):
        return _date_bound_to_utc(value, end_of_day)
    return value


def daysAgo(n: int) -> str:
    """Instant UTC exact à J-n (timestamp complet avec Z ; passe _build_where inchangé)."""
    return (datetime.now(UTC) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_costs(rows, pricing: dict | None = None) -> dict:
    """[v10 §12.3.10] Coût payant + économies free à partir de lignes agrégées.

    ``rows`` : iterable de dicts {model, tokens_input, tokens_output}.
    ``pricing`` : {"currency", "defaults": {input/output_per_mtok},
    "per_model": {modèle: {...}}} — sinon défauts Anthropic Sonnet-class.

    Les modèles `-free` ne coûtent rien mais génèrent ``free_saved_usd``
    (ce qu'ils auraient coûté au tarif par défaut)."""
    p = pricing or {}
    currency = str(p.get("currency", "USD"))
    defaults = p.get("defaults") if isinstance(p.get("defaults"), dict) else {}
    rate_in = float(defaults.get("input_per_mtok", 3.0))
    rate_out = float(defaults.get("output_per_mtok", 15.0))
    per_model_cfg = p.get("per_model") if isinstance(p.get("per_model"), dict) else {}

    paid_usd = 0.0
    saved_usd = 0.0
    per_model_out = []
    seen = set()
    for row in rows:
        model = str(row.get("model") or "?")
        ti = float(row.get("tokens_input") or 0)
        to_ = float(row.get("tokens_output") or 0)
        key = (model, ti, to_)
        if key in seen:
            continue
        seen.add(key)
        rates = per_model_cfg.get(model) or {}
        ri = float(rates.get("input_per_mtok", rate_in))
        ro_ = float(rates.get("output_per_mtok", rate_out))
        cost = ti / 1e6 * ri + to_ / 1e6 * ro_
        is_free = model.endswith("-free")
        if is_free:
            saved_usd += cost
        else:
            paid_usd += cost
        per_model_out.append(
            {
                "model": model,
                "tokens_input": int(ti),
                "tokens_output": int(to_),
                "cost_usd": round(cost, 4),
                "free": is_free,
            }
        )
    per_model_out.sort(key=lambda x: -x["cost_usd"])
    return {
        "currency": currency,
        "paid_usd": round(paid_usd, 4),
        "free_saved_usd": round(saved_usd, 4),
        "per_model": per_model_out,
    }


_persist_lock = __import__("threading").Lock()


def _validate_vpn_config_payload(body) -> list:
    """[v10 §12.1.4] validation DRY-RUN d'un payload VPN — retourne la liste
    des erreurs (vide = valide). Mêmes règles que les endpoints d'application,
    sans toucher au moindre état."""
    errors: list = []
    if not isinstance(body, dict):
        return ["le payload doit être un objet"]

    if "station_count" in body:
        try:
            v = int(body["station_count"])
            if not 1 <= v <= 10:
                errors.append("station_count doit être entre 1 et 10")
        except (TypeError, ValueError):
            errors.append("station_count doit être un entier")

    lr = body.get("latency_rotation")
    if lr is not None:
        if not isinstance(lr, dict):
            errors.append("latency_rotation doit être un objet")
        else:
            _ranges = {
                "slow_threshold_ms": (100, 60000),
                "ewma_threshold_ms": (100, 60000),
                "p95_threshold_ms": (100, 120000),
                "soft_cooldown_sec": (30, 7200),
                "hard_cooldown_sec": (30, 14400),
                "floor_ms": (0, 30000),
            }
            for k, (lo, hi) in _ranges.items():
                if k in lr:
                    try:
                        v = float(lr[k])
                        if not lo <= v <= hi:
                            errors.append(f"latency_rotation.{k}={v} hors bornes [{lo}, {hi}]")
                    except (TypeError, ValueError):
                        errors.append(f"latency_rotation.{k} doit être numérique")
            for k in ("consecutive_slow", "min_requests_before_eval", "max_soft_rotates_per_hour"):
                if k in lr:
                    try:
                        v = int(lr[k])
                        if not 1 <= v <= 50:
                            errors.append(f"latency_rotation.{k}={v} hors bornes [1, 50]")
                    except (TypeError, ValueError):
                        errors.append(f"latency_rotation.{k} doit être un entier")
            sm = lr.get("stream_metric")
            if sm is not None and sm not in ("ttfb", "total"):
                errors.append("latency_rotation.stream_metric doit valoir ttfb|total")
            pm = lr.get("slow_threshold_ms_per_model")
            if pm is not None and not isinstance(pm, dict):
                errors.append("slow_threshold_ms_per_model doit être un objet {modèle: ms}")

    per = body.get("per_station")
    if per is not None:
        if not isinstance(per, dict):
            errors.append("per_station doit être un objet {\"N\": {...}}")
        else:
            allowed_keys = {"quota_per_ip", "country_offset", "country_offset_stride",
                            "watchdog_interval", "proxy_mode"}
            for sid_key, ov in per.items():
                if str(sid_key).strip() not in {str(i) for i in range(1, 11)}:
                    errors.append(f"per_station: station invalide {sid_key!r} (1..10)")
                    continue
                if not isinstance(ov, dict):
                    errors.append(f"per_station[{sid_key}] doit être un objet")
                    continue
                unknown = set(ov) - allowed_keys
                if unknown:
                    errors.append(f"per_station[{sid_key}]: clés non supportées {sorted(unknown)}")

    tr = body.get("trust") if isinstance(body.get("trust"), dict) else None
    if tr is not None and tr.get("mode") not in (None, "lan", "open", "off"):
        errors.append("dashboard_trust.mode doit valoir lan|open|off")

    return errors


async def _apply_import_payload(mgrs, body: dict, fallback_manager=None):
    """[v10 §14.3.31] applique ``{config, state}`` aux managers fournis.

    Retourne ``(response_dict, status_code)``. Validation schéma AVANT
    application ; fan-out TOUTES les stations (l'ancien code n'appliquait
    la config qu'à la station 1)."""
    if "config" in body and not isinstance(body["config"], dict):
        return {"error": "'config' doit être un objet"}, 400
    if "state" in body:
        state = body["state"]
        if not isinstance(state, dict) or not all(k in ("ip_history", "total_switches") for k in state):
            return {"error": "'state' ne supporte que ip_history/total_switches"}, 400

    applied = 0
    if "config" in body:
        config = body["config"]
        targets = list(mgrs or [])
        if not targets and fallback_manager is not None:
            targets = [fallback_manager]
        for m in targets:
            try:
                await m.update_config(config)
                applied += 1
            except Exception as e:
                _debug(f"  [vpn] import config st{getattr(m, '_station', '?')} failed: {e}")

    if "state" in body and fallback_manager is not None:
        state = body["state"]
        if "ip_history" in state:
            fallback_manager._ip_history = state["ip_history"]
        if "total_switches" in state:
            fallback_manager._total_switches = state["total_switches"]

    return {"ok": True, "stations_applied": applied}, 200


def _persist_vpn_config(updates: dict):
    """Persist VPN config changes to config.yaml (non-blocking, best-effort). [32]

    [v10 §14.3.30] retourne **None en cas de succès**, une STRING d'erreur
    sinon — le contrat implicite des callers (`err = _persist...; if not err:`)
    devient enfin réel (l'ancienne version ne retournait jamais rien → les
    gardes d'erreur étaient mortes)."""
    global _config_yaml_known_mtime
    try:
        import yaml

        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
        if not os.path.exists(config_path):
            return f"config.yaml introuvable: {config_path}"
        with _persist_lock:
            with open(config_path, encoding="utf-8") as f:
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
                # [plan 19/08 §1/§2] free multi-attempt + exception ordering —
                # no container effect (read per-request via IP_ROTATION.get),
                # persisted so the GUI selection survives a restart.
                "max_free_attempts": "max_free_attempts",
                "free_exception_fallback": "free_exception_fallback",
                # [Axe 3.1] socks5 backend (static proxy list, auto-rotate toggle,
                # NordVPN country API, custom .ovpn file) — all persisted config.
                "socks5_proxies": "socks5_proxies",
                "socks5_auto_rotate": "socks5_auto_rotate",
                "use_nordvpn_api": "use_nordvpn_api",
                "custom_ovpn_file": "custom_ovpn_file",
                # warm-avalanche
                "ovpn_protocol": "ovpn_protocol",
                "ovpn_endpoint_port": "ovpn_endpoint_port",
                "auto_hetero_boot": "auto_hetero_boot",
            }

        # [free_parallel] nested dict (B) Stations free — validate + merge (preserve existing keys)
        _fp_changed = False
        if "free_parallel" in updates and isinstance(updates["free_parallel"], dict):
            try:
                import config.settings as _st_fp

                existing_fp = ip_rot.get("free_parallel", {}) if isinstance(ip_rot.get("free_parallel"), dict) else {}
                merged_fp = {**existing_fp, **updates["free_parallel"]}
                norm = _st_fp._normalize_free_parallel(merged_fp)
            except Exception:
                # fallback: shallow merge without normalization
                existing_fp = ip_rot.get("free_parallel", {}) if isinstance(ip_rot.get("free_parallel"), dict) else {}
                norm = {**existing_fp, **updates["free_parallel"]}
            old_fp = ip_rot.get("free_parallel", {})
            if old_fp != norm:
                ip_rot["free_parallel"] = norm
                _fp_changed = True
            # remove so key_map loop doesn't see it
            updates = {k: v for k, v in updates.items() if k != "free_parallel"}
        changed = _fp_changed
        for key, yaml_key in key_map.items():
            if key in updates:
                old_val = ip_rot.get(yaml_key)
                new_val = updates[key]
                if old_val != new_val:
                    ip_rot[yaml_key] = new_val
                    changed = True
        # flat free_parallel_* keys (dashboard compat: free_parallel_routing plat)
        _flat_fp_map = {
            "free_parallel_enabled": ("enabled", lambda v: bool(v)),
            "free_parallel_routing": (
                "routing",
                lambda v: str(v).lower() if str(v).lower() in ("round-robin", "failover") else "round-robin",
            ),
            "free_parallel_mode": (
                "mode",
                lambda v: str(v).lower() if str(v).lower() in ("load-balance", "strict", "hedge") else "load-balance",
            ),
            "free_parallel_hedge_delay_ms": ("hedge_delay_ms", lambda v: max(0, min(2000, int(v)))),
            "free_parallel_hedge_max_attempts": ("hedge_max_attempts", lambda v: max(1, min(3, int(v)))),
        }
        for flat_key, (sub, coerce) in _flat_fp_map.items():
            if flat_key in updates:
                try:
                    new_val = coerce(updates[flat_key])
                except Exception:
                    continue
                if "free_parallel" not in ip_rot or not isinstance(ip_rot.get("free_parallel"), dict):
                    ip_rot["free_parallel"] = {}
                old_val = ip_rot["free_parallel"].get(sub)
                if old_val != new_val:
                    ip_rot["free_parallel"][sub] = new_val
                    changed = True
        # normalize final free_parallel after flat merges (clamp, defaults)
        if "free_parallel" in ip_rot and isinstance(ip_rot["free_parallel"], dict):
            try:
                import config.settings as _st_fp2

                norm2 = _st_fp2._normalize_free_parallel(ip_rot["free_parallel"])
                if ip_rot["free_parallel"] != norm2:
                    ip_rot["free_parallel"] = norm2
                    changed = True
            except Exception:
                pass

        if changed:
            config["ip_rotation"] = ip_rot
            # Atomic write via tempfile + fsync + replace (fiabilise sur panne disque)
            import tempfile

            try:
                dir_name = os.path.dirname(config_path) or "."
                fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".config.yaml.tmp.")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        yaml.dump(
                            config, f, default_flow_style=False, allow_unicode=True, sort_keys=False
                        )
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except OSError:
                            pass
                    os.replace(tmp_path, config_path)
                finally:
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass
            except Exception:
                # Fallback non-atomique
                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.dump(
                        config, f, default_flow_style=False, allow_unicode=True, sort_keys=False
                    )
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
                # [free_parallel] keep FREE_PARALLEL mirror in sync
                try:
                    _st.FREE_PARALLEL.clear()
                    _st.FREE_PARALLEL.update(_st._normalize_free_parallel(ip_rot.get("free_parallel", {})))
                except Exception:
                    pass
            except Exception:
                pass
            # [free_parallel] pool hot-reload for direct _persist calls
            try:
                import shared_state as _ss

                _pool = getattr(_ss, "free_ip_pool", None)
                if _pool is not None and hasattr(_pool, "update_config"):
                    _pool.update_config(ip_rot)
            except Exception:
                pass
            # Best-effort hot-reload des managers pour appels directs (tests, SSE)
            try:
                import shared_state

                mgrs = getattr(shared_state, "vpn_managers", None) or []
                if not mgrs and getattr(shared_state, "vpn_manager", None):
                    mgrs = [shared_state.vpn_manager]
                for m in mgrs:
                    if m and hasattr(m, "_config"):
                        for k in updates:
                            if k in key_map:
                                yaml_key = key_map[k]
                                if yaml_key in ip_rot:
                                    m._config[yaml_key] = ip_rot[yaml_key]
                        if (
                            "circuit_breaker_threshold" in updates
                            or "circuit_breaker_recovery" in updates
                        ):
                            try:
                                from vpn_manager import CircuitBreaker

                                m._circuit_breaker = CircuitBreaker(
                                    failure_threshold=m._config.get("circuit_breaker_threshold", 3),
                                    recovery_time=m._config.get("circuit_breaker_recovery", 300),
                                )
                            except Exception:
                                pass
            except Exception:
                pass
            _debug(f"  [vpn] config persisted to {config_path}")
            _config_yaml_known_mtime = _config_yaml_mtime()
            # keep config/settings mtime in sync so maybe_reload doesn't refire
            try:
                from config import settings as _st2

                _st2._config_yaml_mtime = _config_yaml_known_mtime
            except Exception:
                pass

    except Exception as e:
        _debug(f"  [vpn] failed to persist config: {e}")
        return f"persist config.yaml échoué: {type(e).__name__}: {e}"
    return None


def _persist_free_model_map(mapping: dict):
    """Persist ``free_model_map`` to config.yaml (top-level key, best-effort).

    Unlike ip_rotation keys it does NOT live under ``ip_rotation:`` — it is a
    first-class top-level key (config/settings.py ``yaml_get("free_model_map")``),
    so `_persist_vpn_config`'s key_map cannot reach it.
    """
    try:
        import yaml

        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
        if not os.path.exists(config_path):
            return
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        config["free_model_map"] = dict(mapping)
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        # [Axe 3.4] dashboard wrote the file → not manually dirty.
        global _config_yaml_known_mtime
        _config_yaml_known_mtime = _config_yaml_mtime()
        try:
            from config import settings as _st2

            _st2._config_yaml_mtime = _config_yaml_known_mtime
        except Exception:
            pass
    except Exception as e:
        _debug(f"  [free] failed to persist free_model_map: {e}")


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


# ── [Axe 3.1] SOCKS5 / NordVPN / diagnostic helpers ──
# Every external I/O (NordVPN API, docker CLI, raw proxy probes) lives in
# these small isolated helpers so the dashboard endpoints stay testable
# offline (monkeypatch the helper, never the live system).


def _vpn_proxy_mode() -> str:
    """Current proxy mode for the GUI (vpn | socks5 | direct). Reads the
    live manager registry first, then the config mirror, then defaults to
    'vpn' (clients must never see an empty mode)."""
    try:
        import shared_state

        mgrs = getattr(shared_state, "vpn_managers", None) or []
        if isinstance(mgrs, list) and mgrs:
            pm = getattr(mgrs[0], "proxy_mode", None)
            if pm in ("vpn", "socks5", "direct"):
                return pm
        cfg = getattr(config_settings, "IP_ROTATION", None) or {}
        pm = cfg.get("proxy_mode")
        if pm in ("socks5", "direct"):
            return pm
    except Exception:
        pass
    return "vpn"


def _socks5_payload(pool) -> list:
    """Proxy rows for the GUI — defensive getattr (test fakes / older pools
    lack the new attributes). Passwords are never shipped to the browser."""
    rows = getattr(pool, "_socks5_proxies", None) or []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append(
            {
                "host": r.get("host", ""),
                "port": r.get("port", 0),
                "enabled": bool(r.get("enabled", True)),
                "has_password": bool(r.get("password")),
            }
        )
    return out


def _dashboard_pool():
    """The free-IP pool behind the VPN tab endpoints (None-safe)."""
    try:
        import shared_state

        return getattr(shared_state, "free_ip_pool", None)
    except Exception:
        return None


def _dashboard_managers() -> list:
    """The active VPN manager registry ([] when uninitialized)."""
    try:
        import shared_state

        return getattr(shared_state, "vpn_managers", None) or []
    except Exception:
        return []


def _as_index(value) -> int:
    """Index int strict — -1 sur toute valeur non-indexable (les bornes
    sont vérifiées par l'appelant)."""
    try:
        idx = int(value)
    except (TypeError, ValueError):
        return -1
    return idx


def _apply_socks5_rows(rows: list) -> dict | None:
    """Validate + persist the socks5 list, then push it to the pool.
    Returns an error dict on invalid input, None on success."""
    cleaned = []
    for i, r in enumerate(rows or []):
        if not isinstance(r, dict):
            return {"error": f"ligne {i}: entrée invalide"}
        host = str(r.get("host", "") or "").strip()
        if not host:
            return {"error": f"ligne {i}: host manquant"}
        try:
            port = int(r.get("port") or 0)
        except (TypeError, ValueError):
            return {"error": f"ligne {i}: port invalide"}
        if not (1 <= port <= 65535):
            return {"error": f"ligne {i}: port hors bornes (1-65535)"}
        cleaned.append(
            {
                "host": host,
                "port": port,
                "enabled": bool(r.get("enabled", True)),
                "username": str(r.get("username") or "").strip() or None,
                "password": str(r.get("password") or "").strip() or None,
            }
        )
    err = _persist_vpn_config({"socks5_proxies": cleaned})
    if not err:
        try:
            import shared_state

            pool = getattr(shared_state, "free_ip_pool", None)
            if pool is not None and hasattr(pool, "set_socks5_proxies"):
                pool.set_socks5_proxies(cleaned)
        except Exception as e:
            _debug(f"  [vpn] socks5 pool update failed: {e}")
    return None


def _socks5_auto_rotate_state() -> bool:
    """Current auto-rotate toggle — config mirror first, pool second."""
    try:
        cfg = getattr(config_settings, "IP_ROTATION", None) or {}
        if "socks5_auto_rotate" in cfg:
            return bool(cfg["socks5_auto_rotate"])
        import shared_state

        pool = getattr(shared_state, "free_ip_pool", None)
        if pool is not None and hasattr(pool, "_socks5_auto_rotate"):
            return bool(pool._socks5_auto_rotate)
    except Exception:
        pass
    return True


def _read_exact(sock, n: int) -> bytes:
    """Read exactly n bytes or raise (used for the SOCKS5 reply framing)."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("fermeture de la connexion pendant la réponse SOCKS5")
        buf += chunk
    return buf


def _socks5_raw_get(proxy_host: str, proxy_port: int, target: str, timeout: float = 8.0) -> tuple:
    """Full HTTP(S) GET through a SOCKS5 proxy using raw sockets + ssl (no
    socks-extras dependency). Returns (status, body, elapsed_seconds);
    raises on connectivity failure. The CONNECT handshake doubles as the
    proxy liveness probe."""
    import ipaddress
    import ssl
    from urllib.parse import urlsplit

    u = urlsplit(target)
    host, port = u.hostname, u.port or 443
    started = time.monotonic()
    sock = socket.create_connection((proxy_host, int(proxy_port)), timeout=timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall(b"\x05\x01\x00")  # version 5, 1 method, no-auth
        ver, nmeth = _read_exact(sock, 2)
        if ver != 5 or nmeth != 0:
            raise ConnectionError(f"proxy SOCKS5 a refusé le handshake ({ver}/{nmeth})")
        try:
            ipaddress.ip_address(host)
            atyp, addr = 0x01, socket.inet_aton(host)
        except ValueError:
            atyp, hb = 0x03, host.encode()  # hostname ATYP
            addr = bytes([len(hb)]) + hb
        sock.sendall(b"\x05\x01\x00" + bytes([atyp]) + addr + int(port).to_bytes(2, "big"))
        rep = _read_exact(sock, 4)
        if rep[1] != 0:
            raise ConnectionError(f"CONNECT SOCKS5 refusé (code {rep[1]})")
        atyp = rep[3]
        if atyp == 0x01:
            _read_exact(sock, 6)  # IPv4 addr + port
        elif atyp == 0x04:
            _read_exact(sock, 18)  # IPv6 addr + port
        elif atyp == 0x03:
            ln = _read_exact(sock, 1)[0]
            _read_exact(sock, ln + 2)  # hostname + port
        else:
            raise ConnectionError(f"réponse SOCKS5 atyp={atyp}")
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
            f"User-Agent: opencode-proxy/1.0\r\nConnection: close\r\n\r\n"
        )
        with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as tls:
            tls.sendall(req.encode())
            data = b""
            while True:
                chunk = tls.recv(65536)
                if not chunk:
                    break
                data += chunk
    finally:
        sock.close()
    head, _, body = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return status, body.decode("utf-8", "replace"), time.monotonic() - started


async def _socks5_probe(host: str, port: int) -> dict:
    """Test a static SOCKS5 proxy: egress IP + latency + opencode.ai
    reachability through it (contracts: static/app.js socks5/test)."""
    host = (host or "").strip()
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"ok": False, "error": "port invalide"}
    if not host:
        return {"ok": False, "error": "host manquant"}

    def _run():
        status, body, elapsed = _socks5_raw_get(host, port, "https://api.ipify.org/")
        ip = body.strip()
        try:
            s2, _, _ = _socks5_raw_get(host, port, "https://opencode.ai/")
            oc_ok = 200 <= s2 < 400
        except Exception:
            oc_ok = False
        return ip, (200 <= status < 400), elapsed, oc_ok

    try:
        ip, ok, elapsed, oc_ok = await asyncio.to_thread(_run)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": ok, "ip": ip, "opencode_ok": oc_ok, "latency_ms": round(elapsed * 1000, 1)}


async def _nordvpn_api_fetch(url: str, timeout: float = 10.0) -> dict:
    """One GET against the (free, keyless) NordVPN server API. Isolated so
    tests monkeypatch it — the live HTTP path never runs offline."""

    def _do():
        import httpx

        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            r = c.get(url)
            r.raise_for_status()
            return r.json()

    return await asyncio.to_thread(_do)


_NORDVPN_STATIC_COUNTRIES = [
    {"code": "US", "name": "United States"},
    {"code": "GB", "name": "United Kingdom"},
    {"code": "DE", "name": "Germany"},
    {"code": "FR", "name": "France"},
    {"code": "NL", "name": "Netherlands"},
    {"code": "SE", "name": "Sweden"},
    {"code": "CH", "name": "Switzerland"},
    {"code": "ES", "name": "Spain"},
    {"code": "IT", "name": "Italy"},
    {"code": "BE", "name": "Belgium"},
    {"code": "AT", "name": "Austria"},
    {"code": "DK", "name": "Denmark"},
    {"code": "FI", "name": "Finland"},
    {"code": "NO", "name": "Norway"},
    {"code": "PL", "name": "Poland"},
    {"code": "PT", "name": "Portugal"},
    {"code": "CZ", "name": "Czechia"},
    {"code": "RO", "name": "Romania"},
    {"code": "TR", "name": "Turkey"},
    {"code": "JP", "name": "Japan"},
    {"code": "SG", "name": "Singapore"},
    {"code": "AU", "name": "Australia"},
    {"code": "NZ", "name": "New Zealand"},
    {"code": "CA", "name": "Canada"},
    {"code": "MX", "name": "Mexico"},
    {"code": "ZA", "name": "South Africa"},
    {"code": "IN", "name": "India"},
]


async def _nordvpn_countries(use_api: bool) -> list:
    """Country list for the GUI: live NordVPN API when use_nordvpn_api is
    on, else the static fallback (offline-safe)."""
    if use_api:
        try:
            data = await _nordvpn_api_fetch("https://api.nordvpn.com/v1/servers/countries")
            out = []
            for row in data or []:
                if not isinstance(row, dict):
                    continue
                code = str(row.get("code", "")).upper()
                name = str(row.get("name", "")).strip()
                if code and name:
                    out.append({"code": code, "name": name})
            if out:
                return sorted(out, key=lambda c: c["name"])
        except Exception as e:
            _debug(f"  [nordvpn] countries API failed, static fallback: {e}")
    return list(_NORDVPN_STATIC_COUNTRIES)


async def _nordvpn_servers_by_country(code: str, limit: int = 20) -> list:
    """Recommended OpenVPN servers for a country (NordVPN API). Returns
    [{hostname, country, load}]; empty list on any failure."""
    code = (code or "").strip().upper()
    if not code:
        return []
    try:
        countries = await _nordvpn_api_fetch("https://api.nordvpn.com/v1/servers/countries")
        cid = None
        for row in countries or []:
            if isinstance(row, dict) and str(row.get("code", "")).upper() == code:
                cid = row.get("id")
                break
        if cid is None:
            return []
        data = await _nordvpn_api_fetch(
            "https://api.nordvpn.com/v1/servers/recommendations"
            f"?limit={int(limit)}"
            "&filters[servers_technologies][identifier]=openvpn_udp"
            f"&filters[country_id]={int(cid)}"
        )
        out = []
        for row in data or []:
            if not isinstance(row, dict):
                continue
            host = str(row.get("hostname", "")).strip()
            if host:
                out.append({"hostname": host, "country": code, "load": row.get("load", 0)})
        return out
    except Exception as e:
        _debug(f"  [nordvpn] servers_by_country({code}) failed: {e}")
        return []


def _docker_diag() -> dict:
    """System docker info for the diagnostic bundle — isolated subprocess,
    monkeypatchable (never live in tests)."""
    diag = {"available": False, "running": False, "version": None}
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            diag["available"] = True
            diag["version"] = (r.stdout or r.stderr).strip() or None
    except Exception as e:
        diag["error"] = str(e)
        return diag
    try:
        info = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        )
        diag["running"] = info.returncode == 0
    except Exception:
        diag["running"] = False
    return diag


def _docker_compose_config() -> list:
    """`docker compose config` output per active station (diagnostic
    bundle). Empty list when no managers or docker is absent."""
    try:
        import shared_state

        mgrs = getattr(shared_state, "vpn_managers", None) or []
    except Exception:
        mgrs = []
    out = []
    for m in mgrs:
        df = getattr(m, "_docker_compose_file", None)
        if not df:
            continue
        try:
            r = subprocess.run(
                ["docker", "compose", "-f", df, "config"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=_CREATE_NO_WINDOW,
            )
            out.append(f"=== {df}\n{(r.stdout or r.stderr).strip()}")
        except Exception as e:
            out.append(f"=== {df}\n(erreur: {e})")
    return out


def register_dashboard(
    app,
    static_dir,
    conn,
    server_manager_getter=None,
    token_usage=None,
    token_lock=None,
    db_lock=None,
    tools_provider=None,
):
    # Serialize dashboard DB reads with the proxy's writers: opencode.py passes
    # its `_db_commit_lock`; without it a private lock still protects the
    # dashboard's own concurrent reads.
    global _db_lock, _ro_db_path, _shared_conn, _tools_provider
    if db_lock is not None:
        _db_lock = db_lock
    _shared_conn = conn
    _tools_provider = tools_provider
    _ro_db_path = _resolve_ro_path(conn)
    # Add Cache-Control headers for static assets (JS/CSS/HTML)
    from starlette.middleware.base import BaseHTTPMiddleware

    class _StaticCacheMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            if request.url.path.startswith("/static/"):
                response.headers["Cache-Control"] = "public, max-age=3600"
            return response

    app.add_middleware(_StaticCacheMiddleware)

    # [plan v10 §14.0.3] Confiance réseau zéro-friction : loopback + LAN CIDRs
    # passent sans identifiant ; mutations → même-host anti-CSRF + rate-limit ;
    # réseaux inconnus → 403 (ou token si DASHBOARD_TOKEN configuré).
    from trust import DashboardTrustMiddleware

    app.add_middleware(
        DashboardTrustMiddleware,
        token_getter=lambda: _DASHBOARD_TOKEN,
        require_token_getter=lambda: _DASHBOARD_REQUIRE_TOKEN,
    )

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
        # Fast path: local_ips and models are slow (socket, disk), return cached/instant
        try:
            models_info = get_available_models()
        except Exception:
            models_info = []
        # local_ips: use cache if available, otherwise don't block (empty, next poll will fill)
        try:
            if _local_ips_cache and (time.monotonic() - _local_ips_cache[0]) < 60:
                local_ips = _local_ips_cache[1]
            else:
                # don't await — return cached or empty, refresh in background
                local_ips = _local_ips_cache[1] if _local_ips_cache else []
                try:
                    asyncio.create_task(asyncio.to_thread(_get_local_ips))
                except Exception:
                    pass
        except Exception:
            local_ips = []
        return {
            "proxy": PROXY or "",
            "api_key_set": bool(API_KEY),
            "api_key_masked": (API_KEY[:4] + "****" + API_KEY[-4:])
            if API_KEY and len(API_KEY) > 8
            else ("****" if API_KEY else ""),
            "host": HOST,
            "port": PORT,
            "local_ips": local_ips,
            "routes": routes_info,
            "models": models_info,
            "model_limits": get_model_limits_for_all(models_info),
            "model_capabilities": get_model_capabilities_for_all(models_info),
            "proxy_running": server_manager_getter().is_running
            if server_manager_getter and server_manager_getter()
            else True,
            "disable_mapping": config_settings.DISABLE_MAPPING,
            "custom_routes": config_settings.CUSTOM_ROUTES,
            "go_workspace_id_set": bool(config_settings.OPENCODE_GO_WORKSPACE_ID),
            # [plan v10 §14.2.2] ≤4 chars = valeur EN CLAIR sinon — toujours masquer
            "go_workspace_id_masked": (
                config_settings.OPENCODE_GO_WORKSPACE_ID[:4] + "****"
                if len(config_settings.OPENCODE_GO_WORKSPACE_ID) > 4
                else "****"
            )
            if config_settings.OPENCODE_GO_WORKSPACE_ID
            else "",
            "go_auth_cookie_set": bool(config_settings.OPENCODE_GO_AUTH_COOKIE),
            "go_auth_cookie_masked": (config_settings.OPENCODE_GO_AUTH_COOKIE[:6] + "****")
            if config_settings.OPENCODE_GO_AUTH_COOKIE
            and len(config_settings.OPENCODE_GO_AUTH_COOKIE) > 6
            else (""),
            "api_keys": [
                {
                    "api_key_masked": (k["api_key"][:4] + "****" + k["api_key"][-4:])
                    if len(k.get("api_key", "")) > 8
                    else "****",
                    "go_workspace_id_masked": (k.get("go_workspace_id", "")[:4] + "****")
                    if len(k.get("go_workspace_id", "")) > 4
                    else "",
                    "go_auth_cookie_masked": (k.get("go_auth_cookie", "")[:6] + "****")
                    if len(k.get("go_auth_cookie", "")) > 6
                    else "",
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
            "free_parallel": dict(getattr(config_settings, "FREE_PARALLEL", {})),
            "free_model_map": config_settings.FREE_MODEL_MAP,
            "lang": os.getenv("PROXY_LANG", "en"),
        }

    @app.get("/api/routes")
    async def get_routes(
        request: Request, q: str = None, geo_status: str = None, limit: int = 50, offset: int = 0
    ):
        """Routes with geo enrichment + pagination (vivid-hinton P4)."""
        limit = max(1, min(200, int(limit or 50)))
        offset = max(0, int(offset or 0))
        items = []
        for key, route in config_settings.ROUTES.items():
            try:
                g = config_settings.resolve_geo(route)
            except Exception:
                g = {
                    "effective_allowed": set(),
                    "mode": "strict",
                    "require_vpn": False,
                    "geo_status": "ok",
                }
            status = str(g.get("geo_status", "ok"))
            if geo_status and status != geo_status:
                continue
            if (
                q
                and q.lower() not in key.lower()
                and q.lower() not in str(route.get("model", "")).lower()
            ):
                continue
            eff = g.get("effective_allowed", set())
            items.append(
                {
                    "key": key,
                    "match": route.get("match", []),
                    "model": route.get("model", ""),
                    "thinking": route.get("thinking"),
                    "geo": route.get("geo"),
                    "geo_status": status,
                    "effective_allowed": sorted(eff) if isinstance(eff, set) else [],
                    "mode": g.get("mode", "strict"),
                    "require_vpn": bool(g.get("require_vpn", False)),
                }
            )
        total = len(items)
        page = items[offset : offset + limit]
        return {
            "routes": page,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total,
        }

    @app.get("/api/geo-policies")
    async def get_geo_policies(request: Request):
        err = _check_dashboard_token(request)
        if err:
            return err
        return {
            "enabled": bool(getattr(config_settings, "GEO_ENABLED", False)),
            "version": int(getattr(config_settings, "GEO_VERSION", 1) or 1),
            "policies": dict(getattr(config_settings, "GEO_POLICIES", {}) or {}),
            "allow_direct_when_compatible": bool(
                getattr(config_settings, "GEO_ALLOW_DIRECT_WHEN_COMPATIBLE", True)
            ),
        }

    @app.put("/api/geo-policies")
    async def put_geo_policies(request: Request):
        err = _check_dashboard_token(request)
        if err:
            return err
        # X-API-Key control-server guard + If-Match ETag
        ctrl_key = str(config_settings.yaml_get("ip_rotation", "control_api_key", "") or "").strip()
        provided_ctrl = request.headers.get("X-API-Key", "")
        if ctrl_key and provided_ctrl != ctrl_key:
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "message": "X-API-Key valide requise"},
            )
        # ETag via mtime
        current_mtime = _config_yaml_mtime()
        etag = str(int(current_mtime))
        if_match = request.headers.get("If-Match", "")
        if if_match and if_match != etag and if_match != f'"{etag}"':
            return JSONResponse(
                status_code=412,
                content={
                    "error": "precondition_failed",
                    "message": "ETag obsolète — rechargez et réessayez",
                },
            )
        body = await request.json()
        # Validate schema
        if "enabled" in body:
            config_settings.GEO_ENABLED = bool(body["enabled"])
            config_settings._yaml_data.setdefault("geo", {})["enabled"] = bool(body["enabled"])
        if "version" in body:
            try:
                config_settings.GEO_VERSION = int(body["version"])
                config_settings._yaml_data.setdefault("geo", {})["version"] = int(body["version"])
            except Exception:
                return JSONResponse(status_code=400, content={"error": "version doit être un entier"})
        if "policies" in body:
            pol = body["policies"]
            if not isinstance(pol, dict):
                return JSONResponse(status_code=400, content={"error": "policies doit être un objet"})
            config_settings.GEO_POLICIES.clear()
            config_settings.GEO_POLICIES.update(pol)
            config_settings.SORTED_GEO_POLICIES[:] = sorted(pol.items())
            config_settings._yaml_data.setdefault("geo", {})["policies"] = dict(pol)
        if "allow_direct_when_compatible" in body:
            val = bool(body["allow_direct_when_compatible"])
            config_settings.GEO_ALLOW_DIRECT_WHEN_COMPATIBLE = val
            config_settings._yaml_data.setdefault("geo", {})["allow_direct_when_compatible"] = val
        # persist
        try:
            config_settings.save_yaml_config()
            global _config_yaml_known_mtime
            _config_yaml_known_mtime = _config_yaml_mtime()
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})
        return {
            "ok": True,
            "enabled": bool(config_settings.GEO_ENABLED),
            "version": int(config_settings.GEO_VERSION),
            "allow_direct_when_compatible": bool(config_settings.GEO_ALLOW_DIRECT_WHEN_COMPATIBLE),
            "etag": str(int(_config_yaml_mtime())),
        }

    @app.post("/api/geo-policies/rollback")
    async def geo_rollback(request: Request):
        err = _check_dashboard_token(request)
        if err:
            return err
        # Rollback = toggle enabled off (kill-switch) — simple N-1 without history
        config_settings.GEO_ENABLED = False
        config_settings._yaml_data.setdefault("geo", {})["enabled"] = False
        try:
            config_settings.save_yaml_config()
            global _config_yaml_known_mtime
            _config_yaml_known_mtime = _config_yaml_mtime()
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})
        return {"ok": True, "enabled": False}

    @app.get("/api/geo/notifications")
    async def get_geo_notifications(request: Request):
        """Last geo fallback warnings (direct → VPN) for badge/tray toast polling."""
        try:
            import json as _js

            p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "geo_notifications.json")
            if not os.path.exists(p):
                return {"notifications": []}
            with open(p, encoding="utf-8") as _f:
                data = _js.load(_f) or []
            # also enrich with live direct IP/country for current banner
            try:
                from opencode import _direct_country_cache, _direct_ip_cache

                current = {
                    "direct_country": _direct_country_cache.get("country", ""),
                    "direct_ip": _direct_country_cache.get("ip", "") or _direct_ip_cache.get("ip", ""),
                }
            except Exception:
                current = {}
            return {"notifications": data[-20:], "current": current}
        except Exception as e:
            return {"notifications": [], "error": str(e)}

    @app.get("/api/config/custom-routes")
    async def get_custom_routes():
        return config_settings.CUSTOM_ROUTES

    @app.post("/api/config/custom-routes")
    async def update_custom_routes(request: Request):
        err = _check_dashboard_token(request)
        if err:
            return err
        body = await request.json()
        # [v10 §14.2.5] validation schéma AVANT persistance (l'ancien code
        # persistait du JSON brut → crash différé au prochain reload).
        from config.settings import validate_custom_routes as _vcr

        v_err = _vcr(body)
        if v_err:
            return JSONResponse(status_code=400, content={"error": f"custom_routes invalide: {v_err}"})
        _debug(f"  [config] custom routes updated ({len(body)} routes)")
        save_custom_routes(body)
        return {"status": "ok", "message": "Custom routes updated."}

    @app.get("/api/config/web-search")
    async def get_web_search_config():
        from config import WEB_SEARCH_NATIVE_MODELS, yaml_get

        # web_search
        ws_mode = yaml_get("web_search", "mode", "duckduckgo")
        ws_target = yaml_get("web_search", "target_model", None)
        ws_max = yaml_get("web_search", "max_results", 5)
        ws_timeout = yaml_get("web_search", "timeout", 10)
        ws_enabled = yaml_get("web_search", "enabled", True)
        ws_via = yaml_get("web_search", "via_vpn", False)
        ws_onerr = yaml_get("web_search", "on_error", "strip")
        # web_fetch
        wf_mode = yaml_get("web_fetch", "mode", "direct")
        wf_target = yaml_get("web_fetch", "target_model", None)
        wf_maxb = yaml_get("web_fetch", "max_bytes", 12000)
        wf_timeout = yaml_get("web_fetch", "timeout", 15)
        wf_enabled = yaml_get("web_fetch", "enabled", True)
        wf_via = yaml_get("web_fetch", "via_vpn", False)
        wf_onerr = yaml_get("web_fetch", "on_error", "strip")
        return {
            "mode": ws_mode,
            "target_model": ws_target,
            "max_results": ws_max,
            "timeout": ws_timeout,
            "enabled": bool(ws_enabled),
            "via_vpn": bool(ws_via),
            "on_error": ws_onerr,
            "web_fetch": {
                "mode": wf_mode,
                "target_model": wf_target,
                "max_bytes": wf_maxb,
                "timeout": wf_timeout,
                "enabled": bool(wf_enabled),
                "via_vpn": bool(wf_via),
                "on_error": wf_onerr,
            },
            "available_models": sorted(MODELS.keys()),
            "modes": ["duckduckgo", "model", "ddg_then_model", "model_then_ddg"],
            "web_fetch_modes": ["direct", "model", "direct_then_model"],
            "native_models": WEB_SEARCH_NATIVE_MODELS,
        }

    @app.post("/api/config/web-search")
    async def update_web_search_config(request: Request):
        err = _check_dashboard_token(request)
        if err:
            return err
        body = await request.json()
        from config import yaml_set

        # helper to coerce bool
        def _to_bool(v):
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                return v.lower() in ("1", "true", "yes", "on")
            return bool(v)

        # web_search clamps
        if "mode" in body:
            m = str(body["mode"])
            if m in ("duckduckgo", "model", "ddg_then_model", "model_then_ddg"):
                yaml_set("web_search", "mode", m)
            else:
                _debug(f"  [config] web_search invalid mode {m!r} → ignored")
        if "target_model" in body:
            tm = body["target_model"]
            if tm is None or tm in MODELS:
                yaml_set("web_search", "target_model", tm)
            else:
                _debug(f"  [config] web_search invalid target_model {tm!r}")
        if "max_results" in body:
            try:
                v = max(1, min(10, int(body["max_results"])))
                yaml_set("web_search", "max_results", v)
            except Exception:
                pass
        if "timeout" in body:
            try:
                v = max(5, min(30, int(body["timeout"])))
                yaml_set("web_search", "timeout", v)
            except Exception:
                pass
        if "enabled" in body:
            yaml_set("web_search", "enabled", _to_bool(body["enabled"]))
        if "via_vpn" in body:
            yaml_set("web_search", "via_vpn", _to_bool(body["via_vpn"]))
        if "on_error" in body and body["on_error"] == "strip":
            yaml_set("web_search", "on_error", "strip")
        # web_fetch via nested or flat
        wf = body.get("web_fetch") if isinstance(body.get("web_fetch"), dict) else body
        # detect if web_fetch fields present
        if isinstance(wf, dict):
            if "mode" in wf and wf is not body:
                m = str(wf["mode"])
                if m in ("direct", "model", "direct_then_model"):
                    yaml_set("web_fetch", "mode", m)
            elif "web_fetch_mode" in body:
                m = str(body["web_fetch_mode"])
                if m in ("direct", "model", "direct_then_model"):
                    yaml_set("web_fetch", "mode", m)
            # flat keys for web_fetch with prefix
            if "web_fetch_target_model" in body:
                tm = body["web_fetch_target_model"]
                if tm is None or tm in MODELS:
                    yaml_set("web_fetch", "target_model", tm)
            if "target_model" in wf and wf is not body and "web_fetch" in body:
                tm = wf["target_model"]
                if tm is None or tm in MODELS:
                    yaml_set("web_fetch", "target_model", tm)
            if "max_bytes" in wf:
                try:
                    v = max(2000, min(50000, int(wf["max_bytes"])))
                    yaml_set("web_fetch", "max_bytes", v)
                except Exception:
                    pass
            if "web_fetch_max_bytes" in body:
                try:
                    v = max(2000, min(50000, int(body["web_fetch_max_bytes"])))
                    yaml_set("web_fetch", "max_bytes", v)
                except Exception:
                    pass
            if "timeout" in wf and wf is not body:
                try:
                    v = max(5, min(30, int(wf["timeout"])))
                    yaml_set("web_fetch", "timeout", v)
                except Exception:
                    pass
            if "web_fetch_timeout" in body:
                try:
                    v = max(5, min(30, int(body["web_fetch_timeout"])))
                    yaml_set("web_fetch", "timeout", v)
                except Exception:
                    pass
            if "enabled" in wf and wf is not body:
                yaml_set("web_fetch", "enabled", _to_bool(wf["enabled"]))
            if "web_fetch_enabled" in body:
                yaml_set("web_fetch", "enabled", _to_bool(body["web_fetch_enabled"]))
            if "via_vpn" in wf and wf is not body:
                yaml_set("web_fetch", "via_vpn", _to_bool(wf["via_vpn"]))
            if "web_fetch_via_vpn" in body:
                yaml_set("web_fetch", "via_vpn", _to_bool(body["web_fetch_via_vpn"]))
            if "on_error" in wf and wf is not body and wf["on_error"] == "strip":
                yaml_set("web_fetch", "on_error", "strip")
        _debug(
            f"  [config] web search updated: mode={body.get('mode')}, model={body.get('target_model')} web_fetch={body.get('web_fetch')}"
        )
        return {"status": "ok", "message": "Web search config updated."}

    @app.get("/api/config/web-fetch")
    async def get_web_fetch_config():
        from config import WEB_SEARCH_NATIVE_MODELS, yaml_get

        return {
            "mode": yaml_get("web_fetch", "mode", "direct"),
            "target_model": yaml_get("web_fetch", "target_model", None),
            "max_bytes": yaml_get("web_fetch", "max_bytes", 12000),
            "timeout": yaml_get("web_fetch", "timeout", 15),
            "enabled": bool(yaml_get("web_fetch", "enabled", True)),
            "via_vpn": bool(yaml_get("web_fetch", "via_vpn", False)),
            "on_error": yaml_get("web_fetch", "on_error", "strip"),
            "available_models": sorted(MODELS.keys()),
            "modes": ["direct", "model", "direct_then_model"],
            "native_models": WEB_SEARCH_NATIVE_MODELS,
        }

    @app.post("/api/config/web-fetch")
    async def update_web_fetch_config(request: Request):
        err = _check_dashboard_token(request)
        if err:
            return err
        body = await request.json()
        from config import yaml_set

        def _to_bool2(v):
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                return v.lower() in ("1", "true", "yes", "on")
            return bool(v)

        if "mode" in body and str(body["mode"]) in ("direct", "model", "direct_then_model"):
            yaml_set("web_fetch", "mode", str(body["mode"]))
        if "target_model" in body and (body["target_model"] is None or body["target_model"] in MODELS):
            yaml_set("web_fetch", "target_model", body["target_model"])
        if "max_bytes" in body:
            try:
                yaml_set("web_fetch", "max_bytes", max(2000, min(50000, int(body["max_bytes"]))))
            except Exception:
                pass
        if "timeout" in body:
            try:
                yaml_set("web_fetch", "timeout", max(5, min(30, int(body["timeout"]))))
            except Exception:
                pass
        if "enabled" in body:
            yaml_set("web_fetch", "enabled", _to_bool2(body["enabled"]))
        if "via_vpn" in body:
            yaml_set("web_fetch", "via_vpn", _to_bool2(body["via_vpn"]))
        if "on_error" in body and body["on_error"] == "strip":
            yaml_set("web_fetch", "on_error", "strip")
        return {"status": "ok", "message": "Web fetch config updated."}

    @app.get("/api/config/api-keys")
    async def get_api_keys_config(request: Request):
        err = _check_dashboard_token(request)
        if err:
            return err
        return {
            "api_keys": [
                {
                    "api_key_masked": (k["api_key"][:4] + "****" + k["api_key"][-4:])
                    if len(k.get("api_key", "")) > 8
                    else "****",
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
                return {"status": "error", "message": "Valeur de port invalide"}
            if not (1 <= new_port <= 65535):
                return {"status": "error", "message": "Le port doit être entre 1 et 65535"}
            if new_port != PORT:
                env_updates["OPENCODE_PORT"] = str(new_port)
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
                host=body.get("host"),
            )
            mgr = server_manager_getter() if server_manager_getter else None
            if mgr:
                # [fix ticket P1 incident 25/08] mgr.restart() ne doit JAMAIS
                # s'exécuter sur la boucle uvicorn courante : stop() fait
                # self._thread.join() → join du thread COURANT → RuntimeError
                # avant start() = serveur mort (« shutdown OK, startup jamais
                # relancé »). Même pattern que POST /api/proxy/restart :
                # thread dédié qui survit au démontage.
                import threading

                def _do_restart():
                    try:
                        mgr.restart(
                            port=body.get("port"),
                            host=body.get("host"),
                        )
                    except Exception as e:
                        _debug(f"  [config] hot-restart FAILED: {type(e).__name__}: {e}")

                threading.Thread(
                    target=_do_restart,
                    name="proxy-restart",
                    daemon=False,
                ).start()

        return {
            "status": "ok",
            "needs_restart": False,
            "message": "Configuration mise à jour. Redémarrage à chaud déclenché."
            if restart_needed
            else "Configuration mise à jour.",
        }

    # ── Proxy control ──

    @app.get("/api/proxy/status")
    async def proxy_status():
        mgr = server_manager_getter() if server_manager_getter else None
        if mgr:
            return {"running": mgr.is_running, "port": PORT}
        return {"running": True, "port": PORT}

    @app.post("/api/proxy/start")
    async def proxy_start():
        mgr = server_manager_getter() if server_manager_getter else None
        if not mgr:
            return {"status": "ok", "message": "Aucun gestionnaire de serveur disponible"}
        if mgr.is_running:
            return {"status": "ok", "message": "Déjà en cours"}
        # [fix P1] start() dort jusqu'à 5 s (attente bind) — hors loop.
        await asyncio.to_thread(mgr.start)
        return {"status": "ok", "message": "Proxy démarré"}

    @app.post("/api/proxy/stop")
    async def proxy_stop():
        mgr = server_manager_getter() if server_manager_getter else None
        if not mgr:
            return {"status": "ok", "message": "Aucun gestionnaire de serveur disponible"}
        if not mgr.is_running:
            return {"status": "ok", "message": "Déjà arrêté"}
        # [fix ticket P1] stop() joint le thread serveur : depuis la boucle
        # uvicorn (ce handler), join(current_thread) lèverait RuntimeError.
        await asyncio.to_thread(mgr.stop)
        return {"status": "ok", "message": "Proxy arrêté"}

    @app.post("/api/proxy/restart")
    async def proxy_restart(full: bool = False):
        mgr = server_manager_getter() if server_manager_getter else None
        if not mgr:
            return {"status": "error", "message": "Aucun gestionnaire de serveur disponible"}
        # [v10 fix P1 incident 25/08] Le handler tourne SUR la boucle uvicorn
        # qu'on va éteindre : stop()+start() dans ce call-stack couraient
        # contre le démontage de la boucle (serveur parfois laissé mort).
        # Redémarrage sur THREAD DÉDIÉ + réponse immédiate — le thread survit
        # au démontage et le start() s'exécute dans de bonnes conditions.
        import threading

        target = mgr.full_restart if full else mgr.restart
        threading.Thread(target=target, name="proxy-restart", daemon=False).start()
        msg = (
            "Redémarrage complet déclenché" if full else "Redémarrage déclenché"
        )
        return {"status": "ok", "message": msg}

    # ── Stats & history ──

    @app.get("/api/stats")
    async def get_stats(from_date: str = None, to_date: str = None, station: int = None):
        where, params = _build_where(from_date, to_date, station=station)
        cache_key = f"stats:{where}:{tuple(params)}"

        # Return cached result if fresh (< 10s old)
        cached = _stats_cache.get(cache_key)
        if cached is not None:
            return cached

        def _query_stats(db):
            row = db.execute(
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
                "input": total_input,
                "output": row[1],
                "cache": total_cache,
                "total": total_input + row[1] + total_cache,
                "count": total_count,
                "success_count": total_success,
                "fail_count": total_fail,
                "avg_duration_ms": int(row[6]),
                "cache_hit_rate": round(cache_hit_rate, 1),
                "success_rate": round(success_rate, 1) if success_rate is not None else None,
            }

            rows = db.execute(
                "SELECT model, COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0),"
                "       COALESCE(SUM(tokens_cache), 0), COUNT(*),"
                "       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),"
                "       SUM(CASE WHEN success = 0 OR success IS NULL THEN 1 ELSE 0 END),"
                "       COALESCE(AVG(duration_ms), 0)"
                " FROM requests " + where + " GROUP BY model",
                params,
            ).fetchall()

            acct_rows = db.execute(
                "SELECT COALESCE(NULLIF(free_model_ip, ''), account_alias, ''), COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0),"
                "       COALESCE(SUM(tokens_cache), 0), COUNT(*),"
                "       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),"
                "       SUM(CASE WHEN success = 0 OR success IS NULL THEN 1 ELSE 0 END),"
                "       COALESCE(AVG(duration_ms), 0)"
                " FROM requests "
                + where
                + " GROUP BY COALESCE(NULLIF(free_model_ip, ''), account_alias, '')",
                params,
            ).fetchall()

            return totals, rows, acct_rows

        totals, rows, acct_rows = await _db_read_sync(_query_stats)

        sum_total = totals["total"]
        models = {}
        for r in rows:
            t = r[1] + r[2] + r[3]
            m_cache_rate = (r[3] / r[1] * 100) if r[1] > 0 else 0.0
            # [36] null (not 100%) when no requests — the UI shows « — »
            m_success_rate = (r[5] / r[4] * 100) if r[4] > 0 else None
            models[r[0]] = {
                "input": r[1],
                "output": r[2],
                "cache": r[3],
                "total": t,
                "pct": f"{t / sum_total * 100:.1f}%" if sum_total else "0%",
                "count": r[4],
                "success_count": r[5],
                "fail_count": r[6],
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
                "input": r[1],
                "output": r[2],
                "cache": r[3],
                "total": t,
                "pct": f"{t / sum_total * 100:.1f}%" if sum_total else "0%",
                "count": r[4],
                "success_count": r[5],
                "fail_count": r[6],
                "avg_duration_ms": int(r[7]),
                "cache_hit_rate": round(a_cache_rate, 1),
                "success_rate": round(a_success_rate, 1) if a_success_rate is not None else None,
            }

        result = {"models": models, "accounts": accounts, "totals": totals}
        _stats_cache.set(cache_key, result)
        return result

    @app.get("/api/stats/timeseries")
    async def get_stats_timeseries(
        from_date: str = None, to_date: str = None, granularity: str = "hour", station: int = None
    ):
        """Return time-series data for charts: requests count, tokens, avg duration per time bucket."""
        where, params = _build_where(from_date, to_date, station=station)

        # [P2 perf] granularité "day" forcée au-delà de 7 jours : des buckets
        # horaires sur un mois = ~720 groupes scannés pour rien à l'écran.
        if granularity == "hour" and from_date and to_date:
            try:
                f = datetime.fromisoformat(str(from_date).replace("Z", "+00:00"))
                t = datetime.fromisoformat(str(to_date).replace("Z", "+00:00"))
                if f.tzinfo is not None:
                    f = f.astimezone(UTC).replace(tzinfo=None)
                if t.tzinfo is not None:
                    t = t.astimezone(UTC).replace(tzinfo=None)
                if (t - f).total_seconds() > 7 * 86400:
                    granularity = "day"
            except Exception:
                pass

        # [P2 perf] TTL cache : le dashboard re-poll cette endpoint en boucle.
        cache_key = f"timeseries:{granularity}:{where}:{tuple(params)}"
        cached = _stats_cache.get(cache_key)
        if cached is not None:
            return cached

        def _query_timeseries(db):
            # Group by truncated timestamp
            if granularity == "day":
                trunc_expr = "substr(timestamp, 1, 10)"
            elif granularity == "week":
                # [29] real ISO 8601 week grouping (%G ISO year + %V week 01-53);
                # the old day-of-month / 7 heuristic merged adjacent months.
                trunc_expr = "strftime('%G', timestamp) || '-W' || strftime('%V', timestamp)"
            else:  # hour
                trunc_expr = "substr(timestamp, 1, 13) || ':00'"

            rows = db.execute(
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
            return rows

        rows = await _db_read_sync(_query_timeseries)

        result = {
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
        _stats_cache.set(cache_key, result)
        return result

    @app.get("/api/logs")
    async def get_logs(limit: int = 100, offset: int = 0):
        lines = list(log_lines)
        return {
            "logs": lines[offset : offset + limit],
            "total": len(lines),
            "has_more": offset + limit < len(lines),
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
            log_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
            )
            debug_log_path = os.path.join(log_dir, "debug.log")
            set_debug_log_file(debug_log_path)
        from dashboard.display import debug as _debug_fn

        _debug_fn(f"Debug mode {'ENABLED' if enabled else 'DISABLED'} via API")
        return {"enabled": config_settings.DEBUG}

    @app.get("/api/debug/logs")
    async def get_debug_logs(limit: int = 500, offset: int = 0):
        """Return lines from logs/debug.log, most recent first.
        [P2 perf] rotation à 10 Mo côté display.py → le full-read ne vaut que
        sous ce seuil ; au-delà, tail-reader seek-based. No-op immédiat quand
        (size, mtime) est inchangé pour la même page demandée (poll UI)."""
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        debug_log_path = os.path.join(log_dir, "debug.log")
        try:
            if not os.path.exists(debug_log_path):
                return {"logs": [], "total": 0, "has_more": False}

            st = os.stat(debug_log_path)
            file_size = st.st_size
            if file_size == 0:
                return {"logs": [], "total": 0, "has_more": False}

            # Aligné sur _DEBUG_MAX_SIZE (dashboard/display.py) : la rotation
            # plafonne le fichier à 10 Mo — pas 50.
            MAX_FULL_READ = 10 * 1024 * 1024

            # No-op si le fichier ET la page demandée sont inchangés
            sig = (file_size, st.st_mtime, limit, offset)
            cached = getattr(get_debug_logs, "_cache", None)
            if cached and cached[0] == sig:
                return cached[1]

            def _read_all():
                """Read the whole file (fine for files up to 10 MB)."""
                with open(debug_log_path, encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
                total = len(all_lines)
                # Most-recent-first, paginated
                reversed_lines = [line.rstrip("\n") for line in reversed(all_lines)]
                page = reversed_lines[offset : offset + limit]
                return page, total

            def _read_tail():
                """Read from the end of a large file (seek-based).
                Reads up to 10 MB from EOF — enough for thousands of lines."""
                with open(debug_log_path, "rb") as f:
                    f.seek(0, 2)
                    fsize = f.tell()

                    # Read up to 10 MB from end
                    read_size = min(fsize, 10 * 1024 * 1024)
                    f.seek(fsize - read_size)
                    data = f.read().decode("utf-8", errors="replace")

                lines = data.split("\n")
                # Discard partial first line (continuation from before our window)
                if fsize > read_size and len(lines) > 1:
                    lines = lines[1:]
                # Discard trailing empty from final newline
                if lines and lines[-1] == "":
                    lines = lines[:-1]

                total = len(lines)
                reversed_lines = list(reversed(lines))
                page = reversed_lines[offset : offset + limit]
                return page, total

            if file_size <= MAX_FULL_READ:
                page, total = await asyncio.to_thread(_read_all)
            else:
                page, total = await asyncio.to_thread(_read_tail)

            resp = {
                "logs": page,
                "total": total,
                "has_more": offset + limit < total,
            }
            get_debug_logs._cache = (sig, resp)
            return resp
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
        # [v10 §14.3.20] capture la boucle pour publish() thread-safe
        try:
            manager.bind_loop(asyncio.get_running_loop())
        except Exception:
            pass
        queue = await manager.subscribe()
        _debug("  [sse] new SSE subscriber")

        async def event_generator():
            try:
                yield f"event: connected\ndata: {json.dumps({'status': 'ok'})}\n\n"
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=30)
                        yield payload
                    except TimeoutError:
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

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

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

        [P2 perf] agrégation en SQL GROUP BY (l'ancienne version ramenait
        jusqu'à 5000 lignes brutes et agrégeait en Python à chaque poll).
        Les lignes individuelles ne servent que pour `recent` (100 dernières).
        """

        def _query(db):
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
            return grouped, recent, (total[0] if total else 0)

        grouped_rows, recent_rows, total_count = await _db_read_sync(_query)

        by_model: dict = {}
        by_key: dict = {}
        by_workspace: dict = {}
        by_ip: dict = {}
        for r in grouped_rows:
            _paid, free, key, ws, ip, n, ok_n, fail_n, tok_in, tok_out, first_seen, last_seen = (
                r[:12] if len(r) >= 12 else (*r, "")[:12]
            )
            m = by_model.setdefault(
                free,
                {
                    "requests": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "success": 0,
                    "fail": 0,
                },
            )
            m["requests"] += n
            m["tokens_in"] += tok_in or 0
            m["tokens_out"] += tok_out or 0
            m["success"] += ok_n or 0
            m["fail"] += fail_n or 0
            k = by_key.setdefault(key, {"requests": 0, "tokens_in": 0, "tokens_out": 0})
            k["requests"] += n
            k["tokens_in"] += tok_in or 0
            k["tokens_out"] += tok_out or 0
            w = by_workspace.setdefault(ws, {"requests": 0, "tokens_in": 0, "tokens_out": 0})
            w["requests"] += n
            w["tokens_in"] += tok_in or 0
            w["tokens_out"] += tok_out or 0
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

        # Calculate reset times for each IP (quota window = 48h from last request)
        from datetime import datetime, timedelta

        QUOTA_WINDOW_HOURS = 48
        now = datetime.utcnow()
        for _ip_addr, ip_data in by_ip.items():
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
            "total_requests": total_count,
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

            def _query_grouped(db):
                return db.execute(
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

            def _query_identity(db):
                return db.execute(
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

            rows = await _db_read_sync(_query_grouped)
            id_rows = await _db_read_sync(_query_identity)

            # Both stations' _ip_history merged — the newer entry wins
            # (dedup by IP so the free_ip_model table's per-IP rotation data
            # reflects whichever tunnel last served that IP).
            def _merged_history():
                merged: dict = {}
                # [plan 18/08 §4] N-station: merge history from EVERY
                # active station (was mgr1+mgr2 fixed pair); falls back to
                # the caller's manager when no registry is present.
                for mgr in list(getattr(shared_state, "vpn_managers", None) or []) or [vpn_manager]:
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

    @app.get("/api/vpn/station/{station_id}/logs")
    async def get_station_logs(station_id: int, lines: int = 50):
        """[plan v10 §4 Lot 4] derniers logs docker de la station N — MTTR
        per-station sans ouvrir un terminal."""
        import subprocess as _sp

        import shared_state

        mgr = next(
            (
                m
                for m in (getattr(shared_state, "vpn_managers", None) or [])
                if getattr(m, "_station", None) == station_id
            ),
            None,
        )
        if mgr is None:
            return JSONResponse(
                status_code=404, content={"error": f"station {station_id} introuvable"}
            )
        n = max(10, min(int(lines or 50), 300))
        try:
            proc = await asyncio.to_thread(
                _sp.run,
                ["docker", "logs", "--tail", str(n), mgr._docker_container],
                capture_output=True,
                timeout=8,
                creationflags=0x08000000 if sys.platform == "win32" else 0,
            )
            text = ((proc.stdout or b"") + (proc.stderr or b"")).decode(
                "utf-8", errors="replace"
            )[-20000:]
            return {"station": station_id, "tail": n, "logs": text}
        except Exception as e:
            return JSONResponse(status_code=502, content={"error": str(e)})

    @app.get("/api/vpn-status")
    async def get_vpn_status():
        """Get current VPN status, IP, server, and usage stats."""
        import shared_state

        managers = getattr(shared_state, "vpn_managers", None) or []
        refreshed_at = datetime.now(UTC).isoformat()
        refresh_error = None
        stale = False
        if managers:
            try:
                _results = await asyncio.wait_for(
                    asyncio.gather(*(mgr.refresh_status(force=False) for mgr in managers), return_exceptions=True),
                    timeout=2.0,
                )
                for _r in _results:
                    if isinstance(_r, Exception):
                        refresh_error = f"{type(_r).__name__}: {_r}"
                        break
                refreshed_at = datetime.now(UTC).isoformat()
            except TimeoutError:
                stale = True
                refresh_error = "refresh timeout after 2.0s"
                refreshed_at = datetime.now(UTC).isoformat()
                try:
                    asyncio.create_task(
                        asyncio.gather(*(mgr.refresh_status() for mgr in managers), return_exceptions=True)
                    )
                except Exception:
                    pass
            except Exception as _e:
                refresh_error = f"{type(_e).__name__}: {_e}"
            if shared_state.free_ip_pool:
                data = shared_state.free_ip_pool.get_status()
            else:
                try:
                    _all = [m.get_status() for m in managers]
                    _connected = sum(1 for _s in _all if _s.get("status") == "connected")
                    _total = len(_all)
                    _agg = "connected" if _connected > 0 else ("error" if all(_s.get("status") == "error" for _s in _all) else _all[0].get("status", "not_configured"))
                    data = _all[0].copy()
                    data["status"] = _agg
                    data["vpn_status"] = _agg
                    data["healthy"] = _connected
                    data["total"] = _total
                    data["stations"] = [
                        {"station": _s.get("station"), "vpn_status": _s.get("status"), "current_ip": _s.get("ip"), "current_server": _s.get("server"), "vpn": _s}
                        for _s in _all
                    ]
                except Exception:
                    try:
                        data = managers[0].get_status()
                    except Exception:
                        data = {"enabled": False, "status": "not_configured"}
        else:
            data = {"enabled": False, "status": "not_configured"}
        # Per-IP usage stats — injected unconditionally (works in direct mode
        # too: free_ip is then the residential IP). Frontend polls this
        # endpoint every 10s while the vpn tab is active.
        # Fast path: instant GUI — return cached or empty, refresh in background
        if isinstance(data, dict):
            cached_ip = _stats_cache.get("ip_stats")
            if cached_ip is not None:
                data["ip_stats"] = cached_ip
            else:
                data["ip_stats"] = {}
            # refresh in background (don't block)
            # [v10 §14.3.21] dédoublonnage : chaque GET /vpn-status spawnait
            # une tâche même si une autre tournait déjà → accumulation si la
            # DB ralentit. Une seule tâche vivante à la fois.
            try:
                _running = getattr(_ip_stats_db, "_running_task", None)
                if _running is None or _running.done():
                    _ip_stats_db._running_task = asyncio.create_task(
                        _ip_stats_db(shared_state.vpn_manager)
                    )
            except Exception:
                pass
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
            # [plan 18/08 §2.1] Anti-env périmé: clés VPN_* dont la valeur
            # .env diffère de l'env du process — les enfants `docker compose`
            # héritent de l'env du parent (cause racine 19/08). La bannière
            # dashboard s'affiche quand ce champ est présent.
            if config_settings.ENV_DIVERGENCE:
                data["env_divergence"] = [
                    {"key": k, "file": f, "env": e} for k, f, e in config_settings.ENV_DIVERGENCE
                ]
            # [Axe 3.4] config.yaml modifié à la main sans POST dashboard →
            # dirty: le hot-reload est push-only par design, une bannière GUI
            # demande un restart ou une re-push (jamais d'auto-reload).
            data["config_yaml_dirty"] = _config_yaml_mtime() != _config_yaml_known_mtime
            # [Axe 3.1] socks5 backend state — the GUI's proxy table + the
            # auto-rotate toggle (never ship passwords).
            data["proxy_mode"] = _vpn_proxy_mode()
            # [plan v10 §4 Lot 4] latence-adaptive per-station (moteur §3.6)
            try:
                from latency_rotation import get_engine as _get_leng

                _leng = getattr(shared_state, "latency_engine", None) or _get_leng()
                _lat = {}
                for (_lsid, _lip), _tr in list(_leng._trackers.items())[:80]:
                    _lat[f"{_lsid}|{_lip}"] = _tr.snapshot().__dict__
                data["latency"] = _lat
                data["rotation_paused"] = bool(getattr(_leng, "paused", False))
                data["global_degraded_remaining"] = round(
                    max(0.0, float(_leng._global_paused_until) - _leng._now()), 1
                )
            except Exception:
                pass
            data["socks5"] = {
                "proxies": _socks5_payload(shared_state.free_ip_pool),
                "rotate": _socks5_auto_rotate_state(),
            }
            # [vivid-hinton P4] geo enrichment for vpn-status
            try:
                import opencode as _oc

                _dur = getattr(_oc, "_geo_pin_duration", [])
                _avg = sum(_dur) / len(_dur) if _dur else 0
            except Exception:
                _dur, _avg = [], 0
            data["geo"] = {
                "enabled": bool(getattr(config_settings, "GEO_ENABLED", False)),
                "version": int(getattr(config_settings, "GEO_VERSION", 1) or 1),
                "block_total": int(getattr(_oc, "_geo_block_total", 0)) if "_oc" in locals() else 0,
                "pin_latency_avg_ms": round(_avg, 1),
                "pin_latency_p95_ms": round(sorted(_dur)[int(len(_dur) * 0.95)] if _dur else 0, 1),
                "queue_depth": len(getattr(_oc, "_geo_breaker", {})) if "_oc" in locals() else 0,
                "allow_direct_when_compatible": bool(
                    getattr(config_settings, "GEO_ALLOW_DIRECT_WHEN_COMPATIBLE", True)
                ),
                "geo_allow_direct": bool(
                    getattr(config_settings, "GEO_ALLOW_DIRECT_WHEN_COMPATIBLE", True)
                ),
            }
            # [Axe C] geo_strict_union: union of all effective_allowed countries
            # across all geo-enabled routes — lets the GUI show which countries
            # are needed globally.
            try:
                _strict_union: set = set()
                for _rk, _rv in config_settings.ROUTES.items():
                    try:
                        _rg = config_settings.resolve_geo(_rv)
                        _reff = _rg.get("effective_allowed", set())
                        if isinstance(_reff, set):
                            _strict_union |= _reff
                    except Exception:
                        pass
                data["geo_strict_union"] = sorted(_strict_union)
            except Exception:
                data["geo_strict_union"] = []
            # [Axe C] per-route allowed check for current country — read ALL
            # managers and prefer a station already in effective_allowed.
            try:
                cur_country = None
                _all_mgr_countries: list = []
                for mgr in managers:
                    if mgr and getattr(mgr, "_current_country", None):
                        _all_mgr_countries.append(mgr._current_country)
                # Prefer a country in effective_allowed (across any route)
                _any_effective: set = set()
                for _rk, _rv in config_settings.ROUTES.items():
                    try:
                        _rg = config_settings.resolve_geo(_rv)
                        _reff = _rg.get("effective_allowed", set())
                        if isinstance(_reff, set):
                            _any_effective |= _reff
                    except Exception:
                        pass
                for cc in _all_mgr_countries:
                    if cc in _any_effective:
                        cur_country = cc
                        break
                if cur_country is None and _all_mgr_countries:
                    cur_country = _all_mgr_countries[0]  # single-station compat
                data["current_country"] = cur_country
                allowed_for = []
                for k, route in config_settings.ROUTES.items():
                    try:
                        g = config_settings.resolve_geo(route)
                        eff = g.get("effective_allowed", set())
                        if isinstance(eff, set) and cur_country and cur_country in eff:
                            allowed_for.append(k)
                    except Exception:
                        pass
                data["current_country_allowed_for"] = allowed_for
            except Exception:
                pass
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
            # [prancy-unicorn Phase1] staleness + refresh observability for GUI spinner/tooltip
            data["stale"] = stale
            data["refreshed_at"] = refreshed_at
            data["refresh_error"] = refresh_error
            # [prancy-unicorn Phase1 Q5] garde-fou clone frais : warning si N>=2 && !free_parallel
            try:
                _fp_cfg = config_settings.FREE_PARALLEL if hasattr(config_settings, "FREE_PARALLEL") else {}
                _fp_enabled = bool(_fp_cfg.get("enabled")) if isinstance(_fp_cfg, dict) else False
                _n = int(data.get("total") or len(managers) or 0)
                if _n >= 2 and not _fp_enabled:
                    data["free_parallel_warning"] = "tout sur station 1 — activer free_parallel"
                else:
                    data["free_parallel_warning"] = None
            except Exception:
                pass
            # warm-avalanche Q9: control 401 + socks5 EOF observability
            try:
                _c401 = None
                _eof = 0
                for _m in managers:
                    _c = getattr(_m, "_control_last_401_at", None)
                    if _c and (_c401 is None or _c > _c401):
                        _c401 = _c
                    _eof = max(_eof, int(getattr(_m, "_socks5_eof_count", 0) or 0))
                data["control_last_401_at"] = _c401
                data["socks5_eof_count"] = _eof
                # badge 300s
                data["control_401_badge"] = bool(_c401 and (time.time() - _c401) < 300)
            except Exception:
                pass
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

        body = await request.json()

        # Handle credentials — [24] write the single source gluetun actually
        # reads (credentials.env via docker-compose env_file).
        if "credentials" in body:
            creds = body.pop("credentials")
            username = creds.get("username", "")
            password = creds.get("password", "")
            if username and password:
                _write_credentials_env(username, password)

        # [fix 19/08] free_model_map is a TOP-LEVEL config.yaml key (not an
        # ip_rotation one) — hot-reload by mutating the shared dict in place
        # (opencode.py imports the SAME object by reference, so clear()+update
        # is visible without a proxy restart) + persist to config.yaml + sync
        # the in-memory mirror so a later settings.yaml_set() dump does not
        # revert what we just wrote to disk. Consumed — never fanned out.
        if "free_model_map" in body:
            _fmm = body.pop("free_model_map")
            if isinstance(_fmm, dict):
                try:
                    from config import settings as _st

                    _st.FREE_MODEL_MAP.clear()
                    _st.FREE_MODEL_MAP.update(_fmm)
                    _st._yaml_data["free_model_map"] = dict(_fmm)
                except Exception as e:
                    _debug(f"  [free] free_model_map hot-reload failed: {e}")
                    return {"error": f"échec hot-reload free_model_map : {e}"}
                _persist_free_model_map(_fmm)

        # [plan 19/08 §1/§2] free multi-attempt + exception ordering —
        # validated here, then left in body so they reach _persist_vpn_config
        # (config.yaml + in-memory mirror; read per-request via
        # IP_ROTATION.get → hot-reload without restart). No container effect:
        # update_config() stores them harmlessly in the manager _config dict.
        if "max_free_attempts" in body:
            try:
                body["max_free_attempts"] = max(1, min(3, int(body["max_free_attempts"])))
            except (TypeError, ValueError):
                return {"error": "max_free_attempts doit être un entier entre 1 et 3"}
        if "free_exception_fallback" in body:
            if str(body["free_exception_fallback"]) not in ("station-first", "direct"):
                return {"error": "free_exception_fallback doit être 'station-first' ou 'direct'"}
            body["free_exception_fallback"] = str(body["free_exception_fallback"])
        # [free_parallel] validate nested + flat keys
        if "free_parallel" in body:
            if not isinstance(body["free_parallel"], dict):
                return {"error": "free_parallel doit être un objet"}
            try:
                import config.settings as _st_fp

                body["free_parallel"] = _st_fp._normalize_free_parallel(body["free_parallel"])
            except Exception as e:
                return {"error": f"free_parallel invalide : {e}"}
        for _fk in ("free_parallel_enabled", "free_parallel_routing", "free_parallel_mode", "free_parallel_hedge_delay_ms"):
            if _fk in body:
                # will be validated in _persist_vpn_config / pool.update_config; keep as is
                pass
        if "free_parallel_routing" in body:
            if str(body["free_parallel_routing"]).lower() not in ("round-robin", "failover"):
                return {"error": "free_parallel_routing doit être 'round-robin' ou 'failover'"}
            body["free_parallel_routing"] = str(body["free_parallel_routing"]).lower()
        if "free_parallel_mode" in body:
            if str(body["free_parallel_mode"]).lower() not in ("load-balance", "strict", "hedge"):
                return {"error": "free_parallel_mode doit être 'load-balance', 'strict' ou 'hedge'"}
            body["free_parallel_mode"] = str(body["free_parallel_mode"]).lower()

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
                _raw_n = body["station_count"]
                try:
                    _new_n = int(_raw_n)
                except (TypeError, ValueError):
                    _new_n = 0
                # [Axe 3.3] 400 explicite hors bornes — un clamp silencieux
                # masque les erreurs GUI/programmatiques (un 15 tapé lancerait
                # 10 stations sans sourciller).
                if not (1 <= _new_n <= 10):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": f"station_count doit être un entier entre 1 et 10, reçu {_raw_n!r}"
                        },
                    )
                body.pop("station_count")  # consumed — never fanned out
                if _new_n != len(managers):
                    try:
                        from opencode import _apply_station_count

                        await _apply_station_count(_new_n)
                    except Exception as e:
                        _debug(f"  [vpn] station_count hot-reload failed: {e}")
                        return {"error": f"échec hot-reload station_count : {e}"}
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
                    _stack_res = await managers[0].set_stack(str(body["vpn_stack"]))
                    _stack_ok = bool(_stack_res.get("ok"))
                    if _stack_ok:
                        _persist_vpn_config({"vpn_stack": str(body["vpn_stack"])})
                    else:
                        _debug(
                            f"  [vpn] set_stack refused: {_stack_res.get('error')} — config.yaml keeps the previous stack"
                        )
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
        err = _check_dashboard_token(request)
        if err:
            return err
        import shared_state

        body = await request.json()
        enabled = body.get("enabled", True)
        if shared_state.vpn_manager:
            shared_state.vpn_manager.enabled = enabled
            if shared_state.free_ip_pool:
                shared_state.free_ip_pool._vpn = shared_state.vpn_manager
            return {"ok": True, "enabled": enabled}
        return {"error": "gestionnaire VPN non initialisé"}

    @app.post("/api/vpn/connect")
    async def connect_vpn(request: Request):
        """Connect VPN — reconcile status, then connect via compose-managed gluetun."""
        err = _check_dashboard_token(request)
        if err:
            return err
        import shared_state

        if not shared_state.vpn_manager:
            return {"error": "gestionnaire VPN non initialisé"}

        await shared_state.vpn_manager.refresh_status()
        if shared_state.vpn_manager.status == "connected":
            return {
                "ok": True,
                "ip": shared_state.vpn_manager.current_ip,
                "server": shared_state.vpn_manager.current_server,
            }

        try:
            await shared_state.vpn_manager.connect()
            return {
                "ok": True,
                "ip": shared_state.vpn_manager.current_ip,
                "server": shared_state.vpn_manager.current_server,
            }
        except Exception as e:
            return {"error": str(e)}

    @app.post("/api/vpn/disconnect")
    async def disconnect_vpn(request: Request):
        """Disconnect VPN."""
        err = _check_dashboard_token(request)
        if err:
            return err
        import shared_state

        if not shared_state.vpn_manager:
            return {"error": "gestionnaire VPN non initialisé"}
        await shared_state.vpn_manager.disconnect()
        return {"ok": True}

    @app.post("/api/vpn/health-check")
    async def vpn_health_check(request: Request):
        """Run a health check on the current VPN connection."""
        err = _check_dashboard_token(request)
        if err:
            return err
        import shared_state

        if not shared_state.vpn_manager:
            return {"error": "gestionnaire VPN non initialisé"}
        result = await shared_state.vpn_manager.health_check()
        return result

    @app.post("/api/vpn/next")
    async def next_vpn(request: Request):
        """Switch to next VPN server.

        Body may carry ``station`` (0 = active station, default; n = station
        n — the latter only within the resolved station_count).
        """
        err = _check_dashboard_token(request)
        if err:
            return err
        import shared_state

        managers = getattr(shared_state, "vpn_managers", None) or []
        if not managers:
            return {"error": "gestionnaire VPN non initialisé"}
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
                return {
                    "error": f"station {station} non configurée (station_count={len(managers)})"
                }
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
    async def update_vpn(request: Request):
        """Force-check and apply a pending gluetun image update."""
        err = _check_dashboard_token(request)
        if err:
            return err
        import shared_state

        if not shared_state.vpn_manager:
            return {"error": "gestionnaire VPN non initialisé"}
        try:
            available = await shared_state.vpn_manager.check_update()
            if not available:
                return {
                    "ok": False,
                    "error": "aucune mise à jour disponible",
                    "update": shared_state.vpn_manager.get_status()["update"],
                }
            # check_opportune=True ([21]): the manual endpoint must not cut
            # live free streams either — the check runs inside the lock.
            result = await shared_state.vpn_manager.apply_update(check_opportune=True)
            result["update"] = shared_state.vpn_manager.get_status()["update"]
            return result
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/vpn/credentials")
    async def get_vpn_credentials(request: Request):
        """Check if VPN credentials exist (does not return actual values)."""
        err = _check_dashboard_token(request)
        if err:
            return err
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cred_path = os.path.join(root, "credentials.env")
        exists = os.path.exists(cred_path) and os.path.getsize(cred_path) > 0
        username_saved = ""
        if exists:
            try:
                with open(cred_path, encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("OPENVPN_USER="):
                            username_saved = line.split("=", 1)[1].strip()
                            break
            except Exception:
                pass
        return {
            "exists": exists,
            "username_preview": username_saved[:4] + "****" if username_saved else "",
        }

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
            return {"error": "Nom d'utilisateur et mot de passe requis"}

        _write_credentials_env(username, password)
        return {
            "ok": True,
            "note": "restart the VPN container (docker compose up -d) for gluetun to pick up new credentials",
        }

    @app.post("/api/vpn/save-state")
    async def save_vpn_state(request: Request):
        """Persist VPN state to disk."""
        err = _check_dashboard_token(request)
        if err:
            return err
        import shared_state

        if shared_state.vpn_manager:
            shared_state.vpn_manager.save_state()
            return {"ok": True}
        return {"error": "gestionnaire VPN non initialisé"}

    @app.get("/api/vpn/export")
    async def export_vpn_config(request: Request):
        """Export VPN configuration as JSON."""
        err = _check_dashboard_token(request)
        if err:
            return err
        import shared_state

        if not shared_state.vpn_manager:
            return {"error": "gestionnaire VPN non initialisé"}

        config = shared_state.vpn_manager.get_config()
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

    @app.post("/api/vpn/station/{sid}/pin_country")
    async def pin_station_country(sid: int, request: Request):
        """[v10 §12.1.5] Pin manuel d'un pays pour la station {id}.
        Body : {"country": "Japan"} — le control server gluetun fait une
        vraie reconnexion (~8-15 s)."""
        err = _check_dashboard_token(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": "JSON invalide"})
        country = str((body or {}).get("country") or "").strip()
        if not country:
            return JSONResponse(status_code=400, content={"error": "'country' requis"})
        import shared_state

        mgr = next(
            (
                m
                for m in (getattr(shared_state, "vpn_managers", None) or [])
                if getattr(m, "_station", None) == sid
            ),
            None,
        )
        if mgr is None:
            return JSONResponse(status_code=404, content={"error": f"station {sid} introuvable"})
        try:
            ok = await asyncio.wait_for(mgr.pin_country(country), timeout=60)
        except TypeError:
            return JSONResponse(status_code=500, content={"error": "manager sans pin_country"})
        except Exception as e:
            return JSONResponse(status_code=502, content={"error": str(e)[:200]})
        if ok:
            return {"ok": True, "station": sid, "country": country}
        return JSONResponse(
            status_code=502,
            content={"error": "pin refusé par le control server (pays invalide ou tunnel KO)"},
        )

    @app.delete("/api/vpn/station/{sid}/pin_country")
    async def unpin_station_country(sid: int, request: Request):
        """[v10 §12.1.5] Retire le pin manuel : restaure la sélection
        multi-pays complète (SERVER_COUNTRIES)."""
        err = _check_dashboard_token(request)
        if err:
            return err
        import shared_state

        mgr = next(
            (
                m
                for m in (getattr(shared_state, "vpn_managers", None) or [])
                if getattr(m, "_station", None) == sid
            ),
            None,
        )
        if mgr is None:
            return JSONResponse(status_code=404, content={"error": f"station {sid} introuvable"})
        try:
            ok = await asyncio.wait_for(mgr.unpin_country(), timeout=45)
        except Exception as e:
            return JSONResponse(status_code=502, content={"error": str(e)[:200]})
        return {"ok": bool(ok), "station": sid}

    @app.post("/api/vpn/station/{sid}/restart")
    async def restart_station(sid: int, request: Request):
        """[v10 §9.4] Redémarre la station {id} (docker restart léger).
        Le statut converge via les vpn_event SSE (~15-45 s)."""
        err = _check_dashboard_token(request)
        if err:
            return err
        import shared_state

        mgr = next(
            (
                m
                for m in (getattr(shared_state, "vpn_managers", None) or [])
                if getattr(m, "_station", None) == sid
            ),
            None,
        )
        if mgr is None:
            return JSONResponse(status_code=404, content={"error": f"station {sid} introuvable"})
        try:
            await asyncio.wait_for(mgr.restart(), timeout=150)
        except TimeoutError:
            return {"ok": True, "station": sid, "note": "restart en cours — suivre via SSE"}
        except Exception as e:
            return JSONResponse(status_code=502, content={"error": str(e)[:200]})
        return {"ok": True, "station": sid}

    @app.post("/api/vpn/station/{sid}/soft-rotate")
    async def soft_rotate_station(sid: int, request: Request):
        """[v10 §9.4] Déclenche une rotation d'IP pour la station {id}
        (queue pool, anti-flapping et garde-fous §3.6 applicables)."""
        err = _check_dashboard_token(request)
        if err:
            return err
        import shared_state

        mgr = next(
            (
                m
                for m in (getattr(shared_state, "vpn_managers", None) or [])
                if getattr(m, "_station", None) == sid
            ),
            None,
        )
        pool = getattr(shared_state, "free_ip_pool", None)
        if mgr is None or pool is None or not hasattr(pool, "_launch_rotation"):
            return JSONResponse(status_code=404, content={"error": f"station {sid} ou pool indisponible"})
        try:
            pool._launch_rotation(mgr)
        except Exception as e:
            return JSONResponse(status_code=502, content={"error": str(e)[:200]})
        return {"ok": True, "station": sid, "queued": True}

    @app.get("/api/costs")
    async def get_costs(from_date: str = None, to_date: str = None, station: int = None):
        """[v10 §12.3.10] Coût payant + économies réalisées via le free tier.

        Tarification : section `pricing` de config.yaml (défauts Sonnet-class).
        Les modèles `-free` coûtent 0 et créditent `free_saved_usd`."""
        where, params = _build_where(from_date, to_date, station=station)
        pricing = {}
        try:
            import config.settings as _st_c

            _p = _st_c._yaml_data.get("pricing")
            if isinstance(_p, dict):
                pricing = _p
        except Exception:
            pass

        def _query(db):
            rows = db.execute(
                "SELECT model, SUM(tokens_input) AS tokens_input, "
                "SUM(tokens_output) AS tokens_output FROM requests "
                + where
                + " GROUP BY model",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

        rows = await _db_read_sync(_query)
        result = _compute_costs(rows, pricing)
        result["window"] = {"from": from_date, "to": to_date, "station": station}
        return result

    @app.post("/api/vpn-config/validate")
    async def validate_vpn_config(request: Request):
        """[v10 §12.1.4] DRY-RUN : valide un payload de config VPN SANS
        l'appliquer. Retourne {valid, errors[]} — l'UI peut l'appeler avant
        chaque POST réel."""
        err = _check_dashboard_token(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception as e:
            return JSONResponse(status_code=400, content={"valid": False, "errors": [str(e)]})
        errors = _validate_vpn_config_payload(body)
        return {"valid": not errors, "errors": errors}

    @app.post("/api/vpn/rotation-paused")
    async def set_rotation_paused(request: Request):
        """[v10 §3.8/§4 Lot 6] Mode maintenance : gèle soft/hard rotate du
        moteur §3.6. Éphémère PAR DESIGN (non persisté) — évite un oubli ON
        après un restart ; le badge dashboard l'affiche tant qu'il actif."""
        err = _check_dashboard_token(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        paused = bool(body.get("paused"))
        import shared_state

        eng = getattr(shared_state, "latency_engine", None)
        if eng is None:
            from latency_rotation import get_engine as _ge

            eng = _ge()
        eng.paused = paused
        _debug(f"  [vpn] rotation_paused → {paused}")
        return {"ok": True, "paused": paused}

    @app.post("/api/vpn/import")
    async def import_vpn_config(request: Request):
        """Import VPN configuration from JSON."""
        err = _check_dashboard_token(request)
        if err:
            return err
        import shared_state

        body = await request.json()
        resp, status = _apply_import_payload(
            list(getattr(shared_state, "vpn_managers", None) or []),
            body,
            fallback_manager=shared_state.vpn_manager,
        )
        if status != 200:
            return JSONResponse(status_code=status, content=resp)
        return resp

    # ── [Axe 3.1] SOCKS5 backend / NordVPN / diagnostic endpoints ──
    # Contracts pinned from static/app.js (verified 19/08). All state-chan
    # gers carry the dashboard token guard; read-only GETs stay open (the
    # GUI polls them every 10 s). External I/O lives in the isolated helpers
    # above (hot-patchable offline — live docker/API never runs in tests).

    @app.get("/api/vpn/socks5")
    async def get_vpn_socks5():
        """Static SOCKS5 proxy list for the GUI (app.js socks5 GET →
        ``{proxies, rotate}``; proxies never carry passwords)."""
        pool = _dashboard_pool()
        return {"proxies": _socks5_payload(pool), "rotate": _socks5_auto_rotate_state()}

    @app.post("/api/vpn/socks5")
    async def add_vpn_socks5(request: Request):
        """Append a static SOCKS5 proxy (``{host, port, username,
        password}``). Persists + pushes the pool, returns the new list."""
        err = _check_dashboard_token(request)
        if err:
            return err
        body = await request.json()
        pool = _dashboard_pool()
        try:
            port = int(body.get("port") or 1080)
        except (TypeError, ValueError):
            return {"error": f"port invalide: {body.get('port')!r}"}
        rows = list(getattr(pool, "_socks5_proxies", None) or [])
        rows.append(
            {
                "host": str(body.get("host", "") or ""),
                "port": port,
                "enabled": True,
                "username": body.get("username"),
                "password": body.get("password"),
            }
        )
        err = _apply_socks5_rows(rows)
        if err:
            return err
        return {"ok": True, "proxies": _socks5_payload(pool)}

    @app.post("/api/vpn/socks5/remove")
    async def remove_vpn_socks5(request: Request):
        """Remove the proxy at ``{index}`` (app.js remove → ``{proxies}``)."""
        err = _check_dashboard_token(request)
        if err:
            return err
        body = await request.json()
        pool = _dashboard_pool()
        rows = list(getattr(pool, "_socks5_proxies", None) or [])
        idx = _as_index(body.get("index"))
        if not (0 <= idx < len(rows)):
            return {"error": f"proxy index hors bornes: {idx!r}"}
        del rows[idx]
        err = _apply_socks5_rows(rows)
        if err:
            return err
        return {"ok": True, "proxies": _socks5_payload(pool)}

    @app.post("/api/vpn/socks5/toggle")
    async def toggle_vpn_socks5(request: Request):
        """Enable/disable the proxy at ``{index, enabled}``."""
        err = _check_dashboard_token(request)
        if err:
            return err
        body = await request.json()
        pool = _dashboard_pool()
        rows = list(getattr(pool, "_socks5_proxies", None) or [])
        idx = _as_index(body.get("index"))
        if not (0 <= idx < len(rows)):
            return {"error": f"proxy index hors bornes: {idx!r}"}
        rows[idx] = dict(rows[idx])
        rows[idx]["enabled"] = bool(body.get("enabled", True))
        err = _apply_socks5_rows(rows)
        if err:
            return err
        return {"ok": True}

    @app.post("/api/vpn/socks5/test")
    async def test_vpn_socks5(request: Request):
        """Probe one proxy (``{host, port}``) → ``{ok, ip, opencode_ok,
        latency_ms, error}`` (raw SOCKS5 + ssl through ``_socks5_probe``)."""
        err = _check_dashboard_token(request)
        if err:
            return err
        body = await request.json()
        return await _socks5_probe(body.get("host", ""), body.get("port", 0))

    @app.post("/api/vpn/socks5/rotate")
    async def rotate_vpn_socks5(request: Request):
        """Two roles (app.js socks5/rotate):
        - body ``{rotate: bool}`` → persist the auto-rotate toggle;
        - body WITHOUT ``rotate`` → manual rotate to the next usable proxy
          (``{ok, next}`` superset). Both keep the pool in sync."""
        err = _check_dashboard_token(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        if "rotate" in body:
            _v = bool(body.get("rotate"))
            _persist_vpn_config({"socks5_auto_rotate": _v})
            pool = _dashboard_pool()
            if pool is not None and hasattr(pool, "_socks5_auto_rotate"):
                pool._socks5_auto_rotate = _v
            return {"ok": True, "rotate": _v}
        pool = _dashboard_pool()
        if pool is None or not hasattr(pool, "rotate_socks5_now"):
            return {"error": "pool socks5 non disponible"}
        nxt = pool.rotate_socks5_now()
        return {
            "ok": nxt is not None,
            "next": getattr(nxt, "pid", None) if nxt is not None else None,
        }

    @app.post("/api/vpn/proxy-mode")
    async def set_vpn_proxy_mode(request: Request):
        """Switch egress mode ``vpn | socks5 | direct`` — persists + fans out
        to every station (the pool's socks5_mode reads managers[0].proxy_mode,
        so this single property drives the whole free path)."""
        err = _check_dashboard_token(request)
        if err:
            return err
        body = await request.json()
        mode = str(body.get("mode", "")).strip()
        if mode not in ("vpn", "socks5", "direct"):
            return {"error": f"mode doit être vpn|socks5|direct, reçu {mode!r}"}
        managers = _dashboard_managers()
        if not managers:
            return {"error": "gestionnaire VPN non initialisé"}
        for m in managers:
            m.proxy_mode = mode
        _persist_vpn_config({"proxy_mode": mode})
        # When switching from direct → vpn/socks5, the boot gate
        # `vpn_manager.start()` only dials when proxy_mode==vpn, so
        # stations 2..N that were left disconnected in direct must be
        # resurrected now. Fire-and-forget start() for every enabled
        # manager that is not already connected; fail-soft (docker down
        # must not block the API).
        if mode in ("vpn", "socks5"):
            try:

                # Ensure every configured station (station_count) is present.
                # If the registry is short (e.g. boot direct with 1), rebuild
                # it via _apply_station_count to restore the missing managers.
                try:
                    import config.settings as _cs
                    from config.settings import resolved_station_count as _rsc

                    want = _rsc(getattr(_cs, "IP_ROTATION", {}) or {})
                    have = len(managers)
                    if want > have:
                        from opencode import _apply_station_count as _asc

                        await _asc(want)
                        # re-read after upscale
                        managers = _dashboard_managers()
                        for mm in managers:
                            mm.proxy_mode = mode
                except Exception as _e:
                    _debug(f"  [vpn] proxy-mode upscale check failed: {_e}")
                # Now ensure every enabled manager is at least started.
                # start() is idempotent and triggers background connect when
                # status != connected, so this resurrects the 2..N that
                # stayed disconnected in direct.
                # [plan v10 §14.1.12] starts PARALLÈLES + réponse immédiate :
                # l'ancien await séquentiel par station suspendait l'endpoint
                # des minutes, SSE/GUI gelées. Le suivi d'état passe par les
                # vpn_event SSE déjà émis par _set_status.
                _starts = []
                for mm in managers:
                    if getattr(mm, "enabled", True) and getattr(mm, "status", "disconnected") != "connected":
                        _starts.append(
                            asyncio.create_task(
                                mm.start(),
                                name=f"proxy-mode-start-st{getattr(mm, '_station', '?')}",
                            )
                        )
                if _starts:
                    asyncio.create_task(_reap_proxy_mode_starts(_starts))
            except Exception as _e:
                _debug(f"  [vpn] proxy-mode resurrect failed: {_e}")
        return {"ok": True, "mode": mode}

    async def _reap_proxy_mode_starts(tasks):
        """[v10 §14.1.12] récolte les starts détachés : erreurs journalisées,
        jamais perdues silencieusement."""
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for t, r in zip(tasks, results):
            if isinstance(r, Exception):
                _debug(f"  [vpn] proxy-mode detached start failed ({t.get_name()}): {r}")

    @app.get("/api/vpn/nordvpn-available")
    async def nordvpn_available():
        """``{available}`` — credentials.env présent + provider nordvpn."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cred_path = os.path.join(root, "credentials.env")
        has = os.path.exists(cred_path) and os.path.getsize(cred_path) > 0
        cfg = getattr(config_settings, "IP_ROTATION", None) or {}
        provider = str(cfg.get("server_provider") or "nordvpn").lower()
        return {"available": has and provider == "nordvpn"}

    @app.get("/api/vpn/nordvpn-status")
    async def nordvpn_status():
        """``{connected, country, city, ip}`` — adaptateur honnête depuis
        l'état réel des managers (pas d'invention de credentials)."""
        st = {"connected": False, "country": None, "city": None, "ip": None}
        for m in _dashboard_managers():
            try:
                mg = m.get_status()
            except Exception:
                continue
            if st["country"] is None:
                st["country"] = mg.get("current_country") or None
            if m.status == "connected" or mg.get("status") == "connected":
                st["connected"] = True
            if st["ip"] is None:
                st["ip"] = getattr(m, "current_ip", None) or mg.get("ip") or None
            if st["city"] is None:
                st["city"] = mg.get("city") or None
        return st

    @app.get("/api/vpn/nordvpn-countries")
    async def nordvpn_countries_ep():
        """``{countries}`` — API NordVPN quand use_nordvpn_api, sinon liste
        statique (offline-safe)."""
        cfg = getattr(config_settings, "IP_ROTATION", None) or {}
        return {"countries": await _nordvpn_countries(bool(cfg.get("use_nordvpn_api")))}

    @app.get("/api/vpn/countries")
    async def vpn_countries_ep():
        """``{countries}`` — même source que nordvpn-countries (app.js
        vpnLoadCountries consomme ``data.countries``)."""
        cfg = getattr(config_settings, "IP_ROTATION", None) or {}
        return {"countries": await _nordvpn_countries(bool(cfg.get("use_nordvpn_api")))}

    @app.post("/api/vpn/discover-and-add")
    async def vpn_discover_and_add(request: Request):
        """Découvre les serveurs OpenVPN recommandés d'un pays (API NordVPN)
        et ajoute le pays à la liste de rotation ``server_countries``
        (noms séparés par virgules, format config.yaml). Retourne le nombre
        de serveurs découverts (app.js : ``✓ N serveur(s) ajouté(s)``)."""
        err = _check_dashboard_token(request)
        if err:
            return err
        body = await request.json()
        code = str(body.get("country") or "").strip().upper()
        try:
            limit = max(1, min(50, int(body.get("limit") or 5)))
        except (TypeError, ValueError):
            limit = 5
        if not code:
            return {"error": "country requis (code, ex: DE)"}
        servers = await _nordvpn_servers_by_country(code, limit=limit)
        if not servers:
            return {"error": f"aucun serveur trouvé pour {code}"}
        # code → nom de pays (server_countries garde des NOMS de pays)
        cname = code
        for c in await _nordvpn_countries(True):
            if str(c.get("code", "")).upper() == code:
                cname = c.get("name") or code
                break
        cfg = getattr(config_settings, "IP_ROTATION", None) or {}
        current = str(cfg.get("server_countries") or "")
        names = [c.strip() for c in current.split(",") if c.strip()]
        if cname not in names:
            names.append(cname)
        _persist_vpn_config({"server_countries": ", ".join(names)})
        return {"count": len(servers), "country": cname}

    @app.post("/api/vpn/upload-config")
    async def vpn_upload_config(request: Request):
        """Upload un fichier .ovpn (FormData ``{name, config}``) vers
        ``vpn_configs/custom/`` et persiste ``custom_ovpn_file``."""
        err = _check_dashboard_token(request)
        if err:
            return err
        form = await request.form()
        name = str(form.get("name") or "").strip() or "custom"
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        if not name.lower().endswith(".ovpn"):
            name += ".ovpn"
        upload = form.get("config")
        content = upload.file.read() if upload is not None else None
        if not content:
            return {"error": "fichier config manquant (champ FormData 'config')"}
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        custom_dir = os.path.join(root, "vpn_configs", "custom")
        os.makedirs(custom_dir, exist_ok=True)
        path = os.path.join(custom_dir, name)
        with open(path, "wb") as f:
            f.write(content)
        rel = os.path.join("vpn_configs", "custom", name)
        _persist_vpn_config({"custom_ovpn_file": rel.replace(os.sep, "/")})
        return {"ok": True, "path": rel.replace(os.sep, "/")}

    @app.get("/api/vpn/diagnostic")
    async def vpn_diagnostic():
        """Bundle diagnostic (app.js vpnDiagnostic) — mode actuel, statut,
        NordVPN app, docker, WSL2, OpenVPN natif, recommandation. Tout
        l'I/O externe est dans les helpers isolés (monkeypatchables)."""
        import shutil

        managers = _dashboard_managers()
        mode = _vpn_proxy_mode()
        # État agrégé des stations actives.
        status, ip = "not_configured", None
        if managers:
            connected = [m for m in managers if getattr(m, "status", None) == "connected"]
            if connected:
                status = "connected"
            elif any(getattr(m, "status", None) in ("connecting", "rotating") for m in managers):
                status = "connecting"
            elif managers:
                status = "not_connected"
            ip = next((m.current_ip for m in managers if m.current_ip), None)
        nordvpn = {"available": False, "exe": None, "status": None}
        if status == "connected":
            st = await nordvpn_status()
            nordvpn = {"available": True, "exe": None, "status": st}
        # [fix 20/08][Axe 3] Tout l'I/O subprocess/docker est offloadé sur
        # des threads : un daemon docker gelé (le mode défaillance documenté
        # sur le serveur — load > 150) bloquait l'event loop jusqu'à ~115 s
        # (2×10 s diag + 15 s × N compose config + 5 s wsl) et gelait tous
        # les SSE en vol, les workers de rotation et les watchdogs.
        docker = await asyncio.to_thread(_docker_diag)
        wsl2 = {"available": False}
        try:
            r = await asyncio.to_thread(
                lambda: subprocess.run(
                    ["wsl", "--status"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=_CREATE_NO_WINDOW,
                )
            )
            wsl2["available"] = r.returncode == 0
        except Exception:
            pass
        openvpn = {"available": False, "path": None}
        ovpn_path = shutil.which("openvpn")
        if ovpn_path:
            openvpn = {"available": True, "path": ovpn_path}
        elif os.path.exists("/usr/sbin/openvpn"):
            openvpn = {"available": True, "path": "/usr/sbin/openvpn"}
        docker_ok = bool(docker.get("running"))
        if mode == "vpn":
            if status == "connected":
                rec = "Connexion VPN active — rotation IP opérationnelle"
            elif docker_ok:
                rec = "Docker OK — VPN non connecté (voir statut ci-dessus)"
            else:
                rec = "Docker absent ou arrêté — installer/démarrer Docker puis reconnecter"
        else:
            rec = f"Mode {mode} actif (pas de tunnel docker requis)"
        return {
            "current_mode": mode,
            "status": status,
            "ip": ip,
            "nordvpn_app": nordvpn,
            "docker": docker,
            "wsl2": wsl2,
            "openvpn": openvpn,
            "recommendation": rec,
            "config_yaml_dirty": (_config_yaml_mtime() != _config_yaml_known_mtime),
            "config_yaml_mtime": _config_yaml_mtime(),
            "config_yaml_known": _config_yaml_known_mtime,
            "compose_config": await asyncio.to_thread(_docker_compose_config),
        }

    # ── Traffic capture (Wireshark-like raw request view) ──

    @app.get("/api/traffic/status")
    async def get_traffic_status(request: Request):
        err = _check_dashboard_token(request)
        if err:
            return err
        _traffic_mark_viewer()
        status = _traffic_capture.status()
        # `enabled` = état effectif (lazy, viewer-gated) ; `user_enabled` =
        # l'intention du toggle (checkbox UI), capture reprise au retour viewer.
        if _traffic_user_enabled is None:
            _traffic_apply_lazy()
        status["user_enabled"] = bool(_traffic_user_enabled)
        return status

    @app.post("/api/traffic/config")
    async def set_traffic_config(request: Request):
        err = _check_dashboard_token(request)
        if err:
            return err
        global _traffic_user_enabled
        body = await request.json()
        if body.get("enabled") is not None:
            _traffic_user_enabled = bool(body.get("enabled"))
            _traffic_apply_lazy()
        else:
            _traffic_mark_viewer()
        _traffic_capture.configure(
            max_frames=body.get("max_frames"),
            body_cap=body.get("body_cap"),
            max_bytes=body.get("max_bytes"),
        )
        _debug(f"  [traffic] config updated -> {json.dumps(body)}")
        return {"ok": True, **(_traffic_capture.status())}

    @app.get("/api/traffic/frames")
    async def get_traffic_frames(
        request: Request,
        limit: int = 200,
        offset: int = 0,
        method: str = None,
        path: str = None,
        status: int = None,
        aborted: bool = None,
        since: float = None,
    ):
        err = _check_dashboard_token(request)
        if err:
            return err
        _traffic_mark_viewer()
        frames = _traffic_capture.frames(
            limit=limit,
            offset=offset,
            method=method,
            path=path,
            status=status,
            aborted=aborted,
            since=since,
        )
        return {"frames": frames, "total": len(frames), "status": _traffic_capture.status()}

    @app.get("/api/traffic/frames/{frame_id}")
    async def get_traffic_frame_detail(request: Request, frame_id: int):
        err = _check_dashboard_token(request)
        if err:
            return err
        _traffic_mark_viewer()
        frame = _traffic_capture.frame_detail(frame_id)
        if frame is None:
            return JSONResponse(status_code=404, content={"error": "frame non trouvée"})
        return frame

    @app.post("/api/traffic/clear")
    async def clear_traffic(request: Request):
        err = _check_dashboard_token(request)
        if err:
            return err
        _traffic_mark_viewer()
        n = _traffic_capture.clear()
        _debug(f"  [traffic] capture cleared ({n} frames)")
        return {"ok": True, "cleared": n}

    @app.get("/api/traffic/stats")
    async def get_traffic_stats(request: Request, window: float = 60.0):
        err = _check_dashboard_token(request)
        if err:
            return err
        _traffic_mark_viewer()
        return _traffic_capture.stats(window=max(1.0, window))

    # ── Stats & history ──

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
        # [plan v10 §14.2.3] limit/offset NON bornés = dump table entière par
        # requête → clamp défensif (comme /api/routes).
        limit = max(1, min(int(limit or 20), 200))
        offset = max(0, int(offset or 0))
        where, params = _build_where(
            from_date, to_date, status, model, original_model, account, tool, search, station=station
        )
        query = "SELECT * FROM requests " + where + " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        def _query_history(db):
            rows = db.execute(query, params).fetchall()
            # [P2 perf] COUNT caché (TTL 2 s) : la pagination du dashboard
            # re-poll le même where en boucle — un COUNT par fenêtre suffit.
            count_key = f"histcount:{where}:{tuple(params[:-2])}"
            total_count = _stats_cache.get(count_key)
            if total_count is None:
                count_query = "SELECT COUNT(*) FROM requests " + where
                total_row = db.execute(count_query, params[:-2]).fetchone()
                total_count = total_row[0] if total_row else 0
                _stats_cache.set(count_key, total_count)
            return rows, total_count

        rows, total_count = await _db_read_sync(_query_history)

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
                    "station": r["station"] if "station" in r.keys() else None,
                    "free_ip": r["free_model_ip"]
                    if "free_model_ip" in r.keys() and r["free_model_ip"]
                    else "",
                    "geo_country": r["geo_country"] if "geo_country" in r.keys() else None,
                    "geo_blocked": bool(r["geo_blocked"]) if "geo_blocked" in r.keys() and r["geo_blocked"] else False,
                    "geo_direct_country": r["geo_direct_country"] if "geo_direct_country" in r.keys() else None,
                    "geo_direct_ip": r["geo_direct_ip"] if "geo_direct_ip" in r.keys() else None,
                    "geo_via_vpn": bool(r["geo_via_vpn"]) if "geo_via_vpn" in r.keys() and r["geo_via_vpn"] else False,
                    "geo_allowed": r["geo_allowed"] if "geo_allowed" in r.keys() else None,
                    "tools": json.loads(r["tools"])
                    if "tools" in r.keys() and r["tools"] and r["tools"] != "[]"
                    else [],
                    "tools_used": json.loads(r["tools_used"])
                    if "tools_used" in r.keys() and r["tools_used"] and r["tools_used"] != "[]"
                    else [],
                }
                for r in rows
            ],
            "total": total_count,
            "page": offset // limit + 1,
            "per_page": limit,
            "has_more": offset + limit < total_count,
        }

    @app.get("/api/requests/{req_id}")
    async def get_request_detail(req_id: str, request: Request):
        """Return full request details including request/response bodies."""
        err = _check_dashboard_token(request)
        if err:
            return err

        def _query_request(db):
            row = db.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
            return row

        row = await _db_read_sync(_query_request)
        if not row:
            return JSONResponse(status_code=404, content={"error": "Requête non trouvée"})

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
            "client_user_agent": row["client_user_agent"]
            if "client_user_agent" in row.keys()
            else None,
            "account_alias": row["account_alias"] if "account_alias" in row.keys() else None,
            "tools": _parse_json_field(row["tools"]) if "tools" in row.keys() else [],
            "tools_used": _parse_json_field(row["tools_used"])
            if "tools_used" in row.keys()
            else [],
            "request_body": _parse_json_field(row["request_body"])
            if "request_body" in row.keys()
            else None,
            "response_body": _parse_json_field(row["response_body"])
            if "response_body" in row.keys()
            else None,
        }

    @app.get("/api/history/filters")
    async def get_history_filters():
        """Return unique values for history filter dropdowns.

        [P2 perf] scan lourd (3× DISTINCT + parcours JSON de TOUTE la table)
        caché en longue durée ; invalidé par DELETE /api/history. Les tools
        utilisés viennent en premier du registre maintenu par le writer proxy
        (zéro scan), le scan SQL ne servant que de filet de fond.
        """
        global _filters_cache
        if _filters_cache is not None:
            return _filters_cache

        def _query_filters(db):
            models = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT model FROM requests WHERE model IS NOT NULL ORDER BY model"
                ).fetchall()
            ]
            orig_models = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT original_model FROM requests WHERE original_model IS NOT NULL ORDER BY original_model"
                ).fetchall()
            ]
            accounts = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT account_alias FROM requests WHERE account_alias IS NOT NULL ORDER BY account_alias"
                ).fetchall()
            ]
            # Extract all unique tool names from tools_used JSON arrays
            tool_set: set = set()
            if callable(_tools_provider):
                try:
                    tool_set.update(_tools_provider() or ())
                except Exception:
                    pass
            for row in db.execute(
                "SELECT tools_used FROM requests WHERE tools_used IS NOT NULL AND tools_used != '[]'"
            ):
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

        result = await _db_read_sync(_query_filters)
        _filters_cache = result
        return result

    @app.delete("/api/history")
    async def delete_history(before: str = None, all: bool = False, model: str = None):
        _debug(f"  [history] delete: all={all} model={model} before={before}")
        if not all and not before and not model:
            return {"error": "Spécifiez une date 'before', un nom de 'model' ou 'all=true'"}

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
                        token_usage[model]["input"] = token_usage[model]["output"] = token_usage[
                            model
                        ]["cache"] = 0
            elif before:
                conn.execute(
                    "DELETE FROM requests WHERE timestamp < ?",
                    (_normalize_date_bound(before, end_of_day=True),),
                )
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
        # [P2 perf] invalidation des caches dérivés après suppression
        global _filters_cache
        _filters_cache = None
        _stats_cache.invalidate()
        return {"status": "deleted"}
