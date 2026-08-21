"""
NordVPN API Client with caching.

Provides server discovery, recommendations, and .ovpn config download
from the public NordVPN API. Caches responses to avoid redundant calls.

Based on patterns from:
- openpyn-nordvpn (Joty Gill) - API integration, server selection
- vpn-profile-switcher (UriShX) - .ovpn download, recommendation filtering
"""

import os
import time
import asyncio
import logging
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# NordVPN public API endpoints
API_BASE = "https://api.nordvpn.com"
API_SERVERS = f"{API_BASE}/v1/servers"
API_RECOMMENDATIONS = f"{API_BASE}/v1/servers/recommendations"
API_TECHNOLOGIES = f"{API_BASE}/v1/technologies"

# Cache TTL in seconds
DEFAULT_CACHE_TTL = 900  # 15 minutes


@dataclass
class ServerInfo:
    """Parsed NordVPN server information."""
    id: int
    name: str  # e.g. "de1227"
    hostname: str  # e.g. "de1227.nordvpn.com"
    ip_address: str
    country: str
    country_code: str
    city: str
    load: int  # 0-100
    tags: list[str] = field(default_factory=list)
    protocols: list[str] = field(default_factory=list)
    openvpn_udp: bool = False
    openvpn_tcp: bool = False


class NordVPNCache:
    """Simple TTL cache for API responses."""

    def __init__(self, ttl: int = DEFAULT_CACHE_TTL):
        self._ttl = ttl
        self._store: dict[str, tuple[float, any]] = {}

    def get(self, key: str):
        if key in self._store:
            ts, data = self._store[key]
            if time.monotonic() - ts < self._ttl:
                return data
            del self._store[key]
        return None

    def set(self, key: str, data):
        self._store[key] = (time.monotonic(), data)

    def invalidate(self, key: str = None):
        if key:
            self._store.pop(key, None)
        else:
            self._store.clear()


class NordVPNClient:
    """Async client for the NordVPN public API.

    Features:
    - Server recommendations by country/group/protocol
    - .ovpn config download
    - Server list with load/latency info
    - Response caching with configurable TTL
    """

    def __init__(self, cache_ttl: int = DEFAULT_CACHE_TTL):
        self._cache = NordVPNCache(ttl=cache_ttl)
        self._http_client = None

    async def _get_client(self):
        """Get or create shared HTTP client."""
        if self._http_client is None or self._http_client.is_closed:
            import httpx
            self._http_client = httpx.AsyncClient(
                timeout=15,
                headers={"User-Agent": "OpenCode-Proxy/1.0"},
            )
        return self._http_client

    async def close(self):
        """Close the HTTP client."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    async def get_recommendations(
        self,
        country_code: str = None,
        group: str = None,
        protocol: str = "openvpn_udp",
        limit: int = 5,
    ) -> list[ServerInfo]:
        """Get recommended servers from NordVPN API.

        Args:
            country_code: 2-letter country code (e.g. "DE", "FR", "US")
            group: server group identifier (e.g. "legacy_p2p", "standard_vpn")
            protocol: "openvpn_udp" or "openvpn_tcp"
            limit: max servers to return

        Returns:
            List of ServerInfo objects, sorted by load (lowest first)
        """
        cache_key = f"recs:{country_code}:{group}:{protocol}:{limit}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        # Build filters
        filters = [f"filters[servers_technologies][identifier]={protocol}"]
        if country_code:
            # Need country ID, not code - fetch it
            country_id = await self._get_country_id(country_code)
            if country_id:
                filters.append(f"filters[country_id]={country_id}")
        if group:
            filters.append(f"filters[servers_groups][identifier]={group}")
        filters.append(f"limit={limit}")

        url = f"{API_RECOMMENDATIONS}?{'&'.join(filters)}"

        try:
            client = await self._get_client()
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

            servers = [self._parse_server(s) for s in data]
            servers = [s for s in servers if s]  # filter None

            self._cache.set(cache_key, servers)
            logger.info("[nordvpn-api] got %d recommendations for %s",
                        len(servers), country_code or "any")
            return servers

        except Exception as e:
            logger.error("[nordvpn-api] recommendations failed: %s", e)
            return []

    async def download_ovpn_config(self, server: ServerInfo, dest_dir: str) -> Optional[str]:
        """Download .ovpn config file for a server.

        Returns the path to the downloaded file, or None on failure.
        """
        protocol = "udp" if server.openvpn_udp else "tcp"
        filename = f"{server.name}.ovpn"
        dest_path = os.path.join(dest_dir, filename)

        # Check if already downloaded
        if os.path.exists(dest_path):
            return dest_path

        # NordVPN .ovpn download URL pattern
        url = f"https://downloads.nordcdn.com/configs/files/ovpn_{protocol}/{server.hostname}.ovpn"

        try:
            client = await self._get_client()
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()

            os.makedirs(dest_dir, exist_ok=True)
            with open(dest_path, "w") as f:
                f.write(resp.text)

            logger.info("[nordvpn-api] downloaded .ovpn: %s", filename)
            return dest_path

        except Exception as e:
            logger.error("[nordvpn-api] .ovpn download failed for %s: %s", server.name, e)
            return None

    async def get_countries(self) -> list[dict]:
        """Get list of available countries.

        Returns list of {id, code, name} dicts.
        """
        cache_key = "countries"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        try:
            client = await self._get_client()
            resp = await client.get(f"{API_BASE}/v1/countries")
            resp.raise_for_status()
            data = resp.json()

            countries = [
                {"id": c["id"], "code": c["code"], "name": c["name"]}
                for c in data
            ]
            self._cache.set(cache_key, countries)
            return countries

        except Exception as e:
            logger.error("[nordvpn-api] countries fetch failed: %s", e)
            return []

    async def _get_country_id(self, code: str) -> Optional[int]:
        """Get numeric country ID from 2-letter code."""
        countries = await self.get_countries()
        for c in countries:
            if c["code"].upper() == code.upper():
                return c["id"]
        return None

    def _parse_server(self, data: dict) -> Optional[ServerInfo]:
        """Parse raw API server data into ServerInfo."""
        try:
            server = data.get("station", {})
            hostname = server.get("hostname", "")
            name = hostname.split(".")[0] if hostname else ""

            # Extract technologies/protocols
            technologies = data.get("technologies", [])
            protocols = [t.get("identifier", "") for t in technologies]
            openvpn_udp = "openvpn_udp" in protocols
            openvpn_tcp = "openvpn_tcp" in protocols

            # Extract country/city
            country = data.get("locations", [{}])[0].get("country", {})
            city = data.get("locations", [{}])[0].get("city", {})

            return ServerInfo(
                id=data.get("id", 0),
                name=name,
                hostname=hostname,
                ip_address=server.get("ip_address", ""),
                country=country.get("name", ""),
                country_code=country.get("code", ""),
                city=city.get("name", ""),
                load=data.get("load", 0),
                tags=[t.get("label", "") for t in data.get("groups", [])],
                protocols=protocols,
                openvpn_udp=openvpn_udp,
                openvpn_tcp=openvpn_tcp,
            )
        except Exception as e:
            logger.debug("[nordvpn-api] failed to parse server: %s", e)
            return None


# Singleton instance
_client: Optional[NordVPNClient] = None


def get_nordvpn_client(cache_ttl: int = DEFAULT_CACHE_TTL) -> NordVPNClient:
    """Get or create the singleton NordVPN API client."""
    global _client
    if _client is None:
        _client = NordVPNClient(cache_ttl=cache_ttl)
    return _client
