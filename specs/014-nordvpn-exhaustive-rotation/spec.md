# Feature Specification: Control plane NordVPN — découverte, gestion, watchdog et rotation exhaustive

**Feature Branch**: `014-nordvpn-exhaustive-rotation`

**Created**: 2026-11-09

**Status**: Draft

**Input**: « Toutes les IP NordVPN ne sont pas testées. Il faut les classer de la
meilleure à la moins bonne — meilleur rating, meilleure connexion, la moins
utilisée, meilleur ping par rapport à l'opérateur — consommer le quota de
chacune, et basculer automatiquement sur la suivante jusqu'à ce que toutes
soient testées. » Complété par : chaque station est indépendante, toutes
doivent être gérées en parallèle, au maximum utilisables avec le minimum de
coupure, voire zéro ; en mode automatique, tous les transports VPN doivent être
essayés et cyclés ; le watchdog et tout ce qui touche à NordVPN (gestion,
découverte d'IP, etc.) font partie du périmètre.

## Périmètre — control plane NordVPN

Cette feature couvre **tout le control plane de la jambe NordVPN** :

1. **Découverte** : catalogue API, cache, embedded, manifeste, transports,
   charge, fraîcheur, preflight ; scheduler et runs persistants.
2. **Inventaire** : `catalog`, `servers`, `server_transports`, `catalog_sources`,
   `catalog_runs`, `discovery_runs` ; une seule source de vérité.
3. **Gestion de station** : cycle de vie `created` → … → `client_usable`,
   container, tunnel, DNS, egress, AUTH, session, transport, bind/rebind.
4. **Watchdog** : heartbeat, sondes par station, détection tunnel/DNS/egress/
   AUTH/quota/client-unusable, incidents par station, réparation.
5. **Réparation** : file par station, propriétaire unique, backoff, escalade,
   reprise après crash ; aucune réparation globale pour une panne locale.
6. **Qualification** : file exhaustive, score de potentiel, époque, reprise,
   cycle multi-transports.
7. **Rotation / cutover** : standby, make-before-break, quotas, 429,
   rotations parallèles, protection de la dernière station.
8. **Quota** : ledger par IP, `reset_at`, headroom, consommation, reset.
9. **Observabilité** : SLO par station, 6/6, incidents, découverte,
   qualification, transports, cutover, quota.
10. **Config/GUI** : chaque option a un consommateur, un effet et un test.

**Hors périmètre** : la jambe WARP et les proxys publics, sauf leurs
interactions avec le pool ; la réécriture **big-bang** des monolithes (la
réécriture est progressive par strangler, US12) ; l'ajout de stations ; la
jambe payante, un second compte ou l'attente comme solution.

## Contexte

Le catalogue amont contient **7 070 serveurs en ligne**, dont **6 932
compatibles WireGuard**, répartis dans **150 pays** ; la charge médiane est de
10 %, le maximum de 84 %. Le catalogue d'egress, lui, ne contient que **6
options NordVPN**, toutes en `quota`, parce qu'il est alimenté par les six
stations actives (`/api/vpn/fleet`) et non par le catalogue
(`free/egress_runtime.py:275-300`, `free/egress_adapters/nordvpn.py:56`).

Le scanner a touché 6 817 hostnames distincts sur 26 045 tentatives, mais
seulement 74 tentatives ont atteint un 200 (0,28 %) et 22 150 se sont arrêtées
en `no_egress` (85 %). Sur les 4 809 serveurs live tentés (68,0 %), **aucun n'a
obtenu 200** ; les quatre hostnames qui portent les 94 succès sont des hôtes
legacy absents de l'API. 2 261 serveurs live (32,0 %) n'ont jamais été tentés.
Les observations ne couvrent que 1 592 hostnames (dont 1 373 live), avec 94
succès sur **4 hostnames legacy**. La couche de claims a parqué 9 770 des 9 788 hostnames
(`country_backoff` 5 407, `no_egress` 4 242, `bind_error` 121). Le scanner
s'arrête au premier 200 (`scripts/fresh_chain.py:2142-2143`) et les 285 runs
sont tous des runs de réparation, jamais de balayage.

En mode automatique, un seul transport est réellement parcouru
(`fast_discovery: true`, `scripts/fresh_chain.py:1903-1917`), et un 429 passe
au hostname suivant au lieu d'essayer les autres transports (`:2018-2028`).
Les tentatives mesurées sont sans ambiguïté : WireGuard porte **73 200**,
OpenVPN UDP et TCP cumulent **16 045 tentatives et 0 200**. Le fallback
OpenVPN n'est donc ni exercé systématiquement ni diagnostiqué par transport.

Le control plane existe en morceaux : `station_state` est vide,
`repair_jobs`/`repair_requests` sont vides, `free/repair_queue.py` vit dans une
base séparée, les 162 incidents sont tous `scope='fleet'` (157
`client_usable_low`, 3 `sweep_stalled`, 2 `warm_pool_low`), et
`scripts/ip_registry_discover.py` est une découverte ciblée, pas un scheduler.
`ops/supervisor.py` annonce un watchdog non branché. La présente spec couvre
donc la découverte, la gestion de station, le watchdog, la réparation, la
qualification, la rotation et le quota comme un seul control plane.

Le problème n'est donc pas l'absence d'IP. Le problème est l'absence d'une
**file de qualification exhaustive**, d'un **ordre par potentiel**, d'une
**rotation qui consomme le quota puis passe à la suivante**, et d'un **control
plane unifié** pour la découverte, le watchdog et la réparation.

## Objectif système — fiabilité, performance, qualité, modularité, maintenabilité

Le but n'est pas seulement de tester plus d'IP : c'est de construire le système
le plus **fiable, performant, qualitatif, modulable et maintenable** possible.
Ces cinq attributs sont des exigences de premier rang, avec des cibles
mesurables, pas des commentaires.

| Attribut | Définition | Cible mesurable |
|---|---|---|
| **Fiabilité** | Le service continue malgré les pannes locales ; une station ne contamine jamais les autres. | 99,9 % de disponibilité du control plane ; 6/6 ≥ 99 % quand une IP 200 existe ; 0 incident de station propagé en flotte ; reprise < 5 min. |
| **Performance** | Aucun chemin de requête bloquant ; budgets bornés et testés. | API p95 < 500 ms ; UI p95 < 2 s ; preflight 500 < 5 min ; balayage 7 070 < 24 h ; cutover p95 < 2 s ; rotation 429 < 60 s. |
| **Qualité** | La vérité vient de la mesure, avec preuve et classification déterministe. | 100 % des tentatives classées ; 0 `429` en `dead` ; 0 faux `dead` ; 100 % des décisions avec preuve ; 0 doublon d'IP. |
| **Modularité** | Domaine pur, ports/adapters, dépendances inversées, pas de fork. | 0 I/O dans le domaine ; 0 import provider dans le domaine ; modules < 1 500 lignes ; 0 cycle ; ajout source/provider sans toucher au cœur. |
| **Maintenabilité** | Le code est lisible, testé, typé, documenté, observable et réparable ; les monolithes sont démantelés. | Couverture ≥ 85 % nouveau code ; type/lint 0 erreur ; `config_audit` 0 dead/missing ; ADR/debt à jour ; MTTR < 5 min ; D-02/D-20 closes. |

Ces attributs sont détaillés dans les sections ci-dessous et protégés par des
gates de qualité (FR-092 à FR-110, SC-055 à SC-072).

## Objectifs

1. **Une file unique par hostname** : chaque serveur en ligne du catalogue
   entre une fois dans une file de qualification, avec ses métadonnées, ses
   transports, sa charge et son quota connu.
2. **Un score de potentiel** distinct du score mesuré : taux de 200, ping
   opérateur, quota restant, charge API, fraîcheur. Le pays et la source ne
   sont jamais des facteurs de score.
3. **Un ordre explicite et reprenable** : meilleur potentiel d'abord, puis
   quota restant, ping, charge, hostname. La file survit aux redémarrages.
4. **Un balayage complet par époque** : chaque hostname éligible reçoit au
   moins une tentative par époque, avec un état terminal et une preuve.
5. **Une consommation du quota par IP** : une IP `200` sert jusqu'au `429` ;
   elle passe alors en `quota` avec son `reset_at` fournisseur et la station
   claim immédiatement le meilleur candidat suivant. Une IP qualifiée mais non
   assignée reste `usable` et conserve son quota ; elle n'est consommée que
   lorsqu'une station la sert.
6. **Une classification honnête** : `200`, `429`, `no_egress`/`bind_error`
   (infrastructure), `auth` (station), `dead` (host), `skipped` (raison). Aucun
   pays n'est une porte dure.
7. **Un ping relatif à l'opérateur** : DNS, connect et handshake mesurés depuis
   l'hôte de l'opérateur, puis latence through-tunnel pour les 200.
8. **Une observabilité de couverture** : testés / total, 200 / 429 / infra /
   skip, par pays et par transport, prochains candidats, âge du dernier test.
9. **Six stations indépendantes et continues** : chaque station a son propre
   worker, son propre état et son propre curseur ; toutes tournent en
   parallèle ; une station ne pause jamais les autres ; la rotation est
   make-before-break avec standby, pour une disponibilité maximale et une
   coupure nulle ou bornée.
10. **Une stratégie multi-transports cyclique** : en mode automatique, essayer
    tous les transports supportés par le hostname (WireGuard, OpenVPN UDP,
    OpenVPN TCP), puis revenir à WireGuard, avec un nombre de rounds borné, un
    curseur persistant et une mesure de santé par transport.
11. **Un control plane unifié** : découverte, gestion de station, watchdog,
    réparation, qualification, rotation et quota partagent `registry.db` et
    une seule politique. Le watchdog surveille chaque station, pas seulement
    la chaîne ; la découverte est un scheduler, pas un script ciblé ; le
    manager existant reste l'adapteur, sans fork.
12. **Un démantèlement planifié des monolithes** : `vpn/manager.py`,
    `free/pool.py`, `scripts/fresh_chain.py`, `dashboard/api.py` et
    `opencode.py` sont découpés par seams, avec parité de comportement,
    shadow/canary et rollback ; objectif < 1 500 lignes par module ou entrée
    fine.

## Qualité, fiabilité, performance, maintenabilité

Ces exigences sont aussi contraignantes que les exigences fonctionnelles.

### Fiabilité

- Aucun cycle ne doit perdre la file : un crash reprend au hostname suivant,
  sans doublon ni saut.
- Un hostname n'est jamais marqué `dead` à cause d'un `no_egress` de station ou
  de transport ; une panne de tunnel ne condamne jamais l'IP (constitution II).
- Un `429` n'est jamais marqué `dead` ; son `reset_at` fournisseur est respecté
  (constitution IV).
- Une station en `AUTH_FAILED` est mise en quarantaine ; ses hostnames
  retournent en `pending` après réparation.
- Le balayage est fail-soft : une source morte, un DNS cassé ou un probe en
  échec ne vide jamais la file ni le pool actif.
- Toute décision automatique est journalisée avec sa cause et sa preuve
  (constitution VIII).
- Le système survit à un redémarrage OS : file, époque, leases, `reset_at` et
  observations sont persistés dans `registry.db`.
- Le control plane a un **SLO** et un **error budget** : disponibilité
  process, heartbeat watchdog, âge de la dernière sonde ; une brûlure d'error
  budget alerte avant la panne.
- Les opérations sont **idempotentes** : un retry, un crash ou un doublon de
  message ne crée ni double claim, ni double tentative, ni double réparation.
- Les files sont **bornées** et appliquent la **backpressure** : pas de
  mémoire/disque non bornés, pas de boucle de retry infinie.
- Une panne locale est **isolée** : elle ne peut pas modifier l'IP, le lease
  ou l'état d'une autre station ; aucun incident de station n'est agrégé en
  `fleet`.
- Toute erreur est **classée** (`infra`, `quota`, `auth`, `dead`, `skipped`,
  `interrupted`) ; aucune erreur silencieuse, aucun `except: pass` dans le
  chemin critique.
- La reprise après crash est **transactionnelle** : le curseur, le round de
  transport et le lease sont cohérents au redémarrage.

### Continuité de service

- Chaque station est **indépendante** : un worker, un état, un curseur, un
  lease. Aucune rotation globale n'est sérialisée.
- Les six stations sont gérées **en parallèle** ; une station qui qualifie, se
  répare ou tourne ne bloque ni ne pause les cinq autres.
- Aucune **pause globale** : un 429, un `AUTH_FAILED` ou un épuisement ne
  concerne que la station et l'IP impliquées. Les autres continuent de servir.
- Chaque station entretient un **standby** `usable` quand il existe, et tourne
  de façon **proactive** quand le headroom passe sous le seuil, au lieu
  d'attendre le 429.
- **Make-before-break** : l'ancienne IP sert jusqu'à ce que la nouvelle ait
  prouvé un 200 ; si le bind échoue, l'ancienne est restaurée.
- **Coupure cible** : 0 si un second tunnel/espace de noms est disponible ;
  sinon p95 < 2 s et max < 5 s, avec la durée et la raison journalisées.
- **Disponibilité** : ≥ 99 % `client_usable` par station sur 24 h quand au
  moins une IP 200 existe. Aucune station n'est laissée idle ou sacrifiée pour
  accélérer le balayage.
- La priorité est **service > repair > sweep**, évaluée **par station**, pas
  globalement.

### Stratégie multi-transports

- En mode **automatique**, le système essaie **tous les transports supportés**
  par le hostname : `wireguard`, `openvpn-udp`, `openvpn-tcp`, dans un ordre
  configurable, puis **reboucle** sur le premier.
- Un échec de transport (`no_egress`, `bind_error`, timeout) ne condamne jamais
  le hostname : le transport suivant est essayé avant tout backoff de host.
- Un `429` sur un transport n'empêche pas les autres : chaque transport peut
  rendre une IP d'egress différente et un quota différent.
- Le curseur de transport et le round sont **persistés par hostname** ; une
  reprise continue le cycle, elle ne repart pas à WireGuard.
- La santé est mesurée **par transport** : 200, 429, infra, latence. Un
  transport avec 0 succès sur un minimum d'essais est **dégradé** et exposé ;
  il n'est pas retenté aveuglément.
- Le mode **manuel** n'utilise que le transport choisi par l'opérateur ; le
  cycle multi-transports est réservé à `auto`.
- Aucun transport n'est une exigence dure : si WireGuard ou OpenVPN est
  indisponible, les autres continuent ; le hostname n'est `dead` qu'après
  épuisement de tous les transports supportés et des rounds configurés.

### Watchdog et réparation

- Le watchdog surveille **chaque station** (heartbeat, sonde, tunnel, DNS,
  egress, AUTH, quota, `client_usable`) avec un état persistant dans
  `station_state`.
- Une panne locale ne déclenche qu'une **réparation locale** : aucun restart,
  aucune pause et aucun recreate des autres stations.
- Le **superviseur est le seul propriétaire** de la réparation ; la file est
  dans `registry.db` (`repair_jobs`), pas dans une base séparée.
- Les tentatives sont bornées (`max_repair_attempts`, backoff, jitter) ;
  au-delà, l'incident est escaladé et visible, sans boucle infinie.
- Un incident par station est journalisé avec cause, preuve, action et
  résultat ; un incident `fleet` n'est utilisé que pour un vrai problème de
  flotte.
- Le watchdog ne redémarre **jamais** une station saine qui sert, pour
  accélérer le balayage.
- Le watchdog se surveille lui-même : heartbeat stale → redémarrage du
  watchdog seul, pas des stations.
- La découverte est un **scheduler** : API (TTL 6 h) → cache → embedded →
  manifeste ; run persistant, `stale` explicite, jamais un catalogue vide.
- `ops/lifecycle.py`, `ops/reconciler.py`, `ops/supervisor.py`,
  `free/station_health.py` et `vpn/manager.py` sont **réutilisés** par
  strangler ; aucun fork de manager.

### Multiplateforme

- Le domaine, le store, le scorer et le balayage tournent sur **Windows, Linux
  et macOS** ; aucun chemin de code n'est réservé à un OS.
- Aucune API spécifique (`winreg`, `msvcrt`, `fcntl`, `signal.SIGBREAK`)
  n'entre dans le domaine ; les différences passent par un adapter de
  plateforme.
- Les chemins sont `pathlib`, les exécutables sont résolus par
  `sys.executable`/`shutil.which` ; aucun `C:\`, `\` ou `.exe` en dur.
- Les commandes système sont des listes d'arguments ; `shell=True`,
  `os.system` et `os.popen` sont interdits dans le service.

### Exécution silencieuse

- Aucun processus lancé par le balayage, le probe ou une rotation n'ouvre de
  fenêtre ni ne vole le focus.
- Sur Windows, tout lancement non interactif passe par
  `core.process.run_hidden`/`popen_hidden` (`CREATE_NO_WINDOW`) ; sur POSIX,
  les enfants détachés utilisent `start_new_session=True` et
  `stdin=DEVNULL`.
- Aucun `cmd.exe`, `start`, `powershell.exe` visible ou TTY interactif.
- `scripts/platform_audit.py` échoue si un module runtime viole la règle.

### Performance

- Un preflight de 500 hostnames s'exécute en moins de 5 min avec 6 workers.
- Un balayage complet des 7 070 hostnames tient dans une fenêtre de 24 h avec
  six stations, ou expose explicitement la projection et la raison du retard.
- Le chemin de requête ne fait jamais de DNS, de bind, de probe ou d'écriture
  SQLite lourde.
- API de couverture : p95 < 500 ms pour 1 000 lignes ; UI p95 < 2 s.
- Mémoire et disque bornés : batchs, index, rétention, pas de chargement
  intégral des 7 070 lignes dans le dashboard.
- Aucun chemin de requête ne fait de DNS, bind, probe, rotation ou écriture
  SQLite lourde ; ces opérations sont des jobs de fond.
- Les budgets sont testés en benchmark/soak, pas seulement documentés :
  preflight, balayage, cutover, rotation, API, UI.
- Les requêtes SQL sont paginées et indexées ; aucun N+1 sur la file, les
  tentatives ou les incidents.

### Maintenabilité

- Modules < 1 500 lignes ; domaine pur sans I/O ; adapters isolés.
- Un seul scorer de potentiel, un seul scorer mesuré, un seul classifieur de
  résultat ; pas de quatrième formule parallèle.
- Chaque composant est testable sans Docker, avec un store et un probe
  factices.
- Toute option de configuration ou de GUI a un contrat
  `surface → clé → consommateur → effet observable → test`
  (constitution XII).
- Une option sans consommateur est supprimée ou explicitement marquée
  « affichage seul ».
- `config_audit.py` reste à 0 dead / 0 missing.
- Couverture de tests ≥ 85 % sur le domaine et l'application ; 100 % sur les
  chemins critiques (classification, quota, cutover, watchdog, reprise).
- Type checking et lint à 0 erreur sur les nouveaux modules ; complexité
  cyclomatique bornée ; aucune duplication de scorer/classifieur/machine à
  états.
- Chaque port a un **contract test** ; chaque adapter a un test avec fake.
- ADR et `debt.md` à jour : 0 P0 ouverte aux gates, P1 planifiée, aucune
  dette non listée.
- Runbook opérateur : démarrer, pause, reprendre, diagnostiquer, rollback.

### Modularité

- Ajouter une source de catalogue = ajouter un adapter, pas modifier le
  balayage.
- Ajouter un provider = implémenter l'interface de qualification sans modifier
  le store ni le scheduler.
- Le pool ne dépend pas des détails de la file : il reçoit des options prêtes à
  servir.
- Les exports JSON restent remplaçables ; `registry.db` est l'autorité
  (constitution III).
- Règle de dépendance : domaine → application → adapters ; le domaine
  n'importe ni Docker, ni réseau, ni provider, ni SQLite.
- Les ports sont explicites (`CatalogSource`, `QualificationStore`,
  `TransportPlanner`, `StationBinder`, `Prober`, `QuotaLedger`, `Watchdog`,
  `RepairQueue`, `Clock`) et testables avec des fakes.
- Aucun cycle de dépendance ; aucun fork de `vpn/manager.py`, `ops/lifecycle.py`,
  `ops/reconciler.py` ou `ops/supervisor.py`.
- Un seul scorer de potentiel, un seul scorer mesuré, un seul classifieur,
  une seule machine à états de station, un seul ledger de quota.
- Ajouter une source de catalogue ou un provider ne doit toucher qu'un
  adapter, pas le store, le sweep ou le dashboard.
- **Strangler avec ratchet** : aucun nouveau module > 1 500 lignes ; un module
  legacy touché ne doit pas grossir au-delà de sa baseline ; les monolithes
  `opencode.py`, `vpn/manager.py`, `free/pool.py` et `dashboard/api.py` sont
  découpés progressivement (US12), pas réécrits en big-bang.
- La couverture ≥ 85 % s'applique au **nouveau** domaine/application, pas au
  legacy complet (dont la baseline est de 38 %).

### Démantèlement des monolithes

- Un **inventaire des seams** et un graphe de dépendances précèdent toute
  extraction ; chaque seam a un contrat et des tests de caractérisation.
- Chaque extraction est un **strangler** : shadow, canary, rollback, aucun
  changement de comportement.
- `vpn/manager.py` est découpé en cycle de vie station, AUTH/session,
  transport/bind, santé/probe et rotation ; le fichier reste un adapteur fin.
- `free/pool.py` est découpé en sélection, quota, rotation, pool public et
  accounting.
- `scripts/fresh_chain.py` est remplacé par `free/qualification/sweep.py` et
  une CLI fine ; le mode `--repair` reste un wrapper.
- `dashboard/api.py` est découpé en routers et read models ; aucun SQL dans les
  routes.
- `opencode.py` devient un **composition root**/entrypoint ; le pipeline de
  requête, la jambe free et le routage modèle sont extraits.
- Chaque module cible MUST être < 1 500 lignes ou une entrée fine ; la parité
  de performance MUST être ≤ +5 % sur p95.
- Les dettes D-02 (monolithes) et D-20 (dashboard lourd) sont closes à la fin
  du démantèlement.

### Observabilité et qualité des données

- Chaque hostname expose `potential_score`, `measured_score`, `attempts`,
  `last_attempt_at`, `last_result`, `next_eligible_at`, `last_egress_ip`.
- Chaque tentative expose `hostname`, `station`, `transport`, `status`,
  `egress_ip`, `latency_ms`, `rtt_ms`, `reset_at`, `error`, `run_id`.
- Les métriques distinguent `tested`, `200`, `429`, `no_egress`, `bind_error`,
  `auth`, `dead`, `skipped`, `pending`.
- Un dashboard sans preuve est un bug : aucune couverture ne s'affiche sans
  époque, total et âge du dernier test.
- Chaque décision (claim, bind, promote, quota, rotate, repair, skip) écrit
  une ligne structurée avec `run_id`, `station`, `hostname`, `transport`,
  `cause`, `evidence`, `result`.
- Les métriques couvrent les cinq attributs : fiabilité (SLO, incidents,
  heartbeat), performance (latences, débits, files), qualité (200/429/infra,
  faux `dead`, preuves), modularité (adapters, erreurs de contrat) et
  maintenabilité (tests, dette, config).
- Un log, une métrique ou une réponse API ne contient jamais de secret, de
  cookie, de clé ou de header de compte.
- `observations.provider` n'est jamais vide.

## Non-objectifs

- Augmenter le nombre de stations au-delà de six ; la ressource rare est la
  session, pas la file.
- Acheter des IP, ouvrir un second compte ou attendre comme solution.
- Garantir un 200 pour chaque IP ; certaines IP sont réellement épuisées ou
  mortes.
- Maintenir une quota par pays ou par continent.
- Tester les serveurs dédiés qui refusent la jambe free ; ils sont `skipped`
  avec une raison.
- Remplacer WARP ou le public ; la spec ne concerne que la jambe NordVPN.
- Router du trafic payant par un egress free.
- Sacrifier une station, la laisser idle ou la couper pour accélérer le
  balayage.
- Sérialiser les rotations ou imposer une pause globale pour simplifier le
  code.
- Promettre zéro coupure sans second tunnel, standby ou preuve de bascule : la
  spec exige la mesure et le best-effort, pas une affirmation.
- Utiliser un seul transport fixe en mode automatique, ou déclarer un hostname
  mort après un seul échec de transport.
- Réécrire les monolithes en **big-bang** ; ils sont découpés progressivement
  par strangler (US12), avec tests de caractérisation, shadow, canary et
  rollback.
- Créer un second watchdog ou une seconde file de réparation parallèle ; le
  superviseur reste le seul propriétaire.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Une file de qualification classée par potentiel (Priority: P1)

L'opérateur ouvre le dashboard et voit les 7 070 serveurs en ligne classés de
la meilleure à la moins bonne par un score de potentiel : taux de 200 observé,
ping depuis l'opérateur, quota restant, charge API, fraîcheur. Chaque ligne
indique son état, son dernier résultat et son prochain créneau.

**Why this priority**: sans file classée, le scanner choisit un hostname sur
une priorité statique et ne peut pas dire pourquoi.

**Independent Test**: injecter un catalogue de 10 serveurs avec des charges,
des RTT et des historiques différents ; vérifier que l'ordre de la file est
exactement l'ordre du score de potentiel, puis quota, RTT, charge, hostname.

**Acceptance Scenarios**:

1. **Given** un catalogue vide, **When** l'ingestion lit l'API, **Then**
   chaque serveur en ligne a une ligne de file avec `hostname`, `country`,
   `load`, `transports`, `source_id` et `potential_score`.
2. **Given** deux serveurs de même score, **When** la file est ordonnée,
   **Then** celui qui a le plus de quota restant passe devant, puis le RTT le
   plus faible, puis la charge la plus faible, puis le hostname.
3. **Given** un serveur déjà connu, **When** l'API est rafraîchie, **Then** sa
   ligne est mise à jour sans doublon et sa fraîcheur est recalculée.

---

### User Story 2 — Un balayage exhaustif par époque, reprenable (Priority: P1)

Le balayage parcourt toute la file, hostname par hostname, et enregistre pour
chacun un état terminal : `200`, `429`, `no_egress`/`bind_error`, `auth`,
`dead` ou `skipped` avec raison. Une époque se termine lorsque tous les
hostnames éligibles ont un état terminal ou un `next_eligible_at` documenté.
Un crash reprend au curseur, sans recommencer.

**Why this priority**: c'est la définition même de « toutes les IP sont
testées ». Sans époque ni curseur, la couverture n'est pas mesurable.

**Independent Test**: lancer une époque sur 20 hostnames factices, tuer le
processus après 10, relancer ; vérifier que les 10 premiers ne sont pas
re-testés et que les 10 suivants le sont.

**Acceptance Scenarios**:

1. **Given** une époque ouverte, **When** un hostname reçoit un 200, **Then**
   il est marqué `usable` et le balayage **continue** avec le suivant.
2. **Given** une époque ouverte, **When** un hostname reçoit un 429, **Then**
   il est marqué `quota` avec le `reset_at` fournisseur et le balayage
   continue.
3. **Given** un crash au milieu d'une époque, **When** le service redémarre,
   **Then** le curseur reprend au prochain hostname non terminal et aucun
   doublon n'est créé.
4. **Given** une époque terminée, **When** l'opérateur la consulte, **Then**
   elle affiche total, testés, 200, 429, infra, skips et durée.

---

### User Story 3 — Consommer le quota, puis basculer automatiquement (Priority: P1)

Une IP qui répond 200 sert le trafic free. Quand elle renvoie 429, elle passe
en `quota` avec son `reset_at`, et la station claim immédiatement le meilleur
candidat suivant. Le quota de chaque IP est ainsi consommé, puis l'IP revient
à son reset sans être re-testée avant.

**Why this priority**: l'objectif est d'utiliser chaque IP jusqu'à son quota,
pas de la tester une fois et de l'oublier.

**Independent Test**: simuler une IP 200 puis 429 ; vérifier que l'option passe
`quota` avec le reset exact, que la station sélectionne le candidat suivant en
moins de 60 s, et qu'aucune tentative n'est faite avant le reset.

**Acceptance Scenarios**:

1. **Given** une IP `usable`, **When** elle renvoie 429, **Then** elle devient
   `quota` avec `reset_at` et n'est plus sélectionnée avant ce timestamp.
2. **Given** une station qui vient de perdre son IP, **When** elle demande une
   rotation, **Then** elle reçoit le meilleur candidat `pending` ou `quota`
   dont le reset est passé, dans la limite de six sessions.
3. **Given** toutes les IP connues en `quota`, **When** une rotation est
   demandée, **Then** le système expose la pénurie et la prochaine échéance,
   sans fabriquer un 200 ni passer au payant.

---

### User Story 4 — Classer sans condamner : bind, quota, auth, dead (Priority: P1)

Le système distingue un échec d'infrastructure (`no_egress`, `bind_error`,
timeout), un quota (`429`), une authentification (`AUTH_FAILED`) et un host
réellement mort (`NXDOMAIN`, hostname invalide) ; un host retiré du catalogue
est `skipped` (`removed-from-catalog`). Un `no_egress` ne condamne pas le
hostname : il change de transport ou de station, avec un backoff borné.
`auth` condamne la station, pas le host. Aucun pays n'est une porte dure.

**Why this priority**: 85 % des tentatives sont des `no_egress` ; les compter
comme des échecs de hostname parque le catalogue et fausse le score.

**Independent Test**: injecter quatre hosts avec `no_egress`, `429`, `AUTH` et
`NXDOMAIN` ; vérifier les quatre états et qu'aucun pays n'est bloqué.

**Acceptance Scenarios**:

1. **Given** un `no_egress` sur WireGuard, **When** le host supporte OpenVPN,
   **Then** il est re-tenté sur OpenVPN avant tout backoff long.
2. **Given** un `AUTH_FAILED`, **When** la station est en quarantaine, **Then**
   ses hostnames retournent `pending` après réparation et aucun n'est `dead`.
3. **Given** un host retiré du catalogue, **When** l'ingestion suivante passe,
   **Then** il est `skipped` avec `removed-from-catalog`, pas `no_egress`.
4. **Given** 3 × 429 dans un pays, **When** la file est réordonnée, **Then**
   aucun hostname du pays n'est bloqué ; seules les IP concernées sont `quota`.

---

### User Story 5 — Mesurer le ping depuis l'opérateur (Priority: P2)

Avant le bind, le système résout le hostname, ouvre l'endpoint et, si possible,
fait un handshake. Il enregistre DNS, connect et RTT. Ces mesures, prises
depuis l'hôte de l'opérateur, alimentent le score de connexion. Après un 200,
la latence through-tunnel complète le score mesuré.

**Why this priority**: « meilleur ping par rapport à l'opérateur » est un
critère explicite ; un host rapide depuis un autre continent peut être lent
depuis l'opérateur.

**Independent Test**: injecter deux hosts avec des RTT préflight 20 ms et
200 ms ; vérifier que le premier passe devant à charge et quota égaux.

**Acceptance Scenarios**:

1. **Given** un host résolu et joignable, **When** le preflight passe, **Then**
   `dns_ms`, `connect_ms`, `rtt_ms` sont enregistrés avec la source et
   l'horodatage.
2. **Given** un host dont le preflight échoue, **When** le balayage le traite,
   **Then** il est marqué `unreachable` (infra) et ne consomme pas de session.
3. **Given** un 200, **When** la latence through-tunnel est mesurée, **Then**
   elle remplace la valeur préflight dans le score mesuré, sans supprimer la
   mesure préflight.

---

### User Story 6 — Voir la couverture et les prochains candidats (Priority: P2)

Le dashboard expose, par provider et par pays : total, `pending`, `usable`,
`quota`, `auth`, `dead`, `skipped`, testés / total, âge du dernier test, débit
du balayage, prochaine échéance de reset, et les 20 prochains candidats avec
leur score. Un bouton permet de lancer ou de mettre en pause une époque.

**Independent Test**: injecter 100 hosts dans cinq états ; vérifier que l'API
retourne les compteurs exacts et les 20 prochains candidats dans l'ordre.

---

### User Story 7 — Aucune option de configuration inerte (Priority: P1)

Chaque option du balayage a un consommateur runtime et un test d'effet :
`max_attempts_per_host_per_epoch`, `sweep_slice_size`,
`preflight_timeout_s`, `latency_budget_ms`, `weights`, `infra_backoff_s`,
`quota_retry_jitter_s`, `max_active_stations`. Modifier une valeur change
l'ordre, le rythme ou le résultat observable.

**Why this priority**: une option inerte fait croire que le système est
réglable alors qu'il ne l'est pas (constitution XII).

**Independent Test**: pour chaque ligne de `contracts/option-map.md`, modifier
la valeur, recharger, vérifier l'effet et le test.

---

### User Story 8 — Six stations, zéro collision, reprise après crash (Priority: P2)

Six stations maximum partagent la file. Chaque hostname est claimé par une
seule station à la fois ; une IP d'egress n'est jamais partagée par deux
stations. Un redémarrage du superviseur reprend les leases expirés et remet
les hostnames `claimed` orphelins en `pending`.

**Independent Test**: lancer six workers sur 60 hosts ; vérifier qu'aucun host
n'est claimé deux fois et qu'après un kill -9, les leases expirés sont
récupérés.

---

### User Story 9 — Six stations indépendantes, en parallèle, sans coupure (Priority: P1, transverse)

Les six stations sont des ressources indépendantes. Chacune a son worker, son
état (`serving`, `qualifying`, `rotating`, `repairing`), son lease et son
standby. Une station qui qualifie, se répare ou tourne ne pause ni ne ralentit
les autres. Quand une IP approche de son quota, la station prépare un standby
et bascule en **make-before-break** : l'ancienne IP sert jusqu'à ce que la
nouvelle ait prouvé un 200 ; si la nouvelle échoue, l'ancienne est restaurée.
La coupure est nulle si un second tunnel/espace de noms est disponible, sinon
bornée et mesurée.

**Why this priority**: la disponibilité des six stations est l'objectif
permanent ; une rotation qui coupe une station ou qui attend les autres est une
perte de service.

**Independent Test**: six stations avec six IP 200 ; provoquer un 429 sur la
station 3 et un `AUTH_FAILED` sur la station 5 ; vérifier que les stations 1,
2, 4 et 6 ne perdent aucune requête, que 3 bascule sur son standby en p95
< 2 s, et que 5 est réparée sans affecter les autres.

**Acceptance Scenarios**:

1. **Given** six stations qui servent, **When** l'une reçoit un 429, **Then**
   seule cette station tourne ; les cinq autres continuent de servir ; le
   candidat suivant est claimé en parallèle.
2. **Given** un standby `usable`, **When** la station tourne, **Then**
   l'ancienne IP sert jusqu'au 200 de la nouvelle ; la coupure est nulle si un
   second tunnel est disponible, sinon p95 < 2 s ; rollback si le bind échoue.
3. **Given** une station en `AUTH_FAILED`, **When** elle est réparée, **Then**
   les cinq autres ne sont pas affectées et ses hostnames retournent dans la
   file partagée.
4. **Given** une rafale de 429 sur une IP, **When** le seuil global serait
   atteint, **Then** seule l'IP/station concernée est mise en pause ; aucune
   pause globale n'est appliquée.
5. **Given** deux stations qui ont besoin de tourner, **When** elles le
   demandent, **Then** elles tournent en parallèle ; le rate limit global de
   rebind s'applique par minute, pas une sérialisation.
6. **Given** aucune standby, **When** une rotation est nécessaire, **Then** la
   station expose la fenêtre de coupure et sa raison ; elle ne coupe pas
   silencieusement le trafic.
7. **Given** la dernière station utilisable, **When** une rotation est
   demandée, **Then** elle est différée tant qu'un standby n'est pas validé ;
   le système ne descend pas à 0/6.

---

### User Story 10 — Stratégie automatique multi-transports, cyclique (Priority: P1)

En mode `auto`, la station ne se limite pas à WireGuard. Pour chaque hostname,
elle essaie **tous les transports supportés** — WireGuard, OpenVPN UDP,
OpenVPN TCP — dans un ordre configurable. Si WireGuard échoue, elle passe à
OpenVPN UDP puis TCP ; si les trois échouent, elle **reboucle sur WireGuard**
après un backoff, pour un nombre de rounds borné. Le curseur de transport et
le round sont persistés : une reprise continue le cycle. Chaque transport est
mesuré séparément (200, 429, infra, latence) ; un transport qui ne produit
aucun 200 sur un minimum d'essais est signalé dégradé, pas retenté à
l'aveugle.

Chaque station peut utiliser ces trois transports **indépendamment** : aucun
bail global WireGuard, aucune clé partagée et aucun verrou inter-stations ne
limite l'usage simultané d'un transport par plusieurs stations. La seule
exclusivité globale est l'IP d'egress.

**Why this priority**: un blocage UDP WireGuard ne doit pas faire passer un
hostname pour mort alors qu'OpenVPN TCP peut fonctionner ; inversement, un
chemin OpenVPN cassé doit être diagnostiqué, pas masqué par des 429.

**Independent Test**: simuler un hostname qui supporte les trois transports :
WireGuard → `no_egress`, OpenVPN UDP → `429`, OpenVPN TCP → `200`. Vérifier
l'ordre exact. Puis simuler trois échecs et vérifier le retour à WireGuard en
round 2, sans `dead`.

**Acceptance Scenarios**:

1. **Given** un hostname qui supporte les trois transports et le mode `auto`,
   **When** la qualification démarre, **Then** les tentatives se succèdent
   dans l'ordre `wireguard`, `openvpn-udp`, `openvpn-tcp`.
2. **Given** WireGuard en `no_egress`, **When** OpenVPN UDP est supporté,
   **Then** il est essayé avant tout backoff de hostname.
3. **Given** OpenVPN UDP en `429`, **Then** OpenVPN TCP est quand même essayé ;
   le 429 d'un transport ne saute pas les autres.
4. **Given** les trois transports en échec, **When** le round est terminé,
   **Then** WireGuard est re-testé au round suivant après backoff, et le
   hostname n'est pas `dead`.
5. **Given** un transport non supporté par le hostname, **Then** il est
   `skipped` avec `unsupported-transport`, sans compter comme un échec.
6. **Given** le mode `manuel`, **Then** seul le transport choisi est utilisé ;
   le cycle multi-transports ne s'applique qu'à `auto`.
7. **Given** OpenVPN avec 0 succès sur `transport_health_min_attempts`,
   **Then** le transport est marqué `degraded` et exposé ; le hostname n'est
   pas condamné et le diagnostic (bind/AUTH/probe/DNS) est visible.
8. **Given** une reprise après crash au milieu d'un cycle, **Then** le
   curseur reprend au transport/round suivants, sans rejouer les précédents.

---

### User Story 11 — Watchdog, gestion et découverte de bout en bout (Priority: P1)

Le watchdog surveille chaque station — tunnel, DNS, egress, AUTH, quota,
`client_usable` — et déclenche une réparation **locale** à la station
concernée. La découverte est un scheduler qui rafraîchit le catalogue et les
transports. `ops/lifecycle.py`, `ops/reconciler.py`, `ops/supervisor.py` et
`free/station_health.py` sont réutilisés et branchés sur `registry.db` ;
`vpn/manager.py` reste l'adapteur. L'opérateur voit l'état de chaque station,
ses incidents et ses réparations, sans attendre un incident de flotte.

**Why this priority**: sans watchdog par station, une panne locale devient une
panne de flotte ; sans découverte schedulée, la file se fige sur un catalogue
périmé.

**Independent Test**: six stations ; tuer le tunnel de la station 2 ; vérifier
que le watchdog la détecte en < 30 s, ne répare qu'elle, et que les cinq autres
continuent. Couper l'API catalogue ; vérifier que la découverte utilise le
cache/embedded et marque `stale`, sans vider la file. Redémarrer le watchdog ;
vérifier que l'état et les incidents persistent.

**Acceptance Scenarios**:

1. **Given** le tunnel de la station 2 tombe, **When** le watchdog sonde,
   **Then** un incident `scope='station'` est créé, seule la station 2 est
   réparée, et les stations 1/3/4/5/6 ne changent pas d'état.
2. **Given** un `AUTH_FAILED` sur la station 2, **Then** elle est mise en
   quarantaine, ses hostnames retournent `pending`, et aucun n'est `dead`.
3. **Given** un DNS cassé mais un tunnel up, **Then** le fallback DNS est
   utilisé avant tout recreate de la station.
4. **Given** un `no_egress`, **Then** la station rebinde ou change de
   transport ; l'IP n'est pas condamnée.
5. **Given** une réparation qui échoue `max_repair_attempts` fois, **Then**
   l'incident est escaladé, visible, et la boucle s'arrête ; aucune autre
   station n'est touchée.
6. **Given** une station saine qui sert, **When** le balayage veut avancer,
   **Then** le watchdog ne la redémarre pas ; le balayage attend ou utilise
   une autre station.
7. **Given** l'API catalogue indisponible, **When** la découverte tourne,
   **Then** le cache/embedded complète, `stale` est exposé et la file n'est
   pas vidée.
8. **Given** un nouveau serveur dans l'API, **When** la découverte tourne,
   **Then** il entre `pending` avec ses transports ; un serveur retiré devient
   `skipped`.
9. **Given** le heartbeat du watchdog stale, **When** le superviseur le
   détecte, **Then** il redémarre le watchdog seul ; les stations continuent
   de servir.
10. **Given** un redémarrage OS, **Then** `station_state`, `incidents`,
    `repair_jobs` et les curseurs persistent ; aucune réparation n'est rejouée
    en double.

---

### User Story 12 — Démantèlement progressif des monolithes (Priority: P1)

Les monolithes `opencode.py` (31 347 lignes), `vpn/manager.py` (11 385),
`free/pool.py` (8 190) et `dashboard/api.py` (6 845) sont découpés par seams,
sans big-bang. Chaque extraction a des tests de caractérisation, un shadow, un
canary et un rollback. `scripts/fresh_chain.py` est remplacé par le sweep
applicatif ; `opencode.py` devient un composition root. À la fin, chaque
module est < 1 500 lignes ou une entrée fine, les dettes D-02/D-20 sont closes
et la parité de comportement/performance est prouvée.

**Why this priority**: la fiabilité, la performance et la maintenabilité à long
terme sont impossibles si les chemins critiques restent dans des fichiers de
11 000 à 31 000 lignes.

**Independent Test**: extraire un seam (cycle de vie de station) de
`vpn/manager.py`, exécuter la suite de caractérisation + shadow, vérifier
0 régression, module < 1 500 lignes, rollback opérationnel.

**Acceptance Scenarios**:

1. **Given** le début du chantier, **When** l'inventaire est fait, **Then** le
   graphe de dépendances, les seams, les contrats et l'ADR cible sont publiés.
2. **Given** `vpn/manager.py`, **When** l'extraction est terminée, **Then** il
   reste un adapteur fin ; cycle de vie, AUTH/session, transport/bind,
   santé/probe et rotation sont des modules testés.
3. **Given** `free/pool.py`, **When** l'extraction est terminée, **Then**
   sélection, quota, rotation, pool public et accounting sont séparés.
4. **Given** `scripts/fresh_chain.py`, **When** le sweep applicatif est livré,
   **Then** la CLI est fine et `--repair` reste un wrapper.
5. **Given** `dashboard/api.py`, **When** les routers/read models sont livrés,
   **Then** aucune route ne contient de SQL.
6. **Given** `opencode.py`, **When** le composition root est livré, **Then**
   le pipeline de requête, la jambe free et le routage modèle sont extraits.
7. **Given** une extraction, **When** elle est déployée, **Then** shadow +
   canary + rollback sont passés, et p95 ne régresse pas de plus de 5 %.
8. **Given** la fin du chantier, **Then** D-02/D-20 sont closes, 0 cycle,
   couverture ≥ 85 % des modules extraits, aucun module > 1 500 lignes.

---

### Edge Cases

- Un hostname rend une IP déjà connue par une autre station : l'option est
  fusionnée par IP, pas dupliquée ; la deuxième station ne peut pas la prendre.
- Un hostname rend une nouvelle IP après un rebind : l'ancienne IP garde son
  état et son reset, la nouvelle est ajoutée.
- Un 429 sans 200 antérieur : l'IP est mauvaise, pas un quota consommé
  (constitution IV) ; elle reçoit un backoff court et n'est pas comptée comme
  un succès.
- Un 429 avec `Retry-After` absent : le reset est inconnu ; l'IP est mise en
  quarantaine avec le plafond de politique, jamais avant.
- Un hostname disparaît de l'API en cours d'époque : il est `skipped`
  (`removed-from-catalog`), pas `dead`.
- Un hostname réapparaît : il repasse `pending` avec un potentiel recalculé.
- Le catalogue API est indisponible : le cache et l'embedded complètent ; les
  lignes manquantes sont marquées `stale`, jamais supprimées.
- Toutes les IP sont `quota` : la file expose la prochaine échéance et le
  système ne sélectionne rien avant.
- Une station tombe pendant un bind : le lease expire, le hostname retourne
  `pending`, la station est réparée par le superviseur.
- Deux stations demandent une rotation en même temps : elles tournent en
  parallèle ; le rate limit global de rebind s'applique par minute, pas une
  file d'attente globale.
- Une station n'a pas de standby : elle ne rebinde pas une IP encore utilisable
  sans raison ; si elle doit tourner, elle expose la fenêtre de coupure et sa
  raison.
- Un second tunnel/espace de noms n'est pas disponible : la coupure est bornée
  (p95 < 2 s, max < 5 s), mesurée et affichée ; elle n'est pas niée.
- Une station est la dernière utilisable : sa rotation est différée tant qu'un
  standby n'est pas validé ; le système ne descend pas à 0/6.
- WireGuard échoue mais OpenVPN TCP répond 200 : le hostname est `usable` sur
  TCP ; WireGuard reste `infra` pour ce host, pas `dead`.
- Les trois transports renvoient 429 sur la même IP : l'IP est `quota` une
  seule fois ; le hostname est `quota` avec le plus tôt des resets, pas trois
  lignes concurrentes.
- OpenVPN a 0 200 sur `transport_health_min_attempts` : le transport est
  `degraded`, le hostname reste `pending`/`infra`, et le diagnostic est visible.
- Un transport n'est pas supporté par le hostname : `skipped`
  (`unsupported-transport`), sans consommer de session.
- Le disque est plein : le store échoue proprement, la file précédente reste
  lisible, aucune option n'est promue sans observation.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Le système MUST ingérer dans la file chaque serveur `online` du
  catalogue API (7 070 au moment de l'audit), dédupliqué par `hostname`, avec
  `country`, `city`, `load`, `status`, `transports` et `source_id`.
- **FR-002**: Le système MUST compléter les hostnames absents de l'API avec le
  catalogue Gluetun/embedded **uniquement s'ils sont acceptés par le manifeste
  runtime**, en marquant `stale` et `source_id`, sans remplacer une ligne API
  fraîche.
- **FR-003**: Chaque ligne de file MUST porter `hostname`, `provider`,
  `country`, `city`, `load`, `transports`, `potential_score`,
  `measured_score`, `status`, `attempts`, `last_attempt_at`, `last_result`,
  `last_egress_ip`, `next_eligible_at`, `epoch`, `source_id`.
- **FR-004**: Le score de potentiel MUST être calculé avant la première mesure
  à partir de : taux de 200 observé (Laplace), RTT opérateur, quota restant
  connu, charge API, fraîcheur. Le pays et la source MUST NOT être des
  facteurs de score.
- **FR-005**: Le RTT opérateur MUST être mesuré depuis l'hôte de l'opérateur
  par `PreflightProber` (DNS, connect, handshake), avec au moins trois
  échantillons et une médiane, avant le bind.
- **FR-006**: L'ordre de la file MUST être : `potential_score DESC`, puis
  quota restant `DESC`, puis `rtt_ms ASC`, puis `load ASC`, puis `hostname ASC`.
- **FR-007**: Les portes dures MUST s'appliquer avant l'ordre :
  `next_eligible_at <= now`, `reset_at <= now`, statut `pending` ou `quota`
  reseté pour la qualification (un `usable` n'est jamais re-testé), transport
  supporté, hostname non denylisté, non claimé par une autre station.
- **FR-008**: Le système MUST maintenir une **époque** de balayage avec
  `epoch_id`, `started_at`, `finished_at`, `status`, `total`, `tested`,
  `usable`, `quota`, `infra`, `dead`, `skipped` et un curseur persistant.
- **FR-009**: Chaque hostname éligible MUST recevoir au moins une tentative par
  époque, avec un maximum configurable (`max_attempts_per_host_per_epoch`,
  défaut 3) et un `next_eligible_at` explicite.
- **FR-010**: Un `200` MUST promouvoir le hostname en `usable`, créer ou
  mettre à jour l'option d'egress avec l'IP observée, et le balayage MUST
  continuer avec le hostname suivant.
- **FR-011**: Un `429` MUST marquer l'IP `quota` avec le `reset_at` fournisseur
  exact ; il MUST NOT être enregistré comme `dead`.
- **FR-012**: Un `no_egress` ou `bind_error` MUST être classé comme échec
  d'infrastructure : retry sur un autre transport/station, backoff borné,
  jamais `dead` pour le hostname.
- **FR-013**: Un `AUTH_FAILED` MUST mettre la station en quarantaine et
  remettre ses hostnames en `pending` après réparation ; il MUST NOT
  condamner le hostname.
- **FR-014**: Un `dead` MUST être réservé aux échecs terminaux du hostname
  (`NXDOMAIN`, transport définitivement absent, hostname invalide) avec une
  raison explicite ; un host retiré du catalogue est `skipped`
  (`removed-from-catalog`).
- **FR-015**: À chaque `429` d'une IP en service, la station MUST libérer
  l'option et claim le meilleur candidat suivant en moins de 60 s (p95), sans
  attendre le reset de l'IP perdue.
- **FR-016**: Le système MUST tenir un ledger de quota par IP
  (`egress_ips`/`quota_buckets`) et MUST NOT sélectionner une IP dont le
  `reset_at` est futur.
- **FR-017**: Un `429` sans succès antérieur MUST être traité comme une
  mauvaise IP, pas comme un quota consommé (constitution IV) ; il MUST
  néanmoins respecter le `reset_at` fournisseur.
- **FR-018**: Deux stations MUST NOT partager la même IP d'egress ; l'unicité
  est vérifiée avant le claim et à la promotion.
- **FR-019**: Le nombre de stations simultanées MUST rester borné par
  `max_active_stations` (6) ; la file peut contenir 7 070 hostnames.
- **FR-020**: Le balayage MUST s'exécuter en tâche de fond, jamais dans le
  chemin de requête, et MUST être fail-soft.
- **FR-021**: Aucun pays ne MUST être une porte dure ; `country_backoff` MUST
  être supprimé et remplacé par un backoff par IP ou par hostname.
- **FR-022**: Le catalogue MUST être rafraîchi avec un TTL de 6 h (API), un
  cache et un embedded en fallback ; une source indisponible MUST conserver
  les lignes précédentes avec `stale`.
- **FR-023**: Un hostname retiré du catalogue MUST être `skipped`
  (`removed-from-catalog`), pas `dead` ni `no_egress`.
- **FR-024**: Chaque tentative MUST être append-only dans
  `qualification_attempts` avec `provider`, `hostname`, `station`,
  `transport`, `status`, `egress_ip`, `latency_ms`, `rtt_ms`, `reset_at`,
  `error`, `run_id`.
- **FR-025**: L'API MUST exposer la couverture par provider, pays et
  transport : total, `pending`, `usable`, `quota`, `auth`, `dead`, `skipped`,
  testés / total, âge du dernier test, débit, prochains candidats.
- **FR-026**: L'UI MUST afficher la couverture, l'époque courante, les 20
  prochains candidats et un contrôle lancer/pause.
- **FR-027**: Toute option `egress_catalog.qualification.*` MUST avoir un
  consommateur runtime, un effet observable et un test.
- **FR-028**: Un gate MUST échouer si une clé de qualification n'a pas de
  ligne dans `contracts/option-map.md` ou si un contrôle GUI est inerte.
- **FR-029**: Le balayage MUST être idempotent : un redémarrage au milieu d'un
  run ne crée ni doublon ni hostname sans observation.
- **FR-030**: Le store MUST borner la mémoire/disque (batchs, index,
  rétention) et ne jamais charger les 7 070 lignes dans une seule requête UI.
- **FR-031**: Chaque composant MUST être testable sans Docker, avec un store
  et un probe factices.
- **FR-032**: Le système MUST fonctionner sur Windows, Linux et macOS ;
  aucune fonctionnalité ne MUST dépendre d'un seul OS.
- **FR-033**: Aucun processus lancé par le service MUST ouvrir de fenêtre ou
  de console visible ; les lancements non interactifs MUST passer par
  `core.process.run_hidden`/`popen_hidden`.
- **FR-034**: `scripts/platform_audit.py --strict` MUST échouer si un module
  runtime utilise `os.system`, `os.popen`, `shell=True` ou un lancement sans
  politique silencieuse.
- **FR-035**: Les budgets de performance MUST être testés : preflight de 500
  hostnames < 5 min, API couverture p95 < 500 ms, UI p95 < 2 s, balayage
  complet < 24 h avec six stations.
- **FR-036**: Le système MUST filtrer les candidats contre la denylist et les
  flux de threat-intel avant tout bind.
- **FR-037**: Le système MUST exposer et respecter `reset_at` ; une IP en
  `quota` ne MUST pas être re-testée avant son reset, sauf action manuelle
  tracée.
- **FR-038**: Le système MUST conserver un historique borné des tentatives
  (au moins 7 jours) pour expliquer chaque décision.
- **FR-039**: Le système MUST NOT utiliser la jambe payante, un second compte
  ou l'attente comme substitut ; toute pénurie est exposée en free.
- **FR-040**: Ajouter une source de catalogue MUST se faire par configuration
  ou adapter, sans modifier le scorer, le store ou le balayage.
- **FR-041**: Chaque station MUST avoir son propre worker, son propre état
  (`serving`, `qualifying`, `rotating`, `repairing`), son lease et son curseur ;
  aucun lock global ne MUST sérialiser les stations.
- **FR-042**: Les six stations MUST être gérées en parallèle ; une station qui
  qualifie, se répare ou tourne MUST NOT pauser, ralentir ou sérialiser les
  cinq autres.
- **FR-043**: Les pauses globales (`global_429_pause_s`, `exhausted_pause_s`)
  MUST être supprimées ou limitées à la station/IP concernée ; aucun 429 ne
  MUST arrêter les six stations.
- **FR-044**: Chaque station MUST entretenir un standby `usable` quand il en
  existe un, et MUST tourner proactivement quand le headroom passe sous le
  seuil configurable (défaut 20 %), au lieu d'attendre le 429.
- **FR-045**: La rotation MUST être make-before-break : préflight de la cible,
  ancienne IP maintenue en service, preuve 200 de la nouvelle, puis commit ;
  rollback vers l'ancienne si le bind ou le probe échoue.
- **FR-046**: La coupure MUST être nulle si un second tunnel/espace de noms est
  disponible ; sinon p95 < 2 s et max < 5 s, avec durée et raison journalisées.
- **FR-047**: Chaque station MUST viser ≥ 99 % `client_usable` sur 24 h quand
  une IP 200 existe ; aucune station ne MUST être laissée idle ou sacrifiée
  pour le balayage.
- **FR-048**: Une quarantaine `AUTH_FAILED` ou un `bind_error` MUST être locale
  à la station ; les hostnames retournent dans la file partagée et les autres
  stations ne MUST pas être affectées.
- **FR-049**: Les six workers MUST claimer des hostnames distincts atomiquement ;
  le débit de qualification MUST croître avec le nombre de stations, sans
  doublon.
- **FR-050**: Le planificateur MUST autoriser des rotations concurrentes sur
  des stations différentes, sous un rate limit global de rebind ; la règle
  « une seule rotation à la fois » MUST être supprimée.
- **FR-051**: Une station sans standby MUST NOT rebinder une IP encore
  utilisable et en service sans décision explicite de l'opérateur ; la raison
  MUST être journalisée.
- **FR-052**: La dernière station utilisable MUST NOT tourner sans standby
  validé ; le système MUST NOT descendre à 0/6 par une rotation planifiée.
- **FR-053**: Un standby MUST être validé sur la station et le transport cibles
  par un 200 (ou un preflight + probe rapide) avant promotion ; une option
  `usable` validée sur une autre station ne suffit pas comme preuve.
- **FR-054**: Chaque station MUST exposer son état, son IP courante, son
  standby, la durée de cutover, sa disponibilité et la raison de la dernière
  rotation dans l'API et le dashboard.
- **FR-055**: La priorité service > repair > sweep MUST être évaluée par
  station ; le balayage MUST céder à une station qui sert, sans la pauser.
- **FR-056**: La disponibilité MUST être calculée depuis des sondes
  horodatées, par station et pour le 6/6 ; aucun chiffre déclaré n'est accepté
  (constitution I).
- **FR-057**: Une panne ou une réparation sur une station MUST NOT changer
  l'IP courante, le lease ou l'état d'une autre station.
- **FR-058**: Le claim d'un hostname MUST être atomique et le rate limit de
  rebind MUST être global ; aucun autre verrou global ne MUST exister dans le
  chemin de qualification ou de rotation.
- **FR-059**: En mode `auto`, le système MUST essayer tous les transports
  supportés par le hostname, dans l'ordre configurable
  `wireguard` → `openvpn-udp` → `openvpn-tcp` par défaut.
- **FR-060**: Un échec de transport (`no_egress`, `bind_error`, timeout) MUST
  déclencher le transport suivant pour le même hostname avant tout backoff de
  hostname.
- **FR-061**: Après le dernier transport, le système MUST reboucler sur le
  premier pour `transport_retry_rounds` (défaut 2), avec
  `transport_retry_backoff_s` (défaut 60) et un jitter.
- **FR-062**: Un `429` sur un transport MUST NOT sauter les autres transports ;
  chaque transport peut rendre une IP et un quota différents. Le hostname ne
  devient `quota` que si tous les transports supportés sont `quota`.
- **FR-063**: Un `AUTH_FAILED` MUST mettre la station en quarantaine, pas le
  hostname ; les autres transports et stations continuent.
- **FR-064**: Le curseur de transport et le round MUST être persistés par
  hostname ; une reprise MUST continuer le cycle, pas repartir à WireGuard.
- **FR-065**: Un hostname MUST NOT être `dead`/`skipped` après un seul échec
  de transport ; il ne le devient qu'après tous les transports supportés ×
  rounds, ou un échec terminal (`NXDOMAIN`, hostname invalide).
- **FR-066**: Les clés `transport_strategy`, `transport_order`,
  `transport_retry_rounds`, `transport_retry_backoff_s` et
  `transport_health_min_attempts` MUST être configurables ; le mode `manual`
  n'utilise que le transport choisi.
- **FR-067**: Le transport configuré de la station MUST être essayé en premier
  s'il est supporté, mais MUST NOT exclure les autres en mode `auto`.
- **FR-068**: Chaque tentative MUST être unique par
  `(hostname, transport, round)` ; aucun doublon dans un round.
- **FR-069**: Le dashboard MUST exposer, par transport, les 200, 429, infra,
  la latence et le taux de succès, ainsi que le curseur de transport par
  hostname.
- **FR-070**: Un transport avec 0 succès sur `transport_health_min_attempts`
  MUST être marqué `degraded` et exposé ; il MUST NOT être retenté comme s'il
  était sain.
- **FR-071**: Quand OpenVPN UDP/TCP a 0 succès, le système MUST exposer le
  diagnostic (bind, AUTH, probe, DNS, timeout) et MUST NOT condamner les
  hostnames à cause de ce transport.
- **FR-072**: Le raccourci `fast_discovery` à un seul transport MUST être
  supprimé en mode `auto` ; le mode `--repair` peut garder un chemin rapide,
  mais MUST essayer tous les transports avant de déclarer un hostname mort.
- **FR-073**: Aucun transport ne MUST être une exigence dure ; si WireGuard ou
  OpenVPN est indisponible, les autres transports et stations continuent.
- **FR-074**: Le watchdog MUST surveiller chaque station indépendamment
  (heartbeat, sonde tunnel/DNS/egress/AUTH/quota/`client_usable`) à intervalle
  configurable.
- **FR-075**: Le watchdog MUST distinguer `tunnel_down`, `dns_broken`,
  `no_egress`, `auth_failed`, `quota_429`, `client_unusable`, `stale` et
  `healthy`, avec une preuve horodatée par état.
- **FR-076**: Une panne locale MUST déclencher une réparation locale ; aucun
  restart, recreate ou pause des autres stations ne MUST être appliqué.
- **FR-077**: Le superviseur MUST rester le seul propriétaire de la
  réparation ; la file MUST vivre dans `registry.db` (`repair_jobs`), pas dans
  une base séparée.
- **FR-078**: Les réparations MUST être bornées (`max_repair_attempts`,
  backoff, jitter) ; au-delà, l'incident est escaladé et visible, sans boucle
  infinie.
- **FR-079**: Le watchdog MUST NOT redémarrer une station saine qui sert pour
  accélérer le balayage ; la priorité service > repair > sweep est locale.
- **FR-080**: Chaque incident MUST être journalisé avec `scope`
  (`station`/`fleet`), station, cause, preuve, action, résultat, début et fin ;
  un incident de station ne MUST pas être enregistré comme `fleet`.
- **FR-081**: `station_state` MUST être persisté dans `registry.db` avec
  container, transport effectif, état, pays, IP courante, hostname courant,
  `client_usable`, `last_probe_at`, `last_200_at`, raison et `updated_at`.
- **FR-082**: Le watchdog MUST se surveiller lui-même (heartbeat) ; un
  heartbeat stale MUST redémarrer le watchdog seul, pas les stations.
- **FR-083**: La découverte MUST être un scheduler : API (TTL 6 h) → cache →
  embedded → manifeste, avec `stale` explicite et jamais un catalogue vide.
- **FR-084**: La découverte MUST ingérer les serveurs en ligne, leurs
  transports, leur charge et leur pays, dédupliquer par hostname, et marquer
  les serveurs retirés `skipped` (`removed-from-catalog`).
- **FR-085**: La découverte MUST NOT ouvrir de session ni binder une station ;
  elle peut utiliser le preflight (DNS/connect/handshake) sans tunnel.
- **FR-086**: Chaque run de découverte MUST être persisté (`discovery_runs` ou
  `catalog_runs`) avec source, début/fin, compteurs, `stale`, erreurs et
  `server_count`.
- **FR-087**: `ops/lifecycle.py`, `ops/reconciler.py`, `ops/supervisor.py`,
  `free/station_health.py` et `vpn/manager.py` MUST être réutilisés par
  strangler (extraction de logique, pas fork) ; aucun fork du manager ni
  second lifecycle ne MUST être créé.
- **FR-088**: La file de réparation MUST être unifiée dans `registry.db` ;
  `logs/repair_queue.db` ne MUST pas rester une autorité parallèle.
- **FR-089**: La machine à états de santé (`usable`, `suspect`, `repairing`,
  `probing`, `parked`, `disabled`) MUST être persistée et chaque transition
  MUST être journalisée avec sa cause.
- **FR-090**: L'API/UI MUST exposer l'état par station, le watchdog, la
  découverte, les incidents et les réparations, avec les preuves brutes.
- **FR-091**: Une panne de découverte, de watchdog ou de réparation MUST être
  fail-soft : les stations saines continuent de servir et la file reste
  lisible.
- **FR-092**: Le domaine MUST être pur : aucune I/O, aucun import Docker,
  réseau, provider ou SQLite ; la règle de dépendance
  domaine → application → adapters MUST être testée.
- **FR-093**: Chaque frontière MUST passer par un port explicite
  (`CatalogSource`, `QualificationStore`, `TransportPlanner`, `StationBinder`,
  `Prober`, `QuotaLedger`, `Watchdog`, `RepairQueue`, `Clock`) et un adapter
  remplaçable.
- **FR-094**: Aucun module MUST dépasser 1 500 lignes ; la complexité
  cyclomatique MUST être bornée et aucun cycle de dépendance ne MUST exister.
- **FR-095**: Chaque port MUST avoir un contract test ; chaque adapter MUST
  avoir un test avec fake, sans Docker ni réseau.
- **FR-096**: La couverture MUST être ≥ 85 % sur le domaine et l'application,
  et 100 % sur les chemins critiques (classification, quota, cutover,
  watchdog, reprise).
- **FR-097**: Le type checking et le lint MUST être à 0 erreur sur les nouveaux
  modules ; un gate MUST bloquer une régression.
- **FR-098**: Les budgets de performance MUST être mesurés par benchmark/soak
  (preflight, sweep, cutover, rotation, API, UI), pas seulement documentés.
- **FR-099**: Une suite d'injection de fautes MUST couvrir tunnel down, DNS
  cassé, `no_egress`, 429, AUTH_FAILED, crash, disque plein et source
  catalogue indisponible.
- **FR-100**: Chaque décision MUST produire une preuve structurée
  (`run_id`, station, hostname, transport, cause, evidence, result) dans les
  logs et/ou `qualification_attempts`.
- **FR-101**: Le control plane MUST exposer un SLO, un error budget et une
  alerte de brûlure ; la disponibilité MUST être calculée depuis des sondes.
- **FR-102**: Toute option MUST avoir un contrat `surface → clé → consommateur
  → effet → test` ; `config_audit` MUST rester à 0 dead / 0 missing.
- **FR-103**: Les ADR et `debt.md` MUST être à jour : 0 P0 ouverte aux gates,
  P1 planifiée, aucune dette non listée.
- **FR-104**: Un seul scorer de potentiel, un seul scorer mesuré, un seul
  classifieur, une seule machine à états de station et un seul ledger de quota
  MUST exister ; aucune duplication de logique.
- **FR-105**: Les décisions MUST être déterministes et reproductibles : mêmes
  entrées + même horloge injectée → même sortie.
- **FR-106**: Les données MUST avoir une rétention, un backup `VACUUM INTO`
  vérifié, un `integrity_check` et une migration testée sur copie.
- **FR-107**: Aucun secret, cookie ou clé ne MUST apparaître dans un log, une
  métrique ou une réponse API ; aucune jambe payante ni second compte.
- **FR-108**: Un runbook opérateur MUST documenter démarrer, pause, reprendre,
  diagnostiquer, rollback et restaurer.
- **FR-109**: Chaque migration MUST être réversible par backup et testée sur
  une copie ; aucune nouvelle table ne MUST être obligatoire au rollback.
- **FR-110**: Un gate de release MUST échouer si un attribut de qualité
  (fiabilité, performance, qualité, modularité, maintenabilité) régresse.
- **FR-111**: Les files et les caches MUST être bornés et appliquer la
  backpressure ; aucun retry infini, aucune croissance non bornée.
- **FR-112**: Le MTTR MUST être mesuré par cause ; un incident connu MUST être
  réparable en < 5 min avec le runbook.
- **FR-113**: La feature MUST appliquer un **ratchet** : aucun nouveau module
  > 1 500 lignes, aucun module legacy touché ne MUST grossir au-delà de sa
  baseline, aucune nouvelle dette non listée ; les monolithes D-02/D-20 restent
  suivis, pas réécrits.
- **FR-114**: La couverture ≥ 85 % MUST s'appliquer au nouveau
  domaine/application ; la baseline legacy (38 %) MUST être suivie dans le
  registre de dette sans bloquer la feature.
- **FR-115**: Un inventaire des seams, un graphe de dépendances, les contrats et
  une ADR cible MUST précéder toute extraction de monolithe.
- **FR-116**: Chaque seam MUST avoir des tests de caractérisation qui capturent
  le comportement actuel avant extraction.
- **FR-117**: Chaque extraction MUST être un strangler : shadow, canary,
  rollback, aucun changement de comportement observable.
- **FR-118**: `vpn/manager.py` MUST être découpé en cycle de vie station,
  AUTH/session, transport/bind, santé/probe et rotation ; le fichier restant
  MUST être un adapteur fin.
- **FR-119**: `free/pool.py` MUST être découpé en sélection, quota, rotation,
  pool public et accounting.
- **FR-120**: `scripts/fresh_chain.py` MUST être remplacé par
  `free/qualification/sweep.py` et une CLI fine ; `--repair` MUST rester un
  wrapper compatible.
- **FR-121**: `dashboard/api.py` MUST être découpé en routers et read models ;
  aucune route ne MUST contenir de SQL.
- **FR-122**: `opencode.py` MUST devenir un composition root/entrypoint ; le
  pipeline de requête, la jambe free et le routage modèle MUST être extraits.
- **FR-123**: Chaque module extrait MUST être < 1 500 lignes ; le fichier legacy
  MUST décroître de façon monotone à chaque extraction.
- **FR-124**: La parité de performance MUST être prouvée : p95 ≤ +5 % par
  rapport à la baseline pour chaque extraction.
- **FR-125**: Après extraction, la règle de dépendance MUST tenir : 0 cycle,
  0 I/O dans le domaine, 0 import provider/SQLite dans le domaine.
- **FR-126**: Chaque seam MUST avoir characterization tests, contract tests et
  tests d'intégration avec fakes.
- **FR-127**: Chaque extraction MUST avoir un flag de rollback et un
  décommissionnement après canary réussi.
- **FR-128**: Chaque extraction MUST mettre à jour ADR, runbook et
  `implementation-log.md`.
- **FR-129**: Les dettes D-02 (monolithes) et D-20 (dashboard lourd) MUST être
  closes à la fin du démantèlement.
- **FR-130**: Le démantèlement MUST NOT changer le comportement fonctionnel :
  mêmes entrées + même horloge injectée → mêmes sorties, mêmes statuts, mêmes
  preuves.

### Key Entities

- **CatalogServer** : hostname, country, city, load, status, transports,
  source_id, first_seen, last_seen, stale.
- **QualificationCandidate** : hostname, provider, potential_score,
  measured_score, status, attempts, quota_remaining, rtt_ms, load,
  last_result, next_eligible_at, epoch.
- **QualificationAttempt** : attempt_id, run_id, hostname, station, transport,
  status, egress_ip, latency_ms, rtt_ms, reset_at, error, validation,
  started_at, finished_at.
- **QualificationEpoch** : epoch_id, started_at, finished_at, status, total,
  tested, usable, quota, infra, dead, skipped, cursor.
- **EgressOption** : clé canonique, provider, kind, host/port ou station,
  egress IP/bloc, statut, source_ids, first_seen, last_seen, last_checked,
  expires_at, reset_at, success/fail counters.
- **QuotaBucket** : ip, day, bucket, used, successes, failures, last_status,
  reset_at, provider.
- **PotentialScore** : reliability, latency, quota, load, freshness, weights.
- **MeasuredScore** : succès, latence, débit, stabilité, fraîcheur
  (constitution IX).
- **SweepCursor** : epoch_id, hostname, status, next_eligible_at, updated_at.
- **StationLease** : station, hostname, egress_ip, transport, lease_until,
  state.
- **StationRuntime** : station, state (`serving`, `qualifying`, `rotating`,
  `repairing`, `paused`), current_option, current_egress_ip,
  current_transport, standby_option, standby_egress_ip, standby_transport,
  lease_until, last_cutover_at, cutover_ms, availability_1h,
  availability_24h, last_reason.
- **StationCutover** : cutover_id, station, from_option, from_ip, to_option,
  to_ip, started_at, committed_at, finished_at, duration_ms, result
  (`committed`, `rolled-back`, `failed`), reason.
- **TransportPlan** : hostname, `transport_order`, `transport_cursor`,
  `round`, `attempts_by_transport`, `next_eligible_at`, `unsupported`.
- **TransportHealth** : transport, station, attempts, 200, 429, infra,
  latency_p50_ms, success_rate, `degraded`, `last_error`.
- **StationHealthState** : station, state (`usable`, `suspect`, `repairing`,
  `probing`, `parked`, `disabled`), good/bad probes, repair attempts, last
  probe, cause, parked_until, next_retry_at.
- **StationIncident** : id, scope (`station`/`fleet`), station, started_at,
  ended_at, cause, evidence, remediation, result.
- **RepairJob** : id, station, cause, started_at, finished_at,
  candidates_tried, transports_tried, attempts, result, last_error.
- **DiscoveryRun** : id, source, started_at, finished_at, status, server_count,
  transport_count, stale, errors, catalog_fetched_at.
- **CatalogSnapshot** : source_id, fetched_at, etag, stale, server_count,
  error, last_ok_at.
- **ModuleBoundary** : nom, fichier source, modules cibles, seams, owner,
  tests de caractérisation, flag de rollback, statut.
- **ExtractionSlice** : id, source (`manager`, `pool`, `fresh_chain`,
  `dashboard`, `opencode`), cible, état (`planned`, `shadow`, `canary`,
  `active`, `rolled-back`), delta p95, dette fermée.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100 % des 7 070 serveurs `online` ont une ligne de file après
  l'ingestion, avec `source_id` et `transports`.
- **SC-002**: 100 % des hostnames éligibles ont un état terminal par époque
  (`200`, `429`, `dead`, `skipped`) ou un `next_eligible_at` documenté.
- **SC-003**: 0 hostname parqué par `country_backoff` ; le pays n'est jamais
  une porte dure.
- **SC-004**: 0 `429` enregistré comme `dead` ; 100 % des `quota` portent le
  `reset_at` fournisseur.
- **SC-005**: Sur 429 d'une IP en service, la station claim le candidat
  suivant en p95 < 60 s.
- **SC-006**: Un 200 ne termine jamais l'époque ; le balayage continue
  jusqu'au budget ou à la fin de l'époque.
- **SC-007**: 100 % des tentatives sont classées ; les `interrupted`
  représentent < 1 % des tentatives sur une époque complète.
- **SC-008**: Le temps médian pour trouver un premier 200 est < 5 min ; le
  p95 < 15 min.
- **SC-009**: Le décile supérieur du score de potentiel produit au moins 80 %
  des 200 observés.
- **SC-010**: 0 IP avec `reset_at` futur n'est sélectionnée ; 100 % des
  sélections utilisent le ledger de quota.
- **SC-011**: Le dashboard de couverture répond en p95 < 2 s pour 7 070
  lignes et affiche testés / total, 200 / 429 / infra / skip.
- **SC-012**: Un crash puis redémarrage reprend l'époque au curseur, sans
  doublon, avec < 1 min de travail perdu.
- **SC-013**: 0 IP d'egress partagée par deux stations.
- **SC-014**: 100 % des clés `egress_catalog.qualification.*` ont une ligne
  `option-map` avec un test d'effet.
- **SC-015**: 0 fenêtre visible ou vol de focus pendant un boot, un balayage,
  une rotation et la suite de tests sous Windows.
- **SC-016**: `platform_audit` retourne 0 violation sur le dépôt.
- **SC-017**: 100 % des observations ont `provider`, `hostname` et un
  horodatage.
- **SC-018**: Un balayage complet des 7 070 hostnames tient dans 24 h avec six
  stations, ou expose la projection et la raison du retard.
- **SC-019**: 0 usage de la jambe payante, d'un second compte ou de l'attente
  comme substitut.
- **SC-020**: Ajouter une source de catalogue ne nécessite aucun changement
  du scorer, du store ou du balayage.
- **SC-021**: 0 hostname absent de l'API dans la file active ; 100 % des
  tentatives portent sur un hostname du catalogue courant ou du manifeste
  runtime ; les 2 008 hostnames legacy observés dans l'audit sont
  `skipped`/`stale`, jamais testés.
- **SC-022**: Chaque station atteint ≥ 99 % `client_usable` sur 24 h quand au
  moins une IP 200 existe ; le 6/6 atteint ≥ 99 % sur la même fenêtre.
- **SC-023**: 0 pause globale ; un 429 sur une station ne change ni l'IP, ni le
  lease, ni l'état des cinq autres (test de non-régression).
- **SC-024**: 100 % des rotations avec standby ont un cutover p95 < 2 s et
  max < 5 s ; 0 interruption de trafic quand un second tunnel/espace de noms
  est disponible.
- **SC-025**: Six workers qualifient en parallèle ; 0 hostname claimé deux
  fois ; le débit croît avec le nombre de stations.
- **SC-026**: La panne ou la réparation d'une station ne réduit pas la
  disponibilité des cinq autres de plus de 1 % sur 24 h.
- **SC-027**: 100 % des rotations journalisent la durée et la raison du
  cutover ; 0 rebind non planifié sans standby.
- **SC-028**: 0 station idle alors qu'un standby `usable` existe.
- **SC-029**: Le dashboard par station affiche état, IP courante, standby,
  cutover, disponibilité 1 h/24 h et 6/6.
- **SC-030**: Deux rotations simultanées sur deux stations différentes
  réussissent ; aucun test n'observe de sérialisation globale.
- **SC-031**: 0 rotation planifiée de la dernière station utilisable sans
  standby validé ; le système ne descend pas à 0/6 par une rotation.
- **SC-032**: 100 % des `AUTH_FAILED`/`bind_error` sont isolés à leur station ;
  les autres stations conservent leur IP et leur disponibilité.
- **SC-033**: 100 % des hostnames qui supportent ≥ 2 transports ont au moins
  deux tentatives de transport distinctes par époque.
- **SC-034**: 0 hostname `dead` après un seul échec de transport.
- **SC-035**: Le cycle `wireguard` → `openvpn-udp` → `openvpn-tcp` →
  `wireguard` (round 2) est observé dans cet ordre sur un hostname qui supporte
  les trois, quand les trois échouent.
- **SC-036**: Un `429` sur WireGuard n'empêche pas les tentatives OpenVPN UDP
  et TCP ; chaque transport a son statut de quota.
- **SC-037**: Une reprise après crash conserve le curseur de transport et le
  round ; aucun transport déjà essayé n'est rejoué dans le même round.
- **SC-038**: Le dashboard expose, par transport, 200/429/infra/latence et le
  curseur par hostname.
- **SC-039**: En mode `auto`, ≥ 99 % des hostnames multi-transports ont des
  tentatives sur ≥ 2 transports ; 0 hostname n'est limité à un seul transport
  par le code.
- **SC-040**: 0 transport est déclaré sain après 0 succès sur
  `transport_health_min_attempts` ; le transport dégradé est visible.
- **SC-041**: En mode `manual`, 100 % des tentatives utilisent uniquement le
  transport choisi.
- **SC-042**: Un OpenVPN à 0 succès expose son diagnostic (bind/AUTH/probe/DNS)
  et ne condamne aucun hostname.
- **SC-043**: 100 % des stations ont une ligne `station_state` avec
  `client_usable`, `last_probe_at`, IP/hostname courants et transport effectif.
- **SC-044**: Une chute de tunnel simulée sur une station est détectée en
  < 30 s ; seule cette station est réparée ; les cinq autres gardent leur IP et
  leur disponibilité.
- **SC-045**: 0 réparation `fleet` déclenchée par une panne d'une seule
  station.
- **SC-046**: 0 station saine et serving redémarrée par le watchdog ou le
  balayage.
- **SC-047**: 100 % des réparations sont dans `registry.db` ; 0 double
  réparation pour la même station/cause.
- **SC-048**: La découverte tourne au moins toutes les 6 h ; une API
  indisponible utilise cache/embedded et ne vide jamais la file.
- **SC-049**: 100 % des serveurs en ligne et de leurs transports sont ingérés ;
  un serveur retiré est `skipped`, jamais actif.
- **SC-050**: 100 % des incidents de station ont cause, preuve, action et
  résultat ; 0 panne de station enregistrée comme `fleet`.
- **SC-051**: Un heartbeat watchdog stale redémarre le watchdog seul ; les six
  stations continuent de servir.
- **SC-052**: Le dashboard station/watchdog/découverte/réparation répond en
  p95 < 2 s et affiche les preuves brutes.
- **SC-053**: 0 autorité de réparation parallèle : `repair_jobs` et
  `repair_requests` dans `registry.db` sont la seule file.
- **SC-054**: `ops/lifecycle.py`, `ops/reconciler.py`, `ops/supervisor.py` et
  `vpn/manager.py` sont réutilisés ; 0 fork du manager.
- **SC-055**: La disponibilité du control plane est ≥ 99,9 % sur 30 jours
  (process, heartbeat watchdog, âge de la dernière sonde).
- **SC-056**: Le 6/6 `client_usable` est ≥ 99 % sur 24 h quand au moins une IP
  200 existe.
- **SC-057**: 0 panne d'une station ne modifie l'état, l'IP ou le lease d'une
  autre station.
- **SC-058**: Le MTTR d'un incident connu est < 5 min (p95) avec le runbook.
- **SC-059**: 0 tentative non classée ; 0 faux `dead` ; 0 `429` en `dead`.
- **SC-060**: 100 % des décisions ont une preuve brute (run, station,
  hostname, transport, cause, résultat).
- **SC-061**: Les budgets de performance sont mesurés : preflight 500 < 5 min,
  balayage 7 070 < 24 h.
- **SC-062**: API p95 < 500 ms, UI p95 < 2 s, cutover p95 < 2 s, rotation 429
  < 60 s p95.
- **SC-063**: Mémoire et disque bornés : rétention appliquée, files bornées,
  aucune croissance non bornée sur 24 h de soak.
- **SC-064**: 0 I/O et 0 import provider dans le domaine ; règle de dépendance
  vérifiée par un test.
- **SC-065**: 0 module > 1 500 lignes ; 0 cycle de dépendance ; complexité
  sous le seuil.
- **SC-066**: Couverture ≥ 85 % domaine/application ; 100 % chemins critiques.
- **SC-067**: Type checking/lint 0 erreur ; `config_audit` 0 dead / 0 missing.
- **SC-068**: 100 % des ports et adapters ont un contract test.
- **SC-069**: La suite d'injection de fautes passe (tunnel, DNS, 429, AUTH,
  crash, disque plein, source indisponible).
- **SC-070**: 0 secret dans les logs/métriques/API ; 0 jambe payante ; 0 second
  compte.
- **SC-071**: ADR, `debt.md`, runbook et documentation à jour ; 0 dette non
  listée.
- **SC-072**: Le gate de release échoue sur toute régression d'un des cinq
  attributs.
- **SC-073**: 0 nouveau module > 1 500 lignes ; 0 module legacy touché ne
  grossit au-delà de sa baseline ; 0 nouvelle dette non listée.
- **SC-074**: La couverture du nouveau domaine/application est ≥ 85 % ; la
  baseline legacy est suivie sans être présentée comme acquise.
- **SC-075**: Le graphe de dépendances, les seams, les contrats et l'ADR cible
  sont publiés avant la première extraction.
- **SC-076**: `vpn/manager.py` est un adapteur fin (< 1 500 lignes) ; cycle de
  vie, AUTH/session, transport/bind, santé/probe et rotation sont des modules
  testés.
- **SC-077**: `free/pool.py` est < 1 500 lignes ; sélection, quota, rotation,
  pool public et accounting sont séparés.
- **SC-078**: `scripts/fresh_chain.py` est une CLI fine (< 500 lignes) ;
  `free/qualification/sweep.py` porte le balayage.
- **SC-079**: `dashboard/api.py` est découpé en routers/read models ; 0 SQL
  dans les routes.
- **SC-080**: `opencode.py` est un composition root/entrypoint (< 1 500 lignes
  ou entrée fine) ; pipeline, free leg et routage modèle sont extraits.
- **SC-081**: Chaque extraction a une parité de comportement prouvée : mêmes
  entrées → mêmes sorties, 0 régression fonctionnelle.
- **SC-082**: Chaque extraction a une parité de performance : p95 ≤ +5 % vs
  baseline.
- **SC-083**: Après extraction, 0 cycle de dépendance et domaine pur.
- **SC-084**: Couverture ≥ 85 % sur les modules extraits.
- **SC-085**: D-02 et D-20 sont closes ; aucun module > 1 500 lignes dans le
  control plane.

## Definition of Done

Une story n'est terminée que si :

1. ses FR et SC sont satisfaits avec une preuve mesurée ;
2. les tests unitaires, contract, intégration, fautes et performance passent ;
3. le type checking, le lint, la couverture et `config_audit` passent ;
4. le domaine reste pur et aucun cycle/fork n'est introduit ;
5. les métriques, logs et preuves de décision sont exposés ;
6. le runbook et l'ADR sont à jour ;
7. le rollback est testé ;
8. aucune dette P0 n'est ouverte ;
9. si la story touche un monolithe, l'extraction est un strangler avec parité,
   shadow/canary, rollback, et le fichier legacy décroît.

## Assumptions

- Les 7 070 serveurs de l'API sont le catalogue courant faisant autorité ;
  l'embedded ne fait que compléter les hostnames absents.
- Un serveur peut rendre une IP d'egress différente selon le transport et le
  rebind ; l'IP observée est l'unité de quota et de rotation.
- Les six stations sont la limite de sessions ; la file est plus grande.
- Le gate free 200/429 est la seule vérité de qualification ; un bind réussi
  sans 200 n'est pas un succès (constitution VII).
- L'hôte de l'opérateur est le point de mesure du ping ; aucune géolocalisation
  externe n'est nécessaire.
- `registry.db` reste l'autorité pour la file, les tentatives, les époques, les
  IP et les quotas (constitution III).
- Le quota free est par IP ; une série de 429 ne prouve rien sur les autres IP
  du pays ou du pool.
- Zéro coupure exige un second tunnel/espace de noms ou un standby déjà
  qualifié ; sans cela, la coupure est bornée, mesurée et exposée, jamais
  affirmée à zéro.
- Les six sessions restent la limite dure ; le standby est une option
  pré-qualifiée, pas une septième session permanente.
- En mode `auto`, un hostname peut rendre une IP différente par transport ; le
  quota et l'unicité sont évalués par IP observée, pas par transport.
- OpenVPN UDP/TCP a 0 200 sur 16 045 tentatives dans l'audit ; ce n'est pas une
  preuve que toutes les IP OpenVPN sont épuisées, c'est un chemin à
  diagnostiquer avant de le déclarer sain.
- Le mode `manual` reste un override explicite de l'opérateur ; il ne déclenche
  pas le cycle multi-transports.
- `ops/lifecycle.py`, `ops/reconciler.py`, `ops/supervisor.py`,
  `free/station_health.py` et `vpn/manager.py` sont réutilisés par strangler ;
  la feature se branche dessus, elle ne les réécrit pas.
- Le watchdog, la découverte et la réparation font partie de la feature ;
  `registry.db` est leur autorité unique, y compris `station_state`,
  `incidents`, `repair_jobs` et les runs de découverte.
- Une panne du watchdog ou de la découverte est fail-soft : les stations
  saines continuent de servir.
- Le démantèlement des monolithes est **progressif** : chaque extraction est
  un strangler avec parité, shadow, canary et rollback ; aucun big-bang, aucun
  changement de comportement fonctionnel.
- Les monolithes legacy restent lisibles pendant la transition ; un fichier
  legacy ne grossit jamais et décroît à chaque extraction.
