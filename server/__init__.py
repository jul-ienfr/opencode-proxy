"""server — middlewares ASGI purs + cache réponse (Phase 2 refonte).

Extraction à comportement identique depuis ``opencode.py`` (déplacement pur,
cf. docs/PLAN_REFONTE.md §6). Modules 100 % purs : AUCUN import du projet
(``opencode`` / ``config`` / ``dashboard`` interdits) — tout ce qui varie est
injecté à la construction :

* ``debug_fn`` — journalisation (``dashboard.display.debug`` côté hôte) ;
* ``log_fn`` — access log (``dashboard.display.log`` côté hôte) ;
* ``dumps_fn`` / ``dumps_str_fn`` — sérialisation JSON rapide (orjson hôte) ;
* ``rate`` / ``burst`` / ``stale_ttl`` / ``limit_getter`` — lus depuis
  ``config.yaml`` / env CÔTÉ HÔTE (jamais ici, pas de side-effect d'import).

Compat : les noms historiques ``_ResponseCache``, ``_Bucket`` restent
importables depuis ``opencode`` (alias, tests ``test_proxy.py`` /
``test_thinking_e2e.py``).
"""

from server.accesslog import AccessLogMiddleware
from server.cache import ResponseCache, _ResponseCache
from server.throttle import (
    DEFAULT_BURST,
    DEFAULT_RATE,
    DEFAULT_STALE_BUCKET_TTL,
    Bucket,
    RateLimitMiddleware,
    RequestBodyLimitMiddleware,
    _Bucket,
)

__all__ = [
    "DEFAULT_BURST",
    "DEFAULT_RATE",
    "DEFAULT_STALE_BUCKET_TTL",
    "AccessLogMiddleware",
    "Bucket",
    "RateLimitMiddleware",
    "RequestBodyLimitMiddleware",
    "ResponseCache",
    "_Bucket",
    "_ResponseCache",
]
