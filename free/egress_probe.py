"""Résolveur d'egress commun (NordVPN/WARP/proxys).

Une seule implémentation pour mesurer l'IP publique derrière un proxy SOCKS5,
avec la famille forcée quand l'endpoint est IPv6. Utilise ``curl_cffi`` quand
il est disponible, puis le client ``curl`` système en repli.
"""

from __future__ import annotations

import ipaddress
import subprocess
import sys
from typing import Any

EGRESS_URL_V4 = "https://api.ipify.org"
EGRESS_URL_V6 = "https://api6.ipify.org"
EGRESS_URL_TRACE = "https://cloudflare.com/cdn-cgi/trace"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _parse_ip(value: Any, *, ipv6: bool) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return None
    if bool(parsed.version == 6) != bool(ipv6):
        return None
    return str(parsed)


def _trace_ip(text: str) -> str | None:
    for line in str(text or "").splitlines():
        if line.startswith("ip="):
            return line[3:].strip() or None
    return None


def _curl_cffi(proxy: str, ipv6: bool, timeout_s: float) -> str | None:
    try:
        from curl_cffi import CurlOpt
        from curl_cffi import requests as curl_requests
    except Exception:
        return None
    url = EGRESS_URL_V6 if ipv6 else EGRESS_URL_V4
    try:
        if ipv6:
            session = curl_requests.Session(curl_options={CurlOpt.IPRESOLVE: 2})
            try:
                response = session.get(url, proxy=proxy, timeout=timeout_s)
            finally:
                session.close()
            value = _parse_ip(getattr(response, "text", ""), ipv6=True)
            if value:
                return value
            session = curl_requests.Session(curl_options={CurlOpt.IPRESOLVE: 2})
            try:
                response = session.get(
                    EGRESS_URL_TRACE, proxy=proxy, timeout=timeout_s
                )
            finally:
                session.close()
            return _parse_ip(_trace_ip(getattr(response, "text", "")), ipv6=True)
        response = curl_requests.get(
            url,
            proxies={"http": proxy, "https": proxy},
            timeout=timeout_s,
        )
        return _parse_ip(getattr(response, "text", ""), ipv6=False)
    except Exception:
        return None


def _curl_cli(proxy: str, ipv6: bool, timeout_s: float) -> str | None:
    url = EGRESS_URL_V6 if ipv6 else EGRESS_URL_V4
    cmd = ["curl", "-s", "--max-time", str(max(1, int(timeout_s)))]
    if ipv6:
        cmd.append("-6")
    cmd.extend(["--socks5-hostname", proxy])
    cmd.append(url)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(2.0, timeout_s + 5.0),
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return _parse_ip(proc.stdout, ipv6=ipv6)


def resolve_egress(
    *,
    proxy_url: str,
    ipv6: bool = False,
    timeout_s: float = 30.0,
) -> str | None:
    """Mesure l'IP publique derrière ``proxy_url``.

    ``proxy_url`` doit être un URI SOCKS5 (``socks5://`` ou ``socks5h://``).
    Retourne ``None`` si l'IP est absente, invalide ou de la mauvaise famille.
    """
    proxy = str(proxy_url or "").strip()
    if not proxy:
        return None
    if proxy.startswith("socks5://"):
        proxy = "socks5h://" + proxy[len("socks5://") :]
    if proxy.startswith("socks5h://"):
        # curl --socks5-hostname attend host:port, pas une URL complète.
        host_port = proxy[len("socks5h://") :].rstrip("/")
        cli_proxy = host_port
        value = _curl_cffi(proxy, ipv6, timeout_s)
        if value:
            return value
        return _curl_cli(cli_proxy, ipv6, timeout_s)
    return None
