"""Qualification read-only routes.

Extracted from ``dashboard/api.py`` (slice S10).  The routes only read the
qualification projections; they never write the queue and never open a tunnel.
"""

from __future__ import annotations

import asyncio
import csv
import io
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


def register_qualification_routes(
    app: FastAPI,
    db_path_getter: Callable[[], str],
    *,
    token_checker: Callable[[Request], object] | None = None,
    rotation_headroom_pct: float = 20.0,
    last_station_protection: bool = True,
    station_standby_size: int = 1,
    start_epoch: Callable[..., dict] | None = None,
    rotate_apply: Callable[..., Awaitable[dict]] | None = None,
    default_shadow: bool = True,
    allow_live: bool = False,
    qualifier_manager_getter: Callable[[], object] | None = None,
) -> None:
    active_launch: dict = {"process": None, "payload": None}

    def _launch_busy() -> bool:
        process = active_launch.get("process")
        return process is not None and process.poll() is None

    def _qualifier_manager():
        if qualifier_manager_getter is None:
            return None
        try:
            return qualifier_manager_getter()
        except Exception:
            return None

    def _qualifier_snapshot() -> dict:
        manager = _qualifier_manager()
        if manager is not None:
            try:
                return manager.snapshot()
            except Exception:
                pass
        from free.qualification.qualifiers import qualifier_config_snapshot

        return qualifier_config_snapshot()

    @app.get("/api/egress/qualification/qualifiers")
    async def get_qualifiers():
        return _qualifier_snapshot()

    @app.post("/api/egress/qualification/qualifiers")
    async def set_qualifiers(request: Request):
        if token_checker is not None:
            err = token_checker(request)
            if err:
                return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "corps JSON objet attendu"},
            )
        if "enabled" not in body and "count" not in body:
            return JSONResponse(
                status_code=400,
                content={"error": "enabled ou count requis"},
            )
        enabled = bool(body.get("enabled", False))
        try:
            count = int(body.get("count", 0))
        except (TypeError, ValueError):
            return JSONResponse(
                status_code=400,
                content={"error": "count doit être un entier"},
            )
        if count < 0:
            return JSONResponse(
                status_code=400,
                content={"error": "count doit être positif ou nul"},
            )
        manager = _qualifier_manager()
        if manager is None:
            from free.qualification.qualifiers import persist_qualifier_request

            try:
                snapshot = await asyncio.to_thread(
                    persist_qualifier_request,
                    enabled=enabled,
                    count=count,
                )
            except Exception as exc:
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": (
                            "persistance des qualifieurs impossible : "
                            f"{type(exc).__name__}: {exc}"
                        )
                    },
                )
            return {
                "ok": True,
                "restart_required": True,
                **snapshot,
            }
        try:
            snapshot = await manager.apply(enabled=enabled, count=count)
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content={
                    "error": (
                        "application des qualifieurs impossible : "
                        f"{type(exc).__name__}: {exc}"
                    )
                },
            )
        return {
            "ok": True,
            "restart_required": False,
            **snapshot,
        }

    @app.get("/api/egress/qualification/summary")
    async def get_qualification_summary():
        from free.qualification import readmodel

        return readmodel.summary(db_path_getter())

    @app.get("/api/egress/qualification/queue")
    async def get_qualification_queue(
        status: str = "",
        country: str = "",
        transport: str = "",
        limit: int = 50,
        offset: int = 0,
    ):
        from free.qualification import readmodel

        return readmodel.queue(
            db_path_getter(),
            status=status,
            country=country,
            transport=transport,
            limit=min(max(1, int(limit)), 500),
            offset=max(0, int(offset)),
        )

    @app.get("/api/egress/qualification/next")
    async def get_qualification_next(limit: int = 20):
        from free.qualification import readmodel

        return readmodel.next_candidates(
            db_path_getter(),
            limit=min(max(1, int(limit)), 500),
        )

    @app.get("/api/egress/qualification/epochs")
    async def get_qualification_epochs(limit: int = 20):
        from free.qualification import readmodel

        return readmodel.epochs(
            db_path_getter(),
            limit=min(max(1, int(limit)), 200),
        )

    @app.get("/api/egress/qualification/epochs/{epoch_id}")
    async def get_qualification_epoch(epoch_id: str):
        from free.qualification import readmodel

        payload = readmodel.epoch(db_path_getter(), epoch_id)
        if payload is None:
            return JSONResponse(
                status_code=404,
                content={"error": "epoch inconnue", "epoch_id": epoch_id},
            )
        return payload

    @app.post("/api/egress/qualification/epochs")
    async def start_qualification_epoch(request: Request):
        if token_checker is not None:
            err = token_checker(request)
            if err:
                return err
        if start_epoch is None:
            return JSONResponse(
                status_code=503,
                content={"error": "lancement de qualification non configuré"},
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "corps JSON objet attendu"},
            )
        if _launch_busy():
            return JSONResponse(
                status_code=409,
                content={
                    "error": "une époque est déjà en cours",
                    "launch": active_launch.get("payload"),
                },
            )
        try:
            station = int(body.get("station", 1))
        except (TypeError, ValueError):
            station = 0
        if not (1 <= station <= 10):
            return JSONResponse(
                status_code=400,
                content={"error": "station doit être un entier entre 1 et 10"},
            )
        try:
            max_hosts = max(0, min(10000, int(body.get("max_hosts", 100))))
        except (TypeError, ValueError):
            max_hosts = 100
        mode = str(body.get("mode") or "auto").strip().lower()
        if mode not in ("auto", "shadow", "live"):
            return JSONResponse(
                status_code=400,
                content={"error": "mode doit être auto, shadow ou live"},
            )
        if mode == "live" and not allow_live:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "mode live refusé : shadow_mode est encore actif",
                },
            )
        shadow = default_shadow if mode == "auto" else mode == "shadow"
        try:
            launch = start_epoch(
                station=station,
                max_hosts=max_hosts,
                shadow=shadow,
            )
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content={"error": f"lancement impossible : {exc}"},
            )
        active_launch["process"] = launch.get("process")
        payload = {key: value for key, value in launch.items() if key != "process"}
        active_launch["payload"] = payload
        return JSONResponse(status_code=202, content=payload)

    @app.get("/api/egress/qualification/attempts")
    async def get_qualification_attempts(
        hostname: str = "",
        status: str = "",
        epoch_id: str = "",
        limit: int = 50,
        format: str = "",
    ):
        from free.qualification import readmodel

        payload = readmodel.attempts(
            db_path_getter(),
            hostname=hostname,
            status=status,
            epoch_id=epoch_id,
            limit=min(max(1, int(limit)), 500),
        )
        if str(format).lower() == "csv":
            fields = (
                "attempt_id",
                "epoch_id",
                "station",
                "hostname",
                "transport",
                "round",
                "status",
                "egress_ip",
                "rtt_ms",
                "dns_ms",
                "connect_ms",
                "handshake_ms",
                "endpoint_ip",
                "reset_at",
                "error",
                "started_at",
                "finished_at",
            )
            buffer = io.StringIO()
            writer = csv.DictWriter(
                buffer,
                fieldnames=fields,
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(payload["items"])
            return Response(
                content=buffer.getvalue(),
                media_type="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": (
                        "attachment; filename=qualification-attempts.csv"
                    )
                },
            )
        return payload

    @app.get("/api/egress/qualification/transports")
    async def get_qualification_transports():
        from free.qualification import readmodel

        return readmodel.transports(db_path_getter())

    @app.get("/api/egress/qualification/coverage")
    async def get_qualification_coverage():
        from free.qualification import readmodel

        return readmodel.coverage(db_path_getter())

    @app.get("/api/egress/qualification/quota")
    async def get_qualification_quota(limit: int = 200):
        from free.qualification import readmodel

        return readmodel.quota(
            db_path_getter(),
            limit=min(max(1, int(limit)), 1000),
        )

    @app.get("/api/egress/qualification/metrics")
    async def get_qualification_metrics(format: str = "json"):
        from free.qualification import metrics

        values = metrics.snapshot(db_path_getter())
        if str(format).lower() == "prometheus":
            return Response(
                content=metrics.render_prometheus(values),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )
        return values

    @app.get("/api/egress/qualification/stations")
    async def get_qualification_stations():
        from free.qualification import readmodel

        return readmodel.stations(db_path_getter())

    @app.get("/api/egress/qualification/watchdog")
    async def get_qualification_watchdog():
        from free.qualification import readmodel

        return readmodel.watchdog(db_path_getter())

    @app.get("/api/egress/qualification/discovery")
    async def get_qualification_discovery(limit: int = 20):
        from free.qualification import readmodel

        return readmodel.discovery(db_path_getter(), limit=min(max(1, int(limit)), 200))

    @app.post("/api/egress/qualification/candidates/{hostname}/pause")
    async def pause_qualification_candidate(hostname: str, request: Request):
        return _operator_toggle(hostname, paused=True, request=request)

    @app.post("/api/egress/qualification/candidates/{hostname}/resume")
    async def resume_qualification_candidate(hostname: str, request: Request):
        return _operator_toggle(hostname, paused=False, request=request)

    def _operator_toggle(hostname: str, *, paused: bool, request: Request):
        if token_checker is not None:
            err = token_checker(request)
            if err:
                return err
        from free.qualification import readmodel
        from free.qualification.store import QualificationStore

        store = QualificationStore(db_path_getter())
        try:
            changed = store.set_paused(hostname, paused=paused)
        finally:
            store.close()
        readmodel.invalidate(db_path_getter())
        return {
            "ok": True,
            "hostname": hostname,
            "paused": paused,
            "changed": changed,
        }

    @app.get("/api/egress/qualification/repairs")
    async def get_qualification_repairs(limit: int = 50):
        from free.qualification import readmodel

        return readmodel.repairs(db_path_getter(), limit=min(max(1, int(limit)), 500))

    @app.get("/api/egress/qualification/rotations")
    async def get_qualification_rotations(limit: int = 50):
        from free.qualification import readmodel

        return readmodel.rotations(db_path_getter(), limit=min(max(1, int(limit)), 500))

    @app.get("/api/egress/qualification/slo")
    async def get_qualification_slo(target_pct: float = 99.0):
        from free.qualification import readmodel

        return readmodel.slo(db_path_getter(), target_pct=target_pct)

    @app.get("/api/egress/qualification/export")
    async def export_qualification():
        from free.qualification import readmodel

        path = db_path_getter()
        return {
            "summary": readmodel.summary(path),
            "queue": readmodel.queue(path, limit=500),
            "next": readmodel.next_candidates(path, limit=50),
            "epochs": readmodel.epochs(path, limit=50),
            "transports": readmodel.transports(path),
            "stations": readmodel.stations(path),
            "watchdog": readmodel.watchdog(path),
            "discovery": readmodel.discovery(path, limit=50),
            "repairs": readmodel.repairs(path, limit=200),
            "rotations": readmodel.rotations(path, limit=100),
            "quota": readmodel.quota(path, limit=500),
            "slo": readmodel.slo(path),
        }

    @app.post("/api/egress/qualification/rotate")
    async def post_qualification_rotate(request: Request):
        if token_checker is not None:
            err = token_checker(request)
            if err:
                return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        station = int(body.get("station") or 1)
        if bool(body.get("apply")):
            if not allow_live:
                return JSONResponse(
                    {
                        "error": (
                            "apply refusé : shadow_mode est encore actif"
                        )
                    },
                    status_code=409,
                )
            if rotate_apply is None:
                return JSONResponse(
                    {"error": "rotation executor non configuré"},
                    status_code=409,
                )
            try:
                result = await rotate_apply(station=station, body=body)
            except Exception as exc:
                return JSONResponse(
                    {"error": f"rotation apply impossible : {exc}"},
                    status_code=500,
                )
            return JSONResponse(result)

        from free.qualification.apply import station_snapshot
        from free.qualification.rotation import RotationPlanner
        from free.qualification.store import QualificationStore

        store = QualificationStore(db_path_getter())
        try:
            usable = sum(
                1 for row in store.list_station_states() if row.get("client_usable")
            )
            snapshot = station_snapshot(
                store,
                station,
                body,
                standby_size=int(station_standby_size),
            )
            decision = RotationPlanner(
                store,
                headroom_threshold_pct=rotation_headroom_pct,
                last_station_protection=last_station_protection,
            ).plan(
                snapshot,
                usable_stations=max(1, usable),
            )
            return {
                "ok": True,
                "dry_run": True,
                "station": station,
                "usable_stations": usable,
                "decision": decision.snapshot(),
            }
        finally:
            store.close()


__all__ = ["register_qualification_routes"]
