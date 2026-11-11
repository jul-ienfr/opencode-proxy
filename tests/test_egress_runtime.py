"""Composition root: every egress_catalog key has a runtime consumer."""

from __future__ import annotations

import pytest

from free.egress_catalog import Observation, ObservationPhase, Provider
from free.egress_runtime import build_runtime
from free.egress_store import EgressStore


def _config(**overrides):
    public = {
        "enabled": True,
        "discovery_interval_s": 21_600,
        "revalidate_interval_s": 900,
        "max_active": 4,
        "max_options": 100,
        "concurrency": 2,
        "ttl_usable_s": 43_200,
        "ttl_quota_max_s": 86_400,
        "ttl_dead_s": 604_800,
        "sources": [
            {
                "id": "list-a",
                "type": "public_text",
                "url": "https://example.test/list.txt",
                "enabled": True,
            }
        ],
    }
    config = {
        "egress_catalog": {
            "enabled": True,
            "update_jitter_pct": 15,
            "retention_days": 7,
            "providers": {
                "public": {**public, **overrides.get("public", {})},
                "warp": {
                    "enabled": True,
                    "discovery_interval_s": 600,
                    "max_active": 4,
                    "max_options": 10,
                    "concurrency": 2,
                    "ttl_usable_s": 43_200,
                    "ttl_quota_max_s": 86_400,
                    "ttl_dead_s": 604_800,
                },
                "nordvpn": {
                    "enabled": True,
                    "discovery_interval_s": 1200,
                    "max_active": 2,
                    "max_options": 10,
                    "max_active_stations": 2,
                    "concurrency": 2,
                    "ttl_usable_s": 43_200,
                    "ttl_quota_max_s": 86_400,
                    "ttl_dead_s": 604_800,
                },
            },
        },
        "ip_rotation": {"public_proxies": {"max_per_24": 2, "isolation": "process"}},
    }
    return config


def test_disabled_catalog_returns_none(tmp_path):
    config = _config()
    config["egress_catalog"]["enabled"] = False
    assert build_runtime(config, db_path=tmp_path / "registry.db") is None


@pytest.mark.asyncio
async def test_enabled_catalog_builds_scheduler_with_configured_intervals(tmp_path):
    runtime = build_runtime(
        _config(),
        db_path=tmp_path / "registry.db",
        nordvpn_stations=[{"station": "s1", "status": "connected", "egress_ip": "1.2.3.4"}],
    )
    assert runtime is not None
    try:
        assert runtime.scheduler._providers[Provider.PUBLIC].interval_s == 900
        assert runtime.scheduler._providers[Provider.WARP].interval_s == 600
        assert runtime.scheduler._providers[Provider.NORDVPN].interval_s == 1200
        assert runtime.scheduler._jitter == 0.15
        assert runtime.policies[Provider.PUBLIC].max_active == 4
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_sources_file_is_loaded(tmp_path):
    source_file = tmp_path / "sources.yaml"
    source_file.write_text(
        "sources:\n"
        "  - id: low\n    type: public_text\n    url: https://example.test/low.txt\n    priority: 1\n"
        "  - id: high\n    type: public_text\n    url: https://example.test/high.txt\n    priority: 99\n",
        encoding="utf-8",
    )
    config = _config()
    config["egress_catalog"]["providers"]["public"].pop("sources", None)
    config["egress_catalog"]["providers"]["public"]["sources_file"] = str(source_file)
    runtime = build_runtime(config, db_path=tmp_path / "registry.db", nordvpn_stations=[])
    assert runtime is not None
    try:
        adapter = runtime.scheduler._providers[Provider.PUBLIC].adapter
        assert [source.id for source in adapter.sources] == ["high", "low"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_disabled_provider_is_not_built(tmp_path):
    config = _config()
    config["egress_catalog"]["providers"]["warp"]["enabled"] = False
    config["egress_catalog"]["providers"]["nordvpn"]["enabled"] = False
    runtime = build_runtime(config, db_path=tmp_path / "registry.db")
    assert runtime is not None
    try:
        assert set(runtime.scheduler._providers) == {Provider.PUBLIC}
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_threat_feeds_enabled_builds_runtime(tmp_path):
    config = _config()
    config["egress_catalog"]["threat_feeds"] = {
        "enabled": True,
        "refresh_interval_s": 3600,
        "urls": [{"id": "feed-a", "url": "https://feed-a", "enabled": True}],
    }
    runtime = build_runtime(config, db_path=tmp_path / "registry.db", nordvpn_stations=[])
    assert runtime is not None
    try:
        assert runtime.threat_intel is not None
        assert runtime.threat_intel.refresh_interval_s == 3600
        assert [feed.id for feed in runtime.threat_intel.feeds] == ["feed-a"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_public_discovery_is_cached_between_revalidations(tmp_path):
    from free.egress_adapters.public import PublicEgressAdapter
    from free.egress_sources import SourceDefinition

    calls = []

    def fetcher(url, timeout):
        calls.append(url)
        return "1.2.3.4:443"

    async def probe(candidates, **kwargs):
        return {}

    adapter = PublicEgressAdapter(
        sources=[SourceDefinition(id="a", type="public_text", url="https://example.test")],
        fetcher=fetcher,
        probe=probe,
        discovery_interval_s=3600,
    )
    store = EgressStore(tmp_path / "registry.db")
    try:
        from free.egress_catalog import ProviderPolicy

        policy = ProviderPolicy(provider=Provider.PUBLIC)
        await adapter.refresh(store, policy)
        await adapter.refresh(store, policy)
        assert calls == ["https://example.test"]
        adapter._source_last.clear()
        await adapter.refresh(store, policy)
        assert calls == ["https://example.test", "https://example.test"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_maintenance_purges_and_exports(tmp_path):
    runtime = build_runtime(_config(), db_path=tmp_path / "registry.db", nordvpn_stations=[])
    assert runtime is not None
    try:
        import time

        from free.egress_catalog import OptionStatus, new_option

        old = time.time() - 31 * 86_400
        runtime.store.apply(
            new_option(
                provider=Provider.PUBLIC,
                kind="https",
                host="8.8.8.8",
                port=443,
                egress_ip="8.8.8.8",
            ),
            Observation(
                phase=ObservationPhase.LIVENESS,
                status=0,
                result=OptionStatus.DEAD,
                observed_at=old,
            ),
            runtime.policies[Provider.PUBLIC],
        )
        result = runtime.maintenance()
        assert result["options"] == 1
        assert (tmp_path / "egress_catalog.json").is_file()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_block_indicator_retires_matching_options(tmp_path):
    runtime = build_runtime(_config(), db_path=tmp_path / "registry.db", nordvpn_stations=[])
    assert runtime is not None
    try:
        import time

        from free.egress_catalog import OptionStatus, new_option

        option = new_option(
            provider=Provider.PUBLIC,
            kind="https",
            host="9.9.9.9",
            port=443,
            egress_ip="9.9.9.9",
        )
        runtime.store.apply(
            option,
            Observation(
                phase=ObservationPhase.FREE_GATE,
                status=200,
                result="usable",
                observed_at=time.time(),
            ),
            runtime.policies[Provider.PUBLIC],
        )
        result = runtime.block_indicator("9.9.9.9")
        assert result["matched"] == 1
        loaded = runtime.store.get_by_egress(Provider.PUBLIC, "9.9.9.9")
        assert loaded is not None and loaded.status == OptionStatus.DEAD
        assert runtime.public_rows() == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_clean_empty_cycle_clears_the_public_pool(tmp_path):
    class _Pool:
        def __init__(self):
            self.cleared = False

        def clear_public_proxies(self):
            self.cleared = True

    runtime = build_runtime(_config(), db_path=tmp_path / "registry.db", nordvpn_stations=[])
    assert runtime is not None
    try:
        pool = _Pool()
        assert runtime.sync_public_pool(pool, clear_if_empty=False) == 0
        assert pool.cleared is False
        assert runtime.sync_public_pool(pool, clear_if_empty=True) == 0
        assert pool.cleared is True
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_public_rows_never_include_a_denylisted_option(tmp_path):
    config = _config()
    config["egress_catalog"]["denylist"] = ["1.2.3.4/32"]
    runtime = build_runtime(config, db_path=tmp_path / "registry.db", nordvpn_stations=[])
    assert runtime is not None
    try:
        import time

        from free.egress_catalog import new_option

        runtime.store.apply(
            new_option(
                provider=Provider.PUBLIC,
                kind="https",
                host="1.2.3.4",
                port=443,
                egress_ip="1.2.3.4",
            ),
            Observation(
                phase=ObservationPhase.FREE_GATE,
                status=200,
                result="usable",
                observed_at=time.time(),
            ),
            runtime.policies[Provider.PUBLIC],
        )
        assert runtime.public_rows() == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_start_applies_the_catalog_to_the_pool_immediately(tmp_path):
    class _Pool:
        def __init__(self):
            self.rows = []

        def set_public_proxies(self, rows, **kwargs):
            self.rows = list(rows)
            return len(self.rows)

    pool = _Pool()
    runtime = build_runtime(
        _config(),
        db_path=tmp_path / "registry.db",
        nordvpn_stations=[],
        pool_getter=lambda: pool,
    )
    assert runtime is not None
    try:
        import time

        from free.egress_catalog import new_option

        runtime.store.apply(
            new_option(
                provider=Provider.PUBLIC,
                kind="https",
                host="89.207.188.137",
                port=443,
                egress_ip="89.207.188.137",
            ),
            Observation(
                phase=ObservationPhase.FREE_GATE,
                status=200,
                result="usable",
                observed_at=time.time(),
            ),
            runtime.policies[Provider.PUBLIC],
        )
        await runtime.start()
        assert [row["egress_ip"] for row in pool.rows] == ["89.207.188.137"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_sync_public_pool_feeds_only_usable_options(tmp_path):
    runtime = build_runtime(
        _config(),
        db_path=tmp_path / "registry.db",
        nordvpn_stations=[],
    )
    assert runtime is not None

    class _Pool:
        def __init__(self):
            self.rows = []

        def set_public_proxies(self, rows, **kwargs):
            self.rows = list(rows)
            return len(self.rows)

    try:
        from free.egress_catalog import new_option

        option = new_option(
            provider=Provider.PUBLIC,
            kind="https",
            host="1.2.3.4",
            port=443,
            egress_ip="1.2.3.4",
        )
        now = __import__("time").time()
        runtime.store.apply(
            option,
            Observation(
                phase=ObservationPhase.FREE_GATE,
                status=200,
                result="usable",
                observed_at=now,
            ),
            runtime.policies[Provider.PUBLIC],
        )
        pool = _Pool()
        assert runtime.sync_public_pool(pool) == 1
        assert pool.rows[0]["egress_ip"] == "1.2.3.4"
    finally:
        await runtime.close()
