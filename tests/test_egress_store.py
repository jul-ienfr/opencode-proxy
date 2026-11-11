"""SQLite store tests: dedupe, append-only observations, TTL, retention."""

from __future__ import annotations

import json
import sqlite3

from free.egress_catalog import (
    Observation,
    ObservationPhase,
    OptionStatus,
    Provider,
    ProviderPolicy,
    new_option,
)
from free.egress_store import EgressStore


def _store(tmp_path):
    clock = [1_000.0]
    store = EgressStore(tmp_path / "registry.db", clock=lambda: clock[0], retention_days=7)
    return store, clock


def _option(**changes):
    base = {
        "provider": Provider.PUBLIC,
        "kind": "https",
        "host": "1.2.3.4",
        "port": 443,
        "egress_ip": "1.2.3.4",
        "source_ids": ("source-a",),
        "now": 1_000.0,
    }
    base.update(changes)
    return new_option(**base)


def _policy() -> ProviderPolicy:
    return ProviderPolicy(provider=Provider.PUBLIC, ttl_usable_s=100.0, ttl_quota_max_s=500.0)


def test_option_is_persisted_and_read_back(tmp_path):
    store, _ = _store(tmp_path)
    try:
        option = _option()
        store.upsert_option(option)
        loaded = store.get_option(option.key)
        assert loaded is not None
        assert loaded.egress_ip == "1.2.3.4"
        assert loaded.source_ids == ("source-a",)
        assert store.count_provider(Provider.PUBLIC) == 1
    finally:
        store.close()


def test_two_endpoints_behind_one_egress_are_one_option(tmp_path):
    store, _ = _store(tmp_path)
    try:
        store.upsert_option(_option(host="1.2.3.4", port=443))
        store.upsert_option(_option(host="1.2.3.4", port=8443))
        assert store.count_provider(Provider.PUBLIC) == 1
        assert len(store.list_options()) == 1
    finally:
        store.close()


def test_200_observation_is_append_only_and_promotes(tmp_path):
    store, _ = _store(tmp_path)
    try:
        option = _option()
        applied = store.apply(
            option,
            Observation(
                phase=ObservationPhase.FREE_GATE,
                status=200,
                result=OptionStatus.USABLE,
                observed_at=1_000.0,
            ),
            _policy(),
            run_id="run-1",
        )
        assert applied.status == OptionStatus.USABLE
        assert applied.score > 0
        assert [row.key for row in store.selectable(_policy(), now=1_050.0)] == [option.key]
        with sqlite3.connect(store.path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM egress_observations").fetchone()[0]
            message = conn.execute("SELECT run_id FROM egress_observations").fetchone()[0]
        assert count == 1
        assert message == "run-1"
    finally:
        store.close()


def test_success_count_accumulates_across_observations(tmp_path):
    store, clock = _store(tmp_path)
    try:
        option = _option()
        for tick in (1_000.0, 1_010.0):
            clock[0] = tick
            store.apply(
                _option(now=tick),
                Observation(
                    phase=ObservationPhase.FREE_GATE,
                    status=200,
                    result=OptionStatus.USABLE,
                    observed_at=tick,
                ),
                _policy(),
            )
        loaded = store.get_option(option.key)
        assert loaded is not None
        assert loaded.success_count == 2
    finally:
        store.close()


def test_recompute_scores_backfills_existing_options(tmp_path):
    store, _ = _store(tmp_path)
    try:
        store.apply(
            _option(),
            Observation(
                phase=ObservationPhase.FREE_GATE,
                status=200,
                result=OptionStatus.USABLE,
                observed_at=1_000.0,
            ),
            _policy(),
        )
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE egress_options SET score=0")
            conn.commit()
        assert store.recompute_scores(_policy(), now=1_050.0) == 1
        assert store.get_by_egress(Provider.PUBLIC, "1.2.3.4").score > 0
    finally:
        store.close()


def test_429_is_stored_as_quota_with_reset(tmp_path):
    store, _ = _store(tmp_path)
    try:
        applied = store.apply(
            _option(),
            Observation(
                phase=ObservationPhase.FREE_GATE,
                status=429,
                result=OptionStatus.QUOTA,
                observed_at=1_000.0,
            ),
            _policy(),
            reset_at=1_500.0,
        )
        assert applied.status == OptionStatus.QUOTA
        assert applied.reset_at == 1_500.0
        assert store.selectable(_policy(), now=1_400.0) == []
    finally:
        store.close()


def test_retention_purges_old_observations_and_dead_options(tmp_path):
    store, clock = _store(tmp_path)
    try:
        store.apply(
            _option(),
            Observation(
                phase=ObservationPhase.LIVENESS,
                status=0,
                result=OptionStatus.DEAD,
                observed_at=1_000.0,
            ),
            _policy(),
        )
        clock[0] = 1_000.0 + 31 * 86_400.0
        removed = store.purge()
        assert removed["options"] == 1
        assert removed["observations"] == 1
    finally:
        store.close()


def test_export_is_atomic_and_read_only(tmp_path):
    store, _ = _store(tmp_path)
    try:
        store.apply(
            _option(),
            Observation(
                phase=ObservationPhase.FREE_GATE,
                status=200,
                result=OptionStatus.USABLE,
                observed_at=1_000.0,
            ),
            _policy(),
        )
        target = store.export_json(tmp_path / "export.json")
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["providers"][Provider.PUBLIC]["usable"] == 1
        assert len(payload["options"]) == 1
        assert not target.with_suffix(".json.tmp").exists()
    finally:
        store.close()


def test_sync_source_usable_counts_aligns_with_usable_options(tmp_path):
    store, _ = _store(tmp_path)
    try:
        store.upsert_source({"id": "source-a", "type": "public_text", "url": "https://example.test/a"})
        store.apply(
            _option(),
            Observation(
                phase=ObservationPhase.FREE_GATE,
                status=200,
                result=OptionStatus.USABLE,
                observed_at=1_000.0,
            ),
            _policy(),
        )
        assert store.sync_source_usable_counts() == 1
        assert store.get_source("source-a")["usable_count"] == 1
    finally:
        store.close()


def test_runs_and_sources_are_persisted(tmp_path):
    store, _ = _store(tmp_path)
    try:
        source = store.upsert_source(
            {
                "id": "list-a",
                "type": "public_text",
                "url": "https://example.test/list.txt",
                "enabled": True,
                "interval_s": 600,
            }
        )
        assert source["id"] == "list-a"
        store.record_source_candidates("list-a", 10)
        store.record_source_promotion("list-a", 2)
        source = store.get_source("list-a")
        assert source["candidates_count"] == 10
        assert source["usable_count"] == 2
        assert source["yield_pct"] == 20.0
        run_id = store.start_run(Provider.PUBLIC)
        store.finish_run(run_id, status="ok", candidates=3, promoted=1, dead=2)
        runs = store.list_runs(provider=Provider.PUBLIC)
        assert runs[0]["status"] == "ok"
        assert runs[0]["promoted"] == 1
        assert store.list_sources(enabled_only=True)[0]["id"] == "list-a"
        assert store.provider_state()[0]["provider"] == Provider.PUBLIC
    finally:
        store.close()


def test_legacy_public_slash24_block_is_normalized_to_the_egress_ip(tmp_path):
    path = tmp_path / "registry.db"
    store = EgressStore(path)
    store.upsert_option(_option(egress_ip="84.17.47.124", block="84.17.47.0/24"))
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE egress_options SET block='84.17.47.0/24'")
        conn.commit()
    store.close()
    reopened = EgressStore(path)
    try:
        loaded = reopened.get_by_egress(Provider.PUBLIC, "84.17.47.124")
        assert loaded is not None
        assert loaded.block == "84.17.47.124"
    finally:
        reopened.close()


def test_summary_counts_each_state_and_unique_blocks(tmp_path):
    store, _ = _store(tmp_path)
    try:
        store.apply(
            _option(egress_ip="1.2.3.4"),
            Observation(
                phase=ObservationPhase.FREE_GATE,
                status=200,
                result=OptionStatus.USABLE,
                observed_at=1_000.0,
            ),
            _policy(),
        )
        store.apply(
            _option(host="5.6.7.8", egress_ip="5.6.7.8"),
            Observation(
                phase=ObservationPhase.FREE_GATE,
                status=429,
                result=OptionStatus.QUOTA,
                observed_at=1_000.0,
            ),
            _policy(),
            reset_at=2_000.0,
        )
        summary = store.summary()[Provider.PUBLIC]
        assert summary["known"] == 2
        assert summary["usable"] == 1
        assert summary["quota"] == 1
        assert summary["unique_blocks"] == 2
    finally:
        store.close()
