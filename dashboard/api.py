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
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
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
    WEB_SEARCH_NATIVE_MODELS,
    apply_server_changes,
    save_api_keys,
    save_custom_routes,
    save_env,
)
from config.settings import validate_custom_routes as _validate_custom_routes

# [Phase 2 refonte] domicile canonique : observability.capture (traffic_capture.py = shim).
from observability.capture import capture as _traffic_capture

from .display import debug as _debug
from .display import log_lines
from .display import set_debug_log_file as _set_debug_log_file
from .events import get_event_manager
from .quota import (
    SWR_MAX_AGE_S,
    SWR_STALE_S,
    get_available_models,
    get_model_capabilities_for_all,
    get_model_limits_for_all,
    get_quota_snapshot,
    quota_cache_state,
)

# Windows: masquer la fenêtre console des subprocess (évite le flash noir 1s)
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
if hasattr(subprocess, "CREATE_NO_WINDOW"):
    _CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# [Phase 8 refonte] Couche statique extraite vers dashboard/routes/static.py
# (pure, args injectés). Mêmes objets ré-importés (api._precompress_static_assets
# consommé par test_static_cache_asgi.py ; montage inchangé).
from dashboard.routes.static import (  # noqa: E402
    StaticCacheMiddleware as _StaticCacheMiddleware,
)
from dashboard.routes.static import (  # noqa: E402
    load_precompressed as load_precompressed,
)
from dashboard.routes.static import (  # noqa: E402,F401  # re-export (test_static_cache_asgi.py)
    precompress_static_assets as _precompress_static_assets,
)

# ── Simple TTL cache for expensive dashboard queries ──


class _TTLCache:
    """In-memory cache with TTL for reducing redundant DB scans.

    [P4.2 perf] TTL par clé : set(key, value, ttl=...) permet 5-10 s pour
    stats/timeseries/ip_stats (aligné sur le re-fetch SSE) tout en gardant
    2 s pour histcount (les COUNT frais après SSE stats_updated)."""

    def __init__(self, ttl: float = 2.0):
        self._ttl = ttl
        self._store: dict[str, tuple[float, float, Any]] = {}
        self._cleanup_counter = 0

    def get(self, key: str):
        entry = self._store.get(key)
        if entry:
            ts, ttl, val = entry
            if (time.monotonic() - ts) < ttl:
                return val
            # expiré — éviction paresseuse
            self._store.pop(key, None)
        return None

    def set(self, key: str, value, ttl: float | None = None):
        use_ttl = float(ttl) if ttl is not None else self._ttl
        self._store[key] = (time.monotonic(), use_ttl, value)
        self._cleanup_counter += 1
        if self._cleanup_counter >= 50:
            self._cleanup_counter = 0
            self._evict_expired()

    def _evict_expired(self):
        now = time.monotonic()
        expired = [k for k, (ts, ttl, _) in self._store.items() if (now - ts) >= ttl]
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


def _set_filters_cache(value: dict | None) -> None:
    global _filters_cache
    _filters_cache = value
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


def _set_traffic_user_enabled(value: bool) -> None:
    global _traffic_user_enabled
    _traffic_user_enabled = bool(value)


# ── Dashboard auth (opt-in via DASHBOARD_TOKEN env) ──
# When DASHBOARD_TOKEN is set, sensitive endpoints require the header
# `X-Dashboard-Token` (constant-time comparison). Unset → legacy open access
# (documented: set DASHBOARD_TOKEN when the server is exposed beyond localhost).
# [F-H6] Fail-closed when DASHBOARD_REQUIRE_TOKEN=true and host 0.0.0.0 without token.

_DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()


def _require_token_from_config() -> bool:
    """[V7.2] `dashboard_trust.require_token` du YAML — jusqu'ici **ignoré**.

    Le code ne lisait que la variable d'environnement `DASHBOARD_REQUIRE_TOKEN`.
    Un utilisateur qui écrivait `require_token: true` dans `config.yaml` croyait
    donc exiger un token sur son dashboard : rien ne se passait. Un réglage de
    sécurité qui ne s'applique pas en silence est pire qu'un réglage absent.

    Précédence : variable d'environnement d'abord (comportement inchangé pour
    ceux qui l'utilisent), puis le YAML.
    """
    env = os.getenv("DASHBOARD_REQUIRE_TOKEN", "").strip().lower()
    if env:
        return env in ("1", "true", "yes")
    try:
        return bool(config_settings.yaml_get("dashboard_trust", "require_token", False))
    except Exception:
        return False


_DASHBOARD_REQUIRE_TOKEN = _require_token_from_config()
_host_for_warning = "0.0.0.0"
try:
    _host_for_warning = (os.getenv("OPENCODE_HOST", "") or "").strip()
    if not _host_for_warning:
        import yaml as _yaml_warn

        try:
            with open(
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"),
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
__CONFIG_YAML_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
_config_yaml_known_mtime: float = 0.0


def _config_yaml_mtime() -> float:
    try:
        return os.stat(__CONFIG_YAML_PATH).st_mtime
    except OSError:
        return 0.0


_config_yaml_known_mtime = _config_yaml_mtime()


def _set_config_yaml_known_mtime(value: float) -> None:
    global _config_yaml_known_mtime
    _config_yaml_known_mtime = float(value)


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


def _merge_preserved_api_keys(rows: list, *, replace: bool = False) -> list:
    """Merge posted api-key rows over the stored ones, KEEPING the stored
    secret when a posted secret field is empty (finding i).

    [audit 2026-10-30] The old version only preserved rows that were POSTed.
    The UI filtered out every existing row (its ``api_key`` is masked/empty),
    so saving one new key ERASED all the others and the .env fallback was
    disabled by the non-empty api_keys.json. Missing stored rows are now
    appended unless the caller explicitly asks for a replace (``replace``),
    and a row can be matched by alias OR by its masked value (alias rename).
    """
    if not isinstance(rows, list):
        return []
    stored_rows = list(API_KEYS)
    used: set[int] = set()
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        merged = dict(row)
        # Locate the stored row: alias first, then masked secret, then the
        # same index as a last resort (the UI keeps the original order).
        idx = None
        _alias = str(merged.get("alias", "") or "")
        if _alias:
            for i, old in enumerate(stored_rows):
                if i not in used and str(old.get("alias", "") or "") == _alias:
                    idx = i
                    break
        _masked = str(merged.get("api_key_masked", "") or "")
        if idx is None and _masked:
            for i, old in enumerate(stored_rows):
                if i in used:
                    continue
                _ok = str(old.get("api_key", "") or "")
                _om = (_ok[:4] + "****" + _ok[-4:]) if len(_ok) > 8 else "****"
                if _om == _masked:
                    idx = i
                    break
        if idx is None and not merged.get("api_key") and len(out) < len(stored_rows):
            idx = len(out)
        if idx is not None and idx not in used:
            old = stored_rows[idx]
            used.add(idx)
            for field in _SECRET_FIELDS:
                if not merged.get(field) and old.get(field):
                    merged[field] = old[field]
        # A brand-new row with no key is not a key: drop it instead of
        # storing an empty credential that select_next_key() would use.
        if not str(merged.get("api_key", "") or "").strip():
            continue
        merged["api_key"] = str(merged["api_key"]).strip()
        out.append(merged)
    if not replace:
        for i, old in enumerate(stored_rows):
            if i not in used:
                out.append(dict(old))
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
    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            if addr and not str(addr).startswith("127.") and "." in str(addr):
                if addr not in ips:
                    ips.append(str(addr))
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
    local = local_midnight + timedelta(days=1) - timedelta(seconds=1) if end_of_day else local_midnight
    return local.astimezone().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_date_bound(value, end_of_day: bool):
    """YYYY-MM-DD nu (sémantique locale) → borne UTC+Z ; timestamps complets passent tels quels."""
    if isinstance(value, str) and _DATE_ONLY_RE.match(value):
        return _date_bound_to_utc(value, end_of_day)
    return value


def daysAgo(n: int) -> str:
    """Instant UTC exact à J-n (timestamp complet avec Z ; passe _build_where inchangé)."""
    return (datetime.now(UTC) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _period_minutes(from_date, to_date, now: datetime | None = None) -> float | None:
    """Durée en minutes de la période sélectionnée, pour le débit moyen req/min.

    `to_date=today` normalisé = 23:59:59 local (futur) → clampé à `now`, donc pour
    « Aujourd'hui » la durée = temps écoulé depuis minuit, pas 24 h fixes.
    Retourne None si indéterminée (bornes absentes/inversées/durée nulle)."""
    if not from_date or not to_date:
        return None
    if now is None:
        now = datetime.now(UTC)
    try:
        start = datetime.strptime(_normalize_date_bound(from_date, False), _TS_FMT).replace(tzinfo=UTC)
        end = datetime.strptime(_normalize_date_bound(to_date, True), _TS_FMT).replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None
    end_eff = min(end, now)
    if end_eff <= start:
        return None
    minutes = (end_eff - start).total_seconds() / 60.0
    return minutes if minutes > 0 else None


def _period_rates(count: int, period_minutes: float | None) -> tuple[float | None, float | None]:
    """(period_minutes, avg_per_min) PUBLIÉS — cohérents entre eux.

    ``avg_per_min`` est calculé à partir de la période **arrondie**, celle qui
    est effectivement publiée et affichée, et non de la valeur brute. Sans
    cela les deux nombres du dashboard ne se réconcilient pas : une période
    réelle de 7,0499 min s'affiche « 7,0 min », mais 8 requêtes / 7,0499
    donnait 1,13/min alors que 8 / 7,0 = 1,14/min. L'utilisateur voyait donc
    « 8 requêtes · 7,0 min · 1,13 req/min », un calcul faux à l'écran.

    Cas limites :
      * période indéterminée          -> (None, None)  (la tuile affiche « — »)
      * période connue, 0 requête     -> (p, 0.0)      (pas None, pas d'exception)
      * période arrondie tombant à 0  -> on publie la valeur brute (une
        période de moins de 3 s n'est pas représentable à 1 décimale ; on
        garde ainsi la cohérence et on évite une division par zéro).
    """
    if period_minutes is None:
        return None, None
    période = round(period_minutes, 1)
    if période <= 0:
        période = period_minutes
    if count > 0:
        return période, round(count / période, 2)
    return période, 0.0


def _free_model_id_set() -> set[str]:
    """Ensemble canonique des ids gratuits (tier == free), fail-soft.

    Remplace les heuristiques ``-free`` : ``big-pickle`` est gratuit sans
    suffixe, et un id ``-free`` routé payant reste sémantiquement gratuit.
    Les ids historiques absents du catalogue restent couverts par le suffixe
    dans les appelants.
    """
    try:
        from config.live_models import get_model_entry
        from config.model_tier import classify_model
        from config.settings import (
            FREE_MODEL_MAP,
            FREE_MODELS,
            MODELS,
            get_free_substitution_policy,
            get_model_config,
        )

        policy = get_free_substitution_policy()
        ids = {str(mid) for mid in (FREE_MODELS or ())}
        for mid in MODELS:
            try:
                verdict = classify_model(
                    mid,
                    get_model_config(mid),
                    get_model_entry(mid),
                    free_models=FREE_MODELS,
                    free_model_map=FREE_MODEL_MAP,
                    policy=policy,
                )
            except Exception:
                if str(mid).endswith("-free"):
                    ids.add(str(mid))
                continue
            if verdict.get("tier") == "free":
                ids.add(str(mid))
        return ids
    except Exception:
        try:
            from config.settings import FREE_MODELS

            return {str(mid) for mid in (FREE_MODELS or ())}
        except Exception:
            return set()


def _pricing_config() -> dict:
    try:
        value = config_settings._yaml_data.get("pricing")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _compute_costs(rows, pricing: dict | None = None) -> dict:
    """[v10 §12.3.10] Coût payant + économies free à partir de lignes agrégées.

    ``rows`` : iterable de dicts {model, tokens_input, tokens_output}.
    ``pricing`` : {"currency", "defaults": {input/output_per_mtok},
    "per_model": {modèle: {...}}} — sinon défauts Anthropic Sonnet-class.

    Les modèles gratuits (tier canonique, pas seulement le suffixe ``-free``)
    ne coûtent rien mais génèrent ``free_saved_usd`` (ce qu'ils auraient coûté
    au tarif par défaut)."""
    p = pricing or {}
    currency = str(p.get("currency", "USD"))
    defaults = p.get("defaults") if isinstance(p.get("defaults"), dict) else {}
    rate_in = float(defaults.get("input_per_mtok", 3.0)) if isinstance(defaults, dict) else 3.0
    rate_out = float(defaults.get("output_per_mtok", 15.0)) if isinstance(defaults, dict) else 15.0
    per_model_cfg = p.get("per_model") if isinstance(p.get("per_model"), dict) else {}
    free_ids = _free_model_id_set()

    paid_usd = 0.0
    saved_usd = 0.0
    per_model_out: list[dict[str, Any]] = []
    seen = set()
    for row in rows:
        model = str(row.get("model") or "?")
        ti = float(row.get("tokens_input") or 0)
        to_ = float(row.get("tokens_output") or 0)
        key = (model, ti, to_)
        if key in seen:
            continue
        seen.add(key)
        rates = per_model_cfg.get(model) if isinstance(per_model_cfg, dict) else {}
        ri = float(rates.get("input_per_mtok", rate_in)) if isinstance(rates, dict) else rate_in
        ro_ = float(rates.get("output_per_mtok", rate_out)) if isinstance(rates, dict) else rate_out
        cost = ti / 1e6 * ri + to_ / 1e6 * ro_
        is_free = model in free_ids or model.endswith("-free")
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


_QUALITY_BLOCK_SPEC: dict[str, dict[str, tuple]] = {
    "quality_preselection": {
        "enabled": (bool, None, None),
        "fast_tps": (float, 0.1, 1000.0),
        "slow_tps": (float, 0.1, 1000.0),
        "max_load": (int, 0, 100),
        "tier_b_quota": (int, 1, 500),
        "tier_c_quota": (int, 1, 500),
    },
    "quality_rotation": {
        "enabled": (bool, None, None),
        "min_requests": (int, 1, 500),
        "min_samples": (int, 1, 20),
        "trend_pct": (float, 0.0, 100.0),
        "ttfb_ceiling_ms": (float, 100.0, 60000.0),
        "max_per_hour": (int, 0, 60),
        "global_slow_guard": (int, 1, 10),
    },
    "post_rotation_probe": {
        "enabled": (bool, None, None),
        "max_attempts": (int, 1, 5),
        "max_tokens": (int, 1, 32),
    },
}


def _quality_blocks_errors(body) -> list:
    """Validation des blocs qualité/presélection (partiels acceptés)."""
    errors: list = []
    if not isinstance(body, dict):
        return errors
    for block, spec in _QUALITY_BLOCK_SPEC.items():
        if block not in body:
            continue
        data = body[block]
        if not isinstance(data, dict):
            errors.append(f"{block} doit être un objet")
            continue
        for key, val in data.items():
            if key not in spec:
                errors.append(f"{block}.{key} non supporté")
                continue
            typ, lo, hi = spec[key]
            if typ is bool:
                if not isinstance(val, bool):
                    errors.append(f"{block}.{key} doit être booléen")
                continue
            try:
                num = float(val)
            except (TypeError, ValueError):
                errors.append(f"{block}.{key} doit être numérique")
                continue
            if lo is not None and not lo <= num <= hi:
                errors.append(f"{block}.{key}={num} hors bornes [{lo}, {hi}]")
            if typ is int and not num.is_integer():
                errors.append(f"{block}.{key} doit être un entier")
        if block == "quality_preselection":
            _fast = data.get("fast_tps")
            _slow = data.get("slow_tps")
            if _fast is not None and _slow is not None:
                try:
                    if float(_slow) >= float(_fast):
                        errors.append("quality_preselection.slow_tps doit être < fast_tps")
                except (TypeError, ValueError):
                    pass
    return errors


def _validate_vpn_config_payload(body) -> list:
    """[v10 §12.1.4] validation DRY-RUN d'un payload VPN — retourne la liste
    des erreurs (vide = valide). Mêmes règles que les endpoints d'application,
    sans toucher au moindre état."""
    errors: list = []
    if not isinstance(body, dict):
        return ["le payload doit être un objet"]

    errors.extend(_quality_blocks_errors(body))

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
                        v_f = float(lr[k])
                        if not lo <= v_f <= hi:
                            errors.append(f"latency_rotation.{k}={v_f} hors bornes [{lo}, {hi}]")
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
            en = lr.get("enabled")
            if en is not None and not isinstance(en, bool):
                errors.append("latency_rotation.enabled doit être booléen")

    per = body.get("per_station")
    if per is not None:
        if not isinstance(per, dict):
            errors.append('per_station doit être un objet {"N": {...}}')
        else:
            allowed_keys = {
                "quota_per_ip",
                "country_offset",
                "country_offset_stride",
                "watchdog_interval",
                "proxy_mode",
            }
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

        # Source unique du chemin (constante de module `__CONFIG_YAML_PATH`)
        # plutôt qu'un recalcul local depuis `__file__` : les deux désignent le
        # même fichier, mais seule la constante peut être redirigée par un test
        # — sans ça aucun test ne peut exercer ce persist sur une copie.
        config_path = __CONFIG_YAML_PATH
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
                "egress_providers": "egress_providers",
                "egress_strategy": "egress_strategy",
                "egress_weights": "egress_weights",
                "dual_station": "dual_station",
                "strict_free": "strict_free",
                "free_substitution_policy": "free_substitution_policy",
                "quota_per_ip": "quota_per_ip",
                "switch_delay": "switch_delay",
                "docker_container": "docker_container",
                "docker_compose_file": "docker_compose_file",
                "vpn_proxy_port": "vpn_proxy_port",
                "socks5_proxy_port": "socks5_proxy_port",
                "credentials_file": "credentials_file",
                "server_countries": "server_countries",
                # [2026-11-03 any-country] Perimeter + rotation were hot-
                # reloadable in the manager but never persisted by the
                # dashboard: a GUI/API change reverted on restart. Persist
                # them with the rest of the ip_rotation block.
                "country_rotation": "country_rotation",
                "country_offset": "country_offset",
                "country_offset_stride": "country_offset_stride",
                "egress_allow_any_country": "egress_allow_any_country",
                "free_model_spread": "free_model_spread",
                # [public-proxies] feed/worker lifecycle block.
                "public_proxies": "public_proxies",
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
                # [GUI « Essais gratuits / requête »] Auto = budget dérivé du
                # nombre de stations (comportement historique) ; false = la
                # valeur 1-3 du sélecteur est réellement appliquée.
                "auto_max_free_attempts": "auto_max_free_attempts",
                "free_exception_fallback": "free_exception_fallback",
                # [Axe 3.1] socks5 backend (static proxy list, auto-rotate toggle,
                # NordVPN country API, custom .ovpn file) — all persisted config.
                "socks5_proxies": "socks5_proxies",
                "socks5_auto_rotate": "socks5_auto_rotate",
                "socks5_fallback_enabled": "socks5_fallback_enabled",
                "https_proxies": "https_proxies",
                "https_fallback_enabled": "https_fallback_enabled",
                "use_nordvpn_api": "use_nordvpn_api",
                "custom_ovpn_file": "custom_ovpn_file",
                # warm-avalanche
                "ovpn_protocol": "ovpn_protocol",
                "ovpn_endpoint_port": "ovpn_endpoint_port",
                "auto_hetero_boot": "auto_hetero_boot",
                "on_429_action": "on_429_action",
                # `station_bad_ttl_s` = cooldown station en SECONDES (GUI
                # « Cooldown (s) »). `bad_ttl` reste le TTL fast-pin en
                # MINUTES : les deux unités ne doivent plus être confondues.
                "station_bad_ttl_s": "station_bad_ttl_s",
                "bad_ttl": "bad_ttl",
                # [phase 1] qui choisit les serveurs VPN : proxy | gluetun.
                "server_pick_mode": "server_pick_mode",
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
        # [GUI toggle « Cooldown latence »] latency_rotation : merge imbriqué
        # symétrique à free_parallel (preserve les clés seuils existantes dans
        # config.yaml — un POST {"enabled": false} ne doit PAS les écraser).
        _lr_changed = False
        if isinstance(updates.get("latency_rotation"), dict):
            existing_lr = ip_rot.get("latency_rotation") if isinstance(ip_rot.get("latency_rotation"), dict) else {}
            merged_lr = {**existing_lr, **updates["latency_rotation"]}
            if merged_lr != existing_lr:
                ip_rot["latency_rotation"] = merged_lr
                _lr_changed = True
            updates = {k: v for k, v in updates.items() if k != "latency_rotation"}
        # [GUI qualité] blocs imbriqués partiels : merge sans écraser les seuils
        # déjà présents. Un POST {"enabled": false} ne doit pas perdre fast_tps.
        _quality_changed = False
        for _block in _QUALITY_BLOCK_SPEC:
            if not isinstance(updates.get(_block), dict):
                continue
            _existing = ip_rot.get(_block) if isinstance(ip_rot.get(_block), dict) else {}
            _merged = {**_existing, **updates[_block]}
            if _merged != _existing:
                ip_rot[_block] = _merged
                _quality_changed = True
            updates = {k: v for k, v in updates.items() if k != _block}
        # [010] egress_capacity : merge imbriqué pour ne pas perdre
        # target_slots quand seul reserve_policy change.
        _capacity_changed = False
        if isinstance(updates.get("egress_capacity"), dict):
            _existing_cap = (
                ip_rot.get("egress_capacity")
                if isinstance(ip_rot.get("egress_capacity"), dict)
                else {}
            )
            _merged_cap = {**_existing_cap, **updates["egress_capacity"]}
            if _merged_cap != _existing_cap:
                ip_rot["egress_capacity"] = _merged_cap
                _capacity_changed = True
            updates = {k: v for k, v in updates.items() if k != "egress_capacity"}
        changed = _fp_changed or _lr_changed or _quality_changed or _capacity_changed
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
                        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
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
                        if "circuit_breaker_threshold" in updates or "circuit_breaker_recovery" in updates:
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


_RETRY_UNTIL_SUCCESS_DEFAULTS = {
    "enabled": False,
    "max_wait_s": 900,
    "base_delay_s": 1.0,
    "max_delay_s": 60.0,
    "keepalive_interval_s": 10,
    "buffer_max_bytes": 10485760,
}


def _retry_until_success_config() -> dict:
    """Vue effective de `retry_until_success` (défauts inclus)."""
    try:
        from config import settings as _st

        raw = _st._yaml_data.get("retry_until_success")
    except Exception:
        raw = None
    cfg = dict(_RETRY_UNTIL_SUCCESS_DEFAULTS)
    if isinstance(raw, dict):
        cfg.update(raw)
    return cfg


def _write_config_yaml_atomic(config: dict, config_path: str) -> None:
    """Écriture atomique config.yaml (tempfile + fsync + replace)."""
    import tempfile

    import yaml

    dir_name = os.path.dirname(config_path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".config.yaml.tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
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


def _persist_retry_until_success(updates: dict) -> str | None:
    """Persiste `retry_until_success` (top-level) + met à jour le miroir.

    Le middleware lit `config.settings._yaml_data` à chaque requête : la
    mutation en place suffit pour un hot-reload sans redémarrage.
    """
    if not isinstance(updates, dict):
        return "retry_until_success doit être un objet"
    import yaml

    config_path = __CONFIG_YAML_PATH
    if not os.path.exists(config_path):
        return f"config.yaml introuvable: {config_path}"
    with _persist_lock:
        try:
            with open(config_path, encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        except Exception as e:
            return f"lecture config.yaml échouée: {type(e).__name__}: {e}"
        current = config.get("retry_until_success")
        current = dict(current) if isinstance(current, dict) else {}
        merged = {**_RETRY_UNTIL_SUCCESS_DEFAULTS, **current, **updates}
        # Bornes de sécurité — mêmes valeurs que RetryPolicy.
        try:
            merged["enabled"] = bool(merged.get("enabled", False))
        except Exception:
            merged["enabled"] = False
        try:
            merged["max_wait_s"] = max(0.0, min(86400.0, float(merged.get("max_wait_s", 900))))
        except (TypeError, ValueError):
            merged["max_wait_s"] = 900.0
        try:
            merged["base_delay_s"] = max(0.05, min(300.0, float(merged.get("base_delay_s", 1.0))))
        except (TypeError, ValueError):
            merged["base_delay_s"] = 1.0
        try:
            merged["max_delay_s"] = max(
                float(merged["base_delay_s"]),
                min(3600.0, float(merged.get("max_delay_s", 60.0))),
            )
        except (TypeError, ValueError):
            merged["max_delay_s"] = max(float(merged["base_delay_s"]), 60.0)
        try:
            merged["keepalive_interval_s"] = max(1.0, min(120.0, float(merged.get("keepalive_interval_s", 10))))
        except (TypeError, ValueError):
            merged["keepalive_interval_s"] = 10.0
        try:
            merged["buffer_max_bytes"] = max(1024, min(64 * 1024 * 1024, int(merged.get("buffer_max_bytes", 10485760))))
        except (TypeError, ValueError):
            merged["buffer_max_bytes"] = 10485760
        if merged == current:
            return None
        config["retry_until_success"] = merged
        try:
            _write_config_yaml_atomic(config, config_path)
        except Exception as e:
            return f"écriture config.yaml échouée: {type(e).__name__}: {e}"
        try:
            from config import settings as _st

            _st._yaml_data["retry_until_success"] = dict(merged)
            _st._config_yaml_mtime = _config_yaml_mtime()
        except Exception:
            pass
        try:
            global _config_yaml_known_mtime
            _config_yaml_known_mtime = _config_yaml_mtime()
        except Exception:
            pass
        _debug(f"  [retry] config persisted: {merged}")
    return None


def _persist_free_model_map(mapping: dict):
    """Persist ``free_model_map`` to config.yaml (top-level key, best-effort).

    Unlike ip_rotation keys it does NOT live under ``ip_rotation:`` — it is a
    first-class top-level key (config/settings.py ``yaml_get("free_model_map")``),
    so `_persist_vpn_config`'s key_map cannot reach it.
    """
    try:
        import yaml

        # Même constante de module que `_persist_vpn_config` : un seul chemin,
        # redirigeable par les tests (aucun test ne doit écrire le vrai fichier).
        config_path = __CONFIG_YAML_PATH
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


def _write_credentials_env(username: str, password: str, path: str | None = None):
    """Write NordVPN credentials to credentials.env — the single source
    gluetun reads via docker-compose env_file ([24]).

    Plaintext on disk by design (docker compose env_file format); the file
    is .gitignored. A container restart is needed for gluetun to pick up
    the new values.

    [audit 2026-10-30] UPSERT, never overwrite: this file also carries
    ``VPN_CONTROL_API_KEY``, ``HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE`` and
    ``NORDVPN_TOKEN``. The historical ``open(..., "w")`` erased them, which
    made every recreated gluetun container reject its own control API (401)
    and loop unhealthy. Secrets are never logged; values containing a line
    break are rejected (env-file injection).
    """
    import os

    for value, name in ((username, "username"), (password, "password")):
        if "\n" in value or "\r" in value:
            raise ValueError(f"{name} must not contain a line break")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cred_path = path or os.path.join(root, "credentials.env")
    updates = {"OPENVPN_USER": username, "OPENVPN_PASSWORD": password}

    lines: list[str] = []
    if os.path.exists(cred_path):
        with open(cred_path, encoding="utf-8") as f:
            lines = f.read().splitlines()

    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.lstrip()
        if "=" in line and not stripped.startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")

    tmp_path = f"{cred_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp_path, cred_path)
    try:
        os.chmod(cred_path, 0o600)
    except Exception:
        pass
    _debug(f"  [vpn] credentials written to {cred_path} (other keys preserved)")


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
                "label": r.get("label", ""),
                "ipv6": bool(r.get("ipv6", False)),
                "egress_ip": r.get("egress_ip", ""),
                "block": r.get("block", ""),
                "managed": bool(r.get("managed", False)),
                "container": r.get("container", ""),
                "provider": r.get("provider", ""),
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


def _dashboard_egress_catalog():
    """The catalog runtime installed by the lifespan (None when disabled)."""
    try:
        import shared_state

        return getattr(shared_state, "egress_catalog_runtime", None)
    except Exception:
        return None


def _qualification_db_path() -> str:
    """Registry path used by the qualification read models."""
    runtime = _dashboard_egress_catalog()
    store = getattr(runtime, "store", None)
    return str(getattr(store, "path", "logs/registry.db"))


def _egress_observed_counts() -> dict[str, int]:
    """Requêtes assignées par provider (cumul pool) pour le conseiller 010."""
    pool = _dashboard_pool()
    if pool is not None and hasattr(pool, "provider_request_counts"):
        try:
            counts = pool.provider_request_counts()
            if isinstance(counts, dict):
                return {
                    str(key): max(0, int(value or 0))
                    for key, value in counts.items()
                }
        except Exception:
            pass
    return {}


def _egress_endpoint_observed() -> list[dict]:
    """Compteurs par endpoint (cumul pool) pour T029."""
    pool = _dashboard_pool()
    if pool is not None and hasattr(pool, "endpoint_request_counts"):
        try:
            counts = pool.endpoint_request_counts()
            if isinstance(counts, list):
                return [item for item in counts if isinstance(item, dict)]
        except Exception:
            pass
    return []


def _egress_endpoint_inventory() -> list[dict]:
    """Inventaire WARP/proxys pour les cases à cocher de la carte Capacité."""
    pool = _dashboard_pool()
    if pool is not None and hasattr(pool, "endpoint_inventory"):
        try:
            rows = pool.endpoint_inventory()
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        except Exception:
            pass
    return []


def _egress_capacity_counts() -> dict[str, dict[str, int]]:
    """Capacité configurée/activée/utilisable par provider (010).

    Lecture seule, sans Docker/probe réseau. NordVPN vient de
    ``station_count`` + du pool ; WARP/proxys du pool quand il est initialisé,
    sinon des lignes ``socks5_proxies`` du config.
    """
    from config.loader import resolved_station_count

    cfg = getattr(config_settings, "IP_ROTATION", None) or {}
    managers = _dashboard_managers()
    configured = resolved_station_count(cfg)
    enabled = sum(1 for manager in managers if getattr(manager, "enabled", True))
    configured = max(configured, enabled)
    usable = enabled
    pool = _dashboard_pool()
    if pool is not None and hasattr(pool, "usable_station_count"):
        try:
            usable = int(pool.usable_station_count())
        except Exception:
            usable = enabled
    usable = min(max(0, usable), enabled)

    counts: dict[str, dict[str, int]] = {
        "nordvpn": {
            "configured": configured,
            "enabled": enabled,
            "usable": usable,
        },
        "warp": {"configured": 0, "enabled": 0, "usable": 0},
        "proxies": {"configured": 0, "enabled": 0, "usable": 0},
        "direct": {"configured": 1, "enabled": 1, "usable": 1},
    }

    endpoint_counts = None
    if pool is not None and hasattr(pool, "egress_endpoint_counts"):
        try:
            endpoint_counts = pool.egress_endpoint_counts()
        except Exception:
            endpoint_counts = None
    if isinstance(endpoint_counts, dict):
        for name in ("warp", "proxies"):
            raw = endpoint_counts.get(name)
            if isinstance(raw, dict):
                counts[name] = {
                    "configured": max(0, int(raw.get("configured", 0) or 0)),
                    "enabled": max(0, int(raw.get("enabled", 0) or 0)),
                    "usable": max(0, int(raw.get("usable", 0) or 0)),
                    "unique_buckets": max(
                        0,
                        int(raw.get("unique_buckets", raw.get("usable", 0)) or 0),
                    ),
                }
    else:
        rows = cfg.get("socks5_proxies") or []
        if isinstance(rows, list):
            blocks: dict[str, set[str]] = {"warp": set(), "proxies": set()}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                provider = str(row.get("provider") or "").strip().lower()
                name = "warp" if row.get("managed") or provider == "warp" else "proxies"
                counts[name]["configured"] += 1
                if row.get("enabled", True):
                    counts[name]["enabled"] += 1
                    counts[name]["usable"] += 1
                    block = str(row.get("block") or "").strip()
                    if not block:
                        from free.egress_provider import block_for_egress

                        block = block_for_egress(row.get("egress_ip")) or str(
                            row.get("label") or row.get("port") or ""
                        )
                    if block:
                        blocks[name].add(block)
            for name in ("warp", "proxies"):
                counts[name]["unique_buckets"] = len(blocks[name])
    return counts


def _as_index(value) -> int:
    """Index int strict — -1 sur toute valeur non-indexable (les bornes
    sont vérifiées par l'appelant)."""
    try:
        idx = int(value)
    except (TypeError, ValueError):
        return -1
    return idx


def _egress_audit(action: str, details: dict) -> None:
    """Journal append-only des actions capacité/egress (T025).

    Ne doit contenir aucun secret (jamais mot de passe/username). Best-effort :
    un échec disque ne casse pas l'action opérateur.
    """
    try:
        root = os.path.dirname(__CONFIG_YAML_PATH)
        path = os.path.join(root, "logs", "egress_audit.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "action": str(action),
            **details,
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _apply_socks5_rows(rows: list, *, action: str = "socks5_update") -> dict | None:
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
                # [010] préserver les métadonnées WARP : un toggle GUI ne doit
                # pas transformer un endpoint managé en proxy statique.
                "label": str(r.get("label") or "").strip(),
                "ipv6": bool(r.get("ipv6", False)),
                "egress_ip": str(r.get("egress_ip") or "").strip(),
                "managed": bool(r.get("managed", False)),
                "container": str(r.get("container") or "").strip(),
                "image": str(r.get("image") or "").strip(),
                "block": str(r.get("block") or "").strip(),
                "provider": str(r.get("provider") or "").strip(),
            }
        )
    err = _persist_vpn_config({"socks5_proxies": cleaned})
    if not err:
        _egress_audit(
            action,
            {
                "count": len(cleaned),
                "rows": [
                    {
                        "host": row["host"],
                        "port": row["port"],
                        "enabled": row["enabled"],
                        "label": row.get("label", ""),
                        "managed": row.get("managed", False),
                        "provider": row.get("provider", ""),
                    }
                    for row in cleaned
                ],
            },
        )
        try:
            import shared_state

            pool = getattr(shared_state, "free_ip_pool", None)
            if pool is not None and hasattr(pool, "set_socks5_proxies"):
                pool.set_socks5_proxies(cleaned)
        except Exception as e:
            _debug(f"  [vpn] socks5 pool update failed: {e}")
    return None


def _https_manual_rows(pool) -> list:
    """Lignes HTTPS gérées par l'opérateur (jamais les endpoints publics)."""
    rows = getattr(pool, "_manual_https_proxies", None)
    if rows is None:
        rows = getattr(pool, "_https_proxies", None) or []
    return list(rows)


def _https_payload(pool) -> list:
    """HTTPS CONNECT proxy rows for the GUI. Passwords never leave the
    server; the browser only learns whether one is set."""
    rows = _https_manual_rows(pool)
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


def _apply_https_rows(rows: list) -> dict | None:
    """Validate + persist the HTTPS proxy list, then push it to the pool."""
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
    err = _persist_vpn_config({"https_proxies": cleaned})
    if not err:
        pool = _dashboard_pool()
        if pool is not None and hasattr(pool, "set_https_proxies"):
            try:
                pool.set_https_proxies(cleaned)
            except Exception as e:
                _debug(f"  [vpn] https pool update failed: {e}")
    return None


def _https_fallback_state() -> bool:
    """``https_fallback_enabled`` — config mirror first, pool second."""
    try:
        cfg = getattr(config_settings, "IP_ROTATION", None) or {}
        if "https_fallback_enabled" in cfg:
            return bool(cfg["https_fallback_enabled"])
        pool = _dashboard_pool()
        if pool is not None and hasattr(pool, "_https_fallback_enabled"):
            return bool(pool._https_fallback_enabled)
    except Exception:
        pass
    return False


def _stored_proxy_credentials(pool, attr: str, host: str, port: int) -> tuple:
    """Look up a stored static proxy's credentials by host:port so the test
    button never has to resend (or expose) the password."""
    for row in getattr(pool, attr, None) or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("host") or "") != str(host or ""):
            continue
        try:
            if int(row.get("port") or 0) != int(port):
                continue
        except (TypeError, ValueError):
            continue
        return row.get("username"), row.get("password")
    return None, None


def _socks5_fallback_state() -> bool:
    """``socks5_fallback_enabled`` — config mirror first, pool second."""
    try:
        cfg = getattr(config_settings, "IP_ROTATION", None) or {}
        if "socks5_fallback_enabled" in cfg:
            return bool(cfg["socks5_fallback_enabled"])
        pool = _dashboard_pool()
        if pool is not None and hasattr(pool, "_socks5_fallback_enabled"):
            return bool(pool._socks5_fallback_enabled)
    except Exception:
        pass
    return False


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


async def _socks5_probe(
    host: str, port: int, username=None, password=None, *, ipv6: bool = False
) -> dict:
    """Test a static SOCKS5 proxy with the official Bun/TLS replay.

    The probe uses the same curl_cffi fingerprint as the inference leg
    (instead of a generic httpx/raw TLS client) so Cloudflare cannot reject
    a valid proxy on the TLS signature alone.
    ``ipv6`` force la résolution IPv6 (WARP IPv4/IPv6 partagent host:port).
    """
    host = (host or "").strip()
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"ok": False, "error": "port invalide"}
    if not host:
        return {"ok": False, "error": "host manquant"}

    def _run():
        from curl_cffi import requests as curl_requests

        proxy = _proxy_url("socks5h", host, port, username, password)
        kwargs = _apply_official_fp(
            {
                "proxies": {"http": proxy, "https": proxy},
                "timeout": 12,
                "verify": True,
            }
        )
        session_kwargs = {}
        if ipv6:
            try:
                from curl_cffi import CurlOpt

                session_kwargs["curl_options"] = {CurlOpt.IPRESOLVE: 2}
            except Exception:
                session_kwargs = {}
        started = time.monotonic()
        if ipv6:
            with curl_requests.Session(**session_kwargs) as session:
                r = session.get("https://api64.ipify.org/", **kwargs)
                elapsed = time.monotonic() - started
                ip = str(getattr(r, "text", "") or "").strip()
                ok = 200 <= int(getattr(r, "status_code", 0) or 0) < 400
                try:
                    r2 = session.get("https://opencode.ai/", **kwargs)
                    oc_ok = 200 <= int(getattr(r2, "status_code", 0) or 0) < 400
                except Exception:
                    oc_ok = False
        else:
            r = curl_requests.get("https://api.ipify.org/", **kwargs)
            elapsed = time.monotonic() - started
            ip = str(getattr(r, "text", "") or "").strip()
            ok = 200 <= int(getattr(r, "status_code", 0) or 0) < 400
            try:
                r2 = curl_requests.get("https://opencode.ai/", **kwargs)
                oc_ok = 200 <= int(getattr(r2, "status_code", 0) or 0) < 400
            except Exception:
                oc_ok = False
        return ip, ok, elapsed, oc_ok

    try:
        ip, ok, elapsed, oc_ok = await asyncio.to_thread(_run)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": ok, "ip": ip, "opencode_ok": oc_ok, "latency_ms": round(elapsed * 1000, 1)}


def _proxy_url(
    scheme: str,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
) -> str:
    """Build a proxy URL with URL-escaped credentials."""
    from urllib.parse import quote

    if username or password:
        user = quote(username or "", safe="")
        pwd = quote(password or "", safe="")
        auth = f"{user}:{pwd}@"
    else:
        auth = ""
    return f"{scheme}://{auth}{host}:{port}"


def _apply_official_fp(kwargs: dict) -> dict:
    """Apply the measured OpenCode Bun/TLS replay to a curl_cffi call."""
    try:
        import opencode as oc

        impersonate, fp = oc._get_free_fp_kwargs("1.19.5")
        if impersonate:
            kwargs["impersonate"] = impersonate
        if fp:
            kwargs.update(fp)
    except Exception:
        pass
    return kwargs


async def _https_probe(host: str, port: int, username=None, password=None) -> dict:
    """Probe one NordVPN HTTPS CONNECT proxy (``host:89``).

    The proxy leg is TLS but NordVPN's certificate does not match the proxy
    hostname, so proxy verification is disabled via curl options while the
    target (api.ipify.org) stays verified. curl handles the nested TLS.
    """
    host = (host or "").strip()
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"ok": False, "error": "port invalide"}
    if not host:
        return {"ok": False, "error": "host manquant"}

    def _run():
        from curl_cffi import requests as curl_requests
        from curl_cffi.const import CurlOpt

        proxy = _proxy_url("https", host, port, username, password)
        kwargs = _apply_official_fp(
            {
                "proxies": {"http": proxy, "https": proxy},
                "timeout": 12,
                "verify": True,
                "curl_options": {
                    CurlOpt.PROXY_SSL_VERIFYPEER: 0,
                    CurlOpt.PROXY_SSL_VERIFYHOST: 0,
                },
            }
        )
        started = time.monotonic()
        r = curl_requests.get("https://api.ipify.org/", **kwargs)
        elapsed = time.monotonic() - started
        ip = str(getattr(r, "text", "") or "").strip()
        ok = 200 <= int(getattr(r, "status_code", 0) or 0) < 400
        try:
            r2 = curl_requests.get("https://opencode.ai/", **kwargs)
            oc_ok = 200 <= int(getattr(r2, "status_code", 0) or 0) < 400
        except Exception:
            oc_ok = False
        return ip, ok, elapsed, oc_ok

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


def _docker_diag() -> dict[str, Any]:
    """System docker info for the diagnostic bundle — isolated subprocess,
    monkeypatchable (never live in tests)."""
    diag: dict[str, Any] = {"available": False, "running": False, "version": None}
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


_SECRET_ENV_LINE_RE = re.compile(
    r"(?im)^(\s*[A-Za-z0-9_]*"
    r"(?:KEY|TOKEN|PASSWORD|PASS|SECRET|COOKIE|AUTH|PRIVATE|CREDENTIAL|USER)"
    r"[A-Za-z0-9_]*\s*[:=]\s*)(.*)$"
)


def _redact_compose_text(text: str) -> str:
    """Mask secrets in raw `docker compose config` output.

    [audit 2026-10-30] The diagnostic bundle returned the full compose
    model, including ``OPENCODE_API_KEY``, the Go auth cookie, the NordVPN
    token and the gluetun control key. The dashboard is reachable on the
    LAN by default, so this was a credential leak. Keep the keys visible
    for diagnosis; replace only their values.
    """
    if not isinstance(text, str) or not text:
        return text
    return _SECRET_ENV_LINE_RE.sub(lambda m: f"{m.group(1)}\"***REDACTED***\"", text)


def _docker_compose_config() -> list:
    """`docker compose config` output per active station (diagnostic
    bundle). Empty list when no managers or docker is absent. Secret
    values are redacted before leaving the process."""
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
            out.append(f"=== {df}\n{_redact_compose_text((r.stdout or r.stderr).strip())}")
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
    # [P1.3 perf] Cache-Control + gzip/brotli pré-compressé via middleware PUR
    # ASGI (module-level _StaticCacheMiddleware) : ~0.05 ms/requête au lieu de
    # ~1-8 ms avec BaseHTTPMiddleware sur TOUTES les requêtes y compris SSE.
    # PAS de GZipMiddleware global : il compresserait les flux SSE
    # (tampon → casse le temps réel).
    # [Phase 5 boot] Les octets viennent des .br/.gz générés AU BUILD
    # (scripts/precompress.py) quand ils existent — plus de compression zlib
    # de 250 Ko de JS à chaque démarrage ; compression à la volée seulement en
    # repli (checkout sans build). Négociation br > gzip par requête.
    _pre_static = load_precompressed(static_dir)
    app.add_middleware(_StaticCacheMiddleware, precompressed=_pre_static)

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

    # [fresh-ip-hunter] Reprise au boot si le toggle GUI était activé.
    try:
        from free import hunter as _hunter

        _hunter.ensure_started()
    except Exception:
        pass

    # index.html : le front reconstruit (Vite) vit dans `static/dist/`, l'ancien
    # front reste dans `static/` (repli si `npm run build` n'a jamais tourné, par
    # exemple sur un checkout nu). On sert le premier candidat lisible.
    # Lu en mémoire — `/` est le chemin le plus sollicité — et RELU seulement si
    # le mtime a bougé : après un `npm run build`, un simple rafraîchissement du
    # navigateur suffit, sans redémarrer le proxy.
    _index_cache: dict[str, Any] = {"path": None, "mtime": -1.0, "data": None}

    def _load_index() -> bytes | None:
        for candidate in (
            os.path.join(static_dir, "dist", "index.html"),
            os.path.join(static_dir, "index.html"),
        ):
            try:
                st = os.stat(candidate)
            except OSError:
                continue
            if st.st_size <= 0:
                continue
            if _index_cache["path"] == candidate and _index_cache["mtime"] == st.st_mtime:
                return _index_cache["data"]
            try:
                with open(candidate, "rb") as _f:
                    data = _f.read()
            except OSError:
                continue
            _index_cache.update(path=candidate, mtime=st.st_mtime, data=data)
            return data
        return None

    from dashboard.routers.root_routes import register_root_route

    register_root_route(app, index_loader=lambda: _load_index())

    # ── Config endpoints ──

    from dashboard.routers.config_routes import register_config_routes

    register_config_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        settings=config_settings,
        get_models=lambda: get_available_models(),
        local_ips_cache_getter=lambda: _local_ips_cache,
        get_local_ips=lambda: _get_local_ips(),
        default_port=PORT,
        proxy_getter=lambda: PROXY,
        api_key_getter=lambda: API_KEY,
        host_getter=lambda: HOST,
        api_keys_getter=lambda: API_KEYS,
        api_key_routing_getter=lambda: API_KEY_ROUTING,
        model_limits=lambda models: get_model_limits_for_all(models),
        model_capabilities=lambda models: get_model_capabilities_for_all(models),
        save_env=lambda updates: save_env(updates),
        apply_server_changes=lambda **kwargs: apply_server_changes(**kwargs),
        server_manager_getter=(
            lambda: server_manager_getter() if server_manager_getter else None
        ),
        debug=lambda message: _debug(message),
    )

    from dashboard.routers.route_table_routes import register_route_table_routes

    register_route_table_routes(
        app,
        settings=config_settings,
    )

    from dashboard.routers.geo_routes import register_geo_routes

    register_geo_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        settings=config_settings,
        config_yaml_mtime=lambda: _config_yaml_mtime(),
        config_yaml_known_mtime_setter=_set_config_yaml_known_mtime,
        root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )

    from dashboard.routers.config_extra_routes import register_config_extra_routes

    register_config_extra_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        settings=config_settings,
        models_getter=lambda: MODELS,
        native_models=WEB_SEARCH_NATIVE_MODELS,
        yaml_get=lambda *args, **kwargs: config_settings.yaml_get(*args, **kwargs),
        yaml_set=lambda *args, **kwargs: config_settings.yaml_set(*args, **kwargs),
        validate_custom_routes=lambda body: _validate_custom_routes(body),
        save_custom_routes=lambda body: save_custom_routes(body),
        api_keys_getter=lambda: API_KEYS,
        api_key_routing_getter=lambda: API_KEY_ROUTING,
        merge_preserved_api_keys=(
            lambda rows, replace=False: _merge_preserved_api_keys(
                rows,
                replace=replace,
            )
        ),
        save_api_keys=lambda rows: save_api_keys(rows),
        save_env=lambda updates: save_env(updates),
        debug=lambda message: _debug(message),
    )

    # ── Proxy control ──

    from dashboard.routers.proxy_routes import register_proxy_routes

    register_proxy_routes(
        app,
        server_manager_getter=server_manager_getter,
        default_port=PORT,
    )

    from dashboard.routers.stats_routes import register_stats_routes

    register_stats_routes(
        app,
        stats_cache=_stats_cache,
        build_where=_build_where,
        db_read_sync=_db_read_sync,
        period_minutes=_period_minutes,
        period_rates=_period_rates,
        timestamp_format=_TS_FMT,
    )

    from dashboard.routers.debug_routes import register_debug_routes

    register_debug_routes(
        app,
        settings=config_settings,
        log_lines_getter=lambda: log_lines,
        save_env=lambda updates: save_env(updates),
        debug_log_path=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "logs",
            "debug.log",
        ),
        set_debug_log_file=lambda path: _set_debug_log_file(path),
        debug=lambda message: _debug(message),
    )

    # ── SSE events ──

    from dashboard.routers.event_routes import register_event_routes

    register_event_routes(
        app,
        event_manager_getter=get_event_manager,
        debug_logger=_debug,
    )

    # ── Quota endpoints ──

    from dashboard.routers.quota_routes import register_quota_routes

    register_quota_routes(
        app,
        snapshot_getter=get_quota_snapshot,
        cache_state_getter=quota_cache_state,
        max_age_s=SWR_MAX_AGE_S,
        stale_s=SWR_STALE_S,
    )

    from dashboard.routers.usage_routes import register_usage_routes

    register_usage_routes(app, db_read_sync=_db_read_sync)

    # ── VPN / IP rotation endpoints ──

    async def _ip_stats_db(vpn_manager=None) -> dict:
        """Per-IP usage stats for the IP History block (free egress IPs).

        Reads the requests table — free/paid split via the canonical tier set
        (``big-pickle`` is free without a ``-free`` suffix), identity
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
        free_ids = sorted(_free_model_id_set())
        if free_ids:
            free_clause = "model IN ({})".format(",".join("?" for _ in free_ids))
            grouped_params = tuple(free_ids) * 2
        else:
            free_clause = "0"
            grouped_params = ()
        stats: dict = {}
        try:

            def _query_grouped(db):
                return db.execute(
                    "SELECT free_model_ip, COUNT(*) AS total,"
                    f"       SUM(CASE WHEN {free_clause} THEN 1 ELSE 0 END),"
                    f"       SUM(CASE WHEN NOT ({free_clause}) THEN 1 ELSE 0 END),"
                    "       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),"
                    "       SUM(CASE WHEN success = 0 OR success IS NULL THEN 1 ELSE 0 END),"
                    "       MIN(timestamp), MAX(timestamp)"
                    " FROM requests"
                    " WHERE free_model_ip IS NOT NULL AND free_model_ip != ''"
                    " GROUP BY free_model_ip"
                    " ORDER BY COUNT(*) DESC, MAX(timestamp) DESC"
                    " LIMIT 100",
                    grouped_params,
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
        _stats_cache.set("ip_stats", stats, ttl=10)
        return stats

    def _fleet_controller():
        import shared_state
        from ops.fleet import FleetController

        return FleetController(
            list(getattr(shared_state, "vpn_managers", None) or []),
            getattr(shared_state, "free_ip_pool", None),
        )

    from dashboard.routers.station_logs_routes import register_station_logs_routes

    register_station_logs_routes(
        app,
        managers_getter=_dashboard_managers,
    )

    from dashboard.routers.vpn_status_routes import register_vpn_status_routes

    register_vpn_status_routes(
        app,
        settings=config_settings,
        stats_cache=_stats_cache,
        ip_stats_db_getter=lambda: _ip_stats_db,
        proxy_mode_getter=lambda: _vpn_proxy_mode(),
        socks5_payload=lambda pool: _socks5_payload(pool),
        socks5_auto_rotate_state=lambda: _socks5_auto_rotate_state(),
        config_yaml_mtime=lambda: _config_yaml_mtime(),
        config_yaml_known_mtime_getter=lambda: _config_yaml_known_mtime,
    )

    from dashboard.routers.free_routes import register_free_status_routes

    register_free_status_routes(app, token_checker=_check_dashboard_token)

    from dashboard.routers.pool_routes import register_pool_routes

    register_pool_routes(app, pool_getter=_dashboard_pool)

    from dashboard.routers.retry_routes import register_retry_routes

    register_retry_routes(
        app,
        config_getter=lambda: _retry_until_success_config(),
        config_persister=lambda updates: _persist_retry_until_success(updates),
        token_checker=lambda request: _check_dashboard_token(request),
    )

    from dashboard.routers.vpn_config_routes import (
        register_vpn_config_read_routes,
        shared_vpn_manager,
    )

    register_vpn_config_read_routes(
        app,
        manager_getter=shared_vpn_manager,
        managers_getter=_dashboard_managers,
        rotation_config_getter=lambda: getattr(config_settings, "IP_ROTATION", None) or {},
        free_policy_getter=config_settings.get_free_substitution_policy,
        quality_blocks=_QUALITY_BLOCK_SPEC,
    )

    from dashboard.routers.capacity_routes import register_capacity_routes

    register_capacity_routes(
        app,
        config_getter=lambda: getattr(config_settings, "IP_ROTATION", None) or {},
        capacity_counts_getter=lambda: _egress_capacity_counts(),
        observed_counts_getter=lambda: _egress_observed_counts(),
        endpoint_observed_getter=lambda: _egress_endpoint_observed(),
        endpoint_inventory_getter=lambda: _egress_endpoint_inventory(),
        token_checker=lambda request: _check_dashboard_token(request),
        persist_config=lambda updates: _persist_vpn_config(updates),
        audit=lambda name, payload: _egress_audit(name, payload),
    )

    from dashboard.routers.station_routes import register_station_routes

    register_station_routes(
        app,
        token_checker=_check_dashboard_token,
        pool_getter=_dashboard_pool,
        managers_getter=_dashboard_managers,
        config_getter=lambda: getattr(config_settings, "IP_ROTATION", None) or {},
        capacity_counts_getter=_egress_capacity_counts,
    )

    from dashboard.routers.vpn_config_update_routes import (
        register_vpn_config_update_routes,
    )

    register_vpn_config_update_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        settings=config_settings,
        write_credentials_env=lambda username, password: _write_credentials_env(
            username,
            password,
        ),
        persist_free_model_map=lambda mapping: _persist_free_model_map(mapping),
        quality_blocks_errors=lambda body: _quality_blocks_errors(body),
        persist_vpn_config=lambda updates: _persist_vpn_config(updates),
        egress_audit=lambda action, details: _egress_audit(action, details),
        debug=lambda message: _debug(message),
    )

    from dashboard.routers.vpn_config_routes import shared_vpn_manager
    from dashboard.routers.vpn_fleet_routes import register_vpn_fleet_routes

    register_vpn_fleet_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        managers_getter=_dashboard_managers,
        single_manager_getter=shared_vpn_manager,
        fleet_getter=_fleet_controller,
    )

    from dashboard.routers.vpn_health_routes import register_vpn_health_routes

    register_vpn_health_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        manager_getter=shared_vpn_manager,
    )

    from dashboard.routers.vpn_heal_routes import register_vpn_heal_routes

    register_vpn_heal_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        managers_getter=_dashboard_managers,
        station_count_getter=None,
        fleet_getter=_fleet_controller,
    )

    from dashboard.routers.vpn_next_routes import (
        register_vpn_next_routes,
        shared_pool,
    )

    register_vpn_next_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        managers_getter=_dashboard_managers,
        single_manager_getter=shared_vpn_manager,
        pool_getter=shared_pool,
    )

    from dashboard.routers.vpn_update_routes import register_vpn_update_routes

    register_vpn_update_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        manager_getter=shared_vpn_manager,
        managers_getter=_dashboard_managers,
    )

    from dashboard.routers.vpn_state_routes import register_vpn_state_routes

    register_vpn_state_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        manager_getter=shared_vpn_manager,
        credentials_path=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "credentials.env",
        ),
        write_credentials=lambda username, password: _write_credentials_env(
            username,
            password,
        ),
    )

    from dashboard.routers.station_control_routes import (
        register_station_control_routes,
    )

    register_station_control_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        managers_getter=_dashboard_managers,
        pool_getter=shared_pool,
        fleet_getter=_fleet_controller,
    )

    from dashboard.routers.vpn_jobs_routes import register_vpn_jobs_routes

    register_vpn_jobs_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        fleet_getter=_fleet_controller,
    )

    from dashboard.routers.cost_routes import register_cost_routes

    register_cost_routes(
        app,
        build_where=lambda *args, **kwargs: _build_where(*args, **kwargs),
        db_read_sync=lambda fn: _db_read_sync(fn),
        compute_costs=lambda rows, pricing: _compute_costs(rows, pricing),
        pricing_getter=_pricing_config,
    )

    from dashboard.routers.vpn_control_routes import (
        register_vpn_control_routes,
        shared_latency_engine,
    )

    register_vpn_control_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        validate_payload=lambda body: _validate_vpn_config_payload(body),
        engine_getter=shared_latency_engine,
        debug_logger=_debug,
    )

    from dashboard.routers.vpn_import_routes import register_vpn_import_routes

    register_vpn_import_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        managers_getter=_dashboard_managers,
        single_manager_getter=shared_vpn_manager,
        apply_import=lambda managers, body, **kwargs: _apply_import_payload(
            managers,
            body,
            **kwargs,
        ),
    )

    # ── [Axe 3.1] SOCKS5 backend / NordVPN / diagnostic endpoints ──
    # Contracts pinned from static/app.js (verified 19/08). All state-chan
    # gers carry the dashboard token guard; read-only GETs stay open (the
    # GUI polls them every 10 s). External I/O lives in the isolated helpers
    # above (hot-patchable offline — live docker/API never runs in tests).

    from dashboard.routers.proxy_backend_routes import (
        register_proxy_backend_routes,
    )

    register_proxy_backend_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        pool_getter=lambda: _dashboard_pool(),
        socks5_payload=lambda pool: _socks5_payload(pool),
        socks5_auto_rotate_state=lambda: _socks5_auto_rotate_state(),
        socks5_fallback_state=lambda: _socks5_fallback_state(),
        apply_socks5_rows=lambda rows, action="socks5_update": _apply_socks5_rows(
            rows,
            action=action,
        ),
        as_index=lambda value: _as_index(value),
        stored_proxy_credentials=(
            lambda pool, attribute, host, port: _stored_proxy_credentials(
                pool,
                attribute,
                host,
                port,
            )
        ),
        socks5_probe=lambda *args, **kwargs: _socks5_probe(*args, **kwargs),
        https_fallback_state=lambda: _https_fallback_state(),
        https_payload=lambda pool: _https_payload(pool),
        https_manual_rows=lambda pool: _https_manual_rows(pool),
        apply_https_rows=lambda rows: _apply_https_rows(rows),
        https_probe=lambda *args, **kwargs: _https_probe(*args, **kwargs),
        persist_vpn_config=lambda updates: _persist_vpn_config(updates),
    )

    from dashboard.routers.adaptive_routes import register_adaptive_routes

    register_adaptive_routes(
        app,
        pool_getter=lambda: _dashboard_pool(),
    )

    from dashboard.routers.egress_catalog_routes import register_egress_catalog_routes

    register_egress_catalog_routes(app, _dashboard_egress_catalog)

    from dashboard.qualification_launcher import launch_qualification_epoch
    from dashboard.routers.qualification_routes import register_qualification_routes
    from free.qualification.apply import apply_rotation

    def _qualifier_manager():
        try:
            import shared_state as _ss

            return getattr(_ss, "qualifier_manager", None)
        except Exception:
            return None

    _qualification_shadow = bool(
        config_settings.yaml_get(
            "egress_catalog",
            "qualification.shadow_mode",
            True,
        )
    )
    register_qualification_routes(
        app,
        _qualification_db_path,
        token_checker=_check_dashboard_token,
        qualifier_manager_getter=_qualifier_manager,
        start_epoch=lambda **kwargs: launch_qualification_epoch(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            db_path=_qualification_db_path(),
            **kwargs,
        ),
        rotate_apply=lambda **kwargs: apply_rotation(
            root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            db_path=_qualification_db_path(),
            config={
                "ip_rotation": config_settings.yaml_get(
                    "ip_rotation", default={}
                )
                or {},
                "ip_registry": config_settings.yaml_get(
                    "ip_registry", default={}
                )
                or {},
                "egress_catalog": config_settings.yaml_get(
                    "egress_catalog", default={}
                )
                or {},
            },
            **kwargs,
        ),
        default_shadow=_qualification_shadow,
        allow_live=not _qualification_shadow,
        rotation_headroom_pct=float(
            config_settings.yaml_get(
                "egress_catalog",
                "qualification.proactive_rotation_headroom_pct",
                20,
            )
            or 20
        ),
        last_station_protection=bool(
            config_settings.yaml_get(
                "egress_catalog",
                "qualification.last_station_protection",
                True,
            )
        ),
        station_standby_size=int(
            config_settings.yaml_get(
                "egress_catalog",
                "qualification.station_standby_size",
                1,
            )
            or 1
        ),
    )

    from dashboard.routers.egress_catalog_mutations import (
        register_egress_catalog_mutations,
    )

    register_egress_catalog_mutations(
        app,
        catalog_getter=_dashboard_egress_catalog,
        token_checker=_check_dashboard_token,
        denylist_getter=lambda: list(
            config_settings.yaml_get("egress_catalog", "denylist", []) or []
        ),
        denylist_setter=lambda value: config_settings.yaml_set(
            "egress_catalog", "denylist", value
        ),
    )

    from dashboard.routers.egress_mode_routes import register_egress_mode_routes

    register_egress_mode_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        pool_getter=lambda: _dashboard_pool(),
        managers_getter=lambda: _dashboard_managers(),
        settings=config_settings,
        persist_vpn_config=lambda updates: _persist_vpn_config(updates),
        egress_audit=lambda action, details: _egress_audit(action, details),
        debug=lambda message: _debug(message),
    )

    from dashboard.routers.nordvpn_routes import register_nordvpn_routes

    _dashboard_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    register_nordvpn_routes(
        app,
        managers_getter=_dashboard_managers,
        countries_getter=_nordvpn_countries,
        config_getter=lambda: getattr(config_settings, "IP_ROTATION", None) or {},
        credentials_path=os.path.join(_dashboard_root, "credentials.env"),
    )

    from dashboard.routers.vpn_diagnostic_routes import (
        register_vpn_diagnostic_routes,
    )

    register_vpn_diagnostic_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        managers_getter=lambda: _dashboard_managers(),
        proxy_mode_getter=lambda: _vpn_proxy_mode(),
        servers_by_country=lambda *args, **kwargs: _nordvpn_servers_by_country(
            *args,
            **kwargs,
        ),
        countries_getter=lambda *args, **kwargs: _nordvpn_countries(
            *args,
            **kwargs,
        ),
        settings=config_settings,
        persist_vpn_config=lambda updates: _persist_vpn_config(updates),
        docker_diag=lambda: _docker_diag(),
        compose_config=lambda: _docker_compose_config(),
        config_yaml_mtime=lambda: _config_yaml_mtime(),
        config_yaml_known_mtime_getter=lambda: _config_yaml_known_mtime,
        root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        no_window_flag=_CREATE_NO_WINDOW,
    )

    # ── Traffic capture (Wireshark-like raw request view) ──

    from dashboard.routers.traffic_routes import register_traffic_routes

    register_traffic_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        capture=_traffic_capture,
        mark_viewer=_traffic_mark_viewer,
        apply_lazy=_traffic_apply_lazy,
        user_enabled_getter=lambda: _traffic_user_enabled,
        user_enabled_setter=_set_traffic_user_enabled,
        debug=_debug,
    )

    from dashboard.routers.history_routes import register_history_routes

    register_history_routes(
        app,
        token_checker=lambda request: _check_dashboard_token(request),
        build_where=lambda *args, **kwargs: _build_where(*args, **kwargs),
        db_read_sync=lambda fn: _db_read_sync(fn),
        db_query_sync=lambda fn: _db_query_sync(fn),
        stats_cache=_stats_cache,
        free_model_id_set=lambda: _free_model_id_set(),
        filters_cache_getter=lambda: _filters_cache,
        filters_cache_setter=_set_filters_cache,
        tools_provider_getter=lambda: _tools_provider,
        connection_getter=lambda: conn,
        token_usage_getter=lambda: token_usage,
        token_lock_getter=lambda: token_lock,
        normalize_date_bound=(
            lambda value, end_of_day=False: _normalize_date_bound(
                value,
                end_of_day=end_of_day,
            )
        ),
        debug=lambda message: _debug(message),
    )
