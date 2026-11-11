"""Characterization tests for the free quota ledger (T158).

The ledger is the authoritative memory of the server-side daily bucket
``(egress IP, UTC day, model bucket)``.  These tests pin the behaviour the
pool relies on before the quota slice is extracted: exhaustion, reset,
parking, reserve, model bucketing, clearing and persistence.
"""

from __future__ import annotations

import time

from free.quota_ledger import (
    QuotaLedger,
    bucket_for,
    next_utc_midnight,
    quota_block,
    server_ip_key,
    utc_day,
)

NOW = 1_794_000_000.0  # fixed clock; exact date is irrelevant to the ledger


def _ledger(tmp_path, **kwargs) -> QuotaLedger:
    kwargs.setdefault("persist", False)
    kwargs.setdefault("clock", lambda: NOW)
    return QuotaLedger(path=str(tmp_path / "ledger.json"), **kwargs)


def test_unknown_ip_is_usable_without_headroom_limit(tmp_path):
    ledger = _ledger(tmp_path)
    assert ledger.usable("203.0.113.9", "m") == (True, None)
    assert ledger.is_exhausted("203.0.113.9", "m") is False
    assert ledger.headroom("203.0.113.9", "m") == 10**9
    assert ledger.reset_at("203.0.113.9", "m") is None


def test_note_request_consumes_headroom_and_reserve(tmp_path):
    ledger = _ledger(tmp_path, daily_limit=10, reserve_pct=0.10)
    for _ in range(8):
        ledger.note_request("203.0.113.9", "m")
    assert ledger.usable("203.0.113.9", "m") == (True, None)
    assert ledger.headroom("203.0.113.9", "m") == 2

    # reserve = round(10 * 0.10) = 1 -> usable while requests < 10 - 1
    ledger.note_request("203.0.113.9", "m")
    assert ledger.usable("203.0.113.9", "m") == (False, "daily-bucket-reserve")
    assert ledger.headroom("203.0.113.9", "m") == 1


def test_quota_429_exhausts_until_retry_after(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.note_result(
        "203.0.113.9",
        "m",
        429,
        error_type="FreeUsageLimitError",
        retry_after=600,
    )
    assert ledger.is_exhausted("203.0.113.9", "m") is True
    assert ledger.usable("203.0.113.9", "m") == (False, "daily-bucket-exhausted")
    assert ledger.headroom("203.0.113.9", "m") == 0
    assert ledger.reset_at("203.0.113.9", "m") == NOW + 600
    row = ledger.snapshot(now=NOW)[0]
    assert row["retry_in_s"] == 600.0
    assert row["source"] == "observed"


def test_429_without_retry_after_resets_at_utc_midnight(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.mark_exhausted("203.0.113.9", "m")
    assert ledger.reset_at("203.0.113.9", "m") == next_utc_midnight(NOW)


def test_success_clears_exhaustion(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.mark_exhausted("203.0.113.9", "m", retry_after=7200)
    ledger.note_result("203.0.113.9", "m", 200)
    assert ledger.is_exhausted("203.0.113.9", "m") is False
    assert ledger.usable("203.0.113.9", "m") == (True, None)
    assert ledger.reset_at("203.0.113.9", "m") is None


def test_transient_429_does_not_exhaust(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.note_result("203.0.113.9", "m", 429, error_type="RateLimitError")
    state = ledger.snapshot(now=NOW)[0]
    assert state["transient_429"] == 1
    assert state["exhausted"] is False
    assert ledger.usable("203.0.113.9", "m") == (True, None)


def test_max_block_s_caps_effective_reset(tmp_path):
    ledger = _ledger(tmp_path, max_block_s=120)
    ledger.mark_exhausted("203.0.113.9", "m", retry_after=7200)
    assert ledger.reset_at("203.0.113.9", "m") == NOW + 120
    assert ledger.snapshot(now=NOW)[0]["retry_after"] == 7200.0


def test_bucket_expires_at_reset_at(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.mark_exhausted("203.0.113.9", "m", retry_after=60)
    assert ledger.usable("203.0.113.9", "m", now=NOW + 59) == (
        False,
        "daily-bucket-exhausted",
    )
    assert ledger.usable("203.0.113.9", "m", now=NOW + 60) == (True, None)
    assert ledger.exhausted_ips(now=NOW + 60) == set()


def test_exhausted_and_parked_ips_use_stored_keys(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.mark_exhausted("203.0.113.9", "m", retry_after=600)
    ledger.mark_exhausted("2001:db8::1", "m", retry_after=900)
    ledger.mark_exhausted("198.51.100.7", "m", retry_after=7200)
    raw = {"203.0.113.9", "2001:db8::1", "198.51.100.7"}
    assert ledger.exhausted_ips() == raw
    assert ledger.parked_ips(window_s=1800) == {"203.0.113.9", "2001:db8::1"}
    # Consumers normalise the stored keys into quota blocks.
    assert {quota_block(item) for item in ledger.exhausted_ips()} == {
        "203.0.113.9/32",
        "2001:db8::/64",
        "198.51.100.7/32",
    }


def test_model_prefix_mode_separates_buckets(tmp_path):
    ledger = _ledger(tmp_path, bucket_mode="model_prefix")
    ledger.mark_exhausted("203.0.113.9", "alpha-1", retry_after=600)
    assert ledger.usable("203.0.113.9", "alpha-1") == (
        False,
        "daily-bucket-exhausted",
    )
    assert ledger.usable("203.0.113.9", "beta-1") == (True, None)


def test_earliest_reset_can_filter_bucket(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.mark_exhausted("203.0.113.9", "alpha", retry_after=900)
    ledger.mark_exhausted("198.51.100.7", "beta", retry_after=300)
    assert ledger.earliest_reset() == NOW + 300
    assert ledger.earliest_reset(bucket="default") == NOW + 300
    assert ledger.earliest_reset(now=NOW + 300) == NOW + 900
    assert ledger.earliest_reset(now=NOW + 900) is None


def test_clear_by_ip_and_model(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.mark_exhausted("203.0.113.9", "m", retry_after=600)
    ledger.mark_exhausted("198.51.100.7", "m", retry_after=600)
    assert ledger.clear(ip="203.0.113.9") == 1
    assert ledger.usable("203.0.113.9", "m") == (True, None)
    assert ledger.usable("198.51.100.7", "m") == (False, "daily-bucket-exhausted")
    assert ledger.clear(model="m") == 1
    assert ledger.clear() == 0


def test_snapshot_is_sorted_by_last_seen(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.note_request("203.0.113.9", "m", now=NOW - 10)
    ledger.note_request("198.51.100.7", "m", now=NOW)
    rows = ledger.snapshot(now=NOW)
    assert [row["ip"] for row in rows] == ["198.51.100.7", "203.0.113.9"]


def test_json_persistence_round_trips_exhaustion(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = QuotaLedger(path=str(path), persist=True, clock=lambda: NOW)
    ledger.mark_exhausted("203.0.113.9", "m", retry_after=600)
    ledger.flush()

    reopened = QuotaLedger(path=str(path), persist=True, clock=lambda: NOW)
    assert reopened.usable("203.0.113.9", "m") == (
        False,
        "daily-bucket-exhausted",
    )
    assert reopened.reset_at("203.0.113.9", "m") == NOW + 600


def test_server_ip_key_and_quota_block_normalization():
    assert server_ip_key("1.2.3.4") == "1.2.3.4"
    assert server_ip_key(" 1.2.3.4, 5.6.7.8") == "1.2.3.4"
    assert server_ip_key("[2001:db8::1]:443") == "2001:db8::1"
    assert server_ip_key("2001:db8:1:2:3:4:5:6") == "2001:db8:1:2"
    assert server_ip_key("") == "unknown"
    assert quota_block("2001:db8::1") == "2001:db8::/64"
    assert quota_block("1.2.3.4") == "1.2.3.4/32"
    assert quota_block("not-an-ip") is None
    assert bucket_for("deepseek-v4.1-flash-free") == "default"
    assert bucket_for("deepseek-v4.1-flash-free", mode="model_prefix") == "de"


def test_utc_day_matches_midnight_boundary():
    midnight = next_utc_midnight(NOW)
    assert midnight % 86_400.0 == 0
    assert utc_day(NOW) != utc_day(midnight)
    assert time.gmtime(midnight).tm_hour == 0
