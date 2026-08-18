"""test_invariant_a0.py — Invariant A.0: no paid-account artifact on API_BASE_FREE.

Plan verification step 6 / A.7.4: with a "paid" API key configured, an
in-process echo server asserts ABSENCE of the 5 paid signatures on all 4
free paths:

  A  non-stream (curl_cffi, impersonate profile)          _do_free_request_curl_cffi
  B  non-stream direct (httpx, identity UA)               _try_free_model_first
  C  stream via tunnel (curl_cffi, tunnel hop removed)    _open_free_stream
  D  stream direct (httpx, filtered headers + identity)   _open_free_stream

The 5 signatures (invariant A.0 table):
  * Authorization / x-api-key        — paid key (old Path D leak, CRITIC(4))
  * client UA / "python-httpx/..."   — stable client identity
  * Cookie                          — session cookies
  * x-request-id                    — SDK request identifier
  * x-stainless-*                   — SDK library identifiers

anthropic-version (2023-06-01) is a PROTOCOL header, not an account artifact:
paths that pass the client headers through the filter (A/C/D) must keep it
(A.0 table, "conservé"). Path B builds a minimal fresh header set
({"Content-Type"} + identity UA, per plan A.4) and sends no protocol header
at all — that is compliant: the invariant forbids paid artifacts, it does
not require anthropic-version.

Never touches the live system: in-process ThreadingHTTPServer on
127.0.0.1:0, API_BASE_FREE monkeypatched, no config file written, no
second instance, no VPN started, free-usage logging replaced by a no-op
(the live logs/requests.db must never see these test requests).

Path distinguisher (accept-encoding is NOT usable — modern httpx also sends
"br, zstd"): the User-Agent. curl_cffi's impersonation bundle injects its
own UA (a fixed string per target, e.g. Macintosh Chrome 131), while the
httpx paths apply the curated UA from _UA_BY_IMPERSONATE (Windows Chrome
131). _bundle_ua() derives the exact bundle UA for the INSTALLED curl_cffi
version with a control request, so the comparison is version-robust. A Path
C request that silently fell back to direct httpx carries the curated UA
and FAILS the bundle-UA assertion instead of passing silently.
"""
import json
import os
import threading
import http.server
from contextlib import asynccontextmanager

import pytest

import opencode as oc  # module-level import (established pattern, test_proxy.py)

# ── Paid-client payload: every artifact the invariant must keep off free ──
# The key marker "sk-ant" is asserted ABSENT from every header VALUE and
# every request body the echo server receives — any leak, in any format,
# trips the test.
PAID_KEY_MARKER = "sk-ant-test-paid-key-1234567890"
PAID_HEADERS = {
    "Authorization": f"Bearer {PAID_KEY_MARKER}",
    "x-api-key": PAID_KEY_MARKER,
    "User-Agent": "claude-cli/1.0.3 (Claude Code) custom-agent/0.1",
    "Cookie": "session=abc123; ubid=xyz",
    "x-request-id": "req_test_123",
    "x-stainless-arch": "x64",
    "x-stainless-lang": "python",
    "anthropic-version": "2023-06-01",
    "Content-Type": "application/json",
}


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    """Captures every request verbatim; answers a minimal chat completion.

    `captured` is a class-level list of {"headers": {lower: value}, "body": str}
    — the server is session-scoped, the list is cleared per test.
    """

    captured = []

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        self.__class__.captured.append({
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body.decode("utf-8", "replace"),
        })
        payload = json.dumps({
            "id": "echo",
            "object": "chat.completion",
            "model": "free-test-model",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "echo"},
                         "finish_reason": "stop"}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence the default stderr logging
        pass


@pytest.fixture(scope="session")
def echo_server():
    """In-process echo server on an ephemeral port (127.0.0.1:0)."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()


async def _stub_public_ip():
    return "127.0.0.1"


class _StubPool:
    """FreeIPPool stand-in for Path C: reports a tunnel proxy (truthy).

    The proxy string is real but _curl_proxy_url is monkeypatched to None in
    the test, so the REAL curl_cffi stream branch runs with only the tunnel
    hop removed — no VPN needed, no live state touched.
    """

    enabled = True
    proxy_url = "socks5://127.0.0.1:1080"
    active_station = None

    async def on_request(self):
        # Real contract since the stream-tuple fix: (proxy_url, station).
        return self.proxy_url, self.active_station


@pytest.fixture
def free_env(monkeypatch, echo_server):
    """Point every free path at the echo server; neutralise live side effects."""
    # libcurl (Paths A/C) honours proxy env vars — the echo must be reachable.
    for var in ("http_proxy", "https_proxy", "all_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

    _EchoHandler.captured.clear()
    monkeypatch.setattr(oc, "API_BASE_FREE", echo_server)
    monkeypatch.setattr(oc, "_vpn_manager", None)
    monkeypatch.setattr(oc, "_free_ip_pool", None)
    monkeypatch.setattr(oc, "_get_cached_public_ip", _stub_public_ip)
    monkeypatch.setattr(oc, "_log_free_model_usage", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "FREE_MODEL_MAP", {"paid-test-model": "free-test-model"})
    oc._free_model_cooldowns.clear()
    return oc


def assert_no_paid_artifacts(label, headers, body):
    """Invariant A.0: none of the 5 paid signatures may reach the free endpoint."""
    for name in ("authorization", "x-api-key", "cookie", "x-request-id"):
        assert name not in headers, f"{label}: forbidden header {name!r} reached API_BASE_FREE"
    for name, value in headers.items():
        assert not name.startswith("x-stainless-"), \
            f"{label}: SDK identifier {name!r} reached API_BASE_FREE"
        assert PAID_KEY_MARKER not in value, \
            f"{label}: paid key leaked in header {name!r} (value={value!r})"
    ua = headers.get("user-agent", "")
    assert "claude-cli" not in ua and "python-httpx" not in ua, \
        f"{label}: client UA leaked to the free endpoint: {ua!r}"
    assert PAID_KEY_MARKER not in body, f"{label}: paid key leaked in request body"


def _single_capture(label):
    captured = _EchoHandler.captured
    assert len(captured) == 1, f"{label}: expected exactly 1 request to the echo server, got {len(captured)}"
    return captured[0]


async def _bundle_ua(url):
    """UA string the INSTALLED curl_cffi bundle injects for chrome131.

    A control request (no UA header) against the echo server; the request
    itself is discarded (captured is cleared by the caller afterwards).
    """
    from curl_cffi.requests import AsyncSession
    async with AsyncSession(impersonate="chrome131") as session:
        await session.post(url, json={"probe": True},
                           headers={"Content-Type": "application/json"})
    ua = _EchoHandler.captured[-1]["headers"]["user-agent"]
    assert ua, "control request carried no UA — bundle did not inject one"
    return ua


# ── Path B: non-stream direct (httpx) ─────────────────────────────────────
@pytest.mark.asyncio
async def test_path_b_direct_httpx_free_attempt(free_env):
    body = {"model": "paid-test-model",
            "messages": [{"role": "user", "content": "hello"}]}
    result = await oc._try_free_model_first(body, dict(PAID_HEADERS), "openai", "paid-test-model")
    assert result is not None, "free attempt must succeed against the echo server"
    resp, _resp_headers, free_model, _free_ip = result
    assert resp.status_code == 200
    assert free_model == "free-test-model", "model swap to the free model failed"

    cap = _single_capture("Path B")
    headers = cap["headers"]
    assert_no_paid_artifacts("Path B", headers, cap["body"])
    # Identity UA from the curated map (httpx path, bundle UA not available)
    assert headers.get("user-agent") == oc._UA_BY_IMPERSONATE["chrome131"], \
        f"Path B: identity UA expected, got {headers.get('user-agent')!r}"
    # Model swap reached the wire
    assert json.loads(cap["body"])["model"] == "free-test-model"
    # Path B sends a minimal fresh header set ({"Content-Type"} + identity
    # UA, plan A.4) — the client's protocol header is absent by design and
    # nothing paid is present (asserted above)
    assert "anthropic-version" not in headers, \
        "Path B: minimal header set expected (no protocol header on this path)"


# ── Path D: stream direct fallback (httpx) ────────────────────────────────
@pytest.mark.asyncio
async def test_path_d_direct_stream_fallback(free_env):
    body = {"model": "free-test-model",
            "messages": [{"role": "user", "content": "hello"}]}
    async with oc._open_free_stream(oc.API_BASE_FREE, body, dict(PAID_HEADERS),
                                    use_free=True) as resp:
        assert resp.status_code == 200
        lines = [ln async for ln in resp.aiter_lines()]
    assert any("echo" in ln for ln in lines), "stream must deliver the echo payload"

    cap = _single_capture("Path D")
    headers = cap["headers"]
    assert_no_paid_artifacts("Path D", headers, cap["body"])
    assert headers.get("user-agent") == oc._UA_BY_IMPERSONATE["chrome131"], \
        f"Path D: identity UA expected, got {headers.get('user-agent')!r}"
    assert headers.get("anthropic-version") == "2023-06-01"


# ── Path A: non-stream via VPN (curl_cffi) ────────────────────────────────
@pytest.mark.asyncio
async def test_path_a_curl_cffi_non_stream(free_env):
    pytest.importorskip("curl_cffi")
    # Control request first, then clear: the bundle UA is derived from the
    # installed curl_cffi and must match the real request's UA.
    bundle_ua = await _bundle_ua(oc.API_BASE_FREE)
    _EchoHandler.captured.clear()
    body = {"model": "free-test-model",
            "messages": [{"role": "user", "content": "hello"}]}
    resp = await oc._do_free_request_curl_cffi(body, dict(PAID_HEADERS), proxy_url=None)
    assert resp.status_code == 200

    cap = _single_capture("Path A")
    headers = cap["headers"]
    assert_no_paid_artifacts("Path A", headers, cap["body"])
    # curl_cffi bundle UA — proves the impersonate path really ran (a
    # fallback to httpx would carry the curated Windows UA instead)
    assert headers.get("user-agent") == bundle_ua, \
        f"Path A: curl_cffi bundle UA expected, got {headers.get('user-agent')!r}"
    assert headers.get("anthropic-version") == "2023-06-01"


# ── Path C: stream via tunnel (curl_cffi) ─────────────────────────────────
@pytest.mark.asyncio
async def test_path_c_curl_cffi_tunnel_stream(free_env, monkeypatch):
    pytest.importorskip("curl_cffi")
    monkeypatch.setattr(oc, "_free_ip_pool", _StubPool())
    # Drop only the tunnel hop; the REAL curl_cffi stream branch keeps running.
    monkeypatch.setattr(oc, "_curl_proxy_url", lambda p: None)
    bundle_ua = await _bundle_ua(oc.API_BASE_FREE)
    _EchoHandler.captured.clear()
    body = {"model": "free-test-model",
            "messages": [{"role": "user", "content": "hello"}]}
    async with oc._open_free_stream(oc.API_BASE_FREE, body, dict(PAID_HEADERS),
                                    use_free=True) as resp:
        assert resp.status_code == 200
        lines = [ln async for ln in resp.aiter_lines()]
    assert any("echo" in ln for ln in lines), "stream must deliver the echo payload"

    cap = _single_capture("Path C")
    headers = cap["headers"]
    assert_no_paid_artifacts("Path C", headers, cap["body"])
    # Bundle UA — proves the request really came from the curl_cffi stream
    # branch and did NOT silently fall back to direct httpx (which would
    # carry the curated Windows UA)
    assert headers.get("user-agent") == bundle_ua, \
        f"Path C: curl_cffi bundle UA expected — silent fallback to direct? {headers.get('user-agent')!r}"
    assert headers.get("anthropic-version") == "2023-06-01"


# ── _current_free_identity: station-aware identity resolution ─────────────
# Pure resolution (opencode.py): explicit station wins, then
# pool.active_station, then _vpn_manager, then the chrome131 default.
# No network, no fixtures beyond monkeypatch.
class _StubIdentityMgr:
    """Minimal station/manager double: carries a current_identity dict only."""

    def __init__(self, identity):
        self.current_identity = identity


class _StubIdentityPool:
    """FreeIPPool double exposing only active_station for identity resolution."""

    def __init__(self, active_station=None):
        self.active_station = active_station


def test_current_free_identity_explicit_station_wins_over_pool_active(monkeypatch):
    """Explicit station param WINS over the pool's last-picked station."""
    explicit = _StubIdentityMgr(
        {"impersonate": "firefox144", "user_agent": None, "extra_headers": {}})
    active = _StubIdentityMgr(
        {"impersonate": "edge101", "user_agent": None, "extra_headers": {}})
    monkeypatch.setattr(oc, "_free_ip_pool", _StubIdentityPool(active))
    monkeypatch.setattr(oc, "_vpn_manager", None)
    assert oc._current_free_identity(explicit) == explicit.current_identity


def test_current_free_identity_defaults_to_pool_active_station(monkeypatch):
    """station=None → resolves the pool.active_station manager's identity."""
    active = _StubIdentityMgr(
        {"impersonate": "edge101", "user_agent": None, "extra_headers": {}})
    vpn = _StubIdentityMgr(
        {"impersonate": "firefox144", "user_agent": None, "extra_headers": {}})
    monkeypatch.setattr(oc, "_free_ip_pool", _StubIdentityPool(active))
    monkeypatch.setattr(oc, "_vpn_manager", vpn)
    assert oc._current_free_identity() == active.current_identity


def test_current_free_identity_pool_without_active_falls_back_to_vpn_manager(monkeypatch):
    """Pool present but active_station None → falls back to _vpn_manager."""
    vpn = _StubIdentityMgr(
        {"impersonate": "firefox144", "user_agent": None, "extra_headers": {}})
    monkeypatch.setattr(oc, "_free_ip_pool", _StubIdentityPool(None))
    monkeypatch.setattr(oc, "_vpn_manager", vpn)
    assert oc._current_free_identity() == vpn.current_identity


def test_current_free_identity_no_pool_falls_back_to_vpn_manager(monkeypatch):
    """No pool at all → _vpn_manager's identity (historical station-1 face)."""
    vpn = _StubIdentityMgr(
        {"impersonate": "edge101", "user_agent": None, "extra_headers": {}})
    monkeypatch.setattr(oc, "_free_ip_pool", None)
    monkeypatch.setattr(oc, "_vpn_manager", vpn)
    assert oc._current_free_identity() == vpn.current_identity


def test_current_free_identity_no_manager_returns_chrome131_default(monkeypatch):
    """No pool, no VPN manager → the chrome131 default dict (pre-rotation face)."""
    monkeypatch.setattr(oc, "_free_ip_pool", None)
    monkeypatch.setattr(oc, "_vpn_manager", None)
    assert oc._current_free_identity() == \
        {"impersonate": "chrome131", "user_agent": None, "extra_headers": {}}


# ── _open_free_stream count_request=False: retry reuses the stored attempt ─
# Site 2: a retry after a network error must NOT advance the quota counter
# (no on_request()) — it re-reads the ContextVar from the original attempt.
# Both branches run fully offline (faked session / faked _client).
class _StubPoolNeverCount(_StubPool):
    """Pool double whose on_request() MUST never run on count_request=False.

    proxy_url differs from the ContextVar's in the tests below, so a wrong
    read of the pool's proxy is caught. on_request raises — it sits OUTSIDE
    the tunnel branch's try/except, so an accidental call fails loudly.
    """

    proxy_url = "socks5://127.0.0.1:1999"
    calls = 0

    async def on_request(self):
        type(self).calls += 1
        raise AssertionError("count_request=False must not call pool.on_request() "
                             "(the quota counter must not advance on a retry)")


class _StubPoolDisconnectRetry:
    """Pool double for the fresh_station wiring: on_disconnect_retry returns
    a DIFFERENT (proxy, station); on_request would advance the counter and
    must never run.

    Records every on_disconnect_retry argument; `return_disconnect` is
    overridable per instance (e.g. (None, None) when no station is usable).
    """

    enabled = True
    active_station = None  # read by _current_free_identity on the direct path
    calls_to_request = 0
    calls_to_disconnect = []
    _return_disconnect = ("socks5://127.0.0.1:1999", 99)

    async def on_request(self):
        type(self).calls_to_request += 1
        raise AssertionError("fresh_station=True must not call pool.on_request() "
                             "(the quota counter must not advance on a retry)")

    async def on_disconnect_retry(self, failed=None):
        type(self).calls_to_disconnect.append(failed)
        return self._return_disconnect


class _FakeStreamResp:
    """Minimal response double: status + headers, aclose() no-op."""

    status_code = 200
    headers = {}

    async def aclose(self):
        pass


class _FakeCurlSession:
    """curl_cffi AsyncSession double: records constructor kwargs, posts offline.

    `created` is a class-level list of kwargs dicts, cleared per test — the
    tunnel branch's session=AsyncSession(...) is captured there, letting the
    test assert proxy=/impersonate= without any socket I/O.
    """

    created = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).created.append(kwargs)   # captured for the test assertions

    async def post(self, *args, **kwargs):
        return _FakeStreamResp()

    async def close(self):
        pass


class _FakeHttpxClient:
    """httpx.AsyncClient double: records stream() calls, yields offline.

    `calls` is a per-instance list of (method, url, kwargs) — the direct
    fallback branch's `_client.stream(...)` is captured there.
    """

    def __init__(self):
        self.calls = []

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        yield _FakeStreamResp()


@pytest.mark.asyncio
async def test_open_free_stream_count_false_reuses_stored_station(free_env, monkeypatch):
    """Retry (count_request=False) re-enters the tunnel with the ContextVar's
    proxy/station — on_request() is never called, and the stored station is
    the one resolved by _current_free_identity (not pool.active_station)."""
    pytest.importorskip("curl_cffi")
    _StubPoolNeverCount.calls = 0
    _FakeCurlSession.created.clear()

    pool = _StubPoolNeverCount()
    pool.active_station = _StubIdentityMgr(
        {"impersonate": "edge101", "user_agent": None, "extra_headers": {}})
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    # The stored station is an int sentinel here; the real helper would
    # dereference .current_ip on it — stub it (the IP is already in the
    # ContextVar, the retry must not touch live state anyway).
    monkeypatch.setattr(oc, "_free_usage_ip", lambda station=None: "1.2.3.4")
    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _FakeCurlSession)
    seen = {}

    def _identity_spy(station=None):
        seen["station"] = station
        return {"impersonate": "firefox144", "user_agent": None, "extra_headers": {}}

    monkeypatch.setattr(oc, "_current_free_identity", _identity_spy)

    oc._current_free_attempt.set({"proxy_url": "socks5://127.0.0.1:1080",
                                  "station": 2,
                                  "identity": "firefox144",
                                  "ip": "1.2.3.4"})
    try:
        body = {"model": "free-test-model",
                "messages": [{"role": "user", "content": "hello"}]}
        async with oc._open_free_stream(oc.API_BASE_FREE, body, dict(PAID_HEADERS),
                                        use_free=True, count_request=False) as resp:
            assert resp.status_code == 200
    finally:
        oc._current_free_attempt.set({})

    # Quota counter untouched: on_request() must never run on a retry
    assert _StubPoolNeverCount.calls == 0
    # Identity resolved from the STORED station (2), not pool.active_station
    # (a silent fallback to direct would call the spy with station=None)
    assert seen.get("station") == 2
    # Tunnel branch really ran, with the ContextVar's proxy (socks5h = the
    # socks5 fix) — NOT the pool's proxy_url
    assert len(_FakeCurlSession.created) == 1
    sess = _FakeCurlSession.created[0]
    assert sess["impersonate"] == "firefox144"
    assert sess["proxy"] == "socks5h://127.0.0.1:1080"


@pytest.mark.asyncio
async def test_open_free_stream_count_false_empty_attempt_direct_fallback(free_env, monkeypatch):
    """No prior attempt (empty ContextVar) → proxy_url None → direct httpx
    fallback: still no on_request(), and the ContextVar is re-set with
    station None so the next retry also goes direct."""
    _StubPoolNeverCount.calls = 0
    fake_client = _FakeHttpxClient()
    monkeypatch.setattr(oc, "_free_ip_pool", _StubPoolNeverCount())
    monkeypatch.setattr(oc, "_client", fake_client)

    oc._current_free_attempt.set({})
    try:
        body = {"model": "free-test-model",
                "messages": [{"role": "user", "content": "hello"}]}
        async with oc._open_free_stream(oc.API_BASE_FREE, body, dict(PAID_HEADERS),
                                        use_free=True, count_request=False) as resp:
            assert resp.status_code == 200
        # Capture the re-set ContextVar BEFORE the finally reset
        attempt = oc._current_free_attempt.get() or {}
    finally:
        oc._current_free_attempt.set({})

    assert _StubPoolNeverCount.calls == 0
    assert len(fake_client.calls) == 1
    method, url, kwargs = fake_client.calls[0]
    assert method == "POST"
    assert url == oc.API_BASE_FREE
    # Direct path stamped the identity UA (invariant A.0)
    assert kwargs["headers"].get("User-Agent") == oc._UA_BY_IMPERSONATE["chrome131"]
    # Retry state: station None → the next attempt also falls back direct
    assert attempt.get("station") is None
    assert attempt.get("proxy_url") is None


# ── _open_free_stream fresh_station=True: disconnect retry switches station ─
# The 17/08 21:44 ✘ ("Server disconnected without sending a response"): the
# retry re-struck the SAME station/IP that just died — guaranteed failure
# under the per-IP quota model. fresh_station=True asks the pool for a
# DIFFERENT station WITHOUT advancing the counter (no on_request).
@pytest.mark.asyncio
async def test_open_free_stream_fresh_station_switches_station(free_env, monkeypatch):
    """A disconnect retry (fresh_station=True) must call pool.on_disconnect_retry()
    with the ContextVar's stored (failed) station and tunnel over the FRESH
    proxy it returns — on_request() (counter advance) never runs."""
    pytest.importorskip("curl_cffi")
    _StubPoolDisconnectRetry.calls_to_request = 0
    _StubPoolDisconnectRetry.calls_to_disconnect = []
    _FakeCurlSession.created.clear()

    pool = _StubPoolDisconnectRetry()
    pool.active_station = None
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    monkeypatch.setattr(oc, "_free_usage_ip", lambda station=None: "9.9.9.9")
    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _FakeCurlSession)

    def _identity_spy(station=None):
        return {"impersonate": "firefox144", "user_agent": None, "extra_headers": {}}

    monkeypatch.setattr(oc, "_current_free_identity", _identity_spy)

    # The original attempt landed on station 1; it just disconnected.
    oc._current_free_attempt.set({"proxy_url": "socks5://127.0.0.1:1080",
                                  "station": 1,
                                  "identity": "firefox144",
                                  "ip": "9.9.9.9"})
    try:
        body = {"model": "free-test-model",
                "messages": [{"role": "user", "content": "hello"}]}
        async with oc._open_free_stream(oc.API_BASE_FREE, body, dict(PAID_HEADERS),
                                        use_free=True, count_request=False,
                                        fresh_station=True) as resp:
            assert resp.status_code == 200
    finally:
        oc._current_free_attempt.set({})

    # Quota counter untouched: on_request() must never run on a retry
    assert _StubPoolDisconnectRetry.calls_to_request == 0
    # The pool is told WHICH station failed (from the ContextVar), so it can
    # exclude it from the pick.
    assert _StubPoolDisconnectRetry.calls_to_disconnect == [1]
    # Tunnel ran over the FRESH proxy (socks5h = the socks5 fix) — NOT the
    # dead station's ContextVar proxy.
    assert len(_FakeCurlSession.created) == 1
    sess = _FakeCurlSession.created[0]
    assert sess["proxy"] == "socks5h://127.0.0.1:1999"


@pytest.mark.asyncio
async def test_open_free_stream_fresh_station_direct_fallback_preserves_station(free_env, monkeypatch):
    """fresh_station=True but no usable station (on_disconnect_retry returns
    (None, None)) → direct httpx fallback; the ContextVar KEEPS the failed
    station so a later retry can still switch away from it instead of
    re-striking it."""
    _StubPoolDisconnectRetry.calls_to_request = 0
    _StubPoolDisconnectRetry.calls_to_disconnect = []
    fake_client = _FakeHttpxClient()

    pool = _StubPoolDisconnectRetry()
    pool._return_disconnect = (None, None)  # no station usable right now
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    monkeypatch.setattr(oc, "_client", fake_client)

    oc._current_free_attempt.set({"proxy_url": "socks5://127.0.0.1:1080",
                                  "station": 1,
                                  "identity": "",
                                  "ip": "9.9.9.9"})
    try:
        body = {"model": "free-test-model",
                "messages": [{"role": "user", "content": "hello"}]}
        async with oc._open_free_stream(oc.API_BASE_FREE, body, dict(PAID_HEADERS),
                                        use_free=True, count_request=False,
                                        fresh_station=True) as resp:
            assert resp.status_code == 200
        # Capture the re-set ContextVar BEFORE the finally reset
        attempt = oc._current_free_attempt.get() or {}
    finally:
        oc._current_free_attempt.set({})

    assert _StubPoolDisconnectRetry.calls_to_request == 0
    assert _StubPoolDisconnectRetry.calls_to_disconnect == [1]
    assert len(fake_client.calls) == 1  # direct httpx fallback ran
    # The failed station is preserved, not wiped: the next retry can still
    # switch away from it (on_disconnect_retry(excluded=1)).
    assert attempt.get("station") == 1
    assert attempt.get("proxy_url") is None  # no tunnel → proxy None
