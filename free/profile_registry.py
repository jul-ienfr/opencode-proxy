"""Registre de profils d'impersonation vérifiés (P0.5).

Le générateur (`free.identity_generator`) produit des candidats ; ce module
conserve les preuves, applique le TTL et ne laisse sortir que les profils
réellement vérifiés. Aucune I/O réseau ici : le vérificateur reçoit un
callback d'envoi, ce qui rend les transitions testables hors ligne.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from free.identities import SUPPORTED_CLIENTS, VERSION_SPECS

STATUS_CANDIDATE = "candidate"
STATUS_VERIFIED = "verified"
STATUS_STALE = "stale"
STATUS_REJECTED = "rejected"

VERDICT_PASS = "pass"
VERDICT_QUOTA = "quota"
VERDICT_REJECT = "reject"
VERDICT_TRANSIENT = "transient"

SESSION_HEADERS = (
    "x-opencode-session",
    "x-opencode-session-id",
    "x-session-affinity",
    "x-session-id",
)

#: 2.2.5 n'est pas dans `VERSION_SPECS` (voir research P0.5) : sa définition
#: vit dans `free/impersonation.json` et sa capture dans `free/captures/`.
PROFILE_225 = {
    "tls_family": "bun-1.4.4",
    "ja3_md5": "1523504b38f0fae0d881d4b6554aac1b",
    "tools_count": 13,
    "client": "cli",
}

TOOLS_BY_VERSION = {
    "1.18.31": 10,
    "1.18.35": 10,
    "1.19.0": 10,
    "1.19.3": 10,
    "1.19.5": 11,
    "2.2.5": 13,
}

DEFAULT_TTL_S = 7 * 24 * 3600.0
MIN_PASSES = 2
MAX_REJECTS = 3


def _now(now: float | None = None) -> float:
    return float(now if now is not None else time.time())


def classify_evidence(status: int, content_ok: bool, error_frame: bool = False) -> str:
    """Classe une réponse sans jamais confondre quota et rejet de profil."""
    try:
        code = int(status)
    except (TypeError, ValueError):
        return VERDICT_TRANSIENT
    if code == 200 and content_ok and not error_frame:
        return VERDICT_PASS
    if code == 429:
        return VERDICT_QUOTA
    if code in (400, 401, 403):
        return VERDICT_REJECT
    return VERDICT_TRANSIENT


@dataclass(frozen=True, slots=True)
class ProfileEvidence:
    block: str
    status: int
    content_ok: bool
    error_frame: bool = False
    latency_ms: int = 0
    response_sha256: str = ""
    checked_at: float = field(default_factory=time.time)
    verdict: str = ""

    def __post_init__(self) -> None:
        if not self.verdict:
            object.__setattr__(
                self,
                "verdict",
                classify_evidence(self.status, self.content_ok, self.error_frame),
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "block": self.block,
            "status": int(self.status),
            "content_ok": bool(self.content_ok),
            "error_frame": bool(self.error_frame),
            "latency_ms": int(self.latency_ms),
            "response_sha256": self.response_sha256,
            "checked_at": float(self.checked_at),
            "verdict": self.verdict,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfileEvidence:
        return cls(
            block=str(data.get("block") or ""),
            status=int(data.get("status") or 0),
            content_ok=bool(data.get("content_ok")),
            error_frame=bool(data.get("error_frame")),
            latency_ms=int(data.get("latency_ms") or 0),
            response_sha256=str(data.get("response_sha256") or ""),
            checked_at=float(data.get("checked_at") or time.time()),
            verdict=str(data.get("verdict") or ""),
        )


@dataclass
class VerifiedProfile:
    profile_id: str
    version: str
    client: str
    tls_family: str
    ja3: str = ""
    ja3_md5: str = ""
    sigalgs: list[str] = field(default_factory=list)
    user_agent: str = ""
    body_capture: str = ""
    body_sha256: str = ""
    tools_count: int = 0
    session_headers: list[str] = field(default_factory=list)
    status: str = STATUS_CANDIDATE
    evidence: list[ProfileEvidence] = field(default_factory=list)
    verified_at: float | None = None
    expires_at: float | None = None
    source: str = ""
    ttl_s: float = DEFAULT_TTL_S
    shared_by: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "version": self.version,
            "client": self.client,
            "tls_family": self.tls_family,
            "ja3": self.ja3,
            "ja3_md5": self.ja3_md5,
            "sigalgs": list(self.sigalgs),
            "user_agent": self.user_agent,
            "body_capture": self.body_capture,
            "body_sha256": self.body_sha256,
            "tools_count": int(self.tools_count),
            "session_headers": list(self.session_headers),
            "status": self.status,
            "evidence": [item.as_dict() for item in self.evidence],
            "verified_at": self.verified_at,
            "expires_at": self.expires_at,
            "source": self.source,
            "ttl_s": float(self.ttl_s),
            "shared_by": list(self.shared_by),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VerifiedProfile:
        return cls(
            profile_id=str(data.get("profile_id") or ""),
            version=str(data.get("version") or ""),
            client=str(data.get("client") or ""),
            tls_family=str(data.get("tls_family") or ""),
            ja3=str(data.get("ja3") or ""),
            ja3_md5=str(data.get("ja3_md5") or ""),
            sigalgs=[str(item) for item in data.get("sigalgs") or []],
            user_agent=str(data.get("user_agent") or ""),
            body_capture=str(data.get("body_capture") or ""),
            body_sha256=str(data.get("body_sha256") or ""),
            tools_count=int(data.get("tools_count") or 0),
            session_headers=[str(item) for item in data.get("session_headers") or []],
            status=str(data.get("status") or STATUS_CANDIDATE),
            evidence=[ProfileEvidence.from_dict(item) for item in data.get("evidence") or []],
            verified_at=float(data["verified_at"]) if data.get("verified_at") is not None else None,
            expires_at=float(data["expires_at"]) if data.get("expires_at") is not None else None,
            source=str(data.get("source") or ""),
            ttl_s=float(data.get("ttl_s") or DEFAULT_TTL_S),
            shared_by=[str(item) for item in data.get("shared_by") or []],
        )

    @classmethod
    def from_generated(
        cls,
        data: dict[str, Any],
        *,
        body_capture: str,
        body_sha256: str,
        tools_count: int,
        source: str = "identity_generator",
    ) -> VerifiedProfile:
        headers = dict(data.get("headers") or {})
        tls = dict(data.get("tls") or {})
        version = str(data.get("version") or "")
        client = str(data.get("client") or "")
        version_spec = VERSION_SPECS.get(version) or {}
        bun = str(version_spec.get("bun") or "")
        tls_family = f"bun-{bun.rsplit('/', 1)[-1]}" if bun else ""
        session_headers = [name for name in SESSION_HEADERS if name in headers]
        return cls(
            profile_id=f"oc-{version}-{client}-{int(tools_count)}",
            version=version,
            client=client,
            tls_family=tls_family,
            ja3=str(tls.get("ja3") or ""),
            ja3_md5=str(tls.get("ja3_md5") or ""),
            sigalgs=[str(item) for item in tls.get("sigalgs") or []],
            user_agent=str(headers.get("User-Agent") or ""),
            body_capture=body_capture,
            body_sha256=body_sha256,
            tools_count=int(tools_count),
            session_headers=session_headers,
            source=source,
        )

    def pass_blocks(self) -> set[str]:
        return {item.block for item in self.evidence if item.verdict == VERDICT_PASS and item.block}

    def reject_blocks(self) -> set[str]:
        return {item.block for item in self.evidence if item.verdict == VERDICT_REJECT and item.block}

    def quota_blocks(self) -> set[str]:
        return {item.block for item in self.evidence if item.verdict == VERDICT_QUOTA and item.block}

    def transient_blocks(self) -> set[str]:
        return {item.block for item in self.evidence if item.verdict == VERDICT_TRANSIENT and item.block}

    def refresh_status(self, now: float | None = None) -> str:
        ts = _now(now)
        if self.status == STATUS_REJECTED:
            return self.status
        if (
            self.status == STATUS_VERIFIED
            and self.expires_at is not None
            and ts >= float(self.expires_at)
        ):
            self.status = STATUS_STALE
            return self.status
        passes = len(self.pass_blocks())
        rejects = len(self.reject_blocks())
        if rejects >= MAX_REJECTS:
            self.status = STATUS_REJECTED
            return self.status
        if passes >= MIN_PASSES:
            if self.status != STATUS_VERIFIED:
                self.verified_at = ts
                self.expires_at = ts + float(self.ttl_s)
                self.status = STATUS_VERIFIED
            return self.status
        if self.status != STATUS_STALE:
            self.status = STATUS_CANDIDATE
        return self.status

    def is_fresh(self, now: float | None = None) -> bool:
        if self.status != STATUS_VERIFIED:
            return False
        if self.expires_at is None:
            return True
        return _now(now) < float(self.expires_at)


@dataclass(frozen=True, slots=True)
class ProfileAssignment:
    label: str
    profile_id: str | None
    shared: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "profile_id": self.profile_id,
            "shared": self.shared,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ProfileAudit:
    status: str
    duplicates: list[str] = field(default_factory=list)
    mixed: list[dict[str, str]] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    tls_families: list[str] = field(default_factory=list)
    shared: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "duplicates": list(self.duplicates),
            "mixed": list(self.mixed),
            "stale": list(self.stale),
            "rejected": list(self.rejected),
            "tls_families": list(self.tls_families),
            "shared": list(self.shared),
        }


def audit_profile(profile: VerifiedProfile) -> list[str]:
    """Retourne les raisons de rejet d'un profil (vide si cohérent)."""
    reasons: list[str] = []
    if profile.version == "2.2.5":
        expected_tools = PROFILE_225["tools_count"]
        expected_family = PROFILE_225["tls_family"]
        expected_md5 = PROFILE_225["ja3_md5"]
        expected_client = PROFILE_225["client"]
        session_id = True
        bun_suffix = ""
        expected_version_in_ua = "2.2.5"
        expected_client_in_ua = "cli"
    else:
        spec = VERSION_SPECS.get(profile.version)
        if spec is None:
            return [f"unknown_version:{profile.version}"]
        expected_tools = TOOLS_BY_VERSION.get(profile.version, 0)
        expected_family = f"bun-{str(spec['bun']).rsplit('/', 1)[-1]}"
        expected_md5 = str(spec["ja3_md5"])
        expected_client = None
        session_id = bool(spec.get("session_id"))
        bun_suffix = str(spec["bun"])
        expected_version_in_ua = profile.version
        expected_client_in_ua = ""
    if profile.tools_count != expected_tools:
        reasons.append(f"tools_count:{profile.tools_count}!={expected_tools}")
    if profile.tls_family != expected_family:
        reasons.append(f"tls_family:{profile.tls_family}!={expected_family}")
    if profile.ja3_md5 != expected_md5:
        reasons.append("ja3_md5_mismatch")
    if not profile.body_capture:
        reasons.append("missing_body_capture")
    if len(profile.body_sha256) != 64:
        reasons.append("invalid_body_sha256")
    if expected_client is not None and profile.client != expected_client:
        reasons.append(f"client:{profile.client}!={expected_client}")
    if profile.client not in SUPPORTED_CLIENTS:
        reasons.append(f"unsupported_client:{profile.client}")
    if profile.user_agent:
        if expected_version_in_ua not in profile.user_agent:
            reasons.append("ua_version_mismatch")
        if expected_client_in_ua and expected_client_in_ua not in profile.user_agent:
            reasons.append("ua_client_mismatch")
        if bun_suffix and bun_suffix not in profile.user_agent:
            reasons.append("ua_bun_mismatch")
    has_session_id = "x-opencode-session-id" in profile.session_headers
    if session_id and not has_session_id:
        reasons.append("missing_session_id")
    if not session_id and has_session_id:
        reasons.append("unexpected_session_id")
    return reasons


class ProfileRegistry:
    def __init__(
        self,
        profiles: Iterable[VerifiedProfile] | None = None,
        *,
        path: str | Path | None = None,
        ttl_s: float = DEFAULT_TTL_S,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.ttl_s = float(ttl_s)
        self.profiles: dict[str, VerifiedProfile] = {}
        for profile in profiles or ():
            self.add(profile)

    def add(self, profile: VerifiedProfile, *, replace: bool = False) -> VerifiedProfile:
        if not profile.profile_id:
            raise ValueError("profile_id vide")
        if profile.profile_id in self.profiles and not replace:
            raise ValueError(f"profile_id dupliqué: {profile.profile_id}")
        if profile.ttl_s <= 0:
            profile.ttl_s = self.ttl_s
        self.profiles[profile.profile_id] = profile
        return profile

    def get(self, profile_id: str) -> VerifiedProfile | None:
        return self.profiles.get(str(profile_id))

    def record_evidence(
        self,
        profile_id: str,
        evidence: ProfileEvidence,
        *,
        now: float | None = None,
    ) -> VerifiedProfile:
        profile = self.profiles.get(str(profile_id))
        if profile is None:
            raise KeyError(f"profil inconnu: {profile_id}")
        profile.evidence.append(evidence)
        profile.refresh_status(now)
        return profile

    def verified(self, *, now: float | None = None) -> list[VerifiedProfile]:
        out: list[VerifiedProfile] = []
        for profile in self.profiles.values():
            profile.refresh_status(now)
            if profile.is_fresh(now):
                out.append(profile)
        return sorted(out, key=lambda item: item.profile_id)

    def canonical(self, *, now: float | None = None) -> VerifiedProfile | None:
        profiles = self.verified(now=now)
        if not profiles:
            return None
        for profile in profiles:
            if profile.version == "2.2.5" and profile.client == "cli":
                return profile
        return profiles[0]

    def select_for_station(
        self,
        label: str,
        secret: str,
        *,
        now: float | None = None,
        allowed_versions: Iterable[str] | None = None,
    ) -> VerifiedProfile | None:
        profiles = self.verified(now=now)
        if allowed_versions is not None:
            allowed = {str(version) for version in allowed_versions}
            profiles = [profile for profile in profiles if profile.version in allowed]
        if not profiles:
            return None
        digest = hmac.new(
            str(secret).encode(),
            f"profile:{label}".encode(),
            hashlib.sha256,
        ).digest()
        index = int.from_bytes(digest[:8], "big") % len(profiles)
        return profiles[index]

    def assign(
        self,
        labels: Sequence[str],
        secret: str,
        *,
        now: float | None = None,
    ) -> list[ProfileAssignment]:
        profiles = self.verified(now=now)
        if not profiles:
            return [
                ProfileAssignment(label=str(label), profile_id=None, shared=False, reason="no_verified")
                for label in labels
            ]
        for profile in profiles:
            profile.shared_by.clear()
        assignments: list[ProfileAssignment] = []
        used: set[str] = set()
        by_label: dict[str, str] = {}
        for raw_label in labels:
            label = str(raw_label)
            if label in by_label:
                assignments.append(
                    ProfileAssignment(label=label, profile_id=by_label[label], shared=False, reason="duplicate_label")
                )
                continue
            digest = hmac.new(
                str(secret).encode(),
                f"profile:{label}".encode(),
                hashlib.sha256,
            ).digest()
            index = int.from_bytes(digest[:8], "big") % len(profiles)
            if len(used) < len(profiles):
                while profiles[index].profile_id in used:
                    index = (index + 1) % len(profiles)
                shared = False
                reason = "distinct"
            else:
                shared = True
                reason = "reuse"
            chosen = profiles[index]
            used.add(chosen.profile_id)
            by_label[label] = chosen.profile_id
            if shared:
                if label not in chosen.shared_by:
                    chosen.shared_by.append(label)
                reason = "shared"
            assignments.append(
                ProfileAssignment(
                    label=label,
                    profile_id=chosen.profile_id,
                    shared=shared,
                    reason=reason,
                )
            )
        return assignments

    def audit(self, *, now: float | None = None) -> ProfileAudit:
        mixed: list[dict[str, str]] = []
        stale: list[str] = []
        rejected: list[str] = []
        shared: list[str] = []
        families: set[str] = set()
        for profile in sorted(self.profiles.values(), key=lambda item: item.profile_id):
            profile.refresh_status(now)
            if profile.tls_family:
                families.add(profile.tls_family)
            reasons = audit_profile(profile)
            if reasons:
                mixed.append({"profile_id": profile.profile_id, "reason": ",".join(reasons)})
            if profile.status == STATUS_STALE:
                stale.append(profile.profile_id)
            if profile.status == STATUS_REJECTED:
                rejected.append(profile.profile_id)
            if profile.shared_by:
                shared.append(profile.profile_id)
        status = "ok"
        if mixed:
            status = "mixed"
        elif rejected:
            status = "rejected"
        elif stale:
            status = "stale"
        elif shared:
            status = "shared"
        return ProfileAudit(
            status=status,
            mixed=mixed,
            stale=stale,
            rejected=rejected,
            tls_families=sorted(families),
            shared=sorted(set(shared)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "updated_at": _now(),
            "profiles": [profile.as_dict() for profile in sorted(self.profiles.values(), key=lambda item: item.profile_id)],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, path: str | Path | None = None) -> ProfileRegistry:
        profiles = [VerifiedProfile.from_dict(item) for item in data.get("profiles") or []]
        return cls(profiles=profiles, path=path)

    @classmethod
    def load(cls, path: str | Path) -> ProfileRegistry:
        registry_path = Path(path)
        if not registry_path.exists():
            return cls(path=registry_path)
        data = json.loads(registry_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("registre invalide: objet attendu")
        return cls.from_dict(data, path=registry_path)

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("aucun chemin de registre")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, target)
        self.path = target
        return target


def load_registry(path: str | Path | None = None) -> ProfileRegistry:
    target = Path(path) if path is not None else Path(__file__).with_name("profiles") / "verified_profiles.json"
    try:
        return ProfileRegistry.load(target)
    except (OSError, ValueError, json.JSONDecodeError):
        return ProfileRegistry(path=target)


__all__ = [
    "DEFAULT_TTL_S",
    "MAX_REJECTS",
    "MIN_PASSES",
    "PROFILE_225",
    "ProfileAssignment",
    "ProfileAudit",
    "ProfileEvidence",
    "ProfileRegistry",
    "STATUS_CANDIDATE",
    "STATUS_REJECTED",
    "STATUS_STALE",
    "STATUS_VERIFIED",
    "TOOLS_BY_VERSION",
    "VERDICT_PASS",
    "VERDICT_QUOTA",
    "VERDICT_REJECT",
    "VERDICT_TRANSIENT",
    "VerifiedProfile",
    "audit_profile",
    "classify_evidence",
    "load_registry",
]
