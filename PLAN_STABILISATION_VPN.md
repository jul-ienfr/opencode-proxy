# Plan de stabilisation VPN/Stations — opencode-proxy-one

Date : 2026-09-05 | Etat : proposé (non implémenté)

---

## Diagnostic résumé (causes racines vérifiées par audit code+logs)

| # | Cause | Preuve |
|---|-------|--------|
| 1 | AUTH_FAILED NordVPN massif | 476+ occurrences logs, TTL blacklist jusqu'à 1440 min, tous pays (fr/de/nl/se/ch) |
| 2 | Alternance proxy_url True/False | Le pool retourne `(None, None)` quand toutes les stations sont en `bad_until` ou `status != connected` — fenêtre de rotation = False |
| 3 | Watchdog trop agressif | `egress_failure_tick_interval: 5.0` (config) vs défaut code 2.0 — probes toutes les 5s, faux egress dead pendant rotations |
| 4 | Flips udp/tcp/wg sans cooldown effectif | `auto_flip_cooldown_min: 15` en config mais logs montrent des flips toutes les ~40s ; canary WG échoue systématiquement (conteneur déjà en use) |
| 5 | Healthcheck docker != egress réel | Le healthcheck gluetun interroge `/v1/vpn/status` local (connecté != egress OK) — containers "healthy" sans sortie réseau |
| 6 | Image `gluetun:latest` non épinglée | Build du 2026-09-02 avec hotfix netlink #3158 déjà inclus — pas de mise à jour nécessaire actuellement |
| 7 | Divergence `proxy_url` (propriété) vs `on_request()` | La propriété n'a ni LRU ni attente rotation — dit False alors que `on_request` servirait une station |
| 8 | Compose escalation conflict | `docker compose up` échoue car conteneur existe déjà — pas de `rm -f` avant recreate |

---

## Lot 1 — Stabilité immédiate (AUTH_FAILED + egress)

### 1.1 Résoudre l'AUTH_FAILED NordVPN

Problème : identifiants corrects mais NordVPN rejette. Cause : trop de tentatives simultanées depuis la même IP résidentielle (rate-limiting côté NordVPN, pas un vrai bad password).

Actions :
- Ajouter un délai inter-tentatives de 30s entre chaque connexion station (actuellement 6 stations tentent en parallèle, 6 AUTH en <2s, NordVPN bloque)
- Limiter à 2 connexions simultanées max lors du boot/rotation
- Cooldown global de 120s après 3 AUTH_FAILED consécutifs sur des stations différentes

Fichiers : `vpn_manager.py` (`_connect_next_impl`, vers ligne 1634)

### 1.2 Corriger le canary WireGuard (conteneur déjà en use)

Problème : `docker compose up vpn-wg-test` échoue car `opencode-wg-test` existe déjà. Verdict toujours "SANS EGRESS", flip WG annulé en boucle.

Actions :
- Avant `compose up` : `docker rm -f opencode-wg-test` si le conteneur existe et est stale (>30min)
- Ou réutiliser le conteneur existant s'il est healthy
- Timeout de 10s sur le verdict canary

Fichiers : `vpn_manager.py` (méthode canary, vers ligne 939+)

### 1.3 Réduire la fréquence des probes egress

Problème : `egress_failure_tick_interval: 5.0` — une probe SOCKS5 toutes les 5s x 6 stations = surcharge + faux positifs.

Actions :
- Remonter à `egress_failure_tick_interval: 15.0` dans `config.yaml`
- Grace period de 30s après connexion réussie avant la première probe egress
- Ne pas déclarer `egress dead` si la station est en cours de rotation (status = `connecting`)

Fichiers : `config.yaml` + `vpn_manager.py` (`_probe_tunnel_light`)

### 1.4 Fix compose escalation conflict

Problème : `docker compose up` échoue avec "container name already in use".

Actions :
- Avant `compose up` : `docker rm -f <container>` si le conteneur existe mais n'est pas dans l'état attendu
- Utiliser `docker compose up -d --force-recreate` au lieu de `up -d`

Fichiers : `vpn_manager.py` (`_compose_up`, vers ligne 3959)

---

## Lot 2 — Cohérence du pool d'IPs

### 2.1 Unifier `proxy_url` (propriété) et `on_request()`

Problème : deux chemins de décision divergents. La propriété `proxy_url` (free_ip_pool.py:290) n'a pas le fallback LRU ni l'attente rotation.

Actions :
- Supprimer la propriété `proxy_url` ou la rendre identique à `on_request()` (même logique `_best_station` + LRU)
- Tout appel externe passe par `on_request()` exclusivement
- Supprimer le `_current_free_attempt` ContextVar qui crée un 3ème chemin

Fichiers : `free_ip_pool.py` (lignes 290-310), `opencode.py` (lignes 5083-5092)

### 2.2 Bad-mark plus intelligent

Problème : `bad_until` = 60s uniforme pour toute cause. Un AUTH_FAILED global marque TOUTES les stations, plus aucune disponible, `proxy_url=False` en rafale.

Actions :
- Distinguer les causes : 429 = 60s, timeout réseau = 30s, AUTH_FAILED = ne pas marquer (problème de compte, pas de station)
- Ne jamais bad-mark plus de N-2 stations simultanément (garder toujours 2 candidates)
- Score de fiabilité par station (EWMA sur les 10 dernières requêtes) pour la sélection

Fichiers : `free_ip_pool.py` (`notify_connection_failure`, `_station_usable`)

### 2.3 Eliminer la fenêtre rotation -> proxy_url=False

Problème : pendant une rotation, `status` passe à `connecting`, la station est exclue, si c'était la seule -> False.

Actions :
- Pendant une rotation, la station reste servable avec son ancienne IP jusqu'à confirmation de la nouvelle
- `on_request()` attend la rotation en cours (max 5s) au lieu de retourner `(None, None)` immédiatement
- Flag `_rotating` distinct du `status` pour ne pas bloquer le trafic

Fichiers : `free_ip_pool.py` (`_station_usable`, `_rotate_station`)

---

## Lot 3 — Watchdog et flips

### 3.1 Anti-oscillation des flips

Problème : logs montrent `flip -> wireguard ANNULE` toutes les 40s. Watchdog propose, canary refuse, recommence.

Actions :
- Après un flip ANNULE, bloquer toute nouvelle tentative de flip pendant 30 minutes
- Compter les AUTH_FAILED sur une fenêtre glissante de 60 min (pas 30)
- Ne proposer le flip WG que si le canary a réussi 2 fois consécutives

Fichiers : `vpn_manager.py` (`_watchdog_tick`, `_watchdog_escalate`)

### 3.2 Escalade progressive (pas de restart immédiat)

Problème : le watchdog restart les conteneurs dès 5-8 ticks d'egress dead. Le restart coupe les tunnels en cours, aggrave.

Actions :
- Séquence d'escalade : (1) attendre 30s -> (2) probe HTTP proxy -> (3) re-pin pays -> (4) restart conteneur -> (5) compose recreate
- Ne JAMAIS restart un conteneur qui a des streams actifs (vérifier `_registered_streams`)
- Cooldown de 120s entre deux restarts du même conteneur

Fichiers : `vpn_manager.py` (`_watchdog_escalate`, `_watchdog_recover_fresh_ip`)

---

## Lot 4 — Configuration cohérente

### 4.1 Corriger les incohérences config.yaml / compose

| Problème | Fix |
|----------|-----|
| `ovpn_protocol: tcp` en config mais compose défaut `udp` | Forcer `OPENVPN_PROTOCOL=tcp` dans `.env` (source unique) |
| `station_count: 6` mais config ne connaît que 2 ports/conteneurs | Ajouter `compose_service_3..6`, ports 8890-8893, socks5 1082-1085 |
| `dual_station: true` (legacy) vs `station_count: 6` | Supprimer `dual_station`, `state_file_2`, `vpn_proxy_port_2`, `socks5_proxy_port_2` |
| `.ovpn` orphelins dans `vpn_configs/` | Supprimer `de1223.ovpn`, `de1227.ovpn` (inutilisables) |
| Double source identifiants (`credentials.env` + `credentials.txt`) | Supprimer `credentials.txt`, garder uniquement `credentials.env` |

### 4.2 Epingler l'image gluetun

Problème : `qmcgaw/gluetun:latest` — un push upstream casse tout.

Action : remplacer par `qmcgaw/gluetun@sha256:89e3cbe22e0d6f09a18d3e86269392fd9f7f08e8991040741e577f8f127cdfe4` (build 2026-09-02, inclut le hotfix netlink #3158).

Note : l'image actuelle EST déjà la dernière (build du 2 sept). Les commits récents (Prometheus metrics, SOCKS5_ALLOWED_CIDRS, DNS fix) ne sont pas encore dans une release taguée. Pas de mise à jour nécessaire pour l'instant.

### 4.3 Healthcheck docker -> vérifier l'egress réel

Problème : le healthcheck actuel interroge `/v1/vpn/status` local (dit "connected" même sans egress).

Action : remplacer par un healthcheck qui tente un vrai téléchargement via le proxy SOCKS5 :
```yaml
healthcheck:
  test: ["CMD-SHELL", "wget -q -O /dev/null -T 8 -e use_proxy=yes -e http_proxy=socks5h://127.0.0.1:1080 https://api.ipify.org || exit 1"]
  interval: 45s
  timeout: 15s
  retries: 4
  start_period: 90s
```

---

## Lot 5 — Streaming (fix résiduel)

### 5.1 Le streaming n'a PAS de bug code supplémentaire

Le streaming retourne maintenant un refus SSE propre (376 octets) au lieu de 0 octet. Quand le VPN fonctionne (`proxy_url=True`), le streaming fonctionne. Son instabilité est 100% liée à celle du pool VPN.

### 5.2 Retry inter-stations en streaming

Quand `proxy_url=False` en mode `station-first`, le code fait 6 tentatives sur la MEME seconde. Ajouter un délai de 2s entre les tentatives free en streaming pour laisser le temps au pool de récupérer.

Fichiers : `opencode.py` (boucle `for _attempt in range(_free_bound)`, vers ligne 9850)

---

## Ordre d'implémentation

| Priorité | Lot | Impact estimé | Effort |
|----------|-----|---------------|--------|
| P0 | 1.1 (AUTH_FAILED cooldown) | Elimine la cause #1 | ~2h |
| P0 | 4.1 (config cohérente) | Elimine les conflits udp/tcp | ~30min |
| P0 | 1.4 (compose conflict fix) | Elimine les erreurs compose | ~30min |
| P1 | 2.2 (bad-mark intelligent) | Réduit proxy_url=False de 80% | ~3h |
| P1 | 3.1 (anti-oscillation flips) | Elimine le spam canary | ~1h |
| P1 | 1.3 (probes moins fréquentes) | Réduit faux egress dead | ~1h |
| P2 | 2.3 (rotation sans interruption) | Elimine les fenêtres False | ~3h |
| P2 | 3.2 (escalade progressive) | Protège les streams actifs | ~2h |
| P2 | 1.2 (canary fix) | Permet les flips WG | ~1h |
| P3 | 2.1 (unifier proxy_url) | Cohérence long terme | ~2h |
| P3 | 4.2 (épingler image) | Reproductibilité | ~10min |
| P3 | 4.3 (healthcheck egress) | Détection fiable | ~30min |
| P3 | 5.2 (délai retry streaming) | Confort | ~30min |

---

## Critère de succès

- proxy_url=True >= 95% du temps (mesuré sur 1h de trafic)
- AUTH_FAILED < 5/heure (vs 476 actuellement)
- 0 flip ANNULE en boucle (max 1 tentative/30min)
- Streaming : réponse avec contenu >= 90% des requêtes
- Egress dead détecté en <30s mais sans faux positifs pendant les rotations
