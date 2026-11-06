"""free — modèles gratuits : pool, rotation, identités (Phase 6 refonte).

* ``free.pool`` — domicile canonique de ``FreeIPPool`` (déplacé depuis
  ``free_ip_pool.py``, shim conservé) ;
* ``free.rotation`` — domicile canonique fusionné de ``shared_rotation.py``
  (registre IP + curseur identités) et ``latency_rotation.py`` (moteur
  latence-adaptive) — shims conservés ;
* ``free.identities`` — vault d'identités OpenCode par IP (1 IP = 1
  installation : version UA + client + project + ses_/msg_ isolés) ;
* ``free.profile_registry`` / ``free.profile_verifier`` — profils vérifiés
  P0.5 (preuves, TTL, assignation par station) ;
* ``free.profile_generator`` — enveloppe du générateur existant, filtre
  ``verified`` et fallback canonical ;
* ``free.profile_live`` — requête de vérification P0/P0.5, transport injecté ;
* ``free.profile_transport`` — adaptateur curl_cffi du vérificateur ;
* ``free.warp_rotation`` — rotation automatique des stations WARP managées ;
* ``free.station_identity`` — identité par station, prompt templaté,
  affinité de conversation et audit P0.
"""

from free import (
    identities,
    pool,
    profile_generator,
    profile_live,
    profile_registry,
    profile_transport,
    profile_verifier,
    rotation,
    station_identity,
    warp_rotation,
)

__all__ = [
    "identities",
    "pool",
    "profile_generator",
    "profile_live",
    "profile_registry",
    "profile_transport",
    "profile_verifier",
    "rotation",
    "station_identity",
    "warp_rotation",
]
