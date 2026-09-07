"""upstream — accès HTTP amont : pools, breakers, quotas (Phase 3 refonte).

* ``upstream.breaker`` — circuit-breakers per-endpoint + coupe-circuit global
  429 (déplacés depuis ``opencode.py``, déplacement pur) ;
* ``upstream.clients`` — pool de sessions curl + clients HTTP partagés par
  rôle (idem) ;
* ``upstream.quotas`` — cache court fetch_quotas/429 + watchdog TTFB (idem).

Modules purs : AUCUN import du projet (``opencode`` / ``config`` /
``dashboard`` / ``vpn_manager`` interdits). Tout ce qui varie est injecté
(``debug_fn``, seuils lus côté hôte depuis ``config.yaml``, état mutable
possédé par l'hôte et passé en paramètre) — cf. docs/PLAN_REFONTE.md §7.
"""

from upstream import breaker, clients, quotas

__all__ = ["breaker", "clients", "quotas"]
