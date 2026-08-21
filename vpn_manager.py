"""
VPN Manager for OpenCode Proxy.

Manages OpenVPN connections via WSL2 or Docker to rotate IP addresses.
Each VPN session gives a fresh IP = fresh free model quota.

Modes:
- wsl2: runs OpenVPN inside WSL2 (lightweight, no Docker needed)
- docker: runs OpenVPN in a Docker container (reproducible, isolated)
"""

import os
import sys
import time
import json
import asyncio
import logging
import itertools
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Windows: hide console windows for subprocess calls
CREATE_NO_WINDOW = 0x08000000 if __import__('sys').platform == 'win32' else 0

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


# ── NordVPN App Controller ────────────────────────────────────

class NordVPNAppController:
    """Controls the NordVPN Windows desktop app via its CLI.

    Based on patterns from:
    - nordvpn-switcher-pro (Sebastian7700) - WindowsVpnController
    - NordVPN-switcher (kboghe) - cross-platform CLI control
    """

    DEFAULT_PATHS = [
        r"C:\Program Files\NordVPN\nordvpn.exe",
        r"C:\Program Files (x86)\NordVPN\nordvpn.exe",
    ]

    def __init__(self, exe_path: str = None):
        self._exe = exe_path or self._find_nordvpn()
        self._connected = False
        self._current_country = ""
        self._current_city = ""

    def _find_nordvpn(self) -> Optional[str]:
        """Find nordvpn.exe in PATH or common locations."""
        import shutil
        # Check PATH
        path = shutil.which("nordvpn")
        if path:
            return path
        # Check common install locations
        for p in self.DEFAULT_PATHS:
            if os.path.exists(p):
                return p
        return None

    def _run(self, args: list, timeout: int = 30) -> subprocess.CompletedProcess:
        """Run a nordvpn CLI command without popup windows."""
        if not self._exe:
            raise RuntimeError("NordVPN not found. Install from https://nordvpn.com/download/")
        return subprocess.run(
            [self._exe] + args,
            capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )

    async def connect(self, country: str = "", city: str = "", group: str = "") -> str:
        """Connect via NordVPN app. Returns the new public IP."""
        args = ["connect"]
        if country:
            args.append(country)
            if city:
                args.append(city)
        elif group:
            args += ["--group", group]

        logger.info("[nordvpn-app] connecting: %s", " ".join(args))
        result = self._run(args, timeout=30)

        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"NordVPN connect failed: {error}")

        # Wait for connection to establish
        await asyncio.sleep(5)

        # Verify connection
        if not self.is_connected():
            raise RuntimeError("NordVPN connected but status check failed")

        self._connected = True
        self._current_country = country
        self._current_city = city

        # Get IP
        ip = await self._get_ip()
        logger.info("[nordvpn-app] connected → IP %s (country: %s)", ip, country or "auto")
        return ip

    async def disconnect(self):
        """Disconnect NordVPN."""
        result = self._run(["disconnect"], timeout=15)
        self._connected = False
        self._current_country = ""
        self._current_city = ""
        logger.info("[nordvpn-app] disconnected")

    def is_connected(self) -> bool:
        """Check if NordVPN is connected."""
        try:
            result = self._run(["status"], timeout=10)
            self._connected = result.returncode == 0 and "Connected" in result.stdout
            return self._connected
        except Exception:
            return False

    def get_status(self) -> dict:
        """Parse nordvpn status output into structured data."""
        try:
            result = self._run(["status"], timeout=10)
            if result.returncode != 0:
                return {"connected": False, "error": result.stdout.strip()}

            output = result.stdout
            status = {"connected": "Connected" in output}

            # Parse fields from status output
            for line in output.split("\n"):
                line = line.strip()
                if line.startswith("Country:"):
                    status["country"] = line.split(":", 1)[1].strip()
                elif line.startswith("City:"):
                    status["city"] = line.split(":", 1)[1].strip()
                elif line.startswith("Server:"):
                    status["server"] = line.split(":", 1)[1].strip()
                elif line.startswith("IP:"):
                    status["ip"] = line.split(":", 1)[1].strip()
                elif line.startswith("Transfer:"):
                    status["transfer"] = line.split(":", 1)[1].strip()
                elif line.startswith("Uptime:"):
                    status["uptime"] = line.split(":", 1)[1].strip()

            return status
        except Exception as e:
            return {"connected": False, "error": str(e)}

    def list_countries(self) -> list[str]:
        """List available countries."""
        result = self._run(["countries"], timeout=15)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.split("\n") if line.strip()]

    def list_cities(self, country: str) -> list[str]:
        """List cities in a country."""
        result = self._run(["cities", country], timeout=15)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.split("\n") if line.strip()]

    async def _get_ip(self) -> str:
        """Get current public IP."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get("https://api.ipify.org")
                return resp.text.strip()
        except Exception:
            return "unknown"

    @property
    def available(self) -> bool:
        """Check if NordVPN is installed."""
        return self._exe is not None


class VPNManager:
    """Manages VPN connections for IP rotation via WSL2 or Docker."""

    def __init__(self, config: dict):
        self._config = config
        self._mode = config.get("mode", "wsl2")  # wsl2 | docker | native | nordvpn-app | auto

        # Auto-detect best mode if set to "auto"
        if self._mode == "auto":
            self._mode = self._detect_best_mode()

        # Servers can be at top level or nested under "openvpn"
        openvpn_cfg = config.get("openvpn", {})
        self._servers = config.get("servers") or openvpn_cfg.get("servers", [])
        self._vpn_config_dir = openvpn_cfg.get("configs_dir") or config.get("configs_dir", os.path.join(ROOT, "vpn", "configs"))
        self._auth_file = openvpn_cfg.get("auth_file") or config.get("auth_file", os.path.join(ROOT, "vpn", "credentials.txt"))
        self._proxy_port = config.get("vpn_proxy_port", 8888)
        self._quota_per_ip = config.get("quota_per_ip", 300)
        self._enabled = config.get("enabled", False)
        self._switch_delay = config.get("switch_delay", 5)

        # Proxy mode: vpn | socks5 | direct
        self._proxy_mode = config.get("proxy_mode", "vpn")

        # Rotation rules by model pattern
        self._rotation_rules: list[dict] = config.get("rotation_rules", [])
        self._current_rule: Optional[dict] = None

        # Docker settings
        self._docker_image = config.get("docker_image", "openvpn-nordvpn")
        self._docker_container = "opencode-vpn"

        # SOCKS5 proxy settings
        socks5_cfg = config.get("socks5_proxies", {})
        self._socks5_proxies: list[dict] = socks5_cfg.get("list", [])
        self._socks5_rotate: bool = socks5_cfg.get("rotate_socks5", True)
        self._socks5_cycle = None
        self._socks5_current_index: int = -1
        if self._socks5_proxies:
            enabled = [p for p in self._socks5_proxies if p.get("enabled", True)]
            if enabled:
                self._socks5_cycle = itertools.cycle(enabled)

        # State
        self._cycle = itertools.cycle(self._servers) if self._servers else None
        self._current_server = None
        self._current_ip: Optional[str] = None
        self._connected_at: Optional[float] = None
        self._lock = asyncio.Lock()
        self._status = VPNState.DISCONNECTED
        self._error: Optional[str] = None
        self._auth_locked_until: float = 0.0
        self._total_switches = 0

        # NordVPN API integration (optional, enabled via config)
        self._nordvpn_api = None
        self._server_scorer = None
        if config.get("use_nordvpn_api", False):
            try:
                from nordvpn_api import get_nordvpn_client
                from server_scorer import ServerScorer
                cache_ttl = config.get("api_cache_ttl", 900)
                self._nordvpn_api = get_nordvpn_client(cache_ttl=cache_ttl)
                self._server_scorer = ServerScorer()
                # Set country preferences from config
                geo = config.get("geo_filter", {})
                self._server_scorer.set_preferences(
                    preferred_countries=geo.get("include", []),
                    excluded_countries=geo.get("exclude", []),
                )
                logger.info("[vpn] NordVPN API integration enabled")
            except ImportError:
                logger.debug("[vpn] nordvpn_api/server_scorer modules not available")
        self._ip_history: list[dict] = []

        # Reliability components
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=config.get("circuit_breaker_threshold", 3),
            recovery_time=config.get("circuit_breaker_recovery", 300),
        )
        self._backoff = BackoffTimer(
            base_delay=config.get("switch_delay", 5),
            max_delay=config.get("backoff_max_delay", 60),
        )

        # Check if Docker VPN is already running on startup
        if self._mode == "docker":
            self._check_existing_docker()

        # Clean up unused .ovpn files on startup
        self._cleanup_old_configs()

        # Load persisted state
        self.load_state()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value

    @property
    def current_ip(self) -> Optional[str]:
        return self._current_ip

    @property
    def current_server(self) -> Optional[dict]:
        return self._current_server

    @property
    def status(self) -> str:
        return self._status

    @property
    def proxy_url(self) -> str:
        """Return the local proxy URL for routing requests through VPN."""
        return f"http://127.0.0.1:{self._proxy_port}"

    @property
    def socks5_url(self) -> str:
        """Return the SOCKS5 proxy URL for Docker/Gluetun mode."""
        return f"socks5://127.0.0.1:{self._config.get('docker_socks5_port', 1080)}"

    # ── Rotation Rules ──────────────────────────────────────────

    def get_rotation_rule(self, model_name: str) -> Optional[dict]:
        """Find the matching rotation rule for a model name.

        Rules are checked in order; first match wins.
        Each rule can have:
        - model_pattern: glob pattern (e.g. "mimo-*-free")
        - strategy: "round-robin" | "random" | "latency" | "geo"
        - countries: list of country codes
        - quota: requests per IP for this model
        """
        import fnmatch
        for rule in self._rotation_rules:
            pattern = rule.get("model_pattern", "")
            if pattern and fnmatch.fnmatch(model_name, pattern):
                return rule
        return None

    def set_rotation_rules(self, rules: list[dict]):
        """Update rotation rules."""
        self._rotation_rules = rules
        logger.info("[vpn] rotation rules updated: %d rules", len(rules))

    # ── NordVPN API Integration ────────────────────────────────

    async def discover_servers(
        self,
        country_code: str = None,
        group: str = None,
        protocol: str = "openvpn_udp",
        limit: int = 5,
    ) -> list[dict]:
        """Discover servers via NordVPN API and optionally download .ovpn configs.

        Returns list of server dicts with name, hostname, country, load, etc.
        """
        if not self._nordvpn_api:
            logger.warning("[vpn] NordVPN API not enabled (set use_nordvpn_api: true)")
            return []

        servers = await self._nordvpn_api.get_recommendations(
            country_code=country_code,
            group=group,
            protocol=protocol,
            limit=limit,
        )

        result = []
        for s in servers:
            # Update scorer
            if self._server_scorer:
                self._server_scorer.update_server(
                    name=s.name, hostname=s.hostname,
                    country_code=s.country_code, city=s.city,
                    load=s.load,
                )

            result.append({
                "name": s.name,
                "hostname": s.hostname,
                "country": s.country,
                "country_code": s.country_code,
                "city": s.city,
                "load": s.load,
                "openvpn_udp": s.openvpn_udp,
                "openvpn_tcp": s.openvpn_tcp,
            })

        return result

    async def discover_and_add_servers(
        self,
        country_code: str = None,
        group: str = None,
        protocol: str = "openvpn_udp",
        limit: int = 5,
    ) -> list[dict]:
        """Discover servers and add them to the rotation list.

        Downloads .ovpn configs for each server.
        """
        servers = await self.discover_servers(
            country_code=country_code,
            group=group,
            protocol=protocol,
            limit=limit,
        )

        added = []
        for s in servers:
            # Check if already in list
            if any(existing.get("name") == s["name"] for existing in self._servers):
                continue

            # Download .ovpn config
            if self._nordvpn_api:
                from nordvpn_api import ServerInfo
                server_info = ServerInfo(
                    id=0, name=s["name"], hostname=s["hostname"],
                    ip_address="", country=s["country"],
                    country_code=s["country_code"], city=s["city"],
                    load=s["load"], openvpn_udp=s["openvpn_udp"],
                    openvpn_tcp=s["openvpn_tcp"],
                )
                config_path = await self._nordvpn_api.download_ovpn_config(
                    server_info, self._vpn_config_dir
                )
                if config_path:
                    s["config"] = config_path
                    self.add_server(s["name"], config_path)
                    added.append(s)

        if added:
            logger.info("[vpn] discovered and added %d servers via API", len(added))

        return added

    async def get_countries(self) -> list[dict]:
        """Get available countries from NordVPN API."""
        if not self._nordvpn_api:
            return []
        return await self._nordvpn_api.get_countries()

    async def connect_next(self, max_retries: int = 3) -> str:
        """Disconnect current VPN and connect to next server.

        Uses circuit breaker to skip failing servers, exponential backoff
        on failures, and auto-retry on AUTH_FAILED.
        """
        async with self._lock:
            if not self._servers:
                raise RuntimeError("No VPN servers configured")

            now = time.monotonic()
            if now < self._auth_locked_until:
                remaining = int(self._auth_locked_until - now)
                raise RuntimeError(
                    f"NordVPN credentials locked (cooldown {remaining}s left)"
                )

            last_error = None
            servers_tried = 0

            for attempt in range(max_retries + len(self._servers)):
                # State transition: -> connecting
                if not VPNState.can_transition(self._status, VPNState.CONNECTING):
                    logger.debug("[vpn] state transition blocked: %s -> connecting", self._status)
                    continue

                await self._disconnect()

                self._current_server = next(self._cycle)
                server_name = self._current_server.get("name", "?")

                # Skip servers with open circuit breaker
                if not self._circuit_breaker.is_available(server_name):
                    logger.info("[vpn] skipping %s (circuit breaker open)", server_name)
                    continue

                self._status = VPNState.CONNECTING
                self._error = None
                servers_tried += 1

                logger.info("[vpn] connecting to %s via %s... (attempt %d/%d)",
                            server_name, self._mode, servers_tried, max_retries)

                try:
                    if self._mode == "docker":
                        await self._connect_docker()
                    elif self._mode == "native":
                        await self._connect_native()
                    elif self._mode == "nordvpn-app":
                        await self._connect_nordvpn_app()
                    else:
                        await self._connect_wsl2()

                    # Wait for tunnel + proxy to be ready
                    await asyncio.sleep(self._switch_delay)
                    self._current_ip = await self._get_public_ip()
                    self._connected_at = time.monotonic()
                    self._status = VPNState.CONNECTED
                    self._total_switches += 1

                    # Record success
                    self._circuit_breaker.record_success(server_name)
                    self._backoff.record_success()

                    self._ip_history.append({
                        "ip": self._current_ip,
                        "server": server_name,
                        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    })
                    if len(self._ip_history) > 100:
                        self._ip_history = self._ip_history[-100:]

                    logger.info("[vpn] connected → IP %s (server: %s, mode: %s)",
                                self._current_ip, server_name, self._mode)

                    # Persist state periodically (every 10 switches)
                    if self._total_switches % 10 == 0:
                        self.save_state()

                    return self._current_ip

                except Exception as e:
                    error_msg = str(e)
                    last_error = e

                    # Record failure
                    self._circuit_breaker.record_failure(server_name)
                    self._backoff.record_failure()

                    if "AUTH_FAILED" in error_msg or "auth-failed" in error_msg.lower():
                        logger.warning("[vpn] AUTH_FAILED on %s, trying next server... (%d/%d)",
                                       server_name, servers_tried, max_retries)
                        await asyncio.sleep(2)
                        continue
                    else:
                        # Non-auth error on first attempt: raise immediately
                        self._status = VPNState.ERROR
                        self._error = error_msg
                        logger.error("[vpn] connection failed: %s", e)
                        raise

            # All retries exhausted
            self._status = VPNState.ERROR
            self._error = f"All {servers_tried} servers failed (last: {last_error})"
            logger.error("[vpn] all %d server attempts failed", servers_tried)
            raise RuntimeError(f"All {servers_tried} servers failed: {last_error}")

    async def connect_wait(self) -> str:
        """Wait for the user to connect externally, detect IP change."""
        async with self._lock:
            if not VPNState.can_transition(self._status, VPNState.CONNECTING):
                raise RuntimeError(f"Cannot transition from {self._status} to connecting")
            self._status = VPNState.CONNECTING
            self._error = None
            logger.info("[vpn] waiting for external VPN connection...")

        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                old_ip = (await client.get("https://api.ipify.org")).text.strip()
        except Exception:
            old_ip = "unknown"

        logger.info("[vpn] current IP: %s. Waiting for VPN connection...", old_ip)

        for _ in range(40):
            await asyncio.sleep(3)
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10) as client:
                    new_ip = (await client.get("https://api.ipify.org")).text.strip()
                if new_ip and new_ip != old_ip:
                    async with self._lock:
                        self._current_ip = new_ip
                        self._connected_at = time.monotonic()
                        self._status = VPNState.CONNECTED
                        self._total_switches += 1
                        self._ip_history.append({
                            "ip": new_ip, "server": "External VPN",
                            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        })
                    logger.info("[vpn] VPN detected → IP %s", new_ip)
                    return new_ip
            except Exception:
                pass

        self._status = VPNState.ERROR
        self._error = "Timeout waiting for VPN connection"
        raise RuntimeError("No VPN IP change detected after 120s")

    async def disconnect(self):
        """Disconnect the current VPN connection."""
        async with self._lock:
            await self._disconnect()

    async def _disconnect(self):
        """Stop VPN process/container."""
        if self._status == VPNState.DISCONNECTED:
            return

        if self._mode == "docker":
            await self._stop_docker()
        elif self._mode == "native":
            await self._stop_native()
        elif self._mode == "nordvpn-app":
            await self._stop_nordvpn_app()
        else:
            await self._stop_wsl2()
        self._current_ip = None
        self._connected_at = None
        self._status = VPNState.DISCONNECTED
        logger.info("[vpn] disconnected")

    # ── WSL2 mode ──────────────────────────────────────────────

    async def _connect_wsl2(self):
        """Connect via OpenVPN inside WSL2."""
        config_path = self._current_server.get("config", "")
        if not config_path or not os.path.exists(config_path):
            raise FileNotFoundError(f"Config not found: {config_path}")

        # Convert Windows path to WSL path
        wsl_config = self._win_to_wsl(config_path)
        wsl_creds = self._win_to_wsl(self._auth_file)

        # Ensure WSL2 has openvpn + tinyproxy
        await self._wsl_setup()

        # Kill any existing VPN in WSL2
        await self._run_wsl("sudo killall openvpn 2>/dev/null; sudo killall tinyproxy 2>/dev/null")
        await asyncio.sleep(1)

        # Start OpenVPN in background
        cmd = (
            f"sudo openvpn --config {wsl_config} --auth-user-pass {wsl_creds} "
            f"--auth-nocache --daemon --log /tmp/openvpn.log"
        )
        await self._run_wsl(cmd)

        # Wait for tun interface
        logger.info("[vpn] waiting for tun0 in WSL2...")
        for _ in range(30):
            ret = await self._run_wsl("ip link show tun0 2>/dev/null", check=False)
            if ret == 0:
                logger.info("[vpn] tun0 is up")
                break
            await asyncio.sleep(1)
        else:
            # Read log for error
            log = await self._run_wsl("cat /tmp/openvpn.log 2>/dev/null | tail -5", check=False)
            raise RuntimeError(f"tun0 not ready in WSL2. OpenVPN log: {log}")

        # Start tinyproxy
        await self._run_wsl(f"sudo tinyproxy -d 2>/dev/null &", check=False)
        await asyncio.sleep(1)

        # Verify proxy works
        ip = await self._get_public_ip()
        logger.info("[vpn] WSL2 VPN ready, IP: %s", ip)

    async def _stop_wsl2(self):
        """Stop VPN processes in WSL2."""
        await self._run_wsl("sudo killall openvpn 2>/dev/null; sudo killall tinyproxy 2>/dev/null", check=False)

    # ── Native Windows mode ────────────────────────────────────

    async def _connect_native(self):
        """Connect via OpenVPN directly on Windows (no WSL2/Docker).

        Requires OpenVPN installed on Windows (typically in C:\\Program Files\\OpenVPN).
        Uses a local HTTP proxy (tinyproxy or similar) for routing.
        """
        import shutil
        config_path = self._current_server.get("config", "")
        if not config_path or not os.path.exists(config_path):
            raise FileNotFoundError(f"Config not found: {config_path}")

        # Find openvpn.exe
        openvpn_exe = shutil.which("openvpn")
        if not openvpn_exe:
            # Check common Windows paths
            win_paths = [
                r"C:\Program Files\OpenVPN\bin\openvpn.exe",
                r"C:\Program Files (x86)\OpenVPN\bin\openvpn.exe",
            ]
            for p in win_paths:
                if os.path.exists(p):
                    openvpn_exe = p
                    break
        if not openvpn_exe:
            raise RuntimeError(
                "OpenVPN not found. Install from https://openvpn.net/community/ "
                "or add openvpn.exe to PATH"
            )

        # Kill any existing OpenVPN process
        subprocess.run(
            ["taskkill", "/F", "/IM", "openvpn.exe"],
            capture_output=True, creationflags=CREATE_NO_WINDOW,
        )
        await asyncio.sleep(1)

        # Start OpenVPN in background
        abs_config = os.path.abspath(config_path)
        abs_creds = os.path.abspath(self._auth_file)
        proc = await asyncio.create_subprocess_exec(
            openvpn_exe,
            "--config", abs_config,
            "--auth-user-pass", abs_creds,
            "--auth-nocache",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        )

        # Wait for tun interface to appear
        logger.info("[vpn] waiting for tun interface on Windows...")
        for _ in range(30):
            # Check if tun interface exists via ipconfig
            ret = subprocess.run(
                ["ipconfig"],
                capture_output=True, text=True, timeout=5,
                creationflags=CREATE_NO_WINDOW,
            )
            if "TAP" in ret.stdout or "tun" in ret.stdout.lower():
                logger.info("[vpn] tun interface is up")
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError("TUN interface not ready after 30s")

        # Verify IP changed
        ip = await self._get_public_ip()
        logger.info("[vpn] native Windows VPN ready, IP: %s", ip)

    async def _stop_native(self):
        """Stop OpenVPN process on Windows."""
        subprocess.run(
            ["taskkill", "/F", "/IM", "openvpn.exe"],
            capture_output=True, creationflags=CREATE_NO_WINDOW,
        )

    # ── NordVPN App mode ───────────────────────────────────────

    async def _connect_nordvpn_app(self):
        """Connect via NordVPN desktop app on Windows."""
        exe_path = self._config.get("nordvpn_exe", "")
        if not hasattr(self, '_nordvpn_controller') or not self._nordvpn_controller:
            self._nordvpn_controller = NordVPNAppController(exe_path=exe_path or None)

        if not self._nordvpn_controller.available:
            raise RuntimeError(
                "NordVPN not found. Install from https://nordvpn.com/download/ "
                "or set nordvpn_exe in config"
            )

        # Get country/group from config or current server
        country = self._config.get("nordvpn_country", "")
        group = self._config.get("nordvpn_group", "")
        if self._current_server:
            country = country or self._current_server.get("country", "")

        ip = await self._nordvpn_controller.connect(country=country, group=group)
        self._current_ip = ip

    async def _stop_nordvpn_app(self):
        """Disconnect NordVPN desktop app."""
        if hasattr(self, '_nordvpn_controller') and self._nordvpn_controller:
            await self._nordvpn_controller.disconnect()

    async def _wsl_setup(self):
        """Ensure WSL2 has openvpn + tinyproxy installed."""
        ret = await self._run_wsl("which openvpn 2>/dev/null", check=False)
        if ret != 0:
            logger.info("[vpn] installing openvpn + tinyproxy in WSL2...")
            await self._run_wsl("sudo apt-get update -qq && sudo apt-get install -y -qq openvpn tinyproxy")

    async def _run_wsl(self, cmd: str, check: bool = True) -> int:
        """Run a command inside WSL2."""
        proc = await asyncio.create_subprocess_exec(
            "wsl", "-d", "Ubuntu-22.04", "--", "bash", "-c", cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        stdout, stderr = await proc.communicate()
        if check and proc.returncode != 0:
            err = stderr.decode(errors="replace")[:500]
            logger.debug("[vpn] WSL command failed: %s → %s", cmd[:80], err)
        return proc.returncode

    def _win_to_wsl(self, win_path: str) -> str:
        """Convert Windows path to WSL path."""
        abs_path = os.path.abspath(win_path)
        # C:\path → /mnt/c/path
        wsl = abs_path.replace("\\", "/")
        if len(wsl) >= 2 and wsl[1] == ":":
            drive = wsl[0].lower()
            wsl = f"/mnt/{drive}{wsl[2:]}"
        return wsl

    # ── Docker mode ────────────────────────────────────────────

    def _find_docker(self) -> str:
        """Find Docker executable."""
        import shutil
        docker_path = shutil.which("docker")
        if docker_path:
            return docker_path
        # Windows common paths
        win_paths = [
            r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
            r"C:\Program Files (x86)\Docker\Docker\resources\bin\docker.exe",
        ]
        for p in win_paths:
            if os.path.exists(p):
                return p
        return "docker"

    async def _connect_docker(self):
        """Connect via Docker container. Adaptative based on image type.

        Supports 3 image types:
        - gluetun (qmcgaw/gluetun): multi-provider, uses env vars
        - nordvpn-official: our Dockerfile.nordvpn with NordVPN CLI
        - custom: original OpenVPN + tinyproxy setup
        """
        docker_cmd = self._find_docker()
        container = self._docker_container
        image = self._docker_image

        # Determine image type
        is_gluetun = "gluetun" in image.lower()
        is_nordvpn_official = image == "nordvpn-official"

        # Stop existing container
        await self._stop_docker()

        # Build common args
        cmd = [
            docker_cmd, "run", "-d",
            "--name", container,
            "--cap-add", "NET_ADMIN",
            "--device", "/dev/net/tun",
            "-p", f"{self._proxy_port}:8888",
        ]

        if is_gluetun:
            # ── Gluetun mode ──
            # Read credentials
            username, password = self._read_credentials()
            country = self._config.get("nordvpn_country", "")

            cmd += [
                "-e", "VPN_SERVICE_PROVIDER=nordvpn",
                "-e", "VPN_TYPE=openvpn",
                "-e", f"OPENVPN_USER={username}",
                "-e", f"OPENVPN_PASSWORD={password}",
                "-e", "HTTP_PROXY=on",
                "-e", f"HTTP_PROXY_PORT=8888",
                "-e", "SOCKS5_PROXY=on",
                "-e", "SOCKS5_PROXY_PORT=1080",
            ]
            if self._config.get("docker_dns_over_tls", True):
                cmd += ["-e", "DNS_OVER_TLS=on"]
            if country:
                cmd += ["-e", f"SERVER_COUNTRIES={country}"]

            cmd.append(image)

        elif is_nordvpn_official:
            # ── NordVPN Official mode ──
            # Build image if needed
            await self._docker_build_nordvpn()

            token = self._config.get("nordvpn_token", "")
            country = self._config.get("nordvpn_country", "")
            technology = self._config.get("nordvpn_technology", "NordLynx")

            if not token:
                raise RuntimeError("nordvpn_token required for nordvpn-official image")

            cmd += [
                "-e", f"NORDVPN_TOKEN={token}",
                "-e", f"NORDVPN_TECHNOLOGY={technology}",
                "-e", f"VPN_PROXY_PORT={self._proxy_port}",
            ]
            if country:
                cmd += ["-e", f"NORDVPN_COUNTRY={country}"]
            if self._config.get("docker_killswitch", True):
                cmd += ["-e", "NORDVPN_KILLSWITCH=on"]

            cmd.append(image)

        else:
            # ── Custom / legacy mode (OpenVPN + tinyproxy) ──
            config_path = self._current_server.get("config", "")
            if not config_path or not os.path.exists(config_path):
                raise FileNotFoundError(f"Config not found: {config_path}")

            # Ensure image exists
            await self._docker_build()

            abs_config = os.path.abspath(config_path).replace("\\", "/")
            abs_creds = os.path.abspath(self._auth_file).replace("\\", "/")

            cmd += [
                "-v", f"{abs_config}:/vpn/configs/{os.path.basename(config_path)}:ro",
                "-v", f"{abs_creds}:/vpn/credentials.txt:ro",
                "--rm",
                image,
            ]

        # Run container
        logger.info("[vpn] starting Docker container (%s mode)...",
                    "gluetun" if is_gluetun else "nordvpn-official" if is_nordvpn_official else "custom")

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:500]
            raise RuntimeError(f"Docker failed: {err}")

        logger.info("[vpn] Docker container started: %s", container)

        # Wait for container to be healthy (proxy responding)
        for _ in range(30):
            ret = await self._run_docker(
                "curl -s --max-time 3 http://127.0.0.1:8888/ >/dev/null 2>&1",
                check=False
            )
            if ret == 0:
                logger.info("[vpn] Docker VPN proxy is ready")
                break
            await asyncio.sleep(2)
        else:
            # Try to get logs for debugging
            log_cmd = "cat /tmp/openvpn.log 2>/dev/null | tail -5" if not is_gluetun else "logread | tail -5"
            logs = await self._run_docker(log_cmd, check=False)
            raise RuntimeError(f"Docker VPN not ready. Log: {logs}")

    def _read_credentials(self) -> tuple[str, str]:
        """Read NordVPN credentials from file."""
        try:
            with open(self._auth_file, "r") as f:
                lines = f.read().strip().split("\n")
                if len(lines) >= 2:
                    return lines[0], lines[1]
        except Exception:
            pass
        return "", ""

    async def _docker_build_nordvpn(self):
        """Build the nordvpn-official Docker image if it doesn't exist."""
        docker_cmd = self._find_docker()
        # Check if image exists
        proc = await asyncio.create_subprocess_exec(
            docker_cmd, "image", "inspect", "nordvpn-official",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        await proc.communicate()
        if proc.returncode == 0:
            return  # image exists

        # Build from Dockerfile.nordvpn
        logger.info("[vpn] building nordvpn-official Docker image...")
        dockerfile = os.path.join(ROOT, "Dockerfile.nordvpn")
        if not os.path.exists(dockerfile):
            raise FileNotFoundError(f"Dockerfile.nordvpn not found: {dockerfile}")

        build_proc = await asyncio.create_subprocess_exec(
            docker_cmd, "build", "-t", "nordvpn-official", "-f", dockerfile, ROOT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        await build_proc.communicate()
        if build_proc.returncode != 0:
            raise RuntimeError("Failed to build nordvpn-official image")
        logger.info("[vpn] nordvpn-official image built")

    async def _stop_docker(self):
        """Stop and remove Docker VPN container."""
        docker_cmd = self._find_docker()
        proc = await asyncio.create_subprocess_exec(
            docker_cmd, "rm", "-f", self._docker_container,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        await proc.communicate()

    async def _docker_build(self):
        """Build the VPN Docker image if it doesn't exist."""
        docker_cmd = self._find_docker()
        proc = await asyncio.create_subprocess_exec(
            docker_cmd, "image", "inspect", self._docker_image,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        await proc.communicate()
        if proc.returncode != 0:
            logger.info("[vpn] building Docker VPN image...")
            dockerfile = os.path.join(ROOT, "Dockerfile.vpn")
            build_proc = await asyncio.create_subprocess_exec(
                docker_cmd, "build", "-t", self._docker_image, "-f", dockerfile, ROOT,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
            )
            await build_proc.communicate()
            if build_proc.returncode != 0:
                raise RuntimeError("Docker build failed")
            logger.info("[vpn] Docker image built: %s", self._docker_image)

    async def _run_docker(self, cmd: str, check: bool = True) -> int:
        """Run a command on the Docker VPN container."""
        docker_cmd = self._find_docker()
        proc = await asyncio.create_subprocess_exec(
            docker_cmd, "exec", self._docker_container,
            "bash", "-c", cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode

    # ── SOCKS5 Proxy Management ──────────────────────────────

    def _rebuild_socks5_cycle(self):
        """Rebuild the SOCKS5 round-robin cycle from enabled proxies."""
        enabled = [p for p in self._socks5_proxies if p.get("enabled", True)]
        self._socks5_cycle = itertools.cycle(enabled) if enabled else None

    @property
    def proxy_mode(self) -> str:
        return self._proxy_mode

    @proxy_mode.setter
    def proxy_mode(self, value: str):
        if value in ("vpn", "socks5", "direct"):
            self._proxy_mode = value

    @property
    def socks5_rotate(self) -> bool:
        return self._socks5_rotate

    @socks5_rotate.setter
    def socks5_rotate(self, value: bool):
        self._socks5_rotate = value

    def get_socks5_proxies(self) -> list[dict]:
        """Return the list of SOCKS5 proxies (without passwords)."""
        return [
            {"host": p["host"], "port": p["port"],
             "username": p.get("username", ""), "enabled": p.get("enabled", True),
             "has_password": bool(p.get("password"))}
            for p in self._socks5_proxies
        ]

    def get_next_socks5_proxy(self) -> Optional[dict]:
        """Get next SOCKS5 proxy via round-robin. Returns None if no proxies."""
        if not self._socks5_cycle:
            return None
        try:
            proxy = next(self._socks5_cycle)
            return {"host": proxy["host"], "port": proxy["port"],
                    "username": proxy.get("username", ""),
                    "password": proxy.get("password", "")}
        except StopIteration:
            return None

    def get_socks5_proxy_url(self) -> Optional[str]:
        """Get the SOCKS5 proxy URL for the current proxy in rotation."""
        proxy = self.get_next_socks5_proxy()
        if not proxy:
            return None
        if proxy.get("username"):
            return f"socks5://{proxy['username']}:{proxy['password']}@{proxy['host']}:{proxy['port']}"
        return f"socks5://{proxy['host']}:{proxy['port']}"

    def add_socks5_proxy(self, host: str, port: int, username: str = "", password: str = ""):
        """Add a SOCKS5 proxy to the list."""
        self._socks5_proxies.append({
            "host": host, "port": port,
            "username": username, "password": password,
            "enabled": True,
        })
        self._rebuild_socks5_cycle()
        logger.info("[vpn] SOCKS5 proxy added: %s:%d", host, port)

    def remove_socks5_proxy(self, index: int):
        """Remove a SOCKS5 proxy by index."""
        if 0 <= index < len(self._socks5_proxies):
            removed = self._socks5_proxies.pop(index)
            self._rebuild_socks5_cycle()
            logger.info("[vpn] SOCKS5 proxy removed: %s:%d", removed.get("host"), removed.get("port"))

    def toggle_socks5_proxy(self, index: int, enabled: bool):
        """Enable or disable a SOCKS5 proxy by index."""
        if 0 <= index < len(self._socks5_proxies):
            self._socks5_proxies[index]["enabled"] = enabled
            self._rebuild_socks5_cycle()

    async def test_socks5_proxy(self, host: str, port: int) -> dict:
        """Test a SOCKS5 proxy connection.

        Returns dict with keys: ok, ip, opencode_ok, error
        """
        import httpx
        proxy_url = f"socks5://{host}:{port}"
        result = {"ok": False, "ip": None, "opencode_ok": False, "error": None}

        # Test 1: ipify.org via SOCKS5
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=15) as client:
                resp = await client.get("https://api.ipify.org")
                if resp.status_code == 200:
                    result["ok"] = True
                    result["ip"] = resp.text.strip()
                else:
                    result["error"] = f"ipify returned {resp.status_code}"
        except Exception as e:
            result["error"] = f"SOCKS5 connection failed: {e}"
            return result

        # Test 2: opencode.ai via SOCKS5
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=15) as client:
                resp = await client.get("https://opencode.ai")
                result["opencode_ok"] = resp.status_code < 500
        except Exception as e:
            logger.debug("[vpn] SOCKS5 test opencode.ai failed: %s", e)

        return result

    # ── Common ─────────────────────────────────────────────────

    def _check_existing_docker(self):
        """Check if Docker VPN container is already running on startup."""
        try:
            docker_cmd = self._find_docker()
            r = subprocess.run(
                [docker_cmd, "ps", "-a", "--filter", "name=" + self._docker_container, "--format", "{{.Status}}"],
                capture_output=True, text=True, timeout=5,
                creationflags=CREATE_NO_WINDOW,
                encoding="utf-8", errors="replace",
            )
            if "Up" in r.stdout:
                logger.info("[vpn] Docker VPN container already running")
                # Get the IP from the container logs
                r2 = subprocess.run(
                    [docker_cmd, "logs", self._docker_container],
                    capture_output=True, text=True, timeout=5,
                    creationflags=CREATE_NO_WINDOW,
                    encoding="utf-8", errors="replace",
                )
                for line in r2.stdout.split("\n"):
                    ip = None
                    # Legacy format: "VPN IP: x.x.x.x"
                    if "VPN IP:" in line:
                        ip = line.split("VPN IP:")[-1].strip()
                    # Gluetun format: "Public IP address is x.x.x.x"
                    elif "Public IP address is" in line:
                        ip = line.split("Public IP address is")[-1].strip().split()[0]
                    if ip and ip != "unknown":
                        self._current_ip = ip
                        self._status = VPNState.CONNECTED
                        self._connected_at = time.monotonic()
                        self._ip_history.append({
                            "ip": ip, "server": "Docker VPN (existing)",
                            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        })
                        logger.info("[vpn] detected existing VPN connection, IP: %s", ip)
                        return
        except Exception as e:
            logger.debug("[vpn] could not check existing Docker container: %s", e)

    async def _get_public_ip(self) -> str:
        """Get current public IP by querying an external service."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get("https://api.ipify.org")
                return resp.text.strip()
        except Exception as e:
            logger.warning("[vpn] failed to get public IP: %s", e)
            return "unknown"

    def _cleanup_old_configs(self):
        """Remove .ovpn files that are not in the current server list."""
        try:
            configs_dir = self._vpn_config_dir
            if not os.path.isdir(configs_dir):
                return

            # Collect config paths that are in the server list
            active_configs = set()
            for server in self._servers:
                config = server.get("config", "")
                if config:
                    active_configs.add(os.path.basename(config))

            # Find and remove .ovpn files not in the list
            removed = 0
            for f in os.listdir(configs_dir):
                if f.endswith(".ovpn") and f not in active_configs:
                    filepath = os.path.join(configs_dir, f)
                    try:
                        os.remove(filepath)
                        removed += 1
                        logger.info("[vpn] removed unused config: %s", f)
                    except OSError as e:
                        logger.debug("[vpn] could not remove %s: %s", f, e)

            if removed:
                logger.info("[vpn] cleaned up %d unused .ovpn files", removed)

        except Exception as e:
            logger.debug("[vpn] config cleanup failed: %s", e)

    async def health_check(self) -> dict:
        """Run a health check on the current VPN connection.

        Returns dict with: ok, ip_changed, latency_ms, error
        """
        result = {"ok": False, "ip_changed": False, "latency_ms": None, "error": None}

        if self._status != "connected":
            result["error"] = "Not connected"
            return result

        try:
            import httpx
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get("https://api.ipify.org")
                elapsed_ms = int((time.monotonic() - start) * 1000)

            new_ip = resp.text.strip()
            result["latency_ms"] = elapsed_ms

            if new_ip and new_ip != "unknown":
                result["ok"] = True
                if new_ip != self._current_ip:
                    result["ip_changed"] = True
                    logger.warning("[vpn] health check: IP changed from %s to %s",
                                   self._current_ip, new_ip)
                    self._current_ip = new_ip
            else:
                result["error"] = "Could not determine IP"

        except Exception as e:
            result["error"] = str(e)
            logger.error("[vpn] health check failed: %s", e)

        return result

    async def start_watchdog(self, interval: float = 60.0):
        """Background watchdog that checks VPN health periodically.

        If the connection is lost, attempts automatic reconnection.
        Runs until _watchdog_stop is set.
        """
        self._watchdog_stop = asyncio.Event()
        self._watchdog_interval = interval
        logger.info("[vpn-watchdog] started (interval: %ds)", interval)

        while not self._watchdog_stop.is_set():
            try:
                await asyncio.wait_for(self._watchdog_stop.wait(), timeout=interval)
                break  # stop requested
            except asyncio.TimeoutError:
                pass

            # Only check if we think we're connected
            if self._status != VPNState.CONNECTED:
                continue

            try:
                result = await self.health_check()
                if not result["ok"]:
                    logger.warning("[vpn-watchdog] health check failed: %s, attempting reconnect",
                                   result["error"])
                    try:
                        await self.connect_next()
                        logger.info("[vpn-watchdog] reconnected successfully")
                    except Exception as e:
                        logger.error("[vpn-watchdog] reconnect failed: %s", e)
                elif result["ip_changed"]:
                    logger.info("[vpn-watchdog] IP changed to %s (expected)", self._current_ip)
            except Exception as e:
                logger.debug("[vpn-watchdog] check error: %s", e)

        logger.info("[vpn-watchdog] stopped")

    def stop_watchdog(self):
        """Stop the background watchdog task."""
        if hasattr(self, '_watchdog_stop'):
            self._watchdog_stop.set()

    def restart_watchdog(self, interval: float = None):
        """Restart watchdog with new interval (hot-reload)."""
        if interval is None:
            interval = self._config.get("watchdog_interval", 60)
        self.stop_watchdog()
        asyncio.create_task(self.start_watchdog(interval=interval))
        logger.info("[vpn-watchdog] restarted (interval: %ds)", interval)

    # ── Schedule-based Rotation ─────────────────────────────────

    async def start_scheduler(self, schedule_config: dict = None):
        """Background scheduler for time-based IP rotation.

        Args:
            schedule_config: dict with rules like:
                {"enabled": true, "rules": [
                    {"cron": "0 */2 * * *", "action": "rotate"},
                    {"cron": "0 9 * * 1-5", "action": "rotate", "countries": ["FR", "DE"]}
                ]}
        """
        if not schedule_config or not schedule_config.get("enabled"):
            return

        self._scheduler_stop = asyncio.Event()
        rules = schedule_config.get("rules", [])
        logger.info("[vpn-scheduler] started with %d rules", len(rules))

        while not self._scheduler_stop.is_set():
            try:
                await asyncio.wait_for(self._scheduler_stop.wait(), timeout=60)
                break  # stop requested
            except asyncio.TimeoutError:
                pass

            # Check each rule
            now = time.localtime()
            for rule in rules:
                if self._should_trigger(rule, now):
                    logger.info("[vpn-scheduler] triggered rule: %s", rule.get("cron"))
                    try:
                        if self._enabled and self._status == VPNState.CONNECTED:
                            await self.connect_next()
                    except Exception as e:
                        logger.error("[vpn-scheduler] rotation failed: %s", e)

        logger.info("[vpn-scheduler] stopped")

    def _should_trigger(self, rule: dict, now: time.struct_time) -> bool:
        """Check if a cron-like rule should trigger at the current time.

        Simple implementation: checks minute, hour, day-of-month, month, day-of-week.
        Supports: exact values, */N (every N), ranges (1-5), lists (1,3,5).
        """
        cron = rule.get("cron", "")
        parts = cron.split()
        if len(parts) != 5:
            return False

        fields = [now.tm_min, now.tm_hour, now.tm_mday, now.tm_mon, now.tm_wday + 1]

        for part, field_value in zip(parts, fields):
            if part == "*":
                continue
            if part.startswith("*/"):
                try:
                    interval = int(part[2:])
                    if field_value % interval != 0:
                        return False
                except ValueError:
                    return False
            elif "-" in part:
                try:
                    start, end = part.split("-")
                    if not (int(start) <= field_value <= int(end)):
                        return False
                except ValueError:
                    return False
            elif "," in part:
                values = [int(v) for v in part.split(",")]
                if field_value not in values:
                    return False
            else:
                try:
                    if field_value != int(part):
                        return False
                except ValueError:
                    return False

        return True

    def stop_scheduler(self):
        """Stop the background scheduler task."""
        if hasattr(self, '_scheduler_stop'):
            self._scheduler_stop.set()

    def get_status(self) -> dict:
        """Return current VPN status for the dashboard."""
        elapsed = None
        if self._connected_at:
            elapsed = int(time.monotonic() - self._connected_at)

        return {
            "enabled": self._enabled,
            "proxy_mode": self._proxy_mode,
            "mode": self._mode,
            "status": self._status,
            "ip": self._current_ip,
            "server": self._current_server.get("name") if self._current_server else None,
            "server_config": self._current_server.get("config") if self._current_server else None,
            "connected_seconds": elapsed,
            "total_switches": self._total_switches,
            "servers_count": len(self._servers),
            "error": self._error,
            "ip_history": self._ip_history[-10:],
            "proxy_port": self._proxy_port,
            "proxy_url": self.proxy_url,
            "socks5_count": len([p for p in self._socks5_proxies if p.get("enabled", True)]),
            "socks5_current": self._socks5_current_index,
            "circuit_breaker": self._circuit_breaker.get_status(),
            "backoff_failures": self._backoff.consecutive_failures,
            "backoff_delay": self._backoff.delay,
        }

    def get_config(self) -> dict:
        """Return current configuration for the dashboard (all runtime values)."""
        return {
            "enabled": self._enabled,
            "proxy_mode": self._proxy_mode,
            "mode": self._mode,
            "servers": self._servers,
            "auth_file": self._auth_file,
            "vpn_config_dir": self._vpn_config_dir,
            "proxy_port": self._proxy_port,
            "switch_delay": self._switch_delay,
            "quota_per_ip": self._quota_per_ip,
            "docker_image": self._docker_image,
            "socks5_proxies": self.get_socks5_proxies(),
            "socks5_rotate": self._socks5_rotate,
            # NordVPN settings
            "nordvpn_token": self._config.get("nordvpn_token", ""),
            "nordvpn_country": self._config.get("nordvpn_country", ""),
            "nordvpn_group": self._config.get("nordvpn_group", ""),
            "nordvpn_technology": self._config.get("nordvpn_technology", "NordLynx"),
            "nordvpn_exe": self._config.get("nordvpn_exe", ""),
            "docker_killswitch": self._config.get("docker_killswitch", True),
            "docker_dns_over_tls": self._config.get("docker_dns_over_tls", True),
            "use_nordvpn_api": self._config.get("use_nordvpn_api", False),
            # Reliability
            "circuit_breaker_threshold": self._circuit_breaker._failure_threshold,
            "circuit_breaker_recovery": self._circuit_breaker._recovery_time,
            "backoff_max_delay": self._backoff._max_delay,
            "watchdog_interval": self._config.get("watchdog_interval", 60),
        }

    def update_config(self, updates: dict):
        """Update configuration from dashboard (hot-reload, no restart needed)."""
        if "enabled" in updates:
            self._enabled = updates["enabled"]
        if "proxy_mode" in updates:
            self._proxy_mode = updates["proxy_mode"]
        if "mode" in updates:
            self._mode = updates["mode"]
        if "proxy_port" in updates:
            self._proxy_port = updates["proxy_port"]
        if "switch_delay" in updates:
            self._switch_delay = updates["switch_delay"]
        if "quota_per_ip" in updates:
            self._quota_per_ip = updates["quota_per_ip"]
        if "auth_file" in updates:
            self._auth_file = updates["auth_file"]
        if "servers" in updates:
            self._servers = updates["servers"]
            self._cycle = itertools.cycle(self._servers) if self._servers else None
        if "socks5_rotate" in updates:
            self._socks5_rotate = updates["socks5_rotate"]
        # Docker settings
        if "docker_image" in updates:
            self._docker_image = updates["docker_image"]
        # NordVPN settings (token, country, group, technology)
        if "nordvpn_token" in updates:
            self._config["nordvpn_token"] = updates["nordvpn_token"]
        if "nordvpn_country" in updates:
            self._config["nordvpn_country"] = updates["nordvpn_country"]
        if "nordvpn_group" in updates:
            self._config["nordvpn_group"] = updates["nordvpn_group"]
        if "nordvpn_technology" in updates:
            self._config["nordvpn_technology"] = updates["nordvpn_technology"]
        if "nordvpn_exe" in updates:
            self._config["nordvpn_exe"] = updates["nordvpn_exe"]
        if "docker_killswitch" in updates:
            self._config["docker_killswitch"] = updates["docker_killswitch"]
        if "docker_dns_over_tls" in updates:
            self._config["docker_dns_over_tls"] = updates["docker_dns_over_tls"]
        if "use_nordvpn_api" in updates:
            self._config["use_nordvpn_api"] = updates["use_nordvpn_api"]
        # Circuit breaker / backoff
        if "circuit_breaker_threshold" in updates or "circuit_breaker_recovery" in updates:
            threshold = updates.get("circuit_breaker_threshold", self._circuit_breaker._failure_threshold)
            recovery = updates.get("circuit_breaker_recovery", self._circuit_breaker._recovery_time)
            self._circuit_breaker = CircuitBreaker(failure_threshold=threshold, recovery_time=recovery)
        if "backoff_max_delay" in updates:
            max_delay = updates["backoff_max_delay"]
            self._backoff = BackoffTimer(base_delay=self._switch_delay, max_delay=max_delay)
        # Watchdog interval (restart task with new interval)
        if "watchdog_interval" in updates:
            self._config["watchdog_interval"] = updates["watchdog_interval"]
            interval = updates["watchdog_interval"]
            if interval > 0:
                self.restart_watchdog(interval)
            else:
                self.stop_watchdog()

    def add_server(self, name: str, config_path: str):
        """Add a VPN server to the rotation list."""
        self._servers.append({"name": name, "config": config_path})
        self._cycle = itertools.cycle(self._servers)

    def remove_server(self, name: str):
        """Remove a VPN server from the rotation list."""
        self._servers = [s for s in self._servers if s.get("name") != name]
        self._cycle = itertools.cycle(self._servers) if self._servers else None
        if self._current_server and self._current_server.get("name") == name:
            self._current_server = None

    @staticmethod
    def _detect_best_mode() -> str:
        """Auto-detect the best VPN mode available.

        Priority: nordvpn-app > docker > wsl2 > native
        """
        import shutil

        # 1. Check NordVPN App (best for Windows)
        if sys.platform == "win32":
            from vpn_manager import NordVPNAppController
            try:
                controller = NordVPNAppController()
                if controller.available:
                    logger.info("[vpn] auto-detected: nordvpn-app")
                    return "nordvpn-app"
            except Exception:
                pass

        # 2. Check Docker
        docker_path = shutil.which("docker")
        if docker_path:
            logger.info("[vpn] auto-detected: docker")
            return "docker"

        # 3. Check WSL2
        if sys.platform == "win32":
            try:
                import subprocess
                r = subprocess.run(
                    ["wsl", "-l", "-q"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=CREATE_NO_WINDOW,
                )
                if "Ubuntu" in r.stdout:
                    logger.info("[vpn] auto-detected: wsl2")
                    return "wsl2"
            except Exception:
                pass

        # 4. Check native OpenVPN
        openvpn_path = shutil.which("openvpn")
        if openvpn_path:
            logger.info("[vpn] auto-detected: native")
            return "native"

        # Fallback
        logger.warning("[vpn] no VPN mode detected, defaulting to docker")
        return "docker"

    # ── State Persistence ───────────────────────────────────────

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
