"""V5.2 — la trame d'erreur doit parler le protocole du CLIENT, pas de l'amont.

**Le défaut mesuré, en direct.** Un client Anthropic (`POST /v1/messages`) qui
demandait `space-bunny-free` recevait, dans un flux Anthropic :

```
data: {"error":{"message":"Free model request failed: …","type":"api_error"}}
data: [DONE]
```

C'est du **JSON OpenAI** : pas de `event:`, pas d'enveloppe Anthropic. Le SDK
Anthropic ignore les lignes sans `event:` — le client ne voit donc **aucune
erreur**, juste un flux muet. La cause : la forme de la trame était choisie
d'après `protocol`, qui désigne le protocole **amont** (chat pour un modèle
free), au lieu du protocole **client** (`/v1/messages` → Anthropic).

**Ce que ces tests exigent.**

* client Anthropic + amont chat → `event: error` + enveloppe Anthropic ;
* client OpenAI + amont chat → `data: {"error": …}` + `[DONE]` ;
* le `type` et le `message` venus de l'amont survivent (rien n'est remplacé par
  un message vide).
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import opencode as oc  # noqa: E402

PAYLOAD_ANTHROPIC = {
    "type": "error",
    "error": {"type": "server_error", "message": "Upstream request failed: Model is unavailable."},
}


async def _emettre(payload, protocol, client_protocol=None):
    return await oc._stream_error_response(
        "req-test",
        "space-bunny-free",
        "space-bunny-free",
        0.0,
        400,
        b'{"error":{"type":"server_error","message":"Model is unavailable."}}',
        protocol,
        None,
        None,
        "127.0.0.1",
        "free (no auth)",
        [],
        payload,
        client_protocol=client_protocol,
    )


def _frames(octets: bytes) -> list[str]:
    return [l for l in octets.decode("utf-8", "replace").splitlines() if l.strip()]


class TestAnthropicClientGetsAnthropicError:
    @pytest.mark.asyncio
    async def test_free_chat_upstream_with_anthropic_client_emits_event_error(self):
        """LE cas mesuré : amont chat, client `/v1/messages`."""
        brut = await _emettre(PAYLOAD_ANTHROPIC, "openai", client_protocol="anthropic")
        texte = brut.decode("utf-8", "replace")
        assert texte.startswith("event: error"), f"forme OpenAI sur route Anthropic : {texte[:120]!r}"
        assert "[DONE]" not in texte

    @pytest.mark.asyncio
    async def test_the_payload_keeps_the_upstream_type_and_message(self):
        brut = await _emettre(PAYLOAD_ANTHROPIC, "openai", client_protocol="anthropic")
        corps = json.loads(brut.decode("utf-8").split("data: ", 1)[1])
        assert corps["type"] == "error"
        assert corps["error"]["type"] == "server_error"
        assert "Model is unavailable" in corps["error"]["message"]

    @pytest.mark.asyncio
    async def test_no_empty_message(self):
        """V5.2 dit : « transmis tel quel, et non traduit en message vide »."""
        brut = await _emettre(PAYLOAD_ANTHROPIC, "openai", client_protocol="anthropic")
        corps = json.loads(brut.decode("utf-8").split("data: ", 1)[1])
        assert corps["error"]["message"].strip()
        assert corps["error"]["type"].strip()


class TestOpenAIClientGetsOpenAIError:
    @pytest.mark.asyncio
    async def test_chat_route_emits_data_error_and_done(self):
        brut = await _emettre(PAYLOAD_ANTHROPIC, "openai", client_protocol="openai")
        texte = brut.decode("utf-8", "replace")
        assert texte.startswith("data: {")
        assert texte.rstrip().endswith("[DONE]")
        assert "event: error" not in texte

    @pytest.mark.asyncio
    async def test_the_openai_error_keeps_type_and_message(self):
        brut = await _emettre(PAYLOAD_ANTHROPIC, "openai", client_protocol="openai")
        premiere = brut.decode("utf-8").splitlines()[0]
        corps = json.loads(premiere[len("data: ") :])
        assert corps["error"]["type"] == "server_error"
        assert "Model is unavailable" in corps["error"]["message"]


class TestUpstreamTypeFidelity:
    """« Tel quel » ne veut pas dire « aveuglément » : le vocabulaire du client décide."""

    def test_extractor_keeps_a_type_the_client_knows(self):
        assert oc._extract_upstream_error_type('{"error":{"type":"rate_limit_error"}}', "anthropic") == (
            "rate_limit_error"
        )

    def test_extractor_keeps_server_error_for_an_openai_client(self):
        corps = '{"error":{"type":"server_error","message":"Model is unavailable."}}'
        assert oc._extract_upstream_error_type(corps, "openai") == "server_error"

    def test_extractor_refuses_server_error_for_an_anthropic_client(self):
        """`server_error` est un type OpenAI : Anthropic n'en a pas."""
        corps = '{"error":{"type":"server_error","message":"Model is unavailable."}}'
        assert oc._extract_upstream_error_type(corps, "anthropic") is None

    def test_extractor_refuses_an_unknown_type(self):
        assert oc._extract_upstream_error_type('{"error":{"type":"oops"}}', "openai") is None

    def test_extractor_is_safe_on_garbage(self):
        for corps in ("", "pas du json", "[]", "null", '{"error":"texte"}', None, 42):
            assert oc._extract_upstream_error_type(corps, "openai") is None

    @pytest.mark.asyncio
    async def test_openai_client_receives_the_upstream_type_verbatim(self):
        """Le cas qui a motivé le lot : `server_error` était écrasé en `api_error`."""
        payload = {"type": "error", "error": {"type": "api_error", "message": "Free model request failed: …"}}
        brut = await _emettre(payload, "openai", client_protocol="openai")
        corps = json.loads(brut.decode("utf-8").splitlines()[0][len("data: ") :])
        assert corps["error"]["type"] in {"api_error", "server_error"}


class TestBackwardCompatibility:
    @pytest.mark.asyncio
    async def test_without_client_protocol_the_upstream_one_decides(self):
        """Aucun appelant existant ne doit changer de comportement par défaut."""
        anthropic = await _emettre(PAYLOAD_ANTHROPIC, "anthropic")
        assert anthropic.decode("utf-8").startswith("event: error")
        openai = await _emettre(PAYLOAD_ANTHROPIC, "openai")
        assert openai.decode("utf-8").startswith("data: {")


class TestTheCallSitesDeclareTheirClientProtocol:
    """Le contrat des appelants : pas de nouveau chemin muet sans client_protocol."""

    def test_messages_handler_passes_anthropic(self):
        source = (ROOT / "opencode.py").read_text(encoding="utf-8")
        bloc = source.split("async def messages(request: Request):", 1)[1]
        bloc = bloc.split("async def chat_completions", 1)[0]
        appels = bloc.count("_stream_error_response(") + bloc.count("_free_stream_refuse_bytes(")
        declares = bloc.count('client_protocol="anthropic"')
        assert appels > 0
        assert declares >= appels, (
            f"{appels} émissions d'erreur dans /v1/messages mais seulement "
            f"{declares} déclarent client_protocol — une trame OpenAI peut fuir "
            "vers un client Anthropic"
        )

    def test_chat_handler_passes_openai(self):
        source = (ROOT / "opencode.py").read_text(encoding="utf-8")
        bloc = source.split("async def chat_completions(request: Request):", 1)[1]
        appels = bloc.count("_stream_error_response(") + bloc.count("_free_stream_refuse_bytes(")
        declares = bloc.count('client_protocol="openai"')
        assert appels > 0
        assert declares >= appels


def test_stream_error_detail_relays_overflow_marker():
    """Un overflow stream doit rester reconnaissable par le client officiel."""
    msg, typ = oc._stream_error_detail(
        400,
        b'{"error":{"type":"invalid_request_error","message":"prompt is too long: 250000 tokens"}}',
    )
    assert "prompt is too long" in msg
    assert typ == "invalid_request_error"


def test_stream_error_detail_keeps_generic_400():
    msg, typ = oc._stream_error_detail(400, b'{"error":"bad request"}')
    assert msg == "HTTP 400"
    assert typ == "api_error"
