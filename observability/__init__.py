"""observability — persistance, capture trafic, métriques (Phase 2 refonte).

* ``observability.db`` — domicile canonique SQLite WAL + batch writer
  (déplacé depuis ``app/db``, shim conservé) ;
* ``observability.capture`` — domicile canonique capture trafic Wireshark-like
  (déplacé depuis ``traffic_capture.py``, shim conservé).

Modules purs : aucun import ``opencode`` / ``config`` / ``dashboard``.
(``observability.metrics`` viendra en Phase 3 avec ``upstream/quotas`` :
``_build_metrics_text`` dépend des compteurs failover — pas pur aujourd'hui.)
"""

from observability import capture, db

__all__ = ["capture", "db"]
