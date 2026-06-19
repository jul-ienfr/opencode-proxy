"""
Free IP pool for rotating IP addresses on free model requests.

Each VPN session gives a fresh IP = fresh free model quota.
This module manages the rotation and tracks usage per IP.
"""

import time
import logging
import asyncio
from typing import Optional

from vpn_manager import VPNManager

logger = logging.getLogger(__name__)


class FreeIPPool:
    """Manages IP rotation for free model requests.

    When the free model quota for the current IP is exhausted (429),
    the pool switches to the next VPN server for a fresh IP.
    """

    def __init__(self, vpn_manager: VPNManager, quota_per_ip: int = 300):
        self._vpn = vpn_manager
        self._quota_per_ip = quota_per_ip
        self._request_count = 0
        self._session_start: Optional[float] = None
        self._total_free_requests = 0
        this_ip_history: list[dict] = []
        self._ip_stats: dict[str, dict] = {}  # ip -> {requests, start, end}

    @property
    def enabled(self) -> bool:
        return self._vpn.enabled

    async def ensure_connected(self):
        """Ensure VPN is connected. Connect to first server if not."""
        if not self._vpn.enabled:
            return
        if self._vpn.status != "connected":
            await self._vpn.connect_next()
            self._request_count = 0
            self._session_start = time.monotonic()

    async def on_request(self) -> bool:
        """Called before each free model request.

        Returns True if VPN is ready, False if VPN is disabled.
        May switch IP if quota is exhausted.
        """
        if not self._vpn.enabled:
            return False

        await self.ensure_connected()

        self._request_count += 1
        self._total_free_requests += 1

        # Track stats for current IP
        ip = self._vpn.current_ip or "unknown"
        if ip not in self._ip_stats:
            self._ip_stats[ip] = {
                "requests": 0,
                "start": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "server": self._vpn.current_server.get("name", "?") if self._vpn.current_server else "?",
            }
        self._ip_stats[ip]["requests"] += 1

        # Check if we should switch IP (approaching quota limit)
        if self._request_count >= self._quota_per_ip - 10:
            logger.info("[free-ip] approaching quota limit (%d/%d), switching IP",
                        self._request_count, self._quota_per_ip)
            await self.switch_ip()

        return True

    async def on_quota_exhausted(self):
        """Called when free model returns 429 (quota exhausted).

        Immediately switches to next IP.
        """
        if not self._vpn.enabled:
            return

        ip = self._vpn.current_ip or "unknown"
        if ip in self._ip_stats:
            self._ip_stats[ip]["end"] = time.strftime("%Y-%m-%dT%H:%M:%S")

        logger.info("[free-ip] quota exhausted on IP %s, switching", ip)
        await self.switch_ip()

    async def switch_ip(self):
        """Switch to the next VPN server for a fresh IP."""
        try:
            await self._vpn.connect_next()
            self._request_count = 0
            self._session_start = time.monotonic()
        except Exception as e:
            logger.error("[free-ip] failed to switch IP: %s", e)

    def get_status(self) -> dict:
        """Return pool status for the dashboard."""
        current_ip = self._vpn.current_ip
        stats = self._ip_stats.get(current_ip, {}) if current_ip else {}

        return {
            "enabled": self._vpn.enabled,
            "vpn_status": self._vpn.status,
            "current_ip": current_ip,
            "current_server": self._vpn.current_server.get("name") if self._vpn.current_server else None,
            "requests_this_ip": self._request_count,
            "quota_per_ip": self._quota_per_ip,
            "remaining": max(0, self._quota_per_ip - self._request_count),
            "total_free_requests": self._total_free_requests,
            "ips_used": len(self._ip_stats),
            "ip_stats": self._ip_stats,
            "vpn": self._vpn.get_status(),
        }
