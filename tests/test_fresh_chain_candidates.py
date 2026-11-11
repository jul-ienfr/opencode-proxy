"""Candidate filtering for a transport-pinned repair chain."""

from __future__ import annotations

from scripts.fresh_chain import filter_candidates_for_transport


def test_auto_keeps_every_candidate():
    rows = [{"host": "wg", "transports": ["wireguard"]}]
    assert filter_candidates_for_transport(rows, "auto") == rows


def test_transport_filter_drops_incompatible_hosts():
    rows = [
        {"host": "wg-only", "transports": ["wireguard"]},
        {"host": "tcp-ok", "transports": ["wireguard", "openvpn-tcp"]},
        {"host": "no-metadata"},
    ]
    filtered = filter_candidates_for_transport(rows, "openvpn-tcp")
    assert [row["host"] for row in filtered] == ["tcp-ok", "no-metadata"]


def test_transport_filter_falls_back_to_original_when_empty():
    rows = [{"host": "wg-only", "transports": ["wireguard"]}]
    assert filter_candidates_for_transport(rows, "openvpn-tcp") == rows


def test_exhausted_egress_skips_only_known_burned_ip():
    from scripts.fresh_chain import _exhausted_egress

    assert _exhausted_egress({"ip": "1.2.3.4"}, {"1.2.3.4"}) == "1.2.3.4"
    assert _exhausted_egress({"ip": "1.2.3.4"}, {"5.6.7.8"}) == ""
    assert _exhausted_egress({}, {"1.2.3.4"}) == ""
    assert _exhausted_egress({"ip": ""}, {"1.2.3.4"}) == ""


def test_registry_hosts_filters_by_transport(tmp_path, monkeypatch):
    import sqlite3

    from scripts import fresh_chain

    (tmp_path / "logs").mkdir()
    db = tmp_path / "logs" / "registry.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE servers(hostname TEXT);
        CREATE TABLE ip_server_map(
            ip TEXT, hostname TEXT, transport TEXT,
            first_seen_at REAL DEFAULT 0, last_seen_at REAL DEFAULT 0,
            successes INTEGER DEFAULT 0, failures INTEGER DEFAULT 0
        );
        CREATE TABLE egress_ips(
            ip TEXT, last_ok_at REAL, state TEXT, reset_at REAL
        );
        """
    )
    conn.executemany(
        "INSERT INTO servers(hostname) VALUES (?)",
        [("wg.nordvpn.com",), ("tcp.nordvpn.com",)],
    )
    conn.executemany(
        "INSERT INTO ip_server_map(ip, hostname, transport) VALUES (?,?,?)",
        [("1.1.1.1", "wg.nordvpn.com", "wireguard"),
         ("2.2.2.2", "tcp.nordvpn.com", "openvpn-tcp")],
    )
    conn.executemany(
        "INSERT INTO egress_ips(ip, last_ok_at, state, reset_at) VALUES (?,?,?,?)",
        [("1.1.1.1", 100.0, "assigned", 0.0),
         ("2.2.2.2", 100.0, "assigned", 0.0)],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(fresh_chain, "ROOT", tmp_path)
    assert fresh_chain._registry_hosts("openvpn-tcp") == ["tcp.nordvpn.com"]
    assert set(fresh_chain._registry_hosts()) == {"wg.nordvpn.com", "tcp.nordvpn.com"}
