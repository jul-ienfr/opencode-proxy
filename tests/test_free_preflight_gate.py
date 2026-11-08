"""Preflight free : attente interne des cooldowns courts (fix client desktop)."""

from __future__ import annotations

import asyncio

import opencode


def test_gate_waits_short_cooldown_then_continues(monkeypatch) -> None:
    calls = {"n": 0}

    def _retry_after(_model: str, *, min_wait: float = 5.0):
        calls["n"] += 1
        return 0.05 if calls["n"] == 1 else None

    monkeypatch.setattr(opencode, "_free_preflight_retry_after", _retry_after)
    monkeypatch.setattr(opencode, "_free_preflight_inline_wait_s", lambda: 5.0)

    assert asyncio.run(opencode._free_preflight_gate("m", route="chat")) is None
    assert calls["n"] == 2


def test_gate_returns_429_when_wait_exceeds_budget(monkeypatch) -> None:
    monkeypatch.setattr(
        opencode, "_free_preflight_retry_after", lambda _m, **_k: 50.0
    )
    monkeypatch.setattr(opencode, "_free_preflight_inline_wait_s", lambda: 5.0)

    resp = asyncio.run(opencode._free_preflight_gate("m", route="chat"))
    assert resp is not None
    assert resp.status_code == 429
    assert resp.headers.get("retry-after") == "50"
