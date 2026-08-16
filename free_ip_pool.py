"""
Free IP pool for rotating IP addresses on free model requests.

Each VPN session gives a fresh IP = fresh free model quota.
Routes free requests through the compose-managed gluetun tunnel (SOCKS5).
"""

import time
import logging
import asyncio
from typing import Optional

from vpn_manager import RotationFailed, VPNManager

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
        self._rotation_task: Optional[asyncio.Task] = None  # single-flight 429 rotation
        self._last_quota_per_ip: Optional[int] = None  # hot-reload detection (CRITIC(11))

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
            # requests in between go direct immediately. connect_next can
            # raise RotationFailed (CRITIC(5)) — never let that propagate
            # into the request path (fail-open by design).
            now = time.monotonic()
            if self._last_connect_attempt and now - self._last_connect_attempt < self._CONNECT_RETRY_INTERVAL:
                return
            self._last_connect_attempt = now
            try:
                await self._vpn.connect_next()
            except Exception as e:
                logger.warning("[free-ip] tunnel reconnect failed: %s", e)
                return
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

        self._total_free_requests += 1

        mode = self._vpn.proxy_mode
        if mode == "vpn":
            # Only count requests that actually went through the tunnel —
            # when the VPN is down, requests go direct on a residential IP
            # and must not advance the rotation counter ([5]).
            # Hot-reload guard (CRITIC(11)): when quota_per_ip changed via
            # config, the counter refers to the OLD quota — reset lazily so
            # the new quota applies from the next request on.
            if self._last_quota_per_ip is not None and \
                    self._last_quota_per_ip != self._vpn._quota_per_ip:
                logger.info("[free-ip] quota_per_ip changed %s → %s — resetting request counter",
                            self._last_quota_per_ip, self._vpn._quota_per_ip)
                self._request_count = 0
            self._last_quota_per_ip = self._vpn._quota_per_ip
            self._request_count += 1
            # Track activity for opportune update timing
            self._vpn.note_free_request()
            # Track stats for current VPN IP
            ip = self._vpn.current_ip or "unknown"
            if ip not in self._ip_stats:
                self._ip_stats[ip] = {
                    "requests": 0,
                    "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "server": self._vpn.current_server.get("name", "?") if self._vpn.current_server else "?",
                }
            self._ip_stats[ip]["requests"] += 1

            # Check if we should switch IP (approaching quota limit)
            if self._request_count >= self._vpn._quota_per_ip - 10:
                logger.info("[free-ip] approaching quota limit (%d/%d), switching IP",
                            self._request_count, self._vpn._quota_per_ip)
                await self.switch_ip()

        return self.proxy_url

    async def switch_ip(self) -> str:
        """Switch to a fresh VPN IP — honest single attempt (CRITIC(5)).

        The retry/backoff logic lives in ``VPNManager.connect_next`` (3
        attempts + exponential backoff + fail-fast cooldown after a total
        failure). A wrap-retry loop here would either duplicate that work or
        immediately hit the cooldown — so this is ONE call that either
        returns the new IP or raises ``RotationFailed``.

        Raises:
            RotationFailed: rotation failed or was gated (circuit breaker
                open, fail-fast cooldown, tunnel down). Never a silent
                success.
        """
        new_ip = await self._vpn.connect_next()
        if not new_ip:
            # Defensive: connect_next is typed to return str now, but a
            # None would silently reset counters below — fail loudly instead.
            raise RotationFailed("connect_next returned no IP")
        self._request_count = 0
        self._session_start = time.monotonic()
        return new_ip

    def on_quota_exhausted(self):
        """Free quota exhausted (429): rotate to a fresh IP in the background.

        [0]/[42] restored: a 429 is answered with an IP rotation, NOT just a
        paid fallback. Fire-and-forget and single-flight — concurrent 429s
        (stream + non-stream, 4 stream handlers) share ONE rotation via
        ``self._rotation_task`` (and ``VPNManager.connect_next`` is itself
        single-flight on top). The calling request falls back to paid
        immediately; the next free attempt lands on the fresh IP.

        The cooldown is keyed per (model, IP) ([4]), so the rotation gives
        the model a FRESH cooldown key — the new IP is not blocked by the
        429 that triggered this rotation.

        No-op when VPN is disabled or in direct mode.
        """
        if not self._vpn.enabled or self._vpn.proxy_mode != "vpn":
            return
        if self._rotation_task and not self._rotation_task.done():
            return  # a rotation is already in flight
        self._rotation_task = asyncio.create_task(self._rotate_on_quota())

    async def _rotate_on_quota(self):
        try:
            await self.switch_ip()
        except Exception as e:
            # CRITIC(5): a failed background rotation must be logged, not
            # swallowed — the next free request will fall back to paid and
            # retry the rotation later (or not, if the fail-fast cooldown
            # is active in VPNManager).
            logger.warning("[free-ip] background rotation failed: %s", e)
        finally:
            self._rotation_task = None

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
