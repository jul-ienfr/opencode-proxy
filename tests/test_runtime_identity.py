"""Runtime identity — résolution station/profil et construction du wire."""

from __future__ import annotations

import json

import opencode as oc
from free.identities import VERSION_SPECS
from free.profile_registry import ProfileEvidence, ProfileRegistry, VerifiedProfile
from free.runtime_identity import (
    build_body,
    build_headers,
    resolve_station_identity,
    runtime_profile_versions,
    station_label,
)
from free.station_identity import StationIdentity, StationIdentityVault

SECRET = b"r" * 32
BASE_BODY = {
    "messages": [
        {
            "role": "system",
            "content": (
                "You are an AI agent running in OpenCode.\n"
                "Instructions from: C:\\Users\\alice\\repo\\AGENTS.md\n"
                "SECRET_PROJECT_RULE\n"
                "<env>\n  Working directory: C:\\Users\\alice\\repo\n</env>"
            ),
        },
        {"role": "user", "content": "ping"},
    ],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "shell",
                "description": "Use C:\\Users\\alice\\AppData\\Local\\Temp.",
            },
        }
    ],
}


class FakeStation:
    def __init__(self, label: str = "warp-1") -> None:
        self.label = label


def _profile(version: str = "1.19.3") -> VerifiedProfile:
    spec = VERSION_SPECS[version]
    return VerifiedProfile(
        profile_id=f"oc-{version}-desktop",
        version=version,
        client="desktop",
        tls_family=f"bun-{spec['bun'].rsplit('/', 1)[-1]}",
        ja3=spec["ja3"],
        ja3_md5=spec["ja3_md5"],
        sigalgs=list(spec["sigalgs"]),
        user_agent=f"opencode/{version} ai-sdk/provider-utils/4.0.23 {spec['bun']}",
        body_capture=f"free/captures/{version}.json",
        body_sha256="a" * 64,
        tools_count=10,
        session_headers=["x-opencode-session-id", "x-session-affinity", "x-session-id"],
    )


def _verified_registry(version: str = "1.19.5") -> ProfileRegistry:
    registry = ProfileRegistry()
    profile = registry.add(_profile(version))
    for block in ("block-a", "block-b"):
        registry.record_evidence(
            profile.profile_id,
            ProfileEvidence(block=block, status=200, content_ok=True),
        )
    return registry


def _verified_registry_225() -> ProfileRegistry:
    registry = ProfileRegistry()
    profile = registry.add(_profile_225())
    for block in ("block-a", "block-b"):
        registry.record_evidence(
            profile.profile_id,
            ProfileEvidence(block=block, status=200, content_ok=True),
        )
    return registry


def _profile_225() -> VerifiedProfile:
    return VerifiedProfile(
        profile_id="oc-2.2.5-cli-13",
        version="2.2.5",
        client="cli",
        tls_family="bun-1.4.4",
        user_agent="opencode/latest/2.2.5/cli",
        body_capture="free/captures/2.2.5.json",
        body_sha256="a" * 64,
        tools_count=13,
        session_headers=[
            "x-opencode-session",
            "x-opencode-session-id",
            "x-session-affinity",
            "x-session-id",
        ],
    )


def test_station_label_handles_missing_label():
    assert station_label(None) == ""
    assert station_label(FakeStation("")) == ""
    assert station_label(FakeStation("warp-1")) == "warp-1"


def test_resolve_returns_station_identity_and_verified_profile():
    vault = StationIdentityVault(secret=SECRET)
    runtime = resolve_station_identity(FakeStation("warp-1"), vault, _verified_registry_225())
    assert runtime is not None
    assert runtime.identity.label == "warp-1"
    assert runtime.profile is not None
    assert runtime.version == "2.2.5"
    assert runtime.client == "cli"


def test_resolve_rejects_incompatible_1193_for_runtime_body():
    vault = StationIdentityVault(secret=SECRET)
    runtime = resolve_station_identity(FakeStation("warp-1"), vault, _verified_registry("1.19.3"))
    assert runtime is not None
    assert runtime.profile is None


def test_resolve_without_label_returns_none():
    vault = StationIdentityVault(secret=SECRET)
    assert resolve_station_identity(FakeStation(""), vault, _verified_registry_225()) is None


def test_build_headers_1193_use_legacy_wire():
    identity = StationIdentity("warp-1", SECRET)
    headers = identity.headers(
        _profile("1.19.3"),
        conversation_key="conv-a",
        endpoint="https://example.test/v1/chat",
        include_transport=True,
    )
    assert headers["User-Agent"].startswith("opencode/1.19.3")
    assert headers["x-opencode-session-id"] == headers["x-session-affinity"]
    assert headers["x-session-id"] == headers["x-opencode-session-id"]
    assert "x-opencode-project" not in headers
    assert "x-opencode-request" not in headers
    assert headers["Host"] == "example.test"


def test_build_headers_225_emit_quartet_and_trace():
    vault = StationIdentityVault(secret=SECRET)
    runtime = resolve_station_identity(FakeStation("warp-1"), vault, _verified_registry_225())
    assert runtime is not None
    headers = build_headers(runtime, conversation_key="conv-a")
    assert headers["x-opencode-project"] == runtime.identity.project
    assert headers["b3"].startswith(runtime.identity.trace + "-")
    assert headers["x-session-affinity"] == headers["x-opencode-session"]


def test_build_body_removes_local_paths_and_agents_block():
    vault = StationIdentityVault(secret=SECRET)
    runtime = resolve_station_identity(FakeStation("warp-1"), vault, _verified_registry_225())
    assert runtime is not None
    body = build_body(BASE_BODY, runtime)
    serialized = json.dumps(body)
    assert "C:\\Users\\alice" not in serialized
    assert "SECRET_PROJECT_RULE" not in serialized
    assert "Instructions from:" not in serialized
    assert "/workspace/oc-warp-1" in serialized
    assert body["messages"][1] == {"role": "user", "content": "ping"}
    assert BASE_BODY["messages"][0]["content"].startswith("You are an AI agent")


def test_runtime_policy_keeps_last_two_versions():
    registry = ProfileRegistry()
    for profile in (_profile("1.18.31"), _profile("1.19.3"), _profile("1.19.5"), _profile_225()):
        registry.add(profile)
        for block in ("a", "b"):
            registry.record_evidence(profile.profile_id, ProfileEvidence(block, 200, True))
    assert runtime_profile_versions(registry) == ("2.2.5", "1.19.5")


def test_build_body_1195_uses_11_tool_capture():
    vault = StationIdentityVault(secret=SECRET)
    runtime = resolve_station_identity(FakeStation("warp-1"), vault, _verified_registry("1.19.5"))
    assert runtime is not None
    assert runtime.profile is not None
    body = build_body({}, runtime, client_body=BASE_BODY)
    assert len(body["tools"]) == 11
    serialized = json.dumps(body)
    assert "C:\\Users\\alice" not in serialized
    assert "/workspace/oc-warp-1" in serialized
    assert body["stream"] is True


def test_opencode_station_identity_flag_reads_env(monkeypatch):
    monkeypatch.setenv("FREE_STATION_IDENTITY", "1")
    assert oc._station_identity_enabled() is True
    monkeypatch.setenv("FREE_STATION_IDENTITY", "0")
    assert oc._station_identity_enabled() is False


def test_opencode_station_runtime_uses_label_and_registry(monkeypatch):
    vault = StationIdentityVault(secret=SECRET)
    registry = _verified_registry_225()
    monkeypatch.setattr(oc, "_STATION_IDENTITY_VAULT", vault)
    monkeypatch.setattr(oc, "_STATION_PROFILE_REGISTRY", registry)
    monkeypatch.setattr(oc, "_STATION_PROFILE_MTIME", 123.0)
    monkeypatch.setattr(oc.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(oc.os.path, "getmtime", lambda _path: 123.0)
    runtime = oc._station_identity_runtime(FakeStation("warp-1"))
    assert runtime is not None
    assert runtime.identity.label == "warp-1"
    assert runtime.profile is not None
    assert runtime.profile.profile_id == "oc-2.2.5-cli-13"

