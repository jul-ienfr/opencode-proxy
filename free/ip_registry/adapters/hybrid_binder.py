"""Hybrid station binder: control API when safe, Compose otherwise.

A hostname switch on the transport already configured in the container is
applied in a few seconds by Gluetun's control server. A transport change still
requires a Compose recreate, because a container restart re-reads its
environment. The hybrid keeps the fast path for same-transport retries and
never hides a real ``control-no-egress`` behind a two-minute recreate.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..domain import BindResult
from .compose_binder import ComposeHostnameBinder
from .gluetun_control import GluetunControlBinder


def transport_name(
    vpn_type: str | None, openvpn_protocol: str | None = None
) -> str:
    value = str(vpn_type or "wireguard").strip().lower()
    if value.startswith("openvpn"):
        protocol = str(openvpn_protocol or "udp").strip().lower()
        return f"openvpn-{protocol if protocol in ('udp', 'tcp') else 'udp'}"
    return "wireguard"


class HybridBinder:
    def __init__(
        self,
        *,
        control: GluetunControlBinder,
        compose: ComposeHostnameBinder,
        clock: Callable[[], float] = time.monotonic,
        cache_s: float = 300.0,
    ) -> None:
        self._control = control
        self._compose = compose
        self._clock = clock
        self._cache_s = max(0.0, float(cache_s))
        self._known: dict[int, str] = {}
        self._known_at: dict[int, float] = {}

    async def assign(
        self,
        station: int,
        hostname: str,
        country: str | None = None,
        *,
        vpn_type: str | None = None,
        openvpn_protocol: str | None = None,
        dns_mode: str | None = None,
    ) -> BindResult:
        requested = transport_name(vpn_type, openvpn_protocol)
        current = self._current(station)
        # A DNS mode change is an environment change: the control API cannot
        # apply it, so force the Compose path that writes the override.
        if current == requested and not dns_mode:
            result = await self._control.assign(
                station,
                hostname,
                country,
                vpn_type=vpn_type,
                openvpn_protocol=openvpn_protocol,
            )
            if result.ok:
                return result
            if not str(result.error or "").startswith("control-unavailable"):
                # The host itself is unreachable; recreating the same
                # transport would only burn another two minutes.
                return result
        result = await self._compose.assign(
            station,
            hostname,
            country,
            vpn_type=vpn_type,
            openvpn_protocol=openvpn_protocol,
            dns_mode=dns_mode,
        )
        if result.ok:
            self._remember(station, requested)
        return result

    def current_transport(self, station: int) -> str:
        """Transport the container is configured for, or ``""`` if unknown."""
        return self._current(int(station))

    async def release(self, station: int) -> None:
        await self._control.release(station)

    async def current(self, station: int) -> str | None:
        return await self._control.current(station)

    # ── internals ─────────────────────────────────────────────────────
    def _current(self, station: int) -> str:
        now = self._clock()
        cached = self._known.get(station, "")
        if cached and now - self._known_at.get(station, 0.0) < self._cache_s:
            return cached
        live = str(self._control.current_transport() or "").strip()
        if live:
            self._remember(station, live)
        return live

    def _remember(self, station: int, transport: str) -> None:
        self._known[int(station)] = str(transport)
        self._known_at[int(station)] = self._clock()
