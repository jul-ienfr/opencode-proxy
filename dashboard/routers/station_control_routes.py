"""Per-station and fleet control routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

import asyncio
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
) -> None:
    def _manager(station: int) -> Any:
        return next(
            (
                item
                for item in (managers_getter() or [])
                if getattr(item, "_station", None) == station
            ),
            None,
        )

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
            content={
                "error": (
                    "pin refusé par le control server "
                    "(pays invalide ou tunnel KO)"
                )
            },
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
                    content={
                        "error": result.get("error")
                        or "échec application stack"
                    },
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


__all__ = ["register_station_control_routes"]
