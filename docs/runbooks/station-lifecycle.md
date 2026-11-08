# Runbook — cycle de vie des stations NordVPN et WARP

**Version** : 011, 2026-11-07  
**Périmètre** : jambe free, stations NordVPN Gluetun et endpoints Cloudflare WARP.  
**Objectif** : six stations client-utilisables, IP/buckets uniques, réparation
automatique, rotation sans perte de slot.

## 1. Vocabulaire

- **Station / slot** : unité client-utilisable (conteneur, tunnel, port SOCKS).
- **Endpoint** : point de sortie (station NordVPN, conteneur WARP, proxy,
  direct).
- **Bucket** : unité de quota ; `/32` IPv4, `/64` IPv6.
- **Client-utilisable** : preuve récente de contenu via une requête client,
  pas seulement `connected` ou `healthy`.
- **Desired state** : ce qui doit exister (`DesiredStationSpec`,
  `DesiredEndpointSpec`).
- **Réparation** : action automatique avec cause et preuve.
- **Rotation** : remplacement d’IP/session sans perdre le slot.

## 2. Invariants

1. Une station n’est jamais `client_usable` sans preuve de contenu.
2. Deux endpoints ne partagent jamais le même bucket actif.
3. Un conteneur conforme au desired state n’est jamais supprimé.
4. Un seul propriétaire répare : le superviseur.
5. La maintenance est indépendante de `egress_providers`.
6. Les écritures `.env` sont atomiques et sérialisées.
7. Les budgets de rotation sont persistés dans `registry.db`.
8. Un 429 respecte `reset_at` ; aucune IP n’est réessayée avant.
9. Le proxy démarre même si une station est down.
10. Aucune ressource payante, aucun second compte, aucune attente comme
    solution.

## 3. Configuration de référence

```yaml
ip_rotation:
  proxy_mode: socks5
  egress_providers: [warp]
  station_count: 6
  station_max_rotations_per_hour: 10
  repair_owner: supervisor
  station_repair:
    enabled: true
  egress_capacity:
    target_slots: 6
    reserve_policy: full
```

NordVPN reste maintenu même s’il n’est pas dans `egress_providers` :
`_nordvpn_maintained()` ne dépend pas de la sélection trafic.

## 4. Création

### NordVPN

1. Vérifier `station_count` (1–10) et `.env`.
2. Vérifier les credentials :
   `python scripts/refresh_nord_credentials.py --check`.
3. Vérifier les services compose déclarés (`vpn-gluetun`…`vpn-gluetun-10`).
4. Démarrer :
   `docker compose --profile multi-station up -d vpn-gluetun`.
5. Attendre le healthcheck, puis valider le SOCKS :
   `curl --socks5-hostname 127.0.0.1:1080 https://api.ipify.org`.
6. Vérifier l’unicité de l’IP dans `/api/stations`.

### WARP

1. Vérifier `ip_rotation.socks5_proxies` (`provider=warp`, `managed=true`,
   `enabled=true`).
2. Appliquer le provisioner :
   `python scripts/warp_recreate_loopback.py --apply`.
3. Vérifier `127.0.0.1:<port>->1080` dans `docker ps`.
4. Vérifier 6 `/64` uniques.
5. Rafraîchir l’egress :
   `POST /api/vpn/socks5/refresh-egress`.

## 5. Maintenance quotidienne

```powershell
python scripts/station_audit.py --json
curl.exe -s http://127.0.0.1:4000/api/stations
curl.exe -s http://127.0.0.1:4000/api/egress/capacity
```

Contrôles :

- `client_usable` = 6 pour WARP, 6 pour NordVPN ;
- `unique_buckets` = 6 par provider ;
- `duplicates` vide ;
- `capacity_degraded=false` ;
- aucun `rm -f` injustifié dans `logs/debug.log` ;
- `rotation_jobs` et `repair_jobs` cohérents.

## 6. États d’une station

```text
created → starting → tunnel_up → dns_ok → egress_ok → quota_ok
        → client_usable → degraded → repairing → parked → failed
```

`connected` n’est pas `client_usable`.

## 7. Rotation

### WARP

- déclencheur : 429/quota, bloc dupliqué, timeout, rotation manuelle ;
- budget horaire persisté (`rotation_jobs`) ;
- une seule recréation par rotation ;
- post-probe OpenCode avant d’accepter le bloc ;
- écriture `egress_ip`/`block`/ledger après succès.

```powershell
curl.exe -X POST http://127.0.0.1:4000/api/vpn/socks5/rotate-egress
curl.exe -X POST http://127.0.0.1:4000/api/vpn/socks5/refresh-egress
```

### NordVPN

- déclencheur : 429, serveur refusé, egress mort, rotation manuelle ;
- budgets `station_max_rotations_per_hour` et `rotation_storm_cooldown_s` ;
- fast-pin sur hostname noirlisté ;
- vérifier l’IP après rotation et l’unicité.

```powershell
curl.exe -X POST http://127.0.0.1:4000/api/vpn/station/1/soft-rotate
curl.exe -X POST http://127.0.0.1:4000/api/vpn/station/1/probe
```

## 8. Incident — AUTH_FAILED

1. `python scripts/refresh_nord_credentials.py --check` :
   - `401` → token invalide, obtenir un nouveau token ;
   - `200` → credentials valides.
2. Vérifier `credentials.env` (OPENVPN_USER/PASSWORD) et
   `vpn_configs/wireguard.env` (clé NordLynx).
3. Arrêter toutes les stations, en démarrer **une**, vérifier l’egress.
4. Ajouter les stations une par une ; le premier `AUTH_FAILED` indique la
   limite de sessions.
5. Ne pas rotationner en boucle : un AUTH_FAILED n’est pas une mauvaise IP.
6. Aucun paid, aucun second compte.

Script de mesure :

```powershell
python scripts/measure_nordvpn_sessions.py --apply
```

## 9. Incident — doublon de bucket

1. `python scripts/station_audit.py --json` → `duplicates`.
2. Identifier les labels.
3. Tourner un des endpoints :
   `POST /api/vpn/socks5/rotate-egress`.
4. Vérifier `unique_buckets` et `duplicate_stations=0`.
5. Si WARP réutilise le même `/64`, recréer le conteneur et refaire le probe.

## 10. Incident — conteneur WARP mort

1. `docker ps` : absent/exited.
2. `POST /api/vpn/socks5/refresh-egress` puis `rotate-egress`.
3. Le pool demande une rotation sur `timeout` pour un endpoint managé.
4. Vérifier le healthcheck et l’egress après recréation.

## 11. Incident — `.env` divergent

1. Vérifier `VPN_TYPE_STATIONn` dans `.env` et `docker inspect`.
2. Le reconcile ne supprime plus un conteneur vivant.
3. Écrire via `ops.env_store.update_env` (atomique + verrou).
4. Recréer une seule fois si nécessaire, puis vérifier.

## 12. Incident — proxy down

1. `GET /healthz` ; vérifier le processus `opencode.py`.
2. Le proxy doit écouter même si une station est down.
3. En mode dégradé, `/api/stations` doit montrer le déficit réel, jamais un
   faux 6/6.
4. Redémarrer le proxy : `pythonw opencode.py`.

## 13. Rollback

- Configuration : restaurer `config.yaml.bak-*` / `.env.bak-*`.
- Registre : `Copy-Item logs/backups/registry-<ts>.db logs/registry.db`.
- Code : `git diff` est la trace ; pas de commit automatique.
- Après rollback : redémarrer proxy + superviseur, puis `station_audit.py`.

## 14. État connu au 2026-11-07

- WARP : 6/6 client-utilisables, 6 `/64` uniques, loopback.
- NordVPN : 6 conteneurs, 3/6 client-utilisables selon les probes ; 1, 3, 6
  alternent `AUTH_FAILED`/tunnel sans réponse. Credentials valides ; blocage
  compte/session/réseau à mesurer avec un transport validé.
- Proxy : `/healthz=200`, mode `socks5`, providers `[warp]`.
- Tests : 27 plan, 23 modules, 4 budget/env, 4 adapters, 5 XPASS live.

## 15. Références

- `docs/AUDIT_STATIONS_NORDVPN_WARP_2026-11-07.md`
- `docs/evidence/011-implementation-2026-11-07.md`
- `specs/011-station-fleet-usability/`
- `ops/lifecycle.py`, `ops/reconciler.py`, `ops/env_store.py`
- `free/warp_provisioner.py`, `free/egress_reconcile.py`
- `scripts/station_audit.py`, `scripts/warp_recreate_loopback.py`,
  `scripts/measure_nordvpn_sessions.py`
