"""[019 T321] Borne du repli WARP : 2–3 conteneurs, sweeps arrêtés."""

from __future__ import annotations

import pytest

from free.pool import _warp_allowed_containers
from free.warp_provisioner import (
    ProvisionAction,
    WarpEndpointSpec,
    WarpProvisioner,
    is_warp_container_name,
    list_warp_containers,
    warp_container_cap,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 3), ("1", 2), ("2", 2), ("3", 3), ("12", 3), ("abc", 3), ("-4", 2)],
)
def test_warp_container_cap_is_bounded(monkeypatch, raw: str, expected: int) -> None:
    monkeypatch.setenv("WARP_MAX_CONTAINERS", raw)
    assert warp_container_cap() == expected


def test_is_warp_container_name() -> None:
    assert is_warp_container_name("warp-4")
    assert is_warp_container_name(" WARP-12 ")
    assert not is_warp_container_name("opencode-vpn-2")


def _spec(index: int) -> WarpEndpointSpec:
    return WarpEndpointSpec(
        label=f"WARP {index}",
        container=f"warp-{index}",
        port=11080 + index,
    )


def _actual(*indexes: int) -> dict[str, dict[str, str]]:
    return {
        f"warp-{index}": {
            "image": "caomingjun/warp:latest",
            "ports": f"127.0.0.1:{11080 + index}->1080/tcp",
        }
        for index in indexes
    }


def test_plan_skips_create_at_cap() -> None:
    actions = WarpProvisioner().plan([_spec(4)], _actual(1, 2, 3))
    assert actions == [ProvisionAction("skip", "WARP 4", "warp-4", "warp-cap")]


def test_plan_creates_below_cap() -> None:
    actions = WarpProvisioner().plan([_spec(3)], _actual(1, 2))
    assert [action.action for action in actions] == ["create"]


def test_apply_refuses_to_run_a_fourth_warp() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], timeout: int) -> str:
        calls.append(args)
        if args[:2] == ["ps", "-a"]:
            return "warp-1\nwarp-2\nwarp-3\n"
        return ""

    result = WarpProvisioner(runner=runner).apply([_spec(4)], {})
    assert result["ok"] is True
    assert result["applied"][0]["action"] == "skip"
    assert result["applied"][0]["reason"] == "warp-cap"
    assert not any(args[0] == "run" for args in calls)


def test_apply_creates_below_cap() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], timeout: int) -> str:
        calls.append(args)
        if args[:2] == ["ps", "-a"]:
            return "warp-1\n"
        return ""

    result = WarpProvisioner(runner=runner).apply([_spec(2)], {})
    assert result["applied"][0]["action"] == "create"
    assert any(args[0] == "run" and "warp-2" in args for args in calls)


def test_list_warp_containers_filters_names() -> None:
    def runner(args: list[str], timeout: int) -> str:
        return "warp-1\nopencode-vpn-2\nwarp-9\n"

    assert list_warp_containers(runner) == ["warp-1", "warp-9"]


def test_list_warp_containers_is_fail_open() -> None:
    def runner(args: list[str], timeout: int) -> str:
        raise RuntimeError("docker down")

    assert list_warp_containers(runner) == []


def test_pool_disables_containers_beyond_cap(monkeypatch) -> None:
    monkeypatch.setenv("WARP_MAX_CONTAINERS", "2")
    rows = [
        {"label": f"WARP {i}", "container": f"warp-{i}", "managed": True}
        for i in range(1, 6)
    ]
    assert _warp_allowed_containers(rows) == {"warp-1", "warp-2"}


def test_pool_keeps_ipv4_ipv6_pairs_of_a_kept_container(monkeypatch) -> None:
    monkeypatch.setenv("WARP_MAX_CONTAINERS", "2")
    rows = [
        {"label": "WARP 1", "container": "warp-1", "managed": True, "ipv6": False},
        {"label": "WARP 1 IPv6", "container": "warp-1", "managed": True, "ipv6": True},
        {"label": "WARP 2", "container": "warp-2", "managed": True, "ipv6": False},
        {"label": "WARP 2 IPv6", "container": "warp-2", "managed": True, "ipv6": True},
        {"label": "WARP 3", "container": "warp-3", "managed": True, "ipv6": False},
    ]
    assert _warp_allowed_containers(rows) == {"warp-1", "warp-2"}


def test_plan_bound_keeps_configured_containers_first() -> None:
    from scripts.warp_bound_fleet import plan_bound

    plan = plan_bound(
        cap=2,
        existing=["warp-1", "warp-2", "warp-3", "warp-4"],
        configured=["warp-3", "warp-1", "warp-2"],
    )
    assert plan["keep"] == ["warp-3", "warp-1"]
    assert plan["remove"] == ["warp-2", "warp-4"]


def test_plan_bound_fills_from_existing_when_config_is_stale() -> None:
    from scripts.warp_bound_fleet import plan_bound

    plan = plan_bound(cap=3, existing=["warp-7", "warp-8"], configured=["warp-99"])
    assert plan["keep"] == ["warp-7", "warp-8"]
    assert plan["remove"] == []
