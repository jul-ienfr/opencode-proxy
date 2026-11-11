# Implementation Plan: Catalogue d'egress et rotation disponible

**Branch**: `013-egress-catalog` | **Date**: 2026-11-09 | **Spec**: [spec.md](./spec.md)

**Input**: Spec 013 — « plus de rotation possible disponible pour chacun »,
sans forcément plus de stations actives.

## Summary

Construire un **catalogue d'egress provider-agnostique** dans `registry.db`,
alimenté par des sources déclaratives et un scheduler, qui distingue :

- `reachable` : le transport répond (liveness) ;
- `usable` : le gate free a rendu un vrai `200` récemment ;
- `quota` : `429` avec `reset_at` ;
- `auth` : session/credential invalide ;
- `dead` : timeout/refus/TLS.

Le pool actif reste borné (six stations, quelques endpoints publics) ; le
catalogue, lui, conserve **toutes les options de rotation** connues par
provider, avec preuve et fraîcheur. La rotation pioche dans ces options au lieu
de dépendre de la station courante.

## Technical Context

**Language/Version**: Python 3.12+, PowerShell 5.1/7 pour les scripts Windows.

**Primary Dependencies**: FastAPI/uvicorn, SQLite (`registry.db`), `curl_cffi`,
Docker CLI/Compose, `caomingjun/warp`, Gluetun, pytest.

**Storage**: `logs/registry.db` = source de vérité (constitution III) :
sources, options, observations, slots de rotation, runs, quotas. Exports JSON
pour le dashboard uniquement.

**Testing**: pytest unitaire/contrat sans Docker ; adapters factices ;
runner Docker mocké ; smoke live opt-in.

**Target Platform**: Windows 10/11, Linux et macOS ; Docker Desktop/Engine pour
les conteneurs Linux. Le cœur Python est identique, les différences passent par
`core.process` et un adapter de plateforme.

**Project Type**: service web + CLI/scripts + GUI Svelte.

**Performance Goals**: un cycle public de 1 000 candidats < 5 min en mode
échantillon ; un cycle complet 20 000 candidats en tâche de fond ; API
catalogue p95 < 500 ms pour 1 000 options, UI p95 < 2 s ; 0 sélection d'option
`dead`/`auth`.

**Constraints**: free-only, aucun second compte, aucun payant, pas d'attente
comme substitut ; isolation worker pour le public ; `reset_at` fournisseur
autoritaire ; pas de nouvelle dépendance externe ; aucun processus visible,
`shell=True` ou chemin Windows en dur dans le service.

**Scale/Scope**: 3 providers, 1 catalogue, 64 options publiques actives max,
12 blocs WARP connus, jusqu'à 10 IP NordVPN connues mais 6 stations actives
max par défaut (capacité de session mesurée).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principe | Gate | Statut |
|---|---|---|
| I — disponibilité mesurée | Chaque état vient d'une observation horodatée, jamais d'un `connected` | **PASS** — `egress_observations` append-only |
| II — trois cycles de vie | Station, session, IP séparées | **PASS** — `EgressOption.ip` distinct de `RotationSlot.station` |
| III — une source de vérité | `registry.db` autorité | **PASS** — catalogue dans `registry.db`, JSON = export |
| IV — reset fournisseur | aucune IP avant `reset_at` | **PASS** — `quota.reset_at` porte le `Retry-After` |
| V — unicité IP | une IP = un claim | **PASS** — index unique `(provider, egress_ip)` |
| VI — indépendance providers | pas de quota pays | **PASS** — le pays n'est pas une porte |
| VII — aucune preuve, aucun succès | `200` avec contenu obligatoire | **PASS** — promotion seulement après gate free |
| VIII — autonomie / un réparateur | scheduler découvre, ne répare pas | **PASS** — le superviseur reste seul réparateur |
| IX — score/gates | le score ordonne, les gates décident | **PASS** — `usable` + `reset_at` + unicité avant score |
| X — dégradation free | état dégradé explicite | **PASS** — `ProviderState` expose quota/auth/dead |
| XI — fallbacks | source → cache → registre | **PASS** — une source absente ne vide pas le catalogue |
| XII — option GUI effective | option → config → consommateur | **PASS** — API/UI branchées sur le catalogue |
| XIII — schéma/migrations | clés, index, rétention, backup | **PASS** — migration `013` versionnée |
| XIV — modulaire | < 1 500 lignes/module, ports/adapters | **PASS** — domaine pur + adapters |
| XV — multiplateforme | Windows/Linux/macOS, pas d'API OS dans le domaine | **PASS** — `core.process` + `pathlib`, aucun chemin en dur |
| XVI — exécution silencieuse | aucun processus visible, pas de TTY | **PASS** — `run_hidden`/`popen_hidden`, `platform_audit` |
| XVII — gates qualité | fiabilité, perf, qualité, modularité, maintenabilité | **PASS** — budgets testés, option-map, fail-soft, rétention |

## Project Structure

### Documentation (this feature)

```text
specs/013-egress-catalog/
├── plan.md
├── spec.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── egress-catalog.md
└── tasks.md
```

### Source Code (repository root)

```text
free/
├── egress_catalog.py        # domaine pur : options, états, transitions, scoring
├── egress_store.py          # SQLite : migrations, CRUD, rétention, exports
├── egress_sources.py        # registre de sources + parsers (public/WARP/NordVPN)
├── egress_validation.py     # liveness, gate free, probes, classification
├── egress_probes.py         # gate free des tunnels WARP/NordVPN managés
├── egress_runtime.py        # composition root : config → store/adapters/scheduler/pool
├── threat_intel.py          # flux gratuits IP/CIDR, cache fail-soft
├── egress_adapters/
│   ├── public.py            # sources publiques + worker isolé
│   ├── warp.py              # conteneurs, /64 observés, rotation
│   └── nordvpn.py           # stations, IP/serveurs, AUTH/quota
├── egress_scheduler.py      # boucle asyncio, cadence, jitter, fail-soft
└── egress_rotation.py       # sélection d'options pour le pool

dashboard/api.py             # endpoints catalogue + refresh
dashboard-ui/src/pages/vpn/  # onglet catalogue/rotation
scripts/egress_catalog.py    # CLI one-shot (refresh, export, verify)
scripts/platform_audit.py    # gate XV/XVI (fenêtres, shell, chemins en dur)
scripts/ip_reputation.py     # corroboration d'une IP sans la contacter
tests/
├── test_egress_catalog.py
├── test_egress_store.py
├── test_egress_sources.py
├── test_egress_public_adapter.py
├── test_egress_warp_adapter.py
├── test_egress_nordvpn_adapter.py
├── test_egress_scheduler.py
├── test_egress_runtime.py
├── test_egress_api.py
├── test_egress_probes.py
├── test_threat_intel.py
├── test_ip_reputation.py
├── test_option_map.py
├── test_platform_audit.py
└── test_constitution_rules.py
```

**Structure Decision**: conserver le monolithe `opencode.py`/`FreeIPPool` comme
consommateur, ajouter le catalogue à côté (strangler), puis brancher le pool
sur `egress_rotation` quand le catalogue est stable. Aucun nouveau module ne
dépasse 1 500 lignes.

## Design

### 1. Modèle

Voir [data-model.md](./data-model.md). En bref :

- `SourceDefinition` : source déclarative, parser, intervalle, actif.
- `EgressOption` : clé canonique `provider|kind|host:port|egress_ip` ; état,
  TTL, `reset_at`, preuves, sources.
- `Observation` : append-only, phase, statut, latence, message borné.
- `RotationSlot` : option actuellement utilisée par un provider.
- `ProviderPolicy` : cadence, concurrence, caps, TTL, budget.

### 2. Pipeline public

1. `SourceDefinition` → fetch (HTTP/JSON/texte) → parser.
2. Normalisation `scheme://host:port`, rejet privé/loopback.
3. Déduplication par `egress_ip` (pas par host:port).
4. Liveness via `https://api64.ipify.org` à travers le worker isolé.
5. Gate free via le wire 2.2.5 réel (`opencode._revalidate_public_proxies`
   ou un adapter partagé).
6. Promotion `usable` seulement si `200` avec contenu ; `429` → `quota` avec
   `reset_at` ; sinon `dead` avec TTL court.
7. Le pool actif reste plafonné par `public.max_active`.

### 3. WARP

- Le catalogue ne crée pas de station par défaut.
- Il enregistre chaque conteneur existant, chaque `/64` observé et chaque
  rotation (`warp_rotation.py`).
- Une option WARP = `(container, /64)` avec TTL ; un `/64` recréé expire
  l'ancien.
- La rotation demande une option `usable` fraîche au catalogue ; si aucune,
  elle le signale au lieu de brûler une IP.

### 4. NordVPN

- Le catalogue lit les stations et les IP observées (`vpn/manager.py`,
  `fresh_chain`, `registry.db`).
- Une option = `(server_hostname, egress_ip)` ; états `usable`/`quota`/
  `auth`/`dead`.
- `AUTH_FAILED` exclut toutes les IP de la station jusqu'à réparation.
- La rotation choisit une IP fraîche dans le catalogue, pas seulement un
  serveur différent.

### 5. Scheduler

- Boucle asyncio lancée au boot si `egress_catalog.enabled`.
- Cadence par provider, jitter, jamais dans le chemin de requête.
- Un cycle = `CatalogRun` ; échec = conserve le catalogue précédent.
- `close()` annule la tâche et attend les workers bornés.
- Un seul cycle par provider à la fois (single-flight).

### 6. Consommation

- `FreeIPPool` reçoit les options promues via les adapters existants
  (`set_https_proxies`, `set_socks5_proxies`, `set_stations`).
- `egress_rotation.select()` applique les gates durs (provider, `usable`,
  `reset_at`, unicité IP, cap) puis le score de la constitution IX.
- Le pool ne connaît pas les détails du catalogue ; il reçoit des options
  prêtes à servir.

### 7. API / UI

- `GET /api/egress/catalog` : résumé par provider.
- `GET /api/egress/catalog/options?provider=&status=&limit=` : options.
- `GET /api/egress/catalog/runs?limit=` : cycles.
- `POST /api/egress/catalog/refresh` : 202 + job id, protégé token.
- `POST /api/egress/catalog/sources` : ajout/édition d'une source.
- UI : onglet « Catalogue egress » dans Modèles Gratuits, table par provider,
  filtres, dernier cycle, bouton refresh.

## Phases

### Phase 0 — Recherche (1 jour)

- Cartographier les mécaniques existantes et les preuves déjà disponibles.
- Vérifier les formats de sources et les limites Docker/NordVPN.
- Décider du schéma SQLite et de la migration.

### Phase 1 — Catalogue + public (MVP, 2–3 jours)

- Domaine, store, migration, tests.
- Source registry + parser public.
- Adapter public : fetch → liveness → gate (via worker).
- API GET résumé/options.
- Import des 20 045 candidats actuels comme première source.
- **Critère** : 100 % des options actives publiques ont un 200 récent.

### Phase 2 — WARP (2 jours)

- Adapter WARP : conteneurs, `/64`, observations, rotation.
- Ingestion des 12 blocs actuels (6 conteneurs × IPv4/IPv6) ; IPv6 seuls
  promus.
- `egress_rotation` pour WARP.
- **Critère** : 12 options `/64` connues, 0 sélection d'un bloc `quota`.

### Phase 3 — NordVPN (2–3 jours)

- Adapter NordVPN : stations, IP, AUTH, quota.
- Ingestion de l'historique `registry.db` et des IP courantes.
- Rotation sur IP fraîche.
- **Critère** : 6 IP `usable` connues, AUTH_FAILED exclu, 0 sélection
  d'une IP `quota`.

### Phase 4 — Scheduler + UI (1–2 jours)

- Scheduler par provider, single-flight, jitter.
- API refresh/sources/runs.
- UI catalogue.
- Métriques et alertes.

### Phase 5 — Convergence (1 jour)

- Brancher `FreeIPPool` sur `egress_rotation` (shadow puis canary).
- Retirer/archiver `scripts/autoscale_max_ips.py` (one-shot transitoire,
  jamais planifié) au profit du scheduler.
- Mettre à jour `PLAN_PROXIES_PUBLICS_2026-11-09.md`, le CHANGELOG et le
  dashboard.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Nouveau module `egress_catalog` + store + 3 adapters | Trois providers ont des cycles de vie différents mais un même besoin de rotation | Un dictionnaire JSON unique ne porte pas les observations, les TTL, les migrations ni les claims d'IP |
| Scheduler asyncio dans le proxy | La découverte doit tourner sans cron externe et sans bloquer les requêtes | Un cron Windows est invisible, non testable et ne partage pas l'état du pool |
| Ingestion de 20k candidats publics | Le rendement public est faible ; il faut un grand catalogue pour quelques options valides | Un import manuel de 29 lignes est périmé en quelques heures |

## Risks

- **Volume public** : 20k candidats, beaucoup de doublons ; borner la mémoire,
  dédupliquer par egress, batchs.
- **Sources malveillantes** : worker isolé, TLS destination vérifié, aucun
  secret ; déjà en place.
- **NordVPN session limit** : ne pas dépasser la capacité saine ; le
  catalogue connaît plus d'IP que de stations, mais n'ouvre pas plus de
  sessions que mesuré.
- **WARP /64 churn** : TTL court et expiration des anciens blocs.
- **Scheduler bloquant** : tout cycle long en `asyncio.to_thread` ou
  subprocess, jamais dans la boucle de requête.
- **Double source de vérité** : JSON = export, `registry.db` = autorité.
