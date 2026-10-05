"""Phase 1 — wire tests : relais shape-compaction (Serveur passthrough).

Hermétique : import module-level (pattern test_systemone.py), TestClient ASGI
(pattern test_e2e_protocol_matrix.py : UpstreamRecorder + _install_seams),
jamais de boot réseau, jamais de touch logs/requests.db live.

Cinq tests fil :
  (1) une requête shape-compaction passe le body à l'identique sauf
      body["model"] (remap route) ;
  (2) une requête shape est exclue du response cache (2 envois → 2 hits amont) ;
  (3) une erreur amont en chat streaming inclut le body upstream ;
  (4) un non-200 amont en responses streaming produit un event d'erreur
      (pas un simple [DONE] aveugle) ;
  (5) un 429 amont sur une requête shape est relayé intact, pas réécrit en 503.
"""

import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

import opencode as oc
from server.cache import ResponseCache

# ── doubles ──────────────────────────────────────────────────────────

def _shape_chat_body(model="sonnet", n_chars=1500):
    """Body shape-compaction : 100 % user texte pur, sans tools/system."""
    return {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "resume plz " + "x" * n_chars}],
    }


def _shape_responses_body(model="muse-spark-1.3-contributor", n_chars=1500):
    return {
        "model": model,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "resume plz " + "x" * n_chars}],
            }
        ],
    }


def _chat_completion(text="ok"):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "glm-5",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
    }


CHAT_CHUNK_LINES = [
    'data: {"id":"1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hi"}}]}',
    'data: {"id":"1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
    "data: [DONE]",
]


class FakeResponse:
    """Double amont : couvre l'interface consommée par les handlers."""

    def __init__(self, status_code=200, payload=None, lines=None, ctype=None, text=None):
        self.status_code = status_code
        self._payload = payload
        if ctype is None:
            ctype = "application/json" if payload is not None or text is not None else "text/event-stream"
        self.headers = {"content-type": ctype}
        self._lines = list(lines or [])
        if text is not None:
            self.text = text
        else:
            self.text = json.dumps(payload) if payload is not None else ""

    async def aread(self):
        return self.content

    @property
    def content(self):
        return self.text.encode()

    def json(self):
        return self._payload

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aiter_bytes(self):
        for line in self._lines:
            yield (line if isinstance(line, bytes) else line.encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class UpstreamRecorder:
    """Capture, dans l'ordre, ce qui part réellement vers l'amont."""

    def __init__(self, *, upstream=None):
        self.calls: list[dict] = []
        self._upstream = upstream or (lambda endpoint, body, proto: FakeResponse(payload=_chat_completion()))

    def set_upstream(self, fn):
        self._upstream = fn

    @property
    def last(self) -> dict:
        assert self.calls, "aucun appel amont capturé"
        return self.calls[-1]

    def _record(self, seam, endpoint, body, protocol=None, extra=None):
        entry = {
            "seam": seam,
            "endpoint": endpoint,
            "protocol": protocol,
            "body": json.loads(json.dumps(body, default=str)),
        }
        if extra:
            entry.update(extra)
        self.calls.append(entry)
        return entry


def _install_seams(monkeypatch, recorder: UpstreamRecorder, *, real_cache=False):
    """Remplace tous les seams amont + les effets de bord non hermétiques."""

    async def _noop_async(*args, **kwargs):
        return None

    async def _no_free(*args, **kwargs):
        return None

    monkeypatch.setattr(oc, "_enforce_geo_gate", lambda *a, **k: _noop_async_none(), raising=False)
    monkeypatch.setattr(oc, "_cb_should_allow", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(oc, "_cb_record_failure", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(oc, "_cb_record_success", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(oc, "_save_and_log_request", _noop_async, raising=False)
    monkeypatch.setattr(oc, "_log_and_save_error", _noop_async, raising=False)
    monkeypatch.setattr(oc, "_save_request", _noop_async, raising=False)
    monkeypatch.setattr(oc, "_update_token_usage", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(oc, "_log_free_model_usage", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(oc, "_estimate_input_tokens", lambda *a, **k: 7, raising=False)
    monkeypatch.setattr(oc, "_alias_for_key", lambda key: "test-alias", raising=False)
    monkeypatch.setattr(oc, "_try_free_model_first", _no_free, raising=False)
    # Chat-streaming resolves the free leg via _resolve_free_model (not via
    # _try_free_model_first): force None so the paid leg is exercised.
    monkeypatch.setattr(oc, "_resolve_free_model", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(oc, "_has_usable_paid_key", lambda: True, raising=False)
    if real_cache:
        monkeypatch.setattr(oc, "_response_cache", ResponseCache(), raising=False)
    else:
        monkeypatch.setattr(oc, "_response_cache", _NullCache(), raising=False)

    def _auth_headers(protocol, entry=None):
        key = (entry or {}).get("api_key", "test-key-A")
        if protocol == "openai":
            return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        return {"x-api-key": key, "Content-Type": "application/json", "anthropic-version": "2023-06-01"}

    monkeypatch.setattr(oc, "_get_auth_headers", _auth_headers, raising=False)

    async def _do_request_with_retry(endpoint, body, headers, protocol, retry_on_429=True):
        recorder._record("http", endpoint, body, protocol)
        return recorder._upstream(endpoint, body, protocol), headers

    async def _do_free_direct_request(endpoint, body, headers):
        recorder._record("http", endpoint, body, "free")
        return recorder._upstream(endpoint, body, "free"), headers

    async def _do_free_request_curl_cffi(body, headers, proxy_url=None, station=None, endpoint=None, **kwargs):
        recorder._record("http", endpoint, body, "free")
        return recorder._upstream(endpoint, body, "free")

    @asynccontextmanager
    async def _open_free_stream(endpoint, body, headers, use_free, count_request=True, **kwargs):
        recorder._record("free", endpoint, body, extra={"use_free": bool(use_free)})
        yield recorder._upstream(endpoint, body, "anthropic")

    @asynccontextmanager
    async def _open_via_pool(endpoint, body, headers, is_stream=False, forced_pool=None):
        recorder._record("pool", endpoint, body, extra={"is_stream": bool(is_stream)})
        yield recorder._upstream(endpoint, body, "anthropic")

    monkeypatch.setattr(oc, "_do_request_with_retry", _do_request_with_retry, raising=False)
    monkeypatch.setattr(oc, "_do_free_direct_request", _do_free_direct_request, raising=False)
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", _do_free_request_curl_cffi, raising=False)
    monkeypatch.setattr(oc, "_open_free_stream", _open_free_stream, raising=False)
    monkeypatch.setattr(oc, "_open_via_pool", _open_via_pool, raising=False)


async def _noop_async_none(*args, **kwargs):
    return None


class _NullCache:
    """Cache désactivé : jamais de HIT qui court-circuite l'amont."""

    def make_key(self, *a, **k):
        return None

    def get(self, *a, **k):
        return None

    def put(self, *a, **k):
        return None


@pytest.fixture
def client():
    """Client ASGI sur l'app réelle. Pas de ``with`` : le lifespan démarre des
    pollers réseau qui pendent sous pytest."""
    return TestClient(oc.app)


@pytest.fixture
def recorder(monkeypatch):
    rec = UpstreamRecorder()
    _install_seams(monkeypatch, rec)
    return rec


def _stream_text(client, url, body) -> str:
    with client.stream("POST", url, json=body) as r:
        raw = b"".join(r.iter_bytes())
    return raw.decode("utf-8", "replace")


# ── (1) passthrough byte-identique sauf body["model"] ────────────────

def test_shape_passthrough_identical_except_model(client, recorder):
    """Shape-compaction : le wire amont = le body client, sauf model remappé."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=_chat_completion()))
    body = _shape_chat_body(model="sonnet")  # sonnet → glm-5.1 (route config)

    r = client.post("/v1/chat/completions", json=body)

    assert r.status_code == 200, r.text
    wire = recorder.last["body"]
    for key, val in body.items():
        if key == "model":
            continue
        assert wire.get(key) == val, f"champ {key!r} modifié sur le wire"
    assert wire["model"] != body["model"]  # remap route appliqué
    assert wire["model"] == "glm-5.1"


# ── (2) exclusion du response cache ──────────────────────────────────

def test_shape_excluded_from_response_cache(client, monkeypatch):
    """Shape-compaction jamais cachée : 2 envois identiques → 2 hits amont."""
    rec = UpstreamRecorder(upstream=lambda e, b, p: FakeResponse(payload=_chat_completion()))
    _install_seams(monkeypatch, rec, real_cache=True)
    body = _shape_chat_body(model="sonnet")

    r1 = client.post("/v1/chat/completions", json=body)
    r2 = client.post("/v1/chat/completions", json=body)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert len(rec.calls) == 2, f"attendu 2 hits amont, vu {len(rec.calls)}"
    assert "X-Cache" not in dict(r2.headers) or r2.headers.get("X-Cache") != "HIT"


# ── (3) erreur amont en chat streaming : body inclus ─────────────────

def test_chat_stream_upstream_error_includes_body(client, recorder):
    """Chat streaming, amont non-200 : l'event d'erreur porte le body upstream."""
    upstream_body = "upstream exploded: quota gone"
    recorder.set_upstream(lambda e, b, p: FakeResponse(status_code=500, text=upstream_body))
    body = _shape_chat_body(model="sonnet")
    body["stream"] = True

    text = _stream_text(client, "/v1/chat/completions", body)

    assert "data: " in text
    assert upstream_body in text, f"body upstream absent du flux: {text[:400]!r}"
    assert "[DONE]" in text


# ── (4) responses streaming non-200 : event d'erreur, pas [DONE] nu ──

def test_responses_stream_non200_yields_error_event(client, recorder):
    """Responses streaming, amont non-200 : event erreur, pas [DONE] seul."""
    upstream_body = "upstream exploded: context too long"
    recorder.set_upstream(lambda e, b, p: FakeResponse(status_code=400, text=upstream_body))
    body = _shape_responses_body()
    body["stream"] = True

    text = _stream_text(client, "/v1/responses", body)

    lines = [ln for ln in text.splitlines() if ln.startswith("data:")]
    assert lines, f"aucune ligne data: dans {text[:400]!r}"
    payloads = [ln[5:].strip() for ln in lines if ln[5:].strip() != "[DONE]"]
    assert payloads, f"flux [DONE]-only, attendu un event d'erreur: {text[:400]!r}"
    joined = " ".join(payloads)
    assert "error" in joined.lower(), f"pas d'event erreur: {text[:400]!r}"
    assert "400" in joined and upstream_body in joined, f"statut/body absents: {text[:400]!r}"


# ── (5) 429 shape relayé intact, jamais 503 ──────────────────────────

def test_shape_429_relayed_intact_not_503(client, recorder):
    """429 amont sur shape-compaction : statut + body relayés, pas de 503."""
    upstream_body = json.dumps({"error": {"message": "rate limited, slow down"}})
    recorder.set_upstream(
        lambda e, b, p: FakeResponse(status_code=429, text=upstream_body, ctype="application/json")
    )
    body = _shape_chat_body(model="sonnet")

    r = client.post("/v1/chat/completions", json=body)

    assert r.status_code == 429, f"attendu 429 intact, vu {r.status_code}: {r.text[:300]!r}"
    assert "rate limited" in r.text
    assert "All API keys exhausted" not in r.text


# ── (6) stream compactage : tiny → refetch station fraîche → SSE complet ──

def _tiny_chat_completion():
    return {
        "id": "chatcmpl-tiny",
        "object": "chat.completion",
        "created": 1,
        "model": "glm-5",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 50000, "completion_tokens": 17, "total_tokens": 50017},
    }


def _full_chat_completion(text="RESUME-WIRE-COMPLET"):
    payload = _chat_completion(text=text)
    payload["usage"] = {"prompt_tokens": 50000, "completion_tokens": 500, "total_tokens": 50500}
    return payload


def test_compaction_stream_tiny_retries_then_succeeds(client, monkeypatch):
    """Chemin Hermes réel : stream shape-compaction, free tiny puis complet.

    Le client reçoit le SSE synthétisé du 2e essai (jamais l'octet tiny) :
    le live streaming n'émet rien avant la décision — retry sûr à 100 %.
    """
    rec = UpstreamRecorder()
    _install_seams(monkeypatch, rec)
    calls = []

    async def _tiny_then_full(body, headers, protocol, model_id, forced_pool=None, req_id=None):
        calls.append(1)
        if len(calls) == 1:
            return FakeResponse(payload=_tiny_chat_completion()), {}, "glm-5-free", "9.9.9.9"
        return FakeResponse(payload=_full_chat_completion()), {}, "glm-5-free", "9.9.9.10"

    monkeypatch.setattr(oc, "_try_free_model_first", _tiny_then_full, raising=False)
    body = _shape_chat_body(model="sonnet")
    body["stream"] = True

    text = _stream_text(client, "/v1/chat/completions", body)

    assert len(calls) == 2, f"refetch station fraîche attendu, vu {len(calls)}"
    assert "RESUME-WIRE-COMPLET" in text, f"résumé du 2e essai absent: {text[:400]!r}"
    assert text.rstrip().endswith("[DONE]")
    assert '"finish_reason": "stop"' in text or '"finish_reason":"stop"' in text


def test_compaction_stream_untiny_passthrough_single_fetch(client, monkeypatch):
    """Stream compactage non-tiny : 1 seul fetch, SSE synthétisé direct."""
    rec = UpstreamRecorder()
    _install_seams(monkeypatch, rec)
    calls = []

    async def _full_only(body, headers, protocol, model_id, forced_pool=None, req_id=None):
        calls.append(1)
        return FakeResponse(payload=_full_chat_completion()), {}, "glm-5-free", "9.9.9.10"

    monkeypatch.setattr(oc, "_try_free_model_first", _full_only, raising=False)
    body = _shape_chat_body(model="sonnet")
    body["stream"] = True

    text = _stream_text(client, "/v1/chat/completions", body)

    assert len(calls) == 1
    assert "RESUME-WIRE-COMPLET" in text
    assert text.rstrip().endswith("[DONE]")


def _tiny_anthropic_payload():
    return {
        "content": [{"type": "text", "text": "x"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 50000, "output_tokens": 17},
    }


def _full_anthropic_payload(text="RESUME-VIA-COMPLET"):
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 50000, "output_tokens": 500},
    }


def _shape_messages_body(model="minimax-m2.5", n_chars=1500):
    return {
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "resume plz " + "x" * n_chars}],
    }


def test_messages_stream_compaction_tiny_retries_openai_via(client, monkeypatch):
    """Messages streaming, backend openai-via : tiny chat → refetch → SSE Anthropic."""
    rec = UpstreamRecorder()
    _install_seams(monkeypatch, rec)
    calls = []

    async def _tiny_then_full(body, headers, protocol, model_id, forced_pool=None, req_id=None):
        calls.append(1)
        if len(calls) == 1:
            return FakeResponse(payload=_tiny_chat_completion()), {}, "glm-5-free", "9.9.9.9"
        return FakeResponse(payload=_full_chat_completion("RESUME-VIA-COMPLET")), {}, "glm-5-free", "9.9.9.10"

    monkeypatch.setattr(oc, "_try_free_model_first", _tiny_then_full, raising=False)
    body = _shape_messages_body(model="sonnet")
    body["stream"] = True

    text = _stream_text(client, "/v1/messages", body)

    assert len(calls) == 2, f"refetch attendu, vu {len(calls)}"
    assert "RESUME-VIA-COMPLET" in text, f"résumé absent: {text[:400]!r}"
    assert "message_stop" in text


def test_messages_stream_compaction_tiny_retries_anthropic_native(client, monkeypatch):
    """Messages streaming, backend anthropic natif : tiny → refetch → SSE Anthropic."""
    rec = UpstreamRecorder()
    _install_seams(monkeypatch, rec)
    calls = []

    async def _tiny_then_full(body, headers, protocol, model_id, forced_pool=None, req_id=None):
        calls.append(1)
        if len(calls) == 1:
            return FakeResponse(payload=_tiny_anthropic_payload()), {}, "mimo-v2.5-free", "9.9.9.9"
        return FakeResponse(payload=_full_anthropic_payload()), {}, "mimo-v2.5-free", "9.9.9.10"

    monkeypatch.setattr(oc, "_try_free_model_first", _tiny_then_full, raising=False)
    body = _shape_messages_body(model="minimax-m2.5")
    body["stream"] = True

    text = _stream_text(client, "/v1/messages", body)

    assert len(calls) == 2, f"refetch attendu, vu {len(calls)}"
    assert "RESUME-VIA-COMPLET" in text, f"résumé absent: {text[:400]!r}"
    assert "message_stop" in text
