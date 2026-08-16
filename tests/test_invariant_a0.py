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

    async def on_request(self):
        return self.proxy_url


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
