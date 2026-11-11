"""Quota helpers for the free IP pool (T159).

Only the identity rule is extracted for now: a logical ``socks5:<label>`` is
not an egress IP and must never key the daily bucket.  The ledger itself stays
authoritative in :mod:`free.quota_ledger`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def ledger_ip(
    station: Any,
    ip: str | None = None,
    *,
    per_station: Callable[[Any], dict[str, Any]] | None = None,
) -> str:
    """Resolve the real egress IP used as the quota bucket key.

    Falls back to the last confirmed IP recorded for the station, and strips
    the logical SOCKS5 label that is not an address.
    """

    try:
        raw = str(ip or getattr(station, "current_ip", "") or "").strip()
    except Exception:
        raw = str(ip or "").strip()
    if raw.startswith("socks5:"):
        raw = ""
    if not raw and per_station is not None:
        try:
            raw = str((per_station(station) or {}).get("last_confirmed_ip") or "").strip()
        except Exception:
            raw = ""
        if raw.startswith("socks5:"):
            raw = ""
    return raw


def apply_quota_config(
    profile: Any,
    cfg: dict[str, Any],
    *,
    socks5_eps: Any = (),
    per: dict[Any, dict[str, Any]] | None = None,
    vpn: Any = None,
) -> None:
    """Hot-reload ``quota_per_ip`` into the profile and SOCKS endpoints.

    ``FreeIPPool.update_config`` previously refreshed only SOCKS endpoints:
    the ``IpQuotaRegistry`` kept the startup profile, so changing the GUI
    quota had no effect on VPN stations until a restart.
    """

    if "quota_per_ip" in cfg:
        try:
            quota = max(1, int(cfg["quota_per_ip"] or 0))
        except (TypeError, ValueError):
            quota = 0
        if quota:
            try:
                profile.default_quota = quota
            except Exception:
                pass
        current = getattr(
            vpn,
            "_quota_per_ip",
            getattr(vpn, "quota_per_ip", 0),
        )
        for endpoint in socks5_eps or ():
            endpoint._quota_per_ip = current
            state = (per or {}).get(endpoint._station)
            if state is not None and state.get("last_quota_per_ip") != current:
                state["request_count"] = 0
                state["last_quota_per_ip"] = current
    by_model = cfg.get("quota_per_ip_by_model")
    if isinstance(by_model, dict):
        try:
            from free.ip_quota import QuotaProfile

            refreshed = QuotaProfile.from_config(
                {"quota_per_ip_by_model": by_model}
            )
            profile.by_model = refreshed.by_model
        except Exception:
            pass


__all__ = ["apply_quota_config", "ledger_ip"]
