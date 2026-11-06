"""Station egress detection with layered fallbacks and IP validation.

The order is intentional: remote DNS first, local DNS second, HTTP proxy
third, then the container itself. A failure of one site (or of the SOCKS
listener) must not discard a station that can still answer the official free
probe.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Iterable
from typing import Any

IP_URLS = (
    "https://api.ipify.org",
    "https://icanhazip.com",
    "https://checkip.amazonaws.com",
    "https://ipinfo.io/ip",
    "https://ifconfig.me/ip",
)

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip())
        return True
    except ValueError:
        return False


def _container_name(station: int) -> str:
    return "opencode-vpn" if int(station) == 1 else f"opencode-vpn-{int(station)}"


def host_public_ip(
    *, urls: tuple[str, ...] = IP_URLS, timeout_s: float = 5.0
) -> str:
    """Best-effort host egress IP, bypassing any configured HTTP proxy.

    This is a safety input, not a health check: if it cannot be resolved the
    caller simply has one fewer IP to reject. A station that reports the
    host's own address is not tunnelled (Gluetun's control API can return
    the host public IP while the VPN is down) and must never be accepted as a
    fresh egress.
    """

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in urls:
        try:
            with opener.open(url, timeout=max(1.0, float(timeout_s))) as response:
                text = response.read(128).decode("utf-8", "replace").strip()
        except Exception:
            continue
        if _valid_ip(text):
            return text
    return ""


class StationEgress:
    """Callable ``station -> egress IP`` used by binder and probe."""

    def __init__(
        self,
        *,
        get: Callable[..., Any] | None = None,
        urls: tuple[str, ...] = IP_URLS,
        timeout_s: float = 8.0,
        docker_exec: Callable[..., str] | None = None,
        control_api: Callable[..., str] | None = None,
        forbidden_ips: Iterable[str] = (),
    ) -> None:
        self._get = get or self._curl_get
        self._urls = tuple(urls)
        self._timeout_s = max(1.0, float(timeout_s))
        self._docker_exec = docker_exec or self._docker_exec_get
        self._control_api = control_api or self._control_api_get
        self._forbidden = {
            str(ip).strip() for ip in forbidden_ips if str(ip).strip()
        }
        self._last_source = "none"

    def _accept(self, value: str) -> bool:
        text = str(value or "").strip()
        return bool(text) and _valid_ip(text) and text not in self._forbidden

    @property
    def last_source(self) -> str:
        return self._last_source

    async def control_only(self, station: int) -> str:
        """Fast egress for a just-applied control-API hostname switch.

        The control server answers in ~1 s and its status gate rejects a
        disconnected tunnel (where the endpoint reports the HOST public IP).
        Proxy paths are deliberately skipped: they need the full tunnel/DNS
        stack and would turn a 15 s switch into a 60 s wait.
        """

        try:
            value = await asyncio.to_thread(
                self._control_api, int(station), self._timeout_s
            )
        except Exception:
            value = ""
        text = str(value or "").strip()
        if self._accept(text):
            self._last_source = "control_api:/v1/publicip/ip"
            return text
        self._last_source = "none"
        return ""

    async def __call__(self, station: int) -> str:
        # 1) SOCKS5h: hostname resolution happens inside the tunnel.
        # 2) SOCKS5: local DNS fallback.
        # 3) HTTP proxy: the station also exposes an HTTP proxy.
        for scheme, port_base in (
            ("socks5h", 1079),
            ("socks5", 1079),
            ("http", 8887),
        ):
            proxy = f"{scheme}://127.0.0.1:{port_base + int(station)}"
            for url in self._urls:
                try:
                    value = await asyncio.to_thread(
                        self._get,
                        url,
                        {"http": proxy, "https": proxy},
                        self._timeout_s,
                    )
                except Exception:
                    continue
                text = str(value or "").strip()
                if self._accept(text):
                    self._last_source = f"{scheme}:{url}"
                    return text

        # 4) Gluetun's own control API knows the tunnel's public IP and does
        #    not depend on an external IP-echo site.
        try:
            value = await asyncio.to_thread(
                self._control_api, int(station), self._timeout_s
            )
        except Exception:
            value = ""
        text = str(value or "").strip()
        if self._accept(text):
            self._last_source = "control_api:/v1/publicip/ip"
            return text

        # 5) Ask the station container itself (its DNS/tunnel stack may work
        #    when the host-side proxy path is broken).
        for url in self._urls:
            try:
                value = await asyncio.to_thread(
                    self._docker_exec, int(station), url, self._timeout_s
                )
            except Exception:
                continue
            text = str(value or "").strip()
            if self._accept(text):
                self._last_source = f"docker:{url}"
                return text

        self._last_source = "none"
        return ""

    @staticmethod
    def _curl_get(url: str, proxies: dict, timeout: float) -> str:
        from curl_cffi import requests as curl_requests

        return curl_requests.get(
            url,
            proxies=proxies,
            timeout=timeout,
            impersonate="chrome",
        ).text

    @staticmethod
    def _control_exec(station: int, path: str, timeout: float) -> str:
        key = str(os.getenv("VPN_CONTROL_API_KEY") or "").strip()
        if not key:
            return ""
        try:
            proc = subprocess.run(
                [
                    "docker",
                    "exec",
                    _container_name(station),
                    "wget",
                    "-qO-",
                    f"--timeout={max(1, int(timeout))}",
                    "--header",
                    f"X-API-Key: {key}",
                    f"http://127.0.0.1:8000{path}",
                ],
                capture_output=True,
                text=True,
                timeout=max(2.0, float(timeout) + 2.0),
                creationflags=_NO_WINDOW,
            )
        except Exception:
            return ""
        if int(getattr(proc, "returncode", 1)) != 0:
            return ""
        return str(getattr(proc, "stdout", "") or "").strip()

    @staticmethod
    def _vpn_is_running(payload: str) -> bool:
        try:
            data = json.loads(payload or "{}")
        except Exception:
            return str(payload or "").strip().lower() == "running"
        return str((data or {}).get("status", "")).strip().lower() == "running"

    @staticmethod
    def _public_ip(payload: str) -> str:
        try:
            data = json.loads(payload or "{}")
        except Exception:
            return str(payload or "").strip()
        if not isinstance(data, dict):
            return ""
        return str(data.get("public_ip") or data.get("ip") or "").strip()

    @classmethod
    def _control_api_get(cls, station: int, timeout: float) -> str:
        # A disconnected Gluetun reports the HOST public IP on this endpoint;
        # accepting it made every dead tunnel look like a fresh egress.
        status = cls._control_exec(station, "/v1/vpn/status", timeout)
        if not cls._vpn_is_running(status):
            return ""
        return cls._public_ip(
            cls._control_exec(station, "/v1/publicip/ip", timeout)
        )

    @staticmethod
    def _docker_exec_get(station: int, url: str, timeout: float) -> str:
        proc = subprocess.run(
            [
                "docker",
                "exec",
                _container_name(station),
                "wget",
                "-qO-",
                f"--timeout={max(1, int(timeout))}",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=max(2.0, float(timeout) + 2.0),
            creationflags=_NO_WINDOW,
        )
        if int(getattr(proc, "returncode", 1)) != 0:
            return ""
        return str(getattr(proc, "stdout", "") or "").strip()
