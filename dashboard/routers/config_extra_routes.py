"""Custom routes, web tools and API-key configuration routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_config_extra_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    settings: Any,
    models_getter: Callable[[], dict[str, Any]],
    native_models: Any,
    yaml_get: Callable[..., Any],
    yaml_set: Callable[..., Any],
    validate_custom_routes: Callable[[Any], Any],
    save_custom_routes: Callable[[Any], Any],
    api_keys_getter: Callable[[], list[dict[str, Any]]],
    api_key_routing_getter: Callable[[], str],
    merge_preserved_api_keys: Callable[..., Any],
    save_api_keys: Callable[[Any], Any],
    save_env: Callable[[dict[str, Any]], Any],
    debug: Callable[[str], Any],
) -> None:
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)

    @app.get("/api/config/custom-routes")
    async def get_custom_routes():
        return settings.CUSTOM_ROUTES

    @app.post("/api/config/custom-routes")
    async def update_custom_routes(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        error = validate_custom_routes(body)
        if error:
            return JSONResponse(
                status_code=400,
                content={"error": f"custom_routes invalide: {error}"},
            )
        debug(f"  [config] custom routes updated ({len(body)} routes)")
        save_custom_routes(body)
        return {"status": "ok", "message": "Custom routes updated."}

    @app.get("/api/config/web-search")
    async def get_web_search_config():
        models = models_getter()
        return {
            "mode": yaml_get("web_search", "mode", "duckduckgo"),
            "target_model": yaml_get("web_search", "target_model", None),
            "max_results": yaml_get("web_search", "max_results", 5),
            "timeout": yaml_get("web_search", "timeout", 10),
            "enabled": bool(yaml_get("web_search", "enabled", True)),
            "via_vpn": bool(yaml_get("web_search", "via_vpn", False)),
            "on_error": yaml_get("web_search", "on_error", "strip"),
            "web_fetch": {
                "mode": yaml_get("web_fetch", "mode", "direct"),
                "target_model": yaml_get("web_fetch", "target_model", None),
                "max_bytes": yaml_get("web_fetch", "max_bytes", 12000),
                "timeout": yaml_get("web_fetch", "timeout", 15),
                "enabled": bool(yaml_get("web_fetch", "enabled", True)),
                "via_vpn": bool(yaml_get("web_fetch", "via_vpn", False)),
                "on_error": yaml_get("web_fetch", "on_error", "strip"),
            },
            "available_models": sorted(models.keys()),
            "modes": ["duckduckgo", "model", "ddg_then_model", "model_then_ddg"],
            "web_fetch_modes": ["direct", "model", "direct_then_model"],
            "native_models": native_models,
        }

    @app.post("/api/config/web-search")
    async def update_web_search_config(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        models = models_getter()

        if "mode" in body:
            mode = str(body["mode"])
            if mode in ("duckduckgo", "model", "ddg_then_model", "model_then_ddg"):
                yaml_set("web_search", "mode", mode)
            else:
                debug(f"  [config] web_search invalid mode {mode!r} → ignored")
        if "target_model" in body:
            target_model = body["target_model"]
            if target_model is None or target_model in models:
                yaml_set("web_search", "target_model", target_model)
            else:
                debug(f"  [config] web_search invalid target_model {target_model!r}")
        if "max_results" in body:
            try:
                yaml_set(
                    "web_search",
                    "max_results",
                    max(1, min(10, int(body["max_results"]))),
                )
            except Exception:
                pass
        if "timeout" in body:
            try:
                yaml_set(
                    "web_search",
                    "timeout",
                    max(5, min(30, int(body["timeout"]))),
                )
            except Exception:
                pass
        if "enabled" in body:
            yaml_set("web_search", "enabled", _to_bool(body["enabled"]))
        if "via_vpn" in body:
            yaml_set("web_search", "via_vpn", _to_bool(body["via_vpn"]))
        if "on_error" in body and body["on_error"] == "strip":
            yaml_set("web_search", "on_error", "strip")

        web_fetch = (
            body.get("web_fetch")
            if isinstance(body.get("web_fetch"), dict)
            else body
        )
        if isinstance(web_fetch, dict):
            if "mode" in web_fetch and web_fetch is not body:
                mode = str(web_fetch["mode"])
                if mode in ("direct", "model", "direct_then_model"):
                    yaml_set("web_fetch", "mode", mode)
            elif "web_fetch_mode" in body:
                mode = str(body["web_fetch_mode"])
                if mode in ("direct", "model", "direct_then_model"):
                    yaml_set("web_fetch", "mode", mode)
            if "web_fetch_target_model" in body:
                target_model = body["web_fetch_target_model"]
                if target_model is None or target_model in models:
                    yaml_set("web_fetch", "target_model", target_model)
            if (
                "target_model" in web_fetch
                and web_fetch is not body
                and "web_fetch" in body
            ):
                target_model = web_fetch["target_model"]
                if target_model is None or target_model in models:
                    yaml_set("web_fetch", "target_model", target_model)
            if "max_bytes" in web_fetch:
                try:
                    yaml_set(
                        "web_fetch",
                        "max_bytes",
                        max(2000, min(50000, int(web_fetch["max_bytes"]))),
                    )
                except Exception:
                    pass
            if "web_fetch_max_bytes" in body:
                try:
                    yaml_set(
                        "web_fetch",
                        "max_bytes",
                        max(2000, min(50000, int(body["web_fetch_max_bytes"]))),
                    )
                except Exception:
                    pass
            if "timeout" in web_fetch and web_fetch is not body:
                try:
                    yaml_set(
                        "web_fetch",
                        "timeout",
                        max(5, min(30, int(web_fetch["timeout"]))),
                    )
                except Exception:
                    pass
            if "web_fetch_timeout" in body:
                try:
                    yaml_set(
                        "web_fetch",
                        "timeout",
                        max(5, min(30, int(body["web_fetch_timeout"]))),
                    )
                except Exception:
                    pass
            if "enabled" in web_fetch and web_fetch is not body:
                yaml_set(
                    "web_fetch",
                    "enabled",
                    _to_bool(web_fetch["enabled"]),
                )
            if "web_fetch_enabled" in body:
                yaml_set(
                    "web_fetch",
                    "enabled",
                    _to_bool(body["web_fetch_enabled"]),
                )
            if "via_vpn" in web_fetch and web_fetch is not body:
                yaml_set(
                    "web_fetch",
                    "via_vpn",
                    _to_bool(web_fetch["via_vpn"]),
                )
            if "web_fetch_via_vpn" in body:
                yaml_set(
                    "web_fetch",
                    "via_vpn",
                    _to_bool(body["web_fetch_via_vpn"]),
                )
            if (
                "on_error" in web_fetch
                and web_fetch is not body
                and web_fetch["on_error"] == "strip"
            ):
                yaml_set("web_fetch", "on_error", "strip")
        debug(
            "  [config] web search updated: "
            f"mode={body.get('mode')}, model={body.get('target_model')} "
            f"web_fetch={body.get('web_fetch')}"
        )
        return {"status": "ok", "message": "Web search config updated."}

    @app.get("/api/config/web-fetch")
    async def get_web_fetch_config():
        return {
            "mode": yaml_get("web_fetch", "mode", "direct"),
            "target_model": yaml_get("web_fetch", "target_model", None),
            "max_bytes": yaml_get("web_fetch", "max_bytes", 12000),
            "timeout": yaml_get("web_fetch", "timeout", 15),
            "enabled": bool(yaml_get("web_fetch", "enabled", True)),
            "via_vpn": bool(yaml_get("web_fetch", "via_vpn", False)),
            "on_error": yaml_get("web_fetch", "on_error", "strip"),
            "available_models": sorted(models_getter().keys()),
            "modes": ["direct", "model", "direct_then_model"],
            "native_models": native_models,
        }

    @app.post("/api/config/web-fetch")
    async def update_web_fetch_config(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        models = models_getter()

        if "mode" in body and str(body["mode"]) in (
            "direct",
            "model",
            "direct_then_model",
        ):
            yaml_set("web_fetch", "mode", str(body["mode"]))
        if "target_model" in body and (
            body["target_model"] is None or body["target_model"] in models
        ):
            yaml_set("web_fetch", "target_model", body["target_model"])
        if "max_bytes" in body:
            try:
                yaml_set(
                    "web_fetch",
                    "max_bytes",
                    max(2000, min(50000, int(body["max_bytes"]))),
                )
            except Exception:
                pass
        if "timeout" in body:
            try:
                yaml_set(
                    "web_fetch",
                    "timeout",
                    max(5, min(30, int(body["timeout"]))),
                )
            except Exception:
                pass
        if "enabled" in body:
            yaml_set("web_fetch", "enabled", _to_bool(body["enabled"]))
        if "via_vpn" in body:
            yaml_set("web_fetch", "via_vpn", _to_bool(body["via_vpn"]))
        if "on_error" in body and body["on_error"] == "strip":
            yaml_set("web_fetch", "on_error", "strip")
        return {"status": "ok", "message": "Web fetch config updated."}

    @app.get("/api/config/api-keys")
    async def get_api_keys_config(request: Request):
        err = token_checker(request)
        if err:
            return err
        api_keys = api_keys_getter()
        return {
            "api_keys": [
                {
                    "api_key_masked": (
                        key["api_key"][:4] + "****" + key["api_key"][-4:]
                    )
                    if len(key.get("api_key", "")) > 8
                    else "****",
                    "has_go_workspace": bool(key.get("go_workspace_id")),
                    "has_go_cookie": bool(key.get("go_auth_cookie")),
                    "enabled": key.get("enabled", True),
                    "alias": key.get("alias", ""),
                }
                for key in api_keys
            ],
            "count": len(api_keys),
            "routing": api_key_routing_getter(),
        }

    @app.post("/api/config/api-keys")
    async def update_api_keys_config(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        if "api_keys" in body:
            debug(
                f"  [config] API keys updated ({len(body['api_keys'])} keys)"
            )
            merged = merge_preserved_api_keys(
                body["api_keys"],
                replace=bool(body.get("replace")),
            )
            save_api_keys(merged)
            try:
                import opencode as runtime

                runtime._rebuild_key_cache()
            except Exception as error:
                debug(f"  [config] key cache rebuild failed: {error}")
        if "routing" in body:
            save_env({"API_KEY_ROUTING": body["routing"]})
            try:
                import opencode as runtime

                runtime.API_KEY_ROUTING = str(body["routing"])
            except Exception:
                pass
        return {"status": "ok", "message": "API keys saved."}


__all__ = ["register_config_extra_routes"]
