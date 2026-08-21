"""
Server scoring and selection for NordVPN.

Implements intelligent server selection based on:
- Latency (ping time)
- Server load (from NordVPN API)
- Freshness (time since last use)
- Country/city preferences

Based on patterns from:
- openpyn-nordvpn - latency-based selection, load filtering
- nordvpn-switcher-pro - smart caching, criteria-based rotation
"""

import time
import random
import logging
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ServerScore:
    """Scored server with selection metadata."""
    name: str
    hostname: str
    country_code: str
    city: str
    load: int  # 0-100 from NordVPN API
    latency_ms: Optional[int] = None  # measured ping
    last_used: Optional[float] = None  # timestamp
    consecutive_failures: int = 0
    total_uses: int = 0

    @property
    def score(self) -> float:
        """Combined score (lower is better).

        Weighted formula:
        - Load: 40% weight (0-100 scale)
        - Latency: 30% weight (normalized 0-100)
        - Freshness: 20% weight (0-100, newer is better)
        - Failure penalty: 10% weight (0-100, fewer is better)
        """
        load_score = self.load  # 0-100

        # Normalize latency to 0-100 (assume 500ms = worst)
        if self.latency_ms is not None:
            latency_score = min(100, self.latency_ms / 5)
        else:
            latency_score = 50  # unknown = middle

        # Freshness: 0 = just used, 100 = never used or very old
        if self.last_used is None:
            freshness_score = 100
        else:
            hours_since = (time.monotonic() - self.last_used) / 3600
            freshness_score = min(100, hours_since * 10)  # 10 points per hour

        # Failure penalty: 0 = no failures, 100 = many failures
        failure_score = min(100, self.consecutive_failures * 33)

        return (
            load_score * 0.40 +
            latency_score * 0.30 +
            freshness_score * 0.20 +
            failure_score * 0.10
        )


class ServerScorer:
    """Manages server scoring and selection.

    Maintains state about server usage, failures, and latency
    to make intelligent selection decisions.
    """

    def __init__(self):
        self._servers: dict[str, ServerScore] = {}  # name -> ServerScore
        self._preferred_countries: list[str] = []
        self._excluded_countries: list[str] = []

    def set_preferences(
        self,
        preferred_countries: list[str] = None,
        excluded_countries: list[str] = None,
    ):
        """Set country preferences for server selection."""
        self._preferred_countries = [c.upper() for c in (preferred_countries or [])]
        self._excluded_countries = [c.upper() for c in (excluded_countries or [])]

    def update_server(
        self,
        name: str,
        hostname: str = "",
        country_code: str = "",
        city: str = "",
        load: int = 50,
        latency_ms: int = None,
    ):
        """Update or add server info."""
        if name in self._servers:
            s = self._servers[name]
            if hostname:
                s.hostname = hostname
            if country_code:
                s.country_code = country_code.upper()
            if city:
                s.city = city
            s.load = load
            if latency_ms is not None:
                s.latency_ms = latency_ms
        else:
            self._servers[name] = ServerScore(
                name=name,
                hostname=hostname,
                country_code=country_code.upper(),
                city=city,
                load=load,
                latency_ms=latency_ms,
            )

    def record_use(self, name: str):
        """Record that a server was just used."""
        if name in self._servers:
            self._servers[name].last_used = time.monotonic()
            self._servers[name].total_uses += 1

    def record_failure(self, name: str):
        """Record a connection failure."""
        if name in self._servers:
            self._servers[name].consecutive_failures += 1

    def record_success(self, name: str):
        """Record a connection success (reset failure count)."""
        if name in self._servers:
            self._servers[name].consecutive_failures = 0

    def select_best(self, exclude_recent: int = 3) -> Optional[str]:
        """Select the best available server.

        Args:
            exclude_recent: number of most recently used servers to exclude

        Returns:
            Server name, or None if no servers available
        """
        if not self._servers:
            return None

        # Filter out excluded countries
        candidates = [
            s for s in self._servers.values()
            if s.country_code not in self._excluded_countries
        ]

        if not candidates:
            # Fallback: ignore exclusions
            candidates = list(self._servers.values())

        # Sort by last_used to find recently used
        candidates.sort(key=lambda s: s.last_used or 0, reverse=True)
        recent_names = {s.name for s in candidates[:exclude_recent]}

        # Exclude recently used
        available = [s for s in candidates if s.name not in recent_names]

        if not available:
            # All recently used, fall back to full list
            available = candidates

        # Boost score for preferred countries
        for s in available:
            if s.country_code in self._preferred_countries:
                # Reduce score (better) by 20%
                pass  # score is property, we'll handle in sorting

        # Sort by score (lower is better)
        # Apply country preference boost
        def sort_key(s):
            base = s.score
            if s.country_code in self._preferred_countries:
                base *= 0.8  # 20% boost for preferred
            return base

        available.sort(key=sort_key)

        # Add some randomness: pick from top 3
        top_n = available[:min(3, len(available))]
        chosen = random.choice(top_n)

        logger.info("[scorer] selected %s (score: %.1f, load: %d, country: %s)",
                    chosen.name, chosen.score, chosen.load, chosen.country_code)
        return chosen.name

    def get_random_from_pool(self, pool_size: int = 10) -> Optional[str]:
        """Get a random server from the top N by score.

        Used for "complete rotation" mode where we want diversity.
        """
        if not self._servers:
            return None

        candidates = [
            s for s in self._servers.values()
            if s.country_code not in self._excluded_countries
        ]

        if not candidates:
            candidates = list(self._servers.values())

        candidates.sort(key=lambda s: s.score)
        pool = candidates[:min(pool_size, len(candidates))]
        chosen = random.choice(pool)

        return chosen.name

    def get_status(self) -> dict:
        """Return scorer status for dashboard."""
        servers = sorted(self._servers.values(), key=lambda s: s.score)
        return {
            "total_servers": len(self._servers),
            "preferred_countries": self._preferred_countries,
            "excluded_countries": self._excluded_countries,
            "top_5": [
                {
                    "name": s.name,
                    "country": s.country_code,
                    "city": s.city,
                    "load": s.load,
                    "latency_ms": s.latency_ms,
                    "score": round(s.score, 1),
                    "uses": s.total_uses,
                    "failures": s.consecutive_failures,
                }
                for s in servers[:5]
            ],
        }
