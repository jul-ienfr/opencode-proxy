"""test_http_client_self_heal.py — the shared upstream httpx client must
self-heal after the lifespan closes it.

Regression test for the 2026-08-17 incident: dashboard history showed a ✘ row
  mimo-v2.5 → deepseek-v4-flash, error "Cannot send a request, as the client
  has been closed."
That string is raised by httpx itself (httpx/_client.py, `AsyncClient.send()`)
when the client is in ClientState.CLOSED — the proxy's OWN shared `_client`,
closed by `await _client.aclose()` in the FastAPI lifespan shutdown. A stream
that re-entered `_open_free_stream` during a restart handoff hit the closed
client, and `anthropic_stream`'s `except Exception` recorded the RuntimeError
as `error=str(e)` → the ✘.

Fix: `_ensure_http_client()` (same pattern as dashboard/quota.py) rebuilds
`_client`/`_transport` when `_client is None or _client.is_closed`, and every
upstream send site calls it instead of touching `_client` directly.

Covered here (offline, monkeypatched doubles — no sockets, no VPN, no DB):
  * _ensure_http_client returns a live (non-closed) client
  * after the real module `_client` is aclose()d, _ensure_http_client swaps in
    a NEW live client (different object) instead of raising the httpx error
  * the paid stream path (_open_free_stream use_free=False) issues its request
    through the helper, never through a closed client
  * the free direct-fallback stream path (use_free=True, no VPN pool) does the
    same
  * _forward_post issues its POST through the helper too
"""

from contextlib import asynccontextmanager

import pytest

import opencode as oc  # module-level import (established pattern)


class _FakeStreamResp:
    """Minimal response double: status + headers, aclose() no-op."""

    status_code = 200
    headers = {}

    async def aclose(self):
        pass

    async def json(self):
        return {}


class _FakeHttpxClient:
    """httpx.AsyncClient double: records calls, streams/posts offline."""

    def __init__(self):
        self.calls = []
        self.is_closed = False

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        yield _FakeStreamResp()

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _FakeStreamResp()


class _ClosedClient:
    """What `_client` looks like right after the lifespan aclose()d it."""

    is_closed = True

    def stream(self, *a, **k):
        raise RuntimeError("Cannot send a request, as the client has been closed.")

    def post(self, *a, **k):
        raise RuntimeError("Cannot send a request, as the client has been closed.")


def test_ensure_http_client_returns_live_client(monkeypatch):
    """Baseline: the helper hands back a usable, open client."""
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    client = oc._ensure_http_client()
    assert client is not None
    assert not client.is_closed


@pytest.mark.asyncio
async def test_ensure_http_client_recreates_after_close(monkeypatch):
    """The incident scenario, direct, with a REAL httpx client: after the
    module client is aclose()d, the helper must swap in a fresh live one."""
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    original = oc._client  # real module-global AsyncClient (constructed at import)
    assert original is not None and not original.is_closed

    await original.aclose()
    assert original.is_closed, "precondition: client must be closed"

    healed = oc._ensure_http_client()
    assert healed is not original, "must create a NEW client after close"
    assert not healed.is_closed, "healed client must be usable"
    assert oc._client is healed, "module global must now point at the healed client"


@pytest.mark.asyncio
async def test_paid_stream_path_goes_through_helper(monkeypatch):
    """use_free=False stream must call _ensure_http_client() — a closed client
    alone must never be handed to httpx."""
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_free_ip_pool", None)
    closed = _ClosedClient()
    monkeypatch.setattr(oc, "_client", closed)
    spy_calls = []

    def _spy_ensure():
        spy_calls.append(1)
        return _FakeHttpxClient()

    monkeypatch.setattr(oc, "_ensure_http_client", _spy_ensure)

    body = {"model": "mimo-v2.5", "messages": [{"role": "user", "content": "hi"}]}
    async with oc._open_free_stream(
        "http://upstream.local/v1/messages", body, {"Authorization": "Bearer k"}, use_free=False
    ) as resp:
        assert resp.status_code == 200

    assert spy_calls, "_open_free_stream must consult _ensure_http_client()"
    # _ClosedClient.stream would have raised — reaching 200 proves the helper
    # (not the closed client) served the stream.


@pytest.mark.asyncio
async def test_free_direct_fallback_goes_through_helper(monkeypatch):
    """use_free=True with no VPN pool (direct free fallback) must call the
    helper too — the 21:22:13 route (mimo-v2.5 → deepseek-v4-flash) is a free
    model and takes exactly this branch."""
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_free_ip_pool", None)
    monkeypatch.setattr(
        oc,
        "_current_free_identity",
        lambda station=None: {"impersonate": "chrome131", "user_agent": None, "extra_headers": {}},
    )
    monkeypatch.setattr(oc, "_free_usage_ip", lambda station=None: "1.2.3.4")
    closed = _ClosedClient()
    monkeypatch.setattr(oc, "_client", closed)
    spy_calls = []

    def _spy_ensure():
        spy_calls.append(1)
        return _FakeHttpxClient()

    monkeypatch.setattr(oc, "_ensure_http_client", _spy_ensure)

    body = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]}
    async with oc._open_free_stream(
        "http://upstream.local/v1/chat/completions",
        body,
        {"Authorization": "Bearer k", "Content-Type": "application/json"},
        use_free=True,
    ) as resp:
        assert resp.status_code == 200

    assert spy_calls, "free fallback must consult _ensure_http_client()"


@pytest.mark.asyncio
async def test_forward_post_goes_through_helper(monkeypatch):
    """_forward_post (circuit-breaker POST) must consult the helper too."""
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_cb_should_allow", lambda endpoint: True)
    monkeypatch.setattr(oc, "_cb_record_success", lambda endpoint: None)
    monkeypatch.setattr(oc, "_cb_record_failure", lambda endpoint: None)
    closed = _ClosedClient()
    monkeypatch.setattr(oc, "_client", closed)
    spy_calls = []

    def _spy_ensure():
        spy_calls.append(1)
        return _FakeHttpxClient()

    monkeypatch.setattr(oc, "_ensure_http_client", _spy_ensure)

    resp = await oc._forward_post(
        "http://upstream.local/v1/messages", {"model": "glm-5.1"}, {"Authorization": "Bearer k"}
    )
    assert resp.status_code == 200
    assert spy_calls, "_forward_post must consult _ensure_http_client()"
