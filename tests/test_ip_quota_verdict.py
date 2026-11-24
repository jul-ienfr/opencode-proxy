"""Verdict amont (019 T309) + garde ``reset_at``/re-test (019 T312).

Aucun réseau, aucun Docker : le verdict est une fonction pure et la garde du
pool est exercée sur un double minimal ``FreeIPPool.__new__`` avec un vrai
``QuotaLedger`` en mémoire (``persist=False``).
"""

from __future__ import annotations

import free.pool as pool_mod
from free.ip_quota import (
    UPSTREAM_COOLING,
    UPSTREAM_DEAD,
    UPSTREAM_HOT,
    UPSTREAM_UNKNOWN,
    upstream_verdict,
)
from free.pool import FreeIPPool
from free.quota_ledger import QuotaLedger

MODEL = "minimax-m3.1-flash-free"
IP = "198.51.100.20"
NOW = 1_800_000_000.0


# ── T309 — matrice du verdict pur ─────────────────────────────────────────


def test_recent_200_is_hot():
    assert upstream_verdict(NOW - 10, 0.0, None, 0, NOW) == (UPSTREAM_HOT, None)


def test_200_older_than_hot_window_is_unknown():
    assert upstream_verdict(NOW - 1801, 0.0, None, 0, NOW) == (
        UPSTREAM_UNKNOWN,
        None,
    )


def test_single_429_is_cooling_not_dead():
    verdict, cooling_until = upstream_verdict(0.0, NOW, 600, 0, NOW)
    assert verdict == UPSTREAM_COOLING
    assert cooling_until == NOW + 600


def test_429_without_retry_after_caps_at_1200():
    verdict, cooling_until = upstream_verdict(0.0, NOW, None, 0, NOW)
    assert verdict == UPSTREAM_COOLING
    assert cooling_until == NOW + 1200


def test_429_long_retry_after_is_capped_at_last_429_plus_1200():
    verdict, cooling_until = upstream_verdict(0.0, NOW, 5000, 0, NOW + 10)
    assert verdict == UPSTREAM_COOLING
    assert cooling_until == NOW + 1200


def test_expired_cooling_is_unknown_and_retestable():
    # retry_after=60 : la fenêtre expire à NOW+60, le re-test est possible.
    assert upstream_verdict(0.0, NOW, 60, 0, NOW + 61) == (UPSTREAM_UNKNOWN, None)


def test_three_transport_failures_are_dead():
    assert upstream_verdict(0.0, 0.0, None, 3, NOW) == (UPSTREAM_DEAD, None)


def test_two_transport_failures_are_not_dead():
    assert upstream_verdict(0.0, 0.0, None, 2, NOW) == (UPSTREAM_UNKNOWN, None)


def test_recent_200_beats_older_transport_failures():
    assert upstream_verdict(NOW - 5, 0.0, None, 5, NOW) == (UPSTREAM_HOT, None)


def test_newer_429_supersedes_older_200():
    verdict, cooling_until = upstream_verdict(NOW - 100, NOW - 10, 600, 0, NOW)
    assert verdict == UPSTREAM_COOLING
    # min(last_429 + 1200, last_429 + 600) = min(NOW + 1190, NOW + 590).
    assert cooling_until == NOW + 590


def test_ours_used_and_quota_per_ip_are_not_verdict_inputs():
    """Le verdict ne prend ni ``used`` ni ``quota_per_ip`` dans sa signature."""
    import inspect

    params = list(inspect.signature(upstream_verdict).parameters)
    assert params == [
        "last_ok_at",
        "last_429_at",
        "retry_after_s",
        "transport_failures",
        "now",
    ]
    assert "used" not in params
    assert "quota_per_ip" not in params


# ── T312 — garde reset_at + un seul re-test à l'expiration ────────────────


class _Station:
    def __init__(self, sid: int, ip: str):
        self._station = sid
        self.status = "connected"
        self.current_ip = ip
        self._config = {}
        self._current_country = "Switzerland"


def _pool() -> FreeIPPool:
    """Double minimal (cf. ``tests/test_quota_bucket_pool.py``), no network."""
    pool = FreeIPPool.__new__(FreeIPPool)
    pool._per = {}
    pool._quota_enabled = False
    pool._strict_health = False
    pool._ledger = QuotaLedger(persist=False, max_block_s=1200.0)
    pool._ledger_enabled = True
    pool.latency_engine = None
    pool._stations = []
    return pool


def test_reset_at_blocks_until_expiry_then_allows_exactly_one_retest():
    pool = _pool()
    station = _Station(1, IP)
    pool._ledger.note_result(
        IP,
        MODEL,
        429,
        error_type="FreeUsageLimitError",
        retry_after=600,
        now=NOW,
    )
    assert pool._ledger.reset_at(IP, MODEL, now=NOW) == NOW + 600

    # Aucune tentative avant reset_at.
    for probe_at in (NOW, NOW + 1, NOW + 599):
        assert pool._ledger_usable(MODEL, station, now=probe_at) == (
            False,
            "daily-bucket-exhausted",
        )

    # À l'expiration, une seule sélection/retest passe.
    assert pool._ledger_usable(MODEL, station, now=NOW + 600) == (True, None)

    # Le premier envoi réel consomme le re-test.
    pool._ledger_note_request(MODEL, station, now=NOW + 600)
    assert pool._ledger_usable(MODEL, station, now=NOW + 601) == (
        False,
        "retest-in-flight",
    )

    # Un second envoi ne rouvre pas un re-test.
    pool._ledger_note_request(MODEL, station, now=NOW + 602)
    assert pool._ledger_usable(MODEL, station, now=NOW + 603) == (
        False,
        "retest-in-flight",
    )

    # Un 200 clôt le cycle : l'IP est prouvée.
    pool._ledger_note_result(MODEL, station, 200, now=NOW + 604)
    assert pool._ledger_usable(MODEL, station, now=NOW + 605) == (True, None)


def test_second_quota_429_rearms_one_retest_per_cycle():
    pool = _pool()
    station = _Station(1, IP)
    pool._ledger.note_result(
        IP,
        MODEL,
        429,
        error_type="FreeUsageLimitError",
        retry_after=600,
        now=NOW,
    )
    assert pool._ledger_usable(MODEL, station, now=NOW) == (
        False,
        "daily-bucket-exhausted",
    )
    assert pool._ledger_usable(MODEL, station, now=NOW + 600) == (True, None)
    pool._ledger_note_request(MODEL, station, now=NOW + 600)
    assert pool._ledger_usable(MODEL, station, now=NOW + 601) == (
        False,
        "retest-in-flight",
    )

    # Le re-test échoue en quota -> nouveau cycle, une seule chance de plus.
    pool._ledger_note_result(
        MODEL,
        station,
        429,
        error_type="FreeUsageLimitError",
        retry_after=600,
        now=NOW + 601,
    )
    assert pool._ledger_usable(MODEL, station, now=NOW + 602) == (
        False,
        "daily-bucket-exhausted",
    )
    assert pool._ledger_usable(MODEL, station, now=NOW + 1201) == (True, None)


def test_retest_gate_future_reset_is_not_eligible():
    pool = _pool()
    pool._retest_note_reset(MODEL, IP, NOW + 600)
    assert pool._retest_gate(MODEL, IP, now=NOW + 10) == (False, "cooling")
    assert pool._retest_gate(MODEL, IP, now=NOW + 600) == (True, None)
    pool._retest_note_sent(MODEL, IP, now=NOW + 600)
    assert pool._retest_gate(MODEL, IP, now=NOW + 601) == (
        False,
        "retest-in-flight",
    )


def test_transient_429_on_a_fresh_ip_does_not_block():
    pool = _pool()
    station = _Station(1, "198.51.100.21")
    pool._ledger.note_result(
        "198.51.100.21",
        MODEL,
        200,
        now=NOW,
    )
    pool._ledger.note_result(
        "198.51.100.21",
        MODEL,
        429,
        error_type="RateLimitError",
        retry_after=30,
        now=NOW + 1,
    )
    # Un 429 transitoire n'épuise pas le bucket : l'IP reste utilisable.
    assert pool._ledger_usable(MODEL, station, now=NOW + 2) == (True, None)


# ── T309/023 — la réserve prouvée passe devant ────────────────────────────


def test_selection_prefers_proven_reserve(monkeypatch):
    monkeypatch.setattr(pool_mod, "_proven_reserve_ips", lambda: {"203.0.113.7"})
    pool = _pool()
    proven = _Station(1, "203.0.113.7")
    unproven = _Station(2, "198.51.100.99")
    ordered = pool._prefer_proven_reserve([unproven, proven])
    assert ordered == [proven, unproven]


def test_selection_keeps_order_without_reserve(monkeypatch):
    monkeypatch.setattr(pool_mod, "_proven_reserve_ips", lambda: set())
    pool = _pool()
    first = _Station(1, "198.51.100.1")
    second = _Station(2, "198.51.100.2")
    assert pool._prefer_proven_reserve([first, second]) == [first, second]
