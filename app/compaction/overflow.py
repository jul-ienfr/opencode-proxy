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

# Marqueurs par défaut (lowercase comparé) — formulations vues chez les
# upstreams OpenAI/Anthropic + variantes génériques.
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
    "max_tokens",
    "out of context",
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
        if status != 400:
            return False
        if isinstance(body_text, bytes):
            try:
                body_text = body_text.decode("utf-8", errors="replace")
            except Exception:
                return False
        if not isinstance(body_text, str) or not body_text:
            return False
        low = body_text.lower()
        for m in _norm_markers(markers):
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
            return ("deny",
                    f"Context too large (est ~{est} tokens > {deny}): compact the session "
                    "(/compact), start fresh (/clear), or shrink tool outputs, then retry.")
        if warn > 0 and (est >= warn or n_bytes >= warn * 4):
            return ("warn",
                    f"huge context (est ~{est} tokens >= {warn}): upstream may 400/overflow — "
                    "consider /compact")
        return (None, "")
    except Exception:
        return (None, "")


__all__ = ["is_overflow", "_DEFAULT_OVERFLOW_MARKERS", "huge_context_check"]
