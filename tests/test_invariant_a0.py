"""test_invariant_a0.py — Invariant A.0: no paid-account artifact on API_BASE_FREE.

Plan verification step 6 / A.7.4: with a "paid" API key configured, an
in-process echo server asserts ABSENCE of the paid signatures on all 4
free paths:

  A  non-stream (curl_cffi, impersonate profile)          _do_free_request_curl_cffi
  B  non-stream direct (httpx, official identity)         _try_free_model_first
  C  stream via tunnel (curl_cffi, tunnel hop removed)    _open_free_stream
  D  stream direct (httpx, official identity)             _open_free_stream

[gate 2026-09-17] La gateway Zen refuse la jambe free anonyme (« FreeTierError:
can only be used from within OpenCode ») sauf identité client-officiel.
Le nouveau contrat free (cf. _official_free_headers) EXIGE donc :
  * Authorization: Bearer public EXACTEMENT (la clé payante ne part jamais)
  * User-Agent officiel opencode/<ver> (plus de face navigateur chrome)
  * x-opencode-client: desktop / x-opencode-project: global
  * x-opencode-request: msg_<ID ascendant> STABLE par message logique
    (1 ID par requête proxy, partagé par tous les essais — sémantique
    client, cf. request.ts ; voir test_official_client_parity.py)
  * x-opencode-session: ses_<ID descendant> stable (tournée toutes les 30 min)
  * face réseau = replay Bun mesuré (ja3 custom + sigalgs + HTTP/1.1),
    jamais un preset navigateur en rotation

Les signatures interdites restantes (invariant A.0) :
  * x-api-key                                    — clé payante (ancien leak)
  * client UA / "python-httpx/..."               — identité client stable
  * Cookie                                      — cookies de session
  * x-request-id                                — identifiant requête SDK
  * x-stainless-*                               — identifiants lib SDK

Le transport curl_cffi reste utilisé (tunnel/SOCKS5) mais avec les headers
officiels posés explicitement : le bundle n'injecte plus son UA puisque
User-Agent est déjà présent. Les tests n'assertent donc plus le bundle UA.

Never touches the live system: in-process ThreadingHTTPServer on
127.0.0.1:0, API_BASE_FREE monkeypatched, no config file written, no
second instance, no VPN started, free-usage logging replaced by a no-op
(the live logs/requests.db must never see these test requests).
The free session file is redirected to tmp_path (no logs/ write).
"""

import http.server
import json
import re
import threading
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

    captured: list = []

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        self.__class__.captured.append(
            {
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body.decode("utf-8", "replace"),
            }
        )
        try:
            wants_stream = bool(json.loads(body.decode("utf-8") or "{}").get("stream"))
        except Exception:
            wants_stream = False
        if wants_stream:
            # [gate body 2026-09-18] la jambe free force stream:true sur le
            # wire (même pour les appelants non-stream, qui collectent) :
            # l'écho répond en SSE comme l'amont réel.
            sse = (
                b'data: {"id":"echo","object":"chat.completion.chunk","model":"free-test-model",'
                b'"choices":[{"index":0,"delta":{"role":"assistant","content":"echo"},"finish_reason":null}]}\n\n'
                b'data: {"id":"echo","object":"chat.completion.chunk","model":"free-test-model",'
                b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
                b'"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
                b"data: [DONE]\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse)))
            self.end_headers()
            self.wfile.write(sse)
            return
        payload = json.dumps(
            {
                "id": "echo",
                "object": "chat.completion",
                "model": "free-test-model",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "echo"},
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode()
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
def free_env(monkeypatch, echo_server, tmp_path):
    """Point every free path at the echo server; neutralise live side effects."""
    # libcurl (Paths A/C) honours proxy env vars — the echo must be reachable.
    for var in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
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
    # [gate 2026-09-17] la session free ne doit jamais toucher logs/ en test
    monkeypatch.setattr(oc, "_FREE_SESSION_FILE", str(tmp_path / "_free_session_id"))
    oc._FREE_SESSION_CACHE = None
    oc._FREE_SESSION_TS = 0.0
    # [copie exacte 2026-09-18] msg_ par tâche : reset entre tests
    oc._free_msg_id.set(None)
    oc._free_model_cooldowns.clear()
    oc._current_free_attempt.set({})
    oc._current_user_agent.set(None)
    return oc


def assert_no_paid_artifacts(label, headers, body):
    """Invariant A.0: aucun artefact payant ne doit atteindre le free.

    [gate 2026-09-17] ``authorization`` n'est plus interdit en soi : le
    contrat EXIGE ``Bearer public`` (et rien d'autre).
    """
    for name in ("x-api-key", "cookie", "x-request-id"):
        assert name not in headers, f"{label}: forbidden header {name!r} reached API_BASE_FREE"
    for name, value in headers.items():
        assert not name.startswith("x-stainless-"), (
            f"{label}: SDK identifier {name!r} reached API_BASE_FREE"
        )
        assert PAID_KEY_MARKER not in value, (
            f"{label}: paid key leaked in header {name!r} (value={value!r})"
        )
    ua = headers.get("user-agent", "")
    assert "claude-cli" not in ua and "python-httpx" not in ua, (
        f"{label}: client UA leaked to the free endpoint: {ua!r}"
    )
    assert PAID_KEY_MARKER not in body, f"{label}: paid key leaked in request body"


def assert_official_free_identity(label, headers):
    """[gate 2026-09-17] identité client-officiel exigée sur chaque envoi free."""
    assert headers.get("authorization") == "Bearer public", (
        f"{label}: Authorization must be exactly 'Bearer public', got {headers.get('authorization')!r}"
    )
    assert headers.get("user-agent") == oc._OPENCODE_OFFICIAL_UA, (
        f"{label}: official UA expected, got {headers.get('user-agent')!r}"
    )
    assert headers.get("x-opencode-client") == "desktop", (
        f"{label}: x-opencode-client must be 'desktop', got {headers.get('x-opencode-client')!r}"
    )
    assert headers.get("x-opencode-project") == "global", (
        f"{label}: x-opencode-project must be 'global', got {headers.get('x-opencode-project')!r}"
    )
    assert re.fullmatch(r"msg_[0-9a-f]{12}[0-9A-Za-z]{14}", headers.get("x-opencode-request", "") or ""), (
        f"{label}: bad x-opencode-request ID: {headers.get('x-opencode-request')!r}"
    )
    assert re.fullmatch(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}", headers.get("x-opencode-session", "") or ""), (
        f"{label}: bad x-opencode-session ID: {headers.get('x-opencode-session')!r}"
    )


def _single_capture(label):
    captured = _EchoHandler.captured
    assert len(captured) == 1, (
        f"{label}: expected exactly 1 request to the echo server, got {len(captured)}"
    )
    return captured[0]


# ── Path B: non-stream direct (httpx) ─────────────────────────────────────
@pytest.mark.asyncio
async def test_path_b_direct_httpx_free_attempt(free_env):
    body = {"model": "paid-test-model", "messages": [{"role": "user", "content": "hello"}]}
    result = await oc._try_free_model_first(body, dict(PAID_HEADERS), "openai", "paid-test-model")
    assert result is not None, "free attempt must succeed against the echo server"
    resp, _resp_headers, free_model, _free_ip = result
    assert resp.status_code == 200
    assert free_model == "free-test-model", "model swap to the free model failed"

    cap = _single_capture("Path B")
    headers = cap["headers"]
    assert_no_paid_artifacts("Path B", headers, cap["body"])
    # [gate 2026-09-17] identité officielle (plus d'UA navigateur)
    assert_official_free_identity("Path B", headers)
    # Model swap reached the wire
    wire = json.loads(cap["body"])
    assert wire["model"] == "free-test-model"
    # [gate body 2026-09-18] grille tools (bash+read) + stream forcé : le
    # corps non-stream du client part en stream:true avec les shims.
    assert wire.get("stream") is True, "Path B: free wire must force stream:true"
    assert {"bash", "read"} <= {
        t.get("function", {}).get("name") for t in wire.get("tools", []) if isinstance(t, dict)
    }, "Path B: free wire must carry bash+read tools"
    # Le jeu officiel est uniforme : aucun header protocole client
    # (anthropic-version) n'est répercuté
    assert "anthropic-version" not in headers, (
        "Path B: official header set expected (no client protocol header)"
    )


# ── Path D: stream direct fallback (httpx) ────────────────────────────────
@pytest.mark.asyncio
async def test_path_d_direct_stream_fallback(free_env):
    body = {"model": "free-test-model", "messages": [{"role": "user", "content": "hello"}]}
    async with oc._open_free_stream(
        oc.API_BASE_FREE, body, dict(PAID_HEADERS), use_free=True
    ) as resp:
        assert resp.status_code == 200
        lines = [ln async for ln in resp.aiter_lines()]
    assert any("echo" in ln for ln in lines), "stream must deliver the echo payload"

    cap = _single_capture("Path D")
    headers = cap["headers"]
    assert_no_paid_artifacts("Path D", headers, cap["body"])
    # [gate 2026-09-17] identité officielle, pas de répercussion client
    assert_official_free_identity("Path D", headers)
    assert "anthropic-version" not in headers, (
        "Path D: official header set expected (no client protocol header)"
    )


# ── Path A: non-stream via VPN (curl_cffi) ────────────────────────────────
@pytest.mark.asyncio
async def test_path_a_curl_cffi_non_stream(free_env):
    pytest.importorskip("curl_cffi")
    body = {"model": "free-test-model", "messages": [{"role": "user", "content": "hello"}]}
    resp = await oc._do_free_request_curl_cffi(body, dict(PAID_HEADERS), proxy_url=None)
    assert resp.status_code == 200

    cap = _single_capture("Path A")
    headers = cap["headers"]
    assert_no_paid_artifacts("Path A", headers, cap["body"])
    # [gate 2026-09-17] UA officiel posé explicitement (le bundle curl ne
    # l'écrase pas : il ne remplit que les headers absents)
    assert_official_free_identity("Path A", headers)
    assert "anthropic-version" not in headers, (
        "Path A: official header set expected (no client protocol header)"
    )


# ── Path C: stream via tunnel (curl_cffi) ─────────────────────────────────
@pytest.mark.asyncio
async def test_path_c_curl_cffi_tunnel_stream(free_env, monkeypatch):
    pytest.importorskip("curl_cffi")
    monkeypatch.setattr(oc, "_free_ip_pool", _StubPool())
    # Drop only the tunnel hop; the REAL curl_cffi stream branch keeps running.
    monkeypatch.setattr(oc, "_curl_proxy_url", lambda p: None)
    body = {"model": "free-test-model", "messages": [{"role": "user", "content": "hello"}]}
    async with oc._open_free_stream(
        oc.API_BASE_FREE, body, dict(PAID_HEADERS), use_free=True
    ) as resp:
        assert resp.status_code == 200
        lines = [ln async for ln in resp.aiter_lines()]
    assert any("echo" in ln for ln in lines), "stream must deliver the echo payload"

    cap = _single_capture("Path C")
    headers = cap["headers"]
    assert_no_paid_artifacts("Path C", headers, cap["body"])
    # [gate 2026-09-17] identité officielle sur la branche curl aussi
    assert_official_free_identity("Path C", headers)
    assert "anthropic-version" not in headers, (
        "Path C: official header set expected (no client protocol header)"
    )


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
        {"impersonate": "firefox144", "user_agent": None, "extra_headers": {}}
    )
    active = _StubIdentityMgr({"impersonate": "edge101", "user_agent": None, "extra_headers": {}})
    monkeypatch.setattr(oc, "_free_ip_pool", _StubIdentityPool(active))
    monkeypatch.setattr(oc, "_vpn_manager", None)
    assert oc._current_free_identity(explicit) == explicit.current_identity


def test_current_free_identity_defaults_to_pool_active_station(monkeypatch):
    """station=None → resolves the pool.active_station manager's identity."""
    active = _StubIdentityMgr({"impersonate": "edge101", "user_agent": None, "extra_headers": {}})
    vpn = _StubIdentityMgr({"impersonate": "firefox144", "user_agent": None, "extra_headers": {}})
    monkeypatch.setattr(oc, "_free_ip_pool", _StubIdentityPool(active))
    monkeypatch.setattr(oc, "_vpn_manager", vpn)
    assert oc._current_free_identity() == active.current_identity


def test_current_free_identity_pool_without_active_falls_back_to_vpn_manager(monkeypatch):
    """Pool present but active_station None → falls back to _vpn_manager."""
    vpn = _StubIdentityMgr({"impersonate": "firefox144", "user_agent": None, "extra_headers": {}})
    monkeypatch.setattr(oc, "_free_ip_pool", _StubIdentityPool(None))
    monkeypatch.setattr(oc, "_vpn_manager", vpn)
    assert oc._current_free_identity() == vpn.current_identity


def test_current_free_identity_no_pool_falls_back_to_vpn_manager(monkeypatch):
    """No pool at all → _vpn_manager's identity (historical station-1 face)."""
    vpn = _StubIdentityMgr({"impersonate": "edge101", "user_agent": None, "extra_headers": {}})
    monkeypatch.setattr(oc, "_free_ip_pool", None)
    monkeypatch.setattr(oc, "_vpn_manager", vpn)
    assert oc._current_free_identity() == vpn.current_identity


def test_current_free_identity_no_manager_returns_chrome131_default(monkeypatch):
    """No pool, no VPN manager → the chrome131 default dict (pre-rotation face)."""
    monkeypatch.setattr(oc, "_free_ip_pool", None)
    monkeypatch.setattr(oc, "_vpn_manager", None)
    assert oc._current_free_identity() == {
        "impersonate": "chrome131",
        "user_agent": None,
        "extra_headers": {},
    }


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
        raise AssertionError(
            "count_request=False must not call pool.on_request() "
            "(the quota counter must not advance on a retry)"
        )


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
    calls_to_disconnect: list = []
    _return_disconnect = ("socks5://127.0.0.1:1999", 99)

    async def on_request(self):
        type(self).calls_to_request += 1
        raise AssertionError(
            "fresh_station=True must not call pool.on_request() "
            "(the quota counter must not advance on a retry)"
        )

    async def on_disconnect_retry(self, failed=None):
        type(self).calls_to_disconnect.append(failed)
        return self._return_disconnect


class _FakeStreamResp:
    """Minimal response double: status + headers, aclose() no-op."""

    status_code = 200
    headers: dict = {}

    async def aclose(self):
        pass


class _FakeCurlSession:
    """curl_cffi AsyncSession double: records constructor kwargs, posts offline.

    `created` is a class-level list of kwargs dicts, cleared per test — the
    tunnel branch's session=AsyncSession(...) is captured there, letting the
    test assert proxy=/impersonate= without any socket I/O.
    """

    created: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).created.append(kwargs)  # captured for the test assertions

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
        {"impersonate": "edge101", "user_agent": None, "extra_headers": {}}
    )
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

    oc._current_free_attempt.set(
        {
            "proxy_url": "socks5://127.0.0.1:1080",
            "station": 2,
            "identity": "firefox144",
            "ip": "1.2.3.4",
        }
    )
    try:
        body = {"model": "free-test-model", "messages": [{"role": "user", "content": "hello"}]}
        async with oc._open_free_stream(
            oc.API_BASE_FREE, body, dict(PAID_HEADERS), use_free=True, count_request=False
        ) as resp:
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
    # [copie exacte 2026-09-18] replay Bun, pas une face navigateur en
    # rotation : le profil dit firefox144 mais la session part avec le preset
    # chrome131 comme simple PORTEUR (émission OCSP/SCT — headers neutralisés
    # via default_headers=False) + ja3/sigalgs/H1 du client
    # (cf. test_official_client_parity.py).
    assert sess.get("impersonate") == "chrome131", sess
    assert sess.get("ja3") == oc._OPENCODE_JA3
    assert sess.get("extra_fp") == {
        "tls_signature_algorithms": list(oc._OPENCODE_SIG_ALGS),
        "tls_grease": False,
    }
    assert sess.get("http_version") == "v1"
    assert sess.get("default_headers") is False
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
        body = {"model": "free-test-model", "messages": [{"role": "user", "content": "hello"}]}
        async with oc._open_free_stream(
            oc.API_BASE_FREE, body, dict(PAID_HEADERS), use_free=True, count_request=False
        ) as resp:
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
    # [gate 2026-09-17] Direct path stamped the official identity (invariant A.0)
    assert kwargs["headers"].get("User-Agent") == oc._OPENCODE_OFFICIAL_UA
    assert kwargs["headers"].get("Authorization") == "Bearer public"
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
    oc._current_free_attempt.set(
        {
            "proxy_url": "socks5://127.0.0.1:1080",
            "station": 1,
            "identity": "firefox144",
            "ip": "9.9.9.9",
        }
    )
    try:
        body = {"model": "free-test-model", "messages": [{"role": "user", "content": "hello"}]}
        async with oc._open_free_stream(
            oc.API_BASE_FREE,
            body,
            dict(PAID_HEADERS),
            use_free=True,
            count_request=False,
            fresh_station=True,
        ) as resp:
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
async def test_open_free_stream_fresh_station_direct_fallback_preserves_station(
    free_env, monkeypatch
):
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

    oc._current_free_attempt.set(
        {"proxy_url": "socks5://127.0.0.1:1080", "station": 1, "identity": "", "ip": "9.9.9.9"}
    )
    try:
        body = {"model": "free-test-model", "messages": [{"role": "user", "content": "hello"}]}
        async with oc._open_free_stream(
            oc.API_BASE_FREE,
            body,
            dict(PAID_HEADERS),
            use_free=True,
            count_request=False,
            fresh_station=True,
        ) as resp:
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
