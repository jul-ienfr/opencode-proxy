# Audit — rotation VPN, catalogue mondial et 429

**Date** : 2026-11-04  
**Périmètre** : jambe free, six stations Docker/Gluetun, catalogue NordVPN, catalogue
Gluetun, sélection d'IP, réparation automatique, observabilité.  
**Méthode** : lecture du code et de la configuration, requêtes sur les bases
`logs/ip_registry.db`, `logs/catalog_claims.db`, `logs/requests.db`,
`logs/station_slo.jsonl`, appels à `/api/vpn-status` et `/api/free-health`,
inspection Docker, tests de tunnels sur des serveurs isolés.  
**Règle** : chaque conclusion sur une IP ou un serveur vient d'un test de cette IP
ou de ce serveur ; aucune conclusion n'est extrapolée à l'ensemble du catalogue.

---

## 1. Résumé exécutif

Le système ne manque pas de serveurs. Il manque d'un mécanisme de sélection,
de vérification et de réparation qui exploite le catalogue disponible.

À l'heure de l'audit :

- **0/6 stations `model_usable`** ; 2 tunnels `connected` mais bucket quotidien
  épuisé, 4 stations en `error`.
- Sur 1 683 échantillons SLO (26/10 → 04/11) : **478 échantillons à
  `routable=0` (28 %)**, 818/1 631 à `routable <= 2` (50 %), et **46/1 631
  seulement à 6/6 routables (2,8 %)**.
- Le catalogue contient **9 787 serveurs WireGuard Gluetun** dans **150 pays**,
  et **6 948 serveurs WireGuard** dans l'API NordVPN. Le problème n'est donc
  pas l'absence de serveurs ni l'absence de pays.
- Les tentatives récentes sont massivement des échecs de bind :
  **417 `bind_error`**, 49 `429`, 0 `ok` dans la base de claims courante ;
  les fichiers d'état montrent 80–90 % de `bind-error` par station.
- La station 6 a pourtant un index de **5 652 candidats testés** dans son
  historique : le système a déjà essayé des milliers de serveurs. Le problème
  est le **rendement** et l'**ordre** des tentatives, pas le volume brut.

Conclusion : le système teste beaucoup, mais teste **au mauvais endroit, dans
le mauvais ordre, avec un plafond de 20 candidats par chaîne et sans vérifier
que le serveur est encore vivant avant de recréer le conteneur**. Il faut
reconcevoir la sélection, pas ajouter une boucle de plus.

Trois angles morts supplémentaires : **la détection d'IP et le DNS n'ont pas
de vrai fallback**, **le mode VPN auto n'essaie pas les trois technologies par
serveur**, et **les options GUI ne sont pas prouvées effectives**. Ces trois
points sont traités dans le plan aux sections 5.9 à 5.13.

---

## 2. Constats mesurés

### 2.1 Catalogue réellement disponible

| Source | Serveurs WireGuard | Pays | Remarque |
|---|---:|---:|---|
| API NordVPN (`logs/nordvpn_api.json`) | 6 948 | 150 | source la plus fraîche, `status=online` |
| Manifeste Gluetun fusionné (`logs/gluetun_nordvpn_servers.json`) | 9 787 | 150 | union des manifestes, contient l'historique |
| Serveurs présents dans les deux | 6 948 | — | 100 % de l'API est dans Gluetun |
| Serveurs **uniquement** dans Gluetun | 2 839 | — | absents de l'API courante ; **pas tous morts** (voir 2.8) |

Le catalogue Gluetun est un **sur-ensemble** de l'API : il contient des
serveurs qui ne sont plus dans l'API. Ils ne doivent pas être jetés — une
partie fonctionne encore — mais ils doivent être **classés par qualité
observée**, pas par source. L'API apporte la fraîcheur (`status=online`,
`load` courant) ; Gluetun apporte la couverture et la clé publique WireGuard.
Les deux sont nécessaires.

### 2.2 Rendement des tentatives

Base `logs/catalog_claims.db` (état courant, 530 hostnames testés) :

| Statut | Nombre |
|---|---:|
| `bind_error` (dont `no-egress`) | 417 |
| `429` | 49 |
| `no_egress` | 41 |
| `abandoned` | 19 |
| `duplicate` | 3 |
| `ok` | 0 |

Fichiers d'état par station (500 derniers résultats) :

| Station | bind-error | 429 | 200 | index historique |
|---|---:|---:|---:|---:|
| 1 | 416 | 64 | 2 | 17 |
| 2 | 398 | 79 | 1 | 6 |
| 3 | 404 | 84 | 1 | 6 |
| 4 | 415 | 68 | 0 | 5 |
| 5 | 447 | 44 | 3 | 1 117 |
| 6 | 414 | 64 | 2 | **5 652** |

Le taux de 200 sur les tentatives récentes est inférieur à 1 %. Le système
brûle donc l'essentiel de son budget de rebind sur des endpoints qui ne
établissent jamais de tunnel.

### 2.3 Tests de tunnels isolés (hors production)

Un conteneur Gluetun temporaire a été lancé avec la clé WireGuard déjà
enregistrée du compte et un serveur précis, sans toucher aux stations :

| Serveur | Pays | Endpoint | Résultat |
|---|---|---|---|
| `uk2639.nordvpn.com` | Royaume-Uni | `194.88.96.6:51820` | **OK** — egress `194.88.96.71` |
| `za173.nordvpn.com` | Afrique du Sud | `186.247.68.240:51820` | échec, DNS/timeout après 50 s |
| `ar82.nordvpn.com` | Argentine | `187.13.30.111:51820` | échec |
| `jp842.nordvpn.com` | Japon | `186.247.39.62:51820` | échec |
| `au755.nordvpn.com` | Australie | `144.48.37.11:51820` | échec |

Ce test ponctuel confirme deux choses : le catalogue contient bien des serveurs
vivants hors Albanie (UK), et une partie importante des endpoints testés est
injoignable **au moment du test**. Il ne permet pas de conclure que ces
serveurs sont définitivement morts : ils doivent être re-testés, pas bannis à
vie.

### 2.4 SLO observé

`logs/station_slo.jsonl`, 1 683 échantillons du 26/10 au 04/11 :

| Indicateur | Valeur |
|---|---:|
| `routable=0` | 478 / 1 683 (28,4 %) |
| `routable <= 2` (fenêtre récente) | 818 / 1 631 (50,1 %) |
| `routable = 6` | 46 / 1 631 (2,8 %) |
| `healthy = 6` | 824 / 1 631 (50,5 %) |

C'est la définition exacte du symptôme « stations connectées mais pas
utilisables » : le tunnel est souvent `healthy`, mais l'IP n'est pas
`routable`. L'observabilité actuelle mesure le tunnel, pas la capacité à
servir une requête free.

### 2.5 État live au moment de l'audit

`GET /api/vpn-status` :

| Station | VPN | IP | `station_usable` | `model_usable` | Raison |
|---|---|---|---:|---:|---|
| 1 | connected | `187.13.19.102` | true | false | `daily-bucket-exhausted` |
| 2 | error | `187.13.19.99` | false | false | `station-unusable` |
| 3 | error | `187.13.19.115` | false | false | `station-unusable` |
| 4 | connected | `187.40.18.37` | true | false | `daily-bucket-exhausted` |
| 5 | error | `187.13.19.108` | false | false | `station-unusable` |
| 6 | error | `186.247.72.68` | false | false | `station-unusable` |

Les IP `187.13.19.*` et `187.40.18.*` ne sont pas en Albanie : la géolocalisation
`ipinfo` les place en **Australie** (GSL Networks) et l'IP `87.120.102.*` en
Albanie. Le problème n'est donc pas « toutes les IP sont albanaises » ; le
problème est que **la sélection et les protections favorisent historiquement
les serveurs albanais**, ce qui a produit cette impression et cette dépendance.

### 2.6 Registre d'IP

`logs/ip_registry.db` :

| Mesure | Valeur |
|---|---:|
| Lignes `ip_state` | 4 413 |
| IP distinctes | 4 414 |
| Hostnames distincts | 219 |
| Lignes **sans hostname** | **3 286** |
| Lignes **sans pays** | **3 549** |
| Hosts avec plus d'une IP | 147 |
| Maximum d'IP distinctes pour un host | **169** (`us14278.nordvpn.com`) |
| Hosts avec 200 observé | 3 639 lignes (3639/4413) |
| Hosts jamais testés | 517 |

Deux enseignements :

1. **Un même hostname peut produire des dizaines d'IP différentes** (NAT
   NordVPN). Le nombre de serveurs n'est donc pas le seul levier : le nombre
   d'IP distinctes et leur quota le sont aussi.
2. Le registre est **pollué par 3 286 lignes sans hostname**, issues d'un
   import d'inventaire. `eligible()` ne filtre pas sur `hostname` : ces lignes
   sont sélectionnables, puis échouent au bind avec `empty-hostname`. C'est une
   source directe de faux stock et de tentatives perdues.

### 2.7 Qualité par hostname (registre)

Quelques exemples sur les hostnames les plus observés :

| Hostname | Lignes | 200 | 429 | Taux 200 |
|---|---:|---:|---:|---:|
| `al74.nordvpn.com` | 26 | 19 | 5 | 73 % |
| `uk2639.nordvpn.com` | 15 | 14 | 0 | **93 %** |
| `za173.nordvpn.com` | 12 | 10 | 2 | 83 % |
| `al46.nordvpn.com` | 11 | 9 | 2 | 82 % |
| `ch524.nordvpn.com` | 10 | 7 | 3 | 70 % |
| `au755.nordvpn.com` | 11 | 6 | 5 | 55 % |
| `us14278.nordvpn.com` | 169 | 6 | 156 | **3,6 %** |

Il existe donc une **qualité par hostname** très discriminante, déjà stockée
dans le registre, mais **non utilisée par le classement de la chaîne**. Le
classement actuel se base sur `requests.db` (débit) et sur un booléen
« a déjà eu un 200 », pas sur le taux de succès réel.

Attention : les compteurs `ip_state.tests` / `successes` ne sont pas fiables
pour le classement. L'import d'inventaire a créé des lignes sans hostname avec
des compteurs élevés et 100 % de succès (ex. `187.13.9.217`, 2 744 « tests »,
0 échec, aucun hostname). Le score doit être calculé depuis `observations`
(hostname, IP, statut, date, station), pas depuis ces compteurs agrégés.

### 2.8 API vs Gluetun : quel catalogue utiliser ?

Comparaison exhaustive des 6 948 serveurs WireGuard de l'API et des 9 787 du
manifeste Gluetun :

| Mesure | Résultat |
|---|---:|
| Serveurs API absents du manifeste Gluetun | **0** |
| Serveurs Gluetun-only | **2 839** |
| Endpoint IP API ≠ endpoint Gluetun | **0 / 6 948** |
| Pays API ≠ pays Gluetun | **0 / 6 948** |

Les deux sources sont donc **cohérentes** pour les serveurs qu'elles
partagent. L'API est un sous-ensemble propre de Gluetun ; elle apporte
seulement la fraîcheur (`status=online`, `load`, date de mise à jour) et
l'absence des 2 839 serveurs retirés.

Qualité observée sur 7 jours (`observations`) :

| Source | Observations 200 | 429 | Échecs | Taux 200 |
|---|---:|---:|---:|---:|
| Hostnames présents dans l'API | 845 | 13 | 7 | 97,7 % |
| Hostnames Gluetun-only | 408 | 2 | 3 | **98,8 %** |

Parmi les hostnames avec assez d'observations, la médiane du taux de 200 est
de **100 %** dans les deux groupes ; 192/205 hosts API et 57/62 hosts
Gluetun-only sont à ≥ 50 %.

Conclusion : **il ne faut pas choisir un catalogue contre l'autre**. Les
hosts Gluetun-only ne sont pas « morts » ; certains sont excellents. Le
discriminant n'est pas la source, c'est la **qualité observée récente**.
L'API sert à découvrir les serveurs frais ; Gluetun sert de couverture et de
validation de bindabilité.

### 2.9 Clé WireGuard partagée et sessions simultanées

Les conteneurs de production utilisent tous la même clé WireGuard. Mesure
exacte : **7 conteneurs `opencode-vpn*` en marche, 7 valeurs inspectées, 1
seule clé distincte**. Plusieurs étaient `connected` simultanément. La clé
partagée est donc compatible avec au moins plusieurs sessions simultanées.

Le nombre maximal exact de sessions simultanées avec cette clé n'est pas
exposé par NordVPN ; il doit être mesuré en Phase 0 et exposé dans le GUI.
Tant qu'il n'est pas atteint, les stations peuvent tourner en parallèle. Si le
fournisseur refuse une session, la station doit être `STANDBY`, pas `BROKEN`.

### 2.10 IP réellement disponibles

Sur 7 jours, `observations` contient :

- **629 IP distinctes** avec au moins un 200 ;
- **249 hostnames** avec au moins un 200 ;
- des IP performantes dans de nombreux pays (BE, AU, CA, CH, US, BR, AL, …).

Le stock d'IP utilisables n'est donc pas le problème. Le problème est de les
**découvrir, les classer et les réutiliser** au bon moment.

### 2.11 Parallélisme avec la clé partagée

Test contrôlé avec **deux conteneurs Gluetun temporaires**, la **même clé
WireGuard** que la production, et deux serveurs différents, lancés en
parallèle :

| Conteneur | Serveur | Egress observé |
|---|---|---|
| `audit-vpn-a` | `uk2639.nordvpn.com` | `194.88.96.68` |
| `audit-vpn-b` | `al74.nordvpn.com` | `87.120.102.56` |

Les deux tunnels ont répondu **en même temps**, avec des IP distinctes. Le
même serveur `uk2639` a produit `194.88.96.71` puis `194.88.96.68` lors de
deux connexions successives : un hostname peut donc rendre des IP différentes
au fil des reconnections.

Conclusion : le modèle « chaque station indépendante, en parallèle, avec la
clé partagée » est **faisable**. Le nombre maximal de sessions simultanées
avec cette clé reste à mesurer précisément, mais il est au moins supérieur à
2 (et 7 conteneurs de production tournaient déjà avec la même clé).

### 2.12 Matrice de tests

| Test | Statut | Résultat |
|---|---|---|
| API vs Gluetun : endpoint, pays, couverture | **fait** | 0 divergence sur 6 948 ; 2 839 Gluetun-only |
| Qualité historique par source (7 j) | **fait** | API 845 200 / 13 429 ; Gluetun-only 408 200 / 2 429 |
| IP réellement disponibles (7 j) | **fait** | 629 IP, 249 hostnames |
| Bind réel sur serveurs isolés | **fait** | UK OK ; ZA/AR/JP/AU en échec au moment du test |
| Même hostname, IP différentes | **fait** | `uk2639` → `.71` puis `.68` |
| Parallélisme clé partagée | **fait** | 2 conteneurs, 2 IP distinctes simultanées |
| Limite exacte de sessions simultanées | **à faire** | 6/7/8/10 à mesurer sans perturber la prod |
| Taux de faux positifs/négatifs du préflight | **à faire** | à mesurer sur 200 hosts |
| Probabilité de collision d'IP entre deux binds | **à faire** | à mesurer sur 100 binds |
| Reset : réutilisation après `reset_at` | **à faire** | à vérifier sur une IP 429 |
| Audit config strict (`config_audit.py`) | **fait** | 448 clés, 219 lues, 0 dead, 0 missing |
| Fallback site d'IP (site principal KO) | **à faire** | vérifier que la station reste utilisable |
| Fallback DNS (resolver principal KO) | **à faire** | vérifier `DEGRADED`/fallback, pas `no-egress` |
| Tri-stack auto (WG KO → OV UDP → OV TCP) | **à faire** | vérifier les trois technologies |
| Bind OpenVPN TCP réel | **partiel** | test UK OV TCP sans egress en 45 s ; à re-tester avec timeout plus long et serveur OV dédié |
| Effet de chaque option GUI | **à faire** | contrat GUI + test par option |
| Migration DB legacy → registry.db | **à faire** | copie de production, 0 perte utile, rollback |
| Intégrité DB | **à faire** | `PRAGMA integrity_check=ok` |
| Rétention/télémétrie | **à faire** | `registry.db` < 250 Mo, purge quotidienne |
| Règle d’import domaine | **à faire** | test d’import, 0 cycle |

---

## 3. Causes racines

### R1 — Plafond de 20 candidats par chaîne et débit de 6 rebinds/min

- `config.yaml:738` : `max_candidates_per_chain: 20`.
- `scripts/fresh_chain_supervisor.py:1051` : `max_candidates = 20`.
- `scripts/fresh_chain_supervisor.py:1505` : `_start_chain(..., max_candidates, ...)`.
- `config.yaml:732` : `rebind_limit_per_min: 6`.
- `config.yaml:742-743` : `settle_s: 12`, `egress_wait_s: 45`.

Chaque bind raté coûte ~57–60 s. Une chaîne s'arrête après 20 tentatives, puis
`_restore_previous_host()` remet la station sur l'ancien serveur. À 20
candidats / ~20 min, les 9 787 serveurs prendraient **plusieurs jours** avec
une seule chaîne, et encore ~2 jours avec trois chaînes. Ce n'est pas « tester
tout le catalogue » : c'est un échantillon lent, qui recommence souvent.

### R2 — Cantonnement Albanie codé en dur

- `scripts/fresh_chain.py:42-52` : `PROVEN_HOSTS` = 9 serveurs `al*`.
- `scripts/fresh_chain.py:728-740` : ces hosts sont ajoutés avec la source
  `proven`, priorité `-2` dans `_base_key` (`:808-814`), donc **avant** tout
  le catalogue mondial.
- `scripts/fresh_chain_supervisor.py:696-699` : `_is_gold_ip()` = tout
  `87.120.102.*`.
- `scripts/fresh_chain_supervisor.py:756-787` : `_winner_protected()` protège
  une IP « gold » même si elle n'est plus la gagnante.
- `scripts/fresh_chain.py:355-385` : `_registry_hosts()` remonte les anciens
  200, majoritairement albanais.

Ce n'est pas un accident de sélection : c'est une **priorité structurelle**
qui contredit l'exigence « serveurs aux quatre coins du monde ».

### R3 — Le classement ne distingue pas les serveurs frais des serveurs morts

- `scripts/fresh_chain.py:633-858` : `build_candidates()` construit l'union
  API + embarqué.
- `scripts/fresh_chain.py:782-784` : la boucle `embedded` ajoute **tous** les
  hosts du manifeste, y compris ceux absents de l'API.
- `scripts/fresh_chain.py:786-806` : la boucle `public` ne peut plus ajouter
  ces hosts, car `seen` les a déjà marqués.
- `scripts/fresh_chain.py:808-848` : `_base_key()` teste
  `source.startswith("public")`, mais la source réelle est `embedded` pour
  tous les hosts déjà vus. Les serveurs frais et les serveurs morts se
  retrouvent donc dans le **même tier**, triés par `load` puis par hostname.

Résultat : les serveurs frais et les serveurs historiques sont testés au même
niveau, sans utiliser leur taux de 200 récent. Les 2 839 serveurs
Gluetun-only ne sont pas tous morts (408 observations 200 sur 7 jours), donc
il ne faut pas les exclure : il faut les **classer par qualité observée** et
les placer après les serveurs API à qualité égale. L'ordre alphabétique fait
par ailleurs passer des pays entiers (`bb`, `bd`, `be`, …) avant les serveurs
de qualité.

### R4 — Manifeste Gluetun non vérifié dans le conteneur cible

- `scripts/fresh_chain.py:287-333` : `sync_manifest_to_station()` copie le
  manifeste dans le volume, mais ne vérifie pas que le hostname est présent
  après copie.
- `logs/fresh_chain_6.log` : « manifest synced to station 6 » à 21:07.
- Le volume `opencode-proxy-one_gluetun6` contient un `nordvpn.json` **sans
  `be160.nordvpn.com`**.
- Le conteneur 6 a pourtant `SERVER_HOSTNAMES=be160.nordvpn.com` et boucle :
  `ERROR ... the hostname specified is not valid: be160.nordvpn.com`.

Le système choisit un host dans le cache fusionné, mais le conteneur peut
avoir un manifeste plus ancien. Le bind échoue **avant** le tunnel, et la
chaîne enregistre `no-egress`, ce qui noircit un serveur peut-être bon.

### R5 — Aucune rotation réelle côté manager

- `config.yaml:272` : `server_pick_mode: gluetun`.
- `config.yaml:125` : `country_rotation: false`.
- `vpn/manager.py:1503-1507` : le mode est lu.
- `vpn/manager.py:5761-5763` : en WireGuard, `hostnames = []` — le manager ne
  pin **jamais** de hostname via l'API de contrôle.
- `vpn/manager.py:2497-2517` : si `_pin_country_for_rotation()` renvoie
  `None` (car `country_rotation=false`), on retombe sur un redémarrage du
  conteneur avec le **même** `SERVER_HOSTNAMES`.
- `vpn/manager.py:2359` : `connect_next()` est la rotation ; elle ne choisit
  pas de serveur dans le catalogue.

En mode `gluetun` + `country_rotation: false`, un 429 déclenche donc un
redémarrage du même serveur (ou un choix aléatoire dans les pays configurés,
sans contrôle du hostname). Ce n'est pas une rotation catalogue.

### R6 — Une station pinnée ignore le 429

- `free/pool.py:3858` : `on_quota_exhausted()`.
- `free/pool.py:3982-3990` : si `station._pinned_hostname` est défini et que
  le superviseur est vivant, la fonction **retourne sans rotation**.
- `scripts/fresh_chain_supervisor.py:437-495` : `_needs_chain()` ne répare que
  si `station_usable` **et** `model_usable` sont faux.
- `scripts/fresh_chain_supervisor.py:824-874` : `_station_exhausted()` exige
  que **tous** les modèles de probe soient épuisés.
- `config.yaml:750-753` : trois modèles de probe.
- `config.yaml:729` : `ledger_bucket_mode: default` — le bucket « default »
  est partagé, donc `_ledger_ip_exhausted()` peut condamner l'IP entière pour
  un seul modèle.

Trou de couverture : un 429 sur le modèle réellement demandé peut laisser la
station `model_usable=true` (le probe d'un autre modèle a encore du quota), et
aucune chaîne ne démarre. La station reste « connectée mais inutilisable ».

### R7 — Ancienne logique pays à retirer

- `config.yaml:733-734` : `max_per_24: 2`, `max_per_country: 2`.
- `scripts/fresh_chain.py:841-848` : `_candidate_key()` **dégrade** les pays
  déjà représentés de +10 dans le tri ; il ne les exclut pas.
- `scripts/fresh_chain.py:1456-1477` : le `/24` est une vraie porte dure, mais
  le pays n'en est pas une.
- `PROVEN_HOSTS` (R2) passe avant cette dégradation.

La nouvelle exigence ne demande **aucun quota par pays ni par continent** :
toutes les stations peuvent utiliser n'importe quel pays, indépendamment.
Cette logique de pays doit donc être retirée du chemin de sélection (ou
réduite à une préférence GUI optionnelle). La seule contrainte dure devient
l'**unicité de l'IP d'egress** entre stations.

### R7bis — L'unicité d'IP existe en base mais n'est pas la porte unique

- `free/ip_registry/sqlite.py:85-92` : deux index uniques partiels protègent
  `state='assigned'` (une IP → une station, une station → une IP).
- Mais `scripts/fresh_chain.py:1456-1477` vérifie les doublons **au moment du
  bind** via `_active_egress_ips`, sans passer par `assign()`.
- `free/ip_registry/sqlite_registry.py:692-731` : `assign()` gère
  `IntegrityError`, mais le chemin `fresh_chain`/`catalog_claims` ne
  l'utilise pas.
- Le pool chaud et les stations `STANDBY`/`DRAINING` ne sont pas toujours
  comptés dans les IP occupées.

Conséquence : la garantie existe pour le chemin registre, mais pas pour le
chemin chaîne/claims ; deux stations peuvent se retrouver sur la même IP au
moment d'une promotion, ce qui partage le même quota.

### R8 — Le lease de claim (15 s) est plus court qu'un bind (~60 s)

- `scripts/fresh_chain.py:1324-1338` : `CatalogClaims(lease_s=15.0, ...)`.
- `free/catalog_claims.py:123-163` : un claim expiré devient `abandoned` et
  peut être repris par une autre station.
- `free/catalog_claims.py:195-210` : `lease_until = ts + self._lease_s`.

Un bind dure 57–160 s. Pendant ce temps, une autre chaîne peut reprendre le
même hostname, écrire son override et recréer le même conteneur. C'est une
course qui produit des binds croisés et des `no-egress` inexpliqués.

### R9 — Registre pollué par des lignes sans hostname

- `free/ip_registry/sqlite_registry.py:34` : `_ELIGIBLE_STATES`.
- `free/ip_registry/sqlite_registry.py:538-579` : `eligible()` ne filtre ni
  `hostname` ni `country`.
- `logs/ip_registry.db` : 3 286 lignes `hostname=''`, 3 549 `country=''`.

L'allocateur SQLite peut donc choisir une ligne non bindable et échouer en
`empty-hostname`. Ce chemin n'est pas le seul utilisé, mais il pollue les
statistiques et les files de candidats.

### R10 — Deux propriétaires de réparation et une station hors périmètre

- `config.yaml:292-315` : `station_repair.enabled: true` (watchdog manager).
- `config.yaml:729` : `allocator.mode: active` (superviseur).
- `scripts/fresh_chain_supervisor.py:392-403` : `_manager_repair_enabled()`
  donne 600 s de grâce au manager, mais les deux peuvent recréer la même
  station.
- `docker ps` montre `opencode-vpn-7` en marche alors que
  `config.yaml:286` dit `station_count: 6` et que le superviseur ne répare que
  1–6 (`_scope()`, `:986-1001`).

La 7e station consomme une session NordVPN et n'est pas réparée. Elle peut
aussi partager une IP ou un bucket avec une station du scope.

### R11 — Dérive de configuration

- `logs/fresh_chain_5.log` / `fresh_chain_2.log` :
  `[config] env divergence: VPN_TYPE_STATION5='openvpn' in .env but 'wireguard'
  in process env`.
- `config.yaml:754-766` : `allocator.transports` = wireguard pour les 6.
- `.env` : `VPN_TYPE_STATION5=openvpn`, `OPENVPN_PROTOCOL_STATION2=udp`,
  `VPN_TYPE_STATION6=wireguard` mais hostname `be160` invalide.
- `config.yaml:328` : `use_nordvpn_api: false` n'est lu que par le dashboard
  (`dashboard/api.py:997,1822,5318`), pas par le manager.

Les couches ne partagent pas la même vérité : compose, `.env`, manager,
superviseur et chaîne peuvent être en désaccord.

### R12 — L'observabilité mesure le tunnel, pas l'usage

- `logs/station_slo.jsonl` : `healthy` souvent 6, `routable` souvent 0.
- `free/pool.py:3858` et suivants : la décision 429 est locale.
- Le dashboard expose `model_usable` pour **un** modèle, pas par modèle
  demandé.

Sans une définition unique de « client-utilisable » (tunnel + egress vérifié +
200 avec contenu + pas de 429 actif), les opérateurs voient 6/6 et le client
voit 0/6.

### R13 — Le gel global des rotations empêche l'indépendance des stations

- `free/pool.py:4487-4495` : `_launch_rotation()` retourne sans rien faire si
  `_upstream_event_active()` est vrai.
- `free/pool.py:3941-3962` : un 429 transient peut alimenter le breaker
  corrélé ; un 429 quota ne l'alimente pas, mais le breaker reste global.
- `free/pool.py:4005-4042` : la cascade « compte épuisé » peut suspendre
  toutes les stations.

Conséquence : une station peut être bloquée par un événement qui concerne une
autre station. L'opérateur veut des rotations **indépendantes et parallèles**.
Le gel global doit être réservé aux pannes réellement fournisseur-wide, avec
une raison explicite, et ne doit jamais s'appliquer à un 429 quota par IP.

### R14 — Le démarrage ne garantit pas « on garde ce qui est utilisable »

- `vpn/manager.py:2090-2137` : le fast-path de boot adopte l'IP si le tunnel
  est sain, ce qui est une bonne base.
- `scripts/fresh_chain_supervisor.py:1206-1249` : le superviseur peut marquer
  une station `broken` sur un `model_usable` faux ou un probe manquant, même
  si elle sert encore.
- `free/pool.py:3175-3196` : `ensure_connected()` ne reconnecte que les
  stations down (`_station_is_up`), ce qui est correct, mais il n'existe pas
  de règle explicite « conserver une station utilisable au boot » côté
  superviseur.

Le comportement voulu doit être explicite : au boot, on **vérifie** chaque
station existante, on **garde** celles qui répondent 200 avec contenu, on ne
complète que les manquantes, et on n'en recrée aucune « pour faire propre ».

### R15 — Le score de performance n'existe pas encore

- `free/ip_registry/scoring.py` : score basé sur disponibilité, latence,
  fraîcheur ; pas de débit réel, pas de taux de 200 récent, pas de cycle de
  vie « reset → réutilisation ».
- `scripts/fresh_chain.py:583-631` : `_host_quality()` utilise `requests.db`
  pour le débit, mais ce n'est pas intégré au classement des IP.
- `logs/ip_registry.db` : les compteurs `tests/successes` sont pollués par
  l'import (voir 2.7) ; ils ne peuvent pas servir de base au score.

Il faut un score composite calculé sur des observations récentes, avec une
porte de disponibilité (non épuisée, non assignée) et un cycle de vie explicite
`reset_at → disponible → réutilisable`. Il faut distinguer le score d'une IP
connue (`score_ip`) et le score agrégé d'un hostname pas encore bindé
(`score_host`).

### R16 — Pas de fallback pour la détection d'IP et le DNS

- `free/ip_registry/adapters/egress.py:10-63` : quatre URLs, mais uniquement
  via SOCKS5h/SOCKS5. Pas de proxy HTTP, pas d'API de contrôle Gluetun, pas de
  `docker exec`.
- `free/ip_registry/adapters/compose_binder.py:150-166` : si l'egress n'est pas
  détecté, le bind est rejeté `no-egress` — même si le tunnel fonctionne et
  que seuls les sites d'IP sont bloqués.
- `docker inspect opencode-vpn-2` : DNS Gluetun en `doh` avec
  `cloudflare,google` seulement. Si les deux tombent, le DNS de la station
  tombe.
- `vpn/manager.py` : la configuration DNS n'a pas de chaîne de fallback
  DoH/DoT/plain.

Conséquence : une panne d'un site d'IP ou d'un resolver peut faire condamner
une IP ou une station qui fonctionne. C'est un point de défaillance unique.

### R17 — Le mode auto n'est pas un tri-stack par serveur

- `vpn/manager.py:6951-6997` : `_MIXED_SLOTS` affecte un slot fixe par station
  (`s1=WG`, `s2=OV TCP`, `s3=OV UDP`) ; ce n'est pas « essayer les trois pour
  chaque serveur ».
- `vpn/manager.py:6999-7157` : `_auto_flip_decision` flippe la stack globale
  de la station selon des seuils, avec cooldowns ; ce n'est pas une boucle
  WireGuard → OV UDP → OV TCP par candidat.
- `config.yaml:754-766` : `allocator.transports` = wireguard pour les 6
  stations.
- `.env` : `VPN_TYPE_STATION5=openvpn` alors que le process est en wireguard
  (divergence observée dans les logs).
- `scripts/fresh_chain.py` : `--transport` accepte une seule technologie par
  run, pas `auto`.

Conséquence : une station peut rester bloquée sur une technologie alors qu'une
autre fonctionnerait pour le même serveur, et le mode auto ne boucle pas sur
les trois.

### R18 — Les options GUI ne sont pas prouvées effectives

- `scripts/config_audit.py` : audit des clés `yaml_get` ; baseline actuelle
  **448 clés, 219 lues, 0 dead, 0 missing**.
- Mais l'audit ne couvre pas les options lues via `cfg.get`/`update_config` ni
  les contrôles purement dashboard.
- Exemple : `ip_rotation.use_nordvpn_api` n'est lu que par le dashboard
  (`dashboard/api.py:997,1822,5318`), pas par le manager.
- Aucun test ne vérifie qu'un toggle GUI change réellement le comportement
  runtime.

Conséquence : un opérateur peut croire avoir activé une option qui n'a aucun
effet. Il faut un **contrat GUI** option → consommateur → test d'effet.

### R19 — État dispersé et base non fiable

- `logs/ip_registry.db` (5,4 Mo) + `logs/catalog_claims.db` (880 Ko) +
  `logs/free_pool.json` (1 Mo) + `logs/fresh_chain_state*.json` +
  `logs/shared_rotation.json` + `logs/latency_engine.json`.
- `free/ip_registry/sqlite.py` : schéma v2 avec migration ad hoc, pas de
  table `schema_migrations` versionnée, pas de backup.
- `ip_state` : 3 286 lignes `hostname=''`, 3 549 `country=''` ; compteurs
  `tests/successes` non fiables.
- `requests.db` : ~3 Go, pas de rétention ; `debug.log` volumineux.

Conséquence : les décisions peuvent lire un état incohérent, la migration
n'est pas réversible, et la croissance disque finit par peser.

### R20 — Monolithes et couplage

- `opencode.py` ~1,4 Mo, `vpn/manager.py` ~584 Ko, `free/pool.py` ~276 Ko.
- Logique métier mêlée à Docker, HTTP, SQLite et fichiers JSON.
- Pas de règle d'import ni de test d'architecture.
- `fresh_chain*` orchestre par sous-processus/PID/JSON.

Conséquence : chaque correctif est risqué, les tests sont lents, et la dette
se reconstitue plus vite qu'elle ne se rembourse.

### R21 — Pas de rétention ni de sauvegarde

- `requests.db` ~3 Go, `debug.log` ~14 Mo+, `station_slo.jsonl` ~5 Mo.
- Pas de purge planifiée ni de `VACUUM INTO`.
- Pas de test de restauration.

Conséquence : disque, lenteur, sauvegarde inexistante en cas de corruption.

---

## 4. Ce qui n'est pas la cause

- **Ce n'est pas un manque de serveurs** : 6 948 serveurs API, 9 787 Gluetun,
  150 pays, 629 IP distinctes avec 200 sur 7 jours.
- **Ce n'est pas un quota global de compte** : les 429 observés sont des
  `FreeUsageLimitError` avec des `reset_at` différents par IP (00:10–00:58
  UTC), et certaines IP répondent 200 pendant que d'autres sont épuisées.
  La conclusion doit rester par IP, conformément à la règle projet.
- **Ce n'est pas l'absence du catalogue Gluetun** : il est présent et même
  sur-complet ; les hosts Gluetun-only ont produit 408 observations 200 sur
  7 jours. Il est mal exploité, pas inutile.
- **Ce n'est pas l'endpoint free qui est mort** : des 200 réels sont observés
  (404 observations 200 sur 24 h dans `ip_registry.db`), et un tunnel UK testé
  manuellement a produit une IP fraîche.
- **Ce n'est pas un problème de clé WireGuard** : la clé partagée fonctionne
  sur plusieurs stations simultanées (7 conteneurs observés avec la même clé).
  Une clé non enregistrée échoue, ce qui est normal ; ce n'est pas le cas de
  la clé de production.
- **Ce n'est pas un problème de « diversité pays »** : l'opérateur ne demande
  pas un quota par pays ; il demande que chaque station puisse utiliser
  n'importe quel pays, indépendamment, avec une IP unique.

---

## 5. Décisions actées et points restants

### Décisions actées le 2026-11-05

1. **Pays** : toutes les stations peuvent utiliser n'importe quel pays,
   indépendamment. Aucun quota par pays ni par continent. Seule contrainte
   dure : deux stations ne partagent jamais la même IP d'egress.
2. **Rotation** : chaque station fait sa rotation seule, en parallèle, quand
   elle est en 429 ou manuelle. Pas de rotation de flotte synchronisée.
3. **Démarrage** : on garde les stations déjà connectées et utilisables ; on
   ne complète que les stations manquantes.
4. **Propriétaire** : superviseur seul.
5. **Station 7** : supprimée (hors `station_count`).
6. **Clé WireGuard** : commune à toutes les stations.
7. **Rotation proactive** : option GUI, désactivée par défaut ; aucun seuil
   imposé si elle est désactivée.
8. **Score** : composite (taux de 200, latence, débit, stabilité), calculé sur
   des observations récentes.
9. **Réutilisation après reset** : une IP performante redevient disponible
   après `reset_at` ; elle est réutilisée **seulement quand une station a
   besoin d'une rotation**, pas de bascule proactive.
10. **Préflight** : accepté comme accélérateur (test rapide de l'endpoint
    avant de recréer le conteneur).
11. **Fallbacks** : maximum de fallback sur toutes les dépendances (IP, DNS,
    catalogue, manifeste, bind, probe, transport, superviseur).
12. **Transport auto** : WireGuard → OpenVPN UDP → OpenVPN TCP, boucle par
    serveur, chaque station indépendante.
13. **GUI** : chaque option doit avoir un effet réel, un consommateur runtime
    et un test ; aucune option inerte.
14. **Architecture** : refonte strangler, domaine pur, ports/adapters, use
    cases, workers.
15. **Base de données** : `registry.db` source unique, schéma normalisé,
    migrations, contraintes, provenance, rétention, backup.
16. **Dette** : registre `debt.md` suivi par phase, 0 P0 ouverte aux gates.

### Poids du score (tranchés)

- **40 % taux de 200 récent**, **25 % latence**, **20 % débit**,
  **10 % stabilité**, **5 % fraîcheur**. Ces poids sont ajustables dans le GUI
  et couverts par le contrat GUI. Justification : le taux de 200 prédit le
  plus directement la capacité à servir ; la latence et le débit mesurent la
  performance perçue ; la stabilité évite les IP qui oscillent ; la fraîcheur
  départage les observations anciennes.

