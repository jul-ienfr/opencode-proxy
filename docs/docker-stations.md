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

## Décision ADR-001 (finalisée Lot 2)

**Contexte** : le fichier `vpn_manager.py` (~4800 lignes) portait l'état de
TOUTES les stations en attributs d'instances parallèles sans isolation
garantie (un bug d'un manager corrompait l'état partagé global). De plus,
deux P0 lifecycle ont été trouvés par l'audit v8 : le boot reconcile
détruisait une flotte saine sur une heuristique de stack erronée
(§14.1.1), et un downscale pendant une rotation pouvait ressusciter le
conteneur retiré via le shield asyncio (§14.1.2).

**Décision** :
1. Wrapper par **COMPOSITION** (`StationSupervisor` porte l'état isolé —
   tracker latence, breaker local, warm-up — et délègue les opérations au
   manager), pas de fork ni d'héritage ; registry managers nus inchangée ;
   escape hatch `supervisor.enabled: false`.
2. Lifecycle déterministe : la stack attendue au boot vient du **`.env`
   persisté par station** (source écrite par chaque `_apply_stack`), et tout
   downscale passe par `request_rotation_cancel()` (abandon coopératif aux
   checkpoints de `_connect_next_impl`) avant `stop_container`.

**Alternatives écartées** : fork du fichier (dérive de maintenance),
refonte complète (big-ban contradictoire avec §10), cancel brutal des tasks
de rotation (CancelledError sauvage chez les callers 429).

**Conséquences** : rollback du refactor possible jusqu'au jalon Train 1 vert ;
les deux P0 sont couverts par `tests/test_chaos_lifecycle.py` (volet mocké
au gate, volet docker réel opt-in `-m docker`).
