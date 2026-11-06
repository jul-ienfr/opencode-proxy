# Feature Specification: Rotation catalogue mondial et réparation automatique des stations

**Feature Branch**: `002-vpn-catalog-rotation`  
**Created**: 2026-11-04  
**Status**: Draft — Phase 0/1 en cours (voir `implementation-log.md`)  
**Complète** : `specs/001-free-station-availability/spec.md` (les exigences de
disponibilité, de quota et d'observabilité de 001 restent valables ; 002
remplace la partie sélection de serveur et rotation).  
**Input** : « Lorsqu'une station est en 429, elle doit tester tout le catalogue
NordVPN et le catalogue Gluetun, choisir un autre serveur, et ne jamais rester
connectée sur une IP inutilisable. Je veux des serveurs aux quatre coins du
monde, pas seulement en Albanie. »

---

## Contexte et problème

L'audit `audit.md` établit que le système ne manque ni de serveurs ni de pays :
6 948 serveurs WireGuard dans l'API NordVPN, 9 787 dans le manifeste Gluetun,
150 pays. Le dysfonctionnement est dans la **chaîne de décision** :

1. les candidats sont plafonnés à 20 par chaîne et testés à ~60 s chacun ;
2. l'ordre favorise 9 serveurs albanais codés en dur et les anciens 200 ;
3. les serveurs frais et les serveurs historiques sont classés dans le même
   tier, sans score de performance récent ;
4. un 429 sur une station pinnée n'entraîne aucune rotation tant qu'un modèle
   de probe quelconque reste « utilisable » ;
5. le manager ne sait pas choisir un hostname en WireGuard et
   `country_rotation` est désactivé ;
6. le manifeste du conteneur cible n'est pas vérifié avant le bind, ce qui
   provoque des crash loops (`be160` sur la station 6) ;
7. l'ancienne logique de pays est un tri, alors que la seule contrainte
   demandée est l'unicité de l'IP entre stations ;
8. le lease de claim (15 s) est plus court qu'un bind (~60 s) ;
9. il n'existe pas de score composite « performance récente + disponibilité »
   ni de cycle de vie `reset_at → disponible → réutilisable` ;
10. la détection d'IP et le DNS n'ont pas de vraie chaîne de fallback ;
11. le mode VPN `auto` n'essaie pas les trois technologies par serveur ;
12. les options GUI ne sont pas prouvées effectives par un test ;
13. l’état est dispersé entre plusieurs bases, fichiers JSON et monolithes ;
14. la base `ip_registry` n’a pas de migrations versionnées, de rétention ni
    de sauvegarde ;
15. la dette technique n’est pas suivie ni bornée par des gates.

Résultat mesuré : `routable=0` dans 28 % des échantillons SLO, `routable <= 2`
dans 50 %, 6/6 seulement 2,8 % du temps. Le système est souvent « connecté »
mais pas « utilisable ».

## Objectifs

- **O1** — Après un 429 quota sur une IP, **seule cette station** est
  remplacée par une IP vérifiée en **moins de 60 s p95**, sans intervention
  manuelle et sans bloquer les autres stations.
- **O2** — Chaque station peut utiliser **n'importe quel pays**,
  indépendamment des autres. Aucun quota par pays ni par continent. La seule
  contrainte dure est qu'**aucune IP d'egress n'est partagée par deux
  stations**.
- **O3** — Le catalogue complet (API + Gluetun) est exploitable : un balayage
  repris sur plusieurs cycles, avec provenance, fraîcheur, endpoint et
  résultats, sans plafond arbitraire de 20.
- **O4** — Les stations utilisent en priorité les IP les plus performantes
  **disponibles**, puis descendent dans le classement à mesure qu'elles
  s'épuisent ; une IP performante qui redevient disponible après son
  `reset_at` est réutilisée quand une station a besoin d'une rotation.
- **O5** — Aucune station n'est comptée « utilisable » si elle ne peut pas
  servir une requête free réelle (200 avec contenu, pas de 429 actif).
- **O6** — Le système est réparable et observable : chaque décision a une
  cause, chaque échec a une raison, chaque station a un prochain candidat.
- **O7** — Chaque dépendance externe a une **chaîne de fallback** : sites de
  test d'IP, DNS, catalogue, manifeste, bind, probe, transport VPN et
  superviseur. Aucune dépendance unique ne doit rendre une station inutilisable.
- **O8** — En mode VPN `auto`, chaque station peut utiliser **WireGuard,
  OpenVPN UDP et OpenVPN TCP** ; si une technologie échoue, elle essaie la
  suivante, puis boucle sur les trois avant de passer au serveur suivant.
- **O9** — Chaque option du GUI a un **effet réel** et testé. Aucun contrôle
  inerte : une option sans consommateur runtime est supprimée ou marquée
  « affichage seul ».
- **O10** — L’architecture est **modulaire, testable et maintenable** :
  domaine sans I/O, ports/adapters, use cases, migration strangler, aucun
  nouveau monolithe.
- **O11** — Les serveurs, IP, quotas et décisions reposent sur une **base de
  données fiable** : schéma normalisé, migrations, contraintes, provenance,
  rétention, sauvegarde et intégrité vérifiée.

## Non-objectifs

- Modifier l'impersonation TLS/Bun, les en-têtes ou le corps officiel de la
  porte (déjà couvert par 001).
- Ajouter un second compte ou passer à la jambe payante.
- Changer le quota du fournisseur ou le modèle économique.
- Implémenter dans cette session : ce document est un plan à valider.

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 — 429 : remplacement immédiat, jamais de station morte (Priority: P1)

Une requête free arrive sur une station dont l'IP a épuisé son bucket
quotidien. Le système classe l'IP `EXHAUSTED` avec son `reset_at`, retire la
station du routage, promeut une IP vérifiée du pool chaud et, si le pool est
vide, lance une réparation qui teste le catalogue jusqu'à trouver une IP qui
répond 200. La station ne reste jamais « connectée mais inutilisable ».

**Why this priority** : c'est le symptôme visible et la promesse de service.

**Independent Test** : forcer un 429 quota sur une station, vérifier que le
trafic repart sur une autre IP en < 60 s et que la station 429 n'est plus
sélectionnée avant son `reset_at`.

**Acceptance Scenarios** :

1. **Given** 6 stations utilisables, **When** une IP reçoit un 429
   `FreeUsageLimitError`, **Then** elle passe `EXHAUSTED` avec `reset_at`, une
   IP `WARM` est promue, et `client_usable` ne descend pas sous 5.
2. **Given** un pool chaud vide, **When** une IP tombe en 429, **Then** une
   réparation démarre immédiatement et teste des candidats jusqu'à un 200,
   sans attendre un cycle de 20 min ni un flag `model_usable` global.
3. **Given** une station pinnée sur un hostname, **When** un 429 quota arrive,
   **Then** le pin n'empêche pas la réparation : le hostname est re-testé
   seulement s'il produit une nouvelle IP, sinon un autre hostname est choisi.
4. **Given** une IP 429 avec `Retry-After: 3600`, **When** le reset est
   enregistré, **Then** aucun probe ni bind n'a lieu avant `reset_at`.

---

### User Story 2 — Stations indépendantes, tous les pays, une IP par station (Priority: P1)

Chaque station est indépendante : elle peut utiliser n'importe quel pays, à
n'importe quel moment, sans quota par pays ni par continent. Deux stations ne
partagent jamais la même IP d'egress. Une station qui tombe en 429 fait sa
propre rotation, en parallèle des autres, sans les bloquer. Au démarrage, les
stations déjà connectées et utilisables sont conservées ; seules les stations
manquantes sont complétées.

**Why this priority** : c'est le modèle opérationnel exact demandé. La
diversité géographique n'est pas une contrainte ; l'unicité d'IP est la seule
contrainte dure.

**Independent Test** : lancer 100 allocations avec un catalogue de 150 pays et
vérifier qu'aucun pays n'est interdit, que deux stations n'obtiennent jamais
la même IP, et qu'un 429 sur une station ne modifie pas les autres.

**Acceptance Scenarios** :

1. **Given** 6 stations à assigner, **When** le catalogue contient des IP
   disponibles dans plusieurs pays, **Then** chaque station peut recevoir
   n'importe lequel de ces pays, sans plafond par pays.
2. **Given** deux stations ont besoin d'une IP, **When** le sélecteur choisit,
   **Then** les deux IP sont distinctes ; une IP déjà assignée n'est jamais
   réutilisée.
3. **Given** une station reçoit un 429, **When** la réparation démarre,
   **Then** seule cette station est recréée ; les autres gardent leur IP et
   continuent de servir.
4. **Given** deux stations reçoivent un 429 en même temps, **When** elles se
   réparent, **Then** les deux réparations tournent en parallèle, sans pause
   globale de flotte.
5. **Given** le proxy démarre avec 2 stations utilisables sur 6, **When** le
   boot se termine, **Then** les 2 stations sont conservées et les 4
   manquantes sont complétées avec les meilleures IP disponibles.
6. **Given** le code de sélection, **When** on le scanne, **Then** aucun
   hostname, pays ou préfixe d'IP n'apparaît en dur.

---

### User Story 3 — Catalogue complet, vérifié et repris (Priority: P2)

Le système maintient un catalogue unifié API + Gluetun avec provenance et
fraîcheur. Il peut balayer tout le catalogue en arrière-plan, par lots variés,
sans plafond de 20, en reprenant là où il s'est arrêté. Un serveur absent de
l'API mais présent dans Gluetun reste éligible : il est classé selon sa
qualité observée, pas exclu par sa source, et son manifeste est vérifié avant
le bind.

**Why this priority** : c'est la demande « tester tout le catalogue », mais
rendue compatible avec la capacité réelle (6 sessions, ~60 s par bind).

**Independent Test** : lancer un balayage sec sur 9 787 serveurs synthétiques,
vérifier que chaque hostname est visité une fois, que la progression est
persistée, qu'un redémarrage reprend au bon endroit, et qu'un serveur mort
n'est pas re-testé avant son backoff.

**Acceptance Scenarios** :

1. **Given** un catalogue de N serveurs, **When** le balayage tourne, **Then**
   chaque serveur a un statut (`untested`, `preflight_ok`, `bind_ok`,
   `bind_error`, `429`, `dead`) et une date.
2. **Given** un hostname absent du manifeste du conteneur cible, **When** un
   bind est préparé, **Then** le système synchronise et vérifie le manifeste ;
   si le host n'y est toujours pas, il est marqué `manifest_missing` et n'est
   pas proposé.
3. **Given** un serveur Gluetun absent de l'API, **When** il est testé,
   **Then** il est classé selon son taux de 200 récent ; un host Gluetun-only
   performant peut passer avant un host API médiocre, et son échec n'empêche
   pas les autres pays.
4. **Given** un redémarrage du superviseur, **When** le balayage reprend,
   **Then** il ne repart pas de zéro et ne re-teste pas les serveurs en
   backoff.

---

### User Story 4 — Un seul propriétaire de la réparation, observable (Priority: P2)

Un seul composant décide et exécute les réparations. Chaque station expose son
état réel, son IP, son hostname, son pays, son bucket, son prochain candidat
et la raison de son état. Le dashboard distingue `connected` de
`client_usable`.

**Why this priority** : l'audit montre deux propriétaires (watchdog manager et
superviseur) et une observabilité qui compte les tunnels, pas les requêtes.

**Independent Test** : arrêter le superviseur et vérifier que le manager ne
recrée pas la même station en parallèle ; tuer une IP et vérifier que le
dashboard affiche la cause et le prochain candidat.

**Acceptance Scenarios** :

1. **Given** une station en réparation, **When** le watchdog manager la voit,
   **Then** il ne la recrée pas (un seul propriétaire).
2. **Given** une station `connected` avec bucket épuisé, **When** le dashboard
   affiche son état, **Then** elle est `DRAINING`/`EXHAUSTED`, pas
   `client_usable`.
3. **Given** un échec de bind, **When** l'incident est journalisé, **Then** la
   cause (`no-egress`, `manifest_missing`, `429`, `auth`, `timeout`) et le
   hostname sont présents.

---

### User Story 5 — Montée en charge sans code (Priority: P3)

Passer de 6 à N stations ne demande qu'un changement de configuration, avec
les mêmes garanties d'indépendance, d'unicité d'IP et de réparation.

**Why this priority** : la contrainte de sessions est globale ; le nombre de
stations est un paramètre.

**Independent Test** : passer `station_count` de 6 à 8, vérifier que les
conteneurs, ports et overrides sont créés, que les stations au-delà du budget
de sessions sont `standby` et non `broken`.

**Acceptance Scenarios** :

1. **Given** `station_count: N`, **When** N dépasse la limite de sessions,
   **Then** les stations excédentaires sont `standby` et l'état est exposé.
2. **Given** une station ajoutée, **When** le superviseur tourne, **Then**
   elle entre dans la boucle sans modification de code.

---

### User Story 6 — Fallbacks multiples et transport VPN auto (Priority: P1)

Chaque dépendance a plusieurs chemins de secours. Si un site de test d'IP ne
répond pas, un autre est utilisé ; si un resolver DNS tombe, un autre prend le
relais ; si un catalogue ou un manifeste est indisponible, le cache ou la
source suivante est utilisé. En mode `auto`, une station essaie WireGuard,
puis OpenVPN UDP, puis OpenVPN TCP ; si les trois échouent sur un serveur,
elle passe au serveur suivant et boucle.

**Why this priority** : c'est la condition pour que « tester tout le
catalogue » et « ne jamais rester inutilisable » soient vrais même quand une
dépendance externe tombe.

**Independent Test** : couper le site d'IP principal, le DNS principal, l'API
NordVPN et forcer un serveur qui ne répond qu'en OpenVPN TCP ; la station doit
quand même trouver une IP et servir un 200.

**Acceptance Scenarios** :

1. **Given** le site d'IP principal est indisponible, **When** une station se
   connecte, **Then** un site de secours ou l'API de contrôle Gluetun fournit
   l'IP, et la station reste utilisable si le probe officiel répond 200.
2. **Given** le resolver DNS principal est indisponible, **When** une
   résolution est nécessaire, **Then** un resolver de secours (DoH, DoT ou
   plain) est utilisé et l'échec est journalisé.
3. **Given** un serveur ne répond pas en WireGuard, **When** la station
   essaie les transports, **Then** elle essaie OpenVPN UDP puis OpenVPN TCP
   sur le même serveur avant de passer au suivant.
4. **Given** un serveur ne répond sur aucune des trois technologies, **When**
   le cycle est terminé, **Then** le serveur est marqué en échec et la station
   passe au suivant, puis boucle sur le premier après cooldown.
5. **Given** une option GUI est modifiée, **When** l'option est appliquée,
   **Then** le comportement runtime change de façon observable et testée.

---

### User Story 7 — Architecture modulaire, base de données fiable, dette traitée (Priority: P1)

Le système est refactoré par strangler : domaine pur, ports/adapters, use
cases, workers, interfaces. La base `registry.db` devient la source unique
pour les serveurs, transports, endpoints, IP, quotas, leases, observations,
scores et incidents. Les fichiers JSON et les tables legacy sont migrés puis
archivés. La dette listée dans `debt.md` est traitée par phases.

**Why this priority** : sans cette base, les correctifs de rotation resteront
fragiles et se re-casseront au prochain changement.

**Independent Test** : migrer une copie de la DB, comparer les décisions
anciennes/nouvelles sur 100 cas, vérifier 0 perte de ligne utile, 0 IP
partagée, 0 hostname sans provenance, et un rollback par backup.

**Acceptance Scenarios** :

1. **Given** une DB legacy, **When** la migration s’exécute, **Then** les
   serveurs, IP, observations et leases sont migrés avec provenance, et les
   lignes non bindables sont archivées, pas perdues.
2. **Given** le nouveau chemin, **When** une station est réparée, **Then**
   l’assignation est atomique en DB et 0 IP n’est partagée.
3. **Given** un redémarrage, **When** le superviseur reprend, **Then** il lit
   `registry.db`, pas un JSON d’état.
4. **Given** une migration, **When** elle échoue, **Then** le backup est
   restauré et l’ancien chemin reste opérationnel.
5. **Given** le registre de dette, **When** une phase se termine, **Then**
   0 P0 ouverte et les P1 sont planifiées.

---

### Edge Cases

- Toutes les IP connues sont `EXHAUSTED` avant leur reset : la flotte expose
  `capacity_degraded`, ne teste rien avant `reset_at`, et garde au moins une
  station `standby` prête.
- Le reset fournisseur tombe pendant une rotation : la nouvelle IP est
  conservée, l'ancienne reste `EXHAUSTED` jusqu'à son heure.
- Deux stations obtiennent la même IP : la seconde est libérée et re-claim,
  jamais assignée.
- Un probe renvoie 200 sans contenu : compté comme échec, jamais comme succès.
- Un hostname est valide dans l'API mais absent du manifeste du conteneur :
  `manifest_missing`, pas `no-egress`.
- Le compte NordVPN refuse une 7e session : la 7e station est `standby`, pas
  `broken`.
- Le superviseur meurt en cours de bind : au redémarrage, l'état est
  réconcilié depuis SQLite, pas depuis la mémoire.
- Un /24 entier tombe : les stations peuvent toutes utiliser le même /24
  (aucune contrainte de diversité), mais l'incident est signalé ; une
  préférence /24 optionnelle peut être activée dans le GUI.
- Un serveur Gluetun ancien fonctionne encore : il est utilisable et peut
  passer avant un host API médiocre si son taux de 200 récent est meilleur.
- Une clé WireGuard non enregistrée est utilisée : le bind est marqué
  `auth`/`key`, pas `no-egress`, et aucune IP n'est blacklistée.
- Une IP performante redevient disponible après `reset_at` : elle réintègre
  le classement et sera utilisée à la prochaine rotation qui en a besoin,
  sans bascule proactive.
- Tous les sites d'IP échouent mais le probe officiel répond 200 : la station
  n'est pas jetée ; l'IP est marquée `ip_unknown` et retentée en arrière-plan,
  sans affectation tant que l'unicité n'est pas prouvée.
- Tous les resolvers DNS échouent : la station utilise l'API de contrôle
  Gluetun ou un endpoint par IP littérale ; si rien ne marche, elle passe en
  `DEGRADED`, pas en `BROKEN` définitif.
- Un serveur ne supporte que OpenVPN TCP : le mode auto le détecte via le
  manifeste et saute WireGuard/OpenVPN UDP sans consommer d'essai inutile.
- Une option GUI est modifiée mais aucun consommateur runtime n'existe : le
  test de contrat GUI échoue et l'option est retirée ou marquée « affichage
  seul ».
- Une migration DB échoue à mi-chemin : rollback par backup, ancien schéma
  toujours lisible, aucune perte de ligne.
- Deux workers écrivent en même temps : WAL + transactions courtes, pas de
  `database is locked` persistant.
- La DB grossit au-delà du seuil : la purge de rétention s’exécute, les
  agrégats restent, le backup est vérifié.
- Un hostname existe dans `servers` mais aucun endpoint : il reste `untested`
  avec une raison, jamais sélectionnable.
- Une IP n’a pas de mapping serveur : elle est `ip_unknown`, non affectable.

---

## Requirements *(mandatory)*

### Functional Requirements — sélection, indépendance et unicité d'IP

- **FR-100** : Le système MUST maintenir un catalogue unifié des serveurs
  NordVPN avec, pour chaque hostname : pays, ville, load, technologies,
  endpoint IP, clé publique WireGuard, source (`api`, `gluetun`, `cache`),
  date de dernière observation API, date de dernière observation manifeste,
  et statut de fraîcheur.
- **FR-101** : Le système MUST NOT coder en dur un hostname, un pays, une
  région ou une plage d'IP dans les modules de sélection. Un test de
  régression MUST échouer si un littéral `al[0-9]+`, `87.120.102`, ou un nom
  de pays apparaît dans ces modules.
- **FR-102** : Chaque station MUST pouvoir utiliser **n'importe quel pays**,
  indépendamment des autres. Le système MUST NOT appliquer de quota par pays
  ni par continent. La seule contrainte dure de sélection est l'**unicité de
  l'IP d'egress** : une IP assignée à une station MUST NOT être assignée à une
  autre station, même si les deux hostnames sont différents. L'identité est
  l'IP, pas le hostname.
- **FR-103** : Si aucune IP unique et utilisable n'est disponible, le système
  MUST exposer `capacity_degraded` avec la raison (toutes les IP épuisées,
  toutes assignées, ou aucun candidat), et MUST NOT bloquer les autres
  stations.
- **FR-104** : Le classement MUST être un **score composite** calculé sur des
  observations récentes, avec des poids en configuration :
  taux de 200, latence, débit, stabilité et fraîcheur. Le pays et le continent
  ne MUST NOT être des facteurs de score. La source (`api`/`gluetun`) ne
  départage qu'à score égal.
- **FR-105** : Le classement MUST pénaliser un hostname dont le taux de 200
  récent est inférieur à `min_success_rate`. Un hostname Gluetun-only avec un
  bon taux de 200 récent MUST pouvoir passer avant un hostname API médiocre.
- **FR-106** : Le système MUST réserver une part d'exploration (≥ 5 %) au
  **balayage de découverte** (hostnames/IP non testés), et non aux
  affectations des stations : une station qui a besoin d'une IP prend la
  meilleure IP disponible, pas une IP « diverse ».
- **FR-107** : Un hostname ou un pool qui produit `N` échecs consécutifs MAY
  être temporairement dépriorisé, sans jamais exclure un pays durablement ni
  bloquer les autres. Il MUST être re-testé après expiration.
- **FR-108** : Le système MUST NOT utiliser un hostname dont le manifeste du
  conteneur cible ne contient pas l'entrée ; il MUST synchroniser, vérifier,
  puis marquer `manifest_missing` si l'entrée reste absente.
- **FR-109** : La clé WireGuard MUST être **commune à toutes les stations**,
  conformément à la configuration opérateur. Le système MUST vérifier le
  budget de sessions simultanées avec cette clé et MUST distinguer un échec
  `auth`/`key` d'un `no-egress`.

### Functional Requirements — quota, 429 et réparation

- **FR-110** : Chaque 429 MUST être classée (`daily`, `rate_limit`,
  `transient`, `unknown`, `invalid_session`) et traitée selon sa classe.
- **FR-111** : Un 429 `daily` MUST enregistrer `reset_at` depuis
  `Retry-After` (ou le reset fournisseur), marquer l'IP `EXHAUSTED`, et
  interdire tout probe ou bind avant `reset_at`.
- **FR-112** : Un 429 `transient` MUST NOT blacklister l'IP jusqu'au
  lendemain ; il MUST appliquer un cooldown court et un retry.
- **FR-113** : Un 429 sur le modèle demandé par le client MUST déclencher une
  réparation de la station, même si un autre modèle de probe a encore du
  quota.
- **FR-114** : La décision de réparation MUST NOT dépendre d'un unique flag
  `model_usable` global ; elle MUST être calculée par station et par modèle
  actif.
- **FR-115** : Le système MUST maintenir un pool chaud de `warm_pool_size` IP
  vérifiées (200 avec contenu, quota non épuisé), prêtes à être promues.
- **FR-116** : Quand une station tombe, le système MUST promouvoir une IP du
  pool chaud avant de lancer une réparation catalogue ; si le pool est vide,
  il MUST lancer la réparation immédiatement.
- **FR-117** : La promotion MUST être atomique : une IP ne peut être assignée
  qu'à une seule station à la fois ; une IP déjà assignée est libérée, jamais
  volée.
- **FR-118** : La rotation préventive MUST être une **option GUI**, désactivée
  par défaut. Si elle est activée, le seuil (`rotation_threshold_pct`) MUST
  être configurable ; si elle est désactivée, aucune rotation préventive ne
  doit avoir lieu.
- **FR-119** : Le système MUST NOT laisser une station sur une IP `EXHAUSTED`
  plus de `max_drain_s` ; au-delà, elle est `DRAINING` et retirée du routage
  même si le tunnel est `connected`.
- **FR-120** : Le système MUST re-tester un hostname ou une IP après un échec
  selon un backoff exponentiel borné, et MUST NOT le condamner
  définitivement.

### Functional Requirements — catalogue et balayage

- **FR-121** : Le système MUST exposer, pour chaque serveur : `untested`,
  `preflight_ok`, `preflight_failed`, `bind_ok`, `bind_error`, `429`, `dead`,
  `manifest_missing`, avec la date et la raison.
- **FR-122** : Le balayage MUST être repris : un redémarrage reprend au
  curseur persisté, sans re-tester les serveurs en backoff.
- **FR-123** : Le balayage MUST varier les pays et les sources pour la
  découverte, et MUST NOT être alphabétique. Il ne MUST NOT imposer de quota
  par pays : un pays peut être visité plusieurs fois si ses IP sont
  performantes.
- **FR-124** : Le balayage MUST utiliser **les deux sources** : API pour la
  fraîcheur (`status=online`, `load`, date) et Gluetun pour la couverture et
  la validation de bindabilité. Chaque hostname MUST porter sa provenance et
  sa fraîcheur ; la qualité observée, pas la source, MUST déterminer l'ordre
  final.
- **FR-125** : Le système MUST vérifier la fraîcheur de l'endpoint IP avant un
  bind : si l'API et le manifeste divergent, l'API fait foi pour l'endpoint,
  et le hostname est marqué `stale_endpoint` s'il n'est pas joignable.
- **FR-126** : Le système MUST pouvoir tester un endpoint sans créer une
  session complète (préflight handshake WireGuard ou équivalent) et MUST
  enregistrer le résultat.
- **FR-127** : Le système MUST borner le balayage (hosts/min, binds/min,
  sessions simultanées) et MUST exposer sa progression et son ETA.
- **FR-128** : Le système MUST NOT plafonner une réparation à 20 candidats ;
  le plafond, s'il existe, MUST être un paramètre de sécurité explicite et
  MUST être assez grand pour couvrir le catalogue en plusieurs cycles.

### Functional Requirements — fallbacks, transport et GUI

- **FR-150** : Le système MUST définir une **chaîne de fallback ordonnée** pour
  chaque dépendance externe : catalogue, manifeste, résolution DNS, site de
  test d'IP, bind, probe et superviseur. La chaîne MUST être en configuration,
  avec un ordre, un timeout et un backoff par entrée.
- **FR-151** : La détection d'IP MUST essayer, dans l'ordre : SOCKS5h + URLs,
  SOCKS5 + URLs, proxy HTTP + URLs, API de contrôle Gluetun
  (`/v1/publicip/ip`), puis `docker exec` dans le conteneur. Si un site
  échoue, le suivant MUST être utilisé sans recréer la station.
- **FR-152** : Si tous les sites d'IP échouent mais que le probe officiel
  répond 200, la station MUST NOT être jetée : elle est marquée `ip_unknown`,
  l'IP est retentée en arrière-plan, et aucune affectation n'a lieu tant que
  l'unicité n'est pas prouvée.
- **FR-153** : Le DNS MUST avoir plusieurs resolvers de secours (DoH, DoT,
  plain) et MUST basculer automatiquement si le resolver courant échoue.
  L'échec et le resolver utilisé MUST être journalisés. Le resolver hôte MAY
  être utilisé pour le control-plane (catalogue/API), jamais pour le trafic
  de la station (fuite DNS hors tunnel interdite).
- **FR-154** : Le catalogue MUST avoir une chaîne API → cache → manifeste
  Gluetun → embarqué → registre local. Une source indisponible MUST NOT
  bloquer les autres.
- **FR-155** : Le manifeste MUST avoir une chaîne volume de la station →
  cache fusionné → embarqué ; le hostname MUST être vérifié après chaque
  synchronisation.
- **FR-156** : Le bind MUST avoir plusieurs méthodes : Compose override,
  API de contrôle Gluetun si elle supporte le hostname, puis redémarrage
  Docker avec le pays. Une méthode qui échoue MUST passer à la suivante.
- **FR-157** : En mode `vpn_stack: auto`, chaque station MUST essayer
  **WireGuard, OpenVPN UDP et OpenVPN TCP** pour un serveur donné, dans
  l'ordre configuré, et boucler sur les trois avant de passer au serveur
  suivant. Chaque technologie MUST être indépendante par station.
- **FR-158** : Le système MUST vérifier que le hostname supporte la
  technologie avant l'essai (technologies API / manifeste) et MUST NOT
  compter un transport non supporté comme un échec.
- **FR-159** : Chaque tentative de transport MUST être journalisée avec
  `(hostname, transport, IP, statut, cause)` et MUST alimenter le score par
  transport.
- **FR-160** : Les essais OpenVPN MUST passer par le budget AUTH global et un
  backoff, pour éviter une tempête d'AUTH_FAILED quand plusieurs stations
  bouclent sur les trois transports.
- **FR-161** : Le GUI MUST exposer le mode `auto` et l'ordre des transports ;
  une sélection manuelle (`wireguard`, `openvpn-udp`, `openvpn-tcp`) MUST
  forcer ce transport pour la station.
- **FR-162** : Chaque option GUI MUST avoir une entrée dans un **contrat GUI**
  (`option → clé config → consommateur runtime → effet observable → test`).
  Une option sans consommateur MUST être supprimée ou marquée « affichage
  seul ».
- **FR-163** : Le système MUST exécuter un audit de configuration en mode
  strict (`scripts/config_audit.py`) et un test de contrat GUI : aucune option
  ne doit être inerte.
- **FR-164** : Le GUI MUST afficher la valeur **effective** (pas seulement la
  valeur enregistrée) et le dernier moment où elle a été appliquée.
- **FR-165** : Chaque bascule de fallback MUST être observable : source
  utilisée, raison de l'échec précédent, durée, et prochaine tentative.

### Functional Requirements — architecture, maintenabilité et données

- **FR-170** : Le domaine MUST être pur (aucune dépendance à Docker, HTTP,
  SQLite, `asyncio`) ; les dépendances MUST passer par des ports.
- **FR-171** : Les use cases MUST être testables avec des adapters factices,
  sans conteneur ni réseau.
- **FR-172** : La migration MUST suivre un **strangler** : nouveau chemin à
  côté de l’ancien, shadow, canary, puis décommissionnement ; pas de big-bang.
- **FR-173** : Aucun nouveau module MUST dépasser 1 500 lignes ; les
  monolithes existants sont extraits par lots, avec des shims de compatibilité.
- **FR-174** : `registry.db` MUST être la source unique des serveurs,
  transports, endpoints, IP, quotas, leases, observations, scores et
  incidents ; les JSON d’état autoritatifs MUST être migrés puis supprimés.
- **FR-175** : Le schéma MUST avoir des clés primaires, clés étrangères,
  contraintes `CHECK`, index et migrations versionnées.
- **FR-176** : Chaque serveur, endpoint et IP MUST porter sa provenance
  (`source_id`, `fetched_at`, `first_seen`, `last_seen`, `stale`).
- **FR-177** : L’unicité d’IP MUST être garantie par une contrainte DB
  (index unique partiel sur les leases actifs) et vérifiée par un test.
- **FR-178** : Les observations MUST être append-only ; les scores MUST être
  matérialisés à part et recalculés sur une fenêtre récente.
- **FR-179** : La base MUST avoir une rétention (observations 30 j, télémétrie
  7–30 j, incidents 90 j), une purge quotidienne, un checkpoint WAL et un
  backup `VACUUM INTO` vérifié.
- **FR-180** : La migration MUST être testée sur une copie, avec
  `integrity_check` avant/après et rollback par backup.
- **FR-181** : Les lignes non bindables (`hostname=''`) MUST être archivées
  dans une table legacy, pas supprimées sans backup, et exclues de la
  sélection.
- **FR-182** : Chaque phase MUST mettre à jour `debt.md` : 0 P0 ouverte,
  P1 planifiée, pas de nouvelle dette non listée.
- **FR-183** : Le système MUST exposer la version du schéma, la dernière
  migration, la taille DB, la date du dernier backup et le résultat
  `integrity_check`.
- **FR-184** : Le code MUST respecter les règles de dépendance (domaine sans
  I/O, pas de cycle) ; un test d’import le vérifie.
- **FR-185** : Toute nouvelle table MUST avoir une migration, des index, une
  contrainte d’intégrité et un test de repository.

### Functional Requirements — automation et exploitation

- **FR-129** : Le **superviseur** MUST être le seul propriétaire de la
  réparation. Le manager/watchdog MUST NOT recréer une station que le
  superviseur répare.
- **FR-130** : Le superviseur MUST démarrer avec le proxy et être relancé par
  un watchdog s'il meurt.
- **FR-131** : Le système MUST réconcilier l'état depuis SQLite au boot, pas
  depuis la mémoire, et MUST expirer les leases orphelins.
- **FR-132** : Le lease d'un hostname MUST être plus long que la durée
  maximale d'un bind (`lease_ttl_s > bind_timeout_s + marge`), et MUST être
  renouvelé pendant le bind.
- **FR-133** : Le système MUST exposer `client_usable`, `warm_pool`,
  `unique_ip`, `sweep_progress`, `last_429`, `reset_at`, `score`,
  `next_candidate` et la raison de chaque station non utilisable, en JSON et
  dans le dashboard.
- **FR-134** : Chaque décision MUST être journalisée avec station, hostname,
  IP, pays, cause, horodatage, durée et résultat.
- **FR-135** : `station_count` MUST être le seul paramètre nécessaire pour
  ajouter ou retirer une station, avec un état `standby` au-delà du budget de
  sessions.
- **FR-136** : Le système MUST vérifier au boot la cohérence
  `config.yaml` / `.env` / `docker-compose.yml` / conteneurs et exposer les
  divergences (type VPN, protocole, clé, hostname, station_count).
- **FR-137** : Le système MUST NOT compter une station `standby`, `DRAINING`
  ou `EXHAUSTED` comme `broken` ; chaque état a un sens distinct.
- **FR-138** : Le système MUST supprimer ou désactiver explicitement toute
  station hors `station_count` (ex. station 7) et vérifier le budget de
  sessions avant de démarrer.
- **FR-139** : Au démarrage, le système MUST conserver toute station déjà
  connectée et **client-utilisable** (200 avec contenu, pas de 429 actif),
  sans la recréer. Il MUST compléter uniquement les stations manquantes ou
  non utilisables, avec les meilleures IP disponibles. Une station connectée
  sans preuve récente MUST être testée 2–3 fois avant d'être déclarée non
  utilisable ; un timeout isolé ne MUST NOT déclencher une rotation.
- **FR-140** : Les réparations MUST être **par station** et indépendantes :
  un 429 sur une station MUST NOT suspendre ni retarder la rotation des
  autres. Les réparations MUST pouvoir tourner en parallèle, dans la limite
  du budget de sessions et du budget AUTH global.
- **FR-141** : Le système MUST affecter à une station qui a besoin d'une IP la
  **meilleure IP disponible** selon le score composite. Une IP épuisée MUST
  sortir du classement jusqu'à `reset_at`, puis MUST redevenir éligible avec
  son score historique ; elle MUST être réutilisée à la prochaine rotation qui
  en a besoin, sans bascule proactive des stations déjà utilisables.
- **FR-142** : Le système MUST distinguer le score d'une **IP connue** et le
  score d'un **hostname non encore bindé** (agrégat de ses IP observées, taux
  de bind et fraîcheur). Après bind, l'IP obtenue MUST être notée
  individuellement et entrer dans le classement.

### Non-Functional Requirements

- **NFR-100 (latence)** : remplacement d'une IP 429 en < 60 s p95, mesure
  depuis la détection jusqu'au premier 200 sur la remplaçante.
- **NFR-101 (débit)** : un bind réussi (bind + egress + probe) en < 30 s p95
  pour un hostname préflighté ; un échec de préflight en < 2 s p95.
- **NFR-102 (capacité)** : le balayage complet des 9 787 serveurs Gluetun doit
  pouvoir s'exécuter en < 24 h en arrière-plan, sans dégrader le service.
- **NFR-103 (fiabilité)** : `client_usable >= 5/6` ≥ 99,9 % sur 30 jours,
  jamais 0/6 ; 6/6 visé ≥ 99 %.
- **NFR-104 (sécurité)** : aucune IP ni hostname codé en dur ; les secrets
  restent dans `.env`/secrets, jamais dans les logs.
- **NFR-105 (observabilité)** : toute panne de station produit un incident
  avec début, fin, cause et remédiation.
- **NFR-106 (maintenabilité)** : un seul module de sélection, testé
  unitairement, sans dépendance à Docker dans la logique de score.
- **NFR-107 (unicité)** : 0 IP d'egress partagée par deux stations à tout
  moment, y compris pendant une promotion ou un redémarrage. Toute violation
  est un incident P0.
- **NFR-108 (indépendance)** : une rotation sur une station ne doit pas
  ajouter plus de `rotation_max_duration` de latence aux autres stations ;
  aucune pause globale ne doit être déclenchée par un 429 quota.
- **NFR-109 (fallback)** : aucune dépendance unique (site d'IP, DNS,
  catalogue, manifeste, méthode de bind, transport, superviseur) ne doit
  pouvoir rendre une station inutilisable ; chaque chaîne doit avoir au moins
  deux chemins indépendants.
- **NFR-110 (transport)** : le mode auto doit tester les trois technologies
  en moins de `transport_cycle_timeout_s` par serveur, et ne jamais boucler
  sans backoff.
- **NFR-111 (GUI)** : 100 % des options GUI ont un test d'effet ; 0 option
  inerte ; l'audit config strict et le contrat GUI passent en CI.
- **NFR-112 (maintenabilité)** : aucun nouveau module > 1 500 lignes ; domaine
  sans I/O ; test d’import des règles de dépendance.
- **NFR-113 (données)** : `registry.db` < 250 Mo en régime nominal ;
  `PRAGMA integrity_check` = `ok` ; 0 IP partagée ; 0 hostname sans
  provenance.
- **NFR-114 (migration)** : migration d’une copie de production en < 10 min,
  sauvegarde et rollback testés.
- **NFR-115 (dette)** : 0 P0 ouverte à chaque gate ; P1 planifiée ; pas de
  nouvelle dette non listée.

---

## Key Entities

- **CatalogServer** : hostname, pays, ville, load, technologies, endpoint IP,
  clé publique WG, source, `api_seen_at`, `manifest_seen_at`, `stale`,
  `score_host`, statut.
- **ServerTransport** : hostname, transport (WG/OV UDP/OV TCP), supporté,
  `wg_pubkey`, dernier OK, dernière erreur.
- **EndpointTest** : hostname, endpoint IP, transport, type
  (handshake/icmp/tcp), ok, RTT, date, erreur, source de fallback.
- **EgressIp** : IP, hostname, pays, ASN, `first_seen`, `last_ok`, `last_429`,
  `reset_at`, succès, échecs, latence, débit, `score_ip`, état.
- **QuotaBucket** : IP, modèle, jour UTC, bucket, utilisé, reset_at, source.
- **ScoreSnapshot** : type (IP/host), clé, transport, score, composantes,
  échantillons, date de calcul.
- **ConfigSnapshot** : date, source, payload normalisé, checksum.
- **Station** : id, conteneur, port SOCKS, `transport_effective`,
  `transport_order`, état
  (`BOOT/CONNECTING/VERIFYING/USABLE/DRAINING/REPAIRING/STANDBY/BROKEN`),
  pays, IP, hostname, `client_usable`.
- **Lease** : hostname, station, date de début, expiration, statut.
- **WarmSpare** : IP, hostname, pays, modèle, `verified_at`, `expires_at`,
  score.
- **RepairJob** : station, cause, candidats essayés, résultat, durée.
- **SweepCursor** : pays, hostname, dernier test, prochain éligible, compteur.
- **Observation** : station, hostname, IP, transport, statut, `retry_after`,
  `reset_at`, latence, modèle, date, validation du contenu.
- **FallbackEvent** : dépendance, source précédente, source suivante, raison,
  station, début, durée, succès.
- **Incident** : station/flotte, début, fin, cause, remédiation.

## Success Criteria *(mandatory)*

- **SC-100** : Après un 429 forcé sur une station, le trafic repart sur une
  IP vérifiée en < 60 s p95, sans commande manuelle.
- **SC-101** : `client_usable >= 5/6` ≥ 99,9 % et 6/6 ≥ 99 % sur 30 jours
  glissants ; `routable=0` ≤ 0,1 % des échantillons.
- **SC-102** : 0 IP d'egress partagée par deux stations à tout moment ; chaque
  station peut utiliser n'importe quel pays, sans quota par pays ni par
  continent.
- **SC-103** : Zéro littéral de hostname/pays/préfixe IP dans les modules de
  sélection (test de régression).
- **SC-104** : Le balayage couvre 100 % des serveurs API et Gluetun en < 24 h,
  avec un statut et une raison pour chacun.
- **SC-105** : Le taux de bind réussi passe de ~15 % à ≥ 60 % sur les
  candidats préflightés, et le taux de 200 sur les binds réussis à ≥ 50 %.
- **SC-106** : 0 station `connected` avec une IP `EXHAUSTED` reste
  `client_usable` plus de `max_drain_s`.
- **SC-107** : 0 station hors `station_count` en marche ; 0 divergence de
  configuration non exposée.
- **SC-108** : Un redémarrage du superviseur reprend le balayage au curseur
  persisté et ne re-teste pas les serveurs en backoff.
- **SC-109** : Au démarrage, 100 % des stations déjà `client_usable` sont
  conservées sans recréation ; seules les stations manquantes sont
  complétées.
- **SC-110** : Deux 429 simultanés sur deux stations produisent deux
  rotations parallèles, sans pause globale et sans que l'une attende l'autre.
- **SC-111** : Une IP performante épuisée puis remise à disposition après
  `reset_at` est réutilisée à la prochaine rotation qui en a besoin, sans
  bascule proactive d'une station déjà utilisable.
- **SC-112** : Site d'IP principal coupé → la station trouve une IP via un
  fallback et reste utilisable si le probe officiel répond 200 ; 0 station
  jetée sur un échec de site d'IP.
- **SC-113** : Un serveur qui ne répond qu'en OpenVPN TCP est trouvé par le
  mode auto ; un serveur qui ne répond sur aucun des trois transports est
  marqué en échec et le suivant est essayé.
- **SC-114** : 100 % des options GUI ont un test d'effet ; 0 option inerte ;
  le contrat GUI et `config_audit.py --strict` passent en CI.
- **SC-115** : Chaque bascule de fallback est visible dans le dashboard avec
  la source utilisée et la raison de l'échec précédent.
- **SC-116** : `registry.db` est la seule source d’état autoritatif ; aucun
  JSON d’état n’est lu pour une décision de rotation.
- **SC-117** : Migration d’une copie de production réussie, `integrity_check`
  = ok, 0 perte de ligne utile, rollback par backup testé.
- **SC-118** : 0 IP partagée et 0 hostname sans provenance dans la DB après
  migration.
- **SC-119** : Aucun nouveau module > 1 500 lignes ; test d’import des
  dépendances vert.
- **SC-120** : `debt.md` : 0 P0 ouverte, P1 planifiée, pas de nouvelle dette
  non listée à chaque gate.

## Clarifications

### Session 2026-11-04

- **C1 — Définition de « client-utilisable »** : tunnel connecté + egress
  vérifié + probe 200 **avec contenu** + aucun 429 actif pour le modèle
  demandé. Le dashboard doit utiliser cette définition, pas `healthy`.
- **C2 — Quota** : le quota free est par IP d'egress, pas par compte. Les
  décisions et les messages doivent rester par IP.
- **C3 — Catalogue** : les deux sources sont utilisées. L'API apporte la
  fraîcheur ; Gluetun apporte la couverture et la validation de bindabilité.
  Aucun catalogue n'est jeté ; la qualité observée, pas la source, décide de
  l'ordre.
- **C4 — Pays et IP** : toutes les stations peuvent utiliser n'importe quel
  pays, indépendamment. Aucun quota par pays ni par continent. Seule
  contrainte dure : une IP d'egress n'est jamais partagée par deux stations.
- **C5 — Périmètre** : ce document est un plan ; aucune tâche n'est exécutée
  dans cette session.

### Session 2026-11-05

- **C6 — Score** : score composite (taux de 200, latence, débit, stabilité,
  fraîcheur). Poids par défaut proposés : 40/25/20/10/5, ajustables dans le
  GUI.
- **C7 — Reset** : une IP performante redevient éligible après `reset_at` et
  est réutilisée à la prochaine rotation qui en a besoin. Pas de bascule
  proactive d'une station déjà utilisable.
- **C8 — Préflight** : accepté. Test rapide de l'endpoint avant de recréer le
  conteneur, pour éviter ~60 s sur un serveur mort.
- **C9 — Démarrage** : conserver les stations déjà connectées et utilisables ;
  compléter uniquement les manquantes.
- **C10 — Parallélisme** : chaque station se répare indépendamment, en
  parallèle ; aucun gel global pour un 429 quota.
- **C11 — Clé WG** : clé commune à toutes les stations.
- **C12 — Proactive** : option GUI, désactivée par défaut.
- **C13 — Station 7** : supprimée.
- **C14 — Propriétaire** : superviseur seul.
- **C15 — Fallbacks** : maximum de fallback sur toutes les dépendances
  externes (IP, DNS, catalogue, manifeste, bind, probe, transport,
  superviseur). Une dépendance unique ne doit jamais rendre une station
  inutilisable.
- **C16 — Transport auto** : en mode `auto`, chaque station essaie
  WireGuard, OpenVPN UDP, OpenVPN TCP, puis boucle ; chaque station est
  indépendante.
- **C17 — GUI** : chaque option GUI doit avoir un effet réel, un consommateur
  runtime et un test ; aucune option inerte.
- **C18 — Architecture** : refonte par strangler (domaine pur, ports/adapters,
  use cases, workers) ; pas de big-bang.
- **C19 — Base de données** : `registry.db` source unique, schéma normalisé,
  migrations, contraintes, provenance, rétention, backup, intégrité.
- **C20 — Dette** : registre `debt.md` suivi par phase ; 0 P0 ouverte aux
  gates.

### Session 2026-11-05 — Clarify

Scan de clarification : **aucune ambiguïté bloquante restante**. Les décisions
C6–C20 couvrent le périmètre, les données, les fallbacks, le transport, le GUI,
l’architecture et la dette. Les points ouverts sont **empiriques**, pas des
ambiguïtés de spec :

- budget exact de sessions simultanées avec la clé partagée → test Phase 0 ;
- validation réelle OpenVPN UDP/TCP → test Phase 0 ;
- taux de faux positifs/négatifs du préflight → test Phase 0.

Ces points ne bloquent ni le plan ni les tâches ; ils sont suivis dans
`checklists/requirements.md` (CHK048–CHK050).

## Poids du score composite (tranchés)

Poids retenus, ajustables dans le GUI :

| Composant | Poids | Justification |
|---|---:|---|
| Taux de 200 récent | **40 %** | prédit le plus directement la capacité à servir |
| Latence | **25 %** | première perception de performance |
| Débit | **20 %** | qualité des réponses longues |
| Stabilité | **10 %** | évite les IP qui oscillent |
| Fraîcheur | **5 %** | départage les observations anciennes |

Toutes les décisions sont actées.

## Assumptions

- La clé WireGuard est commune à toutes les stations, conformément à la
  configuration opérateur. Le nombre exact de sessions simultanées avec cette
  clé est vérifié au boot et exposé ; les stations au-delà du budget sont
  `STANDBY`, pas `BROKEN`.
- Le trafic free reste de l'ordre de quelques dizaines de requêtes par jour,
  avec des pics absorbables par le pool chaud.
- Le reset quota est un bucket quotidien par IP (~00:10 UTC observé) ;
  `Retry-After` fait foi quand il est présent.
- L'impersonation de la porte est acquise et hors périmètre.
- Le catalogue Gluetun peut contenir des serveurs morts ; il est utile comme
  repli, pas comme vérité.
- Windows + Docker Desktop + PowerShell ; le service démarre au boot.

