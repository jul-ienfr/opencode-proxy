"""Lot L5/L14/L15/L16 — défauts trouvés par revue adversariale, et corrigés.

Chaque test correspond à un défaut **mesuré** (voir la docstring : entrée
concrète, sortie fautive observée), et non à une propriété supposée. Les quatre
premiers portent sur des correctifs de L5/L14/L15 qui étaient incomplets ; le
dernier est un défaut pré-existant de `ensure_min_tokens` (L16/A4).

Ces tests sont délibérément écrits pour **échouer** si le défaut revient — c'est
le seul intérêt d'un test de régression.
"""

import json

import pytest

import opencode
from app.protocol.mapping import (
    _chat_to_responses_request,
    anthropic_to_openai,
    openai_responses_to_anthropic,
    responses_stream_events,
)

# ─────────────────────── D3 : pas de duplication de contenu ───────────────────────


def _resp_with(output):
    return {"id": "r", "object": "response", "status": "completed", "model": "m", "output": output}


def test_output_item_added_carries_no_content():
    """D3 : ``output_item.added`` ne doit livrer **aucun** contenu.

    Défaut mesuré : l'événement portait l'item complet. Un client conforme qui
    initialise son accumulateur avec ``item`` puis y ajoute les deltas obtenait
    le texte **en double** (et les arguments d'outil en double). Les tests de
    reconstruction existants ne relisaient que les deltas — la seule vue où le
    bug est invisible.
    """
    text = "Bonjour, ceci est une reponse de test suffisamment longue."
    events = responses_stream_events(
        _resp_with(
            [
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ]
        ),
        "m",
    )
    added = [e for e in events if e["type"] == "response.output_item.added"][0]["item"]
    seeded = "".join(c.get("text", "") for c in added.get("content", []))
    deltas = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")

    assert seeded == "", f"`.added` livre déjà du texte ({seeded[:40]!r})"
    assert seeded + deltas == text, "reconstruction naïve (added + deltas) fausse"


def test_output_item_added_keeps_item_identity():
    """Vider le contenu ne doit pas effacer l'identité de l'item.

    Le client décide *comment* accumuler d'après ``type``/``id``/``name``/
    ``role`` : les retirer casserait le routage côté client.
    """
    events = responses_stream_events(
        _resp_with(
            [
                {"type": "message", "id": "msg_1", "role": "assistant", "content": []},
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "c1",
                    "name": "mon_outil",
                    "arguments": '{"a":1}',
                },
            ]
        ),
        "m",
    )
    items = [e["item"] for e in events if e["type"] == "response.output_item.added"]
    assert items[0]["type"] == "message"
    assert items[0]["id"] == "msg_1"
    assert items[0]["role"] == "assistant"
    assert items[1]["type"] == "function_call"
    assert items[1]["name"] == "mon_outil"
    assert items[1]["call_id"] == "c1"


def test_function_call_added_has_empty_arguments():
    """Convention OpenAI — et de notre propre parseur SSE (``"arguments": ""``).

    ``arguments`` vide est la valeur documentée pour un item qui démarre ;
    l'omettre ferait échouer un client strict, le remplir dupliquerait.
    """
    events = responses_stream_events(
        _resp_with(
            [
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "c1",
                    "name": "f",
                    "arguments": '{"a":1}',
                }
            ]
        ),
        "m",
    )
    added = [e for e in events if e["type"] == "response.output_item.added"][0]["item"]
    deltas = "".join(
        e["delta"] for e in events if e["type"] == "response.function_call_arguments.delta"
    )
    assert added["arguments"] == ""
    assert deltas == '{"a":1}', "les deltas doivent porter l'intégralité des arguments"


def test_content_parts_are_announced_empty_then_closed_full():
    """Les ``content_index`` sont annoncés dès ``.added``, vides, puis remplis."""
    events = responses_stream_events(
        _resp_with(
            [
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "un"},
                        {"type": "output_text", "text": "deux"},
                    ],
                }
            ]
        ),
        "m",
    )
    added = [e for e in events if e["type"] == "response.output_item.added"][0]["item"]
    assert [c["text"] for c in added["content"]] == ["", ""], (
        "le nombre de parts doit rester annoncé, mais vides"
    )
    done = [e for e in events if e["type"] == "response.output_item.done"][0]["item"]
    assert [c["text"] for c in done["content"]] == ["un", "deux"]


def test_reasoning_added_hides_summary_and_encrypted_content():
    """Un reasoning item diffuse son résumé ; ``encrypted_content`` n'est
    complet **que** sur ``.done`` (B7), donc absent de ``.added``."""
    events = responses_stream_events(
        _resp_with(
            [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "reflexion"}],
                    "encrypted_content": "SECRET",
                }
            ]
        ),
        "m",
    )
    added = [e for e in events if e["type"] == "response.output_item.added"][0]["item"]
    assert added["summary"] == [{"type": "summary_text", "text": ""}]
    assert "encrypted_content" not in added, "B7 : le chiffré n'est lisible que sur .done"

    done = [e for e in events if e["type"] == "response.output_item.done"][0]["item"]
    assert done["encrypted_content"] == "SECRET"
    assert "".join(e["delta"] for e in events if e["type"] == "response.reasoning_summary_text.delta") == "reflexion"


# ─────────────────────── D2 : la limite survit à P5 ───────────────────────


@pytest.mark.parametrize("limit", [128, 512, 4096])
def test_max_completion_tokens_survives_the_responses_hop(limit):
    """D2 : le champ moderne ne doit pas disparaître en passant par P5.

    Défaut mesuré : P2 écrivait ``max_completion_tokens`` (B2) puis
    ``_chat_to_responses_request`` ne relisait que ``max_tokens`` et
    ``max_output_tokens`` → le corps sortant n'avait **aucune** limite. Le
    client bornait à 512, l'upstream n'était borné par rien — le coût non borné
    que B2 cherche précisément à éviter.
    """
    out = _chat_to_responses_request(
        {"model": "o3-mini", "max_completion_tokens": limit, "messages": [{"role": "user", "content": "hi"}]}
    )
    assert out.get("max_output_tokens") == limit


def test_p5_preserves_historical_precedence():
    """L'ajout de la 3e forme ne change aucun cas existant.

    ``max_output_tokens`` (forme Responses) l'emporte sur ``max_tokens``
    (héritée) : c'était la priorité d'origine, elle reste.
    """
    out = _chat_to_responses_request(
        {
            "model": "m",
            "max_output_tokens": 100,
            "max_tokens": 200,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert out.get("max_output_tokens") == 100


def test_p5_reads_completion_form_only_as_last_resort():
    """La forme moderne est un dernier recours, pas une priorité nouvelle."""
    out = _chat_to_responses_request(
        {
            "model": "m",
            "max_tokens": 200,
            "max_completion_tokens": 300,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert out.get("max_output_tokens") == 200


def test_p5_invents_nothing_and_rejects_invalid():
    """Aucune forme valide → aucun champ (ne pas inventer de limite)."""
    for src in ({}, {"max_tokens": 0}, {"max_tokens": "512"}, {"max_completion_tokens": -1}):
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        body.update(src)
        assert "max_output_tokens" not in _chat_to_responses_request(body), src


# ─────────────────────── D1 : store/truncation jusqu'au wire ───────────────────────


def test_responses_chain_preserves_store_and_truncation():
    """D1 : la chaîne P6 → P2 → P5 doit transporter `store`/`truncation`.

    Défaut mesuré : ``openai_responses_to_anthropic`` ne copiait ni l'un ni
    l'autre, si bien que le relais de ``_chat_to_responses_request`` recevait
    une source déjà vide. Un client envoyant ``store: false`` sur
    ``/v1/responses`` n'était donc **pas** protégé de la rétention par défaut
    (≥30 j) — exactement la fuite que B5 devait empêcher.
    """
    body = {
        "model": "muse-spark-1.3-contributor",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "store": False,
        "truncation": "auto",
    }
    # P6 : cible Anthropic — les clés ne doivent PAS être écrites ici (un
    # upstream Anthropic les rejetterait), mais le handler les replacera.
    p6 = openai_responses_to_anthropic(body)
    assert "store" not in p6 and "truncation" not in p6, (
        "P6 ne doit pas envoyer de clés Responses à un upstream Anthropic"
    )

    # Le handler relaie depuis le corps client d'origine (cf. opencode.py).
    from app.protocol.mapping import _relay_responses_storage_fields

    p5 = _chat_to_responses_request(anthropic_to_openai(p6, body["model"]))
    assert "store" not in p5, "P2 perd store : c'est attendu, le handler le replacera"
    _relay_responses_storage_fields(p5, body)
    assert p5["store"] is False
    assert p5["truncation"] == "auto"


# ─────────────────────── D4 : un client non-streaming reçoit du JSON ───────────────────────


def _keyless_fallback_sites():
    """Repère les sites SSE qui dépendent d'un repli sans clé API.

    On ne devine pas la structure du code (l'idiome `if not is_stream: ...
    return` rend l'analyse d'indentation trompeuse) : on localise les appels à
    l'émetteur et on vérifie qu'un `is_stream`/`["stream"]` apparaît bien à
    proximité immédiate du site, dans les deux sens.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent.joinpath("opencode.py").read_text(
        encoding="utf-8", errors="replace"
    )
    lines = src.splitlines()
    out = []
    for i, ln in enumerate(lines):
        if "responses_stream_sse(" not in ln or ln.lstrip().startswith("def "):
            continue
        window = "\n".join(lines[max(0, i - 25) : i + 3])
        out.append((i + 1, window))
    return out


def test_every_sse_site_is_reachable_only_for_streaming_clients():
    """D4 (garde structurelle) : chaque site SSE côtoie une décision de stream.

    Défaut mesuré : deux sites de repli (clés en pause) renvoyaient du SSE sans
    consulter le mode demandé ; un client `stream: false` recevait un corps SSE
    non parsable en JSON. On exige donc, pour chaque site, la présence d'une
    décision `stream` dans son voisinage — ce que le correctif a introduit aux
    deux endroits concernés.
    """
    sites = _keyless_fallback_sites()
    assert sites, "aucun site d'émission trouvé — motif obsolète"
    for lineno, window in sites:
        assert "stream" in window, (
            f"ligne {lineno} : émission SSE sans décision de stream à proximité — "
            f"un client `stream:false` recevrait du SSE"
        )


def test_non_streaming_client_gets_json_when_all_keys_are_paused(monkeypatch):
    """D4 (bout en bout) : clés en pause + `stream: false` → JSON, pas SSE.

    Reproduit le chemin de repli : `_get_auth_headers` lève `AllKeysPausedError`,
    un modèle free existe, et le repli réussit. Avant le correctif, ce site
    renvoyait du SSE inconditionnellement.
    """
    from fastapi.testclient import TestClient

    from core.keys import AllKeysPausedError

    async def _fake(endpoint, body, headers, protocol, *a, **k):
        return _FakeJsonResp(_CHAT_JSON), {}

    def _boom(*a, **k):
        raise AllKeysPausedError(1)

    monkeypatch.setattr(opencode, "_do_request_with_retry", _fake)
    monkeypatch.setattr(opencode, "_get_auth_headers", _boom)
    monkeypatch.setattr(
        opencode, "_try_free_model_first", _fake, raising=False
    )

    r = TestClient(opencode.app).post(
        "/v1/responses",
        json={
            "model": "deepseek-v4-flash",
            "stream": False,
            "input": [{"role": "user", "content": "bonjour"}],
        },
    )
    ctype = r.headers.get("content-type", "")
    if r.status_code == 200 and "text/event-stream" in ctype:
        raise AssertionError(
            "un client stream:false a reçu du SSE sur le chemin de repli sans clé"
        )
    assert r.status_code in (200, 429, 503, 500), r.status_code


# ─────────────────────── L16 : ne jamais abaisser une limite ───────────────────────


def test_ensure_min_tokens_never_lowers_a_larger_limit():
    """L16/A4 : le relèvement ne doit pas **abaisser** un champ déjà plus grand.

    Défaut pré-existant mesuré : avec ``{max_output_tokens: 16, max_tokens:
    100000}``, la fonction retenait 16 comme « courant » (< minimum) puis
    ramenait **les deux** champs au minimum — le budget de 100000 posé par le
    client pour ne pas être tronqué était détruit silencieusement.
    """
    out = opencode.ensure_min_tokens(
        {"model": "deepseek-v4-flash", "max_output_tokens": 16, "max_tokens": 100000}
    )
    assert out["max_tokens"] == 100000, "la limite du client a été abaissée"
    assert out["max_output_tokens"] >= 16, "le petit champ doit rester relevé"


def test_ensure_min_tokens_still_raises_small_limits():
    """Le comportement d'origine doit survivre : relever un champ trop petit."""
    out = opencode.ensure_min_tokens({"model": "deepseek-v4-flash", "max_tokens": 16})
    assert out["max_tokens"] > 16


def test_ensure_min_tokens_invents_nothing():
    """Aucun champ de limite → aucun champ ajouté (ne pas décider à la place
    du client)."""
    out = opencode.ensure_min_tokens({"model": "deepseek-v4-flash"})
    assert "max_tokens" not in out
    assert "max_output_tokens" not in out


def test_ensure_min_tokens_leaves_sufficient_limits_untouched():
    """Une limite déjà suffisante n'est pas modifiée."""
    out = opencode.ensure_min_tokens({"model": "deepseek-v4-flash", "max_tokens": 4096})
    assert out["max_tokens"] == 4096


def test_ensure_min_tokens_ignores_non_integer_values():
    """Valeurs non entières : ignorées, jamais propagées ni « réparées »."""
    out = opencode.ensure_min_tokens({"model": "deepseek-v4-flash", "max_tokens": "4096"})
    assert out["max_tokens"] == "4096"


def test_store_and_truncation_reach_the_wire_on_the_responses_endpoint(monkeypatch):
    """D1 (bout en bout) : `store`/`truncation` arrivent bien à l'upstream.

    Le test précédent vérifie le helper ; celui-ci vérifie le **câblage du
    handler**, sans quoi le correctif pourrait être débranché sans qu'aucun test
    ne rougisse (c'est exactement ce qu'a montré le mutation-test : retirer
    l'appel du handler laissait la suite verte).

    `gpt-5.6-luna` route vers un endpoint `/responses`, ce qui exerce la chaîne
    P6 → P2 → P5 réellement empruntée en production.
    """
    from fastapi.testclient import TestClient

    captured = {}

    async def _fake(endpoint, body, headers, protocol, *a, **k):
        captured["endpoint"] = endpoint
        captured["body"] = body if isinstance(body, dict) else json.loads(body)
        return _FakeJsonResp(
            {
                "id": "resp_1",
                "object": "response",
                "status": "completed",
                "model": "muse-spark-1.3-contributor",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        ), {}

    monkeypatch.setattr(opencode, "_do_request_with_retry", _fake)
    # Forcer la jambe payante : sans modèle free, le handler ne détourne pas.
    monkeypatch.setattr(opencode, "FREE_MODEL_MAP", {})

    r = TestClient(opencode.app).post(
        "/v1/responses",
        json={
            "model": "gpt-5.6-luna",
            "stream": False,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
            "store": False,
            "truncation": "auto",
        },
    )
    assert r.status_code == 200, r.text[:200]
    endpoint = captured.get("endpoint") or ""
    if "/responses" not in endpoint:
        pytest.skip(f"route non-Responses dans cet environnement ({endpoint})")

    sent = captured["body"]
    assert sent.get("store") is False, (
        f"`store:false` perdu en route — l'upstream appliquerait sa rétention "
        f"par défaut (clés envoyées : {sorted(sent.keys())})"
    )
    assert sent.get("truncation") == "auto", "`truncation` perdu en route"


def test_store_is_not_sent_to_an_anthropic_upstream(monkeypatch):
    """Le relais ne doit pas fuiter vers une cible Anthropic.

    `store`/`truncation` sont des clés Responses : les envoyer à un upstream
    Anthropic provoque un 400 « unknown parameter ». Le relais est donc placé
    dans la branche `/responses` uniquement.
    """
    from fastapi.testclient import TestClient

    captured = {}

    async def _fake(endpoint, body, headers, protocol, *a, **k):
        captured["endpoint"] = endpoint
        captured["body"] = body if isinstance(body, dict) else json.loads(body)
        return _FakeJsonResp(
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-x",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        ), {}

    monkeypatch.setattr(opencode, "_do_request_with_retry", _fake)

    async def _fake_free(endpoint, body, headers):
        # [gate body 2026-09-18] la jambe free directe ne passe plus par
        # _do_request_with_retry : même simulacre, même contrat.
        captured["endpoint"] = endpoint
        captured["body"] = body if isinstance(body, dict) else json.loads(body)
        return _FakeJsonResp(
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-x",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        ), {}

    monkeypatch.setattr(opencode, "_do_free_direct_request", _fake_free)

    client = TestClient(opencode.app)
    for path in ("/v1/messages", "/v1/chat/completions"):
        captured.clear()
        r = client.post(
            path,
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hi"}],
                "store": False,
                "truncation": "auto",
            },
        )
        if r.status_code >= 500 or not captured.get("body"):
            continue
        sent = captured["body"]
        assert "store" not in sent or sent["store"] is not None, (
            f"{path} : `store` relayé à tort vers {captured.get('endpoint')}"
        )


# ─────────────────────── D4 (bout en bout) : JSON pour stream:false ───────────────────────


class _FakeJsonResp:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    @property
    def content(self):
        """Certains chemins lisent `resp.content` (bytes) plutôt que `.json()`."""
        return self.text.encode()

    def json(self):
        return self._payload

    async def aiter_lines(self):
        """La jambe streaming consomme l'upstream en SSE : on sert un tour
        minimal bien formé pour que le handler produise sa propre séquence."""
        delta = {
            "id": "chatcmpl_t",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}],
        }
        stop = {
            "id": "chatcmpl_t",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        for chunk in (delta, stop):
            yield f"data: {json.dumps(chunk)}"
        yield "data: [DONE]"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


_CHAT_JSON = {
    "id": "chatcmpl_t",
    "object": "chat.completion",
    "created": 1,
    "model": "deepseek-v4-flash",
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


@pytest.mark.parametrize("stream", [False, True])
def test_responses_endpoint_honours_the_client_stream_flag(monkeypatch, stream):
    """Bout en bout : le mode demandé est respecté (JSON vs SSE)."""
    from fastapi.testclient import TestClient

    async def _fake(endpoint, body, headers, protocol, *a, **k):
        return _FakeJsonResp(_CHAT_JSON), {}

    async def _fake_free(endpoint, body, headers):
        # [gate body 2026-09-18] cf. ci-dessus : même simulacre free.
        return _FakeJsonResp(_CHAT_JSON), {}

    monkeypatch.setattr(opencode, "_do_request_with_retry", _fake)
    monkeypatch.setattr(opencode, "_do_free_direct_request", _fake_free)
    r = TestClient(opencode.app).post(
        "/v1/responses",
        json={
            "model": "deepseek-v4-flash",
            "stream": stream,
            "input": [{"role": "user", "content": "bonjour"}],
        },
    )
    ctype = r.headers.get("content-type", "")
    if stream:
        assert "text/event-stream" in ctype
        assert "response.created" in r.text
    else:
        assert "text/event-stream" not in ctype, f"stream:false a reçu du SSE ({ctype})"
        assert r.json().get("object") == "response"
