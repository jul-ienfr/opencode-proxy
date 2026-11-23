import json

import pytest

from free import wire_profile as wp


@pytest.fixture(scope="module")
def profile():
    return wp.load_profile()


def test_profile_matches_capture(profile):
    assert profile.version == "2.3.0"
    assert profile.ua.endswith("/2.3.0/cli")
    assert len(profile.header_order) == 16
    assert len(profile.body_keys) == 6
    assert len(profile.tool_names) == 13
    assert profile.tool_names[0] == "edit"


def test_fingerprint_is_stable_and_sensitive(profile):
    body = {"model": "m", "messages": [], "stream": True}
    a = wp.fingerprint(body, ua="ua", header_order=("a", "b"), tls_ja3_md5="ja3")
    b = wp.fingerprint(body, ua="ua", header_order=("a", "b"), tls_ja3_md5="ja3")
    c = wp.fingerprint(body, ua="ua2", header_order=("a", "b"), tls_ja3_md5="ja3")
    d = wp.fingerprint(body, ua="ua", header_order=("b", "a"), tls_ja3_md5="ja3")
    assert a == b
    assert a != c and a != d
    assert len(a) == 64


def test_official_capture_is_conformant(profile):
    with open("free/captures/2.3.0.json", encoding="utf-8") as fh:
        raw = json.load(fh)["body_raw"]
    wire = json.loads(raw)
    assert wp.conformance(wire, profile).ok


def test_root_system_is_not_conformant(profile):
    wire = {"model": "m", "messages": [], "tools": [], "stream": True,
            "stream_options": {}, "max_completion_tokens": 1, "system": "x"}
    result = wp.conformance(wire, profile)
    assert not result.ok
    assert "system" in result.forbidden_root


def test_missing_official_tool_is_not_conformant(profile):
    wire = {
        "model": "m",
        "messages": [],
        "tools": [{"type": "function", "function": {"name": "read"}}],
        "stream": True,
        "stream_options": {},
        "max_completion_tokens": 1,
    }
    result = wp.conformance(wire, profile)
    assert not result.ok
    assert "edit" in result.missing_tools


def test_forbidden_tool_is_rejected(profile):
    tools = [{"type": "function", "function": {"name": n}} for n in profile.tool_names]
    tools.append({"type": "function", "function": {"name": "subagent_fork"}})
    wire = {
        "model": "m",
        "messages": [],
        "tools": tools,
        "stream": True,
        "stream_options": {},
        "max_completion_tokens": 1,
    }
    result = wp.conformance(wire, profile)
    assert not result.ok
    assert "subagent_fork" in result.forbidden_tools

def test_note_current_wire_fp_sets_the_attempt_context():
    import opencode as oc

    token = oc._current_free_attempt.set({"ip": "8.8.8.8"})
    try:
        fp = oc._note_current_wire_fp({"model": "m", "messages": []})
        assert fp and len(fp) == 64
        assert (oc._current_free_attempt.get() or {}).get("wire_fp") == fp
    finally:
        oc._current_free_attempt.reset(token)
