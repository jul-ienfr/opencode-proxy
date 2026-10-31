"""V5.4 — contrat SSE Anthropic : la sequence exacte, pas seulement le contenu.

**Ce que ce fichier verifie.** Qu'un flux OpenAI converti en Anthropic respecte
l'ordre que le client officiel attend :
`message_start` -> `content_block_start` -> `content_block_delta`+ ->
`content_block_stop` -> `message_delta` -> `message_stop`.

**Pourquoi l'ordre et pas seulement le texte.** Un client SSE n'accumule pas des
octets : il construit un etat. Un `content_block_delta` avant `content_block_start`
le fait planter ; un `message_stop` manquant le laisse attendre indefiniment ;
deux `message_start` cassent son accumulateur. Ces trois defauts produisent un
flux qui « contient la bonne reponse » et que le client refuse quand meme.

**Pourquoi ce fichier existe separement des tests de conversion.** Les tests de
conversion verifient le *payload* ; celui-ci verifie la *machine a etats*. La
session a montre qu'un correctif peut avoir la bonne forme et le mauvais
comportement — c'est exactement l'ecart entre ces deux familles de test.
"""

import json

import pytest

import opencode as oc

httpx = pytest.importorskip("httpx")


def _chunk(delta=None, finish=None, usage=None, **extra):
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "model": "big-pickle",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }
    if usage is not None:
        payload["usage"] = usage
    payload.update(extra)
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


#: Flux amont realiste : role, deux deltas de texte, fin, usage.
OPENAI_STREAM = [
    _chunk({"role": "assistant", "content": ""}),
    _chunk({"content": "Bonjour"}),
    _chunk({"content": " !"}),
    _chunk({}, finish="stop", usage={"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}),
    b"data: [DONE]\n\n",
]


class _StreamUpstream:
    """Amont qui rend un vrai flux SSE OpenAI.

    Les deux interfaces sont fournies : selon le chemin, le proxy lit
    `aiter_bytes` (passthrough) ou `aiter_lines` (conversion Anthropic).
    """

    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, chunks):
        self._chunks = chunks

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aiter_lines(self):
        for chunk in self._chunks:
            for line in chunk.decode("utf-8").split("\n"):
                if line == "":
                    continue
                yield line


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _Pool:
    enabled = True
    active_station = None
    _stations: list = []

    def _best_station(self):
        return None

    def update_config(self, *_a, **_k):
        return None


def _client():
    transport = httpx.ASGITransport(app=oc.app)
    return httpx.AsyncClient(transport=transport, base_url="http://proxy.test", timeout=30.0)


def _events(text):
    """[(event, data)] dans l'ordre, ignore les commentaires keepalive."""
    out, event = [], None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            out.append((event, line[5:].strip()))
        elif line == "":
            event = None
    return out


@pytest.fixture
def upstream(monkeypatch):
    """Branche l'amont factice sur les deux ouvreurs de la jambe free."""

    def _install(chunks=None):
        response = _StreamUpstream(chunks or OPENAI_STREAM)
        monkeypatch.setattr(oc, "_free_ip_pool", _Pool())
        monkeypatch.setattr(oc, "_free_proxy_mode", lambda: "direct")
        monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
        monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
        monkeypatch.setattr(oc, "_open_free_stream", lambda *a, **k: _Ctx(response))
        monkeypatch.setattr(oc, "_open_via_pool", lambda *a, **k: _Ctx(response))
        return response

    return _install


def _anthropic_body():
    return {
        "model": "big-pickle",
        "stream": True,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "dis bonjour"}],
    }


@pytest.mark.asyncio
async def test_anthropic_stream_follows_the_official_sequence(upstream):
    upstream()
    async with _client() as client:
        response = await client.post("/v1/messages", json=_anthropic_body())

    assert response.status_code == 200, response.text[:300]
    assert "text/event-stream" in response.headers.get("content-type", "")

    events = _events(response.text)
    names = [name for name, _ in events]

    assert names[0] == "message_start", names[:4]
    assert "content_block_start" in names, names
    assert "content_block_delta" in names, names
    assert "content_block_stop" in names, names
    assert "message_delta" in names, names
    assert names[-1] == "message_stop", names[-3:]

    # L'ordre, pas seulement la presence.
    order = [
        names.index("message_start"),
        names.index("content_block_start"),
        names.index("content_block_delta"),
        names.index("content_block_stop"),
        names.index("message_delta"),
        names.index("message_stop"),
    ]
    assert order == sorted(order), f"sequence hors ordre : {names}"

    # Un seul message_start / message_stop : un doublon casse l'accumulateur client.
    assert names.count("message_start") == 1
    assert names.count("message_stop") == 1


@pytest.mark.asyncio
async def test_text_deltas_rebuild_the_original_answer(upstream):
    upstream()
    async with _client() as client:
        response = await client.post("/v1/messages", json=_anthropic_body())

    text = ""
    for name, data in _events(response.text):
        if name == "content_block_delta":
            payload = json.loads(data)
            delta = payload.get("delta") or {}
            if delta.get("type") == "text_delta":
                text += delta.get("text") or ""
    assert text == "Bonjour !", repr(text)


@pytest.mark.asyncio
async def test_message_delta_carries_the_stop_reason(upstream):
    upstream()
    async with _client() as client:
        response = await client.post("/v1/messages", json=_anthropic_body())

    deltas = [json.loads(data) for name, data in _events(response.text) if name == "message_delta"]
    assert deltas, "aucun message_delta"
    assert deltas[0].get("delta", {}).get("stop_reason") in {"end_turn", "stop_sequence", "max_tokens", "tool_use"}, (
        deltas[0]
    )


@pytest.mark.asyncio
async def test_no_event_after_message_stop(upstream):
    upstream()
    async with _client() as client:
        response = await client.post("/v1/messages", json=_anthropic_body())

    names = [name for name, _ in _events(response.text)]
    tail = names[names.index("message_stop") + 1 :]
    assert tail == [], f"evenements apres message_stop : {tail}"


@pytest.mark.asyncio
async def test_reasoning_is_exposed_as_a_thinking_block(upstream):
    """Le raisonnement doit arriver dans un bloc distinct, pas dans le texte.

    Un client qui recoit du raisonnement melange au texte l'affiche a
    l'utilisateur comme une reponse : c'est pire que de le perdre.
    """
    upstream(
        [
            _chunk({"role": "assistant", "content": ""}),
            _chunk({"reasoning_content": "je reflechis"}),
            _chunk({"content": "Bonjour"}),
            _chunk({}, finish="stop"),
            b"data: [DONE]\n\n",
        ]
    )
    async with _client() as client:
        response = await client.post("/v1/messages", json=_anthropic_body())

    assert response.status_code == 200, response.text[:300]
    names = [name for name, _ in _events(response.text)]
    assert "message_stop" in names, names

    text, thinking = "", ""
    for name, data in _events(response.text):
        if name != "content_block_delta":
            continue
        delta = json.loads(data).get("delta") or {}
        if delta.get("type") == "text_delta":
            text += delta.get("text") or ""
        elif delta.get("type") in ("thinking_delta", "reasoning_delta"):
            thinking += delta.get("thinking") or delta.get("text") or ""

    assert text == "Bonjour", repr(text)
    assert "reflechis" in thinking, f"raisonnement perdu ou melange au texte : thinking={thinking!r} text={text!r}"


@pytest.mark.asyncio
async def test_tool_call_becomes_a_tool_use_block(upstream):
    """Un appel d'outil doit produire un bloc `tool_use`, pas du texte."""
    upstream(
        [
            _chunk({"role": "assistant", "content": ""}),
            _chunk(
                {
                    "tool_calls": [
                        {"index": 0, "id": "call_1", "type": "function", "function": {"name": "read", "arguments": ""}}
                    ]
                }
            ),
            _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"path":"a.txt"}'}}]}),
            _chunk({}, finish="tool_calls"),
            b"data: [DONE]\n\n",
        ]
    )
    async with _client() as client:
        response = await client.post("/v1/messages", json=_anthropic_body())

    assert response.status_code == 200, response.text[:400]
    starts = [json.loads(d) for n, d in _events(response.text) if n == "content_block_start"]
    types = [b.get("content_block", {}).get("type") for b in starts]
    assert "tool_use" in types, f"blocs produits : {types} | flux : {response.text[:300]}"

    tool = [b for b in starts if b.get("content_block", {}).get("type") == "tool_use"][0]
    assert tool["content_block"]["name"] == "read"
