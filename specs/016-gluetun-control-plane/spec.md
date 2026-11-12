# Feature Specification: Plan de contrôle Gluetun — sélection, rotation, DNS redondant et vérification d'egress

**Feature Branch**: `016-gluetun-control-plane`

**Created**: 2026-11-11

**Status**: Draft

**Input**: « Adapter le fork `jul-ienfr/gluetun` au projet : API de gestion
complète (rotation, changement d'IP, sélection pays/ville/IP), DNS redondant
multi-fallback, contrôle d'IP redondant multi-fallback. Analyser le projet, le
fork et les projets similaires, puis produire un plan global avec SpecKit. »

## Périmètre

Cette feature couvre le **plan de contrôle par station** qui manque aujourd'hui
à Gluetun, et son intégration à la flotte de six stations :

1. **Sélection** : pays, région, ville, ISP, catégorie, hostname, numéro de
   serveur et IP d'endpoint explicite, avec validation contre le catalogue du
   provider.
2. **Bascule** : application transactionnelle d'une cible, préflight, drain
   optionnel, attente du tunnel, vérification d'egress, commit ou rollback.
3. **Rotation** : choix du prochain serveur selon une liste ordonnée par
   l'orchestrateur ou une politique locale, exclusions (hostname, IP, sous-réseau),
   budget de candidats, arrêt à la première IP prouvée.
4. **DNS redondant** : plusieurs résolveurs par étage, plusieurs étages
   (DoT/DoH/plain), santé active et passive, bascule par requête, `fail-closed`,
   état exposé.
5. **Vérification d'egress** : sources multiples indépendantes, consensus
   k-of-n, exclusion de l'IP d'hôte, contrôle géographique/ASN optionnel,
   canary HTTP 200 avec contenu.
6. **État, événements et sécurité** : état désiré/committé persisté, reprise au
   redémarrage, jobs idempotents, événements, métriques, scopes d'API, secrets
   masqués.
7. **Intégration** : l'orchestrateur Python existant cesse d'utiliser
   `docker exec` + `PUT /v1/vpn/settings` comme chemin chaud et consomme l'API
   de jobs ; `registry.db` reste l'autorité des IP, leases, quotas et du SLO.
8. **Ladder de transports automatique** : la station essaie elle-même
   WireGuard, puis OpenVPN UDP, puis OpenVPN TCP, prend le premier transport qui
   fournit un tunnel et une preuve d'egress, et après un cycle complet revient
   au premier avec backoff pour réessayer. Un seul tunnel est actif à la fois.
   Le fork ne choisit pas un autre serveur/IP dans ce mécanisme.

**Hors périmètre** :

- La jambe payante, un second compte, l'attente comme solution, l'ajout d'une
  septième station ou d'une septième session.
- Le remplacement de `registry.db`, du superviseur, du warm pool ou du score
  composite de la constitution IX. Le fork exécute et prouve ; il ne possède
  ni le quota, ni le claim d'IP, ni le verdict `client_usable` final.
- La réécriture big-bang de `vpn/manager.py` (591 Ko) ou de `opencode.py`.
- Le make-before-break **inter-stations** : il reste assuré par le standby de
  la feature 015. Le fork fournit une bascule intra-station rapide et
  réversible, pas deux tunnels simultanés dans le même conteneur (voir D2).

## Contraintes dures

- **Six stations, pas une de plus** : `station_count=6`, `target_slots=6`. Le
  fork ne crée jamais de station ni de session supplémentaire.
- **Free uniquement** : aucune option, aucun fallback, aucun message ne propose
  la jambe payante, un second compte ou l'attente. Les replis sont WARP,
  proxys publics et autres egress free déjà qualifiés par le projet.
- **Quota par IP** : le quota free est attaché à l'IP, jamais au compte. Le fork
  ne consomme pas le quota modèle pour ses sondes ; il utilise des échos d'IP
  légers et le canary configuré.
- **Aucune conclusion globale** : un échec de rotation renvoie un résultat
  structuré `exhausted` avec la raison de chaque candidat. Il ne produit jamais
  un état terminal « aucune IP disponible » et ne désactive jamais la station.
- **Chaque IP est testée** : aucune IP n'est déclarée bonne sans vérification
  consensus et, si activé, canary 200 avec contenu. Une IP observée dans
  l'historique n'est pas sélectionnable avant re-validation.
- **Une seule vérité par cycle de vie** : le tunnel et ses réglages appartiennent
  au fork ; l'IP, le lease, le quota et le SLO appartiennent à `registry.db`.
- **Rétro-compatibilité** : les routes et variables Gluetun existantes
  continuent de fonctionner ; les nouveautés sont additives et désactivables.
- **Un seul tunnel, un seul transport** : le ladder de transports arrête le
  transport courant avant d'en appliquer un autre ; il ne change jamais de
  serveur, d'IP ou de hostname.

## Contexte

### État du fork

`jul-ienfr/gluetun` est, au 2026-11-11, **strictement identique** à
`passteque/gluetun:master` : comparaison GitHub `identical`, 0 commit d'avance,
0 commit de retard. Il n'y a donc aucune adaptation locale à préserver : les
extensions doivent être créées dans le fork.

L'upstream est déjà riche :

- Sélection statique : `SERVER_COUNTRIES`, `SERVER_REGIONS`, `SERVER_CITIES`,
  `SERVER_ISPS`, `SERVER_NAMES`, `SERVER_NUMBERS`, `SERVER_HOSTNAMES`,
  `SERVER_CATEGORIES`, `SERVER_TIERS`, mode `random`/`ordered`.
- IP d'endpoint : `OPENVPN_ENDPOINT_IP`, `WIREGUARD_ENDPOINT_IP`.
- Rotation implicite : la boucle VPN relit les settings à chaque itération et
  re-sélectionne un serveur après un redémarrage interne.
- DNS : un seul type d'upstream par instance (`dot`, `doh` ou `plain`), une
  liste de providers, un picker aléatoire, des métriques de pool.
- IP publique : boucle `publicip` avec fetchers résilients, consensus
  géographique par pays/région/ville, ban 30 jours sur `429`.
- Serveur de contrôle : `/v1/vpn/status`, `/v1/vpn/settings`, `/v1/dns/status`,
  `/v1/publicip/ip`, `/v1/updater/*`, `/v1/portforward`, avec rôles et clés API
  par route.

### Ce qui manque

1. **Pas d'API de rotation** : la rotation n'existe que comme effet de bord d'un
   redémarrage interne ; aucun job, aucun historique, aucune politique.
2. **Bascule non transactionnelle** : `PUT /v1/vpn/settings` valide puis
   `ApplyStatus(Stopped)` → `ApplyStatus(Running)` (`internal/vpn/state/vpn.go`),
   sans préflight, sans attente prouvée, sans rollback.
3. **Réglages éphémères** : un redémarrage de conteneur réapplique
   l'environnement Compose et perd tout réglage appliqué par l'API.
4. **Pas de sélection par IP d'egress** : `*_ENDPOINT_IP` fixe l'IP du serveur
   VPN, pas l'IP de sortie. Rien ne permet d'exiger une IP de sortie précise ni
   de vérifier qu'elle a changé.
5. **DNS mono-étage** : le type est unique ; si DoT est bloqué, l'instance
   échoue en boucle avec backoff au lieu de basculer vers DoH ou plain. Aucun
   état de santé par étage, aucune sonde active.
6. **Vérification d'IP non probante** : `PUBLICIP_API` fait un consensus de
   champs géographiques, pas un consensus sur la valeur d'IP ; l'IP d'hôte peut
   être acceptée pendant une fenêtre de tunnel down ; aucun canary de contenu.
7. **Secrets exposés** : `GET /v1/vpn/settings` renvoie les credentials ; le
   projet interdit déjà de l'appeler.
8. **Aucun contrat d'orchestration** : le projet parse des corps texte
   (`running`), retente des `PUT`, blackliste des hostnames côté Python et
   recrée le conteneur en dernier recours.

### Ce que fait déjà le projet

L'orchestrateur Python possède déjà :

- `vpn/manager.py` : rotation, pin pays, watchdog, cascade de transports,
  `_control_exec` vers `/v1/vpn/*`, `docker restart`/`--force-recreate`.
- `free/rotation.py`, `free/repair.py`, `free/station_health.py`,
  `free/ip_registry/*` : santé, claims, leases, quota, score.
- `free/ip_registry/adapters/gluetun_control.py` : bascule de hostname via
  `PUT /v1/vpn/settings` avec `docker cp` + `docker exec wget`.
- `free/ip_registry/adapters/egress.py` : détection d'IP en cinq couches
  (SOCKS5h → SOCKS5 → HTTP → control API → `docker exec`).
- `dashboard/routers/station_control_routes.py` : `/api/vpn/station/{sid}/...`,
  `/api/vpn/next`, `/api/vpn/fleet`.
- Six stations actives (`station_count=6`) et quatre définitions Compose
  dormantes (`vpn-gluetun-7..10`) à ne pas réactiver.

La feature 016 ne remplace pas ces modules : elle déplace le **chemin chaud de
bascule** vers une API stable, puis branche l'orchestrateur par strangler.

### Mesures locales pertinentes

- Le réseau de l'opérateur bloque DoH (`dns.google`, `cloudflare-dns`,
  `dns.quad9` expirent) ; DoT et plain ont été validés live. Le ladder par
  défaut de la flotte doit donc être `dot,plain` (DoH disponible mais non
  prioritaire), et non `dot,doh,plain`.
- Les échecs de pin actuels viennent souvent d'un hostname refusé par le
  provider, d'un `AUTH_FAILED` ou d'un TLS qui échoue : le préflight et la
  liste ordonnée de candidats doivent absorber ces cas sans recréer le
  conteneur.
- Le projet utilise déjà un canary WireGuard et un `vpn-wg-test` local : ils
  fournissent un banc d'essai d'intégration sans credentials réels.

## User Scenarios & Testing

### User Story 1 — Bascule déterministe, vérifiée et réversible (Priority: P1)

En tant qu'opérateur, je demande à une station de rejoindre un pays, une ville,
un hostname ou une IP d'endpoint précis. La station valide la cible, bascule,
prouve la nouvelle IP de sortie, puis répond `committed`. Si la preuve échoue,
elle revient à l'état précédent et répond `rolled_back`, sans laisser la station
dans un état inconnu.

**Why this priority** : c'est le primitif dont dépendent la rotation, la
réparation et le dashboard. Sans lui, chaque bascule reste un stop/start aveugle
avec parsing de texte et recréation de conteneur.

**Independent Test** : sur une station de test, appeler
`POST /v1/vpn/switch` avec `{"target":{"hostname":"..."}}`, vérifier le job,
l'IP avant/après, puis provoquer un échec de preuve et vérifier le rollback.

**Acceptance Scenarios** :

1. **Given** une station connectée sur le serveur A, **When** une bascule vers
   le serveur B est demandée avec `require_verified=true`, **Then** le job passe
   par `validating → preflight → applying → awaiting_tunnel → verifying_egress →
   committed`, et l'IP de sortie est différente de A avec un quorum ≥ 2.
2. **Given** une cible dont le hostname n'existe pas dans le catalogue,
   **When** la bascule est demandée, **Then** la réponse est `422` avec
   `code=unknown_target`, aucun tunnel n'est coupé, et l'état courant est
   inchangé.
3. **Given** une cible valide mais dont la vérification d'IP échoue après
   `max_attempts`, **When** le job se termine, **Then** les settings précédents
   sont réappliqués, l'ancienne IP est re-vérifiée, et le job est `rolled_back`
   avec les observations brutes.
4. **Given** un `Idempotency-Key` déjà utilisé, **When** la même requête est
   renvoyée, **Then** le même job est retourné sans deuxième bascule.
5. **Given** une bascule en cours, **When** une seconde bascule est demandée
   sans `supersede`, **Then** la réponse est `409` avec `active_job_id`.

---

### User Story 2 — Rotation jusqu'à preuve, sans épuisement global (Priority: P1)

En tant qu'opérateur, je demande une rotation avec une liste de candidats
ordonnés (ou une politique), des exclusions d'IP/sous-réseau et un budget. La
station essaie les candidats dans l'ordre, s'arrête à la première IP prouvée,
et si le budget est épuisé, renvoie `exhausted` avec la raison de chaque
candidat — jamais « aucune IP disponible ».

**Why this priority** : c'est la demande explicite de rotation et de changement
d'IP. Le projet sait déjà choisir et scorer les IP ; le fork doit exécuter et
prouver, pas re-scorer.

**Independent Test** : fournir trois candidats dont les deux premiers échouent
et le troisième réussit ; vérifier que le job s'arrête au troisième, que l'IP
est nouvelle, et qu'aucune station n'est désactivée.

**Acceptance Scenarios** :

1. **Given** une liste `[A, B, C]` et `require_new_ip=true`, **When** A échoue au
   préflight et B échoue à la preuve, **Then** C est tenté, le job est
   `committed` sur C, et le résultat contient `attempts=[A,B,C]` avec leurs
   raisons.
2. **Given** trois candidats qui échouent tous, **When** le budget est épuisé,
   **Then** le job est `exhausted`, la station reste sur son ancien serveur si
   celui-ci est encore sain, et aucun état global « no IP » n'est écrit.
3. **Given** `require_new_subnet=true` et une IP de sortie dans le même /24 que
   la précédente, **When** la vérification passe mais la contrainte de sous-réseau
   échoue, **Then** le candidat est rejeté avec `same_subnet` et le suivant est
   tenté.
4. **Given** `expected_egress_ip` fourni, **When** l'IP observée diffère,
   **Then** le candidat est rejeté avec `egress_mismatch` et le suivant est
   tenté.
5. **Given** une rotation qui produit un `200` avec contenu sur le canary,
   **When** le job commit, **Then** l'événement `egress.verified` contient l'IP,
   le quorum, les sources et la latence.

---

### User Story 3 — DNS redondant multi-étages et multi-fournisseurs (Priority: P1)

En tant qu'opérateur, je veux que la résolution continue lorsque DoT est bloqué,
lent, ou qu'un fournisseur DNS tombe, en basculant vers DoH puis plain (ou
l'ordre configuré), sans fuite hors tunnel et avec un état observable.

**Why this priority** : le réseau de l'opérateur bloque déjà DoH et un healthcheck
DNS qui échoue redémarre le tunnel en boucle. Un DNS mono-étage est un point de
panne unique pour les six stations.

**Independent Test** : bloquer TCP/853 sur une station, lancer des requêtes,
vérifier la bascule vers plain en moins de 2 s, puis bloquer tous les étages et
vérifier `SERVFAIL` + événement `dns.ladder_exhausted` sans requête hors tunnel.

**Acceptance Scenarios** :

1. **Given** `DNS_LADDER=dot,plain`, **When** TCP/853 est bloqué, **Then** la
   première requête échoue sur DoT, est rejouée sur plain, et la suivante part
   directement sur plain jusqu'à la récupération de DoT.
2. **Given** un fournisseur DoT qui renvoie `SERVFAIL`, **When** un autre
   fournisseur du même étage répond, **Then** la réponse valide est retournée
   sans basculer d'étage.
3. **Given** tous les étages down, **When** une requête arrive, **Then** la
   réponse est `SERVFAIL`, aucune requête n'est envoyée aux résolveurs de
   l'hôte, et l'état expose `fail_closed=true`.
4. **Given** une réponse `NXDOMAIN`, **When** elle provient du premier étage,
   **Then** elle est retournée telle quelle et ne déclenche pas de fallback.
5. **Given** une récupération de DoT, **When** la sonde active réussit deux fois,
   **Then** l'étage repasse `up` et l'événement est émis.

---

### User Story 4 — Vérification d'egress par consensus et canary (Priority: P1)

En tant qu'opérateur, je veux une preuve d'IP indépendante des seuls dires de
Gluetun : plusieurs sources, un quorum, l'exclusion de l'IP d'hôte, et un canary
HTTP 200 avec contenu. Une IP non prouvée n'est jamais un succès.

**Why this priority** : le contrôle d'IP actuel peut accepter l'IP de l'hôte
pendant une fenêtre de tunnel down, et un `200` vide ne prouve pas que le flux
est utilisable. La constitution VII l'interdit.

**Independent Test** : lancer une vérification avec trois échos locaux qui
répondent la même IP, puis un qui ment ; vérifier le quorum et les dissidents.
Répéter avec l'IP de l'hôte et vérifier le rejet.

**Acceptance Scenarios** :

1. **Given** trois sources et `quorum=2`, **When** deux sources répondent
   `203.0.113.7` et une répond `198.51.100.9`, **Then** le résultat est
   `verified=true`, `ip=203.0.113.7`, `confidence=high`, avec la source
   dissidente listée.
2. **Given** une seule source disponible, **When** `allow_single_source=false`,
   **Then** le résultat est `verified=false` avec `reason=insufficient_sources`.
3. **Given** l'IP de l'hôte capturée au boot, **When** une source la renvoie,
   **Then** elle est rejetée avec `host_ip` et ne contribue pas au quorum.
4. **Given** un canary configuré qui renvoie `200` mais un corps vide, **When**
   la vérification s'exécute, **Then** le canary échoue avec `empty_body`.
5. **Given** une vérification fraîche de moins de 10 s, **When** l'API est
   appelée sans `fresh=true`, **Then** le résultat en cache est retourné avec
   `cached=true`.

---

### User Story 5 — État durable, événements, sécurité et intégration flotte (Priority: P2)

En tant qu'opérateur, je veux que l'état désiré survive à un redémarrage, que
les événements soient consommables par le dashboard et `registry.db`, que les
secrets ne fuitent pas, et que l'orchestrateur puisse basculer les six stations
sans `docker exec` ni recréation.

**Why this priority** : sans persistance, chaque redémarrage annule la rotation ;
sans événements, le SLO et les incidents ne voient pas les bascules ; sans
scopes, l'API de contrôle devient une surface d'attaque.

**Independent Test** : appliquer un serveur via l'API, redémarrer le conteneur,
vérifier que le serveur est restauré ; appeler `GET /v1/events` et retrouver le
job ; tester une clé read-only sur `POST /v1/vpn/switch` (403) et vérifier que
`GET /v1/vpn/settings` masque les secrets.

**Acceptance Scenarios** :

1. **Given** un état désiré persisté, **When** le conteneur redémarre,
   **Then** le fork réapplique le serveur désiré (ou expose `drift` si
   `CONTROL_STATE_MODE=env`) et émet `state.recovered`.
2. **Given** une clé API de rôle `reader`, **When** elle appelle
   `POST /v1/vpn/switch`, **Then** la réponse est `403` et aucun job n'est créé.
3. **Given** `GET /v1/vpn/settings` sans `reveal=true`, **When** la réponse est
   sérialisée, **Then** les clés privées, mots de passe et tokens sont masqués.
4. **Given** un abonné SSE, **When** un job change d'état, **Then** l'événement
   est reçu en moins de 1 s avec `seq`, `job_id` et `state`.
5. **Given** les six stations équipées, **When** une rotation est lancée,
   **Then** une seule session est renouvelée à la fois et les cinq autres
   restent `client_usable`.

---

### User Story 6 — Ladder de transports automatique (Priority: P1)

En tant qu'opérateur, je veux que chaque station choisisse seule le meilleur
transport disponible — WireGuard, OpenVPN UDP, OpenVPN TCP — prenne le premier
qui fournit un tunnel prouvé, passe automatiquement au suivant en cas d'échec,
puis revienne au premier et réessaie après un cycle complet, sans redémarrage de
conteneur et sans intervention de l'orchestrateur.

**Why this priority** : un réseau peut bloquer UDP, un serveur peut refuser
OpenVPN, un handshake WireGuard peut échouer. Aujourd'hui le ladder vit dans
Python et coûte des allers-retours Docker et des recréations. Le faire dans
Gluetun réduit la bascule de plusieurs dizaines de secondes à quelques secondes
et rend la station autonome sur son tunnel.

**Independent Test** : forcer l'échec de WireGuard puis d'OpenVPN UDP dans un
conteneur de test, vérifier qu'OpenVPN TCP prend le relais, que l'egress est
prouvé, puis que le cycle revient à WireGuard après le backoff.

**Acceptance Scenarios** :

1. **Given** `TRANSPORT_LADDER=wireguard,openvpn-udp,openvpn-tcp`, **When**
   WireGuard échoue au handshake, **Then** OpenVPN UDP est tenté, puis OpenVPN
   TCP si nécessaire, et le premier transport qui prouve l'egress est
   `committed`.
2. **Given** un cycle complet sans succès, **When** le budget est épuisé,
   **Then** le fork émet `transport.cycle_exhausted`, applique un backoff avec
   jitter, puis reprend au premier transport du ladder.
3. **Given** UDP bloqué par le réseau, **When** WireGuard et OpenVPN UDP
   échouent, **Then** OpenVPN TCP fonctionne et la station reste utilisable.
4. **Given** un transport qui échoue `N` fois, **When** son breaker est `open`,
   **Then** il est ignoré jusqu'au cooldown et une sonde `half_open` décide de
   sa réactivation.
5. **Given** tous les transports échoués, **When** le job se termine,
   **Then** aucune autre IP/serveur n'est choisi par le fork ; l'orchestrateur
   reçoit `transport.cycle_exhausted` et décide d'une rotation.

---

### Edge Cases

- **Redémarrage pendant un job** : le journal doit permettre de classer le job
  `interrupted`, puis de commit si l'état effectif est prouvé, sinon rollback.
- **Cible identique à l'état courant** : réponse `noop` sans couper le tunnel.
- **Provider sans serveur dans le pays demandé** : `422` avec les facettes
  disponibles, pas une rotation à l'aveugle.
- **Tous les candidats refusés par le provider** : `exhausted` structuré, station
  conservée, événement, pas de désactivation.
- **IP de sortie identique après rotation** : rejet `same_ip`, candidat suivant.
- **Sous-réseau identique** : rejet `same_subnet` si demandé.
- **DNS : réponses tronquées (TC)** : retry TCP sur le même étage avant fallback.
- **DNS : `NXDOMAIN`** : réponse valide, jamais de fallback.
- **DNS : tous les étages down** : `SERVFAIL`, `fail_closed`, alerte.
- **Échos d'IP qui mentent ou timeout** : quorum, dissidents, backoff par source.
- **IP d'hôte / IP privée / CGNAT / loopback** : rejet dur.
- **Canary 200 sans contenu** : échec, pas de commit.
- **Rollback impossible** (ancien serveur mort) : `failed_degraded`, événement
  critique, la station reste réparable par l'orchestrateur ; pas de boucle.
- **Deux switches concurrents** : `409`, un seul job actif.
- **State file corrompu** : fallback env, `state.invalid`, pas de crash.
- **Disque plein** : écriture atomique échouée, job `failed`, ancien état
  conservé.
- **DoH bloqué sur le réseau** : ladder `dot,plain` par défaut pour la flotte ;
  DoH reste configurable mais non prioritaire.
- **Quota modèle** : les sondes du fork n'appellent jamais l'API modèle ; le
  canary est un endpoint neutre configurable.
- **Transport bloqué (UDP)** : WireGuard et OpenVPN UDP échouent, TCP prend le
  relais ; l'échec est mémorisé sans condamner la station.
- **Transport qui réussit puis meurt** : le watchdog retente le même transport
  avant de passer au suivant ; après le budget, événement et escalade.
- **Ladder épuisé** : `transport.cycle_exhausted`, backoff avec jitter, reprise
  au premier transport ; aucune rotation de serveur décidée par le fork.
- **Auth échouée sur un transport** : le transport est marqué, les autres sont
  tentés ; aucun bouclage infini sur le même.
- **Deux transports « up »** : impossible par construction ; l'ancien est
  arrêté avant l'application du suivant.
- **MTU différent** : le MTU/PMTUD est recalculé à chaque changement de
  transport ; aucun paquet ne sort avant que le firewall soit réappliqué.

## Requirements

### Functional Requirements

#### Sélection et catalogue

- **FR-001** : Le fork MUST exposer un catalogue de serveurs filtrable par
  provider, type de VPN, pays, région, ville, ISP, hostname, IP, catégorie et
  tier, avec pagination et facettes.
- **FR-002** : Le fork MUST valider toute cible contre le catalogue du provider
  avant de couper le tunnel, et retourner `422` avec les choix valides en cas
  d'inconnue.
- **FR-003** : Le fork MUST accepter une IP d'endpoint explicite et vérifier
  qu'elle appartient bien au hostname sélectionné (ou à défaut, au provider).
- **FR-004** : Le fork MUST distinguer `endpoint_ip` (IP du serveur VPN) de
  `expected_egress_ip` (IP de sortie observée) dans le contrat et la
  documentation.
- **FR-005** : Le fork MUST maintenir un historique consultatif
  `hostname → IPs de sortie observées`, avec TTL et provenance, sans jamais le
  traiter comme un claim.
- **FR-006** : Le fork MUST exposer les statistiques runtime par serveur
  (tentatives, succès, échecs, dernière erreur, latence EWMA, dernière
  utilisation).

#### Bascule et transaction

- **FR-007** : Le fork MUST fournir `POST /v1/vpn/switch` avec une cible, des
  contraintes et un mode synchrone ou asynchrone.
- **FR-008** : Chaque bascule MUST créer un job avec un identifiant stable,
  un état, des timings par phase, les candidats essayés et les preuves brutes.
- **FR-009** : Le fork MUST effectuer un préflight avant toute coupure :
  validation des settings, résolution du hostname, appartenance de l'IP au
  serveur, joignabilité TCP de l'endpoint (OpenVPN) ou vérification UDP
  disponible (WireGuard), présence des credentials.
- **FR-010** : Le fork MUST prendre un snapshot de l'état courant avant
  application et le persister dans un journal atomique.
- **FR-011** : En cas d'échec de tunnel ou de preuve d'egress, le fork MUST
  rollback vers le snapshot précédent et re-vérifier l'ancienne IP.
- **FR-012** : Si le rollback échoue, le fork MUST passer le job en
  `failed_degraded`, émettre un événement critique et s'arrêter ; il MUST NOT
  boucler indéfiniment.
- **FR-013** : Le fork MUST supporter `Idempotency-Key` avec une fenêtre de
  24 h et retourner le job existant pour une clé déjà vue.
- **FR-014** : Le fork MUST sérialiser les bascules par instance : un seul job
  actif, `409` avec `active_job_id` en cas de conflit, `supersede` réservé au
  rôle admin.
- **FR-015** : Le fork MUST supporter un drain optionnel des connexions proxy
  avant l'arrêt du tunnel, avec un timeout borné et un event `drain.timeout`.
- **FR-016** : Le fork MUST exposer `GET /v1/vpn/jobs`, `GET /v1/vpn/jobs/{id}`
  et `POST /v1/vpn/jobs/{id}/cancel`.
- **FR-017** : Le fork MUST exposer `POST /v1/vpn/rollback` pour revenir
  explicitement au dernier état committé.
- **FR-018** : Le fork MUST supporter `dry_run=true` : validation et préflight
  complets, aucune coupure, réponse avec le plan et les candidats.

#### Rotation

- **FR-019** : Le fork MUST fournir `POST /v1/vpn/rotate` avec une politique
  (`countries`, `cities`, `hostnames`, `categories`, `tiers`) ou une liste
  ordonnée de candidats.
- **FR-020** : La rotation MUST accepter `exclude_hostnames`, `exclude_ips`,
  `exclude_subnets`, `require_new_ip`, `require_new_subnet`,
  `expected_egress_ip`, `max_candidates`, `max_attempts` et `timeout_s`.
- **FR-021** : La rotation MUST s'arrêter à la première candidat dont l'egress
  est vérifié et dont toutes les contraintes passent.
- **FR-022** : Une rotation épuisée MUST retourner `exhausted` avec la liste des
  candidats et la raison de chacun ; elle MUST NOT écrire un état global
  « aucune IP disponible » ni désactiver la station.
- **FR-023** : Le fork MUST appliquer un cooldown par hostname après échec
  (configurable, avec backoff) et ne pas re-tenter immédiatement le même
  candidat.
- **FR-024** : Le fork MUST accepter une liste ordonnée fournie par
  l'orchestrateur et ne pas re-scorer cette liste ; le score composite reste
  dans `registry.db` (constitution IX).
- **FR-025** : Le fork MUST refuser toute rotation qui dépasserait la limite de
  sessions ou de stations configurée par l'orchestrateur (champ `budget` ou
  `lease`), sans créer de session supplémentaire.

#### DNS

- **FR-026** : Le fork MUST supporter un ladder ordonné et configurable
  `dot,doh,plain` (au moins deux étages actifs).
- **FR-027** : Chaque étage MUST supporter plusieurs fournisseurs ou adresses,
  avec sélection et métriques par fournisseur.
- **FR-028** : Le fork MUST basculer d'étage par requête sur timeout,
  `SERVFAIL` ou `REFUSED`, et uniquement sur ces conditions ; `NXDOMAIN` est une
  réponse valide.
- **FR-029** : Le fork MUST maintenir une santé active et passive par étage,
  avec seuil d'échec, cooldown, récupération et événements.
- **FR-030** : Le fork MUST être `fail-closed` : si tous les étages sont down,
  il retourne `SERVFAIL` et n'utilise jamais les résolveurs de l'hôte.
- **FR-031** : Le fork MUST exposer `GET /v1/dns/state`, `GET /v1/dns/tiers` et
  `POST /v1/dns/probe`.
- **FR-032** : Le fork MUST conserver le cache, le filtre et le DNS local sur le
  serveur frontal, et non dupliquer ces middlewares par étage.
- **FR-033** : Le fork MUST documenter et tester que DoH peut être bloqué sans
  rendre la station inutilisable (ladder `dot,plain` pour la flotte).

#### Vérification d'egress

- **FR-034** : Le fork MUST fournir `GET /v1/egress`, `GET /v1/egress/verify`,
  `GET /v1/egress/canary` et `GET /v1/egress/history`.
- **FR-035** : La vérification MUST interroger au moins trois sources
  indépendantes par défaut : échos HTTP, DNS whoami et données `publicip` du
  fork.
- **FR-036** : La vérification MUST calculer un consensus k-of-n (défaut k=2,
  n≥3) sur la valeur d'IP, et exposer les dissidents et la confiance.
- **FR-037** : La vérification MUST rejeter les IP privées, loopback, link-local,
  CGNAT, multicast, non spécifiées, l'IP d'hôte/underlay et les IP de la liste
  `forbidden_ips`.
- **FR-038** : Le fork MUST capturer l'IP d'underlay au boot et la conserver pour
  comparaison, sans dépendre d'un écho externe avant le tunnel.
- **FR-039** : Le fork MUST supporter un canary HTTP avec statut 200, corps non
  vide et absence d'erreur ; un 200 vide est un échec.
- **FR-040** : Le fork MUST appliquer un cache TTL (défaut 10 s), un
  single-flight par station et un backoff par source.
- **FR-041** : Le fork MUST exposer la provenance, la latence, l'horodatage et
  l'état de santé de chaque source.
- **FR-042** : Une vérification non prouvée MUST être traitée comme un échec
  pour `require_verified=true` ; elle ne doit jamais être convertie en succès.

#### État, événements, sécurité

- **FR-043** : Le fork MUST persister l'état désiré et le dernier état committé
  dans un fichier atomique versionné, avec checksum et mode 0600.
- **FR-044** : Au démarrage, le fork MUST réconcilier l'état persisté avec
  l'environnement selon `CONTROL_STATE_MODE=state|env|merge` (défaut `state` si
  le fichier est valide), et émettre `state.recovered` ou `state.drift`.
- **FR-045** : Le fork MUST journaliser les jobs et événements dans un fichier
  append-only borné (rotation, rétention) et exposer `GET /v1/events` +
  `GET /v1/events/stream` (SSE).
- **FR-046** : Le fork MUST masquer les secrets dans `GET /v1/vpn/settings` par
  défaut ; `reveal=true` MUST exiger le rôle admin.
- **FR-047** : Le fork MUST ajouter des scopes de routes (`reader`,
  `controller`, `admin`) et refuser par `403` toute opération non autorisée.
- **FR-048** : Le fork MUST limiter la taille des corps, le taux de bascules et
  les timeouts, et ne jamais journaliser les clés API ou credentials.
- **FR-049** : Le fork MUST exposer `GET /v1/control/capabilities` avec la
  version du plan de contrôle, les features actives et les limites.
- **FR-050** : Le fork MUST rester rétro-compatible : les routes et variables
  existantes gardent leur comportement ; les nouvelles routes sont additives.

#### Ladder de transports

- **FR-051** : Le fork MUST exécuter un ladder ordonné et configurable de
  transports (`wireguard`, `openvpn-udp`, `openvpn-tcp`) ; l'ordre par défaut est
  `wireguard,openvpn-udp,openvpn-tcp`.
- **FR-052** : Le fork MUST essayer le premier transport, et en cas d'échec
  (préflight, handshake, timeout, TLS, interface, absence d'egress) passer
  automatiquement au suivant, jusqu'à épuisement du cycle.
- **FR-053** : Après un cycle complet sans succès, le fork MUST revenir au
  premier transport après un backoff exponentiel avec jitter, et réessayer ;
  il MUST NOT boucler sans délai.
- **FR-054** : Le fork MUST n'avoir qu'un seul tunnel actif à la fois : le
  transport précédent est arrêté avant l'application du suivant.
- **FR-055** : Le fork MUST maintenir un breaker par transport (`closed`,
  `open`, `half_open`) avec seuil d'échec, cooldown et sonde de récupération.
- **FR-056** : Le fork MUST typer les erreurs de transport (`auth`,
  `udp_blocked`, `tcp_refused`, `tls`, `handshake_timeout`, `no_egress`,
  `interface_down`) et exposer la raison de chaque tentative.
- **FR-057** : Le fork MUST exposer `GET /v1/vpn/transport` (état courant,
  ladder, tentatives, breaker, prochain retry) et
  `POST /v1/vpn/transport/reset` (admin) pour réarmer un breaker.
- **FR-058** : Le fork MUST accepter un `transport_policy` par job
  (`auto`, `wireguard`, `openvpn-udp`, `openvpn-tcp`, ou liste ordonnée) ;
  `auto` utilise le ladder configuré.
- **FR-059** : Un `VPN_TYPE` explicite MUST forcer un seul transport et
  désactiver le ladder, pour préserver le comportement upstream.
- **FR-060** : Le ladder MUST NOT changer de serveur, d'IP ou de hostname ; il
  ne fait que choisir le transport pour la cible courante. Si aucun transport
  ne fonctionne, il émet `transport.cycle_exhausted` et laisse l'orchestrateur
  décider.
- **FR-061** : Le fork MUST réappliquer le firewall/kill switch et recalculer
  le MTU/PMTUD à chaque changement de transport, avant de déclarer le tunnel
  `up`.
- **FR-062** : Le fork MUST exposer des métriques par transport (tentatives,
  succès, échecs, latence de connexion, état du breaker) et les événements
  `transport.attempt`, `transport.connected`, `transport.failed`,
  `transport.cycle_exhausted`.

### Non-Functional Requirements

- **NFR-001 (performance)** : `GET /v1/vpn/state` p95 < 100 ms ; une bascule
  même transport p95 ≤ 8 s ; un changement de transport p95 ≤ 20 s ; une
  vérification d'egress p95 ≤ 3 s ; un failover DNS p95 ≤ 2 s.
- **NFR-002 (fiabilité)** : aucun job ne peut laisser la station sans état
  connu ; toute écriture critique est atomique ; un crash laisse un journal
  rejouable ; les opérations sont idempotentes.
- **NFR-003 (sécurité)** : secrets masqués, state file 0600, aucune clé dans les
  logs, routes authentifiées, surface limitée au réseau Docker interne.
- **NFR-004 (modularité)** : nouveaux modules Go < 1 500 lignes ; séparation
  domaine (sélection, consensus, état de job) / adapters (provider, HTTP, DNS) ;
  pas de cycle d'import.
- **NFR-005 (observabilité)** : métriques Prometheus par job, phase, étage DNS,
  source d'egress et résultat ; logs structurés avec `job_id`.
- **NFR-006 (compatibilité)** : Go 1.26, image multi-arch amd64/arm64, aucune
  régression des routes existantes, tests de contrat pour l'orchestrateur.
- **NFR-007 (souveraineté)** : le fork ne possède ni le quota, ni le claim, ni
  le score ; il expose des faits et exécute des ordres bornés.
- **NFR-008 (free-only)** : aucune dépendance, option ou documentation ne
  référence la jambe payante, un second compte ou l'attente comme solution.
- **NFR-009 (autonomie locale)** : le ladder de transports et la reconnexion
  même-serveur fonctionnent sans orchestrateur ; une panne de l'orchestrateur ne
  doit pas empêcher la station de choisir un transport et de prouver son egress.

### Key Entities

- **Station** : instance Gluetun (1..6), un tunnel, un pair de proxies, un état
  désiré/committé. Appartient à l'orchestrateur ; le fork en est l'agent.
- **TunnelTarget** : cible de bascule (provider, type, transport, filtres,
  hostname, endpoint_ip, expected_egress_ip).
- **ServerCandidate** : serveur du catalogue (hostname, nom, numéro, pays,
  région, ville, ISP, catégories, tier, IPs, protocoles, clé WG) + statistiques
  runtime.
- **SwitchJob** : transaction de bascule (id, état, cible, candidats, snapshot
  précédent, timings, preuves, erreur).
- **TunnelSnapshot** : settings complets, serveur effectif, endpoint, interface,
  statut, IP de sortie vérifiée, état DNS, horodatage.
- **EgressObservation** : (source, ip, pays, ville, ASN, organisation, latence,
  horodatage, acceptée/rejetée, raison).
- **EgressVerification** : consensus (ip, quorum, confiance, dissidents),
  canary, TTL, horodatage.
- **ResolverTier** : étage DNS (type, fournisseurs/adresses, listener, état,
  succès/échec, latence, cooldown, dernière erreur).
- **ResolverEndpoint** : fournisseur/adresse individuelle, santé et métriques.
- **TransportPolicy** : ladder ordonné (`wireguard,openvpn-udp,openvpn-tcp`),
  budgets par transport, seuils de breaker, backoff et jitter.
- **TransportAttempt** : (transport, serveur, début/fin, résultat, raison typée,
  latence, prochain retry).
- **TransportState** : transport actif, état du ladder, breaker par transport,
  cycle en cours, prochaine tentative.
- **ControlEvent** : événement append-only (seq, ts, type, sévérité, station,
  job_id, données).
- **DesiredState** : état persisté (schéma, settings désirés, dernier commit,
  observations, index des jobs, curseur d'événements).

### API Surface (résumé)

| Méthode | Route | Rôle | Usage |
|---|---|---|---|
| GET | `/v1/control/capabilities` | reader | version, features, limites |
| GET | `/v1/vpn/state` | reader | snapshot complet |
| GET | `/v1/vpn/settings` | reader | settings masqués |
| PUT | `/v1/vpn/settings` | controller | compatibilité, `dry_run`/`wait` optionnels |
| POST | `/v1/vpn/switch` | controller | bascule transactionnelle |
| POST | `/v1/vpn/rotate` | controller | rotation jusqu'à preuve |
| POST | `/v1/vpn/rollback` | controller | retour au dernier commit |
| GET | `/v1/vpn/transport` | reader | état du ladder, tentatives, breakers |
| POST | `/v1/vpn/transport/reset` | admin | réarmer un breaker de transport |
| GET | `/v1/vpn/jobs` | reader | liste des jobs |
| GET | `/v1/vpn/jobs/{id}` | reader | détail + preuves |
| POST | `/v1/vpn/jobs/{id}/cancel` | admin | annulation |
| GET | `/v1/servers` | reader | catalogue filtrable |
| GET | `/v1/servers/{hostname}` | reader | détail + stats |
| GET | `/v1/servers/facets` | reader | pays/villes/ISP/catégories |
| POST | `/v1/servers/refresh` | admin | rafraîchissement provider |
| GET | `/v1/egress` | reader | dernier consensus |
| GET | `/v1/egress/verify` | reader | vérification fraîche |
| GET | `/v1/egress/canary` | reader | canary HTTP |
| GET | `/v1/egress/history` | reader | historique borné |
| GET | `/v1/dns/state` | reader | état du ladder |
| GET | `/v1/dns/tiers` | reader | détail par étage |
| POST | `/v1/dns/probe` | controller | sonde active |
| GET | `/v1/events` | reader | événements depuis `seq` |
| GET | `/v1/events/stream` | reader | SSE |

Le contrat détaillé, les schémas et les codes d'erreur sont dans
`contracts/control-api.md`.

## Success Criteria

### Measurable Outcomes

- **SC-001** : 100 % des jobs de bascule se terminent dans un état explicite
  (`committed`, `rolled_back`, `failed_degraded`, `exhausted`, `cancelled`,
  `interrupted`) ; aucun job ne reste `applying` après un crash.
- **SC-002** : Bascules même transport p95 ≤ 8 s et p99 ≤ 20 s ; changements de
  transport p95 ≤ 20 s et p99 ≤ 45 s, mesurés sur 100 bascules canary.
- **SC-003** : Rotation avec liste de candidats : nouvelle IP vérifiée en ≤ 3
  candidats et ≤ 30 s p95 ; zéro rotation acceptée sans quorum.
- **SC-004** : DNS : la résolution continue en ≤ 2 s p95 après blocage d'un
  étage ; zéro requête hors tunnel quand tous les étages sont down ; zéro
  `NXDOMAIN` transformé en fallback.
- **SC-005** : Vérification d'egress : zéro faux `verified` dans les tests de
  chaos (source menteuse, IP d'hôte, corps vide) ; ≥ 99 % des vérifications
  normales obtiennent un quorum ≥ 2.
- **SC-006** : Après redémarrage du conteneur, le serveur désiré est restauré
  dans 100 % des cas (ou `drift` explicite), sans intervention manuelle.
- **SC-007** : Pendant un rolling rollout 1→6, les six stations restent
  `client_usable` (mesure 015) et aucune septième station/session n'apparaît.
- **SC-008** : `GET /v1/vpn/state` p95 < 100 ms ; événements SSE reçus < 1 s
  après le changement d'état.
- **SC-009** : L'orchestrateur utilise l'API de contrôle pour ≥ 95 % des
  bascules ; le chemin `docker exec` + parsing `running` n'est plus le chemin
  chaud.
- **SC-010** : Aucune régression des routes existantes : les tests de contrat
  `/v1/vpn/status`, `/v1/vpn/settings`, `/v1/publicip/ip` passent avant/après.
- **SC-011** : Ladder de transports : sur un réseau bloquant UDP, la station
  atteint OpenVPN TCP en ≤ 2 tentatives et ≤ 45 s p95 ; après un cycle complet
  sans succès, le retour au premier transport intervient dans 100 % des cas
  après le backoff ; aucun instant avec deux tunnels actifs ; zéro recréation
  de conteneur pour un changement de transport.

## Assumptions

- Le fork peut être buildé et publié comme image privée (GHCR ou registry
  local) et épinglé par digest dans `docker-compose.yml`.
- Les credentials NordVPN free et la clé de contrôle sont disponibles dans
  `credentials.env`, comme aujourd'hui.
- Le réseau de l'opérateur continue de bloquer DoH ; le ladder par défaut de la
  flotte est `dot,plain`, avec DoH configurable.
- Le catalogue de serveurs du provider est déjà téléchargé et stocké par
  l'upstream ; le fork n'invente pas de serveurs.
- L'orchestrateur peut être modifié pour appeler la nouvelle API derrière un
  feature flag, avec fallback sur l'ancien chemin.
- Les six stations actives sont `vpn-gluetun`..`vpn-gluetun-6` ; les services
  `vpn-gluetun-7..10` restent dormants et ne sont pas comptés.
- Le canary du fork utilise un endpoint neutre et ne consomme pas le quota
  modèle ; le verdict `client_usable` reste calculé par la feature 015.

## Dependencies

- Fork `jul-ienfr/gluetun`, upstream `passteque/gluetun` (master, Go 1.26,
  `github.com/qdm12/dns/v2`).
- Projet `opencode-proxy-one` : `vpn/manager.py`, `free/ip_registry/*`,
  `free/station_health.py`, `free/rotation.py`, `dashboard/routers/*`,
  `config.yaml`, `docker-compose.yml`, `credentials.env`.
- `registry.db` et `telemetry.db` (autorité et télémétrie).
- Feature 015 (`015-egress-uptime-slo`) pour le SLO, le warm pool et le verdict
  `client_usable`.
- CI Windows/Linux pour l'orchestrateur ; CI Linux pour le conteneur Go.

## Out of Scope

- Payant, second compte, attente, résidentiel, achat d'IP.
- Ajout ou suppression de stations ; réactivation de `vpn-gluetun-7..10`.
- Réécriture du dashboard ou du monolithe `opencode.py`.
- Make-before-break à deux tunnels dans un même conteneur (voir D2).
- Gestion du quota modèle ou du quota fournisseur au-delà des gates fournis
  par l'orchestrateur.
- Modification des contrats des features 001–015, sauf ajout additif documenté.
