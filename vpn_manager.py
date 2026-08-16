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
        CONNECTED: {DISCONNECTING, ERROR},
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

class VPNManager:
    """Manages the compose-managed gluetun VPN container.

    Container lifecycle is owned by docker-compose: this class only
    inspects/restarts the container and reads its logs. The tunnel
    survives proxy restarts.

    Config keys (ip_rotation section of config.yaml):
        enabled, proxy_mode, free_only, quota_per_ip, switch_delay,
        docker_container, docker_compose_file, vpn_proxy_port, socks5_proxy_port,
        credentials_file, server_countries, circuit_breaker_threshold,
        circuit_breaker_recovery, backoff_max_delay, ip_check_url
    """

    def __init__(self, cfg: dict):
        self._config = cfg
        self._mode = "docker"  # sole mode: compose-managed gluetun (free_ip_pool compat)
        self._enabled = cfg.get("enabled", False)
        self._proxy_mode = cfg.get("proxy_mode", "vpn")  # vpn | direct
        self._free_only = cfg.get("free_only", True)
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
    def current_server(self) -> Optional[dict]:
        return self._current_server

    @property
    def proxy_url(self) -> str:
        """HTTP proxy URL (gluetun HTTP proxy on 8888)."""
        return f"http://127.0.0.1:{self._proxy_port}"

    @property
    def socks5_url(self) -> str:
        """SOCKS5 proxy URL (gluetun SOCKS5 on 1080 — the reliable path on
        Windows Docker Desktop, where the HTTP proxy is not routed)."""
        return f"socks5://127.0.0.1:{self._socks5_port}"

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        """Startup: load persisted state and reconcile with container reality."""
        self.load_state()
        await self.refresh_status()

    async def stop(self) -> None:
        """Shutdown: persist state only. The tunnel is left running
        (it is compose-managed and survives proxy restarts)."""
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
                if not await self._wait_healthy(timeout=120):
                    raise RuntimeError("gluetun not healthy within 120s")
                if await self._check_auth_failed():
                    self._auth_failed = True
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
                self.save_state()
                logger.info("[vpn] connected — IP %s", self._current_ip)
            except Exception as e:
                self._status = VPNState.ERROR
                self._error = str(e)
                self._circuit_breaker.record_failure(self._docker_container)
                self._backoff.record_failure()
                logger.error("[vpn] connect failed: %s", e)
                raise

    async def connect_next(self) -> Optional[str]:
        """Rotate to a fresh IP: compose up if the container is absent,
        else restart it. Validates the new IP (different from current and
        not in the last 10) with 3 attempts + backoff.

        Returns the new IP, or None if all attempts failed.
        """
        async with self._lock:
            if not self._enabled:
                return None
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
                    if not await self._wait_healthy(timeout=120):
                        raise RuntimeError("gluetun not healthy after restart")
                    if await self._check_auth_failed():
                        self._auth_failed = True
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
                        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    })
                    self._ip_history = self._ip_history[-100:]
                    self._circuit_breaker.record_success(self._docker_container)
                    self._backoff.record_success()
                    self.save_state()
                    logger.info("[vpn] rotated → IP %s (switch #%d)", new_ip, self._total_switches)
                    return new_ip

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
            return None

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

    async def refresh_status(self) -> dict:
        """Reconcile state with container reality:
        - container absent          → disconnected
        - container not running     → error
        - AUTH_FAILED in recent logs → error + auth_failed
        - tunnel answering          → connected (IP refreshed)
        """
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

        if await self._check_auth_failed():
            self._auth_failed = True
            self._status = VPNState.ERROR
            self._error = "AUTH_FAILED - NordVPN service credentials rejected"
            logger.error("[vpn] %s", self._error)
            return self.get_status()
        self._auth_failed = False

        ip = await self.get_public_ip()
        if ip:
            self._current_ip = ip
            self._connected_at = time.monotonic()
            self._status = VPNState.CONNECTED
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
            "free_only": self._free_only,
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
            "circuit_breaker_threshold": self._circuit_breaker._failure_threshold,
            "circuit_breaker_recovery": self._circuit_breaker._recovery_time,
            "backoff_max_delay": self._backoff._max_delay,
        }

    async def update_config(self, updates: dict) -> dict:
        """Apply config updates from the dashboard (hot-reload, no restart)."""
        if "enabled" in updates:
            self._enabled = bool(updates["enabled"])
        if "proxy_mode" in updates and updates["proxy_mode"] in ("vpn", "direct"):
            self._proxy_mode = updates["proxy_mode"]
        if "free_only" in updates:
            self._free_only = bool(updates["free_only"])
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
        }

    # ── Credentials ────────────────────────────────────────────

    def _read_credentials(self) -> tuple[str, str]:
        """Read NordVPN service credentials from the credentials file.

        Raises FileNotFoundError if missing/invalid — NEVER silently
        returns empty credentials.
        """
        path = self._auth_file
        if not os.path.exists(path):
            raise FileNotFoundError(f"Credentials file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().strip().splitlines()
        if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
            raise FileNotFoundError(f"Credentials file has no valid user/password: {path}")
        return lines[0].strip(), lines[1].strip()

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
        }

    async def _docker_restart(self) -> None:
        """Restart the gluetun container to get a fresh IP."""
        result = await asyncio.to_thread(
            self._docker_run, ["restart", self._docker_container], 60)
        if result.returncode != 0:
            raise RuntimeError(
                f"docker restart failed: {result.stderr.strip() or result.stdout.strip()}")

    async def _compose_up(self) -> None:
        """Start the gluetun service via docker compose (idempotent)."""
        compose_file = os.path.join(ROOT, self._docker_compose_file)
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

    async def _check_auth_failed(self) -> bool:
        """Scan recent container logs for AUTH_FAILED (OpenVPN auth rejection)."""
        result = await asyncio.to_thread(
            self._docker_run, ["logs", "--since", "10m", self._docker_container], 30)
        if result.returncode != 0:
            return False
        return "AUTH_FAILED" in result.stdout or "auth failed" in result.stdout.lower()

    async def _wait_healthy(self, timeout: float = 120.0) -> bool:
        """Wait until the container runs AND the SOCKS5 tunnel answers."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = await self._docker_inspect()
            if info.get("running") and await self.get_public_ip():
                return True
            await asyncio.sleep(2)
        return False

    # ── State persistence ──────────────────────────────────────

    def _get_state_path(self) -> str:
        """Return path to the VPN state file."""
        return os.path.join(ROOT, "logs", "vpn_state.json")

    def save_state(self):
        """Persist IP history, stats, and circuit breaker state to disk."""
        try:
            state = {
                "ip_history": self._ip_history,
                "total_switches": self._total_switches,
                "circuit_breaker": self._circuit_breaker.get_status(),
                "current_ip": self._current_ip,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            state_path = self._get_state_path()
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            with open(state_path, "w") as f:
                json.dump(state, f, indent=2)
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
            logger.info("[vpn] state loaded: %d IPs in history, %d total switches",
                        len(self._ip_history), self._total_switches)
        except Exception as e:
            logger.debug("[vpn] failed to load state: %s", e)
