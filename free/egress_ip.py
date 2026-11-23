"""Lecture d'IP d'egress concordante — 023 (T030).

Un changement d'IP ne se déclare que si deux sources indépendantes concordent
sur une adresse globale. Les sentinels et valeurs de test sont rejetés : le
``live 1.2.3.4`` du 2026-11-17 ne doit plus libérer une IP prouvée.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass

#: Valeurs observées dans des lectures dégradées, jamais de vraies IP.
SENTINELS: frozenset[str] = frozenset(
    {
        "",
        "unknown",
        "none",
        "null",
        "0.0.0.0",
        "255.255.255.255",
        "1.2.3.4",
        "127.0.0.1",
        "::",
        "::1",
    }
)

MIN_SOURCES = 2


@dataclass(frozen=True)
class IpObservation:
    source: str
    ip: str
    ok: bool = True
    detail: str = ""


@dataclass(frozen=True)
class Agreement:
    ok: bool
    ip: str = ""
    reason: str = ""


def is_global_ip(value: str) -> bool:
    """True pour une adresse publique plausible, jamais pour un sentinel."""
    raw = str(value or "").strip()
    if raw.lower() in SENTINELS:
        return False
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if addr.is_unspecified or addr.is_loopback or addr.is_link_local:
        return False
    if addr.is_multicast or addr.is_private or addr.is_reserved:
        return False
    if not addr.is_global:
        return False
    return True


def agree(observations: Iterable[IpObservation]) -> Agreement:
    """Exige au moins deux sources valides qui concordent."""
    valid = [
        o for o in observations
        if o.ok and is_global_ip(o.ip)
    ]
    sources = {o.source for o in valid}
    if len(sources) < MIN_SOURCES:
        return Agreement(False, reason=f"{len(sources)} valid source(s); need {MIN_SOURCES}")
    ips = {str(ipaddress.ip_address(o.ip.strip())) for o in valid}
    if len(ips) != 1:
        return Agreement(False, reason=f"disagreement: {sorted(ips)}")
    return Agreement(True, next(iter(ips)), reason="agreed")


def normalize(ip: str) -> str:
    try:
        return str(ipaddress.ip_address(str(ip).strip()))
    except ValueError:
        return str(ip or "").strip()
