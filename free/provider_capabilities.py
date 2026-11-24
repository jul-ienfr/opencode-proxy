"""Capacités des providers d'egress — 023 (T008/T150-T153).

Chaque provider déclare sa rotation, son bucket, sa source de quota et sa
santé. Le direct et les proxys sont ``quota_source=shared`` ; WARP IPv4 a une
capacité nulle (NAT partagée), seul un ``/64`` IPv6 compte.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

NORDVPN = "nordvpn"
WARP = "warp"
PROXIES = "proxies"
DIRECT = "direct"

PROVIDER_NAMES = (NORDVPN, WARP, PROXIES, DIRECT)

QUOTA_RETRY_AFTER = "retry_after"
QUOTA_SHARED = "shared"
QUOTA_NONE = "none"


@dataclass(frozen=True)
class ProviderCapabilities:
    name: str
    can_rotate: bool
    quota_source: str
    health_source: str
    reserve_eligible: bool
    wire_profile: str = "2.3.0"

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "can_rotate": self.can_rotate,
            "quota_source": self.quota_source,
            "health_source": self.health_source,
            "reserve_eligible": self.reserve_eligible,
            "wire_profile": self.wire_profile,
        }


PROVIDERS: dict[str, ProviderCapabilities] = {
    NORDVPN: ProviderCapabilities(NORDVPN, True, QUOTA_RETRY_AFTER, "handshake+probe", True),
    WARP: ProviderCapabilities(WARP, True, QUOTA_RETRY_AFTER, "egress_probe", True),
    PROXIES: ProviderCapabilities(PROXIES, True, QUOTA_SHARED, "liveness", False),
    DIRECT: ProviderCapabilities(DIRECT, False, QUOTA_SHARED, "host_egress", False),
}


def capabilities(name: str) -> ProviderCapabilities:
    return PROVIDERS.get(str(name).lower(), PROVIDERS[DIRECT])


def normalize_ipv6_64(ip: str) -> str:
    """Retourne le préfixe ``/64`` normalisé d'une IPv6, sinon l'IP."""
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return str(ip or "").strip()
    if addr.version != 6:
        return str(addr)
    network = ipaddress.ip_network(f"{addr}/64", strict=False)
    return str(network)


def bucket_key(provider: str, ip: str, *, endpoint: str = "") -> str:
    name = str(provider or "").lower()
    raw = str(ip or "").strip()
    if name == WARP:
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return f"warp:{raw or endpoint}"
        return normalize_ipv6_64(raw) if addr.version == 6 else "warp-ipv4-shared"
    if name == PROXIES:
        return f"proxy:{endpoint or raw}"
    if name == DIRECT:
        return f"direct:{raw}"
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return raw
