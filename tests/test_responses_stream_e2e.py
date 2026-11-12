"""[PLAN_AUDIT_CONVERSIONS Lot L5] Bout en bout : le flux Responses sort sur le wire HTTP.

Les tests de `mapping.responses_stream_events` prouvent la **construction** de la
séquence. Ce fichier prouve qu'elle atteint réellement le client à travers le
handler `/v1/responses` — c'est là qu'était le défaut A11/A21 : les cinq sites du
handler émettaient un unique `response.completed`, sans même le
`response.created` initial.

Un test unitaire du constructeur ne l'aurait pas attrapé : le constructeur
n'était simplement **jamais appelé**. On monte donc l'app FastAPI et on appelle
le vrai endpoint avec un upstream Chat simulé (SSE réel), en vérifiant :

* `content-type: text/event-stream` ;
* l'ordre contractuel des événements ;
* la reconstruction exacte du texte depuis les deltas ;
* l'usage final porté par `response.completed` ;
* la sentinelle `[DONE]` conservée pour nos clients existants.
"""

import json

import pytest
from fastapi.testclient import TestClient

import opencode

_TEXT = "Bonjour, ceci est une reponse de test suffisamment longue."
# [CATALOGUE 2026-10-27] `deepseek-v4-flash` n'est plus annonce amont
# (logs/live_models.json) : le gate live renvoyait 404 « Model not found »
# des qu'un test du run forçait le rechargement du catalogue.
_MODEL = "deepseek-v4.1-flash"


class _FakeStreamResp:
    """Upstream Chat simulé : deltas SSE puis chunk d'usage final."""

    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def json(self):
        return {}

    async def aiter_lines(self):
        for start in range(0, len(_TEXT), 20):
            yield "data: " + json.dumps(
                {
                    "id": "chatcmpl_test",
                    "object": "chat.completion.chunk",
                    "model": _MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": _TEXT[start : start + 20]},
                            "finish_reason": None,
                        }
                    ],
                }
            )
        yield "data: " + json.dumps(
            {
                "id": "chatcmpl_test",
                "object": "chat.completion.chunk",
                "model": _MODEL,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18},
            }
        )
        yield "data: [DONE]"


@pytest.fixture
def streamed(monkeypatch):
    """Appelle le vrai endpoint avec un upstream simulé, renvoie (status, headers, events)."""

    async def _fake(endpoint, body, headers, protocol, *a, **k):
        return _FakeStreamResp(), {}

    monkeypatch.setattr(opencode, "_do_request_with_retry", _fake)

    async def _fake_free(endpoint, body, headers):
        # [gate body 2026-09-18] la jambe free directe ne passe plus par
        # _do_request_with_retry : même simulacre, même contrat.
        return _FakeStreamResp(), {}

    monkeypatch.setattr(opencode, "_do_free_direct_request", _fake_free)

    # Pas de `with TestClient(app)` : le contexte déclenche le lifespan de
    # démarrage (pollers/superviseur en tâches de fond) qui ne se termine pas
    # sous pytest. Le client nu suffit — on ne teste que le handler.
    client = TestClient(opencode.app)
    with client.stream(
        "POST",
        "/v1/responses",
        json={
            "model": _MODEL,
            "stream": True,
            "input": [{"role": "user", "content": "dis bonjour"}],
        },
    ) as r:
        status = r.status_code
        ctype = r.headers.get("content-type", "")
        raw = b"".join(r.iter_bytes()).decode()

    frames = [ln[6:] for ln in raw.splitlines() if ln.startswith("data: ") and ln[6:].strip() != "[DONE]"]
    return status, ctype, [json.loads(f) for f in frames], raw


def test_stream_endpoint_returns_event_stream(streamed):
    """Le mode `stream: true` doit répondre en SSE, pas en JSON unique."""
    status, ctype, _events, _raw = streamed
    assert status == 200
    assert "text/event-stream" in ctype


def test_stream_starts_with_response_created(streamed):
    """A21 — LE point : le premier événement est `response.created`.

    Avant correctif, le flux commençait par `response.completed` (quand il ne
    contenait que lui), donc un client attendant `created` n'affichait rien.
    """
    _status, _ctype, events, _raw = streamed
    assert events, "aucun événement SSE émis"
    assert events[0]["type"] == "response.created"


def test_stream_contains_incremental_text_deltas(streamed):
    """A11 — le texte arrive par deltas, pas d'un bloc à la fin."""
    _status, _ctype, events, _raw = streamed
    deltas = [e for e in events if e["type"] == "response.output_text.delta"]
    assert deltas, "aucun delta texte : le faux streaming est revenu"


def test_stream_reconstructs_the_full_text(streamed):
    """Le découpage ne perd ni ne duplique de contenu."""
    _status, _ctype, events, _raw = streamed
    text = "".join(e.get("delta", "") for e in events if e["type"] == "response.output_text.delta")
    assert text == _TEXT


def test_stream_ends_with_completed_and_usage(streamed):
    """`response.completed` est terminal et porte l'usage final."""
    _status, _ctype, events, _raw = streamed
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["usage"]["output_tokens"] == 11
    assert events[-1]["response"]["usage"]["input_tokens"] == 7


def test_stream_sequence_numbers_are_contiguous(streamed):
    """Un trou/ doublon ferait réinitialiser l'état d'un client conforme."""
    _status, _ctype, events, _raw = streamed
    assert [e["sequence_number"] for e in events] == list(range(len(events)))


def test_stream_keeps_done_sentinel(streamed):
    """`[DONE]` reste émis : nos clients existants s'en servent comme fin."""
    _status, _ctype, _events, raw = streamed
    assert raw.rstrip().endswith("[DONE]")


def test_stream_item_lifecycle_is_balanced(streamed):
    """Chaque item ouvert est refermé (sinon le client garde un item pendu)."""
    _status, _ctype, events, _raw = streamed
    added = [e["output_index"] for e in events if e["type"] == "response.output_item.added"]
    done = [e["output_index"] for e in events if e["type"] == "response.output_item.done"]
    assert added == done


# ─────────── non-régression : `stream: false` ne doit PAS devenir du SSE ───────────
#
# La majorité des sites de streaming sont sur des chemins gardés par un
# `if not is_stream: … return` (motif retour-précoce), donc implicitement
# streaming. Cette garde le prouve empiriquement : si une voie non-streaming
# était convertie par erreur, un client demandant du JSON recevrait du SSE et
# casserait.
#
# ATTENTION — l'affirmation « les 5 sites sont gardés » était **fausse** : deux
# sites de repli (clés API en pause) émettaient du SSE sans consulter le mode du
# client, et l'un d'eux était même dans la branche `else:` du garde censé le
# protéger. C'est le défaut D4, corrigé depuis ; les tests de câblage vivent dans
# `test_review_findings_d1_d4.py`. Les fixtures ci-dessous ne couvrent que la
# voie payante nominale : elles n'exerçaient pas `except AllKeysPausedError`, et
# ne pouvaient donc pas voir D4.


class _FakeJsonResp:
    """Upstream non-streaming : réponse Chat JSON complète."""

    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


_CHAT_JSON = {
    "id": "chatcmpl_test",
    "object": "chat.completion",
    "created": 1,
    "model": _MODEL,
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "reponse complete"},
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}


@pytest.fixture
def non_streamed(monkeypatch):
    """Appelle le vrai endpoint avec `stream: false` et un upstream JSON."""

    async def _fake(endpoint, body, headers, protocol, *a, **k):
        return _FakeJsonResp(_CHAT_JSON), {}

    monkeypatch.setattr(opencode, "_do_request_with_retry", _fake)

    async def _fake_free(endpoint, body, headers):
        # [gate body 2026-09-18] cf. fixture streamed ci-dessus.
        return _FakeJsonResp(_CHAT_JSON), {}

    monkeypatch.setattr(opencode, "_do_free_direct_request", _fake_free)
    client = TestClient(opencode.app)
    r = client.post(
        "/v1/responses",
        json={
            "model": _MODEL,
            "stream": False,
            "input": [{"role": "user", "content": "bonjour"}],
        },
    )
    return r


def test_stream_false_returns_json_not_sse(non_streamed):
    """Une requête `stream: false` doit recevoir du JSON, jamais du SSE."""
    ctype = non_streamed.headers.get("content-type", "")
    assert "text/event-stream" not in ctype, (
        f"stream:false renvoie du SSE ({ctype}) : une voie non-streaming a été convertie par erreur en streaming"
    )
    assert "response.created" not in non_streamed.text, "événements SSE émis pour une requête non-streaming"


def test_stream_false_body_is_a_responses_object(non_streamed):
    """Et le corps reste une réponse Responses exploitable."""
    assert non_streamed.status_code == 200
    data = non_streamed.json()
    assert data.get("object") == "response"
    assert data.get("output"), "aucun item de sortie"


# ─────────── amont SSE « cycle de vie seul » (aucun delta de contenu) ───────────
#
# Défaut observé : l'amont répond correctement en non-streaming, mais son flux
# `/v1/responses` n'émet que des événements de cycle de vie. Le proxy rendait
# alors un 200 avec un contenu vide. Le contenu doit être reconstruit depuis
# `output_item.done` / `response.completed`, et si le flux est réellement vide,
# la même requête doit être rejouée en non-streaming.

_LIFECYCLE_TEXT = "texte reconstruit depuis le cycle de vie"

_LIFECYCLE_ITEM = {
    "id": "msg_life",
    "type": "message",
    "role": "assistant",
    "status": "completed",
    "content": [{"type": "output_text", "text": _LIFECYCLE_TEXT, "annotations": []}],
}


class _LifecycleOnlyResp:
    """Flux Responses réduit au cycle de vie (aucun delta de contenu)."""

    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, *, item_done: bool, completed_output: bool):
        self.item_done = item_done
        self.completed_output = completed_output

    def json(self):
        return {}

    async def aiter_lines(self):
        created = {
            "id": "resp_life",
            "object": "response",
            "status": "in_progress",
            "model": _MODEL,
            "output": [],
        }
        yield "data: " + json.dumps({"type": "response.created", "response": created})
        yield "data: " + json.dumps({"type": "response.in_progress", "response": created})
        if self.item_done:
            yield "data: " + json.dumps(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**_LIFECYCLE_ITEM, "content": []},
                }
            )
            yield "data: " + json.dumps(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": _LIFECYCLE_ITEM,
                }
            )
        final = {
            "id": "resp_life",
            "object": "response",
            "status": "completed",
            "model": _MODEL,
            "usage": {"input_tokens": 4, "output_tokens": 6, "total_tokens": 10},
            "output": [_LIFECYCLE_ITEM] if self.completed_output else [],
        }
        yield "data: " + json.dumps({"type": "response.completed", "response": final})
        yield "data: [DONE]"


def _post_stream(monkeypatch, upstream):
    """Monte l'endpoint et renvoie (status, ctype, events, raw, appels amont)."""
    calls: list[dict] = []

    async def _fake(endpoint, body, headers, protocol, *a, **k):
        calls.append(dict(body))
        return upstream(body), {}

    async def _no_free(*a, **k):
        return None

    monkeypatch.setattr(opencode, "_do_request_with_retry", _fake)
    monkeypatch.setattr(opencode, "_try_free_model_first", _no_free)

    client = TestClient(opencode.app)
    with client.stream(
        "POST",
        "/v1/responses",
        json={
            "model": _MODEL,
            "stream": True,
            "input": [{"role": "user", "content": "dis bonjour"}],
        },
    ) as r:
        status = r.status_code
        ctype = r.headers.get("content-type", "")
        raw = b"".join(r.iter_bytes()).decode()

    frames = [ln[6:] for ln in raw.splitlines() if ln.startswith("data: ") and ln[6:].strip() != "[DONE]"]
    return status, ctype, [json.loads(f) for f in frames], raw, calls


def _streamed_text(events) -> str:
    return "".join(e.get("delta", "") for e in events if e["type"] == "response.output_text.delta")


def test_lifecycle_only_stream_recovers_text_from_output_item_done(monkeypatch):
    _status, _ctype, events, _raw, calls = _post_stream(
        monkeypatch, lambda body: _LifecycleOnlyResp(item_done=True, completed_output=False)
    )
    assert _streamed_text(events) == _LIFECYCLE_TEXT
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["usage"]["output_tokens"] == 6
    assert all(c.get("stream") for c in calls), "le repli non-streaming ne devait pas être nécessaire"


def test_lifecycle_only_stream_recovers_text_from_completed(monkeypatch):
    _status, _ctype, events, _raw, _calls = _post_stream(
        monkeypatch, lambda body: _LifecycleOnlyResp(item_done=False, completed_output=True)
    )
    assert _streamed_text(events) == _LIFECYCLE_TEXT
    assert events[-1]["type"] == "response.completed"


def test_lifecycle_only_stream_does_not_duplicate_text(monkeypatch):
    _status, _ctype, events, _raw, _calls = _post_stream(
        monkeypatch, lambda body: _LifecycleOnlyResp(item_done=True, completed_output=True)
    )
    assert _streamed_text(events) == _LIFECYCLE_TEXT


def test_fully_empty_stream_falls_back_to_nonstream(monkeypatch):
    def _upstream(body):
        if body.get("stream"):
            return _LifecycleOnlyResp(item_done=False, completed_output=False)
        return _FakeJsonResp(_CHAT_JSON)

    _status, _ctype, events, _raw, calls = _post_stream(monkeypatch, _upstream)
    assert _streamed_text(events) == "reponse complete"
    assert events[-1]["type"] == "response.completed"
    assert any(not c.get("stream") for c in calls), "le repli non-streaming n'a pas été tenté"
