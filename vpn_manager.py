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

# Project root
ROOT = os.path.dirname(os.path.abspath(__file__))


class VPNManager:
    """Manages VPN connections for IP rotation via WSL2 or Docker."""

    def __init__(self, config: dict):
        self._config = config
        self._mode = config.get("mode", "wsl2")  # wsl2 | docker
        self._servers = config.get("servers", [])
        self._vpn_config_dir = config.get("configs_dir", os.path.join(ROOT, "vpn", "configs"))
        self._auth_file = config.get("auth_file", os.path.join(ROOT, "vpn", "credentials.txt"))
        self._proxy_port = config.get("vpn_proxy_port", 8888)
        self._quota_per_ip = config.get("quota_per_ip", 300)
        self._enabled = config.get("enabled", False)
        self._switch_delay = config.get("switch_delay", 5)

        # Proxy mode: vpn | socks5 | direct
        self._proxy_mode = config.get("proxy_mode", "vpn")

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
        self._status = "disconnected"  # disconnected | connecting | connected | error
        self._error: Optional[str] = None
        self._auth_locked_until: float = 0.0
        self._total_switches = 0
        self._ip_history: list[dict] = []

        # Check if Docker VPN is already running on startup
        if self._mode == "docker":
            self._check_existing_docker()

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

    async def connect_next(self) -> str:
        """Disconnect current VPN and connect to next server."""
        async with self._lock:
            if not self._servers:
                raise RuntimeError("No VPN servers configured")

            now = time.monotonic()
            if now < self._auth_locked_until:
                remaining = int(self._auth_locked_until - now)
                raise RuntimeError(
                    f"NordVPN credentials locked (cooldown {remaining}s left)"
                )

            await self._disconnect()

            self._current_server = next(self._cycle)
            self._status = "connecting"
            self._error = None

            logger.info("[vpn] connecting to %s via %s...",
                        self._current_server.get("name", "?"), self._mode)

            try:
                if self._mode == "docker":
                    await self._connect_docker()
                else:
                    await self._connect_wsl2()

                # Wait for tunnel + proxy to be ready
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
                if len(self._ip_history) > 100:
                    self._ip_history = self._ip_history[-100:]

                logger.info("[vpn] connected → IP %s (server: %s, mode: %s)",
                            self._current_ip,
                            self._current_server.get("name", "?"),
                            self._mode)
                return self._current_ip

            except Exception as e:
                self._status = "error"
                self._error = str(e)
                logger.error("[vpn] connection failed: %s", e)
                raise

    async def connect_wait(self) -> str:
        """Wait for the user to connect externally, detect IP change."""
        async with self._lock:
            self._status = "connecting"
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
                        self._status = "connected"
                        self._total_switches += 1
                        self._ip_history.append({
                            "ip": new_ip, "server": "External VPN",
                            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        })
                    logger.info("[vpn] VPN detected → IP %s", new_ip)
                    return new_ip
            except Exception:
                pass

        self._status = "error"
        self._error = "Timeout waiting for VPN connection"
        raise RuntimeError("No VPN IP change detected after 120s")

    async def disconnect(self):
        """Disconnect the current VPN connection."""
        async with self._lock:
            await self._disconnect()

    async def _disconnect(self):
        """Stop VPN process/container."""
        if self._mode == "docker":
            await self._stop_docker()
        else:
            await self._stop_wsl2()
        self._current_ip = None
        self._connected_at = None
        self._status = "disconnected"
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
        """Connect via OpenVPN inside Docker container."""
        config_path = self._current_server.get("config", "")
        if not config_path or not os.path.exists(config_path):
            raise FileNotFoundError(f"Config not found: {config_path}")

        docker_cmd = self._find_docker()

        # Ensure Docker image exists
        await self._docker_build()

        # Stop existing container
        await self._stop_docker()

        # Run container with VPN config
        abs_config = os.path.abspath(config_path).replace("\\", "/")
        abs_creds = os.path.abspath(self._auth_file).replace("\\", "/")
        container = self._docker_container

        cmd = [
            docker_cmd, "run", "-d",
            "--name", container,
            "--cap-add", "NET_ADMIN",
            "--device", "/dev/net/tun",
            "-p", f"{self._proxy_port}:8888",
            "-v", f"{abs_config}:/vpn/configs/{os.path.basename(config_path)}:ro",
            "-v", f"{abs_creds}:/vpn/credentials.txt:ro",
            "--rm",
            self._docker_image,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:500]
            raise RuntimeError(f"Docker failed: {err}")

        logger.info("[vpn] Docker container started: %s", container)

        # Wait for container to be healthy
        for _ in range(30):
            ret = await self._run_docker("curl -s --max-time 3 http://127.0.0.1:8888/ >/dev/null 2>&1", check=False)
            if ret == 0:
                logger.info("[vpn] Docker VPN proxy is ready")
                break
            await asyncio.sleep(2)
        else:
            logs = await self._run_docker("cat /tmp/openvpn.log 2>/dev/null | tail -5", check=False)
            raise RuntimeError(f"Docker VPN not ready. Log: {logs}")

    async def _stop_docker(self):
        """Stop and remove Docker VPN container."""
        docker_cmd = self._find_docker()
        proc = await asyncio.create_subprocess_exec(
            docker_cmd, "rm", "-f", self._docker_container,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()

    async def _docker_build(self):
        """Build the VPN Docker image if it doesn't exist."""
        docker_cmd = self._find_docker()
        proc = await asyncio.create_subprocess_exec(
            docker_cmd, "image", "inspect", self._docker_image,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        if proc.returncode != 0:
            logger.info("[vpn] building Docker VPN image...")
            dockerfile = os.path.join(ROOT, "Dockerfile.vpn")
            build_proc = await asyncio.create_subprocess_exec(
                docker_cmd, "build", "-t", self._docker_image, "-f", dockerfile, ROOT,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
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
            import subprocess
            docker_cmd = self._find_docker()
            r = subprocess.run(
                [docker_cmd, "ps", "-a", "--filter", "name=" + self._docker_container, "--format", "{{.Status}}"],
                capture_output=True, text=True, timeout=5
            )
            if "Up" in r.stdout:
                logger.info("[vpn] Docker VPN container already running")
                # Get the IP from the container logs
                r2 = subprocess.run(
                    [docker_cmd, "logs", self._docker_container],
                    capture_output=True, text=True, timeout=5
                )
                for line in r2.stdout.split("\n"):
                    if "VPN IP:" in line:
                        ip = line.split("VPN IP:")[-1].strip()
                        if ip:
                            self._current_ip = ip
                            self._status = "connected"
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
        }

    def get_config(self) -> dict:
        """Return current configuration for the dashboard."""
        return {
            "enabled": self._enabled,
            "proxy_mode": self._proxy_mode,
            "mode": self._mode,
            "servers": self._servers,
            "auth_file": self._auth_file,
            "vpn_config_dir": self._vpn_config_dir,
            "proxy_port": self._proxy_port,
            "switch_delay": self._switch_delay,
            "docker_image": self._docker_image,
            "socks5_proxies": self.get_socks5_proxies(),
            "socks5_rotate": self._socks5_rotate,
        }

    def update_config(self, updates: dict):
        """Update configuration from dashboard."""
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
