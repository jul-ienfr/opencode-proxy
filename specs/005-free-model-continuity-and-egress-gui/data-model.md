# Data model — Modèle free, egress et identités

**Feature**: `005-free-model-continuity-and-egress-gui`  
**Statut**: conception.

## 1. Modèle free

### 1.1 `FreeModelCatalog`

| Champ | Type | Source |
|---|---|---|
| `free_ids` | set[string] | `FREE_MODELS` |
| `models` | map[id → {endpoint, protocol}] | `MODELS` |
| `map` | map[paid → free] | `FREE_MODEL_MAP` |
| `default_target` | string | `free_discovery.default_target` |
| `pinned_target` | string | config |
| `preference` | list[string] | config |

### 1.2 `ModelProbe`

| Champ | Type | Source |
|---|---|---|
| `id` | string | `free_models_probed.json` |
| `verdict` | `alive/limited/dead` | probe |
| `status` | int/null | probe |
| `reason` | string | probe |
| `checked_at` | timestamp | probe |

### 1.3 `ModelSelection`

| Champ | Type |
|---|---|
| `target` | string |
| `source` | `config/auto/pinned/none` |
| `previous` | string |
| `reason` | `kept/missing/dead/no_alive/pinned` |
| `changed_at` | timestamp |

## 2. Egress

### 2.1 `EgressDesiredState`

| Champ | Type | Contrainte |
|---|---|---|
| `mode` | `vpn/warp/socks5/direct` | `vpn` ou `warp` en GUI |
| `station_count` | int | 1–10, ≤ limite provider |
| `managed` | bool | ne touche qu'aux stations gérées |
| `warp.provider` | `caomingjun/warp-minimal` | défaut `caomingjun` |
| `warp.base_port` | int | 1024–65000 |
| `warp.ipv6` | bool | true pour `/64` |
| `vpn.transport` | `wireguard` | recette validée |
| `vpn.dns` | `dot` | recette validée |
| `vpn.max_stations` | int | limite mesurée |

### 2.2 `Station`

| Champ | Type |
|---|---|
| `label` | string unique |
| `mode` | `vpn/warp` |
| `host` | string |
| `port` | int |
| `egress_ip` | string |
| `block` | string (`/32` ou `/64`) |
| `state` | `provisioning/healthy/cooling/failed/removed` |
| `last_error` | string/null |
| `last_success_at` | timestamp |
| `identity_ok` | bool |

### 2.3 `ProvisioningPlan`

| Champ | Type |
|---|---|
| `create` | list[StationSpec] |
| `keep` | list[label] |
| `remove` | list[label] |
| `verify` | list[label] |
| `warnings` | list[string] |

### 2.4 `ProvisioningRun`

| Champ | Type |
|---|---|
| `id` | string |
| `plan` | ProvisioningPlan |
| `status` | `running/ok/failed/rolled_back` |
| `steps` | list[{label, action, state, error}] |
| `started_at` / `finished_at` | timestamp |

## 3. Identité

### 3.1 `StationIdentity`

| Champ | Type | Contrainte |
|---|---|---|
| `label` | string | unique |
| `project` | 40 hex | unique, pas `global` |
| `session_state` | {last_ms, counter, lock} | isolé par station |
| `trace` | 32 hex | stable, unique |
| `env_cwd` | string | `/workspace/<label>` |
| `ua` | string | profil vérifié |
| `client` | `cli` (canonique) | partagé, migré si `desktop` hérité |

### 3.2 `IdentityTuple`

| Champ | Type |
|---|---|
| `project` | string |
| `session` | `ses_…` |
| `request` | `msg_…` |
| `trace` | string |
| `span` | string |
| `env_cwd` | string |

### 3.3 `ConversationAffinity`

| Champ | Type |
|---|---|
| `key` | sha256(model/system/first_user/conversation) |
| `station` | label |
| `created_at` / `last_used_at` | timestamp |
| `ttl_s` | int |
| `failover_count` | int |

### 3.4 `IdentityAudit`

| Champ | Type |
|---|---|
| `status` | `ok/duplicate_identity/prompt_leak/shared_session` |
| `stations` | list[{label, project_prefix, session_prefix, trace_prefix}] |
| `duplicates` | list[string] |
| `leaks` | list[{label, pattern}] |
| `shared_vectors` | list[`tls_ja3`/`ua`/`client`/`bearer`] |
| `checked_at` | timestamp |

### 3.5 `VerifiedProfile`

| Champ | Type | Contrainte |
|---|---|---|
| `profile_id` | string | unique |
| `version` / `client` | string | couple officiel |
| `tls_family` | string | `bun-1.3.14` / `bun-1.4.4` |
| `ja3` / `ja3_md5` | string | cohérent avec la famille |
| `body_capture` | path + sha256 | corps de la même version |
| `header_order` | list | ordre exact |
| `session_headers` | list | quartet/trio selon version |
| `status` | `candidate/verified/stale/rejected` | |
| `evidence` | list[ProfileEvidence] | ≥2 blocs pour `verified` |
| `verified_at` / `expires_at` | timestamp | TTL |
| `shared` | bool | réutilisé par plusieurs stations |

### 3.6 `ProfileEvidence`

| Champ | Type |
|---|---|
| `block` | `/64` ou `/32` |
| `status` | HTTP |
| `content_ok` / `error_frame` | bool |
| `latency_ms` | int |
| `response_sha256` | string |
| `checked_at` | timestamp |
| `verdict` | `pass/quota/reject` |

### 3.7 `ProfileAssignment`

| Champ | Type |
|---|---|
| `station` | label |
| `profile_id` | string |
| `assigned_at` | timestamp |
| `reason` | `hash/distinct/reuse/fallback` |
| `shared` | bool |

## 4. Invariants

### Modèle

- **I-M1** Le target actif est dans `FREE_MODELS` et n'est pas `dead`.
- **I-M2** Un mapping explicite `FREE_MODEL_MAP` n'est jamais réécrit.
- **I-M3** Aucun fallback payant.
- **I-M4** Une écriture de config est atomique et sérialisée.

### Egress

- **I-E1** Chaque station active a un bloc distinct.
- **I-E2** Un apply idempotent ne recrée rien.
- **I-E3** Les anciennes stations restent actives jusqu'à vérification.
- **I-E4** Un échec rollback ne coupe pas l'ancien ensemble.
- **I-E5** Les proxies manuels ne sont pas écrasés.
- **I-E6** Un seul apply à la fois.

### Identité

- **I-I1** `project`, `ses_`, `msg_`, `trace` sont disjoints entre stations.
- **I-I2** Une conversation reste sur une station.
- **I-I3** Le prompt ne contient aucun marqueur de la machine réelle.
- **I-I4** Aucune session curl/cookie partagée.
- **I-I5** Un audit en échec exclut la station.

### Profils

- **I-P1** Seul un profil `verified` et non `stale` est émis.
- **I-P2** Un profil `verified` a ≥2 preuves `pass` sur des blocs distincts.
- **I-P3** Un `429` est `quota`, jamais `reject`.
- **I-P4** UA, Bun, JA3, ordre, corps et session headers sont cohérents.
- **I-P5** Une station garde son profil pendant une conversation.
- **I-P6** Un profil partagé est marqué `shared_profile`.
- **I-P7** Le fallback est le profil canonical vérifié, jamais payant.

## 5. États

### 5.1 Modèle

```
config → validé(alive) → conservé
config → absent/dead → candidat(alive) → persisté → propagé
config → aucun alive → no_free_model (aucune écriture)
```

### 5.2 Egress

```
idle → planning → provisioning → verifying → active
                                  └→ failed → rollback → active(ancien)
```

### 5.3 Identité

```
station créée → identity dérivée → audit → ok | exclue
conversation → affinity hit → même station
             └ miss → nouvelle station → affinity set
```

### 5.4 Profils

```
candidat → probing (bloc frais) → pass ×2 → verified
                              ├→ quota → candidat (autre bloc)
                              └→ reject ×3 → rejected
verified → TTL expiré → stale → re-vérification
verified → assigné à une station → distinct | shared_profile
```
