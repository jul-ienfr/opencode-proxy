# Runbook Docker — procédures scale/debug/incidents

> [plan v10 §10.1 Lot 2] Procédures opérationnelles. Sources §11.1.
> Complément de `docs/docker-stations.md` (architecture) et `docs/gluetun-station.md` (rotation).

## Scale N stations (hot, sans restart proxy)

1. GUI → VPN → « Nombre de stations » → Appliquer (`POST /api/vpn/station-count`).
2. Le handler `_apply_station_count` :
   - upscale : construit les managers manquants, `.env` re-synchronisé sous
     lock (§14.1.9), `compose up -d` des nouveaux services, `wait_healthy`.
   - downscale : `pool.cancel_rotations([K])` (retire les sids du pool) →
     `request_rotation_cancel()` sur chaque manager retiré (§14.1.2 :
     abandon coopératif des rotations en vol, PAS de résurrection) →
     `stop()` parallèle → `stop_container()` → garde-fou `rm -f` orphelin en
     to_thread (§14.1.19).
3. Vérifier `/api/vpn-status` : `stations.length == N`, chaque carte verte.

## Debug une station

1. Carte station (VPN tab) → bouton **Logs** = `GET /api/vpn/station/{id}/logs?lines=80`
   (docker logs tail borné, lecture seule).
2. Health check ponctuel : bouton dédié → `POST /api/vpn/health-check/{id}`.
3. Filtres per-station dans Stats/Historique : `?station=N`.

## Conteneur absent au boot / stack divergente

Le reconcile boot (`reconcile_orphan_containers`) tourne AVANT le gather de
start :
- conteneur hors registry → `docker rm -f` (volume nommé conservé) ;
- conteneur dont `VPN_TYPE` ≠ stack du **`.env` persisté**
  (`VPN_TYPE_STATION{n}`, fix §14.1.1 — l'ancienne heuristique fichier-clé
  rm -f toute une flotte OV saine si wireguard.env traînait) → rm ;
- start() recrée ensuite ce qui manque sur la bonne stack.

## Rollback station qui ne remonte pas

`compose up -d --force-recreate <service>` ×2 (retry automatique côté code,
Lot 1 §14.1.19 zone). Si toujours KO : `supervisor.enabled: false`
(config.yaml, hot-reload) → chemin orchestration legacy, puis investigation
via Logs. Dernier recours : `git checkout <tag-precedent>` + restore
snapshot (§13.4).

## Docker Desktop ne démarre pas (Windows)

`ensure_docker_running` relance Desktop UNE fois par process ; le latch ne
se pose qu'après un Popen réussi (§14.3.13). Si échec répété : lancer Docker
Desktop à la main, vérifier `docker ps` hors proxy, WSL à jour
(`wsl --update`).

## Chaos tests (optionnels)

`pytest -m docker -q` — exécute le cycle run/kill -9(KILL)/inspect/rm×2 sur
un conteneur jetable `opencode-chaos-*`. Jamais dans le gate par défaut ;
ne touche JAMAIS aux stations gluetun réelles. Les scénarios de churn mockés
vivent dans `tests/test_restart_churn.py` et tournent à chaque gate.

## Incident type : flotte 0/4 + unhealthy + 401 en boucle

Post-mortem complet : `docs/adr/ADR-005-incident-25-08-role-control-server.md`.
Référence config : `docs/gluetun-station.md`.

Symptômes signature :
- logs conteneur : `401 GET /v1/vpn/status wrote 13B` en rafale (y compris
  depuis le healthcheck interne de gluetun) ;
- docker unhealthy / boucle `Restarting` ; churn « N restarts in 10m » ;
- proxy : stations flip connected↔error, GUI tout rouge.

Checks dans l'ordre (⚠️ les timeouts/EPERM sont des conséquences, pas des
causes — FAQ healthcheck) :
1. **Rôle control server présent ?** `grep AUTH_DEFAULT_ROLE credentials.env`
   — attendu : `{"auth":"apikey","apikey":...}`. Absent/`{}` = cause racine
   ADR-005 → restaurer la ligne puis recréer les conteneurs.
2. Egress interne par station :
   `docker exec <ctn> wget -q -O - -T 6 http://api.ipify.org`.
3. DNS interne vs egress pur : hostname qui échoue alors qu'une IP brute
   répond = problème DoT/DNS (cf. gluetun-station.md §DNS).
4. MTU effectif : `docker exec <ctn> cat /sys/class/net/tun0/mtu` — si 1320
   et TLS black-hole, vérifier WIREGUARD_MTU=1280 appliqué.
5. Canary clé WG : `docker compose --profile wg-test up -d vpn-wg-test`.
6. Dernier recours : `rotation_paused: true` (§3.8) pour geler les rotations,
   laisser les backoffs s'apaiser, diagnostiquer à froid.

## Fuseau unique UTC (graceful-aurora LOT G)
- Proxy (logs/debug.log) ET conteneurs (docker logs) loguent en UTC (suffixe Z) — aucune conversion.
- Correlation canonique : docker logs --since 2026-09-07T21:00:00Z <ctr> vs Select-String debug.log (meme horloge).
- Panneau GUI : heure locale d'affichage uniquement, jamais pour correler.
- Rollback : retirer converter = time.gmtime (dashboard/display.py _utc_formatter).
- Ne PAS toucher l'heure des conteneurs (gluetun = UTC natif).

## Patterns audit routabilité — « 1 seule station sur N » (2026-09-08)

Diagnostic express d'abord (dit QUELLE station et POURQUOI, sans deviner) :
`curl -s http://127.0.0.1:4000/api/pool-status` → `N/M routable` +
`non_routable_reason` par station (`bad_until`, `status=`, `auth-cooling`,
`latency-hard-cool`). Le détail par correctif : PC-1→PC-16,
`docs/audit-routabilite-stations-2026-09-08.md` §8.

### Pattern A — AUTH_FAILED NordVPN en rafale (H1)

1. Compter : `Select-String AUTH_FAILED logs/debug.log | Measure-Object`
   (seuil : < 5/h au total ; 300+/h = rate-limit, pas mot de passe).
2. Corréler : rafales simultanées sur N stations au boot/rotation ? → oui =
   connexions groupées. Vérifier `boot_stagger_s` (config.yaml, défaut
   prod 30) et la règle 2-connexions-max.
3. Agir : ne PAS recréer les conteneurs (chaque restart = nouvelle AUTH qui
   nourrit le rejet) — laisser la grâce (`watchdog_auth_grace_s`) + cooling
   travailler ; en dernier recours, `docker compose pull` (liste serveurs
   fraîche) puis **une** rotation manuelle par station, espacées de 30 s.

### Pattern B — TLS « Serveur injoignable » / liste obsolète (H2)

1. Isoler : `docker logs opencode-vpn-N --since 30m | Select-String "tls|TLS|negotiat"`.
   Cluster sur 1-2 stations, autres saines = serveurs morts en cache.
2. Confirmer : station coincée toujours sur le même hostname malgré les
   restarts (le volume `gluetunN` réutilise `/gluetun/servers.json`).
3. Agir : le refresh applicatif tourne seul (incident + périodique
   `server_list_refresh_interval_s: 21600`) ; en forçage manuel :
   `docker exec <ctn> sh -c 'rm -rf /gluetun/servers.json /gluetun/servers'`
   puis `docker compose up -d --force-recreate <service>`.

### Pattern C — boucle restart watchdog (F1/H3)

1. Compter : `Select-String "restarting opencode-vpn" logs/debug.log`
   par station/heure vs `watchdog_max_restarts_per_hour: 3`. Au-delà =
   budget percé (bug si le message `watchdog_restart_budget_exhausted`
   n'apparaît jamais).
2. Lire LA décision : `Select-String "action=" logs/debug.log` — une seule
   ligne par tick (`restart|cooling|grace|budget-exhausted`). Deux lignes
   contradictoires à la même seconde = régression PC-3.
3. Agir : `budget-exhausted` + `auth_cooling` = escalade nominale, ne rien
   faire ; `gluetun non sain après redémarrage` en boucle = vérifier le
   healthcheck egress (`docker inspect <ctn> --format "{{.State.Health.Status}}"`,
   PC-6 : unhealthy = pas d'egress réelle) puis image gluetun
   (`docker compose pull`, tag `:latest` voulu en attendant v3.42).

### Garde-fous à connaître

- `[pool] invariant_n2` dans les logs = la garde anti-1/N a converti une
  station en servable dégradé : normal, pas une panne.
- `[POLICY] pays refusé hors périmètre` = pin hors `server_countries`
  bloqué (F4) ; `egress_allow_any_country: true` pour autoriser.
- `[watchdog] hb` toutes les 5 min par station : son absence > 10 min =
  proxy muet/planté (F10), pas un réseau calme.
- `[extern] sN conteneur X (re)créé HORS proxy` : quelqu'un d'autre que le
  proxy (terminal, GUI Docker) a recréé le conteneur. Le proxy n'y est
  pour rien — ne pas accuser le watchdog, chercher qui (historique shell,
  Docker Desktop).
- `[free-usage]` : `egress_ip` = sortie tunnel, `client_ip` = demandeur ;
  un `egress_ip` en 192.168.x = sonde anomalie à investiguer (F9).

