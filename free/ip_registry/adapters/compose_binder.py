"""Compose-based server binder.

Validated by the 2026-11-01 spike: recreating a gluetun container with
``SERVER_HOSTNAMES`` pointed at a specific NordVPN hostname reproduces the
same egress across recreations.

The binder never touches the running proxy's request path directly; it only
recreates the target station and waits for the egress to answer.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..domain import BindResult

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class ComposeHostnameBinder:
    """Bind a station to a specific NordVPN hostname via Compose override."""

    def __init__(
        self,
        root: str | Path,
        *,
        compose_file: str = "docker-compose.yml",
        override_path: str = "logs/compose-hostname.yml",
        profile: str = "multi-station",
        recreate_timeout_s: float = 180.0,
        settle_s: float = 10.0,
        egress_wait_s: float = 0.0,
        egress_poll_s: float = 2.0,
        project_name: str | None = None,
        runner: Callable[[list[str], str], subprocess.CompletedProcess] | None = None,
        egress: Callable[[int], Awaitable[str]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._root = Path(root)
        self._compose_file = compose_file
        self._override_path = self._root / override_path
        self._profile = profile
        self._recreate_timeout_s = max(1.0, float(recreate_timeout_s))
        self._settle_s = max(0.0, float(settle_s))
        self._egress_wait_s = max(0.0, float(egress_wait_s))
        self._egress_poll_s = max(0.1, float(egress_poll_s))
        self._runner = runner or self._run
        self._egress = egress
        self._clock = clock
        self._project_name = self._resolve_project_name(project_name)

    def _resolve_project_name(self, project_name: str | None) -> str | None:
        """Compose project the station containers already belong to.

        A chain launched through a different working-directory alias resolves
        the project name from that directory, so ``up`` targets a second
        project and dies with "container name already in use". Passing the
        canonical project explicitly keeps every recreate in the same project.
        """

        explicit = str(project_name or "").strip()
        if explicit:
            return explicit
        from_env = str(os.environ.get("COMPOSE_PROJECT_NAME") or "").strip()
        if from_env:
            return from_env
        try:
            for raw in (self._root / ".env").read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line.startswith("COMPOSE_PROJECT_NAME="):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return value or None
        except OSError:
            pass
        return None

    def service_name(self, station: int) -> str:
        return "vpn-gluetun" if int(station) == 1 else f"vpn-gluetun-{int(station)}"

    def override_path_for(self, station: int) -> Path:
        """One override file per station: parallel chains must never race.

        A shared ``compose-hostname.yml`` made two stations overwrite each
        other's hostname between `docker compose` reads; the result was
        cross-binding and mass ``no-egress`` failures.
        """

        station = int(station)
        stem = self._override_path.stem
        if stem.endswith(f"-{station}") or stem.endswith(f"_{station}"):
            return self._override_path
        return self._override_path.with_name(
            f"{stem}-{station}{self._override_path.suffix}"
        )

    def override_yaml(
        self,
        station: int,
        hostname: str,
        country: str | None = None,
        vpn_type: str | None = None,
        openvpn_protocol: str | None = None,
        dns_mode: str | None = None,
    ) -> str:
        service = self.service_name(station)
        lines = [
            "services:",
            f"  {service}:",
            "    environment:",
            f"      - SERVER_HOSTNAMES={hostname}",
        ]
        if country:
            lines.append(f"      - SERVER_COUNTRIES={country}")
        if vpn_type:
            lines.append(f"      - VPN_TYPE={vpn_type}")
        if openvpn_protocol:
            lines.append(f"      - OPENVPN_PROTOCOL={openvpn_protocol}")
        if dns_mode:
            lines.append(f"      - DNS_UPSTREAM_RESOLVER_TYPE={dns_mode}")
            if str(dns_mode).strip().lower() == "plain":
                # Gluetun refuses to boot when the resolver type is plain and
                # this list is empty ("upstream plain addresses ... 0
                # addresses"). Keep a literal fallback so the DNS fallback can
                # never turn a station into a restart loop.
                lines.append(
                    "      - DNS_UPSTREAM_PLAIN_ADDRESSES=1.1.1.1:53,8.8.8.8:53,9.9.9.9:53"
                )
        return "\n".join(lines) + "\n"

    async def assign(
        self,
        station: int,
        hostname: str,
        country: str | None = None,
        *,
        vpn_type: str | None = None,
        openvpn_protocol: str | None = None,
        dns_mode: str | None = None,
    ) -> BindResult:
        hostname = str(hostname or "").strip()
        country = str(country or "").strip() or None
        if not hostname:
            return BindResult(False, hostname, error="empty-hostname")
        try:
            override_path = self.override_path_for(station)
            override_path.parent.mkdir(parents=True, exist_ok=True)
            override_path.write_text(
                self.override_yaml(
                    station,
                    hostname,
                    country,
                    vpn_type,
                    openvpn_protocol,
                    dns_mode,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            return BindResult(False, hostname, error=f"override-write:{type(exc).__name__}")
        command = ["docker", "compose"]
        if self._project_name:
            command += ["-p", self._project_name]
        command += [
            "--profile",
            self._profile,
            "-f",
            self._compose_file,
            "-f",
            str(override_path),
            "up",
            "-d",
            "--force-recreate",
            "--no-deps",
            self.service_name(station),
        ]
        try:
            proc = await asyncio.to_thread(
                self._runner, command, str(self._root)
            )
        except Exception as exc:
            return BindResult(False, hostname, error=f"recreate:{type(exc).__name__}")
        if int(getattr(proc, "returncode", 1)) != 0:
            detail = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()
            return BindResult(False, hostname, error=f"recreate:{detail[:160]}")
        if self._settle_s:
            await asyncio.sleep(self._settle_s)
        if self._egress is None:
            return BindResult(True, hostname)
        deadline = time.monotonic() + self._egress_wait_s
        last_error = "no-egress"
        while True:
            try:
                egress_ip = await self._egress(station)
                last_error = "no-egress"
            except Exception as exc:
                egress_ip = ""
                last_error = f"egress:{type(exc).__name__}"
            egress_ip = str(egress_ip or "").strip()
            if egress_ip:
                return BindResult(True, hostname, egress_ip=egress_ip)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(self._egress_poll_s, remaining))
        return BindResult(False, hostname, error=last_error)

    async def release(self, station: int) -> None:
        return None

    async def current(self, station: int) -> str | None:
        if self._egress is None:
            return None
        try:
            value = await self._egress(station)
        except Exception:
            return None
        return str(value or "").strip() or None

    _STALE_STATION_ENV = re.compile(
        r"^(VPN_TYPE|SERVER_HOSTNAMES|OPENVPN_PROTOCOL|SERVER_COUNTRIES)_STATION\d+$"
    )

    def _run(self, command: list[str], cwd: str) -> subprocess.CompletedProcess:
        # The long-running proxy can carry station-specific variables from a
        # previous boot. Compose gives the process env precedence over `.env`,
        # so forwarding them silently overrides the per-bind override file.
        # The override always sets VPN_TYPE / SERVER_HOSTNAMES /
        # OPENVPN_PROTOCOL for its own service, so dropping the stale keys is
        # safe and keeps the other stations on their `.env` values.
        env = {
            key: value
            for key, value in os.environ.items()
            if not self._STALE_STATION_ENV.match(key)
        }
        if self._project_name:
            env["COMPOSE_PROJECT_NAME"] = self._project_name
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=self._recreate_timeout_s,
            creationflags=_NO_WINDOW,
        )
