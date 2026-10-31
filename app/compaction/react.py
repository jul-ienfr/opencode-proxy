"""app.compaction.react — orchestration réactive (Phase 2, fail-open).

Un seul point d'entrée : `maybe_condense`. Le handler l'appelle avec la
réponse upstream en échec ; retourne l'historique condensé prêt à forwarder,
ou None (passthrough inchangé — l'appelant relaie alors l'erreur intacte).

Ne lève jamais. Aucun import opencode/config ici (pas de cycle) : le handler
résout model_id/endpoint/protocol (y compris summarizer_model_override) et
fournit les closures auth/do_request.
"""

from __future__ import annotations

import asyncio

from app.compaction.overflow import is_overflow
from app.compaction.summarizer import run_summarizer
from app.compaction.truncate import build_condensed_history, build_condensed_input


async def maybe_condense(
    history_messages,
    *,
    status_code,
    body_text,
    model_id,
    endpoint,
    protocol,
    auth_headers_fn,
    do_request_fn,
    enabled=False,
    summary_max_tokens=2048,
    keep_recent_pairs=4,
    keep_tokens=None,
    estimate_tokens_fn=None,
    timeout_s=60,
    markers=None,
    api="chat",
    history_api=None,
    full_history=False,
    system=None,
    tools=None,
    instructions=None,
    max_attempts=1,
    keep_tail=True,
    force=False,
):
    """Historique condensé [checkpoint, *recent] si 400-overflow avéré, sinon None.

    Garde-fous : enabled False → None ; pas d'overflow → None ; résumé vide /
    échec résumeur → None ; condensé invalide → None. Une seule tentative par
    appel, jamais de récursion (le résumeur passe par do_request_fn direct,
    jamais par un handler).
    """
    try:
        if not enabled:
            return None
        if not force and not is_overflow(status_code, body_text, markers):
            return None
        if not isinstance(history_messages, list) or not history_messages:
            return None
        try:
            _attempts = max(1, min(5, int(max_attempts or 1)))
        except Exception:
            _attempts = 1
        summary = None
        for _attempt in range(_attempts):
            summary = await run_summarizer(
                history_messages,
                model_id=model_id,
                endpoint=endpoint,
                protocol=protocol,
                auth_headers_fn=auth_headers_fn,
                do_request_fn=do_request_fn,
                summary_max_tokens=summary_max_tokens,
                timeout_s=timeout_s,
                api=api,
                full_history=full_history,
                history_api=history_api,
                system=system,
                tools=tools,
                instructions=instructions,
            )
            if isinstance(summary, str) and summary.strip():
                break
            if _attempt + 1 < _attempts:
                await asyncio.sleep(0.5 * (2**_attempt))
        if not isinstance(summary, str) or not summary.strip():
            return None
        # [audit 2026-10-30] ``api`` is the SUMMARIZER body shape. The
        # condensed history must use the CLIENT's shape, which can differ
        # (e.g. free summarizer on /responses, chat client). Using one value
        # for both produced a Responses checkpoint inside a Chat body (400).
        try:
            _hist_api = str(history_api or api or "chat").lower()
        except Exception:
            _hist_api = "chat"
        if _hist_api == "responses":
            condensed, _kept = build_condensed_input(
                history_messages, summary, keep_recent_pairs, keep_tokens, estimate_tokens_fn, keep_tail
            )
        else:
            condensed, _kept = build_condensed_history(
                history_messages, summary, keep_recent_pairs, keep_tokens, estimate_tokens_fn, keep_tail
            )
        if not condensed:
            return None
        return condensed
    except Exception:
        return None


__all__ = ["maybe_condense"]
