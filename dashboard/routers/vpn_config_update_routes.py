"""VPN configuration update route extracted from ``dashboard/api.py``."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_vpn_config_update_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    settings: Any,
    write_credentials_env: Callable[[str, str], Any],
    persist_free_model_map: Callable[[dict[str, Any]], Any],
    quality_blocks_errors: Callable[[dict[str, Any]], list[str]],
    persist_vpn_config: Callable[[dict[str, Any]], Any],
    egress_audit: Callable[[str, dict[str, Any]], Any],
    debug: Callable[[str], Any],
) -> None:
    @app.post("/api/vpn-config")
    async def update_vpn_config(request: Request):
        """Met à jour la configuration VPN (hot-reload + persistance)."""
        err = token_checker(request)
        if err:
            return err
        import shared_state

        body = await request.json()

        if "credentials" in body:
            credentials = body.pop("credentials")
            username = credentials.get("username", "")
            password = credentials.get("password", "")
            if username and password:
                try:
                    write_credentials_env(username, password)
                except ValueError as exc:
                    return JSONResponse(
                        status_code=400,
                        content={"error": str(exc)},
                    )

        if "free_model_map" in body:
            free_model_map = body.pop("free_model_map")
            if isinstance(free_model_map, dict):
                try:
                    from config import settings as runtime_settings

                    runtime_settings.FREE_MODEL_MAP.clear()
                    runtime_settings.FREE_MODEL_MAP.update(free_model_map)
                    runtime_settings._yaml_data["free_model_map"] = dict(
                        free_model_map
                    )
                except Exception as error:
                    debug(f"  [free] free_model_map hot-reload failed: {error}")
                    return {
                        "error": f"échec hot-reload free_model_map : {error}"
                    }
                persist_free_model_map(free_model_map)

        if "max_free_attempts" in body:
            try:
                body["max_free_attempts"] = max(
                    1,
                    min(3, int(body["max_free_attempts"])),
                )
            except (TypeError, ValueError):
                return {
                    "error": "max_free_attempts doit être un entier entre 1 et 3"
                }
        if "auto_max_free_attempts" in body:
            raw_auto = body["auto_max_free_attempts"]
            if isinstance(raw_auto, str):
                body["auto_max_free_attempts"] = raw_auto.strip().lower() in (
                    "1",
                    "true",
                    "yes",
                    "on",
                )
            else:
                body["auto_max_free_attempts"] = bool(raw_auto)
        if "free_exception_fallback" in body:
            if str(body["free_exception_fallback"]) not in (
                "station-first",
                "direct",
            ):
                return {
                    "error": "free_exception_fallback doit être "
                    "'station-first' ou 'direct'"
                }
            body["free_exception_fallback"] = str(body["free_exception_fallback"])
        if "free_substitution_policy" in body:
            policy = str(body["free_substitution_policy"] or "").strip().lower()
            if policy not in settings.FREE_SUBSTITUTION_POLICIES:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "free_substitution_policy doit être "
                        "'homonym' ou 'family'"
                    },
                )
            body["free_substitution_policy"] = policy
        if "on_429_action" in body:
            action = str(body["on_429_action"] or "").strip().lower()
            if action not in ("cooldown", "rotate", "both"):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "on_429_action doit être 'cooldown', "
                        "'rotate' ou 'both'"
                    },
                )
            body["on_429_action"] = action
        if "server_pick_mode" in body:
            mode = str(body["server_pick_mode"] or "").strip().lower()
            if mode not in ("proxy", "gluetun"):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "server_pick_mode doit être 'proxy' ou 'gluetun'"
                    },
                )
            body["server_pick_mode"] = mode
        if "bad_ttl" in body or "cooldown_sec" in body or "badTtl" in body:
            raw_ttl = body.pop(
                "bad_ttl",
                body.pop("cooldown_sec", body.pop("badTtl", None)),
            )
            try:
                bad_ttl = int(float(raw_ttl))
            except (TypeError, ValueError):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "bad_ttl doit être un entier entre 1 et 3600"
                    },
                )
            if not (1 <= bad_ttl <= 3600):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "bad_ttl doit être un entier entre 1 et 3600"
                    },
                )
            body["bad_ttl"] = bad_ttl
        if "station_bad_ttl_s" in body:
            try:
                station_ttl = int(float(body["station_bad_ttl_s"]))
            except (TypeError, ValueError):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "station_bad_ttl_s doit être un entier "
                        "entre 1 et 3600"
                    },
                )
            if not (1 <= station_ttl <= 3600):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "station_bad_ttl_s doit être un entier "
                        "entre 1 et 3600"
                    },
                )
            body["station_bad_ttl_s"] = station_ttl

        if "latency_rotation" in body:
            latency_rotation = body["latency_rotation"]
            if not isinstance(latency_rotation, dict):
                return JSONResponse(
                    status_code=400,
                    content={"error": "latency_rotation doit être un objet"},
                )
            if "enabled" in latency_rotation and not isinstance(
                latency_rotation["enabled"],
                bool,
            ):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "latency_rotation.enabled doit être booléen"
                    },
                )

        quality_errors = quality_blocks_errors(body)
        if quality_errors:
            return JSONResponse(
                status_code=400,
                content={"error": "; ".join(quality_errors)},
            )

        if "free_parallel" in body:
            if not isinstance(body["free_parallel"], dict):
                return {"error": "free_parallel doit être un objet"}
        if "free_parallel_routing" in body:
            if str(body["free_parallel_routing"]).lower() not in (
                "round-robin",
                "failover",
            ):
                return {
                    "error": "free_parallel_routing doit être "
                    "'round-robin' ou 'failover'"
                }
            body["free_parallel_routing"] = str(
                body["free_parallel_routing"]
            ).lower()
        if "free_parallel_mode" in body:
            if str(body["free_parallel_mode"]).lower() not in (
                "load-balance",
                "strict",
                "hedge",
            ):
                return {
                    "error": "free_parallel_mode doit être 'load-balance', "
                    "'strict' ou 'hedge'"
                }
            body["free_parallel_mode"] = str(body["free_parallel_mode"]).lower()

        managers = getattr(shared_state, "vpn_managers", None) or []
        if not managers and "station_count" in (body or {}):
            return JSONResponse(
                status_code=503,
                content={
                    "error": "gestionnaire VPN non initialisé "
                    "(aucune station active)"
                },
            )
        if managers and body:
            requested_station_count: int | None = None
            if "station_count" in body:
                raw_count = body["station_count"]
                try:
                    new_count = int(raw_count)
                except (TypeError, ValueError):
                    new_count = 0
                if not (1 <= new_count <= 10):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": "station_count doit être un entier "
                            f"entre 1 et 10, reçu {raw_count!r}"
                        },
                    )
                body.pop("station_count")
                requested_station_count = new_count
                if new_count != len(managers):
                    try:
                        from opencode import _apply_station_count

                        await _apply_station_count(new_count)
                    except Exception as error:
                        debug(
                            f"  [vpn] station_count hot-reload failed: {error}\n"
                            f"{traceback.format_exc()}"
                        )
                        return JSONResponse(
                            status_code=500,
                            content={
                                "error": f"échec hot-reload station_count : {error}"
                            },
                        )
                    managers = getattr(shared_state, "vpn_managers", None) or []
                else:
                    try:
                        from opencode import _apply_station_count as sync_count

                        await sync_count(new_count)
                    except Exception as error:
                        debug(
                            f"  [vpn] station_count resync failed: {error}\n"
                            f"{traceback.format_exc()}"
                        )
                        return JSONResponse(
                            status_code=500,
                            content={
                                "error": f"échec resync station_count : {error}"
                            },
                        )
                    managers = getattr(shared_state, "vpn_managers", None) or []
            if requested_station_count is not None:
                egress_audit(
                    "station_count",
                    {"station_count": int(requested_station_count)},
                )
            for manager in managers:
                await manager.update_config(body)
            pool = getattr(shared_state, "free_ip_pool", None)
            if pool is not None:
                pool.update_config(body)

            persist_body = {
                key: value for key, value in body.items() if key != "vpn_stack"
            }
            persist_error = persist_vpn_config(persist_body)
            if persist_error:
                debug(f"  [vpn] persist config failed: {persist_error}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": f"échec persistance config : {persist_error}"
                    },
                )

            if "vpn_stack" in body:
                stack_ok = False
                try:
                    stack_result = await managers[0].set_stack(
                        str(body["vpn_stack"])
                    )
                    stack_ok = bool(stack_result.get("ok"))
                    if stack_ok:
                        persist_vpn_config({"vpn_stack": str(body["vpn_stack"])})
                    else:
                        debug(
                            "  [vpn] set_stack refused: "
                            f"{stack_result.get('error')} — config.yaml keeps "
                            "the previous stack"
                        )
                except Exception as error:
                    debug(f"  [vpn] set_stack failed: {error}")
                if stack_ok:
                    for manager in managers[1:]:
                        try:
                            await manager.set_stack(
                                str(body["vpn_stack"]),
                                propagate=False,
                            )
                        except Exception as error:
                            debug(
                                "  [vpn] set_stack (station "
                                f"{manager._station}) failed: {error}"
                            )

        config = managers[0].get_config() if managers else {}
        return {"ok": True, "config": config}


__all__ = ["register_vpn_config_update_routes"]
