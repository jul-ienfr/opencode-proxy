# Data Model — Auto-Egress

**Date**: 2026-11-07
**But**: entités du mode multi-provider, de la sélection et de l'attribution.

## AutoEgressConfig

| Champ | Type | Défaut | Contrainte |
|---|---|---|---|
| `enabled` | bool | false | mode activé |
| `providers` | list[str] | `[warp, nordvpn, proxies]` | providers existants |
| `weights` | map[str,int] | `{warp:60, nordvpn:30, proxies:10}` | somme 100, chaque ≥0 |
| `max_concurrent` | map[str,int] | `{warp:4, nordvpn:2, proxies:2}` | 1–16 |
| `hedge_delay_ms` | int | 1500 | 100–10000 |
| `hedge_max_attempts` | int | 1 | 0–3 |
| `allow_direct` | bool | false | direct explicite seulement |
| `fail_closed` | bool | true | pas de direct silencieux |
| `diversity` | bool | true | préfère un provider différent |
| `sticky` | bool | true | conversation → egress |
| `sticky_ttl_s` | int | 1800 | 60–86400 |

**Validation**:
- somme des poids = 100 (sinon normalisation avec warning).
- un provider non configuré est ignoré, pas d'erreur fatale.
- `allow_direct=true` exige une confirmation explicite dans le GUI.

## UnifiedEndpoint

| Champ | Type | Contrainte |
|---|---|---|
| `id` | string | provider + label, unique |
| `provider` | enum | `warp`, `nordvpn`, `proxy_socks5`, `proxy_https` |
| `label` | string | nom lisible |
| `endpoint_ref` | string | id interne (station, socks5 label, proxy id) |
| `egress_ip` | string | IP réelle mesurée |
| `block` | string | `/64` ou `/32` |
| `family` | enum | `ipv4`, `ipv6` |
| `health` | enum | `up`, `degraded`, `down`, `unknown` |
| `quota_state` | enum | `ok`, `near`, `exhausted`, `parked` |
| `reset_at` | float | 0 si inconnu |
| `weight` | int | poids effectif du provider |
| `inflight` | int | requêtes en cours |
| `last_ok_at` | float | timestamp |
| `last_error_at` | float | timestamp |
| `last_error_class` | string | taxonomie 007 |

**Règles**:
- `block` est dérivé de `egress_ip`, jamais d'un label.
- deux endpoints ne peuvent pas avoir le même `block` actif.
- `quota_state=parked` exclut l'endpoint jusqu'à `reset_at`.
- `health=down` exclut l'endpoint de la sélection.

## ProviderState

| Champ | Type | Contrainte |
|---|---|---|
| `provider` | string | unique |
| `enabled` | bool | config |
| `available` | bool | au moins un endpoint up |
| `endpoints` | int | nombre d'endpoints |
| `inflight` | int | total |
| `success_rate_1h` | float | 0–1 |
| `latency_p50_ms` | int | ≥0 |
| `last_429_at` | float | timestamp |
| `last_403_at` | float | timestamp |
| `fallback_active` | bool | provider en repli |

## SelectionDecision

| Champ | Type | Contrainte |
|---|---|---|
| `request_id` | string | id de requête |
| `model` | string | modèle free |
| `chosen_provider` | string | provider |
| `chosen_endpoint` | string | id |
| `score` | float | 0–1 |
| `alternatives` | list[str] | endpoints tentés |
| `reason` | enum | `score`, `sticky`, `hedge`, `retry`, `fallback` |
| `decided_at` | float | timestamp |

**Règles**:
- une décision par tentative ; les alternatives sont journalisées.
- un `403` ne crée pas de décision de rotation.
- un `429` quota met à jour `quota_state` du provider concerné.

## RequestAttribution

| Champ | Type | Contrainte |
|---|---|---|
| `request_id` | string | id |
| `provider` | string | provider |
| `endpoint` | string | id |
| `block` | string | egress |
| `status` | int | 200/403/429/5xx |
| `error_class` | string | taxonomie |
| `latency_ms` | int | ≥0 |
| `tokens_in/out` | int | ≥0 |
| `created_at` | float | timestamp |

**Règles**:
- append-only.
- alimente la répartition dashboard et les métriques.
- ne contient aucun secret.

## Relations

```text
AutoEgressConfig 1 ─── * UnifiedEndpoint
ProviderState    1 ─── * UnifiedEndpoint
UnifiedEndpoint  1 ─── * SelectionDecision
SelectionDecision 1 ─── 1 RequestAttribution
```

## Migration

- Aucune table existante détruite.
- Ajout de `auto_egress_config` (ou section `config.yaml`) et des colonnes
  d'attribution dans `registry.db`.
- Les endpoints existants sont importés avec `provider` déduit :
  `managed=true` + `ipv6` → `warp` ; station NordVPN → `nordvpn` ;
  proxy statique → `proxy_socks5`/`proxy_https`.
- Backup `VACUUM INTO` avant migration.
