"""Per-station and fleet control routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_station_control_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    managers_getter: Callable[[], list[Any]],
    pool_getter: Callable[[], Any],
    fleet_getter: Callable[[], Any],
    control_getter: Callable[[int], Any] | None = None,
) -> None:
    def _manager(station: int) -> Any:
        return next(
            (item for item in (managers_getter() or []) if getattr(item, "_station", None) == station),
            None,
        )

    def _control(station: int) -> Any:
        if control_getter is None:
            return None
        try:
            return control_getter(int(station))
        except Exception:
            return None

    def _control_unavailable(station: int) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": "control plane indisponible",
                "station": station,
                "code": "control_plane_unavailable",
            },
        )

    def _control_error(exc: Exception) -> JSONResponse:
        status = int(getattr(exc, "status", 0) or 0)
        content = {
            "error": str(exc)[:200],
            "code": str(getattr(exc, "code", "") or "control_plane_error"),
        }
        if status in (400, 401, 403, 404, 409, 422):
            return JSONResponse(status_code=status, content=content)
        return JSONResponse(status_code=502, content=content)

    def _job_response(
        station: int,
        job: Any,
        *,
        wait: bool = False,
        planned: bool = False,
    ) -> JSONResponse:
        status = 200 if planned or (wait and bool(getattr(job, "terminal", False))) else 202
        return JSONResponse(
            status_code=status,
            content={
                "ok": True,
                "station": station,
                "state": str(getattr(job, "state", "") or ""),
                "job": dict(getattr(job, "raw", {}) or {}),
            },
        )

    def _constraints(body: dict[str, Any]) -> Any:
        from free.control_plane.models import Constraints

        raw = body.get("constraints")
        raw = raw if isinstance(raw, dict) else {}
        return Constraints(
            require_new_ip=bool(raw.get("require_new_ip", False)),
            require_new_subnet=bool(raw.get("require_new_subnet", False)),
            exclude_hostnames=tuple(raw.get("exclude_hostnames") or ()),
            exclude_ips=tuple(raw.get("exclude_ips") or ()),
            exclude_subnets=tuple(raw.get("exclude_subnets") or ()),
            forbidden_ips=tuple(raw.get("forbidden_ips") or ()),
            max_candidates=int(raw.get("max_candidates", 0) or 0),
            max_attempts=int(raw.get("max_attempts", 0) or 0),
        )

    def _candidates(body: dict[str, Any]) -> list[Any]:
        from free.control_plane.models import ServerCandidate

        candidates: list[Any] = []
        for item in body.get("candidates") or []:
            if isinstance(item, str):
                hostname, endpoint_ip, expected = item, "", ""
            elif isinstance(item, dict):
                hostname = str(item.get("hostname") or "")
                endpoint_ip = str(item.get("endpoint_ip") or "")
                expected = str(item.get("expected_egress_ip") or "")
            else:
                continue
            hostname = hostname.strip()
            if hostname:
                candidates.append(
                    ServerCandidate(
                        hostname=hostname,
                        endpoint_ip=endpoint_ip.strip(),
                        expected_egress_ip=expected.strip(),
                    )
                )
        return candidates

    async def _body(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _missing(station: int) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"error": f"station {station} introuvable"},
        )

    @app.post("/api/vpn/station/{sid}/pin_country")
    async def pin_station_country(sid: int, request: Request):
        err = token_checker(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": "JSON invalide"})
        country = str((body or {}).get("country") or "").strip()
        if not country:
            return JSONResponse(
                status_code=400,
                content={"error": "'country' requis"},
            )
        manager = _manager(sid)
        if manager is None:
            return _missing(sid)
        try:
            ok = await asyncio.wait_for(manager.pin_country(country), timeout=60)
        except TypeError:
            return JSONResponse(
                status_code=500,
                content={"error": "manager sans pin_country"},
            )
        except Exception as exc:
            return JSONResponse(
                status_code=502,
                content={"error": str(exc)[:200]},
            )
        if ok:
            return {"ok": True, "station": sid, "country": country}
        return JSONResponse(
            status_code=502,
            content={"error": ("pin refusé par le control server (pays invalide ou tunnel KO)")},
        )

    @app.delete("/api/vpn/station/{sid}/pin_country")
    async def unpin_station_country(sid: int, request: Request):
        err = token_checker(request)
        if err:
            return err
        manager = _manager(sid)
        if manager is None:
            return _missing(sid)
        try:
            ok = await asyncio.wait_for(manager.unpin_country(), timeout=45)
        except Exception as exc:
            return JSONResponse(
                status_code=502,
                content={"error": str(exc)[:200]},
            )
        return {"ok": bool(ok), "station": sid}

    @app.post("/api/vpn/station/{sid}/probe")
    async def probe_station(sid: int, request: Request):
        err = token_checker(request)
        if err:
            return err
        manager = _manager(sid)
        if manager is None:
            return _missing(sid)
        try:
            ip = await asyncio.wait_for(manager.get_public_ip(), timeout=30)
        except Exception as exc:
            manager._note_pool_health(False, cause="probe")
            return {"ok": False, "station": sid, "error": str(exc)[:200]}
        ok = bool(ip)
        manager._note_pool_health(ok, cause="probe")
        if ok:
            manager._note_pool_health(True, cause="probe")
            try:
                manager._error = None
                manager._auth_failed = False
                manager._set_status("connected")
            except Exception:
                pass
        return {"ok": ok, "station": sid, "ip": ip}

    @app.post("/api/vpn/station/{sid}/restart")
    async def restart_station(sid: int, request: Request):
        err = token_checker(request)
        if err:
            return err
        manager = _manager(sid)
        if manager is None:
            return _missing(sid)
        try:
            await asyncio.wait_for(manager.restart(), timeout=150)
        except TimeoutError:
            return {
                "ok": True,
                "station": sid,
                "note": "restart en cours — suivre via SSE",
            }
        except Exception as exc:
            return JSONResponse(
                status_code=502,
                content={"error": str(exc)[:200]},
            )
        return {"ok": True, "station": sid}

    @app.post("/api/vpn/station/{sid}/soft-rotate")
    async def soft_rotate_station(sid: int, request: Request):
        err = token_checker(request)
        if err:
            return err
        manager = _manager(sid)
        pool = pool_getter()
        if manager is None or pool is None or not hasattr(pool, "_launch_rotation"):
            return JSONResponse(
                status_code=404,
                content={"error": f"station {sid} ou pool indisponible"},
            )
        try:
            pool._launch_rotation(manager)
        except Exception as exc:
            return JSONResponse(
                status_code=502,
                content={"error": str(exc)[:200]},
            )
        return {"ok": True, "station": sid, "queued": True}

    @app.post("/api/vpn/station/{sid}/stack")
    async def set_station_stack(sid: int, request: Request):
        err = token_checker(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        stack = str((body or {}).get("stack") or "").strip().lower()
        if stack not in ("wireguard", "openvpn"):
            return JSONResponse(
                status_code=400,
                content={"error": "stack doit être wireguard|openvpn"},
            )
        manager = _manager(sid)
        if manager is None:
            return _missing(sid)
        try:
            result = await manager.set_stack(
                stack,
                propagate=True,
                stations=[sid],
                force=True,
            )
            if not result.get("ok"):
                return JSONResponse(
                    status_code=502,
                    content={"error": result.get("error") or "échec application stack"},
                )
            return {
                "ok": True,
                "station": sid,
                "stack": stack,
                "effective": manager._stack_effective,
            }
        except Exception as exc:
            return JSONResponse(
                status_code=502,
                content={"error": str(exc)[:200]},
            )

    @app.post("/api/vpn/station/{sid}/start")
    async def start_station(sid: int, request: Request):
        err = token_checker(request)
        if err:
            return err
        return await fleet_getter().start_station(sid)

    @app.post("/api/vpn/station/{sid}/stop")
    async def stop_station(sid: int, request: Request):
        err = token_checker(request)
        if err:
            return err
        return await fleet_getter().stop_station(sid)

    @app.post("/api/vpn/station/{sid}/connect")
    async def connect_station(sid: int, request: Request):
        err = token_checker(request)
        if err:
            return err
        return await fleet_getter().connect_station(sid)

    @app.post("/api/vpn/station/{sid}/disconnect")
    async def disconnect_station(sid: int, request: Request):
        err = token_checker(request)
        if err:
            return err
        return await fleet_getter().disconnect_station(sid)

    @app.post("/api/vpn/fleet/connect")
    async def fleet_connect(request: Request):
        err = token_checker(request)
        if err:
            return err
        return await fleet_getter().connect_all()

    @app.post("/api/vpn/fleet/disconnect")
    async def fleet_disconnect(request: Request):
        err = token_checker(request)
        if err:
            return err
        return await fleet_getter().disconnect_all(stop_containers=False)

    @app.get("/api/vpn/fleet")
    async def fleet_status(request: Request):
        err = token_checker(request)
        if err:
            return err
        return fleet_getter().status()

    # ── Control plane reads ───────────────────────────────────────────
    @app.get("/api/vpn/station/{sid}/control/state")
    async def station_control_state(sid: int, request: Request):
        err = token_checker(request)
        if err:
            return err
        client = _control(sid)
        if client is None:
            return _control_unavailable(sid)
        try:
            state = await asyncio.to_thread(client.state)
        except Exception as exc:
            return _control_error(exc)
        return {"ok": True, "station": sid, "state": state.raw}

    @app.get("/api/vpn/station/{sid}/control/jobs/{job_id}")
    async def station_control_job(sid: int, job_id: str, request: Request):
        err = token_checker(request)
        if err:
            return err
        client = _control(sid)
        if client is None:
            return _control_unavailable(sid)
        try:
            job = await asyncio.to_thread(client.job, job_id)
        except Exception as exc:
            return _control_error(exc)
        return {
            "ok": True,
            "station": sid,
            "state": job.state,
            "job": job.raw,
        }

    @app.get("/api/vpn/station/{sid}/control/events")
    async def station_control_events(
        sid: int,
        request: Request,
        since: int = 0,
        limit: int = 100,
    ):
        err = token_checker(request)
        if err:
            return err
        client = _control(sid)
        if client is None:
            return _control_unavailable(sid)
        try:
            page = await asyncio.to_thread(
                client.events_page,
                since=since,
                limit=max(1, min(int(limit), 500)),
            )
        except Exception as exc:
            return _control_error(exc)
        return {
            "ok": True,
            "station": sid,
            "items": [event.raw for event in page.items],
            "next_seq": page.next_seq,
            "gap": page.gap,
        }

    # ── Control plane writes (jobs, never docker exec) ────────────────
    @app.post("/api/vpn/station/{sid}/control/switch")
    async def station_control_switch(sid: int, request: Request):
        err = token_checker(request)
        if err:
            return err
        client = _control(sid)
        if client is None:
            return _control_unavailable(sid)
        from free.control_plane.models import TunnelTarget

        body = await _body(request)
        hostname = str(body.get("hostname") or "").strip()
        if not hostname:
            return JSONResponse(
                status_code=400,
                content={"error": "'hostname' requis"},
            )
        target = TunnelTarget(
            hostname=hostname,
            endpoint_ip=str(body.get("endpoint_ip") or "").strip(),
            expected_egress_ip=str(body.get("expected_egress_ip") or "").strip(),
        )
        key = str(body.get("idempotency_key") or f"dashboard-{sid}-{uuid.uuid4().hex}")
        try:
            job = await asyncio.to_thread(
                client.switch,
                target,
                _constraints(body),
                idempotency_key=key,
                wait=bool(body.get("wait", False)),
                dry_run=bool(body.get("dry_run", False)),
                transport_policy=body.get("transport_policy"),
            )
        except Exception as exc:
            return _control_error(exc)
        return _job_response(
            sid,
            job,
            wait=bool(body.get("wait", False)),
            planned=bool(body.get("dry_run", False)),
        )

    @app.post("/api/vpn/station/{sid}/control/rotate")
    async def station_control_rotate(sid: int, request: Request):
        err = token_checker(request)
        if err:
            return err
        client = _control(sid)
        if client is None:
            return _control_unavailable(sid)
        body = await _body(request)
        candidates = _candidates(body)
        if not candidates:
            return JSONResponse(
                status_code=400,
                content={"error": "'candidates' requis"},
            )
        key = str(body.get("idempotency_key") or f"dashboard-{sid}-{uuid.uuid4().hex}")
        try:
            job = await asyncio.to_thread(
                client.rotate,
                candidates,
                _constraints(body),
                idempotency_key=key,
                wait=bool(body.get("wait", False)),
                transport_policy=body.get("transport_policy"),
            )
        except Exception as exc:
            return _control_error(exc)
        return _job_response(sid, job, wait=bool(body.get("wait", False)))

    @app.post("/api/vpn/station/{sid}/control/rollback")
    async def station_control_rollback(sid: int, request: Request):
        err = token_checker(request)
        if err:
            return err
        client = _control(sid)
        if client is None:
            return _control_unavailable(sid)
        body = await _body(request)
        key = str(body.get("idempotency_key") or f"dashboard-{sid}-{uuid.uuid4().hex}")
        try:
            job = await asyncio.to_thread(
                client.rollback,
                wait=bool(body.get("wait", False)),
                idempotency_key=key,
            )
        except Exception as exc:
            return _control_error(exc)
        return _job_response(sid, job, wait=bool(body.get("wait", False)))

    @app.post("/api/vpn/station/{sid}/control/transport/reset")
    async def station_control_transport_reset(sid: int, request: Request):
        err = token_checker(request)
        if err:
            return err
        client = _control(sid)
        if client is None:
            return _control_unavailable(sid)
        body = await _body(request)
        transport = str(body.get("transport") or "").strip() or None
        try:
            state = await asyncio.to_thread(client.transport_reset, transport)
        except Exception as exc:
            return _control_error(exc)
        return {"ok": True, "station": sid, "transport": state.raw}


__all__ = ["register_station_control_routes"]
