"""Famille passthrough et profil ``custom`` — 023 (T051)."""

from __future__ import annotations

import copy
from typing import Any

FAMILY = "passthrough"
SSE_EVENTS = ()


def to_chat(body: dict[str, Any]) -> dict[str, Any]:
    """Corps déjà conforme : aucune transformation."""
    return copy.deepcopy(body) if isinstance(body, dict) else body


def sse_contract() -> tuple[str, ...]:
    return SSE_EVENTS


def count_tokens(
    body: dict[str, Any],
    *,
    estimate_fn: Any | None = None,
) -> int:
    """Compte les tokens d'un corps déjà conforme (aucune normalisation)."""
    if estimate_fn is None:
        from protocol.tokens import estimate_input_tokens as estimate_fn
    return estimate_fn(body)
