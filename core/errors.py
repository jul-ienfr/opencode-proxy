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


def model_deprecated_error(
    model_id: str,
    protocol: str = "anthropic",
    *,
    available: list[str] | None = None,
    status_code: int = 410,
) -> JSONResponse:
    """[LIVE-MODELS] Refus explicite d'un modèle retiré du catalogue vivant.

    Le client qui utilisait ce modèle doit apprendre *pourquoi* il disparaît,
    pas recevoir un 404 nu. On garde le format de son protocole (Anthropic ou
    OpenAI) et on nomme les modèles encore actifs quand ils sont connus.
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

# [V5.2] Vocabulaires d'erreurs acceptés pour un relais « tel quel ».
# Anthropic et OpenAI ne partagent pas leurs noms de type : `server_error` est
# un type OpenAI, absent du vocabulaire Anthropic (qui dit `api_error` pour un
# 5xx). Relayer un type étranger au client n'est pas de la fidélité — c'est un
# nom qu'il ne sait pas interpréter. On filtre donc par vocabulaire du CLIENT.
_ANTHROPIC_ERROR_TYPES: frozenset = frozenset(
    {
        "api_error",
        "authentication_error",
        "billing_error",
        "invalid_request_error",
        "not_found_error",
        "overloaded_error",
        "permission_error",
        "rate_limit_error",
        "request_too_large",
        "timeout_error",
    }
)
_OPENAI_ERROR_TYPES: frozenset = frozenset(
    {
        "api_error",
        "authentication_error",
        "context_length_exceeded",
        "insufficient_quota",
        "invalid_request_error",
        "not_found_error",
        "overloaded_error",
        "permission_error",
        "rate_limit_error",
        "rate_limit_exceeded",
        "server_error",
        "timeout_error",
    }
)

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
    # Batterie élargie v2 (`provider-error.ts`) : le gateway Zen masque
    # parfois le code sous `[code]` (extrait avant match, cf. _strip_gateway_label).
    (r"context_length_exceeded", "invalid_request_error", False, False),
    (r"maximum context", "invalid_request_error", False, False),
    (r"context window", "invalid_request_error", False, False),
    (r"too many tokens", "invalid_request_error", False, False),
    (r"payload.too.large|payload too large|request too large", "invalid_request_error", False, False),
    (r"model maximum context|context.*exceeded|exceed.*context", "invalid_request_error", False, False),
    # quota / crédit épuisé → problème de COMPTE, pas de réseau : ça ne revient
    # pas tout seul, donc pas de rejeu, mais une pause longue côté hôte.
    # Parité v2 : 6 codes officiels + textes historiques du proxy.
    (r"insufficient_quota", "permission_error", False, True),
    (r"usage_not_included", "permission_error", False, True),
    (r"billing_error|billing.*error|out of credits|no credits", "permission_error", False, True),
    (r"gousagelimiterror|go.*usage.*limit", "permission_error", False, True),
    (r"freeusagelimiterror|free.*usage.*limit", "permission_error", False, True),
    (r"creditlimitexceeded|credit.*limit.*exceed", "permission_error", False, True),
    (r"insufficient balance", "permission_error", False, True),
    (r"monthly usage limit", "permission_error", False, True),
    # auth : clé invalide / permission refusée → pas de retry, pause longue
    # (clé condamnée au 401/403). 400+`incorrect api key` inclus (officiel).
    (r"incorrect api key|invalid api key|api_key_invalid|invalid_api_key", "authentication_error", False, True),
    (r"permission_denied|permission denied|forbidden.*key", "permission_error", False, True),
    # prompt refusé par la politique amont → requête à corriger. Non-retry
    # comme les deux précédents, mais PAS de pause de clé : punir toutes les
    # clés parce qu'un prompt est invalide provoquerait une panne auto-infligée.
    # Parité v2 `ContentPolicy` → mappé `invalid_request_error` (non-retry).
    (r"invalid_prompt", "invalid_request_error", False, False),
    (r"content_policy|usage.?policy|policy violation|blocked.*policy|harmful content", "invalid_request_error", False, False),
    # surcharge amont → transitoire, ça se rejoue. Inclut les textes OpenCode
    # « Endpoint is unavailable » : 429 transitoire côté SERVEUR, sans lien avec
    # le quota per-IP — rotater l'IP ne change rien (cf. is_free_quota_failure).
    # Parité v2 : `try again`, `at capacity`, 408/409 traités à part dans
    # `classify_upstream_error` (statut), pas ici. `server_error` générique
    # volontairement exclu (contrat : 503 ordinaire reste `api_error`).
    (r"server_is_overloaded", "overloaded_error", True, False),
    (r"endpoint is unavailable", "overloaded_error", True, False),
    (r"try again|at capacity|temporarily unavailable", "overloaded_error", True, False),
    # « unavailable » / « server_error » génériques VOLONTAIREMENT restreints :
    # un 503 « Service Unavailable » ordinaire sans motif précis reste api_error
    # (contrat épinglé test_corps_sans_motif). Seuls les textes ci-dessus classent.
)

# Compilation unique à l'import. `\b(?:…)\b` : chaque alternative est bornée
# (sans groupe, `\bmotif1|motif2\b` ne bornait que les extrémités). « server_error »
# ne doit pas matcher « server_errors_total » ni « not_server_error ».
_MOTIF_RES: tuple = tuple(
    (re.compile(r"\b(?:" + motif + r")\b", re.IGNORECASE), etype, retryable, pause)
    for motif, etype, retryable, pause in _UPSTREAM_MOTIFS
)


def _strip_gateway_label(body: object) -> str:
    """Extrait le label gateway `[code]` que Zen préfixe (`GATEWAY_CODE_LABEL`).

    Zen réécrit les codes mais garde `[code]` en tête de message ; sans
    extraction, tout est masqué en `server_error`. Rend `"<label> <body>"`
    pour que les motifs voient le code d'origine. Pur, jamais de raise.
    """
    if not isinstance(body, str) or not body:
        return ""
    try:
        import re as _re

        m = _re.match(r"\s*\[([A-Za-z0-9_.-]+)\]\s*(.*)", body, re.DOTALL)
        if m:
            return f"{m.group(1)} {m.group(2)}"
    except Exception:
        pass
    return body if isinstance(body, str) else ""


def _match_upstream_motif(body: object):
    """Premier motif officiel trouvé dans ``body`` → ``(type, rejouable, pause)``.

    Pur, jamais de raise : appelé sur du texte réseau (corps d'erreur amont,
    message d'exception). Rend ``None`` si aucun motif ne correspond — dans ce
    cas l'appelant garde son comportement par défaut (``api_error``).
    Le label gateway `[code]` est déplié avant match (parité Zen).

    [V0.5] Les motifs de surcharge sont ignorés quand le corps parle de débit
    (« rate limit », « too many requests ») : le client officiel a exactement
    cette exclusion (`provider-error.ts:34`) pour l'overflow de contexte, et
    notre motif « try again » attrapait « rate limit exceeded, try again later »
    en `overloaded_error`. Un débit n'est pas une surcharge : il se traite en
    `rate_limit_error`, avec le `retry-after` que le client sait lire.
    """
    if not isinstance(body, str) or not body:
        return None
    try:
        hay = _strip_gateway_label(body)
        parle_de_debit = bool(re.search(r"rate limit|too many requests|throttl", hay, re.IGNORECASE))
        for rx, etype, retryable, pause in _MOTIF_RES:
            if rx.search(hay):
                if etype == "overloaded_error" and parle_de_debit:
                    continue
                return etype, retryable, pause
    except Exception:
        return None
    return None


def extract_upstream_error_type(body: object, vocab: str = "anthropic") -> str | None:
    """[V5.2] Type d'erreur **explicite** du corps amont, s'il est utilisable tel quel.

    V5.2 demande qu'un `event: error` amont soit transmis « tel quel (type,
    message) » — pas retraduit. Or ``classify_upstream_error`` ne reconnaît que
    les motifs de surcharge : un amont qui dit clairement
    ``{"error":{"type":"rate_limit_error",…}}`` ressortait en ``api_error``,
    c'est-à-dire une information perdue alors qu'elle était explicite.

    ``vocab`` est le vocabulaire du **client** visé (``"anthropic"`` pour
    ``/v1/messages``, ``"openai"`` pour ``/v1/chat/completions``). Un type
    absent de ce vocabulaire n'est pas relayé : le client ne saurait pas
    l'interpréter, et le proxy perdrait le mapping de parité déjà vérifié
    (``core/errors.py`` motifs → types Anthropic).

    Pur, jamais de raise. Rend ``None`` quand le corps n'offre rien d'exploitable.
    """
    if not isinstance(body, str) or not body.strip():
        return None
    try:
        import json as _json

        data = _json.loads(body)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if not isinstance(err, dict):
        err = data
    etype = err.get("type")
    if not isinstance(etype, str):
        return None
    etype = etype.strip()
    autorises = _OPENAI_ERROR_TYPES if str(vocab).lower() == "openai" else _ANTHROPIC_ERROR_TYPES
    return etype if etype in autorises else None


# ---------------------------------------------------------------------------
# [V0.5] Table de classification de RÉFÉRENCE, copiée du client officiel
# (`packages/llm/src/route/executor.ts:225-275` + `provider-error.ts:4-43`).
#
# Pourquoi la recopier plutôt que la résumer : nos motifs (ci-dessous) sont
# classés par TEXTE, le client officiel est classé par STATUT avec un motif
# secondaire. Les deux ne se recouvrent pas — un 429 « Too many requests »
# tombait chez nous en `api_error` alors que le client officiel en fait un
# rate limit. Cette table rend l'écart VISIBLE et testable.
# ---------------------------------------------------------------------------

# `provider-error.ts:4-32` — 27 motifs d'overflow de contexte.
_CONTEXT_OVERFLOW_PATTERNS: tuple = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"prompt is too long",
        r"request_too_large",
        r"input is too long for requested model",
        r"exceeds the context window",
        r"exceeds (?:the )?(?:model'?s )?maximum context length(?: of [\d,]+ tokens?|\s*\([\d,]+\))?",
        r"input token count.*exceeds the maximum",
        r"tokens in request more than max tokens allowed",
        r"maximum prompt length is \d+",
        r"reduce the length of the messages",
        r"maximum context length is \d+ tokens",
        r"exceeds (?:the )?maximum allowed input length of [\d,]+ tokens?",
        r"input \(\d+ tokens\) is longer than the model'?s context length \(\d+ tokens\)",
        r"exceeds the limit of \d+",
        r"exceeds the available context size",
        r"greater than the context length",
        r"context window exceeds limit",
        r"exceeded model token limit",
        r"context[_ ]length[_ ]exceeded",
        r"request entity too large",
        r"context length is only \d+ tokens",
        r"input length.*exceeds.*context length",
        r"prompt too long; exceeded (?:max )?context length",
        r"too large for model with \d+ maximum context length",
        r"prompt has [\d,]+ tokens?, but the configured context size is [\d,]+ tokens?",
        r"model_context_window_exceeded",
        r"too many tokens",
        r"token limit exceeded",
    )
)
# `provider-error.ts:34` — ces textes parlent de débit, pas de taille.
_CONTEXT_OVERFLOW_EXCLUSIONS: tuple = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (r"^(throttling error|service unavailable):", r"rate limit", r"too many requests")
)

# `executor.ts:91` — statuts que le client officiel rejoue.
_RETRYABLE_STATUSES: frozenset = frozenset({429, 503, 504, 529})

# Classe officielle → type d'erreur émis sur le fil.
# 5xx et inconnu restent `api_error` : c'est le type Anthropic pour un échec
# serveur (`server_error` est un nom OpenAI — cf. extract_upstream_error_type).
_STATUS_CLASS_TO_TYPE: dict = {
    "content_policy": "invalid_request_error",
    "authentication": "authentication_error",
    "permission": "permission_error",
    "quota": "permission_error",
    "rate_limit": "rate_limit_error",
    "invalid_request": "invalid_request_error",
    "context_overflow": "invalid_request_error",
    "provider_internal": "api_error",
    "unknown": "api_error",
}


def is_context_overflow(message: object) -> bool:
    """[V0.5] Miroir de `isContextOverflow` (`provider-error.ts:36-38`).

    L'exclusion est aussi importante que les motifs : « throttling error » ou
    « rate limit » ne disent pas que le prompt est trop long, et les classer en
    overflow ferait compacter le client pour rien.
    """
    if not isinstance(message, str) or not message:
        return False
    if any(rx.search(message) for rx in _CONTEXT_OVERFLOW_EXCLUSIONS):
        return False
    return any(rx.search(message) for rx in _CONTEXT_OVERFLOW_PATTERNS)


def classify_status_class(status: object, body: object = "") -> str:
    """[V0.5] Classe officielle d'un échec amont — `(statut, corps) → classe`.

    Ordre EXACT de `executor.ts:233-274` : le corps « content policy » gagne sur
    tout, puis 401, 403, 429 (quota vs rate limit), 400/404/409/413/422, puis
    ≥500 ou statut rejouable, puis inconnu.

    Rend un nom de classe officiel (`rate_limit`, `invalid_request`, …) —
    jamais un type de fil : la traduction est le travail de
    ``_STATUS_CLASS_TO_TYPE``, et la garder séparée permet de comparer les deux
    vocabulaires sans les confondre.
    """
    try:
        statut = int(status)
    except (TypeError, ValueError):
        statut = 502
    hay = body if isinstance(body, str) else ""
    if not hay and isinstance(body, (bytes, bytearray)):
        hay = bytes(body).decode("utf-8", "replace")
    try:
        if re.search(r"content[-_\s]?policy|content_filter|safety", hay, re.IGNORECASE):
            return "content_policy"
    except Exception:
        pass
    if statut == 401:
        return "authentication"
    if statut == 403:
        return "permission"
    if statut == 429:
        if re.search(r"insufficient[-_\s]?quota|quota[-_\s]?exceeded", hay, re.IGNORECASE):
            return "quota"
        return "rate_limit"
    if statut in (400, 404, 409, 413, 422):
        return "context_overflow" if is_context_overflow(hay) else "invalid_request"
    if statut >= 500 or statut in _RETRYABLE_STATUSES:
        return "provider_internal"
    return "unknown"


def classify_upstream_error(status: object, body: object) -> tuple:
    """[G1 + parité v2] ``(status, error_type)`` — statut inchangé, type déduit du CORPS.

    Le statut n'est jamais réécrit ici : les règles de fenêtre d'auth
    (401/403 → 503) et de parité (429 → 429, 529 → 529) restent la propriété
    de l'hôte (``opencode._safe_client_status``). Seul le TYPE change, et
    uniquement quand l'amont est explicite — un corps sans motif continue de
    sortir en ``api_error`` exactement comme avant.
    Parité v2 : 402 → quota (`permission_error`, non-retryable) même sans
    motif (facturation) ; le label `[code]` est déplié avant match.
    """
    try:
        status_i = int(status)
    except (TypeError, ValueError):
        status_i = 502
    if status_i == 402:
        return status_i, "permission_error"
    _m = _match_upstream_motif(body)
    if _m is None:
        # [V0.5] Aucun motif : on retombe sur la table de RÉFÉRENCE du client
        # officiel, classée par statut. Avant, tout corps sans motif sortait en
        # `api_error` — y compris un 429 « Too many requests » (le client
        # officiel en fait un rate limit) et un 401 sans message (le client
        # officiel en fait une erreur d'authentification). Les motifs gardent la
        # priorité : ils sont plus spécifiques qu'un statut.
        _classe = classify_status_class(status_i, body)
        return status_i, _STATUS_CLASS_TO_TYPE.get(_classe, "api_error")
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


# Durées humaines dans un corps d'erreur 429 (« retry in 3hr 12min »,
# « quota resets in 2 days », « 45min ») — repli quand l'en-tête Retry-After
# est absent (cas réel : 429 trial zen sans header, 17/08). Somme des
# composantes jour/heure/minute rencontrées ; None si aucune ou hors
# (0, 86400]. Pur, jamais de raise. Ne pas appeler sur un corps non-quota :
# une durée y serait décorrelée du quota (l'appelant a déjà classé).
_BODY_DURATION_RES = (
    (re.compile(r"(\d+)\s*d(?:ays?)?\b", re.IGNORECASE), 86400),
    (re.compile(r"(\d+)\s*(?:h(?:ours?)?|hrs?)\b", re.IGNORECASE), 3600),
    (re.compile(r"(\d+)\s*m(?:in(?:utes?)?)?\b", re.IGNORECASE), 60),
)


def parse_body_retry_seconds(body: object) -> float | None:
    """Extrait une durée de cooldown d'un corps d'erreur 429 (secondes).

    Exemples reconnus : ``"retry in 3hr 12min"`` → 11520.0,
    ``"quota resets in 2 days"`` → 172800 → None (plafond dépassé),
    ``"try again in 45min"`` → 2700.0. ``None`` si aucune durée
    parseable ou total hors (0, 86400].
    """
    try:
        if not isinstance(body, str) or not body:
            return None
        total = 0
        for _rx, _mult in _BODY_DURATION_RES:
            for _m in _rx.finditer(body):
                try:
                    total += int(_m.group(1)) * _mult
                except (TypeError, ValueError):
                    continue
        if total <= 0 or total > 86400:
            return None
        return float(total)
    except Exception:
        return None


def free_quota_scope(status: object, body: object) -> str:
    """Scope d'un 429 free : de QUEL quota parle le provider ?

    * ``"trial-exhausted"`` — ``FreeUsageLimitError`` (trial anonyme per-IP
      épuisé, message « Free usage limit reached... », MESURÉ en prod :
      aucun reset temporel annoncé, le bucket ne revient pas tout seul) ;
    * ``"rate-limited"`` — limite de débit explicite ou autre motif quota
      (fenêtres rolling/weekly : un reset existe, durées applicables) ;
    * ``"transient"`` — 429 sans motif quota (surcharge amont) ;
    * ``"unknown"`` — non-429, corps vide ou illisible.

    Pur, jamais de raise. `GoUsageLimitError` (quotas abonnement avec
    fenêtres) reste ``"rate-limited"`` : lui a un reset, contrairement au
    trial.
    """
    try:
        s = int(status)
    except (TypeError, ValueError):
        return "unknown"
    if s != 429:
        return "unknown"
    if not isinstance(body, str) or not body:
        return "unknown"
    try:
        low = body.lower()
        nospace = low.replace(" ", "").replace("_", "").replace("-", "")
        if "freeusagelimiterror" in nospace or "free usage limit" in low:
            return "trial-exhausted"
        if (
            "rate limit" in low
            or "rate_limit" in low
            or "too many requests" in low
            or "ratelimiterror" in nospace
        ):
            return "rate-limited"
        if is_free_quota_failure(status, body):
            return "rate-limited"
    except Exception:
        return "unknown"
    return "transient"


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
    "extract_upstream_error_type",
    "free_refusal_response",
    "free_quota_scope",
    "is_credit_exhaustion",
    "is_free_quota_failure",
    "model_deprecated_error",
    "openai_error",
    "upstream_retry_signal",
]
