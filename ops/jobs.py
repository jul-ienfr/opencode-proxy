"""Jobs asynchrones courts pour les opérations de flotte.

Les endpoints historiques `connect`/`next` attendent la fin de l'opération
(parfois plusieurs minutes). Ce registre permet aux nouveaux endpoints de
rendre un `202 + job_id` immédiatement, avec un statut consultable et une
progression SSE côté dashboard.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Job:
    id: str
    name: str
    created_at: float
    status: str = "running"  # running | done | failed | cancelled
    result: Any = None
    error: str | None = None
    finished_at: float | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }


class JobRegistry:
    def __init__(self, max_jobs: int = 64) -> None:
        self._jobs: dict[str, Job] = {}
        self._max = max(1, int(max_jobs))

    def create(self, name: str, coro) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], name=str(name), created_at=time.time())
        self._jobs[job.id] = job
        # Évince les plus anciens terminés, jamais un job en cours.
        if len(self._jobs) > self._max:
            for jid, j in list(self._jobs.items()):
                if j.status != "running":
                    self._jobs.pop(jid, None)
                if len(self._jobs) <= self._max:
                    break
        job.task = asyncio.create_task(self._run(job, coro))
        return job

    async def _run(self, job: Job, coro) -> None:
        try:
            job.result = await coro
            job.status = "done"
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        except Exception as e:
            job.status = "failed"
            job.error = f"{type(e).__name__}: {e}"
        finally:
            job.finished_at = time.time()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(str(job_id))

    def status(self) -> dict:
        return {
            "jobs": [j.snapshot() for j in list(self._jobs.values())[-20:]],
            "running": sum(1 for j in self._jobs.values() if j.status == "running"),
        }


_JOBS: JobRegistry | None = None


def get_jobs() -> JobRegistry:
    global _JOBS
    if _JOBS is None:
        _JOBS = JobRegistry()
    return _JOBS


__all__ = ["Job", "JobRegistry", "get_jobs"]
