"""[PLAN_AUDIT_CONVERSIONS Lot L15] Conformité Responses — B5, B6, B8.

**B5 (confirmé).** ``store`` et ``truncation`` n'étaient relayés nulle part
(0 occurrence). Les deux ont un défaut upstream qui surprend : ``store=true``
(rétention ≥30 j) et ``truncation="disabled"`` (400 en dépassement, pas de
troncature silencieuse). Un client envoyant ``store: false`` pour raison de
confidentialité voyait sa consigne **ignorée sans trace**.

**B6 (déjà conforme).** ``stream_options.include_usage`` est requis pour recevoir
le chunk d'usage final : présent sur les deux voies aller, verrouillé ici.

**B8 (confirmé).** Les shapes multimodales ne sont pas interchangeables : Chat
``file`` n'a **pas** de ``file_url``, Responses ``input_file`` en a un, et
``detail`` n'accepte ``"original"`` que côté Responses.

**Parité client officiel (2026-09-18, SDK @ai-sdk/openai).** Le client OpenCode
dialogue en Responses via ``prepareRequest`` : system→developer pour les modèles
de raisonnement, strip des ``id`` d'items (comme Codex), suppression
temperature/top_p sur raisonnement, summary 'detailed' par défaut, relais des
champs optionnels (previous_response_id, prompt_cache_key, service_tier...),
include reasoning.encrypted_content quand store:false. Verrouillé ci-dessous.
"""

import pytest

from app.protocol.mapping import (
    _anthropic_to_responses_request,
    _chat_to_responses_request,
    _ensure_encrypted_content_include,
    _is_responses_reasoning_model,
    _relay_responses_optional_fields,
    _relay_responses_storage_fields,
    _sanitize_native_responses_request,
    _strip_responses_item_ids,
)

# ─────────────────────── B5 : store ───────────────────────


def test_store_false_is_relayed():
    """B5 — LE point confidentialité : ``store: false`` doit atteindre l'upstream.

    Sans relais, la réponse est conservée ≥30 j malgré la consigne du client.
    """
    req = _chat_to_responses_request(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}], "store": False}
    )
    assert req.get("store") is False


def test_store_true_is_relayed():
    """L'autre valeur explicite est relayée aussi (pas seulement le refus)."""
    req = _chat_to_responses_request(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}], "store": True}
    )
    assert req.get("store") is True


def test_store_absent_is_not_invented():
    """Absent → on ne pose PAS de valeur : la décision reste à l'upstream.

    Poser ``store: true`` implicitement prendrait une décision de rétention que
    le proxy n'a pas à prendre à la place du client.
    """
    req = _chat_to_responses_request({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert "store" not in req


@pytest.mark.parametrize("bad", ["yes", 1, 0, [], {}, "false"])
def test_invalid_store_is_dropped_not_forwarded(bad):
    """Une valeur non booléenne serait rejetée par l'upstream (400 évitable)."""
    req = _chat_to_responses_request(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}], "store": bad}
    )
    assert "store" not in req, f"{bad!r} propagé tel quel"


# ─────────────────────── B5 : truncation ───────────────────────


@pytest.mark.parametrize("value", ["auto", "disabled"])
def test_truncation_valid_values_are_relayed(value):
    """B5 : le client choisit son mode de dépassement de contexte."""
    req = _chat_to_responses_request(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}], "truncation": value}
    )
    assert req.get("truncation") == value


def test_truncation_absent_is_not_invented():
    """Absent → défaut upstream préservé (``disabled``, 400 explicite)."""
    req = _chat_to_responses_request({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert "truncation" not in req


@pytest.mark.parametrize("bad", ["banana", "", "AUTO", True, 1, None, ["auto"]])
def test_invalid_truncation_is_dropped(bad):
    """Seules les deux valeurs du schéma passent ; le reste est écarté."""
    req = _chat_to_responses_request(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}], "truncation": bad}
    )
    assert "truncation" not in req, f"{bad!r} propagé tel quel"


def test_relay_helper_is_idempotent():
    """Rejouer le relais ne doit pas dupliquer ni altérer (double conversion)."""
    req = {"model": "m"}
    _relay_responses_storage_fields(req, {"store": False, "truncation": "auto"})
    first = dict(req)
    _relay_responses_storage_fields(req, {"store": False, "truncation": "auto"})
    assert req == first


# ─────────────────────── B5 : chemin Anthropic ───────────────────────


def test_anthropic_path_relays_store_and_truncation():
    """``anthropic_to_openai`` ne transporte pas ces champs : le relais doit être
    fait depuis le corps d'origine, sinon un client Anthropic ne peut ni refuser
    la rétention ni choisir son mode de dépassement."""
    req = _anthropic_to_responses_request(
        {
            "model": "m",
            "max_tokens": 100,
            "store": False,
            "truncation": "auto",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert req.get("store") is False
    assert req.get("truncation") == "auto"


def test_anthropic_path_without_these_fields_is_unchanged():
    """Non-régression : l'absence des champs ne doit rien ajouter."""
    req = _anthropic_to_responses_request(
        {"model": "m", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
    )
    assert "store" not in req
    assert "truncation" not in req


def test_native_responses_body_keeps_store_verbatim():
    """Body déjà au format Responses : verbatim, donc conservé tel quel."""
    req = _sanitize_native_responses_request(
        {"model": "m", "input": [{"role": "user", "content": "hi"}], "store": False}
    )
    assert req.get("store") is False


# ─────────────────────── B6 : include_usage ───────────────────────


def test_stream_options_include_usage_is_set_on_upstream_calls():
    """B6 : ``include_usage`` est REQUIS pour recevoir le chunk d'usage final.

    Verrou de non-régression sur les deux voies aller d'opencode.py : retirer ce
    champ ferait silencieusement disparaître l'usage réel de toute la compta
    streaming.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent.joinpath("opencode.py").read_text(
        encoding="utf-8", errors="replace"
    )
    occurrences = len(re.findall(r'"stream_options"\]\s*=\s*\{"include_usage":\s*True\}', src))
    assert occurrences >= 2, (
        f"include_usage présent {occurrences} fois (<2) : le chunk d'usage final "
        f"ne serait plus reçu sur une des voies Chat"
    )


# ─────────────────────── B8 : shapes multimodales ───────────────────────


def test_responses_input_file_uses_file_url_not_mime_type():
    """B8 : Responses ``input_file`` accepte ``file_url`` — on ne doit pas
    produire un ``mime_type`` inventé que l'upstream rejette."""
    req = _chat_to_responses_request(
        {
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "analyse ce document"},
                        {"type": "document", "source": {"type": "url", "url": "https://x.test/d.pdf"}},
                    ],
                }
            ],
        }
    )
    dumped = str(req)
    assert "mime_type" not in dumped, "mime_type émis vers Responses (non conforme)"


def test_chat_file_has_no_file_url_field():
    """B8 : Chat ``file`` n'a PAS de ``file_url`` — un repli texte est la seule
    option, et c'est ce que fait la conversion."""
    from app.protocol.mapping import anthropic_to_openai

    out = anthropic_to_openai(
        {
            "model": "m",
            "max_tokens": 10,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "voici"},
                        {
                            "type": "document",
                            "source": {"type": "url", "url": "https://x.test/d.pdf"},
                        },
                    ],
                }
            ],
        },
        "m",
    )
    for msg in out.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "file":
                    assert "file_url" not in part, "file_url émis vers Chat (champ inexistant)"


def test_responses_image_detail_original_is_not_forced_to_chat():
    """B8 : ``detail: "original"`` est réservé à Responses ``input_image``."""
    req = _chat_to_responses_request(
        {
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://x.test/i.png", "detail": "high"},
                        }
                    ],
                }
            ],
        }
    )
    dumped = str(req)
    assert "original" not in dumped, "detail=original émis vers un champ Chat"


# ─────────────────────── Parité client officiel ───────────────────────


def _chat(msgs, **kw):
    body = {"model": "muse-spark-1.3-contributor", "messages": msgs}
    body.update(kw)
    return body


def test_system_becomes_developer_for_reasoning_models():
    """SDK (systemMessageMode='developer') : le system Chat part en developer."""
    req = _chat_to_responses_request(
        _chat([{"role": "system", "content": "tu es un agent"}, {"role": "user", "content": "hi"}])
    )
    roles = [it.get("role") for it in req["input"] if isinstance(it, dict)]
    assert "system" not in roles
    assert "developer" in roles


def test_system_kept_for_other_models():
    """Hors muse/spark, le rôle system est inchangé (non-régression)."""
    req = _chat_to_responses_request(
        {
            "model": "glm-5-air",
            "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
        }
    )
    roles = [it.get("role") for it in req["input"] if isinstance(it, dict)]
    assert "system" in roles


@pytest.mark.parametrize("model", ["muse-spark-1.3-contributor", "MUSE-SPARK-1.2-contributor-free", "m"])
def test_is_responses_reasoning_model(model):
    assert _is_responses_reasoning_model(model) == ("muse" in model.lower() or "spark" in model.lower())


def test_strip_item_ids_keeps_pairing_fields():
    """Le client strippe les `id` (comme Codex) mais garde call_id,
    encrypted_content et les item_reference entiers."""
    out = _strip_responses_item_ids(
        [
            {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read", "arguments": "{}"},
            {
                "type": "reasoning",
                "id": "rs_1",
                "encrypted_content": "opaque",
                "summary": [{"type": "summary_text", "text": "t"}],
            },
            {"type": "item_reference", "id": "ref_1"},
            {"role": "user", "id": "msg_1", "content": [{"type": "input_text", "text": "hi"}]},
        ]
    )
    assert "id" not in out[0] and out[0]["call_id"] == "call_1"
    assert "id" not in out[1] and out[1]["encrypted_content"] == "opaque"
    assert out[2] == {"type": "item_reference", "id": "ref_1"}
    assert "id" not in out[3]


def test_native_passthrough_strips_item_ids():
    """Voie native : même strip avant envoi (parité wire officielle)."""
    req = _sanitize_native_responses_request(
        {
            "model": "muse-spark-1.3-contributor",
            "input": [
                {"type": "function_call", "id": "fc_9", "call_id": "call_9", "name": "bash", "arguments": "{}"},
                {"role": "user", "id": "u_1", "content": "hi"},
            ],
        }
    )
    assert "id" not in req["input"][0]
    assert req["input"][0]["call_id"] == "call_9"
    assert "id" not in req["input"][1]


def test_converted_history_carries_no_item_ids():
    """Voie convertie : aucun `id` fabriqué ne fuit vers l'upstream."""
    req = _chat_to_responses_request(
        _chat([{"role": "user", "content": "hi"}], stream=False)
    )
    assert '"id"' not in str(req["input"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("previous_response_id", "resp_abc"),
        ("prompt_cache_key", "ses_abc"),
        ("service_tier", "flex"),
        ("instructions", "réponds en français"),
        ("user", "u-1"),
        ("safety_identifier", "s-1"),
        ("prompt_cache_retention", "30d"),
        ("parallel_tool_calls", False),
        ("max_tool_calls", 4),
        ("top_logprobs", 3),
        ("metadata", {"k": "v"}),
        ("text", {"format": {"type": "text"}}),
        ("include", ["code_interpreter_call.outputs"]),
        ("conversation", "conv_1"),
    ],
)
def test_optional_fields_relayed_when_present(field, value):
    """Champs SDK présents chez le client → relayés (jamais inventés)."""
    req = _chat_to_responses_request(_chat([{"role": "user", "content": "hi"}], **{field: value}))
    assert req.get(field) == value


def test_optional_fields_absent_not_invented():
    """Aucun défaut posé : absent → absent (sémantique upstream préservée)."""
    req = _chat_to_responses_request(_chat([{"role": "user", "content": "hi"}]))
    for field in (
        "previous_response_id",
        "prompt_cache_key",
        "service_tier",
        "instructions",
        "user",
        "metadata",
        "parallel_tool_calls",
        "max_tool_calls",
        "top_logprobs",
        "include",
        "text",
        "conversation",
    ):
        assert field not in req, f"{field} inventé"


def test_conversation_dropped_when_previous_response_id_present():
    """SDK : conversation + previous_response_id mutuellement exclusifs."""
    req = _chat_to_responses_request(
        _chat(
            [{"role": "user", "content": "hi"}],
            conversation="conv_1",
            previous_response_id="resp_1",
        )
    )
    assert req.get("previous_response_id") == "resp_1"
    assert "conversation" not in req


def test_relay_helper_does_not_overwrite_converter_values():
    """Double conversion P6 sûre : le convertisseur gagne sur le relais."""
    req = {"model": "m", "service_tier": "default"}
    _relay_responses_optional_fields(req, {"service_tier": "flex"})
    assert req["service_tier"] == "default"


def test_encrypted_content_include_added_when_store_false():
    """SDK : store:false + reasoning → include reasoning.encrypted_content."""
    req = _chat_to_responses_request(
        _chat(
            [{"role": "user", "content": "hi"}],
            store=False,
            reasoning_effort="high",
        )
    )
    assert req.get("store") is False
    assert "reasoning.encrypted_content" in req.get("include", [])


def test_encrypted_content_include_not_added_by_default():
    """store absent/vrai → rien (pas de surcoût upstream)."""
    req = _chat_to_responses_request(_chat([{"role": "user", "content": "hi"}], reasoning_effort="high"))
    assert "include" not in req
    req2 = _chat_to_responses_request(
        _chat([{"role": "user", "content": "hi"}], store=True, reasoning_effort="high")
    )
    assert "include" not in req2


def test_encrypted_content_include_merged_with_existing():
    """Include client préservé, entrée ajoutée sans doublon (idempotent)."""
    req = {"model": "m", "store": False, "reasoning": {"effort": "high"}, "include": ["a"]}
    _ensure_encrypted_content_include(req)
    _ensure_encrypted_content_include(req)
    assert req["include"].count("reasoning.encrypted_content") == 1
    assert "a" in req["include"]


def test_reasoning_dict_without_summary_gets_detailed():
    """SDK : summary défaut 'detailed' quand un effort est posé sans summary."""
    req = _chat_to_responses_request(
        _chat([{"role": "user", "content": "hi"}], reasoning={"effort": "high"})
    )
    assert req["reasoning"] == {"effort": "high", "summary": "detailed"}


def test_anthropic_path_relays_optional_fields():
    """Chemin Anthropic : previous_response_id et cie suivent aussi."""
    req = _anthropic_to_responses_request(
        {
            "model": "muse-spark-1.3-contributor",
            "max_tokens": 100,
            "previous_response_id": "resp_1",
            "service_tier": "auto",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert req.get("previous_response_id") == "resp_1"
    assert req.get("service_tier") == "auto"


# ─────────────────────── Chaînage : garde orphelin ───────────────────────


def test_orphan_filter_disabled_with_previous_response_id():
    """Avec chaînage serveur, un output sans call visible est légitime (le call
    est dans la réponse chaînée) : le filtre ne doit pas amputer."""
    from app.protocol.mapping import _drop_orphan_responses_input, _drop_orphan_tool_messages

    inp = [{"type": "function_call_output", "call_id": "call_x", "output": "42"}]
    chained = {"previous_response_id": "resp_1"}
    assert _drop_orphan_responses_input(inp, chained) == inp
    assert _drop_orphan_responses_input(inp) == []

    msgs = [{"role": "tool", "tool_call_id": "call_x", "content": "42"}]
    assert _drop_orphan_tool_messages(msgs, chained) == msgs
    assert _drop_orphan_tool_messages(msgs) == []

    conv = {"conversation": "conv_1"}
    assert _drop_orphan_responses_input(inp, conv) == inp


def test_chained_request_keeps_orphans_end_to_end():
    """Bout en bout converti : le chaînage traverse jusqu'au corps wire."""
    req = _chat_to_responses_request(
        _chat(
            [{"role": "user", "content": "suite"}],
            previous_response_id="resp_1",
        )
    )
    assert req.get("previous_response_id") == "resp_1"


# ─────────────────────── Structured output ───────────────────────


def test_response_format_json_object_relayed():
    """response_format json_object (Chat) → text.format (Responses)."""
    req = _chat_to_responses_request(
        _chat([{"role": "user", "content": "hi"}], response_format={"type": "json_object"})
    )
    assert req.get("text") == {"format": {"type": "json_object"}}


def test_response_format_json_schema_relayed():
    """json_schema : name/schema/strict reportés, jamais inventés."""
    req = _chat_to_responses_request(
        _chat(
            [{"role": "user", "content": "hi"}],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "r",
                    "schema": {"type": "object", "properties": {}},
                    "strict": True,
                },
            },
        )
    )
    fmt = req.get("text", {}).get("format", {})
    assert fmt.get("type") == "json_schema"
    assert fmt.get("name") == "r"
    assert fmt.get("strict") is True
    assert isinstance(fmt.get("schema"), dict)


def test_response_format_absent_not_invented():
    """Absent → aucun `text` posé (défaut upstream préservé)."""
    req = _chat_to_responses_request(_chat([{"role": "user", "content": "hi"}]))
    assert "text" not in req


# ─────────────────────── redacted_thinking ───────────────────────


def test_responses_reasoning_encrypted_only_becomes_redacted():
    """Reasoning sans summary visible mais avec encrypted_content → bloc opaque
    rejouable, pas une perte sèche."""
    from app.protocol.mapping import _responses_to_anthropic_response

    out = _responses_to_anthropic_response(
        {
            "output": [
                {"type": "reasoning", "encrypted_content": "opaque", "summary": []},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "voilà"}],
                },
            ]
        },
        "m",
    )
    redacted = [b for b in out["content"] if b.get("type") == "redacted_thinking"]
    assert redacted and redacted[0]["data"] == "opaque"


def test_anthropic_redacted_thinking_becomes_encrypted_content():
    """Sens inverse : redacted_thinking Anthropic → reasoning encrypted_content."""
    from app.protocol.mapping import anthropic_to_openai_responses

    out = anthropic_to_openai_responses(
        {
            "content": [
                {"type": "redacted_thinking", "data": "opaque"},
                {"type": "text", "text": "voilà"},
            ]
        },
        "m",
    )
    reasoning = [i for i in out["output"] if i.get("type") == "reasoning"]
    assert reasoning and reasoning[0].get("encrypted_content") == "opaque"


def test_reasoning_extra_keys_preserved():
    """`reasoning.mode`/`context` (SDK récent) survivent à la conversion."""
    req = _chat_to_responses_request(
        _chat(
            [{"role": "user", "content": "hi"}],
            reasoning={"effort": "high", "mode": "pro", "context": "ctx"},
        )
    )
    assert req["reasoning"].get("mode") == "pro"
    assert req["reasoning"].get("context") == "ctx"


# ─────────────────────── metadata/user/service_tier ───────────────────────


def test_chat_leg_relays_metadata_user_service_tier():
    """Jambes Chat : metadata/user/service_tier valides, relayés sans outils."""
    from app.protocol.mapping import anthropic_to_openai

    out = anthropic_to_openai(
        {
            "model": "m",
            "max_tokens": 10,
            "metadata": {"k": "v"},
            "user": "u-1",
            "service_tier": "flex",
            "messages": [{"role": "user", "content": "hi"}],
        },
        "m",
    )
    assert out.get("metadata") == {"k": "v"}
    assert out.get("user") == "u-1"
    assert out.get("service_tier") == "flex"


def test_anthropic_leg_relays_metadata_and_user_id():
    """Jambes Anthropic : metadata copié, user → metadata.user_id."""
    from app.protocol.mapping import openai_to_anthropic_request

    out = openai_to_anthropic_request(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "metadata": {"k": "v"},
            "user": "u-1",
            "service_tier": "auto",
        }
    )
    assert out.get("metadata", {}).get("k") == "v"
    assert out.get("metadata", {}).get("user_id") == "u-1"
    assert out.get("service_tier") == "auto"


def test_optional_chat_fields_absent_not_invented():
    """Absent → absent sur les jambes Chat/Anthropic aussi."""
    from app.protocol.mapping import anthropic_to_openai, openai_to_anthropic_request

    out = anthropic_to_openai({"model": "m", "max_tokens": 10, "messages": []}, "m")
    assert "metadata" not in out and "user" not in out and "service_tier" not in out
    out2 = openai_to_anthropic_request({"model": "m", "messages": []})
    assert "metadata" not in out2 and "service_tier" not in out2


# ─────────────────────── Clé de cache : hors chaînage ───────────────────────


def test_cache_key_ignores_chaining_fields():
    """F6 : previous_response_id / prompt_cache_key uniques ne doivent pas
    annuler le cache réponse (requêtes identiques → même clé)."""
    from server.cache import ResponseCache

    c = ResponseCache()
    base = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    k1 = c.make_key(dict(base))
    k2 = c.make_key({**base, "previous_response_id": "resp_1", "prompt_cache_key": "ses_abc"})
    assert k1 is not None and k1 == k2
