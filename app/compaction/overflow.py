"""app.compaction.overflow — matcher 400-overflow upstream (Phase 2, pur).

Fonction pure, stdlib uniquement, ne lève jamais : tout input inattendu → False.

Le déclenchement de la compaction serveur est purement réactif : c'est
l'upstream qui fait foi via son 400-overflow, PAS une estimation locale
ni une table de context_window par modèle (voir plan : aucun context_window
dans config/ ni config.yaml — et c'est très bien ainsi).

Règle : status == 400 ET un marqueur d'overflow présent dans le body
(insensible à la casse). Les marqueurs par défaut couvrent les formulations
usuelles (OpenAI/Anthropic) ; extensibles via
config.yaml:server_compaction.overflow_markers.
"""

from __future__ import annotations

import re

# Marqueurs par défaut (lowercase comparé) — formulations vues chez les
# upstreams OpenAI/Anthropic + variantes génériques. Conservés pour la
# compatibilité config (`overflow_markers`) et les tests historiques.
_DEFAULT_OVERFLOW_MARKERS = (
    "prompt too long",
    "is too long",  # prompt/input/request is too long (sans "took too long" des timeouts)
    "context_length_exceeded",
    "context window",
    "too many tokens",
    "maximum context",
    "context limit",
    "token limit",
    "input too long",
    "out of context",
)

# [audit 2026-10-30] Portage des regex du client officiel
# (`provider-error.ts:16-54`) : les marqueurs texte ci-dessus ne couvraient
# que 10 formulations sur ~35. Un overflow non reconnu = pas de compaction
# réactive, donc le client relançait le même contexte en boucle.
_OFFICIAL_OVERFLOW_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"prompt is too long",
        r"input is too long for requested model",
        r"exceeded this model context window limit",
        r"exceeds the context window",
        r"exceeds (?:the )?(?:model'?s )?maximum context length(?: of [\d,]+ tokens?|\s*\([\d,]+\))?",
        r"input token count.*exceeds the maximum",
        r"number of input tokens exceeds maximum length",
        r"tokens in request more than max tokens allowed",
        r"maximum prompt length is \d+",
        r"reduce the length of the messages",
        r"requested input length \d+ exceeds maximum input length",
        r"maximum context length is \d+ tokens",
        r"exceeds (?:the )?maximum allowed input length of [\d,]+ tokens?",
        r"input(?: \(\d+ tokens\))? is longer than the model'?s context length",
        r"exceeds the limit of \d+",
        r"exceeds the available context size",
        r"greater than the context length",
        r"`inputs` tokens \+ `max_new_tokens` must be <= \d+",
        r"context window exceeds limit",
        r"exceeded model token limit",
        r"context[_ ]length[_ ]exceeded",
        r"context length is only \d+ tokens",
        r"input length.*exceeds.*context length",
        r"prompt (?:too long|超长)",
        r"too large for model with \d+ maximum context length",
        r"prompt has [\d,]+ tokens?, but the configured context size is [\d,]+ tokens?",
        r"model_context_window_exceeded",
        r"range of input length should be",
        r"too many tokens",
        r"token limit exceeded",
        r"request_too_large",
    )
)

# Le client officiel exclut les erreurs de débit et les plafonds de SORTIE :
# compacter ne les corrige pas (et masquerait un vrai 429/limite max_tokens).
_OVERFLOW_EXCLUSIONS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^(throttling error|service unavailable):",
        r"rate limit",
        r"too many requests",
        r"max[_ ]tokens must be less than",
    )
)


def _norm_markers(markers) -> tuple[str, ...]:
    """Normalise une liste de marqueurs custom → tuple lowercase non-vide."""
    if not isinstance(markers, (list, tuple)):
        return _DEFAULT_OVERFLOW_MARKERS
    cleaned = [m.lower() for m in markers if isinstance(m, str) and m.strip()]
    return tuple(cleaned) if cleaned else _DEFAULT_OVERFLOW_MARKERS


def is_overflow(status_code, body_text, markers=None) -> bool:
    """True si (status 400 + marqueur d'overflow dans le body).

    status_code : int (ou str convertible) — seul 400 compte (le 413 proxy
    a son format propre et n'est jamais un overflow modèle).
    body_text : str/bytes/None — corps upstream (tronqué OK, matcher substring).
    markers : liste custom optionnelle (config overflow_markers), sinon défauts.
    """
    try:
        try:
            status = int(status_code)
        except (TypeError, ValueError):
            return False
        # [audit 2026-10-30] Some upstreams signal a context overflow with
        # 413/422. Keep the marker requirement so a generic 413 (body size)
        # is not mistaken for an overflow.
        if status not in (400, 413, 422):
            return False
        if isinstance(body_text, bytes):
            try:
                body_text = body_text.decode("utf-8", errors="replace")
            except Exception:
                return False
        if body_text is None:
            # Parité officielle : `400/413 (no body)` est un overflow.
            return status in (400, 413)
        if not isinstance(body_text, str):
            return False
        if not body_text:
            return status in (400, 413)
        low = body_text.lower()
        custom = isinstance(markers, (list, tuple)) and any(
            isinstance(m, str) and m.strip() for m in markers
        )
        if custom:
            for m in _norm_markers(markers):
                if m in low:
                    return True
            return False
        if any(pattern.search(body_text) for pattern in _OVERFLOW_EXCLUSIONS):
            return False
        if any(pattern.search(body_text) for pattern in _OFFICIAL_OVERFLOW_PATTERNS):
            return True
        # Filet de compatibilité : marqueurs historiques non couverts par les
        # regex officielles (configs/tests existants).
        for m in _DEFAULT_OVERFLOW_MARKERS:
            if m in low:
                return True
        return False
    except Exception:
        return False


def huge_context_check(body_bytes_len, est_tokens, *, warn_tokens=0, deny_tokens=0):
    """Garde pré-forward optionnelle (opt-in, tout à 0 = inactive).

    Retourne (level, message) : level None (passe), "warn" (log + continue)
    ou "deny" (refuser par 413 actionnable AVANT l'amont). deny prioritaire.
    Seuils absolus en tokens (pas de table par modèle — doctrine réactive
    préservée par défaut). Ne lève jamais.
    """
    try:
        try:
            n_bytes = int(body_bytes_len or 0)
        except Exception:
            n_bytes = 0
        try:
            est = int(est_tokens or 0)
        except Exception:
            est = 0
        try:
            warn = int(warn_tokens or 0)
        except Exception:
            warn = 0
        try:
            deny = int(deny_tokens or 0)
        except Exception:
            deny = 0
        if deny > 0 and (est >= deny or n_bytes >= deny * 4):
            return (
                "deny",
                f"Context too large (est ~{est} tokens > {deny}): compact the session "
                "(/compact), start fresh (/clear), or shrink tool outputs, then retry.",
            )
        if warn > 0 and (est >= warn or n_bytes >= warn * 4):
            return (
                "warn",
                f"huge context (est ~{est} tokens >= {warn}): upstream may 400/overflow — consider /compact",
            )
        return (None, "")
    except Exception:
        return (None, "")


__all__ = ["is_overflow", "_DEFAULT_OVERFLOW_MARKERS", "huge_context_check"]
