"""P0.5 — registre, vérification et assignation des profils.

Aucun réseau : le vérificateur reçoit un faux sender. Les transitions
`candidate → verified/stale/rejected` sont déterministes.
"""

from __future__ import annotations

import json

import pytest

from free.identities import VERSION_SPECS
from free.profile_generator import VerifiedProfileGenerator
from free.profile_registry import (
    PROFILE_225,
    STATUS_CANDIDATE,
    STATUS_REJECTED,
    STATUS_STALE,
    STATUS_VERIFIED,
    VERDICT_PASS,
    VERDICT_QUOTA,
    VERDICT_REJECT,
    VERDICT_TRANSIENT,
    ProfileEvidence,
    ProfileRegistry,
    VerifiedProfile,
    audit_profile,
    classify_evidence,
    load_registry,
)
from free.profile_verifier import ProfileVerifier


def _profile(
    profile_id: str = "oc-2.2.5-cli-13",
    *,
    version: str = "2.2.5",
    client: str = "cli",
    tls_family: str = PROFILE_225["tls_family"],
    tools_count: int = PROFILE_225["tools_count"],
    user_agent: str = "opencode/latest/2.2.5/cli",
    session_headers: list[str] | None = None,
    ttl_s: float = 1000.0,
) -> VerifiedProfile:
    if version == "2.2.5":
        md5 = PROFILE_225["ja3_md5"]
    else:
        md5 = str(VERSION_SPECS[version]["ja3_md5"])
    return VerifiedProfile(
        profile_id=profile_id,
        version=version,
        client=client,
        tls_family=tls_family,
        ja3="771,4865",
        ja3_md5=md5,
        sigalgs=["rsa_pss_rsae_sha256"],
        user_agent=user_agent,
        body_capture="free/captures/2.2.5.json",
        body_sha256="a" * 64,
        tools_count=tools_count,
        session_headers=session_headers
        if session_headers is not None
        else [
            "x-opencode-session",
            "x-opencode-session-id",
            "x-session-affinity",
            "x-session-id",
        ],
        ttl_s=ttl_s,
    )


def _evidence(block: str, status: int = 200, *, content: bool = True, error: bool = False) -> ProfileEvidence:
    return ProfileEvidence(
        block=block,
        status=status,
        content_ok=content,
        error_frame=error,
        checked_at=1000.0,
    )


def test_classify_evidence_distinguishes_quota_from_reject():
    assert classify_evidence(200, True) == VERDICT_PASS
    assert classify_evidence(200, False) == VERDICT_TRANSIENT
    assert classify_evidence(200, True, True) == VERDICT_TRANSIENT
    assert classify_evidence(429, False) == VERDICT_QUOTA
    assert classify_evidence(403, False) == VERDICT_REJECT
    assert classify_evidence(400, False) == VERDICT_REJECT
    assert classify_evidence(503, False) == VERDICT_TRANSIENT


def test_two_distinct_passes_promote_to_verified():
    registry = ProfileRegistry()
    registry.add(_profile())
    verifier = ProfileVerifier(
        lambda profile, block: _evidence(block),
        ["block-a", "block-b", "block-c"],
        clock=lambda: 1000.0,
    )
    outcome = verifier.verify(registry, "oc-2.2.5-cli-13")
    assert outcome.status == STATUS_VERIFIED
    assert outcome.passes == 2
    assert outcome.rejects == 0
    assert outcome.reason == "verified"
    profile = registry.get("oc-2.2.5-cli-13")
    assert profile is not None
    assert profile.expires_at == 2000.0


def test_quota_is_not_a_reject():
    registry = ProfileRegistry()
    registry.add(_profile())
    responses = {
        "block-a": _evidence("block-a"),
        "block-b": _evidence("block-b", 429, content=False),
        "block-c": _evidence("block-c", 429, content=False),
    }
    verifier = ProfileVerifier(lambda profile, block: responses[block], responses, clock=lambda: 1000.0)
    outcome = verifier.verify(registry, "oc-2.2.5-cli-13")
    assert outcome.status == STATUS_CANDIDATE
    assert outcome.passes == 1
    assert outcome.rejects == 0
    assert outcome.quotas == 2
    assert outcome.reason == "quota_only"


def test_three_distinct_rejects_reject_profile():
    registry = ProfileRegistry()
    registry.add(_profile())
    verifier = ProfileVerifier(
        lambda profile, block: _evidence(block, 403, content=False),
        ["block-a", "block-b", "block-c", "block-d"],
        clock=lambda: 1000.0,
    )
    outcome = verifier.verify(registry, "oc-2.2.5-cli-13")
    assert outcome.status == STATUS_REJECTED
    assert outcome.rejects == 3
    assert outcome.passes == 0


def test_ttl_expiry_marks_stale_and_excludes_profile():
    registry = ProfileRegistry()
    profile = _profile(ttl_s=100.0)
    registry.add(profile)
    registry.record_evidence(profile.profile_id, _evidence("block-a"), now=1000.0)
    registry.record_evidence(profile.profile_id, _evidence("block-b"), now=1000.0)
    assert profile.status == STATUS_VERIFIED
    assert registry.verified(now=1050.0)
    assert registry.verified(now=1200.0) == []
    assert profile.status == STATUS_STALE


def test_select_for_station_is_deterministic():
    registry = ProfileRegistry([_profile("oc-a"), _profile("oc-b"), _profile("oc-c")])
    for profile in registry.profiles.values():
        registry.record_evidence(profile.profile_id, _evidence("block-a"), now=1000.0)
        registry.record_evidence(profile.profile_id, _evidence("block-b"), now=1000.0)
    first = registry.select_for_station("warp-1", "secret", now=1000.0)
    second = registry.select_for_station("warp-1", "secret", now=1000.0)
    assert first is not None and second is not None
    assert first.profile_id == second.profile_id


def test_assign_prefers_distinct_then_marks_shared():
    registry = ProfileRegistry([_profile("oc-a"), _profile("oc-b")])
    for profile in registry.profiles.values():
        registry.record_evidence(profile.profile_id, _evidence("block-a"), now=1000.0)
        registry.record_evidence(profile.profile_id, _evidence("block-b"), now=1000.0)
    two = registry.assign(["warp-1", "warp-2"], "secret", now=1000.0)
    assert len({item.profile_id for item in two}) == 2
    assert all(item.shared is False for item in two)

    six = registry.assign(["warp-1", "warp-2", "warp-3", "warp-4", "warp-5", "warp-6"], "secret", now=1000.0)
    assert len(six) == 6
    assert all(item.profile_id is not None for item in six)
    assert sum(item.shared for item in six) == 4
    assert len({item.profile_id for item in six}) == 2


def test_assign_without_verified_profile_returns_no_verified():
    registry = ProfileRegistry([_profile()])
    assignments = registry.assign(["warp-1"], "secret", now=1000.0)
    assert assignments[0].profile_id is None
    assert assignments[0].reason == "no_verified"


def test_audit_flags_mixed_ua_and_body():
    profile = _profile(
        "oc-1.19.3-desktop-11",
        version="1.19.3",
        client="desktop",
        tls_family="bun-1.3.14",
        tools_count=11,
        user_agent="opencode/latest/2.2.5/cli",
    )
    reasons = audit_profile(profile)
    assert "ua_version_mismatch" in reasons
    assert "ua_bun_mismatch" in reasons
    registry = ProfileRegistry([profile])
    audit = registry.audit()
    assert audit.status == "mixed"
    assert audit.mixed[0]["profile_id"] == "oc-1.19.3-desktop-11"


def test_audit_accepts_consistent_225_profile():
    assert audit_profile(_profile()) == []


def test_registry_roundtrip_is_atomic(tmp_path):
    registry = ProfileRegistry([_profile()])
    target = tmp_path / "profiles" / "verified_profiles.json"
    registry.save(target)
    assert target.exists()
    assert not list(target.parent.glob("*.tmp"))
    loaded = ProfileRegistry.load(target)
    assert loaded.get("oc-2.2.5-cli-13") is not None
    assert json.loads(target.read_text(encoding="utf-8"))["schema"] == 1


def test_duplicate_profile_id_is_refused():
    registry = ProfileRegistry([_profile()])
    with pytest.raises(ValueError):
        registry.add(_profile())


def test_corrupt_registry_falls_back_to_empty(tmp_path):
    target = tmp_path / "verified_profiles.json"
    target.write_text("{not json", encoding="utf-8")
    registry = load_registry(target)
    assert registry.profiles == {}
    assert registry.verified() == []


def test_profile_from_generated_preserves_wire_fields():
    generated = {
        "version": "1.19.5",
        "client": "desktop",
        "tls": {
            "ja3": "771,4865",
            "ja3_md5": VERSION_SPECS["1.19.5"]["ja3_md5"],
            "sigalgs": ["rsa_pss_rsae_sha256"],
        },
        "headers": {
            "User-Agent": "opencode/1.19.5 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4",
            "x-opencode-session": "ses_x",
            "x-opencode-session-id": "ses_x",
        },
    }
    profile = VerifiedProfile.from_generated(
        generated,
        body_capture="free/captures/1.19.5.json",
        body_sha256="b" * 64,
        tools_count=11,
    )
    assert profile.profile_id == "oc-1.19.5-desktop-11"
    assert profile.tls_family == "bun-1.4.4"
    assert profile.user_agent.startswith("opencode/1.19.5")
    assert "x-opencode-session-id" in profile.session_headers


def test_generator_registers_candidate_then_emits_only_after_verification():
    registry = ProfileRegistry()
    generator = VerifiedProfileGenerator(registry)
    candidate = generator.register(
        "1.19.5",
        "desktop",
        body_capture="free/captures/1.19.5.json",
        body_sha256="c" * 64,
        tools_count=11,
    )
    assert candidate.status == STATUS_CANDIDATE
    assert generator.for_station("warp-1", "secret", now=1000.0) is None

    registry.record_evidence(candidate.profile_id, _evidence("block-a"), now=1000.0)
    registry.record_evidence(candidate.profile_id, _evidence("block-b"), now=1000.0)
    selected = generator.for_station("warp-1", "secret", now=1000.0)
    assert selected is not None
    assert selected.profile_id == candidate.profile_id
