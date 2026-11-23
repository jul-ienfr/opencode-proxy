"""Activation/désactivation d'un provider sans redémarrage (023 T016).

Couvre les deux chemins réels de la bascule :

1. le pool (`FreeIPPool.set_egress_config`) utilisé par le dashboard et par
   ``POST /api/egress/config`` — la sélection change immédiatement ;
2. le runtime du catalogue (`free.egress_runtime.build_runtime`) — un provider
   désactivé n'est ni planifié ni sondé, même après le délai de drain ; le
   réactiver le réintroduit.

Aucune API inventée : ``set_egress_config`` et ``build_runtime`` existent et
sont testés ici avec de vrais objets.
"""
from __future__ import annotations

from typing import Any

import pytest

from free.auto_egress import SmoothWeightedRoundRobin
from free.egress_catalog import Provider
from free.egress_runtime import build_runtime
from free.pool import FreeIPPool


def _make_pool(providers: list[str]) -> FreeIPPool:
    """Minimal pool instance with only the egress-selection state wired."""
    pool = object.__new__(FreeIPPool)
    pool._egress_swrr = SmoothWeightedRoundRobin()
    pool._egress_providers = list(providers)
    pool._egress_strategy = "fallback"
    pool._egress_weights = {"nordvpn": 52, "warp": 24, "proxies": 24, "direct": 0}
    return pool


def _runtime_config(*, warp: bool, nordvpn: bool) -> dict[str, Any]:
    return {
        "egress_catalog": {
            "enabled": True,
            "providers": {
                "public": {"enabled": False},
                "warp": {"enabled": warp},
                "nordvpn": {"enabled": nordvpn},
            },
        }
    }


# ---------------------------------------------------------------------------
# Pool-level toggle (the dashboard / POST /api/egress/config path)
# ---------------------------------------------------------------------------


def test_pool_disable_provider_is_immediate() -> None:
    pool = _make_pool(["nordvpn", "warp"])
    assert pool._select_egress_provider(list(pool._egress_providers)) == "nordvpn"

    pool.set_egress_config(providers=["warp"])

    assert pool._egress_providers == ["warp"]
    assert pool._select_egress_provider(list(pool._egress_providers)) == "warp"


def test_pool_reenable_provider_restores_selection() -> None:
    pool = _make_pool(["warp"])
    assert pool._select_egress_provider(list(pool._egress_providers)) == "warp"

    pool.set_egress_config(providers=["nordvpn", "warp"])

    assert pool._egress_providers == ["nordvpn", "warp"]
    # fallback: the first provider in the configured order wins again.
    assert pool._select_egress_provider(list(pool._egress_providers)) == "nordvpn"


# ---------------------------------------------------------------------------
# Runtime-level toggle (build_runtime composition)
# ---------------------------------------------------------------------------


def test_runtime_contains_enabled_providers() -> None:
    runtime = build_runtime(
        _runtime_config(warp=True, nordvpn=True),
        db_path=":memory:",
        nordvpn_stations=[],
    )
    assert runtime is not None
    try:
        assert Provider.WARP in runtime.scheduler._providers
        assert Provider.NORDVPN in runtime.scheduler._providers
        assert Provider.WARP in runtime.policies
        assert Provider.NORDVPN in runtime.policies
    finally:
        import asyncio

        asyncio.run(runtime.close())


def test_runtime_excludes_disabled_provider_immediately() -> None:
    runtime = build_runtime(
        _runtime_config(warp=False, nordvpn=True),
        db_path=":memory:",
        nordvpn_stations=[],
    )
    assert runtime is not None
    try:
        assert Provider.WARP not in runtime.scheduler._providers
        assert Provider.WARP not in runtime.policies
        assert Provider.NORDVPN in runtime.scheduler._providers
        assert Provider.NORDVPN in runtime.policies
    finally:
        import asyncio

        asyncio.run(runtime.close())


@pytest.mark.asyncio
async def test_disabled_provider_is_not_probed_after_drain_s() -> None:
    """A disabled provider is never scheduled, even after the drain window."""
    drain_s = 60.0
    clock_value = [1_000_000.0]

    runtime = build_runtime(
        _runtime_config(warp=False, nordvpn=True),
        db_path=":memory:",
        nordvpn_stations=[],
    )
    assert runtime is not None
    try:
        # Inject a fake clock: the scheduler is not started, but its clock
        # drives next_run_at and any explicit probe.
        runtime.scheduler._clock = lambda: clock_value[0]

        assert Provider.WARP not in runtime.scheduler._providers
        # Advance past the documented drain window (spec: < 60 s).
        clock_value[0] += drain_s
        assert Provider.WARP not in runtime.scheduler._providers
        # Explicit probe requests must not find the provider either.
        assert await runtime.refresh_now("warp") is None

        # The enabled provider remains scheduled.
        assert Provider.NORDVPN in runtime.scheduler._providers
    finally:
        await runtime.close()


def test_runtime_reenabling_restores_provider() -> None:
    disabled = build_runtime(
        _runtime_config(warp=False, nordvpn=True),
        db_path=":memory:",
        nordvpn_stations=[],
    )
    enabled = build_runtime(
        _runtime_config(warp=True, nordvpn=True),
        db_path=":memory:",
        nordvpn_stations=[],
    )
    assert disabled is not None and enabled is not None
    try:
        assert Provider.WARP not in disabled.scheduler._providers
        assert Provider.WARP in enabled.scheduler._providers
        assert Provider.WARP in enabled.policies
    finally:
        import asyncio

        asyncio.run(disabled.close())
        asyncio.run(enabled.close())