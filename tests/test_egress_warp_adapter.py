"""WARP adapter: one option per /64, recreated container expires the old block."""

from __future__ import annotations

import pytest

from free.egress_adapters.warp import WarpEgressAdapter
from free.egress_catalog import OptionStatus, Provider, ProviderPolicy
from free.egress_store import EgressStore
from free.egress_validation import ProbeOutcome


def _policy() -> ProviderPolicy:
    return ProviderPolicy(provider=Provider.WARP, max_options=24, ttl_dead_s=900.0)


@pytest.mark.asyncio
async def test_two_blocks_one_usable_one_quota(tmp_path):
    containers = [
        {"container": "warp-1", "egress_ip": "2a09:bac5:1::1", "block": "2a09:bac5:1::/64"},
        {"container": "warp-2", "egress_ip": "2a09:bac5:2::1", "block": "2a09:bac5:2::/64"},
    ]

    def probe(option):
        if option.station == "warp-1":
            return ProbeOutcome(status=200, egress_ip=option.egress_ip)
        return ProbeOutcome(status=429, retry_after_s=60)

    store = EgressStore(tmp_path / "registry.db")
    try:
        result = await WarpEgressAdapter(containers, probe=probe).refresh(store, _policy())
        assert result.promoted == 1
        assert result.quarantined == 1
        assert {option.status for option in store.list_options()} == {
            OptionStatus.USABLE,
            OptionStatus.QUOTA,
        }
    finally:
        store.close()


@pytest.mark.asyncio
async def test_two_containers_on_one_block_are_one_option(tmp_path):
    containers = [
        {"container": "warp-1", "egress_ip": "2a09:bac5:1::1", "block": "2a09:bac5:1::/64"},
        {"container": "warp-2", "egress_ip": "2a09:bac5:1::2", "block": "2a09:bac5:1::/64"},
    ]

    store = EgressStore(tmp_path / "registry.db")
    try:
        await WarpEgressAdapter(containers).refresh(store, _policy())
        assert store.count_provider(Provider.WARP) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_healthy_container_without_probe_is_only_reachable(tmp_path):
    containers = [{"container": "warp-1", "egress_ip": "2a09:bac5:1::1", "block": "2a09:bac5:1::/64"}]
    store = EgressStore(tmp_path / "registry.db")
    try:
        await WarpEgressAdapter(containers).refresh(store, _policy())
        assert store.list_options(status=OptionStatus.USABLE) == []
        assert store.list_options(status=OptionStatus.REACHABLE)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_max_options_bounds_observed_containers(tmp_path):
    containers = [
        {"container": f"warp-{index}", "egress_ip": f"2a09:bac5:{index}::1", "block": f"2a09:bac5:{index}::/64"}
        for index in range(1, 5)
    ]
    store = EgressStore(tmp_path / "registry.db")
    try:
        policy = ProviderPolicy(provider=Provider.WARP, max_options=2)
        result = await WarpEgressAdapter(containers).refresh(store, policy)
        assert result.candidates == 2
        assert store.count_provider(Provider.WARP) == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_probe_concurrency_is_bounded_by_policy(tmp_path):
    import asyncio

    containers = [
        {"container": f"warp-{index}", "egress_ip": f"2a09:bac5:{index}::1", "block": f"2a09:bac5:{index}::/64"}
        for index in range(1, 5)
    ]
    active = 0
    peak = 0

    async def probe(option):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ProbeOutcome(status=200, egress_ip=option.egress_ip)

    store = EgressStore(tmp_path / "registry.db")
    try:
        policy = ProviderPolicy(provider=Provider.WARP, concurrency=2)
        await WarpEgressAdapter(containers, probe=probe).refresh(store, policy)
        assert peak == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_disabled_container_is_skipped(tmp_path):
    containers = [
        {"container": "warp-1", "egress_ip": "2a09:bac5:1::1", "block": "2a09:bac5:1::/64", "enabled": False}
    ]
    store = EgressStore(tmp_path / "registry.db")
    try:
        result = await WarpEgressAdapter(containers).refresh(store, _policy())
        assert result.candidates == 1
        assert store.list_options() == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_recreated_container_expires_the_old_block(tmp_path):
    store = EgressStore(tmp_path / "registry.db")
    try:
        first = [{"container": "warp-1", "egress_ip": "2a09:bac5:1::1", "block": "2a09:bac5:1::/64"}]
        await WarpEgressAdapter(
            first, probe=lambda option: ProbeOutcome(status=200, egress_ip=option.egress_ip)
        ).refresh(store, _policy())
        second = [{"container": "warp-1", "egress_ip": "2a09:bac5:9::1", "block": "2a09:bac5:9::/64"}]
        await WarpEgressAdapter(
            second, probe=lambda option: ProbeOutcome(status=200, egress_ip=option.egress_ip)
        ).refresh(store, _policy())
        old = store.get_by_egress(Provider.WARP, "2a09:bac5:1::1")
        new = store.get_by_egress(Provider.WARP, "2a09:bac5:9::1")
        assert old is not None and old.status == OptionStatus.DEAD
        assert new is not None and new.status == OptionStatus.USABLE
    finally:
        store.close()
