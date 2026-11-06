# Data Model — Trois modes d'egress

**Feature**: `006-three-egress-modes` | **Date**: 2026-11-06

Aucune migration de stockage. Les entités ci-dessous décrivent la configuration
et la présentation ; les clés existent déjà dans `config.yaml` /
`ip_rotation`.

## EgressMode

| Champ | Type | Valeurs | Rôle |
|---|---|---|---|
| `id` | string | `vpn`, `socks5`, `direct` | valeur interne persistée |
| `label` | string | `NordVPN`, `WARP`, `Direct` | libellé GUI FR/EN |
| `backend` | enum | `docker`, `socks5`, `none` | famille de stations |
| `station_kind` | enum | `docker_vpn`, `warp_endpoint`, `none` | type d'objets affichés |
| `capacity_source` | enum | `stations`, `endpoints`, `none` | source de la capacité |

Règles :

- `id` ne change jamais ; `label` peut être traduit.
- Un mode ne peut pas être actif simultanément avec un autre.
- Le mode est lu au début d'une requête free ; une requête en vol conserve son
  egress.

## RoutingParameter

| Champ | Type | Rôle |
|---|---|---|
| `key` | string | clé `ip_rotation` (ex. `free_parallel.routing`) |
| `label_key` | string | clé de traduction GUI |
| `scope` | enum | `common`, `nordvpn`, `warp`, `direct_info` |
| `consumer` | string | module/fonction qui lit la valeur |
| `effect_nordvpn` | string | effet observable en mode `vpn` |
| `effect_warp` | string | effet observable en mode `socks5` |
| `effect_direct` | string | `ignored` ou `not_applicable` |
| `test` | string | test automatisé ou scénario live |

Règles :

- `scope=common` ⇒ le paramètre apparaît dans la section commune et possède un
  effet réel dans les deux backends.
- `scope=nordvpn` ⇒ visible uniquement en mode `vpn` ; ne doit pas être lu par
  le chemin WARP actif.
- `scope=warp` ⇒ visible uniquement en mode `socks5` ; ne doit pas être lu par
  le chemin NordVPN.
- `effect_direct=not_applicable` ⇒ la GUI l'indique ; la valeur reste
  persistée mais n'est pas présentée comme active.
- Une option sans `consumer` est supprimée ou marquée `display_only`.

## ModeCapability

Matrice dérivée de `RoutingParameter` :

```text
Mode          Common   NordVPN-only   WARP-only   Direct-info
NordVPN       oui      oui            non         non
WARP          oui      non            oui         non
Direct        info     non            non         oui
```

`ModeCapability` est utilisée par :

- la GUI pour afficher/masquer les sections ;
- les tests pour vérifier qu'un réglage n'est pas appliqué au mauvais mode ;
- l'audit pour détecter un contrôle sans consommateur.

## StatusSnapshot

| Champ | Type | Rôle |
|---|---|---|
| `mode` | EgressMode | mode actif |
| `configured` | int | stations Docker ou endpoints WARP |
| `usable` | int | éléments sélectionnables |
| `unique_ips` | int | IP réelles distinctes |
| `capacity_degraded` | bool | capacité sous le seuil |
| `capacity_reason` | string \| null | raison lisible |
| `stations` | list | lignes du backend actif |
| `source` | string | `stations` / `endpoints` / `none` |

Règles :

- En `vpn`, `source=stations` et les lignes sont les managers Docker.
- En `socks5`, `source=endpoints` et les lignes sont les `Socks5Endpoint`
  (label, host:port, egress, bloc, enabled, bad_remaining).
- En `direct`, `source=none`, `configured=usable=0`, pas de capacité de
  station.

## CommonRoutingPolicy (vue logique)

Vue de lecture sur `ip_rotation` ; aucun nouveau fichier.

| Clé | Type | Défaut | Portée |
|---|---|---|---|
| `strict_free` | bool | `false` | common |
| `on_429_action` | enum | `both` | common |
| `station_bad_ttl_s` | int | `60` | common |
| `auto_max_free_attempts` | bool | `true` | common |
| `max_free_attempts` | int | `2` | common |
| `free_exception_fallback` | enum | `station-first` | common |
| `free_parallel.enabled` | bool | `false` | common |
| `free_parallel.routing` | enum | `round-robin` | common |
| `free_parallel.mode` | enum | `load-balance` | common |
| `free_parallel.hedge_delay_ms` | int | `1500` | common |
| `free_parallel.hedge_max_attempts` | int | `1` | common |
| `retry_until_success.enabled` | bool | `false` | common |
| `retry_until_success.max_wait_s` | int | `900` | common |
| `quality_preselection.enabled` | bool | `false` | common |
| `quality_preselection.fast_tps` | float | `50` | common |
| `quality_preselection.slow_tps` | float | `20` | common |
| `quality_preselection.max_load` | int | `80` | nordvpn |
| `quality_preselection.tier_b_quota` | int | `250` | common |
| `quality_preselection.tier_c_quota` | int | `60` | common |
| `quality_rotation.*` | object | disabled | common |
| `latency_rotation.*` | object | disabled | common |
| `post_rotation_probe.*` | object | disabled | warp |
| `quota_per_ip` | int | `500` | common |
| `free_prewarm.*` | object | enabled | common |

## Invariants

1. `proxy_mode` reste l'unique source de vérité du mode.
2. Un paramètre `common` est lu par les deux backends sans copie.
3. Un paramètre `nordvpn` n'est pas lu par le sélecteur WARP.
4. Un paramètre `warp` n'est pas lu par le sélecteur NordVPN.
5. `StatusSnapshot` ne mélange jamais les lignes Docker et WARP.
6. Direct ne fabrique pas de capacité de station.
7. Aucun paramètre commun ne contourne `reset_at`, l'unicité d'IP ou le
   ledger.
