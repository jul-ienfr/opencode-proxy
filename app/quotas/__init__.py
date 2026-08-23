"""
app.quotas — quota tracking (per-IP cooldown, station exhaustion, 429)

Extraction de opencode.py: _free_cooldowns, _FREE_429_DEFAULT 120s, _free_cooldown_key,
_sweep_free_cooldowns, _set_free_cooldown, _free_cooldown_active, _free_attempt_station,
_is_watchdog_cancelled, _free_stations_exhausted, _on_free_429_stream, FreeQuotaExhausted.

Centralise aussi free_ip_pool + shared_rotation pour P3.2.
"""

# Re-export depuis opencode pour compat
try:
    from opencode import (
        _free_cooldown_key as _free_cooldown_key,
        _free_429_cooldown_seconds as _free_429_cooldown_seconds,
        _free_stations_exhausted as _free_stations_exhausted,
        FreeQuotaExhausted as FreeQuotaExhausted,
    )
except ImportError:
    pass

__all__ = [
    "_free_cooldown_key",
    "_free_429_cooldown_seconds",
    "_free_stations_exhausted",
    "FreeQuotaExhausted",
]
