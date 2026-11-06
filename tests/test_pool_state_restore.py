"""Restauration de l'état pool : compteurs oui, bad-marks transitoires non."""

from __future__ import annotations

from free.pool import FreeIPPool


def test_load_pool_state_ignores_transient_bad_marks(monkeypatch, tmp_path):
    pool = object.__new__(FreeIPPool)
    pool._stations = []
    pool._per = {}
    pool._dirty_state = True
    pool._pool_state_file = lambda: str(tmp_path / "free_pool.json")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(
        "free.chain_state.load_json_state",
        lambda *args, **kwargs: {
            "per": {
                "-1": {
                    "request_count": 4,
                    "bad_remaining": 60.0,
                    "bad_cause": "timeout",
                    "ip_stats": {"2a09::1": {"requests": 4}},
                }
            }
        },
    )
    pool.load_pool_state()
    per = pool._per[-1]
    assert per["request_count"] == 4
    assert per["ip_stats"]["2a09::1"]["requests"] == 4
    assert per["bad_until"] is None
    assert per["bad_cause"] is None
