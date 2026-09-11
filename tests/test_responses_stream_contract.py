"""[PLAN_AUDIT_CONVERSIONS Lot L5] Contrat de streaming `/v1/responses` — A11/A21.

**A21 (confirmé par la spec).** L'ensemble minimal d'événements consommé par un
client Responses est ``response.created`` → ``response.output_text.delta``* →
``response.completed`` (+ ``error``). Nous n'émettions **que**
``response.completed`` — sans même le ``response.created`` initial. Conséquence
réelle : un client qui attend ``response.created`` avant d'afficher reste bloqué
jusqu'à la fin de la génération, puis tout apparaît d'un bloc. Le « streaming »
n'était pas du streaming.

Ces tests verrouillent l'ordre contractuel et les **payloads exacts** (B7), qui
sont la partie la plus facile à casser par inadvertance :

* ``output_text.delta`` porte ``{content_index, delta, item_id, logprobs[],
  output_index, sequence_number}`` ;
* ``function_call_arguments.delta`` n'a **ni** ``content_index`` **ni** ``name`` ;
* ``reasoning_summary_text.delta`` utilise **``summary_index``** ;
* chaque événement porte un ``sequence_number`` strictement croissant.
"""

import json

import pytest

from app.protocol.mapping import (
    ResponsesStreamEmitter,
    responses_stream_events,
    responses_stream_sse,
)

# ─────────────────────── fixtures ───────────────────────


def _text_response(text="Bonjour le monde", **extra):
    resp = {
        "id": "resp_test",
        "object": "response",
        "status": "completed",
        "model": "test-model",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
    }
    resp.update(extra)
    return resp


def _types(events):
    return [e["type"] for e in events]


def _deltas(events, etype):
    return [e.get("delta", "") for e in events if e["type"] == etype]


# ─────────────────────── A21 : l'ensemble minimal ───────────────────────


def test_response_created_is_emitted_first():
    """A21 — LE point : sans ``response.created``, un client qui l'attend avant
    d'afficher reste bloqué jusqu'à la fin de la génération."""
    events = responses_stream_events(_text_response(), "test-model")
    assert events[0]["type"] == "response.created"
    assert events[0]["sequence_number"] == 0


def test_minimal_required_set_is_present():
    """A21 : les trois types de l'ensemble minimal documenté sont là."""
    types = _types(responses_stream_events(_text_response(), "test-model"))
    assert "response.created" in types
    assert "response.output_text.delta" in types
    assert "response.completed" in types


def test_response_completed_is_last_and_terminal():
    """Le terminal porte l'usage ; rien ne suit."""
    events = responses_stream_events(_text_response(), "test-model")
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["usage"]["output_tokens"] == 20


def test_created_precedes_all_deltas():
    """Ordre contractuel : ``created`` avant tout delta."""
    events = responses_stream_events(_text_response("un texte assez long"), "test-model")
    created_at = _types(events).index("response.created")
    first_delta = _types(events).index("response.output_text.delta")
    assert created_at < first_delta


def test_completed_carries_usage():
    """L'usage final voyage sur ``response.completed``, pas ailleurs."""
    events = responses_stream_events(_text_response(), "test-model")
    completed = [e for e in events if e["type"] == "response.completed"]
    assert len(completed) == 1
    assert completed[0]["response"]["usage"]["input_tokens"] == 10


def test_before_fix_we_emitted_only_completed():
    """Régression : on n'émet plus un flux réduit au seul terminal.

    C'est la formulation exacte du défaut A11/A21 — un flux d'un seul événement
    n'est pas un flux.
    """
    events = responses_stream_events(_text_response(), "test-model")
    assert len(events) > 2, "flux réduit au terminal : le faux streaming est revenu"
    assert len(_deltas(events, "response.output_text.delta")) >= 1


# ─────────────────────── progression réelle ───────────────────────


def test_long_text_is_split_into_several_deltas():
    """A11 : le client doit voir une progression, pas un bloc unique."""
    long_text = "x" * 500
    events = responses_stream_events(_text_response(long_text), "test-model")
    deltas = _deltas(events, "response.output_text.delta")
    assert len(deltas) > 1, "texte long livré en un seul delta : pas de progression"


def test_concatenated_deltas_reconstruct_the_text():
    """Le découpage ne doit pas altérer le contenu."""
    text = "Voici une réponse. " * 20
    events = responses_stream_events(_text_response(text), "test-model")
    assert "".join(_deltas(events, "response.output_text.delta")) == text


def test_short_text_still_emits_a_delta():
    """Même un texte court passe par un delta (jamais seulement l'item final)."""
    events = responses_stream_events(_text_response("ok"), "test-model")
    assert _deltas(events, "response.output_text.delta") == ["ok"]


def test_unicode_is_not_mangled_by_chunking():
    """Le découpage travaille sur des caractères, pas des octets : un texte
    accentué ou emoji ne doit pas produire de mojibake."""
    text = "Réponse accentuée avec des emojis 🎉🔒 et du 中文" * 5
    events = responses_stream_events(_text_response(text), "test-model")
    assert "".join(_deltas(events, "response.output_text.delta")) == text


# ─────────────────────── B7 : payloads exacts ───────────────────────


def test_output_text_delta_has_documented_fields():
    """B7 : ``output_text.delta`` porte les champs documentés, dont ``logprobs``."""
    events = responses_stream_events(_text_response(), "test-model")
    delta = next(e for e in events if e["type"] == "response.output_text.delta")
    assert "content_index" in delta
    assert "delta" in delta
    assert "item_id" in delta
    assert "output_index" in delta
    assert delta["logprobs"] == [], "logprobs absent ou None : un client strict casse"


def test_function_call_arguments_delta_has_no_content_index_nor_name():
    """B7 : ce payload n'a NI ``content_index`` NI ``name``."""
    resp = _text_response()
    resp["output"].append(
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": '{"city": "Paris"}',
        }
    )
    events = responses_stream_events(resp, "test-model")
    fdelta = next(e for e in events if e["type"] == "response.function_call_arguments.delta")
    assert "content_index" not in fdelta, "content_index ne doit PAS être dans ce payload"
    assert "name" not in fdelta, "name ne doit PAS être dans ce payload"


def test_reasoning_delta_uses_summary_index():
    """B7 : le raisonnement utilise ``summary_index``, pas ``content_index``.

    Émettre ``content_index`` ferait ignorer le fragment par un client conforme.
    """
    resp = _text_response()
    resp["output"].insert(
        0,
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": "je réfléchis"}],
        },
    )
    events = responses_stream_events(resp, "test-model")
    rdelta = next(e for e in events if e["type"] == "response.reasoning_summary_text.delta")
    assert "summary_index" in rdelta
    assert "content_index" not in rdelta


def test_every_event_has_a_sequence_number():
    """B7 : CHAQUE événement porte ``sequence_number``."""
    resp = _text_response()
    resp["output"].append(
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "c1",
            "name": "f",
            "arguments": "{}",
        }
    )
    events = responses_stream_events(resp, "test-model")
    for e in events:
        assert "sequence_number" in e, f"{e['type']} sans sequence_number"


def test_sequence_numbers_are_strictly_increasing_from_zero():
    """Un trou ou un doublon ferait réinitialiser l'état d'un client."""
    events = responses_stream_events(_text_response("un texte un peu long ici"), "test-model")
    seqs = [e["sequence_number"] for e in events]
    assert seqs == list(range(len(events)))


# ─────────────────────── cycle de vie des items ───────────────────────


def test_each_output_item_is_opened_and_closed():
    """``output_item.added`` et ``output_item.done`` s'apparient par index."""
    resp = _text_response()
    resp["output"].append(
        {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "f", "arguments": "{}"}
    )
    events = responses_stream_events(resp, "test-model")
    added = [e["output_index"] for e in events if e["type"] == "response.output_item.added"]
    done = [e["output_index"] for e in events if e["type"] == "response.output_item.done"]
    assert added == done == [0, 1]


def test_each_delta_lies_between_its_item_added_and_done():
    """Un delta orphelin (hors de son item) n'est pas rattachable par le client."""
    events = responses_stream_events(_text_response("un texte assez long pour découper"), "test-model")
    types = _types(events)
    added = types.index("response.output_item.added")
    done = types.index("response.output_item.done")
    for idx, t in enumerate(types):
        if t == "response.output_text.delta":
            assert added < idx < done, "delta émis hors de la fenêtre de son item"


def test_reasoning_encrypted_content_completes_only_at_item_done():
    """B7 : ``encrypted_content`` n'est complet qu'à ``output_item.done``.
    On vérifie que l'item transporté par ``.done`` porte bien le champ."""
    resp = _text_response()
    resp["output"].insert(
        0,
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": "réflexion"}],
            "encrypted_content": "blob_complet",
        },
    )
    events = responses_stream_events(resp, "test-model")
    done_items = [
        e["item"]
        for e in events
        if e["type"] == "response.output_item.done" and e["item"].get("type") == "reasoning"
    ]
    assert done_items, "aucun output_item.done pour le raisonnement"
    assert done_items[0].get("encrypted_content") == "blob_complet"


def test_content_part_is_opened_and_closed_around_deltas():
    """Le texte est encadré par ``content_part.added`` / ``content_part.done``."""
    events = responses_stream_events(_text_response("un texte assez long à découper"), "test-model")
    types = _types(events)
    assert "response.content_part.added" in types
    assert "response.content_part.done" in types
    assert types.index("response.content_part.added") < types.index("response.output_text.delta")


def test_unknown_item_type_is_transported_not_dropped():
    """Un type d'item inconnu est livré en un bloc plutôt que perdu."""
    resp = _text_response()
    resp["output"].append({"type": "web_search_call", "id": "ws_1", "status": "completed"})
    events = responses_stream_events(resp, "test-model")
    done = [e for e in events if e["type"] == "response.output_item.done"]
    assert any(e["item"].get("type") == "web_search_call" for e in done)


# ─────────────────────── robustesse ───────────────────────


def test_empty_output_still_produces_a_valid_stream():
    """Une réponse sans contenu reste un flux valide (created → completed)."""
    events = responses_stream_events(_text_response(), "test-model")
    resp = {"id": "r", "object": "response", "status": "completed", "model": "m", "output": []}
    events = responses_stream_events(resp, "m")
    assert events[0]["type"] == "response.created"
    assert events[-1]["type"] == "response.completed"


def test_malformed_output_items_do_not_raise():
    """``output`` vient d'une conversion : on ne lève jamais."""
    resp = {
        "id": "r",
        "object": "response",
        "status": "completed",
        "model": "m",
        "output": [None, "pas un dict", 42, {"type": "message"}],
    }
    events = responses_stream_events(resp, "m")
    assert events[-1]["type"] == "response.completed"


def test_missing_usage_does_not_raise():
    resp = {"id": "r", "object": "response", "status": "completed", "model": "m", "output": []}
    events = responses_stream_events(resp, "m")
    assert events[-1]["response"].get("usage") is None or isinstance(
        events[-1]["response"].get("usage"), dict
    )


# ─────────────────────── sérialisation SSE ───────────────────────


def test_sse_frames_are_parseable_data_lines():
    """Chaque événement sort en ``data: <json>`` lisible par un client SSE."""
    events = responses_stream_events(_text_response(), "test-model")
    body = responses_stream_sse(events).decode()
    frames = [ln[6:] for ln in body.splitlines() if ln.startswith("data: ") and ln[6:] != "[DONE]"]
    assert len(frames) == len(events)
    for frame in frames:
        parsed = json.loads(frame)
        assert "type" in parsed and "sequence_number" in parsed


def test_sse_body_terminates_with_done_sentinel():
    """Nos clients existants utilisent ``[DONE]`` comme sentinelle de fin."""
    body = responses_stream_sse(responses_stream_events(_text_response(), "test-model"))
    assert body.endswith(b"data: [DONE]\n\n")


def test_stream_ids_are_scoped_per_emitter():
    """Deux streams ne partagent ni ``response_id`` ni compteur (état par stream)."""
    a = ResponsesStreamEmitter("m")
    b = ResponsesStreamEmitter("m")
    assert a.response_id != b.response_id
    assert a.created()["sequence_number"] == 0
    assert b.created()["sequence_number"] == 0
    assert a.created()["sequence_number"] == 1


def test_error_terminal_is_coherent():
    """L5 : un stream avorté garde un **terminal cohérent** — un client sans
    événement terminal laisse sa connexion et son UI bloquées."""
    em = ResponsesStreamEmitter("m")
    em.created()
    failed = em.failed(message="amont coupé")
    assert failed["type"] == "response.failed"
    assert failed["response"]["status"] == "failed"
    assert failed["response"]["error"]["message"] == "amont coupé"


# ─────────────────────── intégration conversions ───────────────────────


def test_anthropic_to_responses_output_is_streamable():
    """Le chemin réel : une réponse Anthropic convertie produit un flux conforme."""
    from app.protocol.mapping import anthropic_to_openai_responses

    anthro = {
        "id": "msg_1",
        "model": "m",
        "content": [
            {"type": "thinking", "thinking": "je réfléchis", "signature": "sig"},
            {"type": "text", "text": "Voici la réponse finale."},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 15},
    }
    oai = anthropic_to_openai_responses(anthro, "m")
    events = responses_stream_events(oai, "m")
    types = _types(events)
    assert types[0] == "response.created"
    assert "response.reasoning_summary_text.delta" in types
    assert "response.output_text.delta" in types
    assert types[-1] == "response.completed"
    assert "".join(_deltas(events, "response.output_text.delta")) == "Voici la réponse finale."


def test_chat_to_responses_output_is_streamable():
    """Second chemin réel : Chat → Responses, avec appel d'outil."""
    from app.protocol.mapping import openai_chat_to_responses

    chat = {
        "id": "chatcmpl_1",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "Je vais chercher la météo.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 8, "completion_tokens": 12},
    }
    oai = openai_chat_to_responses(chat, "m")
    events = responses_stream_events(oai, "m")
    types = _types(events)
    assert types[0] == "response.created"
    assert "response.function_call_arguments.delta" in types
    assert types[-1] == "response.completed"


def test_tool_only_response_has_no_empty_text_delta():
    """Une réponse purement outil ne doit pas émettre de delta texte vide (un
    client afficherait une bulle vide)."""
    resp = {
        "id": "r",
        "object": "response",
        "status": "completed",
        "model": "m",
        "output": [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "c1",
                "name": "f",
                "arguments": "{}",
            }
        ],
    }
    events = responses_stream_events(resp, "m")
    assert _deltas(events, "response.output_text.delta") == []


@pytest.mark.parametrize(
    "name,text",
    [
        ("accents", "é" * 200),
        ("emoji hors BMP", "🦄" * 100),
        ("emoji mixes", "a🦄b" * 60),
        ("CJK", "漢字テスト" * 50),
        ("combining", "e\u0301" * 150),
        ("ZWJ famille", "👨‍👩‍👧‍👦" * 40),
        ("math alphanumerics", "𝕳𝖊𝖑𝖑𝖔" * 40),
        ("mixte complet", "Café 🦄 漢字 👨‍👩‍👧‍👦 𝕳" * 30),
    ],
)
def test_chunking_is_lossless_for_multibyte_text(name, text):
    """Le découpage en tranches de 64 ne doit jamais couper un caractère.

    Le texte est découpé par tranches de 64 **caractères** Python (donc par
    points de code, pas par octets) : un emoji hors BMP ou une séquence ZWJ reste
    intact. Ce test verrouille cette propriété — un découpage par octets
    produirait des demi-caractères et corromprait l'affichage.
    """
    resp = {
        "id": "r",
        "object": "response",
        "status": "completed",
        "model": "m",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }
    events = responses_stream_events(resp, "m")
    got = "".join(e.get("delta", "") for e in events if e["type"] == "response.output_text.delta")
    assert got == text, f"découpage perturbateur sur {name}"

    for e in events:
        if e["type"] == "response.output_text.delta" and e["delta"]:
            d = e["delta"]
            assert not (0xD800 <= ord(d[0]) <= 0xDFFF), f"demi-paire isolée en tête: {name}"
            assert not (0xD800 <= ord(d[-1]) <= 0xDFFF), f"demi-paire isolée en fin: {name}"


@pytest.mark.parametrize(
    "name,output",
    [
        ("output vide", []),
        ("output absent", None),
        (
            "message texte vide",
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": ""}],
                }
            ],
        ),
        (
            "raisonnement summary seul",
            [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "je réfléchis"}],
                }
            ],
        ),
        (
            "raisonnement chiffré sans summary",
            [{"type": "reasoning", "id": "rs_1", "encrypted_content": "AAAA"}],
        ),
        (
            "outil seul",
            [
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "c1",
                    "name": "f",
                    "arguments": '{"a":1}',
                }
            ],
        ),
        (
            "ordre mixte raisonnement+texte+outil",
            [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "réflexion"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "voici"}],
                },
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "c1",
                    "name": "f",
                    "arguments": "{}",
                },
            ],
        ),
    ],
)
def test_stream_is_coherent_for_every_shape(name, output):
    """Toute forme de réponse produit un flux cohérent.

    Propriétés invariantes, quel que soit le contenu :

    * le dernier événement est ``response.completed`` ;
    * les ``sequence_number`` sont contigus depuis 0 (un trou/doublon ferait
      réinitialiser l'état d'un client conforme) ;
    * chaque item ouvert est refermé (sinon un item reste pendu côté client).
    """
    resp = {"id": "r", "object": "response", "status": "completed", "model": "m"}
    if output is not None:
        resp["output"] = output

    events = responses_stream_events(resp, "m")
    types = [e["type"] for e in events]

    assert types[-1] == "response.completed", f"{name}: dernier = {types[-1]}"
    assert [e["sequence_number"] for e in events] == list(range(len(events))), (
        f"{name}: sequence_number non contigus"
    )
    added = [e["output_index"] for e in events if e["type"] == "response.output_item.added"]
    done = [e["output_index"] for e in events if e["type"] == "response.output_item.done"]
    assert added == done, f"{name}: items déséquilibrés added={added} done={done}"


def test_mixed_shapes_use_the_right_event_family_per_item():
    """Chaque type d'item emploie sa propre famille d'événements.

    Un item de raisonnement n'a **pas** de ``content_part`` (il diffuse via
    ``reasoning_summary_text.delta``) ; un appel d'outil diffuse des arguments,
    pas du texte. Confondre les familles ferait ignorer les événements par un
    client conforme.
    """
    resp = {
        "id": "r",
        "object": "response",
        "status": "completed",
        "model": "m",
        "output": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "réflexion"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "voici"}],
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "c1",
                "name": "f",
                "arguments": "{}",
            },
        ],
    }
    events = responses_stream_events(resp, "m")

    def types_for(idx):
        return [e["type"] for e in events if e.get("output_index") == idx]

    assert "response.reasoning_summary_text.delta" in types_for(0)
    assert "response.content_part.added" not in types_for(0), (
        "un item de raisonnement ne doit pas ouvrir de content_part"
    )

    assert "response.output_text.delta" in types_for(1)
    assert "response.content_part.added" in types_for(1)

    assert "response.function_call_arguments.delta" in types_for(2)
    assert "response.output_text.delta" not in types_for(2), (
        "un appel d'outil ne diffuse pas de texte"
    )


def test_function_call_arguments_delta_omits_name_and_content_index():
    """Spec : ``function_call_arguments.delta`` ne porte NI ``name`` NI
    ``content_index`` (le champ n'existe pas pour cette famille)."""
    resp = {
        "id": "r",
        "object": "response",
        "status": "completed",
        "model": "m",
        "output": [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "c1",
                "name": "f",
                "arguments": '{"a":1}',
            }
        ],
    }
    deltas = [
        e for e in responses_stream_events(resp, "m") if e["type"] == "response.function_call_arguments.delta"
    ]
    assert deltas, "aucun delta d'arguments émis"
    for e in deltas:
        assert "name" not in e
        assert "content_index" not in e


def test_no_bare_terminal_site_remains_in_the_handler():
    """Garde structurelle : AUCUN site de `/v1/responses` ne ré-émet un unique
    ``response.completed`` brut.

    Les tests ci-dessus valident le constructeur ; ils ne verraient pas un site
    du handler qui **n'appelle pas** le constructeur — exactement le défaut
    d'origine (5 sites émettaient un payload terminal à la main). Ce test lit la
    source et refuse toute réapparition du motif fautif.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent.joinpath("opencode.py").read_text(
        encoding="utf-8", errors="replace"
    )
    # Motif d'origine : un payload `response.completed` fabriqué à la main pour
    # le wire SSE (et non la séquence construite par l'émetteur).
    bare = re.findall(
        r'\{\s*"type":\s*"response\.completed"\s*,\s*"response":\s*oai_resp\s*\}', src
    )
    assert not bare, (
        f"{len(bare)} site(s) ré-émettent un `response.completed` brut : "
        f"le faux streaming (A11/A21) est revenu. Utiliser "
        f"responses_stream_sse(responses_stream_events(...))."
    )


def test_handler_uses_the_emitter_for_every_streaming_return():
    """Corollaire : chaque émission SSE passe par l'émetteur.

    Un site qui construirait son corps SSE sans passer par
    ``responses_stream_events`` produirait un flux non conforme.

    NB : ce test exigeait auparavant **5** sites. Ce chiffre venait de compter un
    site qui répondait en SSE à un client `stream: false` — c'est-à-dire le
    défaut D4 (cf. `test_review_findings_d1_d4.py`). Le site corrigé renvoie
    désormais du JSON, donc il n'émet plus de SSE : la bonne assertion n'est pas
    un nombre de sites figé, mais l'**égalité** entre émissions SSE et passages
    par l'émetteur.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent.joinpath("opencode.py").read_text(
        encoding="utf-8", errors="replace"
    )
    sse_sites = src.count("responses_stream_sse(")
    emitter_calls = src.count("responses_stream_events(")
    assert sse_sites > 0, "aucun site SSE : le streaming Responses a disparu"
    assert sse_sites == emitter_calls, (
        f"{sse_sites} émission(s) SSE pour {emitter_calls} appel(s) à "
        f"responses_stream_events : un site construit son flux sans l'émetteur"
    )
