"""Tests alignement client officiel (timeouts, bêtas Anthropic, garde warn-only).

- TTFB watchdog : défaut 300 s (headerTimeout officiel), pas 90.
- Idle intra-stream : 300 s (chunkTimeout officiel), pas 120.
- `_ensure_anthropic_beta` : union défaut proxy + bêtas client (pas d'écrasement).
- `_warn_huge_context` : WARN O(1) sans refus, silencieux sous le seuil.
"""

import types

import opencode as oc


def _reset_betas():
    oc._current_client_anthropic_betas.set({})


def test_ttfb_watchdog_default_300(monkeypatch):
    monkeypatch.setattr(oc, "yaml_get", lambda *a, **k: k.get("default", {}))
    assert oc._ttfb_watchdog_timeout_s() == 300.0


def test_sse_idle_timeout_default_300():
    assert oc._SSE_IDLE_TIMEOUT == 300.0


def test_ensure_beta_default_unchanged_without_client():
    _reset_betas()
    try:
        out = oc._ensure_anthropic_beta({"x-api-key": "k"})
        assert out["anthropic-beta"] == oc._ANTHROPIC_BETA
    finally:
        _reset_betas()


def test_ensure_beta_unions_client_betas():
    _reset_betas()
    try:
        oc._capture_client_anthropic_betas(
            {"anthropic-beta": "prompt-caching-2024-07-31", "anthropic-version": "2023-06-01"}
        )
        out = oc._ensure_anthropic_beta({"x-api-key": "k", "Content-Type": "application/json"})
        assert "interleaved-thinking-2025-05-14" in out["anthropic-beta"]
        assert "prompt-caching-2024-07-31" in out["anthropic-beta"]
        assert out["anthropic-version"] == "2023-06-01"
        # pas de doublon si le client redemande le défaut
        oc._capture_client_anthropic_betas({"anthropic-beta": oc._ANTHROPIC_BETA})
        out2 = oc._ensure_anthropic_beta({"x-api-key": "k"})
        assert out2["anthropic-beta"].count("interleaved-thinking-2025-05-14") == 1
    finally:
        _reset_betas()


def test_ensure_beta_never_raises():
    _reset_betas()
    try:
        assert oc._ensure_anthropic_beta(None) is None
        assert oc._ensure_anthropic_beta("nope") == "nope"
    finally:
        _reset_betas()


def _fake_request(content_length=None):
    headers = {"user-agent": "test-harness/1.0"}
    if content_length is not None:
        headers["content-length"] = str(content_length)
    return types.SimpleNamespace(headers=headers, url=types.SimpleNamespace(path="/v1/messages"))


def test_warn_huge_context_fires_and_never_raises(monkeypatch):
    logs = []
    monkeypatch.setattr(oc, "_log", lambda *a, **k: logs.append(" ".join(str(x) for x in a)))
    oc._warn_huge_context(_fake_request(700000))
    assert any("huge-context" in m for m in logs)
    logs.clear()
    oc._warn_huge_context(_fake_request(1000))
    oc._warn_huge_context(_fake_request(None))
    oc._warn_huge_context(None)
    assert logs == []
