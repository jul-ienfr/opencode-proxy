"""Fast hostname switch through Gluetun's control API.

Gluetun accepts a hostname in ``PUT /v1/vpn/settings`` and reconnects in a few
seconds, avoiding a full compose recreate (~2 min). This binder is limited to
a hostname switch on the transport already configured in the container; the
hybrid binder falls back to Compose for a transport change.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..domain import BindResult

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class GluetunControlBinder:
    def __init__(
        self,
        *,
        container: str,
        egress: Callable[[int], Awaitable[str]] | None = None,
        settle_s: float = 2.0,
        egress_wait_s: float = 30.0,
        egress_poll_s: float = 2.0,
        control_key: str = "",
        runner: Callable[[list[str], float], subprocess.CompletedProcess] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._container = str(container)
        self._egress = egress
        self._settle_s = max(0.0, float(settle_s))
        self._egress_wait_s = max(1.0, float(egress_wait_s))
        self._egress_poll_s = max(0.2, float(egress_poll_s))
        self._control_key = str(control_key or "").strip()
        self._runner = runner or self._run
        self._clock = clock

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
        if not hostname:
            return BindResult(False, hostname, error="empty-hostname")
        key = self._control_key or self._container_key()
        if not key:
            return BindResult(False, hostname, error="control-unavailable:no-key")
        body = {
            "provider": {
                "server_selection": {
                    "countries": [str(country)] if country else [],
                    "hostnames": [hostname],
                }
            }
        }
        response = await asyncio.to_thread(self._put_settings, key, body)
        if response is None:
            return BindResult(False, hostname, error="control-unavailable:put")
        # Gluetun answers 204 No Content on success (wget prints nothing);
        # older builds answer "running". Both mean the settings were applied.
        answer = str(response).strip().lower()
        if answer and answer not in ("running", "ok", "200", "204"):
            return BindResult(
                False, hostname, error=f"control-unavailable:{str(response)[:120]}"
            )
        if self._settle_s:
            await asyncio.sleep(self._settle_s)
        if self._egress is None:
            return BindResult(True, hostname)
        deadline = time.monotonic() + self._egress_wait_s
        while True:
            try:
                ip = str(await self._egress(int(station)) or "").strip()
            except Exception:
                ip = ""
            if ip:
                return BindResult(True, hostname, egress_ip=ip)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return BindResult(False, hostname, error="control-no-egress")
            await asyncio.sleep(min(self._egress_poll_s, remaining))

    def current_transport(self) -> str:
        """Transport configured in the container environment.

        Gluetun's control API can change the in-memory hostname, but a
        container restart re-reads the environment. A hostname switch is only
        safe while the requested transport matches that environment; changing
        transport still needs a Compose recreate.
        """

        env = self._container_env()
        vpn_type = str(env.get("VPN_TYPE", "")).strip().lower()
        protocol = str(
            env.get("OPENVPN_PROTOCOL", "udp") or "udp"
        ).strip().lower()
        if vpn_type == "openvpn":
            return f"openvpn-{protocol if protocol in ('udp', 'tcp') else 'udp'}"
        if vpn_type == "wireguard":
            return "wireguard"
        return ""

    async def release(self, station: int) -> None:
        return None

    async def current(self, station: int) -> str | None:
        if self._egress is None:
            return None
        try:
            value = await self._egress(int(station))
        except Exception:
            return None
        return str(value or "").strip() or None

    # ── internals ─────────────────────────────────────────────────────
    def _container_env(self) -> dict[str, str]:
        try:
            proc = self._runner(
                [
                    "docker",
                    "inspect",
                    self._container,
                    "--format",
                    "{{range .Config.Env}}{{println .}}{{end}}",
                ],
                10.0,
            )
        except Exception:
            return {}
        if int(getattr(proc, "returncode", 1)) != 0:
            return {}
        env: dict[str, str] = {}
        for line in str(getattr(proc, "stdout", "") or "").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
        return env

    def _container_key(self) -> str:
        env = os.getenv("VPN_CONTROL_API_KEY", "").strip()
        if env:
            return env
        return self._container_env().get("VPN_CONTROL_API_KEY", "").strip()

    def _put_settings(self, key: str, body: dict) -> str | None:
        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8"
            ) as handle:
                json.dump(body, handle, separators=(",", ":"))
                tmp_path = handle.name
            remote = "/tmp/gluetun-control-body.json"
            cp = self._runner(
                ["docker", "cp", tmp_path, f"{self._container}:{remote}"], 30.0
            )
            if int(getattr(cp, "returncode", 1)) != 0:
                return None
            script = (
                "wget -q -O - -T 20 "
                f"--header='X-API-Key: {key}' "
                f"--method=PUT --body-file={remote} "
                "http://127.0.0.1:8000/v1/vpn/settings"
            )
            proc = self._runner(
                ["docker", "exec", self._container, "sh", "-c", script], 30.0
            )
            if int(getattr(proc, "returncode", 1)) != 0:
                return None
            return str(getattr(proc, "stdout", "") or "").strip()
        except Exception:
            return None
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink()
                except Exception:
                    pass

    def _run(self, command: list[str], timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
