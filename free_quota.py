"""
Free quota: per-IP cooldown, station exhaustion, 429 handling.
Extracted from opencode.py (P3.10).
"""
import time
import re
import asyncio
import email.utils

_free_model_cooldowns: dict[str, float] = {}  # "model|ip" -> monotonic expiry
_FREE_COOLDOWN_MAX = 86400  # hard ceiling: retry-after beyond 24 h → default below
# [incident 17/08 PAYANT] zen's free API 429 (FreeUsageLimitError) carries NO
# retry-after header. The old 3600 s default meant ONE 429 — even a transient
# tunnel-blip direct fallback — disabled every free attempt for a FULL HOUR
# (verified: `skipping free model (cooldown active)` every minute 19:32-20:31,
# a full hour of guaranteed PAID traffic). 120 s bounds the damage: after a
# rotation the (model, IP) key is fresh anyway, and 120 s ≈ worst-case rotation
# time + margin. Explicitly-pronounced retry-after values are still honored.
_FREE_429_DEFAULT = 120.0


def _free_cooldown_key(free_model: str, station=None) -> str:
    """Cooldown key = (free model, egress IP of the station used) ([4]).

    With dual station, the 429 must cooldown the IP of the station that
    actually served the request — not station 1's IP, which may be a
    different, still-fresh key. ``station=None`` → the active station
    (or station 1 as before). "direct" when no VPN IP is known (VPN
    down / direct mode) — in that case there is no fresh-IP path, so the
    key must stay stable.
    """
    vpn = station or (_free_ip_pool.active_station if _free_ip_pool else None) or _vpn_manager
    if getattr(vpn, "pid", None):
        # [Axe 3.1] Static SOCKS5 proxy: no docker IP to key on — the
        # proxy's identity IS its host:port. Each proxy gets its own
        # bucket: a 429 on one must never cooldown the others, which
        # egress separate IPs.
        return f"{free_model}|socks5:{vpn.pid}"
    ip = vpn.current_ip if (vpn and vpn.current_ip) else "direct"
    return f"{free_model}|{ip}"


def _free_429_cooldown_seconds(retry_after: str = "") -> float:
    """Duration (seconds) to cooldown a free model after a 429.

    Accepts a seconds count ("120") or an RFC 9110 HTTP-date
    ("Wed, 21 Oct 2015 07:28:00 GMT") ([7]). Anything unparseable or out
    of (0, 86400] → _FREE_429_DEFAULT (120 s). An absent retry-after means
    we don't know the reset time — [incident 17/08] the OLD 3600 s default
    made one 429 block ALL free attempts for an hour (an hour of paid); the
    short default is safe because the key is (model, IP): the background
    rotation gives a fresh IP/key well within 120 s.
    """
    if not retry_after:
        return _FREE_429_DEFAULT
    v = 0.0
    try:
        v = float(retry_after)
    except (TypeError, ValueError):
        try:
            parsed = email.utils.parsedate_to_datetime(retry_after)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)  # HTTP-date is GMT
            v = (parsed - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return _FREE_429_DEFAULT
    if 0 < v <= _FREE_COOLDOWN_MAX:
        return v
    return _FREE_429_DEFAULT


def _sweep_free_cooldowns() -> int:
    """[plan 18/08 §4.2] Drop expired (model, IP) cooldown entries.

    Without a periodic sweep the map grows without bound: each rotation
    leaves the previous IP's key behind (expiry is only cleaned lazily on
    that exact key's next lookup). Run on the lifespan background tick —
    the len()>32 guard in _set_free_cooldown remains a soft-limit for
    transient bursts between ticks.
    """
    now = time.monotonic()
    expired = [k for k, t in _free_model_cooldowns.items() if t <= now]
    for k in expired:
        del _free_model_cooldowns[k]
    if expired:
        _debug(f"  [cooldown] swept {len(expired)} expired free-cooldown entries")
    return len(expired)


def _set_free_cooldown(free_model: str, seconds: float, station=None) -> None:
    key = _free_cooldown_key(free_model, station)
    expiry = time.monotonic() + seconds
    # soft-limit ([plan 18/08 §4.2]): the periodic _sweep_free_cooldowns is
    # the real memory bound; this only avoids a burst between two ticks.
    if len(_free_model_cooldowns) > 32:
        _sweep_free_cooldowns()
    _free_model_cooldowns[key] = expiry
    _log(f"  FREE COOLDOWN: {key} for {seconds:.0f}s")


def _free_cooldown_active(free_model: str, station=None) -> bool:
    key = _free_cooldown_key(free_model, station)
    expiry = _free_model_cooldowns.get(key, 0.0)
    if expiry > 0 and time.monotonic() < expiry:
        return True
    if expiry > 0:
        del _free_model_cooldowns[key]
    return False


def _free_attempt_station():
    """The station that served the current free attempt (ContextVar).

    ``None`` when the attempt went direct or no free attempt is running
    — callers then fall back to the active/last-used station.
    """
    attempt = _current_free_attempt.get()
    return attempt.get("station") if attempt else None


def _is_watchdog_cancelled(station) -> bool:
    """[plan 18/08 §am.22/piège 19] was THIS task cancelled by the
    egress-death watchdog (pool.cancel_streams on a confirmed-dead tunnel)?

    The stream registry is OPTIONAL pool protocol: a pool double without it
    can never classify (False) — the request path must not depend on it.
    Sync and pure (id-marker lookups), safe to call mid-handler.
    """
    pool = _free_ip_pool
    if station is None or pool is None:
        return False
    return getattr(pool, "is_watchdog_cancelled",
                   lambda *a, **k: False)(station, asyncio.current_task())


def _free_stations_exhausted(free_model: str) -> bool:
    """True when NO station can serve a fresh free attempt for this model.

    A station is exhausted when its tunnel is down / marked bad by a
    recent 429, or when the (model, IP) cooldown key of its current IP
    is still active. Used by strict_free: only refuse the request when
    every station is truly exhausted — otherwise let the free attempt
    land on the usable station instead of paying or refusing.

    [Axe 1.4] A rotation in flight is about to land a fresh (model, IP)
    key on its station — the pool is NOT exhausted. Checking participation
    re-reads the same single-threaded state the pool mutates, so the gate
    is atomic with respect to rotations (no lock needed; see
    ``FreeIPPool.any_rotation_in_flight``).
    """
    if not _free_ip_pool:
        return True
    if getattr(_free_ip_pool, "socks5_mode", False):
        # [Axe 3.1] socks5 mode: the docker stations are inert — the usable
        # set is the enabled static proxies. No rotation can land a fresh
        # (model, IP) key here (no docker), so the rotation-in-flight
        # exemption below does not apply.
        for ep in _free_ip_pool._socks5_enabled_eps():
            if _free_ip_pool._socks5_usable(ep, exclude_approaching=False):
                if not _free_cooldown_active(free_model, ep):
                    return False
        return True
    if not _free_ip_pool._stations:
        return True
    if getattr(_free_ip_pool, "any_rotation_in_flight",
               lambda: False)():
        return False
    for st in _free_ip_pool._stations:
        if _free_ip_pool._station_usable(st, exclude_approaching=False):
            if not _free_cooldown_active(free_model, st):
                return False
    return True


def _on_free_429_stream(free_model: str, retry_after: str = "",
                        forced_pool=None) -> bool:
    """Free endpoint 429 during streaming: cooldown + paid fallback.

    Returns True when the request must be REFUSED (strict_free mode and
    both stations exhausted) — the caller then answers 429/503 to the
    client instead of falling back to paid. Returns False → paid
    fallback (default behavior).

    [0]/[42] restored: a 429 ALSO triggers an IP rotation in the
    background (single-flight via FreeIPPool.on_quota_exhausted; the
    calling request falls back to paid immediately). The cooldown is
    keyed per (model, IP) ([4]) so the fresh IP starts with a fresh key
    — the next free attempt on the new IP is NOT blocked by this 429.

    Axe B: ``forced_pool`` is propagated to the background rotation so
    the rotated station stays within geo-allowed countries.

    No-op when VPN rotation is off.
    """
    station = _free_attempt_station()
    _set_free_cooldown(free_model, _free_429_cooldown_seconds(retry_after), station)
    if _free_ip_pool:
        try:
            _free_ip_pool.on_quota_exhausted(station, forced_pool=forced_pool)
        except TypeError:
            _free_ip_pool.on_quota_exhausted(station)
    return bool(IP_ROTATION.get("strict_free", False)) and _free_stations_exhausted(free_model)


def _free_usage_ip(station=None) -> str:
    """Best-effort egress IP for free-model usage logging ([9]).

    Never does network I/O — prefers the live VPN IP of the station
    used (or station 1 as before), falls back to the cached ipify result
    from the last non-stream probe.
    """
    vpn = station or (_free_ip_pool.active_station if _free_ip_pool else None) or _vpn_manager
    if getattr(vpn, "pid", None):
        # [Axe 3.1] Static SOCKS5 proxy: no docker IP to report — its
        # host:port identity is the correct usage label.
        return f"socks5:{vpn.pid}"
    if vpn and vpn.current_ip:
        return vpn.current_ip
    return _public_ip_cache.get("ip", "") or ""


class FreeQuotaExhausted(Exception):
    """Raised by _try_free_model_first in strict_free mode when a 429
    leaves no usable station (all stations bad/down and their (model, IP)
    cooldown keys still active). The caller converts this into a 429/503
    to the client with Retry-After — never a paid fallback."""

    def __init__(self, retry_after: str = ""):
        super().__init__(f"free quota exhausted on all VPN stations (retry-after={retry_after!r})")
        self.retry_after = retry_after


def _free_quota_exhausted_response(exc: FreeQuotaExhausted, protocol: str):
    """HTTP refusal for strict_free exhaustion (non-stream requests)."""
    retry_after = exc.retry_after or "60"
    if protocol == "anthropic":
        resp = _anthropic_error(429, f"Free quota exhausted on all VPN stations. Retry after {retry_after}s.",
                                error_type="rate_limit_error")
    else:
        resp = _openai_error(429, f"Free quota exhausted on all VPN stations. Retry after {retry_after}s.",
                             error_type="rate_limit_error")
    resp.headers["Retry-After"] = retry_after
    return resp


