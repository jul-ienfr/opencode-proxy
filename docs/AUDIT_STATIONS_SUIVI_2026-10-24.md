# Audit stations/VPN/IP — décisions actées (2026-10-24)

Suivi d'implémentation du plan d'audit. Détails code : voir commentaires
`[audit-2026-10-24]` dans le diff.

## Décisions

1. **Single-worker gunicorn obligatoire** (`gunicorn.conf.py workers=1`).
   État en mémoire (pools, rings, SSE, SQLite single-conn) : multi-workers =
   sur-quota N×500 + rotations concurrentes même IP. Pas de Redis prévu :
   le pool deviendrait un service externe (complexité >> gain). Documenté
   ici, pas de changement runtime.

2. **socket-proxy opt-in, jamais activé par défaut**.
   `docker-socket-proxy` (profil `socket-proxy`) validé `compose config`
   uniquement. Risque actuel assumé et tracé (`docker-compose.yml:144`) :
   RCE proxy = contrôle daemon hôte. Pour durcir : `compose --profile
   socket-proxy up -d` + `DOCKER_HOST=tcp://docker-socket-proxy:2375` dans
   `.env` + retrait montage `docker.sock` + `group_add`. Monitoring
   `vpn-alerts` (profil `monitoring`) tourne sans `docker.sock`.

3. **Flip stack global conservé (1 commande)**.
   Séquentiel par service rejeté : N AUTH séquentielles + N fenêtres
   d'échec partielles + rupture contrat `test_vpn_stack_nstation`.
   Blast-radius tracé par warning log (`_apply_stack: flip GLOBAL`) ;
   auto hétérogène utilise déjà `stations=[...]` per-station. Préférer
   flips per-station en auto.

4. **Warm-up latence : ring/EWMA conservés**.
   Exclure la 1ère requête du ring/EWMA biaise `request_count` et casse
   `test_tracker_min_requests_gate` + contrat v6 (docs + 6 mois d'historique).
   Le biais handshake reste connu, magnitude faible (1 point / 20 ring,
   α=0.3 log-space).

5. **Moteur latence OFF en prod** (`latency_rotation.enabled:false`).
   Tout le code latence est inerte par config. Activation = canary manuel :
   `POST /api/vpn-config {"latency_rotation":{"enabled":true}}`,
   observer `pool_usable`, `slo`, `canary`, `watchdog_restart_total` via
   `scripts/vpn_alerts.py --once`, rollback `enabled:false` si flap.

6. **Secrets** : `VPN_CONTROL_API_KEY` (ou `CONTROL_API_KEY`) dans `.env`
   (chmod 600, gitignoré) prime sur `config.yaml:control_api_key` au boot
   (`config/settings.py`). Le YAML reste fallback compat + warning.
   Rotation : régénérer 32 chars, `python scripts/make_credentials_env.py`,
   `compose up -d --force-recreate` stations.

7. **Breakers** : priorité Engine latence > pool `bad_ttl_by_cause` >
   manager `CircuitBreaker(3/300s)` > superviseur local (aligné sur manager
   via `build_supervisors`). Triple marquage conservé (pas de fusion :
   chaque niveau a sa sémantique — amont corrélé / station / serveur).

8. ** Persistance ** : `logs/shared_rotation.json` (sync immédiat, petit
   fichier — debounce refusé : casse `test_round_trip_reload`),
   `logs/latency_engine.json` (débouncé 60s, skip pytest),
   `logs/free_pool.json` (quotas/ip_stats, débouncé 60s + force sur
   rotation), `logs/vpn_state*.json` + `.bak`, `logs/identities/*.json`.

9. ** Geo vs free ** : même egress, deux faces assumées (navigateur curated
   sur jambe geo, Bun vault sur jambe free — protocoles différents).
   Dashboard expose les deux (`wire_identity` + `current_identity`).

## Reste ouvert

- Coordinateur flotte unique vs 6 watchdogs (compteur `skipped_ticks`
  ajouté pour mesurer les fenêtres aveugles avant de trancher).
- Vérification host-side firewall + DNS-leak continue (aujourd'hui seul
  `scripts/vpn_e2e_smoke.py` ponctuel).
- Logs JSON structurés / OTel (grep-only aujourd'hui).
