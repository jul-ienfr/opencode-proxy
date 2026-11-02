"""NordVPN server catalog adapter.

The catalog is a read-only snapshot of the provider's server list.  It never
writes to the registry: the registry stores egress observations and server
bindings separately.

Resolution is delegated to :class:`~free.ip_registry.sources.SourceRegistry`:
the official API is the primary source, then the manifest fallbacks, while
the embedded gluetun catalog only *completes* hostnames the API omits.  Every
row keeps its ``source_id`` and ``stale`` flag so a fallback is never silent.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .domain import Server
from .sources.adapters import DEFAULT_API_URL
from .sources.base import CatalogResolution
from .sources.manifest import DEFAULT_MANIFEST_PATH
from .sources.parsers import parse_gluetun_payload, parse_official_payload
from .sources.registry import (
    DEFAULT_CACHE_PATH,
    DEFAULT_EMBEDDED_PATH,
    DEFAULT_GLUETUN_MANIFEST_PATH,
    SourceRegistry,
    build_default_registry,
)

DEFAULT_URL = DEFAULT_API_URL
DEFAULT_EXTRA_PATH = DEFAULT_EMBEDDED_PATH


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
        cache_path: str | Path | None = DEFAULT_CACHE_PATH,
        gluetun_manifest_path: str | Path | None = DEFAULT_GLUETUN_MANIFEST_PATH,
        manifest_path: str | Path | None = DEFAULT_MANIFEST_PATH,
        sources: SourceRegistry | None = None,
    ) -> None:
        self._ttl_s = max(0.0, float(ttl_s))
        self._clock = clock
        self._sources = sources or build_default_registry(
            manifest_path=manifest_path,
            url=url,
            fetch=fetch,
            extra_path=extra_path,
            cache_path=cache_path,
            gluetun_manifest_path=gluetun_manifest_path,
        )
        self._servers: tuple[Server, ...] = ()
        self._last_refresh: float | None = None
        self._last_error: str | None = None
        self._last_resolution: CatalogResolution | None = None
        self._last_refresh_failed = False

    @classmethod
    def from_config(
        cls,
        section: dict[str, Any],
        *,
        clock: Callable[[], float] = time.time,
    ) -> NordVpnCatalog:
        """Build a catalog from the ``ip_registry`` config section."""

        raw_sources = section.get("sources") if isinstance(section, dict) else None
        sources = raw_sources if isinstance(raw_sources, dict) else {}
        root = Path(__file__).resolve().parents[2]

        def _path(key: str, default: Path) -> Path:
            value = sources.get(key)
            path = Path(value) if value else default
            return path if path.is_absolute() else root / path

        return cls(
            ttl_s=float(section.get("catalog_refresh_s", 21600) or 21600),
            clock=clock,
            manifest_path=_path("manifest", DEFAULT_MANIFEST_PATH),
            extra_path=_path("embedded_path", DEFAULT_EXTRA_PATH),
            cache_path=_path("cache_path", DEFAULT_CACHE_PATH),
            gluetun_manifest_path=_path(
                "gluetun_manifest_path", DEFAULT_GLUETUN_MANIFEST_PATH
            ),
        )


    def snapshot(self) -> tuple[Server, ...]:
        return self._servers

    @property
    def last_refresh_at(self) -> float | None:
        return self._last_refresh

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def last_resolution(self) -> CatalogResolution | None:
        return self._last_resolution

    @property
    def source_id(self) -> str | None:
        return self._last_resolution.source_id if self._last_resolution else None

    @property
    def catalog_fetched_at(self) -> float | None:
        return self._last_resolution.fetched_at if self._last_resolution else None

    @property
    def etag(self) -> str | None:
        return self._last_resolution.etag if self._last_resolution else None

    @property
    def stale(self) -> bool:
        """True when the served snapshot is not a fresh primary resolution."""

        if self._last_resolution is None:
            return True
        return bool(self._last_refresh_failed or self._last_resolution.stale)

    def is_stale(self, now: float | None = None) -> bool:
        if self._last_refresh is None:
            return True
        if self._last_refresh_failed:
            return True
        return (now if now is not None else self._clock()) - self._last_refresh >= self._ttl_s

    async def refresh(self) -> tuple[Server, ...]:
        resolution = await self._sources.resolve()
        if resolution.servers:
            self._servers = resolution.servers
            self._last_refresh = self._clock()
            self._last_resolution = resolution
            self._last_refresh_failed = False
            self._last_error = "; ".join(resolution.errors) or None
        else:
            self._last_refresh_failed = True
            if resolution.errors:
                self._last_error = "; ".join(resolution.errors)
        return self._servers

    # ── backwards-compatible pure parsers ────────────────────────────────

    @staticmethod
    def _parse(raw: Any) -> list[Server]:
        return parse_official_payload(raw)

    @staticmethod
    def _parse_gluetun(raw: Any) -> list[Server]:
        return parse_gluetun_payload(raw)

    @staticmethod
    def _merge(api_servers: list[Server], extra_servers: list[Server]) -> list[Server]:
        """Merge embedded servers under the API snapshot (API wins on duplicates)."""

        merged: dict[str, Server] = {}
        for server in extra_servers:
            merged.setdefault(server.hostname, server)
        for server in api_servers:
            merged[server.hostname] = server
        return list(merged.values())
