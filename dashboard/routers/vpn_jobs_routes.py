"""Fleet job routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_vpn_jobs_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    fleet_getter: Callable[[], Any],
    jobs_getter: Callable[[], Any] | None = None,
) -> None:
    def _jobs() -> Any:
        if jobs_getter is not None:
            return jobs_getter()
        from ops.jobs import get_jobs

        return get_jobs()

    @app.post("/api/vpn/jobs/fleet/{action}")
    async def fleet_job(action: str, request: Request):
        err = token_checker(request)
        if err:
            return err
        fleet = fleet_getter()
        actions = {
            "connect": fleet.connect_all,
            "disconnect": lambda: fleet.disconnect_all(stop_containers=False),
            "heal": fleet.heal,
        }
        function = actions.get(str(action))
        if function is None:
            return JSONResponse(
                status_code=400,
                content={"error": f"action inconnue: {action}"},
            )
        job = _jobs().create(f"fleet:{action}", function())
        return JSONResponse(status_code=202, content=job.snapshot())

    @app.get("/api/vpn/jobs/{job_id}")
    async def get_job(job_id: str):
        job = _jobs().get(job_id)
        if job is None:
            return JSONResponse(
                status_code=404,
                content={"error": "job introuvable"},
            )
        return job.snapshot()

    @app.get("/api/vpn/jobs")
    async def list_jobs():
        return _jobs().status()


__all__ = ["register_vpn_jobs_routes"]
