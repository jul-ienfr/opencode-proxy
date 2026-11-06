# Contrat — Provisioning des egress

**Statut**: conception. Aucun code dans cette itération.

## État désiré

```yaml
free_egress:
  mode: warp                 # vpn | warp | socks5 | direct
  station_count: 5
  managed: true
  warp:
    provider: caomingjun     # caomingjun | warp-minimal
    base_port: 11080
    ipv6: true
    image: caomingjun/warp:latest
  vpn:
    provider: nordvpn
    transport: wireguard
    dns: dot
    max_stations: 6
```

## Interface Provider

```
plan(desired, actual) -> Plan
apply(plan) -> Run
health(station) -> StationHealth
```

`Plan` :

- `create[]` : nouvelles stations (label, mode, port, options) ;
- `keep[]` : stations déjà conformes ;
- `remove[]` : stations surnuméraires ;
- `verify[]` : stations à vérifier (create + keep).

## Implémentations

### VpnProvisioner

- réutilise `_apply_station_count(n)` et le compose/gluetun existant ;
- transport WireGuard + DNS DoT (recette validée 2026-11-06) ;
- vérifie l'auth et l'egress de chaque station ;
- respecte `vpn.max_stations` (limite de sessions mesurée) ;
- ne touche pas aux stations hors `managed`.

### WarpProvisioner

- `caomingjun/warp` : un conteneur par station, port `base_port + i`,
  label `warp-ipv6-<i>`, `ipv6: true` ;
- `warp-minimal` (optionnel) : un processus, plusieurs exits MASQUE ;
- vérifie un `/64` distinct par station ;
- écrit les entrées `socks5_proxies` **managed** (pas les proxies manuels) ;
- recharge le pool sans redémarrer le proxy.

## Plan / Apply

```
apply:
  lock (409 si déjà en cours)
  preflight:
    - docker disponible
    - ports libres
    - limites provider
    - config valide
  créer les nouvelles
  vérifier:
    - egress répond
    - probe free 200 (ou quota explicite)
    - audit identité (disjoint, pas de fuite)
  activer les nouvelles dans le pool
  drainer/retirer les anciennes
  persister la config en dernier
  unlock
```

## Invariants

- **EP-01** Idempotent : même état désiré → aucun conteneur recréé.
- **EP-02** Blue/green : les anciennes restent actives jusqu'à vérification.
- **EP-03** Échec → retirer les nouvelles, conserver les anciennes.
- **EP-04** Un seul apply à la fois (verrou + `409 busy`).
- **EP-05** Ne jamais écraser un proxy `socks5` manuel.
- **EP-06** Chaque station active a un bloc d'egress distinct (dédup IP/`/64`).
- **EP-07** La config est persistée en dernier, après convergence.
- **EP-08** Les secrets (tokens, credentials) ne sont jamais loggés.
- **EP-09** Un station_count hors limites est refusé, pas clampé.
- **EP-10** Un endpoint SOCKS5 statique n'est jamais évalué avec le contrat
  santé Docker (auth/tunnel) ; `enabled`, `bad_until` et le quota suffisent.
- **EP-11** Le cooldown d'une station est keyé sur son egress réel (`/64`
  IPv6), jamais sur son label stable, pour qu'une rotation débloque le nouveau
  bloc immédiatement.

## Limites

| Provider | Limite | Comportement |
|---|---|---|
| VPN | ~5–6 sessions | refus au-delà, message |
| WARP caomingjun | throttle registration | vagues + backoff |
| WARP warp-minimal | throttle registration | provider optionnel |

## Tests

- plan `warp 0 → 3` → 3 create, 0 remove ;
- plan `warp 3 → 3` → 0 create, 0 remove (idempotent) ;
- plan `warp 5 → 3` → 2 remove ;
- échec de la 3ᵉ création → rollback, 2 anciennes conservées ;
- apply concurrent → 409 ;
- proxy manuel dans `socks5_proxies` → conservé ;
- station avec `/64` dupliqué → exclue.
