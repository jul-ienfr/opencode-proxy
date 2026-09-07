"""config.loader — résolution de config SANS état (Phase 1 refonte).

Déplacé depuis ``config/settings.py`` (déplacement pur) : lecteurs
``os.environ`` (``_env*``), normaliseurs (``normalize_429_action``,
``_normalize_free_parallel``), résolveurs (``resolved_station_count``,
``_ensure_auto_max_free_attempts_warn`` — ``yaml_data`` passé en paramètre).

AUCUN import du projet. ``logger`` = ``"config.settings"`` (nom historique
préservé — les enregistrements de ces fonctions gardaient ce nom).

Périmètre assumé (documenté) : le STORE (``_yaml_data``,
``_config_yaml_mtime``) et ses accesseurs (``load_yaml_config``,
``save_yaml_config``, ``yaml_get/set``, ``load_env_file``) RESTENT dans
``config/settings.py`` — les tests et le runtime rebindent/mutent
``settings._yaml_data`` / ``settings.CONFIG_PATH`` directement
(``test_free_discovery.py``, ``test_hot_reload_phase4.py``,
``opencode.py``, ``dashboard/api.py``) : les déplacer romprait le contrat.
De même, les side-effects d'import (``load_env_file()``,
``load_yaml_config()``, thread ``upstream-models-fetch``) restent
orchestrés par ``settings.py`` au même point jusqu'à la Phase 9
(``app/composition.py`` + lifespan explicite) — ce module n'en a AUCUN.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("config.settings")

_VALID_429_ACTIONS = ("cooldown", "rotate", "both")


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


def normalize_429_action(raw, default: str = "both") -> str:
    """[PLAN-corrections-429 P12/D3] Shared normalizer for on_429_action values.

    Returns one of "cooldown" | "rotate" | "both"; anything else falls back
    to `default`. Used by opencode.py, free_ip_pool.py and vpn_manager.py so
    the str()/strip()/lower() validation lives in exactly one place.
    """
    action = str(raw if raw else default).strip().lower()
    return action if action in _VALID_429_ACTIONS else default


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


def _ensure_auto_max_free_attempts_warn(cfg: dict, yaml_data: dict, source: str = "boot") -> None:
    """Warn + back-fill auto_max_free_attempts (dérivé du station_count).

    [plan v2 auto-sync] auto_max_free_attempts defaults to True (derived
    effective_max = clamp(N,1,3)). Legacy config.yaml missing the key: WARN
    when the stored value diverges from the derived one so the operator knows
    to set ``auto_max_free_attempts=false`` to keep a deliberate manual value.
    Idempotent on reload.
    """
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
        sec = (yaml_data or {}).get("ip_rotation")
        if isinstance(sec, dict) and "auto_max_free_attempts" not in sec:
            sec["auto_max_free_attempts"] = True
    except Exception:
        pass


__all__ = [
    "_FREE_PARALLEL_DEFAULTS",
    "_VALID_429_ACTIONS",
    "_ensure_auto_max_free_attempts_warn",
    "_env",
    "_env_bool",
    "_env_int",
    "_normalize_free_parallel",
    "normalize_429_action",
    "resolved_station_count",
]
