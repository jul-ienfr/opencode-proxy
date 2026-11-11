"""Catalog source tests: parsing, transports, dedupe, embedding exclusion."""

from __future__ import annotations

import json

from free.qualification.domain import CandidateStatus
from free.qualification.source import NordVpnCatalogSource


def _row(hostname: str, **changes):
    base = {
        "hostname": hostname,
        "status": "online",
        "load": 10,
        "locations": [
            {
                "country": {
                    "name": "Sweden",
                    "city": {"name": "Stockholm"},
                }
            }
        ],
        "technologies": [
            {"identifier": "wireguard_udp"},
            {"identifier": "openvpn_udp"},
            {"identifier": "openvpn_tcp"},
        ],
    }
    base.update(changes)
    return base


def test_parses_online_server_and_transports(tmp_path):
    path = tmp_path / "api.json"
    path.write_text(json.dumps([_row("se1.nordvpn.com")]), encoding="utf-8")
    source = NordVpnCatalogSource(path)
    candidates = source.candidates(now=100.0)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.hostname == "se1.nordvpn.com"
    assert candidate.country == "Sweden"
    assert candidate.city == "Stockholm"
    assert candidate.transports == ("wireguard", "openvpn-udp", "openvpn-tcp")
    assert candidate.potential_score > 0


def test_denylisted_ip_is_skipped(tmp_path):
    path = tmp_path / "api.json"
    path.write_text(
        json.dumps([_row("blocked.nordvpn.com", ips=["1.2.3.4"])]),
        encoding="utf-8",
    )
    source = NordVpnCatalogSource(path, denylist=["1.2.3.0/24"])
    candidate = source.candidates(now=100.0)[0]
    assert candidate.status is CandidateStatus.SKIPPED
    assert candidate.skipped_reason == "denylisted"


def test_dedicated_ip_group_is_skipped(tmp_path):
    path = tmp_path / "api.json"
    path.write_text(
        json.dumps(
            [
                _row(
                    "dedicated.nordvpn.com",
                    groups=[{"identifier": "legacy_dedicated_ip"}],
                )
            ]
        ),
        encoding="utf-8",
    )
    source = NordVpnCatalogSource(path)
    candidate = source.candidates(now=100.0)[0]
    assert candidate.status is CandidateStatus.SKIPPED
    assert candidate.skipped_reason == "dedicated"
    assert candidate.transports == ("wireguard", "openvpn-udp", "openvpn-tcp")


def test_skips_offline_and_missing_hostname(tmp_path):
    path = tmp_path / "api.json"
    path.write_text(
        json.dumps(
            [
                _row("online.nordvpn.com"),
                _row("offline.nordvpn.com", status="offline"),
                _row("", status="online"),
            ]
        ),
        encoding="utf-8",
    )
    source = NordVpnCatalogSource(path)
    candidates = source.candidates(now=100.0)
    assert [item.hostname for item in candidates] == ["online.nordvpn.com"]


def test_dedupes_by_hostname(tmp_path):
    path = tmp_path / "api.json"
    path.write_text(json.dumps([_row("se1.nordvpn.com"), _row("SE1.nordvpn.com")]), encoding="utf-8")
    source = NordVpnCatalogSource(path)
    assert len(source.candidates(now=100.0)) == 1


def test_stats_counts_online_and_countries(tmp_path):
    path = tmp_path / "api.json"
    path.write_text(
        json.dumps([_row("a.nordvpn.com"), _row("b.nordvpn.com", status="offline")]),
        encoding="utf-8",
    )
    source = NordVpnCatalogSource(path)
    stats = source.stats()
    assert stats.total == 2
    assert stats.online == 1
    assert stats.with_transports == 1
    assert stats.countries == 1


def test_host_without_supported_transport_is_skipped(tmp_path):
    path = tmp_path / "api.json"
    path.write_text(
        json.dumps([_row("dedicated.nordvpn.com", technologies=[{"identifier": "ikev2"}])]),
        encoding="utf-8",
    )
    source = NordVpnCatalogSource(path)
    candidate = source.candidates(now=100.0)[0]
    assert candidate.transports == ()
    assert str(candidate.status) == "skipped"
    assert candidate.skipped_reason == "unsupported-transport"


def test_ingest_populates_server_transports(tmp_path):
    from free.qualification.store import QualificationStore

    path = tmp_path / "api.json"
    path.write_text(json.dumps([_row("se1.nordvpn.com")]), encoding="utf-8")
    store = QualificationStore(tmp_path / "registry.db")
    try:
        source = NordVpnCatalogSource(path)
        assert source.ingest(store, now=100.0) == 1
        rows = store._conn.execute(
            "SELECT hostname, transport FROM server_transports ORDER BY transport"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("se1.nordvpn.com", "openvpn-tcp"),
            ("se1.nordvpn.com", "openvpn-udp"),
            ("se1.nordvpn.com", "wireguard"),
        ]
    finally:
        store.close()


def test_reingest_marks_an_existing_pending_host_skipped(tmp_path):
    from free.qualification.store import QualificationStore

    path = tmp_path / "api.json"
    store = QualificationStore(tmp_path / "registry.db")
    try:
        source = NordVpnCatalogSource(path)
        path.write_text(json.dumps([_row("de1.nordvpn.com")]), encoding="utf-8")
        source.ingest(store, now=100.0)
        row = store._conn.execute(
            "SELECT status, skipped_reason FROM qualification_candidates"
            " WHERE hostname='de1.nordvpn.com'"
        ).fetchone()
        assert row["status"] == "pending"

        path.write_text(
            json.dumps(
                [
                    _row(
                        "de1.nordvpn.com",
                        groups=[{"identifier": "legacy_dedicated_ip"}],
                    )
                ]
            ),
            encoding="utf-8",
        )
        source.ingest(store, now=101.0)
        row = store._conn.execute(
            "SELECT status, skipped_reason FROM qualification_candidates"
            " WHERE hostname='de1.nordvpn.com'"
        ).fetchone()
        assert row["status"] == "skipped"
        assert row["skipped_reason"] == "dedicated"
    finally:
        store.close()


def test_reingest_preserves_an_operator_pause(tmp_path):
    from free.qualification.store import QualificationStore

    path = tmp_path / "api.json"
    path.write_text(json.dumps([_row("se1.nordvpn.com")]), encoding="utf-8")
    store = QualificationStore(tmp_path / "registry.db")
    try:
        source = NordVpnCatalogSource(path)
        source.ingest(store, now=100.0)
        with store._conn:
            store._conn.execute(
                "UPDATE qualification_candidates SET status='skipped',"
                " skipped_reason='operator-paused' WHERE hostname='se1.nordvpn.com'"
            )
        source.ingest(store, now=101.0)
        row = store._conn.execute(
            "SELECT status, skipped_reason FROM qualification_candidates"
            " WHERE hostname='se1.nordvpn.com'"
        ).fetchone()
        assert row["status"] == "skipped"
        assert row["skipped_reason"] == "operator-paused"
    finally:
        store.close()
