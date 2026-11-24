"""Verdict upstream vs compteur local — feature 019, tâche T309.

``ours_used`` reste informatif ; seul un 429 quota ou trois échecs transport
changent le verdict.
"""

from __future__ import annotations

from free.quota_ledger import QuotaLedger

MODEL = "minimax-m3.1-flash-free"


def _ledger(tmp_path):
    return QuotaLedger(path=str(tmp_path / "quota.db"), persist=False)


def test_used_one_with_recent_200_is_hot(tmp_path):
    ledger = _ledger(tmp_path)
    now = 1_800_000_000.0
    ledger.note_request("198.51.100.10", MODEL, now=now)
    ledger.note_result("198.51.100.10", MODEL, 200, now=now + 1)
    verdict = ledger.verdict("198.51.100.10", MODEL, now=now + 2)
    assert verdict["verdict"] == "hot"
    assert verdict["ours_used"] == 1, "notre compteur ne décide pas du verdict"


def test_single_429_is_cooling_not_dead(tmp_path):
    ledger = _ledger(tmp_path)
    now = 1_800_000_000.0
    ledger.note_result(
        "198.51.100.11",
        MODEL,
        429,
        error_type="FreeUsageLimitError",
        retry_after=600,
        now=now,
    )
    verdict = ledger.verdict("198.51.100.11", MODEL, now=now + 10)
    assert verdict["verdict"] == "cooling"
    assert verdict["cooling_until"] == now + 600
    assert verdict["evidence"] == "429-quota"


def test_three_transport_failures_are_dead(tmp_path):
    ledger = _ledger(tmp_path)
    now = 1_800_000_000.0
    for i in range(3):
        ledger.note_result("198.51.100.12", MODEL, 0, error_type="ConnectError", now=now + i)
    verdict = ledger.verdict("198.51.100.12", MODEL, now=now + 5)
    assert verdict["verdict"] == "dead"
    assert verdict["transport_failures"] == 3


def test_success_clears_transport_failures(tmp_path):
    ledger = _ledger(tmp_path)
    now = 1_800_000_000.0
    for i in range(2):
        ledger.note_result("198.51.100.13", MODEL, 500, now=now + i)
    ledger.note_result("198.51.100.13", MODEL, 200, now=now + 3)
    ledger.note_result("198.51.100.13", MODEL, 500, now=now + 4)
    verdict = ledger.verdict("198.51.100.13", MODEL, now=now + 5)
    assert verdict["verdict"] == "hot", "un 200 remet le compteur transport à zéro"


def test_expired_cooling_is_retested_not_hot(tmp_path):
    ledger = _ledger(tmp_path)
    now = 1_800_000_000.0
    ledger.note_result(
        "198.51.100.14",
        MODEL,
        429,
        error_type="FreeUsageLimitError",
        retry_after=60,
        now=now,
    )
    verdict = ledger.verdict("198.51.100.14", MODEL, now=now + 61)
    assert verdict["verdict"] == "unknown"
    assert verdict["evidence"] == "cooling-expired-retest"
    assert verdict["cooling_until"] is None
