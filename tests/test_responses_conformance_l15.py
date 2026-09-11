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
"""

import pytest

from app.protocol.mapping import (
    _anthropic_to_responses_request,
    _chat_to_responses_request,
    _relay_responses_storage_fields,
    _sanitize_native_responses_request,
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
