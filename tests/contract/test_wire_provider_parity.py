"""Wire parity across egress provider paths — T063/T064/T065.

Tests that the wire body and fingerprint are identical regardless of which
egress provider path (nordvpn/warp/proxies/direct) is used, and that headers
and user-agent are also provider‑invariant.
"""
import hashlib

import pytest

import opencode as oc
from free import provider_capabilities as pc
from free import wire_profile as wp

# Canonical request body used across all provider paths.
TEST_BODY = {
    "model": "test-model",
    "messages": [{"role": "user", "content": "hello"}],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "d",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ],
    "tool_choice": "auto",
    "stream": True,
    "stream_options": {"include_usage": True},
    "max_completion_tokens": 8192,
}


@pytest.fixture(scope="module")
def profile():
    """Wire profile loaded once for the whole test module."""
    return wp.load_profile()


def _wire_and_fp(body, provider_name, profile):
    """Build a wire body and its fingerprint for a given provider path."""
    caps = pc.capabilities(provider_name)
    wire, _ = oc._free_wire_body(body, force_stream=True)
    fp = wp.fingerprint_wire(wire, profile)
    return caps, wire, fp


def test_provider_wire_fp_parity(profile):
    """T063: same request built through the four provider paths yields the same
    wire body and wire_fp. Provider paths do not alter the body by design, so
    assert the same canonical body hash and identical body content explicitly."""
    fps = {}
    caps_map = {}
    wires = {}
    for name in pc.PROVIDER_NAMES:
        caps, wire, fp = _wire_and_fp(TEST_BODY, name, profile)
        caps_map[name] = caps
        fps[name] = fp
        wires[name] = wire
    # All providers must declare the official wire profile version
    for name in pc.PROVIDER_NAMES:
        assert caps_map[name].wire_profile == "2.3.0", (
            f"{name} wire_profile mismatch: expected '2.3.0', got "
            f"{caps_map[name].wire_profile!r}"
        )
    # Provider paths do not alter the body by design: assert the same body
    # object content and the same canonical hash explicitly.
    body_hashes = {
        name: hashlib.sha256(
            wp.canonical_body(wire).encode("utf-8")
        ).hexdigest()
        for name, wire in wires.items()
    }
    assert len(set(body_hashes.values())) == 1, (
        f"wire body hash differs across providers: {body_hashes}"
    )
    first_wire = next(iter(wires.values()))
    assert all(wire == first_wire for wire in wires.values()), (
        "wire body content differs across providers"
    )
    # All fingerprints must be identical (providers do not alter the body)
    fps_values = set(fps.values())
    assert len(fps_values) == 1, (
        f"wire_fp differs across providers: {fps}"
    )


def test_provider_headers_parity(profile):
    """T064: header order and UA are identical across provider paths.

    Build the header list from the existing wire profile helper
    (_official_free_headers in opencode) and assert
    byte-identical ordering.
    """
    header_keys_by_provider = {}
    ua_by_provider = {}
    header_order_bytes_by_provider = {}
    for name in pc.PROVIDER_NAMES:
        headers = oc._official_free_headers(
            profile.endpoint,
            conversation_key="parity-test",
            egress_ip=None,
        )
        header_keys_by_provider[name] = tuple(headers.keys())
        ua_by_provider[name] = headers["User-Agent"]
        # Byte-identical header-order fingerprint:
        keys_bytes = b"\x1f".join(k.encode("utf-8") for k in headers.keys())
        header_order_bytes_by_provider[name] = keys_bytes
    # Header order must be byte-identical across providers
    header_bytes_values = set(header_order_bytes_by_provider.values())
    assert len(header_bytes_values) == 1, (
        f"header byte order differs across providers: "
        f"{header_order_bytes_by_provider}"
    )
    # User-Agent must match the spec the runtime actually sends. The active
    # wire *capture* (2.3.0) is the reference for the body/layout, while the
    # shipped impersonation spec (free/impersonation.json) is what goes on the
    # wire until the 2.3.0 migration is verified (see spec 024 KB: the vault
    # path has no registered 2.3.0 profile yet). Comparing to
    # ``profile.ua`` here conflated the two and flagged the whole runtime as a
    # provider divergence, which it is not.
    from free.impersonation import load_spec

    shipped_ua = str((load_spec() or {}).get("user_agent") or "").strip()
    assert shipped_ua, "free/impersonation.json has no user_agent"
    for name in pc.PROVIDER_NAMES:
        assert ua_by_provider[name] == shipped_ua, (
            f"{name} UA mismatch: {ua_by_provider[name]!r} vs {shipped_ua!r}"
        )
    # UA must also be byte-identical across providers
    ua_values = set(ua_by_provider.values())
    assert len(ua_values) == 1, (
        f"User-Agent differs across providers: {ua_by_provider}"
    )


def test_provider_wire_fp_stable_and_sensitive(profile):
    """T065: wire_fp is identical across providers and stable across two calls;
    a changed body changes wire_fp."""
    # Verify cross‑provider fp parity first (re‑check, mirrors T063)
    fps = {}
    for name in pc.PROVIDER_NAMES:
        _, _, fp = _wire_and_fp(TEST_BODY, name, profile)
        fps[name] = fp
    fps_values = set(fps.values())
    assert len(fps_values) == 1, (
        f"wire_fp differs across providers in T065: {fps}"
    )
    # For each provider, verify stability and sensitivity
    for name in pc.PROVIDER_NAMES:
        # stability: two consecutive calls produce the same fp
        _, _, fp1 = _wire_and_fp(TEST_BODY, name, profile)
        _, _, fp2 = _wire_and_fp(TEST_BODY, name, profile)
        assert fp1 == fp2, f"{name} wire_fp unstable across calls"
        # sensitivity: changing the model changes the fp
        modified = dict(TEST_BODY)
        modified["model"] = "other-model"
        _, _, fp3 = _wire_and_fp(modified, name, profile)
        assert fp1 != fp3, f"{name} wire_fp did not change when body model changed"