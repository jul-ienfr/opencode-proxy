"""Pont P0/P0.5 : requête de vérification avec identité de station.

Construit les en-têtes et le corps templaté, puis classifie la réponse. Le
transport (`post`) est injecté : curl_cffi, sous-processus ou faux de test.
Aucune I/O réseau dans ce module.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from free.profile_registry import (
    ProfileEvidence,
    VerifiedProfile,
    classify_evidence,
)
from free.station_identity import StationIdentity, template_official_body


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    endpoint: str
    headers: dict[str, str]
    body: dict

    def fingerprint(self) -> str:
        payload = repr((self.endpoint, sorted(self.headers.items()), sorted(self.body.keys())))
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class VerificationResponse:
    status: int
    content: str = ""
    error_frame: bool = False
    latency_ms: int = 0
    response_sha256: str = ""

    def __post_init__(self) -> None:
        if self.response_sha256:
            return
        digest = hashlib.sha256(str(self.content or "").encode()).hexdigest()
        object.__setattr__(self, "response_sha256", digest)


PostVerification = Callable[[VerificationRequest, str], VerificationResponse]


def build_verification_request(
    identity: StationIdentity,
    profile: VerifiedProfile,
    base_body: dict,
    endpoint: str,
) -> VerificationRequest:
    headers = identity.headers(
        profile,
        conversation_key=f"verify:{profile.profile_id}",
        endpoint=endpoint,
        include_transport=True,
    )
    body = template_official_body(base_body, identity)
    return VerificationRequest(endpoint=endpoint, headers=headers, body=body)


@dataclass
class IdentityProfileSender:
    identity: StationIdentity
    profile: VerifiedProfile
    base_body: dict
    endpoint: str
    post: PostVerification
    clock: Callable[[], float] = field(default_factory=lambda: time.time)

    def __call__(self, profile: VerifiedProfile, block: str) -> ProfileEvidence:
        request = build_verification_request(self.identity, profile, self.base_body, self.endpoint)
        started = float(self.clock())
        try:
            response = self.post(request, block)
        except Exception:
            return ProfileEvidence(
                block=block,
                status=0,
                content_ok=False,
                checked_at=self.clock(),
            )
        elapsed_ms = int(max(0.0, (float(self.clock()) - started) * 1000.0))
        if not isinstance(response, VerificationResponse):
            return ProfileEvidence(
                block=block,
                status=0,
                content_ok=False,
                latency_ms=elapsed_ms,
                checked_at=self.clock(),
            )
        content_ok = bool(str(response.content or "").strip()) and not response.error_frame
        verdict = classify_evidence(response.status, content_ok, response.error_frame)
        return ProfileEvidence(
            block=block,
            status=response.status,
            content_ok=content_ok,
            error_frame=response.error_frame,
            latency_ms=int(response.latency_ms or elapsed_ms),
            response_sha256=response.response_sha256,
            checked_at=self.clock(),
            verdict=verdict,
        )


__all__ = [
    "IdentityProfileSender",
    "PostVerification",
    "VerificationRequest",
    "VerificationResponse",
    "build_verification_request",
]
