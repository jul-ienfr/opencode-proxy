"""Station egress detection with DNS fallbacks and IP validation."""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Callable
from typing import Any

IP_URLS = (
    "https://api.ipify.org",
    "https://icanhazip.com",
    "https://checkip.amazonaws.com",
    "https://ipinfo.io/ip",
)


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip())
        return True
    except ValueError:
        return False


class StationEgress:
    """Callable ``station -> egress IP`` used by binder and probe."""

    def __init__(
        self,
        *,
        get: Callable[..., Any] | None = None,
        urls: tuple[str, ...] = IP_URLS,
        timeout_s: float = 8.0,
    ) -> None:
        self._get = get or self._curl_get
        self._urls = tuple(urls)
        self._timeout_s = max(1.0, float(timeout_s))
        self._last_source = "none"

    @property
    def last_source(self) -> str:
        return self._last_source

    async def __call__(self, station: int) -> str:
        for scheme in ("socks5h", "socks5"):
            proxy = f"{scheme}://127.0.0.1:{1079 + int(station)}"
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
                if _valid_ip(text):
                    self._last_source = f"{scheme}:{url}"
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
