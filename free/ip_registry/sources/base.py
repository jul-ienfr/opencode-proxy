"""Common types for the multi-source server catalog.

A source is a *reader*, never a selector: adapters only fetch and parse.  The
resolution policy (primary, fallbacks, completeness sources, stale marking)
lives in :mod:`free.ip_registry.sources.registry`, so adding a provider means
adding one adapter and one manifest entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..domain import Server


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """One fetch result, including provenance and failure state.

    ``stale`` is set by the adapter when the payload itself is old (static
    catalog, cache older than its TTL) and by the resolver when the source is
    used as a fallback.  ``complete`` says whether the payload can be treated
    as the full catalog: a partial snapshot must never deactivate servers.
    """

    source_id: str
    servers: tuple[Server, ...] = ()
    fetched_at: float = 0.0
    stale: bool = False
    etag: str | None = None
    error: str | None = None
    complete: bool = True

    @property
    def ok(self) -> bool:
        return bool(self.servers)


@dataclass(frozen=True, slots=True)
class CatalogResolution:
    """Outcome of the primary → fallback → completeness pipeline."""

    servers: tuple[Server, ...] = ()
    source_id: str = ""
    fetched_at: float = 0.0
    stale: bool = True
    etag: str | None = None
    errors: tuple[str, ...] = field(default_factory=tuple)
    primary_used: bool = False
    fallback_used: bool = False
    supplementary: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return bool(self.servers)

    @property
    def error(self) -> str | None:
        """Aggregate errors, shaped like :class:`SourceSnapshot` for the DB."""

        return "; ".join(self.errors) or None

    @property
    def complete(self) -> bool:
        """A primary, fresh resolution may retire servers missing from it."""

        return bool(self.primary_used and not self.stale)


class CatalogSource(Protocol):
    """A fetchable catalog source."""

    source_id: str

    async def fetch(self) -> SourceSnapshot: ...
