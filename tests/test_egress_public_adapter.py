"""Public adapter: 200 promotes, 429 quarantines, dead expires, egress dedupe."""

from __future__ import annotations

import pytest

from free.egress_adapters.public import CandidateProbe, PublicEgressAdapter
from free.egress_catalog import Observation, ObservationPhase, OptionStatus, Provider, ProviderPolicy
from free.egress_sources import SourceDefinition
from free.egress_store import EgressStore


def _source(**changes) -> SourceDefinition:
    base = {
        "id": "list-a",
        "type": "public_text",
        "url": "https://example.test/list.txt",
        "enabled": True,
    }
    base.update(changes)
    return SourceDefinition(**base)


def _adapter(probe, *, sources=None, fetcher=None) -> PublicEgressAdapter:
    return PublicEgressAdapter(
        sources=sources or [_source()],
        fetcher=fetcher or (lambda url, timeout: ""),
        probe=probe,
        concurrency=2,
        timeout=2,
    )


@pytest.mark.asyncio
async def test_three_candidates_produce_one_usable_one_quota_one_dead(tmp_path):
    text = "\n".join(["1.2.3.4:443", "5.6.7.8:443", "9.10.11.12:443"])
    outcomes = {
        "http://1.2.3.4:443": CandidateProbe(
            url="http://1.2.3.4:443", egress_ip="1.2.3.4", liveness_status=200, free_status=200
        ),
        "http://5.6.7.8:443": CandidateProbe(
            url="http://5.6.7.8:443",
            egress_ip="5.6.7.8",
            liveness_status=200,
            free_status=429,
            retry_after_s=120,
        ),
        "http://9.10.11.12:443": CandidateProbe(
            url="http://9.10.11.12:443", liveness_status=0, error="timeout"
        ),
    }

    async def probe(candidates, **kwargs):
        return {candidate["url"]: outcomes[candidate["url"]] for candidate in candidates}

    store = EgressStore(tmp_path / "registry.db")
    try:
        store.upsert_source(
            {"id": "list-a", "type": "public_text", "url": "https://example.test/list.txt"}
        )
        result = await _adapter(probe, fetcher=lambda url, timeout: text).refresh(
            store, ProviderPolicy(provider=Provider.PUBLIC, max_options=100)
        )
        assert result.candidates == 3
        assert result.promoted == 1
        assert result.quarantined == 1
        assert result.dead == 1
        statuses = {option.status for option in store.list_options()}
        assert statuses == {OptionStatus.USABLE, OptionStatus.QUOTA, OptionStatus.DEAD}
        usable = store.list_options(status=OptionStatus.USABLE)
        assert usable[0].egress_ip == "1.2.3.4"
        quota = store.list_options(status=OptionStatus.QUOTA)[0]
        assert quota.reset_at > quota.last_checked_at
        source = store.get_source("list-a")
        assert source["candidates_count"] == 3
        assert source["usable_count"] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_liveness_alone_never_promotes(tmp_path):
    async def probe(candidates, **kwargs):
        return {
            candidate["url"]: CandidateProbe(
                url=candidate["url"],
                egress_ip="1.2.3.4",
                liveness_status=200,
                free_status=0,
            )
            for candidate in candidates
        }

    store = EgressStore(tmp_path / "registry.db")
    try:
        result = await _adapter(
            probe, fetcher=lambda url, timeout: "1.2.3.4:443"
        ).refresh(store, ProviderPolicy(provider=Provider.PUBLIC))
        assert result.promoted == 0
        assert store.list_options(status=OptionStatus.REACHABLE)
        assert store.list_options(status=OptionStatus.USABLE) == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_two_endpoints_on_one_egress_are_one_option(tmp_path):
    text = "1.2.3.4:443\n1.2.3.5:8443"

    async def probe(candidates, **kwargs):
        return {
            candidate["url"]: CandidateProbe(
                url=candidate["url"],
                egress_ip="1.2.3.4",
                liveness_status=200,
                free_status=200,
            )
            for candidate in candidates
        }

    store = EgressStore(tmp_path / "registry.db")
    try:
        await _adapter(probe, fetcher=lambda url, timeout: text).refresh(
            store, ProviderPolicy(provider=Provider.PUBLIC)
        )
        assert store.count_provider(Provider.PUBLIC) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_disabled_source_is_not_fetched(tmp_path):
    called = []

    def fetcher(url, timeout):
        called.append(url)
        return "1.2.3.4:443"

    async def probe(candidates, **kwargs):
        return {}

    store = EgressStore(tmp_path / "registry.db")
    try:
        adapter = _adapter(
            probe,
            sources=[_source(enabled=False)],
            fetcher=fetcher,
        )
        result = await adapter.refresh(store, ProviderPolicy(provider=Provider.PUBLIC))
        assert result.candidates == 0
        assert called == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_source_fetch_failure_is_fail_soft(tmp_path):
    def fetcher(url, timeout):
        raise OSError("dns down")

    async def probe(candidates, **kwargs):
        return {}

    store = EgressStore(tmp_path / "registry.db")
    try:
        result = await _adapter(probe, fetcher=fetcher).refresh(
            store, ProviderPolicy(provider=Provider.PUBLIC)
        )
        assert result.candidates == 0
        assert result.errors
    finally:
        store.close()


@pytest.mark.asyncio
async def test_default_cycle_budget_bounds_the_cycle(tmp_path):
    text = "\n".join(f"1.2.3.{index}:443" for index in range(1, 20))
    seen = []

    async def probe(candidates, **kwargs):
        seen.extend(candidates)
        return {}

    adapter = PublicEgressAdapter(
        sources=[_source()],
        fetcher=lambda url, timeout: text,
        probe=probe,
        cycle_budget=3,
    )
    store = EgressStore(tmp_path / "registry.db")
    try:
        await adapter.refresh(store, ProviderPolicy(provider=Provider.PUBLIC, max_options=100))
        assert len(seen) == 3
    finally:
        store.close()


@pytest.mark.asyncio
async def test_quota_is_not_probed_before_its_reset(tmp_path):
    store = EgressStore(tmp_path / "registry.db")
    try:
        from free.egress_catalog import new_option

        option = new_option(
            provider=Provider.PUBLIC,
            kind="http",
            host="1.2.3.4",
            port=443,
            egress_ip="1.2.3.4",
        )
        store.apply(
            option,
            Observation(
                phase=ObservationPhase.FREE_GATE,
                status=429,
                result="quota",
                observed_at=__import__("time").time(),
            ),
            ProviderPolicy(provider=Provider.PUBLIC),
            reset_at=__import__("time").time() + 3600,
        )
        called = []

        async def probe(candidates, **kwargs):
            called.extend(candidates)
            return {}

        result = await _adapter(probe, fetcher=lambda url, timeout: "1.2.3.4:443").refresh(
            store, ProviderPolicy(provider=Provider.PUBLIC)
        )
        assert called == []
        assert result.skipped == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_threat_listed_active_option_is_retired(tmp_path):
    import time

    from free.egress_catalog import new_option

    class _Threat:
        def matches(self, host):
            return host == "103.26.10.23"

    store = EgressStore(tmp_path / "registry.db")
    try:
        policy = ProviderPolicy(provider=Provider.PUBLIC, ttl_usable_s=100.0)
        option = new_option(
            provider=Provider.PUBLIC,
            kind="http",
            host="103.26.10.23",
            port=2080,
            egress_ip="103.26.10.23",
        )
        store.apply(
            option,
            Observation(
                phase=ObservationPhase.FREE_GATE,
                status=200,
                result="usable",
                observed_at=time.time(),
            ),
            policy,
        )
        called = []

        async def probe(candidates, **kwargs):
            called.extend(candidates)
            return {}

        adapter = PublicEgressAdapter(
            sources=[_source()],
            fetcher=lambda url, timeout: "",
            probe=probe,
            threat_intel=_Threat(),
        )
        await adapter.refresh(store, policy)
        loaded = store.get_by_egress(Provider.PUBLIC, "103.26.10.23")
        assert loaded is not None and loaded.status == OptionStatus.DEAD
        assert called == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_sources_file_is_reloaded_without_restart(tmp_path):
    import time

    source_file = tmp_path / "sources.yaml"
    source_file.write_text(
        "sources:\n"
        "  - id: one\n    type: public_text\n    url: https://example.test/one.txt\n    priority: 1\n",
        encoding="utf-8",
    )
    calls = []

    def fetcher(url, timeout):
        calls.append(url)
        return ""

    async def probe(candidates, **kwargs):
        return {}

    adapter = PublicEgressAdapter(
        sources=[],
        fetcher=fetcher,
        probe=probe,
        sources_file=str(source_file),
        discovery_interval_s=21600,
    )
    store = EgressStore(tmp_path / "registry.db")
    try:
        policy = ProviderPolicy(provider=Provider.PUBLIC)
        await adapter.refresh(store, policy)
        assert calls == ["https://example.test/one.txt"]
        time.sleep(0.02)
        source_file.write_text(
            "sources:\n"
            "  - id: one\n    type: public_text\n    url: https://example.test/one.txt\n    priority: 1\n"
            "  - id: two\n    type: public_text\n    url: https://example.test/two.txt\n    priority: 2\n",
            encoding="utf-8",
        )
        await adapter.refresh(store, policy)
        assert "https://example.test/two.txt" in calls
        assert {source.id for source in adapter.sources} == {"one", "two"}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_local_text_source_is_used_without_network(tmp_path):
    proxy_file = tmp_path / "manual.txt"
    proxy_file.write_text("1.2.3.4:443\nsocks5://5.6.7.8:1080\n", encoding="utf-8")
    seen = []

    def fetcher(url, timeout):
        raise AssertionError("local source must not use the network fetcher")

    async def probe(candidates, **kwargs):
        seen.extend(candidates)
        return {}

    adapter = PublicEgressAdapter(
        sources=[_source(id="manual", type="local_text", url=str(proxy_file), scheme="https")],
        fetcher=fetcher,
        probe=probe,
    )
    store = EgressStore(tmp_path / "registry.db")
    try:
        await adapter.refresh(store, ProviderPolicy(provider=Provider.PUBLIC))
        assert {candidate["scheme"] for candidate in seen} == {"https", "socks5"}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_cycle_window_is_balanced_across_sources(tmp_path):
    sources = [
        _source(id="a", scheme="https", url="https://a.test/list"),
        _source(id="b", scheme="socks5", url="https://b.test/list"),
    ]
    seen = []

    def fetcher(url, timeout):
        if "a.test" in url:
            return "\n".join(f"1.1.1.{index}:443" for index in range(10))
        return "\n".join(f"2.2.2.{index}:1080" for index in range(10))

    async def probe(candidates, **kwargs):
        seen.extend(candidates)
        return {}

    adapter = PublicEgressAdapter(sources=sources, fetcher=fetcher, probe=probe, cycle_budget=4)
    store = EgressStore(tmp_path / "registry.db")
    try:
        await adapter.refresh(store, ProviderPolicy(provider=Provider.PUBLIC, max_options=100))
        assert sum(1 for candidate in seen if candidate["scheme"] == "https") == 2
        assert sum(1 for candidate in seen if candidate["scheme"] == "socks5") == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_source_added_through_the_store_is_used_without_restart(tmp_path):
    calls = []

    def fetcher(url, timeout):
        calls.append(url)
        return "1.2.3.4:443"

    async def probe(candidates, **kwargs):
        return {}

    store = EgressStore(tmp_path / "registry.db")
    try:
        store.upsert_source(
            {
                "id": "api-source",
                "type": "public_text",
                "url": "https://api.example/list.txt",
                "enabled": True,
                "interval_s": 3600,
            }
        )
        adapter = PublicEgressAdapter(sources=[], fetcher=fetcher, probe=probe)
        await adapter.refresh(store, ProviderPolicy(provider=Provider.PUBLIC))
        assert calls == ["https://api.example/list.txt"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_per_source_interval_prevents_refetch(tmp_path):
    calls = []

    def fetcher(url, timeout):
        calls.append(url)
        return "1.2.3.4:443"

    async def probe(candidates, **kwargs):
        return {}

    adapter = PublicEgressAdapter(
        sources=[_source(interval_s=3600)],
        fetcher=fetcher,
        probe=probe,
    )
    store = EgressStore(tmp_path / "registry.db")
    try:
        policy = ProviderPolicy(provider=Provider.PUBLIC)
        await adapter.refresh(store, policy)
        await adapter.refresh(store, policy)
        assert calls == ["https://example.test/list.txt"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_active_usable_options_are_revalidated_each_cycle(tmp_path):
    import time

    from free.egress_catalog import new_option

    store = EgressStore(tmp_path / "registry.db")
    try:
        policy = ProviderPolicy(provider=Provider.PUBLIC, max_active=4, ttl_usable_s=100.0)
        active = new_option(
            provider=Provider.PUBLIC,
            kind="https",
            host="1.2.3.4",
            port=443,
            egress_ip="1.2.3.4",
        )
        store.apply(
            active,
            Observation(
                phase=ObservationPhase.FREE_GATE,
                status=200,
                result="usable",
                observed_at=time.time(),
            ),
            policy,
        )
        seen = []

        async def probe(candidates, **kwargs):
            seen.extend(candidates)
            return {
                candidate["url"]: CandidateProbe(
                    url=candidate["url"],
                    egress_ip=candidate["host"],
                    liveness_status=200,
                    free_status=200,
                )
                for candidate in candidates
            }

        result = await _adapter(
            probe, fetcher=lambda url, timeout: "5.6.7.8:443"
        ).refresh(store, policy)
        hosts = {candidate["host"] for candidate in seen}
        assert {"1.2.3.4", "5.6.7.8"} <= hosts
        assert result.promoted == 2
        loaded = store.get_by_egress(Provider.PUBLIC, "1.2.3.4")
        assert loaded is not None and loaded.last_status == 200
    finally:
        store.close()


@pytest.mark.asyncio
async def test_reachable_options_are_revalidated_each_cycle(tmp_path):
    import time

    from free.egress_catalog import new_option

    store = EgressStore(tmp_path / "registry.db")
    try:
        policy = ProviderPolicy(provider=Provider.PUBLIC, max_active=4, ttl_usable_s=100.0)
        option = new_option(
            provider=Provider.PUBLIC,
            kind="socks5",
            host="188.187.3.75",
            port=1080,
            egress_ip="188.187.3.75",
        )
        store.apply(
            option,
            Observation(
                phase=ObservationPhase.LIVENESS,
                status=200,
                result=OptionStatus.REACHABLE,
                observed_at=time.time(),
            ),
            policy,
        )
        seen = []

        async def probe(candidates, **kwargs):
            seen.extend(candidates)
            return {
                candidate["url"]: CandidateProbe(
                    url=candidate["url"],
                    egress_ip=candidate["host"],
                    liveness_status=200,
                    free_status=200,
                )
                for candidate in candidates
            }

        result = await _adapter(probe, fetcher=lambda url, timeout: "").refresh(store, policy)
        assert [candidate["host"] for candidate in seen] == ["188.187.3.75"]
        assert result.promoted == 1
        assert store.get_by_egress(Provider.PUBLIC, "188.187.3.75").status == OptionStatus.USABLE
    finally:
        store.close()


@pytest.mark.asyncio
async def test_threat_intel_match_is_never_probed(tmp_path):
    called = []

    async def probe(candidates, **kwargs):
        called.extend(candidates)
        return {}

    class _Threat:
        def matches(self, host):
            return host == "103.26.10.23"

    adapter = PublicEgressAdapter(
        sources=[_source()],
        fetcher=lambda url, timeout: "103.26.10.23:2080\n1.2.3.4:443",
        probe=probe,
        threat_intel=_Threat(),
    )
    store = EgressStore(tmp_path / "registry.db")
    try:
        result = await adapter.refresh(store, ProviderPolicy(provider=Provider.PUBLIC))
        assert [candidate["host"] for candidate in called] == ["1.2.3.4"]
        assert result.skipped == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_denylisted_candidate_is_never_probed(tmp_path):
    called = []

    async def probe(candidates, **kwargs):
        called.extend(candidates)
        return {}

    adapter = PublicEgressAdapter(
        sources=[_source()],
        fetcher=lambda url, timeout: "103.26.10.23:2080\n1.2.3.4:443",
        probe=probe,
        denylist=["103.26.10.23/32"],
    )
    store = EgressStore(tmp_path / "registry.db")
    try:
        result = await adapter.refresh(store, ProviderPolicy(provider=Provider.PUBLIC))
        assert [candidate["host"] for candidate in called] == ["1.2.3.4"]
        assert result.skipped == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_cycle_timeout_is_bounded(tmp_path):
    import asyncio

    async def probe(candidates, **kwargs):
        await asyncio.sleep(10)
        return {}

    adapter = PublicEgressAdapter(
        sources=[_source()],
        fetcher=lambda url, timeout: "1.2.3.4:443",
        probe=probe,
        cycle_timeout_s=0.05,
    )
    store = EgressStore(tmp_path / "registry.db")
    try:
        started = asyncio.get_running_loop().time()
        result = await adapter.refresh(store, ProviderPolicy(provider=Provider.PUBLIC))
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 1.0
        assert result.errors and "timeout" in result.errors[0]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_cycle_rotates_through_the_catalog(tmp_path):
    text = "\n".join(f"1.2.3.{index}:443" for index in range(1, 7))
    windows: list[set[str]] = []

    async def probe(candidates, **kwargs):
        windows.append({candidate["url"] for candidate in candidates})
        return {}

    adapter = PublicEgressAdapter(
        sources=[_source()],
        fetcher=lambda url, timeout: text,
        probe=probe,
        cycle_budget=3,
    )
    store = EgressStore(tmp_path / "registry.db")
    try:
        policy = ProviderPolicy(provider=Provider.PUBLIC, max_options=100)
        await adapter.refresh(store, policy)
        await adapter.refresh(store, policy)
        assert len(windows[0]) == 3
        assert len(windows[1]) == 3
        assert windows[0].isdisjoint(windows[1])
        assert len(windows[0] | windows[1]) == 6
    finally:
        store.close()


@pytest.mark.asyncio
async def test_cursor_is_persisted_for_one_shot_runs(tmp_path):
    text = "\n".join(f"1.2.3.{index}:443" for index in range(1, 7))
    windows: list[set[str]] = []

    async def probe(candidates, **kwargs):
        windows.append({candidate["url"] for candidate in candidates})
        return {}

    store = EgressStore(tmp_path / "registry.db")
    try:
        policy = ProviderPolicy(provider=Provider.PUBLIC, max_options=100)
        for _ in range(2):
            adapter = PublicEgressAdapter(
                sources=[_source()],
                fetcher=lambda url, timeout: text,
                probe=probe,
                cycle_budget=3,
            )
            await adapter.refresh(store, policy)
        assert windows[0].isdisjoint(windows[1])
        assert len(windows[0] | windows[1]) == 6
    finally:
        store.close()


@pytest.mark.asyncio
async def test_max_candidates_bounds_the_cycle(tmp_path):
    text = "\n".join(f"1.2.3.{index}:443" for index in range(1, 20))
    seen = []

    async def probe(candidates, **kwargs):
        seen.extend(candidates)
        return {}

    store = EgressStore(tmp_path / "registry.db")
    try:
        await _adapter(probe, fetcher=lambda url, timeout: text).refresh(
            store,
            ProviderPolicy(provider=Provider.PUBLIC, max_options=100),
            max_candidates=5,
        )
        assert len(seen) == 5
    finally:
        store.close()
