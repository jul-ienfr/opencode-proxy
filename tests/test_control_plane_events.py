"""Control plane event ingestion: cursor, idempotence and registry mapping."""

from __future__ import annotations

import json
import sqlite3

import httpx

from free.control_plane.client import GluetunControlClient
from free.control_plane.events import EventIngestor, apply_control_event
from free.ip_registry.registry_db import ingest_control_events, load_control_cursor
from rotation.infrastructure.db.migrate import apply_schema, connect

BASE_URL = "http://vpn-gluetun-4:8000"
STATION = 4


def make_db(tmp_path):
    path = tmp_path / "registry.db"
    conn = connect(path)
    apply_schema(conn)
    conn.close()
    return path


def make_client(handler):
    return GluetunControlClient(
        BASE_URL,
        station=STATION,
        api_key="ingest-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )


def page(items, *, gap=False):
    return httpx.Response(
        200,
        json={"items": items, "next_seq": items[-1]["seq"] if items else 0, "gap": gap},
    )


def event(seq, event_type, *, data=None, ts="2026-11-13T12:00:00Z", severity="info", job_id=""):
    return {
        "seq": seq,
        "ts": ts,
        "type": event_type,
        "severity": severity,
        "job_id": job_id,
        "data": data or {},
    }


def rows(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def test_poll_applies_each_event_once_and_persists_the_cursor(tmp_path):
    db_path = make_db(tmp_path)
    items = [
        event(1, "job.committed", data={"hostname": "nl-2.nordvpn.com", "ip": "203.0.113.9"}),
        event(2, "egress.verified", data={"ip": "203.0.113.9", "latency_ms": 42}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/events"
        return page(items)

    ingestor = EventIngestor(make_client(handler), station=STATION, db_path=str(db_path))

    first = ingestor.poll()
    assert first.ingested == 2
    assert first.last_seq == 2
    assert first.advanced

    second = ingestor.poll()
    assert second.ingested == 0
    assert second.last_seq == 2

    observations = rows(db_path, "SELECT * FROM observations ORDER BY id")
    assert len(observations) == 2
    assert observations[0]["hostname"] == "nl-2.nordvpn.com"
    assert observations[0]["ip"] == "203.0.113.9"
    assert observations[0]["validation"] == "job.committed"
    assert observations[1]["latency_ms"] == 42
    assert observations[1]["validation"] == "egress.verified"

    restarted = EventIngestor(make_client(handler), station=STATION, db_path=str(db_path))
    assert restarted.cursor() == 2
    assert restarted.poll().ingested == 0


def test_replayed_events_are_deduplicated_by_station_and_seq(tmp_path):
    db_path = make_db(tmp_path)
    events = [event(1, "job.committed", data={"hostname": "a", "ip": "203.0.113.1"})]

    first = ingest_control_events(STATION, events, apply=apply_control_event, db_path=db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM control_cursor")
        conn.commit()
    finally:
        conn.close()
    replay = ingest_control_events(STATION, events, apply=apply_control_event, db_path=db_path)

    assert first["ingested"] == 1
    assert replay["ingested"] == 0
    assert replay["duplicates"] == 1
    assert len(rows(db_path, "SELECT id FROM observations")) == 1


def test_failed_degraded_creates_a_critical_incident(tmp_path):
    db_path = make_db(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return page([event(3, "job.failed_degraded", job_id="job-3", data={"error": "no egress"})])

    EventIngestor(make_client(handler), station=STATION, db_path=str(db_path)).poll()

    incidents = rows(db_path, "SELECT * FROM incidents")
    assert len(incidents) == 1
    assert incidents[0]["scope"] == "station"
    assert incidents[0]["cause"] == "job.failed_degraded"
    details = json.loads(incidents[0]["details"])
    assert details["severity"] == "critical"
    assert details["job_id"] == "job-3"


def test_exhausted_enqueues_a_repair_without_disabling_the_station(tmp_path):
    db_path = make_db(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return page([event(4, "job.exhausted", data={"hostname": "nl-2.nordvpn.com"})])

    EventIngestor(make_client(handler), station=STATION, db_path=str(db_path)).poll()

    repairs = rows(db_path, "SELECT * FROM repair_requests")
    assert len(repairs) == 1
    assert repairs[0]["station"] == STATION
    assert repairs[0]["reason"] == "job.exhausted"
    assert repairs[0]["status"] == "pending"
    assert repairs[0]["expires_at"] > repairs[0]["requested_at"]


def test_repeated_rolled_back_requests_a_repair_on_the_second_event(tmp_path):
    db_path = make_db(tmp_path)
    events = [
        event(5, "job.rolled_back", data={"hostname": "a"}),
        event(6, "job.rolled_back", data={"hostname": "a"}),
    ]
    ingest_control_events(STATION, events, apply=apply_control_event, db_path=db_path)

    repairs = rows(db_path, "SELECT * FROM repair_requests")
    assert [row["reason"] for row in repairs] == ["job.rolled_back.repeated"]
    observations = rows(db_path, "SELECT valid FROM observations")
    assert [row["valid"] for row in observations] == [0, 0]


def test_transport_exhaustion_is_a_high_priority_repair(tmp_path):
    db_path = make_db(tmp_path)
    events = [event(7, "transport.cycle_exhausted", data={"transport": "openvpn-tcp"})]
    ingest_control_events(STATION, events, apply=apply_control_event, db_path=db_path)

    repairs = rows(db_path, "SELECT reason FROM repair_requests")
    assert repairs[0]["reason"] == "transport.cycle_exhausted:priority=high"


def test_state_and_dns_events_become_incidents(tmp_path):
    db_path = make_db(tmp_path)
    events = [
        event(8, "state.drift", data={"reason": "station mismatch"}),
        event(9, "dns.tier_down", data={"tier": "dot"}),
        event(10, "dns.ladder_exhausted", severity="critical"),
    ]
    ingest_control_events(STATION, events, apply=apply_control_event, db_path=db_path)

    incidents = rows(db_path, "SELECT scope, cause, details FROM incidents ORDER BY id")
    assert [row["scope"] for row in incidents] == ["state", "dns", "dns"]
    assert [row["cause"] for row in incidents] == [
        "state.drift",
        "dns.tier_down",
        "dns.ladder_exhausted",
    ]
    severities = [json.loads(row["details"])["severity"] for row in incidents]
    assert severities == ["warning", "warning", "critical"]


def test_gap_is_surfaced_without_blocking_ingestion(tmp_path):
    db_path = make_db(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return page([event(12, "job.committed", data={"hostname": "a"})], gap=True)

    result = EventIngestor(make_client(handler), station=STATION, db_path=str(db_path)).poll()

    assert result.gap is True
    assert result.ingested == 1
    assert load_control_cursor(STATION, db_path=db_path) == 12


def test_mapping_failure_rolls_back_the_cursor(tmp_path):
    db_path = make_db(tmp_path)
    events = [event(13, "job.committed", data={"hostname": "a"})]

    def explode(_conn, _station, _event):
        raise RuntimeError("mapping failed")

    try:
        ingest_control_events(STATION, events, apply=explode, db_path=db_path)
    except RuntimeError:
        pass

    assert load_control_cursor(STATION, db_path=db_path) == 0
    assert rows(db_path, "SELECT station, seq FROM control_events") == []
    assert rows(db_path, "SELECT id FROM observations") == []
