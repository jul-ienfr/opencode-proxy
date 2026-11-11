"""Cutover tests: make-before-break, rollback, measured result."""

from __future__ import annotations

import asyncio

import pytest

from free.qualification.cutover import CutoverService, CutoverTarget
from free.qualification.store import QualificationStore


class FakePorts:
    def __init__(self, *, prepare=True, switch=True, probe=True, rollback=True):
        self.prepare_ok = prepare
        self.switch_ok = switch
        self.probe_ok = probe
        self.rollback_ok = rollback
        self.calls = []

    async def prepare(self, target):
        self.calls.append("prepare")
        return self.prepare_ok

    async def switch(self, target):
        self.calls.append("switch")
        return self.switch_ok

    async def probe(self, target):
        self.calls.append("probe")
        return self.probe_ok

    async def rollback(self, target):
        self.calls.append("rollback")
        return self.rollback_ok


def _target() -> CutoverTarget:
    return CutoverTarget(
        station=1,
        from_option_key="old",
        from_egress_ip="1.1.1.1",
        to_option_key="new",
        to_egress_ip="2.2.2.2",
        to_transport="wireguard",
        standby=True,
    )


class SlowPrepare(FakePorts):
    async def prepare(self, target):
        self.calls.append("prepare")
        await asyncio.sleep(0.2)
        return True


@pytest.mark.asyncio
async def test_cutover_timeout_bounds_prepare(tmp_path):
    store = QualificationStore(tmp_path / "registry.db", clock=lambda: 100.0)
    ports = SlowPrepare()
    try:
        service = CutoverService(
            store,
            ports,
            timeout_s=0.01,
            clock=lambda: 100.0,
        )
        result = await service.execute(_target())
        assert result.status == "failed"
        assert result.reason == "prepare:TimeoutError"
        assert ports.calls == ["prepare"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_success_commits_without_rollback(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        ports = FakePorts()
        service = CutoverService(store, ports, clock=lambda: 100.0)
        result = await service.execute(_target())
        assert result.status == "committed"
        assert ports.calls == ["prepare", "switch", "probe"]
        row = store._conn.execute("SELECT * FROM station_cutovers").fetchone()
        assert row["result"] == "committed"
        assert row["from_egress_ip"] == "1.1.1.1"
        assert row["to_egress_ip"] == "2.2.2.2"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_prepare_failure_leaves_old_untouched(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        ports = FakePorts(prepare=False)
        service = CutoverService(store, ports, clock=lambda: 100.0)
        result = await service.execute(_target())
        assert result.status == "failed"
        assert ports.calls == ["prepare"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_probe_failure_rolls_back(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        ports = FakePorts(probe=False)
        service = CutoverService(store, ports, clock=lambda: 100.0)
        result = await service.execute(_target())
        assert result.status == "rolled-back"
        assert ports.calls == ["prepare", "switch", "probe", "rollback"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_switch_failure_rolls_back(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        ports = FakePorts(switch=False)
        service = CutoverService(store, ports, clock=lambda: 100.0)
        result = await service.execute(_target())
        assert result.status == "rolled-back"
        assert ports.calls == ["prepare", "switch", "rollback"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_exception_is_a_failed_cutover_not_a_crash(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        class Broken(FakePorts):
            async def probe(self, target):
                raise RuntimeError("boom")

        service = CutoverService(store, Broken(), clock=lambda: 100.0)
        result = await service.execute(_target())
        assert result.status == "rolled-back"
        assert "RuntimeError" in result.reason
    finally:
        store.close()
