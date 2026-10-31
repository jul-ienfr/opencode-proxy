"""upstream.retry — helper retry centralisé (P0-5).

Remplace les ``sleep()`` magiques dispersés (``sleep(60)``,
``sleep(1)``, backoff paid inline) par UNE politique unique :

* honore le ``Retry-After`` amont (secondes OU date HTTP) quand il est
  exploitable, borné à ``max_s`` (au-delà → backoff local, on ne bloque
  jamais un worker des minutes sur une consigne farfelue) ;
* sinon backoff exponentiel ``base * 2**attempt`` ;
* jitter ``±25 %`` systématique (parité SDK, anti-thundering-herd).

Module PUR (stdlib seul) : AUCUN import du projet — mêmes règles que
``upstream.breaker`` / ``upstream.clients``. L'hôte (``opencode.py``)
possède les seuils (lus depuis ``config.yaml``) et les passe en paramètres.
"""

from __future__ import annotations

import datetime as _dtm
import email.utils as _eu
import random


def parse_retry_after(value: object, max_s: float = 900.0) -> float | None:
    """Parse un header ``Retry-After`` → secondes d'attente, ou ``None``.

    Accepte les secondes (``"120"``, ``120``) et les dates HTTP
    (``email.utils.parsedate_to_datetime``). Retourne ``None`` si absent,
    illisible, ``<= 0`` ou ``> max_s`` (consigne inexploitable → backoff
    local). ``max_s`` borne le blocage worker (parité officielle v2 :
    `retryAfterMs` borné 15 min = 900 s ; paid historique 600 relevé).
    """
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text:
        return None
    delay: float | None = None
    try:
        delay = float(text)
    except (TypeError, ValueError):
        delay = None
        try:
            _dt = _eu.parsedate_to_datetime(text)
            _now = _dtm.datetime.now(tz=_dt.tzinfo) if _dt.tzinfo else _dtm.datetime.utcnow()
            delay = max(0.0, (_dt - _now).total_seconds())
        except Exception:
            return None
    try:
        delay_f = float(delay)
    except (TypeError, ValueError):
        return None
    if not 0 < delay_f <= float(max_s):
        return None
    return delay_f


def compute_retry_delay(
    attempt: int,
    retry_after: float | None = None,
    base: float = 1.0,
    cap: float = 600.0,
    jitter: bool = True,
) -> float:
    """Délai avant ré-essai ``attempt`` (0-based) : ``Retry-After`` si fourni,
    sinon ``base * 2**attempt`` plafonné à ``cap``. Jitter ``×[0.75, 1.25)``
    sauf ``jitter=False`` (tests déterministes). Retourne toujours ``>= 0``.
    """
    try:
        n = int(attempt)
    except (TypeError, ValueError):
        n = 0
    if n < 0:
        n = 0
    if retry_after is not None:
        try:
            wait = float(retry_after)
        except (TypeError, ValueError):
            wait = base * (2**n)
    else:
        wait = base * (2**n)
    try:
        cap_f = float(cap)
    except (TypeError, ValueError):
        cap_f = 600.0
    if cap_f > 0:
        wait = min(wait, cap_f)
    if wait < 0:
        wait = 0.0
    if jitter:
        try:
            wait = wait * (0.75 + random.random() * 0.5)
        except Exception:
            pass
    return wait


# ─────────── Statuts rejouables (parité client officiel 1.19.3) ───────────
#
# Source : `packages/llm/src/route/executor.ts` (client officiel) —
# `retryableStatus()` renvoie vrai pour 429, 503, 504, 529 **et tout statut
# ≥ 500**. Tout le reste (400, 401, 403, 404, 409, 413, 422, 425…) est classé
# `InvalidRequest` et n'est JAMAIS rejoué : réémettre une requête que l'amont
# a déclarée invalide ne peut que reproduire l'erreur, en consommant du quota
# et de la latence.
#
# Écarts volontaires par rapport à ce que le proxy faisait avant :
#   * 409 (Conflict) était rejoué → non rejoué (l'officiel le classe
#     InvalidRequest) ;
#   * 408 (Request Timeout), 425 (Too Early) et 499 (client closed) étaient
#     rejoués → non rejoués ici : ce sont des conditions de *transport*, pas
#     des surcharges amont, et le proxy a ses propres reprises de transport ;
#   * 429 était absent de l'ensemble « 5xx » et n'était rejoué que par les
#     chemins free dédiés → désormais rejouable partout, comme l'officiel ;
#   * 501/505+ (5xx hors liste historique) étaient ignorés → rejoués, comme
#     tout 5xx chez l'officiel.
RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 503, 504, 529} | set(range(500, 600)))


def is_retryable_status(status: object) -> bool:
    """Vrai si le statut justifie une reprise (parité client officiel).

    Pur, sans raise : appelé sur le chemin de la requête. Un statut illisible
    n'est pas rejouable — mieux vaut rendre l'erreur que boucler dessus.
    """
    try:
        code = int(status)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return False
    return code in RETRYABLE_STATUSES


# ─────────── Budget de reprises (PLAN_ALIGNEMENT_CLIENT_OFFICIEL G2) ───────────

# Plafond du budget. La jambe libre DÉRIVE sa borne de ce budget
# (``max(budget, len(API_KEYS))`` + coups libres) : une valeur démesurée en
# config gonflerait le nombre de connexions amont par requête client sans que
# personne ne l'observe. Le plan vise 3-4 tentatives si les 502 le justifient —
# 10 laisse une marge très large sans sortir du plausible.
RETRY_BUDGET_MAX = 10


def resolve_max_retries(raw: object, default: int = 2) -> int:
    """Normalise un budget de reprises lu en config → entier dans ``[1, RETRY_BUDGET_MAX]``.

    Pur et sans raise : appelé sur la valeur issue de ``config.yaml`` (donc du
    disque, éditable à chaud) par l'hôte, sur le chemin de la requête.

    Ordre de décision, et POURQUOI :

    * ``None`` / clé absente → ``default`` (le budget d'aujourd'hui) ;
    * **booléen** → ``default`` : YAML 1.1 lit ``yes`` / ``on`` en ``True``, et
      ``True`` est un ``int`` en Python — le prendre pour ``1`` transformerait
      un « active les reprises » tapé trop vite en « une seule tentative » ;
    * entier / flottant / chaîne numérique → la valeur, **bornée** ;
    * ``< 1`` → ``1`` : aujourd'hui ``retry_attempts: 0`` fait
      ``while attempt < 0`` / ``range(0)`` — la requête client échouerait SANS
      qu'aucun appel amont ne parte. Une tentative minimum est le seul
      comportement défendable ;
    * tout le reste (dict, liste, objet, texte non numérique) → ``default`` :
      mieux vaut l'ancien budget, connu, qu'une valeur qu'on ne comprend pas.
    """
    try:
        fallback = int(default)
    except (TypeError, ValueError):
        fallback = 2
    if isinstance(fallback, bool):  # pragma: no cover — garde défensive
        fallback = 2
    if fallback < 1:
        fallback = 1
    elif fallback > RETRY_BUDGET_MAX:
        fallback = RETRY_BUDGET_MAX

    if raw is None or isinstance(raw, bool):
        return fallback

    value: int
    if isinstance(raw, int):
        value = int(raw)
    elif isinstance(raw, float):
        if raw != raw or raw in (float("inf"), float("-inf")):
            return fallback
        value = int(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return fallback
        try:
            value = int(float(text))
        except (TypeError, ValueError):
            return fallback
    else:
        return fallback

    if value < 1:
        return 1
    if value > RETRY_BUDGET_MAX:
        return RETRY_BUDGET_MAX
    return value


__all__ = [
    "RETRY_BUDGET_MAX",
    "RETRYABLE_STATUSES",
    "compute_retry_delay",
    "is_retryable_status",
    "parse_retry_after",
    "resolve_max_retries",
]
