"""[PLAN-raisonnement Phase E] Tests e2e raisonnement complet côté client.

Contrat vérifié :
- non-streaming : blocs thinking synthétisés portent une signature locale
  non vide ; reasoning chiffré → redacted_thinking ;
- streaming : signature_delta émis AVANT le content_block_stop du bloc
  thinking, y compris fin de stream brutale (ordre contractuel
  thinking_delta* → signature_delta → content_block_stop) ;
- multi-tours : blocs SYNTHÉTIQUES strippés de l'historique vers les
  upstreams, blocs ORIGINAUX préservés, redacted_thinking strippé hors
  Anthropic.
"""

import base64
import json

import protocol_mapping as pm


def _parse_sse(raw: bytes):
    """b'event: X\\ndata: {...}\\n\\n' -> (event, payload)."""
    lines = raw.decode().split("\n")
    event = lines[0].removeprefix("event: ").strip()
    payload = json.loads(lines[1].removeprefix("data: ").strip())
    return event, payload


# ── Non-streaming (Phase B) ───────────────────────────────────────────


def test_nonstream_thinking_has_signature():
    oai = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "42",
                    "reasoning_content": "6*7=42",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    out = pm.openai_to_anthropic(oai, "glm-5.1-free")
    thinking = out["content"][0]
    assert thinking["type"] == "thinking"
    assert thinking["signature"], "signature absente du bloc thinking"
    assert thinking["signature"] == pm._local_signature(thinking["thinking"])


def test_nonstream_encrypted_reasoning_becomes_redacted():
    blob = base64.b64encode(b"\x9f" * 96).decode()  # base64, sans espace, %4==0
    oai = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "ok",
                    "reasoning_content": blob,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }
    out = pm.openai_to_anthropic(oai, "m")
    first = out["content"][0]
    assert first["type"] == "redacted_thinking"
    assert first["data"] == blob
    assert "signature" not in first and "thinking" not in first


def test_nonstream_plaintext_reasoning_not_redacted():
    oai = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "ok",
                    "reasoning_content": "Je réfléchis à la question posée.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }
    out = pm.openai_to_anthropic(oai, "m")
    assert out["content"][0]["type"] == "thinking"


# ── Streaming (Phase C) ───────────────────────────────────────────────


async def test_finalize_emits_signature_delta_before_stop(monkeypatch):
    import opencode

    saved = []

    async def _fake_save(*a, **kw):
        saved.append((a, kw))

    monkeypatch.setattr(opencode, "_save_and_log_request", _fake_save)

    sig = pm._local_signature("raisonnement streamé")
    events = []
    async for ev in opencode._finalize_and_close_stream(
        True,  # started
        [1],  # open_blocks — bloc thinking encore ouvert
        2,  # text_block_idx
        1,  # reasoning_block_idx
        {},  # tool_block_idx
        10,  # stream_out_tokens
        None,  # actual_usage
        "glm-5.1-free",
        100,
        "msg_test",
        "claude-test",
        {"glm-5.1-free": {"input": 0, "output": 0, "cache": 0}},
        __import__("asyncio").Lock(),
        False,
        "paid-model",
        "",
        0.0,
        "req1",
        "req1",
        "openai",
        "enabled",
        "high",
        "127.0.0.1",
        {},
        [],
        [],
        {},
        reasoning_signature=sig,
    ):
        events.append(_parse_sse(ev))

    types = [(e, p.get("index")) for e, p in events]
    assert ("content_block_delta", 1) in types
    sig_i = types.index(("content_block_delta", 1))
    delta = events[sig_i][1]["delta"]
    assert delta == {"type": "signature_delta", "signature": sig}
    stop_i = types.index(("content_block_stop", 1))
    assert sig_i < stop_i, "signature_delta doit précéder content_block_stop du bloc thinking"
    assert types[-1] == ("message_stop", None)


async def test_terminate_after_started_flushes_signature_on_abrupt_end():
    import opencode

    sig = pm._local_signature("texte partiel")
    events = []
    async for ev in opencode._terminate_after_started(
        [0, 1], 7, thinking_idx=1, thinking_sig=sig
    ):
        events.append(_parse_sse(ev))

    kinds = [(e, p.get("index"), p.get("delta")) for e, p in events]
    # stop des deux blocs ouverts, dans l'ordre
    assert [k for k in kinds if k[0] == "content_block_stop"] == [
        ("content_block_stop", 0, None),
        ("content_block_stop", 1, None),
    ]
    # signature_delta UNIQUEMENT sur le bloc thinking, avant son stop
    deltas = [k for k in kinds if k[0] == "content_block_delta"]
    assert deltas == [(("content_block_delta"), 1, {"type": "signature_delta", "signature": sig})]
    assert kinds.index(deltas[0]) < kinds.index(("content_block_stop", 1, None))
    # terminaison propre après flush
    assert kinds[-2][0] == "message_delta" and kinds[-2][2]["stop_reason"] == "error"
    assert kinds[-1][0] == "message_stop"


async def test_terminate_without_thinking_no_signature_delta():
    import opencode

    events = []
    async for ev in opencode._terminate_after_started([0], 3):
        events.append(_parse_sse(ev))
    assert not any(e == "content_block_delta" for e, _ in events)


# ── Multi-tours (Phase D) ─────────────────────────────────────────────


def test_multitour_roundtrip_synthetic_stripped_original_kept():
    # 1) réponse upstream openai → réponse anthropic signée localement
    resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Réponse.",
                    "reasoning_content": "raisonnement du modèle tiers",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }
    out = pm.openai_to_anthropic(resp, "glm-5.1-free")
    thinking_block = out["content"][0]

    # 2) le client renvoie l'historique → conversion vers upstream openai
    body = {
        "model": "x",
        "thinking": {"type": "enabled"},
        "messages": [
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": out["content"]},
            {"role": "user", "content": "Q2"},
        ],
    }
    conv = pm.anthropic_to_openai(body, "upstream-model")
    asst = next(m for m in conv["messages"] if m["role"] == "assistant")
    # bloc SYNTHÉTIQUE supprimé de l'historique upstream
    assert "raisonnement du modèle tiers" not in json.dumps(conv)
    # le bloc servi au client était complet (signature locale)
    assert thinking_block["signature"] == pm._local_signature("raisonnement du modèle tiers")


def test_multitour_original_thinking_preserved_for_openai_upstream():
    body = {
        "model": "x",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "vraie réflexion",
                        "signature": "SIGNATURE-AUTHENTIQUE==",
                    },
                    {"type": "text", "text": "Réponse."},
                ],
            }
        ],
    }
    conv = pm.anthropic_to_openai(body, "upstream")
    asst = conv["messages"][0]
    assert asst["reasoning_content"] == "vraie réflexion"


def test_multitour_redacted_stripped_for_openai_upstream():
    body = {
        "model": "x",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "redacted_thinking", "data": "BLOB"},
                    {"type": "text", "text": "Réponse."},
                ],
            }
        ],
    }
    conv = pm.anthropic_to_openai(body, "upstream")
    assert "BLOB" not in json.dumps(conv)


def test_strip_synthetic_thinking_keeps_original_and_redacted():
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "synthétique proxy",
                        "signature": pm._local_signature("synthétique proxy"),
                    },
                    {
                        "type": "thinking",
                        "thinking": "authentique",
                        "signature": "SIG-ANTHROPIC-REELLE==",
                    },
                    {"type": "redacted_thinking", "data": "BLOB"},
                    {"type": "text", "text": "Suite"},
                ],
            }
        ]
    }
    stripped = pm.strip_synthetic_thinking(body)
    assert stripped == 1
    blocks = body["messages"][0]["content"]
    types = [b["type"] for b in blocks]
    assert types == ["thinking", "redacted_thinking", "text"]
    assert blocks[0]["signature"] == "SIG-ANTHROPIC-REELLE=="


def test_openai_history_to_anthropic_upstream_no_forged_signature():
    oai_body = {
        "model": "claude-sonnet-4-5",
        "messages": [
            {"role": "user", "content": "Hi"},
            {
                "role": "assistant",
                "content": "Hello!",
                "reasoning_content": "réflexion d'un autre modèle",
            },
        ],
    }
    anthro = pm.openai_to_anthropic_request(oai_body)
    dumped = json.dumps(anthro)
    assert '"thinking"' not in dumped, (
        "aucun bloc thinking forgé vers un upstream Anthropic "
        "(signature cryptographique exigée par Anthropic)"
    )
    assert "réflexion d'un autre modèle" not in dumped


def test_provenance_detector_rejects_authentic_signatures():
    text = "n'importe quel raisonnement"
    assert pm._is_local_signature(text, pm._local_signature(text))
    assert not pm._is_local_signature(text, "SIGNATURE-AUTHENTIQUE==")
    assert not pm._is_local_signature(text, "")
    assert not pm._is_local_signature("", pm._local_signature(""))
