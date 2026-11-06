"""Contrats du ledger de quota free ``(IP, jour UTC, bucket)``."""

from __future__ import annotations

import json

from free.quota_ledger import (
    QuotaLedger,
    bucket_for,
    next_utc_midnight,
    server_ip_key,
    utc_day,
)

NOW = 1_700_000_000.0  # 2023-11-14T22:13:20Z
MIDNIGHT = 1_700_006_400.0  # 2023-11-15T00:00:00Z
MODEL = "deepseek-v4.1-flash-free"
FREE_BODY = (
    '{"type":"error","error":{"type":"FreeUsageLimitError","message":'
    '"Free usage limit reached. Free models work in OpenCode ..."}}'
)


def test_server_ip_key_matches_handler():
    assert server_ip_key("187.40.18.94") == "187.40.18.94"
    assert server_ip_key(" 187.40.18.94 ") == "187.40.18.94"
    assert server_ip_key("2001:db8:abcd:1234:5678:9abc:def0:1111") == "2001:db8:abcd:1234"
    assert server_ip_key("2001:db8::1") == "2001:db8::1"
    assert server_ip_key("[2001:db8:abcd:1234::1]:443") == "2001:db8:abcd:1234"
    assert server_ip_key("1.2.3.4, 5.6.7.8") == "1.2.3.4"
    assert server_ip_key("") == "unknown"
    assert server_ip_key(None) == "unknown"


def test_utc_day_and_reset():
    assert utc_day(NOW) == "20231114"
    assert next_utc_midnight(NOW) == MIDNIGHT
    assert next_utc_midnight(MIDNIGHT) == MIDNIGHT + 86_400


def test_bucket_mode():
    assert bucket_for(MODEL) == "default"
    assert bucket_for(MODEL, "model_prefix") == "de"
    assert bucket_for("flint-40b-free", "model_prefix") == "fl"
    assert bucket_for("", "model_prefix") == "default"


def test_free_usage_429_marks_bucket_until_midnight():
    ledger = QuotaLedger(persist=False, clock=lambda: NOW)
    ledger.note_request("10.0.0.7", MODEL)
    ledger.note_result(
        "10.0.0.7",
        MODEL,
        429,
        error_type="FreeUsageLimitError",
        retry_after=MIDNIGHT - NOW,
        body=FREE_BODY,
    )
    assert ledger.is_exhausted("10.0.0.7", MODEL) is True
    assert ledger.usable("10.0.0.7", MODEL) == (False, "daily-bucket-exhausted")
    assert ledger.reset_at("10.0.0.7", MODEL) == MIDNIGHT
    assert ledger.headroom("10.0.0.7", MODEL) == 0


def test_retry_after_cap_reprobes_before_midnight():
    """[2026-11-06] RA = distance à 00:00 UTC, pas la vraie recovery.

    afree-proxy mesure RA=20 h 46 et un 200 sur la même IP 18 min plus tard.
    Quand ``max_block_s`` est configuré, le bucket reste marqué épuisé mais
    redevient candidate au plafond ; la valeur brute reste visible.
    """
    ledger = QuotaLedger(persist=False, clock=lambda: NOW, max_block_s=1200)
    ledger.note_result(
        "10.0.0.7",
        MODEL,
        429,
        error_type="FreeUsageLimitError",
        retry_after=MIDNIGHT - NOW,
        body=FREE_BODY,
    )
    assert ledger.reset_at("10.0.0.7", MODEL) == NOW + 1200
    assert ledger.is_exhausted("10.0.0.7", MODEL) is True
    # Au plafond, l'IP redevient testable sans attendre minuit.
    ledger.clock = lambda: NOW + 1201
    assert ledger.is_exhausted("10.0.0.7", MODEL) is False


def test_same_ip_different_model_shares_default_bucket():
    ledger = QuotaLedger(persist=False, clock=lambda: NOW)
    ledger.note_result(
        "10.0.0.8",
        "mimo-v2.6-flash-free",
        429,
        error_type="FreeUsageLimitError",
        retry_after=MIDNIGHT - NOW,
    )
    assert ledger.is_exhausted("10.0.0.8", MODEL) is True


def test_transient_429_does_not_mark_bucket():
    ledger = QuotaLedger(persist=False, clock=lambda: NOW)
    ledger.note_result(
        "10.0.0.9",
        MODEL,
        429,
        error_type="Transient429",
        retry_after=30,
        body='{"error":{"message":"Endpoint is unavailable"}}',
    )
    assert ledger.is_exhausted("10.0.0.9", MODEL) is False
    assert ledger.usable("10.0.0.9", MODEL) == (True, None)


def test_success_clears_exhaustion():
    ledger = QuotaLedger(persist=False, clock=lambda: NOW)
    ledger.note_result(
        "10.0.0.10",
        MODEL,
        429,
        error_type="FreeUsageLimitError",
        retry_after=MIDNIGHT - NOW,
    )
    assert ledger.is_exhausted("10.0.0.10", MODEL) is True
    ledger.note_result("10.0.0.10", MODEL, 200)
    assert ledger.is_exhausted("10.0.0.10", MODEL) is False


def test_expiry_by_time():
    ledger = QuotaLedger(persist=False, clock=lambda: NOW)
    ledger.note_result(
        "10.0.0.11",
        MODEL,
        429,
        error_type="FreeUsageLimitError",
        retry_after=1000,
    )
    assert ledger.usable("10.0.0.11", MODEL, now=NOW + 500)[0] is False
    assert ledger.usable("10.0.0.11", MODEL, now=NOW + 1001)[0] is True


def test_model_prefix_mode_separates_buckets():
    ledger = QuotaLedger(persist=False, bucket_mode="model_prefix", clock=lambda: NOW)
    ledger.note_result(
        "10.0.0.12",
        "deepseek-v4.1-flash-free",
        429,
        error_type="FreeUsageLimitError",
        retry_after=MIDNIGHT - NOW,
    )
    assert ledger.is_exhausted("10.0.0.12", "deepseek-v4.1-flash-free") is True
    assert ledger.is_exhausted("10.0.0.12", "flint-40b-free") is False


def test_earliest_reset_filters_bucket():
    ledger = QuotaLedger(persist=False, clock=lambda: NOW)
    ledger.note_result(
        "10.0.0.13",
        MODEL,
        429,
        error_type="FreeUsageLimitError",
        retry_after=MIDNIGHT - NOW,
    )
    assert ledger.earliest_reset() == MIDNIGHT
    assert ledger.earliest_reset(bucket="default") == MIDNIGHT
    assert ledger.earliest_reset(bucket="fl") is None


def test_persistence_round_trip(tmp_path):
    path = tmp_path / "quota_ledger.json"
    ledger = QuotaLedger(path=str(path), persist=True, clock=lambda: NOW)
    ledger.note_request("10.0.0.14", MODEL)
    ledger.note_result(
        "10.0.0.14",
        MODEL,
        429,
        error_type="FreeUsageLimitError",
        retry_after=MIDNIGHT - NOW,
    )
    ledger.flush()

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert any(item["ip"] == "10.0.0.14" for item in raw["items"])

    restored = QuotaLedger(path=str(path), persist=True, clock=lambda: NOW)
    assert restored.is_exhausted("10.0.0.14", MODEL) is True
    assert restored.reset_at("10.0.0.14", MODEL) == MIDNIGHT
    assert restored.headroom("10.0.0.14", MODEL) == 0


def test_snapshot_shape():
    ledger = QuotaLedger(persist=False, clock=lambda: NOW)
    ledger.note_result(
        "10.0.0.15",
        MODEL,
        429,
        error_type="FreeUsageLimitError",
        retry_after=MIDNIGHT - NOW,
    )
    rows = ledger.snapshot()
    assert len(rows) == 1
    row = rows[0]
    assert row["ip"] == "10.0.0.15"
    assert row["day"] == "20231114"
    assert row["bucket"] == "default"
    assert row["exhausted"] is True
    assert row["reset_at_iso"] == "2023-11-15T00:00:00Z"
    assert row["retry_in_s"] == MIDNIGHT - NOW
