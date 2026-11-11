"""Rotation selection: hard gates first, distinct blocks, capped pool rows."""

from __future__ import annotations

from free.egress_catalog import Observation, ObservationPhase, OptionStatus, Provider, ProviderPolicy
from free.egress_rotation import public_pool_rows, rotation_plan, select_many, select_option
from free.egress_store import EgressStore


def _store(tmp_path):
    return EgressStore(tmp_path / "registry.db")


def _add(store, policy, *, host, egress, status=OptionStatus.USABLE, latency=500.0, block=None):
    from free.egress_catalog import new_option

    option = new_option(
        provider=Provider.PUBLIC,
        kind="https",
        host=host,
        port=443,
        egress_ip=egress,
        block=block or "",
    )
    return store.apply(
        option,
        Observation(
            phase=ObservationPhase.FREE_GATE,
            status=200 if status == OptionStatus.USABLE else 429,
            result=status,
            observed_at=1_000.0,
            latency_ms=latency,
        ),
        policy,
        reset_at=2_000.0 if status == OptionStatus.QUOTA else 0.0,
    )


def test_select_option_skips_quota_and_dead(tmp_path):
    store = _store(tmp_path)
    policy = ProviderPolicy(provider=Provider.PUBLIC, ttl_usable_s=10_000.0)
    try:
        _add(store, policy, host="1.2.3.4", egress="1.2.3.4", status=OptionStatus.QUOTA)
        _add(store, policy, host="5.6.7.8", egress="5.6.7.8")
        selected = select_option(store, policy, now=1_100.0)
        assert selected is not None
        assert selected.egress_ip == "5.6.7.8"
    finally:
        store.close()


def test_select_many_returns_distinct_blocks_only(tmp_path):
    store = _store(tmp_path)
    policy = ProviderPolicy(provider=Provider.PUBLIC, ttl_usable_s=10_000.0)
    try:
        _add(store, policy, host="1.2.3.4", egress="1.2.3.4", block="1.2.3.0/24")
        _add(store, policy, host="1.2.3.5", egress="1.2.3.5", block="1.2.3.0/24")
        _add(store, policy, host="5.6.7.8", egress="5.6.7.8", block="5.6.7.0/24")
        selected = select_many(store, policy, count=10, now=1_100.0)
        assert [option.block for option in selected] == ["1.2.3.0/24", "5.6.7.0/24"]
    finally:
        store.close()


def test_public_pool_rows_are_ready_for_the_worker(tmp_path):
    store = _store(tmp_path)
    policy = ProviderPolicy(provider=Provider.PUBLIC, max_active=2, ttl_usable_s=10_000.0)
    try:
        _add(store, policy, host="1.2.3.4", egress="1.2.3.4")
        _add(store, policy, host="5.6.7.8", egress="5.6.7.8")
        rows = public_pool_rows(store, policy, max_active=2, now=1_100.0)
        assert len(rows) == 2
        assert rows[0]["url"].startswith("https://")
        assert rows[0]["provider"] == "public"
        assert rows[0]["last_status"] == 200
        assert all(row["egress_ip"] for row in rows)
    finally:
        store.close()


def test_rotation_plan_reports_empty_provider(tmp_path):
    store = _store(tmp_path)
    policy = ProviderPolicy(provider=Provider.NORDVPN)
    try:
        plan = rotation_plan(store, [policy], now=1_000.0)
        assert plan[Provider.NORDVPN]["status"] == "empty"
    finally:
        store.close()
