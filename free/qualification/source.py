"""Catalog source for the qualification queue.

Reads the NordVPN API snapshot (``logs/nordvpn_api.json``) and turns online
servers into candidates.  It never performs network I/O; the caller decides
when to refresh the snapshot.  Embedded/legacy rows are deliberately not
imported here: the API is the live catalog authority.
"""

from __future__ import annotations

import ipaddress
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import Candidate, CandidateStatus
from .scoring import PotentialWeights, potential_score
from .transport import DEFAULT_TRANSPORT_ORDER

TRANSPORT_IDS = {
    "wireguard_udp": "wireguard",
    "openvpn_udp": "openvpn-udp",
    "openvpn_tcp": "openvpn-tcp",
}
DEFAULT_API_CACHE = Path("logs") / "nordvpn_api.json"


@dataclass(frozen=True, slots=True)
class CatalogStats:
    total: int = 0
    online: int = 0
    skipped: int = 0
    with_transports: int = 0
    countries: int = 0
    source_id: str = ""


class NordVpnCatalogSource:
    """Pure parser + queue upsert for the live catalog snapshot."""

    source_id = "nordvpn_api_servers"

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock=time.time,
        weights: PotentialWeights | None = None,
        denylist: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.path = Path(path) if path else DEFAULT_API_CACHE
        self._clock = clock
        self._weights = weights or PotentialWeights()
        self._denied: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for raw in denylist or ():
            try:
                self._denied.append(
                    ipaddress.ip_network(str(raw).strip(), strict=False)
                )
            except ValueError:
                continue

    def load(self, path: str | Path | None = None) -> list[dict[str, Any]]:
        target = Path(path) if path else self.path
        raw = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            rows = raw.get("servers") or []
        elif isinstance(raw, list):
            rows = raw
        else:
            rows = []
        return [dict(row) for row in rows if isinstance(row, dict)]

    def candidates(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        now: float | None = None,
    ) -> list[Candidate]:
        ts = float(self._clock() if now is None else now)
        source_rows = rows if rows is not None else self.load()
        seen: set[str] = set()
        out: list[Candidate] = []
        for row in source_rows:
            candidate = self._candidate(row, ts)
            if candidate is None:
                continue
            key = candidate.hostname.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
        return out

    def _candidate(self, row: dict[str, Any], now: float) -> Candidate | None:
        hostname = str(row.get("hostname") or "").strip().lower()
        if not hostname:
            return None
        status = str(row.get("status") or "").strip().lower()
        if status and status != "online":
            return None
        transports = self.transports(row)
        country, city = self.location(row)
        try:
            load = float(row.get("load") or 0.0)
        except (TypeError, ValueError):
            load = 0.0
        skip_reason = ""
        if self.is_dedicated(row):
            skip_reason = "dedicated"
        elif self.is_denylisted(row):
            skip_reason = "denylisted"
        elif not transports:
            skip_reason = "unsupported-transport"
        candidate = Candidate(
            hostname=hostname,
            provider="nordvpn",
            country=country,
            city=city,
            load=max(0.0, load),
            status=CandidateStatus.PENDING if not skip_reason else CandidateStatus.SKIPPED,
            transports=transports,
            skipped_reason=skip_reason,
            source_id=self.source_id,
            first_seen_at=now,
            last_seen_at=now,
            updated_at=now,
        )
        return candidate.with_updates(
            potential_score=potential_score(candidate, now=now, weights=self._weights)
        )

    @staticmethod
    def is_dedicated(row: dict[str, Any]) -> bool:
        for group in row.get("groups") or []:
            if not isinstance(group, dict):
                continue
            if str(group.get("identifier") or "").strip().lower() == "legacy_dedicated_ip":
                return True
        return False

    def is_denylisted(self, row: dict[str, Any]) -> bool:
        if not self._denied:
            return False
        values = row.get("ips")
        if not isinstance(values, list):
            values = [values] if values else []
        for value in values:
            try:
                address = ipaddress.ip_address(str(value).strip())
            except ValueError:
                continue
            if any(address in network for network in self._denied):
                return True
        return False

    @staticmethod
    def transports(row: dict[str, Any]) -> tuple[str, ...]:
        found: set[str] = set()
        for tech in row.get("technologies") or []:
            if not isinstance(tech, dict):
                continue
            identifier = str(tech.get("identifier") or "").strip().lower()
            mapped = TRANSPORT_IDS.get(identifier)
            if mapped:
                found.add(mapped)
        return tuple(item for item in DEFAULT_TRANSPORT_ORDER if item in found)

    @staticmethod
    def location(row: dict[str, Any]) -> tuple[str, str]:
        locations = row.get("locations") or []
        if not locations or not isinstance(locations[0], dict):
            return "", ""
        first: dict[str, Any] = locations[0]
        country_raw = first.get("country")
        country_obj: dict[str, Any] = country_raw if isinstance(country_raw, dict) else {}
        city_raw = country_obj.get("city")
        city_obj: dict[str, Any] = city_raw if isinstance(city_raw, dict) else {}
        return str(country_obj.get("name") or ""), str(city_obj.get("name") or "")

    def ingest(
        self,
        store,
        rows: list[dict[str, Any]] | None = None,
        *,
        now: float | None = None,
    ) -> int:
        candidates = self.candidates(rows, now=now)
        count = int(store.upsert_candidates(candidates, now=now))
        store.upsert_server_transports(candidates)
        return count

    def stats(self, rows: list[dict[str, Any]] | None = None) -> CatalogStats:
        source_rows = rows if rows is not None else self.load()
        online = [row for row in source_rows if str(row.get("status") or "").lower() == "online"]
        with_transports = sum(1 for row in online if self.transports(row))
        countries = {self.location(row)[0] for row in online if self.location(row)[0]}
        return CatalogStats(
            total=len(source_rows),
            online=len(online),
            skipped=len(source_rows) - len(online),
            with_transports=with_transports,
            countries=len(countries),
            source_id=self.source_id,
        )


__all__ = ["CatalogStats", "DEFAULT_API_CACHE", "NordVpnCatalogSource", "TRANSPORT_IDS"]
