# Audit — Gestion des stations, choix d'IP et rotation

Date : 2026-10-26  
Périmètre : `vpn/manager.py`, `free/pool.py`, `free/rotation.py`, `ops/supervisor.py`, `opencode.py`, `dashboard/api.py`, `config.yaml`, flotte Docker locale.  
Méthode : lecture du code + observation read-only de la flotte live (`/api/pool-status`, `/api/vpn-status`, `docker ps/inspect`, sondes SOCKS5, logs). Aucune rotation ni action destructive n'a été déclenchée.

## 0. Verdict exécutif

| Question | Réponse |
|---|---|
| Les stations sont-elles gérées de façon optimale ? | **Non, pas complètement.** Le nominal est robuste (6/6 connectées et routables), mais la gestion de flotte est incomplète et il existe des incohérences d'état réelles. |
| La décision de changer d'IP est-elle optimale ? | **Non.** Elle est déterministe et « fraîche », mais pas pilotée par la performance mesurée. |
| La rotation est-elle fonctionnelle ? | **Oui en nominal**, mais bruitée, coûteuse et sans SLO de succès. |
| Prend-on la meilleure IP possible ? | **Non.** On prend une IP fraîche, dans le pays suivant du curseur, sur un serveur réputé peu chargé — pas l'IP la plus rapide pour cet hôte. |
| Performance | **Moyenne.** Les scans de logs et le rafraîchissement de statut sont trop fréquents et non incrémentaux. |
| Maintenabilité | **Moyenne.** Code très documenté et testé, mais plusieurs chemins morts, duplications de sélection et une dérive `.env` / conteneurs. |

## 1. Ce qui fonctionne bien

- Flotte 1–10 stations, hot-swap sans redémarrage du proxy (`_apply_station_count`, `pool.set_stations`).
- Boot étagé : 2 connexions simultanées maximum + `boot_stagger_s` (anti-rafale AUTH NordVPN).
- Rotation single-flight par station, 2 workers bornés, file prioritaire, cooldowns, breaker, anti-storm.
- Downscale : annulation coopérative des rotations, `stop()` puis suppression du conteneur.
- Registre partagé des IP récentes + curseur d'identité, persisté atomiquement.
- Warm-up post-rotation : la première requête sur une IP neuve n'est pas comptée comme lente.
- Observabilité riche : `/api/pool-status` expose `non_routable_reason`, `egress_state`, compteurs, historique.

## 2. Comment l'IP est choisie aujourd'hui

### 2.1 Sélection de la station (à chaque requête free)

1. `FreeIPPool.on_request()` appelle `ensure_connected()`, puis `_best_station()`.
2. `_station_usable()` filtre : tunnel `connected`/`degraded`, pas de `bad_until`, pas de cooldown latence hard, pas au seuil de quota (selon la passe).
3. Le routing décide :
   - `free_parallel` désactivé ou `failover` : sticky station 1..N ;
   - `round-robin` + `load-balance` : station avec le plus petit `request_count` ;
   - `round-robin` + `strict`/`hedge` : ordre strict 1..N, compteur `_rr_idx`.
4. Le hedge prend le primaire puis, après `hedge_delay_ms`, un second candidat trié par `request_count`.
5. Aucun de ces tris ne regarde la latence mesurée, le taux de succès ou la qualité de l'IP.

### 2.2 Sélection du pays / serveur (à la connexion ou rotation)

1. `_pin_country_for_rotation()` avance le curseur partagé `SharedRotationState.next_country()`.
2. Le pays est choisi par `curseur absolu + offset + stride × (station-1)`, avec exclusion du pays courant et des pays des autres stations.
3. `_control_pin_country_gated()` interroge l'API NordVPN `/v1/servers/recommendations`, trie par `load`, garde le top 5, exclut blacklist et « récemment utilisés ».
4. Le top 5 est envoyé à gluetun via `PUT /v1/vpn/settings` (`hostnames`), puis attente `running` + scan AUTH/TLS + probe IP.
5. En WireGuard, `hostnames` est volontairement vidé : gluetun choisit lui-même le serveur dans le pays.
6. Après probe, `_finalize_ip()` accepte la première IP non récente ; jusqu'à 3 rounds de recovery.

### 2.3 Pourquoi ce n'est pas « la meilleure IP »

- Le pays suit un **curseur de fair-play**, pas un classement de performance.
- Le serveur suit le **`load` NordVPN** (charge côté serveur), pas la RTT/TTFB depuis cet hôte.
- Le top 5 est une short-list ; gluetun peut prendre n'importe lequel des 5, et en WG il n'y a aucune short-list.
- La station qui sert la requête est choisie par **compteur de requêtes**, pas par qualité.
- Aucune sonde active de latence par IP : `ip_latency.py` ne fait que mémoriser les durées des requêtes, il ne sonde pas.
- Le moteur de rotation latence est **désactivé en production** (`latency_rotation.enabled: false`). Même activé, il ne ferait que déclencher une rotation après des requêtes lentes ; il ne classe pas les candidats.
- À la première connexion, `connect()` prend le pays suivant puis **n'importe quelle IP fraîche** ; aucune comparaison de candidats.

Mesure live illustrative (3 GET `api.ipify.org` par proxy, uniquement pour montrer la dispersion) :

| Station | Port SOCKS5 | Latence moyenne | Min |
|---|---:|---:|---:|
| 1 | 1080 | 305 ms | 288 ms |
| 2 | 1081 | 241 ms | 211 ms |
| 3 | 1082 | 357 ms | 307 ms |
| 4 | 1083 | 372 ms | 289 ms |
| 5 | 1084 | 352 ms | 287 ms |
| 6 | 1085 | 317 ms | 222 ms |

La station 2 est ~1,5× plus rapide que la 4 sur cet échantillon. Le round-robin les sert pourtant à égalité.

## 3. Findings prioritaires

### P0-1 — Flapping d'état et fausses erreurs (confirmé live)

**Preuves** : sur la dernière heure, station 1 a produit 69 lignes « resets voie secondaire », 43 « degraded pin-loop », 98 « clearing stale error » et 161 « egress OK ». Sur 2 h : 104 « Serveur VPN injoignable », 58 « healthcheck-restart churn », 214 « clearing stale error ». La station alterne `degraded` ↔ `connected` toutes les ~40 s alors que l'egress est OK.

**Causes** :
1. `refresh_status()` traite des resets OpenVPN de la voie secondaire comme un problème du tunnel effectif, même quand la stack effective est WireGuard et que l'egress répond (`vpn/manager.py:2725-2805`).
2. `_check_server_issue()` n'a pas de frontière de guérison fiable quand la stack effective n'écrit pas `Initialization Sequence Completed` : les vieux resets restent « live ».
3. `_check_restart_churn()` utilise `_restart_churn_recovered_at` sans borne : 4 restarts sur 14 h sont comptés comme un churn et réarment le watchdog (`vpn/manager.py:7349-7387`). Le chemin qui remet `_restart_churn=False` ne remet pas la frontière, donc le scan re-détecte les mêmes marqueurs (`vpn/manager.py:8210-8238`).

**Impact** : bruit logs/SSE, CPU, risque de recovery/restart inutile d'un tunnel sain, statut GUI mensonger, et parfois exclusion temporaire de la station du routage.

**Correctif** :
- Filtrer les signatures de logs par stack effective : ignorer les resets OV sur une station WG, ou les exposer comme métrique informative sans changer le statut.
- Maintenir une frontière `last_effective_ok_at` et ne compter que les marqueurs postérieurs.
- Borner la fenêtre de churn (ex. max 60 min) et imposer un seuil en restarts/minute.
- Remettre la frontière à `now` quand un tick sain efface l'erreur.
- Ajouter un anti-flap : 2 observations consécutives avant de passer en `error`/`degraded`, hystérésis 30–60 s.

### P0-2 — Dérive `.env` / stacks hétérogènes (confirmé live)

**Preuve** : `.env` contient `VPN_TYPE_STATION1..6=wireguard`, alors que les conteneurs 1 et 4 sont en WireGuard et 2, 3, 5, 6 en OpenVPN. La configuration déclarée ne correspond plus à la réalité.

**Cause** : à l'upscale, `_apply_station_count()` appelle `managers[0]._apply_stack(managers[0]._stack_effective)` avec `stations=None`. Même en mode « no-op », `_apply_stack()` réécrit **tous** les `VPN_TYPE_STATION*` actifs avec une seule stack (`opencode.py:2052-2053`, `vpn/manager.py:6397-6466`). En mode `auto` + `auto_mixed_stacks`, cela écrase la répartition WG / OV-TCP / OV-UDP.

**Impact** : au prochain `compose up` hors proxy, ou après une recréation, les 4 stations OV peuvent basculer silencieusement en WG. C'est une violation directe de l'objectif « la stack suit le mode choisi » et une source de reconnexions massives.

**Correctif** :
- `_apply_stack()` doit accepter une map `{station: stack}` et n'écrire que le delta.
- À l'upscale, synchroniser chaque manager avec **sa** stack effective (`apply_mixed_slot()`), jamais celle de la station 1.
- Ajouter un reconcile au boot et un test qui compare `.env` aux `printenv VPN_TYPE` des conteneurs.
- Interdire le flip global en mode `auto_mixed_stacks`.

### P0-3 — Gestion de flotte incomplète et « Disconnect » qui ne tient pas

**Preuves** :
- `/api/vpn/toggle` ne change que `shared_state.vpn_manager.enabled` (station 1) et le miroir du pool ; les autres managers restent `enabled=True` avec watchdogs/updaters actifs (`dashboard/api.py:3533-3547`).
- `/api/vpn/connect` ne connecte que la station 1 (`dashboard/api.py:3550-3577`).
- `/api/vpn/disconnect` appelle `VPNManager.disconnect()`, qui ne fait que changer l'état en mémoire et **ne stoppe pas le conteneur** (`vpn/manager.py:2479-2490`). Le prochain `refresh_status()` peut repasser en `CONNECTED` car aucun flag « disconnect voulu » n'existe.
- `/api/vpn/heal` ne fait rien si le nombre de managers est correct, même si une station est down (`dashboard/api.py:3605-3626`).
- Les endpoints `station/{sid}/restart` et `soft-rotate` existent, mais l'UI n'expose que « Changer d'IP » ; il n'y a pas de start/stop par station (`static/app.js:3612-3631`).

**Impact** : l'opérateur croit avoir déconnecté ou réparé la flotte alors que ce n'est pas le cas. Gestion « intégrale » non atteinte.

**Correctif** :
- Ajouter une API de flotte : `POST /api/vpn/fleet/{connect,disconnect,toggle}` + `POST /api/vpn/stations/{sid}/{start,stop,restart,connect,disconnect}`.
- `disconnect` doit poser un état désiré persistant et arrêter le routage (et, si demandé, les conteneurs), pas seulement l'état mémoire.
- `connect`/`toggle` doivent s'appliquer à tous les managers et au pool.
- `heal` doit reconnecter les stations down, pas seulement créer les manquantes.
- Exposer restart/start/stop dans l'UI par station.

### P0-4 — Persistance des compteurs de quota non fiable (confirmé live)

**Preuve** : `logs/free_pool.json` contient `request_count` 1–3 pour les stations, alors que `/api/pool-status` affiche 47–49 requêtes sur l'IP courante. Le fichier date de 10:08, le process a démarré à 10:06, il est 10:36 : 28 minutes de compteurs ne sont pas persistées.

**Cause** : `save_pool_state()` n'est appelé que sur bad-mark et sur rotation (`free/pool.py:573,1490`). Le « debounce 60 s » ne déclenche aucun flush périodique : sans rotation, rien n'est écrit.

**Impact** : après crash/restart, le proxy sous-estime le quota consommé et peut dépasser la limite per-IP (429), ou ne déclenche jamais la rotation proactive.

**Correctif** :
- Flush périodique (30–60 s) si l'état est dirty + flush au shutdown.
- Sauvegarder aussi `last_confirmed_ip` et refuser de restaurer un compteur si l'IP a changé.
- Test de crash/restart : le compteur restauré doit être à ±1 requête près.
- Nettoyer les valeurs de test (`1.1.1.1`) et isoler le fichier sous pytest.

### P0-5 — La sélection d'IP n'est pas pilotée par la qualité

Voir §2. C'est le point central : le système est **fonctionnel, frais et équitable**, mais pas **optimal**. Il manque un score de qualité par station/IP et une sonde active.

## 4. Findings P1

### P1-1 — Mise à jour d'image non coordonnée à l'échelle de la flotte

Chaque manager fait son propre `docker compose pull` de la même image (`vpn/manager.py:7614-7653`, `7832-7855`). Le premier pull change le digest du tag partagé ; les managers suivants peuvent voir « pas de changement » et ne jamais recréer leur conteneur. Le verrou `vpn_update.lock` ne sérialise que l'application, pas la détection. Résultat possible : flotte mixte ancienne/nouvelle image.

**Correctif** : un seul updater de flotte : pull une fois, comparer l'image ID de chaque conteneur au digest cible, recréer séquentiellement, vérifier chaque station, rollback global.

### P1-2 — `boot_error` reste stale

`/api/vpn-status` affiche encore `boot 5/6` alors que 6/6 sont connectées. `shared_state.boot_error` n'est écrit qu'à la fin du fan-out de boot (`opencode.py:2561-2581`) et jamais recalculé. `scripts/verify_100.py` le traite comme un échec.

**Correctif** : recalculer à chaque changement d'état, ou exposer `boot_error` comme snapshot horodaté et le lever dès que la flotte est complète.

### P1-3 — Scans Docker trop lourds et non incrémentaux

- `refresh_status()` lit `docker logs --since <started_at>` à chaque refresh ; mesuré 476 Ko / 240 ms (station 1) et 1,75 Mo / 479 ms (station 6). À 6 stations et un poll dashboard de 10 s, cela peut coûter ~2 s de subprocess par poll ; avec la rotation de logs à 30 Mo, le budget 2 s explose.
- `_check_restart_churn()` relit les logs depuis la dernière recovery (jusqu'à 14 h dans le cas live).
- Pendant un pin, `_scan_auth_tls()` fait un `docker exec` toutes les 0,5 s, soit jusqu'à ~90 appels par rotation (`vpn/manager.py:5128-5137`).

**Correctif** :
- Lire les logs par incrément (`--tail`, watermark, ou `since` du dernier offset).
- Ne scanner auth/TLS que sur transition ou toutes les 2–3 s.
- S'appuyer d'abord sur `/v1/vpn/status` + healthcheck, et ne lire les logs qu'en cas de doute.
- Augmenter le TTL de statut et ne forcer le refresh que sur événement Docker.

### P1-4 — Seuil de quota probablement mal calibré

`quota_per_ip: 500`, seuil proactif 440–490. Les IP live sont à 47–49 requêtes, et les rotations observées sont surtout réactives (429/erreurs). Si le quota amont réel par IP est sensiblement inférieur à 500, la rotation proactive ne se déclenche jamais et le système dépend des 429.

**Correctif** : estimer le quota réel par modèle/IP à partir des 429 et `Retry-After`, borner `threshold = min(configured, observed_quota - marge)`, avec marge proportionnelle à la concurrence (pas 10 fixes).

### P1-5 — Fenêtre de rotation trop courte face aux pins réels

`rotation_wait_timeout: 5 s` et la grâce `connecting` est aussi de 5 s (`free/pool.py:617-637,2022-2075`), alors qu'un pin contrôle prend typiquement 8–15 s. Après 5 s, la station en rotation est exclue : la requête attend 5 s puis bascule en payant si aucune autre station n'est libre. La promesse « rotation sans coupure » n'est vraie que sur les 5 premières secondes.

**Correctif** : grâce = p95 de durée de pin (ou 20–30 s), avec probe de l'ancienne IP ; ne retirer la station qu'après preuve de mort ou cut-over validé.

### P1-6 — Endpoints de gestion bloquants

`/api/vpn/connect` attend `connect()` (jusqu'à 120 s + rounds), `/api/vpn/next` attend `connect_next()` (jusqu'à 240 s). Sans timeout HTTP ni job id, le client peut abandonner et retenter. `restart` a un timeout 150 s, mais pas `connect`.

**Correctif** : retour `202 Accepted` + job id/SSE de progression, ou timeout explicite avec état consultable.

### P1-7 — Le least-loaded marque toute la short-list comme « utilisée »

`_on_success()` enregistre **tous** les hostnames du top-5 comme récemment utilisés (`vpn/manager.py:5139-5145`), pas seulement le serveur réellement choisi. Le prochain pin exclut donc les 5 meilleurs serveurs, même s'ils sont sains, et descend dans la liste de charge. Cela dégrade la qualité au fil des rotations.

**Correctif** : extraire le hostname réellement utilisé depuis les logs (`_current_hostname`) et n'enregistrer que celui-là. En cas d'incertitude, enregistrer au plus le premier.

### P1-8 — Breaker superviseur mort et clés de config mortes

- `StationSupervisor.record_failure()` / `record_success()` ne sont appelés nulle part en production : le breaker local reste toujours fermé. `build_supervisors()` (qui lit les seuils config) n'est pas utilisé ; le boot passe par `sync_supervisors()`, qui crée les superviseurs avec les seuils par défaut.
- `supervisor.warmup_excluded_requests` est lu par une fonction jamais appelée.
- `latency_rotation.prewarm_after_rotate` est lu mais jamais consommé.
- Les trackers/cooldowns du moteur de latence ne sont pas purgés au downscale : un sid réutilisé peut hériter d'anciens cooldowns.

**Impact** : fausse impression de triple protection ; configuration sans effet ; état résiduel après changement de topologie.

**Correctif** : soit brancher réellement le breaker superviseur, soit le supprimer ; utiliser `build_supervisors` partout ; appeler `warmup_excluded_requests()` dans le tracker ; purge `LatencyRotationEngine` par sid sur downscale ; supprimer ou implémenter `prewarm_after_rotate`.

### P1-9 — Secret dans un fichier tracké

`config.yaml` est tracké par git et contient `control_api_key` en clair (ligne 296). La clé doit être considérée comme compromise si le dépôt est partagé. La faire primer depuis `.env` ne suffit pas tant que le YAML reste tracké.

**Correctif** : retirer la clé du YAML, la déplacer dans `.env`/secret manager, la régénérer, ajouter un test/gitleaks bloquant.

### P1-10 — Image `:latest` et reproductibilité

`docker-compose.yml:26` utilise `qmcgaw/gluetun:latest`. Les commentaires l'assument, mais cela rend les mises à jour non déterministes et complique le rollback. Épingler un digest ou une version, avec un job de mise à jour explicite.

## 5. Findings P2 / hygiène

- **Redondance de downscale** : `stop_container()` fait déjà `compose stop` + `rm -f`, puis `_apply_station_count()` refait un `rm -f` par station retirée et logue « orphan garde-fou » (`opencode.py:2130-2147`). Un seul `rm` suffit.
- **`vpn_manager_2` downscale** : `if new_n >= 2` au lieu de `len(managers) >= 2` (`opencode.py:2159`) ; avec un ensemble non contigu, l'alias peut pointer un manager retiré.
- **`managers[:new_n]`** suppose une numérotation contiguë ; `set_stations()` supporte le non-contigu, pas ce chemin.
- **Pays affiché** : `current_country` est `null` pour 2 stations live ; `get_status()` expose alors la liste complète `server_countries` dans `country`, et l'UI affiche « — ». Il manque une résolution du pays réel (control/geo) quand le pin n'est pas connu.
- **`bad_until`** est un timestamp monotonic exposé brut dans l'API ; préférer `bad_remaining_s` seul ou un timestamp UTC.
- **`_recent_servers`** n'est pas persisté : après restart, les serveurs récemment utilisés peuvent être réutilisés. Le registre d'IP, lui, est persisté.
- **`_recent_servers`** n'est purgé que lorsqu'un host est interrogé ; les entrées jamais revues restent en mémoire (borne ~nombre de serveurs, faible).
- **5 pays pour 6 stations** : deux stations partagent un pays (live : stations 2 et 6 en NL). C'est documenté, mais l'invariant « jamais deux stations sur le même pays » n'est pas tenu. Ajouter des pays ou assumer explicitement la duplication dans l'UI.
- **`_rr_idx`** n'est pas remis à zéro lors d'un `set_stations` : la distribution peut être irrégulière après un scale.
- **`free_prewarm`** ne préchauffe que la station retournée par `pool.proxy_url` ; les autres stations ne sont pas préchauffées après rotation.
- **Hedge** : live 213 victoires primaires / 38 hedge / 251. Le hedge coûte une requête upstream supplémentaire pour ~15 % de gains ; à surveiller si le quota est contraint.
- **Fichier `free_pool.json`** contient `last_confirmed_ip: 1.1.1.1` (valeur de test) : signe d'une pollution du state de prod par les tests.

## 6. Performance — chiffres et priorités

| Point | Coût observé / risque | Action |
|---|---|---|
| `docker logs --since started_at` | 0,24–0,48 s par station, jusqu'à 30 Mo | lecture incrémentale |
| 6 stations × poll 10 s | ~2 s de subprocess par poll | TTL 10 s + refresh sur événement |
| scan pin toutes les 0,5 s | ~90 `docker exec` par rotation | 2–3 s ou control-only |
| churn scan fenêtre non bornée | relit 14 h de logs | borne 60 min |
| flapping station 1 | ~100 lignes/2 h + SSE | corriger P0-1 |
| rotation 17 échecs / 5 breaker en 2 h 49 | MTTR non mesuré | SLO + métriques |
| logs 1,75 Mo / 479 ms (station 6) | proche du timeout 2 s | cache/incrémental |

## 7. Cible recommandée — un vrai score de qualité

Créer un `StationQuality` (ou `IpQuality`) unique, utilisé par **toutes** les sélections :

```
eligible = tunnel_up
        && !bad_until
        && quota_remaining > marge(concurrence)
        && geo_ok
        && breaker_available
        && !hard_cooled

score = 0.40 * latency_score      # EWMA TTFB + p95
      + 0.20 * success_score      # taux 200 / (200+429+timeout)
      + 0.15 * quota_score        # fraîcheur / headroom
      + 0.15 * server_score       # load NordVPN + historique host
      + 0.10 * stability_score    # resets, flips, âge IP
```

Règles :
- Sonde active légère par station (GET statique via SOCKS5, 1 requête / 30 s / station, étalée) ; elle n'utilise pas l'endpoint d'inférence et ne consomme pas le quota.
- Sélection `argmax(score)` avec ε-greedy / part minimale pour éviter la starvation.
- Hedge uniquement si `score(2) > score(1) - seuil` et si le coût est acceptable.
- Rotation si `score < floor` pendant K mesures, ou quota restant < marge, ou 429.
- À la première connexion : classer les pays/serveurs par historique, connecter, sonder, et re-rotation proactive bornée si le score est sous le plancher.
- Rotation : garder l'ancienne IP jusqu'au cut-over validé ; grâce = p95 de pin, pas 5 s.
- Historique par hostname/ASN/pays pour choisir le prochain serveur, pas seulement le `load` instantané.

## 8. Plan d'action ordonné

> Roadmap détaillée par phases, dépendances, efforts, flags et critères de sortie : [ROADMAP_STATIONS_IP_2026-10-26.md](ROADMAP_STATIONS_IP_2026-10-26.md).

### Semaine 1 — arrêter les défauts visibles
1. P0-1 flapping : filtre stack effective + frontière + borne churn + anti-flap.
2. P0-2 `.env` / stacks : sync par station + reconcile + test de dérive.
3. P0-3 flotte : disconnect persistant, connect/toggle fleet-wide, start/stop par station, heal réel.
4. P0-4 persistance quota : flush périodique + shutdown + test crash.
5. P1-2 `boot_error` dynamique.

### Semaine 2 — qualité et performance
6. P1-3 logs incrémentaux + scans moins fréquents.
7. P1-7 n'enregistrer qu'un seul host (le premier de la short-list), pas les 5.
8. P1-4 quota adaptatif + marge selon concurrence.
9. P1-5 grâce de rotation = p95 pin.
10. P1-1 updater de flotte unique.
11. P1-6 endpoints 202/job.

### Semaine 3 — sélection optimale
12. `StationQuality` + sonde active + classement.
13. Sélection par score dans `_best_station`, `pick_candidates`, `_best_station_excluding`.
14. Premier connexion classée + re-rotation proactive bornée.
15. Métriques : `station_score`, `probe_p50/p95`, `rotation_success_rate`, `rotation_p95`, `eligible_stations`, `flap_count_1h`, `image_version_per_station`, `env_drift`.
16. Tests de performance et de non-régression.

### Critères d'acceptation
- 0 flapping de statut > 2 transitions/h/station hors rotation réelle.
- `.env` == `printenv VPN_TYPE` pour chaque station, y compris après upscale.
- `disconnect` : aucune requête free ne sort tant que l'état voulu est « disconnected », même après 5 min.
- Crash/restart : compteur quota restauré à ±1.
- Sélection : sur 3 stations mockées 100/300/600 ms, la plus rapide est choisie ≥ 90 % après warm-up, sans starvation.
- Rotation : succès ≥ 95 %, p95 < 60 s, 0 fallback payant sur rotation réussie.
- Refresh statut : p95 < 100 ms/station, aucune lecture de logs complète.
- Mise à jour : toutes les stations sur la même image après application.

## 9. Conclusion

Le socle est solide : multi-station, anti-rafale, single-flight, registre d'IP, identités, watchdogs. Mais le système optimise aujourd'hui **la fraîcheur, l'équité et la disponibilité**, pas **la meilleure IP**. Et il souffre de deux défauts de gestion réels : une dérive de configuration des stacks et un flapping d'état qui fabrique de fausses erreurs.

La priorité n'est donc pas d'activer le moteur de latence existant, mais de :
1. fiabiliser l'état des stations et la configuration,
2. rendre la gestion de flotte réellement complète,
3. introduire un score de qualité mesuré et une sélection par score.

Tant que ces trois points ne sont pas faits, la réponse à « prend-on la meilleure IP ? » reste **non**.
