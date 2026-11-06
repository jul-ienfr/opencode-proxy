"""Per-station repair queue: pool -> supervisor hand-off."""

from __future__ import annotations

from free.repair_queue import RepairQueue


def test_request_is_idempotent_per_station(tmp_path):
    q = RepairQueue(tmp_path / "repair.db", ttl_s=600)
    q.request(2, "quota-429", model="m", ip="1.2.3.4", now=100.0)
    q.request(2, "quota-429", model="m2", ip="1.2.3.5", now=101.0)
    pending = q.pending(now=102.0)
    assert len(pending) == 1
    assert pending[0]["station"] == 2
    assert pending[0]["model"] == "m2"
    assert pending[0]["ip"] == "1.2.3.5"


def test_complete_removes_the_request(tmp_path):
    q = RepairQueue(tmp_path / "repair.db")
    q.request(3, "quota-429", now=100.0)
    assert q.pending(now=101.0)
    q.complete(3)
    assert q.pending(now=101.0) == []


def test_expired_request_is_not_returned_and_can_be_pruned(tmp_path):
    q = RepairQueue(tmp_path / "repair.db", ttl_s=60)
    q.request(4, "quota-429", now=100.0)
    assert q.pending(now=161.0) == []
    assert q.prune(now=161.0) == 1
