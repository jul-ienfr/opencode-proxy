"""Egress capacity advisor routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request


def register_capacity_routes(
    app: FastAPI,
    *,
    config_getter: Callable[[], dict[str, Any]],
    capacity_counts_getter: Callable[[], Any],
    observed_counts_getter: Callable[[], Any],
    endpoint_observed_getter: Callable[[], Any],
    endpoint_inventory_getter: Callable[[], Any],
    token_checker: Callable[[Request], Any],
    persist_config: Callable[[dict[str, Any]], str | None],
    audit: Callable[[str, dict[str, Any]], Any],
) -> None:
    @app.get("/api/egress/capacity")
    async def get_egress_capacity():
        from free.auto_egress import providers_from_mode
        from free.capacity import capacity_config, evaluate_capacity

        cfg = config_getter() or {}
        selected = cfg.get("egress_providers")
        if not isinstance(selected, list) or not selected:
            selected = providers_from_mode(str(cfg.get("proxy_mode") or "vpn"))
        strategy = str(cfg.get("egress_strategy") or "fallback")
        raw_weights = cfg.get("egress_weights")
        weights = dict(raw_weights) if isinstance(raw_weights, dict) else {}
        target, policy = capacity_config(cfg.get("egress_capacity"))
        report = evaluate_capacity(
            providers=capacity_counts_getter(),
            selected=list(selected),
            strategy=strategy,
            weights=weights,
            target_slots=target,
            reserve_policy=policy,
            observed=observed_counts_getter(),
            endpoint_observed=endpoint_observed_getter(),
        )
        payload = report.to_dict()
        payload["endpoints"] = endpoint_inventory_getter()
        return payload

    @app.post("/api/egress/capacity")
    async def set_egress_capacity(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        updates: dict[str, int | str] = {}
        if "target_slots" in body:
            try:
                target = int(body.get("target_slots"))
            except (TypeError, ValueError):
                return {"error": "target_slots doit être un entier entre 1 et 10"}
            if not (1 <= target <= 10):
                return {"error": "target_slots doit être entre 1 et 10"}
            updates["target_slots"] = target
        if "reserve_policy" in body:
            policy = str(body.get("reserve_policy") or "").strip().lower()
            if policy not in ("full", "shared", "proportional"):
                return {
                    "error": "reserve_policy doit être full|shared|proportional"
                }
            updates["reserve_policy"] = policy
        if not updates:
            return {"error": "target_slots ou reserve_policy requis"}
        err = persist_config({"egress_capacity": updates})
        if err:
            return {"error": err}
        audit("capacity_config", {"egress_capacity": dict(updates)})
        return {"ok": True, "egress_capacity": updates}


__all__ = ["register_capacity_routes"]
