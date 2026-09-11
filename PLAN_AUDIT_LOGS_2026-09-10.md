# PLAN — Audit logs & DB : tous les problèmes / warnings

> Date : 2026-09-10 | Sources : `logs/debug.log` (+ `debug.log.1`), `logs/requests.db`, `logs/free_model_usage`, `config.yaml`, `.env`, `boot_prof.err`
> Méthode : scan exhaustif hors payloads (voir §1). Rien n'a été modifié.
> Outils réutilisables créés : `scripts/log_audit.py`, `scripts/db_audit.py`

---

## 1. Méthode et périmètre

| Source | Volume | Fenêtre couverte |
|---|---|---|
| `logs/debug.log` | 30,7 Mo, 136 329 lignes | 2026-09-09 23:47Z → 2026-09-10 08:00Z (~8 h, après rotation) |
| `logs/debug.log.1` | 50,0 Mo | fenêtre antérieure |
| `logs/requests.db` | 6,15 Go, 107 288 requêtes | 2026-08-28 → 2026-09-10 08:01Z |
| `logs/free_model_usage` | 128 875 lignes | 2026-08-28 → 2026-09-10 |

`logging` ne pose **aucun tag `ERROR`/`CRITICAL`** émis par l'application : le seul niveau réellement
produit est `WARNING` (488 occurrences, **un seul message distinct**). Les 199 occurrences du mot
`CRITICAL` dans le fichier sont du **texte utilisateur** présent dans des payloads loggés.

**103 795 lignes sur 136 329 (76 %) sont des dumps de payload** (SSE bruts, bodies convertis, schémas).
Elles sont exclues de tout comptage ci-dessous — sans ce filtre, les « familles de problèmes »
(`exception`, `timeout`, `fallback`, `429`, `5xx`) sont massivement polluées par le texte des prompts.

Fail 2 du fichier : `_handle_429` apparaît 663 fois alors que la DB ne compte que **2 échecs 429**
sur la même période → les marqueurs textuels seuls ne sont pas fiables, la DB est la source de vérité.

---

## 2. Synthèse

| # | Problème | Sévérité | État | Volume mesuré |
|---|---|---|---|---|
| 1 | Dashboard exposé sur le LAN (`DASHBOARD_TOKEN` absent) | **P0** | actif | warning à chaque boot |
| 2 | VPN `AUTH_FAILED` + boucle `degraded pin-loop` | **P0** | actif | 1 076 + 1 071 lignes / 8 h |
| 3 | Sorties vides silencieuses (`success=1`, `out=0`) | **P0** | actif | 2 939 cas / 7 j (10,9 % du xhigh) |
| 4 | `thinking` demandé mais `reasoning_content` absent | **P1** | actif | 488 WARNING / 8 h |
| 5 | DB 6,15 Go dont **1,47 Go d'espace mort** | **P1** | actif | freelist 360 533 pages |
| 6 | `request_body` = 3,8 Go stocké, post-mortem impossible | **P1** | actif | 1 333 + 657 lignes de troncature |
| 7 | Aucun tag `ERROR` dans les logs | **P1** | actif | 0 `ERROR`, 488 `WARNING` |
| 8 | 401 groupés sur modèles free | **P1** | sporadique | 103 échecs (dont 5 en 1 min le 09/09) |
| 9 | `403 DataPolicyError` avant forward | **P1** | actif | 6 events / 8 h |
| 10 | 22 640 réponses `400` sur jambe free | **P2** | en extinction | 0 aujourd'hui |
| 11 | 2 720 × HTTP 429 (faux 429 du proxy) | **P2** | **résolu** | concentré 03-04/09 |
| 12 | 453 × HTTP 403 (upstream) | **P2** | **résolu** | concentré 03-04/09 |
| 13 | Locks, `vpn_state*.json`, dumps orphelins | **P2** | actif | 6 locks, 7 états, 16 fichiers |
| 14 | Logs surdimensionnés (50 Mo/fichier) | **P2** | actif | 80 Mo pour ~32 500 lignes utiles |
| 15 | Travail non commité (543 lignes `opencode.py`) | **P2** | **résolu** | committé en `110d587` |
| 16 | Clutter racine (profils, logs d'import) | **P3** | actif | 11 fichiers |
| 17 | **Migration DB de fond échoue en silence** (`OperationalError: locked`) | **P1** | actif | 3 échecs, 0 retry |
| 18 | **Les tests écrivent dans le `debug.log` de production** | **P1** | actif | 27 lignes / 3 boots |

**Boot : résolu.** `[boot+0.0s] DB open` → `[boot+0.4s] token counters restored` → `[boot+1.8s]` au pire.
L'objectif « utilisable en < 60 s » est atteint (contre > 60 s avant).

---

## 3. Détail par problème

### P0-1 — Dashboard exposé sur le LAN

**Preuve** — `boot_prof.err` :
```
DASHBOARD_TOKEN not set while host is 0.0.0.0 — dashboard is open to LAN.
Set DASHBOARD_TOKEN in .env (header X-Dashboard-Token) for any non-localhost deployment.
```
`.env` ne contient aucune clé `DASHBOARD_TOKEN` (35 variables vérifiées, absente).
Code : `dashboard/api.py:184-189` (émission du warning), `dashboard/api.py:161` + `opencode.py:10787` (garde opt-in).

**Cause** — La garde est *opt-in* : sans variable, aucun contrôle d'accès n'est monté, et le bind par
défaut est `0.0.0.0`.

**Impact** — `/api/stats`, `/api/logs`, historique complet des requêtes (dont bodies) accessibles à
tout le réseau local. Les bodies contiennent du texte utilisateur et des URLs de workspace.

**Correctif**
1. Générer un token (`secrets.token_urlsafe(32)`) et l'ajouter dans `.env` : `DASHBOARD_TOKEN=…`.
2. Documenter le header `X-Dashboard-Token` dans `README.md` (l'usage est documenté dans le code,
   pas dans le README utilisateur).
3. Envisager `OPENCODE_HOST=127.0.0.1` si le dashboard n'est pas accédé depuis le LAN.

**Vérification** — `curl http://localhost:4000/api/stats` sans header → **401** ; avec header → 200.
Aucun warning `DASHBOARD_TOKEN not set` au boot suivant.

---

### P0-2 — VPN `AUTH_FAILED` récurrent et boucle `degraded pin-loop`

**Preuve** (fenêtre courante de 8 h, 1 076 + 1 071 lignes) :
```
639×  [vpn] AUTH_FAILED on de1158.nordvpn.com (failure #643, TTL 1440min) — fast-pin will skip it
 74×  [vpn] AUTH_FAILED on fr1329.nordvpn.com      73× fr1143    73× se657    72× fr1124
  4×  [vpn] AUTH_FAILED - identifiants NordVPN rejetés
645×  [vpn] s2 resets voie secondaire mais egress OK (stack openvpn) — degraded pin-loop, IP gardée
292×  [vpn] s3 …    125×  s6 …    5×  s4 …
643×  [vpn-watchdog] s2 egress OK — clearing stale error (auth_failed=False), set CONNECTED
```

**Cause** — Deux phénomènes distincts :
1. `de1158` est en échec 643 fois et reste **banni 24 h** (`TTL 1440min`). Le compteur continue de
   s'incrémenter : le registre d'échec n'est jamais remis à zéro après un succès, et un serveur
   définitivement invalide reste compté indéfiniment.
2. `s2`/`s3`/`s6` oscillent en `degraded pin-loop` : le watchdog observe un egress sain et remet
   `CONNECTED`, puis la voie secondaire reset à nouveau. Ce cycle se répète ~643× sur s2 en 8 h
   (~1 cycle / 45 s), ce qui consomme des cycles CPU et noie le log.

**Correctif**
1. Vérifier `vpn_configs/credentials.txt` : les 4 messages « identifiants NordVPN rejetés » signifient
   que le gate AUTH (commit `9af3934`) rejette les credentials — soit le fichier est vide/obsolète,
   soit le format est incorrect. C'est la cause racine des 1 076 `AUTH_FAILED`.
2. Réduire le TTL d'échec pour les serveurs en échec AUTH répété, ou plafonner le compteur :
   après N échecs consécutifs, retirer le serveur du pool pour la session au lieu de le re-tester.
3. Sur `degraded pin-loop` : logger **une fois** par transition d'état, avec un compteur, au lieu
   d'une ligne par cycle. Le passage `degraded → CONNECTED` ne doit pas être émis 643 fois.
4. Ajouter un garde anti-oscillation : si `degraded` alterne plus de K fois en T minutes, geler la
   station (backoff) au lieu de reboucler.

**Vérification** — Sur une fenêtre de 8 h après correctif : `AUTH_FAILED` sur `de1158` = 0,
lignes `degraded pin-loop` < 20, `restarts_1h = 0` dans les heartbeats watchdog.

---

### P0-3 — Sorties vides silencieuses (tiny output)

**Preuve** — `requests`, 7 derniers jours :

| `thinking` | `effort` | Requêtes | Sorties vides | Taux |
|---|---|---|---|---|
| adaptive | xhigh | 25 453 | **2 783** | **10,9 %** |
| adaptive | max | 1 983 | 248 | **12,5 %** |
| adaptive | high | 4 733 | 61 | 1,3 % |
| none | none | 14 130 | 50 | 0,4 % |

Détail des cas `adaptive/xhigh` : durée moyenne 2 832 ms, **durée max 283 828 ms (4 min 44 s)**,
input moyen 3 885 tokens.

**Cause** — Le proxy écrit `success=1` et `finish_reason=stop` même quand l'upstream renvoie une
réponse vide (`out=0`, `tools_used=[]`). Trois mécanismes, tous décrits et **non corrigés** dans
`PLAN_CORRECTION_TINY_OUTPUT_27F16308.md` (statut : « PLAN uniquement — rien n'est implémenté ») :
1. `_finalize_stream_tokens` (`opencode.py:7354-7402`) réconcilie sans garde de ratio.
2. `_save_and_log_request` (`opencode.py:6886`) écrit `success=1` inconditionnellement.
3. `_cb_record_success` (`opencode.py:2888`) **récompense** l'endpoint responsable de l'échec.

L'input moyen de 3 885 tokens **infirme que ce soit uniquement un problème de huge-context** : la
majorité des sorties vides se produisent sur des contextes normaux. Le cas documenté (597 k tokens)
est l'extrême, pas la règle.

**Impact** — Tours agent vides et silencieux : le client reçoit un tour « réussi » sans contenu ni
tool-call. Invisible côté monitoring (pas d'erreur, pas de 4xx/5xx, CB récompensé). C'est le problème
fonctionnel le plus coûteux de la liste.

**Correctif** — Implémenter `PLAN_CORRECTION_TINY_OUTPUT_27F16308.md`, en priorisant :
1. **P0 du plan** : stocker `ttfb_ms`, `est_input`, `actual_in`, `response_preview` (500 chars) et
   `original_count`/`dropped_chars` → rend l'incident rejouable.
2. Garde de cohérence : si `actual_in > seuil` **et** `out < 100` **et** `tools_used == []`, marquer
   `success=0` avec un `error` explicite, et **ne pas** récompenser le CB
   (ce point est indépendant du huge-context et doit être fait en premier — il couvre les 2 939 cas).
3. Ne pas synthétiser `finish_reason=stop` sur un stream dont le contenu utile est nul.

**Vérification** — `SELECT COUNT(*) FROM requests WHERE tokens_output=0 AND success=1` sur 7 jours
doit tomber à ~0 ; les cas restants doivent porter un `error` non nul.

---

### P1-4 — `thinking` demandé, `reasoning_content` absent

**Preuve** — Le **seul** `WARNING` applicatif du log, 488 fois :
```
[stream-oai] WARNING: thinking requested (type=adaptive, effort=xhigh) but upstream returned no reasoning_content
```

**Cause** — L'upstream accepte `reasoning: {effort: xhigh}` mais ne renvoie aucun `reasoning_content`.
Le proxy le détecte et avertit, mais ne dégrade pas la requête (pas de retry en effort inférieur).

**Corrélation** — Les efforts à plus fort taux de sorties vides sont exactement `xhigh` (10,9 %) et
`max` (12,5 %), contre 1,3 % en `high`. Les deux problèmes ont probablement la même cause amont :
pousser `xhigh` sur un modèle qui ne le supporte pas.

**Correctif**
1. Vérifier les caps par modèle (`config.yaml`, commit `4afcc22` « caps issus de re-sondes ») :
   `xhigh` est-il légitime sur les modèles réellement servis ?
2. Sur `no reasoning_content` détecté : retenter **une fois** en effort inférieur (`high`) plutôt que
   de retourner un tour vide silencieux.
3. Passer ce message en `WARNING` **agrégé** (compteur par modèle) pour ne pas saturer le log.

**Vérification** — Compteur `thinking_missing` dans `logs/debug.log` sur 24 h après correctif ;
taux de sorties vides `xhigh` aligné sur celui de `high` (< 2 %).

---

### P1-5 — Base SQLite : 6,15 Go dont 1,47 Go d'espace mort

**Preuve** — `PRAGMA` sur `logs/requests.db` :

| Métrique | Valeur |
|---|---|
| Taille fichier | 6,15 Go (6 148 Mo) |
| `page_count` × `page_size` | 1 425 279 × 4 096 = **5,84 Go** |
| `freelist_count` | **360 533 pages = 1 477 Mo (25 %)** |
| Lignes `requests` | 107 288 |
| Lignes `free_model_usage` | 128 875 |
| `request_body` (somme) | **3 822 Mo** |
| `response_body` (somme) | 15,8 Mo |
| WAL en attente | 4,07 Mo |
| `mmap_size` configuré | 268 435 456 (256 Mo) |

**Cause** — `database.weekly_purge_days: 90` purge des lignes, mais SQLite ne rend jamais l'espace au
système sans `VACUUM` : 25 % du fichier est de l'espace libéré non réclamé. Le poids vient
intégralement de `request_body` (3,8 Go pour 15,8 Mo de réponses).

**Correctif**
1. `VACUUM INTO 'logs/requests-compact.db'` puis bascule (non destructif, relançable) — en
   arrière-plan uniquement, jamais au boot (contrainte projet respectée).
2. Réduire `MAX_BODY_STORAGE` (`observability/db.py:34`, actuellement 100 000) pour les corps de
   requête et/ou stocker une **empreinte + queue** au lieu du corps complet (cf. P1-6).
3. Rotation mensuelle `requests-YYYY-MM.db` (déjà identifiée comme cible dans le plan boot Phase 5) :
   à 107 k requêtes en 13 jours, le fichier double tous les ~2 mois.
4. Vérifier que le `wal_checkpoint_interval: 3600` s'exécute réellement (WAL à 4 Mo).

**Vérification** — `PRAGMA freelist_count` < 5 % de `page_count` après VACUUM ; taille < 4 Go.

---

### P1-6 — Post-mortem impossible : troncature des bodies

**Preuve** — 1 333 lignes `... [<N> chars truncated]` + 657 lignes
`[schema] truncate description <N>><N> model='muse-spark-1.x-contributor'`.

`observability/db.py:34` (`MAX_BODY_STORAGE=100_000`) et `observability/db.py:41-79`
(`truncate_body_for_storage`, garde `messages[:2]` = **la tête**) : on conserve le system-prompt et
on perd la queue — donc la vraie question utilisateur et les derniers résultats d'outils.

**Impact** — Tout incident (dont les sorties vides P0-3) est non rejouable. C'est le prérequis du
plan tiny-output pour cette raison.

**Correctif** — Mode « huge body » : conserver **tête bornée (system + tools) + QUEUE (N derniers
messages)** avec `original_count` et `dropped_chars`, au lieu de la tête seule. Migration additive
NULL-safe.

**Vérification** — Sur une requête > 100 k chars : `dropped_chars > 0` **et** le dernier message
utilisateur présent dans `request_body`.

---

### P1-7 — Aucun niveau `ERROR` dans les logs

**Preuve** — Sur 136 329 lignes : `WARNING: 488`, `ERROR: 0`, `CRITICAL applicatif: 0`.
Toutes les erreurs réelles (429, 403, 401, 400, `All connection attempts failed`,
`getaddrinfo failed`) sont écrites en texte libre **sans tag de niveau**. Les 199 `CRITICAL`
détectés sont du texte utilisateur dans des payloads.

**Impact** — Aucune supervision par niveau n'est possible : un `grep ERROR` sur `debug.log` retourne
0 alors que la DB enregistre 3 369 échecs. Les faux positifs `CRITICAL` rendent le filtrage inverse
tout aussi inutilisable.

**Correctif**
1. Émettre un tag `[ERROR]`/`[CRITICAL]` sur tous les chemins d'échec (refus free, 4xx/5xx upstream,
   échec de connexion, exception).
2. **Ne jamais écrire de payload brut dans `debug.log`** (76 % du volume, et source des faux niveaux) :
   logger un résumé (longueur, hash, modèle) et garder le payload dans les dumps JSON dédiés.
3. Ajouter un test qui échoue si une ligne de `debug.log` dépasse N caractères ou contient un
   marqueur de payload.

**Vérification** — `grep -c ERROR logs/debug.log` après une journée ≈ nombre d'échecs DB ;
`grep -c CRITICAL` = 0 hors incidents réels ; volume du log divisé par ≥ 3.

---

### P1-8 — 401 groupés sur modèles free

**Preuve** — 103 échecs `HTTP 401`, `mimo-v2.5` en tête (84×, du 28/08 au 09/09), puis une **rafale
groupée le 2026-09-09 14:54** touchant 5 modèles free distincts dans la même minute :
`kimi-k2.7` (14:54:08), `hy3-free` (14:54:30), `laguna-s-2.1-free` (14:54:32), `x-preview-f-free`
(14:54:34), `ox-alpha-free` (14:54:36).

**Cause** — Une rafale simultanée sur des modèles différents indique une **authentification morte à
l'instant T** (clé/route/workspace), pas des modèles individuellement invalides.

**Correctif**
1. `key_pause_401_sec: 3600` existe déjà : vérifier qu'il s'applique bien à ces cas (les 5 modèles
   ont échoué à 2 s d'intervalle → la pause n'a pas coupé la rafale).
2. Distinguer 401 « clé invalide » (pause longue) de 401 « transitoire » (retry court) dans le log.
3. Vérifier `OPENCODE_API_KEY` / `OPENCODE_GO_AUTH_COOKIE` (585 chars — un cookie long est suspect
   de troncature ou d'expiration).

**Vérification** — Aucune rafale de 401 multi-modèles sur 7 jours.

---

### P1-9 — `403 DataPolicyError` avant forward (actif)

**Preuve** — 6 events dans la fenêtre de 8 h, groupés par 2 à chaque boot :
```
[datapolicy-guard] 403 DataPolicyError → garde pré-forward (pas de retry)
  url=https://opencode.ai/workspace/wrk_01KQQG13W80W15YQPS4CZGY9FM/go
[datapolicy-guard] 403 DataPolicyError → garde pré-forward (pas de retry)
  url=opt-in requis (URL non extraite du body)
```

**Cause** — Deux motifs : une URL de workspace connue, et un cas où **l'URL n'est pas extraite du
body** (`opt-in requis`). Le second est un angle mort : la garde se déclenche sans pouvoir nommer la
cible.

**Correctif**
1. Le motif « URL non extraite du body » se produisant 2× par boot, vérifier s'il s'agit d'une
   requête de warmup/sondage interne et l'exclure explicitement de la garde.
2. Sinon, améliorer l'extraction d'URL (le body analysé diffère peut-être du format attendu).
3. Ces 6 events sont en `WARNING` implicite → leur donner un niveau explicite (cf. P1-7).

**Vérification** — Plus de ligne `URL non extraite du body` au boot ; garde toujours effective sur
`wrk_01KQQG13W80W15YQPS4CZGY9FM`.

---

### P2-10 — 22 640 réponses `400` sur la jambe free (en extinction)

**Preuve** — `free_model_usage` : 103 964 `200`, **22 640 `400`**, 2 168 `429`, 54 `502`, 49 `503`,
8 `500`, 1 `401`.

| Modèle free | `400` | Période |
|---|---|---|
| `muse-spark-1.2-contributor-free` | 17 101 | 28/08 → **04/09 21:11** (terminé) |
| `muse-spark-1.3-contributor-free` | 5 299 | 03/09 → 09/09 17:49 |
| `deepseek-v4-flash-free` | 234 | 02/09 → 09/09 |
| `mimo-v2.5-free` | 6 | 28/08 → 07/09 |

Pics journaliers : 8 650 (01/09), 3 795 (03/09), 3 122 (29/09), 1 104 (04/09), 353 (05/09),
164 (07/09), 57 (09/09), **0 le 10/09**.

**Cause probable** — Le commit `4b4848b` « fix(schema): transpile `\p{Cc|Cf|Zl|Zp}` patterns au lieu
du 400 sur jambe Responses » traite exactement ce symptôme. La décroissance vers 0 est cohérente
avec un correctif déployé.

**Correctif** — Aucun correctif actif requis. Nettoyer les 7 dumps `logs/free400_msg_*.json`
(0,15–0,25 Mo chacun) qui datent du 09/09, et surveiller une éventuelle régression via
`free_model_usage WHERE status=400` (alerte si > 50/jour).

**Vérification** — `status=400` = 0 sur 7 jours consécutifs.

---

### P2-11 — 2 720 × HTTP 429 : **faux 429 fabriqués par le proxy** (résolu)

**Preuve** — 2 720 des 3 369 échecs totaux (81 %) sont `HTTP 429`, concentrés :
1 500 le 04/09 et 1 138 le 03/09, quasi exclusivement sur `muse-spark-1.2-contributor`.
Répartis sur les 6 stations (628/580/457/386/338/305) → **pas** une station en limite.
`free_status = NULL` pour 2 700 des 2 720 → le 429 n'a jamais vu de 429 upstream.
Depuis le 05/09 : 2 échecs 429 (07/09) seulement.

**Cause** — Confirmée par `PLAN_CORRECTION_FAUX_429.md` : tout échec free non-200 était ré-étiqueté
`FreeQuotaExhausted` → 429 « quota épuisé » fabriqué, alors que l'upstream répondait 503.
Plan marqué **implémenté le 2026-09-05**.

**Correctif** — Aucun. Vérifier que le lot A (refus véridique, statut et `Retry-After` réels
propagés) est bien en place : `strict_free` est passé à `false` (`config.yaml:280`), ce qui est
cohérent avec la correction.

**Vérification** — `SELECT COUNT(*) FROM requests WHERE error='HTTP 429' AND free_status IS NULL`
doit rester à 0.

---

### P2-12 — 453 × HTTP 403 upstream (résolu)

**Preuve** — 232 le 03/09 + 122 le 04/09 sur `muse-spark-1.2-contributor`, plus 88 le 28/08.
Concentré sur les fenêtres 11h-12h et 15h-18h. Aucun 403 depuis le 04/09 14h.

**Correctif** — Aucun. Le modèle `muse-spark-1.2-contributor` cumule 3 141 échecs toutes causes
confondues : c'est le point noir historique. Vérifier qu'il est toujours une route active pertinente
(`GET /v1/models`).

**Vérification** — `error='HTTP 403'` = 0 sur 7 jours.

---

### P2-13 — Artefacts orphelins (locks, états VPN, dumps)

**Preuve** — dans `logs/` :

| Type | Fichiers | Détail |
|---|---|---|
| Locks d'instance | 6 | `opencode-4000.lock` (10/09 00:01), `-4001` (09/09 14:53), **`-4002` (25/08)**, **`-4010` (26/08)**, **`-4101` (07/09)**, **`-4102` (07/09)** |
| États VPN | 7 + 5 `.bak` | `vpn_state.json`, `vpn_state{2..7}.json` — `state_file` + `state_file_2` déclarés dans `config.yaml:274`, donc 5 fichiers au-delà du contrat |
| Dumps 400 | 7 | `free400_msg_*.json`, 09/09 |
| Fichiers vides | 2 | `paused_keys.yaml` (0 o), `shared_rotation.json` (0 o) |

**Cause** — Les locks des ports 4002/4010/4101/4102 correspondent à des instances lancées puis
abandonnées (25/08 → 07/09) ; les `vpn_state{4,5,6,7}.json` sont des vestiges d'instances
multi-station antérieures (commit `febea1e` « resync station_count GUI/API/persist (bloque sur 2) »).
Un `paused_keys.yaml` vide de 0 octet à 09:53 pendant qu'on compte 252 lignes évoquant des pauses
mérite vérification.

**Correctif**
1. Purger les locks des ports non actifs (4002, 4010, 4101, 4102) — vérifier d'abord qu'aucun process
   ne les détient (`Get-NetTCPConnection`).
2. Purger `vpn_state{4..7}.json` + `.bak`, ne garder que les fichiers réellement référencés par
   `config.yaml`.
3. Supprimer les 7 `free400_msg_*.json` une fois P2-10 clos.
4. Ajouter un nettoyage au boot (fichiers dont le port ne correspond à aucune instance vivante).

**Vérification** — `logs/` ne contient plus que les états référencés en config + le lock de
l'instance courante.

---

### P2-14 — Logs surdimensionnés

**Preuve** — `debug.log` = 30,7 Mo, `debug.log.1` = 50,0 Mo. `config.yaml:445`
`debug.max_size: 52428800` (50 Mo). 76 % du contenu est du payload : **~62 Mo de logs pour
~32 500 lignes exploitables**.

**Correctif**
1. Arrêter le log de payload (cf. P1-7 correctif 2) — c'est le gain principal, ×4 au minimum.
2. Baisser `debug.max_size` à 10 Mo et `log_lines_max` (`200`) selon l'usage réel du dashboard.
3. Vérifier que la rotation (`dashboard/display.py:111`) conserve bien l'historique utile sans
   saturer le disque.

**Vérification** — `debug.log` < 10 Mo après 24 h d'exploitation normale.

---

### P2-15 — Travail non commité → **RÉSOLU pendant l'audit**

**Constat initial** — `git diff --stat` : 7 fichiers, **498 insertions / 152 suppressions**, dont
**`opencode.py` : 543 lignes modifiées**. Non suivis : `tests/test_boot_db_phase2.py`.

**Évolution en cours d'audit** — Un processus concurrent a travaillé dans le même dépôt pendant
l'analyse : il a créé un stash (`boot-speed`), remis l'arbre à `HEAD`, puis **committé** le tout sous
`110d587 perf(boot): chemin import vers listen non-bloquant (DB, tiktoken, rich, docker)`. L'arbre
est désormais **propre** (`git status --porcelain` ne liste plus que les fichiers non suivis créés
par cet audit).

Effet secondaire observé : pendant la fenêtre de stash (≈ 10:13:37 → 10:14:37), les trois fichiers
produits par cet audit (`PLAN_AUDIT_LOGS_2026-09-10.md`, `scripts/log_audit.py`, `scripts/db_audit.py`)
ont temporairement disparu du disque, avant d'être restaurés au `stash pop`. **Aucune perte.**

**Action restante** — Committer les trois fichiers non suivis de cet audit s'ils doivent être
conservés (`scripts/log_audit.py` et `scripts/db_audit.py` sont réutilisables en régression continue).

**Leçon opérationnelle** — Deux agents ont modifié le même dépôt simultanément sans coordination.
Toute analyse produisant des références `fichier:ligne` peut être invalidée en cours de route — d'où
la re-vérification systématique des ancres faite en §5 (toutes confirmées sur `110d587`).

---

### P3-16 — Clutter racine

**Preuve** — 11 fichiers de diagnostic à la racine, sans lien avec le code :

| Fichier | Taille |
|---|---|
| `boot.prof` | 742 Ko |
| `_tmp_req.json` | 96 Ko |
| `_audit_dump1.txt` | 44 Ko |
| `importtime*.log` (5 fichiers) | ~200 Ko |
| `ft.log`, `hx.log`, `it3.log`, `it_trace.log` | ~130 Ko |
| `boot_prof.err`, `boot_top.txt` | 4 Ko |

**Correctif** — Le commit `0da8849` annonce « ignore dumps transients » : vérifier que `.gitignore`
couvre bien ces motifs, puis supprimer les fichiers. Les profils de boot ont rempli leur rôle
(boot désormais à < 2 s).

---

### P1-17 — La migration DB de fond échoue en silence

**Preuve** — 3 occurrences, dont une aujourd'hui :
```
[2026-09-10 08:01:08Z]   [boot+0.3s] db migrate+canary done (bg)
[2026-09-10 08:01:08Z]   [db] migrate+canary bg FAILED: OperationalError: locked
```

**Cause** — `_db_migrate_bg` (`opencode.py:474-501`) prend `_db_commit_lock` puis exécute
`migrate_and_canary(_conn)`. La migration doit poser des `ALTER TABLE` + index, ce qui exige un verrou
**exclusif** — or le writer de fond (`commit_interval: 5`) tient la connexion partagée. Deux
conséquences graves :
1. Le `except Exception` ligne 500 écrit l'échec en `_debug` **uniquement** — pas de `_log`, donc
   **invisible** quand `OPENCODE_DEBUG` est coupé.
2. Le docstring annonce « warning différé au prochain boot » (ligne 478) : **ce report n'existe pas
   dans le code**. L'échec est simplement perdu. Aucun retry.

Contradiction dans le même bloc : `log_boot_phase("db migrate+canary done (bg)")` (ligne 499) est émis
**avant** l'échec → le log affirme « done » puis « FAILED » à la même seconde.

**Impact** — Si les `ALTER` de la phase 2 ne passent pas, le schéma reste partiel : des colonnes et
index attendus restent absents. Aucune alerte. C'est exactement le type de panne silencieuse qui
explique pourquoi certains champs de télémétrie sont vides.

**Correctif**
1. Ajouter un **retry avec backoff** (ex. 3 tentatives espacées de 5/15/30 s) : `locked` est
   transitoire par nature, il suffit d'attendre la fin du batch writer.
2. Passer l'échec définitif en `_log` (visible sans DEBUG) **et** l'exposer dans `/readyz`
   (clé `degraded["db_migrate"]`).
3. Corriger l'ordre : n'émettre `log_boot_phase("... done")` **qu'après** succès.
4. Implémenter le report annoncé (ou supprimer la phrase du docstring).

**Vérification** — Aucune ligne `migrate+canary bg FAILED` sur 10 boots consécutifs ; en cas d'échec
forcé, `/readyz` renvoie `degraded.db_migrate` non nul.

---

### P1-18 — Les tests écrivent dans le `debug.log` de production

**Preuve** — 9 lignes `[vpn-canary]` apparaissent à **chaque boot**, exactement 2 s après :
```
[07:39:53] boot → [07:39:55] validation egress WireGuard (test)... / bring-up échoué: boom compose
[07:50:35] boot → [07:50:37] idem (×2)
[08:02:20] boot → [08:02:22] idem (×2)
```
Or `"boom compose"` **n'existe nulle part dans le code de production** : c'est une fixture de test
(`tests/test_audit_routabilite_pc.py:373`, `raise RuntimeError("boom compose")`) et le motif
`reason="test"` est passé uniquement par `tests/` (lignes 378, 399).

**Cause** — `dashboard/display.py` garde un état **module-global** (`_debug_file`, `_debug_file_path`,
lignes 45-46). Et `opencode.py:399` fait, **à l'import** :
```python
set_debug_log_file(os.path.join(LOG_DIR, "debug.log"))   # → logs/debug.log réel
attach_module_logger("vpn_manager")                       # ligne 406
```
Donc tout process qui importe `opencode` (donc **pytest**, qui collecte les tests important le
module) **réoriente le logger `vpn_manager` vers le fichier de production**. Les tests qui isolent
correctement (`test_vpn_logging.py:52`, `test_audit_routabilite_pc.py:552`) redirigent vers `tmp_path`
— mais uniquement **le leur** : dès qu'un autre test émet sur `vpn_manager` **avant** la redirection,
la ligne part dans `logs/debug.log`. `caplog.at_level(..., logger="vpn_manager")` **capture sans
empêcher** la propagation vers le `FileHandler` déjà attaché.

**Impact**
1. Le `debug.log` de production contient des événements qui **ne se sont jamais produits**
   (`boom compose`, `WireGuard SANS EGRESS`) — tout diagnostic VPN basé sur ce fichier est faux par
   construction. C'est directement ce qui a fait croire à 6 cycles de panne canari inexistants.
2. Les échecs de test sont noyés dans le log d'exploitation.
3. Toute analyse de tendance sur les événements VPN est corrompue par ce bruit.

**Correctif**
1. **Ne pas appeler `set_debug_log_file` à l'import** (`opencode.py:399`). Le déplacer dans la
   fonction de démarrage réelle (`main()` / lifespan), après le parse des arguments.
2. Dans `conftest.py`, ajouter une fixture **autouse** qui force `set_debug_log_file(tmp_path/"debug.log")`
   pour toute la session et **détache** les handlers de `vpn_manager` / `free_ip_pool`.
3. Garde-fou : si `PYTEST_CURRENT_TEST` ou `pytest` est dans `sys.modules`, refuser d'ouvrir
   `logs/debug.log` et lever une erreur explicite.
4. Purger les 27 lignes contaminées du `debug.log` courant une fois le correctif posé.

**Vérification** — Lancer la suite complète puis `grep -c 'boom compose' logs/debug.log` → **0** ;
mtime de `logs/debug.log` inchangé après un run pytest (hors tests d'intégration explicites).

---

## 4. Faux positifs — à ne PAS traiter

Ces éléments ressortent d'un scan par mots-clés mais **ne sont pas des problèmes** :

| Signal | Volume | Réalité |
|---|---|---|
| `CRITICAL` | 199 | Le mot apparaît dans le **texte utilisateur** (payloads loggés). 0 `CRITICAL` applicatif. |
| `exception` | 1 634 | Le mot apparaît dans les prompts + dans `clearing stale error`. 0 `Traceback` dans tout le fichier. |
| `timeout` | 1 302 | Provient de `x-stainless-timeout: NOT_GIVEN` dans les headers loggés, pas d'un timeout réel. |
| `429` | 4 | Les vrais échecs 429 sont dans la DB : 2 sur la fenêtre. |
| `5xx` | 4 265 | Nombres dans les deltas SSE (`sequence_number`, `output_index`). |
| `fallback` | 638 | Texte de prompt. |
| `context_length`, `overload`, `JSONDecode`, `CancelledError` | 0–2 | Non significatif. |
| `route: opencode.py` | — | Aucun `Traceback` dans les 136 329 lignes : pas de crash non géré. |

**Recommandation structurelle** : ces faux positifs disparaissent si les payloads cessent d'être
écrits dans `debug.log` (correctif P1-7.2). C'est la correction à plus fort effet de levier de tout
ce plan : elle règle simultanément P1-7 (niveaux fiables), P2-14 (taille), et rend tout futur audit
de log exploitable.

---

## 5. Catalogue complet des corrections à implémenter

Chaque correction est ancrée sur un `fichier:ligne` **vérifié**. **25 corrections**, groupées en 6 lots.
La colonne « Effort » est une estimation de travail effectif.
Ancres re-vérifiées sur l'arbre courant (HEAD `110d587`) : `opencode.py` fait 13 993 lignes et les
lignes 399 / 474 / 1875 / 3167 / 7165 / 7633 / 9521 correspondent exactement à ce qui est décrit.

### Lot 1 — Reprise de l'existant et sécurité

#### C1 — Sécuriser le dashboard : définir `DASHBOARD_TOKEN` (réf. P0-1)
- **Fichiers** : `.env` (ajout), `README.md` (documentation).
- **Problème** : `.env` ne contient aucune clé `DASHBOARD_TOKEN` (35 variables vérifiées) alors que le
  bind par défaut est `0.0.0.0`. Warning émis à **chaque** boot.
- **Action** : générer `python -c "import secrets;print(secrets.token_urlsafe(32))"` et ajouter
  `DASHBOARD_TOKEN=<valeur>` dans `.env`. Documenter le header `X-Dashboard-Token` dans le README.
  **Le code est déjà prêt** (`dashboard/api.py:161` lecture, `:219` `_check_dashboard_token`,
  `:184-189` warning ; `opencode.py:10787` garde) — **aucune modification de code requise**.
- **Test** : `curl -s -o NUL -w "%{http_code}" http://localhost:4000/api/stats` → `401` ;
  avec `-H "X-Dashboard-Token: <token>"` → `200`.
- **Effort** : 15 min.

#### C2 — Restreindre le bind si le dashboard n'est pas utilisé à distance (réf. P0-1)
- **Fichier** : `.env` (`OPENCODE_HOST`).
- **Action** : si le dashboard n'est consulté que localement, passer `OPENCODE_HOST=127.0.0.1`.
  Sinon conserver `0.0.0.0` **avec C1 obligatoirement appliqué**.
- **Test** : `Get-NetTCPConnection -LocalPort 4000` → `LocalAddress` = `127.0.0.1`.
- **Effort** : 5 min. **Dépend de** : C1.

#### C3 — Vérifier l'état du dépôt après le commit concurrent (réf. P2-15) — **déjà résolu**
- **Constat** : pendant cet audit, un processus concurrent a stashé puis **committé** le travail en
  attente sous `110d587 perf(boot): chemin import vers listen non-bloquant (DB, tiktoken, rich, docker)`.
  L'arbre est désormais **propre** : les 498 insertions / 152 suppressions ne sont plus en suspens.
- **Action** : plus rien à committer. En revanche, décider du sort des trois fichiers créés par cet
  audit, actuellement **non suivis** : `PLAN_AUDIT_LOGS_2026-09-10.md`, `scripts/log_audit.py`,
  `scripts/db_audit.py` (utiles en régression continue → à committer).
- **Test** : `git status --porcelain` ne montrant que les fichiers volontairement ajoutés.
- **Effort** : 15 min.

### Lot 2 — VPN

#### C4 — Corriger les identifiants NordVPN (réf. P0-2) — **cause racine**
- **Fichier** : `vpn_configs/credentials.txt` (référencé par `config.yaml:179`).
- **Problème** : 4 événements `AUTH_FAILED - identifiants NordVPN rejetés` (`vpn/manager.py:1889`,
  `2626`) signalent un rejet d'authentification **global**, pas un serveur défaillant. C'est la cause
  racine des 1 076 `AUTH_FAILED`.
- **Action** : vérifier que le fichier contient `user` puis `pass` sur 2 lignes, sans BOM ni CRLF
  parasite, avec des identifiants valides.
- **Test** : `docker logs <conteneur>` après un pin manuel → aucune ligne `AUTH_FAILED`.
- **Effort** : 30 min (diagnostic inclus). **Sans ce correctif, C5/C6/C7 ne masquent que le symptôme.**

#### C5 — Plafonner le compteur d'échecs par hôte (réf. P0-2)
- **Fichier** : `vpn/manager.py:6826-6852` (`_record_auth_failure`).
- **Problème mesuré** : `de1158.nordvpn.com` atteint `failure #643` et reste banni 24 h
  (`bad_ttl_max: 1440`, lignes 811-812). Le compteur n'est jamais remis à zéro après un succès.
- **Action** :
  1. Plafonner `entry["failures"] = min(entry["failures"] + 1, 10)` pour garder le log lisible.
  2. Au-delà de 5 échecs **dans la session**, retirer l'hôte de la liste candidate au lieu de le
     re-tester : un hôte dont l'auth est rejetée ne guérira pas dans la session.
  3. Baisser `bad_ttl_max` de 1440 à 360 min dans `config.yaml` (24 h est excessif pour un hôte
     transitoirement en échec).
- **Test** : `tests/test_graceful_aurora.py` (LOT K déjà présent, ligne 525+) — ajouter un cas
  « 10 échecs → compteur plafonné, hôte exclu du pool ».
- **Effort** : 1 h.

#### C6 — Dédupliquer le log de dégradation `pin-loop` (réf. P0-2)
- **Fichier** : `vpn/manager.py:2588-2593`.
- **Problème mesuré** : 645 lignes identiques pour s2 en 8 h (~1 toutes les 45 s).
- **Action** : n'émettre le `logger.warning` que **sur transition d'état** (`CONNECTED → DEGRADED`) ;
  mémoriser `self._degraded_logged` et passer les répétitions en `logger.debug` avec un compteur.
- **Test** : appeler la branche 50× sans changement d'état → **1** seule ligne WARNING.
- **Effort** : 30 min.

#### C7 — Dédupliquer `clearing stale error` du watchdog (réf. P0-2)
- **Fichier** : `vpn/manager.py:7827-7842`.
- **Problème mesuré** : 643 lignes `s2 egress OK — clearing stale error … set CONNECTED` en 8 h, car
  le watchdog tourne toutes les 20 s (`watchdog_interval: 20`).
- **Action** : ne logger en WARNING que si l'état **changeait** (`self._status != VPNState.CONNECTED`
  ou `self._error` non vide) ; sinon `DEBUG`. Le bloc remet déjà `_auth_failed`, `_server_issue`,
  `_restart_churn` à False — seul le log est à conditionner.
- **Test** : 100 ticks consécutifs avec egress OK → 1 seule ligne WARNING.
- **Effort** : 30 min.

#### C8 — Garde anti-oscillation `DEGRADED ↔ CONNECTED` (réf. P0-2)
- **Fichier** : `vpn/manager.py` (boucle watchdog, 7809-7842).
- **Action** : compter les allers-retours par fenêtre glissante ; au-delà de 5 transitions en 10 min,
  geler la station (backoff) au lieu de reboucler, et exposer `oscillation_count` dans le heartbeat
  (le bloc `vpn/manager.py:4103-4116` expose déjà `auth_failed`, `restarts_1h`).
- **Test** : simuler 6 transitions en 10 min → la 6e gèle la station, un seul log agrégé.
- **Effort** : 2 h. **Dépend de** : C6, C7.

#### C9 — Rendre auditable le verdict `_check_auth_failed` (réf. P0-2)
- **Fichier** : `vpn/manager.py:6551-6610` (comparaison `rfind` ligne 6577).
- **Action** : la logique `Initialization Sequence Completed` vs `AUTH_FAILED` est correcte sur le
  principe, mais `reset voie secondaire` et `egress OK` sont contradictoires et alimentent la boucle.
  Ajouter un log explicite du verdict (`stale` vs `live`) pour rendre la décision vérifiable.
- **Test** : cas « AUTH_FAILED puis INIT dans le même chunk » → verdict `stale`, pas d'incrément.
- **Effort** : 1 h.

### Lot 3 — Fiabilité fonctionnelle

#### C10 — Garde de cohérence sortie vide (réf. P0-3) — **la plus importante**
- **Fichiers** : `opencode.py:7165-7189` (`_save_and_log_request`), `opencode.py:7633-7681`
  (`_finalize_stream_tokens`).
- **Problème mesuré** : 2 939 cas / 7 j avec `tokens_output = 0` **et** `success = 1`. Le docstring
  ligne 7189 dit littéralement « Log success and save to DB with success=True » et la ligne 7224
  passe `success=True` **inconditionnellement**.
- **Action** :
  1. Calculer une condition de sortie vide : `final_out == 0` (ou `< 100` pour couvrir les
     micro-sorties) **et** `tools_used` vide **et** aucun bloc texte ni thinking émis.
  2. Si vraie → chemin d'échec (`success=False`) avec un `error` explicite du type
     `"empty upstream output (in=<final_in>, out=0, no tool call)"`.
  3. Dans `_finalize_stream_tokens`, ajouter un **garde de ratio** (lignes 7652-7676) : ne pas écraser
     `stream_out` par un `final_out` nul si `actual_usage` est incohérent.
- **Test** : test unitaire `_save_and_log_request` avec `out=0` → ligne DB `success=0` + `error` non
  nul ; test `_finalize_stream_tokens` avec `actual_usage={"completion_tokens": 0}` → ratio détecté.
- **Effort** : 3 h. **Ne dépend pas** du huge-context : couvre les 2 939 cas dont l'input moyen est de
  3 885 tokens.

#### C11 — Ne pas récompenser le circuit breaker sur sortie vide (réf. P0-3)
- **Fichiers** : `opencode.py:3167` (`_cb_record_success`), appelants `:9281`, `:11912`, `:12684`.
- **Action** : conditionner l'appel à la garde C10. Aujourd'hui un endpoint qui renvoie du vide est
  **récompensé**, donc le breaker le considère sain et continue de le préférer.
- **Test** : endpoint renvoyant systématiquement du vide → le breaker finit par l'ouvrir.
- **Effort** : 1 h. **Dépend de** : C10.

#### C12 — Dégradation d'effort sur `no reasoning_content` (réf. P1-4)
- **Fichiers** : `opencode.py:8331-8334` (stream), `opencode.py:9801-9805` (non-stream).
- **Problème mesuré** : 488 WARNING en 8 h, et `xhigh` (10,9 %) / `max` (12,5 %) ont un taux de
  sorties vides 8 à 10× supérieur à `high` (1,3 %) — cause probablement commune.
- **Action** :
  1. Quand `reasoning_block_idx is None and thinking_type != "none"`, retenter **une fois** avec
     `effort` abaissé d'un cran (`xhigh → high`) avant de rendre la main.
  2. Si le retry échoue aussi, retourner le résultat en logguant un WARNING **agrégé** (compteur par
     modèle) au lieu d'une ligne par requête.
  3. Vérifier les caps de `config.yaml` (commit `4afcc22`) : `xhigh` est-il légitime sur tous les
     modèles servis ?
- **Test** : mock upstream renvoyant 200 sans `reasoning_content` → exactement **2** appels, le 2e
  avec `effort=high`.
- **Effort** : 2 h.

#### C13 — Retry + visibilité sur la migration DB de fond (réf. P1-17)
- **Fichier** : `opencode.py:474-501` (`_db_migrate_bg`).
- **Problème mesuré** : 3 × `OperationalError: locked`, loggé en `_debug` uniquement, sans retry, et
  `log_boot_phase("... done (bg)")` (ligne 499) émis **avant** l'échec → le log affirme « done » puis
  « FAILED » à la même seconde.
- **Action** :
  1. Retry avec backoff : 3 tentatives à 5 / 15 / 30 s (`locked` est transitoire — attendre la fin du
     batch writer).
  2. Échec définitif → `_log` (visible sans `OPENCODE_DEBUG`) **et** `degraded["db_migrate"]` exposé
     dans `/readyz`.
  3. Déplacer `log_boot_phase("db migrate+canary done (bg)")` **après** le succès.
  4. Implémenter le « warning différé au prochain boot » annoncé au docstring ligne 478, ou retirer
     la phrase.
- **Test** : simuler un `OperationalError` aux 2 premières tentatives → succès à la 3e, une seule
  ligne de log finale.
- **Effort** : 1 h 30.

### Lot 4 — Observabilité (prérequis de tout audit futur)

#### C14 — Découpler le log de payload d'`OPENCODE_DEBUG` (réf. P1-7, P2-14) — **levier maximal**
- **Fichiers** : `opencode.py:9520-9521`, `opencode.py:10189`, autres `_debug()` de payload ;
  nouvelle clé sous `debug:` dans `config.yaml`.
- **Problème mesuré** : **103 795 lignes sur 136 329 (76 %)** sont des dumps de payload, parce que
  `.env` contient `OPENCODE_DEBUG=1` (`config/settings.py:398`). C'est la cause des 199 faux
  `CRITICAL`, de l'absence de niveaux fiables et des 62 Mo de fichiers.
- **Action** :
  1. Ajouter `config.yaml` → `debug: log_payloads: false` (défaut `false`).
  2. Garder les `_debug()` de payload derrière
     `if _cfg_settings.DEBUG and yaml_get("debug", "log_payloads", False)`.
  3. Les payloads restent disponibles à la demande via les dumps JSON dédiés.
- **Test** : en `DEBUG=1`, un message de payload **n'apparaît pas** dans `debug.log`.
- **Effet** : divise le log par ≥ 4 et supprime la cause racine des faux niveaux.
- **Effort** : 1 h.

#### C15 — Émettre de vrais niveaux `ERROR` / `CRITICAL` (réf. P1-7)
- **Fichiers** : chemins d'échec — refus free, 4xx/5xx upstream, échecs de connexion, exceptions.
- **Problème mesuré** : `grep ERROR` → **0** sur 136 329 lignes, alors que la DB enregistre 3 369
  échecs. Toutes les erreurs sont en texte libre sans tag de niveau.
- **Action** : router les échecs via `_log`/`logging.error` avec un préfixe normalisé (`[ERROR]` /
  `[CRITICAL]`) pour rendre la supervision par niveau possible.
- **Test** : `grep -c ERROR logs/debug.log` après une journée ≈ nombre d'échecs en DB (±10 %).
- **Effort** : 2 h. **Dépend de** : C14 (sinon les faux positifs persistent).

#### C16 — Isoler les tests du `debug.log` de production (réf. P1-18) — **critique**
- **Fichiers** : `opencode.py:399` (`set_debug_log_file` à l'import), `tests/conftest.py`.
- **Problème mesuré** : `boom compose` (fixture de `tests/test_audit_routabilite_pc.py:373`) apparaît
  dans le log de production **3× par boot** (07:39:55, 07:50:37, 08:02:22), car `set_debug_log_file`
  s'exécute **à l'import** (ligne 399) et `attach_module_logger("vpn_manager")` (ligne 406) attache le
  handler au fichier réel. `caplog.at_level(...)` capture **sans empêcher** la propagation.
- **Action** :
  1. Retirer `set_debug_log_file(...)` de la portée module (ligne 399) et l'appeler dans le démarrage
     réel (`main()` / lifespan).
  2. `conftest.py` : fixture **autouse** de session forçant un `debug.log` en `tmp_path` et
     **détachant** les handlers de `vpn_manager` / `free_ip_pool`.
  3. Garde-fou : si `PYTEST_CURRENT_TEST` est défini ou `pytest` dans `sys.modules`, refuser d'ouvrir
     `logs/debug.log` (erreur explicite).
  4. Purger les 27 lignes contaminées du `debug.log` courant.
- **Test** : suite complète puis `grep -c 'boom compose' logs/debug.log` → **0** ; mtime du fichier
  inchangé après pytest.
- **Effort** : 2 h. **Sans cela, tout diagnostic VPN tiré du log est faux par construction.**

#### C17 — Test de non-régression de l'hygiène du log (réf. P1-7, P2-14)
- **Fichier** : `tests/test_log_hygiene.py` (nouveau).
- **Action** : test échouant si (a) une ligne de `debug.log` dépasse N caractères, (b) un marqueur de
  payload est présent (`converted to openai`, `trying responses_sse convert`, `event type=`),
  (c) une ligne contient `boom compose` ou un motif de fixture de test.
- **Test** : le test lui-même ; vérifier qu'il échoue bien sur le log contaminé actuel.
- **Effort** : 1 h. **Dépend de** : C14, C16.

#### C18 — Troncature tête **+ queue** des bodies (réf. P1-6)
- **Fichier** : `observability/db.py:41-79` (`truncate_body_for_storage`).
- **Problème mesuré** : lignes 60 et 73 → `messages[:2]` = **la tête seule**. On garde le
  system-prompt, on perd la question utilisateur. 1 333 + 657 lignes de troncature.
- **Action** : conserver `messages[:2] + messages[-4:]` avec
  `{"_truncated": True, "original_count": len(messages), "dropped_chars": <calculé>}` ; ajouter
  `dropped_chars` au schéma (migration additive NULL-safe).
- **Test** : body de 300 k chars → `dropped_chars > 0` **et** dernier message utilisateur présent.
- **Effort** : 2 h.

#### C19 — Résoudre `URL non extraite du body` (réf. P1-9)
- **Fichiers** : garde `datapolicy-guard`, extraction d'URL.
- **Problème mesuré** : 2 lignes par boot — `[datapolicy-guard] 403 DataPolicyError → garde
  pré-forward (pas de retry) url=opt-in requis (URL non extraite du body)`.
- **Action** : identifier si la requête fautive est un warmup/sondage interne (l'exclure alors
  explicitement), sinon étendre l'extraction d'URL au format réellement reçu.
- **Test** : plus aucune ligne `URL non extraite du body` au boot ; garde toujours effective sur
  `wrk_01KQQG13W80W15YQPS4CZGY9FM`.
- **Effort** : 1 h 30.

### Lot 5 — Données

#### C20 — Récupérer les 1,47 Go d'espace mort (réf. P1-5)
- **Fichiers** : `observability/db.py:531-551` (`vacuum_if_needed`), `:697-724` (`weekly_maintain`),
  `opencode.py:2424-2465` (boucle hebdo).
- **Problème mesuré** : `freelist_count` = 360 533 pages = **1 477 Mo (25 % du fichier)**.
- **Constat important** : `weekly_maintain` (dimanche 03:00) exécute bien
  purge + `wal_checkpoint(TRUNCATE)` + `VACUUM` (lignes 705-713) — **mais aucune ligne
  `[db] maintenance hebdo OK` ni `DB VACUUM` n'apparaît dans les logs conservés**, alors que la purge
  du 06/09 aurait dû libérer des pages. Il faut donc **d'abord établir si le VACUUM s'exécute**.
- **Action** :
  1. **Diagnostiquer d'abord** : `vacuum_if_needed` avale toute exception
     (`except Exception: pass`, lignes 550-551) avec le commentaire « l'app hôte journalise via son
     wrapper » — or le wrapper `_db_vacuum_if_needed` (`opencode.py:564-578`) journalise en `_debug`,
     donc **invisible si DEBUG est coupé**, et `cleanup_old_bodies` ne l'appelle que si
     `deleted_rows > 0`. Rendre l'échec visible en `_log` pour savoir si le VACUUM est rejeté
     (typiquement `database is locked` en conflit avec `commit_interval: 5`).
  2. Une fois le diagnostic posé : exécuter `VACUUM INTO 'logs/requests-compact.db'` puis basculer —
     non destructif, relançable, **en arrière-plan uniquement** (contrainte projet : jamais au boot).
  3. Ne pas passer à un VACUUM quotidien (trop coûteux) : conserver l'hebdo une fois le conflit de
     verrou résolu.
- **Test** : `PRAGMA freelist_count` < 5 % de `page_count` ; taille < 4 Go.
- **Effort** : 2 h (diagnostic inclus). **Gain attendu : 1,47 Go.**

#### C21 — Réduire et faire tourner la base (réf. P1-5)
- **Fichiers** : `observability/db.py:34` (`MAX_BODY_STORAGE = 100_000`), config de rétention.
- **Problème mesuré** : `request_body` = 3 822 Mo contre 15,8 Mo de réponses ; 107 k requêtes en
  13 jours → le fichier double en ~2 mois.
- **Action** :
  1. Réduire `MAX_BODY_STORAGE` de 100 000 à 32 000 : le post-mortem utile tient dans C18
     (tête + queue), pas dans 100 k chars de tête.
  2. **Incohérence de rétention à trancher** : `_periodic_db_cleanup` (`opencode.py:2417`) appelle
     `cleanup_old_bodies` avec `delete_after_days = 30` (défaut, `opencode.py:581`) tandis que
     `weekly_maintain` purge à `weekly_purge_days = 90` (`config.yaml`). Deux politiques
     contradictoires — en aligner une seule.
  3. Implémenter la rotation mensuelle `requests-YYYY-MM.db` + `ATTACH`.
- **Test** : croissance hebdomadaire du fichier < 200 Mo.
- **Effort** : 4 h. **Dépend de** : C20.

#### C22 — Durcir la pause sur 401 groupés (réf. P1-8)
- **Fichier** : gestion `key_pause` (`config.yaml:419-421` : `default_pause: 60`, `max_pause: 600`,
  `key_pause_401_sec: 3600`).
- **Problème mesuré** : 5 modèles free distincts ont échoué en 401 en **28 secondes**
  (09/09 14:54:08 → 14:54:36) — la pause n'a pas coupé la rafale.
- **Action** :
  1. Vérifier que `key_pause_401_sec` s'applique bien à chacun de ces cas (une rafale à 2 s
     d'intervalle signifie que la clé n'était pas encore marquée en pause).
  2. Appliquer la pause **dès le premier** 401 sur la clé, avant de tenter les modèles suivants.
  3. Distinguer 401 « clé invalide » (pause longue) de 401 « transitoire » (retry court).
  4. Vérifier `OPENCODE_API_KEY` / `OPENCODE_GO_AUTH_COOKIE` (585 chars — cookie long, suspect
     d'expiration ou de troncature).
- **Test** : simuler 2 × 401 consécutifs → 1 seule requête upstream, clé en pause.
- **Effort** : 1 h 30.

### Lot 6 — Hygiène

#### C23 — Purger les artefacts orphelins (réf. P2-13)
- **Fichiers** : `logs/`.
- **Action** :
  1. Vérifier qu'aucun process ne détient les ports, puis supprimer `opencode-4002.lock`, `-4010`
     (26/08), `-4101`, `-4102` (07/09).
  2. Supprimer `vpn_state{4,5,6,7}.json` + `.bak` — seuls `state_file` et `state_file_2`
     (`config.yaml:274`) sont contractuels.
  3. Supprimer les 7 `free400_msg_*.json` (P2-10 clos).
  4. Vérifier `paused_keys.yaml` (0 octet) : si des clés sont en pause sans être persistées, le
     mécanisme de pause est inopérant entre redémarrages.
  5. Ajouter un nettoyage au démarrage : supprimer tout fichier dont le port ne correspond à aucune
     instance vivante.
- **Test** : `logs/` ne contient que les états référencés en config + le lock de l'instance courante.
- **Effort** : 45 min.

#### C24 — Archiver le clutter racine (réf. P3-16)
- **Fichiers** : racine du dépôt (11 fichiers).
- **Action** : vérifier que `.gitignore` couvre `boot.prof`, `boot_prof.err`, `boot_top.txt`,
  `importtime*.log`, `it*.log`, `ft.log`, `hx.log`, `_tmp_req.json`, `_audit_dump1.txt` (commit
  `0da8849` annonce « ignore dumps transients »), puis supprimer les fichiers.
- **Test** : `git status --porcelain` ne les listant plus.
- **Effort** : 15 min.

#### C25 — Ajuster la taille des logs (réf. P2-14)
- **Fichiers** : `config.yaml:442-446` (`debug.max_size: 52428800`, `log_lines_max: 200`).
- **Action** : après C14 (qui retire 76 % du volume), baisser `max_size` à **10 Mo** et ajuster
  `log_lines_max` selon l'usage réel du dashboard.
- **Test** : `debug.log` < 10 Mo après 24 h.
- **Effort** : 15 min. **Dépend de** : C14.

---

## 6. Ordre d'exécution recommandé

| Ordre | Correction | Nature | Effort | Dépendances |
|---|---|---|---|---|
| 1 | **C1 + C2** — token dashboard | sécurité (bloquant) | 20 min | — |
| 2 | **C4** — identifiants NordVPN | cause racine VPN | 30 min | — |
| 3 | **C13** — retry migration DB | panne silencieuse | 1 h 30 | — |
| 4 | **C16** — isoler les tests du log | fiabilité du diagnostic | 2 h | — |
| 5 | **C14** — découpler payload/DEBUG | levier maximal | 1 h | — |
| 6 | **C10 + C11** — garde sortie vide + CB | 2 939 cas | 4 h | C14 |
| 7 | **C6 + C7** — dédupliquer logs VPN | bruit / CPU | 1 h | — |
| 8 | **C15** — tags ERROR réels | supervision | 2 h | C14 |
| 9 | **C17** — test non-régression log | garde-fou | 1 h | C14, C16 |
| 10 | **C5 + C8 + C9** — VPN structurel | stabilité | 4 h | C4, C6, C7 |
| 11 | **C12** — dégradation effort | fiabilité | 2 h | — |
| 12 | **C18** — troncature tête + queue | post-mortem | 2 h | — |
| 13 | **C20** — VACUUM + diagnostic | **1,47 Go** | 2 h | — |
| 14 | **C19** — datapolicy URL | propreté | 1 h 30 | — |
| 15 | **C22** — pause 401 | robustesse auth | 1 h 30 | — |
| 16 | **C21** — rétention + rotation DB | croissance | 4 h | C20 |
| 17 | **C23 + C24 + C25** — hygiène | propreté | 1 h 15 | C14 |
| — | **C3** — état du dépôt | **déjà résolu** | 15 min | — |

**Chemin critique** : C1/C2 → C4 → C13 → C16 → C14 → C10/C11.
Les six premières étapes (~5 h 20 cumulées) suppriment l'essentiel du risque et rendent le reste
mesurable. Effort total estimé : **~32 h**.

---

## 7. Vérifications de fin

| # | Contrôle | Cible | Correction |
|---|---|---|---|
| 1 | `curl /api/stats` sans token | 401 | C1 |
| 2 | `AUTH_FAILED` sur `de1158` / 8 h | 0 | C4, C5 |
| 3 | Lignes `degraded pin-loop` / 8 h | < 20 | C6, C7, C8 |
| 4 | `requests` où `out=0 AND success=1` / 7 j | ~0 | C10 |
| 5 | `grep -c ERROR logs/debug.log` vs échecs DB | cohérents (±10 %) | C15 |
| 6 | `grep -c CRITICAL logs/debug.log` | 0 | C14 |
| 7 | Taille `debug.log` après 24 h | < 10 Mo | C14, C25 |
| 8 | `PRAGMA freelist_count` / `page_count` | < 5 % | C20 |
| 9 | Taille `requests.db` | < 4 Go | C20, C21 |
| 10 | `free_model_usage WHERE status=400` / jour | 0 | — (déjà résolu) |
| 11 | Fichiers dans `logs/` non référencés en config | 0 | C23 |
| 12 | `git status --porcelain` | propre | C3 |
| 13 | `migrate+canary bg FAILED` sur 10 boots | 0 | C13 |
| 14 | `grep -c 'boom compose' logs/debug.log` après pytest | 0 | C16 |
| 15 | `dropped_chars > 0` **et** dernier message présent | vrai | C18 |
| 16 | `grep 'URL non extraite du body'` au boot | 0 | C19 |
| 17 | Retry `no reasoning_content` → 2 appels dont `effort=high` | vrai | C12 |

---

## 8. Journal de bord — état résolu

| Problème | Preuve de résolution |
|---|---|
| Boot > 60 s | `[boot+0.0s] DB open` → `[boot+1.8s]` max, 4 boots mesurés le 10/09 |
| Faux 429 « quota épuisé » | 0 échec 429 depuis le 05/09 (`PLAN_CORRECTION_FAUX_429.md` lot A implémenté) |
| 453 × HTTP 403 upstream | 0 depuis le 04/09 14h |
| 22 640 × status 400 jambe free | 0 le 10/09 (décroissance continue depuis le 04/09) |
| Travail non commité (543 lignes) | committé en `110d587` par le processus concurrent |
| 199 `CRITICAL` fantômes | identifiés comme texte utilisateur — nécessite C14 pour disparaître |
| `importtime` / boot profiling | objectif atteint, fichiers à archiver (C24) |
