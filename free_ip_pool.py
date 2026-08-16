"""
Free IP pool for rotating IP addresses on free model requests.

Each VPN session gives a fresh IP = fresh free model quota.
Routes free requests through the compose-managed gluetun tunnel (SOCKS5).
"""

import time
import logging
import asyncio
from typing import Optional

from vpn_manager import VPNManager

logger = logging.getLogger(__name__)


class FreeIPPool:
    """Manages IP rotation for free model requests.

    Routes free model requests through the gluetun SOCKS5 tunnel
    (compose-managed Docker container).
    """

    _CONNECT_RETRY_INTERVAL = 300  # min seconds between docker reconnect attempts when down

    def __init__(self, vpn_manager: VPNManager):
        self._vpn = vpn_manager
        self._request_count = 0
        self._session_start: Optional[float] = None
        self._total_free_requests = 0
        self._ip_stats: dict[str, dict] = {}
        self._last_connect_attempt: Optional[float] = None

    @property
    def enabled(self) -> bool:
        return self._vpn.enabled

    @property
    def proxy_mode(self) -> str:
        """Return the current proxy mode: vpn, or direct."""
        return self._vpn.proxy_mode

    @property
    def proxy_url(self) -> Optional[str]:
        """Return the SOCKS5 proxy URL when the tunnel is up.

        Gluetun is the only backend (compose-managed Docker): free requests
        are routed via SOCKS5 (the HTTP proxy is not routed on Windows
        Docker Desktop). Returns None when not connected or disabled.
        """
        if not self._vpn.enabled or self._vpn.proxy_mode != "vpn":
            return None
        if self._vpn.status == "connected":
            return self._vpn.socks5_url
        return None

    async def ensure_connected(self):
        """Ensure VPN is connected. Connect to first server if not."""
        if not self._vpn.enabled:
            return
        if self._vpn.proxy_mode == "vpn" and self._vpn.status != "connected":
            # Cooldown: when the tunnel is down (e.g. AUTH_FAILED), a docker
            # reconnect can take minutes. Only try again every 5 minutes;
            # requests in between go direct immediately.
            now = time.monotonic()
            if self._last_connect_attempt and now - self._last_connect_attempt < self._CONNECT_RETRY_INTERVAL:
                return
            self._last_connect_attempt = now
            await self._vpn.connect_next()
            self._request_count = 0
            self._session_start = time.monotonic()

    async def on_request(self) -> Optional[str]:
        """Called before each free model request.

        Returns the proxy URL if ready, None if disabled.
        May switch IP/proxy if quota is exhausted.
        """
        if not self._vpn.enabled:
            return None

        await self.ensure_connected()

        self._request_count += 1
        self._total_free_requests += 1

        mode = self._vpn.proxy_mode
        if mode == "vpn":
            # Track activity for opportune update timing
            self._vpn.note_free_request()
            # Track stats for current VPN IP
            ip = self._vpn.current_ip or "unknown"
            if ip not in self._ip_stats:
                self._ip_stats[ip] = {
                    "requests": 0,
                    "start": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "server": self._vpn.current_server.get("name", "?") if self._vpn.current_server else "?",
                }
            self._ip_stats[ip]["requests"] += 1

            # Check if we should switch IP (approaching quota limit)
            if self._request_count >= self._vpn._quota_per_ip - 10:
                logger.info("[free-ip] approaching quota limit (%d/%d), switching IP",
                            self._request_count, self._vpn._quota_per_ip)
                await self.switch_ip()

        return self.proxy_url

    async def on_quota_exhausted(self):
        """Called when free model returns 429 (quota exhausted)."""
        if not self._vpn.enabled or self._vpn.proxy_mode != "vpn":
            return
        ip = self._vpn.current_ip or "unknown"
        if ip in self._ip_stats:
            self._ip_stats[ip]["end"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        logger.info("[free-ip] quota exhausted on IP %s, switching", ip)
        await self.switch_ip()

    async def switch_ip(self):
        """Switch to a fresh VPN IP.

        Validates that the new IP is actually different from recent IPs
        to avoid reconnecting to the same server.
        """
        old_ip = self._vpn.current_ip
        recent_ips = [s.get("ip") for s in self._vpn._ip_history[-10:]]

        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                await self._vpn.connect_next()

                new_ip = self._vpn.current_ip

                # Validate IP actually changed
                if new_ip and old_ip:
                    if new_ip == old_ip:
                        logger.warning("[free-ip] IP unchanged after switch (%s), attempt %d/%d",
                                       new_ip, attempt + 1, max_attempts)
                        if attempt < max_attempts - 1:
                            continue
                        else:
                            logger.error("[free-ip] IP unchanged after %d attempts, proceeding anyway", max_attempts)
                    elif new_ip in recent_ips:
                        logger.warning("[free-ip] IP %s was recently used, attempt %d/%d",
                                       new_ip, attempt + 1, max_attempts)
                        if attempt < max_attempts - 1:
                            continue

                self._request_count = 0
                self._session_start = time.monotonic()
                return  # success

            except Exception as e:
                logger.error("[free-ip] failed to switch IP (attempt %d/%d): %s",
                             attempt + 1, max_attempts, e)
                if attempt < max_attempts - 1:
                    await asyncio.sleep(2 ** attempt)  # brief backoff between attempts

    def get_status(self) -> dict:
        """Return pool status for the dashboard."""
        current_ip = self._vpn.current_ip
        stats = self._ip_stats.get(current_ip, {}) if current_ip else {}

        return {
            "enabled": self._vpn.enabled,
            "proxy_mode": self._vpn.proxy_mode,
            "vpn_status": self._vpn.status,
            "current_ip": current_ip,
            "current_server": self._vpn.current_server.get("name") if self._vpn.current_server else None,
            "requests_this_ip": self._request_count,
            "quota_per_ip": self._vpn._quota_per_ip,
            "remaining": max(0, self._vpn._quota_per_ip - self._request_count),
            "total_free_requests": self._total_free_requests,
            "ips_used": len(self._ip_stats),
            "ip_stats": self._ip_stats,
            "vpn": self._vpn.get_status(),
        }
