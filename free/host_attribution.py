"""Runtime metric: free VPN requests carrying a server_hostname.

Kept in its own module (not ``opencode``) because the proxy runs as
``__main__``: ``import opencode`` from the dashboard would create a second
module object with independent globals.  A normal module import is shared by
both paths.
"""

from __future__ import annotations

import logging
import threading

_LOCK = threading.Lock()
_STATS: dict[str, int] = {
    "free_vpn_requests": 0,
    "with_host": 0,
    "missing_host": 0,
}


def reset() -> None:
    with _LOCK:
        for key in _STATS:
            _STATS[key] = 0


def note(server_host: str | None) -> None:
    """Count one free VPN request and warn when attribution is missing.

    A regression here means the preselection silently falls back to the API
    ``load``: the warning is the only signal before the reports go flat.
    """

    with _LOCK:
        _STATS["free_vpn_requests"] += 1
        if server_host:
            _STATS["with_host"] += 1
            return
        _STATS["missing_host"] += 1
        missing = _STATS["missing_host"]
        if missing == 20 or (missing > 20 and missing % 100 == 0):
            total = _STATS["free_vpn_requests"]
            logging.getLogger("host_attribution").warning(
                "[host-attribution] %d requêtes free VPN sans server_host "
                "(couverture %.1f%%) — la présélection retombe sur le load",
                missing,
                100.0 * _STATS["with_host"] / max(1, total),
            )


def snapshot() -> dict[str, float | int]:
    with _LOCK:
        total = _STATS["free_vpn_requests"]
        with_host = _STATS["with_host"]
        missing = _STATS["missing_host"]
    return {
        "free_vpn_requests": total,
        "with_host": with_host,
        "missing_host": missing,
        "coverage": round(with_host / total, 4) if total else None,
    }


__all__ = ["note", "reset", "snapshot"]
