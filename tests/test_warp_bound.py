"""Tests de la borne WARP (019 T321).

WARP est un fallback, pas la flotte principale : au plus
``MAX_WARP_CONTAINERS`` conteneurs managés.  Le compteur vient du provisioner
(Docker, ou liste persistée quand Docker est indisponible).
"""

from __future__ import annotations

from free.warp_provisioner import (
    MAX_WARP_CONTAINERS,
    WARP_CAP_REASON,
    WarpEndpointSpec,
    WarpProvisioner,
    warp_cap_status,
)


def _spec(label: str = "warp-test", port: int = 11080, **kwargs) -> WarpEndpointSpec:
    defaults = {
        "label": label,
        "container": label,
        "port": port,
        "block": "2a09:bac1:27a0:2060::/64",
    }
    defaults.update(kwargs)
    return WarpEndpointSpec(**defaults)


def _runner(calls: list[list[str]]):
    def run(args, timeout=120):
        calls.append(list(args))
        return ""

    return run


def test_max_is_three() -> None:
    assert MAX_WARP_CONTAINERS == 3


def test_cap_status_allows_two_existing() -> None:
    provisioner = WarpProvisioner(container_lister=lambda: ["warp-1", "warp-2"])
    status = provisioner.warp_cap_status()
    assert status == {"count": 2, "max": MAX_WARP_CONTAINERS, "allowed": True}


def test_cap_status_refuses_three_existing() -> None:
    provisioner = WarpProvisioner(
        container_lister=lambda: ["warp-1", "warp-2", "warp-3"]
    )
    status = provisioner.warp_cap_status()
    assert status["count"] == 3
    assert status["max"] == MAX_WARP_CONTAINERS
    assert status["allowed"] is False


def test_cap_status_unknown_fails_open() -> None:
    provisioner = WarpProvisioner(container_lister=lambda: None)
    status = provisioner.warp_cap_status()
    assert status["count"] is None
    assert status["max"] == MAX_WARP_CONTAINERS
    assert status["allowed"] is True


def test_apply_allows_new_container_below_cap() -> None:
    calls: list[list[str]] = []
    provisioner = WarpProvisioner(
        runner=_runner(calls),
        container_lister=lambda: ["warp-1", "warp-2"],
    )
    result = provisioner.apply([_spec(label="warp-3", port=11086)], {})
    assert result["ok"] is True
    assert any(call and call[0] == "run" for call in calls)


def test_apply_refuses_when_cap_reached() -> None:
    calls: list[list[str]] = []
    provisioner = WarpProvisioner(
        runner=_runner(calls),
        container_lister=lambda: ["warp-1", "warp-2", "warp-3"],
    )
    result = provisioner.apply([_spec(label="warp-4", port=11087)], {})
    assert result["ok"] is False
    assert result["reason"] == WARP_CAP_REASON
    assert result["error"] == WARP_CAP_REASON
    assert result["count"] == 3
    assert result["applied"] == []
    assert calls == []


def test_apply_fails_open_when_count_unknown() -> None:
    calls: list[list[str]] = []
    provisioner = WarpProvisioner(
        runner=_runner(calls),
        container_lister=lambda: None,
    )
    result = provisioner.apply([_spec(label="warp-3", port=11086)], {})
    assert result["ok"] is True
    assert any(call and call[0] == "run" for call in calls)


def test_module_level_status_uses_provisioner(monkeypatch) -> None:
    monkeypatch.setattr(
        WarpProvisioner,
        "list_managed_containers",
        lambda self: ["warp-1", "warp-2"],
    )
    assert warp_cap_status() == {
        "count": 2,
        "max": MAX_WARP_CONTAINERS,
        "allowed": True,
    }
