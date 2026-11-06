"""RepairOrchestrator tests (no Docker, no network)."""

from __future__ import annotations

import asyncio

from rotation.application.repair import (
    RepairOrchestrator,
    RepairRequest,
)


async def test_one_job_per_station_and_bounded_parallelism():
    state = {"active": 0, "max": 0, "calls": []}

    async def runner(request):
        state["calls"].append(request.station)
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        try:
            await asyncio.sleep(0.02)
            return True
        finally:
            state["active"] -= 1

    orchestrator = RepairOrchestrator(runner, max_parallel=2)
    requests = [
        RepairRequest(1, "quota-429"),
        RepairRequest(2, "quota-429"),
        RepairRequest(3, "no-egress"),
        RepairRequest(4, "no-egress"),
        RepairRequest(5, "boot"),
        RepairRequest(1, "duplicate"),
    ]
    results = await orchestrator.repair(requests)

    assert len(results) == 5
    assert sorted(state["calls"]) == [1, 2, 3, 4, 5]
    assert state["max"] <= 2
    assert all(result.ok for result in results)


async def test_runner_failure_is_reported_per_station():
    async def runner(request):
        if request.station == 2:
            raise RuntimeError("bind failed")
        return False

    orchestrator = RepairOrchestrator(runner, max_parallel=3)
    results = await orchestrator.repair(
        [RepairRequest(1), RepairRequest(2), RepairRequest(3)]
    )

    by_station = {result.station: result for result in results}
    assert by_station[1].ok is False
    assert by_station[1].error == ""
    assert by_station[2].ok is False
    assert "RuntimeError" in by_station[2].error
    assert by_station[3].ok is False


def test_boot_reconciliation_closes_jobs_for_missing_stations():
    keep, close = RepairOrchestrator.reconcile(
        [{"station": 1}, {"station": 4}, {"station": 9}],
        live_stations=[1, 2, 3, 4],
    )
    assert keep == [1, 4]
    assert close == [9]
