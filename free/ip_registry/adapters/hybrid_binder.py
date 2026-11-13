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
        control: object,
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
        self._credentials_checked: dict[int, bool] = {}

    @property
    def tri_stack(self) -> bool:
        """True when the control plane owns the transport ladder."""
        return bool(getattr(self._control, "is_control_plane", False))

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
        current = "" if self.tri_stack else self._current(station)
        # A DNS mode change or stale credentials are environment changes: the
        # control API cannot apply them, so force the Compose recreate.
        if not dns_mode and self._credentials_fresh(station):
            control_first = self.tri_stack or current == requested
            if control_first:
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
            self._credentials_checked[int(station)] = True
        return result

    def current_transport(self, station: int) -> str:
        """Transport the container is configured for, or ``""`` if unknown."""
        return self._current(int(station))

    async def release(self, station: int) -> None:
        await self._control.release(station)

    async def current(self, station: int) -> str | None:
        return await self._control.current(station)

    # ── internals ─────────────────────────────────────────────────────
    def _credentials_fresh(self, station: int) -> bool:
        station = int(station)
        if station not in self._credentials_checked:
            self._credentials_checked[station] = not (
                self._compose.credentials_stale(station)
            )
        return self._credentials_checked[station]

    def _current(self, station: int) -> str:
        now = self._clock()
        cached = self._known.get(station, "")
        if cached and now - self._known_at.get(station, 0.0) < self._cache_s:
            return cached
        try:
            live_value = self._control.current_transport(station)
        except TypeError:
            # The legacy binder takes no station argument.
            live_value = self._control.current_transport()
        live = str(live_value or "").strip()
        if live:
            self._remember(station, live)
        return live

    def _remember(self, station: int, transport: str) -> None:
        self._known[int(station)] = str(transport)
        self._known_at[int(station)] = self._clock()
