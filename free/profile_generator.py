"""Générateur de profils vérifiés (P0.5).

Enveloppe `free.identity_generator` : produit des candidats, les enregistre
dans le registre et ne laisse sortir que des profils `verified`. Le candidat
reste hybride tant que le corps de sa version n'a pas été capturé ; l'audit
le signale, et il n'est pas assignable avant deux preuves live.
"""

from __future__ import annotations

from dataclasses import dataclass

from free.identity_generator import generate_profile
from free.profile_registry import (
    DEFAULT_TTL_S,
    ProfileRegistry,
    VerifiedProfile,
)


@dataclass
class VerifiedProfileGenerator:
    registry: ProfileRegistry
    ttl_s: float = DEFAULT_TTL_S

    def candidate(
        self,
        version: str,
        client: str = "desktop",
        *,
        body_capture: str,
        body_sha256: str,
        tools_count: int,
        source: str = "identity_generator",
    ) -> VerifiedProfile:
        data = generate_profile(
            version=version,
            client=client,
            include_body=False,
        )
        profile = VerifiedProfile.from_generated(
            data,
            body_capture=body_capture,
            body_sha256=body_sha256,
            tools_count=tools_count,
            source=source,
        )
        profile.ttl_s = float(self.ttl_s)
        return profile

    def register(
        self,
        version: str,
        client: str = "desktop",
        *,
        body_capture: str,
        body_sha256: str,
        tools_count: int,
        source: str = "identity_generator",
        replace: bool = False,
    ) -> VerifiedProfile:
        profile = self.candidate(
            version,
            client,
            body_capture=body_capture,
            body_sha256=body_sha256,
            tools_count=tools_count,
            source=source,
        )
        return self.registry.add(profile, replace=replace)

    def for_station(
        self,
        label: str,
        secret: str,
        *,
        now: float | None = None,
    ) -> VerifiedProfile | None:
        selected = self.registry.select_for_station(label, secret, now=now)
        if selected is not None:
            return selected
        return self.registry.canonical(now=now)


__all__ = ["VerifiedProfileGenerator"]
