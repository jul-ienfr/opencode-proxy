"""Contrat commun des providers d'egress (feature 007).

Ce module est volontairement pur : il ne connaît ni Docker, ni le pool, ni le
dashboard. Les implémentations (WARP, gluetun, gateway IPv6, statique) vivent
ailleurs et respectent ce protocole.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from free.quota_ledger import quota_block

block_for_egress = quota_block


def family_for_egress(ip: str | None) -> str | None:
    """``ipv6`` / ``ipv4`` / ``None``."""
    try:
        parsed = ipaddress.ip_address(str(ip or "").strip())
    except ValueError:
        return None
    return "ipv6" if parsed.version == 6 else "ipv4"


def measure_egress(endpoint: Any, *, timeout_s: float = 6.0) -> str | None:
    """Mesure l'egress réel d'un endpoint et vérifie sa famille.

    Réutilise ``endpoint.refresh_egress`` quand il existe (WARP/SOCKS5) ; sinon
    lit ``egress_ip``. Rejette ``socks5:<label>`` et une famille incorrecte.
    Ne lève jamais.
    """
    try:
        refresh = getattr(endpoint, "refresh_egress", None)
        if callable(refresh):
            ip = refresh(timeout=timeout_s)
        else:
            ip = getattr(endpoint, "egress_ip", None)
    except Exception:
        return None
    value = str(ip or "").strip()
    if not value or value.startswith("socks5:"):
        return None
    family = family_for_egress(value)
    if family is None:
        return None
    expected = "ipv6" if bool(getattr(endpoint, "ipv6", False)) else "ipv4"
    if family != expected:
        return None
    return value


@dataclass(frozen=True, slots=True)
class EgressRotationResult:
    """Résultat d'une rotation, commun à tous les providers."""

    label: str
    ok: bool
    old_ip: str | None
    new_ip: str | None
    block: str | None
    attempts: int
    reason: str
    provider: str = "warp"

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "ok": self.ok,
            "old_ip": self.old_ip,
            "new_ip": self.new_ip,
            "block": self.block,
            "attempts": self.attempts,
            "reason": self.reason,
            "provider": self.provider,
        }


@runtime_checkable
class EgressProvider(Protocol):
    """Port de rotation/santé d'un provider.

    ``rotate`` doit :
    - refuser un bloc dans ``forbidden_blocks`` ou déjà pris ;
    - refuser une famille incompatible avec ``endpoint.ipv6`` ;
    - appeler ``probe`` avant d'accepter (si fourni) ;
    - toujours retourner un ``EgressRotationResult``, jamais lever.
    """

    name: str

    def rotate(
        self,
        endpoint: Any,
        *,
        forbidden_blocks: set[str] | None = None,
        probe: Any = None,
    ) -> EgressRotationResult: ...

    def refresh_egress(self, endpoint: Any) -> str | None: ...

    def health(self) -> dict[str, Any]: ...


__all__ = [
    "EgressProvider",
    "EgressRotationResult",
    "block_for_egress",
    "family_for_egress",
    "measure_egress",
]
