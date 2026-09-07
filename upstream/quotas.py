"""upstream.quotas — cache quotas/429 + watchdog TTFB (Phase 3 refonte).

Déplacement PUR depuis ``opencode.py`` (§ « fetch_quotas sur 429 » +
« Watchdog TTFB paid »). AUCUN import du projet :

* ``fetch_quotas_cached`` : le dict cache + TTL sont passés en paramètre
  (l'hôte possède ``_fetch_quotas_429_cache``), le fetch réel est injecté
  (``fetch_fn`` — l'hôte passe ``dashboard.quota.fetch_quotas`` en lazy,
  comme avant) ;
* ``TTFBWatchdogTimeout`` : signal « aucun byte dans le délai » (l'orchestre
  — stage-1 même-clé-connexion-neuve, stage-2 clé-alt, 504 — RESTE dans les
  chemins requête hôtes, ``test_perf_lot3_regressions.py`` les verrouille) ;
* ``bump_ttfb_failover`` / ``alias_for_api_key`` : compteurs/clés passés en
  paramètre (l'hôte possède ``_TTFB_FAILOVER_COUNTS`` — muté directement par
  les tests — et ``API_KEYS``) ;
* lectures ``config.yaml`` (``ttfb_watchdog.enabled/timeout_s``) : RESTENT
  côté hôte (``_ttfb_watchdog_enabled()`` / ``_ttfb_watchdog_timeout_s()``,
  patchés par tests).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

DEFAULT_FETCH_QUOTAS_429_TTL_S = 30.0
DEFAULT_TTFB_TIMEOUT_S = 90.0
TTFB_TIMEOUT_MIN_S = 5.0
TTFB_TIMEOUT_MAX_S = 600.0


class TTFBWatchdogTimeout(Exception):
    """Aucun byte upstream reçu dans le délai du watchdog (premier byte)."""


# Alias historique (opencode._TTFBWatchdogTimeout).
_TTFBWatchdogTimeout = TTFBWatchdogTimeout


async def fetch_quotas_cached(
    cache: dict[str, tuple[float, dict]],
    wid: str,
    cookie: str,
    fetch_fn: Callable[[str, str], Awaitable[dict]],
    *,
    ttl_s: float = DEFAULT_FETCH_QUOTAS_429_TTL_S,
) -> dict:
    """fetch_quotas avec cache court (30 s) par workspace — anti-burst 429.

    Sans cache, une rafale de 429 sur le même workspace spammait l'endpoint
    de quotas (un fetch par 429). Le reset_in_sec retourné évolue lentement,
    l'erreur induite sur la pause est négligeable et le 429 suivant re-valide
    après expiration. Pas de flag config : pure observabilité + réduction de
    charge, rollback = TTL 0.

    Fail-soft côté cache uniquement : toute erreur du fetch remonte à
    l'appelant (qui bascule sur la pause par défaut, comportement historique).
    """
    try:
        _hit = cache.get(wid)
        if _hit is not None and (time.monotonic() - _hit[0]) < ttl_s:
            if isinstance(_hit[1], dict):
                return _hit[1]
    except Exception:
        pass
    quotas = await fetch_fn(wid, cookie)
    try:
        if isinstance(quotas, dict):
            cache[wid] = (time.monotonic(), quotas)
    except Exception:
        pass
    return quotas


def bump_ttfb_failover(
    counts: dict[tuple[str, str], int],
    lock,
    key_alias: str,
    stage: str,
) -> None:
    """Métrique proxy_ttfb_failover_total{key_alias, retry_stage} — fail-soft."""
    try:
        with lock:
            k = (str(key_alias or "?"), str(stage))
            counts[k] = counts.get(k, 0) + 1
    except Exception:
        pass


def alias_for_api_key(keys, api_key: str) -> str:
    """Alias lisible d'une clé (métriques TTFB) — "?" si inconnue."""
    try:
        for _e in keys or ():
            if _e.get("api_key") == api_key:
                return str(_e.get("alias", "?"))
    except Exception:
        pass
    return "?"


def clamp_ttfb_timeout(value, *, default: float = DEFAULT_TTFB_TIMEOUT_S) -> float:
    """Borne le timeout watchdog lu depuis la config ([5, 600] s)."""
    try:
        v = float(value or default)
        return max(TTFB_TIMEOUT_MIN_S, min(TTFB_TIMEOUT_MAX_S, v))
    except Exception:
        return default


__all__ = [
    "DEFAULT_FETCH_QUOTAS_429_TTL_S",
    "DEFAULT_TTFB_TIMEOUT_S",
    "TTFB_TIMEOUT_MAX_S",
    "TTFB_TIMEOUT_MIN_S",
    "TTFBWatchdogTimeout",
    "alias_for_api_key",
    "bump_ttfb_failover",
    "clamp_ttfb_timeout",
    "fetch_quotas_cached",
    "_TTFBWatchdogTimeout",
]
