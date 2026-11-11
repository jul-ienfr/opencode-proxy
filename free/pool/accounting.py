"""Accounting projections for the free IP pool (T160).

Read-only counters used by the dashboard/capacity advisor.  They never probe
the network: they only read state the pool already maintains.
"""

from __future__ import annotations

from typing import Any


def egress_endpoint_counts(pool: Any) -> dict[str, dict[str, int]]:
    """Count WARP/proxy endpoints for the capacity advisor.

    ``configured`` includes disabled endpoints, ``enabled`` those turned on,
    ``usable`` those passing the existing bad/quota marks.
    """

    out = {
        "warp": {"configured": 0, "enabled": 0, "usable": 0, "unique_buckets": 0},
        "proxies": {"configured": 0, "enabled": 0, "usable": 0, "unique_buckets": 0},
    }
    blocks: dict[str, set[str]] = {"warp": set(), "proxies": set()}
    for endpoint in list(getattr(pool, "_socks5_eps", None) or []):
        name = "warp" if pool._provider_matches(endpoint, "warp") else "proxies"
        out[name]["configured"] += 1
        if not getattr(endpoint, "enabled", True):
            continue
        out[name]["enabled"] += 1
        try:
            block = str(getattr(endpoint, "block_key", "") or "").strip()
            if not block:
                block = str(getattr(endpoint, "block", "") or "").strip()
            if not block:
                from free.egress_provider import block_for_egress

                block = block_for_egress(
                    getattr(endpoint, "egress_ip", None)
                ) or str(getattr(endpoint, "pid", "") or "")
            if block:
                blocks[name].add(block)
            if pool._socks5_client_usable(endpoint):
                out[name]["usable"] += 1
        except Exception:
            continue
    for name in ("warp", "proxies"):
        out[name]["unique_buckets"] = len(blocks[name])
    return out


def provider_request_counts(pool: Any) -> dict[str, int]:
    """Requests assigned per provider (cumulative since start)."""

    out = {"nordvpn": 0, "warp": 0, "proxies": 0, "direct": 0}
    for station in list(getattr(pool, "_stations", None) or []):
        try:
            out["nordvpn"] += int(
                pool._per_station(station).get("request_count", 0) or 0
            )
        except Exception:
            continue
    for endpoint in list(getattr(pool, "_socks5_eps", None) or []):
        name = "warp" if pool._provider_matches(endpoint, "warp") else "proxies"
        try:
            out[name] += int(
                pool._per_station(endpoint).get("request_count", 0) or 0
            )
        except Exception:
            continue
    return out


def endpoint_request_counts(pool: Any) -> list[dict[str, Any]]:
    """Per-endpoint cumulative counters to detect a hot endpoint."""

    out: list[dict[str, Any]] = []
    for station in list(getattr(pool, "_stations", None) or []):
        try:
            requests = int(
                pool._per_station(station).get("request_count", 0) or 0
            )
        except Exception:
            requests = 0
        out.append(
            {
                "provider": "nordvpn",
                "endpoint": str(
                    getattr(station, "pid", None)
                    or getattr(station, "_station", "")
                ),
                "requests": max(0, requests),
            }
        )
    for endpoint in list(getattr(pool, "_socks5_eps", None) or []):
        name = "warp" if pool._provider_matches(endpoint, "warp") else "proxies"
        try:
            requests = int(
                pool._per_station(endpoint).get("request_count", 0) or 0
            )
        except Exception:
            requests = 0
        out.append(
            {
                "provider": name,
                "endpoint": str(getattr(endpoint, "pid", "") or ""),
                "requests": max(0, requests),
            }
        )
    return out


__all__ = [
    "egress_endpoint_counts",
    "endpoint_request_counts",
    "provider_request_counts",
]
