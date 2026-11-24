"""Respect strict de ``reset_at`` et re-test unique — feature 019, T312.

Le ledger doit interdire toute tentative avant l'échéance, appliquer le
plafond ``ledger_max_block_s`` de façon visible, puis ne laisser passer qu'un
seul re-test de l'IP cooling.
"""

from __future__ import annotations

from free.quota_ledger import QuotaLedger
from scripts.fresh_chain_supervisor import (
    _ledger_reset_at,
    _station_cooling,
    _station_exhausted,
)

MODEL = "minimax-m3.1-flash-free"
IP = "198.51.100.20"
NOW = 1_800_000_000.0


def _ledger(tmp_path, *, cap: float = 1200.0) -> QuotaLedger:
    return QuotaLedger(
        path=str(tmp_path / "quota.db"), persist=False, max_block_s=cap
    )


def _cool_bucket(ip: str = IP, reset_at: float = NOW + 600.0) -> dict:
    return {
        "ip": ip,
        "bucket": "default",
        "exhausted": True,
        "reset_at": reset_at,
        "last_status": 429,
        "last_error": "FreeUsageLimitError",
        "retest_pending": False,
    }


def _health(bucket: dict, *, cap: float = 1200.0) -> dict:
    return {"quota_ledger": {"max_block_s": cap, "buckets": [bucket]}}


def test_zero_attempt_before_reset_then_exactly_one_retest(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.note_result(
        IP, MODEL, 429, error_type="FreeUsageLimitError", retry_after=600, now=NOW
    )

    for probe_at in (NOW, NOW + 1, NOW + 599):
        ok, reason = ledger.usable(IP, MODEL, now=probe_at)
        assert ok is False and reason == "daily-bucket-exhausted"
        verdict = ledger.verdict(IP, MODEL, now=probe_at)
        assert verdict["verdict"] == "cooling"
        assert verdict["cooling_until"] == NOW + 600

    at_reset = NOW + 600
    ok, reason = ledger.usable(IP, MODEL, now=at_reset)
    assert ok is True and reason is None
    verdict = ledger.verdict(IP, MODEL, now=at_reset)
    assert verdict["evidence"] == "cooling-expired-retest"

    ledger.note_request(IP, MODEL, now=at_reset)
    ok, reason = ledger.usable(IP, MODEL, now=at_reset + 1)
    assert ok is False and reason == "retest-in-flight"
    assert ledger.verdict(IP, MODEL, now=at_reset + 1)["evidence"] == "retest-in-flight"

    ledger.note_request(IP, MODEL, now=at_reset + 2)
    ok, _ = ledger.usable(IP, MODEL, now=at_reset + 3)
    assert ok is False, "le second envoi ne rouvre pas un re-test"

    ledger.note_result(IP, MODEL, 200, now=at_reset + 4)
    ok, reason = ledger.usable(IP, MODEL, now=at_reset + 5)
    assert ok is True and reason is None
    assert ledger.verdict(IP, MODEL, now=at_reset + 5)["verdict"] == "hot"


def test_second_429_rearms_one_retest_per_cycle(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.note_result(
        IP, MODEL, 429, error_type="FreeUsageLimitError", retry_after=600, now=NOW
    )
    ledger.note_request(IP, MODEL, now=NOW + 600)
    ledger.note_result(
        IP,
        MODEL,
        429,
        error_type="FreeUsageLimitError",
        retry_after=600,
        now=NOW + 601,
    )
    assert ledger.usable(IP, MODEL, now=NOW + 602)[0] is False
    assert ledger.usable(IP, MODEL, now=NOW + 1201)[0] is True


def test_cap_bounds_midnight_and_is_visible(tmp_path):
    ledger = _ledger(tmp_path, cap=1200.0)
    ledger.note_result(
        IP, MODEL, 429, error_type="FreeUsageLimitError", retry_after=0, now=NOW
    )
    assert ledger.reset_at(IP, MODEL, now=NOW + 1) == NOW + 1200
    row = ledger.snapshot(now=NOW + 1)[0]
    assert row["max_block_s"] == 1200.0
    assert row["reset_at"] == NOW + 1200

    uncapped = QuotaLedger(
        path=str(tmp_path / "nocap.db"), persist=False, max_block_s=0.0
    )
    uncapped.note_result(
        IP, MODEL, 429, error_type="FreeUsageLimitError", retry_after=0, now=NOW
    )
    assert uncapped.reset_at(IP, MODEL, now=NOW + 1) >= NOW + 1000


def test_supervisor_blocks_station_until_reset_at():
    station = {"station": 1, "current_ip": IP}
    health = _health(_cool_bucket())
    decision = _station_cooling(station, health, now=NOW + 10)
    assert decision["allow"] is False
    assert decision["remaining_s"] == 590.0
    assert decision["cap_s"] == 1200.0
    assert _ledger_reset_at(health, ip=IP, now=NOW + 10) == NOW + 600
    assert _station_exhausted(station, health, [MODEL], now=NOW + 10) is True

    expired = _health(
        {
            "ip": IP,
            "bucket": "default",
            "exhausted": False,
            "reset_at": 0.0,
            "last_status": 429,
            "last_error": "FreeUsageLimitError",
            "retest_pending": True,
        }
    )
    assert _station_cooling(station, expired, now=NOW + 601)["allow"] is True
    assert _station_exhausted(station, expired, [MODEL], now=NOW + 601) is False


def test_station_on_other_ip_is_not_blocked():
    station = {"station": 2, "current_ip": "203.0.113.9"}
    decision = _station_cooling(station, _health(_cool_bucket()), now=NOW + 10)
    assert decision["allow"] is True
