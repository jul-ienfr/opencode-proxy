"""P0/P0.5 — requête de vérification et classification, sans réseau."""

from __future__ import annotations

from free.profile_live import (
    IdentityProfileSender,
    VerificationRequest,
    VerificationResponse,
    build_verification_request,
)
from free.profile_registry import (
    DEFAULT_TTL_S,
    PROFILE_225,
    STATUS_VERIFIED,
    VERDICT_PASS,
    VERDICT_QUOTA,
    VERDICT_REJECT,
    ProfileRegistry,
    VerifiedProfile,
)
from free.profile_verifier import ProfileVerifier
from free.station_identity import StationIdentity

SECRET = b"z" * 32

BASE_BODY = {
    "model": "deepseek-v4.1-flash-free",
    "messages": [
        {
            "role": "system",
            "content": (
                "You are an AI agent running in OpenCode.\n\n"
                "Today's date: Wed Nov 04 2026\n"
                "<env>\n"
                "  Working directory: C:\\Users\\alice\\repo\n"
                "  Platform: win32\n"
                "  Is directory a git repo: yes\n"
                "</env>"
            ),
        },
        {"role": "user", "content": "ping"},
    ],
    "stream": True,
}


def _profile(version: str = "2.2.5", client: str = "cli") -> VerifiedProfile:
    return VerifiedProfile(
        profile_id=f"oc-{version}-{client}",
        version=version,
        client=client,
        tls_family=PROFILE_225["tls_family"] if version == "2.2.5" else "bun-1.4.4",
        ja3_md5=PROFILE_225["ja3_md5"],
        user_agent="opencode/latest/2.2.5/cli" if version == "2.2.5" else f"opencode/{version} runtime/bun/1.4.4",
        body_capture="free/captures/2.2.5.json",
        body_sha256="a" * 64,
        tools_count=13 if version == "2.2.5" else 11,
        session_headers=["x-opencode-session", "x-opencode-session-id"],
    )


def _identity(label: str = "warp-1") -> StationIdentity:
    return StationIdentity(label, SECRET)


def test_build_request_uses_station_identity_and_templated_prompt():
    identity = _identity("warp-1")
    profile = _profile()
    request = build_verification_request(identity, profile, BASE_BODY, "https://example.test/v1/chat")
    assert request.headers["x-opencode-project"] == identity.project
    assert request.headers["x-opencode-session"].startswith("ses_")
    assert request.headers["x-opencode-request"].startswith("msg_")
    assert request.headers["Host"] == "example.test"
    system = request.body["messages"][0]["content"]
    assert "Working directory: /workspace/oc-warp-1" in system
    assert "C:\\Users\\alice" not in system
    assert "Platform:" not in system
    assert request.body["messages"][1] == {"role": "user", "content": "ping"}
    assert BASE_BODY["messages"][0]["content"].startswith("You are an AI agent")


def test_sender_classifies_pass_quota_and_reject():
    identity = _identity()
    profile = _profile()
    registry = ProfileRegistry([profile])

    def post(request: VerificationRequest, block: str) -> VerificationResponse:
        if block == "pass":
            return VerificationResponse(status=200, content="pong")
        if block == "quota":
            return VerificationResponse(status=429, content='{"error":"quota"}')
        return VerificationResponse(status=403, content="forbidden")

    sender = IdentityProfileSender(identity, profile, BASE_BODY, "https://example.test/v1/chat", post)
    assert sender(profile, "pass").verdict == VERDICT_PASS
    assert sender(profile, "quota").verdict == VERDICT_QUOTA
    assert sender(profile, "reject").verdict == VERDICT_REJECT
    assert registry.get(profile.profile_id) is not None


def test_sender_handles_exception_as_transient():
    identity = _identity()
    profile = _profile()

    def post(request: VerificationRequest, block: str) -> VerificationResponse:
        raise RuntimeError("network down")

    sender = IdentityProfileSender(identity, profile, BASE_BODY, "https://example.test/v1/chat", post)
    evidence = sender(profile, "block-a")
    assert evidence.status == 0
    assert evidence.verdict == "transient"
    assert evidence.content_ok is False


def test_verifier_promotes_after_two_passes_with_identity_sender():
    identity = _identity("warp-1")
    profile = _profile()
    registry = ProfileRegistry([profile])

    def post(request: VerificationRequest, block: str) -> VerificationResponse:
        assert request.headers["x-opencode-project"] == identity.project
        return VerificationResponse(status=200, content="pong")

    sender = IdentityProfileSender(identity, profile, BASE_BODY, "https://example.test/v1/chat", post)
    verifier = ProfileVerifier(sender, ["block-a", "block-b"], clock=lambda: 1000.0)
    outcome = verifier.verify(registry, profile.profile_id)
    assert outcome.status == STATUS_VERIFIED
    assert outcome.passes == 2
    stored = registry.get(profile.profile_id)
    assert stored is not None
    assert stored.expires_at == 1000.0 + DEFAULT_TTL_S
    assert all(item.response_sha256 for item in stored.evidence)


def test_sender_does_not_reuse_conversation_session_across_profiles():
    identity = _identity("warp-1")
    first = _profile()
    second = _profile("1.19.5", "desktop")
    sessions: list[str] = []

    def post(request: VerificationRequest, block: str) -> VerificationResponse:
        sessions.append(request.headers["x-opencode-session"])
        return VerificationResponse(status=200, content="pong")

    sender = IdentityProfileSender(identity, first, BASE_BODY, "https://example.test/v1/chat", post)
    assert sender(first, "block-a").verdict == VERDICT_PASS
    assert sender(second, "block-b").verdict == VERDICT_PASS
    assert sessions[0] != sessions[1]

