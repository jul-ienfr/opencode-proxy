# Audit du système free — 2026-11-04

Portée : jambe free, stations VPN, registre d'IP, quota, réparation,
observabilité. Objectif : se rapprocher le plus possible de 99,99 %
d'utilisabilité par station, sans hardcode de pays ni d'IP.

Méthode : lecture du code et de la configuration, pas des docs. Chaque
affirmation est référencée `fichier:ligne`. Les sections 2 à 4 sont complétées
par les audits en cours.

---

## 1. Modèle d'état et quota — le cœur du problème

### 1.1 Sept stores, cinq alphabets d'état

| Store | Vocabulaire | Clé | Persistance | Lu par le chemin requête ? |
|---|---|---|---|---|
| `IpState` (`free/ip_registry/domain.py:13`) | fresh/candidate/claimed/assigned/exhausted/error/retired | `egress_ip` | `logs/ip_registry.db` | **non** |
| `IpQuotaLease` (`free/ip_quota.py:25,146`) | warming/qualified/active/draining/exhausted/bad | `(model, ip)` | mémoire + `free_pool.json` | **oui** (dominant) |
| `BucketState` (`free/quota_ledger.py:87`) | exhausted + source | `(ip, jour UTC, bucket)` | `logs/quota_ledger.json` | oui |
| `_free_cooldown_scope` (`opencode.py:13468`) | ""/trial-exhausted/rate-limited/transient | `model|ip` | mémoire | oui |
| `_model_ip_health` (`free/pool.py:1729`) | ok/n429/consecutive_429/blocked_until | `(model, station, ip)` | mémoire | oui |
| `FreeIPPool._per` (`free/pool.py:815`) | bad_until/bad_cause | station | `free_pool.json` | oui |
| `LatencyRotationEngine` (`free/rotation.py:553`) | soft/hard | `(station, ip)` | fichier propre | oui |

Conséquence : le store qui contient `reset_at` et la note (SQLite) **n'est pas
lu sur le chemin de la requête**. Le sélecteur dominant (`BestIpSelector`,
`free/best_ip.py:118-161`) classe sur `headroom × success_rate` d'un lease qui
**n'a pas de champ `reset_at`** (`free/ip_quota.py:130-167`).

### 1.2 Le reset fournisseur est contourné de quatre façons

1. **Plafond à 120 s.** `opencode.py:14052-14054` : si `model_ip_bad` est vrai
   — ce qui est le cas juste après `note_model_ip` (`:13997-14018`) — le
   cooldown est `min(retry_after, 120)`. Un `Retry-After: 21600` devient
   **120 s**. L'IP est re-testée dans le mur toutes les deux minutes.
2. **Le lease n'a pas de `reset_at`.** `free/ip_quota.py:130-167` : seulement
   `blocked_until` (30 s config, 120 s défaut) et `headroom`, qui ne se
   réinitialise jamais au changement de jour.
3. **La clé du ledger inclut le jour UTC.** `quota_ledger.py:211-213` : un
   `reset_at` qui traverse minuit est stocké sous le jour D ; à 00:00 la
   lecture crée un bucket vide pour D+1 (`:348-380`) et `usable()` répond
   `True` alors que le reset est dans le futur.
4. **`EXHAUSTED` sans `reset_at` est réclamable.** `_released_state` renvoie
   `EXHAUSTED` sur 429 sans écrire `reset_at` (`sqlite_registry.py:984-985`) ;
   `_claimable` l'accepte (`:912-925`) et `score_record` lui donne **40**
   (`scoring.py:63-66`), au-dessus de `min_score: 20`.

C'est une violation directe du principe IV de la constitution.

### 1.3 La « note de présélection » n'existe pas à l'exécution

Trois classements coexistent, aucun ne partage son échelle :

1. `score_record` 0–100 (`scoring.py`) : utilisé **uniquement** par le registre
   SQLite (`eligible`, `_claimable`), jamais par le chemin requête.
2. Poids `stations.quality` (`config.yaml:68-91`) : utilisé seulement quand le
   sélecteur quota décline (`pool.py:2727-2739`).
3. `IpQuotaLease.score_key` (`free/ip_quota.py:235-249`) : **le vrai rang**
   quand `model` est connu — `headroom × success_rate`, puis latence.

Donc la note dont tu veux te servir pour « utiliser les meilleures d'abord »
n'est pas celle qui décide. Elle est même stockée (`ip_state.score`) sans
lecteur.

### 1.4 Dérive configuration ↔ code

| Clé | Config | Défaut code | Effet |
|---|---|---|---|
| `allocator.mode` | `false` | `"off"` | **aucun code `observe`/`enforce` n'existe** : seul `== "off"` est branché. Les docs qui décrivent `enforce` décrivent du code absent. |
| `allocator.max_per_24` | `6` | `2`, absent du schéma typé | diversité inopérante |
| `allocator.probe_models` | 1 | 3 | un 429 `deepseek` condamne l'IP |
| `allocator.transports` | 5 × `openvpn-udp` | `[]`, fallback `wireguard` | contredit `ovpn_protocol: tcp` |
| `allocator.global_429_threshold` | `100000` | `20` | pause globale jamais déclenchée |
| `allocator.exhausted_threshold` | `5000` | `40` | idem |
| `allocator.max_parallel_repairs` | `6` | `6` | docs : 2 ; 6 = toutes les stations en réparation |
| `stations.bad_ip_ttl_s` | `60` | `1800` | quarantaine « mauvaise IP » 30× trop courte |
| `stations.quota_ip_block_s` | `30` | `120` | re-test 4× trop tôt |

### 1.5 Conséquences mesurables

- Une IP à quota épuisé peut être re-sélectionnée au bout de 30 à 120 s, ou
  immédiatement après minuit UTC, ou après un `release`.
- Le nombre de requêtes gaspillées sur des IP bloquées n'est pas compté comme
  un incident ; il est compté comme un échec de station, ce qui déclenche des
  rotations inutiles.
- Le dashboard affiche un reset « inventé » à `last_seen + 48 h`
  (`dashboard/api.py:3349-3364`), pas le `reset_at` réel.

---

## 2. Chemin de requête et bascule

### 2.1 Sélection

Le chemin réel est `FreeIPPool._best_station()` (`free/pool.py:2615`) via
`on_request()` (`:3212`). Avec la config livrée (`ip_quota.enabled: true`), le
classement dominant est `BestIpSelector.rank()` (`free/pool.py:2697-2724`),
c'est-à-dire `IpQuotaLease.score_key` : `headroom × success_rate`, puis
latence. Le `StationSelector` de qualité n'intervient qu'en repli
(`:2727-2739`) ; le registre SQLite n'est jamais consulté.

### 2.2 La bascule non-stream contourne les portes de quota

`opencode.py:14897`, `:14900`, `:14905` : les re-picks appellent
`_best_station_excluding_many(_excl, forced_pool)` **sans `model=`**. Or
`_station_usable` ne vérifie le lease de quota et le ledger que si `model` est
fourni (`free/pool.py:2492-2499`). Donc, après un échec, le failover peut
re-choisir une IP dont le quota est épuisé : seuls les cooldowns côté
`opencode.py` l'en empêchent, et ils sont plafonnés à 120 s (§1.2).

### 2.3 La bascule stream peut faire A → B → A → B

`on_disconnect_retry()` n'exclut que la **dernière** station échouée plus les
`trial-dead` (`free/pool.py:4051-4186`) ; il n'y a pas d'ensemble cumulatif
`tried` comme en non-stream (`opencode.py:14848-14907`). Une station peut donc
être retentée plusieurs fois dans la même requête, et le budget d'essais est
consommé sans progresser.

### 2.4 Budgets et temps client

| Élément | Valeur | Effet |
|---|---|---|
| Budget requête global | `request_budget_s: 240` (`config.yaml:648`) | plafond dur : 504 JSON ou trame SSE `timeout_error` |
| Lecture upstream free | 600 s (`config.yaml:15`) | une connexion pendue peut consommer tout le budget |
| Premier octet stream | 30 s (`config.yaml:510`) | par tentative |
| Tentatives non-stream | 6 | × (slot 3 s + connect 10 s + read 600 s) ≈ 61 min sans le middleware |
| Tentatives stream | `5 + max(2, clés)` = 7 | × 43 s ≈ 305 s, plafonné à 240 s |
| Pause inter-tentatives | 0,35 à 2 s | cumul ≤ 7,9 s |
| Attente rotation | 5 s (`rotation_wait_timeout`) | contournée en `strict_free` |

### 2.5 Deux comportements qui fabriquent du 0/6

- **Cooldown global par modèle** : `_free_failover_exhausted()`
  (`opencode.py:14084-14113`) applique un cooldown à **toutes** les stations
  pour un modèle après 3 429 quota réels sur des IP distinctes. Un épuisement
  transitoire d'un modèle se traduit donc par « aucune station utilisable »
  pour ce modèle.
- **Bad-mark sans dimension modèle** : `_apply_bad_mark()` écrit
  `bad_until` sur la station (`free/pool.py:2250-2269`), lu sans `model`
  (`:2421-2425`). Une panne de tunnel exclut la station pour tous les modèles,
  ce qui est correct pour le tunnel mais pas pour un échec lié au modèle.

### 2.6 Verrous

Aucun verrou ne sérialise les requêtes upstream. `_pool_pick_lock` ne couvre
que la sélection (`free/pool.py:3301-3365`), le sémaphore par
`(modèle, station)` attend au plus 3 s puis fail-open (`:2029-2057`), et le
pool de sessions curl crée une session de débordement après 5 s
(`upstream/clients.py:119-140`). La bascule n'attend donc pas derrière les
autres requêtes ; elle est lente par ses timeouts, pas par contention.

## 3. Pipeline de réparation

### 3.1 Le chiffre qui décide de tout

Mesures sur les logs du 2026-11-04, 83 réparations réussies :

| Métrique | Valeur |
|---|---|
| Meilleure réparation (chaîne → winner) | **27 s** |
| Médiane (p50) | **785 s — 13 min** |
| p90 | **2 944 s — 49 min** |
| Maximum | **3 445 s — 57 min** |
| Chaîne sans winner | moyenne 1 027–1 644 s, max 3 585 s |
| Winners / lancements de chaîne | **40 / 790 (5 %)** |
| `no-egress` / rejets de bind | **5 847 / ~5 950 (98 %)** |

Conséquence directe : à 13 min de réparation médiane, **une seule réparation
par mois plafonne une station à 99,97 %** ; quatre réparations la font tomber à
99,88 %. Le budget 99,99 % est de 4,38 min/mois. Autrement dit, le SLO se joue
sur le MTTR, pas sur la vitesse du rebind.

### 3.2 Où passent les 13 minutes

Décomposition p50 par candidat, à 6 chaînes :

| Composant | Temps | Part |
|---|---|---|
| Attente inter-candidats (`--delay 8` + `mark` + **budget de rebind**) | 41 s (jusqu'à 55 s) | ~34–46 % |
| Docker recreate + clean | 5–9 s | ~5 % |
| `settle_s: 12` aveugle, avant tout test d'egress | 12 s | 10 % |
| Egress (0 s si le tunnel répond pendant le settle, sinon 45 s + dépassement) | 0–45 s | 0–45 % |
| Probe (POST + SSE) | 5 s | ~4 % |
| Écritures registre / mark / pin | ~1 s | ~1 % |

Le budget de rebind est **la contrainte dure** : `rebind_limit_per_min: 3`
partagé par 6 chaînes = **120 s par candidat par chaîne**, ce qui correspond
exactement au p50 `claim → claim` mesuré (120 s sur les six stations).
Avec 40 candidats par chaîne, une chaîne peut tourner **80 minutes** avant de
renoncer.

### 3.3 Le défaut de conception : détruire avant de prouver

`_clean_station_container()` (`fresh_chain.py:874-892`) supprime le conteneur
**avant** chaque bind, puis `docker compose up --force-recreate` recrée. Il n'y
a ni bleu/vert, ni chevauchement : le port SOCKS disparaît, les requêtes en
cours échouent, et la station est indisponible pendant **toute** la
réparation — 13 min en médiane.

Le seul « retour arrière » est `_restore_previous_host()` (`fresh_chain.py:1275-1308`),
qui refait un recreate complet (~65 s) et n'a servi que 11 fois.

### 3.4 Pourquoi 98 % de `no-egress`

Le pipeline enchaîne les hostnames du catalogue sans pré-qualification :
manifeste, auth et transport ne sont vérifiés qu'après le recreate. Résultat :
~5 847 échecs d'egress sur ~5 950 binds, et 5 % de chaînes qui trouvent un
winner. Le système passe l'essentiel de son temps à détruire des stations pour
tester des serveurs qui ne peuvent pas monter.

### 3.5 Parallélisme

`max_parallel_repairs: 6` (`config.yaml:728`) autorise **les six stations à se
réparer en même temps** — donc zéro station serviante. Le débit global est de
toute façon limité par `rebind_limit_per_min: 3` : lancer six chaînes ne les
accélère pas, cela les ralentit toutes à 120 s par candidat. Deux chaînes à
1,5 rebind/min avancent plus vite que six à 0,5.

### 3.6 Délais codés en dur

`settle_s 12`, `--delay 8`, `recreate_timeout 180`, probe 60 s × 2 schémas,
egress 4 URLs × 2 schémas × 8 s en série (jusqu'à 64 s, ce qui dépasse le
deadline de 45 s), lease de claim 15 s, `INTERVAL_S 20`, `HEALTH_GRACE_S 600`.
Aucun n'est dans `config.yaml`.

## 4. Démarrage et observabilité

### 4.1 L'automatisation est éteinte par construction

- `opencode.py:2226-2230` : l'auto-démarrage de `fresh_chain_supervisor.py` est
  conditionné à `allocator.mode != "off"`. La config dit `false`
  (`config.yaml:725-726`), normalisé en `"off"` (`free/ip_registry/config.py:66-68`).
  Donc **le superviseur n'est jamais lancé par le proxy**.
- `fresh_chain_supervisor.py:1028-1035` : même lancé à la main (par
  `lancer-proxy.bat:48`), il idle et logue `allocator mode=off`. Il ne répare
  rien.
- Sur Windows, `Start.bat:18` et `lancer-silent.vbs:15` ne lancent que
  `opencode.py` ; aucun ne démarre le superviseur. Seuls `lancer-proxy.bat:48`
  et `lancer-gui.bat:41` le lancent, pour rien tant que le mode est `off`.
- `opencode.service:15-17` redémarre le proxy sous Linux (`Restart=always`).
  Sous Windows, aucun mécanisme ne relance `opencode.py` s'il meurt.

### 4.2 Aucun superviseur unique

Six boucles indépendantes peuvent agir sur une station :

| Boucle | Portée |
|---|---|
| `vpn/manager.py::_watchdog_loop` (`:9604`) | une par station (6) |
| `vpn/manager.py::_auth_repair_loop` (`:8198`) | une par station |
| `scripts/fresh_chain_supervisor.py` | une par machine (lock) |
| `free/pool.py::_proactive_loop` (`:1545`) | rotation planifiée 60 s |
| `free/pool.py::_rotation_worker` (`:4516`) | `rotation_concurrency: 1` |
| `docker_events.py` | réveil des watchdogs |

Le manager et l'allocateur **n'ont pas de verrou commun** : le manager utilise
ses locks asyncio (`vpn/manager.py:1947-1952`), l'allocateur un fichier lock par
station (`fresh_chain_supervisor.py:155-174`). La coordination repose sur des
drapeaux HTTP et des grâces (`:437-495`), pas sur une exclusion mutuelle. Deux
réparations concurrentes restent possibles.

`ops/supervisor.py:13` documente lui-même que le watchdog/restart est « to be
wired later ».

### 4.3 La disponibilité n'est pas mesurée

- Les états exposés sont des booléens instantanés (`station_usable`,
  `model_usable`, `health.state`) calculés à la requête, jamais historisés.
- Le seul échantillonneur temporel est `scripts/station_slo_watch.py` (JSONL
  toutes les 60 s) ; il n'est lancé par aucun service, script de démarrage ou
  lifespan. Dernier fichier écrit : `logs/station_slo.jsonl`, **26/10**.
- Il n'existe ni table d'uptime/downtime, ni journal d'incidents avec
  début/fin, ni endpoint d'exposition. Le `slo` de `/health?detail=full` est un
  SLO de **latence**, pas de disponibilité (`opencode.py:22224-22235`).
- Le pool chaud n'a pas d'API : `verified_spares` n'est lu que par l'alerte de
  capacité du superviseur (`fresh_chain_supervisor.py:569-586`), et
  `warm_spare_ready` désigne le « spare station » configuré, pas la table des
  IP validées.

Conclusion : aujourd'hui, personne ne peut dire si le système est à 99,9 % ou
à 97 %, ni combien de temps une station a été indisponible.

### 4.4 Le redémarrage ne réconcilie pas

- `SqliteIpRegistry.reconcile()` n'est appelé **que dans les tests**
  (`tests/test_ip_registry_sqlite_registry.py:149`). Les leases expirés et les
  assignations orphelines ne sont pas fermés au boot.
- La réconciliation réelle vit dans le superviseur (`:221-282`), donc elle ne
  tourne pas quand le mode est `off`.
- L'état redémarre depuis des JSON (`vpn_state{n}.json`, `free_pool.json`),
  pas depuis SQLite ; les pins `.env` périmés ne sont nettoyés que par
  l'allocateur (`fresh_chain_supervisor.py:931-983`).

### 4.5 Alertes et logs

- `scripts/vpn_alerts.py` est le seul alerteur ; il n'est lancé que par le
  profil Compose `monitoring` (`docker-compose.yml:668-692`), non démarré par
  les scripts de boot. Sans `VPN_ALERT_WEBHOOK`, il écrit sur stdout.
- Le superviseur n'émet qu'un `ALERT` dans son propre log toutes les 300 s
  (`fresh_chain_supervisor.py:1173-1183`).
- `logs/fresh_chain_supervisor.log` et `logs/fresh_chain_<n>.log` n'ont
  **aucune rotation** ; `debug.log` n'est tourné qu'à l'écriture et seulement
  si `server.debug` est actif (`config.yaml:4`, `dashboard/display.py:250,323`).

---

## 5. Baseline live — 2026-11-04 08:39Z

Première mesure automatique (échantillonneur `station_slo_watch.py`, 30 s) :

| Élément | Valeur |
|---|---|
| `client_usable` | **1/6** |
| Station 1 | `status=error`, `auth_failed=true` |
| Stations 2, 3, 5, 6 | `quota=bad-ip` |
| Station 4 | seule `client_usable` |
| `healthy` / `routable` | 5 / 1 |
| 429 free cumulés | 39 |
| Fallbacks `upstream_5xx` | 9 |

Cinq incidents ouverts dans `logs/station_incidents.db`. La mesure tourne
désormais en continu ; c'est la référence à laquelle comparer chaque lot.

## Synthèse — les cinq défauts structurels

| # | Défaut | Preuve | Impact | Correction |
|---|---|---|---|---|
| 1 | La réparation détruit la station avant de prouver la remplaçante | `fresh_chain.py:874-892` | 13 min d'indispo médiane par réparation | pré-qualifier, puis bascule sans coupure ou réparation bornée |
| 2 | Le reset fournisseur n'est pas une porte dure | `opencode.py:14052` (plafond 120 s), `ip_quota.py:130` (pas de `reset_at`), `quota_ledger.py:211` (clé datée) | re-tests dans le mur, quota gaspillé, faux échecs, rotations inutiles | `reset_at` dans le lease, clé sans jour, suppression du plafond |
| 3 | Le failover contourne les portes de quota | `opencode.py:14897,14900,14905` (pas de `model=`), `free/pool.py:4051` (pas d'exclusion cumulative) | retries sur IP épuisées, A→B→A | `model` toujours transmis, `tried` cumulatif |
| 4 | L'automatisation est éteinte et non mesurée | `opencode.py:2227` (gated sur `mode`), `config.yaml:726` (`false`), `station_slo_watch.py` non lancé | aucune réparation automatique, aucune preuve de disponibilité | mode `observe` réel, échantillonneur permanent, réconciliation au boot |
| 5 | Le pays et l'IP sont figés par du code | `fresh_chain.py:38-52` (`PROVEN_HOSTS`), `.env` (pin `al41`), `config.yaml:730` (`max_per_24: 6`) | optimum local Albanie, diversité inopérante | suppression des littéraux, score + anti-affinité, exploration 5 % |

### Ce qu'il faut supprimer

- `PROVEN_HOSTS` et tout littéral `*.nordvpn.com` / pays dans le code de sélection.
- Les pins `SERVER_HOSTNAMES_STATIONn` / `SERVER_COUNTRIES_STATIONn` du `.env`,
  et les overrides `logs/compose-hostname*.yml` périmés.
- `min(retry_after, 120)` dans `opencode.py`.
- La clé de ledger incluant le jour UTC.
- `settle_s: 12` aveugle, `--delay 8`, la lecture upstream à 600 s.
- `max_candidates_per_chain: 40`, `max_parallel_repairs: 6`, `max_per_24: 6`.
- Le `probe_models` à une seule entrée.

### Ce qu'il faut ajouter

- Un mode `observe` réel : le superviseur tourne, mesure, journalise, n'agit pas.
- Un échantillonneur SLO par station (30 s) + journal d'incidents SQLite.
- La réconciliation `SqliteIpRegistry.reconcile()` au boot.
- Un verrou inter-processus allocateur ↔ manager.
- Une pré-qualification des candidats (manifeste, auth, transport) **avant**
  le recreate.
- Une API `warm_pool` et une métrique de couverture par pays.
- Des alertes actives par défaut, pas un profil Compose optionnel.

### Plan en quatre lots

| Lot | Contenu | Effet attendu |
|---|---|---|
| **0 — Mesurer** (1–2 j) | `observe`, SLO 30 s, incidents, reconcile, verrou, alerte | on sait où on est ; zéro risque |
| **1 — Arrêter l'hémorragie** (2–3 j) | reset_at dur, ledger, failover `model=`, diversité 2/2, suppression hardcodes | moins de retries inutiles, moins de rotations |
| **2 — MTTR** (3–5 j) | pré-qualification, settle supprimé, egress parallèle, 8 candidats max, 2 chaînes | p50 de 13 min à **< 60 s** |
| **3 — Capacité** (1–2 sem) | warm pool vérifié, couverture pays, score live, exploration 5 %, N stations | marge réelle, 6/6 visé |

### Objectif chiffré

- **99,9 %** par station : atteignable dès que le MTTR passe sous ~2 min avec
  moins de 10 réparations/mois.
- **99,99 %** par station : exige MTTR **< 40 s** et **≤ 4 réparations/mois**.
- **6/6 strict** : exige une session supplémentaire (7e slot) pour valider la
  remplaçante avant de lâcher l'ancienne.
