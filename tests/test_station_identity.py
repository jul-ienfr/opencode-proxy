"""P0 — identité par station, prompt templaté, affinité et audit.

Aucun réseau. Le secret est injecté dans les tests ; le vault et l'affinité
sont déterministes.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import pytest

from free.profile_registry import VerifiedProfile
from free.station_identity import (
    ENV_PREFIX,
    ConversationAffinity,
    IdentitySecretError,
    StationIdentity,
    StationIdentityVault,
    audit_identity,
    audit_station_set,
    load_or_create_secret,
    render_system_prompt,
    safe_label,
    template_official_body,
)

SECRET = b"x" * 32
BASE_PROMPT = """You are an AI agent running in OpenCode, a coding agent harness.

Instructions from: C:\\Users\\alice\\repo\\AGENTS.md
# Project rules
SECRET_PROJECT_RULE_XYZ
Use the local workspace.

Today's date: Wed Nov 04 2026

<env>
  Working directory: C:\\Users\\alice\\repo
  Workspace root folder: C:\\Users\\alice\\repo
  Is directory a git repo: yes
  Platform: win32
  Prefer C:\\Users\\alice\\AppData\\Local\\Temp
</env>
"""

FORBIDDEN = (
    "C:\\Users\\alice",
    "Workspace root folder:",
    "Is directory a git repo:",
    "Platform:",
    "Prefer ",
    "Instructions from:",
    "AGENTS.md",
    "SECRET_PROJECT_RULE_XYZ",
)


def _identity(label: str = "warp-1") -> StationIdentity:
    return StationIdentity(label, SECRET)


def _profile(version: str = "2.2.5", client: str = "cli") -> VerifiedProfile:
    if version == "2.2.5":
        user_agent = "opencode/latest/2.2.5/cli"
    else:
        user_agent = f"opencode/{version} ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4"
    return VerifiedProfile(
        profile_id=f"oc-{version}-{client}",
        version=version,
        client=client,
        tls_family="bun-1.4.4",
        user_agent=user_agent,
        body_capture="free/captures/2.2.5.json",
        body_sha256="a" * 64,
        tools_count=13 if version == "2.2.5" else 11,
        session_headers=["x-opencode-session", "x-opencode-session-id"],
    )


def test_project_trace_and_env_are_unique_per_station():
    first = _identity("warp-1")
    second = _identity("warp-2")
    assert first.project != second.project
    assert first.trace != second.trace
    assert first.env_cwd == ENV_PREFIX + "warp-1"
    assert second.env_cwd == ENV_PREFIX + "warp-2"
    assert len(first.project) == 40
    assert len(first.trace) == 32
    assert first.project != "global"


def test_same_label_same_secret_is_stable():
    first = _identity("warp-1")
    second = _identity("warp-1")
    assert first.project == second.project
    assert first.trace == second.trace
    assert first.env_cwd == second.env_cwd


def test_same_label_different_secret_changes_project():
    first = StationIdentity("warp-1", SECRET)
    second = StationIdentity("warp-1", b"y" * 32)
    assert first.project != second.project
    assert first.trace != second.trace


def test_session_is_sticky_per_conversation_and_distinct_between_stations():
    first = _identity("warp-1")
    second = _identity("warp-2")
    a1 = first.session_for("conv-a")
    a2 = first.session_for("conv-a")
    b1 = first.session_for("conv-b")
    other = second.session_for("conv-a")
    assert a1 == a2
    assert a1 != b1
    assert a1 != other
    assert a1.startswith("ses_") and len(a1) == 30


def test_msg_is_fresh_per_call():
    identity = _identity()
    first = identity.new_msg()
    second = identity.new_msg()
    assert first != second
    assert first.startswith("msg_")
    assert second.startswith("msg_")


def test_headers_are_station_scoped_and_225_has_tracing():
    first = _identity("warp-1")
    second = _identity("warp-2")
    h1 = first.headers(_profile(), conversation_key="conv-a")
    h2 = second.headers(_profile(), conversation_key="conv-a")
    assert h1["x-opencode-project"] != h2["x-opencode-project"]
    assert h1["x-opencode-session"] != h2["x-opencode-session"]
    assert h1["x-opencode-request"] != h2["x-opencode-request"]
    assert h1["b3"].startswith(first.trace + "-")
    assert h1["traceparent"].startswith("00-" + first.trace + "-")
    assert h1["x-session-affinity"] == h1["x-opencode-session"]
    assert h1["x-session-id"] == h1["x-opencode-session"]
    assert h1["x-opencode-session-id"] == h1["x-opencode-session"]


def test_headers_1195_do_not_emit_225_tracing():
    identity = _identity()
    headers = identity.headers(_profile("1.19.5", "desktop"), conversation_key="conv-a")
    assert "x-opencode-session-id" in headers
    assert "b3" not in headers
    assert "traceparent" not in headers
    assert "x-session-affinity" not in headers
    assert "x-session-id" not in headers


def test_headers_1193_use_legacy_session_set():
    identity = _identity()
    headers = identity.headers(_profile("1.19.3", "desktop"), conversation_key="conv-a")
    assert "x-opencode-session-id" in headers
    assert headers["x-session-affinity"] == headers["x-opencode-session-id"]
    assert headers["x-session-id"] == headers["x-opencode-session-id"]
    assert "x-opencode-client" not in headers
    assert "x-opencode-project" not in headers
    assert "x-opencode-request" not in headers
    assert "x-opencode-session" not in headers


def test_headers_11831_use_affinity_only():
    identity = _identity()
    headers = identity.headers(_profile("1.18.31", "desktop"), conversation_key="conv-a")
    assert "x-opencode-session-id" not in headers
    assert headers["x-session-affinity"] == headers["x-session-id"]
    assert "x-opencode-client" not in headers
    assert "x-opencode-project" not in headers
    assert "x-opencode-request" not in headers


def test_prompt_template_removes_real_path_and_keeps_official_marker():
    identity = _identity("warp-1")
    prompt = identity.prompt(BASE_PROMPT, now=datetime(2026, 11, 6, tzinfo=UTC))
    assert "You are an AI agent running in OpenCode" in prompt
    assert "Working directory: /workspace/oc-warp-1" in prompt
    assert "Today's date: Fri Nov 06 2026" in prompt
    for marker in FORBIDDEN:
        assert marker not in prompt
    assert "C:\\Users\\alice" not in prompt


def test_prompt_replaces_stray_windows_path():
    base = "Path outside env: C:\\Users\\alice\\repo\\notes.txt\n" + BASE_PROMPT
    prompt = _identity("warp-1").prompt(base, now=datetime(2026, 11, 6, tzinfo=UTC))
    assert "C:\\Users\\alice" not in prompt
    assert "/workspace/oc-warp-1" in prompt


def test_prompt_template_appends_env_when_missing():
    prompt = render_system_prompt(
        "You are an AI agent running in OpenCode.",
        "/workspace/oc-warp-2",
        now=datetime(2026, 11, 6, tzinfo=UTC),
    )
    assert "<env>" in prompt
    assert "Working directory: /workspace/oc-warp-2" in prompt
    assert "Today's date: Fri Nov 06 2026" in prompt


def test_template_official_body_only_rewrites_system_message():
    identity = _identity("warp-1")
    body = {
        "model": "deepseek-v4.1-flash-free",
        "messages": [
            {"role": "system", "content": BASE_PROMPT},
            {"role": "user", "content": "keep me"},
        ],
        "stream": True,
    }
    templated = template_official_body(body, identity, now=datetime(2026, 11, 6, tzinfo=UTC))
    assert "Working directory: /workspace/oc-warp-1" in templated["messages"][0]["content"]
    assert "C:\\Users\\alice" not in templated["messages"][0]["content"]
    assert templated["messages"][1] == {"role": "user", "content": "keep me"}
    assert body["messages"][0]["content"] == BASE_PROMPT


def test_template_official_body_leaves_title_prompt_without_env():
    identity = _identity("warp-1")
    title = "You are a title generator. You output ONLY a thread title."
    body = {"messages": [{"role": "system", "content": title}]}
    templated = template_official_body(body, identity)
    assert templated["messages"][0]["content"] == title


def test_template_sanitizes_tool_descriptions():
    identity = _identity("warp-1")
    body = {
        "messages": [{"role": "system", "content": BASE_PROMPT}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "shell",
                    "description": "Use C:\\Users\\alice\\AppData\\Local\\Temp for temp files.",
                },
            }
        ],
    }
    templated = template_official_body(body, identity)
    serialized = json.dumps(templated)
    assert "C:\\Users\\alice" not in serialized
    assert "/workspace/oc-warp-1" in serialized


def test_audit_accepts_clean_station_set():
    identities = [_identity("warp-1"), _identity("warp-2")]
    result = audit_station_set(
        identities,
        BASE_PROMPT,
        FORBIDDEN,
        now=datetime(2026, 11, 6, tzinfo=UTC),
    )
    assert result.status == "ok"
    assert result.duplicates == []
    assert result.leaks == []
    assert result.checked == 2


def test_audit_detects_duplicate_project():
    first = _identity("warp-1")
    second = _identity("warp-2")
    second.project = first.project
    result = audit_station_set([first, second], BASE_PROMPT, FORBIDDEN)
    assert result.status == "duplicate_identity"
    assert any(item.startswith("project:") for item in result.duplicates)


def test_audit_detects_prompt_leak_outside_env():
    leaked = "Leak: alice-hostname\n" + BASE_PROMPT
    identity = _identity()
    result = audit_station_set([identity], leaked, (*FORBIDDEN, "alice-hostname"))
    assert result.status == "prompt_leak"
    assert any(item["label"] == identity.label for item in result.leaks)
    assert "prompt_leak:alice-hostname" in audit_identity(identity, leaked, (*FORBIDDEN, "alice-hostname"))


def test_secret_file_is_created_and_reloaded(tmp_path):
    path = tmp_path / "identity_secret"
    first = load_or_create_secret(path)
    second = load_or_create_secret(path)
    assert len(first) == 32
    assert first == second
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_corrupt_secret_raises(tmp_path):
    path = tmp_path / "identity_secret"
    path.write_text("zz", encoding="ascii")
    with pytest.raises(IdentitySecretError):
        load_or_create_secret(path)


def test_vault_reuses_identity_and_refuses_short_secret(tmp_path):
    vault = StationIdentityVault(secret=SECRET, secret_path=tmp_path / "secret")
    first = vault.identity_for("warp-1")
    second = vault.identity_for("warp-1")
    assert first is second
    with pytest.raises(IdentitySecretError):
        StationIdentityVault(secret=b"short", secret_path=tmp_path / "secret2")


def test_safe_label_blocks_path_traversal():
    assert safe_label("warp-1") == "warp-1"
    assert safe_label("../../etc/passwd") == "etc-passwd"
    assert safe_label("") == "station"


def test_affinity_sticky_ttl_and_failover(caplog):
    affinity = ConversationAffinity(ttl_s=10)
    affinity.set("conv-a", "warp-1", now=1000)
    assert affinity.get("conv-a", now=1005) == "warp-1"
    assert affinity.get("conv-a", now=1011) is None
    affinity.set("conv-b", "warp-1", now=2000)
    affinity.set("conv-c", "warp-1", now=2000)
    affinity.set("conv-d", "warp-2", now=2010)
    assert affinity.prune(now=2011) == 2
    affinity.set("conv-e", "warp-1", now=3000)
    with caplog.at_level("INFO"):
        assert affinity.failover("conv-e", "warp-2", now=3000) == "warp-2"
    assert affinity.get("conv-e", now=3000) == "warp-2"
    assert "conversation_failover" in caplog.text
