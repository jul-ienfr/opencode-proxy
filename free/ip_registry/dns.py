"""DNS fallback policy for a station.

DoT is the primary runtime resolver. When a station keeps failing to produce
egress (the signature of a DNS/healthcheck loop), the next bind recreates the
container with the next resolver mode (plain UDP). A successful bind resets the
station to the primary mode.
"""

from __future__ import annotations

from typing import Iterable


class DnsFallback:
    def __init__(
        self,
        *,
        order: Iterable[str] = ("dot", "plain"),
        failures_before_fallback: int = 2,
    ) -> None:
        modes = tuple(
            str(mode).strip().lower()
            for mode in order
            if str(mode).strip().lower() in ("dot", "doh", "plain")
        )
        self._order = modes or ("dot", "plain")
        self._threshold = max(1, int(failures_before_fallback))
        self._failures: dict[int, int] = {}
        self._index: dict[int, int] = {}

    @property
    def order(self) -> tuple[str, ...]:
        return self._order

    def mode(self, station: int) -> str:
        index = max(0, min(self._index.get(int(station), 0), len(self._order) - 1))
        return self._order[index]

    def note_failure(self, station: int, error: str = "") -> str:
        station = int(station)
        count = self._failures.get(station, 0) + 1
        self._failures[station] = count
        if count >= self._threshold:
            self._failures[station] = 0
            index = min(self._index.get(station, 0) + 1, len(self._order) - 1)
            self._index[station] = index
        return self.mode(station)

    def reset(self, station: int) -> None:
        station = int(station)
        self._failures.pop(station, None)
        self._index.pop(station, None)
