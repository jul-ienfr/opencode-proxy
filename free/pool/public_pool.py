"""Public proxy pool helpers (T160).

Pure row selection extracted from ``FreeIPPool``: URL validation, egress
deduplication and a subnet cap that keeps many providers on the same /24 or
/64 from becoming one quota bucket.
"""

from __future__ import annotations

import ipaddress
import urllib.parse

_ALLOWED_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})


def public_subnet(value: str) -> str:
    """IPv4 → /24, IPv6 → /64; invalid input → empty string."""

    try:
        ip = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return ""
    if ip.version == 4:
        return str(ipaddress.ip_network(f"{ip}/24", strict=False))
    return str(ipaddress.ip_network(f"{ip}/64", strict=False))


def select_public_rows(rows: list, *, max_active: int, max_per_24: int) -> list:
    """Normalise and cap public proxy rows.

    Keeps one row per egress IP (or host:port when the egress is unknown) and
    at most ``max_per_24`` rows per /24 or /64.
    """

    selected: list = []
    seen: set[str] = set()
    per_subnet: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        try:
            parsed = urllib.parse.urlsplit(url)
            scheme = parsed.scheme.lower()
            port = int(parsed.port or (443 if scheme == "https" else 80))
        except (ValueError, TypeError):
            continue
        if scheme not in _ALLOWED_SCHEMES:
            continue
        host = (parsed.hostname or "").strip()
        if not host or not (1 <= port <= 65535):
            continue
        egress_raw = str(row.get("egress_ip") or "").strip()
        try:
            egress_ip = str(ipaddress.ip_address(egress_raw)) if egress_raw else ""
        except ValueError:
            egress_ip = ""
        key = egress_ip or f"{host.lower()}:{port}"
        if key in seen:
            continue
        subnet = public_subnet(egress_ip) if egress_ip else ""
        if subnet and per_subnet.get(subnet, 0) >= max_per_24:
            continue
        seen.add(key)
        if subnet:
            per_subnet[subnet] = per_subnet.get(subnet, 0) + 1
        selected.append(
            {
                **row,
                "url": url,
                "egress_ip": egress_ip,
                "block": str(row.get("block") or subnet).strip(),
                "ipv6": bool(row.get("ipv6")) or bool(egress_ip and ":" in egress_ip),
            }
        )
        if len(selected) >= max_active:
            break
    return selected


__all__ = ["public_subnet", "select_public_rows"]
