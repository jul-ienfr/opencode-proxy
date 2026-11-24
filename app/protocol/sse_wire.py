"""Helpers SSE partagés (extraits d'``opencode.py`` — T146/T210).

Regroupe la construction d'événements SSE, la sentinelle « des octets sont
déjà partis » et la terminaison gracieuse d'un flux interrompu. Ces helpers
étaient définis au milieu du monolithe ; ils n'ont aucune dépendance projet
hors la sérialisation JSON, donc ils vivent ici et ``opencode`` les ré-importe
sous leurs noms historiques (``_sse``, ``_stream_has_yielded``, …).
"""

from __future__ import annotations

import json
from typing import Any

try:  # orjson est le chemin nominal ; repli stdlib si absent.
    import orjson as _orjson
except Exception:  # pragma: no cover - l'hôte importe orjson fail-fast
    _orjson = None


def _json_loads(data: bytes | str, **_kw: Any):
    if isinstance(data, str):
        data = data.encode()
    return _orjson.loads(data)


def _json_dumps_str(obj: Any, **_kw: Any) -> str:
    return _orjson.dumps(obj).decode()


def sse(event: str, payload: dict) -> bytes:
    """Un événement SSE ``event:`` + ``data:`` (encodage unicode préservé)."""
    return f"event: {event}\ndata: {_json_dumps_str(payload)}\n\n".encode()


# Alias historique utilisé partout dans le monolithe.
_sse = sse


def stream_has_yielded(started, open_blocks, stream_out, line_buf: str = "") -> bool:
    """True once any SSE byte has been flushed to the client — retry must not concat."""
    try:
        has_blocks = bool(open_blocks)
    except Exception:
        has_blocks = False
    try:
        has_out = int(stream_out or 0) > 0
    except Exception:
        has_out = bool(stream_out)
    return bool(
        started
        or has_blocks
        or has_out
        or (isinstance(line_buf, str) and bool(line_buf.strip()))
    )


_stream_has_yielded = stream_has_yielded


async def terminate_after_started(
    open_blocks,
    stream_out,
    thinking_idx=None,
    thinking_sig="",
    stop_reason="error",
):
    """Graceful SSE termination after a mid-stream failure (avoids concat retry).

    [PLAN-raisonnement Phase C.2] si un bloc thinking est encore ouvert,
    son `signature_delta` local est émis AVANT le content_block_stop — même
    en fin de stream brutale, le client reçoit un bloc thinking complet
    (thinking_delta* → signature_delta → stop), sinon il l'abandonne.
    """
    for idx in list(open_blocks):
        if thinking_idx is not None and idx == thinking_idx and thinking_sig:
            yield _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "signature_delta", "signature": thinking_sig},
                },
            )
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
    yield _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": {"output_tokens": stream_out},
        },
    )


_terminate_after_started = terminate_after_started


def iter_sse_data(lines):
    """Yield les payloads JSON des lignes ``data:`` d'un flux SSE."""
    for line in lines or []:
        s = (
            line.strip()
            if isinstance(line, str)
            else bytes(line).decode("utf-8", "replace").strip()
        )
        if not s.startswith("data:"):
            continue
        payload = s[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield _json_loads(payload)
        except Exception:
            continue


_iter_sse_data = iter_sse_data


def normalize_chat_reasoning_chunk(chunk: dict) -> bool:
    """Ajoute ``delta.reasoning_content`` depuis les autres dialectes.

    Les amonts free ne parlent pas le même dialecte :
      * deepseek / glm / big-pickle -> ``reasoning_content`` ;
      * mimo / nemotron             -> ``reasoning`` (+ ``reasoning_details[]``,
        style OpenRouter).

    Le client officiel 1.19.3 ne lit QUE ``delta.reasoning_content``. On
    AJOUTE donc l'alias sans retirer les champs d'origine (les clients
    OpenRouter, eux, lisent ``reasoning``/``reasoning_details``).

    Rend True si un alias a été ajouté. Ne lève jamais.
    """
    changed = False
    try:
        if not isinstance(chunk, dict):
            return False
        for ch in chunk.get("choices") or []:
            d = ch.get("delta") if isinstance(ch, dict) else None
            if not isinstance(d, dict):
                continue
            cur = d.get("reasoning_content")
            if isinstance(cur, str) and cur:
                continue
            alt = d.get("reasoning")
            if not (isinstance(alt, str) and alt):
                det = d.get("reasoning_details")
                if isinstance(det, list):
                    alt = "".join(
                        p.get("text", "")
                        for p in det
                        if isinstance(p, dict) and isinstance(p.get("text"), str)
                    )
            if isinstance(alt, str) and alt:
                d["reasoning_content"] = alt
                changed = True
    except Exception:
        return changed
    return changed


_normalize_chat_reasoning_chunk = normalize_chat_reasoning_chunk

__all__ = [
    "_iter_sse_data",
    "_normalize_chat_reasoning_chunk",
    "_sse",
    "_stream_has_yielded",
    "_terminate_after_started",
    "iter_sse_data",
    "normalize_chat_reasoning_chunk",
    "sse",
    "stream_has_yielded",
    "terminate_after_started",
]
