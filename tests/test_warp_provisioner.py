"""Tests du provisioner WARP (011 US2)."""

from __future__ import annotations

import pytest

from free.warp_provisioner import ProvisionAction, WarpEndpointSpec, WarpProvisioner


def _spec(label: str = "warp-test", port: int = 11080, **kwargs) -> WarpEndpointSpec:
    defaults = {
        "label": label,
        "container": label,
        "port": port,
        "block": "2a09:bac1:27a0:2060::/64",
    }
    defaults.update(kwargs)
    return WarpEndpointSpec(**defaults)


def test_plan_empty_is_empty() -> None:
    assert WarpProvisioner().plan([]) == []


def test_plan_creates_missing() -> None:
    actions = WarpProvisioner().plan([_spec()], {})
    assert len(actions) == 1
    assert actions[0].action == "create"


def test_plan_noop_when_conforme() -> None:
    actual = {
        "warp-test": {
            "image": "caomingjun/warp:latest",
            "ports": "127.0.0.1:11080->1080/tcp",
        }
    }
    actions = WarpProvisioner().plan([_spec()], actual)
    assert actions[0].action == "noop"


def test_plan_recreates_on_wrong_bind() -> None:
    actual = {
        "warp-test": {
            "image": "caomingjun/warp:latest",
            "ports": "0.0.0.0:11080->1080/tcp",
        }
    }
    actions = WarpProvisioner().plan([_spec()], actual)
    assert actions[0].action == "recreate"
    assert actions[0].reason == "bind"


def test_plan_rejects_duplicate_port() -> None:
    with pytest.raises(ValueError, match="duplicate port"):
        WarpProvisioner().plan(
            [_spec(label="a", port=11080), _spec(label="b", port=11080)]
        )


def test_plan_rejects_duplicate_block() -> None:
    with pytest.raises(ValueError, match="duplicate block"):
        WarpProvisioner().plan(
            [
                _spec(label="a", port=11080, block="2a09:bac1:27a0:2060::/64"),
                _spec(label="b", port=11081, block="2a09:bac1:27a0:2060::/64"),
            ]
        )


def test_apply_runs_docker_run_with_loopback_and_options() -> None:
    calls: list[list[str]] = []

    def runner(args, timeout=120):
        calls.append(list(args))
        return ""

    result = WarpProvisioner(runner=runner).apply([_spec(env={"WARP_SLEEP": "2"})], {})
    assert result["ok"] is True
    run = next(args for args in calls if args and args[0] == "run")
    assert "127.0.0.1:11080:1080" in run
    assert "--memory" in run
    assert "--label" in run
    assert "WARP_SLEEP=2" in run


def test_apply_reports_rollback_on_run_failure() -> None:
    calls: list[list[str]] = []

    def runner(args, timeout=120):
        calls.append(list(args))
        if args and args[0] == "run":
            raise RuntimeError("boom")
        return ""

    result = WarpProvisioner(runner=runner).apply([_spec()], {})
    assert result["ok"] is False
    assert "boom" in result["error"]


def test_delete_requires_confirmation() -> None:
    assert WarpProvisioner().delete("warp-test") == {
        "ok": False,
        "error": "confirmation_required",
    }


def test_action_serialization() -> None:
    action = ProvisionAction("create", "warp-test", "warp-test", "missing")
    assert action.as_dict()["action"] == "create"
