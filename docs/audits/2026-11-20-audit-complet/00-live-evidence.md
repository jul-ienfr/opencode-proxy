# Preuves live — 2026-11-20 (mesures directes, non dérivées d'un auditeur)

Toutes les mesures ci-dessous ont été prises par l'auditeur principal le
2026-11-20 entre 12:24 et 12:40 Paris, sur la machine de l'opérateur, proxy
en fonctionnement (PID écoutant sur `0.0.0.0:4000`).

Fichier brut conservé : `.artifacts/audit-2026-11-20/vpn-status-20261120.json`.

---

## 1. `GET /api/vpn-status` — le payload se contredit toujours

Comparaison avec l'audit SRE du 2026-11-14 (`specs/019-.../audit.md` §2.1,
aujourd'hui uniquement dans `git stash`).

| Champ | 2026-11-14 | 2026-11-20 | Lecture |
|---|---|---|---|
| `stations_total` | 6 | **6** | la flotte est bien de 6 |
| `station_count` | 6 | **6** | — |
| `configured_stations` | 16 | **20** | ⚠️ **aggravé** (16 → 20) |
| `usable_stations` | 10 | **20** | ⚠️ impossible : 20 > 6 stations |
| `unique_usable_ips` | 10 | **20** | ⚠️ idem |
| `active_stations_with_quota` | 13 | **10** | ⚠️ > 6 stations |
| `healthy` | 6 | **1** | dégradation |
| `routable` | 0 | **1** | — |
| `repairing_stations` | — | **11** | ⚠️ > 6 stations |
| `active_station` | -25 | **-26** | ⚠️ **négatif** |
| `duplicate_ip_groups` | — | **1** | violation potentielle du principe V |
| `capacity_reason` | — | `1 station(s) partagent une IP` | ⚠️ violation du principe V |
| `last_error` | — | `probe-ProxyError` | — |

**P0-02 de l'audit 019 est ouvert et s'est aggravé** : l'API publie 20 stations
« utilisables » et 20 « configurées » pour une flotte de 6, et un
`active_station` négatif.

## 2. `station_health` — 58 stations fantômes, 46 se déclarent `usable`

| Mesure | Valeur |
|---|---|
| clés dans `station_health` | **64** |
| clés négatives | **58** (plage `-86` … `-1`) |
| clés positives | 6 (`1`…`6`) |
| entrées `usable=true` | **46** |
| dont avec `good_probes=0` | **46 / 46** |

Les 46 entrées `usable=true` sont toutes des stations négatives avec
`good_probes=0`, `bad_probes=0`, `last_probe_age_s=null` : elles se déclarent
saines sans **aucune** sonde. Violation directe du principe I (« la
disponibilité se mesure, elle ne se déclare pas »).

## 3. Stations réelles — 4/6 en `vpn=error`

| St. | `vpn_status` | IP | `client_usable` | health | `bad_probes` | `good_probes` | `last_ok_age_s` |
|---|---|---|---|---|---|---|---|
| 1 | error | 187.14.96.182 | false | repairing | **1917** | 0 | 171 604 (≈ 47 h) |
| 2 | error | **`1.2.3.4`** | false | suspect | 3 | 0 | 133 |
| 3 | error | 187.14.82.76 | false | repairing | **1909** | 0 | 170 976 (≈ 47 h) |
| 4 | error | 187.13.184.75 | false | repairing | **1947** | 0 | 174 300 (≈ 48 h) |
| 5 | connected | 187.14.80.21 | **true** | usable | 0 | 49 | 3,9 |
| 6 | error | 187.15.142.162 | false | repairing | **1480** | 0 | 132 802 (≈ 37 h) |

Deux observations :

1. **Station 2 porte l'IP sentinel `1.2.3.4`**, explicitement listée comme
   valeur à rejeter dans `free/egress_ip.py:23` (« le `live 1.2.3.4` du
   2026-11-17 ne doit plus libérer une IP prouvée »). Elle a malgré tout été
   servie comme `client_usable=true` quelques minutes plus tôt (mesure
   12:26), avec `ip_state=active` et `quota_headroom=199`.
2. **`bad_probes` atteint 1 947** sur la station 4 sans qu'aucun mécanisme
   n'ait parké la station : le réparateur sonde en boucle depuis ~48 h.

## 4. `logs/registry.db` (160 Mo) — mesures SQL

### 4.1 Rendement de la chaîne d'approvisionnement (P0-04)

`qualification_attempts` — 40 761 lignes :

| Statut | 2026-11-14 | 2026-11-20 | Évolution |
|---|---|---|---|
| `bind_error` | 6 148 (59,1 %) | **33 383 (81,9 %)** | ⚠️ **aggravé** |
| `200` | 584 (5,6 %) | **3 125 (7,7 %)** | légère amélioration |
| `429` | 2 341 (22,5 %) | 2 898 (7,1 %) | — |
| `skipped` | 636 | 636 | figé |
| `no_egress` | 593 | 603 | — |

`claims` — 9 788 lignes :

| Statut | 2026-11-14 | 2026-11-20 |
|---|---|---|
| `no_egress` | 7 651 (78,2 %) | **9 244 (94,4 %)** |
| `ok` | 17 (0,17 %) | **4 (0,04 %)** |
| `country_backoff` | 1 667 | 492 |
| `abandoned` | 73 | 44 |

**Le préflight avant bind (tâche T311 de la spec 019) n'a pas été
implémenté** : 81,9 % des tentatives meurent en `bind_error`, et 94,4 % des
claims en `no_egress` pour 4 succès.

### 4.2 Modèle de quota (P0-01)

`quota_buckets` — 8 519 lignes :

| `last_status` | Nombre | Part |
|---|---|---|
| **429** | **7 570** | **88,9 %** |
| 200 | 781 | 9,2 % |
| 0 (jamais vu) | 168 | 2,0 % |

`ip_state` — 283 lignes : `unproven` 167, `proven` 116. **Aucun état
`spent` ni `burned`** alors que 7 570 buckets sont en 429 : le modèle d'état
d'IP de la spec 023 n'est pas alimenté par les 429 observés.

### 4.3 Warm pool (P0-04 / P1-04)

| Mesure | 2026-11-14 | 2026-11-20 |
|---|---|---|
| lignes totales | 249 | **742** |
| **vivantes** (`expires_at > now`) | 15 | **2** |
| `transport` vide | — | **683** |
| `model` vide | — | **684** |
| `score_ip = 0` | 3 | **742 (100 %)** |

La purge (T304) n'a pas été faite : 740 lignes mortes sur 742. Le pool est
passé de 15 IP vivantes à **2**, et les colonnes `transport`, `model`,
`score_ip` ne sont jamais remplies — exactement le constat P2-04 de l'audit
019, non corrigé.

### 4.4 Jobs de rotation (P1-10)

`rotation_jobs` — 3 808 lignes :

| `result` | Nombre |
|---|---|
| `failed` | 1 377 |
| `ok` | 1 254 |
| `abandoned` | 1 042 |
| **`started` (sans `finished_at`)** | **135** |

Dont **134 > 1 h** et **133 > 24 h**. La réconciliation (T-rotation) a
partiellement fonctionné (1 042 passés en `abandoned`) mais **la fuite
continue** : 135 jobs ouverts.

### 4.5 Incidents (P1-03)

`incidents` — **35 219** lignes (17 516 le 2026-11-14, soit **×2,01 en 6
jours**), dont **2 ouverts**. Répartition :

| `cause` | Nombre |
|---|---|
| `tunnel_down` | **26 102 (74,1 %)** |
| `client_unusable` | 5 113 |
| `auth_failed` | 3 137 |
| `no_egress` | 543 |
| `client_usable_low` | 180 |
| `duplicate_ip` | 83 |
| `sweep_stalled` | 55 |
| `warm_pool_low` | 6 |

Aucun regroupement par cause/fenêtre : 26 102 incidents `tunnel_down`
individuels. Le debounce (P1-03) n'a pas été implémenté.

### 4.6 Dérive base de données

| Fichier | 2026-11-14 | 2026-11-20 |
|---|---|---|
| `logs/registry.db` | 87,2 Mio | **160 Mo** (×1,83) |
| `logs/requests.db` | 1,79 Gio | **160 Mo** (purge faite ✅) |
| `logs/registry.db-wal` | 14,2 Mio | 5,3 Mo |
| `logs/station_slo.jsonl` | — | **131,6 Mo** |
| `logs/debug.log.1` | 66,6 Mo | 63,5 Mo |
| `logs/nordvpn_api.json` | — | 27,1 Mo |
| **`logs/` au total** | — | **15 Go** |

`logs/backups/` = **13 Go** à lui seul (188 fichiers, dont un
`requests-20261116T174327Z.db` de **998 Mo** et 10 `registry-*.db` de
127–146 Mo).

## 5. État de la spec 019 — jamais commitée

Les cinq fichiers de `specs/019-audit-optimisation-flotte-free/` (audit.md
755 l., plan.md 218 l., spec.md 200 l., tasks.md 259 l., checklist.md 114 l.)
existent **uniquement dans `git stash@{0}`** (`spec19-wip-2026-11-14`, commit
d'untracked `bb0e80d`). Le répertoire `specs/019-*` **n'existe pas** dans
l'arbre de travail, alors que la branche courante s'appelle
`019-audit-optimisation-flotte-free`.

Conséquence : **aucune** des tâches T301–T3xx n'a de trace dans le dépôt, et
les 5 causes racines P0 de cet audit sont, au 2026-11-20, **toujours
ouvertes ou aggravées** (voir §1, §3, §4).

## 6. Processus et hôtes

| Mesure | Valeur |
|---|---|
| Écoute `:4000` | `0.0.0.0:4000` (PID 40208) |
| `pythonw.exe` en cours | **4** (PIDs 24212, 27164, 11280, 17496) |
| `python.exe` en cours | 6 |
| Worktrees git enregistrés | **7** (dont `.audit-wt`, `.audit-logs-wt` dans le dépôt) |
| Taille du dépôt (`.git`) | 117 Mo |
| Taille de l'arbre de travail | **16 Go** (dont `logs/` 15 Go) |

### 6.1 Conteneurs Docker (21 en cours)

| Conteneur | État | Image |
|---|---|---|
| `opencode-vpn`, `-2`, `-3`, `-4`, `-6` | **Up 2 days (unhealthy)** | `gluetun-016:local` |
| `opencode-vpn-5` | Up 2 days (healthy) | `gluetun-016:local` |
| `warp-1` … `warp-12` | Up (healthy) | `caomingjun/warp:latest` |
| `oc-public-egress-b1794e7a68b2` | Up ~1 h | `python:3.12-slim` |
| `ofu-engine`, `v2-postgres-1` | Up | hors projet |

**5 des 6 stations sont `unhealthy` depuis 2 jours** alors que
`station_health` les présente comme `repairing` sans jamais les parker.

### 6.2 Collisions de ports SOCKS5 WARP

`/api/vpn-status` expose 28 entrées `socks5_proxies` pour 12 ports distincts :
chaque port est déclaré **deux fois** (`WARP n` et `WARP n IPv6`), et **6
ports ont les deux entrées `enabled=true` simultanément** :

| Port | Entrées actives |
|---|---|
| 11087 | `WARP 4`, `WARP 4 IPv6` |
| 11106 | `WARP 7`, `WARP 7 IPv6` |
| 11107 | `WARP 8`, `WARP 8 IPv6` |
| 11110 | `WARP 11`, `WARP 11 IPv6` |
| 11111 | `WARP 12`, `WARP 12 IPv6` |

Deux « proxys » distincts pointant le même `host:port` ne peuvent pas être
deux egress différents : le second écraserait le premier. 20 entrées sur 28
sont `enabled`, dont 12 marquées `IPv6`.

### 6.3 `/health` ment sur la taille de la flotte

```json
{"status":"degraded","status_reasons":["free: 1 endpoint(s) partagent un bucket"],
 "free_capacity":{"configured_stations":20,"stations_total":20,
                  "usable_stations":20,"unique_usable_ips":20,
                  "unique_buckets":19,"active_stations_with_quota":9,
                  "duplicate_ip_groups":1,"duplicate_stations":1,
                  "capacity_degraded":true}}
```

Le endpoint de santé — celui qu'un superviseur interroge — annonce **20
stations** pour une flotte de 6. Un moniteur externe calibré sur
`stations_total` ne peut pas détecter une flotte à 1/6.

### 6.4 `/metrics` existe (contrairement à T302, partiellement livré)

`GET /metrics` → **200**, 167 lignes. Métriques présentes : `app_uptime_s`
(12 372 s), **`app_restarts_total = 4`**, **`app_health_failures_total = 43`**,
`client_reserve_ips = 2`, `probe_budget_remaining = 98`, `client_429_total`
(5 : 3 `all_fallbacks_dead`, 2 `upstream_429`), `vpn_station_connected` par
station (seule la 5 est à 1), `vpn_station_degraded`,
`vpn_station_cooldown_active`.

Le superviseur d'app (T301) **existe donc partiellement** (4 redémarrages
enregistrés), mais `app_health_failures_total = 43` signale que `/health`
échoue souvent — cohérent avec le payload de §6.3.

### 6.5 Sécurité — authentification désactivée dans le réseau de confiance

| Test | Résultat |
|---|---|
| `GET /health` sans token | 200 |
| `GET /metrics` sans token | 200 |
| `GET /api/stats` sans token | **200** |
| Écoute | `0.0.0.0:4000` (toutes interfaces) |

`config.yaml` : `dashboard_trust.require_token: false`,
`client_auth.mode: none`. Les routes de contrôle de la flotte sont donc
joignables sans authentification depuis toute machine du réseau. C'est le
constat **P1-06** de l'audit 019, non corrigé.

> **⚠️ CORRECTION (2026-11-20, en traitant T025)** — « depuis toute machine du
> réseau » est **imprécis**. Vérification de `trust.py` :
>
> | IP | `is_loopback` | `ip_trusted` |
> |---|---|---|
> | `127.0.0.1`, `::1` | **True** | True |
> | `192.168.1.5`, `100.90.85.128` | False | **True** (dans `lan_cidrs`) |
> | `8.8.8.8` | False | **False** → **403** |
>
> **Il y a une protection par réseau** : `trust.py` refuse en **403** tout ce
> qui n'est ni loopback ni dans `lan_cidrs`. L'énoncé exact est donc : *« dans
> le réseau de confiance (LAN + `100.64.0.0/10`), les routes de contrôle sont
> accessibles sans jeton »* — et c'est une **décision délibérée**, documentée
> par le test `test_vpn_endpoints::test_posts_require_token_when_configured`
> (« *[v9 §14.0.3] Zéro friction : le réseau est l'identifiant* »).
>
> **Conséquence** : ce n'est pas un bug mais un **arbitrage d'exposition**.
> J'ai tenté de l'activer (T025) ; le test a échoué **à raison**, et j'ai
> restauré l'état d'origine. La décision revient à l'opérateur.

### 6.6 Divergence `egress_provider_state` ↔ `egress_options`

Deux tables du **même** `registry.db` publient deux vérités opposées :

| Provider | `egress_provider_state.usable` | `egress_options` où `status='usable'` |
|---|---|---|
| nordvpn | **0** | **186** |
| warp | **17** | **28** |
| public | **172** | **1 413** |

Pour NordVPN, `reachable=0` **et** `usable=0` alors que 186 options sont
`usable` avec `last_status=200`. Ces 186 sont pourtant **toutes périmées** :
`last_checked_at` a entre **48,8 h et 238 h** (médiane 76,5 h ; 71 ont plus
de 7 jours). Aucune purge n'a été faite.

### 6.7 `ip_state` — 116 IP `proven` sans TTL ni sonde

| Mesure | Valeur |
|---|---|
| `proven` | 116 |
| âge depuis `proven_at` | 48,4 h … 58,7 h (médiane 51,4 h) |
| `reset_at` renseigné | **14 / 116** |
| `probe_count = 0` | **116 / 116** |

Aucune n'a jamais été sondée (`probe_count=0`), 102 n'ont aucun `reset_at`,
et toutes datent de plus de 48 h. La station 2 (`1.2.3.4`) n'a **aucune**
ligne `ip_state` ; la station 3 a l'état `unproven` alors que l'API la
présente avec `ip_state=qualified` ; les stations 4 et 6 sont `proven` dans
`ip_state` alors qu'elles sont `client_usable=false` depuis ~48 h.

### 6.8 `quota_leases` en mémoire — 40 leases, tous `selectable`

| Mesure | Valeur |
|---|---|
| leases | 40 |
| `state='qualified'` | 36 |
| `state='active'` | 4 |
| **`selectable=true`** | **40 / 40** |
| leases sur station **négative** | majorité (`-8`, `-12`, `-15`, `-25`, `-28`…) |
| `success_count=0` **et** `error_count=0` | majorité |

Le sélecteur considère 40 leases « sélectionnables » dont la plupart portent
une station négative inexistante et n'ont **jamais** ni réussi ni échoué.
`ip_stats` est **vide** (`n=0`).

## 7. Dépôt et configuration

### 7.1 `docker-compose.yml` — services vestigiaux toujours déclarés

14 services réels (hors ancres YAML) :

| Service | Verdict |
|---|---|
| `opencode-proxy` | actif |
| `vpn-gluetun`, `-2` … `-6` | 6 stations, actives |
| **`vpn-gluetun-7`, `-8`, `-9`, `-10`** | ⚠️ **vestiges** d'une 7ᵉ–10ᵉ station supprimée |
| `vpn-wg-test` | banc d'essai |
| `docker-socket-proxy`, `vpn-alerts` | support |

Constats **P2-02** de l'audit 019 : **non corrigé**. En revanche `.env` a bien
été nettoyé (`SERVER_HOSTNAMES_STATION1..6` uniquement, plus de 7–10).

### 7.2 Travail de la spec 023 non commité

`git status` (107 entrées) : **75 modifiés, 3 supprimés, 29 non suivis**. Les
29 non suivis incluent l'intégralité du socle de la spec 023 :

```
?? free/classify_429.py          ?? free/ip_state.py
?? free/egress_ip.py             ?? free/probe_budget.py
?? free/provider_capabilities.py ?? free/rotation_policy.py
?? free/wire_admission.py        ?? free/wire_profile.py
?? free/clients/                 ?? specs/023-client-usable-egress/
?? scripts/capacity_probe.py     ?? scripts/client_smoke.py
?? scripts/egress_state.py       ?? scripts/egress_toggle.py
?? scripts/soak_zero_429.py      ?? scripts/wire_ab.py
?? tests/contract/               ?? tests/unit/
?? config/store.py               ?? docs/CLIENTS.md
?? free/captures/2.3.0.json
```

Ces modules sont **exécutés par le proxy en cours** (ils alimentent
`ip_state`, `probe_budget`, `wire_profile`) mais **aucun n'est versionné** :
un `git clean -fd` ou un `git stash` les efface, et aucune revue n'a eu lieu.
Le fichier `specs/023-.../tasks.md` le reconnaît lui-même (« pas encore
branchés dans le chemin chaud »).

### 7.3 Dépendances mortes / cycles

`requirements.txt` épingle `gunicorn==22.0.0` alors que le déploiement réel
est `pythonw opencode.py --gui` sur Windows ; `uvloop` est conditionné
`sys_platform != "win32"`. `api_keys.json` ne contient que `{}` (2 octets) et
est gitignoré.

### 7.4 Fichiers de sauvegarde à la racine

| Motif | Nombre |
|---|---|
| `.env.bak-*` | **18** |
| `config.yaml.*.bak*` | **16** |
| `credentials.env.bak-*` | **12** |
| `*_reverify.xml` | 11 |
| `can[0-9].json` | 5 |
| `PLAN_*.md` / `RAPPORT_*.md` | 13 (~450 ko) |

Plus un fichier nommé **`=`** (0 octet) à la racine, des worktrees git
`.audit-wt/` et `.audit-logs-wt/` **à l'intérieur** du dépôt (13 Mo + 14 Mo),
et un dépôt git imbriqué `gluetun/.git` (31 Mo).

## 8. Taux de succès réel par provider (`egress_options`)

| Provider | options | succès cumulés | échecs cumulés | **taux de succès** | options ayant ≥ 1 succès |
|---|---|---|---|---|---|
| nordvpn | 735 | 512 | 4 136 | **11,0 %** | 306 |
| public | 30 621 | 6 537 | 185 798 | **3,4 %** | 2 077 |
| warp | 517 | 6 300 | **190 175** | **3,2 %** | 467 |

WARP cumule **190 175 échecs** pour 6 300 succès — soit 30 échecs par succès.
Les proxys publics, 28 échecs par succès. Seul NordVPN a un taux exploitable
(11 %), et c'est précisément le provider dont `egress_provider_state` déclare
`usable = 0`.

Barrière dure `reset_at` : 343 options ont un `reset_at` futur (321 public,
20 warp, 2 nordvpn) — le respect de la barrière semble tenu côté
`egress_options`, mais **102 des 116 IP `proven`** de `ip_state` n'ont aucun
`reset_at` du tout.

Score moyen des options `usable` : nordvpn 66,8 · public 73,7 · warp 41,2.
Le score WARP est presque deux fois plus bas que celui des proxys publics,
alors que `egress_weights` lui donne **24 %** du trafic (nordvpn 52 %,
proxies 24 %, warp 24 %).

## 9. Performance réelle — où passe le temps

### 9.1 Latence de bout en bout (`requests.db`, 2 000 dernières requêtes)

| Percentile | `duration_ms` |
|---|---|
| min | 14 |
| p50 | 1 203 |
| p90 | 3 750 |
| p95 | 4 578 |
| p99 | 12 140 |
| max | 18 422 |

Répartition : **3,1 %** des requêtes finissent sous 500 ms, **9,2 %** sous 2 s.

### 9.2 Le plancher de 14 ms — surcoût du proxy

Les requêtes les plus rapides de la base sont **toutes** des `jev-1.13-free`
non-stream à **14–15 ms**. C'est le coût du proxy seul (réception, conversion,
appel amont local, réponse), sans génération. Le budget de la spec 023
(« surcoût proxy p50 < 30 ms hors génération ») est donc **tenu**.

### 9.3 Le temps est dans la génération, pas dans le proxy

| Modèle | `is_stream` | n | moyenne | max |
|---|---|---|---|---|
| `deepseek-v4.1-flash-free` | 1 | 33 928 | **16,6 s** | 222,8 s |
| `mimo-v2.6-flash-free` | 1 | 590 | **25,7 s** | 177,1 s |
| `glm-5.3-flash-free` | 1 | 130 | 21,1 s | 78,7 s |
| `jev-1.13-free` | 0 | 4 663 | **1,7 s** | 115,9 s |

**Conclusion pour la question du langage** : le proxy n'est pas le goulot.
Remplacer Python par Go ou Rust ne gagnerait que sur les ~14 ms de plancher,
soit **1,2 %** d'une requête médiane de 1 203 ms.

### 9.4 Latence par statut free — le coût des échecs

| `free_status` | n | moyenne |
|---|---|---|
| `200` | 3 785 | **1,67 s** |
| `400` | 420 | 2,23 s |
| `429` | 1 839 | **11,49 s** |
| `502` | 373 | 1,39 s |
| `503` | 1 653 | **19,67 s** |

Un `503` coûte **19,7 s** en moyenne, un `429` **11,5 s** : les échecs de la
jambe free sont **12 à 7 fois plus lents** qu'un succès. C'est le vrai coût de
performance du système, et il vient du **retry/fallback en cascade**, pas du
langage.

### 9.5 Taux de succès global

| `success` | n |
|---|---|
| 1 | 37 022 (86,5 %) |
| 0 | 5 765 (13,5 %) |

Erreurs : `HTTP 503` 2 994 · `HTTP 429` 1 759 · `HTTP 502` 502 ·
`HTTP 400` 420 · `HTTP 401` 72 · `HTTP 403` 5 ·
`upstream_tiny_output` 13.

### 9.6 Stations négatives dans la télémétrie client

`requests.station` contient des **stations négatives** qui ont servi du
trafic réel :

| `station` | n | taux de 200 |
|---|---|---|
| 1 | 3 105 | 11,1 % |
| 5 | 2 333 | **39,1 %** |
| 2 | 2 032 | 11,9 % |
| **-6** | **1 795** | **2,5 %** |
| **-2** | **1 721** | **2,4 %** |
| 3 | 1 565 | 13,9 % |
| **-4** | **1 508** | 4,6 % |
| 4 | 1 329 | 16,0 % |
| **-7** | **1 157** | 3,5 % |
| 6 | 1 157 | 32,2 % |
| **-10** | **952** | **1,2 %** |
| **-5** | **912** | 9,1 % |

Au moins 8 045 requêtes client ont été servies par des **stations
négatives** (identifiants fantômes) avec un taux de 200 de 1,2 % à 9,1 %,
contre 32–39 % pour les vraies stations 5 et 6. Le sélecteur envoie donc une
part significative du trafic réel vers des cibles fantômes.

`winner_station` est **vide** sur toutes les lignes et `hedged=0` sur les
42 787 requêtes : les colonnes de télémétrie prévues pour le hedging et le
choix de station ne sont jamais alimentées.

## 10. Langage — mesures pour la décision Go/Rust

### 10.1 Le fork Gluetun est **déjà en Go**

| Mesure | Valeur |
|---|---|
| fichiers `.go` dans `gluetun/` | **1 040** |
| lignes de Go | **110 580** |
| branche | `016-control-plane` |
| modules internes | `control/`, `egress/`, `dns/`, `healthcheck/`, `loopstate/`, `metrics/`, `firewall/`, `httpserver/`, `configuration/` … |
| endpoints exposés | `/v1/vpn/{state,status,rotate,switch,transport,transport/reset,rollback}` |

**110 580 lignes de Go sont déjà écrites et opérationnelles** dans ce projet
(le control plane des stations), soit **plus que tout le Python de production
réuni** hors tests (opencode.py 33,6 k + free/ 45,2 k + scripts/ 28 k +
app/ 12,3 k + vpn/ 11,6 k + dashboard/ 11,9 k ≈ 143 k). Le projet est donc
**déjà un système polyglotte**, et Go y est le langage du composant le plus
critique (le tunnel).

### 10.2 Ce qui bloque un portage Python → Go/Rust

| Dépendance | Usages | Équivalent Go/Rust | Verdict |
|---|---|---|---|
| **`curl_cffi`** (empreinte TLS JA3 exacte, `ja3_md5=1523504b…`) | **281** occurrences, 61 dans `opencode.py` | Go : `utls` (fork de crypto/tls) · Rust : `rquest`/`boring` | faisable mais réécriture de toute la couche HTTP |
| `tiktoken` (comptage de tokens exact) | 25 | Go : `tiktoken-go` · Rust : `tiktoken-rs` | portage direct |
| `FastAPI`/`Starlette` (35 routes) | — | Go : `chi`/`gin` · Rust : `axum` | portage direct |
| `pywebview`/`pystray` (GUI tray) | `gui/` 484 l. | Go : `systray` · Rust : `tray-icon` | à réécrire |
| `asyncio` (181 `async def`) | — | goroutines / tokio | gain réel |
| SQLite | — | `modernc.org/sqlite` / `rusqlite` | portage direct |

La contrainte dure est **`curl_cffi`** : toute la légitimité de la jambe free
repose sur l'émission d'un ClientHello au `ja3_md5` exact de la capture
officielle. En Go, `utls` sait le faire ; en Rust, `rquest`/`boring` aussi —
mais c'est **la** partie à valider en premier, sur une IP fraîche, car une
erreur d'empreinte brûle une IP (principe XVIII).

### 10.3 Le gain attendu, chiffré

| Scénario | Gain |
|---|---|
| Plancher proxy (14 ms → ~2 ms) | **1,2 %** de la médiane (1 203 ms) |
| Coût des échecs (`429` 11,5 s / `503` 19,7 s) | **inchangé** : c'est de l'attente réseau, pas du CPU |
| Génération modèle (16,6 s) | **inchangé** |
| Démarrage (budget 1 200 ms imposé par la CI) | gain réel mais marginal |
| Concurrence (GIL → goroutines) | gain **réel mais non mesuré** : le proxy sert 100 flux concurrents |

Le poste où un portage gagne vraiment est la **concurrence** : 42 787 requêtes
avec un GIL, 181 fonctions `async`, 73 points de `asyncio.to_thread` /
`run_in_executor`. C'est là que Go apporterait quelque chose — pas sur la
latence unitaire.

### 10.4 Coût du portage

| Poste | Volume |
|---|---|
| Python de production (hors tests) | ≈ **143 000 lignes** |
| Tests Python | **107 377 lignes** (à réécrire aussi) |
| Total à porter | ≈ **250 000 lignes** |
| Modules > 1 500 lignes à découper avant tout portage | `opencode.py` 33 618 · `vpn/manager.py` 11 471 · `free/pool/__init__.py` 8 362 · `app/protocol/mapping.py` 6 329 · `scripts/fresh_chain_supervisor.py` 3 294 · `scripts/fresh_chain.py` 3 242 · `dashboard/api.py` 3 047 · `config/settings.py` 2 782 |

Un portage big-bang de 250 000 lignes sur un système dont **la flotte est
actuellement à 1/6 `client_usable`** ne corrige aucun des défauts mesurés
(§1–§9) : il les déplace dans un autre langage.

## 11. Base de données — fragmentation et rétention

### 11.1 `requests.db` — 62 % du fichier est de l'espace mort

| PRAGMA | Valeur |
|---|---|
| `page_size` | 4 096 |
| `page_count` | 40 766 |
| `journal_mode` | `wal` |
| **`freelist_count`** | **25 499 pages = 99,6 Mo** |
| `auto_vacuum` | **0 (désactivé)** |
| `synchronous` | 2 (FULL) |
| taille sur disque | 159,2 Mo |

**99,6 Mo sur 159,2 Mo (62,6 %)** sont des pages libérées que SQLite ne rend
jamais au système sans `VACUUM`. La purge des corps a bien fonctionné (seulement
2 114 `request_body` non vides sur 42 784 lignes), mais l'espace n'a pas été
récupéré. La tâche P1-05 de l'audit 019 (`VACUUM`/`auto_vacuum`) **n'a pas
été faite**.

Rétention actuelle : 25 401 lignes de plus de 7 jours encore présentes (sur
42 784), 0 au-delà de 30 jours. `body_retention_days=7` nullifie les corps
mais ne supprime pas les lignes.

### 11.2 `registry.db` — 160 Mo, `auto_vacuum` désactivé

| PRAGMA | Valeur |
|---|---|
| `page_count` | 40 804 |
| `freelist_count` | 1 002 (3,9 Mo) |
| `auto_vacuum` | **0** |
| `wal_autocheckpoint` | 1 000 |
| `synchronous` | 2 (FULL) |
| `schema_migrations` | **3 versions** (initial, egress provider columns, qualification control plane) |

Croissance portée par les tables d'observation, **aucune purge visible** :

| Table | Lignes | Plus ancienne |
|---|---|---|
| `egress_observations` | **325 937** | 7,7 j |
| `egress_threat_intel` | **174 163** | — |
| `scan_attempts` | 61 027 | — |
| `qualification_attempts` | 40 824 | — |
| `incidents` | 35 249 | 12,6 j |
| `egress_options` | 31 873 | — |
| `server_transports` | 20 945 | — |
| `catalog` / `claims` | 9 788 chacun | 12,7 j |
| `rotation_jobs` | 3 808 | 12,8 j |

Les index de purge existent (`idx_incidents_time`, `idx_warm_expiry`,
`idx_egress_obs_time`, …) : **la mécanique est prête, le job de purge ne
tourne pas**. Principe XIII non tenu (« La base a une rétention, une purge,
un checkpoint WAL, un backup `VACUUM INTO` vérifié et un `integrity_check`
régulier »).

### 11.3 Sauvegardes — 13 Go, jamais purgées

`logs/backups/` contient **188 fichiers pour 13 Go** :

| Fichier | Taille |
|---|---|
| `requests-20261116T174327Z.db` | **998 Mo** |
| `registry-20261119T152953Z.db` | 146 Mo |
| `registry-20261120T093010Z.db` | 143 Mo |
| `registry-20261119T092952Z.db` | 142 Mo |
| `registry-20261120T033007Z.db` | 139 Mo |
| `registry-20261119T032948Z.db` | 139 Mo |
| `registry-20261119T212959Z.db` | 135 Mo |
| `registry-20261118T212947Z.db` | 135 Mo |
| `registry-20261118T152941Z.db` | 131 Mo |
| `registry-20261118T092932Z.db` | 127 Mo |

Un backup `registry-*.db` de ~140 Mo est produit **deux fois par jour** depuis
le 2026-11-05, sans rétention : 188 fichiers, aucun supprimé. C'est 87 % des
15 Go de `logs/`.

## 12. Configuration — clés mortes et legacy auto-déclarés

`scripts/config_coverage.baseline.json` (le fichier de référence du gate CI
`config_audit --strict`) déclare **lui-même** les clés mortes tolérées :

```json
"morte_connue": [
  "geo.policies.meta-muse-spark.last_checked_at",
  "thinking.effort_caps.ling-3.0-flash-fin",
  "thinking.effort_caps.muse_spark"
],
"legacy_a_retirer": [
  "ip_rotation.dual_station",
  "ip_rotation.vpn_proxy_port_2",
  "ip_rotation.socks5_proxy_port_2",
  "ip_rotation.state_file_2",
  "ip_rotation.compose_service_2",
  "ip_rotation.credentials_file"
]
```

Vérification : **les 6 clés `legacy_a_retirer` sont toujours présentes dans
`config.yaml`** (1 occurrence chacune) et **aucune n'apparaît dans
`config/settings.py`** — ce sont des vestiges de la seconde station
(`*_2`) supprimée, conservés « pour rétro-compat » depuis l'audit du
2026-09-08. Le commentaire du baseline renvoie à un « plan de retrait avec
ADR » qui n'a jamais eu lieu.

Ces clés ne sont pas comptées comme dette par le gate (liste « informative »),
donc elles **ne feront jamais échouer la CI**. C'est un trou dans le gate
« zéro option morte » du principe XII.

`scripts/option_map.py` (cité par la constitution XVII comme gate obligatoire)
**n'existe pas** ; seul `tests/test_option_map.py` est référencé par
`scripts/quality_gate.py:105`.

## 13. Régression du P0-05 (rotation dans le chemin chaud)

L'audit 019 (P0-05) avait identifié que `free/pool/__init__.py` déclenchait et
journalisait une rotation à **chaque requête**, produisant 98 003 lignes
`[warp-rotate]` dans `debug.log.1`.

**État au 2026-11-20** :

| Fichier | Lignes `warp-rotate` |
|---|---|
| `logs/debug.log` (courant) | **23 598** |
| `logs/debug.log.1` (roté) | 98 003 |

Le correctif a été **partiel**. `free/pool/__init__.py:5381-5394` :

```python
if self._vpn.proxy_mode in ("socks5", "auto_egress"):
    # [P0 2026-11-14] In `vpn` mode the WARP endpoints are not the
    # egress: scanning and rotating them on every request only spammed
    # `[warp-rotate] on_request` and added network churn.
    try:
        logger.info(
            "[warp-rotate] on_request enabled=%s mode=%s",
            getattr(self._vpn, "enabled", "?"),
            getattr(self._vpn, "proxy_mode", "?"),
        )
        self.rotate_exhausted_managed()
    except Exception:
        pass
```

Le commentaire dit « en mode `vpn` … cela spammait », et la parade a été
d'**ajouter une condition** (`socks5`/`auto_egress`) au lieu de retirer le
`logger.info` du chemin chaud. Conséquence : dans le mode effectivement
utilisé (`auto_egress`, cf. `proxy_mode` de l'API), **le log INFO et l'appel
`rotate_exhausted_managed()` tournent toujours à chaque requête**.

Le `logger.info` est de surcroît en `INFO` (pas `DEBUG`) et le
`except Exception: pass` avale toute erreur de la rotation déclenchée depuis
le chemin de requête.

La tâche T307 de la spec 019 (« Retirer le log et le rotate du chemin chaud »)
n'a donc **pas** été correctement exécutée, alors que son critère
d'acceptation était « ≤ 1 ligne/min au repos ». Mesure : 23 598 lignes.

## 14. P0-02 confirmé — deux autorités d'état concurrentes

L'audit 019 reprochait à l'API et à `station_state` de publier deux vérités.
Vérification au 2026-11-20 :

| Surface | Rôle | Preuve |
|---|---|---|
| `station_state` (table) | écrite par la **qualification** | `free/qualification/store.py:682` (`upsert_station_state`), appelée par `free/qualification/watchdog.py:161` |
| `/api/vpn-status` | calcule son **propre** état | `free/pool/__init__.py` + `dashboard/routers/vpn_status_routes.py` |

**`/api/vpn-status` ne lit jamais `station_state`.** Le seul lecteur de la
table dans le dashboard est `qualification_routes.py:479` — une route
`/api/egress/qualification/*` distincte, qui compte les `client_usable` pour
son propre affichage.

Comparaison des deux surfaces à la même minute (2026-11-20 12:26–12:33) :

| Station | `station_state.state` | `station_state.client_usable` | API `client_usable` | API `health.state` |
|---|---|---|---|---|
| 1 | REPAIRING | 0 | false | repairing |
| 2 | REPAIRING | 0 | *(true à 12:26)* | suspect |
| 3 | REPAIRING | 0 | false | repairing |
| 4 | REPAIRING | 0 | false | repairing |
| 5 | USABLE | 1 | true | usable |
| 6 | REPAIRING | 0 | false | repairing |

Les deux surfaces **ne s'accordent pas sur la station 2** : la table la dit
`REPAIRING/client_usable=0` avec l'IP `1.2.3.4`, l'API l'a servie en
`client_usable=true` quelques minutes plus tôt. Aucun test ne compare les
deux (le test T308 « non-divergence » de la spec 019 n'existe pas).

C'est la démonstration directe du **principe I** violé : deux chiffres de
disponibilité, calculés différemment, sans échantillons bruts concordants.

## 15. P0-03 partiellement résolu — le superviseur existe

L'audit 019 reprochait à l'app `:4000` de n'avoir **aucun** superviseur de
processus. Au 2026-11-20 :

| Élément | État |
|---|---|
| `scripts/app_supervisor.py` | **existe** (15 032 o, 2026-11-16) |
| `logs/app-supervisor.log` | existe (59 554 o, actif à 09:04) |
| `/metrics` → `app_restarts_total` | **4** |
| `/metrics` → `app_uptime_s` | **13 298 s** (3,7 h) |
| `/metrics` → `app_health_failures_total` | **43** |
| Tâche planifiée `opencode`/`proxy`/`supervisor` | **aucune** |

**Acquis** : le superviseur est livré et fonctionne (4 redémarrages
enregistrés, uptime de 3,7 h) — c'est la tâche T301 de la spec 019,
partiellement réalisée.

**Ce qui reste ouvert** :

1. **43 échecs de `/health`** pour 4 redémarrages : le health-check échoue
   ~10 fois par redémarrage. Cohérent avec le payload de santé qui annonce
   20 stations pour une flotte de 6 (§6.3) — le critère « alerte si l'app
   est absente > 30 s » n'est pas démontré.
2. **Aucune tâche planifiée** : le superviseur n'est donc pas armé au
   démarrage de la machine. Un redémarrage de l'OS ne restitue pas le
   service sans intervention (principe XVII, « Un redémarrage de l'OS
   restitue l'état persisté … sans revalidation manuelle »).
3. La spec 021 (`021-app-crash-resilience`) déclare elle-même
   **T028–T029, T031–T035 ouverts** dans son `spec.md`.

## 16. P0-04 confirmé — le warm pool n'est pas consommé

L'audit 019 reprochait à la chaîne d'approvisionnement de ne pas lire le
warm pool. Au 2026-11-20 :

| Mesure | 2026-11-14 | 2026-11-20 |
|---|---|---|
| `warm_pool` vivantes | 15 | **2** |
| `claims ok` | 17 | **4** |
| `claims no_egress` | 78,2 % | **94,4 %** |
| `bind_error` | 59,1 % | **81,9 %** |

Le warm pool est passé de 15 IP vivantes à **2**, et les succès de claim de
17 à **4**. Le rendement s'est **dégradé**, pas amélioré. La tâche T310
(« consommer le warm pool avant qualification ») n'a pas été exécutée, et
T311 (préflight avant bind) non plus — 81,9 % des tentatives meurent en
`bind_error` contre 59,1 % six jours plus tôt.

## 17. Modules au-dessus du seuil constitutionnel (1 500 lignes)

Le principe XIV pose deux règles distinctes : « **aucun nouveau module ne
dépasse 1 500 lignes** » et « les monolithes existants sont extraits par
lots ». La date de création sépare les deux cas :

| Module | LOC | Créé le | Verdict |
|---|---:|---|---|
| `opencode.py` | **33 618** | 2026-05-04 | monolithe historique → extraction |
| `vpn/manager.py` | **11 471** | 2026-09-07 | historique → extraction |
| **`free/pool/__init__.py`** | **8 362** | **2026-11-11** | ⚠️ **NOUVEAU** — interdiction XIV |
| `app/protocol/mapping.py` | 6 329 | 2026-09-07 | historique → extraction |
| `scripts/fresh_chain_supervisor.py` | 3 294 | **2026-11-01** | ⚠️ **NOUVEAU** — interdiction XIV |
| `scripts/fresh_chain.py` | 3 242 | **2026-11-01** | ⚠️ **NOUVEAU** — interdiction XIV |
| `dashboard/api.py` | 3 047 | 2026-05-04 | historique → extraction (43 routeurs déjà sortis) |
| `config/settings.py` | 2 782 | 2026-05-04 | historique |
| `scripts/gen_golden_fixtures.py` | 1 588 | 2026-08-24 | historique |

**Trois modules créés après la ratification de la constitution (v1.3.1,
2026-11-04) dépassent le seuil** : `free/pool/__init__.py` (2026-11-11,
8 362 l.), `scripts/fresh_chain_supervisor.py` et `scripts/fresh_chain.py`
(2026-11-01, 3 294 et 3 242 l.). Ce ne sont pas des monolithes « hérités »
à extraire par strangler : ce sont des modules neufs qui violent la règle
au moment même de leur création.

C'est le constat le plus structurel du projet : **la constitution n'est pas
appliquée au moment d'écrire le code**, seulement invoquée dans les
documents de spec.

### 17.1 Preuve que le monolithe grossit, commit après commit

`git show <sha>:free/pool/__init__.py | wc -l` sur les 8 derniers commits
qui l'ont touché :

| Commit | Sujet | LOC |
|---|---|---|
| `f0d89a3` | fix(free): align quota contracts and config audit | 8 086 |
| `746dbff` | feat(022): add client reserve and reserve-only routing | 8 096 |
| `ba61667` | test(022): add simulated load and probe isolation contracts | 8 106 |
| `10deddf` | feat(022): add reserve switches and rotation guard test | 8 108 |
| `9a604fe` | feat(022): gate direct egress and add the zero-429 CI contract | 8 131 |
| `1702230` | fix(022): let a station leave and re-enter the reserve | 8 142 |
| `0a0d95f` | fix(022): keep WARP servable when the reserve is non-empty | 8 149 |
| `3a8d06e` | fix(013): route public proxies after catalog 200 proof | **8 205** |
| *(arbre de travail)* | + stash 019 (162 l.) | **8 362** |

**Croissance monotone : +276 lignes en 8 commits, jamais une extraction.**
Chaque tâche de spec (022, 013) ajoute du code au monolithe au lieu de créer
le module que le principe XIV exige. Le fichier est un point de passage
obligé de toutes les features, donc la dette s'accumule mécaniquement.

Le même schéma s'applique à `opencode.py` (33 618 l.) et
`vpn/manager.py` (11 471 l.) : ce sont les fichiers que **toutes** les specs
touchent, et aucun plan de découpage n'est exécuté.

## 18. Spec 023 — l'écart entre `tasks.md` et le code réel

`specs/023-client-usable-egress/tasks.md` publie un tableau d'état qui
déclare :

> | `ip_state`, `classify_429`, `wire_profile`, **`wire_admission`** | oui (56 tests) | admission en mode `observe` seulement |

Vérification par grep des importeurs réels (hors le fichier lui-même) :

| Module | Importé par |
|---|---|
| `free/egress_ip.py` | 72 fichiers |
| `free/ip_state.py` | 15 fichiers |
| `free/probe_budget.py` | 6 fichiers |
| `free/classify_429.py` | 2 fichiers |
| `free/wire_profile.py` | 2 fichiers |
| `free/provider_capabilities.py` | 2 fichiers |
| `free/rotation_policy.py` | 1 fichier |
| **`free/wire_admission.py`** | **0 fichier** |

`free/wire_admission.py` (81 l., classe `Admission`, fonctions `admit` et
`admit_many`) n'a **qu'un seul mentionneur dans tout le dépôt** :
`tests/unit/test_wire_admission.py:2`. Il n'est **pas** branché « en mode
observe » — il n'est branché nulle part.

C'est le garde-fou du **principe XVIII**, la règle que la constitution
qualifie elle-même de « bug de sévérité maximale » si elle est enfreinte :
« Aucune requête client non conforme n'atteint une IP prouvée ». Le module
qui devait l'appliquer existe, est testé, et **n'est appelé par aucun chemin
runtime**.

### 18.1 Conséquence mesurable

| Fait | Mesure |
|---|---|
| IP `proven` en base | 116 |
| IP `proven` sans `reset_at` | 102 |
| IP `proven` avec `probe_count=0` | **116 / 116** |
| Buckets en `last_status=429` | 7 570 / 8 519 (**88,9 %**) |
| `ip_state` en `spent` ou `burned` | **0** |

Aucune IP n'est jamais passée en `spent` ni `burned` alors que 7 570 buckets
sont en 429 et que la station 2 a servi l'IP sentinel `1.2.3.4`. Le modèle
d'état d'IP de la spec 023 est alimenté en `unproven`/`proven` mais **jamais
mis à jour vers un état terminal** — faute d'admission branchée, rien ne
déclenche la transition.

### 18.2 Le socle 023 n'est pas versionné

Les 8 modules ci-dessus, plus `free/clients/`, `config/store.py`,
`docs/CLIENTS.md`, `scripts/{capacity_probe,client_smoke,egress_state,egress_toggle,soak_zero_429,wire_ab}.py`,
`tests/contract/`, `tests/unit/` et `specs/023-*` sont **tous non suivis par
git** (`git status` → `??`). Ils sont exécutés par le proxy en cours
d'exécution mais ne sont ni revus, ni réversibles, ni protégés d'un
`git clean -fd`.

## 19. Bilan comparatif des 5 P0 de l'audit 019 (2026-11-14 → 2026-11-20)

| ID | Constat de l'audit 019 | État au 2026-11-20 | Preuve |
|---|---|---|---|
| **P0-01** | Quota modélisé comme compteur local au lieu d'un bucket amont partagé ; une IP à `used=1/200` dite épuisée | ❌ **Ouvert** | 7 570/8 519 buckets en 429 (88,9 %) ; `ip_state` ne connaît que `unproven`/`proven`, **0** `spent`/`burned` |
| **P0-02** | Trois composants publient trois vérités sur `client_usable` | ❌ **Ouvert et aggravé** | `/api/vpn-status` ne lit jamais `station_state` (§14) ; l'API annonce **20 stations** pour une flotte de 6 (14/11 : 16) |
| **P0-03** | L'app `:4000` n'a aucun superviseur de processus | ⚠️ **Partiellement résolu** | `scripts/app_supervisor.py` livré, 4 redémarrages, uptime 3,7 h — mais **43 échecs `/health`** et **aucune tâche planifiée** (§15) |
| **P0-04** | Rendement 5,6 % de 200, warm pool non consommé | ❌ **Ouvert et aggravé** | `bind_error` 59,1 % → **81,9 %** ; `claims no_egress` 78,2 % → **94,4 %** ; warm pool vivant 15 → **2** ; `claims ok` 17 → **4** (§16) |
| **P0-05** | Rotation déclenchée et journalisée à chaque requête | ❌ **Régressé** | `free/pool/__init__.py:5389` toujours dans le chemin chaud ; **23 598** lignes `[warp-rotate]` dans le `debug.log` courant (§13) |

### P1 de l'audit 019 — état

| ID | Constat | État |
|---|---|---|
| P1-01 | Cinq écrivains pour l'IP/pin | ❌ ouvert (4 pins en base, `.env` toujours écrit) |
| P1-02 | AUTH storms, pause ré-armée par plusieurs chemins | ⚠️ `auth_failures_30m = 0` à l'instant, mais 3 137 incidents `auth_failed` cumulés |
| P1-03 | 17 516 incidents sans debounce | ❌ **aggravé** : 35 219 incidents (+101 %), dont 26 102 `tunnel_down` |
| P1-04 | `warm_pool`/`incidents`/`claims` non purgés | ❌ **aggravé** : 740 lignes mortes / 742 ; `registry.db` 87 → **160 Mo** |
| P1-05 | `requests.db` 1,79 Go, pas de compaction | ⚠️ taille ramenée à 159 Mo, mais **99,6 Mo de pages libres** jamais récupérées (`auto_vacuum=0`, pas de `VACUUM`) |
| P1-06 | Aucune auth dashboard | ❌ **ouvert** : `require_token: false`, `client_auth.mode: none`, `/api/stats` répond 200 sans token |
| P1-07 | `free_status` NULL, `timestamp` TEXT, pas de `/metrics` | ⚠️ `/metrics` **livré** (167 lignes) ; `free_status` rempli à **26 %**, `timestamp` toujours TEXT |
| P1-08 | Fonctions superviseur sans test | ⚠️ suite globale excellente (6 379/6 385) mais `crashguard`, `chain_repair`, `console_auth`, `provider_capabilities` sans test |
| P1-09 | `rotate_timeout_s` > budget du fork | ❌ ouvert |
| P1-10 | 1 042 `rotation_jobs` orphelins | ⚠️ réconciliés en `abandoned`, mais **135 `started`** subsistent dont 133 > 24 h |

### P2 de l'audit 019 — état

| ID | Constat | État |
|---|---|---|
| P2-01 | Scripts de diagnostic à la racine | ❌ aggravé (29 fichiers non suivis, 110 scripts dans `logs/`) |
| P2-02 | `docker-compose.yml` déclare 7–10 | ❌ **ouvert** : 14 services dont `vpn-gluetun-7..10` + `vpn-wg-test` (`.env` nettoyé ✅) |
| P2-03 | 12 conteneurs WARP + sweeps | ❌ 12 WARP + `wgsweep`/`udpsweep` toujours actifs |
| P2-04 | `warm_pool.transport/model/score` vides | ❌ **aggravé** : 683/742 `transport` vides, 684 `model` vides, **742/742** `score_ip=0` |
| P2-05 | Trois valeurs pour la même grandeur | ❌ **ouvert** : `quota_per_ip=200` (`config.yaml:280`), `quota_estimate=500` (`:1179`), `warm_pool_min=3` (`:1259`) |
| P2-06 | `vpn_state{1..6}.json` périmés | ❌ ouvert |

**Synthèse** : sur 5 P0, **0 sont fermés**, 1 est partiellement résolu
(P0-03), 3 sont ouverts et 1 a régressé (P0-05). Sur 10 P1, 0 fermés.
L'audit du 2026-11-14 a produit un diagnostic exact ; il n'a pas produit de
correction, faute d'avoir été commité.

## 20. P1-01 confirmé — les deux sources de pins divergent

L'audit 019 reprochait à l'IP/pin d'avoir cinq écrivains. Au 2026-11-20, la
table `pins` **existe enfin** (créée par migration) mais cohabite toujours
avec `.env` :

| Station | `pins` (base) | `.env` |
|---|---|---|
| 1 | `al46.nordvpn.com` (Albania) | `SERVER_HOSTNAMES_STATION1` |
| 2 | *(absent)* | `SERVER_HOSTNAMES_STATION2` |
| 3 | `is91.nordvpn.com` (Iceland) | `SERVER_HOSTNAMES_STATION3` |
| 4 | *(absent)* | `SERVER_HOSTNAMES_STATION4` |
| 5 | `tn4.nordvpn.com` (Tunisia) | `SERVER_HOSTNAMES_STATION5` |
| 6 | `us12811.nordvpn.com` (United States) | `SERVER_HOSTNAMES_STATION6` |

Deux constats :

1. **4 pins sur 6** en base, tous de `source='env-migration'` (importés, pas
   posés par le superviseur) et **tous avec `egress_ip` vide** — la colonne
   qui doit lier le pin à l'IP d'egress n'est jamais remplie.
2. `.env` porte **6 pins**, la base **4** : les stations 2 et 4 n'existent que
   dans `.env`. Les deux sources ne sont pas synchronisées.

La tâche T306 de la spec 019 (« pins dans le registre, `.env` généré depuis
le registre ») n'est donc pas exécutée : la table a été créée, mais `.env`
reste une seconde vérité active.

## 21. Vérifications complémentaires des P2 de l'audit 019

### 21.1 P2-03 — la charge hôte n'a pas été réduite

| Mesure | Valeur |
|---|---|
| conteneurs `warp-*` actifs | **12** |
| autres | `oc-public-egress-*` (1), `ofu-engine` (hors projet) |
| total `docker ps` | **21** |

Les 12 conteneurs WARP (image `caomingjun/warp:latest`, ~1,03 Go) tournent
toujours, aux côtés de 6 stations Gluetun dont **5 sont `unhealthy` depuis 2
jours**. La recommandation de l'audit 019 (« borner WARP à 2–3 ») n'a pas
été appliquée.

### 21.2 P2-06 — une seconde vérité d'état par station

`logs/vpn_state{1..6}.json` existe toujours, avec 5 fichiers figés au
2026-11-18 et un seul actif :

| Fichier | Dernière écriture |
|---|---|
| `vpn_state.json` | 2026-11-18 10:10 |
| `vpn_state2.json` | 2026-11-18 12:21 |
| `vpn_state3.json` | 2026-11-18 00:54 |
| `vpn_state4.json` | 2026-11-18 11:19 |
| `vpn_state5.json` | **2026-11-20 09:04** |
| `vpn_state6.json` | 2026-11-18 19:01 |

Cinq fichiers d'état périmés depuis 2 jours cohabitent avec `station_state`
(base), `pins` (base), `.env` et l'API — soit **cinq surfaces d'état de
station** pour six stations, exactement le constat P1-01/P0-02 de l'audit
019.

### 21.3 Hygiène de `logs/` — 322 fichiers JSON, 110 scripts Python

| Mesure | Valeur |
|---|---|
| fichiers dans `logs/` | **927** |
| dont `.json` | **322** |
| dont `.py` | **~110** |
| taille totale | **15 Go** |
| dont `logs/backups/` | 13 Go |
| dont `logs/oc1195/` | 404 Mo |
| dont `logs/archive/` | 317 Mo |

Les 110 scripts Python de `logs/` (`check_*.py`, `diag_*.py`,
`compare_*.py`, `find_visible_spawn*.py`…) sont des outils d'investigation
one-shot, non suivis, non documentés. L'un d'eux
(`logs/find_visible_spawn.py`) **réimplémente une version du gate XVI**
(« aucune fenêtre intempestive ») alors que le gate officiel est
`tests/test_no_visible_spawn.py` — deux implémentations susceptibles de
diverger, dont une hors CI.
