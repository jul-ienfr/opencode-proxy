"""Apply a rotation decision through the production cutover path.

The router keeps the dry-run planner for previews; ``apply_rotation`` builds
the real registry adapter and executes the same decision.  It is only called
when the qualification module is out of shadow mode, so a dry-run deployment
can never rebind a live station.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .cutover_adapter import AdapterCutoverPorts
from .rotation import RotationPlanner, StationSnapshot
from .rotation_executor import RotationExecutor
from .runtime import build_cutover_service
from .station_adapter import build_registry_station_adapter
from .store import QualificationStore


def station_snapshot(
    store: QualificationStore,
    station: int,
    body: dict[str, Any],
    *,
    standby_size: int = 1,
) -> StationSnapshot:
    """Build the planner snapshot from persisted state + request overrides."""

    state = store.station_state(station) or {}
    runtime = store.station_runtime(station) or {}
    return StationSnapshot(
        station=int(station),
        state=str(runtime.get("state") or state.get("state") or "serving"),
        current_ip=str(
            state.get("current_ip") or runtime.get("current_egress_ip") or ""
        ),
        current_hostname=str(
            state.get("current_host")
            or runtime.get("current_option_key")
            or ""
        ),
        quota_remaining=(
            int(body["quota_remaining"])
            if body.get("quota_remaining") is not None
            else None
        ),
        quota_estimate=int(body.get("quota_estimate") or 500),
        standby_ip=str(runtime.get("standby_egress_ip") or ""),
        standby_hostname=str(runtime.get("standby_option_key") or ""),
        standby_size=(
            int(body["standby_size"])
            if body.get("standby_size") is not None
            else int(standby_size)
        ),
    )


def _qualification_cfg(config: dict[str, Any]) -> dict[str, Any]:
    catalog = config.get("egress_catalog")
    if not isinstance(catalog, dict):
        return {}
    qualification = catalog.get("qualification")
    return qualification if isinstance(qualification, dict) else {}


async def apply_rotation(
    *,
    root: str | Path,
    db_path: str | Path,
    config: dict[str, Any],
    station: int,
    body: dict[str, Any],
    usable_stations: int | None = None,
) -> dict[str, Any]:
    """Execute one planner decision on the real station."""

    cfg = _qualification_cfg(config)
    store = QualificationStore(db_path)
    try:
        snapshot = station_snapshot(
            store,
            int(station),
            body,
            standby_size=int(cfg.get("station_standby_size", 1) or 0),
        )
        usable = (
            int(usable_stations)
            if usable_stations is not None
            else sum(
                1
                for row in store.list_station_states()
                if row.get("client_usable")
            )
        )
        adapter = build_registry_station_adapter(
            config,
            root=root,
            station=int(station),
        )
        ports = AdapterCutoverPorts(
            adapter,
            station=int(station),
        )
        cutover = build_cutover_service(store, ports, config)
        planner = RotationPlanner(
            store,
            headroom_threshold_pct=float(
                cfg.get("proactive_rotation_headroom_pct", 20) or 20
            ),
            last_station_protection=bool(
                cfg.get("last_station_protection", True)
            ),
        )
        executor = RotationExecutor(store, planner, cutover)
        result = await executor.execute(
            snapshot,
            usable_stations=max(1, usable),
        )
        return {
            "ok": True,
            "dry_run": False,
            "station": int(station),
            "usable_stations": usable,
            "decision": result.decision.snapshot(),
            "cutover": (
                result.cutover.snapshot()
                if result.cutover is not None
                else None
            ),
        }
    finally:
        store.close()


__all__ = ["apply_rotation", "station_snapshot"]
