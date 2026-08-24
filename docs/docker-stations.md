# Stations Docker — services, cycle de vie, superviseurs

> [plan v10 §10.1 Lot 1] Tranche doc livrée avec Lot 1 (sources §11.1 :
> Compose services/healthcheck/restart —
> <https://docs.docker.com/reference/compose-file/services/>).
> Complétée au Lot 2 (procédures scale/debug → runbook).

## Architecture multi-stations

`docker-compose.yml` définit un ancrage `x-gluetun-multi-base` (ligne ~5)
factorisant l'image gluetun, les capabilités `NET_ADMIN`, et le healthcheck.
Chaque station = un service instancié depuis cet ancrage :

| Service | Station | Ports SOCKS/HTTP | Volume nommé |
|---|---|---|---|
| `vpn-gluetun` | 1 | 1080 / 8888 | `gluetun` |
| `vpn-gluetun-2` | 2 | 1079+1 / 8887+1 | `gluetun-2` |
| `vpn-gluetun-N` | N ≤ 10 | 1079+N / 8887+N | `gluetun-N` |

Invariants (contrat §1 du plan) :
- `restart: unless-stopped` sur chaque service — jamais de `compose down`
  global en production.
- healthcheck `interval: 30s`, `start_period: 60s` (durcissement Lot 2 :
  retries + timeout alignés §3.4).
- Le proxy hôte parle au daemon via `/var/run/docker.sock` monté ; le
  conteneur proxy lui-même n'est PAS géré par ce compose.

## Stack par station (`.env`)

Clés `VPN_TYPE_STATION{n}` = `wireguard | openvpn` lues par la substitution
compose. Écrites par `_apply_stack` sous lock inter-managers (fix §14.1.9) ;
les autres clés (secrets NordVPN) ne sont jamais touchées.

## Superviseurs (Lot 1)

- Un `StationSupervisor` par station (`station_supervisor.py`) : état isolé
  (tracker latence par IP, breaker local léger, warm-up post-rotation v6).
- Registry source de vérité inchangée (`shared_state.vpn_managers`) ;
  superviseurs alignés 1:1 dans `shared_state.station_supervisors`.
- **Escape hatch** : `supervisor.enabled: false` (config.yaml, hot-reloadé
  à chaque `_apply_station_count`) supprime tous les superviseurs et rend
  le chemin 100% legacy. Rollback du refactor jusqu'au jalon Train 1 vert.

## Décision ADR-001 (brouillon — finalisé Lot 2)

**Contexte** : le fichier `vpn_manager.py` (4770 lignes) portait l'état de
TOUTES les stations en attributs d'instances parallèles sans isolation
garantie (un bug d'un manager corrompait l'état partagé global).

**Décision** : wrapper par COMPOSITION (`StationSupervisor` porte l'état
isolé, délègue les opérations), pas de fork ni d'héritage — réversible,
zero-risk pour les consumers existants, testable en mock pur.

**Alternatives écartées** : fork du fichier (dérive de maintenance),
refonte complète (risque big-bang, contradictoire avec §10).
