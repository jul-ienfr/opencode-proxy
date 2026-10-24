"""V5.3 — parite d'usage cote Anthropic.

**Ce qui est verifie.** Qu'un `usage` OpenAI (`prompt_tokens`, `completion_tokens`,
`prompt_tokens_details.cached_tokens`) arrive au client Anthropic sous les noms
qu'il attend : `input_tokens`, `output_tokens`, `cache_read_input_tokens`.

**Pourquoi ca compte plus qu'il n'y parait.** Un client qui affiche un cout ou un
taux de cache lit ces trois champs. S'ils manquent, il affiche zero — et un zero
silencieux est indiscernable d'une absence reelle de cache : c'est exactement le
type de mensonge que cette session a passe son temps a traquer.

**Ce qui est assume et documente.** A `message_start`, l'usage amont n'est pas
encore connu (OpenAI ne l'envoie qu'a la fin du flux) : `input_tokens` y est donc
une **estimation**, et `cache_read_input_tokens` vaut 0. La valeur reelle arrive
a `message_delta`. Les tests ci-dessous verrouillent ce partage plutot que de
pretendre que le debut du flux en sait plus qu'il ne peut.
"""

import json

import pytest

import opencode as oc

httpx = pytest.importorskip("httpx")


def _chunk(delta=None, finish=None, usage=None):
    payload = {
        "id": "chatcmpl-usage",
        "object": "chat.completion.chunk",
        "model": "big-pickle",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }
    if usage is not None:
        payload["usage"] = usage
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


USAGE = {
    "prompt_tokens": 100,
    "completion_tokens": 7,
    "total_tokens": 107,
    "prompt_tokens_details": {"cached_tokens": 40},
    "completion_tokens_details": {"reasoning_tokens": 5},
}

STREAM = [
    _chunk({"role": "assistant", "content": ""}),
    _chunk({"content": "ok"}),
    _chunk({}, finish="stop", usage=USAGE),
    b"data: [DONE]\n\n",
]


class _Upstream:
    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, chunks):
        self._chunks = chunks

    async def aiter_lines(self):
        for chunk in self._chunks:
            for line in chunk.decode("utf-8").split("\n"):
                if line:
                    yield line

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


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
    _stations = []

    def _best_station(self):
        return None

    def update_config(self, *_a, **_k):
        return None


@pytest.fixture
def upstream(monkeypatch):
    response = _Upstream(STREAM)
    monkeypatch.setattr(oc, "_free_ip_pool", _Pool())
    monkeypatch.setattr(oc, "_free_proxy_mode", lambda: "direct")
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_open_free_stream", lambda *a, **k: _Ctx(response))
    monkeypatch.setattr(oc, "_open_via_pool", lambda *a, **k: _Ctx(response))
    return response


def _payloads(text, event_name):
    out, current = [], None
    for line in text.splitlines():
        if line.startswith("event:"):
            current = line[6:].strip()
        elif line.startswith("data:") and current == event_name:
            raw = line[5:].strip()
            if raw and raw != "[DONE]":
                out.append(json.loads(raw))
    return out


@pytest.mark.asyncio
async def test_message_start_carries_the_anthropic_usage_keys(upstream):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=oc.app), base_url="http://proxy.test", timeout=30.0
    ) as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "big-pickle",
                "stream": True,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "dis bonjour"}],
            },
        )

    assert response.status_code == 200, response.text[:300]
    start = _payloads(response.text, "message_start")[0]
    usage = start["message"]["usage"]
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
        assert key in usage, f"cle Anthropic manquante : {key} (usage={usage})"


@pytest.mark.asyncio
async def test_message_delta_carries_the_real_output_and_cache(upstream):
    """La fin du flux porte les chiffres reels — c'est la qu'ils existent."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=oc.app), base_url="http://proxy.test", timeout=30.0
    ) as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "big-pickle",
                "stream": True,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "dis bonjour"}],
            },
        )

    delta = _payloads(response.text, "message_delta")[0]
    usage = delta.get("usage") or {}
    assert usage.get("output_tokens") == 7, usage
    assert usage.get("cache_read_input_tokens") == 40, f"cache amont perdu : {usage}"


@pytest.mark.asyncio
async def test_start_and_delta_are_not_contradictory(upstream):
    """Le debut annonce une estimation, la fin la verite : jamais l'inverse.

    Un `input_tokens` final inferieur a celui annonce au depart serait une
    regression deja vue ailleurs : le client afficherait une consommation qui
    diminue en cours de reponse.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=oc.app), base_url="http://proxy.test", timeout=30.0
    ) as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "big-pickle",
                "stream": True,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "dis bonjour"}],
            },
        )

    start_usage = _payloads(response.text, "message_start")[0]["message"]["usage"]
    delta_usage = _payloads(response.text, "message_delta")[0].get("usage") or {}
    assert start_usage.get("input_tokens", 0) >= 0
    # `message_delta` ne renvoie pas input_tokens (conforme au protocole) : on
    # verifie seulement qu'il n'en invente pas un plus petit.
    if "input_tokens" in delta_usage:
        assert delta_usage["input_tokens"] >= start_usage.get("input_tokens", 0)
