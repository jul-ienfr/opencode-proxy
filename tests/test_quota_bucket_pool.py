"""Intégration pool ↔ ledger : un bucket journalier épuisé bloque l'IP."""

from __future__ import annotations

from types import SimpleNamespace

from free.pool import FreeIPPool
from free.quota_ledger import QuotaLedger

MODEL = "deepseek-v4.1-flash-free"
FREE_BODY = (
    '{"type":"error","error":{"type":"FreeUsageLimitError","message":'
    '"Free usage limit reached. Free models work in OpenCode ..."}}'
)


class _Station:
    def __init__(self, sid: int, ip: str):
        self._station = sid
        self.status = "connected"
        self.current_ip = ip
        self._config = {}
        self._current_country = "Switzerland"


def _pool() -> FreeIPPool:
    pool = FreeIPPool.__new__(FreeIPPool)
    pool._per = {}
    pool._quota_enabled = False
    pool._strict_health = False
    pool._ledger = QuotaLedger(persist=False)
    pool._ledger_enabled = True
    pool.latency_engine = None
    return pool


def test_exhausted_bucket_blocks_station_but_not_tunnel_gate():
    pool = _pool()
    st = _Station(1, "10.0.0.1")
    assert pool._station_usable(st, exclude_approaching=False, model=MODEL) is True

    pool._ledger.note_result(
        "10.0.0.1",
        MODEL,
        429,
        error_type="FreeUsageLimitError",
        retry_after=3600,
    )

    assert pool._station_usable(st, exclude_approaching=False, model=MODEL) is False
    # Le gate de warmup (model=None) ne doit pas confondre « bucket épuisé »
    # avec « tunnel mort » : la station reste routable pour le diagnostic.
    assert pool._station_usable(st, exclude_approaching=False, model=None) is True
    assert pool.model_usable(MODEL, st) == (False, "daily-bucket-exhausted")
    assert pool.quota_bucket_exhausted(MODEL) is True


def test_new_ip_is_a_new_bucket():
    pool = _pool()
    st = _Station(1, "10.0.0.2")
    pool._ledger.note_result(
        "10.0.0.2",
        MODEL,
        429,
        error_type="FreeUsageLimitError",
        retry_after=3600,
    )
    assert pool.model_usable(MODEL, st)[0] is False

    st.current_ip = "10.0.0.3"
    assert pool.model_usable(MODEL, st) == (True, None)
    assert pool.quota_bucket_exhausted(MODEL) is True  # l'ancienne IP reste marquée


def test_note_model_ip_marks_and_success_clears():
    pool = _pool()
    st = _Station(2, "10.0.0.4")

    pool.note_model_ip(MODEL, st, 429, body=FREE_BODY, retry_after=3600)
    assert pool.model_usable(MODEL, st)[0] is False

    pool.note_model_ip(MODEL, st, 200)
    assert pool.model_usable(MODEL, st) == (True, None)


def test_transient_429_does_not_block():
    pool = _pool()
    st = _Station(3, "10.0.0.5")

    pool.note_model_ip(
        MODEL,
        st,
        429,
        body='{"error":{"message":"Endpoint is unavailable"}}',
        retry_after=30,
    )
    assert pool.model_usable(MODEL, st) == (True, None)


def test_status_snapshot_exposes_ledger():
    pool = _pool()
    st = _Station(4, "10.0.0.6")
    pool.note_model_ip(MODEL, st, 429, body=FREE_BODY, retry_after=3600)

    status = pool.quota_bucket_status(MODEL)
    assert status["enabled"] is True
    assert status["exhausted"] is True
    assert status["earliest_reset_at"] is not None
    assert len(status["buckets"]) == 1
    assert status["buckets"][0]["ip"] == "10.0.0.6"


def test_ledger_fail_open_when_disabled():
    pool = _pool()
    pool._ledger_enabled = False
    st = _Station(5, "10.0.0.7")
    pool._ledger.note_result(
        "10.0.0.7",
        MODEL,
        429,
        error_type="FreeUsageLimitError",
        retry_after=3600,
    )
    assert pool.model_usable(MODEL, st) == (True, None)


# Keep SimpleNamespace imported for future fake-VPN fixtures without lint noise.
_ = SimpleNamespace
