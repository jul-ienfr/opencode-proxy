"""app.compaction.streaming — synthèse SSE bufferisée + prédicat tiny (pur).

Quand une requête de compactage arrive en ``stream:true``, les boucles live
émettent chaque chunk dès réception : si l'amont free ne produit qu'un micro
complément (signature ``upstream_tiny_output`` : énorme input, micro output,
aucun outil), le client a DÉJÀ reçu l'octet défectueux et aucun retry
same-stream n'est possible (``stream_retry_suppressed_after_started``).

La parade (free-only, pré-réponse) : fetch bufferisé (stream forcé OFF),
1 refetch station fraîche sur tiny, PUIS émission au client. Fonctions pures,
stdlib uniquement, ne lèvent jamais :
- ``should_buffer_compaction`` — gate (stream + shape compaction + sans
  outils + tiny_retry actif + jambe free) ;
- ``is_tiny_result`` — même règle que le garde ``suspect_tiny_output``
  (seuils injectés, défauts identiques) ;
- ``completion_text`` — texte d'un JSON final (Anthropic/Chat/Responses) ;
- ``chat_sse_from_completion`` / ``anthropic_sse_from_completion`` —
  séquences SSE valides reconstruites depuis un JSON complet (mêmes champs
  que les émetteurs live : ``_sse`` / ``chat.completion.chunk``).
"""

from __future__ import annotations

import json as _json


def should_buffer_compaction(
    *,
    is_stream=False,
    is_compaction=False,
    tiny_retry_enabled=True,
    has_free_leg=False,
) -> bool:
    """True si le stream de compactage doit passer en mode bufferisé.

    Quatre conditions cumulatives : client en stream, shape compaction
    détectée, ``server_compaction.tiny_retry`` actif, conversation servie en
    free (le retry tiny est free-only — le payant ne tronque pas).

    PAS de veto sur les outils déclarés : les compactions Claude Code
    rejouent l'historique complet AVEC leurs 15+ outils (tour agent normal
    sinon). La fidélité est garantie EN SORTIE par
    ``final_has_nontext_blocks`` : tout final portant autre chose que du
    texte (tool_use, thinking, bloc compaction natif…) retombe en live.
    """
    try:
        return bool(
            is_stream
            and is_compaction
            and tiny_retry_enabled
            and has_free_leg
        )
    except Exception:
        return False


def is_tiny_result(inp, out, tools_used, *, input_min=40000, output_max=100) -> bool:
    """True si (énorme input + micro output + aucun outil) — miroir du garde
    ``suspect_tiny_output`` (mêmes défauts) : un résumé de 40k qui tient en
    17 tokens n'en est pas un, c'est une troncation amont à rejouer."""
    try:
        if tools_used:
            return False
        return (inp or 0) >= (input_min or 40000) and (out or 0) < (output_max or 100)
    except Exception:
        return False


def completion_text(data) -> str:
    """Texte d'un JSON final client (Anthropic ``content[]`` / Chat
    ``choices[0]`` / Responses ``output[]``). Item ``compaction`` opaque
    ignoré (jamais interprété). Chaîne vide si introuvable (jamais None)."""
    try:
        if not isinstance(data, dict):
            return ""
        content = data.get("content")
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    t = b.get("text", "")
                    if isinstance(t, str):
                        parts.append(t)
            if parts:
                return "".join(parts)
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            msg = choices[0].get("message", {})
            if isinstance(msg, dict):
                c = msg.get("content", "")
                if isinstance(c, str) and c:
                    return c
                if isinstance(c, list):
                    parts = []
                    for b in c:
                        if isinstance(b, dict) and isinstance(b.get("text"), str):
                            parts.append(b["text"])
                    if parts:
                        return "".join(parts)
        output = data.get("output")
        if isinstance(output, list):
            parts = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type", "")).lower() in ("compaction", "compaction_trigger"):
                    continue
                icontent = item.get("content")
                if isinstance(icontent, list):
                    for b in icontent:
                        if isinstance(b, dict) and str(b.get("type", "")) in (
                            "output_text",
                            "text",
                            "input_text",
                        ):
                            t = b.get("text", "")
                            if isinstance(t, str):
                                parts.append(t)
            if parts:
                return "".join(parts)
        return ""
    except Exception:
        return ""


def final_has_nontext_blocks(data) -> bool:
    """True si le final porte autre chose que du texte pur (→ live only).

    Garde-fou de fidélité du mode bufferisé : la synthèse SSE ne sait rendre
    que du texte. Tout bloc ``tool_use``/``tool_result``/``thinking``,
    ``tool_calls``, ``reasoning``, item ``function_call`` ou bloc/item
    ``compaction`` natif (opaque, à rejouer tel quel) renvoie vers le live
    streaming — jamais de résumé amputé. Doute quelconque → True (sûr).
    Ne lève jamais.
    """
    try:
        if not isinstance(data, dict):
            return True
        for b in data.get("content") or []:
            if isinstance(b, dict) and b.get("type") in (
                "tool_use",
                "tool_result",
                "thinking",
                "redacted_thinking",
                "server_tool_use",
                "web_search_tool_result",
                "compaction",
            ):
                return True
        choices = data.get("choices") or []
        msg = (choices[0].get("message") or {}) if choices and isinstance(choices[0], dict) else {}
        if isinstance(msg, dict):
            if msg.get("tool_calls"):
                return True
            if msg.get("reasoning_content") or msg.get("reasoning"):
                return True
        for it in data.get("output") or []:
            if isinstance(it, dict) and it.get("type") in (
                "function_call",
                "function_call_output",
                "reasoning",
                "compaction",
                "compaction_trigger",
            ):
                return True
        return False
    except Exception:
        return True


def _dump(payload) -> bytes:
    try:
        return _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except Exception:
        return b"{}"


def chat_sse_from_completion(
    text, *, model="", msg_id="", created=0, prompt_tokens=0, completion_tokens=0
) -> list:
    """SSE Chat Completions depuis un texte complet : 1 delta + final + DONE.

    Forme acceptée par les SDK OpenAI (accumulation des deltas) : le client
    qui a demandé ``stream:true`` reçoit un flux valide, émis d'un coup
    après le fetch bufferisé (TTFB = durée totale — acceptable pour un
    résumé de fond, meilleur qu'un échec garanti).
    """
    try:
        text = text if isinstance(text, str) else ""
        base = {
            "id": msg_id or "chatcmpl-compact",
            "object": "chat.completion.chunk",
            "created": created or 0,
            "model": model or "",
        }
        first = dict(base)
        first["choices"] = [
            {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}
        ]
        last = dict(base)
        last["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        last["usage"] = {
            "prompt_tokens": prompt_tokens or 0,
            "completion_tokens": completion_tokens or 0,
        }
        return [
            b"data: " + _dump(first) + b"\n\n",
            b"data: " + _dump(last) + b"\n\n",
            b"data: [DONE]\n\n",
        ]
    except Exception:
        return [b"data: [DONE]\n\n"]


def anthropic_sse_from_completion(
    text,
    *,
    model="",
    msg_id="",
    input_tokens=0,
    output_tokens=0,
    cache_read=0,
    stop_reason="end_turn",
) -> list:
    """SSE Anthropic depuis un texte complet : séquence message
    (start → block_start → delta → block_stop → message_delta → message_stop).

    Mêmes champs que l'émetteur live (``message_start.usage``,
    ``text_delta``, ``message_delta.usage``) : les SDK Anthropic et Claude
    Code l'acceptent comme un stream ordinaire.
    """
    try:
        text = text if isinstance(text, str) else ""
        mid = msg_id or "msg_compact"
        return [
            b"event: message_start\ndata: "
            + _dump(
                {
                    "type": "message_start",
                    "message": {
                        "id": mid,
                        "type": "message",
                        "role": "assistant",
                        "model": model or "",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": input_tokens or 0,
                            "output_tokens": 0,
                            "cache_read_input_tokens": cache_read or 0,
                        },
                    },
                }
            )
            + b"\n\n",
            b"event: content_block_start\ndata: "
            + _dump({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            + b"\n\n",
            b"event: content_block_delta\ndata: "
            + _dump({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
            + b"\n\n",
            b"event: content_block_stop\ndata: "
            + _dump({"type": "content_block_stop", "index": 0})
            + b"\n\n",
            b"event: message_delta\ndata: "
            + _dump(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason or "end_turn"},
                    "usage": {"output_tokens": output_tokens or 0},
                }
            )
            + b"\n\n",
            b"event: message_stop\ndata: " + _dump({"type": "message_stop"}) + b"\n\n",
        ]
    except Exception:
        return []


__all__ = [
    "should_buffer_compaction",
    "is_tiny_result",
    "final_has_nontext_blocks",
    "completion_text",
    "chat_sse_from_completion",
    "anthropic_sse_from_completion",
]
