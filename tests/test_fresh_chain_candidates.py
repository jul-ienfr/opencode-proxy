"""Candidate filtering for a transport-pinned repair chain."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.fresh_chain import (
    _retry_429_delay_s,
    _retry_transient_429,
    _retryable_429,
    filter_candidates_for_transport,
)


def _probe_result(status: int, *, ra: float = 0.0, scope: str = "", valid: bool = True):
    return SimpleNamespace(
        status=status,
        retry_after_s=ra,
        quota_scope=scope,
        valid=valid,
    )


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


def test_retryable_429_only_for_short_or_ambiguous_limits():
    # A daily 429 without Retry-After may be a rolling window: one retry.
    assert _retryable_429(_probe_result(429, scope="daily")) is True
    assert _retryable_429(_probe_result(429, ra=30.0, scope="rate_limit")) is True
    assert _retryable_429(_probe_result(429, scope="transient")) is True
    # A long Retry-After is the daily cap: rotate instead of waiting.
    assert _retryable_429(_probe_result(429, ra=36000.0, scope="daily")) is False
    assert _retryable_429(_probe_result(200)) is False
    assert _retryable_429(_probe_result(429, valid=False)) is False


def test_retry_delay_is_short_and_bounded():
    assert _retry_429_delay_s(_probe_result(429)) == 8.0
    assert _retry_429_delay_s(_probe_result(429, ra=5.0)) == 5.0
    assert _retry_429_delay_s(_probe_result(429, ra=3600.0)) == 15.0


@pytest.mark.asyncio
async def test_retry_transient_429_reprobes_the_same_model():
    calls: list[tuple[int, str]] = []
    sleeps: list[float] = []
    answer = _probe_result(200)

    class Probe:
        async def probe(self, station: int, model: str):
            calls.append((station, model))
            return answer

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    original = _probe_result(429)
    out = await _retry_transient_429(
        Probe(), 3, original, "deepseek", sleep=fake_sleep
    )
    assert out is answer
    assert calls == [(3, "deepseek")]
    assert sleeps == [8.0]


@pytest.mark.asyncio
async def test_retry_skips_a_long_daily_429():
    calls: list[tuple[int, str]] = []

    class Probe:
        async def probe(self, station: int, model: str):
            calls.append((station, model))
            return _probe_result(200)

    async def fake_sleep(delay: float) -> None:  # pragma: no cover - must not run
        raise AssertionError("a long daily 429 must not sleep")

    original = _probe_result(429, ra=36000.0, scope="daily")
    out = await _retry_transient_429(
        Probe(), 1, original, "deepseek", sleep=fake_sleep
    )
    assert out is original
    assert calls == []


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
