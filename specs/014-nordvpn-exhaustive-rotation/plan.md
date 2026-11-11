# Implementation Plan: Control plane NordVPN — découverte, gestion, watchdog et rotation exhaustive

**Branch**: `014-nordvpn-exhaustive-rotation` | **Date**: 2026-11-09 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/014-nordvpn-exhaustive-rotation/spec.md`

## Summary

Le système possède 7 070 serveurs NordVPN en ligne mais n'en qualifie qu'une
fraction : le catalogue d'egress ne voit que six stations, la file de claims est
ordonnée par `fail_count`/`priority` statique, les pays sont mis en pause en
bloc, et le scanner s'arrête au premier 200. Le plan couvre **tout le control
plane NordVPN** : découverte schedulée, inventaire, gestion de station,
watchdog, réparation, file de qualification, score de potentiel, balayage par
époque, rotation sur 429, stratégie multi-transports cyclique
(WireGuard → OpenVPN UDP → OpenVPN TCP → WireGuard) et six stations
indépendantes et parallèles. Les composants existants (`ops/lifecycle.py`,
`ops/reconciler.py`, `ops/supervisor.py`, `free/station_health.py`,
`vpn/manager.py`) sont réutilisés par strangler ; `registry.db` reste
l'autorité. Le tout reste sous la limite de six sessions, sans quota pays et
sans jambe payante.

## Technical Context

**Language/Version**: Python 3.12 (dépôt existant), SQLite 3 (WAL), asyncio.

**Primary Dependencies**: `free.egress_*` (catalogue, store, rotation,
adapters), `free.ip_registry` (preflight, scoring, catalog), `free.ip_quota`,
`free.catalog_claims`, `free.station_health`, `free.repair_queue`,
`ops.lifecycle`, `ops.reconciler`, `ops.supervisor`, `ops.providers.nordvpn`,
`vpn/manager.py`, `scripts/fresh_chain.py`, `dashboard/api.py`, `config.yaml`.

**Storage**: `logs/registry.db` — autorité unique (constitution III). Nouvelles
tables `qualification_candidates`, `qualification_attempts`,
`qualification_epochs`, `station_runtime`, `station_cutovers`,
`transport_health`, `discovery_runs` ; réutilisation de `egress_options`,
`egress_ips`, `quota_buckets`, `sweep_cursor`, `server_transports`,
`station_state`, `incidents`, `repair_jobs`, `catalog_runs`, `catalog_sources`.

**Testing**: `pytest` ; tests unitaires du domaine sans I/O, tests du store sur
SQLite temporaire, tests du balayage avec probe/binder factices, tests
d'intégration sur un catalogue de 20 hostnames, tests de reprise après crash.

**Target Platform**: Windows, Linux, macOS (constitution XV) ; service
non interactif, aucun fenêtre (constitution XVI).

**Project Type**: application Python existante (proxy + free leg + dashboard) ;
ajout d'un module de qualification et branchement dans le runtime.

**Performance Goals**:
- Preflight de 500 hostnames < 5 min avec 6 workers.
- Balayage complet des 7 070 hostnames < 24 h avec six stations.
- API couverture p95 < 500 ms pour 1 000 lignes ; UI p95 < 2 s.
- Rotation sur 429 : candidat suivant claimé en p95 < 60 s.

**Constraints**:
- Six stations/sessions maximum ; la file peut contenir 7 070 hostnames.
- Le quota free est par IP, jamais par compte.
- Aucun pays en porte dure ; aucune quota par pays.
- Aucune jambe payante, aucun second compte, aucune attente comme solution.
- Aucun cycle long dans le chemin de requête.
- Modules < 1 500 lignes ; domaine pur sans I/O.
- Les six stations sont indépendantes et parallèles ; aucun pause globale.
- En mode `auto`, tous les transports supportés sont parcourus en cycle ; un
  échec de transport ne condamne pas le hostname.
- Disponibilité cible ≥ 99 % `client_usable` par station quand une IP 200
  existe ; cutover p95 < 2 s avec standby.
- Aucune station n'est sacrifiée pour le balayage.

**Scale/Scope**: 7 070 serveurs, 6 932 WireGuard, 150 pays, 6 stations,
~26 000 tentatives historiques, 4 871 buckets de quota.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principe | Vérification | Statut |
|---|---|---|
| I. Disponibilité mesurée | Chaque couverture vient de `qualification_attempts` + observations brutes ; pas de chiffre déclaré. | PASS |
| II. Trois ressources | La file est par hostname ; la session reste bornée à 6 ; l'IP observée est l'unité de quota. Un `no_egress` ne condamne pas l'IP. Chaque station a son worker et son état ; aucune rotation n'est sérialisée. | PASS |
| III. Source de vérité | Nouvelles tables dans `registry.db` ; `catalog` legacy n'est plus l'ordre de claim ; exports JSON en lecture seule. | PASS |
| IV. Reset fournisseur | `reset_at` de la 429 est autoritaire ; aucune IP n'est re-testée avant ; un 429 sans 200 antérieur est une mauvaise IP. | PASS |
| V. IP unique par station | Claim exclusif par `egress_ip`, index d'unicité, libération explicite. | PASS |
| VI. Couverture mondiale, pas de quota pays | `country_backoff` supprimé ; le pays est une métadonnée, jamais une porte. | PASS |
| VII. Aucune preuve, aucun succès | Seul un 200 avec contenu promeut `usable` ; un bind sans egress n'est pas un succès. | PASS |
| VIII. Autonomie, un seul réparateur | Le superviseur reste seul propriétaire de la réparation ; le balayage est un job de fond distinct ; une panne de station ne pause pas les autres. | PASS |
| IX. Score ordonne, portes dures décident | `potential_score` ordonne la file ; `measured_score` ordonne la rotation ; les portes (reset, unicité, preuve) passent avant. | PASS |
| X. Dégradation explicite, free | La pénurie est exposée avec la prochaine échéance ; aucun fallback payant ; la coupure est mesurée, jamais niée. | PASS |
| XI. Fallbacks multiples | Catalogue API → cache → embedded → registre ; preflight → bind → probe ; transport WG → OVPN UDP → OVPN TCP → WG (cycle borné). | PASS |
| XII. Option GUI = effet réel | `contracts/option-map.md` + gate `config_audit` ; chaque clé a un consommateur et un test. | PASS |
| XIII. Données traçables | Provenance (`source_id`), fraîcheur, clés, index, rétention, backup. | PASS |
| XIV. Modulaire, strangler | Nouveau package `free/qualification/` ; découpage par slices de `vpn/manager.py`, `free/pool.py`, `fresh_chain`, `dashboard/api.py`, `opencode.py` ; shadow, canary, rollback, puis décommissionnement. | PASS |
| XV. Multiplateforme | `pathlib`, aucun appel OS dans le domaine, adapters de plateforme. | PASS |
| XVI. Exécution silencieuse | `run_hidden`/`popen_hidden`, `start_new_session`, aucun shell visible. | PASS |

**Post-Phase 1 re-check** : la conception ajoute des tables, des scorers purs,
un service de balayage, un plan de transport et un runtime par station ; aucun
principe n'est affaibli. Les points sensibles sont la suppression de
`country_backoff` (requise par VI), la suppression des pauses globales
(requise par II/VIII) et le cycle multi-transports (requis par XI) ; chacun
aura un test de non-régression.

## Quality Architecture

Le système doit être le plus **fiable, performant, qualitatif, modulable et
maintenable** possible. L'architecture est organisée pour que ces attributs
soient structurels, pas ajoutés après coup.

### Couches et règle de dépendance

```text
domain/          pur : Candidate, Attempt, Score, TransportPlan,
                 StationState, Classification, CutoverPlan, invariants
    ▲
application/     sweep, station runtime, cutover, watchdog, discovery, repair
    ▲
adapters/        SQLite store, egress store, Gluetun/Compose binder, prober,
                 quota ledger, dashboard/API, clock
```

- Le domaine n'importe **ni Docker, ni réseau, ni provider, ni SQLite**.
- Les dépendances pointent vers le domaine, jamais l'inverse.
- Chaque frontière est un **port** avec un contract test ; les adapters sont
  remplaçables et testables avec des fakes.
- Aucun cycle de dépendance, aucun fork de `vpn/manager.py`, `ops/lifecycle.py`,
  `ops/reconciler.py` ou `ops/supervisor.py`.

### Ports et adapters

| Port | Adapter réel | Fake de test |
|---|---|---|
| `CatalogSource` | API NordVPN, cache, embedded, manifeste | catalogue JSON statique |
| `QualificationStore` | SQLite `registry.db` | store en mémoire |
| `StationBinder` | Gluetun/Compose/control API | binder factice |
| `Prober` | gate free officiel | probe scripté (200/429/timeout) |
| `QuotaLedger` | `egress_ips`/`quota_buckets` | ledger déterministe |
| `Watchdog`/`RepairQueue` | `ops/supervisor.py`, `repair_jobs` | horloge + fautes injectées |
| `Clock` | `time.time`/`monotonic` | horloge virtuelle |

### Fiabilité structurelle

- **Isolation** : un worker par station, un état par station, aucun lock
  global ; une panne locale ne peut pas contaminer les autres.
- **Idempotence** : clés `(hostname, transport, round)`, leases, curseurs
  transactionnels, reprise après crash.
- **Backpressure** : files bornées, budgets de temps, `max_attempts`,
  `max_repair_attempts`, pas de retry infini.
- **Fail-soft** : une source, un probe, un watchdog ou un dashboard en échec
  ne vide jamais la file ni ne coupe le service.
- **SLO/error budget** : heartbeat, âge de sonde, disponibilité par station ;
  alerte avant la panne.

### Performance structurelle

- Aucun I/O dans le chemin de requête ; sweep, discovery, repair et cutover
  sont des jobs de fond.
- Batchs SQL, index, pagination, agrégats précalculés ; pas de N+1.
- Budgets mesurés : API p95 < 500 ms, UI p95 < 2 s, preflight 500 < 5 min,
  sweep 7 070 < 24 h, cutover p95 < 2 s, rotation 429 < 60 s.
- Benchmarks et soak de 24 h dans la suite de qualité.

### Qualité structurelle

- Une seule source de vérité (`registry.db`), un seul scorer par phase, un
  seul classifieur, une seule machine à états de station.
- Classification déterministe et exhaustive : `200`, `429`, `infra`, `auth`,
  `dead`, `skipped`, `interrupted`.
- Preuve obligatoire pour chaque décision ; aucun chiffre déclaré.
- 0 `429` en `dead`, 0 faux `dead`, 0 doublon d'IP.

### Maintenabilité structurelle

- **Ratchet legacy** : aucun nouveau module > 1 500 lignes ; aucun module
  legacy touché ne grossit au-delà de sa baseline ; les monolithes
  `opencode.py`, `vpn/manager.py`, `free/pool.py`, `dashboard/api.py` sont
  découpés par slices (voir Monolith Rewrite Plan), pas réécrits en big-bang.
- Modules < 1 500 lignes ; complexité bornée ; 0 duplication de logique.
- Couverture ≥ 85 % **nouveau** domaine/application ; 100 % chemins critiques ;
  la baseline legacy (38 %) est suivie, pas maquillée.
- Type checking, lint, import-linter, `config_audit`, `platform_audit`,
  `option-map` et scan de secrets dans le gate.
- ADR, `debt.md`, runbook et documentation à jour ; 0 dette non listée.

### Stratégie de test

1. **Unitaire** : domaine pur, propriétés (score, classification, quotas).
2. **Contract** : chaque port, chaque adapter avec fake.
3. **Intégration** : sweep + store + binder + prober factices.
4. **Fautes** : tunnel down, DNS, `no_egress`, 429, AUTH, crash, disque plein,
   source indisponible.
5. **Performance** : benchmarks et soak 24 h.
6. **Migration/rollback** : copie de `registry.db`, `VACUUM INTO`, restore.
7. **Canary** : une station, un pays, puis six stations.

### Gates de qualité

- `ruff`/`mypy` 0 erreur sur les nouveaux modules.
- Couverture ≥ 85 % (domaine/application), 100 % critique.
- `import-linter` : 0 cycle, domaine pur.
- Taille des modules < 1 500 lignes ; complexité sous seuil.
- `config_audit` 0 dead / 0 missing ; `option-map` complet.
- `platform_audit --strict` 0 violation ; 0 fenêtre visible.
- Scan de secrets 0 ; 0 paid/second account.
- Benchmark/soak dans les budgets ; suite de fautes verte.

## Monolith Rewrite Plan

Les monolithes sont un risque structurel pour la fiabilité, la performance et
la maintenabilité. Le plan les **découpe progressivement**, sans big-bang.

### Principe

- **Strangler** : chaque seam est extrait derrière un contrat, avec tests de
  caractérisation, shadow, canary et rollback.
- **Ratchet** : le fichier legacy ne grossit jamais et décroît à chaque
  extraction ; aucun nouveau module > 1 500 lignes.
- **Parité** : mêmes entrées + même horloge → mêmes sorties, mêmes statuts,
  mêmes preuves. p95 ≤ +5 % vs baseline.
- **Un slice à la fois** : une extraction active par monolithe ; pas de
  big-bang, pas de fork parallèle.

### Cible

| Monolithe | Lignes | Découpage cible | Critère de sortie |
|---|---:|---|---|
| `opencode.py` | 31 347 | composition root ; pipeline requête, free leg, routage modèle | entrée fine < 1 500 lignes ; modules extraits testés |
| `vpn/manager.py` | 11 385 | cycle de vie station, AUTH/session, transport/bind, santé/probe, rotation | adapteur fin < 1 500 ; 0 logique métier dans le manager |
| `free/pool.py` | 8 190 | sélection, quota, rotation, pool public, accounting | < 1 500 ; modules testés |
| `dashboard/api.py` | 6 845 | routers, read models, projections | < 1 500 ou routeurs fins ; 0 SQL dans les routes |
| `scripts/fresh_chain.py` | 2 535 | sweep applicatif + CLI | CLI < 500 ; sweep dans `free/qualification/sweep.py` |
| `scripts/fresh_chain_supervisor.py` | 1 934 | superviseur fin, watchdog/repair | < 1 500 ; watchdog/repair dans `free/qualification/` |

### Séquence

1. **Inventaire** : graphe de dépendances, seams, contrats, ADR cible,
   tests de caractérisation.
2. **Manager** : cycle de vie → AUTH/session → transport/bind → santé/probe →
   rotation ; le manager devient un adapteur.
3. **Pool** : sélection → quota → rotation → pool public → accounting.
4. **Fresh chain** : `free/qualification/sweep.py` → CLI fine ; `--repair`
   reste un wrapper.
5. **Dashboard** : read models → routers ; aucune route SQL.
6. **Opencode** : pipeline requête → free leg → routage modèle → composition
   root.
7. **Décommissionnement** : suppression des flags/legacy, clôture D-02/D-20,
   audit final.

### Règles de sécurité

- Un flag de rollback par extraction ; shadow avant canary.
- Canary sur une station, puis un pays, puis six stations.
- Aucune extraction ne change le comportement fonctionnel ; les tests de
  caractérisation sont écrits **avant**.
- Un module legacy ne peut pas grossir ; la taille est vérifiée à chaque PR.
- Aucun cycle, domaine pur, contract tests pour chaque seam.
- Une extraction qui échoue est rollbackée, pas masquée par un flag permanent.

### Métriques de chantier

- Lignes par module et % de réduction du legacy.
- Nombre de seams extraits / restants.
- Parité fonctionnelle (tests de caractérisation) et delta p95.
- Couverture des modules extraits.
- Statut D-02/D-20, nombre de rollbacks, incidents liés à l'extraction.

### Risques

| Risque | Mitigation |
|---|---|
| Régression comportementale | Characterization tests + shadow + canary + rollback. |
| Couplage caché | Graphe de dépendances, import-linter, extraction par seam. |
| Performance | Benchmark avant/après, p95 ≤ +5 %. |
| Rollback complexe | Flag par extraction, ancien chemin conservé jusqu'au canary. |
| Chantier sans fin | Slices bornés, critère de sortie par module, D-02/D-20. |

## Project Structure

### Documentation (this feature)

```text
specs/014-nordvpn-exhaustive-rotation/
├── audit.md             # audit mesuré (racines, preuves)
├── spec.md              # cette spécification
├── plan.md              # ce plan
├── research.md          # décisions techniques (Phase 0)
├── data-model.md        # schéma et machines à états (Phase 1)
├── quickstart.md        # vérification opérateur (Phase 1)
├── runbook.md           # exploitation, incidents, rollback
├── rewrite-inventory.md # monolithes, seams, slices
├── release-checklist.md # checklist de sortie
├── contracts/
│   ├── qualification-api.md
│   ├── option-map.md
│   └── quality-gates.md
└── tasks.md             # découpage d'implémentation
```

### Source Code (repository root)

```text
free/
├── qualification/
│   ├── __init__.py
│   ├── domain.py          # Candidate, Attempt, PotentialScore, transitions (pur)
│   ├── scoring.py         # potential_score, measured_score
│   ├── store.py           # SQLite: candidates, attempts, epochs, cursor
│   ├── source.py          # API/cache/embedded → file, transports, stale
│   ├── preflight.py       # wrapper PreflightProber + RTT opérateur
│   ├── transport.py       # cycle WG/OVPN UDP/OVPN TCP, cursor, santé
│   ├── station.py         # worker par station, standby, état indépendant
│   ├── cutover.py         # make-before-break, commit/rollback, mesure
│   ├── watchdog.py        # heartbeat, sondes, incidents par station
│   ├── discovery.py       # scheduler API/cache/embedded, runs, stale
│   ├── repair.py          # file de réparation registry.db, backoff, escalade
│   ├── sweep.py           # époque, claim/lease, workers, classification
│   └── runtime.py         # composition, config, scheduler
├── egress_adapters/
│   └── nordvpn.py         # option observée ; plus de cap catalogue
├── egress_runtime.py      # branche le service de qualification
├── egress_store.py        # options + observations (existant)
├── catalog_claims.py      # claims → file ; suppression country_backoff
├── ip_quota.py            # headroom, reset_at (existant)
├── ip_registry/
│   ├── preflight.py       # mesure DNS/connect/handshake (existant)
│   └── scoring.py         # score mesuré (existant)
├── station/               # extrait de vpn/manager.py
│   ├── lifecycle.py
│   ├── auth_session.py
│   ├── transport_bind.py
│   ├── health_probe.py
│   └── rotation.py
├── pool/                  # extrait de free/pool.py
│   ├── selection.py
│   ├── quota.py
│   ├── rotation.py
│   └── public_pool.py
└── request_pipeline/      # extrait de opencode.py
    ├── request.py
    ├── free_leg.py
    └── model_routing.py

dashboard/
├── api.py                 # composition root des routes
└── routers/               # extraits de dashboard/api.py
    ├── station_routes.py
    ├── qualification_routes.py
    └── read_models.py

scripts/
├── qualification_sweep.py # CLI: --epoch, --max, --station, --dry-run
├── fresh_chain.py         # mode --sweep (continue après 200, cycle transports)
└── fresh_chain_supervisor.py

config.yaml                # egress_catalog.qualification.*

tests/
├── test_qualification_domain.py
├── test_qualification_store.py
├── test_qualification_source.py
├── test_qualification_sweep.py
├── test_qualification_rotation.py
├── test_qualification_resume.py
├── test_qualification_preflight.py
├── test_qualification_station.py
├── test_qualification_transport.py
├── test_qualification_cutover.py
├── test_qualification_watchdog.py
├── test_qualification_discovery.py
├── test_qualification_repair.py
└── test_qualification_api.py
```

**Structure Decision**: un package dédié `free/qualification/`, avec un domaine
pur et un store SQLite, branché dans `free/egress_runtime.py` et exposé par le
dashboard. `scripts/fresh_chain.py` devient un consommateur de la file (mode
`--sweep`) et conserve son mode `--repair` pour les urgences. Les monolithes
sont découpés progressivement vers `free/station/`, `free/pool/`,
`free/request_pipeline/` et `dashboard/routers/` ; aucun nouveau daemon, le
balayage est planifié par le runtime existant.

## Phase 0 — Research (résumé)

Les décisions détaillées sont dans `research.md`. Points clés :

1. **Unité de qualification = hostname + transport**, pas « IP » : l'IP n'est
   connue qu'après le bind. La file stocke le hostname ; l'IP observée est
   ensuite fusionnée dans `egress_options`.
2. **Score de potentiel** (avant mesure) : 30 % fiabilité (taux de 200
   Laplace), 30 % RTT opérateur, 20 % quota restant, 15 % charge API, 5 %
   fraîcheur. Le pays et la source sont exclus (constitution IX/VI).
3. **Score mesuré** (après 200) : 40 % succès, 25 % latence, 20 % débit, 10 %
   stabilité, 5 % fraîcheur (constitution IX).
4. **Ordre** : `potential_score DESC`, quota restant `DESC`, RTT `ASC`, charge
   `ASC`, hostname `ASC` ; portes dures avant.
5. **Classification** : `200` → `usable` ; `429` → `quota` + `reset_at` ;
   `no_egress`/`bind_error` → `infra` + retry borné ; `auth` → station ;
   `dead` → host terminal ; `skipped` → raison.
6. **Balayage** : époque persistante, curseur, reprise, `max_attempts` par
   host, aucun arrêt sur 200.
7. **Rotation** : sur 429, libération immédiate et claim du meilleur candidat ;
   le ledger IP est consulté avant tout bind.
8. **Suppression de `country_backoff`** : remplacé par un backoff par
   hostname/IP ; test de non-régression.
9. **Pas de nouvelle mécanique** : réutiliser `PreflightProber`,
   `egress_catalog`, `egress_store`, `ip_quota`, `scan_audit`.
10. **Stations parallèles** : un worker et un état par station, claims
    atomiques, aucun lock global, aucune pause globale ; priorité service >
    repair > sweep par station.
11. **Make-before-break** : standby validé, ancienne IP conservée, preuve 200,
    commit, rollback, cutover mesuré (0 si second tunnel, sinon p95 < 2 s).
12. **Transport cycle** : en `auto`, parcourir `wireguard` → `openvpn-udp` →
    `openvpn-tcp` → `wireguard` sur `transport_retry_rounds`, avec curseur,
    round, santé par transport et diagnostic OpenVPN si 0 succès.
13. **Control plane unifié** : watchdog par station, découverte schedulée,
    réparation dans `registry.db`, réutilisation de `ops/lifecycle.py`,
    `ops/reconciler.py`, `ops/supervisor.py`, `free/station_health.py` et
    `vpn/manager.py` ; aucune seconde mécanique parallèle.

## Phase 1 — Design (résumé)

Voir `data-model.md` et `contracts/`. Résumé :

- `qualification_candidates` : une ligne par hostname, clé primaire
  `hostname`, index `(status, next_eligible_at, potential_score DESC)`.
- `qualification_attempts` : append-only, une ligne par tentative, index
  `(hostname, started_at)`, `(epoch_id, status)`.
- `qualification_epochs` : une ligne par époque, compteurs matérialisés.
- `sweep_cursor` : curseur `(epoch_id, hostname)` réutilisé.
- `server_transports` : population depuis l'API (`technologies`), une ligne
  par `(hostname, transport)`.
- `qualification_candidates` : ajouter `transport_cursor`, `transport_round`,
  `attempts_by_transport` (JSON), `last_transport`, `last_transport_result`.
- `station_runtime` : état par station, IP courante, standby, lease,
  disponibilité, dernier cutover.
- `station_cutovers` : append-only, durée, résultat, raison.
- `transport_health` : agrégat par transport/station (200, 429, infra,
  latence, dégradé).
- `station_state` : état persistant par station (container, transport, IP,
  hostname, `client_usable`, `last_probe_at`, `last_200_at`, raison).
- `incidents` : scope `station`/`fleet`, cause, preuve, action, résultat.
- `repair_jobs`/`repair_requests` : file unique dans `registry.db`, backoff,
  attempts, transports, résultat.
- `discovery_runs`/`catalog_runs` : source, début/fin, compteurs, `stale`,
  erreurs, `server_count`.
- `egress_options` : options `candidate`/`usable`/`quota`/`auth`/`dead` ;
  fusion par `egress_ip` ; `reset_at` fournisseur.
- `egress_ips`/`quota_buckets` : ledger IP ; `quota_remaining` dérivé.

## Phase 2 — Implementation strategy

1. **Setup** : créer le package, les tests vides, la config, le feature.json
   (déjà fait).
2. **Foundational** : domaine + schéma + store + migration ; tests du domaine
   et du store ; aucun branchement runtime.
3. **US1** : ingestion API/cache/embedded → file ; transports ; score de
   potentiel ; ordre ; tests.
4. **US2** : époque, curseur, workers, reprise ; tests de crash.
5. **US3** : ledger quota, promotion 200, rotation 429, unicité IP ; tests.
6. **US4** : classification, suppression `country_backoff`, backoff infra ;
   tests de non-régression pays.
7. **US5** : preflight opérateur, RTT, score de connexion ; tests.
8. **US6** : API/dashboard de couverture ; tests de performance.
9. **US7** : option-map, config_audit, tests d'effet.
10. **US8** : concurrence six stations, leases, reprise superviseur ; tests.
11. **US9** : workers par station, standby, make-before-break, cutover,
    suppression des pauses globales, disponibilité 99 % ; tests de parallélisme
    et de coupure.
12. **US10** : cycle multi-transports, curseur/round, santé par transport,
    diagnostic OpenVPN, suppression du raccourci `fast_discovery` en `auto` ;
    tests d'ordre et de reprise.
13. **US11** : watchdog par station, découverte schedulée, réparation unifiée,
    `station_state`/`incidents`/`repair_jobs`, réutilisation lifecycle/
    reconciler/supervisor/manager ; tests de panne locale et de reprise.
14. **US12** : inventaire/seams, extraction manager/pool/fresh_chain/
    dashboard/opencode, parité, shadow/canary, rollback, D-02/D-20.
15. **Qualité** : gates ruff/mypy/coverage/import-linter, tests de contrat,
    injection de fautes, benchmarks, soak, ADR/debt/runbook, gate de release.
16. **Rollout** : shadow (file alimentée, aucun bind), canary (une station,
    un pays), puis général ; `fresh_chain --sweep` remplace le mode claims
    par défaut ; `--repair` reste. Activer les rotations parallèles après le
    canary cutover, puis le watchdog et la découverte schedulée.

## Risks and Mitigations

| Risque | Impact | Mitigation |
|---|---|---|
| Le balayage consomme le quota des IP qui servent | Baisse de capacité | Priorité service > repair > sweep ; ne pas balayer une IP `usable` en service ; utiliser le preflight avant le bind. |
| 85 % de `no_egress` | Balayage lent, faux échecs | Preflight DNS/connect/handshake ; fallback transport ; classer `infra` et non `dead` ; borner les retries. |
| Six sessions seulement | Goulot | File plus grande que la flotte ; workers par station ; pas de nouvelle session au-delà de `max_active_stations`. |
| Catalogue legacy 9 788 vs API 7 070 | Hosts fantômes | La file est alimentée par l'API ; les lignes legacy absentes sont `skipped`/`stale`, jamais claimées. |
| Migration des claims | Perte d'historique | Shadow + dual-write ; `claims` reste lisible ; rollback par backup. |
| Volume de tentatives | Disque/CPU | Rétention 7 jours, index, batchs, agrégats par époque. |
| Dashboard 7 070 lignes | Lenteur | Pagination, agrégats précalculés, cache 5 s, pas de chargement intégral. |
| Suppression `country_backoff` | Ruée sur un pays | Ordre par potentiel + quota ; pas de porte pays ; jitter sur les resets. |
| Crash en cours d'époque | Doublons | Curseur transactionnel, leases, idempotence, test de reprise. |
| Options GUI inertes | Mensonge opérationnel | `option-map` + gate + test d'effet. |
| OpenVPN 0/200 sur 16 045 tentatives | Fallback inutile, faux `dead` | Mesure par transport ; `degraded` + diagnostic bind/AUTH/probe/DNS ; ne jamais condamner le host. |
| Cycle multi-transports et quota | Consommation excessive | Essayer le transport suivant sur échec infra ; 429 par transport ; `transport_retry_rounds` borné ; priorité service. |
| Coupure au cutover | Perte de trafic | Standby validé, make-before-break, rollback, second tunnel si disponible, cutover mesuré. |
| Rotations parallèles et rate provider | 429/AUTH en rafale | Rate limit global de rebind, jitter, pas de pause globale ; per-station quarantine. |
| Dernière station utilisable | 0/6 | Protection de la dernière station ; rotation différée sans standby. |
| Watchdog non branché | Panne locale → panne de flotte | `station_state`/`incidents` persistés ; sondes par station ; réparation locale ; heartbeat watchdog. |
| Découverte non schedulée | Catalogue périmé, file figée | Scheduler API 6 h + cache/embedded ; `stale` explicite ; `discovery_runs`. |
| File de réparation séparée | Double réparateur, perte d'état | `repair_jobs` dans `registry.db` ; superviseur seul propriétaire. |
| Réutilisation manager | Régression | Strangler, shadow, canary, tests de non-régression ; aucun fork. |
| Dette de qualité | Régression lente | Gates ruff/mypy/coverage/import-linter, ADR/debt, runbook, release gate. |
| Performance non mesurée | Dérive | Benchmarks + soak 24 h, budgets dans la CI/nightly. |
| Couplage domaine/adapters | Maintenance difficile | Règle de dépendance, ports, contract tests, import-linter. |

## Rollout / Migration

1. **Shadow** : créer les tables, ingérer le catalogue, calculer les scores,
   mais ne faire aucun bind. Comparer l'ordre de la file à l'ordre actuel.
2. **Canary** : activer le balayage sur une station et 100 hostnames ; vérifier
   200/429/infra, le cycle des transports et la reprise.
3. **Parallèle** : activer les six workers et les rotations concurrentes ;
   vérifier qu'aucune panne de station n'affecte les autres et que le 6/6 tient.
4. **Cutover** : activer le standby et le make-before-break sur une station,
   mesurer la coupure, puis généraliser.
5. **Watchdog/découverte** : activer `station_state`, `incidents`,
   `repair_jobs` et le scheduler de découverte ; provoquer une panne locale et
   vérifier qu'elle ne touche qu'une station.
6. **Général** : `fresh_chain --sweep` devient le chemin de découverte ;
   `--repair` reste le chemin d'urgence.
7. **Décommissionnement** : `catalog_claims.claim_next` ne lit plus `catalog`
   legacy ; `country_backoff` est supprimé ; les pauses globales sont
   supprimées ; `logs/repair_queue.db` n'est plus une autorité ; `catalog`
   reste une table historique en lecture seule.
8. **Rollback** : backup `VACUUM INTO` avant migration ; les nouvelles tables
   sont optionnelles ; `egress_options`, `claims` et `repair_jobs` restent
   compatibles.

## Complexity Tracking

Aucune violation de constitution. La complexité ajoutée est justifiée par
l'absence de control plane unifié : file de qualification, runtime station,
watchdog, découverte et santé par transport. Les composants existants sont
réutilisés par strangler ; les modules restent < 1 500 lignes et le domaine ne
fait aucune I/O.
