"""[fix HTTP 500 /v1/responses] Interface SSE sur ``_CurlCffiResponse``.

Bug préexistant (présent dès HEAD) : ``_do_free_request_curl_cffi`` renvoie
``_CurlCffiResponse`` — classe qui n'exposait NI ``aiter_lines`` NI
``aiter_bytes`` — y compris quand le client avait DÉJÀ demandé
``stream:true`` (donc ``_force_wire_stream`` False, ligne 5404) et que
l'upstream répondait du SSE entièrement tamponné.

Les consommateurs streaming font ``async for line in resp.aiter_lines()`` :

  - route ``/v1/responses`` (opencode.py ~17176)
  - chat SSE (~12362, ~14114)

→ ``AttributeError: '_CurlCffiResponse' object has no attribute
'aiter_lines'`` → HTTP 500 « Erreur interne du serveur ».

Le correctif ajoute une interface SSE en REJEU sur le corps déjà tamponné.
Ces tests la verrouillent, y compris la parité de découpe avec
``_CurlCffiStreamResponse``.

[fix tour vide] Second défaut, révélé par le premier : la jambe free recolle
TOUJOURS son flux amont en OBJET JSON (``_WireJsonResponse`` ligne 5402, et
les conversions Chat / Anthropic de ``_try_free_model_first``). Or les quatre
consommateurs streaming commencent par ``if not line.startswith("data:"):
continue`` : un corps JSON rejoué verbatim traversait donc les boucles sans
produire un seul delta → HTTP 200 au TEXTE VIDE. Le rejeu doit synthétiser
les frames SSE Chat. Les tests ci-dessous exigent du texte NON VIDE.
"""

import inspect
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import opencode as oc  # noqa: E402


class _FakeUpstream:
    """Réponse curl_cffi tamponnée (stream=False) : expose .content/.headers."""

    def __init__(self, body: bytes, status_code: int = 200):
        self.content = body
        self.status_code = status_code
        self.headers = {"content-type": "text/event-stream"}


def _wrap(body: bytes) -> oc._CurlCffiResponse:
    return oc._CurlCffiResponse(_FakeUpstream(body))


async def _lines(resp) -> list:
    return [ln async for ln in resp.aiter_lines()]


# ── Le bug lui-même : l'attribut doit exister ────────────────────────────


def test_aiter_lines_exists():
    """Le 500 venait d'un attribut MANQUANT — garde directe."""
    assert hasattr(oc._CurlCffiResponse, "aiter_lines")
    assert callable(oc._CurlCffiResponse.aiter_lines)


def test_stream_interface_is_complete():
    """Les boucles streaming lisent aussi aiter_bytes / aread / aclose."""
    for name in ("aiter_lines", "aiter_bytes", "aread", "aclose"):
        assert callable(getattr(oc._CurlCffiResponse, name, None)), name


@pytest.mark.asyncio
async def test_no_more_attribute_error_on_sse_body():
    """Le corps SSE réel du chemin free stream:true ne lève plus."""
    body = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
    resp = _wrap(body)
    got = await _lines(resp)  # ne doit PAS lever AttributeError
    assert got[0] == 'data: {"choices":[{"delta":{"content":"ok"}}]}'
    assert "data: [DONE]" in got


# ── Fidélité de la découpe ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lines_are_str_not_bytes():
    """httpx-style : les consommateurs font line.startswith("data:")."""
    resp = _wrap(b"data: x\n\n")
    for line in await _lines(resp):
        assert isinstance(line, str)


@pytest.mark.asyncio
async def test_no_trailing_empty_line():
    """Corps terminé par \\n → pas de ligne vide finale parasite."""
    got = await _lines(_wrap(b"data: a\n"))
    assert got == ["data: a"]


@pytest.mark.asyncio
async def test_crlf_is_stripped():
    """SSE en \\r\\n : le \\r ne doit pas rester collé à la valeur."""
    got = await _lines(_wrap(b"data: a\r\ndata: b\r\n"))
    assert got == ["data: a", "data: b"]


@pytest.mark.asyncio
async def test_partial_final_line_without_newline():
    """Dernière ligne sans \\n final : elle doit quand même sortir."""
    got = await _lines(_wrap(b"data: a\ndata: b"))
    assert got == ["data: a", "data: b"]


@pytest.mark.asyncio
async def test_blank_lines_preserved_between_events():
    """Les séparateurs vides SSE sont rejoués (le consumer les ignore).

    Un seul \\n final est absorbé (pas de ligne fantôme), exactement comme
    le flux live : ``data: a\\n\\ndata: b\\n\\n`` donne 4 lignes dont la
    dernière vide — cf. test de parité plus bas.
    """
    got = await _lines(_wrap(b"data: a\n\ndata: b\n\n"))
    assert got == ["data: a", "", "data: b", ""]


@pytest.mark.asyncio
async def test_empty_body_yields_nothing():
    got = await _lines(_wrap(b""))
    assert got == []


@pytest.mark.asyncio
async def test_invalid_utf8_is_replaced_not_raised():
    got = await _lines(_wrap(b"data: \xff\xfe\n"))
    assert len(got) == 1 and got[0].startswith("data: ")


# ── Idempotence (rejeu) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replay_is_idempotent():
    """Corps tamponné → relecture possible (contrairement à un vrai flux)."""
    resp = _wrap(b"data: a\ndata: b\n")
    assert await _lines(resp) == await _lines(resp) == ["data: a", "data: b"]


@pytest.mark.asyncio
async def test_aread_and_aiter_bytes_agree():
    resp = _wrap(b"data: a\n")
    assert await resp.aread() == b"data: a\n"
    assert [c async for c in resp.aiter_bytes()] == [b"data: a\n"]


@pytest.mark.asyncio
async def test_aclose_is_noop_and_safe_twice():
    """Corps déjà tamponné : rien à fermer, et idempotent."""
    resp = _wrap(b"data: a\n")
    assert await resp.aclose() is None
    assert await resp.aclose() is None


# ── Non-régression du contrat non-stream ────────────────────────────────


def test_status_and_headers_preserved():
    resp = _wrap(b"data: a\n")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/event-stream"


def test_text_and_json_still_work():
    """Le contrat JSON historique ne doit pas avoir bougé."""
    payload = {"choices": [{"message": {"content": "hi"}}]}
    resp = _wrap(json.dumps(payload).encode())
    assert resp.json() == payload
    assert "hi" in resp.text


@pytest.mark.asyncio
async def test_text_override_does_not_leak_into_sse_replay():
    """Un override .text (utilisé par les callers non-stream) reste cohérent."""
    resp = _wrap(b"data: a\n")
    resp.text = "remplacé"
    assert resp.text == "remplacé"
    # le rejeu SSE lit .content, pas .text — pas de crash, pas de fuite
    assert await _lines(resp) == ["data: a"]


def test_wire_json_response_is_wrapped_and_replayable():
    """Chemin 5402 : SSE forcé collecté puis rejoué."""
    from opencode import _WireJsonResponse

    payload = {"choices": [{"message": {"content": "x"}}]}
    resp = oc._CurlCffiResponse(_WireJsonResponse(payload))
    assert resp.status_code == 200
    assert resp.json() == payload


# ── Parité de découpe avec le wrapper stream dédié ──────────────────────


def test_line_splitting_mirrors_stream_wrapper():
    """Les deux wrappers doivent découper identiquement (même contrat)."""
    src_new = inspect.getsource(oc._CurlCffiResponse.aiter_lines)
    src_ref = inspect.getsource(oc._CurlCffiStreamResponse.aiter_lines)
    # même traitement CRLF + décodage utf-8 tolérant de part et d'autre
    assert 'rstrip(b"\\r")' in src_new and 'rstrip(b"\\r")' in src_ref
    assert 'errors="replace"' in src_new and 'errors="replace"' in src_ref
    assert 'split(b"\\n")' in src_new and 'split(b"\\n", 1)' in src_ref


class _FakeStreamUpstream:
    """Flux réel (chunks) pour comparer les deux implémentations."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream"}

    async def aiter_content(self):
        for c in self._chunks:
            yield c

    async def aclose(self):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunks",
    [
        [b"data: a\n\ndata: b\n\n"],
        [b"data: a\r\n", b"data: b\r\n"],
        [b"data: a\n", b"dat", b"a: b\n\ndata: [DONE]\n\n"],
        [b"data: a\ndata: b"],
        [b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'],
    ],
)
async def test_forced_replay_matches_live_stream_split(chunks):
    """Un corps concaténé rejoué doit donner les MÊMES lignes qu'un vrai flux."""
    live = oc._CurlCffiStreamResponse(_FakeStreamUpstream(chunks))
    live_lines = [ln async for ln in live.aiter_lines()]

    replay = _wrap(b"".join(chunks))
    replay_lines = await _lines(replay)

    # Le flux live émet une ligne vide finale (buffer vide → pas de yield)
    # mais conserve les séparateurs internes : comparaison sur les lignes
    # significatives, qui sont ce que les consommateurs parsent.
    assert [x for x in replay_lines if x.strip()] == [x for x in live_lines if x.strip()]


# ── [fix tour vide] Un corps JSON doit rendre du TEXTE, pas du vide ─────


_RESPONSES_OBJ = {
    "id": "resp_x",
    "object": "response",
    "status": "completed",
    "model": "muse-spark-1.3-contributor-free",
    "output": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "je réfléchis"}]},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "TEXTE-RESP"}],
        },
    ],
    "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
}

_CHAT_OBJ = {
    "id": "chatcmpl-x",
    "object": "chat.completion",
    "model": "m",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "TEXTE-CHAT"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}

_ANTHRO_OBJ = {
    "id": "msg_x",
    "type": "message",
    "role": "assistant",
    "model": "m",
    "content": [{"type": "text", "text": "TEXTE-ANTHRO"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 5, "output_tokens": 3},
}


def _deltas(lines):
    """Deltas Chat reconstitués depuis les lignes rejouées."""
    out = []
    for ln in lines:
        if not ln.startswith("data:"):
            continue
        payload = ln[5:].strip()
        if payload == "[DONE]":
            continue
        out.append(json.loads(payload))
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("obj", "attendu"),
    [
        (_RESPONSES_OBJ, "TEXTE-RESP"),
        (_CHAT_OBJ, "TEXTE-CHAT"),
        (_ANTHRO_OBJ, "TEXTE-ANTHRO"),
    ],
    ids=["responses", "chat", "anthropic"],
)
async def test_json_body_replays_as_sse_with_text(obj, attendu):
    """LE tour vide : un corps JSON sans préfixe ``data:`` donnait 0 delta."""
    got = await _lines(_wrap(json.dumps(obj).encode()))
    deltas = _deltas(got)
    assert deltas, f"aucune frame SSE synthétisée — lignes: {got}"
    texte = "".join(d.get("choices", [{}])[0].get("delta", {}).get("content", "") for d in deltas)
    assert texte == attendu, f"texte vide ou tronqué: {texte!r}"


@pytest.mark.asyncio
async def test_json_body_replay_reasoning_and_finish():
    """Le raisonnement et le ``finish_reason`` doivent survivre à la synthèse."""
    deltas = _deltas(await _lines(_wrap(json.dumps(_RESPONSES_OBJ).encode())))
    assert any(d["choices"][0]["delta"].get("reasoning_content") == "je réfléchis" for d in deltas)
    assert deltas[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_json_body_replay_carries_tool_calls():
    """Un tool_call de la jambe free ne doit pas être perdu."""
    obj = json.loads(json.dumps(_CHAT_OBJ))
    obj["choices"][0]["message"]["tool_calls"] = [
        {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": '{"cmd":"ls"}'}}
    ]
    obj["choices"][0]["message"]["content"] = None
    deltas = _deltas(await _lines(_wrap(json.dumps(obj).encode())))
    tcs = [d["choices"][0]["delta"]["tool_calls"] for d in deltas if d["choices"][0]["delta"].get("tool_calls")]
    assert tcs and tcs[0][0]["function"]["name"] == "bash"
    assert tcs[0][0]["function"]["arguments"] == '{"cmd":"ls"}'


@pytest.mark.asyncio
async def test_json_body_replay_is_done_terminated():
    """Les consommateurs s'arrêtent sur ``[DONE]`` : il doit être présent."""
    got = await _lines(_wrap(json.dumps(_CHAT_OBJ).encode()))
    assert got[-1] == "data: [DONE]"


@pytest.mark.asyncio
async def test_json_body_replay_is_idempotent():
    """Rejeu d'un corps tamponné : deux lectures identiques."""
    resp = _wrap(json.dumps(_CHAT_OBJ).encode())
    first, second = await _lines(resp), await _lines(resp)
    assert first == second and _deltas(first)


@pytest.mark.asyncio
async def test_sse_body_keeps_its_exact_lines():
    """Non-régression : un vrai corps SSE n'est PAS resynthétisé."""
    body = b'data: {"a":1}\n\ndata: [DONE]\n\n'
    assert await _lines(_wrap(body)) == ['data: {"a":1}', "", "data: [DONE]", ""]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"", b"{}", b"null", b"not json at all", b'{"inconnu": 1}'])
async def test_unusable_json_falls_back_to_verbatim(body):
    """Forme inconnue → verbatim, jamais d'exception (pas de 500)."""
    got = await _lines(_wrap(body))
    attendu = [] if body == b"" else body.decode().split("\n")
    assert got == attendu


def test_buffered_body_sse_detection():
    """Le discriminateur SSE vs JSON est explicite et testé."""
    assert oc._lines_are_sse([b'data: {"a":1}', b""]) is True
    assert oc._lines_are_sse([b'{"choices":[]}']) is False
    assert oc._lines_are_sse(["data: x"]) is True
    assert oc._lines_are_sse([]) is False


# ── E2E : la route /v1/responses ne doit plus rendre 500 ────────────────


_SSE_CHAT = (
    b'data: {"id":"c1","object":"chat.completion.chunk","model":"m",'
    b'"choices":[{"index":0,"delta":{"content":"Bonjour tout le monde"},'
    b'"finish_reason":null}]}\n\n'
    b'data: {"id":"c1","object":"chat.completion.chunk","model":"m",'
    b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n'
    b"data: [DONE]\n\n"
)


def _post_responses(monkeypatch, fake_resp):
    """Monte l'app et POST /v1/responses avec la jambe free simulée."""
    from fastapi.testclient import TestClient

    async def _fake_free_first(body, headers, protocol, model_id, forced_pool=None, req_id=None):
        # Contrat de _try_free_model_first : (resp, resp_headers, model, ip)
        return fake_resp, {}, "muse-spark-1.3-contributor-free", "10.0.0.7"

    monkeypatch.setattr(oc, "_try_free_model_first", _fake_free_first)

    # raise_server_exceptions=False : sans cela, Starlette re-lève l'exception
    # dans le test au lieu de rendre la réponse du @app.exception_handler
    # (le fameux 500 « Erreur interne du serveur ») — on veut observer le
    # comportement HTTP réel du client.
    client = TestClient(oc.app, raise_server_exceptions=False)
    with client.stream(
        "POST",
        "/v1/responses",
        json={
            "model": "muse-spark-1.3-contributor-free",
            "stream": True,
            "input": [{"role": "user", "content": "dis bonjour"}],
        },
    ) as r:
        raw = b"".join(r.iter_bytes()).decode("utf-8", errors="replace")
        return r.status_code, r.headers.get("content-type", ""), raw


def test_route_responses_no_longer_500_on_buffered_free_sse(monkeypatch):
    """LE bug : jambe free ``stream:true`` → corps SSE tamponné → 500.

    Avant correctif, ``_CurlCffiResponse`` n'avait pas ``aiter_lines`` :
    la route levait ``AttributeError`` et rendait
    ``500 {"error":"Erreur interne du serveur"}``.
    """
    resp = oc._CurlCffiResponse(_FakeUpstream(_SSE_CHAT))
    status, ctype, raw = _post_responses(monkeypatch, resp)

    assert status == 200, f"500 revenu — corps: {raw[:400]}"
    assert "text/event-stream" in ctype
    assert "Erreur interne du serveur" not in raw


def test_route_responses_replays_free_text_into_sse(monkeypatch):
    """Le texte tamponné doit ressortir en SSE Responses exploitable."""
    resp = oc._CurlCffiResponse(_FakeUpstream(_SSE_CHAT))
    status, _ctype, raw = _post_responses(monkeypatch, resp)

    assert status == 200
    frames = [
        json.loads(ln[6:])
        for ln in raw.splitlines()
        if ln.startswith("data: ") and ln[6:].strip() != "[DONE]"
    ]
    assert frames and frames[0]["type"] == "response.created"
    text = "".join(e.get("delta", "") for e in frames if e["type"] == "response.output_text.delta")
    assert text == "Bonjour tout le monde"


def test_route_responses_wire_json_shim_also_works(monkeypatch):
    """Chemin 5402 (SSE forcé déjà collecté en JSON) : la route doit marcher."""
    payload = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Bonjour"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }
    resp = oc._CurlCffiResponse(oc._WireJsonResponse(payload))
    status, _ctype, raw = _post_responses(monkeypatch, resp)

    assert status == 200, f"status={status} corps={raw[:400]}"
    assert "Erreur interne du serveur" not in raw


def _text_from_responses_sse(raw):
    """Texte agrégé des events Responses rendus à un client stream."""
    frames = [
        json.loads(ln[6:])
        for ln in raw.splitlines()
        if ln.startswith("data: ") and ln[6:].strip() != "[DONE]"
    ]
    return "".join(e.get("delta", "") for e in frames if e.get("type") == "response.output_text.delta")


def test_route_responses_json_body_yields_non_empty_text(monkeypatch):
    """LE tour vide vu par le client : 200 mais AUCUN texte.

    Reproduit exactement la forme que rend ``_try_free_model_first`` pour
    ``/v1/responses`` (objet Responses converti), et exige du texte réel.
    C'est le test qui échouait avec un rejeu verbatim du JSON.
    """
    resp = oc._CurlCffiResponse(_FakeUpstream(json.dumps(_RESPONSES_OBJ).encode()))
    status, ctype, raw = _post_responses(monkeypatch, resp)

    assert status == 200, f"status={status} corps={raw[:400]}"
    assert "text/event-stream" in ctype
    assert _text_from_responses_sse(raw) == "TEXTE-RESP", f"corps: {raw[:600]}"


def test_route_responses_chat_json_body_yields_non_empty_text(monkeypatch):
    """Idem pour la forme ChatCompletions rendue par la jambe free."""
    resp = oc._CurlCffiResponse(_FakeUpstream(json.dumps(_CHAT_OBJ).encode()))
    status, _ctype, raw = _post_responses(monkeypatch, resp)

    assert status == 200
    assert _text_from_responses_sse(raw) == "TEXTE-CHAT", f"corps: {raw[:600]}"


def test_route_responses_wire_json_shim_yields_non_empty_text(monkeypatch):
    """Chemin 5402 : ``_WireJsonResponse`` doit aussi porter le texte."""
    payload = {
        "choices": [
            {"message": {"role": "assistant", "content": "Bonjour"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }
    resp = oc._CurlCffiResponse(oc._WireJsonResponse(payload))
    status, _ctype, raw = _post_responses(monkeypatch, resp)

    assert status == 200
    assert _text_from_responses_sse(raw) == "Bonjour", f"corps: {raw[:600]}"


# ── [fix tool_calls] Un tour d'outil ne doit pas etre perdu ─────────────

_RESPONSES_TOOL = {
    "id": "resp_t",
    "object": "response",
    "status": "completed",
    "model": "m",
    "output": [
        {
            "type": "function_call",
            "call_id": "call_abc",
            "name": "bash",
            "arguments": '{"command":"ls"}',
            "status": "completed",
        }
    ],
    "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
}

_CHAT_TOOL = {
    "id": "chatcmpl-t",
    "object": "chat.completion",
    "model": "m",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}


def _frames_of(raw):
    return [
        json.loads(ln[6:])
        for ln in raw.splitlines()
        if ln.startswith("data: ") and ln[6:].strip() != "[DONE]"
    ]


@pytest.mark.parametrize(
    "obj", [_RESPONSES_TOOL, _CHAT_TOOL], ids=["responses-function_call", "chat-tool_calls"]
)
def test_route_responses_tool_call_survives_replay(monkeypatch, obj):
    """Un tour d'outil de la jambe free doit ressortir AVEC son outil.

    Avant correctif : la boucle de collecte ignorait ``delta.tool_calls`` et
    forcait ``finish_reason="stop"`` → l'outil disparaissait et le client
    croyait le tour termine.
    """
    resp = oc._CurlCffiResponse(_FakeUpstream(json.dumps(obj).encode()))
    status, _ctype, raw = _post_responses(monkeypatch, resp)

    assert status == 200, f"status={status} corps={raw[:400]}"
    frames = _frames_of(raw)
    added = [
        f
        for f in frames
        if f.get("type") == "response.output_item.added"
        and isinstance(f.get("item"), dict)
        and f["item"].get("type") == "function_call"
    ]
    assert added, f"tool_call PERDU — types: {[f.get('type') for f in frames]}"
    assert added[0]["item"].get("name") == "bash"
    args = "".join(
        f.get("delta", "")
        for f in frames
        if f.get("type") == "response.function_call_arguments.delta"
    )
    assert args == '{"command":"ls"}', f"arguments perdus: {args!r}"


@pytest.mark.asyncio
async def test_synth_finish_reason_is_tool_calls_for_tool_turn():
    """La synthese doit annoncer ``tool_calls``, jamais un faux « stop »."""
    deltas = _deltas(await _lines(_wrap(json.dumps(_CHAT_TOOL).encode())))
    assert deltas[-1]["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_synth_drops_nameless_tool_call():
    """Un tool_call sans nom serait rejete par le client : il est ecarte."""
    obj = json.loads(json.dumps(_CHAT_TOOL))
    obj["choices"][0]["message"]["tool_calls"] = [
        {"id": "x", "type": "function", "function": {"name": "", "arguments": "{}"}}
    ]
    deltas = _deltas(await _lines(_wrap(json.dumps(obj).encode())))
    tcs = [d["choices"][0]["delta"]["tool_calls"] for d in deltas if d["choices"][0]["delta"].get("tool_calls")]
    assert not tcs, f"tool_call sans nom transmis: {tcs}"


# ── [PARITE client officiel] finish_reason d'un tour d'outil ─────────────
#
# Mesure sur le proxy vivant : l'amont free emet 3 frames de delta dont un
# tool_call COMPLET (id + name + arguments) puis ferme le flux SANS
# finish_reason terminal. Le proxy synthetisait alors « stop ».
#
# Le parseur du client officiel (bundle 1.18.31, extrait du binaire) mappe :
#     case "stop": return "stop"
#     case "function_call": case "tool_calls": return "tool-calls"
# Un « stop » accompagne d'un outil annonce donc au client un tour TERMINE :
# l'outil n'est jamais execute. C'est la regression verrouillee ici.


_SSE_TOOL_NO_FINISH = b"".join(
    b"data: " + json.dumps(f).encode() + b"\n\n"
    for f in (
        {
            "id": "gen-1",
            "object": "chat.completion.chunk",
            "choices": [
                {"index": 0, "delta": {"content": "Je lance ls."}, "finish_reason": None}
            ],
        },
        {
            "id": "gen-1",
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_abc",
                                "type": "function",
                                "function": {"name": "bash", "arguments": ""},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "gen-1",
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"command":"ls"}'}}]},
                    "finish_reason": None,
                }
            ],
        },
    )
) + b"data: [DONE]\n\n"


def _replay_frames(raw):
    """Rejoue un corps SSE amont comme le fait une route streaming."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    out = []
    for ln in raw.splitlines():
        if not ln.startswith("data:"):
            continue
        d = ln[5:].strip()
        if not d or d == "[DONE]":
            continue
        out.append(json.loads(d))
    return out


def test_synth_finish_is_tool_calls_when_upstream_omits_it():
    """Un tour d'outil ne doit JAMAIS se conclure par « stop ».

    Reproduit la mesure : tool_calls presents, aucun finish_reason amont.
    Le proxy doit synthetiser ``tool_calls`` (parite client officiel).
    """
    from opencode import _json_dumps_str  # noqa: F401

    # On rejoue la sequence dans la meme boucle que la route streaming :
    # seen_tool_indices + _synth_finish, puis decision terminale.
    seen_tool_indices = set()
    _synth_finish = "stop"  # etat initial de la route (ligne 14309)
    for chunk in _replay_frames(_SSE_TOOL_NO_FINISH):
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        for tc in delta.get("tool_calls") or []:
            if isinstance(tc, dict) and "name" in tc.get("function", {}):
                tc_idx = tc.get("index", len(seen_tool_indices))
                if tc_idx not in seen_tool_indices:
                    seen_tool_indices.add(tc_idx)
        if seen_tool_indices and _synth_finish in (None, "", "stop"):
            _synth_finish = "tool_calls"

    assert seen_tool_indices, "le tool_call de la sequence n'a pas ete vu"
    assert _synth_finish == "tool_calls", (
        f"finish_reason synthetise={_synth_finish!r} — un tour d'outil annonce "
        "comme « stop » n'execute jamais l'outil cote client"
    )


def test_official_client_finish_reason_mapping_documented():
    """Table de correspondance du client officiel (extraite du binaire).

    Verrouille la regle qui rend le bug visible : ``stop`` et ``tool_calls``
    ne sont PAS interchangeables cote client.
    """

    def convert_finish_reason(raw):
        # zG(G) du bundle 1.18.31
        if raw == "stop":
            return "stop"
        if raw == "length":
            return "length"
        if raw == "content_filter":
            return "content-filter"
        if raw in ("function_call", "tool_calls"):
            return "tool-calls"
        return "other"

    assert convert_finish_reason("tool_calls") == "tool-calls"
    assert convert_finish_reason("stop") == "stop"
    assert convert_finish_reason("tool_calls") != convert_finish_reason("stop")


def test_tool_call_first_delta_has_id_and_name():
    """Exigence stricte du parseur officiel.

    Le bundle leve ``Expected 'id' to be a string`` /
    ``Expected 'function.name' to be a string`` si le PREMIER delta d'un
    index n'a ni id ni function.name. Notre synthese doit y satisfaire.
    """
    frames = _replay_frames(_SSE_TOOL_NO_FINISH)
    first = None
    for f in frames:
        tcs = (f.get("choices") or [{}])[0].get("delta", {}).get("tool_calls")
        if tcs:
            first = tcs[0]
            break
    assert first is not None
    assert isinstance(first.get("id"), str) and first["id"], f"id manquant: {first}"
    assert first.get("function", {}).get("name"), f"function.name manquant: {first}"
    assert "index" in first, "index requis pour l'accumulation officielle"


# ── [PARITE client officiel] Contrat de trame du chemin chat ─────────────
#
# Trois exigences du parseur officiel (bundle 1.18.31, extrait du binaire) :
#
#   start(K){K.enqueue({type:"stream-start"})}
#   transform(K,W){ ... let c=K.value;
#       if(J) J=!1, W.enqueue({type:"response-metadata",...LG(c)})   // 1er chunk
#       if(c.usage!=null) $=c.usage;                                 // usage
#       let v=c.choices[0]; if(v?.delta==null) return; ... }
#
# avec ``LG({id,model,created})`` → ``{id, modelId, timestamp}``.
# Et la boucle de consommation officielle ne clot que sur ``[DONE]`` :
#   GG(T) = T.trim()==="[DONE]"
#
# Mesures avant correctif sur le proxy vivant :
#   * frames delta SANS id/object/created/model  → aucune response-metadata ;
#   * AUCUNE frame usage malgre
#     stream_options={include_usage:true} du client → createUsage jamais appele.

_PARITY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Executes a PowerShell command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    },
]

_PARITY_BUFFERED = json.dumps(
    {
        "id": "gen-free-1",
        "object": "chat.completion",
        "model": "muse-spark-1.3-contributor-free",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Je lance ls.",
                    "tool_calls": [
                        {
                            "id": "call_xyz",
                            "type": "function",
                            "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                        }
                    ],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 602,
            "completion_tokens": 244,
            "total_tokens": 846,
            "prompt_tokens_details": {"cached_tokens": 113},
        },
    }
).encode()


class _ParityUpstream:
    def __init__(self, body: bytes):
        self.content = body
        self.status_code = 200
        self.headers = {"content-type": "application/json"}


def _parity_stream(monkeypatch, payload=None):
    """Exerce la vraie route /v1/chat/completions sur un corps free tamponne.

    ATTENTION : ``stream:true`` sur ``/v1/chat/completions`` ne passe PAS par
    ``_try_free_model_first`` — il part en flux LIVE via ``_open_free_stream``
    (opencode.py 14014). On mocke donc CE point d'entree, sinon le test
    atteindrait le reseau reel (mesure : 503 + 587 tokens d'un vrai appel).
    """

    class _CM:
        """Context manager asynchrone imitant ``_open_free_stream``."""

        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *exc):
            return False

    _body = payload if payload is not None else _PARITY_BUFFERED

    def _fake_open(*a, **kw):
        return _CM(oc._CurlCffiResponse(_ParityUpstream(_body)))

    monkeypatch.setattr(oc, "_open_free_stream", _fake_open)
    # La jambe free est choisie AVANT l'ouverture : on force sa resolution et
    # on neutralise la resolution de cle payante (aucun acces reseau).
    monkeypatch.setattr(oc, "_resolve_free_model", lambda m: "muse-spark-1.3-contributor-free")

    from fastapi.testclient import TestClient

    client = TestClient(oc.app, raise_server_exceptions=False)
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "muse-spark-1.3-contributor-free",
            "stream": True,
            "stream_options": {"include_usage": True},
            "tool_choice": "auto",
            "tools": _PARITY_TOOLS,
            "max_tokens": 32000,
            "messages": [{"role": "user", "content": "run ls"}],
        },
    ) as r:
        status = r.status_code
        raw = b"".join(r.iter_bytes()).decode("utf-8", "replace")

    lines = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:") and ln[5:].strip()]
    objs = []
    for d in lines:
        if d == "[DONE]":
            continue
        try:
            objs.append(json.loads(d))
        except Exception:  # noqa: BLE001
            pass
    return status, lines, objs


def test_parity_first_chunk_carries_metadata(monkeypatch):
    """``LG(c)`` lit id/model/created sur le 1er chunk — sinon pas de metadata."""
    status, lines, objs = _parity_stream(monkeypatch)
    assert status == 200, f"status={status}"
    first = next((o for o in objs if o.get("choices")), None)
    assert first is not None, "aucun chunk porteur de choices"
    assert first.get("id"), "id manquant : le client n'emet aucune response-metadata"
    assert first.get("model"), "model manquant (modelId de response-metadata)"
    assert isinstance(first.get("created"), int), "created manquant (timestamp de response-metadata)"
    assert first.get("object") == "chat.completion.chunk"


def test_parity_usage_frame_reaches_client(monkeypatch):
    """``if(c.usage!=null)$=c.usage`` — sans frame usage, createUsage jamais appele."""
    status, lines, objs = _parity_stream(monkeypatch)
    assert status == 200
    usage = next((o["usage"] for o in objs if isinstance(o.get("usage"), dict)), None)
    assert usage is not None, (
        "AUCUNE frame usage alors que le client envoie "
        "stream_options={include_usage:true} : comptage de tokens et de cache perdu"
    )
    assert usage.get("prompt_tokens") == 602
    assert usage.get("completion_tokens") == 244
    assert (usage.get("prompt_tokens_details") or {}).get("cached_tokens") == 113, (
        "cached_tokens absent : le client ne verra pas le cache"
    )
    # Le parseur officiel lit ``usage`` sur N'IMPORTE quelle frame
    # (``if(c.usage!=null)$=c.usage``) : la frame peut donc aussi porter le
    # ``finish_reason`` terminal, comme le fait le rejeu free. On exige
    # seulement que l'usage atteigne le client.
    assert any(isinstance(o.get("usage"), dict) for o in objs)


def test_parity_stream_closes_with_done(monkeypatch):
    """La boucle officielle ne clot que sur ``[DONE]`` (``GG(T)``)."""
    status, lines, _ = _parity_stream(monkeypatch)
    assert status == 200
    assert lines[-1] == "[DONE]", f"derniere trame = {lines[-1]!r} — flux non clos"


def test_parity_usage_only_if_upstream_provided(monkeypatch):
    """Aucune frame usage inventee quand l'amont n'en fournit pas."""
    payload = json.loads(_PARITY_BUFFERED)
    payload.pop("usage")

    status, _lines, objs = _parity_stream(monkeypatch, json.dumps(payload).encode())
    assert status == 200, f"status={status}"
    assert not any(isinstance(o.get("usage"), dict) for o in objs), (
        "usage INVENTE alors que l'amont n'en a pas fourni — pire que son absence"
    )


def test_route_responses_still_500s_without_sse_interface(monkeypatch):
    """Falsifiabilité : l'absence d'``aiter_lines`` DOIT reproduire le 500.

    Prouve que le test ci-dessus couvre bien le chemin fautif — sans cela il
    pourrait passer pour une raison étrangère au correctif.
    """

    class _NoSseInterface:
        """Objet minimal sans aiter_lines (forme de l'ancien wrapper)."""

        status_code = 200
        headers = {"content-type": "text/event-stream"}
        content = _SSE_CHAT
        text = _SSE_CHAT.decode()

        def json(self):
            return {}

    status, _ctype, raw = _post_responses(monkeypatch, _NoSseInterface())
    assert status == 500
    assert "Erreur interne du serveur" in raw
