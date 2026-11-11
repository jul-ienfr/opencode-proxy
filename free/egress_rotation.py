"""Rotation selection over the egress catalog.

Hard gates first (provider, status, expiry, provider reset, unique block),
then the constitution IX score orders the survivors.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from free.egress_catalog import (
    EgressOption,
    Provider,
    ProviderPolicy,
    is_selectable,
    score_option,
)
from free.egress_store import EgressStore


def select_option(
    store: EgressStore,
    policy: ProviderPolicy,
    *,
    now: float | None = None,
    claimed_blocks: set[str] | None = None,
    limit: int | None = None,
) -> EgressOption | None:
    ts = time.time() if now is None else float(now)
    candidates = store.selectable(policy, now=ts, limit=limit or max(8, policy.max_active * 4))
    eligible = [
        option
        for option in candidates
        if is_selectable(option, ts, policy, claimed_blocks=claimed_blocks)
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda option: score_option(option, ts, policy))


def select_many(
    store: EgressStore,
    policy: ProviderPolicy,
    *,
    count: int,
    now: float | None = None,
) -> list[EgressOption]:
    """Pick up to ``count`` distinct blocks, best score first."""

    ts = time.time() if now is None else float(now)
    candidates = store.selectable(policy, now=ts, limit=max(count * 8, 64))
    eligible = [
        option
        for option in candidates
        if is_selectable(option, ts, policy)
    ]
    eligible.sort(key=lambda option: score_option(option, ts, policy), reverse=True)
    selected: list[EgressOption] = []
    blocks: set[str] = set()
    for option in eligible:
        unit = option.block or option.egress_ip or option.key
        if unit in blocks:
            continue
        blocks.add(unit)
        selected.append(option)
        if len(selected) >= max(1, int(count)):
            break
    return selected


def public_pool_rows(
    store: EgressStore,
    policy: ProviderPolicy,
    *,
    max_active: int | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Rows accepted by ``FreeIPPool.set_public_proxies``.

    The worker is fed only catalog-proven ``usable`` options.  The catalog
    keeps the rest; the active pool stays capped.
    """

    cap = max(1, int(max_active or policy.max_active or 1))
    selected = select_many(store, policy, count=cap, now=now)
    return rows_from_options(selected)


def rows_from_options(options: Iterable[EgressOption]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for option in options:
        if not option.host or not option.port:
            continue
        scheme = option.kind if option.kind in ("http", "https", "socks5", "socks5h") else "https"
        host = f"[{option.host}]" if ":" in option.host else option.host
        rows.append(
            {
                "url": f"{scheme}://{host}:{int(option.port)}",
                "egress_ip": option.egress_ip,
                "block": option.block,
                "provider": "public",
                "label": option.key,
                "last_status": int(option.last_status or 200),
                "last_checked_at": float(option.last_checked_at or 0.0),
                "score": float(option.score or 0.0),
            }
        )
    return rows


def provider_status(store: EgressStore, provider: str) -> dict[str, Any]:
    return dict(store.summary().get(str(provider)) or {})


def rotation_plan(
    store: EgressStore,
    policies: Iterable[ProviderPolicy],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    ts = time.time() if now is None else float(now)
    plan: dict[str, Any] = {}
    for policy in policies:
        option = select_option(store, policy, now=ts)
        plan[policy.provider] = {
            "selected": option.key if option else "",
            "egress_ip": option.egress_ip if option else "",
            "block": option.block if option else "",
            "score": score_option(option, ts, policy) if option else 0.0,
            "status": option.status if option else "empty",
        }
    return plan


__all__ = [
    "Provider",
    "provider_status",
    "public_pool_rows",
    "rotation_plan",
    "rows_from_options",
    "select_many",
    "select_option",
]
