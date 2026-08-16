"""
VPN Manager for OpenCode Proxy — gluetun / compose-managed only.

The VPN tunnel is a single docker-compose service (vpn-gluetun, container
"opencode-vpn") that survives proxy restarts. This manager NEVER creates
or stops containers — it only:
  - starts the service via `docker compose up -d vpn-gluetun`
  - inspects / restarts the container
  - scans container logs for AUTH_FAILED
  - probes the public IP through the SOCKS5 tunnel (127.0.0.1:1080)

proxy_mode:
  - "vpn": free model requests are routed via socks5://127.0.0.1:1080
  - "direct": no proxy
"""

import os
import sys
import time
import json
import asyncio
import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# Windows: hide console windows for subprocess calls
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Project root
ROOT = os.path.dirname(os.path.abspath(__file__))


# ── Identity rotation (client fingerprint) ─────────────────────

# Stable desktop impersonation targets of curl_cffi 0.14 (verified by
# Session(impersonate=...) instantiation). Alpha (chrome133a), mobile,
# android, iOS, tor and generic variants are excluded.
_KNOWN_IMPERSONATIONS = frozenset({
    "chrome99", "chrome100", "chrome101", "chrome104", "chrome107",
    "chrome110", "chrome116", "chrome119", "chrome120", "chrome123",
    "chrome124", "chrome131", "chrome136", "chrome142",
    "edge99", "edge101",
    "firefox133", "firefox135", "firefox144",
    "safari153", "safari155", "safari15_3", "safari15_5", "safari170",
    "safari17_0", "safari180", "safari184", "safari18_0", "safari18_4",
    "safari260", "safari2601",
})

_DEFAULT_IDENTITY_PROFILE = {"impersonate": "chrome131", "user_agent": None, "extra_headers": {}}


def _normalize_identity_profiles(profiles) -> list[dict]:
    """Validate user-supplied identity profiles.

    Each entry: {"impersonate": <known target>, "user_agent": str|None,
    "extra_headers": dict}. Invalid entries are skipped with a warning.
    Never returns an empty list — falls back to the chrome131 default.
    """
    result = []
    if isinstance(profiles, list):
        for p in profiles:
            if not isinstance(p, dict):
                logger.warning("[identity] skipping invalid profile (not a dict): %r", p)
                continue
            imp = str(p.get("impersonate", "")).strip()
            if imp not in _KNOWN_IMPERSONATIONS:
                logger.warning("[identity] skipping unknown impersonation target %r", imp)
                continue
            ua = p.get("user_agent")
            extra = p.get("extra_headers") if isinstance(p.get("extra_headers"), dict) else {}
            result.append({
                "impersonate": imp,
                "user_agent": ua if isinstance(ua, str) and ua.strip() else None,
                "extra_headers": dict(extra),
            })
    if not result:
        logger.warning("[identity] no valid profiles — using default %r",
                       _DEFAULT_IDENTITY_PROFILE["impersonate"])
        result = [dict(_DEFAULT_IDENTITY_PROFILE)]
    return result


# ── State Machine ──────────────────────────────────────────────

class VPNState:
    """Valid VPN connection states and transitions."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    ERROR = "error"

    # Valid transitions: from_state -> set of allowed to_states
    TRANSITIONS = {
        DISCONNECTED: {CONNECTING, ERROR},
        CONNECTING: {CONNECTED, DISCONNECTING, ERROR, DISCONNECTED},
        CONNECTED: {CONNECTING, DISCONNECTING, ERROR},
        DISCONNECTING: {DISCONNECTED, ERROR},
        ERROR: {DISCONNECTED, CONNECTING},
    }

    @classmethod
    def can_transition(cls, from_state: str, to_state: str) -> bool:
        return to_state in cls.TRANSITIONS.get(from_state, set())


# ── Circuit Breaker ────────────────────────────────────────────

class CircuitBreaker:
    """Per-server circuit breaker: tracks consecutive failures.

    States: closed (normal) -> open (failing, skip server) -> half-open (testing)
    """

    def __init__(self, failure_threshold: int = 3, recovery_time: float = 300.0):
        """
        Args:
            failure_threshold: consecutive failures before opening circuit
            recovery_time: seconds to wait before trying again (half-open)
        """
        self._failure_threshold = failure_threshold
        self._recovery_time = recovery_time
        self._servers: dict[str, dict] = {}  # server_name -> {failures, opened_at, state}

    def record_success(self, server_name: str):
        """Record a successful connection — reset failure count."""
        self._servers[server_name] = {"failures": 0, "opened_at": 0, "state": "closed"}

    def record_failure(self, server_name: str):
        """Record a failed connection — increment count, open if threshold reached."""
        info = self._servers.get(server_name, {"failures": 0, "opened_at": 0, "state": "closed"})
        info["failures"] += 1
        if info["failures"] >= self._failure_threshold:
            info["state"] = "open"
            info["opened_at"] = time.monotonic()
            logger.warning("[circuit-breaker] server %s OPEN after %d failures",
                           server_name, info["failures"])
        self._servers[server_name] = info

    def is_available(self, server_name: str) -> bool:
        """Check if a server is available (circuit closed or half-open ready)."""
        info = self._servers.get(server_name)
        if not info or info["state"] == "closed":
            return True
        if info["state"] == "open":
            # Check if recovery time has passed -> half-open
            if time.monotonic() - info["opened_at"] >= self._recovery_time:
                info["state"] = "half-open"
                logger.info("[circuit-breaker] server %s -> half-open (testing)", server_name)
                return True
            return False
        # half-open: allow one attempt
        return True

    def get_status(self) -> dict:
        """Return status of all tracked servers."""
        return {
            name: {"failures": info["failures"], "state": info["state"]}
            for name, info in self._servers.items()
        }


# ── Backoff Timer ──────────────────────────────────────────────

class BackoffTimer:
    """Exponential backoff for connection attempts."""

    def __init__(self, base_delay: float = 5.0, max_delay: float = 60.0, multiplier: float = 2.0):
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._multiplier = multiplier
        self._current_delay = base_delay
        self._consecutive_failures = 0

    def record_success(self):
        """Reset backoff on success."""
        self._consecutive_failures = 0
        self._current_delay = self._base_delay

    def record_failure(self):
        """Increase backoff on failure."""
        self._consecutive_failures += 1
        self._current_delay = min(
            self._base_delay * (self._multiplier ** self._consecutive_failures),
            self._max_delay
        )

    @property
    def delay(self) -> float:
        return self._current_delay

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures


# ── VPN Manager ────────────────────────────────────────────────

class RotationFailed(RuntimeError):
    """IP rotation failed or was gated (circuit breaker open, fail-fast
    cooldown after a recent failed rotation, or all attempts failed).

    Raised by ``connect_next`` so callers can distinguish a genuine
    rotation failure from "no rotation needed". Never treat a caught
    RotationFailed as a silent success (CRITIC(5)).
    """


class VPNManager:
    """Manages the compose-managed gluetun VPN container.

    Container lifecycle is owned by docker-compose: this class only
    inspects/restarts the container and reads its logs. The tunnel
    survives proxy restarts.

    Config keys (ip_rotation section of config.yaml):
        enabled, proxy_mode, quota_per_ip, switch_delay,
        docker_container, docker_compose_file, vpn_proxy_port, socks5_proxy_port,
        credentials_file, server_countries, circuit_breaker_threshold,
        circuit_breaker_recovery, backoff_max_delay, ip_check_url,
        identity_rotation, identity_profiles
    """

    def __init__(self, cfg: dict):
        self._config = cfg
        self._mode = "docker"  # sole mode: compose-managed gluetun (free_ip_pool compat)
        self._enabled = cfg.get("enabled", False)
        self._proxy_mode = cfg.get("proxy_mode", "vpn")  # vpn | direct
        self._quota_per_ip = cfg.get("quota_per_ip", 300)
        self._switch_delay = cfg.get("switch_delay", 5)
        self._docker_container = cfg.get("docker_container", "opencode-vpn")
        self._docker_compose_file = cfg.get("docker_compose_file", "docker-compose.yml")
        self._proxy_port = cfg.get("vpn_proxy_port", 8888)
        self._socks5_port = cfg.get("socks5_proxy_port", 1080)
        self._auth_file = cfg.get(
            "credentials_file", os.path.join(ROOT, "vpn_configs", "credentials.txt"))
        self._server_countries = cfg.get("server_countries", "Germany")
        self._ip_check_url = cfg.get("ip_check_url", "https://api.ipify.org")

        # Identity rotation (client fingerprint, advanced on IP rotation)
        self._identity_rotation_enabled = cfg.get("identity_rotation", True)
        self._identity_profiles = _normalize_identity_profiles(cfg.get("identity_profiles"))
        self._identity_index = 0  # restored/clamped by load_state()
        self._rotation_task: Optional[asyncio.Task] = None  # single-flight rotation ([1]+[18])

        # Auto-update (gluetun image)
        self._update_enabled = cfg.get("update_enabled", True)
        self._update_check_interval = cfg.get("update_check_interval", 21600)
        self._update_apply_window = cfg.get("update_apply_window", "03:00-05:00")
        self._update_idle_minutes = cfg.get("update_apply_idle_minutes", 15)
        self._update_max_defer_hours = cfg.get("update_apply_max_defer_hours", 24)
        self._update_task: Optional[asyncio.Task] = None
        self._update_available = False
        self._update_current_digest: Optional[str] = None
        self._update_new_digest: Optional[str] = None
        self._update_old_image_id: Optional[str] = None
        self._update_known_since: Optional[float] = None
        self._update_checked_at: Optional[str] = None
        self._update_applied_at: Optional[str] = None
        self._update_last_error: Optional[str] = None
        self._last_free_request_at: Optional[float] = None

        # Runtime state
        self._status = VPNState.DISCONNECTED
        self._error: Optional[str] = None
        self._current_ip: Optional[str] = None
        self._current_server: Optional[dict] = None
        self._connected_at: Optional[float] = None
        self._auth_failed = False
        self._total_switches = 0
        self._ip_history: list[dict] = []
        self._lock = asyncio.Lock()
        # Fail-fast rotation cooldown (CRITIC(6)): after a total rotation
        # failure, refuse new rotations for 300 s — covers every rotation
        # path (ensure_connected, switch_ip, on_quota_exhausted, manual)
        # instead of the old per-caller timer in free_ip_pool only.
        self._ROTATION_FAIL_COOLDOWN = 300
        self._last_rotation_failed_at: Optional[float] = None
        # [37] Dashboard cache: /api/vpn-status polls every 10 s and each
        # full refresh costs 2 docker subprocess calls + a tunnel HTTP probe.
        # Within _STATUS_CACHE_SECONDS of a completed refresh, refresh_status
        # returns the live in-memory state instead of re-probing docker.
        self._STATUS_CACHE_SECONDS = 5.0
        self._last_status_refresh_at: Optional[float] = None
        # Free streams currently open (updated by opencode.py via
        # note_free_stream_start/end) — auto-update must not interrupt them ([21]).
        self._active_free_streams = 0

        # Reliability
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=cfg.get("circuit_breaker_threshold", 3),
            recovery_time=cfg.get("circuit_breaker_recovery", 300),
        )
        self._backoff = BackoffTimer(
            base_delay=cfg.get("switch_delay", 5),
            max_delay=cfg.get("backoff_max_delay", 60),
        )

        self.load_state()

    # ── Public properties ──────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = bool(value)

    @property
    def proxy_mode(self) -> str:
        return self._proxy_mode

    @proxy_mode.setter
    def proxy_mode(self, value: str):
        if value in ("vpn", "direct"):
            self._proxy_mode = value

    @property
    def status(self) -> str:
        return self._status

    @property
    def current_ip(self) -> Optional[str]:
        return self._current_ip

    @property
    def identity_index(self) -> int:
        return self._identity_index

    @property
    def current_identity(self) -> dict:
        """Active identity profile (dict with impersonate/user_agent/extra_headers).

        Returns profile[0] whenever rotation is disabled, proxy_mode is not
        "vpn", the VPN is not connected, or only one profile is configured.
        """
        if (not self._identity_rotation_enabled or self._proxy_mode != "vpn"
                or not self._enabled or self._status != VPNState.CONNECTED
                or len(self._identity_profiles) <= 1):
            return self._identity_profiles[0]
        return self._identity_profiles[self._identity_index % len(self._identity_profiles)]

    @property
    def current_server(self) -> Optional[dict]:
        return self._current_server

    @property
    def proxy_url(self) -> str:
        """HTTP proxy URL (gluetun HTTP proxy on 8888).

        VPN_PROXY_URL overrides the host:port: inside the compose
        deployment the loopback ports are unreachable from the proxy
        container — docker-compose.yml points it at the internal
        network instead ([13]).
        """
        return os.environ.get("VPN_PROXY_URL") or f"http://127.0.0.1:{self._proxy_port}"

    @property
    def socks5_url(self) -> str:
        """SOCKS5 proxy URL (gluetun SOCKS5 on 1080 — the reliable path on
        Windows Docker Desktop, where the HTTP proxy is not routed).

        VPN_SOCKS5_URL overrides the host:port for the same reason as
        proxy_url ([13]).
        """
        return os.environ.get("VPN_SOCKS5_URL") or f"socks5://127.0.0.1:{self._socks5_port}"

    def _compose_file_path(self) -> str:
        """Compose file path for `docker compose` invocations.

        VPN_DOCKER_COMPOSE_FILE overrides the ROOT-relative default: inside
        the containerized deployment, compose runs against the HOST daemon,
        so its relative paths (./logs, .env, credentials.env...) resolve on
        the host — a /app path would resolve against a nonexistent host /app.
        docker-compose.yml then points at the host project dir, which is
        mounted read-only at the same absolute path ([13]).
        """
        return os.environ.get("VPN_DOCKER_COMPOSE_FILE") or os.path.join(ROOT, self._docker_compose_file)

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        """Startup: load persisted state and reconcile with container reality."""
        self.load_state()
        await self.refresh_status()
        if self._update_enabled and not self._update_task:
            self._update_task = asyncio.create_task(self._updater_loop())

    async def stop(self) -> None:
        """Shutdown: persist state only. The tunnel is left running
        (it is compose-managed and survives proxy restarts)."""
        if self._update_task:
            self._update_task.cancel()
            self._update_task = None
        self.save_state()

    async def connect(self) -> None:
        """Bring the tunnel up: compose up + wait healthy + record IP."""
        async with self._lock:
            if self._status == VPNState.CONNECTED and self._current_ip:
                return
            if not VPNState.can_transition(self._status, VPNState.CONNECTING):
                raise RuntimeError(f"Cannot transition from {self._status} to connecting")
            self._status = VPNState.CONNECTING
            self._error = None
            try:
                await self._compose_up()
                started_at = await self._wait_healthy(timeout=120)
                if started_at is None:
                    raise RuntimeError("gluetun not healthy within 120s")
                if await self._check_auth_failed(started_at):
                    self._auth_failed = True
                    self._current_ip = None  # stale IP must not be served ([5])
                    raise RuntimeError("AUTH_FAILED - NordVPN service credentials rejected")
                self._auth_failed = False
                self._current_ip = await self.get_public_ip()
                if not self._current_ip:
                    raise RuntimeError("could not determine public IP through tunnel")
                self._connected_at = time.monotonic()
                self._status = VPNState.CONNECTED
                self._current_server = {
                    "name": self._docker_container, "country": self._server_countries}
                self._circuit_breaker.record_success(self._docker_container)
                self._backoff.record_success()
                self._last_rotation_failed_at = None  # tunnel is up: clear cooldown
                self.save_state()
                logger.info("[vpn] connected — IP %s", self._current_ip)
            except asyncio.CancelledError:
                # Client disconnect mid-connect must not leave the state
                # stuck in CONNECTING ([17]).
                self._status = VPNState.ERROR
                self._error = "connect cancelled"
                raise
            except Exception as e:
                self._status = VPNState.ERROR
                self._error = str(e)
                self._circuit_breaker.record_failure(self._docker_container)
                self._backoff.record_failure()
                logger.error("[vpn] connect failed: %s", e)
                raise

    async def connect_next(self) -> str:
        """Rotate to a fresh IP — single-flight.

        Concurrent callers (429 bursts, quota-exhausted, manual
        /api/vpn/next) share one rotation: the first caller starts it, the
        others await the same task. One rotation at a time — no more
        cascades of docker restarts (previously up to N concurrent callers
        × 3 attempts each). The stored task reference also prevents
        mid-flight GC.

        Raises:
            RotationFailed: rotation refused (fail-fast cooldown after a
                recent failed rotation (CRITIC(6)) or circuit breaker open
                ([25])) or all attempts failed (CRITIC(5)). Callers must
                treat this as a real failure, never as a silent success.

        Returns:
            The new IP on success.
        """
        if self._rotation_task and not self._rotation_task.done():
            return await asyncio.shield(self._rotation_task)
        # Fail-fast cooldown: after a total rotation failure, refuse new
        # rotations for 300 s. Covers every rotation path (ensure_connected,
        # switch_ip, on_quota_exhausted, manual) instead of the old
        # per-caller timer in free_ip_pool only (CRITIC(6)).
        if self._last_rotation_failed_at is not None:
            since = time.monotonic() - self._last_rotation_failed_at
            if since < self._ROTATION_FAIL_COOLDOWN:
                raise RotationFailed(
                    f"rotation cooldown active ({self._ROTATION_FAIL_COOLDOWN - int(since)}s left)")
        # Circuit breaker gate ([25]): no rotation while the breaker is open.
        if not self._circuit_breaker.is_available(self._docker_container):
            raise RotationFailed("circuit breaker open — skipping rotation")
        task = asyncio.create_task(self._connect_next_impl())
        self._rotation_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._rotation_task is task:
                self._rotation_task = None

    async def _connect_next_impl(self) -> str:
        """Actual rotation: compose up if the container is absent,
        else restart it. Validates the new IP (different from current and
        not in the last 10) with 3 attempts + backoff.

        Returns the new IP on success; raises RotationFailed otherwise
        (CRITIC(5)).
        """
        async with self._lock:
            if not self._enabled:
                raise RotationFailed("VPN disabled — cannot rotate")
            if not VPNState.can_transition(self._status, VPNState.CONNECTING):
                raise RuntimeError(f"Cannot transition from {self._status} to connecting")

            old_ip = self._current_ip
            recent_ips = [s.get("ip") for s in self._ip_history[-10:]]
            last_error: Optional[Exception] = None

            for attempt in range(3):
                self._status = VPNState.CONNECTING
                self._error = None
                try:
                    await self._ensure_container()
                    started_at = await self._wait_healthy(timeout=120)
                    if started_at is None:
                        raise RuntimeError("gluetun not healthy after restart")
                    if await self._check_auth_failed(started_at):
                        self._auth_failed = True
                        self._current_ip = None  # stale IP must not be served ([5])
                        raise RuntimeError("AUTH_FAILED after restart")
                    self._auth_failed = False

                    # Let the new tunnel stabilize before probing the IP
                    await asyncio.sleep(self._switch_delay)

                    new_ip = await self.get_public_ip()
                    if not new_ip:
                        raise RuntimeError("could not determine public IP")
                    if old_ip and new_ip == old_ip:
                        raise RuntimeError(f"IP unchanged after restart ({new_ip})")
                    if new_ip in recent_ips and attempt < 2:
                        raise RuntimeError(f"IP {new_ip} recently used")

                    # Success
                    self._current_ip = new_ip
                    self._current_server = {
                        "name": self._docker_container, "country": self._server_countries}
                    self._connected_at = time.monotonic()
                    self._status = VPNState.CONNECTED
                    self._total_switches += 1
                    self._ip_history.append({
                        "ip": new_ip,
                        "server": self._docker_container,
                        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    })
                    self._ip_history = self._ip_history[-100:]
                    self._circuit_breaker.record_success(self._docker_container)
                    self._backoff.record_success()
                    # Advance the client identity in sync with the IP rotation
                    if (self._identity_rotation_enabled and self._proxy_mode == "vpn"
                            and len(self._identity_profiles) > 1):
                        self._identity_index = (
                            self._identity_index + 1) % len(self._identity_profiles)
                    self._last_rotation_failed_at = None  # success: clear cooldown
                    self.save_state()
                    logger.info("[vpn] rotated → IP %s (switch #%d)", new_ip, self._total_switches)
                    return new_ip

                except asyncio.CancelledError:
                    # Client disconnect mid-rotation must not leave the
                    # state stuck in CONNECTING ([17]).
                    self._status = VPNState.ERROR
                    self._error = "rotation cancelled by client disconnect"
                    raise
                except Exception as e:
                    last_error = e
                    self._circuit_breaker.record_failure(self._docker_container)
                    self._backoff.record_failure()
                    logger.warning("[vpn] rotation attempt %d/3 failed: %s", attempt + 1, e)
                    if attempt < 2:
                        await asyncio.sleep(self._backoff.delay)

            self._status = VPNState.ERROR
            self._error = f"IP rotation failed after 3 attempts (last: {last_error})"
            logger.error("[vpn] %s", self._error)
            # CRITIC(5): a failed rotation is a failure, not a silent None —
            # raise so callers can react honestly. Also arm the fail-fast
            # cooldown (CRITIC(6)) so we don't hammer a dead tunnel.
            self._last_rotation_failed_at = time.monotonic()
            raise RotationFailed(self._error)

    async def connect_wait(self, timeout: float = 120.0) -> bool:
        """Wait for an externally managed tunnel to come up (no container action).

        Returns True once the public IP through the SOCKS5 tunnel answers.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ip = await self.get_public_ip()
            if ip:
                self._current_ip = ip
                self._connected_at = time.monotonic()
                self._status = VPNState.CONNECTED
                self._current_server = {
                    "name": self._docker_container, "country": self._server_countries}
                return True
            await asyncio.sleep(3)
        self._status = VPNState.ERROR
        self._error = f"Timeout waiting for VPN connection ({int(timeout)}s)"
        logger.error("[vpn] %s", self._error)
        return False

    async def disconnect(self) -> None:
        """Disconnect: update state + persist. Never stops the container —
        the tunnel is owned by docker-compose and stays up."""
        async with self._lock:
            if self._status == VPNState.DISCONNECTED:
                return
            self._status = VPNState.DISCONNECTING
            self._current_ip = None
            self._connected_at = None
            self._status = VPNState.DISCONNECTED
            self.save_state()
            logger.info("[vpn] disconnected (tunnel left running, compose-managed)")

    # ── Status & health ────────────────────────────────────────

    async def refresh_status(self, force: bool = False) -> dict:
        """Reconcile state with container reality:
        - container absent          → disconnected
        - container not running     → error
        - AUTH_FAILED in recent logs → error + auth_failed
        - tunnel answering          → connected (IP refreshed)

        Cache ([37]): within _STATUS_CACHE_SECONDS of a completed refresh,
        return the live in-memory state instead of re-probing docker —
        get_status() reads current fields, so only the docker/network probe
        is deferred (≤5 s staleness, fine for the dashboard's 10 s poll).
        Pass force=True from paths that must see reality immediately
        (startup reconcile, image update apply/rollback).
        """
        if not force and self._last_status_refresh_at is not None and \
                time.monotonic() - self._last_status_refresh_at < self._STATUS_CACHE_SECONDS:
            return self.get_status()
        # Stamp before probing: concurrent calls within the window coalesce
        # onto this one instead of firing their own docker subprocesses.
        self._last_status_refresh_at = time.monotonic()
        info = await self._docker_inspect()
        if not info:
            if self._status != VPNState.DISCONNECTED:
                logger.warning("[vpn] container %s not found", self._docker_container)
            self._status = VPNState.DISCONNECTED
            self._current_ip = None
            self._error = None
            return self.get_status()

        if not info.get("running"):
            self._status = VPNState.ERROR
            self._error = "container not running"
            return self.get_status()

        if await self._check_auth_failed(info.get("started_at", "")):
            self._auth_failed = True
            self._status = VPNState.ERROR
            self._error = "AUTH_FAILED - NordVPN service credentials rejected"
            self._current_ip = None  # stale IP must not be served ([5])
            logger.error("[vpn] %s", self._error)
            return self.get_status()
        self._auth_failed = False

        ip = await self.get_public_ip()
        if ip:
            self._current_ip = ip
            self._connected_at = time.monotonic()
            self._status = VPNState.CONNECTED
            self._error = None
            self._current_server = {
                "name": self._docker_container, "country": self._server_countries}
        else:
            self._status = VPNState.ERROR
            self._error = "container running but tunnel not answering"
        return self.get_status()

    async def health_check(self) -> dict:
        """Probe the tunnel through SOCKS5 and measure latency."""
        result = {"ok": False, "ip_changed": False, "latency_ms": None, "error": None}
        if self._status != VPNState.CONNECTED:
            result["error"] = "Not connected"
            return result
        try:
            import httpx
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=15, proxy=self.socks5_url) as client:
                resp = await client.get(self._ip_check_url)
                elapsed_ms = int((time.monotonic() - start) * 1000)
            new_ip = resp.text.strip()
            result["latency_ms"] = elapsed_ms
            if new_ip:
                result["ok"] = True
                if new_ip != self._current_ip:
                    result["ip_changed"] = True
                    logger.warning("[vpn] health check: IP changed %s → %s",
                                   self._current_ip, new_ip)
                    # Network probe above stays outside the lock; only the
                    # state mutation is guarded ([23]).
                    async with self._lock:
                        self._current_ip = new_ip
            else:
                result["error"] = "Could not determine IP"
        except Exception as e:
            result["error"] = str(e)
            logger.error("[vpn] health check failed: %s", e)
        return result

    async def get_public_ip(self) -> Optional[str]:
        """Query the public IP through the SOCKS5 tunnel (127.0.0.1:1080).

        Returns None on any failure — never "unknown" as a success value.
        """
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10, proxy=self.socks5_url) as client:
                resp = await client.get(self._ip_check_url)
                ip = resp.text.strip()
                return ip or None
        except Exception as e:
            logger.error("[vpn] failed to get public IP via SOCKS5: %s", e)
            return None

    # ── Config ─────────────────────────────────────────────────

    def get_config(self) -> dict:
        """Return current configuration for the dashboard."""
        return {
            "enabled": self._enabled,
            "proxy_mode": self._proxy_mode,
            "mode": self._mode,
            "quota_per_ip": self._quota_per_ip,
            "switch_delay": self._switch_delay,
            "docker_container": self._docker_container,
            "docker_compose_file": self._docker_compose_file,
            "vpn_proxy_port": self._proxy_port,
            "socks5_proxy_port": self._socks5_port,
            "credentials_file": self._auth_file,
            "server_countries": self._server_countries,
            "ip_check_url": self._ip_check_url,
            "update_enabled": self._update_enabled,
            "update_check_interval": self._update_check_interval,
            "update_apply_window": self._update_apply_window,
            "update_apply_idle_minutes": self._update_idle_minutes,
            "update_apply_max_defer_hours": self._update_max_defer_hours,
            "circuit_breaker_threshold": self._circuit_breaker._failure_threshold,
            "circuit_breaker_recovery": self._circuit_breaker._recovery_time,
            "backoff_max_delay": self._backoff._max_delay,
            "identity_rotation": self._identity_rotation_enabled,
            "identity_profiles": self._identity_profiles,
        }

    async def update_config(self, updates: dict) -> dict:
        """Apply config updates from the dashboard (hot-reload, no restart)."""
        if "enabled" in updates:
            self._enabled = bool(updates["enabled"])
        if "proxy_mode" in updates and updates["proxy_mode"] in ("vpn", "direct"):
            self._proxy_mode = updates["proxy_mode"]
        if "quota_per_ip" in updates:
            self._quota_per_ip = max(1, int(updates["quota_per_ip"]))
        if "switch_delay" in updates:
            self._switch_delay = max(0.0, float(updates["switch_delay"]))
        if "server_countries" in updates:
            self._server_countries = str(updates["server_countries"])
        if "circuit_breaker_threshold" in updates or "circuit_breaker_recovery" in updates:
            threshold = updates.get(
                "circuit_breaker_threshold", self._circuit_breaker._failure_threshold)
            recovery = updates.get(
                "circuit_breaker_recovery", self._circuit_breaker._recovery_time)
            self._circuit_breaker = CircuitBreaker(
                failure_threshold=int(threshold), recovery_time=float(recovery))
        if "backoff_max_delay" in updates:
            self._backoff = BackoffTimer(
                base_delay=self._switch_delay,
                max_delay=float(updates["backoff_max_delay"]))
        if "update_enabled" in updates:
            self._update_enabled = bool(updates["update_enabled"])
        if "update_check_interval" in updates:
            self._update_check_interval = max(300, int(updates["update_check_interval"]))
        if "update_apply_window" in updates:
            self._update_apply_window = str(updates["update_apply_window"])
        if "update_apply_idle_minutes" in updates:
            self._update_idle_minutes = max(1, int(updates["update_apply_idle_minutes"]))
        if "update_apply_max_defer_hours" in updates:
            self._update_max_defer_hours = max(1, int(updates["update_apply_max_defer_hours"]))
        if "identity_rotation" in updates:
            self._identity_rotation_enabled = bool(updates["identity_rotation"])
        if "identity_profiles" in updates:
            new_profiles = _normalize_identity_profiles(updates["identity_profiles"])
            self._identity_profiles = new_profiles
            self._identity_index %= len(new_profiles)  # clamp (config may shrink)
        return self.get_config()

    def get_status(self) -> dict:
        """Return current VPN status for the dashboard."""
        elapsed = int(time.monotonic() - self._connected_at) if self._connected_at else None
        return {
            "enabled": self._enabled,
            "proxy_mode": self._proxy_mode,
            "mode": self._mode,
            "status": self._status,
            "ip": self._current_ip,
            "server": self._current_server.get("name") if self._current_server else None,
            "country": self._current_server.get("country") if self._current_server else None,
            "connected_seconds": elapsed,
            "total_switches": self._total_switches,
            "error": self._error,
            "auth_failed": self._auth_failed,
            "ip_history": self._ip_history[-10:],
            "proxy_port": self._proxy_port,
            "proxy_url": self.proxy_url,
            "socks5_url": self.socks5_url,
            "container": self._docker_container,
            "circuit_breaker": self._circuit_breaker.get_status(),
            "backoff_failures": self._backoff.consecutive_failures,
            "backoff_delay": self._backoff.delay,
            "identity_index": self._identity_index,
            "current_identity": self.current_identity,
            "update": {
                "enabled": self._update_enabled,
                "check_interval": self._update_check_interval,
                "apply_window": self._update_apply_window,
                "idle_minutes": self._update_idle_minutes,
                "max_defer_hours": self._update_max_defer_hours,
                "available": self._update_available,
                "current_digest": self._update_current_digest,
                "new_digest": self._update_new_digest,
                "checked_at": self._update_checked_at,
                "applied_at": self._update_applied_at,
                "last_error": self._update_last_error,
            },
        }

    # ── Docker helpers (all blocking — run via asyncio.to_thread) ──

    def _docker_run(self, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
        """Run a docker CLI command (blocking — call via asyncio.to_thread)."""
        try:
            return subprocess.run(
                ["docker", *args],
                capture_output=True, text=True, timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
                encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            raise RuntimeError("docker CLI not found on PATH")

    async def _docker_inspect(self) -> dict:
        """Inspect the gluetun container. Returns {} if absent or docker unavailable."""
        try:
            result = await asyncio.to_thread(
                self._docker_run, ["inspect", self._docker_container], 15)
        except RuntimeError as e:
            logger.warning("[vpn] %s", e)
            return {}
        if result.returncode != 0:
            return {}
        try:
            info = json.loads(result.stdout)[0]
        except (json.JSONDecodeError, IndexError):
            return {}
        state = info.get("State", {})
        return {
            "running": state.get("Running", False),
            "healthy": state.get("Health", {}).get("Status") == "healthy",
            "restarting": state.get("Restarting", False),
            "started_at": state.get("StartedAt", ""),
        }

    async def _docker_restart(self) -> None:
        """Restart the gluetun container to get a fresh IP.

        Note ([25]): this deliberately bypasses docker compose — a plain
        ``docker restart`` keeps the compose definition untouched (no drift
        of the declared service). Image updates go through ``apply_update``
        instead, which recreates the container from the new image.
        """
        result = await asyncio.to_thread(
            self._docker_run, ["restart", self._docker_container], 60)
        if result.returncode != 0:
            raise RuntimeError(
                f"docker restart failed: {result.stderr.strip() or result.stdout.strip()}")

    async def _compose_up(self) -> None:
        """Start the gluetun service via docker compose (idempotent)."""
        compose_file = self._compose_file_path()
        result = await asyncio.to_thread(
            self._docker_run,
            ["compose", "-f", compose_file, "up", "-d", "vpn-gluetun"], 120)
        if result.returncode != 0:
            raise RuntimeError(
                f"docker compose up failed: {result.stderr.strip() or result.stdout.strip()}")

    async def _ensure_container(self) -> None:
        """Compose up if the container is absent, else restart it."""
        info = await self._docker_inspect()
        if not info:
            await self._compose_up()
        else:
            await self._docker_restart()

    async def _check_auth_failed(self, started_at: str = "") -> bool:
        """Scan container logs for AUTH_FAILED (OpenVPN auth rejection).

        Bound the scan to logs since the container's last start (StartedAt):
        an AUTH_FAILED logged before the last restart is stale (resolved by
        that restart) and must not flip the state to ERROR. Falls back to a
        10-minute window when the start time is unknown.
        """
        since = started_at if started_at else "10m"
        result = await asyncio.to_thread(
            self._docker_run, ["logs", "--since", since, self._docker_container], 30)
        if result.returncode != 0:
            return False
        return "AUTH_FAILED" in result.stdout or "auth failed" in result.stdout.lower()

    async def _wait_healthy(self, timeout: float = 120.0) -> Optional[str]:
        """Wait until the container runs AND the SOCKS5 tunnel answers.

        Returns the container's StartedAt on success — callers bind their
        AUTH_FAILED scan to it, so a pre-restart AUTH_FAILED still in the
        logs cannot flip state (docker logs span container restarts, [15]).
        Returns None on failure/timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = await self._docker_inspect()
            if info.get("running") and await self.get_public_ip():
                return info.get("started_at", "")
            # Fail fast: AUTH_FAILED (rejected credentials) never recovers on
            # its own — do not sit out the timeout, report immediately.
            if not info or not info.get("running") or await self._check_auth_failed(info.get("started_at", "")):
                return None
            await asyncio.sleep(2)
        return None

    # ── Auto-update (gluetun image) ────────────────────────────

    _UPDATE_LOCK_PATH = os.path.join(ROOT, "logs", "vpn_update.lock")

    def note_free_request(self) -> None:
        """Record free-request activity (used to pick an opportune
        moment for applying a pending update)."""
        self._last_free_request_at = time.monotonic()

    def note_free_stream_start(self) -> None:
        """Count an open free stream (called by opencode.py when a free
        stream starts). Auto-update must not interrupt live streams ([21])."""
        self._active_free_streams += 1

    def note_free_stream_end(self) -> None:
        """Counterpart of note_free_stream_start — called when a free
        stream closes, in a finally block."""
        if self._active_free_streams > 0:
            self._active_free_streams -= 1

    async def _docker_image_id(self, image: str) -> Optional[str]:
        result = await asyncio.to_thread(
            self._docker_run,
            ["image", "inspect", image, "--format", "{{.Id}}"], 30)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    async def _docker_repo_digest(self, image: str) -> Optional[str]:
        result = await asyncio.to_thread(
            self._docker_run,
            ["image", "inspect", image, "--format", "{{index .RepoDigests 0}}"], 30)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    async def check_update(self) -> bool:
        """Check whether a newer gluetun image is available (non-disruptive).

        `docker compose pull` downloads the new image but never touches the
        running container. A digest change before/after means an update is
        available; the previous image ID is kept for rollback.
        """
        if not self._update_enabled:
            return False
        try:
            image = "qmcgaw/gluetun"
            old_digest = await self._docker_repo_digest(image)
            if not old_digest:
                return False  # image not installed yet — nothing to update
            old_id = await self._docker_image_id(image)
            compose_file = self._compose_file_path()
            result = await asyncio.to_thread(
                self._docker_run,
                ["compose", "-f", compose_file, "pull", "vpn-gluetun"], 300)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            new_digest = await self._docker_repo_digest(image)
            self._update_checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._update_last_error = None
            if new_digest and new_digest != old_digest:
                self._update_available = True
                self._update_current_digest = old_digest
                self._update_new_digest = new_digest
                self._update_old_image_id = old_id
                self._update_known_since = self._update_known_since or time.monotonic()
                logger.info("[vpn-update] new gluetun image available: %s", new_digest[:19])
                return True
            self._update_available = False
            self._update_new_digest = None
            return False
        except Exception as e:
            self._update_last_error = str(e)
            logger.warning("[vpn-update] check failed: %s", e)
            return False

    def _acquire_update_lock(self) -> bool:
        """Cross-instance lock: refuse if another process is applying an
        update right now. Stale locks (older than 15 min) are broken."""
        try:
            os.makedirs(os.path.dirname(self._UPDATE_LOCK_PATH), exist_ok=True)
            fd = os.open(self._UPDATE_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(self._UPDATE_LOCK_PATH) > 900:
                    os.remove(self._UPDATE_LOCK_PATH)
                    return self._acquire_update_lock()
            except OSError:
                pass
            return False
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True

    def _release_update_lock(self) -> None:
        try:
            os.remove(self._UPDATE_LOCK_PATH)
        except OSError:
            pass

    async def apply_update(self, check_opportune: bool = False) -> dict:
        """Apply a pending update: recreate the container with the new image,
        verify the tunnel, roll back on failure (best-effort).

        Args:
            check_opportune: re-check opportune-ness INSIDE the lock
                ([21] TOCTOU fix) and skip when live free streams would be
                cut. The background updater and the manual /api/vpn/update
                endpoint pass True; the lock is held across the whole apply
                (which can take minutes), so the check must happen here, not
                before acquiring.
        """
        async with self._lock:
            if not self._update_available:
                return {"ok": False, "error": "no update available"}
            if check_opportune and not self._update_opportune():
                return {"ok": False, "error": "not opportune (traffic active)"}
            if check_opportune and self._active_free_streams > 0:
                return {"ok": False, "error": "free streams active — deferring update"}
            if not self._acquire_update_lock():
                return {"ok": False, "error": "another instance is applying an update"}
            try:
                compose_file = self._compose_file_path()
                result = await asyncio.to_thread(
                    self._docker_run,
                    ["compose", "-f", compose_file, "up", "-d", "--pull", "never", "vpn-gluetun"], 120)
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip() or result.stdout.strip())
                started_at = await self._wait_healthy(timeout=120)
                if started_at is None:
                    raise RuntimeError("tunnel not healthy after update")
                if await self._check_auth_failed(started_at):
                    self._auth_failed = True
                    raise RuntimeError("AUTH_FAILED after update")
                self._auth_failed = False
                # force: container was just recreated — a cached status
                # would report the pre-update container ([37]).
                await self.refresh_status(force=True)
                self._update_available = False
                self._update_applied_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self._update_known_since = None
                self._update_old_image_id = None
                self.save_state()
                logger.info("[vpn-update] applied — container recreated with new image (IP %s)",
                            self._current_ip)
                return {"ok": True, "ip": self._current_ip}
            except Exception as e:
                self._update_last_error = str(e)
                logger.error("[vpn-update] apply failed, rolling back: %s", e)
                await self._rollback_update(self._update_old_image_id)
                return {"ok": False, "error": str(e)}
            finally:
                self._release_update_lock()

    async def _rollback_update(self, old_image_id: Optional[str]) -> None:
        """Best-effort rollback: re-tag the previous image and recreate."""
        if not old_image_id:
            logger.error("[vpn-update] no previous image ID — cannot roll back")
            return
        try:
            image = "qmcgaw/gluetun"
            await asyncio.to_thread(self._docker_run, ["tag", old_image_id, image], 30)
            compose_file = self._compose_file_path()
            await asyncio.to_thread(
                self._docker_run,
                ["compose", "-f", compose_file, "up", "-d", "--pull", "never", "vpn-gluetun"], 120)
            # force: container was just recreated — see _apply_update ([37]).
            await self.refresh_status(force=True)
            logger.warning("[vpn-update] rolled back to previous image %s", old_image_id[:19])
        except Exception as e:
            logger.error("[vpn-update] rollback failed: %s", e)

    def _update_opportune(self) -> bool:
        """Decide whether NOW is a good moment to apply a pending update.

        - tunnel down           → apply immediately (no traffic at risk)
        - nightly window (03:00-05:00 local) AND idle ≥ N min → apply
        - known for > max_defer_hours → apply anyway
        - otherwise             → wait for the next tick
        """
        if self._status != VPNState.CONNECTED:
            return True  # tunnel down: nothing to disrupt
        if self._update_known_since and \
                time.monotonic() - self._update_known_since > self._update_max_defer_hours * 3600:
            logger.info("[vpn-update] deferring >%dh — applying anyway",
                        self._update_max_defer_hours)
            return True
        try:
            start_str, end_str = self._update_apply_window.split("-")
            start_h = int(start_str.split(":")[0])
            end_h = int(end_str.split(":")[0])
        except (ValueError, AttributeError):
            start_h, end_h = 3, 5
        in_window = start_h <= time.localtime().tm_hour < end_h
        if not in_window:
            return False
        if self._last_free_request_at is None or \
                time.monotonic() - self._last_free_request_at >= self._update_idle_minutes * 60:
            return True
        return False

    async def _updater_loop(self) -> None:
        """Background loop: detect available updates and apply them at the
        most opportune moment. First iteration runs immediately, then every
        update_check_interval seconds."""
        while True:
            try:
                if await self.check_update() and self._update_opportune():
                    # Re-check opportune-ness inside the lock ([21]): traffic
                    # may have started between the outer check and the apply.
                    await self.apply_update(check_opportune=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[vpn-update] background loop error: %s", e)
            await asyncio.sleep(self._update_check_interval)

    # ── State persistence ──────────────────────────────────────

    def _get_state_path(self) -> str:
        """Return path to the VPN state file."""
        return os.path.join(ROOT, "logs", "vpn_state.json")

    def save_state(self):
        """Persist IP history, stats, and circuit breaker state to disk.

        Atomic write ([20]): temp file + os.replace, so a crash mid-write
        can never leave a truncated state file.
        """
        try:
            state = {
                "ip_history": self._ip_history,
                "total_switches": self._total_switches,
                "circuit_breaker": self._circuit_breaker.get_status(),
                "current_ip": self._current_ip,
                "identity_index": self._identity_index,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            state_path = self._get_state_path()
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            tmp_path = state_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, state_path)
            logger.debug("[vpn] state saved to %s", state_path)
        except Exception as e:
            logger.debug("[vpn] failed to save state: %s", e)

    def load_state(self):
        """Load persisted state from disk."""
        try:
            state_path = self._get_state_path()
            if not os.path.exists(state_path):
                return
            with open(state_path, "r") as f:
                state = json.load(f)
            self._ip_history = state.get("ip_history", [])
            self._total_switches = state.get("total_switches", 0)
            # Restore identity index with clamp (config may have shrunk)
            self._identity_index = state.get("identity_index", 0) % len(self._identity_profiles)
            # Restore circuit breaker state ([20]): an open breaker survives a
            # restart. get_status() saves failures/state but no opened_at, so
            # a restored open breaker goes half-open immediately — intended
            # (a reboot is a natural recovery attempt).
            cb = state.get("circuit_breaker")
            if isinstance(cb, dict):
                self._circuit_breaker._servers = {
                    k: {"failures": v.get("failures", 0),
                        "opened_at": v.get("opened_at", 0),
                        "state": v.get("state", "closed")}
                    for k, v in cb.items() if isinstance(v, dict)
                }
            # Restore the last known IP so a fresh start doesn't show "unknown"
            self._current_ip = state.get("current_ip") or self._current_ip
            logger.info("[vpn] state loaded: %d IPs in history, %d total switches",
                        len(self._ip_history), self._total_switches)
        except Exception as e:
            logger.debug("[vpn] failed to load state: %s", e)
