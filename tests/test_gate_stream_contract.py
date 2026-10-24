"""Le forcage `stream:true` sur la jambe free : contrat mesure, pas commodite.

**La mesure qui fonde ces tests** (2026-10-22, trois corps identiques sauf
`stream`, meme modele, meme tunnel) :

| Corps | Statut |
|---|---|
| `stream: true` | **200** (`text/event-stream`, vrai flux) |
| `stream: false` | **403** `FreeTierError` |
| `stream` absent | **403** `FreeTierError` |

Le refus tombe en 1,2 s contre 5,7 s pour un appel accepte : la porte rejette
**avant** toute generation. Le forcage n'est donc pas une commodite de notre
cote, c'est la condition d'entree.

**Pourquoi ces tests existent.** La reconstitution JSON qui accompagne le
forcage est couteuse et ressemble a une dette technique. Sans ce fichier, la
prochaine personne qui la trouve lourde la supprimera comme une evidence — et
cassera la jambe free. Un commentaire ne survit pas a six mois ; un test rouge,
si.
"""

import pytest

import opencode as oc


def chat_body(**extra):
    body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
    body.update(extra)
    return body


def responses_body(**extra):
    body = {"input": [{"role": "user", "content": "hi"}], "max_output_tokens": 8}
    body.update(extra)
    return body


class TestForcedStream:
    def test_chat_without_stream_is_forced(self):
        wire, flipped = oc._free_wire_body(chat_body(), "big-pickle")
        assert wire.get("stream") is True
        assert flipped is True, "le forçage doit être signalé (reconstitution nécessaire)"

    def test_responses_without_stream_is_forced(self):
        wire, flipped = oc._free_wire_body(responses_body(), "big-pickle")
        assert wire.get("stream") is True
        assert flipped is True

    def test_an_explicit_false_is_forced_too(self):
        """`stream:false` est un 403 mesuré : on ne le laisse pas passer tel quel."""
        wire, flipped = oc._free_wire_body(chat_body(stream=False), "big-pickle")
        assert wire.get("stream") is True
        assert flipped is True

    def test_an_already_streaming_body_is_untouched(self):
        wire, flipped = oc._free_wire_body(chat_body(stream=True), "big-pickle")
        assert flipped is False
        assert wire.get("stream") is True

    def test_the_original_body_is_not_mutated(self):
        body = chat_body()
        oc._free_wire_body(body, "big-pickle")
        assert "stream" not in body, "le corps d'entrée ne doit pas être modifié en place"


class TestGateContract:
    def test_the_read_shim_accompanies_the_forced_stream(self):
        """Les deux conditions mesurées voyagent ensemble : stream + read."""
        wire, _ = oc._free_wire_body(chat_body(), "big-pickle")
        names = [t["function"]["name"] for t in wire.get("tools") or []]
        assert "read" in names
        assert wire.get("stream") is True

    def test_a_body_without_messages_or_input_is_left_alone(self):
        """Hors chat et hors responses, le proxy ne touche à rien."""
        wire, flipped = oc._free_wire_body({"foo": "bar"}, "big-pickle")
        assert flipped is False
        assert wire == {"foo": "bar"}
