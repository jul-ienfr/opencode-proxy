"""ops — exploitation : mono-instance, superviseurs, cycle de vie (Phase 8 refonte).

* ``ops.lock`` — verrou mono-instance (déplacé depuis ``opencode.py``) ;
* ``ops.supervisor`` — domicile canonique de ``station_supervisor.py``
  (déplacement pur, shim conservé).

Purs : AUCUN import du projet (``opencode`` / ``config`` / ``dashboard``
interdits). ``log_fn`` / ``debug_fn`` injectés à l'appel.
"""

from ops import lock, supervisor

__all__ = ["lock", "supervisor"]
