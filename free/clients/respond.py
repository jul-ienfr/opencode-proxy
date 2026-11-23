"""Réponses et erreurs par famille — 023 (T061/T062).

Un seul endroit pour traduire une erreur interne vers le format du client.
Aucun 429 synthétique : ``capacity_exhausted`` est un 503.
"""

from __future__ import annotations

import json
from typing import Any

from free.clients import (
    family_anthropic,
    family_openai_chat,
    family_openai_responses,
    family_passthrough,
)

_FAMILIES = {
    family_anthropic.FAMILY: family_anthropic,
    family_openai_chat.FAMILY: family_openai_chat,
    family_openai_responses.FAMILY: family_openai_responses,
    family_passthrough.FAMILY: family_passthrough,
}


def convert_request(body: dict[str, Any], family: str) -> dict[str, Any]:
    module = _FAMILIES.get(family)
    if module is None:
        return family_openai_chat.to_chat(body)
    return module.to_chat(body)


def sse_contract(family: str) -> tuple[str, ...]:
    module = _FAMILIES.get(family)
    return module.sse_contract() if module else ()


def count_tokens(
    body: dict[str, Any],
    family: str,
    *,
    estimate_fn: Any | None = None,
) -> int:
    """[023 T066] Compte les tokens d'entrée selon la famille du client.

    Chaque famille ne transmet au compteur QUE les champs de son schéma
    (Anthropic : ``system``/``messages``/``tools`` ; Responses :
    ``instructions``/``input``/``tools`` ; Chat : ``messages``/``tools``).
    Cela évite qu'un corps multi-format (ou un client mal typé) fasse compter
    deux fois le même historique. Le multimodal reste géré par
    ``protocol.tokens.estimate_input_tokens`` (coût média proportionnel).
    """
    module = _FAMILIES.get(family)
    fn = getattr(module, "count_tokens", None) if module is not None else None
    if fn is not None:
        return int(fn(body, estimate_fn=estimate_fn))
    if estimate_fn is None:
        from protocol.tokens import estimate_input_tokens as estimate_fn
    return int(estimate_fn(body))


def error_payload(
    family: str,
    message: str,
    *,
    error_type: str = "api_error",
    code: str = "",
) -> dict[str, Any]:
    if family == family_anthropic.FAMILY:
        return {"type": "error", "error": {"type": error_type, "message": message}}
    payload: dict[str, Any] = {"message": message, "type": error_type}
    if code:
        payload["code"] = code
    return {"error": payload}


def local_refusal(
    family: str,
    reason: str,
    *,
    field: str = "",
) -> tuple[int, dict[str, Any]]:
    message = "Request cannot be converted to the official wire"
    if field:
        message += f" (field: {field})"
    if reason:
        message += f": {reason}"
    return 422, error_payload(
        family,
        message,
        error_type="wire_incompatible",
        code="wire_incompatible",
    )


def capacity_exhausted(
    family: str,
    reset_at: float | None = None,
) -> tuple[int, dict[str, Any]]:
    message = "No proven free capacity is available right now"
    if reset_at:
        message += f"; next reset at {int(reset_at)}"
    return 503, error_payload(
        family,
        message,
        error_type="capacity_exhausted",
        code="capacity_exhausted",
    )


def quota_exhausted(
    family: str,
    *,
    retry_after: str = "",
    message: str = "Free usage limit reached",
) -> tuple[int, dict[str, Any]]:
    return 429, error_payload(
        family,
        message,
        error_type="rate_limit_error",
        code="free_usage_limit",
    )


def sse_frame(event: str, data: Any) -> bytes:
    body = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    if event:
        return f"event: {event}\ndata: {body}\n\n".encode()
    return f"data: {body}\n\n".encode()


def sse_done() -> bytes:
    return b"data: [DONE]\n\n"
