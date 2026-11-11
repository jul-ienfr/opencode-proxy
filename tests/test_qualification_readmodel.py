"""Read-only projection tests for the qualification dashboard."""

from __future__ import annotations

import time

from free.qualification import readmodel
from free.qualification.domain import Attempt, AttemptStatus, Candidate, CandidateStatus
from free.qualification.readmodel import (
    attempts,
    coverage,
    discovery,
    epoch,
    epochs,
    next_candidates,
    queue,
    quota,
    repairs,
    rotations,
    slo,
    stations,
    summary,
    transports,
    watchdog,
)
from free.qualification.store import QualificationStore


def _candidate(hostname: str, **changes) -> Candidate:
    base = {
        "hostname": hostname,
        "country": "Sweden",
        "load": 10.0,
        "transports": ("wireguard", "openvpn-udp"),
        "potential_score": 70.0,
        "first_seen_at": 100.0,
        "last_seen_at": 100.0,
        "updated_at": 100.0,
    }
    base.update(changes)
    return Candidate(**base)


def test_summary_and_queue_are_read_only(tmp_path):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.upsert_candidates([_candidate("a"), _candidate("b", potential_score=90.0)])
        store.upsert_server_transports([_candidate("a")])
        store.create_epoch("epoch-1", total=2, now=100.0)
        attempt = Attempt(
            hostname="a",
            station=1,
            transport="wireguard",
            status=AttemptStatus.QUOTA,
            started_at=100.0,
            finished_at=100.0,
        )
        store.record_attempt(attempt)
        store.apply_attempt("a", attempt, candidate_status=CandidateStatus.QUOTA, now=100.0)
        store.upsert_station_state(
            1,
            state="healthy",
            current_ip="1.2.3.4",
            current_host="a.nordvpn.com",
            client_usable=True,
            now=100.0,
        )
        store.upsert_station_runtime(
            1,
            state="serving",
            standby_egress_ip="5.5.5.5",
            cutover_ms=12.0,
            availability_1h=99.0,
            now=100.0,
        )
        store.open_incident(2, "tunnel_down", now=100.0)
        store.record_repair_job(2, "tunnel_down", now=100.0)
        store.record_discovery_run(source="api", started_at=100.0, finished_at=101.0, server_count=2)
        store.close()

        payload = summary(db)
        assert payload["enabled"]
        assert payload["total"] == 2
        assert payload["quota"] == 1
        assert payload["pending"] == 1

        rows = queue(db, status="pending", limit=10)
        assert rows["total"] == 1
        assert rows["items"][0]["hostname"] == "b"

        epoch_rows = epochs(db)
        assert epoch_rows["items"][0]["epoch_id"] == "epoch-1"

        attempt_rows = attempts(db, hostname="a")
        assert attempt_rows["items"][0]["status"] == "429"

        transport_rows = transports(db)
        assert transport_rows["items"][0]["transport"] in ("openvpn-udp", "wireguard")

        station_rows = stations(db)
        assert station_rows["items"][0]["station"] == 1
        assert station_rows["items"][0]["client_usable"] is True
        assert station_rows["items"][0]["standby_egress_ip"] == "5.5.5.5"
        assert station_rows["items"][0]["cutover_ms"] == 12.0

        watchdog_rows = watchdog(db)
        assert watchdog_rows["incidents"][0]["cause"] == "tunnel_down"

        discovery_rows = discovery(db)
        assert discovery_rows["items"][0]["server_count"] == 2

        repair_rows = repairs(db)
        assert repair_rows["items"][0]["station"] == 2

        slo_payload = slo(db)
        assert slo_payload["stations_total"] == 1
        assert slo_payload["stations_usable"] == 1
        assert slo_payload["availability_1h_pct"] == 99.0
        assert slo_payload["six_of_six"] is False
    finally:
        try:
            store.close()
        except Exception:
            pass


def test_transport_health_aggregates_attempts(tmp_path):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.upsert_candidates([_candidate("a")])
        store.upsert_server_transports([_candidate("a")])
        for transport, status, rtt in (
            ("wireguard", AttemptStatus.OK, 40.0),
            ("openvpn-udp", AttemptStatus.QUOTA, 0.0),
            ("openvpn-tcp", AttemptStatus.TIMEOUT, 0.0),
        ):
            store.record_attempt(
                Attempt(
                    hostname="a",
                    station=1,
                    transport=transport,
                    status=status,
                    rtt_ms=rtt,
                    started_at=100.0,
                    finished_at=100.0,
                )
            )
    finally:
        store.close()
    items = {row["transport"]: row for row in transports(db)["items"]}
    assert items["wireguard"]["health"] == "healthy"
    assert items["wireguard"]["ok"] == 1
    assert items["wireguard"]["avg_rtt_ms"] == 40.0
    assert items["openvpn-udp"]["health"] == "quota"
    assert items["openvpn-udp"]["quota"] == 1
    assert items["openvpn-tcp"]["health"] == "infra"
    assert items["openvpn-tcp"]["infra"] == 1


def test_transport_degraded_after_min_attempts_without_success(tmp_path):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.upsert_candidates([_candidate("a")])
        store.upsert_server_transports([_candidate("a")])
        for index in range(5):
            attempt = Attempt(
                hostname="a",
                station=1,
                transport="openvpn-udp",
                status=AttemptStatus.TIMEOUT,
                error="dns timeout",
                round=index,
                started_at=100.0 + index,
                finished_at=100.0 + index,
            )
            store.record_attempt(attempt)
            store.apply_attempt(
                "a",
                attempt,
                candidate_status=CandidateStatus.PENDING,
                transport_health_min_attempts=5,
                now=100.0 + index,
            )
        last_ok = Attempt(
            hostname="a",
            station=1,
            transport="wireguard",
            status=AttemptStatus.OK,
            rtt_ms=25.0,
            started_at=200.0,
            finished_at=200.0,
        )
        store.record_attempt(last_ok)
        store.apply_attempt(
            "a",
            last_ok,
            candidate_status=CandidateStatus.USABLE,
            transport_health_min_attempts=5,
            now=200.0,
        )
    finally:
        store.close()
    items = {row["transport"]: row for row in transports(db)["items"]}
    udp = items["openvpn-udp"]
    assert udp["attempts"] == 5
    assert udp["successes"] == 0
    assert udp["degraded"] is True
    assert udp["health"] == "degraded"
    assert udp["last_error"] == "dns timeout"
    assert udp["diagnostic"] == "dns"
    assert any(item["transport"] == "openvpn-udp" for item in transports(db)["openvpn_diagnostics"])
    assert items["wireguard"]["health"] == "healthy"
    assert items["wireguard"]["latency_p50_ms"] == 25.0


def test_quota_projection_reads_ledger(tmp_path):
    import sqlite3
    from pathlib import Path as PathlibPath

    db = tmp_path / "registry.db"
    schema = (
        PathlibPath(__file__).resolve().parents[1]
        / "rotation"
        / "infrastructure"
        / "db"
        / "schema.sql"
    ).read_text(encoding="utf-8")
    conn = sqlite3.connect(db)
    try:
        conn.executescript(schema)
        now = time.time()
        conn.execute(
            """
            INSERT INTO egress_ips(
                ip, provider, first_seen_at, last_seen_at, successes,
                failures, score_ip, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("1.2.3.4", "nordvpn", now, now, 2, 1, 50.0, "assigned"),
        )
        conn.execute(
            """
            INSERT INTO quota_buckets(
                ip, provider, model, day, bucket, used, successes, failures,
                last_status, last_error, reset_at, source,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "1.2.3.4",
                "nordvpn",
                "default",
                "2026-11-09",
                "1.2.3.4/32",
                10,
                2,
                1,
                429,
                "quota",
                now + 600.0,
                "observed",
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    payload = quota(db)
    assert payload["enabled"] is True
    assert payload["summary"]["ips"] == 1
    assert payload["summary"]["quota_429"] == 1
    assert payload["summary"]["next_reset_at"] is not None
    assert payload["buckets"][0]["block_key"] == ""
    assert payload["buckets"][0]["bucket"] == "1.2.3.4/32"


def test_summary_exposes_skip_reasons(tmp_path):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                _candidate(
                    "dedicated",
                    status=CandidateStatus.SKIPPED,
                    skipped_reason="dedicated",
                ),
                _candidate(
                    "unsupported",
                    status=CandidateStatus.SKIPPED,
                    skipped_reason="unsupported-transport",
                ),
                _candidate(
                    "paused",
                    status=CandidateStatus.SKIPPED,
                    skipped_reason="operator-paused",
                ),
                _candidate("eligible"),
            ]
        )
    finally:
        store.close()
    payload = summary(db)
    assert payload["total"] == 4
    assert payload["pending"] == 1
    assert payload["dedicated"] == 1
    assert payload["unsupported_transport"] == 1
    assert payload["paused"] == 1
    assert payload["by_skipped_reason"]["dedicated"] == 1


def test_coverage_groups_countries_and_transports(tmp_path):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                _candidate("se1", country="Sweden"),
                _candidate("se2", country="Sweden"),
                _candidate("jp1", country="Japan"),
                _candidate(
                    "dedicated",
                    country="Sweden",
                    status=CandidateStatus.SKIPPED,
                    skipped_reason="dedicated",
                ),
            ]
        )
        store.upsert_server_transports([_candidate("se1")])
    finally:
        store.close()
    payload = coverage(db)
    by_country = {row["country"]: row for row in payload["countries"]}
    assert by_country["Sweden"]["total"] == 3
    assert by_country["Sweden"]["pending"] == 2
    assert by_country["Sweden"]["skipped"] == 1
    assert by_country["Japan"]["total"] == 1
    assert payload["countries"][0]["country"] == "Sweden"
    assert {row["transport"] for row in payload["transports"]} == {
        "wireguard",
        "openvpn-udp",
    }


def test_rotations_expose_cutover_history(tmp_path):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.record_cutover(
            station=1,
            from_option_key="a.nordvpn.com",
            from_egress_ip="1.1.1.1",
            to_option_key="b.nordvpn.com",
            to_egress_ip="2.2.2.2",
            transport="wireguard",
            started_at=100.0,
            finished_at=101.0,
            duration_ms=1000.0,
            result="committed",
        )
    finally:
        store.close()
    items = rotations(db)["items"]
    assert items[0]["station"] == 1
    assert items[0]["to_egress_ip"] == "2.2.2.2"
    assert items[0]["result"] == "committed"
    assert items[0]["duration_ms"] == 1000.0


def test_epoch_detail_and_missing(tmp_path):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.create_epoch("epoch-1", total=2, now=100.0)
        store.close_epoch("epoch-1", status="complete", now=110.0)
    finally:
        store.close()
    payload = epoch(db, "epoch-1")
    assert payload is not None
    assert payload["status"] == "complete"
    assert payload["finished_at"] == 110.0
    assert epoch(db, "missing") is None


def test_missing_database_is_fail_soft(tmp_path):
    missing = tmp_path / "missing.db"
    assert summary(missing)["enabled"] is False
    assert queue(missing)["items"] == []
    assert epochs(missing)["items"] == []
    assert attempts(missing)["items"] == []
    assert transports(missing)["items"] == []


def test_next_candidates_returns_claimable_rows(tmp_path):
    db = tmp_path / "registry.db"
    now = time.time()
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                Candidate(
                    hostname="ready.nordvpn.com",
                    country="Sweden",
                    transports=("wireguard",),
                    potential_score=90.0,
                    first_seen_at=now,
                    last_seen_at=now,
                    updated_at=now,
                ),
                Candidate(
                    hostname="future.nordvpn.com",
                    country="Sweden",
                    transports=("wireguard",),
                    potential_score=95.0,
                    next_eligible_at=now + 3600.0,
                    first_seen_at=now,
                    last_seen_at=now,
                    updated_at=now,
                ),
            ],
            now=now,
        )
    finally:
        store.close()
    payload = next_candidates(db, limit=10)
    assert len(payload["items"]) == 1
    assert payload["items"][0]["hostname"] == "ready.nordvpn.com"


def test_queue_filters_country_and_transport(tmp_path):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                Candidate(
                    hostname="se.nordvpn.com",
                    country="Sweden",
                    transports=("wireguard",),
                    first_seen_at=100.0,
                    last_seen_at=100.0,
                    updated_at=100.0,
                ),
                Candidate(
                    hostname="jp.nordvpn.com",
                    country="Japan",
                    transports=("openvpn-tcp",),
                    first_seen_at=100.0,
                    last_seen_at=100.0,
                    updated_at=100.0,
                ),
            ]
        )
    finally:
        store.close()
    assert queue(db, country="Sweden")["total"] == 1
    assert queue(db, transport="openvpn-tcp")["total"] == 1
    assert queue(db, country="Sweden", transport="openvpn-tcp")["total"] == 0


def test_readmodel_cache_ttl_scope_and_invalidate(tmp_path, monkeypatch):
    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.upsert_candidates([_candidate("a")])
    finally:
        store.close()
    clock = {"now": 100.0}
    monkeypatch.setattr(readmodel, "_clock", lambda: clock["now"])
    readmodel.invalidate()
    assert readmodel.summary(db)["total"] == 1
    assert readmodel.cache_info()["entries"] >= 1

    store = QualificationStore(db)
    try:
        store.upsert_candidates([_candidate("b")])
    finally:
        store.close()
    assert readmodel.summary(db)["total"] == 1  # cached for 5 s

    clock["now"] += 6.0
    assert readmodel.summary(db)["total"] == 2

    store = QualificationStore(db)
    try:
        store.upsert_candidates([_candidate("c")])
    finally:
        store.close()
    readmodel.invalidate(db)
    assert readmodel.summary(db)["total"] == 3

    assert readmodel.queue(db, limit=1)["items"][0]["hostname"]
    readmodel.invalidate()
    assert readmodel.cache_info()["entries"] == 0


def test_readmodel_7070_rows_stay_under_two_seconds(tmp_path):
    import statistics

    db = tmp_path / "registry.db"
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                _candidate(
                    f"host{index}.nordvpn.com",
                    potential_score=float(index % 100),
                )
                for index in range(7070)
            ]
        )
    finally:
        store.close()
    readmodel.invalidate()
    timings = []
    for _ in range(20):
        started = time.perf_counter()
        assert readmodel.summary(db)["total"] == 7070
        assert readmodel.queue(db, limit=50)["items"]
        timings.append((time.perf_counter() - started) * 1000.0)
    p95 = statistics.quantiles(timings, n=20)[18]
    assert p95 < 2000.0
    assert readmodel.cache_info()["ttl_s"] == 5.0
