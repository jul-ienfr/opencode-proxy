# Data Model — WARP primaire + replis d'egress free

**Date**: 2026-11-07
**Portée**: entités nécessaires au provider-agnosticisme, au park/reset, à la
déduplication et au fallback.

## EgressEndpoint

Station logique exposée au pool. Remplace le `Socks5Endpoint` actuel par un
objet enrichi, sans casser la compatibilité (`provider` absent → WARP).

| Champ | Type | Contrainte |
|---|---|---|
| `label` | string | unique, non vide, stable |
| `provider` | enum | `warp`, `warp_alt`, `isp_ipv6`, `oracle`, `proton`, `windscribe`, `vps_ipv6`, `static` |
| `host` / `port` | string / int | port 1–65535 |
| `ipv6` | bool | `true` → egress IPv6 obligatoire |
| `managed` | bool | `true` → rotation contrôlée par le proxy |
| `egress_ip` | string | IP réelle mesurée, jamais `socks5:<label>` |
| `block` | string | `/64` ou `/32` normalisé |
| `reset_at` | float | timestamp fournisseur, 0 si inconnu |
| `state` | enum | `connecting`, `connected`, `degraded`, `parked`, `exhausted`, `disabled` |
| `last_429_at` | float | dernier quota observé |
| `last_ok_at` | float | dernier `200` prouvé |
| `fail_count` | int | échecs consécutifs |
| `provider_ref` | string | id conteneur/gateway/listener |

**Validation**:
- `egress_ip` doit être une IP valide (pas `socks5:*`).
- Pour `ipv6=true`, `egress_ip` doit être IPv6 ; une IPv4 est rejetée.
- `block` doit correspondre à `egress_ip` (`/64` IPv6, `/32` IPv4).
- Un `label` ne peut apparaître qu'une fois.
- Un bloc ne peut être affecté qu'à un endpoint à la fois (claim exclusif).

**State transitions**:

```text
connecting → connected → degraded → parked → exhausted
     │            │           │          │
     └────────────┴───────────┴──────────┴→ disabled
```

- `connected` : probe 200 récent.
- `degraded` : échec transitoire, pas de bloc épuisé.
- `parked` : bloc épuisé jusqu'à `reset_at`.
- `exhausted` : quota confirmé sans reset proche.
- `disabled` : opérateur ou provider off.

## EgressBlock

Bloc de quota. Clé primaire `(block, provider)`.

| Champ | Type | Contrainte |
|---|---|---|
| `block` | string | `/64` IPv6 ou `/32` IPv4 |
| `provider` | string | provider d'origine |
| `first_seen_at` | float | timestamp |
| `last_seen_at` | float | timestamp |
| `reset_at` | float | fournisseur, 0 si inconnu |
| `exhausted` | bool | quota confirmé |
| `source` | enum | `observed`, `inferred`, `probe`, `migrated` |
| `last_error` | string | `FreeUsageLimitError`, `RateLimitError`, etc. |
| `evidence_ref` | string | chemin de preuve live |

**Règles**:
- Un bloc est unique par provider ; le même `/64` chez deux providers est
  deux blocs distincts pour la comptabilité provider, mais un seul egress
  physique à un instant donné.
- `reset_at` vient exclusivement du fournisseur (`Retry-After`, corps 429).
- Un `403` ne crée ni ne modifie un bloc.

## FallbackPolicy

Configuration ordonnée, dans `config.yaml`.

| Champ | Type | Défaut | Contrainte |
|---|---|---|---|
| `order` | list[str] | `[warp, warp_alt, isp_ipv6, oracle, proton, windscribe, vps_ipv6]` | WARP en premier |
| `max_fallback_stations` | int | 2 | 1–2 |
| `probe_interval_s` | int | 300 | ≥ 60 |
| `recovery_successes` | int | 2 | 1–5 |
| `cooldown_s` | int | 900 | ≥ 300 |
| `max_block_s` | int | 0 (strict) | 0 = pas de probe anticipé |
| `fail_closed` | bool | true | jamais de direct silencieux |

**Déclencheurs**:
- quota après épuisement des rotations WARP ;
- échec/throttle WARP > 10 min ;
- conteneur down > 2 min ;
- bloc qui revient épuisé plusieurs fois ;
- override manuel.

**Non-déclencheurs**:
- `403 FreeTierError` ;
- `RateLimitError` court ;
- `5xx` transitoire ;
- erreur d'auth/version.

## QuotaObservation

Journal append-only des observations de quota.

| Champ | Type | Contrainte |
|---|---|---|
| `id` | int | PK auto |
| `model` | string | modèle free demandé |
| `block` | string | bloc observé |
| `provider` | string | provider |
| `status` | int | 200/403/429/5xx |
| `error_class` | enum | `GATE_403`, `QUOTA_429`, `RATE_LIMIT_429`, `TRANSIENT_5XX`, `ACCOUNT_OR_GLOBAL`, `OK` |
| `retry_after_s` | float | 0 si absent |
| `reset_at` | float | 0 si inconnu |
| `observed_at` | float | timestamp |
| `evidence_ref` | string | preuve brute |

**Règles**:
- append-only ; pas d'update.
- Un `200` ne compte que si le contenu est valide et sans `event: error`.
- Un `403` est journalisé mais ne marque pas le bloc.

## ProviderHealth

| Champ | Type | Contrainte |
|---|---|---|
| `provider` | string | unique |
| `enabled` | bool | config |
| `available` | bool | probe récente |
| `last_probe_at` | float | timestamp |
| `last_success_at` | float | timestamp |
| `last_failure_at` | float | timestamp |
| `last_error` | string | raison |
| `fallback_active` | bool | station en fallback |

## GatewayAllocation (optionnel VPS)

| Champ | Type | Contrainte |
|---|---|---|
| `block` | string | `/64` dans le `/48` |
| `port` | int | 11090–11199 |
| `source_ip` | string | adresse stable dans le bloc |
| `label` | string | station |
| `state` | enum | `allocated`, `active`, `draining`, `released` |
| `allocated_at` | float | timestamp |
| `released_at` | float | 0 si actif |

**Règles**:
- Pas de chevauchement de blocs.
- Pas de réseau `::`, multicast, link-local.
- Persistance SQLite ; rechargement au boot.
- Libération explicite avant réallocation.

## Relations

```text
EgressEndpoint 1 ─── * QuotaObservation
EgressEndpoint 1 ─── 1 EgressBlock        (bloc courant)
EgressBlock    1 ─── * QuotaObservation
ProviderHealth 1 ─── * EgressEndpoint
GatewayAllocation 1 ─── 1 EgressEndpoint  (VPS uniquement)
```

## Migration

- `Socks5Endpoint` existant : `provider` absent → `warp` si `managed=true`,
  sinon `static`.
- `registry.db` : ajouter colonnes `provider`, `block_key`, `quota_axis`,
  `reset_at` avec valeurs par défaut ; migration versionnée, backup
  `VACUUM INTO` avant.
- Aucune suppression de colonne ni de table dans cette phase.
