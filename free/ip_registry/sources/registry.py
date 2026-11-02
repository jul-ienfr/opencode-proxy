"""Resolution policy for the multi-source catalog.

Order is explicit and data-driven: the manifest's ``catalog_primary`` wins,
then ``catalog_fallbacks`` in order, then ``hostname_completeness`` sources
are merged *under* the winner.  A fallback is never silent: the resolution is
flagged ``stale`` and every row carries its own ``source_id``/``stale``.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..domain import Server
from .adapters import DEFAULT_API_URL, JsonCatalogSource, OfficialApiSource
from .base import CatalogResolution, CatalogSource, SourceSnapshot
from .manifest import DEFAULT_MANIFEST_PATH, SourceManifest
from .parsers import parse_gluetun_payload, parse_official_payload, stamp_servers

DEFAULT_EMBEDDED_PATH = Path(__file__).resolve().parents[3] / "logs" / "gluetun_nordvpn_embedded.json"
DEFAULT_CACHE_PATH = Path(__file__).resolve().parents[3] / "logs" / "nordvpn_api.json"
DEFAULT_GLUETUN_MANIFEST_PATH = Path(__file__).resolve().parents[3] / "logs" / "gluetun_manifest.json"


class SourceRegistry:
    """Holds adapters and resolves one catalog snapshot."""

    def __init__(
        self,
        *,
        manifest: SourceManifest | None = None,
        adapters: Iterable[CatalogSource] | None = None,
    ) -> None:
        self._manifest = manifest
        self._adapters: dict[str, CatalogSource] = {}
        for adapter in adapters or ():
            self.register(adapter)

    @property
    def manifest(self) -> SourceManifest | None:
        return self._manifest

    def register(self, adapter: CatalogSource) -> None:
        self._adapters[str(adapter.source_id)] = adapter

    def adapter(self, source_id: str) -> CatalogSource | None:
        return self._adapters.get(source_id)

    def capabilities(self) -> dict[str, bool]:
        """Which manifest sources have a real adapter in this checkout."""

        if self._manifest is not None:
            ids = sorted(self._manifest.sources)
        else:
            ids = sorted(self._adapters)
        return {source_id: source_id in self._adapters for source_id in ids}

    def catalog_order(self) -> tuple[str, ...]:
        if self._manifest is not None:
            order = self._manifest.catalog_order()
            if order:
                return order
        primary = "nordvpn_api_servers"
        if primary in self._adapters:
            return (primary,)
        return tuple(self._adapters)

    def completeness_order(self) -> tuple[str, ...]:
        if self._manifest is not None:
            return self._manifest.completeness_order()
        return ()

    async def resolve(self) -> CatalogResolution:
        order = self.catalog_order()
        errors: list[str] = []
        base: SourceSnapshot | None = None
        base_index = -1

        for index, source_id in enumerate(order):
            adapter = self._adapters.get(source_id)
            if adapter is None:
                errors.append(f"no-adapter:{source_id}")
                continue
            snapshot = await self._safe_fetch(adapter, errors)
            if snapshot is None:
                continue
            if snapshot.servers:
                base = snapshot
                base_index = index
                break
            errors.append(f"{source_id}: {snapshot.error or 'empty'}")

        if base is None:
            return CatalogResolution(
                source_id=order[0] if order else "",
                fetched_at=time.time(),
                stale=True,
                errors=tuple(errors),
            )

        stale = bool(base.stale or base_index > 0)
        merged: dict[str, Server] = {
            server.hostname: server
            for server in stamp_servers(
                base.servers,
                source_id=base.source_id,
                stale=stale,
                fetched_at=base.fetched_at,
            )
        }
        supplementary: list[str] = []
        for source_id in self.completeness_order():
            if source_id == base.source_id:
                continue
            adapter = self._adapters.get(source_id)
            if adapter is None:
                # Optional completeness sources may be unavailable by design;
                # they must not pollute the resolution errors.
                continue
            snapshot = await self._safe_fetch(adapter, errors)
            if snapshot is None or not snapshot.servers:
                if snapshot is not None and snapshot.error:
                    errors.append(f"{source_id}: {snapshot.error}")
                continue
            supplementary.append(source_id)
            for server in stamp_servers(
                snapshot.servers,
                source_id=source_id,
                stale=True,
                fetched_at=snapshot.fetched_at,
            ):
                merged.setdefault(server.hostname, server)

        return CatalogResolution(
            servers=tuple(merged.values()),
            source_id=base.source_id,
            fetched_at=base.fetched_at,
            stale=stale,
            etag=base.etag,
            errors=tuple(errors),
            primary_used=base_index == 0,
            fallback_used=base_index > 0,
            supplementary=tuple(supplementary),
        )

    @staticmethod
    async def _safe_fetch(
        adapter: CatalogSource,
        errors: list[str],
    ) -> SourceSnapshot | None:
        try:
            return await adapter.fetch()
        except Exception as exc:  # noqa: BLE001 - a source must never stop the chain
            errors.append(f"{adapter.source_id}: {type(exc).__name__}: {exc}")
            return None


def build_default_registry(
    *,
    manifest_path: str | Path | None = None,
    url: str = DEFAULT_API_URL,
    fetch: Any | None = None,
    extra_path: str | Path | None = DEFAULT_EMBEDDED_PATH,
    cache_path: str | Path | None = DEFAULT_CACHE_PATH,
    gluetun_manifest_path: str | Path | None = DEFAULT_GLUETUN_MANIFEST_PATH,
) -> SourceRegistry:
    """Wire the sources available in this checkout.

    Unimplemented sources in the manifest (for example the packed ``nordcache``
    decoder) stay declared but have no adapter: the resolver reports
    ``no-adapter:<id>`` when it would have been needed.
    """

    try:
        manifest = SourceManifest.load(manifest_path or DEFAULT_MANIFEST_PATH)
    except Exception:
        manifest = None

    registry = SourceRegistry(manifest=manifest)
    registry.register(OfficialApiSource(url=url, fetch=fetch))

    embedded = Path(extra_path) if extra_path else None
    if embedded is not None and embedded.exists():
        registry.register(
            JsonCatalogSource(
                source_id="gluetun_embedded_catalog",
                path=embedded,
                parser=parse_gluetun_payload,
                always_stale=True,
                complete=False,
            )
        )
    cache = Path(cache_path) if cache_path else None
    if cache is not None and cache.exists():
        registry.register(
            JsonCatalogSource(
                source_id="nordvpn_api_cache",
                path=cache,
                parser=parse_official_payload,
                max_age_s=21600.0,
            )
        )
    container_manifest = Path(gluetun_manifest_path) if gluetun_manifest_path else None
    if container_manifest is not None and container_manifest.exists():
        registry.register(
            JsonCatalogSource(
                source_id="gluetun_manifest",
                path=container_manifest,
                parser=parse_gluetun_payload,
                max_age_s=21600.0,
            )
        )
    return registry
