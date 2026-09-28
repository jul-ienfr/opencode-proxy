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


__all__ = ["is_overflow", "_DEFAULT_OVERFLOW_MARKERS"]
