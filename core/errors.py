"""core.errors — erreurs + helpers de réponse standardisés (Phase 9 refonte).

Déplacement PUR depuis ``opencode.py`` (§ error helpers + faux-429 Lot A).
AUCUN import du projet (``fastapi.responses`` seul) :

* ``UpstreamError``, ``FreeRefusal``, ``FreeQuotaExhausted`` : classes pures ;
* ``anthropic_error`` / ``openai_error`` : purs ;
* ``free_refusal_response`` : ``anthropic_error_fn`` / ``openai_error_fn`` /
  ``redact_fn`` injectés (l'hôte possède ``_redact`` — politique de
  redaction des corps) ; le wrapper hôte ``_free_refusal_response``
  conserve la signature (patch-visibility + zéro churn aux ~10 sites) ;
* ``classify_upstream_error`` [G1] : purs.
"""

from __future__ import annotations

import email.utils
import re

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


class ModelOutOfScope(Exception):
    """[PLAN N3] Id hors du périmètre de fourniture du proxy.

    Le proxy ne fournit que les modèles servis par un provider opencode
    (`config.models_catalog.is_servable`), plus les deux ids internes
    `jev-1.13*`. Tout le reste est hébergé par un provider **tiers** dont la
    route n'est pas exposée par la passerelle : le refus doit être explicite
    plutôt que de laisser le repli implicite inventer `zen/go/v1`.

    `reason` porte la phrase complète — id, hôtes amont, `npm` de chacun — pour
    que le client puisse corriger sa demande au lieu de lire un 404 nu.
    """

    def __init__(self, model_id: str, reason: str):
        try:
            model_id = str(model_id or "")
        except Exception:
            model_id = ""
        super().__init__(reason or f'model "{model_id}" is not provided by this proxy')
        self.model_id = model_id
        self.reason = reason or str(self)


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
    - sinon → statut upstream relayé (503 → 503), type déduit du corps par
      ``classify_upstream_error`` (``api_error`` par défaut, comme avant),
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
    # [fix faux-429] Un 429 amont n'est PAS toujours un quota épuisé : OpenCode
    # renvoie aussi 429 « Endpoint is unavailable » (surcharge serveur,
    # transitoire). On regarde le VRAI corps AVANT d'affirmer « quota épuisé »
    # (et de brandir rate_limit_error) — sinon on ment au client sur une panne
    # serveur. Le message « quota épuisé » n'est émis que pour un VRAI quota.
    _fb_body = getattr(exc, "body", "") or ""
    if status == 429 and is_free_quota_failure(status, _fb_body):
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
    # [G1] Le message porte déjà le corps amont borné : on le fait passer par
    # le classement pour qu'un overflow (« context_length_exceeded ») remonte
    # en `invalid_request_error` — le client compacte alors — au lieu du
    # fourre-tout `api_error` qu'il ne sait pas interpréter.
    _st, _etype = classify_upstream_error(status, _msg)
    if protocol == "anthropic":
        resp = anthropic_error_fn(_st, _msg, error_type=_etype)
    else:
        resp = openai_error_fn(_st, _msg, error_type=_etype)
    if _ra_out:
        resp.headers["Retry-After"] = _ra_out
    return resp


# ─────────── [PLAN_AUDIT_CLIENT_OFFICIEL G1] classement des erreurs amont ───────────

# Le client officiel ne se fie pas au seul statut HTTP : il lit le CORPS de
# l'erreur amont pour décider du traitement
# (`packages/opencode/src/provider/error.ts:102-154`, bibliothèque de motifs
# `packages/llm/src/provider-error.ts:4-38`). Sans ça, un overflow de contexte
# remonte en ``api_error`` — type fourre-tout que le client ne sait pas
# interpréter — et il ne déclenche pas SA compaction.
#
# Chaque entrée : (motif regex, type d'erreur Anthropic, rejouable, pause de
# clé). Le PREMIER motif trouvé gagne : les plus spécifiques d'abord.
_UPSTREAM_MOTIFS: tuple = (
    # overflow de contexte → le client compacte. Non rejouable chez nous (la
    # compaction réactive a déjà eu sa chance) et SANS pause de clé : la clé
    # n'a rien fait, c'est la conversation qui est trop longue.
    (r"context_length_exceeded", "invalid_request_error", False, False),
    # quota / crédit épuisé → problème de COMPTE, pas de réseau : ça ne revient
    # pas tout seul, donc pas de rejeu, mais une pause longue côté hôte
    # (parité du site historique « 400 credit error »). Les deux motifs
    # officiels + les deux textes déjà reconnus par le proxy, une seule table.
    (r"insufficient_quota", "permission_error", False, True),
    (r"usage_not_included", "permission_error", False, True),
    (r"insufficient balance", "permission_error", False, True),
    (r"monthly usage limit", "permission_error", False, True),
    # prompt refusé par la politique amont → requête à corriger. Non-retry
    # comme les deux précédents, mais PAS de pause de clé : punir toutes les
    # clés parce qu'un prompt est invalide provoquerait une panne auto-infligée.
    (r"invalid_prompt", "invalid_request_error", False, False),
    # surcharge amont → transitoire, ça se rejoue. Inclut les textes OpenCode
    # « Endpoint is unavailable » : 429 transitoire côté SERVEUR, sans lien avec
    # le quota per-IP — rotater l'IP ne change rien (cf. is_free_quota_failure).
    (r"server_is_overloaded", "overloaded_error", True, False),
    (r"endpoint is unavailable", "overloaded_error", True, False),
    # « unavailable » / « server_error » génériques VOLONTAIREMENT absents :
    # un 503 « Service Unavailable » ordinaire doit rester api_error (contrat
    # épinglé test_corps_sans_motif). Seul le texte OpenCode précis est classé.
)

# Compilation unique à l'import. `\b` des deux côtés : « server_error » ne doit
# pas matcher « server_errors_total » ni « not_server_error », et les motifs
# officiels sont en snake_case, donc délimités par de la ponctuation JSON.
_MOTIF_RES: tuple = tuple(
    (re.compile(r"\b" + motif + r"\b", re.IGNORECASE), etype, retryable, pause)
    for motif, etype, retryable, pause in _UPSTREAM_MOTIFS
)


def _match_upstream_motif(body: object):
    """Premier motif officiel trouvé dans ``body`` → ``(type, rejouable, pause)``.

    Pur, jamais de raise : appelé sur du texte réseau (corps d'erreur amont,
    message d'exception). Rend ``None`` si aucun motif ne correspond — dans ce
    cas l'appelant garde son comportement par défaut (``api_error``).
    """
    if not isinstance(body, str) or not body:
        return None
    try:
        for rx, etype, retryable, pause in _MOTIF_RES:
            if rx.search(body):
                return etype, retryable, pause
    except Exception:
        return None
    return None


def classify_upstream_error(status: object, body: object) -> tuple:
    """[G1] ``(status, error_type)`` — statut inchangé, type déduit du CORPS.

    Le statut n'est jamais réécrit ici : les règles de fenêtre d'auth
    (401/403 → 503) et de parité (429 → 429, 529 → 529) restent la propriété
    de l'hôte (``opencode._safe_client_status``). Seul le TYPE change, et
    uniquement quand l'amont est explicite — un corps sans motif continue de
    sortir en ``api_error`` exactement comme avant.
    """
    try:
        status_i = int(status)
    except (TypeError, ValueError):
        status_i = 502
    _m = _match_upstream_motif(body)
    if _m is None:
        return status_i, "api_error"
    return status_i, _m[0]


def upstream_retry_signal(status: object, body: object) -> tuple:
    """[G1] ``(rejouable, pause_de_cle)`` pour un corps d'erreur amont.

    - rejouable : ``server_is_overloaded`` / ``server_error`` — transitoire,
      ça se rejoue (parité officielle).
    - pause_de_cle : quota/crédit épuisé — l'hôte décide de la DURÉE
      (``_key_pauser._max_pause`` chez nous) ; le module reste pur et ne
      connaît pas le politique de pause.
    """
    try:
        int(status)
    except (TypeError, ValueError):
        return False, False
    _m = _match_upstream_motif(body)
    if _m is None:
        return False, False
    return _m[1], _m[2]


def is_free_quota_failure(status: object, body: object) -> bool:
    """[fix faux-429 + anti-rotation-storm] Un échec free est-il un VRAI
    quota per-IP (rotation d'IP utile) ou une erreur serveur transitoire
    (rotation INUTILE — l'IP neuve serait rejetée pareil) ?

    OpenCode répond parfois 429 « Endpoint is unavailable » (surcharge
    serveur, transitoire) SANS lien avec le quota par IP. Le proxy lisait
    tout statut 429 comme un quota épuisé : il posait le faux message
    « Free quota exhausted on all VPN stations. » ET déclenchait une rotation
    d'IP (`on_429_action: both`) — consommant des IP neuves + des handshakes
    AUTH NordVPN à chaque occurrence. Rafale → throttling compte NordVPN →
    stations en erreur → boucle auto-destructrice.

    On ne classe « quota » QUE si le CORPS porte un motif de quota/crédit
    épuisé (`usage_not_included`, `insufficient_quota`, … → pause_de_cle dans
    la table officielle) OU une limite de débit explicite. Tout 429 sans ce
    motif (dont « Endpoint is unavailable » → `overloaded_error`) est
    transitoire : pas de rotation, message véridique côté client.

    Pur : jamais de raise. Un statut hors 429 rend toujours False (ce helper
    ne qualifie que les 429).
    """
    try:
        s = int(status)
    except (TypeError, ValueError):
        return False
    if s != 429:
        return False
    # Quota/crédit épuisé = pause_de_cle dans la table de motifs officielle.
    try:
        _replayable, _pause = upstream_retry_signal(s, body)
    except Exception:
        _pause = False
    if _pause:
        return True
    # Limite de débit explicite (429 « rate limit » lisible) sans être un
    # épuisement de crédit : reconnue comme quota (rotation d'IP utile).
    if isinstance(body, str) and body:
        low = body.lower()
        if "rate limit" in low or "rate_limit" in low or "too many requests" in low:
            return True
    return False


def is_credit_exhaustion(body: object) -> bool:
    """[G1] Vrai si le corps porte un motif de quota/crédit épuisé.

    Remplace les tests en dur ``any(x in resp.text for x in ("Insufficient
    balance", "Monthly usage limit"))`` dispersés dans les trois sites « 400
    credit error » : les motifs officiels ``insufficient_quota`` /
    ``usage_not_included`` entrent dans la même famille (une seule table, un
    seul endroit à faire évoluer).
    """
    _m = _match_upstream_motif(body)
    return _m is not None and bool(_m[2])


__all__ = [
    "FreeQuotaExhausted",
    "FreeRefusal",
    "ModelOutOfScope",
    "UpstreamError",
    "_anthropic_error",
    "_openai_error",
    "anthropic_error",
    "classify_upstream_error",
    "free_refusal_response",
    "is_credit_exhaustion",
    "is_free_quota_failure",
    "openai_error",
    "upstream_retry_signal",
]
