"""NordVPN adapter: 200 / quota / AUTH, session cap, IP history."""

from __future__ import annotations

import pytest

from free.egress_adapters.nordvpn import NordVpnEgressAdapter
from free.egress_catalog import OptionStatus, Provider, ProviderPolicy
from free.egress_store import EgressStore
from free.egress_validation import ProbeOutcome


def _policy() -> ProviderPolicy:
    return ProviderPolicy(provider=Provider.NORDVPN, max_options=10, ttl_quota_max_s=600.0)


def _station(station: int, ip: str, **changes):
    base = {
        "station": f"vpn-gluetun-{station}",
        "server": f"server-{station}",
        "egress_ip": ip,
        "status": "connected",
    }
    base.update(changes)
    return base


@pytest.mark.asyncio
async def test_200_429_and_auth_are_distinct_states(tmp_path):
    stations = [
        _station(1, "1.2.3.4"),
        _station(2, "5.6.7.8"),
        _station(3, "9.10.11.12", status="auth_failed"),
    ]

    def probe(option):
        if option.egress_ip == "1.2.3.4":
            return ProbeOutcome(status=200, egress_ip="1.2.3.4")
        return ProbeOutcome(status=429, retry_after_s=300)

    store = EgressStore(tmp_path / "registry.db")
    try:
        result = await NordVpnEgressAdapter(stations, probe=probe).refresh(store, _policy())
        assert result.promoted == 1
        assert result.quarantined == 2
        statuses = {option.status for option in store.list_options()}
        assert statuses == {OptionStatus.USABLE, OptionStatus.QUOTA, OptionStatus.AUTH}
        assert store.list_options(status=OptionStatus.AUTH)[0].egress_ip == "9.10.11.12"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_auth_failed_station_is_never_probed(tmp_path):
    called = []

    def probe(option):
        called.append(option.station)
        return ProbeOutcome(status=200, egress_ip=option.egress_ip)

    store = EgressStore(tmp_path / "registry.db")
    try:
        await NordVpnEgressAdapter(
            [_station(1, "1.2.3.4", status="auth_failed")], probe=probe
        ).refresh(store, _policy())
        assert called == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_probe_concurrency_is_bounded_by_policy(tmp_path):
    import asyncio

    stations = [_station(index, f"1.2.3.{index}") for index in range(1, 5)]
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
        policy = ProviderPolicy(provider=Provider.NORDVPN, concurrency=2)
        await NordVpnEgressAdapter(stations, probe=probe).refresh(store, policy)
        assert peak == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_session_cap_observes_more_ips_than_it_probes(tmp_path):
    stations = [_station(index, f"1.2.3.{index}") for index in range(1, 5)]
    called = []

    def probe(option):
        called.append(option.egress_ip)
        return ProbeOutcome(status=200, egress_ip=option.egress_ip)

    store = EgressStore(tmp_path / "registry.db")
    try:
        result = await NordVpnEgressAdapter(
            stations, probe=probe, max_active_stations=1
        ).refresh(store, _policy())
        assert called == ["1.2.3.1"]
        assert result.promoted == 1
        assert store.count_provider(Provider.NORDVPN) == 4
        assert len(store.list_options(status=OptionStatus.REACHABLE)) == 3
    finally:
        store.close()


@pytest.mark.asyncio
async def test_station_without_egress_is_dead(tmp_path):
    store = EgressStore(tmp_path / "registry.db")
    try:
        await NordVpnEgressAdapter([_station(1, "")]).refresh(store, _policy())
        assert store.list_options(status=OptionStatus.DEAD)
    finally:
        store.close()
