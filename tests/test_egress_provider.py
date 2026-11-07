"""Contrat EgressProvider 007 : blocs, familles, résultat, protocole."""

from __future__ import annotations

from types import SimpleNamespace

from free.egress_provider import (
    EgressProvider,
    EgressRotationResult,
    block_for_egress,
    family_for_egress,
)


def test_block_for_egress_ipv6_uses_slash_64():
    assert block_for_egress("2a09:bac5:32d3:14f0::1") == "2a09:bac5:32d3:14f0::/64"


def test_block_for_egress_ipv4_uses_slash_32():
    assert block_for_egress("104.28.1.2") == "104.28.1.2/32"


def test_block_for_egress_invalid_is_none():
    assert block_for_egress("") is None
    assert block_for_egress("socks5:warp-a") is None
    assert block_for_egress(None) is None


def test_family_for_egress():
    assert family_for_egress("2a09:bac5:32d3:14f0::1") == "ipv6"
    assert family_for_egress("104.28.1.2") == "ipv4"
    assert family_for_egress("socks5:warp-a") is None


def test_rotation_result_shape():
    result = EgressRotationResult(
        label="warp-1",
        ok=True,
        old_ip="2a09:bac5:32d3:14f0::1",
        new_ip="2a09:bac5:32d4:14f0::1",
        block="2a09:bac5:32d4:14f0::/64",
        attempts=2,
        reason="rotated",
        provider="warp",
    )
    assert result.as_dict()["block"] == "2a09:bac5:32d4:14f0::/64"
    assert result.as_dict()["provider"] == "warp"


def test_protocol_is_runtime_checkable():
    class Dummy:
        name = "dummy"

        def rotate(self, endpoint, *, forbidden_blocks=None, probe=None):
            return EgressRotationResult("dummy", True, None, None, None, 0, "noop", "dummy")

        def refresh_egress(self, endpoint):
            return None

        def health(self):
            return {"ok": True}

    assert isinstance(Dummy(), EgressProvider)
    endpoint = SimpleNamespace(label="x")
    assert Dummy().rotate(endpoint).provider == "dummy"
