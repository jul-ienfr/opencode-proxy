"""
OpenVPN connection manager for IP rotation.

Manages OpenVPN connections to rotate IP addresses for free model quota.
Each VPN session gives a fresh IP = fresh free model quota.
"""

import os
import sys
import json
import time
import signal
import asyncio
import logging
import itertools
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class VPNManager:
    """Manages OpenVPN connections for IP rotation.

    Each connection creates a new tun interface with a different IP.
    Free model quotas are per-IP, so rotating IPs = rotating quotas.
    """

    def __init__(self, config: dict):
        self._config = config
        self._servers = config.get("servers", [])
        self._auth_file = config.get("auth_file", "")
        self._protocol = config.get("protocol", "udp")
        self._enabled = config.get("enabled", False)
        self._switch_delay = config.get("switch_delay", 5)

        self._cycle = itertools.cycle(self._servers) if self._servers else None
        self._current_server = None
        self._process: Optional[subprocess.Popen] = None
        self._current_ip: Optional[str] = None
        self._connected_at: Optional[float] = None
        self._lock = asyncio.Lock()
        self._status = "disconnected"  # disconnected | connecting | connected | error
        self._error: Optional[str] = None

        # Cooldown after AUTH_FAILED to avoid NordVPN credential lockout extension.
        # NordVPN temporarily locks service credentials after repeated failed auths.
        self._auth_locked_until: float = 0.0

        # Stats
        self._total_switches = 0
        self._ip_history: list[dict] = []

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

    async def connect_next(self) -> str:
        """Disconnect current VPN and connect to next server.

        Returns the new public IP address.
        """
        async with self._lock:
            if not self._servers:
                raise RuntimeError("No VPN servers configured")

            # Respect AUTH_FAILED cooldown to avoid extending NordVPN lockout
            now = time.monotonic()
            if now < self._auth_locked_until:
                remaining = int(self._auth_locked_until - now)
                raise RuntimeError(
                    f"NordVPN credentials temporarily locked (cooldown {remaining}s left). "
                    f"Caused by repeated failed auths. Will retry automatically."
                )

            await self._disconnect()

            self._current_server = next(self._cycle)
            self._status = "connecting"
            self._error = None

            logger.info("[vpn] connecting to %s (%s)...",
                        self._current_server.get("name", "?"),
                        self._current_server.get("config", "?"))

            try:
                await self._connect(self._current_server)
                # Wait for tunnel to establish
                await asyncio.sleep(self._switch_delay)
                self._current_ip = await self._get_public_ip()
                self._connected_at = time.monotonic()
                self._status = "connected"
                self._total_switches += 1

                self._ip_history.append({
                    "ip": self._current_ip,
                    "server": self._current_server.get("name", "?"),
                    "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
                # Keep last 100 entries
                if len(self._ip_history) > 100:
                    self._ip_history = self._ip_history[-100:]

                logger.info("[vpn] connected → IP %s (server: %s)",
                            self._current_ip,
                            self._current_server.get("name", "?"))
                return self._current_ip

            except Exception as e:
                self._status = "error"
                self._error = str(e)
                logger.error("[vpn] connection failed: %s", e)
                raise

    async def disconnect(self):
        """Disconnect the current VPN connection."""
        async with self._lock:
            await self._disconnect()

    async def _kill_process(self):
        """Kill the OpenVPN process if running."""
        if self._process is not None:
            try:
                self._process.terminate()
                for _ in range(10):
                    if self._process.poll() is not None:
                        break
                    await asyncio.sleep(0.3)
                if self._process.poll() is None:
                    self._process.kill()
                    self._process.wait(timeout=5)
            except Exception as e:
                logger.warning("[vpn] error killing process: %s", e)
            finally:
                self._process = None

    async def _disconnect(self):
        """Kill OpenVPN process and wait for tun interface to go down."""
        await self._kill_process()
        self._current_ip = None
        self._connected_at = None
        self._status = "disconnected"
        logger.info("[vpn] disconnected")

    async def _connect(self, server: dict):
        """Start OpenVPN with the given server config."""
        config_path = server.get("config", "")
        if not config_path or not os.path.exists(config_path):
            raise FileNotFoundError(f"OpenVPN config not found: {config_path}")

        # Find OpenVPN binary
        openvpn_cmd = self._find_openvpn()
        if not openvpn_cmd:
            raise FileNotFoundError(
                "OpenVPN not found. Install it from https://openvpn.net/community-downloads/ "
                "or add it to PATH."
            )

        cmd = [openvpn_cmd, "--config", config_path]

        # Find auth file: use configured path, or default location
        auth_file = self._auth_file
        if not auth_file or not os.path.exists(auth_file):
            # Try default locations
            default_paths = [
                os.path.join(os.path.dirname(config_path), "credentials.txt"),
                "vpn_configs/credentials.txt",
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "vpn_configs", "credentials.txt"),
            ]
            for p in default_paths:
                if os.path.exists(p):
                    auth_file = os.path.abspath(p)
                    break

        # Only add --auth-user-pass if the .ovpn file doesn't already have it
        with open(config_path, 'r') as f:
            ovpn_content = f.read()
        if 'auth-user-pass' not in ovpn_content:
            if auth_file and os.path.exists(auth_file):
                cmd.extend(["--auth-user-pass", auth_file])

        # Disable interactive prompts
        cmd.append("--auth-nocache")

        # Route only through tun (don't replace default gateway unless needed)
        # cmd.append("--redirect-gateway", "def1", "bypass-dhcp")

        logger.debug("[vpn] cmd: %s", " ".join(cmd))

        # Start OpenVPN in background
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # Stream output and detect outcome (success / AUTH_FAILED) instead of
        # blindly sleeping. This lets us react fast and, crucially, detect
        # AUTH_FAILED so we can enter a cooldown and NOT hammer NordVPN
        # (which would extend a credential lockout).
        outcome = None  # "connected" | "auth_failed" | "failed"
        output_lines = []
        deadline = time.monotonic() + 30  # max 30s to establish
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                # Process exited
                rest = b""
                try:
                    rest = self._process.stdout.read()
                except Exception:
                    pass
                if rest:
                    output_lines.append(rest.decode(errors="replace"))
                # Decide outcome from collected output
                joined = "".join(output_lines)
                if "AUTH_FAILED" in joined:
                    outcome = "auth_failed"
                else:
                    outcome = "failed"
                break
            line = self._process.stdout.readline()
            if not line:
                await asyncio.sleep(0.2)
                continue
            text = line.decode(errors="replace").rstrip()
            output_lines.append(text + "\n")
            # Success: interface assigned
            if "Initialization Sequence Completed" in text or ("TUN/TAP device" in text and "opened" in text):
                outcome = "connected"
                break
            # Auth failure detected early
            if "AUTH_FAILED" in text:
                outcome = "auth_failed"
                break

        if outcome == "auth_failed":
            # Kill any lingering process
            await self._kill_process()
            # Set a 10-minute cooldown so we don't extend NordVPN's lockout
            self._auth_locked_until = time.monotonic() + 600
            self._error = "AUTH_FAILED — NordVPN credentials locked (cooldown 10min)"
            logger.warning("[vpn] AUTH_FAILED — entering 10min cooldown to avoid lockout extension")
            raise RuntimeError(
                "AUTH_FAILED: NordVPN rejected the credentials. This is usually a "
                "temporary lock after repeated failed attempts. Cooldown 10 minutes. "
                "Credentials are likely correct — retry later."
            )

        if outcome != "connected":
            await self._kill_process()
            joined = "".join(output_lines)[:500]
            raise RuntimeError(f"OpenVPN failed to connect: {joined}")

    def _find_openvpn(self) -> str | None:
        """Find the OpenVPN binary on the system."""
        import shutil

        # Check PATH first
        openvpn_path = shutil.which("openvpn")
        if openvpn_path:
            return openvpn_path

        # Windows common paths
        if sys.platform == "win32":
            win_paths = [
                r"C:\Program Files\OpenVPN\bin\openvpn.exe",
                r"C:\Program Files (x86)\OpenVPN\bin\openvpn.exe",
                os.path.expanduser(r"~\OpenVPN\bin\openvpn.exe"),
            ]
            for p in win_paths:
                if os.path.exists(p):
                    return p

        # Linux common paths
        else:
            linux_paths = [
                "/usr/sbin/openvpn",
                "/usr/bin/openvpn",
                "/usr/local/sbin/openvpn",
                "/usr/local/bin/openvpn",
            ]
            for p in linux_paths:
                if os.path.exists(p):
                    return p

        return None

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

    def get_status(self) -> dict:
        """Return current VPN status for the dashboard."""
        elapsed = None
        if self._connected_at:
            elapsed = int(time.monotonic() - self._connected_at)

        return {
            "enabled": self._enabled,
            "status": self._status,
            "ip": self._current_ip,
            "server": self._current_server.get("name") if self._current_server else None,
            "server_config": self._current_server.get("config") if self._current_server else None,
            "protocol": self._protocol,
            "connected_seconds": elapsed,
            "total_switches": self._total_switches,
            "servers_count": len(self._servers),
            "error": self._error,
            "ip_history": self._ip_history[-10:],  # Last 10 IPs
        }

    def get_config(self) -> dict:
        """Return current configuration for the dashboard."""
        return {
            "enabled": self._enabled,
            "servers": self._servers,
            "auth_file": self._auth_file,
            "protocol": self._protocol,
            "switch_delay": self._switch_delay,
        }

    def update_config(self, updates: dict):
        """Update configuration from dashboard."""
        if "enabled" in updates:
            self._enabled = updates["enabled"]
        if "protocol" in updates:
            self._protocol = updates["protocol"]
        if "switch_delay" in updates:
            self._switch_delay = updates["switch_delay"]
        if "auth_file" in updates:
            self._auth_file = updates["auth_file"]
        if "servers" in updates:
            self._servers = updates["servers"]
            self._cycle = itertools.cycle(self._servers) if self._servers else None

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
