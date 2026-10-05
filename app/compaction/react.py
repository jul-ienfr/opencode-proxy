"""app.compaction.react — orchestration réactive (Phase 2, fail-open).

Un seul point d'entrée : `maybe_condense`. Le handler l'appelle avec la
réponse upstream en échec ; retourne l'historique condensé prêt à forwarder,
ou None (passthrough inchangé — l'appelant relaie alors l'erreur intacte).

Ne lève jamais. Aucun import opencode/config ici (pas de cycle) : le handler
résout model_id/endpoint/protocol (y compris summarizer_model_override) et
fournit les closures auth/do_request.
"""

from __future__ import annotations

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
    timeout_s=60,
    markers=None,
    api="chat",
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
        if not is_overflow(status_code, body_text, markers):
            return None
        if not isinstance(history_messages, list) or not history_messages:
            return None
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
        )
        if not isinstance(summary, str) or not summary.strip():
            return None
        try:
            _api = str(api or "chat").lower()
        except Exception:
            _api = "chat"
        if _api == "responses":
            condensed, _kept = build_condensed_input(history_messages, summary, keep_recent_pairs)
        else:
            condensed, _kept = build_condensed_history(history_messages, summary, keep_recent_pairs)
        if not condensed:
            return None
        return condensed
    except Exception:
        return None


__all__ = ["maybe_condense"]
