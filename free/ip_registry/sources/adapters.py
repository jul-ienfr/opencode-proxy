"""Concrete catalog adapters.

Every adapter returns a :class:`SourceSnapshot` instead of raising: a source
being down is an expected state, not an exception that can stop the catalog.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .base import SourceSnapshot
from .parsers import parse_gluetun_payload, parse_official_payload

DEFAULT_API_URL = "https://api.nordvpn.com/v1/servers?limit=0"


class OfficialApiSource:
    """Primary source: ``GET /v1/servers`` from the NordVPN API."""

    source_id = "nordvpn_api_servers"

    def __init__(
        self,
        *,
        url: str = DEFAULT_API_URL,
        fetch: Callable[[], Any] | None = None,
        timeout_s: float = 120.0,
        clock: Callable[[], float] = time.time,
        complete: bool = True,
    ) -> None:
        self._url = url
        self._fetch = fetch
        self._timeout_s = max(1.0, float(timeout_s))
        self._clock = clock
        self._complete = complete

    async def fetch(self) -> SourceSnapshot:
        now = self._clock()
        try:
            raw, etag = await asyncio.to_thread(self._load)
            servers = parse_official_payload(raw)
        except Exception as exc:
            return SourceSnapshot(
                source_id=self.source_id,
                fetched_at=now,
                stale=True,
                error=f"{type(exc).__name__}: {exc}",
                complete=self._complete,
            )
        return SourceSnapshot(
            source_id=self.source_id,
            servers=tuple(servers),
            fetched_at=now,
            stale=False,
            etag=etag,
            complete=self._complete,
        )

    def _load(self) -> tuple[Any, str | None]:
        if self._fetch is not None:
            return self._fetch(), None
        import requests

        response = requests.get(self._url, timeout=self._timeout_s)
        response.raise_for_status()
        return response.json(), response.headers.get("ETag")


class JsonCatalogSource:
    """Read a JSON catalog from disk (gluetun manifest, embedded, cache).

    ``always_stale`` is for static artifacts shipped with an image: they stay
    usable, but every row must be labelled as not fresh.  ``max_age_s`` marks
    a cache stale once its file is older than the TTL.
    """

    def __init__(
        self,
        *,
        source_id: str,
        path: str | Path,
        parser: Callable[[Any], list[Any]] = parse_gluetun_payload,
        always_stale: bool = False,
        max_age_s: float | None = None,
        complete: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.source_id = str(source_id)
        self._path = Path(path)
        self._parser = parser
        self._always_stale = bool(always_stale)
        self._max_age_s = max_age_s
        self._complete = bool(complete)
        self._clock = clock

    async def fetch(self) -> SourceSnapshot:
        now = self._clock()
        if not self._path.exists():
            return SourceSnapshot(
                source_id=self.source_id,
                fetched_at=now,
                stale=True,
                error=f"missing file: {self._path}",
                complete=self._complete,
            )
        try:
            servers, mtime = await asyncio.to_thread(self._read)
        except Exception as exc:
            return SourceSnapshot(
                source_id=self.source_id,
                fetched_at=now,
                stale=True,
                error=f"{type(exc).__name__}: {exc}",
                complete=self._complete,
            )
        stale = self._always_stale
        if self._max_age_s is not None and now - mtime > self._max_age_s:
            stale = True
        return SourceSnapshot(
            source_id=self.source_id,
            servers=tuple(servers),
            fetched_at=mtime,
            stale=stale,
            complete=self._complete,
        )

    def _read(self) -> tuple[list[Any], float]:
        with open(self._path, encoding="utf-8") as handle:
            raw = json.load(handle)
        return self._parser(raw), self._path.stat().st_mtime
