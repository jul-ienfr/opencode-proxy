"""Supervisor repair adapter tests: local restart, no cross-station call."""

from __future__ import annotations

import sys
import types

import pytest

from free.qualification.repair_adapter import (
    SupervisorRepairAdapter,
    build_supervisor_repair_adapter,
)


class FakeSupervisor:
    def __init__(self, station: int, *, fail=False):
        self.station = station
        self.fail = fail
        self.calls = []

    async def restart(self, reason: str = "") -> None:
        self.calls.append(reason)
        if self.fail:
            raise RuntimeError("restart failed")


@pytest.mark.asyncio
async def test_repair_restarts_only_the_affected_station():
    s1 = FakeSupervisor(1)
    s2 = FakeSupervisor(2)
    adapter = SupervisorRepairAdapter({1: s1, 2: s2})
    assert await adapter.repair(1, "tunnel_down") is True
    assert s1.calls == ["watchdog:tunnel_down"]
    assert s2.calls == []


@pytest.mark.asyncio
async def test_missing_station_is_false():
    adapter = SupervisorRepairAdapter({})
    assert await adapter.repair(3, "tunnel_down") is False


@pytest.mark.asyncio
async def test_restart_exception_is_false_not_crash():
    adapter = SupervisorRepairAdapter({1: FakeSupervisor(1, fail=True)})
    assert await adapter.repair(1, "dns_broken") is False


def test_build_from_shared_state(monkeypatch):
    module = types.SimpleNamespace(station_supervisors=[FakeSupervisor(1), FakeSupervisor(2)])
    monkeypatch.setitem(sys.modules, "shared_state", module)
    adapter = build_supervisor_repair_adapter()
    assert adapter is not None
    assert set(adapter.supervisors) == {1, 2}


def test_build_returns_none_without_supervisors(monkeypatch):
    module = types.SimpleNamespace(station_supervisors=[])
    monkeypatch.setitem(sys.modules, "shared_state", module)
    assert build_supervisor_repair_adapter() is None
