"""Lecture d'IP d'egress — 023 (T030).

Un changement d'IP ne se déclare que sur une adresse globale. Les sentinels et
valeurs de test sont rejetés : le ``live 1.2.3.4`` du 2026-11-17 ne doit plus
libérer une IP prouvée.
"""

from __future__ import annotations

import ipaddress

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
