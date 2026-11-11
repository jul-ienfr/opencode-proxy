"""Transport cycle tests: all modes, 429 does not skip, wrap to WireGuard."""

from __future__ import annotations

from free.qualification.domain import AttemptStatus
from free.qualification.transport import build_plan


def test_auto_uses_all_supported_transports_in_order():
    plan = build_plan(("wireguard", "openvpn-udp", "openvpn-tcp"))
    assert plan.current == "wireguard"
    plan = plan.advance(AttemptStatus.NO_EGRESS)
    assert plan.current == "openvpn-udp"
    plan = plan.advance(AttemptStatus.NO_EGRESS)
    assert plan.current == "openvpn-tcp"


def test_auto_wraps_to_wireguard_after_all_fail():
    plan = build_plan(("wireguard", "openvpn-udp", "openvpn-tcp"), max_rounds=2)
    for _ in range(3):
        plan = plan.advance(AttemptStatus.NO_EGRESS)
    assert plan.current == "wireguard"
    assert plan.round == 1


def test_429_does_not_skip_other_transports():
    plan = build_plan(("wireguard", "openvpn-udp", "openvpn-tcp"))
    plan = plan.advance(AttemptStatus.QUOTA)
    assert plan.current == "openvpn-udp"


def test_200_stops_the_cycle():
    plan = build_plan(("wireguard", "openvpn-udp"))
    plan = plan.advance(AttemptStatus.OK)
    assert plan.current is None
    assert plan.done


def test_auth_stops_the_cycle():
    plan = build_plan(("wireguard", "openvpn-udp"))
    plan = plan.advance(AttemptStatus.AUTH)
    assert plan.current is None
    assert plan.done


def test_unsupported_transport_is_skipped():
    plan = build_plan(("openvpn-tcp",))
    assert plan.order == ("openvpn-tcp",)
    assert plan.current == "openvpn-tcp"


def test_manual_uses_only_chosen_transport():
    plan = build_plan(("wireguard", "openvpn-udp"), strategy="manual", manual="openvpn-udp")
    assert plan.order == ("openvpn-udp",)
    assert plan.current == "openvpn-udp"


def test_empty_supported_is_exhausted():
    plan = build_plan(())
    assert plan.current is None
    assert plan.exhausted
