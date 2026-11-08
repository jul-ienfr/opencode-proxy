# Feature Specification: Toutes les stations et toutes les IP utilisables

**Feature Branch**: `011-station-fleet-usability`  
**Created**: 2026-11-07  
**Status**: Draft  
**Input**: « L'objectif est que tout soit fonctionnel et que toutes les stations ou toutes les IP soient utilisables. » Audit : `docs/AUDIT_STATIONS_NORDVPN_WARP_2026-11-07.md`.

## Clarifications

### Session 2026-11-07

- Q : Que signifie « utilisable » ?
  R : Une station est utilisable si elle a un conteneur vivant, un tunnel/egress
  qui répond, un quota non épuisé, et une preuve de succès client récente
  (réponse de contenu, pas une page d'accueil). `connected` ou `healthy` ne
  suffit jamais.
- Q : Faut-il six stations NordVPN **et** six WARP simultanément ?
  R : Oui. L'objectif est six slots client-utilisables par provider sélectionné
  lorsque la politique de réserve est `full` ; en `shared`, six slots au total
  suffisent, mais l'état doit dire explicitement quelle capacité survit à une
  panne.
- Q : Une IP WARP IPv4 compte-t-elle comme une station ?
  R : Non. WARP IPv4 est une NAT partagée : elle ne crée pas de bucket de quota
  supplémentaire. Seul un `/64` IPv6 distinct compte comme bucket.
- Q : Les stations NordVPN non sélectionnées pour le trafic doivent-elles
  rester maintenues ?
  R : Oui. La maintenance et la rotation sont indépendantes de la sélection
  trafic : une station désactivée pour le routage n'est pas une station
  abandonnée.
- Q : Que faire quand une IP est épuisée ?
  R : La marquer avec son `reset_at` fournisseur, ne plus la sonder avant, et
  basculer sur une IP fraîche d'un bucket distinct. Jamais de réutilisation
  avant reset, jamais de second compte, jamais de payant.

## Vocabulaire

- **Station** : slot client-utilisable (conteneur, tunnel, port SOCKS), distinct
  de la session NordVPN et de l'IP d'egress.
- **Endpoint** : point de sortie concret (station NordVPN, conteneur WARP,
  proxy statique, direct).
- **Bucket** : unité de quota ; `/32` pour IPv4, `/64` pour IPv6. Deux
  endpoints dans le même bucket ne doublent pas la capacité.
- **Desired state** : description déclarative de ce qui doit exister
  (provider, stack, transport, host/pays, port, génération).
- **Actual state** : ce que Docker, le tunnel et la sonde client observent.
- **Client-utilisable** : preuve récente qu'une requête client a produit du
  contenu, sans `event: error`.
- **Réparation** : action automatique qui remet une station dans l'état
  desired, avec une cause et une preuve.
- **Rotation** : remplacement d'une IP/session par une autre sans perdre le
  slot ni interrompre les autres stations.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Voir la vérité sur chaque station (P1)

En tant qu'opérateur, je veux une vue par station qui distingue
`configurée`, `créée`, `vivante`, `egress OK`, `quota OK` et
`client-utilisable`, afin de savoir immédiatement quelles stations sont
réellement utilisables.

**Why this priority** : sans cette vérité, toute décision de rotation ou de
capacité est une opinion. C'est le socle de l'objectif « tout fonctionnel ».

**Independent Test** : l'API et le GUI exposent six stations NordVPN et six
endpoints WARP avec les six états et une preuve horodatée ; une station
`connected` sans probe client apparaît comme non utilisable.

**Acceptance Scenarios** :

1. **Given** six conteneurs NordVPN vivants, **When** je consulte la vue
   stations, **Then** chaque station affiche son état réel, son IP, son bucket,
   sa dernière preuve et sa prochaine action.
2. **Given** un conteneur `healthy` dont le tunnel ne porte pas de trafic,
   **When** la sonde client échoue, **Then** la station est `degraded` ou
   `failed`, jamais `usable`.
3. **Given** deux endpoints WARP dans le même `/64`, **When** je consulte la
   capacité, **Then** le système affiche 5 buckets utilisables, pas 6.

---

### User Story 2 — Créer exactement la flotte demandée (P1)

En tant qu'opérateur, je veux que six stations NordVPN et six endpoints WARP
soient créés de façon déterministe, idempotente et sans collision, afin
qu'aucun slot ne manque et qu'aucun doublon ne gaspille le quota.

**Why this priority** : l'audit live montre 5 conteneurs NordVPN sur 6 et un
doublon WARP. La création est la première marche de la disponibilité.

**Independent Test** : depuis un état vide, une commande crée 6 + 6 ; une
seconde exécution ne crée rien de plus ; chaque station a un nom, un port et un
bucket uniques.

**Acceptance Scenarios** :

1. **Given** un desired state à 6 stations, **When** le reconcile s'exécute,
   **Then** les 6 conteneurs existent, sont nommés `opencode-vpn-1..6` et
   publient les ports attendus.
2. **Given** une création interrompue, **When** le reconcile reprend, **Then**
   il nettoie l'état partiel et recrée sans collision de port.
3. **Given** un endpoint WARP dont le `/64` est déjà utilisé, **When** il est
   créé ou rafraîchi, **Then** il est refusé ou tourné vers un bucket distinct.

---

### User Story 3 — Maintenir et réparer sans boucle (P1)

En tant qu'opérateur, je veux qu'une seule autorité répare chaque station, avec
une cause classée et un budget, afin d'éviter les redémarrages en boucle et de
ramener une station à `client-utilisable` en moins de trois minutes.

**Why this priority** : le watchdog a produit 31 restarts en 10 minutes et le
reconcile a supprimé cinq stations saines en apparence. La réparation est
aujourd'hui une source de panne.

**Independent Test** : on tue un tunnel, on coupe un hostname, on épuise un
quota et on injecte un stack divergent ; chaque cas produit une action
différente, une seule fois, avec une preuve.

**Acceptance Scenarios** :

1. **Given** un hostname mort, **When** la sonde échoue, **Then** la station
   change de host vers un serveur vivant, sans restart infini du même host.
2. **Given** un conteneur au bon stack, **When** le reconcile passe, **Then** il
   n'est jamais supprimé.
3. **Given** trois échecs consécutifs, **When** la politique de park
   s'applique, **Then** la station est parkée avec une date de reprise et une
   cause, sans bloquer les autres.
4. **Given** un quota épuisé, **When** la sonde client renvoie 429, **Then** la
   station est marquée quota jusqu'à `reset_at`, pas « tunnel mort ».

---

### User Story 4 — Tourner sans perdre de slot (P1)

En tant qu'opérateur, je veux que la rotation remplace une IP épuisée par une
IP fraîche d'un bucket distinct, en respectant les budgets, sans jamais
interrompre les autres stations ni réutiliser une IP avant reset.

**Why this priority** : la rotation est le levier principal de continuité free ;
une rotation qui réutilise un bucket brûlé ne produit aucun gain.

**Independent Test** : on épuise une IP, on observe la rotation, puis on vérifie
que le bucket a changé, que le ledger est à jour et que les autres stations
n'ont pas bougé.

**Acceptance Scenarios** :

1. **Given** une station à 429, **When** la rotation s'exécute, **Then** la
   nouvelle IP a un bucket différent, le ledger est écrit et la station repasse
   `client-utilisable`.
2. **Given** un doublon de `/64`, **When** la rotation ciblée s'exécute,
   **Then** l'endpoint en double obtient un bucket distinct ou reste parké.
3. **Given** le budget horaire atteint, **When** une rotation est demandée,
   **Then** elle est refusée avec une raison visible, sans casser la station.
4. **Given** un redémarrage du proxy, **When** il reprend, **Then** les budgets
   et les `reset_at` sont rechargés depuis le registre, pas remis à zéro.

---

### User Story 5 — Décider la capacité sur des preuves (P2)

En tant qu'opérateur, je veux que le conseiller de capacité compte les buckets
uniques et les stations réellement utilisables, afin qu'il ne dise jamais
« 6/6 » quand cinq buckets existent.

**Why this priority** : une capacité fausse conduit à surcharger les buckets,
à brûler les quotas et à croire que l'objectif est atteint.

**Independent Test** : six endpoints WARP dont deux partagent un `/64` donnent
`unique_buckets = 5` et un verdict `degraded` ou `insufficient` selon la
politique.

**Acceptance Scenarios** :

1. **Given** 6 endpoints et 5 buckets, **When** le rapport est calculé,
   **Then** `usable_buckets = 5` et le déficit est visible.
2. **Given** 6 buckets mais 2 stations non client-utilisables, **When** le
   rapport est calculé, **Then** `client_usable = 4` et le verdict le reflète.
3. **Given** un provider non sélectionné, **When** le rapport est calculé,
   **Then** il est affiché comme désactivé, pas comme en panne, mais son état de
   maintenance reste visible.

---

### User Story 6 — Sécuriser l'exposition (P3)

En tant qu'opérateur, je veux que les proxies WARP et le dashboard ne soient
jamais exposés au LAN par défaut, afin que le quota et les statuts ne soient pas
accessibles à un tiers.

**Why this priority** : les six conteneurs WARP publient aujourd'hui sur
`0.0.0.0` et le dashboard tourne sans token sur `0.0.0.0`.

**Independent Test** : après application, `docker ps` ne montre que des binds
`127.0.0.1` pour WARP ; un accès LAN au dashboard sans token est refusé.

**Acceptance Scenarios** :

1. **Given** un endpoint WARP créé, **When** on inspecte ses ports, **Then**
   le bind est `127.0.0.1`.
2. **Given** `server.host = 0.0.0.0` sans token, **When** le proxy démarre,
   **Then** il refuse de démarrer ou avertit explicitement.

---

### User Story 7 — Consolidation inter-providers (P2)

En tant qu'opérateur, je veux que NordVPN et WARP partagent le même moteur de
cycle de vie — création, observation, santé, réparation, rotation, budgets,
preuves et statut — afin qu'une même panne produise la même décision et la même
trace, quel que soit le provider.

**Why this priority** : aujourd'hui, NordVPN et WARP ont deux implémentations
séparées pour les mêmes concepts : deux normalisations de bloc, deux résolveurs
d'egress, deux politiques de rotation, deux statuts, deux réparations. Chaque
correctif doit être appliqué deux fois, et les comportements divergent.

**Independent Test** : on injecte la même panne (`server_dead`, `quota_429`,
`duplicate_block`, `container_dead`) sur une station NordVPN et un endpoint
WARP ; les deux produisent le même état, la même cause, la même action et une
preuve équivalente dans le registre.

**Acceptance Scenarios** :

1. **Given** une panne `quota_429` sur NordVPN et sur WARP, **When** le moteur
   décide, **Then** les deux sont classés `quota_429`, parkés jusqu'au
   `reset_at` et tracés dans `repair_jobs`.
2. **Given** un doublon de bucket, **When** le moteur observe, **Then** les deux
   providers utilisent la même fonction de normalisation et le même verdict.
3. **Given** une rotation demandée, **When** le budget est atteint, **Then**
   les deux providers refusent avec le même code et la même raison.
4. **Given** une station et un endpoint, **When** je consulte le statut,
   **Then** les deux exposent le même schéma minimal.

---

### Edge Cases

- Un conteneur est vivant mais son tunnel est mort : ne pas le compter usable.
- Un hostname ne résout plus : changer de host, pas restart infini.
- Deux endpoints WARP partagent un `/64` après un redémarrage externe : le
  détecter et tourner l'un des deux.
- Un quota est épuisé sans header `Retry-After` : utiliser la politique du
  fournisseur, jamais une estimation locale.
- Un redémarrage du proxy survient pendant une rotation : reprendre depuis le
  registre, ne pas réinitialiser les budgets.
- Un provider est désélectionné : sa flotte reste maintenue, mais son trafic
  ne l'utilise pas.
- Un port est déjà occupé : la création échoue proprement et ne laisse pas de
  conteneur orphelin.
- Docker n'est pas disponible : le proxy démarre en mode dégradé explicite,
  sans détruire l'état.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001** Le système DOIT exposer, pour chaque station, les états
  `configured`, `created`, `running`, `tunnel_up`, `dns_ok`, `egress_ok`,
  `quota_ok`, `client_usable`, `parked`, `failed`.
- **FR-002** Une station NE DOIT PAS être marquée `client_usable` sans une
  preuve de contenu horodatée issue d'une requête client réelle.
- **FR-003** Le système DOIT maintenir un desired state unique par station
  (provider, stack, transport, host/pays, port, image, génération).
- **FR-004** Toute création, recréation ou suppression DOIT être idempotente et
  dérivée du desired state.
- **FR-005** Le système NE DOIT PAS supprimer un conteneur dont le stack et la
  génération correspondent au desired state.
- **FR-006** Le système DOIT détecter et corriger un écart entre le desired
  state, le conteneur réel et le `.env`/compose.
- **FR-007** Le système DOIT créer ou recréer une station manquante avant de
  lancer une rotation sur une autre station.
- **FR-008** Chaque station DOIT avoir un identifiant stable, un nom de
  conteneur stable, un port unique et un bucket d'egress unique.
- **FR-009** Le système DOIT refuser un endpoint WARP dont le `/64` est déjà
  utilisé par un autre endpoint actif.
- **FR-010** Le système DOIT réconcilier l'egress live d'un endpoint WARP avec
  `egress_ip`, `block`, le ledger et la config, à chaque changement détecté.
- **FR-011** Le système DOIT compter les buckets uniques, pas les endpoints,
  dans le rapport de capacité.
- **FR-012** Le système DOIT distinguer `auth_failed`, `server_dead`, `dns`,
  `transport`, `healthcheck`, `quota_429` et `proxy_down` dans la cause d'échec.
- **FR-013** Chaque cause DOIT avoir une action de réparation propre et
  journalisée.
- **FR-014** Un seul propriétaire DOIT exécuter la réparation ; le watchdog et
  le manager NE DOIVENT PAS recréer une station que le réparateur prend en
  charge.
- **FR-015** La réparation DOIT être plafonnée par station et par heure, avec
  backoff, park et reprise.
- **FR-016** La maintenance DOIT être indépendante de la sélection trafic : un
  provider non sélectionné reste maintenu.
- **FR-017** La rotation DOIT respecter `reset_at` et ne jamais réutiliser une
  IP épuisée avant son reset.
- **FR-018** La rotation DOIT écrire la nouvelle IP, le bloc, le lease et le
  ledger dans la même transaction logique.
- **FR-019** La rotation DOIT être plafonnée, persistée et rechargée après un
  redémarrage.
- **FR-020** Le système DOIT exposer une rotation préventive sur quota bas,
  latence, âge d'IP ou échecs consécutifs, avec seuils configurables.
- **FR-021** Le système DOIT exposer un verdict de capacité `ok`, `degraded` ou
  `insufficient` basé sur les buckets uniques et les preuves client.
- **FR-022** Le système DOIT exposer une vue par station dans le GUI avec
  l'état, la preuve, le bucket, le quota et la prochaine action.
- **FR-023** Le système DOIT exposer une API de diagnostic par station
  (`GET /api/stations`, `GET /api/stations/{id}`) et des actions explicites de
  rotation/réparation.
- **FR-024** Les ports des endpoints WARP DOIVENT être publiés sur
  `127.0.0.1` par défaut.
- **FR-025** Le dashboard DOIT exiger un token si `server.host` n'est pas
  loopback.
- **FR-026** Aucune création de ressource payante, aucun second compte et
  aucune attente ne DOIT être proposé comme solution.
- **FR-027** Toute action automatique DOIT être traçable dans
  `logs/registry.db` (station, cause, preuve, résultat, durée).
- **FR-028** Le système DOIT survivre à un redémarrage du proxy sans perdre le
  desired state, les budgets, les leases ni les `reset_at`.
- **FR-029** Le système DOIT exposer un mode dégradé explicite quand une
  station ou un provider est indisponible, sans fabriquer un faux 6/6.
- **FR-030** Le système DOIT refuser de démarrer en mode `0.0.0.0` sans token
  ou avertir clairement l'opérateur.
- **FR-031** Le démarrage et la connexion des stations désirées DOIVENT avoir un
  propriétaire explicite, indépendant de `repair_owner` et de la sélection
  trafic.
- **FR-032** Les écritures de `.env` DOIVENT être atomiques et sérialisées entre
  processus ; aucun fichier temporaire partagé.
- **FR-033** Un upscale qui échoue DOIT rollback le `station_count`, le registre
  et le conteneur partiel.
- **FR-034** Le proxy DOIT démarrer même si la station 1 est down ; la
  dépendance Compose ne doit pas être un point de défaillance unique.
- **FR-035** Toutes les clés de politique de réparation DOIVENT être lues par le
  propriétaire et `repair_owner` DOIT être modifiable à chaud.
- **FR-036** Les actions manuelles `next`/`pin_country` DOIVENT passer par le
  propriétaire et ne pas courser un job en cours.
- **FR-037** Le provisioning WARP DOIT réappliquer les options du conteneur
  (`-e`, limites, labels, devices) et utiliser une image épinglée.
- **FR-038** Les logs de rotation/réparation WARP DOIVENT être persistés dans le
  fichier de logs principal.
- **FR-039** Le statut DOIT exposer `egress_ip`, `block`, `managed`, `container`,
  `ipv6`, ne jamais forcer `duplicate_ip_groups=0` ou `parked_stations=0`.
- **FR-040** Le refresh des credentials DOIT inclure la clé WireGuard et
  redémarrer les stations concernées de façon contrôlée.
- **FR-041** Aucun secret de control NE DOIT être injecté dans l'environnement
  du proxy ; `/api/vpn/diagnostic` DOIT exiger un token.
- **FR-042** L'auto-update DOIT être cohérent avec l'image épinglée par digest.
- **FR-043** NordVPN et WARP DOIVENT partager un modèle de slot commun
  (`SlotSpec`, `SlotObservation`, `SlotState`) et un moteur de cycle de vie
  unique.
- **FR-044** Les providers DOIVENT être branchés par des adapters
  (`NordVpnProvider`, `WarpProvider`) qui n'implémentent que les mécanismes
  spécifiques (compose/auth/session vs docker run/registration).
- **FR-045** La normalisation des buckets DOIT être une seule fonction
  partagée par les deux providers.
- **FR-046** La résolution d'egress DOIT être un seul module partagé, avec la
  même famille, le même timeout et la même preuve.
- **FR-047** La politique de réparation DOIT être la même pour les deux
  providers : mêmes causes, mêmes actions, mêmes budgets, même park.
- **FR-048** La rotation DOIT utiliser le même budget, le même déclencheur,
  la même persistance et la même trace pour les deux providers.
- **FR-049** Le statut DOIT exposer un schéma minimal commun aux deux
  providers, avec les champs spécifiques en sous-objet.
- **FR-050** Les logs et preuves DOIVENT être écrits par le même pipeline,
  avec `provider`, `slot`, `cause`, `action`, `evidence`, `result`.
- **FR-051** Les duplications existantes (`block_for_ip`/`block_for_egress`,
  deux résolveurs, deux statuts, deux réparations) DOIVENT être supprimées ou
  ramenées à un seul point d'entrée.
- **FR-052** La consolidation NE DOIT PAS masquer les différences de mécanisme :
  auth/session NordVPN, registration Cloudflare, ports et images restent des
  détails d'adapter.
- **FR-053** `usable_stations`, `unique_usable_ips`, `active_stations_with_quota`
  et `capacity_degraded` DOIVENT être dérivés du même ensemble de slots
  `client_usable` que `station_health` ; aucun compteur ne peut contredire un
  `boot_error` à 0/6.

### Key Entities

- **DesiredStationSpec** : provider, station id, stack, transport, host/pays,
  port, image, enabled, generation, source.
- **StationState** : état courant, cause, dernière preuve, dernière IP, bucket,
  quota, parked_until, repair_attempts.
- **Endpoint** : label, host, port, provider, managed, container, image,
  ipv6, egress_ip, block, enabled.
- **Session** : identité NordVPN, compte, hostname, transport, started_at,
  released_at, reason.
- **EgressIP** : ip, family, block, provider, first_seen, last_ok, last_429,
  reset_at, state.
- **QuotaBucket** : block, day, used, successes, failures, last_status,
  reset_at, source.
- **Lease** : station, ip, block, hostname, transport, started_at, expires_at,
  released_at, reason.
- **RotationJob** : station, trigger, old_ip, new_ip, old_block, new_block,
  attempts, result, duration.
- **RepairJob** : station, cause, action, attempts, result, evidence,
  started_at, ended_at.
- **CapacityReport** : target_slots, policy, providers, unique_buckets,
  client_usable, verdict, warnings.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001** 6 stations NordVPN et 6 endpoints WARP existent, avec 6 noms, 6
  ports et 12 buckets uniques (6 NordVPN + 6 WARP) après un cycle de reconcile.
- **SC-002** 6/6 stations NordVPN sont `client_usable` pendant 24 h continues,
  avec une preuve de contenu par station toutes les 15 min.
- **SC-003** 6/6 endpoints WARP sont `client_usable` avec 6 `/64` distincts
  pendant 24 h continues.
- **SC-004** 0 doublon d'IP ou de `/64` dans l'inventaire actif.
- **SC-005** 0 `rm -f` injustifié et 0 recréation d'un conteneur conforme au
  desired state sur 100 cycles de reconcile.
- **SC-006** MTTR < 180 s pour une station tombée ; < 3 restarts par station et
  par heure ; 0 station en boucle plus de 10 min.
- **SC-007** Une rotation préserve le nombre de slots utilisables : aucune
  baisse de `client_usable` due à une rotation sur une autre station.
- **SC-008** 0 réutilisation d'IP avant `reset_at` ; 100 % des 429 sont
  enregistrés avec un reset fournisseur.
- **SC-009** Le rapport de capacité ne dit jamais `ok` avec moins de
  `target_slots` buckets uniques client-utilisables.
- **SC-010** Le proxy écoute sur `:4000` en moins de 30 s après le démarrage,
  même si une station est down, et expose un mode dégradé explicite.
- **SC-011** 0 port WARP publié sur `0.0.0.0` par défaut ; dashboard sans token
  refusé sur une interface non loopback.
- **SC-012** 100 % des actions de réparation/rotation ont une entrée dans le
  registre avec cause et preuve.
- **SC-013** Un upscale en échec laisse `station_count`, le registre et les
  conteneurs dans l'état initial, avec 0 station fantôme.
- **SC-014** 0 pin hostname/pays au-delà de `station_count` ; 0 secret de
  control dans l'environnement du proxy.
- **SC-015** 100 % des rotations/réparations WARP ont une ligne de log
  exploitable avec label, ancien IP, nouvelle IP, raison et durée.
- **SC-016** Le proxy démarre en < 30 s même si la station 1 est AUTH_FAILED,
  et le dashboard reste accessible en mode dégradé explicite.
- **SC-017** Une même panne injectée sur NordVPN et WARP produit le même état,
  la même cause, la même action et une preuve équivalente dans le registre.
- **SC-018** 0 duplication de logique de bucket/egress/réparation/rotation
  entre les deux providers ; 1 seul point d'entrée par concept.
- **SC-019** Le budget de rotation, le park et le reset sont partagés et
  persistent après redémarrage pour les deux providers.
- **SC-020** Le schéma de statut commun est identique pour les deux providers
  et validé par un test de contrat.
- **SC-021** `usable_stations` est toujours égal au nombre de slots
  `client_usable` dans `station_health` ; 0 occurrence de 6/6 avec
  `boot_error` 0/6.

## Assumptions

- L'objectif permanent reste six stations client-utilisables ; `target_slots`
  est un paramètre 1–10.
- NordVPN et WARP restent les deux providers free principaux ; les proxys
  statiques et Direct sont des secours.
- WARP IPv4 ne crée pas de bucket ; seul un `/64` IPv6 distinct compte.
- Le compte NordVPN est unique ; les sessions sont rares et doivent être
  réutilisées, pas multipliées.
- Le quota est par bucket et par jour, avec reset fournisseur ; aucune
  estimation locale ne remplace `reset_at`.
- Le registre SQLite est la source de vérité à terme ; les JSON existants sont
  des exports à migrer.
- Le système doit rester free-only : aucune solution payante, aucun second
  compte, aucune attente comme réponse.
- Windows + Docker Desktop est la plateforme de référence ; les scripts
  PowerShell et les chemins `logs/` sont conservés.
