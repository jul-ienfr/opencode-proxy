# Implementation Plan: Egress utilisable par les clients — NordVPN, WARP, proxys, direct

**Branch**: `023-client-usable-egress` | **Date**: 2026-11-17 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/023-client-usable-egress/spec.md`

## Summary

Rendre la jambe free réellement utilisable par les clients, sur les quatre
chemins (NordVPN, WARP, proxys, direct), sans brûler ni jeter les IP. Le plan
sépare l'émulation réseau (TLS, en-têtes, corps, identité) de la couche de
compatibilité client (protocole, outils, capacités), introduit une machine à
états d'IP unique, une classification des 429, un budget de sondes séparé, un
réparateur borné, et un registre de clients déclaratif.

L'approche est un strangler en trois phases : P0 arrête la destruction des IP,
P1 sert proprement les clients (conversion, admission, SSE, outils), P2 étend
et optimise les providers. Aucune phase ne touche la jambe payante.

## Technical Context

**Language/Version**: Python 3.12+

**Primary Dependencies**: FastAPI, curl_cffi (impersonation TLS), SQLite (WAL),
Docker Compose + Gluetun, pytest

**Storage**: `registry.db` (états d'IP, buckets, preuves, incidents),
`requests.db` (télémétrie client), `free/captures/*.json` (profils wire)

**Testing**: pytest (`-k "not docker"`), tests de contrat par famille, tests
d'intégration, A/B live à IP neuve par variante

**Target Platform**: Windows (poste opérateur), Linux (CI/production), macOS
(cœur supporté)

**Project Type**: service web unique (proxy) + scripts d'exploitation

**Performance Goals**: surcoût proxy p50 < 30 ms hors génération ; TTFB non
dégradé ; 100 flux concurrents ; sélection < 1 ms

**Constraints**: quota par IP ; brûlure ≥ 24 h ; aucune recréation de conteneur ;
free-only, aucun repli payant ; aucune fenêtre visible

**Scale/Scope**: 6 stations NordVPN, conteneurs WARP multiples, proxys publics,
1 direct, ≥ 8 clients, 42 exigences fonctionnelles

## Constitution Check

*GATE: doit passer avant la recherche et être revérifié après le design.*

| Principe | Gate | Statut |
|---|---|---|
| I — disponibilité mesurée | preuves horodatées, `client_usable` calculé | PASS |
| IV — reset barrière dure | `reset_at` fournisseur uniquement, aucun estimé | PASS |
| V — unicité d'IP | claim exclusif dans `ip_state` | PASS |
| VII — aucune preuve, aucun succès | 200 avec contenu du même `wire_fp` | PASS |
| VIII — un seul réparateur | superviseur propriétaire, token bucket | PASS |
| X — dégradation explicite | 503/429 honnêtes, jamais de paiement | PASS |
| XIV — modularité | nouveaux modules < 1500 lignes, adapters | PASS |
| XV — multiplateforme | cœur Python, adapters de plateforme | PASS |
| XVI — silencieux | `run_hidden` pour tout processus | PASS |
| XVII — gates qualité | gate, audits, option-map, CHANGELOG | PASS |
| XVIII — brûlure de format | IP = un format, A/B à IP neuve | PASS |

Aucune violation : pas de dérogation demandée.

## Project Structure

### Documentation (this feature)

```text
specs/023-client-usable-egress/
├── spec.md              # spécification
├── plan.md              # ce fichier
├── research.md          # analyse et preuves
├── data-model.md        # entités et invariants
├── quickstart.md        # validation locale et live
├── contracts/           # contrats HTTP et providers
├── tasks.md             # tâches d'implémentation
└── checklist.md         # revue qualité des exigences
```

### Source Code (repository root)

```text
free/
├── ip_state.py              # machine à états d'IP
├── classify_429.py          # classification des 429
├── wire_profile.py          # constructeur unique + wire_fp
├── wire_admission.py        # garde d'admission
├── wire_convert.py          # conversion par famille
├── probe_budget.py          # budget de sondes séparé
├── egress_ip.py             # lecteurs d'IP concordants
├── egress_provider.py       # capacités des providers
├── clients/
│   ├── registry.json        # clients et familles
│   ├── tool_policy.json     # politique d'outils
│   ├── capabilities.py
│   ├── family_anthropic.py
│   ├── family_openai_chat.py
│   ├── family_openai_responses.py
│   ├── family_passthrough.py
│   ├── tool_policy.py
│   ├── respond.py
│   └── fixtures/
└── ip_registry/             # registre existant (migration additive)

scripts/
├── fresh_chain_supervisor.py  # réparateur unique
├── fresh_chain.py             # qualification
├── wire_ab.py                 # A/B à IP neuve
├── capacity_probe.py          # capacité réelle par bucket
├── client_smoke.py            # smoke par client
├── egress_state.py            # état providers/buckets
├── egress_toggle.py           # activation/désactivation
└── soak_zero_429.py           # soak 24 h

tests/
├── contract/                  # par famille et par endpoint
├── integration/               # cycles IP, rotation, providers
└── unit/                      # états, conversion, outils, budgets
```

**Structure Decision**: strangler. Les nouveaux modules sont additifs ; le
monolithe `opencode.py` délègue `_free_wire_body` à `wire_profile` et les
branches client aux adapters de famille. Aucun nouveau module > 1500 lignes.

## Complexity Tracking

Aucune violation du Constitution Check. La complexité ajoutée (registre de
clients, machine à états, providers) remplace des branches existantes du
monolithe ; elle est isolée, testée et réversible par flag.

## 1. Stratégie

Strangler en trois phases, chacune utilisable seule et réversible. L'ordre est
imposé par les preuves : d'abord **arrêter de détruire les IP** (P0), ensuite
**servir proprement** (P1), enfin **optimiser et étendre** (P2). Aucune phase ne
touche la jambe payante.

Invariant de bout en bout : **une IP = un format + un bucket + un état**, et
seul un `wire_fp` conforme peut atteindre une IP `proven`/`serving`.

## 2. Architecture cible

```
client ──► conversion (convert|compat) ──► wire_profile ──► admission
                                                              │
                                          wire_fp conforme ?  │ non → 4xx local
                                                              │ oui
                                                              ▼
                                                     egress_router
                                     ┌──────────┬──────────┬──────────┬────────┐
                                   nordvpn     warp      proxies    direct
                                     │          │          │          │
                                  ip_state  ip_state   ip_state   bucket partagé
                                     └──────────┴──────────┴──────────┘
                                                │
                                         quota_ledger (probe ≠ client)
                                                │
                                          classify_429
                                    format | quota | session | upstream
```

### 2.1 `ip_state` — une seule machine à états

Nouveau module `free/ip_state.py` (domaine pur) + persistance dans
`registry.db` :

| État | Sens | Sortie |
|---|---|---|
| `unproven` | jamais 200 avec ce `wire_fp` | `proven` (200 contenu du même fp) |
| `proven` | 200 récent, bucket intact, non sondée depuis cooldown | `serving`, `spent`, `burned`, `mismatch` |
| `serving` | sert du trafic client | `spent` (quota), `burned` (format) |
| `spent` | 429 quota avec `reset_at` | `proven` après reset + une sonde officielle |
| `burned` | 429 format sur IP non prouvée | `unproven` après `burned_at + 24 h` **et** reset |
| `mismatch` | deux lecteurs concordent sur une autre IP | `unproven` (claim libéré) |

Colonnes : `provider`, `bucket_key`, `wire_fp`, `proven_at`, `serving_at`,
`reset_at`, `burned_at`, `reason`, `evidence_ref`, `probe_count`.

Règles dures :

- `burned` ⇒ aucune sonde, aucun client, avant `max(burned_at+24h, reset_at)`.
- `serving` ne repasse jamais `proven` par simple expiration : il faut une
  nouvelle preuve 200 du même `wire_fp`.
- un `mismatch` exige **deux** lectures indépendantes concordantes et une
  adresse globale (rejet de `1.2.3.4`, `0.0.0.0`, documentation, `unknown`).

### 2.2 `wire_profile` unique

Nouveau module `free/wire_profile.py` :

- charge la capture active (`free/captures/<version>.json`) une fois ;
- expose `build(client_body, *, mode) -> (wire_body, wire_fp, meta)` ;
- `wire_fp = sha256(canonical(body) + ua + header_order + tls_ja3_md5)` ;
- modes :
  - `convert` : le corps client est converti — clés officielles, 13 outils
    officiels, `system` replacé dans `messages`, `max_completion_tokens`
    clampé, `stream_options` ajouté, ordre des clés officiel ;
  - `compat` : corps conservé mais **marqué** ; interdit sur `proven`/`serving`
    (repli local 422 `wire_incompatible`), jamais envoyé à l'egress ;
  - `observe` : construit `convert`, journalise le diff, n'applique pas encore
    (phase de canary).

`opencode._free_wire_body` devient un délégué mince : plus aucune composition
parallèle. Les tests 003 verrouillent l'unicité du constructeur.

### 2.3 Admission et classification

`free/wire_admission.py` :

1. `wire_fp` calculé ;
2. sélection d'un endpoint dont `ip_state ∈ {proven, serving}` **et**
   `state.wire_fp == wire_fp` ;
3. sinon, aucun endpoint conforme → 422 local (pas d'egress) ou `compat`
   explicitement demandé par l'opérateur, dans ce cas routé uniquement vers un
   endpoint `unproven` dédié (jamais la réserve).

`free/classify_429.py` :

- conserve le corps amont brut (limite `database.max_body_chars`) ;
- `format` : `FreeUsageLimitError` sur un endpoint `unproven` avec ce
  `wire_fp` ;
- `quota` : `FreeUsageLimitError` avec `Retry-After` exploitable ;
- `session` : erreurs de session NordVPN ;
- `upstream` : 5xx/timeout ;
- écrit la classe, `reset_at`, `wire_fp`, `evidence_ref` dans `ip_state` et
  dans `requests.db` (nouvelles colonnes).

### 2.4 Budget de sondes séparé

`free/probe_budget.py` :

- ledger distinct par `(provider, bucket_key)` : `probe_count`, `client_count` ;
- `probe_budget_per_ip` (défaut 2), `probe_cooldown_s` (défaut 600) ;
- une IP `proven`/`serving` n'est **jamais** sondée par `fresh_chain` ;
- une IP `proven` n'est re-sondée qu'à expiration de `proof_max_age_s`, et
  toujours avec le même `wire_fp` (principe XVIII).

### 2.5 Réparateur unique et rotation bornée

`scripts/fresh_chain_supervisor.py` :

- supprime `_UNUSABLE_STRIKES_MAX = 1` au profit d'une règle de preuve :
  libération seulement sur `quota` (avec `reset_at`), `burned`, ou `mismatch`
  double-source ;
- token bucket par station : `rotation.min_interval_s` (défaut 600), sauf
  `burned`/`mismatch` prouvé ;
- coalescence `(provider, station)` : une seule chaîne active par station ;
  les demandes en attente sont fusionnées (compteur `coalesced`) ;
- plafond global `rotation.max_per_hour` (défaut 6) ;
- journalise pour chaque libération : `reason`, `wire_fp`, `evidence_ref`,
  lecteurs d'IP.

### 2.6 Quatre providers de première classe

`free/egress_runtime.py` : chaque provider expose

```python
class EgressProvider:
    name: str                  # nordvpn | warp | proxies | direct
    can_rotate: bool
    bucket_key: Callable[[Endpoint], str]   # /32, /64, proxy:host, "direct"
    wire_profile: str          # version de capture exigée
    quota_source: Literal["retry_after", "shared", "none"]
    health_source: str
```

- NordVPN : rotation par control API (`PUT /v1/vpn/settings`), jamais de
  recréation de conteneur.
- WARP : buckets `/64` IPv6, IPv4 = NAT partagée (capacité 0).
- Proxys : bucket par endpoint, `quota_source=shared`, preuve obligatoire.
- Direct : `can_rotate=false`, `quota_source=shared`, jamais compté en réserve.

### 2.7 Configuration unifiée

```yaml
egress:
  enabled: true
  strategy: balance            # balance | priority | failover
  client_usable_only: true
  providers:
    nordvpn: {enabled: true,  weight: 52}
    warp:    {enabled: true,  weight: 24}
    proxies: {enabled: true,  weight: 24}
    direct:  {enabled: false, weight: 0}
  wire:
    client_mode: convert       # convert | compat | observe
    burn_guard: true
  rotation:
    min_interval_s: 600
    max_per_hour: 6
    probe_budget_per_ip: 2
    probe_cooldown_s: 600
    release_on_single_429: false
    require_ip_agreement: true

clients:
  enabled: true
  default_family: openai_chat     # famille de repli pour un client inconnu
  registry: free/clients/registry.json
  tool_policy: free/clients/tool_policy.json
  unknown_client: custom
  strict_capabilities: true       # une capacité non déclarée est refusée en local
  families:
    anthropic_messages: {enabled: true}
    openai_chat:        {enabled: true}
    openai_responses:   {enabled: true}
    passthrough:        {enabled: false}
```


Migration au chargement : `ip_rotation.proxy_mode` et `egress_providers`
alimentent `egress.providers.*` si la clé n'existe pas ; une seule vérité
runtime ensuite, exposée par `/api/egress/config`.

## 3. Phases et critères de sortie

Les identifiants de tâches et leur découpage par user story sont dans
`tasks.md`. Vue d'ensemble :

| Phase | User stories | Objectif | Critère de sortie |
|---|---|---|---|
| Setup | — | structure, config, migration | gate verte |
| Foundational | — | `ip_state`, `classify_429`, `wire_profile`, admission | socle bloquant levé |
| MVP | US1 | client servi sans 429 | 24 h à 0 429 si réserve ≥ 1 |
| P1 | US3, US4, US7, US8, US9 | ne brûler aucune IP, servir tous les clients | SC-008 à SC-012 |
| P1 | US2 | quatre providers activables | SC-006 |
| P2 | US5, US6, US10 | rotation bornée, observabilité, wire identique | SC-002, SC-003, SC-013 |
| Polish | — | capacité, performance, chaos, soak | SC-001 à SC-014 |

**MVP** = Setup + Foundational + US1. Chaque story est validée
indépendamment avant la suivante.

## 4. Tests et gates

- **Unitaires** : machine à états (`ip_state`), classification, `wire_fp`,
  conversion, budgets, token bucket, rejet sentinel.
- **Propriétés** : jamais deux états simultanés ; `burned` ⇒ zéro sélection ;
  `compat` ⇒ zéro egress prouvé ; monotonie des compteurs.
- **Intégration** : 200 → serving → 429 quota → spent → reset → proven ;
  rotation API sans recréation (`StartedAt` inchangé).
- **Chaos** : lecteur d'IP renvoyant `1.2.3.4`, timeout, tunnel coupé,
  `429` simultané sur 6 stations.
- **Live (obligatoire, principe XVIII)** : `scripts/wire_ab.py` — une IP neuve
  par variante, ≥ 20 échantillons, même fenêtre, rapport horodaté.
- **Gates** : `pytest -k "not docker"`, `config_audit --strict`,
  `platform_audit --strict`, `test_option_map.py`, ruff, `py_compile`, matrice
  Windows/Linux.

## 5. Observabilité et SLO

- `/api/egress/state` : par provider et par bucket — `proven`, `serving`,
  `spent`, `burned`, `unproven`, `probe_count`, `reset_at`.
- `/api/egress/incidents` : chaque libération avec `reason`, `wire_fp`,
  `evidence_ref`, lecteurs.
- `/api/egress/429` : classe, corps amont (tronqué), IP, provider, `Retry-After`.
- SLO : `client_success_rate` (200 avec contenu / requêtes client) ≥ 99 % sur
  1 h quand `reserve ≥ 1` ; `rotation_rate` ≤ 6/h ; `burn_rate` = 0.

## 6. Déploiement et rollback

- Déploiement par phase, chaque phase derrière un flag
  (`egress.wire.burn_guard`, `egress.rotation.proof_release`).
- Rollback : flag OFF → comportement précédent ; aucun schéma destructif
  (`ip_state` est additive).
- Sauvegarde `registry.db` (`VACUUM INTO`) avant migration, vérifiée
  (`integrity_check`).
- Aucune recréation de conteneur pour une rotation (API gluetun uniquement).

## 7. Risques et parades

| Risque | Parade |
|---|---|
| Conversion client refusée à tort | mode `observe` d'abord, diff journalisé, canary 10 % |
| Latence de sélection | `ip_state` en mémoire, écriture asynchrone batchée |
| Migration config ambiguë | migration explicite + test de parité `proxy_mode` ↔ providers |
| Famine de buckets | réserve multi-provider, WARP `/64`, jamais de promesse sur direct |
| Régression du gate | A/B IP neuve, `wire_fp` versionné, rollback par flag |

## 8. Couche de compatibilité client (L0-L6)

### 8.1 Modules

```
free/clients/
  registry.json              # clients, familles, endpoints, auth, capacités
  tool_policy.json           # autorisés, interdits, alias, shims
  capabilities.py            # négociation, défauts sûrs, validation
  family_anthropic.py        # requête, réponse, SSE Anthropic
  family_openai_chat.py      # requête, réponse, SSE chat + [DONE]
  family_openai_responses.py # requête, réponse, SSE response.*
  family_passthrough.py      # corps déjà conforme, aucune transformation
  tool_policy.py             # mapping aller/retour, healing, shims
  respond.py                 # adapters JSON/SSE, erreurs, usage, stop_reason
  fixtures/<client>/*.json   # golden request/response/SSE
```

Un adapter par **famille**. Ajouter Kilo Code, Hermes Agent, Aider ou un client
inconnu = une entrée `registry.json`, pas un nouveau chemin de code.

### 8.2 Registre déclaratif (extrait)

```json
{
  "claude-code": {
    "family": "anthropic_messages",
    "endpoints": ["/v1/messages", "/v1/messages/count_tokens"],
    "auth": {"x-api-key": true, "anthropic-version": true},
    "capabilities": {
      "stream": true, "tools": true, "tool_choice": true,
      "reasoning": true, "vision": true, "cache_control": true,
      "count_tokens": true, "max_context": 200000
    },
    "system_role": "system",
    "tool_policy": "anthropic"
  },
  "codex": {
    "family": "openai_responses",
    "endpoints": ["/v1/responses"],
    "auth": {"bearer": true},
    "capabilities": {"stream": true, "tools": true, "reasoning": true, "vision": true},
    "tool_policy": "openai"
  },
  "hermes-agent": {"family": "openai_chat", "tool_policy": "openai"},
  "kilo-code":    {"family": "openai_chat", "tool_policy": "openai"},
  "deepseek":     {"family": "openai_chat", "reasoning": "reasoning_content"},
  "aider":        {"family": "openai_chat", "stream": false, "tools": false},
  "custom":       {"family": "openai_chat", "strict": true}
}
```

### 8.3 Pipeline de conversion (une passe, sans I/O)

1. **Détection** : endpoint + en-têtes + forme du corps → famille ; sinon
   `custom`. Jamais d'exception non gérée.
2. **Validation des capacités** : `stream`, `tools`, `vision`, `context`,
   `reasoning` ; une capacité absente ⇒ 422 local explicite.
3. **Normalisation des messages** : rôles, `system` (racine ↔ message),
   `tool_use`/`tool_result` ↔ `tool_calls`/`tool`, ids, images, `cache_control`.
4. **Politique d'outils** (voir 8.4) : mapping client → officiel, interdits,
   shims, extras bornés.
5. **Construction wire** : `wire_profile.build()` → corps officiel + `wire_fp`.
6. **Admission** : sélection d'un bucket dont `ip_state.wire_fp == wire_fp`.
7. **Envoi** : pool de connexions par `(proxy, impersonate)`, une tentative par
   bucket, aucun retry sur 429.
8. **Réponse** : adapter de famille — SSE/JSON, appels d'outils re-mappés,
   `stop_reason`, usage, erreurs.

Le pipeline est pur et testable sans réseau ; seul l'envoi est I/O.

### 8.4 Politique d'outils (données, pas de branches)

```json
{
  "official_grid": ["edit", "experimental_image_generate", "glob", "grep",
                    "question", "read", "shell", "skill", "subagent",
                    "webfetch", "websearch", "write", "execute"],
  "forbidden": ["subagent_fork", "subagent-fork"],
  "aliases": {
    "Read": "read", "Write": "write", "Edit": "edit", "MultiEdit": "edit",
    "Bash": "shell", "Glob": "glob", "Grep": "grep", "WebFetch": "webfetch",
    "WebSearch": "websearch", "Task": "subagent", "TodoWrite": "todowrite"
  },
  "shims": {"todowrite": {"strategy": "text_plan"}},
  "client_extras_max": 8,
  "unknown_tool": "text_heal",
  "tool_choice": {"none": "auto"}
}
```

Règles :

- un outil officiel du client garde **son** schéma exécutable, sinon shim ;
- un outil interdit ne part jamais, et un appel entrant à cet outil est rendu
  en texte ;
- un nom inconnu est passé en extra seulement s'il est déclaré par le client ;
- au retour, tout appel est re-mappé vers le nom client d'origine ; un appel
  non déclaré n'est jamais relayé tel quel.

### 8.5 Sémantique d'identité par famille

- **Anthropic** : quartet de session stable par conversation ;
  `traceparent` nouveau par requête, `b3` stable par session ;
  `metadata.user_id` jamais partagé entre IP.
- **OpenAI Chat** : `prompt_cache_key` = session de conversation ;
  `user` client conservé s'il est présent, sinon dérivé par IP.
- **Responses** : `prompt_cache_key` idem ; `previous_response_id` n'est
  jamais réutilisé à travers une IP.
- Règle commune : aucun chemin de workspace, aucun hash de dépôt, aucun id
  partagé entre deux IP ou deux clients.

### 8.6 Fidélité SSE et mapping d'erreurs

Contrats par famille (testés, 100 flux) :

| Famille | Séquence minimale | Usage final | Erreur mid-stream |
|---|---|---|---|
| Anthropic | `message_start` → `content_block_start/delta/stop` → `message_delta` → `message_stop` | `message_delta.usage` | `event: error` traduit |
| OpenAI Chat | chunks `choices[].delta` → `finish_reason` → `[DONE]` | `usage` si `include_usage` | objet `error` |
| Responses | `response.created` → `response.output_item.*` → `response.completed` | `response.completed.usage` | `response.failed` |

Mapping : quota amont → 429 client avec le même `Retry-After` ; aucune capacité
locale → 503 `capacity_exhausted` avec reset exact ; conversion impossible →
422 `wire_incompatible` avec le champ fautif ; corps trop gros → 413.

### 8.7 Multimodal et contexte

- images/fichiers : `image` Anthropic ↔ `image_url` OpenAI ; `b64` et URL ;
  capacité absente ⇒ 422.
- contexte : budget `keep_tokens` par famille, compaction avant envoi ; le
  prompt système officiel n'est jamais tronqué.
- `count_tokens` : endpoint amont si disponible, sinon estimation documentée
  avec marge, jamais un chiffre inventé.

### 8.8 Performance

- une seule sérialisation du corps (celle qui produit `wire_fp`) ;
- capture et tables d'outils chargées une fois, immuables ;
- SSE écrit par événement, sans buffering, avec backpressure et détection de
  déconnexion client ;
- pool de connexions par `(proxy, impersonate)` ; plafond de flux par bucket ;
- hedging : au plus un doublon, uniquement si aucun octet n'a été reçu, jamais
  sur un bucket `spent`.

## 9. Providers : contrats détaillés

| Contrat | NordVPN | WARP | Proxys | Direct |
|---|---|---|---|---|
| `can_rotate` | oui (control API) | oui (borné) | oui (source refresh) | non |
| `bucket_key` | IP `/32` | `/64` IPv6 ; IPv4 capacité 0 | `host:port` | IP hôte |
| `quota_source` | `retry_after` | `retry_after`/partagé | partagé | partagé |
| `health_source` | handshake + sonde | sonde egress | liveness | egress hôte |
| `wire_profile` | identique | identique | identique | identique |
| réserve client | oui | oui (`/64` seulement) | non | non |
| interdits | recréation de conteneur | recréation non bornée | proxy non qualifié | `enforce_vpn_only` |

Règles communes :

- le wire est identique sur les quatre chemins ; un test compare JA3/JA4,
  l'ordre des en-têtes et `wire_fp` pour une même requête ;
- un provider désactivé n'est plus sélectionné en < 60 s et n'est plus sondé
  après `drain_s` ;
- une rotation n'échoue jamais silencieusement : cause, preuve et état sont
  journalisés.
