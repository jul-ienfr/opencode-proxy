# Requirements Quality Checklist: Continuité free, egress GUI et isolation d'identité

**Purpose**: Vérifier que la spec 005 est complète, testable, non ambiguë et
alignée sur la constitution avant toute implémentation.
**Created**: 2026-11-06
**Feature**: `specs/005-free-model-continuity-and-egress-gui/spec.md`

**Note**: `[x]` = critère de qualité des exigences revu et satisfait. Ce n'est
pas un statut d'implémentation : aucun code de la spec 005 n'est écrit.

## Portée et objectifs

- [x] CHK001 Les trois sujets sont distincts : continuité du modèle, pilotage
      GUI des egress, isolation d'identité.
- [x] CHK002 Les objectifs 1–9 sont explicitement mesurables ou testables.
- [x] CHK003 Les non-objectifs excluent l'implémentation immédiate, le payant,
      le second compte et le contournement des limites de sessions.
- [x] CHK004 La règle P0 « pas de multi-station avant isolation d'identité »
      est explicite et bloquante.
- [x] CHK005 Le quota reste défini par bloc d'egress, jamais comme global ou
      illimité.
- [x] CHK006 Aucune solution proposée ne dépend de la jambe payante.

## Continuité du modèle free

- [x] CHK007 La spec réutilise `FREE_MODELS`, `FREE_MODEL_MAP`,
      `free_models_probed.json` et `ensure_free_models_on_boot`.
- [x] CHK008 Aucun second fetcher de catalogue ni seconde sonde n'est requis.
- [x] CHK009 Le target est conservé s'il est présent et non `dead`.
- [x] CHK010 Le remplaçant est choisi parmi les `alive`, avec un ordre de
      priorité défini (famille, préférence, fraîcheur).
- [x] CHK011 Le remplaçant est persisté et propagé à
      `FREE_DISCOVERY_DEFAULT_TARGET`.
- [x] CHK012 Une entrée explicite de `FREE_MODEL_MAP` n'est jamais réécrite.
- [x] CHK013 Aucun modèle payant n'est choisi en fallback.
- [x] CHK014 L'état `no_free_model` est exposé sans écriture destructive.
- [x] CHK015 La réconciliation est sérialisée et déclenchée après le probe.
- [x] CHK016 Les fallbacks hardcodés (`jev-1.13-free`, défaut de
      `fresh_chain.py`) sont identifiés comme à supprimer.
- [ ] CHK017 La sémantique exacte du pin (`pinned_target` souverain ou simple
      préférence) est tranchée — voir Q-02.

## Egress GUI et provisioning

- [x] CHK018 La GUI propose `VPN (NordVPN)` et `WARP` en modes principaux.
- [x] CHK019 `socks5` et `direct` restent disponibles en section avancée.
- [x] CHK020 Le nombre de stations est borné et les limites provider sont
      affichées, sans clamp silencieux.
- [x] CHK021 L'état désiré et l'état réel sont exposés par l'API.
- [x] CHK022 Un plan dry-run précède tout apply.
- [x] CHK023 Le plan distingue create / keep / remove / verify.
- [x] CHK024 L'apply est idempotent et ne touche que le delta.
- [x] CHK025 Chaque station est vérifiée (egress + probe free) avant activation.
- [x] CHK026 Une bascule bleu/vert conserve les anciennes stations jusqu'à
      vérification des nouvelles.
- [x] CHK027 Un échec déclenche un rollback et ne coupe pas l'ancien ensemble.
- [x] CHK028 Un verrou sérialise les apply et renvoie `409 provisioning_busy`.
- [x] CHK029 Les proxies `socks5` manuels ne sont jamais écrasés.
- [x] CHK030 Le provisioner VPN réutilise `_apply_station_count`.
- [x] CHK031 Le provisioner WARP crée des conteneurs labellisés avec des blocs
      distincts.
- [x] CHK032 Chaque option GUI a un consommateur runtime, un effet observable
      et un test (contrat GUI).
- [ ] CHK033 Le provider WARP par défaut est tranché (`caomingjun` vs
      `warp-minimal`) — voir Q-04.
- [ ] CHK034 Le plafond exact de stations WARP et VPN est tranché — voir Q-05.
- [ ] CHK035 L'emplacement GUI (nouvelle carte vs extension de `VpnStatus`)
      est tranché — voir Q-07.
- [ ] CHK036 Le traitement du WARP IPv4 partagé (dernier dans la rotation vs
      retrait) est tranché — voir Q-11.
- [ ] CHK037 Le mode d'exécution du provisioning (contrôleur intégré vs helper
      séparé) est tranché — voir Q-12.

## Isolation d'identité et anti-corrélation

- [x] CHK038 Chaque station a un `project` unique dérivé du label et d'un
      secret local, jamais de l'IP seule ni du workspace réel.
- [x] CHK039 `project` fait 40 hex et n'est jamais `global` ni le hash du
      dépôt réel.
- [x] CHK040 `ses_` est minté par `(station, conversation)`, canonique `…ffe`,
      jamais partagé entre stations.
- [x] CHK041 `msg_` est frais par requête et jamais partagé.
- [x] CHK042 La trace est station-scopée ; le span est par requête.
- [x] CHK043 Le bloc `<env>` est minimal et synthétique
      (`Working directory: /workspace/<label>`).
- [x] CHK044 Le prompt officiel est templaté avec la date UTC courante, sans
      chemin réel ni date de capture.
- [x] CHK045 Une conversation est épinglée à une station ; le round-robin ne
      s'applique qu'aux conversations nouvelles ou sans historique.
- [x] CHK046 Le failover ne se produit que si la station est inutilisable ;
      l'historique n'est jamais envoyé en parallèle à deux stations.
- [x] CHK047 Les sessions curl, connexions et cookies ne sont pas partagés
      entre stations.
- [x] CHK048 L'audit vérifie la disjonction des tuples et scanne les
      sous-strings interdites.
- [x] CHK049 Une station dupliquée ou fuyante est exclue, ou l'apply échoue.
- [x] CHK050 Les vecteurs partagés par conception (`Bearer public`, TLS/JA3,
      UA si un seul profil, `client` canonique) sont documentés, limités et
      exposés dans `shared_vectors` de l'audit.
- [ ] CHK051 Les valeurs synthétiques exactes du `<env>` (platform, workspace
      root) sont tranchées — voir Q-08.
- [ ] CHK052 Le choix de conserver un seul profil UA/TLS ou d'en introduire
      plusieurs est tranché — voir Q-09.
- [ ] CHK053 La politique de failover d'une conversation (station unique vs
      re-pin immédiat) est tranchée — voir Q-10.
- [ ] CHK054 L'emplacement et la sauvegarde du secret d'identité sont tranchés
      — voir Q-16.

## Cas limites et erreurs

- [x] CHK055 Le target qui disparaît pendant un cycle de probe est traité sans
      casser la requête en cours.
- [x] CHK056 Un probe absent, corrompu ou stale ne provoque pas d'écriture.
- [x] CHK057 Tous les candidats `limited`/`unproven` produisent
      `no_free_model`, jamais un payant.
- [x] CHK058 Un pin devenu `dead` est exposé comme `pinned_unavailable`, sans
      substitution silencieuse.
- [x] CHK059 Un `/64` ou une IPv4 dupliqué exclut la station.
- [x] CHK060 Le throttle d'enregistrement WARP conserve l'ancien ensemble.
- [x] CHK061 Docker absent, port occupé ou limite VPN dépassée échoue au
      préflight sans modification.
- [x] CHK062 Deux apply concurrents : le second reçoit `409`.
- [x] CHK063 Un proxy manuel sur un port du plan est conservé.
- [x] CHK064 Un secret d'identité absent/illisible refuse le multi-station.
- [x] CHK065 Une collision de `project` est détectée et la station exclue.
- [x] CHK066 L'expiration du TTL d'affinité ne duplique pas l'historique.
- [x] CHK067 Un prompt templaté refusé par le gate échoue fermé, sans retour
      au chemin réel.
- [x] CHK068 Une panne IPv6/WARP ne bascule pas silencieusement vers `direct`
      désactivé.
- [x] CHK069 Une requête sans historique peut être routée librement ; dès qu'un
      historique existe, l'affinité est obligatoire.

## Testabilité et critères

- [x] CHK070 Chaque user story a un Independent Test.
- [x] CHK071 Les scénarios Given/When/Then sont vérifiables.
- [x] CHK072 Les AC-001 à AC-014 couvrent les trois sujets et l'isolation.
- [x] CHK073 Les SC-001 à SC-010 sont mesurables sur un jeu déterministe.
- [x] CHK074 Les tests d'isolation peuvent être construits sans envoyer de
      requêtes, puis confirmés sur un egress sacrifiable.
- [x] CHK075 Le plan prévoit un test live de deux stations avec diff
      d'identités.
- [x] CHK076 Les échecs de provisioning ont un scénario de rollback testé.
- [x] CHK077 L'idempotence est testée par trois apply successifs.

## Constitution et cohérence

- [x] CHK078 La spec ne contredit aucun principe de la constitution.
- [x] CHK079 La disponibilité reste mesurée, jamais déclarée.
- [x] CHK080 Une IP d'egress n'appartient qu'à une station.
- [x] CHK081 Aucune preuve, aucun succès : un 200 sans contenu ne compte pas.
- [x] CHK082 La dégradation est explicite et reste free.
- [x] CHK083 Chaque option GUI a un effet réel (constitution XII).
- [x] CHK084 Aucun secret, token ou chemin réel ne figure dans les documents
      du paquet 005.
- [x] CHK085 Aucun placeholder `TODO`/`TBD` ne reste dans la spec.
- [x] CHK086 Les renvois entre spec, research, plan, data-model, contrats,
      tasks et questions ouvertes sont cohérents.

## Décisions ouvertes

- [ ] CHK087 Q-01 à Q-18 sont tranchées ou explicitement acceptées par défaut.
- [ ] CHK088 Le provider WARP et le plafond de stations sont validés par une
      mesure, pas par une hypothèse.
- [ ] CHK089 La politique de pin et de failover est validée par l'opérateur.
- [ ] CHK090 Le paquet 005 est approuvé avant le début de P0.

## Profils vérifiés

- [x] CHK091 Le générateur n'émet que des profils `verified` et non `stale` ;
      le drapeau statique de `VERSION_SPECS` ne suffit pas ; la capture ne
      fournit jamais project/session/trace.
- [x] CHK092 Le registre conserve provenance, empreinte de capture, preuves,
      famille TLS, TTL et écriture atomique.
- [x] CHK093 La vérification exige deux `200` avec contenu sur des blocs
      distincts ; un `429` est `quota`, jamais `reject`.
- [x] CHK094 Aucun tuple mixte UA/Bun/JA3/ordre/corps/session n'est assigné.
- [x] CHK095 L'assignation est déterministe par station et stable pendant la
      conversation.
- [x] CHK096 Les profils distincts sont préférés ; la réutilisation est marquée
      `shared_profile`.
- [x] CHK097 Le fallback est le profil canonical vérifié, jamais payant.
- [x] CHK098 L'audit expose `profile_id`, `tls_family`, `shared_profile`,
      `stale`, `rejected`.
- [ ] CHK099 Le nombre de profils à vérifier et le TTL sont tranchés — Q-17.
- [ ] CHK100 La politique de réutilisation quand `M < N` est tranchée — Q-18.

## Rotation automatique WARP

- [x] CHK101 Une station WARP managée épuisée est tournée automatiquement,
      même sans requête en cours (watchdog).
- [x] CHK102 Un proxy non `managed` n'est jamais redémarré ni recréé.
- [x] CHK103 Le nouvel egress est vérifié et son bloc est distinct de l'ancien
      et des autres stations ; la config est persistée.
- [x] CHK104 La rotation est single-flight par label avec cooldown anti-tempête.

## Sélection runtime et cooldown

- [x] CHK105 Un endpoint SOCKS5 statique n'est pas soumis au contrat santé
      Docker (auth/tunnel) ; il reste sélectionnable si activé et sans quota.
- [x] CHK106 Le cooldown est keyé sur l'egress réel (`/64` IPv6), pas sur le
      label stable.
- [x] CHK107 Six requêtes proxy consécutives sur six WARP managés rendent 200.
- [x] CHK108 Une conversation reste sur la même station ; une nouvelle
      conversation peut être routée ailleurs (affinité runtime).
- [x] CHK109 Le round-robin SOCKS5 n'est actif que si la GUI l'a demandé :
      `socks5_auto_rotate=true`, `free_parallel.enabled=true` et
      `free_parallel.routing=round-robin`. En `failover`, les nouvelles
      conversations restent sticky sur le primaire.
- [x] CHK110 Les modes GUI sont appliqués : `strict` = compteur 1,2,3… ;
      `load-balance` = station la moins chargée ; `hedge` = course de deux
      stations au premier tour, gagnante épinglée ensuite.
- [x] CHK111 `free_parallel.enabled=false` ou le toggle SOCKS5 de rotation
      désactivé rend la sélection sticky, avec bascule uniquement sur
      panne/quota.
- [x] CHK112 `quality_rotation` s'applique aux endpoints WARP managés (les
      non-managés sont ignorés) et `latency_rotation` hard-cooled ne rend
      jamais zéro candidat SOCKS5 (dernier passage d'admission).
- [x] CHK113 `post_rotation_probe` sonde le nouvel egress après rotation :
      `200`/`429` = joignable, autres échecs retentés puis bad-markés.
- [x] CHK114 Un endpoint WARP managé bloqué pour un couple `(modèle, IP)`
      est tourné par le watchdog même sans épuisement du ledger journalier.
- [x] CHK115 Les timeouts SOCKS5 sont corrélés par couples `(station, IP)`
      distincts ; au seuil, aucun bad-mark supplémentaire n'est posé.
- [x] CHK116 Un redémarrage ne restaure pas les bad-marks transitoires ; les
      compteurs persistent et les stations sont immédiatement disponibles.
- [x] CHK117 Un endpoint managé `trial-dead` est tourné par le watchdog avec
      la cause `trial_dead`, sans attendre 6 h.
- [x] CHK118 L'application de l'egress et la détection de bloc dupliqué se
      font sous le verrou global ; six endpoints = six `/64` distincts.
- [x] CHK119 Le runtime Windows lance docker/curl avec `CREATE_NO_WINDOW` ;
      aucune fenêtre de console intempestive.
- [x] CHK120 La GUI SOCKS5 affiche label/egress/bloc et le Test cible le
      label exact (IPv6 forcé pour un endpoint IPv6).
- [x] CHK121 Un timeout SOCKS5 respecte le plancher de redondance
      `min(3, n-1)` avant tout bad-mark.
- [x] CHK122 En SOCKS5, `/health` capacité = endpoints statiques réels
      (7/7), pas les stations Docker vides.

## Notes

- CHK017, CHK033–CHK037, CHK051–CHK054, CHK087–CHK090, CHK099–CHK100 sont des
  **décisions d'opérateur**, pas des ambiguïtés rédactionnelles : elles restent
  ouvertes jusqu'à arbitrage.
- La checklist est un artefact de revue des exigences ; les statuts
  d'implémentation seront tenus dans `tasks.md`.
