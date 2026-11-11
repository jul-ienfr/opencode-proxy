"""Pure selection helpers for the free IP pool (T159).

Kept free of pool state so the strangler slices can be tested without
constructing a ``FreeIPPool``.  ``__init__`` re-exports ``egress_rank`` as
``_egress_rank`` for the historical call sites.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any


def egress_rank(station: Any) -> int:
    """Second-choice penalty: ``tunnel_up_full`` (0) before ``socks_down`` (1).

    Unknown or absent state means 0 (no behaviour change for stubs).
    """

    try:
        if str(
            getattr(station, "_egress_state", "tunnel_up_full")
            or "tunnel_up_full"
        ) == "tunnel_up_socks_down":
            return 1
    except Exception:
        pass
    return 0


def dedupe_by_ip(
    stations: Iterable[Any],
    *,
    sort_key: Callable[[Any], Any] | None = None,
) -> list[Any]:
    """Keep one station per observed egress IP (quota is an IP property).

    ``sort_key`` orders candidates before deduplication.  A key failure falls
    back to the input order, matching the legacy in-method behaviour.
    """

    try:
        ordered = sorted(stations, key=sort_key) if sort_key is not None else list(stations)
    except Exception:
        ordered = list(stations)
    out: list[Any] = []
    seen: set[str] = set()
    for station in ordered:
        try:
            ip = str(getattr(station, "current_ip", "") or "")
        except Exception:
            ip = ""
        if ip and ip != "unknown":
            if ip in seen:
                continue
            seen.add(ip)
        out.append(station)
    return out


def degraded_rank(
    station: Any,
    *,
    per_state: dict[Any, dict[str, Any]] | None = None,
    health_registry: Any = None,
) -> int:
    """0 = healthy, 1 = degraded but still serviceable (last-resort tie-break).

    ``SUSPECT`` means one failed probe, not yet repairing: such a station is
    still routable but must sort behind a ``USABLE`` one.
    """

    try:
        sid = getattr(station, "_station", None)
        per = per_state.get(sid) if isinstance(per_state, dict) and isinstance(sid, int) else None
        if per and per.get("degraded_override"):
            return 1
        if isinstance(sid, int) and health_registry is not None:
            health = health_registry.get(sid, create=False)
            if health is not None and health.state == "suspect":
                return 1
    except Exception:
        pass
    return 0


def station_id(station: Any) -> int:
    """Station id used by ordering and per-station state."""

    try:
        return int(getattr(station, "_station", 0) or 0)
    except (TypeError, ValueError):
        return 0


def prefer_healthy(
    usable: Iterable[Any],
    *,
    degraded_rank: Callable[[Any], int],
) -> list[Any]:
    """Healthy stations before degraded-but-serviceable ones."""

    try:
        full = [
            st
            for st in usable
            if egress_rank(st) == 0 and degraded_rank(st) == 0
        ]
        if full:
            return full
        egress_ok = [st for st in usable if egress_rank(st) == 0]
        return egress_ok or list(usable)
    except Exception:
        return list(usable)


def quality_stats(
    station: Any,
    *,
    per_station: Callable[[Any], dict[str, Any]],
) -> tuple[int, int, float | None, float | None]:
    """(requests on this IP, quota, server load, in-flight) for the selector."""

    inflight: float | None = None
    try:
        per = per_station(station)
        used = int(per.get("request_count", 0) or 0)
        inflight = float(len(per.get("stream_tasks") or ()))
    except Exception:
        used = 0
    try:
        limit = int(getattr(station, "_quota_per_ip", 0) or 0)
    except Exception:
        limit = 0
    try:
        load = getattr(station, "_server_load", None)
    except Exception:
        load = None
    return used, limit, load, inflight


def selection_latency(
    station: Any,
    *,
    quality: Any = None,
) -> float | None:
    """Observed latency for selection (quality registry first, then load)."""

    try:
        sid = int(getattr(station, "_station", -1))
        ip = str(getattr(station, "current_ip", "") or "unknown")
        if quality is not None:
            item = quality.get(sid, ip)
            value = item.latency_ms()
            if value:
                return float(value)
    except Exception:
        pass
    try:
        value = getattr(station, "_server_load", None)
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "dedupe_by_ip",
    "degraded_rank",
    "egress_rank",
    "prefer_healthy",
    "quality_stats",
    "selection_latency",
    "station_id",
]
