"""free — modèles gratuits : pool, rotation, identités (Phase 6 refonte).

* ``free.pool`` — domicile canonique de ``FreeIPPool`` (déplacé depuis
  ``free_ip_pool.py``, shim conservé) ;
* ``free.rotation`` — domicile canonique fusionné de ``shared_rotation.py``
  (registre IP + curseur identités) et ``latency_rotation.py`` (moteur
  latence-adaptive) — shims conservés ;
* ``free.identities`` — vault d'identités OpenCode par IP (1 IP = 1
  installation : version UA + client + project + ses_/msg_ isolés).
"""

from free import identities, pool, rotation

__all__ = ["identities", "pool", "rotation"]
