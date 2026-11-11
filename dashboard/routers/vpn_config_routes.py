"""Read-only VPN config and stack-info routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI


def shared_vpn_manager() -> Any:
    import shared_state

    return getattr(shared_state, "vpn_manager", None)


def build_vpn_config(
    manager: Any,
    *,
    rotation_config: dict[str, Any],
    quality_blocks: tuple[str, ...],
    free_policy_getter: Callable[[], Any],
) -> dict[str, Any]:
    if manager is None:
        return {
            "enabled": False,
            "servers": [],
            "auth_file": "",
            "protocol": "udp",
            "free_substitution_policy": free_policy_getter(),
        }
    config = manager.get_config()
    try:
        for block in quality_blocks:
            if isinstance(rotation_config.get(block), dict):
                config[block] = dict(rotation_config[block])
        from free.auto_egress import providers_from_mode

        providers = rotation_config.get("egress_providers")
        if not isinstance(providers, list) or not providers:
            providers = providers_from_mode(str(config.get("proxy_mode") or "vpn"))
        config["egress_providers"] = list(providers)
        config["egress_strategy"] = str(
            rotation_config.get("egress_strategy") or "fallback"
        )
        weights = rotation_config.get("egress_weights")
        config["egress_weights"] = dict(weights) if isinstance(weights, dict) else {}
    except Exception:
        pass
    return config


def build_vpn_stack_info(managers: list[Any]) -> dict[str, Any]:
    return {
        "stations": {
            str(manager._station): manager.stack_info() for manager in managers
        }
    }


def register_vpn_config_read_routes(
    app: FastAPI,
    *,
    manager_getter: Callable[[], Any],
    managers_getter: Callable[[], list[Any]],
    rotation_config_getter: Callable[[], dict[str, Any]],
    free_policy_getter: Callable[[], Any],
    quality_blocks: tuple[str, ...],
) -> None:
    @app.get("/api/vpn-config")
    async def get_vpn_config():
        return build_vpn_config(
            manager_getter(),
            rotation_config=rotation_config_getter() or {},
            quality_blocks=quality_blocks,
            free_policy_getter=free_policy_getter,
        )

    @app.get("/api/vpn-stack-info")
    async def get_vpn_stack_info():
        return build_vpn_stack_info(managers_getter())


__all__ = [
    "build_vpn_config",
    "build_vpn_stack_info",
    "register_vpn_config_read_routes",
    "shared_vpn_manager",
]
