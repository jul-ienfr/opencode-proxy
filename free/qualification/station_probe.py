"""HTTP station probe for the watchdog.

Reads the dashboard's ``/api/vpn-status`` payload and maps each station to a
``StationObservation``.  The HTTP call is injectable so tests never touch the
network.  This is an adapter: it may do I/O; the watchdog domain stays pure.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from .watchdog import StationObservation


class HttpStationProbe:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:4000",
        path: str = "/api/vpn-status",
        timeout_s: float = 10.0,
        fetch: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.path = str(path or "/api/vpn-status")
        self.timeout_s = max(1.0, float(timeout_s))
        self._fetch = fetch
        self._clock = clock

    async def probe(self, station: int) -> StationObservation:
        payload = await asyncio.to_thread(self._payload)
        if not isinstance(payload, dict):
            return StationObservation(station=int(station), error="status-unavailable")
        rows = payload.get("stations")
        if not isinstance(rows, list):
            return StationObservation(station=int(station), error="status-unavailable")
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                sid = int(row.get("station") or 0)
            except (TypeError, ValueError):
                continue
            if sid != int(station):
                continue
            return self._observe(int(station), row)
        return StationObservation(station=int(station), error="station-missing")

    def _payload(self) -> Any:
        if self._fetch is not None:
            try:
                return self._fetch()
            except Exception as exc:
                return {"error": f"{type(exc).__name__}: {exc}"}
        url = f"{self.base_url}{self.path}"
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8", "replace"))
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _observe(self, station: int, row: dict[str, Any]) -> StationObservation:
        vpn_raw = row.get("vpn")
        vpn: dict[str, Any] = vpn_raw if isinstance(vpn_raw, dict) else {}
        status = str(
            vpn.get("status") or row.get("status") or row.get("vpn_status") or ""
        ).strip().lower()
        error = str(row.get("error") or vpn.get("error") or "").strip()
        connected = status in ("connected", "degraded")
        auth_failed = bool(vpn.get("auth_failed")) or "auth" in status or "auth" in error.lower()
        egress_ip = str(
            row.get("ip")
            or row.get("current_ip")
            or vpn.get("current_ip")
            or vpn.get("ip")
            or ""
        ).strip()
        hostname = str(
            vpn.get("current_host")
            or vpn.get("pinned_hostname")
            or row.get("current_host")
            or row.get("hostname")
            or ""
        ).strip()
        transport = str(
            vpn.get("type")
            or vpn.get("protocol")
            or vpn.get("ovpn_protocol_effective")
            or row.get("transport")
            or ""
        ).strip()
        raw_usable = row.get("model_usable")
        if raw_usable is None:
            raw_usable = bool(connected and egress_ip and not auth_failed)
        client_usable = bool(raw_usable)
        reason = str(
            row.get("model_usable_reason")
            or vpn.get("model_usable_reason")
            or ""
        ).strip()
        dns_ok = bool(connected and "dns" not in error.lower())
        return StationObservation(
            station=int(station),
            connected=bool(connected),
            dns_ok=dns_ok,
            egress_ip=egress_ip,
            auth_failed=bool(auth_failed),
            client_usable=bool(client_usable),
            latency_ms=float(row.get("latency_ms") or 0.0),
            country=str(row.get("country") or vpn.get("country") or ""),
            hostname=hostname,
            transport=transport,
            container=str(row.get("container") or vpn.get("container") or ""),
            error=error,
            reason=reason,
        )


def build_http_station_probe(base_url: str = "http://127.0.0.1:4000") -> HttpStationProbe:
    return HttpStationProbe(base_url=base_url)


__all__ = ["HttpStationProbe", "build_http_station_probe"]
