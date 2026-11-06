"""Vérificateur de profils : orchestration sans réseau.

Le callback `send(profile, block)` est fourni par l'appelant. Il peut envoyer
via curl_cffi, un sous-processus ou un faux pour les tests. Le vérificateur
applique la règle P0.5 : deux `pass` sur des blocs distincts, un `429` n'est
jamais un rejet, trois `reject` distincts condamnent le profil.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from free.profile_registry import (
    MAX_REJECTS,
    MIN_PASSES,
    STATUS_REJECTED,
    STATUS_VERIFIED,
    VERDICT_TRANSIENT,
    ProfileEvidence,
    ProfileRegistry,
    VerifiedProfile,
)

SendProfile = Callable[[VerifiedProfile, str], ProfileEvidence]


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    profile_id: str
    status: str
    passes: int
    rejects: int
    quotas: int
    transients: int
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "status": self.status,
            "passes": self.passes,
            "rejects": self.rejects,
            "quotas": self.quotas,
            "transients": self.transients,
            "reason": self.reason,
        }


class ProfileVerifier:
    def __init__(
        self,
        send: SendProfile,
        blocks: Iterable[str],
        *,
        min_passes: int = MIN_PASSES,
        max_rejects: int = MAX_REJECTS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.send = send
        self.blocks = tuple(dict.fromkeys(str(block) for block in blocks if str(block).strip()))
        self.min_passes = max(1, int(min_passes))
        self.max_rejects = max(1, int(max_rejects))
        self.clock = clock or time.time

    def verify(self, registry: ProfileRegistry, profile_id: str) -> VerificationOutcome:
        profile = registry.get(profile_id)
        if profile is None:
            raise KeyError(f"profil inconnu: {profile_id}")
        used: set[str] = set()
        for block in self.blocks:
            if len(profile.pass_blocks()) >= self.min_passes:
                break
            if len(profile.reject_blocks()) >= self.max_rejects:
                break
            if block in used:
                continue
            used.add(block)
            evidence = self._safe_send(profile, block)
            registry.record_evidence(profile_id, evidence, now=self.clock())
        profile.refresh_status(self.clock())
        return self._outcome(profile)

    def _safe_send(self, profile: VerifiedProfile, block: str) -> ProfileEvidence:
        try:
            evidence = self.send(profile, block)
        except Exception:
            evidence = ProfileEvidence(
                block=block,
                status=0,
                content_ok=False,
                verdict=VERDICT_TRANSIENT,
            )
        if not isinstance(evidence, ProfileEvidence):
            return ProfileEvidence(
                block=block,
                status=0,
                content_ok=False,
                verdict=VERDICT_TRANSIENT,
            )
        if evidence.block != block:
            return ProfileEvidence(
                block=block,
                status=evidence.status,
                content_ok=evidence.content_ok,
                error_frame=evidence.error_frame,
                latency_ms=evidence.latency_ms,
                response_sha256=evidence.response_sha256,
                checked_at=evidence.checked_at,
                verdict=evidence.verdict,
            )
        return evidence

    def _outcome(self, profile: VerifiedProfile) -> VerificationOutcome:
        passes = len(profile.pass_blocks())
        rejects = len(profile.reject_blocks())
        quotas = len(profile.quota_blocks())
        transients = len(profile.transient_blocks())
        if profile.status == STATUS_VERIFIED:
            reason = "verified"
        elif profile.status == STATUS_REJECTED:
            reason = "rejected"
        elif passes >= self.min_passes:
            reason = "insufficient_blocks"
        elif not self.blocks:
            reason = "no_blocks"
        elif quotas and not rejects:
            reason = "quota_only"
        else:
            reason = "insufficient_evidence"
        return VerificationOutcome(
            profile_id=profile.profile_id,
            status=profile.status,
            passes=passes,
            rejects=rejects,
            quotas=quotas,
            transients=transients,
            reason=reason,
        )


__all__ = ["ProfileVerifier", "SendProfile", "VerificationOutcome"]
