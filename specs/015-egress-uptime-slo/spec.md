# Feature Specification: Disponibilité mesurée et bascule rapide des egress

**Feature Branch**: `015-egress-uptime-slo`

**Created**: 2026-11-11

**Status**: Draft

**Input**: « Je veux un audit complet et exhaustif de la gestion des IP, VPN,
WARP et Proxylist. Je veux que toutes les stations fonctionnent. NordVPN doit
être à 100 % d'uptime et utilisable, car un uptime sans utilisabilité ne sert à
rien. Je sais que 100 % n'est pas possible, mais je souhaite s'en rapprocher au
maximum, idéalement 99,99 %, avec le moins de coupures possible. Les stations
doivent être constamment utilisables. Lorsqu'il y a une rotation, elle doit être
la plus rapide possible. Si on n'arrive pas à se connecter au provider NordVPN,
il faut chercher une solution, relancer la station, changer de technologie,
comme le fait le logiciel NordVPN sous Windows : OpenVPN UDP → TCP → WireGuard,
et inversement. »

## Périmètre

Cette feature couvre **toute la chaîne d'egress de la jambe free** :

1. **Stations** : cycle de vie, santé, réparation, propriétaire unique.
2. **Transports** : WireGuard, OpenVPN UDP, OpenVPN TCP, cycle automatique par
   hostname et par station, indépendance des stations.
3. **IP** : registre, claims, leases, quota par IP, `reset_at`, `/24`, warm pool.
4. **Rotation** : déclencheurs, standby préqualifié, make-before-break,
   cutover, vitesse, protection de la dernière station.
5. **WARP** : provisioning, egress IPv4/IPv6, rotation, santé, standby.
6. **Proxylist** : sources, qualification, liveness, score, denylist, standby.
7. **Observabilité** : une seule définition de `client_usable`, incidents
   bruts, fenêtres 1 h / 24 h / 7 j / 30 j, SLO, burn rate, alertes.
8. **Robustesse** : redémarrage, crash, PID/locks, états persistés, fail-soft.

**Hors périmètre** : la jambe payante, un second compte, l'attente comme
solution, l'ajout de stations, la réécriture big-bang des monolithes. WARP et
les proxys restent des egress **free** de secours, jamais un substitut payant.

**Contrainte dure — six stations, pas une de plus** : le nombre de stations est
fixé par `station_count` (6). Le système ne crée, ne répare et ne compte
**jamais** une septième station. Le warm pool est une réserve d'**IP d'egress**
pour ces six stations, pas une station supplémentaire. WARP et les proxys
publics sont des egress de secours du service, jamais des stations 7/8 et jamais
comptés dans le 6/6. `target_slots` doit valoir 6, et le canary WireGuard ne
doit pas être une septième session.

## Contexte

L'audit `audit.md` montre que le proxy fonctionne (200 sur les deux modèles
free) mais que les six stations sont à 0/6 `client_usable`, avec des pauses
AUTH de 15 min et des chaînes qui tournent 20 min sans gagner. Les causes sont
des boucles de réparation : pin gagnant effacé, guard AUTH global, deux
réparateurs, probes trop coûteuses, ledger qui ressuscite des IP épuisées, et
absence de mesure d'uptime.

La constitution impose : disponibilité mesurée (I), trois ressources séparées
(II), `registry.db` seule vérité (III), `reset_at` fournisseur (IV), une IP par
station (V), transports indépendants (VI), preuve de contenu (VII), un seul
réparateur (VIII), dégradation explicite et free (X), fallbacks multiples (XI),
options GUI réelles (XII), modularité (XIV), multiplateforme (XV), processus
silencieux (XVI), gates de qualité (XVII).

## User Scenarios & Testing

### User Story 0 — Six stations exactement, réparées et rotées automatiquement (Priority: P1)

En tant qu'opérateur, je fixe le nombre de stations à 6. Le système ne doit
jamais en créer une septième ; en revanche, chacune des six doit être réparée et
rotée automatiquement dès qu'elle n'est plus utilisable, sans attendre les
autres et sans intervention.

**Why this priority**: c'est la contrainte de dimensionnement explicite. Une
septième station ou une septième session fausse la limite NordVPN et retarde la
réparation des six vraies stations.

**Independent Test**: mettre une station down, vérifier qu'elle est la seule
réparée, qu'aucune station 7 n'apparaît, et qu'elle revient en service pendant
que les cinq autres restent intactes.

**Acceptance Scenarios**:

1. **Given** `station_count=6`, **When** la station 3 devient inutilisable,
   **Then** seule la station 3 est rotée, aucune station 7 n'est créée, et les
   stations 1, 2, 4, 5, 6 conservent leur IP.
2. **Given** les six stations sont down, **When** la réparation démarre,
   **Then** les six sont traitées avec fairness (3 slots, pas de famine) et
   aucune septième station n'est ajoutée pour « aider ».
3. **Given** une station redevient utilisable, **When** son 200 est prouvé,
   **Then** elle réintègre le 6/6 et sa chaîne s'arrête immédiatement.
4. **Given** `target_slots` ou un canary dépasserait 6, **When** la config est
   chargée, **Then** le démarrage échoue avec une erreur explicite.

---

### User Story 1 — Aucune coupure quand un transport tombe (Priority: P1)

En tant qu'opérateur, lorsqu'un transport ne fonctionne plus pour un hostname
(OpenVPN UDP filtré, AUTH refusé, WireGuard sans egress), la station essaie
automatiquement les autres transports dans un ordre borné, sans intervention et
sans geler les autres stations.

**Why this priority**: c'est la demande explicite « comme le logiciel NordVPN
sous Windows ». Aujourd'hui la cascade n'existe que dans le scanner, et une
pause AUTH globale gèle même WireGuard.

**Independent Test**: bloquer UDP sur un hostname, lancer une réparation, et
vérifier que la station obtient un 200 via TCP puis WireGuard en moins de 60 s,
sans pause globale.

**Acceptance Scenarios**:

1. **Given** une station down sur OpenVPN UDP, **When** le transport UDP échoue,
   **Then** la station tente OpenVPN TCP, puis WireGuard, avec un budget de
   temps par transport et un backoff, et s'arrête au premier 200 prouvé.
2. **Given** deux stations en AUTH_FAILED en moins de 60 s, **When** le guard
   se déclenche, **Then** seules les tentatives OpenVPN sont espacées ; les
   stations WireGuard continuent et aucune pause ne dépasse 60 s.
3. **Given** un hostname qui échoue sur les trois transports, **When** le cycle
   se termine, **Then** le hostname est mis en backoff (pas en mort définitive)
   et la station passe au candidat suivant.

---

### User Story 2 — Aucune coupure quand une IP épuise son quota (Priority: P1)

En tant qu'opérateur, une station dont l'IP approche de son quota est drainée et
remplacée par une IP déjà qualifiée avant que le client ne subisse un 429.

**Why this priority**: le quota est par IP et tombe après 20–40 requêtes ; sans
standby, chaque épuisement devient une coupure de plusieurs minutes.

**Independent Test**: consommer le quota d'une IP jusqu'au seuil de drain,
vérifier que la station bascule sur une IP du warm pool en moins de 30 s et que
le client n'observe pas de 429.

**Acceptance Scenarios**:

1. **Given** une IP à 80 % de son budget, **When** le seuil de drain est atteint,
   **Then** une IP standby est réclamée et le cutover se fait sans couper le
   tunnel courant avant que le nouveau soit prouvé.
2. **Given** un 429 avec `Retry-After`, **When** l'IP est marquée épuisée,
   **Then** `reset_at` est respecté, l'IP n'est plus claimée avant, et aucune
   autre IP n'est refroidie à cause de ce 429.
3. **Given** un 429 sans corps exploitable, **When** le scope est inconnu,
   **Then** l'IP reçoit un cooldown court (≤ 120 s) et non un park jusqu'à
   minuit.

---

### User Story 3 — Récupération rapide après panne de station (Priority: P1)

En tant qu'opérateur, une station qui perd son tunnel ou son IP redevient
`client_usable` en moins de 30 s au p95, sans qu'une autre station soit
sacrifiée et sans qu'une chaîne puisse monopoliser un slot.

**Why this priority**: l'objectif est 99,99 % utilisable, pas 100 % « connecté ».

**Independent Test**: tuer le tunnel d'une station, mesurer le temps jusqu'au
prochain 200 réel ; répéter sur les six stations ; vérifier qu'aucune autre
station ne perd son IP.

**Acceptance Scenarios**:

1. **Given** une station serving, **When** son tunnel tombe, **Then** le
   superviseur détecte, claim une IP standby, cutover, prouve un 200 et remet la
   station en service en ≤ 30 s p95.
2. **Given** une chaîne qui n'a produit aucun 200 après N binds, **When** le
   budget de progression est atteint, **Then** elle libère son slot et un autre
   slice est essayé, sans attendre 20 min.
3. **Given** un gagnant fraîchement prouvé, **When** le manager n'a pas encore
   adopté le nouveau host, **Then** le pin et le winner sont conservés au moins
   180 s et ne sont jamais effacés sur la seule différence de host live.

---

### User Story 4 — WARP et proxylist comme vrai standby (Priority: P2)

En tant qu'opérateur, si NordVPN devient indisponible, WARP et les proxys
publics prennent le relais sans coupure et sans être déjà épuisés par le trafic
normal.

**Why this priority**: les fallbacks existent mais sont consommés en continu et
leur catalogue est statique ; ce ne sont pas des réserves.

**Independent Test**: couper NordVPN pendant 15 min ; vérifier que les six
stations restent utilisables via WARP/proxys et que le quota de réserve n'a pas
été consommé avant l'incident.

**Acceptance Scenarios**:

1. **Given** NordVPN coupé, **When** le pool bascule, **Then** WARP/proxys
   maintiennent le service et l'incident est journalisé avec la cause
   `provider_down` ; le verdict 6/6 stations reste explicitement 0/6, sans
   station 7.
2. **Given** un conteneur WARP recréé, **When** son IP change, **Then** le
   catalogue live voit la nouvelle IP sans restart du proxy.
3. **Given** un proxy public mort, **When** le cycle de qualification le
   détecte, **Then** il est marqué `infra` (court) et non `dead` (7 jours) si la
   faute est locale.

---

### User Story 5 — SLO mesuré et vérifiable (Priority: P2)

En tant qu'opérateur, je vois la disponibilité réelle des six stations sur 1 h,
24 h, 7 j et 30 j, avec les incidents bruts, la cause, l'IP, le transport et le
temps de cutover.

**Why this priority**: sans mesure, un 99,99 % est une opinion.

**Independent Test**: injecter une panne connue, vérifier que l'incident
apparaît dans la fenêtre 1 h avec la bonne durée et la bonne cause, et que le
calcul manuel sur les échantillons bruts donne le même chiffre.

**Acceptance Scenarios**:

1. **Given** 30 jours d'échantillons, **When** l'opérateur ouvre le SLO,
   **Then** la disponibilité est calculée depuis les échantillons bruts, avec
   le nombre d'échantillons et les bornes de fenêtre.
2. **Given** une station à 0 %, **When** cinq autres sont à 100 %, **Then** la
   moyenne flotte n'est pas 100 % (pas de filtre `> 0`).
3. **Given** une coupure de 5 min, **When** le burn rate dépasse le budget,
   **Then** une alerte est émise sans dépendre d'un profil Compose opt-in.

---

### User Story 6 — Redémarrage et crash sans état fantôme (Priority: P3)

En tant qu'opérateur, après un reboot ou un crash, les stations repartent avec
les bons leases, sans `parked_until` ni `blocked_until` issus de
`time.monotonic()`.

**Why this priority**: un état fantôme peut immobiliser une station ou une IP
des heures après un reboot.

**Independent Test**: redémarrer le proxy avec des états persistés, vérifier
qu'aucune station n'est parkée ni aucune IP bloquée par un timestamp
monotonic.

**Acceptance Scenarios**:

1. **Given** un état persisté `parked_until` ou `blocked_until`, **When** le
   proxy redémarre, **Then** l'état est converti en wall-clock ou ignoré.
2. **Given** un lease actif au crash, **When** le proxy redémarre, **Then**
   `reconcile` libère ou renouvelle le lease selon l'IP live, sans doublon.

---

### Edge Cases

- Dernière station utilisable : aucune rotation ne doit la retirer avant qu'une
  remplaçante ait prouvé un 200.
- Six sessions NordVPN déjà utilisées : un septième bind doit être refusé ou
  sérialisé, jamais lancer une tempête AUTH.
- Toutes les IP du warm pool sont épuisées : la dégradation est explicite, avec
  la prochaine échéance `reset_at`, pas un 6/6 fabriqué.
- Un hostname n'existe plus dans l'API NordVPN : il est retiré sans condamner
  les autres.
- Un proxy public renvoie un 200 sans contenu : il n'est pas promu (preuve de
  contenu).
- Une IP est partagée par deux stations : une seule la garde, l'autre rotate.
- Un reboot pendant un cutover : l'état revient au dernier 200 prouvé, pas à un
  pin non adopté.
- Une source de proxys disparaît du YAML : elle est démise, pas fetchée à vie.
- Un conteneur WARP n'a pas de `HEALTHCHECK` : la rotation utilise un probe
  actif avec deadline, pas 4×90 s.

## Requirements

### Functional Requirements

- **FR-001** : `client_usable` DOIT signifier un 200 réel avec contenu, obtenu
  via l'egress de la station, avec un âge maximum borné (≤ 120 s pour le
  routing, ≤ 900 s pour l'affichage). Un statut `connected`, un control API ou
  une simple résolution d'IP ne suffisent jamais.
- **FR-002** : la flotte DOIT exposer une disponibilité mesurée sur 1 h, 24 h,
  7 j et 30 j, calculée depuis des échantillons bruts horodatés, avec le nombre
  d'échantillons et les bornes de fenêtre. Le 6/6 porte **exclusivement** sur
  les six stations configurées, jamais sur une septième.
- **FR-003** : la cible DOIT être paramétrable ; la valeur par défaut est
  **99,99 % de requêtes client réussies** sur 30 jours, avec un objectif
  secondaire de **99,9 % de temps à 6/6** et une cible stretch de 99,99 % de
  temps à 6/6 quand le standby d'IP N+2 est en place.
- **FR-004** : chaque station DOIT pouvoir essayer **WireGuard → OpenVPN UDP →
  OpenVPN TCP → WireGuard** par hostname, avec un budget de temps par transport
  (`transport_cycle_timeout_s`), un backoff et un circuit breaker par
  `(station, transport)`.
- **FR-005** : une panne AUTH OpenVPN NE DOIT PAS geler WireGuard ni les autres
  stations ; la pause globale est plafonnée à 60 s, décroît avec le temps et est
  effacée par tout 200 réel.
- **FR-006** : le système DOIT maintenir un **warm pool** d'au moins
  `station_count + 2` **IP d'egress préqualifiées** pour les six stations,
  réapprovisionné en fond, avec TTL, claim exclusif et promotion sur preuve de
  contenu. Ce pool ne crée aucune station : c'est une réserve d'IP pour les six
  mêmes stations.
- **FR-007** : une rotation DOIT être **make-before-break** : l'IP de secours
  est claimée et prouvée avant que l'IP serving soit libérée ; la dernière
  station utilisable n'est jamais retirée sans remplaçante prouvée. Une rotation
  ne crée jamais de station supplémentaire : elle remplace l'egress d'une des
  six stations.
- **FR-008** : le cutover DOIT être ≤ 30 s p95 et ≤ 60 s p99 avec standby ;
  sans standby, la dégradation est explicite et le client reçoit un 503 avec
  `Retry-After`, jamais une attente bloquante > 2 s dans le chemin requête.
- **FR-009** : le quota DOIT être modélisé **par IP** ; `reset_at` fournisseur
  est autoritaire ; aucun 429 ne refroidit une autre IP ; un 429 de scope
  inconnu reçoit un cooldown court.
- **FR-010** : une IP NE DOIT PAS être réassignée si `reset_at` est dans le
  futur ou si le dernier 429 est postérieur à la dernière preuve de 200.
- **FR-011** : les claims DOIVENT être atomiques (transaction conditionnelle ou
  index unique), les leases renewés pendant le service, et les locks
  vérifier le PID et le propriétaire.
- **FR-012** : le **superviseur est le seul réparateur** ; le pool ne recrée
  jamais une station sur 429, il enqueue une demande prioritaire. Le manager ne
  répare pas quand `repair_owner=supervisor`. Le superviseur ne répare que les
  stations de `station_count` et refuse toute station 7.
- **FR-013** : le superviseur DOIT traiter les six stations indépendamment, avec
  au moins 3 slots, un budget de progression (N binds sans 200) et un backoff
  par station ; aucune chaîne ne monopolise un slot au-delà du budget. Une
  station down déclenche sa rotation immédiatement, sans attendre que les autres
  aient fini.
- **FR-014** : les sondes de vivacité DOIVENT être à zéro quota (GET statique) ;
  les sondes de modèle réelles DOIVENT avoir un budget par IP et ne jamais
  épuiser une IP serving. La double confirmation ne s'applique qu'aux candidats
  qui ont déjà un 200.
- **FR-015** : WARP DOIT exposer un catalogue **live** (les rotations sont vues
  sans restart), prouver la famille IPv4/IPv6, et avoir un healthcheck actif
  avec deadline si l'image n'en fournit pas.
- **FR-016** : la proxylist DOIT persister la santé des sources, distinguer
  `infra` (court) de `dead` (long), appliquer la denylist à l'IP d'egress, et
  garder des workers stables.
- **FR-017** : WARP et les proxys DOIVENT avoir une réserve de standby
  (politique `reserve` ou poids nul quand 6/6 NordVPN est sain) pour ne pas être
  consommés avant l'incident. Ils ne remplacent jamais une station et ne sont
  jamais comptés dans le 6/6 : ils maintiennent le **service** quand NordVPN est
  indisponible.
- **FR-018** : le système DOIT écrire un seul journal d'incidents, avec station,
  IP, transport, cause, début, fin, durée et preuve ; les deux journaux actuels
  sont unifiés.
- **FR-019** : chaque option de configuration DOIT avoir un consommateur
  runtime et un test ; les clés mortes (`auth_global_pause_s`,
  `transport_cycle_timeout_s`, `transport_auth_backoff_s`,
  `auth_fallback_transport`) sont câblées ou supprimées.
- **FR-020** : après reboot/crash, les états temporels DOIVENT être en
  wall-clock ou invalidés ; `reconcile` libère les leases orphelins sans
  dupliquer une IP.
- **FR-021** : toute dégradation DOIT être exposée avec sa raison et la
  prochaine échéance (`reset_at`, session, prochain candidat), sans fabriquer un
  6/6.
- **FR-022** : les gates existantes (tests, `ruff`, `config_audit`,
  `platform_audit`, option-map, CI Windows/Linux) DOIVENT rester vertes.

### Key Entities

- **Station** : slot 1–6, tunnel, SOCKS, état de santé, transport effectif,
  dernière preuve de 200, lease actif.
- **Session** : connexion NordVPN, ressource bornée (6), partagée entre
  transports mais pas entre stations.
- **Egress IP** : IP publique, pays, hostname, `/24` ou `/64`, état
  (`fresh/candidate/assigned/draining/exhausted/error/retired`), quota,
  `reset_at`, lease.
- **Transport** : `wireguard`, `openvpn-udp`, `openvpn-tcp` ; circuit breaker,
  budget, dernière erreur typée.
- **Warm spare** : IP préqualifiée, preuve de contenu, TTL, score, provenance.
- **Incident** : ouverture/fermeture, station, IP, transport, cause, durée,
  échantillons.
- **SLO window** : 1 h, 24 h, 7 j, 30 j ; échantillons, disponibilité, budget,
  burn rate.

## Success Criteria

### Measurable Outcomes

- **SC-001** : disponibilité **requête client ≥ 99,99 %** sur 30 jours
  (fenêtre glissante), mesurée sur des 200 réels avec contenu.
- **SC-002** : disponibilité **6/6 temps ≥ 99,9 %** sur 30 jours, cible stretch
  99,99 % après warm pool d'IP N+2 et réserve WARP/proxys. Le 6/6 désigne les
  six stations configurées ; une septième station ou session rend le score
  invalide.
- **SC-003** : cutover p95 ≤ 30 s, p99 ≤ 60 s ; rotation avec standby p95
  ≤ 20 s. Une station down déclenche sa rotation en ≤ 5 s.
- **SC-004** : aucun épisode 0/6 > 60 s sur 30 jours ; aucun gel de flotte causé
  par une seule station ; aucune famine de slot parmi les six.
- **SC-005** : aucune pause AUTH > 60 s ; aucun 429 provoqué par nos propres
  sondes sur une IP serving.
- **SC-006** : consommation de quota par les sondes ≤ 5 % du budget quotidien
  d'une IP serving.
- **SC-007** : soak 24 h : 6/6 sur ≥ 99 % des échantillons, zéro deadlock, zéro
  IP dupliquée, zéro lease orphelin ; soak 72 h pour la release.
- **SC-008** : 100 % des findings P0 de `audit.md` fermés avec un test de
  non-régression ; 0 P0 ouverte dans `DEBT.md` aux gates.

### Acceptance Tests

- **AT-01** : couper OpenVPN UDP → la station finit en 200 via TCP ou WG en
  ≤ 60 s ; aucune autre station ne perd son IP.
- **AT-02** : couper WireGuard → la station finit en 200 via OVPN en ≤ 60 s.
- **AT-03** : deux AUTH_FAILED en 60 s → pause ≤ 60 s, WireGuard continue,
  retour 6/6 ≤ 2 min.
- **AT-04** : épuiser une IP → drain + cutover sur standby ≤ 30 s, aucun 429
  client.
- **AT-05** : 429 avec `Retry-After` → IP non retestée avant `reset_at` ;
  aucune autre IP refroidie.
- **AT-06** : tuer un tunnel → station `client_usable` ≤ 30 s p95.
- **AT-07** : gagnant frais → pin conservé ≥ 180 s, adopté, station usable.
- **AT-08** : NordVPN coupé 15 min → WARP/proxys maintiennent le service, mais
  le 6/6 stations reste à 0/6 et l'incident est marqué `provider_down` ; aucune
  station 7 n'est créée.
- **AT-09** : reboot avec états persistés → aucun `parked_until`/`blocked_until`
  fantôme.
- **AT-10** : injecter 0/6 → l'incident apparaît dans la fenêtre 1 h avec durée
  et cause exactes.
- **AT-11** : vérifier `station_count=6`, `target_slots=6`, aucune station 7 dans
  `/api/pool-status` ni dans le superviseur ; un test de config refuse 7.

## Assumptions

- **Six stations exactement**, une session active par station ; aucune station 7,
  aucun canary compté comme station ; WARP et proxys ne consomment pas de
  session NordVPN et ne sont pas des stations.
- Le quota free est par IP (IPv4 /32, IPv6 /64), jamais par compte ; les 429
  observés ne qualifient que les IP testées.
- Le catalogue NordVPN contient des milliers de serveurs ; le problème est la
  qualification et la rotation, pas la taille du catalogue.
- WARP et les proxys publics restent des egress free de secours, pas des
  providers payants.
- `registry.db` reste la source de vérité ; les exports JSON sont en lecture
  seule.
- Le système doit rester multiplateforme Windows/Linux/macOS et silencieux.
- Un 99,99 % de temps à 6/6 dépend de la taille du warm pool et de la réserve
  WARP/proxys ; si ces réserves sont insuffisantes, la dégradation est exposée,
  pas maquillée.
