# Audit des requêtes — 2026-11-08

**Fenêtre** : 2026-11-08 00:00 → 22:05 UTC (snapshot `logs/requests.db`).
**Sources** : `logs/requests.db`, `logs/registry.db` (rotation_jobs), `/api/stats`,
`/api/vpn-status`, `/api/free-health`, `logs/debug.log`.
**But** : expliquer la dégradation observée aujourd'hui et proposer un plan
priorisé pour réduire durablement les échecs.

> **Limite de mesure importante.** Une ligne de `requests` est une **tentative**
> du handler, pas forcément une requête client unique : le middleware
> `RetryUntilSuccess` rejoue le handler, et chaque rejeu crée un nouvel `id`.
> Les chiffres ci-dessous sont donc des **tentatives**. Le taux d'échec
> réellement vu par le client est plus faible, mais il n'est pas mesurable
> aujourd'hui — c'est en soi un défaut d'observabilité (voir P2).

---

## 1. Chiffres clés

| Indicateur | Valeur | Lecture |
|---|---:|---|
| Tentatives loggées | 7 767 | +271 % vs les 2 094 du 2026-11-07 |
| Succès | 3 618 | **46,6 %** |
| Échecs | 4 149 | 53,4 % |
| Échecs `503` locaux | 2 647 | **34,1 % du trafic** |
| Échecs `429` finaux | 1 496 | 19,3 % |
| Tiny output | 6 | 0,1 % |
| Tentatives station (`free_model_usage`) | 13 642 | 3,6 tentatives par succès |
| Succès station | 3 774 | **27,7 %** des tentatives |
| 429 station | 9 868 | **72,3 %** des tentatives |
| IP sans aucun succès (≥10 essais) | 217 | 4 591 tentatives gaspillées (33,7 %) |
| Latence succès p50 / p95 / p99 | 13,8 s / 50,4 s / 70,5 s | très élevée |
| `AUTH_FAILED` NordVPN | 6 052 lignes | flotte Docker inutilisable |
| Rotations WARP réussies / échouées | 616 / 154 | ~26 rotations/heure |

**Hors smoke test SystemOne** (2 647 tentatives `503`) : 5 120 tentatives,
3 618 succès, soit **70,7 %**. Le vrai problème de capacité reste les 1 496
`429` et les 9 868 tentatives station à 429.

---

## 2. Causes classées

### P0-A — Le smoke test SystemOne est cassé par `auto_egress` (2 647 échecs)

**Preuve** : 2 647 requêtes `jev-1.13-free`, `is_stream=0`, `duration_ms≈0`,
UA `python-requests/2.33.1` et `Python-urllib/3.12`, client `192.168.31.101`.
Corps : `{"model":"jev-1.13-free","state":"smoke test pool health",...}` et
`"Rig test 100 MH/s stable"`.

Réponse exacte enregistrée :

```json
{"error":"free jev-1.13-free: pool indisponible (proxy_mode=auto_egress) — pas de direct résidentiel"}
```

**Cause code** : `_systemone_free_via_pool` n'accepte que
`proxy_mode in ("vpn","socks5")` (`opencode.py:27758`) et fail-close sur les
mêmes modes (`:28136`). `auto_egress` n'est pas reconnu, alors que le pool
WARP est disponible et que le chemin streaming utilise déjà WARP.

**Impact** : 34 % du trafic journalier est un faux échec, 0 % de succès, et le
smoke test ne teste plus rien. Il pollue aussi toutes les métriques de succès.

**Correctif** : accepter `auto_egress` dans les deux conditions, et aligner le
chemin free non-stream générique (`opencode.py:15497`, `15837`, `15854`,
`15868`). Ajouter un test avec `proxy_mode="auto_egress"` et un faux pool.
Le test de non-régression doit vérifier qu'aucun 503 « pool indisponible » ne
sort quand le pool SOCKS/WARP est actif.

---

### P0-B — Les six stations NordVPN sont en `AUTH_FAILED` (capacité divisée)

**Preuve** : `/api/vpn-status` → `status=degraded`, stations 1..6 en
`vpn_status=error`, `auth_failed=true`, `error=AUTH_FAILED`. Le log contient
**6 052** lignes `AUTH_FAILED` aujourd'hui. `credentials.env` et
`vpn_configs/credentials.txt` datent du 2026-11-07 15:16.

**Impact** : les six tunnels Docker ne servent rien ; tout le trafic free
repose sur les 6 conteneurs WARP. C'est la cause racine de la pression sur les
quotas WARP et des rotations en rafale.

**Correctif** :
1. Parquer temporairement les stations Docker (`disabled_stations` ou arrêt des
   conteneurs) pour arrêter les 6 000 tentatives AUTH et le bruit.
2. Valider les credentials sur **une seule** station (session unique), vérifier
   l'egress, puis réactiver progressivement. La limite de sessions simultanées
   NordVPN est un suspect connu : six stations + un test = refus.
3. Ne réactiver les six qu'après un `AUTH_OK` et un egress public par station.

Ne pas remplacer par un second compte ni par l'attente : le problème est la
validation des credentials existants et la limite de sessions.

---

### P0-C — Les endpoints WARP IPv4 sont des adresses NAT partagées, déjà épuisées

**Preuve** (`free_model_usage`, aujourd'hui) :

| IP | Tentatives | Succès | 429 |
|---|---:|---:|---:|
| 104.28.230.202 | 622 | 0 | 622 |
| 104.28.198.200 | 88 | 0 | 88 |
| 104.28.230.204 | 67 | 0 | 67 |
| 104.28.198.202 | 55 | 0 | 55 |

Ces `/32` sont partagés par plusieurs utilisateurs WARP : ils sont déjà à
quota nul et le resteront. Ils représentent ~830 tentatives inutiles/jour et
déclenchent des rotations qui détruisent aussi le `/64` IPv6 de la station.

**Correctif** : garder **un seul** endpoint IPv4 comme canari (WARP 1), passer
les cinq autres `enabled: false`, et ne jamais rotationner un IPv4 WARP (déjà
fait dans `free/pool.py` et `free/warp_rotation.py`). Les `/64` IPv6 restent
les seuls porteurs de quota utile.

À terme, si Cloudflare change le pool NAT, réactiver les canaris un par un.

---

### P1-A — 429 journaliers (`FreeUsageLimitError`) + gel anti-churn de 180 s

**Preuve** : les 1 496 `429` finaux ont tous le corps
`FreeUsageLimitError` / « Free usage limit reached ». Ce n'est pas un rate-limit
court : c'est le bucket journalier par IP. Les logs montrent aussi :

```
ÉVÉNEMENT AMONT : 3 signaux (429) sur 3 couples distincts / 60s
→ gel des rotations pendant 180s (refus honnête, pas de churn)
```

**Problème** : quand toutes les stations sont à quota journalier, le gel
empêche justement la rotation qui pourrait trouver un `/64` frais. On obtient
des fenêtres de 3 minutes où **aucune** requête ne peut réussir. Les 217 IP
mortes (4 591 tentatives) montrent que le blocage actuel est trop court : les
mêmes IP sont retentées en boucle.

**Correctif** :
1. Distinguer `FreeUsageLimitError` (bucket journalier) d'un 429 transitoire.
2. Pour un bucket journalier : bloquer le couple `(modèle, IP)` jusqu'au reset
   (ou `Retry-After` réel, sinon 1–6 h), pas 90–300 s.
3. Exclure le bucket journalier du gel anti-churn : autoriser **une** rotation
   contrôlée toutes les 5–10 min quand toute la flotte est épuisée.
4. Conserver le gel uniquement pour les 429 transitoires/overload.

---

### P1-B — Rotation WARP trop fréquente

**Preuve** : `rotation_jobs` : ~105 rotations acceptées par ancien conteneur
sur la journée ; 616 rotations réussies et 154 échouées dans les logs ;
94 `duplicate_block`, 11 `health_timeout`. Les rotations recréent les
conteneurs (`docker rm` + `run`), ce qui provoque des trous de service et
invalide les IP, les sessions HTTP et les identités.

**Correctif** :
- `station_max_rotations_per_hour` : 10 → **3**.
- `MIN_ROTATION_INTERVAL_S` : 300 → **600–900 s** (et le brancher sur la config).
- Ne rotationner que sur quota journalier réel, jamais sur timeout/5xx.
- Un seul conteneur tourné à la fois (déjà `rotation_concurrency: 1`).
- Mettre à jour `rotation_jobs` avec le résultat final, pas seulement
  `started` (voir P2).

---

### P1-C — Attentes de slot de 3 s (6 359 timeouts)

**Preuve** : 6 359 lignes `model slot timeout 3.xs … fail-open` réparties sur
toute la journée, avec un pic aux heures de trafic. Le plafond
`max_inflight_per_model_ip: 8` sature, on attend 3 s, puis on continue quand
même (`fail-open`) : la protection ne protège pas et coûte 3 s de latence.

**Correctif** :
- `model_slot_wait_s` : 3 → **0,25 s** (fail-open rapide), ou vraie file
  d'attente côté proxy avec un plafond global de concurrence free.
- Mesurer la distribution des attentes ; si l'amont tolère, monter
  `max_inflight_per_model_ip` à 12–16 après test A/B.
- Ne pas empiler cette attente avec les retries : aujourd'hui elle s'ajoute
  aux 13,8 s de p50.

---

### P1-D — Concentration sur `deepseek-v4.1-flash-free`

**Preuve** : 4 473 tentatives (57 % des lignes), 89,7 % des tokens, 72,7 % de
succès. Mimo : 555 tentatives, 58,4 %. GLM : 90, 44,4 %. Le client
`Anthropic/Python 0.125.0` représente 4 146 tentatives (63,8 % de succès).

**Impact** : le bucket journalier de deepseek est épuisé en premier, et c'est
le modèle le plus lent (16,6 s de moyenne).

**Correctif** : à court terme, proposer au client un modèle de repli
(`mimo-v2.6-flash-free`) quand le bucket deepseek est épuisé. À moyen terme,
ajouter une chaîne de repli free explicite (deepseek → mimo → glm) au niveau du
proxy, avec une politique de substitution contrôlée. Ne pas changer
silencieusement le modèle sans accord du client.

---

### P2 — Observabilité insuffisante

1. **Requêtes client vs tentatives** : `requests.id` est recréé à chaque rejeu
   du middleware. Impossible de mesurer le taux d'échec réel par requête
   client. Ajouter `client_request_id`, `attempt_no`, `final_status`.
2. **`rotation_jobs`** : toutes les lignes ont `result=started` ; les résultats
   réels ne sont que dans le log. Ajouter `finished_at`, `result`, `reason`.
3. **`/api/free-health`** : `served: []` et tous les modèles `unproven` alors
   que 3 618 succès ont eu lieu. Le canary/health ne reflète pas l'état réel.
4. **Alertes** : ajouter des seuils sur 429/tentative, essais sur IP morte,
   rotations/heure, p95 de slot wait, AUTH_FAILED.
5. **`usable_stations: 4`** alors qu'aucune station Docker n'est utilisable :
   l'indicateur mélange WARP et Docker et induit en erreur.

---

## 3. Plan d'action priorisé

### Phase 0 — Aujourd'hui (gain immédiat, risque faible)

| # | Action | Fichier / config | Effet attendu | Vérification |
|---|---|---|---|---|
| 0.1 | Accepter `auto_egress` sur la jambe free non-stream et SystemOne | `opencode.py:27758,28136,15497,15837,15854,15868` + tests | Supprime 2 647 faux 503 ; taux de tentative 46,6 → ~70 % | Smoke `/v1/systemone` 200/429, plus aucun « pool indisponible » |
| 0.2 | Parquer les 6 stations Docker tant que l'auth est cassée | `ip_rotation.disabled_stations` ou arrêt conteneurs | Supprime ~6 000 AUTH_FAILED/jour et le bruit de rotation | `/api/vpn-status` sans `auth_failed`; logs AUTH_FAILED → 0 |
| 0.3 | Garder un seul WARP IPv4 canari, désactiver les 5 autres | `config.yaml` `socks5_proxies` | Supprime ~800 essais morts/jour et des rotations | 0 tentative sur `.200/.202/.204` hors canari |

### Phase 1 — Cette semaine (réduction structurelle)

| # | Action | Effet attendu | Vérification |
|---|---|---|---|
| 1.1 | Blocage quota journalier par `(modèle, IP)` jusqu'au reset | Moins de reprises sur IP morte, moins de 429 finaux | 4 591 essais morts → < 500 |
| 1.2 | Exclure le quota journalier du gel 180 s ; une rotation contrôlée/5–10 min | Récupération plus rapide quand toute la flotte est épuisée | Fenêtres 429 réduites |
| 1.3 | Rotation 3/h, intervalle 10–15 min, uniquement quota journalier | 616 rotations/jour → < 100 | `rotation_jobs` + logs |
| 1.4 | Slot wait 3 s → 0,25 s ou file globale | p50/p95 en baisse | `model slot timeout` et latence |
| 1.5 | Repli modèle `deepseek → mimo → glm` (opt-in) | Moins de 429 sur deepseek | Taux de succès client |
| 1.6 | Restaurer NordVPN : valider un token/session, puis 1 → 6 stations | Retrouve la capacité des 6 stations | 6/6 `usable`, egress public par station |

### Phase 2 — Suivi

| # | Action | Effet |
|---|---|---|
| 2.1 | Colonnes `client_request_id`, `attempt_no`, `final_status` | Mesure du vrai taux d'échec client |
| 2.2 | `rotation_jobs.result/finished_at/reason` | Audit des rotations sans parser les logs |
| 2.3 | Réparer `/api/free-health` (`served`/`unproven`) | Tableau de bord honnête |
| 2.4 | Alertes 429/tentative, IP mortes, rotations/h, slot p95, AUTH_FAILED | Détection avant incident |
| 2.5 | SLO latence par modèle + choix de modèle conseillé | Moins de perception « c'est lent » |

---

## 4. Seuils de succès du plan

À 24 h après Phase 0 :

- `503 pool indisponible` = **0** ;
- `AUTH_FAILED` < **50**/jour ;
- tentatives WARP IPv4 mortes < **100**/jour ;
- taux de succès des tentatives **> 65 %** (hors smoke) ;
- rotations WARP < **150**/jour ;
- p95 succès < **35 s** (objectif intermédiaire).

À 7 jours après Phase 1 :

- 429 finaux < **5 %** des tentatives ;
- essais sur IP morte < **5 %** des tentatives ;
- au moins **4/6** stations client-utilisables en permanence ;
- p50 succès < **10 s**, p95 < **30 s**.

---

## 5. Ordre d'exécution recommandé

1. **0.1** (SystemOne `auto_egress`) : correctif code + test, aucun impact
   négatif attendu.
2. **0.3** (WARP IPv4) : config, réversible en une ligne.
3. **0.2** (parquer NordVPN) : ops, à faire avec validation des credentials.
4. **1.1 + 1.2** (quota journalier / gel) : le gain le plus important sur les
   429 et le churn.
5. **1.3** (budget de rotation), puis **1.4** (slot wait).
6. **1.5** (repli modèle), **1.6** (restauration NordVPN).
7. **Phase 2** en continu.

---

## 6. Ce qu'il ne faut pas faire

- Réintroduire un `200` avec une trame d'erreur pour masquer les 429 : la
  correction V5.1 est volontaire et les tests la verrouillent.
- Rotationner tous les conteneurs à la fois : chaque rotation crée un trou de
  service et invalide les IP.
- Utiliser le direct résidentiel en `auto_egress` : `enforce_vpn_only` est
  actif, et c'est ce qui a produit le 503 SystemOne.
- Attribuer le problème au seul modèle `jev-1.13-free` : il n'est que le
  révélateur du chemin `auto_egress` non supporté.

## 7. Mise à jour — correctifs P0 appliqués (2026-11-08 23:45)

- **P0.1** : `_FREE_POOL_MODES = ("vpn","socks5","auto_egress")` couvre les
  chemins non-stream et SystemOne. `GET /v1/systemone` atteint WARP :
  `200` observé, puis `400 api_usage_error` selon le payload amont ;
  plus aucun `503 pool indisponible`.
- **P0.2** : token NordVPN `200`, credentials de service valides ; un Gluetun
  isolé se connecte. Les `AUTH_FAILED` venaient de la limite de sessions et des
  chasseurs `fresh_chain` qui relançaient les stations. Les six stations sont
  parkées (`disabled_stations: [1..6]`) et les six chasseurs + superviseur
  arrêtés ; `AUTH_FAILED` = 0 sur la fenêtre d'observation.
  - Validation station 1 : connexion OpenVPN OK, IP `202.49.186.166` obtenue,
    mais la sonde d'egress public échoue puis le serveur suivant rend
    `AUTH_FAILED`. Station reparkée ; la réactivation complète reste T018.
  - Validation WireGuard : `VPN_TYPE_STATION1=wireguard` +
    `SERVER_HOSTNAMES_STATION1=al46.nordvpn.com` → conteneur healthy,
    `Public IP 87.120.102.220 (Albania)`. Le manager réordonne ensuite les
    serveurs et réintroduit des `AUTH_FAILED` ; stations reparkées, pins WG
    conservés dans `.env`.
  - Parkage propre : `ip_registry.allocator.mode: off` + arrêt du superviseur
    `fresh_chain` et des six chasseurs. Aucun conteneur VPN en marche,
    `AUTH_FAILED` = 0, pool WARP actif.
  - **Résolution T018** : les six stations passées en WireGuard via
    `/api/vpn/station/{sid}/stack` + pins `al46/al73/al37/al36/al41/al45` +
    `/probe` → **6/6 `client_usable=true`**, egress `87.120.102.x`,
    `AUTH_FAILED` = 0, chat free `deepseek-v4.1-flash-free` → **HTTP 200**
    en 3,7 s.
- **P0.2 bis** : correction du couplage `FreeIPPool.enabled` /
  `on_request` / `get_status` avec `_vpn.enabled` — WARP reste actif quand
  Docker est parké.
- **P0.3** : 12 lignes WARP déclarées, **7 actives** (6 IPv6 + `WARP 1` canari
  IPv4). Les cinq autres IPv4 sont désactivées.
- Prochaine étape : P1 quota journalier / rotation / slot wait, puis
  réactivation NordVPN 1 → 6 après validation de session.

## 8. Mise à jour — santé stations et faux bad-IP (2026-11-09 09:45)

Deux régressions observées après T018 :

- les six stations passaient `egress-stale` ~15 min après la remise en route :
  le superviseur `fresh_chain` étant arrêté, plus aucune sonde ne rafraîchissait
  `last_ok` ;
- un 429 sur un lease neuf (aucun succès enregistré) condamnait l'IP 900 s en
  `bad-ip` ; la rafale de retry a brûlé les six stations d'un coup.

Correctifs :

- `_station_egress_probe_loop` : GET statique zéro quota via le SOCKS de chaque
  station toutes les 120 s, alimente `StationHealthRegistry` ; réglages
  `ip_rotation.station_health.probe_interval_s` et `probe_timeout_s` ;
- `bad_ip_min_429: 2` : premier 429 sans succès = blocage court
  `quota_ip_block_s` (300 s), quarantaine longue `bad_ip_ttl_s` seulement au
  deuxième refus sans succès ;
- `max_effective_age_s: 900` reste la borne, mais les sondes la rafraîchissent.

Vérification : `last_probe_age_s` ~20 s sur les six stations après restart,
gate `pytest -k "not docker"` complète OK, `config_audit --strict` 0 dead /
0 missing.

### 8.1 — Cause racine des 429 NordVPN : IP de session épuisée

Test direct du chemin free officiel via le SOCKS de chaque station (même
wire que le proxy) :

| Egress testé | Résultat |
|---|---|
| hôte direct `37.69.230.71` | **200** |
| WARP IPv6 `2a09:bac5:...` | **200** |
| NordVPN al46 `87.120.102.218` | 429 `FreeUsageLimitError` |
| NordVPN al109 `186.247.44.54` | 429 `FreeUsageLimitError` |
| NordVPN uk765 `194.88.100.2` | 429 `FreeUsageLimitError` |

Le quota est bien **par IP**, mais les IP de sortie NordVPN déjà vues sont
épuisées. Une reconnexion de la même station al46 a rendu une **nouvelle IP
`87.120.102.213` qui répond 200**. La rotation doit donc viser une IP
d'egress fraîche, pas seulement un serveur frais : `force-recreate` du
conteneur jusqu'à ce que le test d'inférence rende 200.

### 8.2 — Les six stations WARP répondent 200

Test direct du chemin free officiel via le SOCKS de chaque conteneur
`warp-1`…`warp-6` (une requête réelle par endpoint) :

| Station | Egress IPv6 | Résultat |
|---|---|---|
| WARP 1 | `2a09:bac5:32d5:be::13:24d` | **200** |
| WARP 2 | `2a09:bac5:32d1:26aa::3da:1a` | **200** |
| WARP 3 | `2a09:bac5:32d2:26b4::3db:3c` | **200** |
| WARP 4 | `2a09:bac5:32d5:14e6::215:b9` | **200** |
| WARP 5 | `2a09:bac5:32d3:26b4::3db:3a` | **200** |
| WARP 6 | `2a09:bac5:32d4:14f0::216:9c` | **200** |

Les six stations WARP sont donc client-utilisables en free ; les six
conteneurs NordVPN restent une couche d'appoint dont l'IP doit être
recyclée quand elle est épuisée.

Re-test après rotation WARP (09:29Z) : **6/6 HTTP 200** de nouveau, avec de
nouvelles IP (`2a09:bac5:32d0:14e6::215:88`, `...:32d3:1519::21a:1e`,
`...:32d2:150f::219:bf`, `...:32d2:be::13:294`). La rotation automatique
continue donc de fournir des IP saines. Validation streaming : HTTP 200,
`text/event-stream`, premier chunk 1,98 s, `[DONE]`.

