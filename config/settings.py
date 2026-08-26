import json
import logging
import os
import random
import re
import secrets
import subprocess
import sys
import threading
import time
from functools import lru_cache as _lru_cache
from typing import Any

# Windows: masquer la fenêtre console des subprocess (évite le flash noir 1s)
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

try:
    from vpn_manager import (
        _normalize_country as _vpn_normalize_country,
    )  # single source (no duplication)
except ImportError:

    def _vpn_normalize_country(name: str) -> str:  # fallback before vpn_manager importable
        c = name.strip().replace("_", " ").strip().title()
        return c


"""
Contrat config — audit F-H4:
- YAML (config.yaml) = source primaire (upstream, routing, models, ip_rotation, geo, cache, etc.)
- ENV (.env) = secrets only (OPENCODE_API_KEY, OPENCODE_PROXY, DASHBOARD_TOKEN, OPENCODE_GO_*, PROXY)
- ENV_DIVERGENCE guard: warn when VPN_*/GEO_* env differs from .env file (compose children inherit process env, file not reloaded)
- Hot-reload: maybe_reload_custom_routes() poll 5s (vs inotify: cross-platform Windows/Linux, cheap mtime stat, no watchdog dep)
"""

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
ENV_PATH = os.path.join(ROOT, ".env")
CUSTOM_ROUTES_PATH = os.path.join(ROOT, "custom_routes.json")
API_KEYS_PATH = os.path.join(ROOT, "api_keys.json")

# Config keys safe to expose via API (not secrets)
CONFIG_KEYS = [
    "OPENCODE_PROXY",
    "OPENCODE_HOST",
    "OPENCODE_PORT",
    "OPUS_MAP_MODEL",
    "SONNET_MAP_MODEL",
    "HAIKU_MAP_MODEL",
    "DISABLE_MAPPING",
]


# ── YAML Config Loader ───────────────────────────────────────────────
# Hot-reload coalesces config.yaml + custom_routes.json under single _reload_lock

_yaml_data: dict[str, Any] = {}
_config_yaml_mtime: float = 0.0  # set after initial load


def load_yaml_config() -> dict:
    """Load config.yaml as the primary configuration source."""
    global _yaml_data, _config_yaml_mtime
    try:
        import yaml
    except ImportError:
        logger.warning("[config] pyyaml not installed — falling back to .env only")
        _yaml_data = {}
        _config_yaml_mtime = 0.0
        return {}

    if not os.path.exists(CONFIG_PATH):
        logger.info("[config] config.yaml not found at %s — using .env defaults", CONFIG_PATH)
        _yaml_data = {}
        _config_yaml_mtime = 0.0
        return {}

    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            _yaml_data = yaml.safe_load(f) or {}
        try:
            _config_yaml_mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            _config_yaml_mtime = 0.0
        logger.info("[config] loaded config.yaml (%d top-level keys)", len(_yaml_data))
        # F-H6: generate control_api_key once if empty (boot single-flight, portalocker if available)
        try:
            _ir = _yaml_data.get("ip_rotation", {})
            if isinstance(_ir, dict) and not str(_ir.get("control_api_key") or "").strip():
                _new_key = secrets.token_urlsafe(32)
                _ir["control_api_key"] = _new_key
                _yaml_data["ip_rotation"] = _ir
                # Try file-lock for gunicorn multi-worker (portalocker optional)
                _lock_acquired = False
                _lock_file = None
                try:
                    import portalocker  # type: ignore

                    _lock_file = open(CONFIG_PATH + ".lock", "a+", encoding="utf-8")
                    portalocker.lock(_lock_file, portalocker.LOCK_EX)
                    _lock_acquired = True
                    # Re-read under lock: another worker may have just generated it
                    try:
                        with open(CONFIG_PATH, encoding="utf-8") as _rf:
                            _reloaded = yaml.safe_load(_rf) or {}
                        _re_ir = (
                            _reloaded.get("ip_rotation", {})
                            if isinstance(_reloaded.get("ip_rotation"), dict)
                            else {}
                        )
                        if str(_re_ir.get("control_api_key") or "").strip():
                            _yaml_data = _reloaded
                            _ir = _re_ir
                        else:
                            # Still empty — persist our generated key
                            raise FileNotFoundError  # fall through to save
                    except Exception:
                        # Save our generated key
                        tmp = CONFIG_PATH + ".tmp"
                        with open(tmp, "w", encoding="utf-8") as _wf:
                            yaml.dump(
                                _yaml_data,
                                _wf,
                                default_flow_style=False,
                                allow_unicode=True,
                                sort_keys=False,
                            )
                            _wf.flush()
                            try:
                                os.fsync(_wf.fileno())
                            except OSError:
                                pass
                        os.replace(tmp, CONFIG_PATH)
                        try:
                            _config_yaml_mtime = os.path.getmtime(CONFIG_PATH)
                        except OSError:
                            pass
                        logger.info("[config] generated control_api_key (persisted to config.yaml)")
                        # warm-avalanche: resync immédiat credentials.env (évite window divergent)
                        try:
                            from scripts.make_credentials_env import _sync_control_api_key as _scak

                            _scak()
                        except Exception:
                            try:
                                _creds = os.path.join(ROOT, "credentials.env")
                                _up = _new_key
                                # inline upsert fallback
                                _lines = []
                                _found = False
                                if os.path.exists(_creds):
                                    with open(_creds, encoding="utf-8") as _rf:
                                        for _ln in _rf:
                                            if _ln.strip().startswith("VPN_CONTROL_API_KEY="):
                                                _lines.append(f"VPN_CONTROL_API_KEY={_up}\n")
                                                _found = True
                                            else:
                                                _lines.append(_ln)
                                if not _found:
                                    if _lines and not _lines[-1].endswith("\n"):
                                        _lines[-1] += "\n"
                                    _lines.append(f"VPN_CONTROL_API_KEY={_up}\n")
                                with open(_creds, "w", encoding="utf-8") as _wf2:
                                    _wf2.writelines(_lines)
                            except Exception:
                                pass
                except ImportError:
                    # No portalocker — atomic save without lock (single-worker safe)
                    tmp = CONFIG_PATH + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as _wf:
                        yaml.dump(
                            _yaml_data,
                            _wf,
                            default_flow_style=False,
                            allow_unicode=True,
                            sort_keys=False,
                        )
                        _wf.flush()
                        try:
                            os.fsync(_wf.fileno())
                        except OSError:
                            pass
                    os.replace(tmp, CONFIG_PATH)
                    try:
                        _config_yaml_mtime = os.path.getmtime(CONFIG_PATH)
                    except OSError:
                        pass
                    logger.info("[config] generated control_api_key (persisted, no lock)")
                    try:
                        from scripts.make_credentials_env import _sync_control_api_key as _scak2

                        _scak2()
                    except Exception:
                        pass
                except Exception as _e:
                    logger.warning("[config] control_api_key generation race: %s", _e)
                finally:
                    if _lock_acquired and _lock_file is not None:
                        try:
                            import portalocker

                            portalocker.unlock(_lock_file)
                            _lock_file.close()
                            try:
                                os.remove(CONFIG_PATH + ".lock")
                            except OSError:
                                pass
                        except Exception:
                            pass
        except Exception as _e:
            logger.debug("[config] control_api_key ensure skipped: %s", _e)
        return _yaml_data
    except Exception as e:
        logger.error("[config] failed to load config.yaml: %s", e)
        _yaml_data = {}
        return {}


def save_yaml_config():
    """Write current config back to config.yaml (atomic tmp+fsync+replace)."""
    global _yaml_data
    try:
        import yaml
    except ImportError:
        return
    # [plan v10 §14.0.2 v10 — anti-écrasement] Un process démarré AVANT
    # l'ajout de nouvelles sections ré-écrit le fichier sans elles (incident
    # Lot D : dashboard_trust/client_auth/supervisor effacés 2× par un proxy
    # live à l'état mémoire ancien). Fusion conservatrice : toute section
    # protégée présente sur disque mais absente de la mémoire est RESTAURÉE.
    _PROTECTED_SECTIONS = ("dashboard_trust", "client_auth", "supervisor")
    _PROTECTED_NESTED = {"ip_rotation": ("latency_rotation",)}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as _f_disk:
            _on_disk = yaml.safe_load(_f_disk) or {}
        if not isinstance(_on_disk, dict):
            _on_disk = {}
        for _sec in _PROTECTED_SECTIONS:
            if _sec not in _yaml_data and isinstance(_on_disk.get(_sec), dict):
                globals()["_yaml_data"][_sec] = _on_disk[_sec]
        for _parent, _subs in _PROTECTED_NESTED.items():
            _disk_parent = _on_disk.get(_parent)
            if not isinstance(_disk_parent, dict):
                continue
            mem_parent = _yaml_data.setdefault(_parent, {})
            if isinstance(mem_parent, dict):
                for _sub in _subs:
                    if _sub not in mem_parent and _sub in _disk_parent:
                        mem_parent[_sub] = _disk_parent[_sub]
    except Exception:
        pass  # jamais bloquer la sauvegarde pour une fusion manquée
    # _reload_lock may not yet be defined at import time; resolve at call time
    lock = globals().get("_reload_lock")
    # Use lock if available, else write without lock (boot path)
    try:
        if lock is not None:
            lock.acquire()
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(_yaml_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, CONFIG_PATH)
        try:
            globals()["_config_yaml_mtime"] = os.path.getmtime(CONFIG_PATH)
        except OSError:
            pass
        # [fix dirty-banner] keep dashboard's mtime in sync for background saves
        # (free-discovery, geo) — otherwise dashboard/config_yaml_dirty stays true
        # after every auto-persist even though file was written by the proxy itself.
        try:
            import dashboard.api as _dash  # may not be imported yet at boot

            if hasattr(_dash, "_config_yaml_known_mtime"):
                try:
                    _dash._config_yaml_known_mtime = os.path.getmtime(CONFIG_PATH)
                except OSError:
                    pass
        except Exception:
            pass
        logger.debug("[config] saved config.yaml (atomic)")
    finally:
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass


def yaml_get(section: str, key: str = None, default=None):
    """Read a value from the YAML config. Returns default if missing."""
    section_data = _yaml_data.get(section, {})
    if key is None:
        return section_data if isinstance(section_data, dict) else (section_data or default)
    if isinstance(section_data, dict):
        return section_data.get(key, default)
    return default


def yaml_set(section: str, key: str, value):
    """Write a value to the YAML config and persist."""
    if section not in _yaml_data or not isinstance(_yaml_data.get(section), dict):
        _yaml_data[section] = {}
    _yaml_data[section][key] = value
    save_yaml_config()


# ── .env Loader (override YAML defaults) ─────────────────────────────

# [plan 18/08 §2.1] Divergence env périmée: rempli par load_env_file()
# quand une clé VPN_* présente dans os.environ (héritée d'un parent ou
# posée avant le boot) diffère de la valeur du fichier .env. Ce sont les
# clés que load_env_file refuse de recharger — l'env du process gagne sur
# le fichier pour chaque enfant `docker compose` (cause racine 19/08).
# Exposé par /api/vpn-status → env_divergence (dashboard bannière). Côté
# déterministe, la correction est dans vpn_manager._compose_env().
ENV_DIVERGENCE: list = []


def load_env_file():
    """Load environment variables from .env file if it exists."""
    if not os.path.exists(ENV_PATH):
        return
    count = 0
    divergence = []
    loaded = set()
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if key not in os.environ:
                    os.environ[key] = value
                    count += 1
                    loaded.add(key)
                elif (
                    (key.startswith("VPN_") or key.startswith("GEO_"))
                    and value != os.environ[key]
                    and key not in loaded
                ):
                    # Stale parent env: the .env value was already there at
                    # boot but the process env is ahead of it — compose
                    # children inherit that env, so the file is NOT what the
                    # fleet runs (19/08 incident). Keys loaded by a PREVIOUS
                    # load_env_file() call are aligned by definition — never
                    # flagged (a second call after a _apply_stack runtime
                    # rewrite of VPN_TYPE_STATION* is not a divergence).
                    divergence.append((key, value, os.environ[key]))
    if divergence:
        for key, file_val, env_val in divergence:
            logger.warning(
                "[config] env divergence: %s=%r in .env but %r in process env — "
                "compose children inherit the process env, which WINS over the "
                "file (19/08 root cause); restart the proxy or re-push the "
                "config to re-sync",
                key,
                file_val,
                env_val,
            )
        ENV_DIVERGENCE[:] = divergence
    logger.debug("[config] load_env_file: loaded %d new vars from .env", count)


def _env(key: str, default=None):
    """Read env var, falling back to default."""
    val = os.getenv(key)
    if val is not None:
        return val
    return default


def _env_bool(key: str, default=False):
    val = os.getenv(key)
    if val is not None:
        return val.lower() in ("1", "true", "yes")
    return default


def _env_int(key: str, default=0):
    val = os.getenv(key)
    if val is not None:
        try:
            return int(val)
        except ValueError:
            logging.warning("Invalid integer for %s=%r, using default %d", key, val, default)
    return default


# ── Initialize config ────────────────────────────────────────────────

load_env_file()
load_yaml_config()

# ── Secrets (YAML → .env override) ──────────────────────────────────
PROXY = _env("OPENCODE_PROXY", yaml_get("upstream", "proxy", ""))
API_KEY = _env("OPENCODE_API_KEY", "")
OPENCODE_GO_WORKSPACE_ID = _env("OPENCODE_GO_WORKSPACE_ID", "")
OPENCODE_GO_AUTH_COOKIE = _env("OPENCODE_GO_AUTH_COOKIE", "")
OPENCODE_GO_USE_BALANCE = _env_bool("OPENCODE_GO_USE_BALANCE", True)
API_KEY_ROUTING = _env("API_KEY_ROUTING", yaml_get("routing", "key_routing", "round-robin"))
CACHE_MIN_PROMPT_SIZE = _env_int(
    "CACHE_MIN_PROMPT_SIZE", yaml_get("cache", "min_prompt_size", 2000)
)
DEBUG = _env_bool("OPENCODE_DEBUG", yaml_get("server", "debug", False))

# ── Upstream endpoints ──────────────────────────────────────────────
API_BASE_OPENAI = yaml_get(
    "upstream", "openai_base", "https://opencode.ai/zen/go/v1/chat/completions"
)
API_BASE_ANTHROPIC = yaml_get(
    "upstream", "anthropic_base", "https://opencode.ai/zen/go/v1/messages"
)
API_BASE_FREE = yaml_get("upstream", "free_base", "https://opencode.ai/zen/v1/chat/completions")

# ── Free model mapping (paid → free equivalent) ────────────────────
FREE_MODEL_MAP = yaml_get("free_model_map", default={})

# ── IP rotation (OpenVPN for free model quota) ────────────────────
IP_ROTATION = yaml_get("ip_rotation", default={})

# ── Geo (P1 — single source config.yaml:geo, kill-switch enabled:false) ─
# Source: https://ai.developer.meta.com/legal/geographic-use-policy
# Snapshot 2026-08-20 JS-rendered — WebFetch returned empty skeleton, manual
# transcription required. Single source (no geo_policies.json) — policies
# live in config.yaml:geo.policies, routes reference via geo: {extends: name}.
GEO_ENABLED: bool = bool(yaml_get("geo", "enabled", False))
GEO_VERSION: int = int(yaml_get("geo", "version", 1) or 1)
GEO_POLICIES: dict = (
    yaml_get("geo", "policies", {}) if isinstance(yaml_get("geo", "policies", {}), dict) else {}
)
SORTED_GEO_POLICIES: list = sorted(GEO_POLICIES.items()) if isinstance(GEO_POLICIES, dict) else []
GEO_ALLOW_DIRECT_WHEN_COMPATIBLE: bool = bool(yaml_get("geo", "allow_direct_when_compatible", True))

# [P1 perf] mémo resolve_geo — epoch bumpé à chaque hot-reload touchant les
# entrées geo/routes ; clé complète dans resolve_geo (contenu + inputs).
_geo_resolve_cache: dict = {}
_GEO_RESOLVE_CACHE_MAX = 1024
_geo_cache_epoch = 0


def _bump_geo_cache() -> None:
    global _geo_cache_epoch
    _geo_cache_epoch += 1
    _geo_resolve_cache.clear()


def _server_countries_set() -> set:
    """Normalized set(server_countries) via single-source _vpn_normalize_country."""
    raw = IP_ROTATION.get("server_countries", "") if isinstance(IP_ROTATION, dict) else ""
    if isinstance(raw, list):
        parts = [str(p).strip() for p in raw if str(p).strip()]
    elif isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        parts = []
    return {_vpn_normalize_country(p) for p in parts if p}


def _resolve_geo_extends(raw_geo: dict) -> dict:
    """Resolve geo.extends: shallow copy of GEO_POLICIES[name] merged with overrides (route wins)."""
    if not isinstance(raw_geo, dict):
        return {}
    extends = raw_geo.get("extends")
    if not extends:
        return dict(raw_geo)
    if not isinstance(extends, str):
        logger.warning("[geo] extends must be str, got %r — dropping extends", extends)
        d = dict(raw_geo)
        d.pop("extends", None)
        return d
    base = GEO_POLICIES.get(extends)
    if not isinstance(base, dict):
        logger.warning("[geo] extends=%r not found in geo.policies — dropping extends", extends)
        d = dict(raw_geo)
        d.pop("extends", None)
        return d
    merged = dict(base)
    for k, v in raw_geo.items():
        if k == "extends":
            continue
        merged[k] = v
    return merged


def _normalize_geo_list(countries, server_set: set) -> tuple[set, list]:
    """Normalize list via _vpn_normalize_country, drop invalid (WARN), dedup. Returns (valid_set, dropped)."""
    valid: set = set()
    dropped: list = []
    if not countries:
        return valid, dropped
    if not isinstance(countries, (list, tuple)):
        logger.warning("[geo] countries must be list, got %r — dropping", type(countries).__name__)
        return valid, dropped
    for c in countries:
        if not isinstance(c, str) or not c.strip():
            dropped.append(c)
            continue
        norm = _vpn_normalize_country(c)
        if norm not in server_set:
            logger.warning(
                "[geo] country %r → %r not in server_countries — dropping (intersection check)",
                c,
                norm,
            )
            dropped.append(c)
            continue
        valid.add(norm)
    return valid, dropped


def resolve_geo(route: dict) -> dict:
    """Resolve effective geo for a route.

    Returns {effective_allowed: set, mode: str, require_vpn: bool, geo_status: str}
    where geo_status is ok|misconfigured|disabled.
    Validation after normalization against set(server_countries normalized).
    Precedence blocked > allowed: effective = (allowed - blocked) ∩ server_countries
    else server_countries - blocked if only blocked. Empty effective + strict => misconfigured.
    """
    if not isinstance(route, dict):
        return {
            "effective_allowed": set(),
            "mode": "strict",
            "require_vpn": False,
            "geo_status": "disabled" if not GEO_ENABLED else "ok",
        }
    raw_geo = route.get("geo")
    if not raw_geo or not isinstance(raw_geo, dict):
        return {
            "effective_allowed": set(),
            "mode": "strict",
            "require_vpn": False,
            "geo_status": "disabled" if not GEO_ENABLED else "ok",
        }
    if not GEO_ENABLED:
        # Kill-switch: passthrough but still report disabled status (P1 no enforcement)
        return {
            "effective_allowed": set(),
            "mode": str(raw_geo.get("mode", "strict")),
            "require_vpn": bool(raw_geo.get("require_vpn", False)),
            "geo_status": "disabled",
        }
    # [P1 perf] mémo résolution : la clé couvre TOUTES les entrées qui
    # influencent le résultat (contenu geo, flag global, server_countries,
    # identité GEO_POLICIES + epoch bumpé au reload) — un monkeypatch/test ou
    # un hot-reload produit donc forcément une clé différente.
    server_set = _server_countries_set()
    cache_key = (
        _geo_cache_epoch,
        repr(raw_geo),
        tuple(sorted(server_set)),
        id(GEO_POLICIES),
    )
    cached = _geo_resolve_cache.get(cache_key)
    if cached is not None:
        return {
            "effective_allowed": set(cached[0]),
            "mode": cached[1],
            "require_vpn": cached[2],
            "geo_status": cached[3],
        }
    geo = _resolve_geo_extends(raw_geo)
    mode = str(geo.get("mode", "strict")).lower()
    if mode not in ("strict", "prefer", "warn"):
        logger.warning("[geo] invalid mode %r — fallback to strict", mode)
        mode = "strict"
    require_vpn = bool(geo.get("require_vpn", False))
    allowed_raw = geo.get("allowed_countries", None)
    blocked_raw = geo.get("blocked_countries", None)
    # Normalize (invalid WARN+drop)
    allowed_set: set = set()
    blocked_set: set = set()
    if allowed_raw is not None:
        allowed_set, _ = _normalize_geo_list(allowed_raw, server_set)
    if blocked_raw is not None:
        blocked_set, _ = _normalize_geo_list(blocked_raw, server_set)
    # DRY note: blocked > allowed (dedup)
    if allowed_set and blocked_set:
        overlap = allowed_set & blocked_set
        if overlap:
            logger.warning("[geo] blocked > allowed overlap %r — blocked wins", sorted(overlap))
        allowed_set = allowed_set - blocked_set
    has_allowed = allowed_raw is not None
    has_blocked = blocked_raw is not None

    def _cached(effective: set, m: str, rv: bool, status: str) -> dict:
        if len(_geo_resolve_cache) >= _GEO_RESOLVE_CACHE_MAX:
            _geo_resolve_cache.clear()
        _geo_resolve_cache[cache_key] = (frozenset(effective), m, rv, status)
        return {
            "effective_allowed": set(effective),
            "mode": m,
            "require_vpn": rv,
            "geo_status": status,
        }

    if not has_allowed and not has_blocked:
        return _cached(set(server_set) if server_set else set(), mode, require_vpn, "ok")
    if allowed_set and blocked_set:
        effective = (allowed_set - blocked_set) & server_set
    elif allowed_set:
        effective = allowed_set & server_set
    elif blocked_set:
        effective = server_set - blocked_set
    else:
        # Both normalized empty after WARN drops
        if has_allowed:
            # allowed declared but nothing valid => empty effective => misconfigured in strict
            effective = set()
        else:
            # only blocked declared but all invalid => nothing to block
            return _cached(
                set(server_set) if server_set else set(), mode, require_vpn, "ok"
            )
    geo_status = "misconfigured" if (not effective and mode == "strict") else "ok"
    return _cached(effective, mode, require_vpn, geo_status)


def geo_strict_union() -> set:
    """Union des effective_allowed de toutes les policies strict+require_vpn (Axe B).

    Vide si GEO désactivé ou aucune policy strict. Consulté par vpn_manager
    pour filtrer les rotations géo-restricted.
    """
    if not GEO_ENABLED:
        return set()
    union: set = set()
    for _name, _pol in GEO_POLICIES.items() if isinstance(GEO_POLICIES, dict) else []:
        # La policy brute peut ne pas avoir blocked/allowed — on passe par
        # un faux route {geo: {extends: name}} pour réutiliser resolve_geo
        # (normalisation + intersection server_countries).
        try:
            _info = resolve_geo({"geo": {"extends": _name}})
        except Exception:
            continue
        if (
            _info.get("mode") == "strict"
            and _info.get("require_vpn")
            and _info.get("geo_status") != "misconfigured"
        ):
            eff = _info.get("effective_allowed")
            if isinstance(eff, set):
                union |= eff
    return union


def resolved_station_count(cfg: dict) -> int:
    """Resolve the number of parallel VPN stations (1-10).

    Canonical key: ``station_count``. Retro-compat: absent →
    ``dual_station: true`` ⇒ 2, else 1. Clamped to [1, 10] — the
    NordVPN account limit is 10 simultaneous connections.
    """
    try:
        n = int(cfg.get("station_count", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    if n:
        return max(1, min(10, n))
    return 2 if cfg.get("dual_station", False) else 1


# [plan v2 auto-sync] auto_max_free_attempts defaults to True (derived
# effective_max = clamp(N,1,3)). Legacy config.yaml missing the key: WARN
# when the stored value diverges from the derived one so the operator knows
# to set ``auto_max_free_attempts=false`` to keep a deliberate manual value.
# Placed AFTER resolved_station_count so the helper is available. Idempotent
# on reload — the same guard runs in maybe_reload_custom_routes.
def _ensure_auto_max_free_attempts_warn(cfg: dict, source: str = "boot") -> None:
    if not isinstance(cfg, dict) or "auto_max_free_attempts" in cfg:
        return
    try:
        stored = int(cfg.get("max_free_attempts", 2) or 2)
    except (TypeError, ValueError):
        stored = 2
    stored = max(1, min(stored, 5))
    derived = max(1, min(int(resolved_station_count(cfg) or 1), 5))
    if stored != derived:
        logger.warning(
            "[config] manual max_free_attempts=%s differs from derived=%s "
            "(station_count=%s) — enabling auto_max_free_attempts=true; "
            "set auto_max_free_attempts=false to keep manual",
            stored,
            derived,
            resolved_station_count(cfg),
        )
    cfg["auto_max_free_attempts"] = True
    # keep the in-yaml mirror consistent so a later save_yaml doesn't drop it
    try:
        sec = _yaml_data.get("ip_rotation")
        if isinstance(sec, dict) and "auto_max_free_attempts" not in sec:
            sec["auto_max_free_attempts"] = True
    except Exception:
        pass


_ensure_auto_max_free_attempts_warn(IP_ROTATION, source="boot")

# ── Free parallel (stations free) — two routings découplés ─────────
# (B) Stations free: enabled bool, routing round-robin|failover,
#     mode load-balance|hedge, hedge_delay_ms 0-2000, hedge_max_attempts 1-3
# Defaults conservateurs OFF (pas de parallélisation sans action GUI).
# P1 melodic-pearl: hedge 300→150ms / 1→2 pour N=10 (cap burst 3)
_FREE_PARALLEL_DEFAULTS = {
    "enabled": False,
    "routing": "round-robin",
    "mode": "load-balance",
    "hedge_delay_ms": 150,
    "hedge_max_attempts": 2,
}


def _normalize_free_parallel(raw) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    try:
        enabled = bool(raw.get("enabled", _FREE_PARALLEL_DEFAULTS["enabled"]))
    except Exception:
        enabled = False
    routing = str(raw.get("routing", _FREE_PARALLEL_DEFAULTS["routing"]) or "round-robin").lower()
    if routing not in ("round-robin", "failover"):
        logger.warning("[config] free_parallel.routing invalid %r — fallback to round-robin", routing)
        routing = "round-robin"
    mode = str(raw.get("mode", _FREE_PARALLEL_DEFAULTS["mode"]) or "load-balance").lower()
    if mode not in ("load-balance", "strict", "hedge"):
        logger.warning("[config] free_parallel.mode invalid %r — fallback to load-balance", mode)
        mode = "load-balance"
    try:
        delay = int(raw.get("hedge_delay_ms", _FREE_PARALLEL_DEFAULTS["hedge_delay_ms"]))
    except Exception:
        delay = 300
    delay = max(0, min(2000, delay))
    try:
        max_att = int(raw.get("hedge_max_attempts", _FREE_PARALLEL_DEFAULTS["hedge_max_attempts"]))
    except Exception:
        max_att = 1
    max_att = max(1, min(3, max_att))
    return {
        "enabled": enabled,
        "routing": routing,
        "mode": mode,
        "hedge_delay_ms": delay,
        "hedge_max_attempts": max_att,
    }


FREE_PARALLEL: dict = _normalize_free_parallel(IP_ROTATION.get("free_parallel", {}))
# back-fill IP_ROTATION mirror so save_yaml keeps it
try:
    if "free_parallel" not in IP_ROTATION or not isinstance(IP_ROTATION.get("free_parallel"), dict):
        IP_ROTATION["free_parallel"] = dict(FREE_PARALLEL)
    else:
        # ensure normalized values are persisted back
        IP_ROTATION["free_parallel"] = dict(FREE_PARALLEL)
    # keep _yaml_data mirror
    sec = _yaml_data.get("ip_rotation")
    if isinstance(sec, dict):
        sec["free_parallel"] = dict(FREE_PARALLEL)
except Exception:
    pass


def get_free_parallel() -> dict:
    return FREE_PARALLEL


def free_parallel_enabled() -> bool:
    return bool(FREE_PARALLEL.get("enabled", False))

# ── Server ──────────────────────────────────────────────────────────
HOST = _env("OPENCODE_HOST", yaml_get("server", "host", "0.0.0.0"))
PORT = _env_int("OPENCODE_PORT", yaml_get("server", "port", 4000))
# [P6] WEB_PORT supprimé : rien n'a jamais écouté sur :8082 — le dashboard
# est servi par le port principal (PORT).

# ── Model family prefix → protocol mapping ─────────────────────────
# Used by _fetch_upstream_models() to assign the correct protocol
# to auto-discovered models from the upstream API.
# When opencode.ai adds a new model family, add its prefix here.
KNOWN_PROTOCOLS = {
    # OpenAI protocol models
    "glm": "openai",
    "kimi": "openai",
    "deepseek": "openai",
    "mimo": "openai",
    "hy": "openai",
    "nemotron": "openai",
    "muse": "openai",
    "spark": "openai",
    "big": "openai",
    "laguna": "openai",
    "north": "openai",
    # Anthropic protocol models
    "minimax": "anthropic",
    "qwen": "anthropic",
}


def _resolve_protocol(model_id: str) -> str:
    """Resolve the protocol for a model ID using KNOWN_PROTOCOLS.

    Extracts the family prefix (first token before '-' or '.', then strips
    trailing digits) and looks it up in KNOWN_PROTOCOLS.
    Falls back to "openai" if unknown.

    Examples:
        "kimi-k2.7"    -> "kimi"  -> "openai"
        "qwen3.7-plus" -> "qwen"  -> "anthropic"
        "glm-5.2"      -> "glm"   -> "openai"
    """
    import re

    prefix = model_id.split("-")[0].split(".")[0].lower()
    prefix = re.sub(r"\d+$", "", prefix)  # "qwen3" -> "qwen"
    return KNOWN_PROTOCOLS.get(prefix, "openai")


# ── Models ──────────────────────────────────────────────────────────
_models_cfg = yaml_get("models", default={})
MODELS = {}
# muse-* and spark-* models use /v1/responses (Responses API), not /chat/completions
# Paid: https://opencode.ai/zen/go/v1/responses, Free: https://opencode.ai/zen/v1/responses
_RESPONSES_ENDPOINT = "https://opencode.ai/zen/go/v1/responses"
_RESPONSES_FREE_ENDPOINT = "https://opencode.ai/zen/v1/responses"


def _resolve_model_endpoint(model_id: str, model_data: dict, protocol: str) -> str:
    """Resolve the upstream endpoint for a configured model.

    An explicit per-model `endpoint` key wins over the -free / muse-spark
    heuristics: 'go' → Go chat completions (authenticated subscription),
    'free' → free chat completions, any other value is used verbatim as a
    full URL. Example: ox-alpha-free lives on the Go endpoint despite its
    -free suffix (its free-tier twin is x-preview-f-free).
    """
    explicit = model_data.get("endpoint")
    if explicit:
        value = str(explicit).strip()
        lowered = value.lower()
        if lowered == "go":
            return API_BASE_OPENAI
        if lowered == "free":
            return API_BASE_FREE
        return value
    lid = model_id.lower()
    if lid.endswith("-free"):
        if "muse" in lid or "spark" in lid:
            return _RESPONSES_FREE_ENDPOINT
        return API_BASE_FREE
    if "muse" in lid or "spark" in lid:
        return _RESPONSES_ENDPOINT
    return API_BASE_OPENAI if protocol == "openai" else API_BASE_ANTHROPIC


for _model_id, _model_data in _models_cfg.items():
    if isinstance(_model_data, dict):
        _proto = _model_data.get("protocol", "openai")
        _endpoint = _resolve_model_endpoint(_model_id, _model_data, _proto)
        MODELS[_model_id] = {"endpoint": _endpoint, "protocol": _proto}


def _fetch_upstream_models(timeout: float = 3.0):
    """Fetch available models from upstream API and add them to MODELS.

    Timeout réduit à 3s (vs 10s avant) pour ne pas bloquer le démarrage.
    Appelé en arrière-plan, jamais bloquant pour le 1er chargement GUI.
    """
    try:
        url = f"{API_BASE_OPENAI.rsplit('/chat/completions', 1)[0]}/models"
        # --max-time 3s + connect 2s : échec rapide si upstream lent
        result = subprocess.run(
            ["curl", "-s", "--max-time", str(int(timeout)), "--connect-timeout", "2", url],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
            creationflags=_CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            raise Exception(f"curl failed: {result.stderr[:200]}")
        data = json.loads(result.stdout)
        models = data.get("data", [])
        added = 0
        for m in models:
            model_id = m.get("id", "")
            if model_id and model_id not in MODELS:
                proto = _resolve_protocol(model_id)
                endpoint = API_BASE_OPENAI if proto == "openai" else API_BASE_ANTHROPIC
                MODELS[model_id] = {"endpoint": endpoint, "protocol": proto}
                added += 1
        if added:
            # [P4 correctesse] les modèles découverts doivent être visibles
            # immédiatement : sans clear du LRU, get_model_config sert encore
            # les défauts OpenAI stale pour un id absent au moment du 1er appel.
            try:
                get_model_config.cache_clear()
            except Exception:
                pass
        logger.info("[config] upstream models: fetched %d, added %d new", len(models), added)
        return added
    except Exception as e:
        logger.debug("[config] upstream models fetch failed (non-bloquant): %s", e)
        return 0


# Lancement non-bloquant en arrière-plan (daemon thread) — ne retarde pas l'import
def _fetch_upstream_models_background():
    try:
        time.sleep(0.5)
        _fetch_upstream_models(timeout=3.0)
    except Exception:
        pass


try:
    _bg_thread = threading.Thread(
        target=_fetch_upstream_models_background, daemon=True, name="upstream-models-fetch"
    )
    _bg_thread.start()
    logger.debug("[config] upstream fetch lancé en arrière-plan (3s timeout)")
except Exception as e:
    logger.debug("[config] impossible de lancer le thread upstream: %s", e)

# ── Web search native allowlist (v3.3) ─────────────────────────
WEB_SEARCH_NATIVE_MODELS: list = yaml_get("web_search_native", default=["muse-spark-1.2-contributor", "muse-spark-1.2-contributor-free"])
if not isinstance(WEB_SEARCH_NATIVE_MODELS, list):
    WEB_SEARCH_NATIVE_MODELS = ["muse-spark-1.2-contributor", "muse-spark-1.2-contributor-free"]

# ── Free discovery (auto-detect -free models) ─────────────────────
FREE_DISCOVERY = (
    yaml_get("free_discovery", default={})
    if isinstance(yaml_get("free_discovery", default={}), dict)
    else {}
)
FREE_DISCOVERY_INTERVAL = int(
    FREE_DISCOVERY.get("interval", yaml_get("background", "free_models_refresh_interval", 3600))
    or 3600
)
FREE_DISCOVERY_ENABLED = bool(FREE_DISCOVERY.get("enabled", True))
FREE_DISCOVERY_AUTO_PERSIST = bool(FREE_DISCOVERY.get("auto_persist", True))
FREE_DISCOVERY_DEFAULT_TARGET = FREE_DISCOVERY.get("default_target", "mimo-v2.5-free")
# -free ids served only via the authenticated Go endpoint (e.g. ox-alpha-free):
# never added to the anonymous free pool / FREE_MODELS by auto-discovery.
GO_ONLY_IDS: set = set()
_goi_raw = FREE_DISCOVERY.get("go_only_ids", [])
if isinstance(_goi_raw, list):
    GO_ONLY_IDS.update(str(x).strip().lower() for x in _goi_raw if str(x).strip())

# Seed FREE_MODELS from existing free_model_map values (known frees at boot)
FREE_MODELS: set = {v for v in FREE_MODEL_MAP.values() if isinstance(v, str) and v}
FREE_MODEL_POOL: list = sorted(FREE_MODELS)
# Observability state (exposed via GET /api/free-models)
_FREE_DISCOVERY_STATE: dict[str, Any] = {
    "last_refresh": None,
    "next_refresh": None,
    "source": "none",
    "consecutive_failures": 0,
    "removed": [],
    "detected": sorted(FREE_MODELS),
}
_FREE_DISCOVERY_URLS_CACHE = None


def _free_discovery_urls() -> list:
    """Union of free discovery URLs (derived from bases, dedup)."""
    global _FREE_DISCOVERY_URLS_CACHE
    override = yaml_get("upstream", "free_models_url", "")
    if isinstance(override, str) and override.strip():
        return [override.strip().rstrip("/")]
    urls = []
    for base in (API_BASE_FREE, API_BASE_OPENAI):
        if not base:
            continue
        b = base.strip()
        if "/chat/completions" in b:
            b = b.rsplit("/chat/completions", 1)[0]
        u = b.rstrip("/") + "/models"
        if u not in urls:
            urls.append(u)
    return urls


def _is_free_model(m: dict) -> bool:
    """Cascade: pricing/is_free/free/capabilities.free → suffix -free."""
    if not isinstance(m, dict):
        return False
    mid = m.get("id", "")
    if not isinstance(mid, str) or not mid:
        return False
    pricing = m.get("pricing")
    if isinstance(pricing, dict):
        try:
            inp = pricing.get("input", None)
            out = pricing.get("output", None)
            if inp is not None and out is not None and float(inp) == 0 and float(out) == 0:
                return True
        except Exception:
            pass
    for k in ("is_free", "free"):
        if m.get(k) is True:
            return True
    caps = m.get("capabilities")
    if isinstance(caps, dict) and caps.get("free") is True:
        return True
    if mid.endswith("-free"):
        return True
    return False


def _detect_free_ids(payloads: list) -> set:
    """Extract free ids from a list of /models payloads (cascade)."""
    free = set()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        data = payload.get("data")
        if not isinstance(data, list):
            continue
        for m in data:
            if _is_free_model(m):
                mid = m.get("id", "")
                if isinstance(mid, str) and mid:
                    free.add(mid)
    return free


def _fetch_free_models_sync(timeout: float = 10) -> tuple:
    """Fetch union of discovery URLs via httpx (3 retries exp 1.5 on 5xx/timeout only, 429 respects Retry-After).

    Returns (free_ids: set[str], source: str, payloads: list[dict]).
    Fail-soft: raises only if ALL urls failed; caller logs warning.
    """
    urls = _free_discovery_urls()
    payloads: list[Any] = []
    source_parts = []
    last_err: Exception | None = None
    for url in urls:
        success = False
        for attempt in range(3):
            try:
                try:
                    import httpx as _httpx
                except ImportError:
                    # Fallback to curl subprocess (Windows may lack curl but try)
                    import json as _json

                    r = subprocess.run(
                        ["curl", "-s", "--max-time", str(int(timeout)), url],
                        capture_output=True,
                        text=True,
                        timeout=timeout + 5,
                        creationflags=_CREATE_NO_WINDOW,
                    )
                    if r.returncode != 0:
                        raise RuntimeError(f"curl failed: {r.stderr[:200]}") from None
                    data = _json.loads(r.stdout)
                    payloads.append(data)
                    source_parts.append(url)
                    success = True
                    last_err = None
                    break
                # httpx path
                _kwargs: dict[str, Any] = {"timeout": timeout}
                if PROXY:
                    _kwargs["proxy"] = PROXY
                with _httpx.Client(**_kwargs) as _client:
                    _resp = _client.get(url)
                if _resp.status_code == 429:
                    _ra = _resp.headers.get("Retry-After", "")
                    try:
                        _delay = int(str(_ra).strip())
                    except Exception:
                        _delay = 60
                    logger.warning("[free-discovery] 429 from %s Retry-After=%s", url, _delay)
                    # Do not retry blindly on 429 — respect Retry-After
                    last_err = RuntimeError(f"429 Retry-After {_delay} from {url}")
                    break
                if 500 <= _resp.status_code < 600:
                    raise RuntimeError(f"5xx {_resp.status_code} from {url}")
                _resp.raise_for_status()
                data = _resp.json()
                payloads.append(data)
                source_parts.append(url)
                success = True
                last_err = None
                break
            except Exception as e:
                last_err = e
                msg = str(e)
                is_retryable = (
                    "5xx" in msg
                    or "timeout" in msg.lower()
                    or "timed out" in msg.lower()
                    or "connect" in msg.lower()
                    or "ConnectTimeout" in msg
                    or "ReadTimeout" in msg
                )
                if not is_retryable or attempt == 2:
                    if not success:
                        logger.debug(
                            "[free-discovery] fetch failed %s attempt %d: %s", url, attempt + 1, e
                        )
                    break
                delay = (1.5**attempt) + random.uniform(-0.1, 0.1)
                # jitter ±10% already via random; clamp min 0
                if delay < 0:
                    delay = 0
                time.sleep(delay)
        # next url
    if not payloads:
        if last_err is not None:
            raise last_err
        return set(), "none", []
    free_ids = _detect_free_ids(payloads)
    # HTML filet only if cascade found nothing
    if not free_ids:
        try:
            docs_url = "https://opencode.ai/docs/fr/zen/"
            try:
                import httpx as _httpx2

                _kwargs2: dict[str, Any] = {"timeout": timeout}
                if PROXY:
                    _kwargs2["proxy"] = PROXY
                with _httpx2.Client(**_kwargs2) as _c2:
                    _r2 = _c2.get(docs_url)
                    if _r2.status_code == 200:
                        _html = _r2.text
                        _ids = set(
                            re.findall(r"(?i)<td[^>]*>\s*([a-z0-9.\-]+-free)\s*</td>", _html)
                        )
                        if _ids:
                            free_ids = _ids
                            source_parts.append("docs:html")
            except ImportError:
                pass
        except Exception as e:
            logger.debug("[free-discovery] html filet failed: %s", e)
    source = "|".join(source_parts) if source_parts else "none"
    return free_ids, source, payloads


def _free_endpoint_for(free_id: str) -> str:
    """Return the correct free endpoint for a model.

    muse-* and spark-* models use the /v1/responses endpoint (Responses API),
    while other models use the standard /v1/chat/completions endpoint.
    """
    lid = free_id.lower()
    if "muse" in lid or "spark" in lid:
        return "https://opencode.ai/zen/v1/responses"
    return API_BASE_FREE


def _apply_discovered_free_models(free_ids: set, source: str = "none") -> int:
    """Apply discovered free_ids to MODELS/FREE_MODEL_MAP/FREE_MODEL_POOL.

    Delta-check: if set == FREE_MODELS → no-op (0, no mtime bump).
    Otherwise mutates MODELS (add missing free_ids with endpoint/protocol),
    FREE_MODELS/FREE_MODEL_POOL in-place, and FREE_MODEL_MAP add-only
    (paid → paid-free homonyme). Returns number of new MODELS added.
    Thread-safe via _reload_lock if available.
    """
    global FREE_MODEL_POOL
    if not isinstance(free_ids, set):
        free_ids = set(free_ids)
    if GO_ONLY_IDS:
        _go_only_hits = {f for f in free_ids if str(f).lower() in GO_ONLY_IDS}
        if _go_only_hits:
            logger.info(
                "[free-discovery] go-only ids excluded from anonymous pool: %s",
                ", ".join(sorted(_go_only_hits)),
            )
            free_ids -= _go_only_hits
    if free_ids == FREE_MODELS:
        for _fid in sorted(free_ids):
            if "muse" in _fid.lower() or "spark" in _fid.lower():
                _exp = _free_endpoint_for(_fid)
                _cur = MODELS.get(_fid, {}).get("endpoint", "")
                if _cur and _cur != _exp:
                    MODELS[_fid]["endpoint"] = _exp
                    logger.info("[free-discovery] corrected endpoint %s → %s", _fid, _exp)
        logger.debug(
            "[free-discovery] no delta (still %d free ids) source=%s", len(free_ids), source
        )
        _FREE_DISCOVERY_STATE["detected"] = sorted(free_ids)
        _FREE_DISCOVERY_STATE["source"] = source
        return 0
    removed = sorted(FREE_MODELS - free_ids) if FREE_MODELS else []
    if removed:
        logger.info(
            "[free-discovery] upstream removed %s — keeping local, manual cleanup needed",
            ", ".join(removed),
        )
        _FREE_DISCOVERY_STATE["removed"] = removed
    else:
        _FREE_DISCOVERY_STATE["removed"] = []
    lock = globals().get("_reload_lock")
    added = 0
    try:
        if lock is not None:
            lock.acquire()
        for fid in sorted(free_ids):
            expected = _free_endpoint_for(fid)
            if fid not in MODELS:
                proto = _resolve_protocol(fid)
                MODELS[fid] = {"endpoint": expected, "protocol": proto}
                added += 1
                prefix = fid.split("-")[0].split(".")[0].lower()
                prefix_clean = re.sub(r"\d+$", "", prefix)
                if prefix_clean not in KNOWN_PROTOCOLS:
                    logger.warning(
                        "[free-discovery] unknown family %s for %s → openai", prefix_clean, fid
                    )
            else:
                cur = MODELS[fid].get("endpoint", "")
                if cur != expected and ("muse" in fid.lower() or "spark" in fid.lower()):
                    MODELS[fid]["endpoint"] = expected
                    logger.info("[free-discovery] corrected endpoint %s → %s", fid, expected)
        # Update FREE_MODELS in-place (keep object identity for importers that hold ref)
        FREE_MODELS.clear()
        FREE_MODELS.update(free_ids)
        FREE_MODEL_POOL = sorted(free_ids)
        # Keep state
        _FREE_DISCOVERY_STATE["detected"] = sorted(free_ids)
        _FREE_DISCOVERY_STATE["source"] = source
        # FREE_MODEL_MAP add-only: paid → paid-free homonyme if exists
        # Iterate over a snapshot of MODELS keys (paid candidates = not free themselves)
        for paid in list(MODELS.keys()):
            if paid in free_ids:
                continue
            homonyme = f"{paid}-free"
            if homonyme in free_ids and paid not in FREE_MODEL_MAP:
                FREE_MODEL_MAP[paid] = homonyme
                logger.info("[free-discovery] mapped %s → %s (homonyme)", paid, homonyme)
        # default_target validation
        dt = FREE_DISCOVERY_DEFAULT_TARGET
        if dt and dt not in free_ids and free_ids:
            fallback = FREE_MODEL_POOL[0] if FREE_MODEL_POOL else dt
            logger.warning(
                "[free-discovery] default_target %r not in FREE_MODELS — fallback %r", dt, fallback
            )
        logger.info(
            "[free-discovery] fetched %d free ids, added %d new MODELS, source=%s",
            len(free_ids),
            added,
            source,
        )
        try:
            get_model_config.cache_clear()
        except Exception:
            pass
    finally:
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass
    return added


def _persist_free_mappings():
    """Merge add-only free mappings into config.yaml (atomic tmp+fsync+replace under lock)."""
    if not FREE_DISCOVERY_AUTO_PERSIST:
        return
    try:
        # Ensure sections exist
        if "free_model_map" not in _yaml_data or not isinstance(
            _yaml_data.get("free_model_map"), dict
        ):
            _yaml_data["free_model_map"] = {}
        if "models" not in _yaml_data or not isinstance(_yaml_data.get("models"), dict):
            _yaml_data["models"] = {}
        # Merge FREE_MODEL_MAP add-only
        for k, v in FREE_MODEL_MAP.items():
            if k not in _yaml_data["free_model_map"]:
                _yaml_data["free_model_map"][k] = v
        # Merge MODELS add-only (only free ids or newly discovered)
        for mid, cfg in MODELS.items():
            if mid not in _yaml_data["models"]:
                # Persist minimal protocol hint
                proto = cfg.get("protocol", "openai")
                _yaml_data["models"][mid] = {"protocol": proto}
        save_yaml_config()
        logger.debug("[free-discovery] persisted %d free mappings", len(FREE_MODEL_MAP))
    except Exception as e:
        logger.warning("[free-discovery] persist failed: %s", e)


def _ensure_free_models_sync() -> int:
    """Synchronous ensure (fetch → apply → persist). Returns added count. Fail-soft."""
    if not FREE_DISCOVERY_ENABLED:
        return 0
    try:
        free_ids, source, _payloads = _fetch_free_models_sync(timeout=10)
        if not free_ids:
            logger.warning("[free-discovery] no free ids detected source=%s", source)
            _FREE_DISCOVERY_STATE["source"] = source
            return 0
        added = _apply_discovered_free_models(free_ids, source=source)
        if added or free_ids != set(_yaml_data.get("free_model_map", {}).values()):
            _persist_free_mappings()
        # Reset consecutive failures on success
        _FREE_DISCOVERY_STATE["consecutive_failures"] = 0
        import datetime as _dt

        now_iso = _dt.datetime.now(_dt.UTC).isoformat()
        _FREE_DISCOVERY_STATE["last_refresh"] = now_iso
        # next_refresh computed by caller (interval + jitter)
        return added
    except Exception as e:
        _FREE_DISCOVERY_STATE["consecutive_failures"] = (
            _FREE_DISCOVERY_STATE.get("consecutive_failures", 0) + 1
        )
        logger.warning(
            "[free-discovery] ensure failed (%d consecutive): %s",
            _FREE_DISCOVERY_STATE["consecutive_failures"],
            e,
        )
        return 0


def _ensure_free_models_async():
    try:
        import threading as _th

        _th.Thread(target=_ensure_free_models_sync, daemon=True).start()
    except Exception:
        pass


# [P4 race boot] _reload_lock défini AVANT le spawn des threads discovery
# (plus bas) : l'ancienne définition en ligne ~1541 laissait une fenêtre où
# le thread free-discovery appelait save_yaml_config/_bump_route_version
# avec globals().get("_reload_lock") = None → écritures hors verrou en
# course avec le reload principal.
_reload_lock = threading.Lock()

try:
    if FREE_DISCOVERY_ENABLED:
        _ensure_free_models_async()
except Exception:
    pass

# ── Routing ─────────────────────────────────────────────────────────
DISABLE_MAPPING = _env_bool("DISABLE_MAPPING", yaml_get("routing", "disable_mapping", False))
ALIASES = yaml_get("routing", "aliases", {"nimo": "mimo"})


def load_routes():
    """Load ROUTES from YAML config or use default.

    Custom routes take priority over auto-generated routes: if a custom route
    matches the same model name as an auto-generated route, the auto-generated
    one is removed. This ensures custom mappings (e.g. mimo-v2.5 → glm-5.2)
    are not shadowed by the default identity mapping.
    """
    routes = {}
    if not DISABLE_MAPPING:
        alias_reverse = {}
        for alias, canonical in ALIASES.items():
            alias_reverse.setdefault(canonical, []).append(alias)

        for model_id in MODELS:
            key = model_id.replace("-", "").replace(".", "").replace("_", "")
            match_keywords = [model_id]
            prefix = model_id.split("-")[0]
            for alias in alias_reverse.get(prefix, []):
                match_keywords.append(model_id.replace(prefix, alias, 1))
            routes[key] = {"match": match_keywords, "model": model_id}

    # Collect all match patterns from custom routes
    custom_match_patterns = set()
    for value in CUSTOM_ROUTES.values():
        if isinstance(value, dict):
            for m in value.get("match", []):
                custom_match_patterns.add(m.lower())

    # Remove auto-generated routes whose match patterns overlap with custom routes
    if custom_match_patterns:
        keys_to_remove = []
        for key, route in routes.items():
            if isinstance(route, dict):
                for m in route.get("match", []):
                    if m.lower() in custom_match_patterns:
                        keys_to_remove.append(key)
                        break
        for key in keys_to_remove:
            del routes[key]

    # Custom user-defined routes (added AFTER cleanup so they always survive)
    for key, value in CUSTOM_ROUTES.items():
        routes[key] = value

    # Model route overrides
    routes["opus"] = {
        "match": ["opus"],
        "model": _env("OPUS_MAP_MODEL", yaml_get("routing", "opus_model", "kimi-k2.6")),
    }
    routes["sonnet"] = {
        "match": ["sonnet"],
        "model": _env("SONNET_MAP_MODEL", yaml_get("routing", "sonnet_model", "glm-5.1")),
    }
    routes["haiku"] = {
        "match": ["haiku"],
        "model": _env("HAIKU_MAP_MODEL", yaml_get("routing", "haiku_model", "minimax-m2.5")),
    }
    logger.debug("[config] load_routes: %d routes loaded", len(routes))
    return routes


# ── Custom Routes ───────────────────────────────────────────────────


def load_custom_routes() -> dict:
    """Load custom routes from YAML or JSON file."""
    # Try YAML first
    yaml_routes = yaml_get("custom_routes", default={})
    if yaml_routes:
        return yaml_routes
    # Fallback to JSON file
    if os.path.exists(CUSTOM_ROUTES_PATH):
        try:
            with open(CUSTOM_ROUTES_PATH, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logging.warning("Failed to load custom_routes.json: %s", e)
    return {}


def validate_custom_routes(routes) -> str | None:
    """[plan v10 §14.2.5 Lot 6] None si le payload est valide, sinon un
    message d'erreur exploitable par l'endpoint (→ 400)."""
    if not isinstance(routes, dict):
        return "custom_routes doit être un objet {modèle: règles}"
    for model, rules in routes.items():
        if not isinstance(model, str) or not model.strip():
            return f"clé modèle invalide: {model!r}"
        if not isinstance(rules, dict):
            return f"{model}: les règles doivent être un objet"
        for k, v in rules.items():
            if not isinstance(k, str):
                return f"{model}: clé de règle non-string {k!r}"
            if not (
                isinstance(v, (str, int, float, bool))
                or (isinstance(v, list) and all(isinstance(x, str) for x in v))
            ):
                return f"{model}.{k}: type non supporté ({type(v).__name__})"
    return None


def save_custom_routes(routes: dict):
    """Save custom routes to the YAML config and reload ROUTES.

    [plan v10 §14.4.11] source UNIQUE : l'écriture parallèle du
    custom_routes.json legacy est SUPPRIMÉE (deux sources qui divergeaient à
    chaque save). Les anciens fichiers JSON restent LUS par
    load_custom_routes en fallback (installs existants). Lève ValueError sur
    payload invalide — l'endpoint le mappe en 400 (§14.2.5)."""
    err = validate_custom_routes(routes)
    if err:
        raise ValueError(err)
    global SORTED_ROUTES, SORTED_CUSTOM_ROUTES
    # Remplacement TOTAL (et non .update) : l'unique appelant (dashboard
    # /api/config/custom-routes) poste l'état complet reconstruit par le GUI.
    # Un merge ferait ressusciter les routes supprimées côté GUI.
    _yaml_data["custom_routes"] = dict(routes)
    with _reload_lock:
        CUSTOM_ROUTES.clear()
        CUSTOM_ROUTES.update(routes)
        ROUTES.clear()
        ROUTES.update(load_routes())
        SORTED_ROUTES = _sort_routes_by_match(ROUTES)
        SORTED_CUSTOM_ROUTES = _sort_routes_by_match(CUSTOM_ROUTES)
        _bump_route_version_unlocked()
    save_yaml_config()


# ── API Keys ────────────────────────────────────────────────────────


def load_api_keys() -> list[dict]:
    """Load API key configs from api_keys.json (gitignored, primary). Falls back to YAML, then .env single-key."""
    if os.path.exists(API_KEYS_PATH):
        try:
            with open(API_KEYS_PATH, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    for i, k in enumerate(data):
                        if not k.get("alias"):
                            k["alias"] = f"Compte {i + 1}"
                    return data
        except Exception as e:
            logging.warning("Failed to load api_keys.json: %s", e)
    yaml_keys = yaml_get("api_keys", default=[])
    if yaml_keys:
        for i, k in enumerate(yaml_keys):
            if not k.get("alias"):
                k["alias"] = f"Compte {i + 1}"
        return yaml_keys
    # Fallback: single key from .env
    if API_KEY:
        return [
            {
                "api_key": API_KEY,
                "go_workspace_id": OPENCODE_GO_WORKSPACE_ID,
                "go_auth_cookie": OPENCODE_GO_AUTH_COOKIE,
            }
        ]
    return []


def save_api_keys(configs: list[dict]):
    """Save API key configs to api_keys.json (never config.yaml — secrets stay out of git)."""
    with open(API_KEYS_PATH, "w", encoding="utf-8") as f:
        json.dump(configs, f, indent=2, ensure_ascii=False)
    API_KEYS[:] = configs  # Atomic replacement — readers never see empty list


# ── Module-level state ──────────────────────────────────────────────

CUSTOM_ROUTES = load_custom_routes()
ROUTES = load_routes()
API_KEYS = load_api_keys()


# ── Hot-reload: Custom Routes ───────────────────────────────────────


def _get_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


_custom_routes_mtime = _get_mtime(CUSTOM_ROUTES_PATH)
_custom_routes_last_check = 0.0
_CUSTOM_ROUTES_CHECK_INTERVAL = yaml_get("background", "custom_routes_check_interval", 5)
# _reload_lock est défini PLUS HAUT (avant le spawn des threads discovery) —
# voir [P4 race boot].


def _sort_routes_by_match(routes: dict) -> list:
    """Return routes sorted by longest match pattern first (most specific)."""
    return sorted(
        routes.values(),
        key=lambda r: max((len(m) for m in r.get("match", [])), default=0),
        reverse=True,
    )


# Pre-sorted route lists (rebuilt on load/reload)
SORTED_ROUTES = _sort_routes_by_match(ROUTES)
SORTED_CUSTOM_ROUTES = _sort_routes_by_match(CUSTOM_ROUTES)

# Monotonic route-generation counter: bumped on every mutation of
# ROUTES/CUSTOM_ROUTES/SORTED_* (save_custom_routes, maybe_reload, save_env).
# opencode._route_for compares it to its last-seen value to invalidate the
# O(1) _route_cache — one int compare per request.
ROUTE_VERSION: int = 0


def _bump_route_version_unlocked():
    """Variante SANS verrou — à n'appeler QUE en tenant déjà _reload_lock."""
    global ROUTE_VERSION
    ROUTE_VERSION += 1
    try:
        get_model_config.cache_clear()
    except AttributeError:
        pass


def _bump_route_version():
    """Invalider tous les caches de routage après une mutation des routes.

    Point d'invalidation UNIQUE : tout site qui reconstruit ROUTES/CUSTOM_ROUTES
    doit appeler ce helper (ou la variante unlocked s'il tient déjà le verrou).
    Lock non réentrant : ne JAMAIS appeler save_yaml_config() depuis un with
    _reload_lock.
    """
    with _reload_lock:
        _bump_route_version_unlocked()


def maybe_reload_custom_routes():
    """Re-read config.yaml + custom_routes.json if modified. Rate-limited, thread-safe.

    Single source: config.yaml holds geo policies; single poller + single
    _reload_lock. Atomic order: yaml_data -> IP_ROTATION -> geo -> routes -> SORTED_*.
    Poll 5s vs inotify: mtime check is O(1), cross-platform (Windows + Linux),
    no extra dep (watchdog), negligible cost vs per-request stat; 5s coalesces bursts.
    """
    global _custom_routes_mtime, _custom_routes_last_check, _config_yaml_mtime
    global \
        SORTED_ROUTES, \
        SORTED_CUSTOM_ROUTES, \
        SORTED_GEO_POLICIES, \
        GEO_ENABLED, \
        GEO_VERSION, \
        GEO_ALLOW_DIRECT_WHEN_COMPATIBLE, \
        FREE_PARALLEL, \
        WEB_SEARCH_NATIVE_MODELS
    now = time.time()
    if now - _custom_routes_last_check < _CUSTOM_ROUTES_CHECK_INTERVAL:
        return
    _custom_routes_last_check = now
    try:
        cfg_mtime = _get_mtime(CONFIG_PATH)
        cr_mtime = _get_mtime(CUSTOM_ROUTES_PATH)
        if cfg_mtime == _config_yaml_mtime and cr_mtime == _custom_routes_mtime:
            return
        cfg_changed = cfg_mtime != _config_yaml_mtime
        cr_changed = cr_mtime != _custom_routes_mtime
        # Pre-load new custom routes outside lock (I/O)
        new_cr = None
        if cr_changed or cfg_changed:
            # load_custom_routes reads _yaml_data; if cfg changed we reload yaml first under lock,
            # so defer new_cr load until after yaml reload. For now, placeholder.
            if not cfg_changed:
                new_cr = load_custom_routes()
        with _reload_lock:
            # ── yaml_data -> IP_ROTATION -> GEO (atomic) ──
            if cfg_changed:
                try:
                    import yaml as _yaml

                    with open(CONFIG_PATH, encoding="utf-8") as f:
                        new_yaml = _yaml.safe_load(f) or {}
                except Exception as e:
                    logging.warning("[config] reload config.yaml failed: %s", e)
                    new_yaml = None
                if new_yaml is not None:
                    _yaml_data.clear()
                    _yaml_data.update(new_yaml)
                    _config_yaml_mtime = cfg_mtime
                    # IP_ROTATION in-place (keep object identity)
                    new_ip = (
                        new_yaml.get("ip_rotation", {})
                        if isinstance(new_yaml.get("ip_rotation"), dict)
                        else {}
                    )
                    # Fix P2: snapshot old server_countries AVANT clear/update
                    # sinon _old_sc lu en 1715 vaut déjà new_ip → regen .env jamais déclenché
                    _old_sc_snapshot = IP_ROTATION.get("server_countries", "")
                    IP_ROTATION.clear()
                    if isinstance(new_ip, dict):
                        IP_ROTATION.update(new_ip)
                    # [plan v2 auto-sync] ensure auto flag + WARN on divergence (idempotent)
                    try:
                        _ensure_auto_max_free_attempts_warn(IP_ROTATION, source="reload")
                    except Exception:
                        pass
                    # free_parallel in-place (hot-reload without restart)
                    try:
                        _norm = _normalize_free_parallel(IP_ROTATION.get("free_parallel", {}))
                        FREE_PARALLEL.clear()
                        FREE_PARALLEL.update(_norm)
                        # keep IP_ROTATION mirror normalized
                        IP_ROTATION["free_parallel"] = dict(_norm)
                    except Exception:
                        pass
                    # GEO in-place
                    geo_sec = (
                        new_yaml.get("geo", {}) if isinstance(new_yaml.get("geo"), dict) else {}
                    )
                    GEO_ENABLED = bool(geo_sec.get("enabled", False))
                    try:
                        GEO_VERSION = int(geo_sec.get("version", 1) or 1)
                    except Exception:
                        GEO_VERSION = 1
                    new_policies = (
                        geo_sec.get("policies", {})
                        if isinstance(geo_sec.get("policies"), dict)
                        else {}
                    )
                    GEO_POLICIES.clear()
                    if isinstance(new_policies, dict):
                        GEO_POLICIES.update(new_policies)
                    SORTED_GEO_POLICIES[:] = sorted(GEO_POLICIES.items())
                    _bump_geo_cache()
                    GEO_ALLOW_DIRECT_WHEN_COMPATIBLE = bool(
                        geo_sec.get("allow_direct_when_compatible", True)
                    )
                    # v3.3: WEB_SEARCH_NATIVE_MODELS hot-reload
                    try:
                        new_wsn = new_yaml.get("web_search_native", ["muse-spark-1.2-contributor", "muse-spark-1.2-contributor-free"])
                        if isinstance(new_wsn, list) and new_wsn:
                            WEB_SEARCH_NATIVE_MODELS[:] = new_wsn
                    except Exception:
                        pass
                    # go_only_ids hot-reload (in-place set — discovery filter stays live)
                    try:
                        _fd_sec = (
                            new_yaml.get("free_discovery", {})
                            if isinstance(new_yaml.get("free_discovery"), dict)
                            else {}
                        )
                        _new_goi = _fd_sec.get("go_only_ids", [])
                        if isinstance(_new_goi, list):
                            GO_ONLY_IDS.clear()
                            GO_ONLY_IDS.update(
                                str(x).strip().lower() for x in _new_goi if str(x).strip()
                            )
                    except Exception:
                        pass
                    # P2: server_countries change → regen .env via make_credentials_env
                    try:
                        _old_sc = _old_sc_snapshot  # snapshot pris avant IP_ROTATION.clear()
                        _new_sc = new_ip.get("server_countries", "")
                        if _old_sc != _new_sc and _new_sc:
                            # [P4 perf] HORS loop : peut_reload tourne sur
                            # l'event loop (via _route_for) — un subprocess
                            # jusqu'à 10 s ici gelait TOUTES les streams.
                            # Thread daemon fire-and-forget à la place.
                            import subprocess as _sp2
                            import threading as _th2

                            def _regen_env() -> None:
                                try:
                                    _sp2.run(
                                        [
                                            __import__("sys").executable,
                                            os.path.join(
                                                ROOT, "scripts", "make_credentials_env.py"
                                            ),
                                        ],
                                        capture_output=True,
                                        timeout=10,
                                        creationflags=_CREATE_NO_WINDOW,
                                    )
                                    logging.info(
                                        "[config] server_countries changed → .env regen"
                                    )
                                except Exception as _e2:
                                    logging.warning("[config] .env regen failed: %s", _e2)

                            _th2.Thread(
                                target=_regen_env, daemon=True, name="env-regen"
                            ).start()
                    except Exception:
                        pass
                    logging.info(
                        "[config] reloaded config.yaml geo.enabled=%s version=%s policies=%d allow_direct=%s",
                        GEO_ENABLED,
                        GEO_VERSION,
                        len(GEO_POLICIES),
                        GEO_ALLOW_DIRECT_WHEN_COMPATIBLE,
                    )
                # custom_routes may live in yaml: need to reload after yaml swap
                new_cr = load_custom_routes()
                cr_changed = True  # force route rebuild after yaml change
            if new_cr is None:
                new_cr = load_custom_routes()
            if cr_changed or cfg_changed:
                # Only bump mtime after successful load
                _custom_routes_mtime = cr_mtime
                old_cr_keys = set(CUSTOM_ROUTES.keys())
                new_cr_keys = set(new_cr.keys())
                for k in (new_cr_keys - old_cr_keys) | (new_cr_keys & old_cr_keys):
                    CUSTOM_ROUTES[k] = new_cr[k]
                for k in old_cr_keys - new_cr_keys:
                    del CUSTOM_ROUTES[k]

                new_routes = load_routes()
                old_r_keys = set(ROUTES.keys())
                new_r_keys = set(new_routes.keys())
                for k in (new_r_keys - old_r_keys) | (new_r_keys & old_r_keys):
                    ROUTES[k] = new_routes[k]
                for k in old_r_keys - new_r_keys:
                    del ROUTES[k]

                SORTED_ROUTES = _sort_routes_by_match(ROUTES)
                SORTED_CUSTOM_ROUTES = _sort_routes_by_match(CUSTOM_ROUTES)
                # Invalidation centralisée : on tient déjà le verrou → variante
                # unlocked (Lock non réentrant).
                _bump_route_version_unlocked()
                _bump_geo_cache()
                logging.info(
                    "Reloaded routes (%d routes, cfg_changed=%s)", len(ROUTES), cfg_changed
                )
    except Exception as e:
        logging.warning("Failed to reload config: %s", e)


@_lru_cache(maxsize=512)
def get_model_config(model_id: str) -> dict:
    """Return merged config for model_id with sensible defaults (LRU cached)."""
    cfg = MODELS.get(model_id, {})
    defaults = {"endpoint": API_BASE_OPENAI, "protocol": "openai"}
    return {**defaults, **cfg}


# ── Runtime updates (called by dashboard API) ───────────────────────


def save_env(updates: dict):
    """Update .env file and apply values at runtime."""
    existing = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    existing[key.strip()] = value.strip()

    existing.update(updates)
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        for key, value in existing.items():
            f.write(f"{key}={value}\n")

    for key, value in updates.items():
        os.environ[key] = value
        if key == "OPENCODE_PROXY":
            global PROXY
            PROXY = value
        elif key == "OPENCODE_API_KEY":
            global API_KEY
            API_KEY = value
        elif key == "OPENCODE_GO_WORKSPACE_ID":
            global OPENCODE_GO_WORKSPACE_ID
            OPENCODE_GO_WORKSPACE_ID = value
        elif key == "OPENCODE_GO_AUTH_COOKIE":
            global OPENCODE_GO_AUTH_COOKIE
            OPENCODE_GO_AUTH_COOKIE = value
        elif key == "API_KEY_ROUTING":
            global API_KEY_ROUTING
            API_KEY_ROUTING = value
        elif key == "DISABLE_MAPPING":
            global DISABLE_MAPPING
            DISABLE_MAPPING = value.lower() in ("1", "true", "yes")
        elif key == "OPENCODE_HOST":
            global HOST
            HOST = value
        elif key == "OPENCODE_DEBUG":
            global DEBUG
            DEBUG = value.lower() in ("1", "true", "yes")
        elif key == "OPENCODE_GO_USE_BALANCE":
            global OPENCODE_GO_USE_BALANCE
            OPENCODE_GO_USE_BALANCE = value.lower() in ("1", "true", "yes")

    global ROUTES, SORTED_ROUTES
    ROUTES = load_routes()
    SORTED_ROUTES = _sort_routes_by_match(ROUTES)
    _bump_route_version()  # pas de verrou tenu ici → variante lockée
    _bump_geo_cache()
    logger.debug("[config] save_env: applied %d vars", len(updates))


def apply_server_changes(port=None, host=None):
    """Update HOST, PORT at runtime."""
    global HOST, PORT
    if host is not None:
        HOST = host
    if port is not None:
        PORT = int(port)
