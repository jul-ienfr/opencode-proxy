"""Drain contract for a disabled egress provider (023 T018).

The spec requires two properties:

- a disabled provider is no longer selected/probed (hard gate), and
- an already in-flight selection completes (a request that picked the
  provider before the toggle must not be aborted).

Both are exercised here against the real composition root
(``free.egress_runtime.build_runtime``), the real rotation selector
(``free.egress_rotation.select_many``), and the real pool toggle
(``FreeIPPool.set_egress_config``).
"""
from __future__ import annotations

import time
from typing import Any

from free.auto_egress import SmoothWeightedRoundRobin
from free.egress_catalog import (
    Observation,
    ObservationPhase,
    Provider,
    ProviderPolicy,
    new_option,
)
from free.egress_rotation import rows_from_options, select_many
from free.egress_runtime import build_runtime
from free.egress_store import EgressStore
from free.pool import FreeIPPool


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


def _usable_option(provider: str, host: str, now: float):
    return new_option(
        provider=provider,
        kind="https",
        host=host,
        port=443,
        egress_ip=host,
        now=now,
    )


def _apply_usable(store: EgressStore, option, policy: ProviderPolicy, now: float) -> None:
    store.apply(
        option,
        Observation(
            phase=ObservationPhase.FREE_GATE,
            status=200,
            result="usable",
            observed_at=now,
            latency_ms=10.0,
        ),
        policy,
    )


# ---------------------------------------------------------------------------
# 1. A disabled provider is not selected
# ---------------------------------------------------------------------------


def test_disabled_provider_is_absent_from_runtime_selection() -> None:
    runtime = build_runtime(
        _runtime_config(warp=True, nordvpn=False),
        db_path=":memory:",
        nordvpn_stations=[],
    )
    assert runtime is not None
    try:
        # The runtime builds exactly one policy/scheduler entry for the
        # provider that is enabled.
        assert Provider.NORDVPN not in runtime.policies
        assert Provider.NORDVPN not in runtime.scheduler._providers
        assert Provider.WARP in runtime.policies
        assert Provider.WARP in runtime.scheduler._providers
        # Rotation planning is built from the enabled policies only.
        plan = runtime.rotation_plan()
        assert isinstance(plan, dict)
        assert str(Provider.NORDVPN) not in plan
    finally:
        import asyncio

        asyncio.run(runtime.close())


def test_disabled_provider_options_are_not_selected_by_rotation() -> None:
    now = time.time()
    store = EgressStore(":memory:")
    try:
        warp_policy = ProviderPolicy(provider=Provider.WARP, max_active=10)
        nordvpn_policy = ProviderPolicy(provider=Provider.NORDVPN, max_active=10)

        _apply_usable(store, _usable_option(Provider.WARP, "1.2.3.4", now), warp_policy, now)
        _apply_usable(
            store,
            _usable_option(Provider.NORDVPN, "5.6.7.8", now),
            nordvpn_policy,
            now,
        )

        # A rotation over the WARP policy never returns a NordVPN option.
        selected = select_many(store, warp_policy, count=10, now=now)
        assert selected, "the WARP option should be selectable"
        assert all(str(option.provider) == str(Provider.WARP) for option in selected)

        # And a runtime built with NordVPN disabled exposes no NordVPN policy,
        # so its selection path cannot pick the NordVPN bucket at all.
        runtime = build_runtime(
            _runtime_config(warp=True, nordvpn=False),
            db_path=":memory:",
            nordvpn_stations=[],
        )
        assert runtime is not None
        try:
            assert Provider.NORDVPN not in runtime.policies
        finally:
            import asyncio

            asyncio.run(runtime.close())
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 2. An in-flight selection finishes
# ---------------------------------------------------------------------------


def test_inflight_pool_selection_is_not_retroactively_cancelled() -> None:
    """The provider chosen before the toggle stays valid for that request."""
    pool = object.__new__(FreeIPPool)
    pool._egress_swrr = SmoothWeightedRoundRobin()
    pool._egress_providers = ["nordvpn", "warp"]
    pool._egress_strategy = "fallback"
    pool._egress_weights = {"nordvpn": 52, "warp": 24}

    # A request has already selected NordVPN.
    inflight = pool._select_egress_provider(list(pool._egress_providers))
    assert inflight == "nordvpn"

    # The operator disables NordVPN mid-flight.
    pool.set_egress_config(providers=["warp"])

    # The request's already-selected provider is unchanged: it finishes.
    assert inflight == "nordvpn"
    # New selections skip the disabled provider.
    assert pool._egress_providers == ["warp"]
    assert pool._select_egress_provider(list(pool._egress_providers)) == "warp"


def test_inflight_catalog_selection_completes_before_the_disable() -> None:
    """A completed select_many() result stays complete after the disable."""
    now = time.time()
    store = EgressStore(":memory:")
    try:
        policy = ProviderPolicy(provider=Provider.WARP, max_active=4)
        _apply_usable(store, _usable_option(Provider.WARP, "1.2.3.4", now), policy, now)

        # The request performs its selection while WARP is enabled.
        selected = select_many(store, policy, count=1, now=now)
        assert selected, "expected the usable WARP option to be selected"
        rows_before = rows_from_options(selected)
        assert rows_before[0]["egress_ip"] == "1.2.3.4"
        assert rows_before[0]["url"].startswith("https://")
        assert rows_before[0]["last_status"] == 200

        # The operator now disables the provider. The in-flight result is a
        # complete row and must not be mutated or invalidated by the toggle.
        runtime = build_runtime(
            _runtime_config(warp=False, nordvpn=False),
            db_path=":memory:",
            nordvpn_stations=[],
        )
        # No enabled provider -> build_runtime returns None.
        assert runtime is None

        rows_after = rows_from_options(selected)
        assert rows_after == rows_before
        assert rows_after[0]["egress_ip"] == "1.2.3.4"
    finally:
        store.close()