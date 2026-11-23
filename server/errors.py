"""server.errors — adaptateur HTTP des erreurs (regle XIV).

[P1 2026-11-20] T127 — extraction depuis ``core/errors.py``.

``core/errors.py`` est le module DOMAINE : hierarchie d'exceptions et
classement purs, aucun framework web. La construction des ``JSONResponse``
(FastAPI/Starlette) et la mutation d'en-tetes HTTP vivaient dans ce module
domaine, ce qui violait la regle XIV *et* faisait payer ``fastapi.responses``
(~374 ms mesures) a tout importateur de ``core.errors`` (``free/``,
``upstream/``, ``core.lazy`` via ``core/__init__``).

Ce module est l'ADAPTATEUR : il importe la logique PURE depuis
``core.errors`` et la traduit en reponse HTTP. Aucun etat, aucune I/O
reseau — seulement domaine -> wire.

Compatibilite : les noms restent importables depuis ``core.errors`` (via son
``__getattr__`` PEP 562, paresseux et memoise) pour ne casser aucun appelant
existant (``opencode.py``, ``server/throttle.py``, tests). Le sens des
nouveaux imports est ``server.errors``.
"""

from __future__ import annotations

import email.utils

from fastapi.responses import JSONResponse

from core.errors import FreeRefusal, classify_upstream_error, is_free_quota_failure


def anthropic_error(
    status_code: int,
    message: str,
    error_type: str = "api_error",
    *,
    reason: str = "",
) -> JSONResponse:
    """Return an error in Anthropic Messages API format."""
    error: dict[str, object] = {"type": error_type, "message": message}
    if reason:
        error["reason"] = reason
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": error},
    )


# Alias historique (opencode._anthropic_error).
_anthropic_error = anthropic_error


def openai_error(
    status_code: int,
    message: str,
    error_type: str = "invalid_request_error",
    *,
    reason: str = "",
) -> JSONResponse:
    """Return an error in OpenAI API format."""
    error: dict[str, object] = {
        "message": message,
        "type": error_type,
        "code": str(status_code),
    }
    if reason:
        error["reason"] = reason
    return JSONResponse(
        status_code=status_code,
        content={"error": error},
    )


# Alias historique (opencode._openai_error — appele directement par tests).
_openai_error = openai_error


def model_deprecated_error(
    model_id: str,
    protocol: str = "anthropic",
    *,
    available: list[str] | None = None,
    status_code: int = 410,
) -> JSONResponse:
    """[LIVE-MODELS] Refus explicite d'un modele retire du catalogue vivant.

    Le client qui utilisait ce modele doit apprendre *pourquoi* il disparait,
    pas recevoir un 404 nu. On garde le format de son protocole (Anthropic ou
    OpenAI) et on nomme les modeles encore actifs quand ils sont connus.
    """
    model = str(model_id or "")
    message = (
        f'Model "{model}" is deprecated or no longer served by OpenCodeProxy. '
        "Use GET /v1/models to choose an active model."
    )
    if available:
        sample = ", ".join(str(x) for x in available[:8])
        if sample:
            message += f" Active models include: {sample}."
    if str(protocol).lower() == "openai":
        return openai_error(status_code, message, error_type="model_deprecated")
    return anthropic_error(status_code, message, error_type="not_found_error")


def free_refusal_response(
    exc: FreeRefusal,
    protocol: str,
    *,
    anthropic_error_fn=anthropic_error,
    openai_error_fn=openai_error,
    redact_fn,
    reason: str = "",
    on_429=None,
) -> JSONResponse:
    """[PLAN_CORRECTION_FAUX_429 Lot A1] Reponse HTTP veridique (non-stream).

    - status == 429 -> 429 quota, message historique, Retry-After = header
      upstream REEL uniquement (omis si absent — jamais 60/120 invente).
    - sinon -> statut upstream relaye (503 -> 503), type deduit du corps par
      ``classify_upstream_error`` (``api_error`` par defaut, comme avant),
      message ``Free model request failed with status {s}: {body}``
      (miroir exact de ``_free_stream_refuse_bytes``), Retry-After
      propage uniquement si present.
    """
    try:
        status = int(getattr(exc, "status", 429))
    except (TypeError, ValueError):
        status = 429
    if not 100 <= status <= 599:
        status = 502
    retry_after = (getattr(exc, "retry_after", "") or "").strip()
    # Valide : secondes ou date HTTP, sinon on omet (jamais invente)
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
    # [fix faux-429] Un 429 amont n'est PAS toujours un quota epuise : OpenCode
    # renvoie aussi 429 « Endpoint is unavailable » (surcharge serveur,
    # transitoire). On regarde le VRAI corps AVANT d'affirmer « quota epuise »
    # (et de brandir rate_limit_error) — sinon on ment au client sur une panne
    # serveur. Le message « quota epuise » n'est emis que pour un VRAI quota.
    _fb_body = getattr(exc, "body", "") or ""
    if status == 429 and is_free_quota_failure(status, _fb_body):
        if on_429 is not None:
            try:
                on_429(reason)
            except Exception:
                pass
        if _ra_out:
            _msg = f"Free quota exhausted on all VPN stations. Retry after {_ra_out}s."
        else:
            _msg = "Free quota exhausted on all VPN stations."
        reason_kw = {"reason": reason} if reason else {}
        if protocol == "anthropic":
            resp = anthropic_error_fn(429, _msg, error_type="rate_limit_error", **reason_kw)
        else:
            resp = openai_error_fn(429, _msg, error_type="rate_limit_error", **reason_kw)
        if reason:
            resp.headers["X-Free-Reason"] = reason
        if _ra_out:
            resp.headers["Retry-After"] = _ra_out
        return resp
    _body_txt = redact_fn(getattr(exc, "body", "") or "", 300)
    _msg = f"Free model request failed with status {status}: {_body_txt}"
    # [G1] Le message porte deja le corps amont borne : on le fait passer par
    # le classement pour qu'un overflow (« context_length_exceeded ») remonte
    # en `invalid_request_error` — le client compacte alors — au lieu du
    # fourre-tout `api_error` qu'il ne sait pas interpreter.
    _st, _etype = classify_upstream_error(status, _msg)
    # [fix 2026-11-21] Ne jamais relayer un 401/403 amont brut : le client
    # (Claude Code) ouvrirait sa fenêtre d'authentification alors que la cause
    # est côté proxy. Mêmes règles que ``opencode._safe_client_status``
    # (401/403 -> 503, 499 -> 502).
    if _st in (401, 403):
        _st = 503
    elif _st == 499:
        _st = 502
    if protocol == "anthropic":
        resp = anthropic_error_fn(_st, _msg, error_type=_etype)
    else:
        resp = openai_error_fn(_st, _msg, error_type=_etype)
    if _ra_out:
        resp.headers["Retry-After"] = _ra_out
    return resp


__all__ = [
    "_anthropic_error",
    "_openai_error",
    "anthropic_error",
    "free_refusal_response",
    "model_deprecated_error",
    "openai_error",
]
