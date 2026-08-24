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
