# Data Model — 023-client-usable-egress

Persistance : `registry.db` (décisions et états) et `requests.db` (télémétrie).
Aucune donnée de secret dans ces tables.

## 1. IPState

Une ligne par `(provider, ip)`. Clé primaire `(provider, ip)` ; index sur
`state`, `bucket_key`, `reset_at`, `wire_fp`.

| Champ | Type | Contrainte |
|---|---|---|
| `provider` | texte | `nordvpn|warp|proxies|direct` |
| `ip` | texte | adresse globale validée ; sentinels refusés |
| `bucket_key` | texte | `/32`, `/64`, `host:port`, IP hôte |
| `state` | texte | `unproven|proven|serving|spent|burned|mismatch` |
| `wire_fp` | texte | sha256 hex ; requis pour `proven`/`serving` |
| `proven_at` | horodatage | requis pour `proven`/`serving` |
| `serving_at` | horodatage | nullable |
| `reset_at` | horodatage | fournisseur uniquement ; requis pour `spent` |
| `burned_at` | horodatage | requis pour `burned` |
| `reason` | texte | cause de la dernière transition |
| `evidence_ref` | texte | clé d'une ligne `Evidence` |
| `probe_count` | entier | ≥ 0 ; plafonné par `probe_budget_per_ip` |
| `updated_at` | horodatage | mise à jour à chaque transition |

### Transitions autorisées

| Depuis | Vers | Condition |
|---|---|---|
| `unproven` | `proven` | 200 avec contenu, même `wire_fp`, preuve conservée |
| `proven` | `serving` | sélection client |
| `proven`/`serving` | `spent` | 429 classe `quota` + `reset_at` fournisseur |
| `unproven`/`proven` | `burned` | 429 classe `format` ; `burned_at` posé |
| `proven`/`serving` | `mismatch` | deux lecteurs concordants sur une autre IP |
| `spent` | `unproven` | `now >= reset_at`, puis re-preuver |
| `burned` | `unproven` | `now >= max(burned_at + 24 h, reset_at)` |
| `mismatch` | `unproven` | claim libéré, preuve nouvelle requise |

Invariants :

1. `burned` ⇒ aucune sélection, aucune sonde, aucun client.
2. `spent` ⇒ aucune sélection avant `reset_at`.
3. `proven`/`serving` ⇒ `wire_fp` non vide et `proven_at` non nul.
4. Un `mismatch` exige `evidence_ref` contenant deux lectures indépendantes.
5. Aucune transition vers `proven` sans 200 avec contenu.

## 2. Bucket

Unité de quota, dérivée de `IPState.bucket_key`.

| Champ | Type | Contrainte |
|---|---|---|
| `bucket_key` | texte | unique par provider |
| `provider` | texte | identique à `IPState` |
| `capacity` | entier | 1 pour `/32` et `/64` ; 0 pour WARP IPv4 |
| `reset_at` | horodatage | le plus proche des IP `spent` |
| `probe_count` | entier | compteur du ledger sondes |
| `client_count` | entier | compteur du ledger client |

Invariants : WARP IPv4 (`104.28.0.0/16` partagé) ⇒ `capacity = 0` ; deux
endpoints d'un même bucket ne doublent jamais la capacité.

## 3. WireProfile

| Champ | Type | Contrainte |
|---|---|---|
| `version` | texte | capture active (`2.3.0`) |
| `endpoint` | texte | `/v1/chat/completions` |
| `ua` | texte | `opencode/latest/<version>/cli` |
| `header_order` | liste | 16 noms, ordre exact |
| `body_keys` | liste | 6 clés officielles |
| `tools` | liste | 13 noms officiels |
| `tls_ja3_md5` | texte | empreinte mesurée |
| `body_sha256` | texte | capture de référence |
| `wire_fp` | texte | sha256(corps canonique + UA + ordre + JA3) |

Invariants : une seule capture active ; un `wire_fp` différent invalide les
preuves existantes pour un nouveau format (principe XVIII).

## 4. ClientProfile

| Champ | Type | Contrainte |
|---|---|---|
| `id` | texte | `claude-code`, `codex`, `kilo-code`, `hermes-agent`, `deepseek`, `aider`, `openai-sdk`, `custom` |
| `family` | texte | `anthropic_messages|openai_chat|openai_responses|passthrough` |
| `endpoints` | liste | chemins servis |
| `auth` | objet | `x-api-key`, `bearer`, `anthropic-version` |
| `capabilities` | objet | `stream`, `tools`, `vision`, `reasoning`, `cache_control`, `count_tokens`, `max_context` |
| `tool_policy` | texte | nom de politique (`anthropic`, `openai`, `custom`) |
| `enabled` | booléen | désactivation sans redéploiement |

Invariants : un client inconnu résout `custom` ; aucune capacité non déclarée
n'est utilisée silencieusement.

## 5. ToolPolicy

| Champ | Type | Contrainte |
|---|---|---|
| `official_grid` | liste | 13 noms officiels, ordre de la capture |
| `forbidden` | liste | `subagent_fork`, `subagent-fork` + configurables |
| `aliases` | objet | client → officiel |
| `shims` | objet | outil → stratégie (`text_plan`, `reject`) |
| `client_extras_max` | entier | défaut 8 |
| `unknown_tool` | texte | `text_heal|reject` |
| `tool_choice` | objet | `none → auto` |

Invariants : un outil interdit ne part jamais ; un appel revenu porte toujours
le nom client d'origine ; un appel non déclaré n'est jamais relayé tel quel.

## 6. ProbeBudget

| Champ | Type | Contrainte |
|---|---|---|
| `provider` | texte | clé |
| `bucket_key` | texte | clé |
| `probe_count` | entier | ≤ `probe_budget_per_ip` par fenêtre |
| `last_probe_at` | horodatage | respecte `probe_cooldown_s` |
| `client_count` | entier | ledger séparé |

Invariant : une IP `proven`/`serving` n'est jamais sondée par `fresh_chain`.

## 7. Evidence

| Champ | Type | Contrainte |
|---|---|---|
| `id` | texte | clé |
| `kind` | texte | `200_content|429_quota|429_format|mismatch|ab` |
| `provider` | texte | provider concerné |
| `ip` | texte | IP concernée |
| `wire_fp` | texte | empreinte du wire |
| `payload_ref` | texte | chemin ou ligne de télémétrie |
| `created_at` | horodatage | immuable |

## 8. Incident

| Champ | Type | Contrainte |
|---|---|---|
| `id` | texte | clé |
| `kind` | texte | `release|rotation|burn|mismatch|probe` |
| `provider`, `station`, `ip` | texte | contexte |
| `reason` | texte | cause |
| `evidence_ref` | texte | obligatoire pour `release`/`burn` |
| `created_at` | horodatage | immuable |

## 9. Relations

```
ProviderCapabilities 1 ── n Bucket 1 ── n IPState
ClientProfile 1 ── 1 ToolPolicy
IPState 1 ── 0..1 Evidence (evidence_ref)
Incident n ── 1 Evidence
WireProfile 1 ── n IPState (wire_fp)
```

## 10. Rétention et migration

- `IPState` et `Incident` : conservés 30 j minimum, purge après 90 j.
- `Evidence` : référencée par les incidents ; purge coordonnée.
- Migration additive uniquement ; sauvegarde `VACUUM INTO` + `integrity_check`
  avant application ; rollback par restauration du backup.
