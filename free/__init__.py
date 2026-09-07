"""free — modèles gratuits : pool, rotation, identités (Phase 6 refonte).

* ``free.pool`` — domicile canonique de ``FreeIPPool`` (déplacé depuis
  ``free_ip_pool.py``, shim conservé) ;
* ``free.rotation`` — domicile canonique fusionné de ``shared_rotation.py``
  (registre IP + curseur identités) et ``latency_rotation.py`` (moteur
  latence-adaptive) — shims conservés.
"""

from free import pool, rotation

__all__ = ["pool", "rotation"]
