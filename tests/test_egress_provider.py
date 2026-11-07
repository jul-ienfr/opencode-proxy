"""Contrat EgressProvider 007 : blocs, familles, résultat, protocole."""

from __future__ import annotations

from types import SimpleNamespace

from free.egress_provider import (
    EgressProvider,
    EgressRotationResult,
    block_for_egress,
    family_for_egress,
    measure_egress,
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


class _RefreshEndpoint:
    def __init__(self, ip, *, ipv6=True, raise_error=False):
        self._ip = ip
        self.ipv6 = ipv6
        self._raise = raise_error

    def refresh_egress(self, timeout=6.0):
        if self._raise:
            raise RuntimeError("boom")
        return self._ip


def test_measure_egress_accepts_matching_family():
    assert measure_egress(_RefreshEndpoint("2a09:bac5:32d3:14f0::1")) == "2a09:bac5:32d3:14f0::1"
    assert measure_egress(_RefreshEndpoint("104.28.1.2", ipv6=False)) == "104.28.1.2"


def test_measure_egress_rejects_wrong_family_and_logical_id():
    assert measure_egress(_RefreshEndpoint("104.28.1.2", ipv6=True)) is None
    assert measure_egress(_RefreshEndpoint("socks5:warp-a")) is None
    assert measure_egress(_RefreshEndpoint("invalid")) is None


def test_measure_egress_accepts_plain_egress_ip_and_fail_soft():
    class Plain:
        ipv6 = False
        egress_ip = "104.28.1.2"

    assert measure_egress(Plain()) == "104.28.1.2"
    assert measure_egress(_RefreshEndpoint("", raise_error=True)) is None


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
