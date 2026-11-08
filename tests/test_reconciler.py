"""Tests du reconciler (011 T012)."""

from __future__ import annotations

from ops.desired_state import DesiredEndpointSpec, DesiredStationSpec
from ops.reconciler import ActualSlot, decide, reconcile


def _station(**kwargs) -> DesiredStationSpec:
    defaults = {
        "provider": "nordvpn",
        "station": 1,
        "stack": "wireguard",
        "transport": "wireguard",
        "container": "opencode-vpn",
        "port": 1080,
    }
    defaults.update(kwargs)
    return DesiredStationSpec(**defaults)


def test_missing_creates() -> None:
    assert decide(_station(), None).action == "create"


def test_conforme_is_noop() -> None:
    actual = ActualSlot(
        provider="nordvpn",
        slot_id=1,
        container="opencode-vpn",
        generation=1,
        stack="wireguard",
        transport="wireguard",
        running=True,
        health="healthy",
    )
    assert decide(_station(), actual).action == "noop"


def test_generation_mismatch_recreates_once() -> None:
    actual = ActualSlot(
        provider="nordvpn",
        slot_id=1,
        generation=1,
        stack="wireguard",
        transport="wireguard",
        running=True,
        health="healthy",
    )
    decision = decide(_station(generation=2), actual)
    assert decision.action == "recreate"
    assert decision.reason == "generation"


def test_stack_mismatch_recreates() -> None:
    actual = ActualSlot(
        provider="nordvpn",
        slot_id=1,
        generation=1,
        stack="openvpn",
        transport="openvpn-udp",
        running=True,
        health="healthy",
    )
    assert decide(_station(), actual).reason == "stack"


def test_stopped_starts() -> None:
    actual = ActualSlot(provider="nordvpn", slot_id=1, running=False)
    assert decide(_station(), actual).action == "start"


def test_unhealthy_repairs_not_deletes() -> None:
    actual = ActualSlot(
        provider="nordvpn",
        slot_id=1,
        generation=1,
        stack="wireguard",
        transport="wireguard",
        running=True,
        health="unhealthy",
    )
    decision = decide(_station(), actual)
    assert decision.action == "repair"
    assert decision.action != "delete"


def test_endpoint_reconcile_by_container() -> None:
    desired = DesiredEndpointSpec(
        provider="warp",
        label="warp-test",
        container="warp-test",
        port=11080,
        block="2a09:bac1:27a0:2060::/64",
    )
    actual = [
        ActualSlot(
            provider="warp",
            slot_id=0,
            container="warp-test",
            generation=1,
            stack="warp",
            transport="socks5",
            running=True,
            health="healthy",
            block="2a09:bac1:27a0:2060::/64",
        )
    ]
    decisions = reconcile([desired], actual)
    assert decisions[0].action == "noop"


def test_never_deletes_a_running_conforming_slot() -> None:
    desired = [_station()]
    actual = [
        ActualSlot(
            provider="nordvpn",
            slot_id=1,
            generation=1,
            stack="wireguard",
            transport="wireguard",
            running=True,
            health="healthy",
        )
    ]
    assert all(decision.action != "delete" for decision in reconcile(desired, actual))
