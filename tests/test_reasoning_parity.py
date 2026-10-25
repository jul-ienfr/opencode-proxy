"""Parité du raisonnement sur les trois protocoles.

Constat initial (2026-10-25) : le raisonnement sortait en chat, mais les
façades Anthropic et Responses semblaient le perdre. Vérification : c'était un
faux négatif (le modèle n'avait pas raisonné sur l'appel testé). Les trois
conversions le portent déjà :

  * chat non-stream   -> ``message.reasoning_content`` (collecteur SSE)
  * chat stream       -> ``delta.reasoning_content`` (passthrough)
  * Anthropic         -> bloc ``thinking`` (stream: ``thinking_delta``)
  * Responses         -> item ``reasoning`` (stream: événements reasoning)

Ces tests figent la parité côté conversion (le stream a déjà ses tests dans
``test_chat_sse_to_anthropic.py``).
"""

import pytest

from app.protocol.mapping import (
    openai_chat_to_responses,
    openai_to_anthropic,
    strip_synthetic_thinking,
)

REASONING = "17*23 = 17*(20+3) = 340 + 51 = 391."
CHAT = {
    "choices": [
        {
            "message": {"role": "assistant", "content": "391", "reasoning_content": REASONING},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def test_anthropic_non_stream_expose_le_raisonnement_en_bloc_thinking():
    out = openai_to_anthropic(CHAT, "m")
    blocks = out["content"]
    assert [b["type"] for b in blocks] == ["thinking", "text"]
    assert blocks[0]["thinking"] == REASONING
    assert blocks[0]["signature"], "une signature locale est attendue"
    assert blocks[1]["text"] == "391"


def test_anthropic_signature_locale_est_strippee_au_retour():
    """Le client peut renvoyer le bloc : il ne doit jamais partir signé."""
    out = openai_to_anthropic(CHAT, "m")
    body = {"messages": [{"role": "assistant", "content": out["content"]}]}
    assert strip_synthetic_thinking(body) == 1
    assert [b["type"] for b in body["messages"][0]["content"]] == ["text"]


def test_responses_non_stream_expose_un_item_reasoning():
    out = openai_chat_to_responses(CHAT, "m")
    types = [it["type"] for it in out["output"]]
    assert types == ["reasoning", "message"]
    assert out["output"][0]["summary"][0]["text"] == REASONING
    assert out["output"][1]["content"][0]["text"] == "391"


def test_sans_raisonnement_aucun_bloc_vide():
    chat = {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {}}
    anthro = openai_to_anthropic(chat, "m")
    assert [b["type"] for b in anthro["content"]] == ["text"]
    resp = openai_chat_to_responses(chat, "m")
    assert [it["type"] for it in resp["output"]] == ["message"]
