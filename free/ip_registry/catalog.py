"""NordVPN server catalog adapter.

The catalog is a read-only snapshot of the provider's server list.  It never
writes to the registry: the registry stores egress observations and server
bindings separately.

The public API omits older servers that gluetun still embeds (for example the
Albanian ``al36..al75`` pool).  ``extra_path`` optionally points at that
embedded catalog so discovery can see and rank those servers too.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .domain import Server

DEFAULT_URL = "https://api.nordvpn.com/v1/servers?limit=0"
DEFAULT_EXTRA_PATH = Path(__file__).resolve().parents[2] / "logs" / "gluetun_nordvpn_embedded.json"


class NordVpnCatalog:
    """Fetch and cache the NordVPN server list."""

    def __init__(
        self,
        *,
        url: str = DEFAULT_URL,
        ttl_s: float = 21600.0,
        fetch: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
        extra_path: str | Path | None = DEFAULT_EXTRA_PATH,
    ) -> None:
        self._url = url
        self._ttl_s = max(0.0, float(ttl_s))
        self._fetch = fetch or self._http_fetch
        self._clock = clock
        self._extra_path = Path(extra_path) if extra_path else None
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
        errors: list[str] = []
        api_servers: list[Server] = []
        try:
            raw = await asyncio.to_thread(self._fetch)
            api_servers = self._parse(raw)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

        extra_servers: list[Server] = []
        if self._extra_path is not None and self._extra_path.exists():
            try:
                extra_servers = await asyncio.to_thread(self._read_extra)
            except Exception as exc:
                errors.append(f"extra:{type(exc).__name__}: {exc}")

        servers = self._merge(api_servers, extra_servers)
        if servers:
            self._servers = tuple(servers)
            self._last_refresh = self._clock()
            self._last_error = "; ".join(errors) or None
        elif errors:
            self._last_error = "; ".join(errors)
        return self._servers

    def _http_fetch(self) -> Any:
        import requests

        response = requests.get(self._url, timeout=120)
        response.raise_for_status()
        return response.json()

    def _read_extra(self) -> list[Server]:
        with open(self._extra_path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            raw = raw.get("servers") or []
        return self._parse_gluetun(raw)

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

    @staticmethod
    def _parse_gluetun(raw: Any) -> list[Server]:
        """Parse gluetun's embedded catalog format (``vpn``/``ips``/``wgpubkey``)."""
        if isinstance(raw, dict):
            raw = raw.get("servers") or []
        if not isinstance(raw, list):
            return []
        servers: list[Server] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if str(item.get("vpn") or "").strip().lower() != "wireguard":
                continue
            hostname = str(item.get("hostname") or "").strip()
            country = str(item.get("country") or "").strip()
            if not hostname or not country:
                continue
            ips = item.get("ips") or []
            server_ip = None
            if isinstance(ips, list) and ips:
                server_ip = str(ips[0] or "").strip() or None
            city = str(item.get("city") or "").strip() or None
            servers.append(
                Server(
                    hostname=hostname,
                    country=country,
                    server_ip=server_ip,
                    city=city,
                    load=0.0,
                    technologies=("WireGuard",),
                )
            )
        return servers

    @staticmethod
    def _merge(api_servers: list[Server], extra_servers: list[Server]) -> list[Server]:
        """Merge embedded servers under the API snapshot (API wins on duplicates)."""
        merged: dict[str, Server] = {}
        for server in extra_servers:
            merged.setdefault(server.hostname, server)
        for server in api_servers:
            merged[server.hostname] = server
        return list(merged.values())
