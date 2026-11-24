"""Préflight DNS/port avant bind — feature 019, tâche T311.

Un candidat dont le nom ne résout pas (ou dont le port TCP est fermé pour un
transport TCP) ne doit jamais coûter un bind : le binder recréerait un
conteneur pour rien. Budget total : 5 s (2 s DNS + 2 s TCP).
"""

from __future__ import annotations

import socket
import time
from typing import Any

__all__ = ["PREFLIGHT_BUDGET_S", "preflight_host"]

PREFLIGHT_BUDGET_S = 5.0
DNS_TIMEOUT_S = 2.0
TCP_TIMEOUT_S = 2.0
TCP_PORT = 443


def _resolve(hostname: str, timeout: float = DNS_TIMEOUT_S) -> list[str]:
    old = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(max(0.2, float(timeout)))
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return []
    finally:
        socket.setdefaulttimeout(old)
    seen: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in seen:
            seen.append(addr)
    return seen


def _tcp_open(host: str, port: int = TCP_PORT, timeout: float = TCP_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection((host, port), timeout=max(0.2, float(timeout))):
            return True
    except OSError:
        return False


def preflight_host(
    hostname: str,
    transport: str = "wireguard",
    *,
    timeout_s: float = PREFLIGHT_BUDGET_S,
) -> tuple[bool, str, dict[str, Any]]:
    """Retourne ``(ok, reason, details)``. N'ouvre aucun tunnel."""
    started = time.monotonic()
    host = str(hostname or "").strip()
    details: dict[str, Any] = {"host": host, "transport": transport}
    if not host:
        return False, "empty-host", details
    addrs = _resolve(host, timeout=min(DNS_TIMEOUT_S, timeout_s))
    details["resolved"] = addrs[:4]
    if not addrs:
        details["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
        return False, "dns-failed", details
    remaining = timeout_s - (time.monotonic() - started)
    if remaining <= 0:
        details["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
        return True, "dns-ok-budget-exhausted", details
    tcp_ok = _tcp_open(addrs[0], timeout=min(TCP_TIMEOUT_S, remaining))
    details["tcp_443"] = tcp_ok
    details["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    if str(transport).startswith("openvpn") and not tcp_ok:
        return False, "port-unreachable", details
    # WireGuard est UDP : un TCP fermé est un signal, pas une preuve.
    return True, "ok" if tcp_ok else "dns-ok-tcp-closed-advisory", details
