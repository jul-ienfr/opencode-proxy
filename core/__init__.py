"""core — sélection des clés API + pauser (Phase 5 refonte).

* ``core.keys`` — ``KeyPauser`` (pause 429/401/403 + persistance disque),
  ``AllKeysPausedError``, sélection round-robin/failover (déplacés depuis
  ``opencode.py``, déplacement pur).

AUCUN import du projet (``opencode`` / ``config`` / ``dashboard`` interdits ;
stdlib + ``yaml`` seuls) :

* ``debug_fn`` / ``log_fn`` / ``alias_fn`` injectés (l'hôte passe
  ``dashboard.display.debug/log`` et ``_alias_for_key`` — via lambdas
  résolues À L'APPEL car ces globaux hôtes sont définis APRÈS la classe
  dans ``opencode.py`` et patchés par tests) ;
* ``prefix_cache`` (dict mémo SHA-256) possédé par l'hôte et passé au
  constructeur (partagé entre instances, vidé par ``_rebuild_key_cache``) ;
* ``max_pause`` injecté (l'hôte lit ``config.yaml``) ;
* toute la sélection (``select_next_key``…) prend les listes/index/pauser
  en paramètres et RETOURNE l'état mis à jour — l'hôte possède les globaux
  (``API_KEYS``, ``_key_failover_index``… rebind par
  ``test_perf_lot3_regressions.py`` / ``test_no_valid_keys_guard.py``) et
  les wrappers d'une ligne les lisent À L'APPEL.
"""

from core import keys

__all__ = ["keys"]
