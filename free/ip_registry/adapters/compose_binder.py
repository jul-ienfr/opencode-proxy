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
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..domain import BindResult


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
        self._runner = runner or self._run
        self._egress = egress
        self._clock = clock

    def service_name(self, station: int) -> str:
        return "vpn-gluetun" if int(station) == 1 else f"vpn-gluetun-{int(station)}"

    def override_yaml(self, station: int, hostname: str, country: str | None = None) -> str:
        service = self.service_name(station)
        lines = [
            "services:",
            f"  {service}:",
            "    environment:",
            f"      - SERVER_HOSTNAMES={hostname}",
        ]
        if country:
            lines.append(f"      - SERVER_COUNTRIES={country}")
        return "\n".join(lines) + "\n"

    async def assign(
        self, station: int, hostname: str, country: str | None = None
    ) -> BindResult:
        hostname = str(hostname or "").strip()
        country = str(country or "").strip() or None
        if not hostname:
            return BindResult(False, hostname, error="empty-hostname")
        try:
            self._override_path.parent.mkdir(parents=True, exist_ok=True)
            self._override_path.write_text(
                self.override_yaml(station, hostname, country), encoding="utf-8"
            )
        except Exception as exc:
            return BindResult(False, hostname, error=f"override-write:{type(exc).__name__}")
        command = [
            "docker",
            "compose",
            "--profile",
            self._profile,
            "-f",
            self._compose_file,
            "-f",
            str(self._override_path),
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
        try:
            egress_ip = await self._egress(station)
        except Exception as exc:
            return BindResult(False, hostname, error=f"egress:{type(exc).__name__}")
        egress_ip = str(egress_ip or "").strip()
        if not egress_ip:
            return BindResult(False, hostname, error="no-egress")
        return BindResult(True, hostname, egress_ip=egress_ip)

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

    def _run(self, command: list[str], cwd: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=self._recreate_timeout_s,
        )
