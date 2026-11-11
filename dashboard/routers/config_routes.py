"""Configuration read/update routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request


def register_config_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    settings: Any,
    get_models: Callable[[], Any],
    local_ips_cache_getter: Callable[[], Any],
    get_local_ips: Callable[[], list[Any]],
    default_port: int,
    proxy_getter: Callable[[], str],
    api_key_getter: Callable[[], str],
    host_getter: Callable[[], str],
    api_keys_getter: Callable[[], list[dict[str, Any]]],
    api_key_routing_getter: Callable[[], str],
    model_limits: Callable[[Any], Any],
    model_capabilities: Callable[[Any], Any],
    save_env: Callable[[dict[str, Any]], Any],
    apply_server_changes: Callable[..., Any],
    server_manager_getter: Callable[[], Any] | None,
    debug: Callable[[str], Any],
) -> None:
    def _manager() -> Any:
        return server_manager_getter() if server_manager_getter else None

    @app.get("/api/config")
    async def get_config(request: Request):
        err = token_checker(request)
        if err:
            return err
        routes_info = {}
        for name, info in settings.ROUTES.items():
            routes_info[name] = {
                "match": info["match"],
                "model": info["model"],
            }
        models_info: Any
        try:
            models_info = get_models()
        except Exception:
            models_info = []
        try:
            local_ips_cache = local_ips_cache_getter()
            if local_ips_cache and (time.monotonic() - local_ips_cache[0]) < 60:
                local_ips = local_ips_cache[1]
            else:
                local_ips = local_ips_cache[1] if local_ips_cache else []
                try:
                    asyncio.create_task(asyncio.to_thread(get_local_ips))
                except Exception:
                    pass
        except Exception:
            local_ips = []
        manager = _manager()
        if manager:
            # [fix 10/11] Même self-heal que /api/proxy/status : ne pas
            # afficher « proxy arrêté » sur un flag resté à False après un
            # boot plus long que le délai synchrone de ServerManager.start().
            refresh = getattr(manager, "refresh_state", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:
                    pass
        try:
            port = int(getattr(manager, "port", default_port)) if manager else default_port
        except (TypeError, ValueError):
            port = default_port
        free_policy = settings.get_free_substitution_policy()
        free_map = settings.FREE_MODEL_MAP or {}
        free_map_effective = dict(free_map)
        try:
            from config.model_tier import tier_counts

            model_counts = tier_counts(
                models_info.values() if isinstance(models_info, dict) else ()
            )
        except Exception:
            model_counts = {
                "total": len(models_info) if isinstance(models_info, dict) else 0,
                "free": 0,
                "paid": 0,
                "paid_free_fallback": 0,
                "paid_free_ignored": 0,
                "unknown": 0,
                "route_mismatch": 0,
                "catalog_mismatch": 0,
            }
        proxy = proxy_getter()
        api_key = api_key_getter()
        host = host_getter()
        workspace_id = settings.OPENCODE_GO_WORKSPACE_ID
        auth_cookie = settings.OPENCODE_GO_AUTH_COOKIE
        api_keys = api_keys_getter()
        return {
            "proxy": proxy or "",
            "api_key_set": bool(api_key),
            "api_key_masked": (api_key[:4] + "****" + api_key[-4:])
            if api_key and len(api_key) > 8
            else ("****" if api_key else ""),
            "host": host,
            "port": port,
            "local_ips": local_ips,
            "routes": routes_info,
            "models": models_info,
            "model_counts": model_counts,
            "model_limits": model_limits(models_info),
            "model_capabilities": model_capabilities(models_info),
            "proxy_running": manager.is_running if manager else True,
            "disable_mapping": settings.DISABLE_MAPPING,
            "custom_routes": settings.CUSTOM_ROUTES,
            "go_workspace_id_set": bool(workspace_id),
            "go_workspace_id_masked": (
                workspace_id[:4] + "****" if len(workspace_id) > 4 else "****"
            )
            if workspace_id
            else "",
            "go_auth_cookie_set": bool(auth_cookie),
            "go_auth_cookie_masked": (auth_cookie[:6] + "****")
            if auth_cookie and len(auth_cookie) > 6
            else (""),
            "api_keys": [
                {
                    "api_key_masked": (key["api_key"][:4] + "****" + key["api_key"][-4:])
                    if len(key.get("api_key", "")) > 8
                    else "****",
                    "go_workspace_id_masked": (key.get("go_workspace_id", "")[:4] + "****")
                    if len(key.get("go_workspace_id", "")) > 4
                    else "",
                    "go_auth_cookie_masked": (key.get("go_auth_cookie", "")[:6] + "****")
                    if len(key.get("go_auth_cookie", "")) > 6
                    else "",
                    "enabled": key.get("enabled", True),
                    "alias": key.get("alias", ""),
                }
                for key in api_keys
            ],
            "routing": api_key_routing_getter(),
            "free_parallel": dict(getattr(settings, "FREE_PARALLEL", {})),
            "free_substitution_policy": free_policy,
            "free_model_map": free_map,
            "free_model_map_effective": free_map_effective,
            "lang": os.getenv("PROXY_LANG", "en"),
        }

    @app.post("/api/config")
    async def update_config(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        env_updates = {}
        current_port = default_port
        current_host = host_getter()

        if "routes" in body:
            for route_name, model in body["routes"].items():
                if route_name == "opus":
                    env_updates["OPUS_MAP_MODEL"] = model
                elif route_name == "sonnet":
                    env_updates["SONNET_MAP_MODEL"] = model
                elif route_name == "haiku":
                    env_updates["HAIKU_MAP_MODEL"] = model

        if "proxy" in body:
            env_updates["OPENCODE_PROXY"] = body["proxy"]

        if "disable_mapping" in body:
            env_updates["DISABLE_MAPPING"] = "1" if body["disable_mapping"] else "0"

        if "api_key" in body and body["api_key"]:
            env_updates["OPENCODE_API_KEY"] = body["api_key"]

        if "go_workspace_id" in body and body["go_workspace_id"]:
            env_updates["OPENCODE_GO_WORKSPACE_ID"] = body["go_workspace_id"]

        if "go_auth_cookie" in body and body["go_auth_cookie"]:
            env_updates["OPENCODE_GO_AUTH_COOKIE"] = body["go_auth_cookie"]

        if "routing" in body:
            env_updates["API_KEY_ROUTING"] = body["routing"]
            try:
                import opencode as opencode_module

                opencode_module.API_KEY_ROUTING = str(body["routing"])
            except Exception:
                pass

        if "lang" in body:
            env_updates["PROXY_LANG"] = body["lang"]

        restart_needed = False

        if "port" in body:
            try:
                new_port = int(body["port"])
            except (ValueError, TypeError):
                return {"status": "error", "message": "Valeur de port invalide"}
            if not (1 <= new_port <= 65535):
                return {
                    "status": "error",
                    "message": "Le port doit être entre 1 et 65535",
                }
            if new_port != current_port:
                env_updates["OPENCODE_PORT"] = str(new_port)
                restart_needed = True

        if "host" in body:
            new_host = body["host"]
            if new_host != current_host:
                env_updates["OPENCODE_HOST"] = new_host
                restart_needed = True

        if env_updates:
            save_env(env_updates)

        if restart_needed:
            apply_server_changes(
                port=body.get("port"),
                host=body.get("host"),
            )
            manager = _manager()
            if manager:

                def _do_restart():
                    try:
                        manager.restart(
                            port=body.get("port"),
                            host=body.get("host"),
                        )
                    except Exception as error:
                        debug(
                            f"  [config] hot-restart FAILED: "
                            f"{type(error).__name__}: {error}"
                        )

                threading.Thread(
                    target=_do_restart,
                    name="proxy-restart",
                    daemon=False,
                ).start()

        return {
            "status": "ok",
            "needs_restart": False,
            "message": "Configuration mise à jour. Redémarrage à chaud déclenché."
            if restart_needed
            else "Configuration mise à jour.",
        }


__all__ = ["register_config_routes"]
