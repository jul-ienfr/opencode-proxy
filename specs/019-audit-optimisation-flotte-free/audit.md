# Audit SRE — flotte free `opencode-proxy-one`

**Date** : 2026-11-14, fenêtre de mesure 12:09–12:16 Paris (11:09–11:16Z)
**Périmètre** : six stations `opencode-vpn*`, fork Gluetun `016-control-plane`,
superviseur `scripts/fresh_chain_supervisor.py`, app `opencode.py` (port 4000),
registre `logs/registry.db`, `logs/requests.db`, dashboard.
**Mode** : lecture seule. Aucune rotation, aucun redémarrage, aucune écriture de
code. Les seules écritures de cet audit sont les livrables de
`specs/019-audit-optimisation-flotte-free/`.

---

## 0. Inventaire documentaire lu

| Document | Retenu |
|---|---|
| `AGENTS.md` (234 l.) | Règles non négociables (quota par IP, free, six stations, superviseur jamais gelé, seul `client_usable` compte) + journal de crise du jour. |
| `specs/017-fork-soft-migration/` | ADR-017-01 : « le fork répare la station, le superviseur répare la flotte » ; migration douce, rollback. |
| `specs/018-.../spec.md` | Cible 6/6 autonome, classes de panne, contraintes dures. |
| `specs/018-.../plan.md` | Un seul `POST /v1/vpn/rotate` par chaîne ; le fork exécute candidats + ladder. |
| `specs/018-.../tasks.md` (177 l.) | T201–T211 ; T211 (DNS transitoire) non terminé. |
| `specs/018-.../checklist.md` | T211 décochée. |
| `.specify/templates/*` | Format spec/plan/tasks/checklist réutilisé pour 019. |
| `ops/runbooks/control-plane.md` | Pré-requis `CONTROL_PLANE_ENABLED`, clé hors `config.yaml`, déploiement 6→1, rollback. |
| `README.md` | App + dashboard sur `:4000`, `OPENCODE_HOST=0.0.0.0` par défaut, `DASHBOARD_TOKEN` optionnel. |
| `docker-compose.yml` | `restart: unless-stopped` ; services stations 7–10 encore déclarés. |
| `config.yaml` | Quotas, rotation, AUTH, rétention, plan de contrôle. |
| `scripts/fresh_chain_supervisor.py` (2965 l.) | Propriétaire candidats/claims/warm pool/réparations. |
| `scripts/fresh_chain.py` (3054 l.) | Chaîne de rafraîchissement d'une station. |
| `free/pool/__init__.py`, `free/qualification/store.py`, `free/warp_rotation.py` | Sélection, état station, rotation WARP. |
| `gluetun/` (branche `016-control-plane`) | Contrôle, transports, DNS, egress, healthcheck, jobs. |
| `tests/` (468 entrées) | Tests Python ; tests Go dans le fork. |
| `.github/workflows/ci.yml` | CI présente. |
| `logs/*` | Supervisor 4,0 Mo, debug 3,2 Mo, `registry.db` 88 Mo, `requests.db` 1,8 Go. |
| `git log` des deux dépôts | 47 commits en 12 h (principal), 8 commits (fork, dernier 09:28Z). |

---

## 1. Résumé exécutif

**Mesuré à 11:12:12Z : 0/6 stations `client_usable`, 0/6 `model_usable`, alors
que les six tunnels sont `connected`, `healthy`, avec egress et IP publiques
valides.** Cinq stations sont classées `daily-bucket-exhausted` alors que le
compteur local affiche 1 à 55 requêtes sur 200. La 6 est `station-unusable`
avec 136/200 utilisées. Le `warm_pool` contient **15 IP vivantes** au même
instant, inutilisées : les six stations sont en `DRAINING` sur quota.

Le problème n'est donc pas « plus d'IP » ni « le compte est épuisé ». C'est un
**défaut de modèle de quota et de propriété de l'état** : le ledger local
(`quota_used/200`) ne décrit pas le bucket amont partagé, et trois composants
publient trois vérités différentes sur « cette station sert-elle ? » —
`/api/vpn-status` dit 0/6, `registry.db/station_state` dit station 3 `USABLE`
à 11:10:21Z, le fork détient un troisième état.

Trois risques P0 en découlent : (1) la flotte ne peut structurellement pas
prouver 6/6 sur un même cycle ; (2) l'app `:4000` n'a **aucun** superviseur de
processus (PID 7264 démarré à 11:08:22Z, parent disparu, aucune tâche
planifiée) ; (3) le chemin de requête déclenche et journalise une rotation
(`free/pool/__init__.py:5134`) à **chaque requête** — **98 003** lignes
`[warp-rotate]` dans `logs/debug.log.1` (fichier courant : 0, rotation à 00:13).

Le code avance vite (47 commits/12 h, hétérogénéité d'images résolue : les six
stations sont sur le digest `075f0915`), mais l'effort porte surtout sur des
symptômes de boucle (AUTH, chaînes, pins) plutôt que sur les deux causes
racines : **le quota par IP mal modélisé** et **l'absence d'autorité unique de
l'état station**.

---

## 2. État mesuré (faits, pas hypothèses)

### 2.1 API `GET /api/vpn-status` (`refreshed_at=2026-11-14T11:12:12Z`)

| St. | IP | VPN | `client_usable` | `model_usable` | raison | `used/200` | `ip_state` | blocage | pin |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 186.247.66.25 | connected | false | false | daily-bucket-exhausted | 2 | bad | 456 s | al46 |
| 2 | 186.247.44.66 | connected | false | false | daily-bucket-exhausted | 55 | active | 0 | — |
| 3 | 84.247.40.19 | connected | false | false | daily-bucket-exhausted | 4 | bad | 869 s | is91 |
| 4 | 195.86.23.7 | connected | false | false | daily-bucket-exhausted | 1 | bad | 603 s | — |
| 5 | 187.15.85.142 | connected | false | false | daily-bucket-exhausted | 3 | active | 0 | tn4 |
| 6 | 187.15.108.63 | connected | false | false | station-unusable | 136 | active | 267 s | us12811 |

Totaux API (`refreshed_at=11:19:49Z`) : `usable_stations=10`,
`healthy=6`, `healthy_routable=0`, `routable=0`,
`active_stations_with_quota=13`, `unique_usable_ips=10`,
`station_count=6`, `configured_stations=16`, `warm_spare_ready=false`,
`rotation_paused=false`, `active_station=-25`. **Le payload se contredit
lui-même** : 10 stations « utilisables » et 13 « avec quota » alors que
`healthy_routable=0` et que les six stations ont `client_usable=false` ;
16 stations configurées pour une flotte de 6.

### 2.2 `registry.db/station_state` (lu à 11:10:21Z, `updated_at=1794654621.84`)

| Station | state | `client_usable` | reason |
|---|---|---|---|
| 1 | DRAINING | 0 | quota |
| 2 | DRAINING | 0 | quota |
| 3 | **USABLE** | **1** | healthy |
| 4 | DRAINING | 0 | quota |
| 5 | DRAINING | 0 | quota |
| 6 | DRAINING | 0 | quota |

**Contradiction directe** avec §2.1 sur la station 3 et sur le statut
DRAINING/USABLE des autres.

### 2.3 Pool et approvisionnement

| Mesure | Valeur | Source |
|---|---|---|
| `warm_pool` lignes totales | 249 | `select count(*) from warm_pool` |
| `warm_pool` vivantes (`expires_at>now`) | **15** | même requête filtrée |
| IP distinctes vivantes | 15 | `count(distinct ip)` |
| Lignes mortes jamais purgées | 234 | 249 − 15 |
| `quota_buckets` total | 6 756 | table |
| dont `last_status=429` | 6 404 (**94,8 %**) | group by |
| dont `last_status=200` | 183 (2,7 %) | group by |
| dont `last_status=0` | 169 | group by |
| `reset_at` futur | 13 | `reset_at>now` |
| `claims` total | 9 788 | table |
| `no_egress` | 7 651 (**78,2 %**) | group by |
| `country_backoff` | 1 667 | group by |
| `duplicate` | 370 | group by |
| `abandoned` | 73 | group by |
| `ok` | 17 (0,17 %) | group by |
| `429` | 10 | group by |
| `qualification_attempts` | 10 395 | table |
| dont `bind_error` | 6 148 (59,1 %) | group by |
| dont `429` | 2 341 (22,5 %) | group by |
| dont `200` | 584 (**5,6 %**) | group by |
| dont `no_egress` | 593 | group by |
| dont `skipped` | 636 | group by |
| Incidents | 17 516 au total, **1 ouvert** | table |

Lecture : le rendement réel de la chaîne d'approvisionnement est de **5,6 % de
200** et **78 % des claims meurent en `no_egress`**. Ce n'est pas un problème
de rareté d'IP : 15 IP vivantes dorment pendant que six stations attendent.

### 2.4 Conteneurs et images

- 6 stations `opencode-vpn`, `opencode-vpn-2..6` : toutes
  `gluetun-016:local`, digest **`075f0915` uniforme**, `restart=unless-stopped`,
  `healthy`.
- 12 conteneurs `warp-1..12` (`caomingjun/warp`, image 1,03 Go) + 1 conteneur
  `oc-public-egress-*` + `wgsweep` + `udpsweep` + `ofu-engine` + stack `v2-*`
  d'un autre projet. **La flotte VPN est minoritaire dans l'hôte.**
- `logs/compose-control-plane-{1..6}.yml` : plan de contrôle actif partout
  (`control_plane.mode=active`, `transport_owner=fork`).

### 2.5 Processus app

- `GET http://127.0.0.1:4000/health` → **200**.
- PID **7264** (`pythonw.exe opencode.py --gui`), **démarré 11:08:22Z**
  (3 min avant l'audit), parent `4360` **inexistant**.
- `logs/app-stdout.log` = **0 octet** ; `logs/app-stderr.log` = 6 Ko de `INFO`.
- Aucune tâche planifiée `opencode|proxy` (`schtasks /query`).
- Aucun conteneur `opencode-proxy` dans `docker ps` : l'app tourne hors Compose,
  donc **hors de `restart: unless-stopped`**.

### 2.6 Logs

- `logs/fresh_chain_supervisor.log` : 5 démarrages entre 09:22:21Z et
  10:45:34Z ; 247 lignes `code reload`, **la dernière à 08:32:05Z** (boucle
  éteinte) ; 880 lignes contenant `AUTH`.
- `logs/debug.log.1` (66,6 Mo, rotation à 00:13) : **98 003** lignes
  `[warp-rotate]` ; `logs/debug.log` courant : 0 ligne (le spam a cessé depuis
  la rotation, pas depuis un correctif).
- `logs/auth_global_pause.json` : `{"until": 1794649365}` → 09:42:45Z (expiré).
- `logs/vpn_state{1..6}.json` : 23–32 Ko, modifiés entre 08:48Z et 10:47Z ;
  **états par station, distincts de `registry.db`**.
- `.env` : `SERVER_HOSTNAMES_STATION1..10` — les entrées 7–10 sont des
  vestiges de la 7ᵉ station supprimée.

### 2.7 Données

| Fichier | Taille | Constat |
|---|---|---|
| `logs/registry.db` | 91 484 160 o (87,2 Mio) | WAL actif, `integrity_check=ok`, schéma v3 |
| `logs/registry.db-wal` | 14 218 152 o | non checkpointé au moment de l'audit |
| `logs/requests.db` | 1 923 633 152 o (1,79 Gio) | 63 992 requêtes, corps moyen 56,6 Ko |
| `logs/requests.db-wal` | 4,3 Mo | — |
| `logs/requests.bak-20261105-201418.db` | 1,3 Go | sauvegarde jamais purgée |

- 33 332 requêtes de plus de 7 jours encore présentes ; 33 001 corps déjà
  nullifiés ; **`freelist_count=0`** → la taille est faite de données récentes,
  pas de pages libres.
- `registry.db` : 249 lignes `warm_pool` dont 234 mortes, 17 516 incidents,
  6 756 buckets, aucune politique de purge visible.
- `rotation_jobs` : **2 352 lignes, dont 1 042 `started` (44 %)** sans état
  terminal ; **1 041 ont plus d'1 h** et **1 022 plus de 24 h** (plus ancienne
  `2026-11-07 16:50:28Z`) ; sur la dernière heure, 9 `ok`, 1 `failed`,
  1 `started` — la fuite est toujours active. Répartition globale :
  800 `ok`, 510 `failed` (`duplicate_block` 249, `quota_exhausted` 233,
  `same_block` 23, `health_timeout` 5), 1 042 `started`.

### 2.8 Re-mesure de clôture (11:22Z, avant rédaction finale)

| Mesure | Valeur | Source |
|---|---|---|
| `client_usable` | **0/6** | `logs/query_status.py`, 11:22:19Z |
| Dernière alerte superviseur | `client_usable=0/6 (<2) — warm_pool=15 — repair active` à 11:21:43Z | `fresh_chain_supervisor.log` |
| `station_state` | 6/6 `DRAINING`, `reason=quota`, `updated_at=11:23:06Z` | `registry.db` |
| `warm_pool` vivant | **15** (249 lignes, 234 mortes) | `registry.db` |
| `quota_buckets` | 6 758 dont **6 403 `last_status=429`** (94,7 %) et 185 `200` (2,7 %) | `registry.db` |
| Stations | 6/6 `connected`, `healthy`, egress OK ; 6/6 `daily-bucket-exhausted` | API `:4000` |
| Images | 6/6 même digest `075f0915`, `restart=unless-stopped` | `docker inspect` |
| App | PID **7264** écoute `0.0.0.0:4000`, parent toujours absent | `netstat` |
| `registry.db` / `requests.db` | 91,6 Mo / **1,94 Go** | filesystem |

**Conclusion de la re-mesure** : les chiffres du §2 tiennent (0/6, 15 IP
vivantes, quota amont 429 à ~95 %). La seule correction est le volume du spam
`[warp-rotate]` : **98 003 lignes**, pas 4 121 — le fichier `debug.log` courant
a été roté à 00:13, ce qui explique l'écart.

### 2.9 Contrôle de reprise (12:15Z, une heure plus tard)

| Mesure | 11:22Z | 12:15Z | Lecture |
|---|---|---|---|
| `client_usable` | 0/6 | **0/6** | Aucun retour ; 0/6 depuis ~12:30 heure de Paris (dernier 1/6 à 12:06) |
| Raison des 6 stations | 5 quota + 1 `station-unusable` | **6/6 `daily-bucket-exhausted`** | Le défaut de quota est désormais la seule cause |
| `quota_used` (st. 1→6) | 2 / 55 / 4 / 1 / 3 / 136 | **4 / 56 / 6 / 5 / 5 / 137** | Les compteurs montent, les stations restent bloquées → confirme P0-01 |
| `warm_pool` vivant | 15 | **15** | Pool inchangé, toujours non consommé (P0-04) |
| `rotation_jobs` orphelins > 5 min | 1 041 (> 1 h) | **1 042** | La fuite continue (P1-10) |
| Incidents ouverts | 1 | **1** | Stable |
| API : `usable_stations` / `active_stations_with_quota` / `routable` | 10 / 13 / 0 | **11 / 12 / 0** | Le payload se contredit toujours (P0-02) |
| Dernier commit principal | 11:45 | **11:45** (`5d409b9`) | Aucun progrès code depuis 30 min |
| Dernier commit fork | 09:28 | **09:28** | Idem |

**Conclusion du contrôle** : l'état est stationnaire et le diagnostic ne change
pas — le blocage est le modèle de quota amont (P0-01), pas le tunnel ni le
warm pool. Les deux seules évolutions sont l'aggravation mesurable de P1-10
(+1 job orphelin) et la disparition de la 6ᵉ cause distincte, ce qui simplifie
la lecture sans changer la priorité. `config.yaml` a de plus été modifié par la
rotation WARP (egress IP 1/2/3) entre 12:08 et 12:20 : c'est un effet de
l'automatisation, pas une action de l'audit.

---

## 3. Cartographie architecture et propriété

```
                    ┌──────────────────────────────────────────────┐
  client ──HTTP──▶  │ app opencode.py :4000                        │
                    │  free/pool : sélection, quota_leases, probes │
                    │  free/warp_rotation : repli WARP             │
                    │  dashboard : /api/vpn-status                 │
                    └───────┬───────────────────────┬──────────────┘
                            │ docker compose / pin  │ SQLite
                            ▼                       ▼
                    ┌───────────────┐        ┌──────────────┐
                    │ superviseur   │        │ registry.db  │
                    │ fresh_chain_  │◀──────▶│ IP, quota,   │
                    │ supervisor.py │        │ claims, SLO  │
                    │ chains, AUTH  │        │ station_state│
                    └───────┬───────┘        └──────────────┘
                            │ POST /v1/vpn/rotate
                            ▼
                    ┌───────────────────────────────────────────┐
                    │ fork Gluetun 016 (:8001..8006)            │
                    │ control plane, transports, DNS, egress,   │
                    │ jobs rotate, état /v1/vpn/state           │
                    └───────────────────────────────────────────┘
```

### Propriété réelle, composant par composant

| Objet | Propriétaire de droit (docs) | Propriétaires de fait (mesuré) |
|---|---|---|
| IP courante | fork (`transport_owner=fork`) | fork + `.env SERVER_HOSTNAMES_STATIONn` + `vpn_state{n}.json` + `station_state.current_ip` + API |
| Pin hostname | superviseur (`_persist_pin`, `_clear_stale_pins`) | `.env` (5 pins), app (`pinned_hostname`), `vpn_state*.json` |
| Lease/quota | `registry.db` (ADR) | `registry.db` **et** `quota_leases` en mémoire app **et** `quota_buckets` |
| Décision de rotation | superviseur (ADR-017-01) | superviseur **et** `free/pool` (`rotate_exhausted_managed` à chaque requête) **et** fork (jobs) |
| `client_usable` | app (calcul) | app (API) **et** `station_state` (qualification) **et** incidents |
| Warm pool | superviseur (`_refresh_warm_pool`) | superviseur (écrit) mais **personne ne le consomme** |
| Réparation conteneur | superviseur | superviseur + app (`station_repair`, `recreate`) |

**Conclusion** : la règle « une seule autorité » de l'ADR-017-01 n'est pas
appliquée. Cinq surfaces écrivent l'IP/pin/état. Aucun test d'intégration ne
compare ces surfaces.

## 4. Constats

Sévérité : **P0** = empêche le 6/6 ou peut perdre la flotte ; **P1** = dégrade
la fiabilité/qualité de façon mesurable ; **P2** = dette ou risque de dérive.
Chaque constat est séparé en fait mesuré / cause / recommandation.

### 4.1 Tableau de synthèse

| ID | Sév. | Domaine | Preuve | Cause racine | Impact | Recommandation | Effort | Risque | Critère d'acceptation |
|---|---|---|---|---|---|---|---|---|---|
| P0-01 | P0 | Quota | API 11:12:12Z : 0/6 `client_usable`, stations 1/4/5 à `used=2/1/3` sur 200 et pourtant `daily-bucket-exhausted` ; 6 404/6 756 buckets en 429 (94,8 %) | Le ledger local `quota_per_ip=200` (`config.yaml:276`) est traité comme le quota réel, alors que le bucket amont est **partagé par IP entre utilisateurs** ; un seul 429 condamne l'IP et `Retry-After` est pris pour un reset fiable | Aucune station ne sert durablement ; la flotte oscille 0–3/6 ; 6/6 impossible par construction | Séparer `ours_used` (compteur) et `upstream_verdict` (200/429 observé) ; TTL court sur 429 ; ne jamais déduire « bucket plein » de `used` ; plafonner `reset_at` à `ledger_max_block_s` (déjà 1200 s, `config.yaml:199`) et le rendre visible | M | Moyen : changer le sens de `ip_state` peut faire re-prober des IP encore chaudes | 6/6 sur un même cycle de probe (120 s) pendant 30 min, sans intervention |
| P0-02 | P0 | État | API 0/6 à 11:12:12Z vs `station_state` station 3 `USABLE/client_usable=1` à 11:10:21Z ; le même payload API affiche `usable_stations=10`, `active_stations_with_quota=13`, `routable=0`, `configured_stations=16` | `station_state` est écrit par `free/qualification/store.py:702` (qualification) et l'API par `free/pool` ; plusieurs compteurs de l'API ne sont pas dérivés du même ensemble que `client_usable` | Le superviseur et l'app peuvent réparer/rotationner sur des prémisses opposées ; diagnostic impossible ; SLO faux ; 16 stations configurées pour 6 | Une seule autorité : `registry.db` **read-model** alimenté par les probes de l'app, exposé tel quel par l'API ; supprimer l'écriture qualification ou la marquer `shadow` ; dériver `usable_stations` de `client_usable` ; un test compare les deux à chaque cycle | M | Faible si la table devient un cache | Zéro divergence API/`station_state` sur 24 h ; `usable_stations = count(client_usable)` ; test d'intégration qui échoue en cas d'écart |
| P0-03 | P0 | Fiabilité app | PID 7264 démarré 11:08:22Z, parent 4360 disparu ; `app-stdout.log` 0 o ; aucune tâche planifiée ; pas de conteneur `opencode-proxy` | L'app est lancée à la main (`pythonw opencode.py --gui`) ; `restart: unless-stopped` du Compose ne s'applique pas ; stderr redirigé mais pas de trace de crash | Rejoue le crash de 10:51Z : tout le plan de contrôle tombe, aucune relance, aucune capture | Lanceur supervisé (NSSM/Tâche planifiée/watchdog) avec `restart`, capture stdout+stderr horodatée, heartbeat `/health` et alerte > 30 s | S | Faible | `taskkill /F` sur le PID → service de retour < 30 s, traceback capturé, alerte émise |
| P0-04 | P0 | Approvisionnement | `claims` : 7 651 `no_egress` (78,2 %), 17 `ok` ; `qualification_attempts` : 6 148 `bind_error`, 584 `200` (5,6 %) ; `warm_pool` 15 IP vivantes **non consommées** | La chaîne teste des candidats en bind multi-étapes sans préflight ; le warm pool est réalimenté mais jamais lu par l'affectation ; les resets sont re-testés trop tôt | Rendement 5,6 %, gaspillage d'IP saines, 6/6 hors d'atteinte même quand le quota existe | Consommer le warm pool **avant** toute qualification ; un candidat = un préflight + un bind ; interdire tout re-test avant `reset_at` ; métrique `warm_pool_used/total` | M | Moyen : warm pool peut contenir des preuves périmées → TTL 6 h et re-probe à la consommation | Taux de 200 > 30 % sur 2 h ; `warm_pool` consommé > 80 % ; zéro re-test avant `reset_at` |
| P0-05 | P0 | Performance / logs | `free/pool/__init__.py:5134` journalise `[warp-rotate] on_request` à **chaque requête**, puis appelle `rotate_exhausted_managed()` (`:5136`) ; **98 003** lignes dans `debug.log.1` (0 dans le fichier courant, rotation à 00:13) | Log INFO et déclencheur de rotation placés dans le chemin chaud, sans garde « y a-t-il une IP épuisée ? » | Bruit qui masque les incidents, CPU/I/O, risque de rotation en rafale sous charge | Déplacer le log en DEBUG et ne l'émettre qu'au changement d'état ; ne déclencher la rotation que si l'ensemble épuisé est non vide ; test de non-régression | S | Faible | ≤ 1 ligne/min au repos ; aucune rotation déclenchée par une requête quand `exhausted=∅` |
| P1-01 | P1 | Propriété | 4 pins non vides dans `.env` (`SERVER_HOSTNAMES_STATION1,3,5,6`), `station_state.current_ip`, `vpn_state{1..6}.json` (08:48–10:47Z), `quota_leases` mémoire, fork | Cinq écrivains pour IP/pin ; `_persist_pin` (superviseur) et le binder écrivent `.env` sans verrou commun | Pins périmés (déjà arrivé : demande de réparation station 3 sur une IP qui n'était plus la sienne, AGENTS.md:231) ; bascule sur la mauvaise IP | Un seul store (registre) + génération des pins depuis lui ; `.env` en lecture seule ; `_clear_stale_pins` testé | M | Moyen | Un seul fichier contient l'IP/pin ; zéro pin divergent sur 24 h |
| P1-02 | P1 | AUTH | 880 lignes `AUTH` dans le log superviseur ; 5 redémarrages 09:22–10:45 ; `auth_global_pause.json` expiré à 09:42:45Z | Pause globale persistée mais ré-armée par plusieurs chemins ; `_auth_gate` (`fresh_chain_supervisor.py:744`) teste `last_attempt` global sans propriétaire unique | AUTH storms, candidats brûlés, stations recréées en cascade | Un seul écrivain de la pause (superviseur), compteur d'essais par station **et** global dans le registre ; test de concurrence | M | Moyen | ≤ 1 tentative AUTH/10 min toutes stations ; 0 AUTH_FAILED en rafale sur 1 h |
| P1-03 | P1 | Incidents | 17 516 incidents ; 7 `tunnel_down` simultanés à 11:08:13Z, refermés à 11:08:49Z ; 1 ouvert | Chaque transition de conteneur crée un incident ; pas de debounce ni de regroupement par cause | Bruit d'alerte, SLO faussé, post-mortem impossible | Regrouper par `(cause, fenêtre 60 s)` ; un incident flotte pour 6 stations ; severité dérivée du nombre de stations | S | Faible | ≤ 5 incidents/heure hors panne réelle ; un incident par cause fleet-wide |
| P1-04 | P1 | Données registre | `registry.db` 87,2 Mio + WAL 14,2 Mio ; 234/249 lignes `warm_pool` mortes ; 17 516 incidents ; `integrity_check=ok` | Aucune purge/TTL sur `warm_pool`, `incidents`, `claims` ; WAL non checkpointé à l'audit | Croissance monotone, requêtes lentes, sauvegardes lourdes | Purge `warm_pool` (expirés), `incidents` (> 7 j), `claims` (> 7 j) ; checkpoint WAL sur seuil ; test de rétention | S | Faible | `warm_pool` = vivants uniquement ; DB < 50 Mio ; WAL < 4 Mio |
| P1-05 | P1 | Données requêtes | `requests.db` 1,79 Gio ; 63 992 requêtes ; corps moyen 56,6 Ko ; 33 332 lignes > 7 j ; `freelist_count=0` ; `.bak` 1,3 Go | `body_retention_days=7` (`config.yaml:879`) nullifie les corps mais ne supprime pas les lignes ni ne compacte ; aucune purge hebdo visible (`weekly_purge_days=90`) | Disque saturé, backups lents, requêtes analytiques coûteuses | Purge des lignes > 7 j, `VACUUM`/`auto_vacuum`, plafond de taille, suppression du `.bak` après vérification | M | Moyen (VACUUM lock) | DB < 500 Mio ; zéro ligne > 7 j ; purge planifiée testée |
| P1-06 | P1 | Sécurité | `config.yaml:45` `require_token:false`, `:46` `client_auth.mode:none`, `:48` clé vide ; app écoute `0.0.0.0:4000`, joignable sur `100.90.85.128` | Le dashboard et l'API proxy n'ont pas d'auth ; le token est optionnel et non armé | Toute machine du tailnet/LAN peut piloter la flotte (`/api/proxy/restart`, stations) | Activer `DASHBOARD_TOKEN` et `client_auth` sur l'interface non-loopback ; garder `127.0.0.1` par défaut ; test 401 | S | Faible | 401 sans token sur toutes les routes de contrôle ; aucun port public |
| P1-07 | P1 | Observabilité | `free_status` NULL sur 1 499/1 500 requêtes des 6 h ; `requests.timestamp` TEXT (comparaisons numériques cassées) ; pas de `/metrics` | Le champ n'est pas rempli au moment de l'écriture ; pas d'export Prometheus ; SLO calculé à la volée | Impossible de mesurer taux de 200, latence, resets ; les dashboards mentent | Instrumenter `free_status`, `station`, `ttfb_ms` à l'écriture ; exposer `/metrics` ; un job SLO par minute | M | Moyen | 100 % des requêtes ont `free_status` ; SLO calculable sur 24 h sans log |
| P1-08 | P1 | Tests | `tests/` : seul `_auth_gate` est testé (`test_station_allocator_supervisor.py:374`) ; `_best_resume_station`, `_refresh_warm_pool`, `_station_settling`, `_remove_extra_stations` sans test dédié ; T211 ouverte | Les correctifs récents ont été ajoutés sans test unitaire ; la CI ne couvre pas les nouvelles branches | Régressions silencieuses, revert nécessaire (5d409b9) | Un test par fonction + test d'intégration « 6/6 même cycle » ; lint/CI bloquants | M | Faible | Couverture des 5 fonctions ; CI verte ; T211 cochée |
| P1-09 | P1 | Performance rotation | `rotate_timeout_s=300` (`config.yaml:1227`) > `rotation_max_duration=240` ; `rotation_concurrency=1`, `rotation_stagger=10`, `station_min_rotation_interval_s=600` (`:670`) | Le timeout appelant est plus long que le budget du fork ; les rotations sont sérialisées sans estimation de file | Attente jusqu'à 4 min/station, 4 stations ≈ 12 min ; timeouts confondus avec échecs | Aligner `rotate_timeout_s` sur `rotation_max_duration + marge` ; exposer la position de file ; rotation asynchrone avec `job_id` | S | Faible | Timeout = 0 sur 100 rotations ; latence p95 < 240 s |
| P1-10 | P1 | Données / rotation | `rotation_jobs` : 1 041/2 352 lignes (44 %) en `started` sans fin, dont 1 022 > 24 h (plus ancienne 2026-11-07 16:50Z) ; 1 job/heure fuit encore | Aucun chemin n'écrit l'état terminal quand le job expire, crashe ou est abandonné ; aucune réconciliation au démarrage | `rotate_committed/rotate_total` est incalculable, la table croît, le dashboard peut afficher des rotations « en cours » fantômes | Réconciliation : tout `started` plus vieux que `rotation_max_duration + marge` → `abandoned` avec raison ; état terminal garanti par `try/finally` ; test de fuite | S | Faible | 0 `started` > 5 min ; taux committed calculable sur 24 h |
| P2-01 | P2 | Hygiène dépôt | `git status` : 9 fichiers non suivis (`check_now.py`, `probe_*.py`, `query_*.py`, `_arb.py`), `AGENTS.md` et `config.yaml` modifiés | Scripts de diagnostic laissés à la racine ; pas de `.gitignore` pour eux | Bruit, risque de commit accidentel, revue difficile | Déplacer dans `scripts/diagnostics/` ou supprimer ; `.gitignore` ciblé | S | Faible | `git status` propre hors livrables 019 |
| P2-02 | P2 | Exploitation | `docker-compose.yml:478-544` déclare `opencode-vpn-7..10` ; `.env` `SERVER_HOSTNAMES_STATION7..10` ; `station_count=6` | La suppression de la 7ᵉ station n'a pas nettoyé Compose ni `.env` | Un `docker compose up` avec profil large peut recréer une 7ᵉ station | Supprimer les services 7–10 et les variables ; test `docker compose config` | S | Faible | `docker compose config` ne liste que 6 stations |
| P2-03 | P2 | Ressources | 12 conteneurs `warp-*` (image 1,03 Go) + `wgsweep` + `udpsweep` + stack `v2-*` + `ofu-engine` | Le repli WARP a été dimensionné large et n'est jamais réduit | Contention CPU/RAM/réseau sur l'hôte qui exécute les 6 tunnels | Borner WARP à 2–3, arrêter les sweep inutilisés, mesurer avant/après | S | Moyen (repli) | CPU hôte < 70 % ; 2 WARP suffisent au fallback |
| P2-04 | P2 | Qualité données | `warm_pool.transport=''`, `model=''`, `score_ip=0.0` sur les 3 dernières lignes ; `verified_at` jusqu'à 6 h | Colonnes jamais remplies par `_refresh_warm_pool` | Impossible de prioriser par transport/modèle/score | Remplir à l'insertion ou supprimer les colonnes | S | Faible | 100 % des lignes vivantes ont transport+model+score |
| P2-05 | P2 | Config | `quota_per_ip=200` (`:276`) vs `qualification.quota_estimate=500` (`:1172`) vs `warm_pool_min=3` (`:1252`) vs cible 6 | Trois valeurs pour la même grandeur, aucune dérivée | Seuils incohérents, warm pool sous-dimensionné | Une seule source (`quota_per_ip`) et `warm_pool_min=6` | S | Faible | Une seule valeur dans `config.yaml` ; test de cohérence |
| P2-06 | P2 | État périmé | `vpn_state{1..6}.json` modifiés 08:48–10:47Z, jamais supprimés ; `AGENTS.md` cite une demande de réparation périmée | Fichiers de debug laissés comme état | Confusion lors d'un diagnostic, risque de rejeu | Déplacer en `logs/debug/` avec date, ou supprimer après 24 h | S | Faible | Aucun `vpn_state*.json` à la racine de `logs/` |

## 5. Analyse par domaine

### 5.1 Quota free par IP — le modèle est faux, pas la ressource

Le code traite `quota_per_ip=200` comme un compteur de consommation
(`quota_used`, `remaining`) **et** comme un prédicat d'éligibilité
(`ip_state=bad`, `daily-bucket-exhausted`). Or :

- une IP à `used=1/200` est déclarée épuisée (station 4) ;
- une IP à `used=136/200` est déclarée `station-unusable` (station 6) ;
- 15 IP du warm pool sont vivantes au même moment ;
- 94,8 % des buckets observés sont en 429, mais 183 ont vu un 200.

Ces quatre faits sont incompatibles avec « quota de 200 par IP pour notre
usage ». Ils sont compatibles avec un **bucket amont partagé**, où notre
compteur ne mesure que nos propres requêtes et où le 429 dépend des autres
utilisateurs de l'IP. La conséquence opérationnelle est double :

1. il ne faut pas conclure « bucket plein » depuis `used` ;
2. il ne faut pas prendre `Retry-After` pour un `reset_at` fiable (déjà
   mesuré : `RA=20 h 46`, puis 200 sur la même IP 18 min après, AGENTS.md:234).

**Stratégie free par IP recommandée** (sans payant, sans second compte, sans
« attendre » comme réponse unique) :

- **Verdict par IP, pas par station** : `ip_state ∈ {unknown, hot, cooling,
  dead}` avec `hot` = 200 vu < 10 min, `cooling` = 429 vu avec TTL de 10 min
  (plafonné par `ledger_max_block_s`), `dead` = 3 échecs de transport.
- **Deux compteurs distincts** : `ours_used` (ce que nous avons envoyé) et
  `upstream_verdict` (dernier statut observé). Seul le second décide.
- **Consommer avant de qualifier** : le warm pool est la première source ; la
  qualification ne sert qu'à le réalimenter.
- **Reset réel** : `reset_at = min(Retry-After, last_429 + 1200 s)` et
  re-probe **une seule fois** à l'échéance, jamais avant.
- **Partage** : une IP ne doit pas servir deux stations simultanément
  (`_ip_shared_with_other`, `fresh_chain_supervisor.py:1630`) ; le registre
  doit le garantir par un lease, pas par une convention.

### 5.2 Fiabilité — l'app est le maillon faible

Le crash de 10:51Z n'est pas prouvé (aucun traceback dans
`app-stderr.log`), mais trois signaux convergent : **98 003** appels
`rotate_exhausted_managed()` depuis le chemin de requête, un log INFO à chaque
appel, et un redémarrage manuel à 11:08:22Z. Même sans preuve de causalité, la
**capacité de reprise** est nulle : l'app n'est ni conteneurisée ni supervisée.

Le superviseur, lui, est robuste par construction (verrou, heartbeat, lock par
station) mais il a redémarré 5 fois en 83 minutes et écrit 247 lignes de
`code reload`. La boucle est éteinte depuis 08:32Z, ce qui est un progrès réel ;
le risque restant est le **redémarrage manuel** qui remet à zéro les compteurs
en mémoire (pins, pauses, leases).

### 5.3 Performance — le coût est dans la file, pas dans le rotate

`rotation_concurrency=1`, `rotation_stagger=10`,
`rotation_max_duration=240`, `rotate_timeout_s=300`. La 4ᵉ station d'une vague
attend au mieux `3 × (240 + 10) ≈ 12,5 min`. Comme `rotate_timeout_s` est plus
long que le budget du fork, l'appelant attend un job déjà terminé ou en échec :
les erreurs sont classées « timeout » alors que le fork a rendu un verdict.

Le débit de candidats est le second coût : 6 148 `bind_error` pour 584 `200`.
Chaque `bind_error` consomme un cycle de transport (WG → UDP → TCP) sans
préflight. Un préflight DNS/port de 5 s avant bind éliminerait la majorité.

### 5.4 Données — rétention partielle, pas de compaction

`requests.db` : la rétention nullifie les corps (`opencode.py:3858`) mais
conserve les lignes ; `weekly_purge_days=90` et `archive_after_days=60`
laissent 33 332 lignes de plus de 7 jours. La base reste à 1,79 Gio parce que
les 30 991 corps récents pèsent 56 Ko chacun. `freelist_count=0` prouve qu'il
n'y a rien à récupérer par `VACUUM` : il faut **supprimer des lignes**.

`registry.db` : 87 Mio pour 6 756 buckets et 17 516 incidents. Le WAL de
14 Mio au moment de l'audit indique un checkpoint insuffisant. Les 234 lignes
mortes du warm pool ne sont jamais purgées et faussent les compteurs
(`warm_pool` « 249 » vs 15 vivantes).

### 5.5 Qualité et tests

Les cinq fonctions ajoutées hier soir (`_auth_gate`, `_best_resume_station`,
`_refresh_warm_pool`, `_station_settling`, `_remove_extra_stations`) n'ont
qu'un test dédié (`_auth_gate`). Les correctifs récents sont donc validés par
observation, pas par test — ce qui explique le revert `8b493dd` → `5d409b9`
en 2 minutes. La CI existe mais ne bloque pas ces régressions.

### 5.6 Sécurité

Points positifs : la clé du plan de contrôle vient de l'environnement
(`control_plane.api_key_env`), le fork répond `401` sans clé (vérifié sur
`:8001`), `.env` est ignoré par git, aucun secret en clair dans `config.yaml`.

Points négatifs : dashboard et API proxy sans token (`require_token:false`,
`client_auth.mode:none`), écoute `0.0.0.0:4000` et joignable sur
`100.90.85.128` (tailnet). Toute machine du tailnet peut appeler
`/api/proxy/restart` ou les routes de station. Les logs contiennent des IP
publiques et des hostnames, pas de credentials.

### 5.7 Observabilité et SLO

Il n'y a pas de métriques exportées. Le SLO est reconstruit depuis SQLite et
les logs, avec un champ `free_status` NULL dans 99,9 % des requêtes récentes.
Les incidents sont comptés mais pas regroupés. Un SLO « 6/6 même cycle » n'est
donc pas mesurable aujourd'hui : c'est le premier livrable de mesure à poser.

### 5.8 Exploitation et migration

Les six stations sont maintenant sur la même image `075f0915`, ce qui clôt
l'hétérogénéité observée à 09:10. La migration douce a donc eu lieu. Deux
dérives subsistent : Compose déclare encore 7–10 stations, et 12 conteneurs
WARP consomment l'hôte. Le rollback du plan de contrôle est documenté
(`ops/runbooks/control-plane.md`) mais n'a pas été testé dans cet audit.

---

## 6. Registre des risques

| Risque | Prob. | Impact | Exposition | Mitigation |
|---|---|---|---|---|
| Crash app `:4000` sans relance | Élevée (déjà arrivé) | Critique : flotte aveugle | P0-03 | Lanceur supervisé + alerte |
| 6/6 jamais prouvé (quota mal modélisé) | Certaine | Critique : objectif non atteint | P0-01 | Verdict par IP + warm pool |
| Divergence API/registre | Certaine | Élevé : mauvaises décisions | P0-02 | Autorité unique |
| Rotation déclenchée par une requête | Moyenne | Élevé : churn, log flood | P0-05 | Garde + DEBUG |
| AUTH storm au redémarrage | Moyenne | Élevé : candidats brûlés | P1-02 | Pause unique persistée |
| Jobs de rotation sans état terminal | Certaine (44 % du stock) | Moyen : SLO faux, table qui croît | P1-10 | Réconciliation + `try/finally` |
| Disque saturé par `requests.db` | Moyenne (1,79 Go) | Élevé : écritures bloquées | P1-05 | Purge + plafond |
| Dashboard pilotable sans token | Moyenne (tailnet) | Moyen | P1-06 | Token + loopback |
| Régression silencieuse superviseur | Élevée | Moyen | P1-08 | Tests + CI |
| 7ᵉ station recréée par Compose | Faible | Élevé (violation contrainte) | P2-02 | Nettoyer Compose/.env |

## 7. Stratégie cible

### 7.1 Propriété (une seule autorité par objet)

| Objet | Autorité | Les autres deviennent |
|---|---|---|
| IP courante, transport, pays | fork (`/v1/vpn/state`) | recopiés en lecture dans `registry.db` |
| Pin hostname | `registry.db` | `.env` généré, jamais édité à la main |
| Lease/quota | `registry.db` | cache mémoire avec TTL court |
| Décision de rotation | superviseur (seul appelant de `/v1/vpn/rotate`) | app signale « épuisé », ne rotate pas |
| `client_usable` | app (probe live) → écrit `station_state` | API lit `station_state`, ne recalcule pas |
| Warm pool | superviseur (`_refresh_warm_pool`) | affectation le consomme |
| SLO / incidents | job dédié depuis `registry.db` | dashboard lit |

Règle : **un objet = un écrivain**. Toute autre surface est un read-model
horodaté avec un champ `source` et un test de non-divergence.

### 7.2 Candidats et quota

1. Le warm pool est la file d'attente principale ; `warm_pool_min=6`.
2. Un candidat ne sort du pool qu'à la consommation ou à l'expiration (6 h).
3. Préflight (DNS + port + egress) avant tout bind ; un échec de préflight ne
   consomme pas de crédit de rotation.
4. `reset_at` respecté strictement ; `ledger_max_block_s=1200` plafonne.
5. Le taux de 200 est la métrique de rendement ; objectif > 30 %.

### 7.3 Fiabilité

- App sous lanceur supervisé, `restart=always`, stdout+stderr capturés,
  heartbeat et alerte.
- Superviseur : un seul écrivain de la pause AUTH, compteurs persistés,
  tests de concurrence.
- Incidents regroupés par cause et fenêtre ; un incident flotte pour N stations.
- DNS : `doh` en tête du ladder, `plain` dernier recours, jamais de teardown
  sur un échec DNS seul (T211).

### 7.4 Données

- `registry.db` : purge TTL (`warm_pool` expirés, `incidents` > 7 j,
  `claims` > 7 j), checkpoint WAL sur seuil, `integrity_check` quotidien.
- `requests.db` : purge des lignes > 7 j, `auto_vacuum=INCREMENTAL`, plafond
  500 Mio, suppression du `.bak` après contrôle.

### 7.5 Observabilité

- `/metrics` Prometheus : `station_client_usable`, `warm_pool_live`,
  `ip_200_rate`, `rotate_latency_seconds`, `app_up`, `code_reload_total`,
  `auth_attempts_total`, `incidents_open`.
- SLO calculé par job minute, écrit dans `registry.db`, exposé au dashboard.

---

## 8. Roadmap priorisée

### Phase 0 — Mesurer et sécuriser (jour 1, sans toucher au routage)

| # | Tâche | Dépend de | Gain attendu |
|---|---|---|---|
| 0.1 | Lanceur supervisé de l'app + capture stderr | — | Fin des pertes totales au crash |
| 0.2 | `/metrics` + SLO « 6/6 même cycle » | — | Rend l'objectif mesurable |
| 0.3 | Token dashboard + `client_auth` | — | Ferme l'accès tailnet |
| 0.4 | Purge `warm_pool`/`incidents` + checkpoint | — | Registre < 50 Mio |

### Phase 1 — Une seule vérité d'état (jours 1–2)

| # | Tâche | Dépend de | Gain attendu |
|---|---|---|---|
| 1.1 | `station_state` = read-model de l'app | 0.2 | Zéro divergence API/registre |
| 1.2 | Pins générés depuis le registre ; `.env` en lecture seule | 1.1 | Fin des pins périmés |
| 1.3 | Test d'intégration de non-divergence | 1.1 | Bloque les régressions |
| 1.4 | Retirer le log/rotate du chemin chaud (`free/pool:5134`) | 0.2 | Moins de bruit, moins de churn |

### Phase 2 — Quota et approvisionnement (jours 2–4) — débloque le 6/6

| # | Tâche | Dépend de | Gain attendu |
|---|---|---|---|
| 2.1 | Séparer `ours_used` / `upstream_verdict` | 1.1 | Verdict IP correct |
| 2.2 | Consommer le warm pool avant qualification | 2.1 | +80 % d'IP utilisées |
| 2.3 | Préflight avant bind | 2.1 | Moins de `bind_error` (59 %) |
| 2.4 | Respect strict de `reset_at` + plafond 1200 s | 2.1 | Fin des re-tests prématurés |
| 2.5 | Objectif mesuré : 6/6 sur un cycle de 120 s | 2.1–2.4 | Critère de sortie |

### Phase 3 — Fiabilité et performance (jours 4–6)

| # | Tâche | Dépend de | Gain attendu |
|---|---|---|---|
| 3.1 | Un seul écrivain AUTH + tests | 1.1 | Fin des AUTH storms |
| 3.2 | Aligner `rotate_timeout_s` et `rotation_max_duration` | 2.4 | Moins de faux timeouts |
| 3.3 | Rotation asynchrone avec `job_id` + position de file | 3.2 | Latence p95 < 240 s |
| 3.4 | Regroupement des incidents | 0.2 | Bruit divisé par 10 |
| 3.5 | T211 DNS transitoire | 3.4 | Moins de teardown |

### Phase 4 — Dette et exploitation (jours 6–8)

| # | Tâche | Dépend de | Gain attendu |
|---|---|---|---|
| 4.1 | Purge `requests.db` + plafond | 0.4 | Disque < 500 Mio |
| 4.2 | Nettoyer Compose/.env stations 7–10 | — | Contrainte « six stations » garantie |
| 4.3 | Borner WARP à 2–3 | 3.2 | CPU hôte libéré |
| 4.4 | Tests des 5 fonctions superviseur + CI bloquante | 1.3 | Zéro revert surprise |
| 4.5 | Nettoyer scripts racine + `vpn_state*.json` | — | Dépôt lisible |

**Chemin critique vers 6/6** : 0.2 → 1.1 → 2.1 → 2.2 → 2.3 → 2.4 → 2.5.
Tout le reste améliore la tenue dans le temps mais ne débloque pas la cible.

---

## 9. Plan de mesure

### 9.1 Définitions

- **6/6 sur le même cycle** : les six stations ont `client_usable=true` dans
  la même fenêtre de probe (`probe_interval_s=120`), avec
  `last_200_at ≥ cycle_start`. Six 200 à des heures différentes ne comptent pas.
- **Warm pool vivant** : `count(*) from warm_pool where expires_at > now` et
  `verified_at > now − 6 h`.
- **Taux de 200** : `200 / (200 + 429 + no_egress + bind_error)` sur 1 h.
- **Respect des resets** : aucune tentative sur une IP dont
  `reset_at > started_at`.

### 9.2 Requêtes de vérification (lecture seule)

```sql
-- 6/6 sur le même cycle (à exécuter toutes les 2 min ; doit renvoyer 6)
SELECT COUNT(*) AS usable,
       MAX(last_200_at) AS newest_200,
       MIN(last_200_at) AS oldest_200
FROM station_state
WHERE client_usable = 1
  AND last_200_at > strftime('%s','now') - 120;

-- Warm pool vivant (doit rester >= 6)
SELECT COUNT(DISTINCT ip) FROM warm_pool
WHERE expires_at > strftime('%s','now');

-- Resets non respectés : doit TOUJOURS renvoyer 0
-- (toute qualification commencée après le 429 et avant son reset_at)
SELECT COUNT(*) FROM qualification_attempts a
JOIN quota_buckets b ON b.ip = a.egress_ip
WHERE b.last_status = '429'
  AND a.started_at > b.last_seen_at
  AND a.started_at < b.reset_at;

-- Incidents ouverts et incidents/heure
SELECT COUNT(*) FROM incidents WHERE ended_at IS NULL;
SELECT COUNT(*) FROM incidents WHERE started_at > strftime('%s','now') - 3600;

-- Taux de rotation committed (après T324 : plus aucun 'started' orphelin)
SELECT SUM(result='ok') * 1.0 / COUNT(*) FROM rotation_jobs
WHERE started_at > strftime('%s','now') - 3600;

-- Jobs orphelins (doit renvoyer 0 après T324)
SELECT COUNT(*) FROM rotation_jobs
WHERE result='started' AND started_at < strftime('%s','now') - 300;

-- Code reload : delta du log sur 15 min (doit être 0 hors déploiement)
-- grep -c "code reload" logs/fresh_chain_supervisor.log
```

### 9.3 Tableau de bord minimum (seuils d'alerte)

| Métrique | Seuil vert | Alerte |
|---|---|---|
| `station_client_usable` | 6 pendant 30 min | < 6 pendant 10 min |
| `warm_pool_live` | ≥ 6 | < 6 |
| `code_reload_total` (15 min) | 0 | > 0 |
| `app_up` | 1 | 0 pendant 30 s |
| `rotate_committed` / `rotate_total` | > 50 % | < 20 % |
| `ip_200_rate` (1 h) | > 30 % | < 10 % |
| `rotate_latency_p95` | < 240 s | > 300 s |
| `resets_respected` | 100 % | < 100 % |
| `rotation_jobs_orphelins` | 0 | > 0 pendant 5 min |
| `incidents_open` | 0 | > 3 |

### 9.4 Critère de sortie de la feature 019

1. 6/6 `client_usable` sur un même cycle, tenu 30 min, **mesuré par la requête
   §9.2**, pas par le dashboard seul.
2. `warm_pool_live ≥ 6` pendant la même fenêtre.
3. 0 `code reload` sur 15 min.
4. `app_up=1` sur 24 h, avec un test de kill réussi.
5. 100 % des resets respectés sur 24 h.
6. `rotate_committed/rotate_total > 50 %` sur 1 h.

---

## 10. Questions à trancher

### 10.1 Qui possède IP, pin, lease et décision de rotation ?

**Le fork possède l'IP et le transport ; le registre possède le pin et le
lease ; le superviseur possède la décision de rotation ; l'app ne fait que
signaler.** Concrètement : le fork publie `/v1/vpn/state` ; le superviseur
recopie l'IP dans `station_state.current_ip` à chaque cycle ; `registry.db` est
la seule source du pin et du lease ; l'app lit `station_state` et n'écrit
jamais l'IP. Les doubles sources (`vpn_state*.json`, `SERVER_HOSTNAMES_*` dans
`.env`, `quota_leases` mémoire) deviennent des caches en lecture, régénérés
depuis le registre. Un test compare les quatre surfaces à chaque cycle.

### 10.2 Comment garantir que l'app ne meurt plus ?

Lanceur supervisé (NSSM ou tâche planifiée au démarrage) avec
`restart=always`, redirection `stdout` **et** `stderr` dans des fichiers
horodatés, heartbeat `/health` vérifié toutes les 10 s, alerte si absent 30 s.
Le crash de 10:51Z n'a laissé aucun traceback : la première action est de
capturer `sys.excepthook`, `faulthandler` et la sortie du lanceur.

### 10.3 Comment obtenir 6/6 avec des buckets partagés et intermittents ?

En cessant de demander 6 IP parfaites simultanément. On maintient
`warm_pool_live ≥ 6` en continu, on consomme une IP par station au moment où
elle est `hot`, et on accepte qu'une station repasse `DRAINING` sans perdre la
cible : le 6/6 se mesure sur un cycle de 120 s, pas sur une journée. Les IP
`cooling` sont conservées avec leur `reset_at` et re-testées une seule fois à
l'échéance.

### 10.4 Comment augmenter le rendement sans AUTH storm ni rotation prématurée ?

- Préflight avant bind (élimine 59 % de `bind_error`).
- Une seule tentative AUTH par 10 min, toutes stations confondues.
- Rotation uniquement sur `cooling` expiré ou `dead`, jamais sur un 429 unique.
- Warm pool consommé avant qualification (une IP déjà prouvée ne se re-qualifie
  pas).

### 10.5 Comment réduire la latence et les timeouts de 300 s ?

`rotate_timeout_s=300` doit devenir `rotation_max_duration + 30 = 270`, ou
mieux : l'appel rend un `job_id` immédiatement et le superviseur interroge
`/v1/vpn/jobs/{id}`. La sérialisation (`concurrency=1`) est acceptable si la
file est visible ; elle ne l'est pas si l'appelant bloque 5 min sans savoir où
il en est. Objectif : p95 < 240 s, zéro faux timeout.

### 10.6 Comment borner `registry.db` et `requests.db` ?

`registry.db` : purge des expirés (warm pool, claims, incidents > 7 j),
checkpoint WAL à 4 Mio, `integrity_check` quotidien. `requests.db` : suppression
des lignes > 7 j (pas seulement des corps), `auto_vacuum=INCREMENTAL`,
`VACUUM` hors pointe, plafond 500 Mio, suppression du `.bak` de 1,3 Go.
L'atomicité est déjà assurée par WAL + `busy_timeout=5000` ; il manque un
`PRAGMA synchronous=NORMAL` documenté et un test de coupure.

### 10.7 Quels tests/CI/observabilité ajouter ?

- Unitaires : `_auth_gate`, `_best_resume_station`, `_refresh_warm_pool`,
  `_station_settling`, `_remove_extra_stations`.
- Intégration : « 6/6 même cycle » avec un registre factice ; non-divergence
  API/`station_state` ; respect de `reset_at`.
- Contrat : un seul `rotate` par chaîne, ordre des candidats.
- CI : lint + mypy + pytest sur les chemins superviseur, bloquante.
- Observabilité : `/metrics`, SLO minute, alerte sur les 9 seuils §9.3.

---

## 11. Faits, hypothèses, incertitudes

**Faits mesurés dans cet audit** : 0/6 à 11:12:12Z ; 15 IP vivantes dans le
warm pool ; contradiction station 3 ; **98 003** lignes `[warp-rotate]` ; app
PID 7264 sans parent ni tâche planifiée ; 6 404/6 756 buckets en 429 ;
7 651/9 788 claims en `no_egress` ; 584/10 395 qualifications en 200 ;
`requests.db` 1,79 Gio ; six stations sur le même digest ; `401` du fork sans
clé.

**Hypothèses (non prouvées)** :

1. Le crash de 10:51Z est lié au spam `warp-rotate` — plausible (chemin chaud,
   appel par requête) mais aucun traceback ne le confirme.
2. Le bucket amont est partagé entre utilisateurs — déduit de `used=1` +
   `daily-bucket-exhausted` et de AGENTS.md:234, pas observé côté fournisseur.
3. Les 15 IP du warm pool sont réellement saines — `verified_at` non vérifié
   par re-probe pendant l'audit (lecture seule).

**Incertitudes à lever en Phase 0** : cause du crash (capture stderr), taux de
200 réel par IP (instrumentation), validité des 15 IP (re-probe contrôlée).

---

## 12. Annexes — commandes reproductibles

```bash
# État live
curl -s http://127.0.0.1:4000/api/vpn-status
python logs/query_status.py
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'

# Images des stations
for c in opencode-vpn opencode-vpn-{2..6}; do
  docker inspect "$c" --format '{{.Name}} {{.Image}} {{.HostConfig.RestartPolicy.Name}}'
done

# Registre (lecture seule)
python - <<'PY'
import sqlite3, time
c = sqlite3.connect('file:logs/registry.db?mode=ro', uri=True)
print('warm live', c.execute('select count(*) from warm_pool where expires_at>?', (time.time(),)).fetchone())
print('station_state', c.execute('select station,state,client_usable,reason from station_state').fetchall())
print('claims', c.execute('select status,count(*) from claims group by status').fetchall())
print('buckets', c.execute('select last_status,count(*) from quota_buckets group by last_status').fetchall())
PY

# Volume et rétention
python - <<'PY'
import sqlite3
c = sqlite3.connect('file:logs/requests.db?mode=ro', uri=True)
print('pages', c.execute('pragma page_count').fetchone(),
      'freelist', c.execute('pragma freelist_count').fetchone())
PY

# Bruit
grep -c "warp-rotate" logs/debug.log.1
grep -c "code reload" logs/fresh_chain_supervisor.log
```

**Fin du rapport.** Les changements proposés sont spécifiés dans
`specs/019-audit-optimisation-flotte-free/{spec,plan,tasks,checklist}.md`.
Aucun code n'a été modifié par cet audit.
