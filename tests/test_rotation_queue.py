"""File de rotation observable — feature 019, tâche T316."""

from __future__ import annotations

import pytest

import free.warp_rotation as wr
from free.warp_rotation import (
    rotation_queue_snapshot,
    _queue_enqueue,
    _queue_finish,
    _queue_start,
)


@pytest.fixture(autouse=True)
def _clean_queue():
    wr._ROTATION_QUEUE.clear()
    yield
    wr._ROTATION_QUEUE.clear()


def test_four_queued_rotations_expose_positions_one_to_four():
    for index in range(1, 5):
        _queue_enqueue(f"job-{index}", f"warp-{index}", f"container-{index}", now=100.0 + index)
    snapshot = rotation_queue_snapshot(now=110.0, eta_default_s=120.0)
    assert [entry["position"] for entry in snapshot] == [1, 2, 3, 4]
    assert [entry["job_id"] for entry in snapshot] == [
        "job-1",
        "job-2",
        "job-3",
        "job-4",
    ]
    assert [entry["eta_s"] for entry in snapshot] == [120.0, 240.0, 360.0, 480.0]


def test_started_rotation_leaves_the_queue():
    for index in range(1, 4):
        _queue_enqueue(f"job-{index}", f"warp-{index}", f"container-{index}", now=100.0 + index)
    _queue_start("job-1", now=200.0)
    snapshot = rotation_queue_snapshot(now=210.0, eta_default_s=60.0)
    assert [entry["job_id"] for entry in snapshot] == ["job-2", "job-3"]
    assert [entry["position"] for entry in snapshot] == [1, 2]


def test_finished_rotation_is_removed():
    _queue_enqueue("job-x", "warp-x", "container-x", now=100.0)
    _queue_finish("job-x")
    assert rotation_queue_snapshot(now=110.0) == []


def test_request_rotation_returns_job_id_when_asked(monkeypatch):
    class _Endpoint:
        label = "warp-test"
        container = "warp-test"
        managed = True
        ipv6 = True

    monkeypatch.setattr(wr, "INFLIGHT", set())
    monkeypatch.setattr(wr, "LAST_ROTATION", {})
    monkeypatch.setattr(wr, "MIN_ROTATION_INTERVAL_S", 300.0)
    rotator = wr.WarpRotator()
    monkeypatch.setattr(rotator, "_run", lambda *args: None)
    token = rotator.request_rotation(
        _Endpoint(), object(), lambda *args: None, return_job_id=True
    )
    assert token
    snapshot = rotator.queue_snapshot()
    assert snapshot and snapshot[0]["job_id"] == str(token)
    assert snapshot[0]["position"] == 1
