"""streaming — Server-Sent Events : pompe ping + coalesce (Phase 7 refonte).

* ``streaming.sse`` — ``sse_pump`` (déplacée depuis ``opencode.py``,
  déplacement pur : asyncio seul, zéro global projet — intervalles et
  plafonds passés en paramètres).

L'hôte garde les wrappers fins ``_sse_keepalive`` / ``_sse_coalesce``
(leurs défauts sont lus depuis ``config.yaml`` côté hôte) et l'alias
``_sse_pump`` (seams ``test_sse_*``).
"""

from streaming import sse

__all__ = ["sse"]
