"""Dedicated qualification slots controlled from the dashboard.

The six production stations must never be pre-empted by a live sweep.  This
manager reuses the Compose services already declared for stations 7-10
(``vpn-gluetun-7``..``vpn-gluetun-10``, profile ``multi-station``): it starts
them in WireGuard so the Gluetun control API can switch hostnames in seconds,
runs one live ``QualificationSweep`` per slot against the shared store, and
stops the containers as soon as the operator asks for fewer slots.

Everything is fail-soft: a Docker error is reported in the slot snapshot, it
never crashes the proxy.  No tunnel is opened at import time.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .station_adapter import build_registry_station_adapter, first_probe_model
from .sweep import QualificationSweep

_LOG = logging.getLogger(__name__)
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

KNOWN_TRANSPORTS = ("wireguard", "openvpn-udp", "openvpn-tcp")
MIN_STATION = 1
MAX_STATION = 10
MAX_SLOTS = MAX_STATION - MIN_STATION + 1
DEFAULT_STATION_BASE = 7
DEFAULT_MAX_COUNT = 4
DEFAULT_RECOMMENDED_MAX = 2
DEFAULT_MAX_HOSTS_PER_EPOCH = 40
DEFAULT_INTERVAL_S = 30.0


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


@dataclass(frozen=True)
class QualifierConfig:
    """Bounded operator request for the dedicated qualifiers."""

    enabled: bool = False
    count: int = 0
    max_count: int = DEFAULT_MAX_COUNT
    recommended_max: int = DEFAULT_RECOMMENDED_MAX
    station_base: int = DEFAULT_STATION_BASE
    transport: str = "wireguard"
    max_hosts_per_epoch: int = DEFAULT_MAX_HOSTS_PER_EPOCH
    interval_s: float = DEFAULT_INTERVAL_S
    max_attempts_per_host: int = 1
    transport_rounds: int = 1

    @classmethod
    def from_mapping(cls, raw: Any = None) -> QualifierConfig:
        data = raw if isinstance(raw, dict) else {}
        station_base = _as_int(data.get("station_base"), DEFAULT_STATION_BASE)
        station_base = max(MIN_STATION, min(station_base, MAX_STATION))
        available = MAX_STATION - station_base + 1
        max_count = _as_int(data.get("max_count"), DEFAULT_MAX_COUNT)
        max_count = max(1, min(max_count, available))
        recommended = _as_int(data.get("recommended_max"), DEFAULT_RECOMMENDED_MAX)
        recommended = max(0, min(recommended, max_count))
        count = _as_int(data.get("count"), 0)
        count = max(0, min(count, max_count))
        transport = str(data.get("transport") or "wireguard").strip().lower()
        if transport not in KNOWN_TRANSPORTS:
            transport = "wireguard"
        return cls(
            enabled=bool(data.get("enabled", False)),
            count=count,
            max_count=max_count,
            recommended_max=recommended,
            station_base=station_base,
            transport=transport,
            max_hosts_per_epoch=max(
                1, min(1000, _as_int(data.get("max_hosts_per_epoch"), DEFAULT_MAX_HOSTS_PER_EPOCH))
            ),
            interval_s=max(
                1.0, min(86400.0, _as_float(data.get("interval_s"), DEFAULT_INTERVAL_S))
            ),
            max_attempts_per_host=max(
                1, min(10, _as_int(data.get("max_attempts_per_host"), 1))
            ),
            transport_rounds=max(
                1, min(5, _as_int(data.get("transport_rounds"), 1))
            ),
        )

    @property
    def active_count(self) -> int:
        return self.count if self.enabled else 0

    @property
    def stations(self) -> tuple[int, ...]:
        return tuple(self.station_base + index for index in range(self.active_count))

    def as_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "count": self.count,
            "max_count": self.max_count,
            "recommended_max": self.recommended_max,
            "station_base": self.station_base,
            "transport": self.transport,
            "max_hosts_per_epoch": self.max_hosts_per_epoch,
            "interval_s": self.interval_s,
            "max_attempts_per_host": self.max_attempts_per_host,
            "transport_rounds": self.transport_rounds,
        }


def qualifier_config_from_settings() -> QualifierConfig:
    """Read ``egress_catalog.qualification.qualifiers`` from config.yaml."""

    try:
        from config.settings import yaml_get
    except Exception:  # pragma: no cover - config always importable in prod
        return QualifierConfig()
    try:
        raw = yaml_get("egress_catalog", "qualification.qualifiers", {}) or {}
    except Exception:
        raw = {}
    return QualifierConfig.from_mapping(raw)


def _snapshot(
    config: QualifierConfig,
    *,
    tasks: dict[int, Any] | None = None,
    errors: dict[int, str] | None = None,
    epochs: dict[int, dict[str, Any]] | None = None,
    managed: set[int] | None = None,
    started_at: float = 0.0,
    manager: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    tasks = tasks or {}
    errors = errors or {}
    epochs = epochs or {}
    managed = managed or set()
    slots: list[dict[str, Any]] = []
    for station in config.stations:
        task = tasks.get(station)
        running = task is not None and not bool(getattr(task, "done", lambda: True)())
        error = str(errors.get(station) or "")
        if error:
            state = "error"
        elif running:
            state = "running"
        elif station in managed:
            state = "starting"
        else:
            state = "stopped"
        epoch = dict(epochs.get(station) or {})
        slots.append(
            {
                "station": station,
                "state": state,
                "running": running,
                "last_error": error,
                "last_epoch": epoch.get("epoch_id", ""),
                "last_claimed": int(epoch.get("claimed", 0) or 0),
                "last_promoted": int(epoch.get("promoted", 0) or 0),
                "last_finished_at": epoch.get("finished_at"),
                "last_duration_s": epoch.get("duration_s"),
            }
        )
    warning = ""
    if config.enabled and config.count > config.recommended_max:
        warning = (
            f"{config.count} qualifieurs demandés : au-delà de "
            f"{config.recommended_max}, les sessions NordVPN supplémentaires "
            "peuvent provoquer des AUTH_FAILED sur les stations de production."
        )
    return {
        "enabled": config.enabled,
        "count": config.active_count,
        "desired_count": config.count,
        "max_count": config.max_count,
        "recommended_max": config.recommended_max,
        "station_base": config.station_base,
        "transport": config.transport,
        "max_hosts_per_epoch": config.max_hosts_per_epoch,
        "interval_s": config.interval_s,
        "max_attempts_per_host": config.max_attempts_per_host,
        "transport_rounds": config.transport_rounds,
        "manager": bool(manager),
        "started_at": float(started_at or 0.0),
        "now": float(time.time() if now is None else now),
        "warning": warning,
        "slots": slots,
    }


def qualifier_config_snapshot(raw: Any = None) -> dict[str, Any]:
    """Static snapshot used when the live manager is not attached."""

    config = QualifierConfig.from_mapping(raw)
    return _snapshot(config, manager=False)


def persist_qualifier_request(*, enabled: bool, count: int) -> dict[str, Any]:
    """Merge the operator request into config.yaml, preserving siblings."""

    from config.settings import load_yaml_config, yaml_get, yaml_set

    load_yaml_config()
    current = dict(yaml_get("egress_catalog", "qualification", {}) or {})
    merged = dict(current.get("qualifiers") or {})
    merged["enabled"] = bool(enabled)
    merged["count"] = int(count)
    config = QualifierConfig.from_mapping(merged)
    current["qualifiers"] = config.as_mapping()
    yaml_set("egress_catalog", "qualification", current)
    return _snapshot(config, manager=False)


def _run_docker(command: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=180,
        creationflags=_NO_WINDOW,
    )


class QualifierManager:
    """Lifecycle + sweep loops for the dedicated qualification slots."""

    def __init__(
        self,
        root: str | Path,
        *,
        config: QualifierConfig | None = None,
        config_getter: Callable[[], dict[str, Any]] | None = None,
        persist: Callable[[dict[str, Any]], Any] | None = None,
        runner: Callable[[list[str], str], subprocess.CompletedProcess] | None = None,
        epoch_runner: Callable[[int, QualifierConfig], Any] | None = None,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._root = Path(root)
        self._config = config or QualifierConfig()
        self._config_getter = config_getter
        self._persist = persist or persist_qualifier_request
        self._runner = runner or _run_docker
        self._epoch_runner = epoch_runner
        self._clock = clock
        self._log = log or (lambda message: _LOG.info("%s", message))
        self._tasks: dict[int, asyncio.Task] = {}
        self._errors: dict[int, str] = {}
        self._epochs: dict[int, dict[str, Any]] = {}
        self._managed: set[int] = set()
        self._runtimes: dict[int, Any] = {}
        self._lock = asyncio.Lock()
        self._stopping = False
        self._started_at = 0.0

    # ── public API ─────────────────────────────────────────────────────
    @property
    def config(self) -> QualifierConfig:
        return self._config

    def snapshot(self) -> dict[str, Any]:
        return _snapshot(
            self._config,
            tasks=self._tasks,
            errors=self._errors,
            epochs=self._epochs,
            managed=self._managed,
            started_at=self._started_at,
            manager=True,
            now=self._clock(),
        )

    async def start(self) -> dict[str, Any]:
        self._stopping = False
        self._started_at = self._clock()
        async with self._lock:
            await self._reconcile_locked(self._config)
            return self._snapshot_locked()

    async def apply(self, *, enabled: bool, count: int) -> dict[str, Any]:
        async with self._lock:
            merged = dict(self._config.as_mapping())
            merged["enabled"] = bool(enabled)
            merged["count"] = int(count)
            config = QualifierConfig.from_mapping(merged)
            await self._reconcile_locked(config)
            self._config = config
            if self._persist is not None:
                try:
                    await asyncio.to_thread(self._persist, config.as_mapping())
                except Exception as exc:
                    self._log(
                        "qualifiers: persistance impossible: "
                        f"{type(exc).__name__}: {exc}"
                    )
            return self._snapshot_locked()

    async def stop(self) -> None:
        self._stopping = True
        async with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except BaseException:
                    pass
            for station in sorted(self._managed):
                await self._docker_stop(station)
            self._managed.clear()
            for station in list(self._runtimes):
                self._drop_runtime(station)

    # ── reconciliation ─────────────────────────────────────────────────
    async def _reconcile_locked(self, config: QualifierConfig) -> None:
        desired = set(config.stations)
        for station in sorted(set(self._tasks) - desired):
            await self._stop_slot_locked(station)
        for station in sorted(desired - set(self._tasks)):
            self._errors.pop(station, None)
            if not await self._docker_up(station, config.transport):
                continue
            self._managed.add(station)
            self._tasks[station] = asyncio.create_task(
                self._slot_loop(station, config),
                name=f"qualifier-{station}",
            )

    async def _stop_slot_locked(self, station: int) -> None:
        task = self._tasks.pop(station, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        if station in self._managed:
            await self._docker_stop(station)
            self._managed.discard(station)
        self._drop_runtime(station)

    def _drop_runtime(self, station: int) -> None:
        runtime = self._runtimes.pop(station, None)
        if runtime is None:
            return
        try:
            runtime.close()
        except Exception as exc:
            self._log(
                f"qualifiers: close runtime {station}: "
                f"{type(exc).__name__}: {exc}"
            )

    def _snapshot_locked(self) -> dict[str, Any]:
        return _snapshot(
            self._config,
            tasks=self._tasks,
            errors=self._errors,
            epochs=self._epochs,
            managed=self._managed,
            started_at=self._started_at,
            manager=True,
            now=self._clock(),
        )

    # ── slot loop ──────────────────────────────────────────────────────
    async def _slot_loop(self, station: int, config: QualifierConfig) -> None:
        while not self._stopping:
            started = self._clock()
            try:
                if self._epoch_runner is not None:
                    result = await self._epoch_runner(station, config)
                else:
                    result = await self._default_epoch(station, config)
                if isinstance(result, dict):
                    epoch = dict(result)
                    epoch.setdefault("finished_at", self._clock())
                    epoch.setdefault("duration_s", round(self._clock() - started, 3))
                    self._epochs[station] = epoch
                self._errors.pop(station, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._errors[station] = f"{type(exc).__name__}: {exc}"
                self._log(
                    f"qualifiers: slot {station} error: {type(exc).__name__}: {exc}"
                )
                self._drop_runtime(station)
            try:
                await asyncio.sleep(max(1.0, float(config.interval_s)))
            except asyncio.CancelledError:
                raise

    async def _default_epoch(
        self, station: int, config: QualifierConfig
    ) -> dict[str, Any]:
        runtime = self._runtime_for(station)
        base = runtime.sweep
        sweep_config = replace(
            base.config,
            station=int(station),
            max_hosts=max(1, int(config.max_hosts_per_epoch)),
            max_attempts_per_host=max(1, int(config.max_attempts_per_host)),
            transport_rounds=max(1, int(config.transport_rounds)),
            transport_order=(config.transport,),
            strategy="auto",
            manual_transport="",
            shadow=False,
            run_id=f"qualifier-{int(station)}",
        )
        sweep = QualificationSweep(
            runtime.store,
            runtime.sweep.adapter,
            config=sweep_config,
            clock=getattr(base, "_clock", time.time),
        )
        closed = runtime.store.close_running_epochs(
            f"qualifier-{int(station)}-",
            status="partial",
            now=self._clock(),
        )
        if closed:
            self._log(
                f"qualifiers: station {station}: {closed} époque(s) "
                "orpheline(s) fermée(s)"
            )
        epoch_id = f"qualifier-{int(station)}-{int(self._clock())}"
        result = await sweep.run_epoch(
            epoch_id=epoch_id,
            max_hosts=sweep_config.max_hosts,
            shadow=False,
        )
        return result.snapshot()

    def _runtime_for(self, station: int):
        """Build once per slot: one store connection, one adapter.

        Rebuilding the whole runtime at every epoch would reopen SQLite (and
        re-run schema/retention) every 30 s per slot; the store and the
        control binder are long-lived by design.
        """

        cached = self._runtimes.get(int(station))
        if cached is not None:
            return cached
        from .runtime import build_qualification

        app_config = self._config_getter() if self._config_getter is not None else {}
        if not isinstance(app_config, dict):
            app_config = {}
        adapter = build_registry_station_adapter(
            app_config,
            root=self._root,
            station=int(station),
            probe_models=(first_probe_model(app_config),),
            probe_timeout_s=8.0,
            control_settle_s=1.5,
            control_egress_wait_s=20.0,
            control_egress_poll_s=1.0,
            force_control=True,
        )
        runtime = build_qualification(app_config, adapter=adapter)
        if runtime is None:
            raise RuntimeError("qualification désactivée dans config.yaml")
        self._runtimes[int(station)] = runtime
        return runtime

    # ── Docker lifecycle ───────────────────────────────────────────────
    @staticmethod
    def service_name(station: int) -> str:
        return "vpn-gluetun" if int(station) == 1 else f"vpn-gluetun-{int(station)}"

    def override_path(self, station: int) -> Path:
        return self._root / "logs" / f"compose-qualifier-{int(station)}.yml"

    def _write_override(self, station: int, transport: str) -> Path | None:
        from .station_adapter import transport_parts

        vpn_type, protocol = transport_parts(transport)
        path = self.override_path(station)
        lines = [
            "# Généré par free/qualification/qualifiers.py — ne pas éditer.",
            "services:",
            f"  {self.service_name(station)}:",
            "    environment:",
            f"      - VPN_TYPE={vpn_type}",
        ]
        if protocol:
            lines.append(f"      - OPENVPN_PROTOCOL={protocol}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:
            self._errors[int(station)] = (
                f"override-write:{type(exc).__name__}: {exc}"
            )
            return None
        return path

    async def _docker_up(self, station: int, transport: str) -> bool:
        override = self._write_override(station, transport)
        if override is None:
            return False
        command = [
            "docker",
            "compose",
            "--profile",
            "multi-station",
            "-f",
            "docker-compose.yml",
            "-f",
            str(override),
            "up",
            "-d",
            "--no-deps",
            self.service_name(station),
        ]
        try:
            proc = await asyncio.to_thread(
                self._runner, command, str(self._root)
            )
        except Exception as exc:
            self._errors[int(station)] = f"docker-up:{type(exc).__name__}: {exc}"
            return False
        if int(getattr(proc, "returncode", 1)) != 0:
            detail = (
                getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or ""
            ).strip()
            self._errors[int(station)] = f"docker-up:{detail[:200] or 'unknown'}"
            return False
        return True

    async def _docker_stop(self, station: int) -> bool:
        command = ["docker", "stop", f"opencode-vpn-{int(station)}"]
        try:
            proc = await asyncio.to_thread(
                self._runner, command, str(self._root)
            )
        except Exception as exc:
            self._errors[int(station)] = f"docker-stop:{type(exc).__name__}: {exc}"
            return False
        if int(getattr(proc, "returncode", 1)) != 0:
            detail = (
                getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or ""
            ).strip()
            if "no such container" not in detail.lower():
                self._errors[int(station)] = f"docker-stop:{detail[:200] or 'unknown'}"
                return False
        return True


def build_qualifier_manager(
    root: str | Path,
    *,
    config: QualifierConfig | None = None,
    config_getter: Callable[[], dict[str, Any]] | None = None,
) -> QualifierManager:
    return QualifierManager(
        root,
        config=config or qualifier_config_from_settings(),
        config_getter=config_getter,
    )


__all__ = [
    "QualifierConfig",
    "QualifierManager",
    "build_qualifier_manager",
    "persist_qualifier_request",
    "qualifier_config_from_settings",
    "qualifier_config_snapshot",
]
