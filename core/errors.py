"""core.errors — erreurs + helpers de réponse standardisés (Phase 9 refonte).

Déplacement PUR depuis ``opencode.py`` (§ error helpers + faux-429 Lot A).
AUCUN import du projet (``fastapi.responses`` seul) :

* ``UpstreamError``, ``FreeRefusal``, ``FreeQuotaExhausted`` : classes pures ;
* ``anthropic_error`` / ``openai_error`` : purs ;
* ``free_refusal_response`` : ``anthropic_error_fn`` / ``openai_error_fn`` /
  ``redact_fn`` injectés (l'hôte possède ``_redact`` — politique de
  redaction des corps) ; le wrapper hôte ``_free_refusal_response``
  conserve la signature (patch-visibility + zéro churn aux ~10 sites).
"""

from __future__ import annotations

import email.utils

from fastapi.responses import JSONResponse


class UpstreamError(Exception):
    """Raised when an upstream HTTP request fails (connection, timeout, etc.)."""

    def __init__(self, message: str, status_code: int = 502, original: Exception = None):
        super().__init__(message)
        self.status_code = status_code
        self.original = original


class FreeRefusal(Exception):
    """[PLAN_CORRECTION_FAUX_429 Lot A] Refus free véridique (remplace le faux 429).

    Porte le VRAI statut upstream + le VRAI body tronqué + le VRAI
    Retry-After (jamais inventé). ``status==429`` = vrai quota épuisé ;
    tout autre statut = erreur upstream relayée telle quelle (503 → 503).
    ``FreeQuotaExhausted`` reste comme sous-classe legacy (compat tests /
    call sites) — les handlers doivent catcher ``FreeRefusal``.
    """

    def __init__(self, status: int = 429, body: str = "", retry_after: str = ""):
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = 429
        if not 100 <= status <= 599:
            status = 502
        try:
            body = str(body or "")[:2000]
        except Exception:
            body = ""
        try:
            retry_after = str(retry_after or "")
        except Exception:
            retry_after = ""
        super().__init__(
            f"free refusal status={status} retry-after={retry_after!r} body={body[:120]!r}"
        )
        self.status = status
        self.body = body
        self.retry_after = retry_after


class FreeQuotaExhausted(FreeRefusal):
    """Legacy alias — vrai 429 quota uniquement (status=429).

    Gardé pour compat (tests + anciens raise à 1 arg). Les nouveaux
    refus non-quota lèvent directement ``FreeRefusal(status, body, ...)``.
    """

    def __init__(self, retry_after: str = "", status: int = 429, body: str = ""):
        super().__init__(status=status, body=body, retry_after=retry_after)


def anthropic_error(status_code: int, message: str, error_type: str = "api_error") -> JSONResponse:
    """Return an error in Anthropic Messages API format."""
    return JSONResponse(
        status_code=status_code,
        content={
            "type": "error",
            "error": {"type": error_type, "message": message},
        },
    )


# Alias historique (opencode._anthropic_error).
_anthropic_error = anthropic_error


def openai_error(
    status_code: int, message: str, error_type: str = "invalid_request_error"
) -> JSONResponse:
    """Return an error in OpenAI API format."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {"message": message, "type": error_type, "code": str(status_code)},
        },
    )


# Alias historique (opencode._openai_error — appelé directement par tests).
_openai_error = openai_error


def free_refusal_response(exc: FreeRefusal, protocol: str, *, anthropic_error_fn=anthropic_error, openai_error_fn=openai_error, redact_fn) -> JSONResponse:
    """[PLAN_CORRECTION_FAUX_429 Lot A1] Réponse HTTP véridique (non-stream).

    - status == 429 → 429 quota, message historique, Retry-After = header
      upstream RÉEL uniquement (omis si absent — jamais 60/120 inventé).
    - sinon → statut upstream relayé (503 → 503), type ``api_error``,
      message ``Free model request failed with status {s}: {body}``
      (miroir exact de ``_free_stream_refuse_bytes``), Retry-After
      propagé uniquement si présent.
    """
    try:
        status = int(getattr(exc, "status", 429))
    except (TypeError, ValueError):
        status = 429
    if not 100 <= status <= 599:
        status = 502
    retry_after = (getattr(exc, "retry_after", "") or "").strip()
    # Valide : secondes ou date HTTP, sinon on omet (jamais inventé)
    _ra_out = ""
    if retry_after:
        try:
            float(retry_after)
            _ra_out = retry_after
        except (TypeError, ValueError):
            try:
                email.utils.parsedate_to_datetime(retry_after)
                _ra_out = retry_after
            except Exception:
                _ra_out = ""
    if status == 429:
        if _ra_out:
            _msg = f"Free quota exhausted on all VPN stations. Retry after {_ra_out}s."
        else:
            _msg = "Free quota exhausted on all VPN stations."
        if protocol == "anthropic":
            resp = anthropic_error_fn(429, _msg, error_type="rate_limit_error")
        else:
            resp = openai_error_fn(429, _msg, error_type="rate_limit_error")
        if _ra_out:
            resp.headers["Retry-After"] = _ra_out
        return resp
    _body_txt = redact_fn(getattr(exc, "body", "") or "", 300)
    _msg = f"Free model request failed with status {status}: {_body_txt}"
    if protocol == "anthropic":
        resp = anthropic_error_fn(status, _msg, error_type="api_error")
    else:
        resp = openai_error_fn(status, _msg, error_type="api_error")
    if _ra_out:
        resp.headers["Retry-After"] = _ra_out
    return resp


__all__ = [
    "FreeQuotaExhausted",
    "FreeRefusal",
    "UpstreamError",
    "_anthropic_error",
    "_openai_error",
    "anthropic_error",
    "free_refusal_response",
    "openai_error",
]
