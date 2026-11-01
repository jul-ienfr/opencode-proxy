"""NordVPN server catalog adapter.

The catalog is a read-only snapshot of the provider's server list.  It never
writes to the registry: the registry stores egress observations and server
bindings separately.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from .domain import Server

DEFAULT_URL = "https://api.nordvpn.com/v1/servers?limit=0"


class NordVpnCatalog:
    """Fetch and cache the NordVPN server list."""

    def __init__(
        self,
        *,
        url: str = DEFAULT_URL,
        ttl_s: float = 21600.0,
        fetch: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._url = url
        self._ttl_s = max(0.0, float(ttl_s))
        self._fetch = fetch or self._http_fetch
        self._clock = clock
        self._servers: tuple[Server, ...] = ()
        self._last_refresh: float | None = None
        self._last_error: str | None = None

    def snapshot(self) -> tuple[Server, ...]:
        return self._servers

    @property
    def last_refresh_at(self) -> float | None:
        return self._last_refresh

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def is_stale(self, now: float | None = None) -> bool:
        if self._last_refresh is None:
            return True
        return (now if now is not None else self._clock()) - self._last_refresh >= self._ttl_s

    async def refresh(self) -> tuple[Server, ...]:
        try:
            raw = await asyncio.to_thread(self._fetch)
            servers = self._parse(raw)
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            return self._servers
        if servers:
            self._servers = tuple(servers)
            self._last_refresh = self._clock()
            self._last_error = None
        return self._servers

    def _http_fetch(self) -> Any:
        import requests

        response = requests.get(self._url, timeout=120)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _parse(raw: Any) -> list[Server]:
        if not isinstance(raw, list):
            return []
        servers: list[Server] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "") != "online":
                continue
            hostname = str(item.get("hostname") or "").strip()
            locations = item.get("locations") or []
            country = ""
            city = None
            if isinstance(locations, list) and locations:
                loc = locations[0] if isinstance(locations[0], dict) else {}
                country_obj = loc.get("country") or {}
                country = str((country_obj or {}).get("name") or "").strip()
                city_obj = loc.get("city") or {}
                city = str((city_obj or {}).get("name") or "").strip() or None
            if not hostname or not country:
                continue
            technologies = tuple(
                str((tech or {}).get("name") or "").strip()
                for tech in (item.get("technologies") or [])
                if isinstance(tech, dict) and str((tech or {}).get("name") or "").strip()
            )
            try:
                load = float(item.get("load") or 0.0)
            except (TypeError, ValueError):
                load = 0.0
            servers.append(
                Server(
                    hostname=hostname,
                    country=country,
                    server_ip=str(item.get("station") or "").strip() or None,
                    city=city,
                    load=max(0.0, load),
                    technologies=technologies,
                )
            )
        return servers
