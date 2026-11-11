"""Periodic qualification scheduler.

The scheduler owns the loop, never the request path.  Each cycle runs
discovery when due, the watchdog, then one sweep epoch.  Failures are contained
per component so a broken feed or a single station cannot stop the loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class QualificationSchedulerConfig:
    interval_s: float = 900.0
    max_hosts: int = 0
    shadow: bool = True
    run_discovery: bool = True
    run_watchdog: bool = True
    run_preflight: bool = True
    preflight_limit: int = 500
    preflight_transport: str = "wireguard"
    watchdog_interval_s: float = 30.0
    global_pause_on_429: bool = False
    global_pause_s: float = 60.0
    service_priority: bool = True


class QualificationScheduler:
    def __init__(
        self,
        sweep: Any = None,
        *,
        sweeps: list[Any] | None = None,
        discovery: Any = None,
        watchdog: Any = None,
        preflight: Any = None,
        config: QualificationSchedulerConfig | None = None,
        clock: Callable[[], float] = time.time,
        on_cycle: Callable[[dict[str, Any]], Any] | None = None,
        station_state_getter: Callable[[int], str] | None = None,
    ) -> None:
        self.sweeps = list(sweeps or ())
        if not self.sweeps and sweep is not None:
            self.sweeps = [sweep]
        self.sweep = self.sweeps[0] if self.sweeps else sweep
        self.discovery = discovery
        self.watchdog = watchdog
        self.preflight = preflight
        self.config = config or QualificationSchedulerConfig()
        self._clock = clock
        self._on_cycle = on_cycle
        self._station_state_getter = station_state_getter
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_watchdog_at: float | None = None
        self._paused_until: float = 0.0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run_once(self, *, now: float | None = None) -> dict[str, Any]:
        ts = float(self._clock() if now is None else now)
        report: dict[str, Any] = {"started_at": ts, "errors": []}
        if ts < self._paused_until:
            report["paused"] = True
            report["paused_until"] = self._paused_until
            report["finished_at"] = ts
            return report
        if (
            self.config.run_discovery
            and self.discovery is not None
            and self._discovery_due(ts)
        ):
            try:
                report["discovery"] = await asyncio.to_thread(
                    self.discovery.run,
                    now=ts,
                )
            except Exception as exc:  # fail-soft: keep sweeping
                report["errors"].append(f"discovery:{type(exc).__name__}")
                _LOG.warning(
                    "qualification.scheduler.discovery_error",
                    extra={"error": f"{type(exc).__name__}: {exc}"},
                )
        if self.config.run_watchdog and self.watchdog is not None:
            due = (
                self._last_watchdog_at is None
                or ts - self._last_watchdog_at
                >= max(1.0, float(self.config.watchdog_interval_s))
            )
            if due:
                try:
                    report["watchdog"] = await self.watchdog.run_once(now=ts)
                    self._last_watchdog_at = ts
                except Exception as exc:
                    report["errors"].append(f"watchdog:{type(exc).__name__}")
                    _LOG.warning(
                        "qualification.scheduler.watchdog_error",
                        extra={"error": f"{type(exc).__name__}: {exc}"},
                    )
        if self.config.run_preflight and self.preflight is not None:
            try:
                report["preflight"] = await self.preflight.run(
                    limit=max(1, int(self.config.preflight_limit)),
                    transport=self.config.preflight_transport,
                    now=ts,
                )
            except Exception as exc:
                report["errors"].append(f"preflight:{type(exc).__name__}")
                _LOG.warning(
                    "qualification.scheduler.preflight_error",
                    extra={"error": f"{type(exc).__name__}: {exc}"},
                )
        try:
            if self.sweeps:
                launched = []
                skipped = []
                for item in self.sweeps:
                    reason = self._service_skip(item)
                    if reason:
                        station = int(
                            getattr(
                                getattr(item, "config", None),
                                "station",
                                0,
                            )
                            or 0
                        )
                        skipped.append(
                            {
                                "station": station,
                                "skipped": True,
                                "reason": reason,
                            }
                        )
                        continue
                    launched.append(item)
                results = []
                if launched:
                    results = await asyncio.gather(
                        *(
                            item.run_epoch(
                                max_hosts=max(0, int(self.config.max_hosts)),
                                shadow=bool(self.config.shadow),
                            )
                            for item in launched
                        ),
                        return_exceptions=True,
                    )
                report["sweeps"] = skipped + list(results)
                if skipped:
                    report["skipped_sweeps"] = skipped
                if len(launched) == 1:
                    report["sweep"] = results[0]
                for result in results:
                    if isinstance(result, BaseException):
                        report["errors"].append(
                            f"sweep:{type(result).__name__}"
                        )
                if self.config.global_pause_on_429:
                    quota = sum(
                        int(getattr(result, "quota", 0) or 0)
                        for result in results
                        if not isinstance(result, BaseException)
                    )
                    if quota > 0:
                        self._paused_until = ts + max(
                            1.0,
                            float(self.config.global_pause_s),
                        )
                        report["paused_until"] = self._paused_until
        except Exception as exc:
            report["errors"].append(f"sweep:{type(exc).__name__}")
            _LOG.warning(
                "qualification.scheduler.sweep_error",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )
        report["finished_at"] = self._clock()
        if self._on_cycle is not None:
            try:
                self._on_cycle(report)
            except Exception:
                pass
        _LOG.info(
            "qualification.scheduler.cycle",
            extra={
                "errors": len(report["errors"]),
                "shadow": bool(self.config.shadow),
                "max_hosts": int(self.config.max_hosts),
            },
        )
        return report

    def _service_skip(self, sweep: Any) -> str:
        """A serving/repairing station is never preempted by a live sweep."""

        if not self.config.service_priority or self.config.shadow:
            return ""
        getter = self._station_state_getter
        if getter is None:
            return ""
        station = int(
            getattr(getattr(sweep, "config", None), "station", 0) or 0
        )
        if station <= 0:
            return ""
        try:
            state = str(getter(station) or "").strip().lower()
        except Exception:
            return ""
        if state in ("serving", "rotating", "repairing", "paused"):
            return f"service-priority:{state}"
        return ""

    def _discovery_due(self, now: float) -> bool:
        discovery = self.discovery
        if discovery is None:
            return False
        try:
            return bool(discovery.is_due(now=now))
        except Exception:
            return False

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run_forever(),
            name="qualification-scheduler",
        )

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except BaseException:
            pass

    async def _run_forever(self) -> None:
        while not self._stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(1.0, float(self.config.interval_s)),
                )
            except TimeoutError:
                continue
            break


__all__ = ["QualificationScheduler", "QualificationSchedulerConfig"]
