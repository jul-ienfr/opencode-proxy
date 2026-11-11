"""Cheap endpoint preflight for VPN candidates.

The preflight deliberately avoids opening a full VPN tunnel: it resolves the
hostname, opens the transport endpoint and (when a handshake probe is
injected) checks that the peer answers. It is a filter, not a quality
decision: a candidate that passes still has to be bound and probed by the
normal chain.

The module is I/O-injectable so tests and the scanner can use fakes. It does
not import Docker or the provider adapters.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from statistics import median
from typing import Any

TCP_PORTS = {"openvpn-tcp": 443, "openvpn-udp": 1194, "wireguard": 51820}
UDP_TRANSPORTS = {"wireguard", "openvpn-udp"}


@dataclass(frozen=True)
class PreflightResult:
    hostname: str
    transport: str
    country: str = ""
    endpoint_ip: str = ""
    dns_ms: float = 0.0
    connect_ms: float = 0.0
    rtt_ms: float = 0.0
    dns_ok: bool = False
    endpoint_ok: bool = False
    handshake_ok: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.dns_ok and self.endpoint_ok and self.handshake_ok)


@dataclass
class PreflightSummary:
    total: int = 0
    ok: int = 0
    failed: int = 0
    rtt_p50_ms: float | None = None
    errors: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_results(cls, results: Iterable[PreflightResult]) -> PreflightSummary:
        items = list(results)
        rtts = [r.rtt_ms for r in items if r.ok and r.rtt_ms > 0]
        errors: dict[str, int] = {}
        for item in items:
            if item.ok:
                continue
            key = item.error.split(":", 1)[0] or "unknown"
            errors[key] = errors.get(key, 0) + 1
        return cls(
            total=len(items),
            ok=sum(1 for item in items if item.ok),
            failed=sum(1 for item in items if not item.ok),
            rtt_p50_ms=float(median(rtts)) if rtts else None,
            errors=errors,
        )


class PreflightProber:
    """Resolve + endpoint + optional handshake checks with a timeout budget."""

    def __init__(
        self,
        *,
        resolve: Callable[[str, int], Awaitable[list[str]]] | None = None,
        tcp_connect: Callable[[str, int, float], Awaitable[float]] | None = None,
        udp_probe: Callable[[str, int, float], Awaitable[float]] | None = None,
        handshake: Callable[[str, str, float], Awaitable[float]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._resolve = resolve or self._default_resolve
        self._tcp_connect = tcp_connect or self._default_tcp_connect
        self._udp_probe = udp_probe or self._default_udp_probe
        self._handshake = handshake
        self._clock = clock

    async def probe(
        self,
        hostname: str,
        transport: str,
        country: str = "",
        *,
        port: int | None = None,
        timeout: float = 3.0,
    ) -> PreflightResult:
        hostname = str(hostname or "").strip()
        transport = str(transport or "").strip().lower()
        budget = max(0.5, float(timeout))
        if not hostname:
            return PreflightResult("", transport, country, error="empty-hostname")
        if transport not in TCP_PORTS:
            return PreflightResult(
                hostname, transport, country, error=f"unknown-transport:{transport}"
            )
        endpoint_port = int(port or TCP_PORTS[transport])
        started = self._clock()
        try:
            addresses = await asyncio.wait_for(
                self._resolve(hostname, endpoint_port), timeout=budget
            )
        except Exception as exc:
            return PreflightResult(
                hostname,
                transport,
                country,
                error=f"dns:{type(exc).__name__}",
            )
        dns_ms = (self._clock() - started) * 1000.0
        endpoint_ip = next(
            (str(ip) for ip in addresses if str(ip).count(".") == 3), ""
        )
        if not endpoint_ip:
            return PreflightResult(
                hostname,
                transport,
                country,
                dns_ms=dns_ms,
                error="dns:no-ipv4",
            )
        try:
            if transport in UDP_TRANSPORTS:
                connect_ms = await asyncio.wait_for(
                    self._udp_probe(endpoint_ip, endpoint_port, budget),
                    timeout=budget,
                )
            else:
                connect_ms = await asyncio.wait_for(
                    self._tcp_connect(endpoint_ip, endpoint_port, budget),
                    timeout=budget,
                )
        except Exception as exc:
            return PreflightResult(
                hostname,
                transport,
                country,
                endpoint_ip=endpoint_ip,
                dns_ms=dns_ms,
                dns_ok=True,
                error=f"endpoint:{type(exc).__name__}",
            )
        handshake_ms = 0.0
        try:
            if self._handshake is not None:
                handshake_ms = await asyncio.wait_for(
                    self._handshake(endpoint_ip, transport, budget),
                    timeout=budget,
                )
        except Exception as exc:
            return PreflightResult(
                hostname,
                transport,
                country,
                endpoint_ip=endpoint_ip,
                dns_ms=dns_ms,
                connect_ms=float(connect_ms or 0.0),
                dns_ok=True,
                endpoint_ok=True,
                error=f"handshake:{type(exc).__name__}",
            )
        rtt_ms = float(connect_ms or 0.0) + float(handshake_ms or 0.0)
        return PreflightResult(
            hostname,
            transport,
            country,
            endpoint_ip=endpoint_ip,
            dns_ms=dns_ms,
            connect_ms=float(connect_ms or 0.0),
            rtt_ms=rtt_ms,
            dns_ok=True,
            endpoint_ok=True,
            handshake_ok=True,
        )

    async def preflight_many(
        self,
        candidates: Iterable[dict[str, Any]],
        transport: str,
        *,
        concurrency: int = 64,
        timeout: float = 3.0,
        budget_s: float = 30.0,
        port: int | None = None,
    ) -> list[PreflightResult]:
        """Probe candidates concurrently without exceeding ``concurrency``.

        Each candidate is bounded by ``timeout``; the whole batch is bounded
        by ``budget_s`` (a timed-out item is reported as ``budget`` instead of
        stalling the sweep).
        """

        semaphore = asyncio.Semaphore(max(1, int(concurrency)))
        budget = max(0.5, float(budget_s))

        async def one(item: dict[str, Any]) -> PreflightResult:
            host = str(item.get("host") or item.get("hostname") or "").strip()
            country = str(item.get("country") or "").strip()
            if not host:
                return PreflightResult("", transport, country, error="empty-hostname")
            async with semaphore:
                try:
                    return await asyncio.wait_for(
                        self.probe(
                            host,
                            transport,
                            country,
                            port=port,
                            timeout=timeout,
                        ),
                        timeout=budget,
                    )
                except TimeoutError:
                    return PreflightResult(
                        host, transport, country, error="budget"
                    )
                except Exception as exc:
                    return PreflightResult(
                        host,
                        transport,
                        country,
                        error=f"preflight:{type(exc).__name__}",
                    )

        return list(await asyncio.gather(*(one(item) for item in candidates)))

    # ── default stdlib probes ─────────────────────────────────────────
    @staticmethod
    async def _default_resolve(hostname: str, port: int) -> list[str]:
        def _lookup() -> list[str]:
            infos = socket.getaddrinfo(
                hostname, port, family=socket.AF_INET, type=socket.SOCK_STREAM
            )
            return list(dict.fromkeys(str(info[4][0]) for info in infos))

        return await asyncio.to_thread(_lookup)

    @staticmethod
    async def _default_tcp_connect(ip: str, port: int, timeout: float) -> float:
        def _connect() -> float:
            started = time.monotonic()
            with socket.create_connection((ip, int(port)), timeout=timeout):
                return (time.monotonic() - started) * 1000.0

        return await asyncio.to_thread(_connect)

    @staticmethod
    async def _default_udp_probe(ip: str, port: int, timeout: float) -> float:
        """Send one datagram and wait briefly for a peer response.

        A silent protocol (WireGuard) can legitimately not answer a random
        payload; callers that need a real handshake must inject ``handshake``.
        The default therefore treats a successful send as endpoint-reachable;
        the full bind/probe remains the source of truth for quality.
        """

        def _send() -> float:
            started = time.monotonic()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(min(0.5, max(0.05, timeout)))
                sock.sendto(b"\x00", (ip, int(port)))
                try:
                    sock.recvfrom(2048)
                except (TimeoutError, OSError):
                    pass
            return (time.monotonic() - started) * 1000.0

        return await asyncio.to_thread(_send)
