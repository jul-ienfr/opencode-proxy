"""Free threat-intel feeds applied before any public-proxy connection.

The feeds are plain-text lists (abuse.ch, blocklist.de, ipsum, Spamhaus DROP).
Parsing accepts IPs, ``ip:port``, CIDR and URLs.  Refreshes are per source and
fail-soft: a feed that is down keeps its previous indicators.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from free.egress_sources import fetch_text
from free.egress_store import EgressStore

LOGGER = logging.getLogger("egress-catalog")


@dataclass(frozen=True, slots=True)
class ThreatFeed:
    id: str
    url: str
    enabled: bool = True


@dataclass
class ThreatStats:
    exact: int = 0
    networks: int = 0
    feeds: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"exact": self.exact, "networks": self.networks, "feeds": self.feeds}


def parse_feed_config(raw: Any) -> list[ThreatFeed]:
    rows = raw if isinstance(raw, list) else []
    feeds: list[ThreatFeed] = []
    for item in rows:
        if isinstance(item, str):
            url = item.strip()
            if url:
                feeds.append(ThreatFeed(id=url, url=url))
            continue
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        feeds.append(
            ThreatFeed(
                id=str(item.get("id") or url).strip(),
                url=url,
                enabled=bool(item.get("enabled", True)),
            )
        )
    return feeds


def _public_indicator(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def parse_indicators(text: str) -> tuple[set[str], set[str]]:
    """Return ``(exact_ips, cidr_networks)`` from a plain-text feed."""

    ips: set[str] = set()
    networks: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        # URLs (URLhaus) -> host; CSV/TSV/space lists -> first token.
        if "://" in line:
            try:
                host = (urllib.parse.urlsplit(line.split()[0]).hostname or "").strip()
            except ValueError:
                continue
            if host and _public_indicator(host):
                ips.add(str(ipaddress.ip_address(host)))
            continue
        token = line.replace("\t", " ").replace(",", " ").replace(";", " ").split()[0]
        token = token.strip().strip("\"'")
        if not token:
            continue
        if token.count(":") == 1 and token.rsplit(":", 1)[1].isdigit():
            token = token.rsplit(":", 1)[0]
        try:
            network = ipaddress.ip_network(token, strict=False)
        except ValueError:
            continue
        if not _public_indicator(str(network.network_address)):
            continue
        if network.prefixlen == network.max_prefixlen:
            ips.add(str(network.network_address))
        else:
            networks.add(str(network))
    return ips, networks


class ThreatIntel:
    """Cached indicator set backed by ``registry.db``."""

    def __init__(
        self,
        store: EgressStore,
        feeds: Iterable[ThreatFeed] | None = None,
        *,
        refresh_interval_s: float = 21_600.0,
        fetcher: Callable[[str, float], str] | None = None,
    ) -> None:
        self.store = store
        self.feeds = [feed for feed in (feeds or []) if feed.enabled]
        self.refresh_interval_s = max(300.0, float(refresh_interval_s))
        self._fetcher = fetcher
        self._exact: set[str] = set()
        self._networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    def load(self, *, force: bool = False) -> None:
        if not force and self._loaded_at and time.time() - self._loaded_at < 300.0:
            return
        exact: set[str] = set()
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for row in self.store.threat_indicators():
            value = str(row.get("indicator") or "")
            if str(row.get("kind")) == "ip":
                if _public_indicator(value):
                    exact.add(value)
                continue
            try:
                network = ipaddress.ip_network(value, strict=False)
            except ValueError:
                continue
            if _public_indicator(str(network.network_address)):
                networks.append(network)
        self._exact = exact
        self._networks = networks
        self._loaded_at = time.time()

    def matches(self, host: str) -> bool:
        try:
            address = ipaddress.ip_address(str(host or "").strip())
        except ValueError:
            return False
        self.load()
        if str(address) in self._exact:
            return True
        return any(address in network for network in self._networks)

    def refresh_once(self, *, fetcher: Callable[[str, float], str] | None = None) -> dict[str, Any]:
        fetcher = fetcher or self._fetcher
        counts: dict[str, int] = {}
        errors: list[str] = []
        for feed in self.feeds:
            try:
                text = fetch_text(feed.url, fetcher=fetcher)
                ips, networks = parse_indicators(text)
                counts[feed.id] = self.store.replace_threat_indicators(
                    feed.id,
                    feed.url,
                    ips=ips,
                    networks=networks,
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:300]
                counts[feed.id] = 0
                errors.append(f"{feed.id}: {message}")
                self.store.record_threat_feed_error(feed.id, feed.url, message)
        self.store.prune_threat_feeds(feed.id for feed in self.feeds)
        self.load(force=True)
        return {"counts": counts, "errors": errors, "stats": self.stats()}

    async def run_forever(self) -> None:
        while True:
            async with self._lock:
                result = await asyncio.to_thread(self.refresh_once)
            if result["errors"]:
                LOGGER.warning("threat feeds: %s", "; ".join(result["errors"])[:500])
            else:
                LOGGER.info("threat feeds: %s", result["counts"])
            await asyncio.sleep(self.refresh_interval_s)

    def stats(self) -> dict[str, Any]:
        self.load()
        return ThreatStats(
            exact=len(self._exact),
            networks=len(self._networks),
            feeds=self.store.threat_feeds(),
        ).as_dict()


__all__ = ["ThreatFeed", "ThreatIntel", "ThreatStats", "parse_feed_config", "parse_indicators"]
