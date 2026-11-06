# Data model — Identités, quotas et cooldowns de la jambe free

**Feature**: `004-repo-parity-audit`  
**Sources**: `opencode.py`, `free/identities.py`, `free/quota_ledger.py`,
`free/ip_quota.py`, `free/pool.py`, `config.yaml`.

## 1. Entités

### 1.1 `FreeRequest`

Corps reçu du client, jamais muté.

| Champ | Type | Rôle |
|---|---|---|
| `model` | string | modèle demandé (payant ou free) |
| `messages` | list | historique, y compris system/tool |
| `tools` | list | outils client |
| `tool_choice` | string/object | `auto`, `none`, forcé |
| `stream` | bool | demande client |
| `max_tokens` / `max_completion_tokens` | int | budget de sortie |
| `conversation` | string (optionnel) | clé de conversation pour le `ses_` |

### 1.2 `FreeWire`

Copie d'envoi construite par `_free_wire_body` + `_official_free_headers`.

| Champ | Type | Invariant |
|---|---|---|
| `model` | string | modèle free effectif |
| `messages` | list | contient au moins un `<env>` valide |
| `tools` | list | 13 officiels + extras client (max 9) |
| `stream` | bool | toujours `true` vers l'amont |
| `stream_options` | object | `include_usage: true` |
| `max_completion_tokens` | int | ≤ 32 000 |
| `tool_choice` | absent | `none` retiré ; `auto` seulement si injecté |
| `prompt_cache_key` | string (responses) | `ses_` de conversation |

### 1.3 `EnvBlock`

| Champ | Type | Contrainte |
|---|---|---|
| tag | `<env>` | exact, pas `<env x="1">` |
| `Working directory:` | ligne | exacte, casse comprise |
| valeur | string | libre (`/`, `/tmp`, métadonnée) |
| position | premier `<env` d'un message système | un couple invalide avant un valide empoisonne |
| `</env>` | fermeture | doit suivre le premier `<env>` |

### 1.4 `GateTool`

| Champ | Type | Contrainte |
|---|---|---|
| `function.name` | string | l'un des 13 noms officiels |
| `function.description` | string | jamais `null` |
| `function.parameters` | object | `properties` non vide |
| `strict` | bool | `false` (2.2.5) |

### 1.5 `Identity`

Identité par egress IP (`free/identities.py`).

| Champ | Type | Source |
|---|---|---|
| `version` | `"1.19.5"` | `ROTATION_VERSIONS` |
| `client` | `cli`/`desktop` | profil |
| `project` | 40 hex ou `global` | par IP |
| `ses_global` | `ses_` + 12 hex + 14 base62 | horloge inversée |
| `ses_ts` | timestamp | rotation 30 min |
| `conversations` | LRU 128 | `ses_` par conversation |
| `msg_` | `msg_` + 26 | frais par envoi |
| `ja3` | `1523504b…` | Bun 1.4.4 |

### 1.6 `QuotaBucket`

Clé `(ip_norm, jour UTC, bucket)` ; persisté dans `logs/registry.db` ou JSON.

| Champ | Type | Rôle |
|---|---|---|
| `requests` | int | tentatives |
| `successes` | int | 2xx |
| `failures` | int | non-2xx |
| `transient_429` | int | 429 non quota |
| `retry_after` | float | valeur brute du header |
| `reset_at` | float | `ts + min(ra, max_block_s)` ou minuit |
| `exhausted` | bool | bucket épuisé |
| `source` | string | `observed`, `observed-ok`, `expired`, `inferred` |

### 1.7 `StationCooldown`

Clé `(modèle, IP)` ; en mémoire (`_free_model_cooldowns`).

| Champ | Type | Rôle |
|---|---|---|
| `expiry` | monotonic | fin du cooldown |
| `scope` | `trial-exhausted`/`rate-limited`/`transient` | nature |
| `seconds` | float | `min(max(header, trial_dead), cap)` |

### 1.8 `EgressPath`

| Type | Identité | Quota |
|---|---|---|
| direct | IP publique de l'hôte | bucket de cette IP |
| VPN (gluetun) | IP de la station | bucket de cette IP |
| WARP IPv4 (SOCKS5) | `104.28.230.202` | bucket /32 partagé |
| WARP IPv6 (SOCKS5, `ipv6: true`) | `2a09:bac5:32d4:be::13:23e` | bucket `/64` |

### 1.8.1 `StaticEgress` (`Socks5Endpoint`)

| Champ | Type | Rôle |
|---|---|---|
| `label` | string | pid/state distinct (obligatoire si même `host:port`) |
| `ipv6` | bool | force `IPRESOLVE=6` au niveau session curl_cffi |
| `egress_ip` | string | IP réelle configurée (optionnelle) |
| `current_ip` | string | IP réelle si connue, sinon `socks5:<pid>` |
| `_egress_probe_attempted` | bool | sonde one-shot déjà tentée |

### 1.9 `RequestAttempt`

| Champ | Type | Rôle |
|---|---|---|
| `model` | string | modèle free |
| `station` | object | egress utilisé |
| `ip` | string | IP réelle de la tentative |
| `status` | int | 200/429/403/5xx |
| `retry_after` | string | header brut |
| `body` | string | corps d'erreur |
| `latency_ms` | int | mesure |

## 2. Invariants

- **I-01** Le corps client n'est jamais muté ; toute correction est faite sur
  une copie.
- **I-02** Un `FreeWire` chat contient au moins un `<env>` valide.
- **I-03** Le premier `<env>` d'un message système est valide ; un couple
  invalide en tête n'est jamais laissé tel quel.
- **I-04** `tool_choice: "none"` n'est jamais envoyé à l'amont free.
- **I-05** `stream` est toujours `true` vers l'amont ; un client non-stream est
  replié côté proxy.
- **I-06** Chaque outil de la grille a `properties` non vide et une
  `description` string.
- **I-07** Deux IPs ne partagent jamais un `ses_`, un `msg_`, un `project` ou
  un trace ID.
- **I-08** Un 429 quota ne bloque que l'IP qui l'a reçu, jamais le modèle
  global.
- **I-09** Un cooldown station est borné par `free.cooldown_cap_s` (défaut
  1 200 s) ; 0 désactive le plafond.
- **I-10** Un bucket ledger est clé par `(IP, jour UTC, bucket)` ; le plafond
  `max_block_s` ne change pas la clé.
- **I-11** Un 200 observé lève l'exhaustion du bucket (`exhausted=False`).
- **I-12** Le mode direct ne dépend d'aucune station VPN.
- **I-13** Deux entrées statiques partageant `host:port` DOIVENT avoir des
  `label` distincts ; sinon le state et le ledger collisionnent.
- **I-14** Une entrée `ipv6: true` force `IPRESOLVE=6` sur une session curl
  dédiée (variant `-ipv6`) ; l'IPv4 ne partage jamais cette session.
- **I-15** Le ledger keye l'IPv6 réel au `/64` ; deux adresses du même `/64`
  sont le même bucket.

## 3. Transitions

### 3.1 429 quota

```
200 ──► note_result(200) ──► exhausted=False, reset_at=0
429 FreeUsageLimitError
  ├─ header RA > 0 ──► reset_at = ts + min(RA, max_block_s)
  └─ pas de header ──► reset_at = prochain minuit UTC
station : cd = min(max(RA ou 120, trial_dead si pas de header), cap)
```

### 3.2 Session

```
création ──► ses_ (horloge inversée)
  ├─ conversation connue ──► même ses_
  ├─ nouvelle conversation ──► nouveau ses_ (LRU 128)
  └─ 30 min ──► rotation du ses_ global
```

### 3.3 Garde `<env>`

```
messages
  ├─ premier <env> valide ──► inchangé
  ├─ premier <env> invalide ──► insertion d'un bloc valide en tête
  └─ aucun <env> ──► insertion d'un bloc valide en tête
```

### 3.4 Outils

```
client tools
  ├─ nom officiel exact ──► conservé à sa place
  ├─ nom officiel absent ──► placeholder (nom + path)
  └─ extra ──► conservé après la grille (max 9)
```

## 4. Sources de vérité

| Donnée | Source | Persistance |
|---|---|---|
| Identité par IP | `free/identities.py` | `logs/identities/<ip>.json` |
| Bucket quota | `free/quota_ledger.py` | `logs/registry.db` |
| Cooldown station | `opencode.py` | mémoire (monotonic) |
| Config | `config.yaml` | fichier |
| Capture wire | `free/captures/2.2.5.json` | fichier |
| Corps officiel | `free/official_payload.json` | fichier |
