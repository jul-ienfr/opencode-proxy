# Audit performance — opencode-proxy-one

**Date** : 29/10/2026, 07:31–08:25 (local)
**Périmètre** : latence, débit (requêtes/minute), ressources, qualité, maintenabilité/modularité.
**Méthode** : DB `logs/requests.db` (24 h / 6 h / 1 h), `/api/vpn-status`, `/metrics`, CPU/RSS, WAL/disque, lecture des chemins chauds, micro-benchmarks existants. Aucun test de charge upstream n'a été lancé (quota) : les gains sont des estimations, à valider par A/B.

> **Résumé exécutif** : le proxy n'est **pas** limité par le CPU local ni par la concurrence à la charge actuelle (~8–15 rpm, pic 12 requêtes simultanées, 48 slots). La latence est dominée par **le débit de génération upstream, qui varie de 6× selon l'IP de sortie** (6,5–9 tok/s contre 40–55 tok/s pour le même modèle et la même taille de sortie). Le sélecteur de stations ne mesure que le **TTFB**, pas le débit total : il peut donc classer « meilleure » une IP rapide au premier octet mais très lente à générer. **38 % du trafic récent attribuable par IP était sur des IP lentes** ; un routage conscient du débit peut réduire la latence médiane d'un facteur ~2,5–3,5. Le second sujet est l'anomalie **~60 s** (38 % des requêtes récentes, `success=1`, parfois seulement 69 tokens de sortie) : elle est invisible dans la DB car les flux n'enregistrent ni TTFB, ni statut, ni raison d'arrêt. Enfin, la DB/WAL (4,8 Go avant troncature), la queue DB (10 000 lignes, jusqu'à 10 Mo chacune) et le VACUUM hebdomadaire sous verrou sont des risques ressources à traiter avant de monter en débit.

---

## 1. Base mesurée

### 1.1 Trafic et latence (24 h)

| Mesure | Valeur |
|---|---|
| Requêtes | 7 681 |
| Succès | 7 677 (99,95 %) |
| Durée moyenne | 41,1 s |
| p50 / p75 / p90 | 37,2 s / 62,9 s / 91,7 s |
| p95 / p99 / max | 102,8 s / 156,2 s / 236,1 s |
| Concurrence moyenne / pic | 3,58 / 19 |
| Dernière heure | 476 req (7,9 rpm), pic 12, moyenne 7,0 |
| Tokens entrée / sortie | 666,5 M / 4,47 M |
| Cache read | 620,1 M (**93 %** de l'entrée) |

### 1.2 Modèles (24 h)

| Modèle | Requêtes | Durée moy. | Sortie moy. |
|---|---:|---:|---:|
| `deepseek-v4.1-flash-free` | 6 458 | 44,7 s | 630 tok |
| `mimo-v2.6-flash-free` | 288 | 80,8 s | 730 tok |
| `jev-1.13-free` | 844 | 0,8 s | 19 tok |
| `big-pickle` (payant) | 110 | 2,2 s | 24 tok |
| `glm-5.3-flash-free` | 5 | 3,2 s | 31 tok |

Le trafic est massivement `deepseek-v4.1-flash-free`, avec des contextes d'entrée de 80–130 k tokens. Le cache prompt (93 %) est un actif majeur : toute optimisation qui réécrit l'ordre des messages peut le casser.

### 1.3 Débit par IP de sortie (6 h, IP avec ≥ 20 requêtes)

| IP | n | Durée moy. | Sortie moy. | **tok/s** |
|---|---:|---:|---:|---:|
| 187.13.10.107 | 181 | 11,9 s | 586 | **49,1** |
| 187.15.173.49 | 165 | 8,4 s | 347 | **41,1** |
| 86.104.23.100 | 146 | 9,6 s | 529 | **54,9** |
| 187.40.49.191 | 144 | 14,0 s | 562 | **40,1** |
| 187.13.10.83 | 121 | 12,3 s | 634 | **51,5** |
| 187.40.54.144 | 30 | 9,0 s | 468 | **51,8** |
| 176.223.172.37 | 148 | 50,6 s | 620 | 12,3 |
| 86.104.22.8 | 140 | 58,7 s | 508 | **8,7** |
| 86.104.21.144 | 127 | 56,0 s | 478 | **8,5** |
| 186.247.161.121 | 134 | 58,4 s | 394 | **6,8** |

Parmi les 1 441 requêtes attribuables par IP sur 6 h, **549 (38 %) étaient sur les quatre IP lentes**. Les IP lentes ont un p50 de **60,0 s** ; les IP rapides n'ont quasiment aucune requête entre 55 et 65 s. À 06:40, un même bucket de 10 min contenait **58 tok/s et 2,8 tok/s simultanément** : ce n'est donc pas un plafond global à cet instant.

### 1.4 Stations (24 h, colonne `station`)

| Station | n | Durée moy. | tok/s |
|---|---:|---:|---:|
| 3 (Suède) | 403 | 12,0 s | **29,0** |
| 4 (Suisse) | 541 | 32,0 s | 14,2 |
| 1 (France) | 416 | 49,1 s | 12,2 |
| 5 (Pays-Bas) | 885 | 46,6 s | 12,0 |
| 6 (Allemagne) | 549 | 47,6 s | 11,9 |
| 2 (Pays-Bas) | 521 | 48,5 s | 10,6 |
| **(null)** | **4 298** | 39,4 s | — |

**56 % des lignes n'ont pas de station** : l'attribution est le premier trou d'observabilité à corriger.

### 1.5 Sélecteur de qualité (snapshot 08:15)

| Station | IP au snapshot | Score | TTFB EWMA | Débit total constaté | Interprétation |
|---|---|---:|---:|---:|---|
| 2 | 86.104.21.144 | **0,915** | 1 473 ms | 8,5 tok/s | classée « meilleure »… |
| 4 | 86.104.22.8 | 0,883 | 3 003 ms | 8,7 tok/s | …mais très lente en génération |
| 6 | 187.13.10.83 | 0,629 | 15 085 ms | 51,5 tok/s | sous-notée par le TTFB |
| 3 | 187.40.54.144 | 0,655 | probe 1 690 ms | 29,0 tok/s (24 h) | la plus rapide, mais à froid |

Le registre est clé par `(station, IP)` et le dashboard agrège par station : après une rotation, l'entrée affichée peut être l'ancienne IP. La station 3, la plus rapide en débit total, était sous-utilisée car à froid (confiance 0,5) et moins bonne en TTFB.

### 1.6 Ressources

| Mesure | Valeur |
|---|---|
| CPU process | 703 s depuis 07:31 (~26 % d'un cœur en moyenne) |
| CPU au repos (30 s) | 3,59 s = **12 % d'un cœur** (trafic résiduel inclus) |
| CPU/requête observé | 0,15–1,2 s selon la part de fond (non isolée) |
| RSS | 500 Mo |
| Threads | 38 |
| DB `requests.db` | 2,47 Go (324 902 lignes) |
| Corps stockés | **2,17 Go** |
| WAL | **2,36 Go → 0** (tronqué pendant l'audit, 2,36 Go libérés) |
| Disque C: | 9,3 Go → **13,7 Go** libres |
| Croissance corps | 400–630 Mo/jour aux jours pleins |
| Cache réponse | 0/1 000 entrées (les flux ne sont pas cachés) |
| Cache conversion | **1 hit / 287 miss (0,3 %)** |

---

## 2. Où part le temps

```
Client → FastAPI (middlewares) → route → pool free
      → sélecteur (TTFB) → curl_cffi SOCKS5 → Docker/gluetun → NordVPN → upstream
      → pompe SSE → conversion protocole → client
      → queue DB → writer → SQLite WAL
```

Décomposition :

1. **Client → proxy** : localhost, < 1 ms. Le parsing/conversion de requête est de l'ordre de 0,1–1 ms (micro-benchmarks). Ce n'est pas le sujet.
2. **Proxy → upstream** : le tunnel VPN/Docker ajoute un aller-retour ; les probes `robots.txt` (nouveau client à chaque probe) mesurent 0,8–1,7 s, handshake inclus. Le TTFB réel des requêtes est de 1,5–15 s selon la station.
3. **Prefill upstream** : 80–130 k tokens d'entrée, 93 % servis par le cache prompt. Le premier tour d'une conversation paie le prefill complet ; les suivants sont rapides.
4. **Génération upstream** : 500–800 tokens de sortie. C'est **le poste dominant** : à 8 tok/s → 60–100 s ; à 50 tok/s → 10–16 s.
5. **Proxy → client** : conversion/SSE par événement. Coût local réel à instrumenter, mais non dominant à 8–15 rpm.
6. **Queue DB** : écriture asynchrone, ~1 ms par ligne en régime normal ; peut devenir un tail latency si la DB est verrouillée (VACUUM, cleanup).

**Conclusion** : à la charge actuelle, optimiser le CPU local ne fera pas gagner 30 s. Il faut d'abord **choisir la bonne IP/station** et **détecter les flux qui n'avancent pas**.

---

## 3. Le problème n°1 : 6× d'écart de débit selon l'IP

### 3.1 Preuve

Même modèle, même taille de sortie (~500–630 tokens), même proxy :

- 86.104.22.8 : 58,7 s, 8,7 tok/s, p50 60,0 s.
- 187.13.10.83 : 12,3 s, 51,5 tok/s, p50 7,0 s.

Les clients sont multiples (`Anthropic/Python 0.125.0`, `deepseek-harness/0.2.0-rc.2`, `0.2.1-alpha.1`) et tous subissent les 60 s sur les mêmes IP : ce n'est pas un timeout d'un seul client. Les tailles de corps diffèrent (44–93 Ko) mais les IP lentes sont lentes pour les deux tailles, et les IP rapides rapides pour les deux.

### 3.2 Pourquoi le sélecteur ne le voit pas

- `opencode.py:9023` enregistre `ttfb_ms` dans la qualité, pas la durée totale.
- `free/quality.py:109-136` : l'EWMA de latence ne reçoit que le TTFB.
- `free/quality.py:159-214` : le score pondère latence 0,4 / succès 0,2 / quota 0,15 / serveur 0,15 / stabilité 0,1. Aucun poids « débit ».
- `stream_metric: ttfb` (`config.yaml:393`) : passer à `total` **ne marche pas** pour les flux, car le chemin `total` n'est pas implémenté côté qualité (le code sort à `opencode.py:9016` et seul le non-streaming enregistre `latency=False` à `opencode.py:12150`).
- Le registre est clé par `(station, IP)` : après rotation, la station repart à froid ; le dashboard peut afficher l'ancienne IP (`free/quality.py:276-293` agrège par `sid`).

### 3.3 Ce qu'il faut faire

**P0.1 — Mesurer le débit réel par (station, IP).**
Ajouter à `IpQuality` : `tps_ewma`, `tps_n`, `total_ewma_ms`, `last_stop_reason`, `slow_lane_until`. À la fin du flux (native et converti), appeler `observe_completion(duration_ms, output_tokens, stop_reason, aborted)` :
`tps = output_tokens / max(0.001, duration_ms/1000)`, EWMA α=0,3.

**P0.2 — Pénaliser/quarantaine les IP lentes.**
Config : `throughput_weight: 0.25`, `slow_lane_floor_tps: 20`, `slow_lane_min_samples: 3`, `slow_lane_cooldown_s: 600`.
Si `tps_ewma < floor` après 3 échantillons → `slow_lane_until = now + cooldown` ; le sélecteur exclut l'IP (sauf exploration) et déclenche une rotation anticipée si la station n'a pas d'autre IP saine.

**P0.3 — Score hiérarchique station + IP.**
Un prior station (24 h, décayé) mélangé à l'évidence de l'IP courante évite le trou de 500 requêtes après chaque rotation. Exemple : `score = 0,65 × station_prior + 0,35 × ip_score`, avec confiance.

**P0.4 — Least-in-flight + quota.**
Le sélecteur ne connaît pas la concurrence en cours. Ajouter `inflight` par station (incrément/décrément autour du stream) et un tie-break : à score comparable, choisir la station la moins chargée, puis celle avec le plus de quota restant. Objectif : utiliser les 6 quotas (stations 1/3/5 étaient à 0 requête sur leur IP courante) au lieu de saturer la « meilleure » jusqu'au mur.

**P0.5 — Rotation anticipée sur débit.**
Aujourd'hui la rotation se déclenche à ~490 requêtes/IP. Les IP lentes sont à 127–148 requêtes et le restent. Ajouter : si `tps_ewma < floor` sur 3 requêtes → rotation (comptée dans les 10/h). C'est le levier le plus direct : une IP lente est remplacée en quelques minutes au lieu d'attendre 350 requêtes.

**P0.6 — Budget token par IP (hypothèse à tester).**
Le quota est en requêtes ; l'upstream limite peut-être en tokens (entrée + sortie). Ajouter `token_budget_per_ip` (ex. 8–12 M) et rotate au premier des deux seuils. À valider par l'expérience : corréler le passage en slow lane avec les tokens servis depuis la rotation.

**Gain attendu** : si 38 % du trafic passe de ~55 s à ~10–14 s, la moyenne du trafic attribuable tombe de ~27 s à ~12 s (facteur ~2,3) ; sur 24 h, de 41 s à ~20–25 s. **Risque** : score instable, starvation, rotation excessive. **Validation** : tests synthétiques fast/slow, A/B 24 h, surveillance 429 et quota.

---

## 4. Le problème n°2 : l'anomalie ~60 s

### 4.1 Faits

- 38 % des requêtes attribuables (6 h) sont sur des IP dont le p50 est ~60 s.
- `success=1`, `is_stream=1`, `free_status=NULL`, `error=NULL`.
- Sorties de 69 à 984 tokens ; 69 tokens en 62,5 s = ~1 tok/s.
- Quasiment aucune requête 55–65 s sur les IP rapides (0/121 pour 187.13.10.83, 2/144 pour 187.40.49.191).
- Le pic p95 va jusqu'à 77–80 s : ce n'est pas un cap strict à 60,0 s pour tous.

### 4.2 Hypothèses (non tranchées)

1. **Slow lane upstream par IP** : l'upstream throttlerait les IP après un volume de tokens/requêtes, avec une réponse qui traîne ~60 s.
2. **Gateway 60 s** : un timeout de passerelle couperait le flux à 60 s en laissant un 200 tronqué.
3. **Timeout client** : peu probable comme cause unique, car plusieurs clients et des durées > 60 s existent.
4. **Timeout proxy** : `sse_idle_timeout` est à 300 s et `request_budget_s` à 240 s ; ni l'un ni l'autre ne produit 60 s.

La DB ne permet pas de trancher : pas de TTFB, pas de `stop_reason`, pas d'`aborted`, pas de dernier événement SSE pour les flux.

### 4.3 Plan de diagnostic (P0)

1. Enregistrer pour chaque flux : `ttfb_ms`, `total_ms`, `output_tokens`, `stop_reason`, `aborted`, `last_event_type`, `upstream_status`, `client_disconnected`.
2. Test contrôlé sur une IP lente : petit prompt + `max_tokens=32`. Si 60 s → throttle/tunnel, pas la longueur de contexte.
3. Même IP, 1 vs 4 requêtes concurrentes : si la durée scale → limite de concurrence par IP.
4. Même IP, identité `chrome` vs `safari` : si le débit change → throttling par fingerprint.
5. Corréler le slow lane avec les tokens servis depuis la rotation (budget token).
6. Vérifier la fin du flux : `message_stop` / `[DONE]` / `stop_reason` ; si absent → troncature silencieuse.

### 4.4 Mitigations

- **P0.2** (routage/quarantaine) : éviter les IP lentes.
- **P0.7 — Watchdog TTFB free** : si aucun octet après 30 s (configurable par modèle), annuler le stream, libérer le slot, retenter sur une autre station **avant** d'avoir émis le moindre octet. Si des octets sont déjà partis, émettre une erreur SSE explicite et fermer ; ne jamais marquer `success`.
- **P0.8 — Sémantique de succès** : `success=1` seulement si le flux se termine proprement (`message_stop`/`[DONE]`), pas sur un 200 tronqué. Enregistrer `stop_reason` et `aborted`.
- **P1** : fallback payant si le fleet est globalement lent et que le budget le permet.

**Gain attendu** : suppression des attentes de 60–240 s sans progression ; p95 divisé par 2–3 ; qualité perçue bien meilleure. **Risque** : annuler un flux qui allait aboutir (quota gaspillé). **Validation** : p99 TTFB < 30 s, aucun doublon de sortie, quota consommé < 5 % en plus.

---

## 5. Débit (RPM) : ce qui limite réellement

### 5.1 Little's law

`RPM ≈ concurrence / durée_moyenne × 60`

- Aujourd'hui : C≈7, T≈41 s → **10 rpm** (observé 8–10).
- 48 slots (6 stations × 8 sessions), T=41 s → **70 rpm**.
- T=12 s (bonnes IP) → **240 rpm**.
- T=6 s → **480 rpm**.

### 5.2 Quota vs sessions

- Quota théorique : 6 × 500 × 10 rotations/h = 30 000/h = **500 rpm**.
- Mais les rotations prennent 30–60 s, la concurrence de rotation est limitée, et l'upstream impose probablement une limite par IP. Le **vrai plafond réaliste avec 6 IP est ~150–250 rpm**, pas 500.
- Le pool de 48 sessions est donc le facteur limitant avant le quota à T=41 s ; à T=12 s, c'est l'upstream/la concurrence par IP.

### 5.3 Recommandations

1. **D'abord le routage** (section 3) : à concurrence constante, il peut doubler le débit utile.
2. **Mesurer la concurrence supportée par IP** : ramp 1/2/4/8 streams sur une IP, mesurer 429 et tok/s. Ajuster `curl_sessions_per_station` (8 aujourd'hui) ; 8 peut provoquer le slow lane.
3. **Backpressure** : au-delà de 48, le pool crée une session overflow après 5 s (`CHECKOUT_WAIT_S=5`). Ajouter un cap global (ex. 64) + file courte + 503 propre, plutôt que des sessions TLS en rafale.
4. **Ajouter des IP** : 2–4 SOCKS5 résidentiels testés (latence, 429, ban) ou stations supplémentaires. C'est le seul levier pour dépasser ~250 rpm de façon durable.
5. **Fallback payant** pour la latence interactive, si l'économie est plate (à vérifier).
6. **Ne pas ajouter de workers** : l'état (quotas, qualité, compteurs) est en mémoire et `gunicorn.conf.py` impose `workers=1`. Un multi-worker exigerait d'externaliser l'état.

---

## 6. Optimisations de latence (hors routage)

| Action | Effet | Effort | Priorité |
|---|---|---|---|
| Watchdog TTFB free 30 s + retry avant premier octet | Supprime les attentes mortes | 1–2 j | P0 |
| Prewarm **toutes** les stations après rotation + toutes les 5 min | Évite le handshake à froid (5 stations froides aujourd'hui) | 1 j | P1 |
| Hedge adaptatif (seulement si aucun octet après 4 s **et** station rapide idle) | Coupe la queue lente sans doubler tout le quota | 2–3 j | P1 |
| Routage par budget de latence / modèle rapide pour les petits tours | `glm-5.3` 3,2 s, `big-pickle` 2,2 s vs 44,7 s | 1–2 sem. | P2 |
| Compaction contexte > 80 k tokens | Réduit prefill/TTFT et coût | 1–2 sem. | P2 |
| SSE : coalesce 16 Ko pour le natif, parsing une passe, pas de double parse | Baisse le CPU/requête et augmente le plafond RPM | 2–4 j | P1 |
| Cache conversion : plafonner par octets ou désactiver pour les gros corps | 0,3 % de hit, ~50 Mo potentiels | 0,5 j | P1 |
| Cache réponse : 0 hit (flux) | Code mort, à retirer/réaffecter | 0,5 j | P2 |
| `/v1/models` : cache 60 s | ~100 ms par appel | 0,5 j | P2 |
| Réseau : mesurer direct vs SOCKS5 vs Docker ; WSL2/uvloop si > 100 ms | Gain tunnel/event loop | 1 sem. | P2 |

**SSE** : `opencode.py:16051-16114` décode chaque chunk en texte, split les lignes, parse chaque événement JSON, puis (pour le chat) convertit, joint, ré-encode et **re-parse** pour compter les tokens. Le `ChatSseToAnthropicState` suit déjà l'usage : le second parse est redondant. Pour le natif Anthropic, on peut passer les octets tels quels et ne parser que `message_start`/`message_delta` (ou scanner `"usage"` au niveau octet). Cible : < 0,1 s CPU/requête de streaming.

---

## 7. Ressources : DB, WAL, queue, mémoire

### 7.1 DB/WAL — risque principal

- DB 2,47 Go, corps 2,17 Go, WAL **2,36 Go** (tronqué pendant l'audit → 0, 2,36 Go libérés).
- `PRAGMA journal_size_limit = -1` : le WAL reste à son high-water mark même après checkpoint. **Ajouter `journal_size_limit=64 Mo`** dans `observability/db.py:482-499`.
- `cleanup_old_bodies` (`observability/db.py:805-855`) fait un **UPDATE massif** de tous les corps > 7 jours en une transaction : c'est le suspect le plus probable du WAL à 2,3 Go (le cleanup tourne 60 s après chaque démarrage). Le batcher par 2 000 lignes, commit + checkpoint passif entre les lots, et le planifier à 04:00 (pas 60 s après chaque démarrage).
- `weekly_maintain` (`observability/db.py:1046-1084`) fait `wal_checkpoint(TRUNCATE)` + `VACUUM` **sous le lock writer** : le VACUUM d'une DB de 2,4 Go bloque les écritures et remplit la queue. Utiliser `VACUUM INTO` + swap, ou supprimer le VACUUM (les pages libérées sont réutilisées), ou `auto_vacuum=INCREMENTAL` dans une migration.
- Rétention : `body_retention_days` par défaut 7 j, 400–630 Mo/jour → 3–4 Go de corps. Passer à **3 j** + `MAX_BODY_STORAGE=16 000` + compression zstd/zlib (3–5×), ou stocker les corps dans une table séparée / un body store.
- Index : 8 index sur 325 k lignes ; `idx_success` (faible sélectivité) et `idx_account` sont candidats à la suppression. Ajouter un index couvrant pour les stats ou, mieux, une table d'agrégats journaliers (`stats_daily` par modèle/station) pour que `/api/stats` ne scanne plus les lignes.

### 7.2 Queue DB

- `_db_queue` : 10 000 lignes, chaque ligne peut porter un corps jusqu'à 2 Mo (`DB_RAW_SIZE_CAP`) ou 10 Mo (limite HTTP). **La limite est en lignes, pas en octets** : 10 000 × 2 Mo = 20 Go potentiels.
- Sur `QueueFull`, le fallback (`opencode.py:1150-1170`) fait `asyncio.to_thread` + insert synchrone **dans le chemin de requête** : si la DB est verrouillée, la requête attend.
- Correctifs : compteur `_db_queue_bytes` + cap 64 Mo ; au-delà, stub du corps (garder les métadonnées) ou drop avec métrique ; jamais d'insert synchrone dans le chemin ; `db_queue_depth`, `db_dropped_total`, `db_write_ms`.
- Séparer métadonnées (petites, toujours écrites) et corps (best-effort, échantillonné/compressé).

### 7.3 Mémoire / CPU de fond

- RSS 500 Mo, 38 threads. Le dump `py-spy` montre des threads d'exécuteur bloqués dans `_sync_ddg` (`server/websearch.py`) : `web_search.enabled: true` ; les recherches DuckDuckGo synchrones peuvent saturer le pool par défaut et bloquer d'autres `to_thread`.
- 6 stations × scan Docker logs toutes les 20 s (`log_scan_tail: 2000`) : candidat au CPU de fond (12 % au repos).
- Correctifs : websearch async avec timeout/semaphore/cache et executor dédié ; scans Docker via events/health plutôt que logs, ou intervalle 60 s ; `gc.freeze()` après boot ; budget mémoire et alerte RSS > 1 Go.
- Cache conversion 0,3 % de hit : plafonner à 64 entrées/16 Mo ou désactiver pour `len(raw) > 16 Ko`. Cache réponse 0 hit : le retirer.

---

## 8. Qualité / fiabilité

### 8.1 Boucle AUTH_FAILED

- 33 échecs en ~24 min sur `nl1255.nordvpn.com` / `186.247.163.12`, toutes les ~43 s, malgré un TTL de 1 440 min.
- Les compteurs d'état (`auth_fail_30min`) restent à 0 et les logs **ne portent pas l'ID de station** : impossible de savoir quelle station boucle.
- Risque : rate-limit NordVPN, voire verrouillage de compte.
- Correctifs : préfixer chaque log par `stN` ; vérifier la blacklist **avant** le pin et après ; si l'hôte pinné est blacklisté, re-pin immédiat vers un autre pays ; backoff global si ≥ 3 AUTH/5 min ; quarantaine 24 h par hôte ; arrêter après 3 rotations échouées et alerter. Test de non-régression : après un hôte blacklisté, le pin suivant choisit un hôte différent.

### 8.2 Troncature silencieuse

- Un flux 200 qui se termine sans `message_stop`/`[DONE]` est marqué `success=1`. Le client reçoit une réponse incomplète sans erreur.
- Correctif : `success` seulement sur fin propre ; `stop_reason`/`aborted`/`last_event` en DB ; erreur SSE explicite sinon. C'est indissociable du diagnostic 60 s.

### 8.3 Attribution / dashboard

- 56 % des lignes sans `station` : passer la station et l'IP au `_save_request` (capturées au début, pas après nettoyage du contexte).
- Le dashboard qualité agrège par `sid` et peut afficher l'ancienne IP après rotation : afficher l'IP courante + toutes les entrées + un agrégat station.
- Ajouter `vpn_country`, `ttfb_ms`, `stop_reason`, `aborted` (table `request_metrics` séparée pour ne pas élargir la table principale).

---

## 9. Maintenabilité / modularité

- **Monolithe** : `opencode.py` ~25 000 lignes, 1,19 Mo. Les modules `server/`, `upstream/`, `free/`, `observability/`, `app/protocol/` existent déjà ; poser des règles d'import (`import-linter`) pour éviter les cycles et les imports inverses.
- **Config typée** : 813 lignes de YAML, `yaml_get` permissif. Migrer vers des modèles Pydantic par section (`QualityConfig`, `PoolConfig`, `DbConfig`, `StreamingConfig`), validation au boot, clés inconnues signalées, `config_version` + migrations. Générer la doc depuis le schéma.
- **Profils** : `eco` (2 sessions, pas de hedge, rétention 1 j, probes 60 s), `balanced` (4 sessions, routage débit, 3 j), `max` (8 sessions, hedge adaptatif, fallback payant, 16 k corps). Un seul endroit pour les presets, surcharge par clé.
- **Ports/adapters** : `StationScheduler` (`pick/release/observe/status`), `QualityStore`, `BodyStore`, `MetricsSink`, `ConfigProvider`. Les tests utilisent des fakes ; le reste du code ne dépend plus des globales.
- **Hot reload complet** : aujourd'hui `_periodic_config_watch` ne recharge que `ip_rotation` ; les poids qualité, la taille du pool, les timeouts exigent un restart. Implémenter un diff validé : appliquer à chaud les clés sûres, marquer « restart required » pour les autres.
- **Benchmarks** : `scripts/bench_perf.py` + un nouveau `bench_e2e.py` avec un **faux upstream** (SSE enregistré, TTFB/tok-s contrôlés) pour rejouer 100 streams sans quota. Budgets CI : p50/p95 TTFB/total, CPU/RSS, WAL, queue. Aujourd'hui les mesures live ne sont pas reproductibles.
- **Déploiement** : découpler le serveur du GUI/tray (le tray et la webview dans le même process compliquent le headless et le redémarrage) ; envisager WSL2/Linux + uvloop pour le serveur, le GUI restant côté Windows. À mesurer avant de migrer.

---

## 10. Feuille de route

### État d'implémentation — 29/10/2026 18:56

| Chantier | État | Preuve / mesure |
|---|---|---|
| P0.1 Qualité débit + slow lane | **Fait** | `free/quality.py` : EWMA tok/s, score 0–1, quarantaine 10 min ; `free/pool.py` : garde-fou flotte ; **+ slow lane TTFB** (`ttfb_ceiling_ms=8000`, quarantaine après 3 TTFB EWMA > 8 s même si le débit est bon). **+ exploration aléatoire des stations froides** (`free/selector.py` : `ranked[1]` → tirage parmi les `tps_n=0`). Live final : s1 **65,45 tok/s** (score 0,712), s2 **53,62** (0,720), s5 **50,46** (0,777), s6 **33,62** (0,667) ; **0 slow lane** ; 4 stations actives. |
| P0.2 TTFB réel + watchdog | **Fait** | TTFB mesuré au **premier octet du corps** (plus à l'en-tête), timeout free 30 s, `_FreeTtfbTimeout`. Le flag `_real_truncation` marque désormais le chemin « error + [DONE] » comme **échec** (avant : faux succès). Live : 33/33 succès, 0 troncature sur l'échantillon. |
| P0.3 Attribution station | **Fait** | Fallback `station_for_ip()` + enregistrement de fin sur les chemins OpenAI et Anthropic→OpenAI + `station_override` = **station réellement servie** (fin du biais « station demandée » après failover). Live : lignes sur **s5**, **0 ligne s4** depuis l'exclusion ; 1 null/6 (prompt court). |
| P0.4 WAL / cleanup / mémoire | **Fait** | `journal_size_limit=64 Mo`, cleanup par lots de 2 000, **04:00 local** (plus au démarrage), VACUUM retiré du cleanup. Live : WAL **2,43 Go → 4,1 Mo**, RSS **3,6 Go → 330 Mo**, disque libre **7,0 → 9,4 Go**. |
| P0.5 Boucle AUTH_FAILED | **Fait** | Cause : **s4** puis **s3** re-tentaient AUTH sur des hôtes frais (`se667`, `fr1332`, `uk-nl20`…) ; `disabled_stations: [3, 4]`, containers arrêtés, managers conservés (IDs contigus). **Plafond de sessions du compte** (s3 échoue sur 2 serveurs différents), pas un serveur fautif. Le scan de logs re-lisait la même queue : **hôte déjà blacklisté → `False`** (plus de re-pin en boucle) + dédup **egress `tunnel_up_full`**, **`kind="server"`** (un server-refused ne s'écrit plus en AUTH), **`country_rotation: false`**. Live : **AUTH 0, server-refused 0** sur la fenêtre ; **4 stations actives** (1,2,5,6), `auth_fail_30min=0`. |
| P0.6 Observabilité | **Fait** | Colonnes `ttfb_ms`, `stop_reason`, `aborted` (migration + ALTER boot) ; live : TTFB **1,6–3,0 s**, `tool_calls`, `aborted=0`. |
| P0.7 Queue DB en octets | **Fait** | Cap **256 Mo** ; au-delà, la ligne garde les métadonnées et **drop les corps** (`db_queue_bytes_drop`). Test dédié. |

> **Lecture** : le baseline des sections 1–9 est **antérieur** à ces correctifs. Dernière fenêtre live (18:53–18:55Z) : **5 requêtes, 0 échec, 0 abort**, **7,35 s** de moyenne, TTFB **2,98 s** ; s1 2 (660 tok), s2 1 (1 214 tok), s5 2 (177 tok). Qualité : s1 **65,45 tok/s**, s2 **53,62**, s5 **50,46**, s6 **33,62** ; **4 stations actives**, **0 slow lane**, **AUTH 0 / server-refused 0**. Les longues sorties expliquent la moyenne ; le débit est bon. Base complète : **4 488 lignes, 4 415 OK (98,4 %)**, 58×429, 10×401, 3×502, 3×400 ; max **235 s / 17 094 tokens** (sortie longue, pas un stall). Ressources : WAL **4,1 Mo**, RSS **261–489 Mo** selon la charge de la queue, disque stable. Station 5 **47,97 tok/s / score 0,954** ; station 1 **25,43 tok/s** ; 3 stations froides seront rééchantillonnées par l'exploration 5 %.

**Anomalie ~60 s — diagnostic 29/10** : **2 340 lignes 50–75 s dans la base** (toutes époques), **0 `aborted`**, 3 seulement avec TTFB. Les cas récents montrent deux familles : (a) **sortie longue légitime** (52 s / **3 865 tokens**, 74 tok/s ; max base **235 s / 17 094 tokens**) ; (b) **génération lente** (54 s / 324 tokens, TTFB **15,9 s**). Ce n'est donc pas un stall après premier octet : la mitigation est la **qualité + slow lane TTFB 8 s**, pas un watchdog d'inactivité (qui couperait des tool calls légitimes). Les anciens 60 s incluaient aussi des 429/502 (échecs réels).

> **Piège de mesure** : `timestamp` est stocké en ISO `2026-10-29T17:58:00Z`, alors que `datetime('now')` renvoie `2026-10-29 17:58:00` (espace, sans `T/Z`). La comparaison de chaînes `timestamp >= datetime('now','-5 minutes')` est **toujours vraie** (`'T' > ' '`) et renvoie toute la base. Utiliser des bornes explicites `timestamp >= '2026-10-29T17:50:00Z'`.

**Limites** : (1) l'anomalie est **classée** (long output vs slow TTFB/tok-s), pas éliminée à 100 % ; (2) **s3 et s4 exclues** (`disabled_stations: [3, 4]`) : s3 a fait un vrai `AUTH_FAILED` sur **`fr1332` puis `uk-nl20`** après restart ⇒ plafond de sessions du compte, pas serveur ; **4 stations actives** (1,2,5,6) à **34–65 tok/s**, **AUTH 0 / server-refused 0** sur la fenêtre ; une 5ᵉ session sur le même compte n'ajoute **aucun RPM utile** ; (3) pas de test de charge upstream (quota) : les RPM 150–250 restent une projection.

**Décision rotation pays** : `country_rotation: false` — les re-pins au boot déclenchaient des rafales AUTH et n'apportaient pas de débit ; les IP restent choisies par qualité. Pour réactiver : repasser à `true` et accepter une rafale AUTH au prochain boot.

**Décision s3/s4** : stations exclues via `ip_rotation.disabled_stations: [3, 4]` (config.yaml), containers arrêtés. Pour réactiver : retirer l'ID de la liste, redémarrer le container, puis vérifier `auth_fail_30min=0` et `egress_state=tunnel_up_full` sur `/api/vpn-status` pendant 10 min avant de le remettre en rotation. Les managers restent instanciés (IDs contigus) mais `enabled=false` et le pool les ignore (`_station_usable`).

**Pourquoi 4 stations et pas 6** : 4 × 500 = **2 000 requêtes/cycle** et 4 × 8 = **32 sessions** ; 5/6 donneraient 2 500/3 000 et 40/48 **si l'authentification passait**. Test du 29/10 : s3 redémarrée → `AUTH_FAILED` sur `fr1332` **puis** `uk-nl20` (serveurs différents) ⇒ **plafond de sessions du compte NordVPN**, pas un serveur fautif. Une 5ᵉ station qui boucle AUTH apporte **0 RPM utile** et risque de rate-limiter les 4 saines. Pour augmenter réellement : **second compte** ou plan avec plus de sessions simultanées ; multiplier les containers sur le même compte n'ajoute rien. À 12 rpm, 4 stations ne sont pas le goulot (client/upstream).

### P0 — cette semaine (impact maximal, risque maîtrisé)

1. **Qualité consciente du débit + quarantaine slow lane** (2–3 j). Gain : latence médiane ÷2–3. **Fait.**
2. **Watchdog TTFB free + sémantique de troncature** (1–2 j). Gain : plus d'attentes mortes, plus de faux succès. **Fait.**
3. **Boucle AUTH_FAILED** (0,5–1 j). Gain : fiabilité, protection du compte. **Fait (s4 exclue, dédup stale log, 5 stations saines).**
4. **WAL `journal_size_limit` + cleanup batché + queue en octets** (1 j). Gain : disque stable, pas de blocage DB. **Fait.**
5. **Observabilité minimale** : station/IP/TTFB/stop_reason/aborted (1–2 j). Gain : rend les autres chantiers mesurables. **Fait.**

### P1 — 1 à 2 semaines

6. Sélecteur hiérarchique + least-in-flight + budget token + prewarm toutes stations. **Partiel** : exploration aléatoire des froides **fait** (s6 119 tok/s découverte, score 0,96) + **least-in-flight** (`inflight_penalty=0.08`, max 0,60, alimenté par `stream_tasks`) ; budget token reste.
7. SSE une passe + coalesce 16 Ko ; cache conversion plafonné ; cache réponse retiré.
8. Body store compressé/séparé + agrégats journaliers + audit des index.
9. Config typée + profils + hot reload complet + bench e2e.

### P2 — 2 à 6 semaines

10. Routage modèle/latence, compaction contexte, fallback payant.
11. Réseau : direct vs SOCKS5 vs Docker, WSL2/uvloop, headless.
12. Ports/adapters, externalisation d'état, roadmap multi-worker.

---

## 11. SLO cibles

| Indicateur | Cible |
|---|---|
| p50 total (deepseek-free, ~600 tok) | < 15 s |
| p95 total | < 45 s |
| p99 total | < 90 s |
| TTFB p95 | < 5 s |
| Part slow lane | < 5 % |
| Flux proprement terminés | > 99,5 % |
| Troncatures silencieuses | 0 |
| 429 | < 1 % |
| RPM soutenu (6 IP) | 150–250 |
| RPM avec 8–10 IP | 300+ |
| CPU à 100 rpm | < 50 % d'un cœur |
| RSS | < 800 Mo |
| WAL | < 256 Mo |
| DB | < 3 Go |
| Disque libre | > 20 Go |
| `/api/stats` p95 | < 300 ms |
| Queue DB | < 100 lignes / < 64 Mo |

---

## 12. À ne pas faire

- Ajouter des workers uvicorn/gunicorn sans externaliser l'état (quotas, qualité, compteurs) : le single-worker est structurel.
- Passer au round-robin naïf : il ignore le slow lane et le quota.
- Activer le hedge sur **toutes** les requêtes : double le quota et divise le RPM par deux.
- Passer `stream_metric: total` seul : les flux ne sont plus enregistrés dans la qualité (le chemin `total` n'est pas implémenté).
- Lancer un VACUUM pendant le trafic ou le cleanup 60 s après chaque démarrage.
- Stocker 10 Mo de corps par défaut ou plafonner la queue en lignes sans plafond d'octets.
- Se fier au TTFB comme mesure de vitesse : c'est précisément ce qui a laissé passer les IP à 8 tok/s.

---

## 13. Plan de mesure / expériences

1. **A/B 24 h** : sélecteur actuel vs sélecteur débit ; mesurer p50/p95 total, tok/s, 429, quota, slow lane.
2. **Matrice slow lane** : petit prompt × {1,4} concurrence × {chrome,safari} × {IP lente, IP rapide} ; mesurer TTFB, total, tokens, `stop_reason`.
3. **Ramp pool** 1/2/4/8 streams par IP ; noter 429 et tok/s.
4. **Budget token** : corréler le passage en slow lane avec les tokens servis depuis la rotation.
5. **Payant vs free** : latence/coût sur 100 requêtes représentatives.
6. **Réseau** : `curl -w` direct vs SOCKS5 vs container ; DNS/connect/TLS/TTFB.
7. **Profils** eco/balanced/max sur une journée type.

---

## 14. Références code

- Qualité TTFB : `opencode.py:9023`, `opencode.py:10659-10702`, `free/quality.py:109-214`.
- Sélecteur : `free/pool.py:940-1018`, `free/pool.py:540-573`.
- Streaming : `opencode.py:16051-16114`, `streaming/sse.py:38-180`.
- Queue DB : `opencode.py:1150-1170` ; WAL : `opencode.py:1416-1423` ; cleanup : `opencode.py:3015-3044`.
- DB : `observability/db.py:49-50`, `482-499`, `805-855`, `1046-1084`.
- Config : `config.yaml:294-308` (qualité), `355-360` (free_parallel), `374-394` (latence), `609-618` (DB), `625-628` (streaming).
- Stats dashboard : `dashboard/api.py:2428-2441` (alias `from`/`to` déjà ajoutés ; live 0,3–0,7 s contre 3,4 s avant).
- TTFB watchdog payant : `opencode.py:12595-12607` ; non appliqué au free.

---

## 15. Conclusion

Le proxy est déjà bien optimisé localement (orjson, pool curl, cache, rate limiter ASGI, DB batchée, redaction rapide). Le plafond actuel n'est ni le CPU ni le quota : c'est **la qualité de l'IP de sortie et l'absence de mesure du débit réel**. Corriger cela, puis sécuriser la DB/WAL et la sémantique de fin de flux, donnera l'essentiel du gain perceptible. Le reste — modularité, config typée, profils, benchmarks — est ce qui permettra de tenir ce niveau de performance sans régression.
