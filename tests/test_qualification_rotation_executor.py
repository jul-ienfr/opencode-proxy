"""Rotation executor tests: plan -> cutover -> runtime state."""

from __future__ import annotations

import pytest

from free.qualification.cutover import CutoverResult
from free.qualification.domain import Candidate
from free.qualification.rotation import RotationPlanner, StationSnapshot
from free.qualification.rotation_executor import RotationExecutor
from free.qualification.store import QualificationStore


class FakeCutover:
    def __init__(self, status="committed"):
        self.status = status
        self.targets = []

    async def execute(self, target):
        self.targets.append(target)
        return CutoverResult(
            station=target.station,
            status=self.status,
            duration_ms=12.0,
            reason="" if self.status == "committed" else "probe-failed",
        )


def _candidate(hostname: str, **changes) -> Candidate:
    base = {
        "hostname": hostname,
        "transports": ("wireguard", "openvpn-udp"),
        "potential_score": 80.0,
        "last_egress_ip": "9.9.9.9",
        "first_seen_at": 100.0,
        "last_seen_at": 100.0,
        "updated_at": 100.0,
    }
    base.update(changes)
    return Candidate(**base)


@pytest.mark.asyncio
async def test_keep_does_not_call_cutover(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        cutover = FakeCutover()
        executor = RotationExecutor(store, RotationPlanner(store), cutover, clock=lambda: 100.0)
        result = await executor.execute(
            StationSnapshot(1, current_ip="1.1.1.1", quota_remaining=400)
        )
        assert result.decision.action == "keep"
        assert cutover.targets == []
        assert result.cutover is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_promote_standby_commits_and_updates_runtime(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        cutover = FakeCutover("committed")
        executor = RotationExecutor(store, RotationPlanner(store), cutover, clock=lambda: 100.0)
        result = await executor.execute(
            StationSnapshot(
                1,
                current_ip="1.1.1.1",
                quota_remaining=20,
                standby_ip="2.2.2.2",
                standby_hostname="standby.nordvpn.com",
            )
        )
        assert result.decision.action == "promote_standby"
        assert cutover.targets[0].to_egress_ip == "2.2.2.2"
        runtime = store.station_runtime(1)
        assert runtime["state"] == "serving"
        assert runtime["current_egress_ip"] == "2.2.2.2"
        assert runtime["cutover_ms"] == 12.0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_rotate_uses_selected_candidate(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_candidates([_candidate("next.nordvpn.com")])
        cutover = FakeCutover("committed")
        executor = RotationExecutor(store, RotationPlanner(store), cutover, clock=lambda: 100.0)
        result = await executor.execute(
            StationSnapshot(1, current_ip="1.1.1.1", quota_remaining=10),
            usable_stations=6,
        )
        assert result.decision.action == "rotate"
        assert cutover.targets[0].to_option_key == "next.nordvpn.com"
        assert cutover.targets[0].to_egress_ip == "9.9.9.9"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_rollback_keeps_old_current(tmp_path):
    store = QualificationStore(tmp_path / "registry.db")
    try:
        store.upsert_station_runtime(
            1,
            state="serving",
            current_egress_ip="1.1.1.1",
            now=100.0,
        )
        cutover = FakeCutover("rolled-back")
        executor = RotationExecutor(store, RotationPlanner(store), cutover, clock=lambda: 100.0)
        result = await executor.execute(
            StationSnapshot(
                1,
                current_ip="1.1.1.1",
                quota_remaining=20,
                standby_ip="2.2.2.2",
            )
        )
        assert result.cutover is not None
        assert result.cutover.status == "rolled-back"
        runtime = store.station_runtime(1)
        assert runtime["current_egress_ip"] == "1.1.1.1"
        assert runtime["state"] == "idle"
    finally:
        store.close()
