# Feature Specification: Migration douce vers le fork Gluetun 016

**Feature Branch**: `017-fork-soft-migration`

**Created**: 2026-11-14

**Status**: Active

**Input**: « Il faut migrer vers ce nouveau fork afin que tout soit ultra
fonctionnel, ultra qualitatif, ultra fiable, ultra rapide, ultra performant.
Le superviseur doit tout réparer ; on ne gèle pas le superviseur. Ce qui
fonctionne ne doit pas être remplacé pour rien : une station saine est laissée
telle quelle, même pour une mise à jour. À la rotation, quand elle ne fonctionne
plus, on la passe à la nouvelle version. UDP, WireGuard et TCP doivent tous
rester utilisables. »

## Périmètre

Cette feature couvre la **migration opérationnelle** de la flotte de six
stations vers l'image fork `gluetun-016:local` et le plan de contrôle 016 :

1. **Migration douce** : une station qui sert un client free n'est jamais
   recréée pour une raison de version. La migration se fait uniquement quand
   une station est déjà inutilisable (`station_usable=false`,
   `model_usable=false`, ou aucune IP) ou lors d'une rotation déclenchée par un
   quota 429.
2. **Superviseur propriétaire** : `scripts/fresh_chain_supervisor.py` reste en
   marche en permanence et est le seul acteur qui répare. Aucune intervention
   manuelle `docker compose` en régime établi.
3. **Chemin chaud v2** : le superviseur utilise `GluetunControlV2Binder` et
   l'API de contrôle du fork (`/v1/vpn/switch`, `/v1/vpn/rotate`,
   `/v1/vpn/state`, `/v1/egress`) via des ports loopback publiés.
4. **Repli froid Compose** : si l'API est indisponible, si la cible n'est pas
   résoluble depuis un tunnel mort, ou si la boucle VPN est crashée, le
   superviseur recrée le conteneur avec l'image courante et le format « auto ».
5. **Ladder complet** : WireGuard, OpenVPN UDP et OpenVPN TCP restent tous dans
   l'ordre de rotation. Aucun transport n'est supprimé pour contourner une
   panne temporaire.
6. **Qualité mesurée** : chaque réparation est prouvée par le consensus
   d'egress ; les latences, taux de succès et recreates sont observés.

**Hors périmètre** : jambe payante, second compte, attente comme solution,
septième station ou septième session, réécriture big-bang des monolithes.

## Contrainte dure — six stations, pas une de plus

`station_count=6`, `target_slots=6`. Le warm pool ne crée que des IP d'egress,
jamais une station supplémentaire. Une rotation ne doit jamais laisser la flotte
sous 5 stations utilisables plus de 180 s.

## Scénarios utilisateurs

### User Story 1 — Une station saine n'est jamais touchée (P1)

Une station dont le tunnel est vérifié et dont le modèle free répond 200 reste
strictement inchangée, même si une nouvelle image du fork est disponible.

**Pourquoi** : toute recréation inutile coupe le service et consomme un bind
NordVPN ; c'est le contraire de la fiabilité demandée.

**Test indépendant** : marquer une image plus récente comme disponible, laisser
le superviseur tourner 30 min, vérifier `StartedAt` et l'image de la station
saine inchangés.

**Critères d'acceptation** :

1. **Given** une station `station_usable=true` et `model_usable=true`,
   **When** le superviseur cycle, **Then** aucun job de réparation ni recreate
   n'est créé pour elle.
2. **Given** une nouvelle image taguée, **When** aucune panne n'est détectée,
   **Then** la station conserve son image et son uptime.

---

### User Story 2 — Le superviseur répare seul une station cassée (P1)

Une station perd son tunnel. Le superviseur la détecte, tente un switch v2,
puis un recreate Compose si nécessaire, et la remet en service avec une preuve
d'egress, sans intervention humaine.

**Pourquoi** : c'est le cœur de l'autonomie demandée ; un superviseur qu'il faut
réparer à la main n'est pas un superviseur.

**Test indépendant** : arrêter le tunnel d'une station de test (pas une station
qui sert), attendre un cycle, vérifier `healthy`, `egress.verified=true` et
`client_usable=true`.

**Critères d'acceptation** :

1. **Given** une station sans IP ou `station_usable=false`, **When** le
   superviseur la voit, **Then** il lance une chaîne dans les 30 s.
2. **Given** un switch v2 qui renvoie `resolution_failed` ou une boucle VPN
   `crashed`/`awaiting tunnel`, **When** le binder hybride le reçoit,
   **Then** il recrée le conteneur en Compose avec `VPN_TYPE=` vide.
3. **Given** un recreate réussi, **When** le ladder runtime se termine,
   **Then** l'egress est vérifié par consensus avant de déclarer la station
   réparée.

---

### User Story 3 — La rotation garde les trois transports (P1)

Quand une IP est épuisée (429), la station tourne vers une nouvelle IP en
essayant WireGuard, puis OpenVPN UDP, puis OpenVPN TCP. Le premier transport qui
prouve un egress gagne ; un cycle complet revient au premier avec backoff.

**Pourquoi** : la qualité dépend de la capacité à passer par le transport
disponible, pas d'un choix figé.

**Test indépendant** : forcer un 429 sur une station, vérifier que le job expose
`ladder=[wireguard, openvpn-udp, openvpn-tcp]` et que le transport actif est
celui qui a fourni l'egress.

**Critères d'acceptation** :

1. **Given** `VPN_TYPE` non défini et un hostname pinné, **When** le ladder
   démarre, **Then** les trois transports sont tentés dans l'ordre.
2. **Given** WireGuard indisponible et OpenVPN UDP disponible, **When** le
   ladder bascule, **Then** la station commit sur `openvpn-udp` sans recreate.
3. **Given** une cible morte sur les trois transports, **When** le cycle est
   épuisé, **Then** la station est marquée réparable et le candidat suivant est
   choisi ; la station saine précédente n'est pas détruite.

---

### User Story 4 — Le chemin chaud est rapide et idempotent (P2)

Le superviseur bascule un hostname par l'API en quelques secondes, sans
`docker exec`, avec une clé d'idempotence propre à chaque cible.

**Pourquoi** : la vitesse et la reprise après timeout dépendent de l'API, pas
d'un recreate de deux minutes.

**Test indépendant** : rejouer la même requête de switch avec la même clé ;
vérifier un seul job ; changer de cible et vérifier une clé différente.

**Critères d'acceptation** :

1. **Given** une station sur le fork avec le port de contrôle publié,
   **When** le superviseur bascule un hostname, **Then** aucun `docker exec`
   n'est utilisé.
2. **Given** deux cibles successives dans la même chaîne, **When** les switches
   partent, **Then** les clés d'idempotence diffèrent et aucun
   `idempotency_conflict` n'apparaît.
3. **Given** un timeout réseau, **When** la même requête est rejouée,
   **Then** la même clé réutilise le même job.

---

### User Story 5 — La migration est observable et bornée (P2)

Chaque réparation produit des événements, des métriques et des logs exploitables ;
les retries sont bornés et ne doublent pas sans plafond.

**Pourquoi** : « ultra fiable » exige de prouver le comportement, pas de
l'espérer.

**Test indépendant** : provoquer trois échecs consécutifs, vérifier le breaker,
le cooldown et l'événement `transport.failed` ; vérifier qu'aucune boucle ne
dépasse les budgets.

**Critères d'acceptation** :

1. **Given** un échec de transport, **When** le breaker atteint son seuil,
   **Then** le transport est sauté avec `breaker_open` et un `retry_at` borné.
2. **Given** une réparation terminée, **When** l'opérateur consulte l'état,
   **Then** il voit l'image, le transport, l'IP vérifiée et l'âge de la preuve.

## Exigences fonctionnelles

- **FR-001** : le superviseur est le seul propriétaire des réparations ; aucun
  script manuel n'est requis en régime établi.
- **FR-002** : une station n'est recréée que si elle est inutilisable ou en
  rotation ; une station saine est exclue de toute migration de version.
- **FR-003** : un recreate de migration utilise l'image fork courante et écrit
  `VPN_TYPE=` vide pour activer le ladder.
- **FR-004** : le chemin chaud utilise l'API de contrôle via
  `http://127.0.0.1:800{station}` ; les ports sont publiés en loopback.
- **FR-005** : un switch v2 qui renvoie `resolution_failed`, `vpn loop is
  crashed` ou `awaiting tunnel` déclenche un repli Compose.
- **FR-006** : chaque cible de switch a une clé d'idempotence distincte ; une
  rotation utilise un hash des candidats.
- **FR-007** : le ladder conserve `wireguard`, `openvpn-udp`, `openvpn-tcp`
  dans cet ordre ; aucun transport n'est retiré par configuration.
- **FR-008** : le préflight du ladder résout l'IP depuis le catalogue du
  provider, jamais par le DNS du tunnel avant que celui-ci soit up.
- **FR-009** : aucun commit sans consensus d'egress vérifié et sans exclusion
  de l'IP de l'hôte.
- **FR-010** : aucune station, session ou compte supplémentaire ; le quota
  reste par IP.
- **FR-011** : les retries sont bornés (tentatives, cooldown, backoff maximum,
  âge de chaîne) et repartent d'un état sain après succès.
- **FR-012** : chaque réparation émet un événement et laisse une trace
  exploitable (image, transport, IP, raison, durée).

## Exigences non fonctionnelles

- **NFR-001 (fiabilité)** : 6/6 stations `client_usable` en cible ; jamais moins
  de 5/6 pendant plus de 180 s à cause d'une réparation.
- **NFR-002 (latence réparation)** : détection ≤ 30 s ; réparation jusqu'à
  egress vérifié p95 ≤ 180 s.
- **NFR-003 (latence switch)** : switch v2 p95 ≤ 45 s si le premier transport
  fonctionne ; ≤ 180 s avec repli de transport.
- **NFR-004 (zéro churn)** : 0 recreate d'une station saine pour migration sur
  une fenêtre de 24 h.
- **NFR-005 (performance)** : aucun `docker exec`/`docker cp` sur le chemin
  chaud quand l'API répond ; un seul job actif par station.
- **NFR-006 (qualité)** : tests unitaires et de contrat verts pour le fork, le
  binder v2, le binder hybride et le compose auto.
- **NFR-007 (sobriété)** : un seul tunnel actif par station, pas de septième
  session, pas de double tunnel inter-stations.

## Entités clés

- **Station** : conteneur `opencode-vpn-N`, image, transport actif, hostname,
  IP d'egress vérifiée, état `client_usable`.
- **Job de réparation** : `switch`, `rotate`, `rollback` ou recreate Compose ;
  état terminal, tentatives, clé d'idempotence, preuve d'egress.
- **Ladder** : liste ordonnée `wireguard,openvpn-udp,openvpn-tcp`, breakers,
  backoff, transport commité.
- **Image fork** : `gluetun-016:local`, digest, date de build, capacité
  `switch/rotate/transport_ladder`.
- **Preuve d'egress** : consensus k-of-n, IP, sources, dissenters, âge.

## Critères de succès

- **SC-001** : une station saine reste inchangée (même `StartedAt`, même image)
  pendant 24 h de supervision.
- **SC-002** : une station cassée est réparée par le superviseur seul, avec
  `egress.verified=true`, en p95 ≤ 180 s.
- **SC-003** : les trois transports sont visibles dans `/v1/vpn/transport` et
  aucun n'est retiré de la politique.
- **SC-004** : aucun `idempotency_conflict` ni `docker exec` sur le chemin
  chaud pendant une rotation de 20 candidats.
- **SC-005** : 6/6 stations `client_usable` en free, avec des IP d'egress
  distinctes, sans payant, sans second compte, sans attente.
