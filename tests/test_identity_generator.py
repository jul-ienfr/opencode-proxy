"""Générateur d'impersonation : matrice versions/clients verrouillée.

Références live mitm 2026-11-01 :
* 1.18.31 : UA Bun 1.3.14, quartet sans x-opencode-session-id ;
* 1.19.0 : UA Bun 1.3.14, quartet + session-id ;
* 1.19.3 : UA Bun 1.3.14, quartet + session-id ;
* 1.19.5 : UA Bun 1.4.4, quartet + session-id.
Corps principal identique : 11 tools (websearch inclus), système 21 604 chars.
"""

from __future__ import annotations

import opencode as oc
from free.identity_generator import generate_many, generate_profile


def _profile(version: str, client: str = "desktop", generation: int = 1):
    return generate_profile(version=version, client=client, generation=generation)


def test_generated_headers_match_each_version_capture():
    expectations = {
        "1.18.31": ("opencode/1.18.31 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14", False),
        "1.19.0": ("opencode/1.19.0 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14", True),
        "1.19.3": ("opencode/1.19.3 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14", True),
        "1.19.5": ("opencode/1.19.5 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4", True),
    }
    for version, (ua, session_id) in expectations.items():
        p = _profile(version)
        h = p["headers"]
        assert h["Authorization"] == "Bearer public"
        assert h["Content-Type"] == "application/json"
        assert h["User-Agent"] == ua
        assert h["x-opencode-client"] == "desktop"
        assert h["x-opencode-project"] == p["project"]
        assert h["x-opencode-request"] == h["x-opencode-request"]
        assert h["x-opencode-session"] == p["session"]
        assert ("x-opencode-session-id" in h) is session_id
        if session_id:
            assert h["x-opencode-session-id"] == p["session"]
        assert "x-session-affinity" not in h
        assert "x-session-id" not in h
        assert "x-opencode-directory" not in h
        assert "x-opencode-workspace" not in h


def test_tls_profiles_match_captured_client_hellos():
    old = (
        "771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49161-49171"
        "-49162-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-21,"
        "29-23-24,0"
    )
    new = (
        "771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49161-49171"
        "-49162-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43,"
        "4588-29-23-24,0"
    )
    for version in ("1.18.31", "1.18.35", "1.19.0", "1.19.3"):
        p = _profile(version)
        assert p["tls"]["ja3"] == old, version
        assert p["tls"]["ja3_md5"] == "d871d02cecbde59abbf8f4806134addf"
        assert len(p["tls"]["sigalgs"]) == 9
    p = _profile("1.19.5")
    assert p["tls"]["ja3"] == new
    assert p["tls"]["ja3_md5"] == "1523504b38f0fae0d881d4b6554aac1b"
    assert len(p["tls"]["sigalgs"]) == 11
    assert "ed25519" in p["tls"]["sigalgs"]
    assert p["tls"]["grease"] is False


def test_proxy_fp_selector_uses_version_specific_ja3():
    _, fp_old = oc._get_free_fp_kwargs("1.18.31")
    _, fp_1193 = oc._get_free_fp_kwargs("1.19.3")
    _, fp_1195 = oc._get_free_fp_kwargs("1.19.5")
    assert fp_old["ja3"] == oc._OPENCODE_JA3
    assert fp_1193["ja3"] == oc._OPENCODE_JA3
    assert fp_1195["ja3"] == oc._OPENCODE_JA3_1195
    assert fp_old["extra_fp"]["header_order"] == oc._OPENCODE_HEADER_ORDER
    assert fp_1193["extra_fp"]["header_order"] == oc._OPENCODE_HEADER_ORDER_119
    assert fp_1195["extra_fp"]["header_order"] == oc._OPENCODE_HEADER_ORDER_119
    assert fp_1195["extra_fp"]["tls_signature_algorithms"] == list(oc._OPENCODE_SIG_ALGS_1195)


def test_generated_header_order_matches_client():
    p = _profile("1.19.5")
    assert list(p["headers"].keys()) == [
        "Authorization",
        "Content-Type",
        "User-Agent",
        "x-opencode-client",
        "x-opencode-project",
        "x-opencode-request",
        "x-opencode-session",
        "x-opencode-session-id",
        "Connection",
        "Accept",
        "Host",
        "Accept-Encoding",
    ]


def test_generated_body_profile_is_the_11_tool_official_copy():
    p = _profile("1.19.5")
    body = p["body"]
    assert body["tools_count"] == 11
    assert body["tools"] == [
        "bash",
        "edit",
        "glob",
        "grep",
        "read",
        "skill",
        "task",
        "todowrite",
        "webfetch",
        "websearch",
        "write",
    ]
    assert body["system_chars"] == 21604


def test_generator_alternates_versions_and_clients_with_distinct_ids():
    profiles = generate_many(
        count=8,
        versions=["1.18.31", "1.19.0", "1.19.3", "1.19.5"],
        clients=["desktop", "cli"],
        start_generation=1,
    )
    assert [p["version"] for p in profiles] == [
        "1.18.31",
        "1.19.0",
        "1.19.3",
        "1.19.5",
        "1.18.31",
        "1.19.0",
        "1.19.3",
        "1.19.5",
    ]
    assert [p["client"] for p in profiles] == ["desktop", "cli"] * 4
    assert len({p["project"] for p in profiles}) == 8
    assert len({p["session"] for p in profiles}) == 8
    assert len({p["headers"]["x-opencode-request"] for p in profiles}) == 8
    assert profiles[0]["verified"] is True
    assert profiles[1]["verified"] is False  # cli non capturé live


def test_generated_profiles_do_not_touch_the_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_IDENTITIES_DIR", str(tmp_path / "ids"))
    before = list((tmp_path / "ids").glob("*.json")) if (tmp_path / "ids").exists() else []
    generate_many(count=3, versions=["1.19.5"], clients=["desktop"])
    after = list((tmp_path / "ids").glob("*.json")) if (tmp_path / "ids").exists() else []
    assert after == before == [], "le générateur ne doit pas persister d'identité"


def test_title_request_passes_through_unchanged():
    payload = oc._official_free_payload()
    title_body = {
        "model": "deepseek-v4.1-flash-free",
        "max_tokens": 32000,
        "temperature": 0.5,
        "reasoning_effort": "low",
        "messages": [
            {"role": "system", "content": "You are a title generator. You output ONLY a thread title."},
            {"role": "user", "content": "Generate a title for this conversation:\n"},
            {"role": "user", "content": "hi"},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    wire, forced = oc._free_wire_body(title_body, force_stream=True)
    assert forced is False
    assert wire == title_body
    assert "tools" not in wire
    assert "You are opencode, an interactive CLI tool" not in wire["messages"][0]["content"]
    assert payload["tools"], "le payload officiel reste disponible pour les gros corps"
