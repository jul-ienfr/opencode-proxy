> **Réconciliation (2026-11-23, T228 / spec 024)** : les tâches ouvertes de cette spec
> sont **supersédées** par la spec `024-audit-optimisation-globale` (et, pour le trafic
> free, par `023-client-usable-egress`). Ne pas les reprendre telles quelles : se
> référer à `specs/024-audit-optimisation-globale/tasks.md` pour la liste de travail
> courante. Les tâches déjà `[x]` restent valides comme historique.

# Tasks: Egress utilisable par les clients — NordVPN, WARP, proxys, direct

**Input**: Design documents from `/specs/023-client-usable-egress/`

**Prerequisites**: `plan.md` (requis), `spec.md` (requis pour les user stories),
`research.md`, `data-model.md`, `contracts/`

**Tests**: inclus — la constitution (XVII) exige un test ou une preuve live par
exigence fonctionnelle.

**Organization**: tâches groupées par user story pour une implémentation et une
validation indépendantes.

## Format: `[ID] [P?] [Story] Description`

- **[P]** : peut tourner en parallèle (fichiers différents, aucune dépendance)
- **[Story]** : user story de rattachement (US1…US10)
- Chaque tâche cite le chemin exact des fichiers

## Path Conventions

Projet unique : modules à la racine (`free/`, `scripts/`, `tests/`,
`opencode.py`), comme décrit dans `plan.md` (Project Structure).

---

> **État d'implémentation (2026-11-17, live)** : socle, classification, couche
> client, endpoints et garde sentinel livrés ; gate complète verte (exit 0) ;
> Claude Code 3/3 `200` après redémarrage (pid 29404). Les modules d'admission,
> de budget de sondes et de rotation sont testés mais pas encore branchés dans
> le chemin chaud ; les tâches correspondantes restent `[~]` ou `[ ]`.
>
> | Livré | Testé | Reste à brancher |
> |---|---|---|
> | `ip_state`, `classify_429`, `wire_profile`, `wire_admission` | oui (56 tests) | admission en mode `observe` seulement |
> | `clients/*` (registre, capacités, familles, politique d'outils, `respond`) | oui (31 contrats) | conversion active pour tous les clients dans `_free_wire_body` |
> | `probe_budget`, `egress_ip`, `provider_capabilities` | oui | — (branchés : sonde et superviseur) |
> | `rotation_policy` + garde de rotation | oui | — (branché : 180 s / 12 par heure) |
> | endpoints `/api/egress/{state,incidents,429,config}` + POST config | oui (live) | dashboard UI |
> | normalisation racine `system`, `wire_fp`, preuves `ip_state` | oui (live) | colonnes `requests.db` |
> | scripts : state, toggle, smoke, wire_ab, capacity, soak | oui | — |

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: structure et configuration minimales

- [x] T001 [P] Créer `free/clients/`, `free/clients/fixtures/`,
  `tests/contract/`, `tests/integration/`, `tests/unit/`
- [x] T002 [P] Déclarer `egress.providers.*` et `clients.*` dans
  `config/settings.py` + `config.yaml` + `tests/test_option_map.py`
- [x] T003 [P] Migration additive de `requests.db` (`429_class`, `wire_fp`,
  `reset_at`, `upstream_body`) dans `observability/db.py`, avec sauvegarde
  `VACUUM INTO` vérifiée

**Checkpoint**: configuration et schéma prêts, gate verte.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: socle commun à toutes les stories

- [x] T004 [US1] `free/ip_state.py` : machine à états
  `unproven|proven|serving|spent|burned|mismatch` + migration `registry.db`
  (`tests/unit/test_ip_state.py`)
- [x] T005 [US1] `free/classify_429.py` : classes `format|quota|session|upstream`,
  corps amont conservé ; hint `free tier` seule retirée (rate-limit générique =
  upstream, pas burn FORMAT) ; `parse_retry_after` borné 24 h / rejette
  `inf|nan|oversize` (`tests/unit/test_classify_429.py`)
- [x] T006 [US3] `free/wire_profile.py` : constructeur unique + `wire_fp`
  (`tests/unit/test_wire_profile.py`)
- [x] T007 [US3] `free/wire_admission.py` : `compat` interdit sur
  `proven`/`serving` (`tests/unit/test_wire_admission.py`)
- [x] T008 [US2] `free/egress_provider.py` : capacités `can_rotate`,
  `bucket_key`, `quota_source`, `health_source`

**Checkpoint**: socle prêt — les stories peuvent démarrer.

---

## Phase 3: User Story 1 - Client servi sans 429 (Priority: P1) — MVP

**Goal**: servir une requête client sur une IP de réserve prouvée, sans 429.

**Independent Test**: réserve ≥ 1 → requête `/v1/messages` → 200 avec contenu.

- [x] T009 [P] [US1] Test de contrat `/v1/messages` dans
  `tests/contract/test_anthropic_messages.py`
- [x] T010 [P] [US1] Test d'intégration réserve → service dans
  `tests/integration/test_client_reserve_flow.py`
  **LIVRÉ le 2026-11-22** — réserve construite par les vraies API
  (`client_reserve.add` + `ip_state.IPStateStore.transition(PROVEN)`),
  sélection réelle `FreeIPPool._best_station` (IP de réserve retenue, station
  hors réserve écartée), requête via `respond.convert_request`, 200 avec
  contenu validé par `openai_to_anthropic`, egress du service = IP de réserve,
  aucun 429. Frontière : seul le transport HTTP amont est un double (pas de
  socket/Docker). 3 tests, `pytest tests/integration/test_client_reserve_flow.py
  -q -p no:cacheprovider` → 3 passed. Marqués `client_reserve` pour désactiver
  l'isolation autouse de `_reserve_allows`.
- [x] T011 [US1] Routage client + admission dans `opencode.py`
- [x] T012 [US1] 503 `capacity_exhausted` si aucune capacité, 429 seulement si
  amont réel, dans `opencode.py`
- [x] T013 [US1] Sélection de réserve depuis `ip_state` dans
  `free/pool/__init__.py`
- [x] T014 [US1] Harnais de soak 24 h `scripts/soak_zero_429.py`

**Checkpoint**: MVP — un client est servi sans 429.

---

## Phase 4: User Story 2 - Quatre providers activables (Priority: P1)

**Goal**: NordVPN, WARP, proxys et direct activables indépendamment.

**Independent Test**: activer chaque provider seul puis combiné ; désactiver.

- [x] T015 [P] [US2] Contrat de configuration et d'état dans
  `tests/contract/test_egress_config.py`
- [x] T016 [P] [US2] Tests activation/désactivation < 60 s dans
  `tests/unit/test_provider_toggle.py`
- [x] T017 [US2] Unifier `egress.providers.*` avec migration depuis
  `proxy_mode`/`egress_providers` dans `config/settings.py`
- [x] T018 [US2] Sélection runtime et drain dans `free/egress_runtime.py`
- [x] T019 [US2] Exposer `/api/egress/config` dans `dashboard/api.py`
  *(livré dans `opencode.py` : `GET/POST /api/egress/config` — même app FastAPI)*

**Checkpoint**: les quatre providers se pilotent seuls.

---

## Phase 5: User Story 3 - Le proxy ne brûle aucune IP (Priority: P1)

**Goal**: aucun corps non conforme n'atteint une IP prouvée.

**Independent Test**: envoyer un corps Claude Code → wire officiel ; un corps
incompatible → 422 local sans egress.

- [x] T020 [P] [US3] Test de parité wire contre la capture dans
  `tests/contract/test_wire_parity.py`
- [x] T021 [P] [US3] Tests de la politique d'outils dans
  `tests/unit/test_tool_policy.py`
- [~] T022 [US3] Pipeline `convert` dans `free/wire_convert.py`
- [x] T023 [US3] Moteur de politique + mapping retour dans
  `free/clients/tool_policy.py`
- [x] T024 [US3] Budget de sondes séparé dans `free/probe_budget.py`
- [x] T025 [US3] Refus `compat` sur proven dans `free/wire_admission.py`
- [x] T026 [US3] `scripts/wire_ab.py` : A/B à IP neuve par variante

**Checkpoint**: 0 corps incompatible n'atteint un bucket prouvé.

---

## Phase 6: User Story 4 - Libération prouvée (Priority: P1)

**Goal**: une IP prouvée n'est libérée que sur preuve dure.

**Independent Test**: 429 ambigu → IP conservée ; sentinel `1.2.3.4` → rejeté.

- [x] T027 [P] [US4] Tests de preuve de libération dans
  `tests/unit/test_release_evidence.py`
- [x] T028 [P] [US4] Tests sentinel et double lecteur dans
  `tests/unit/test_egress_ip.py`
- [~] T029 [US4] Libération prouvée dans `scripts/fresh_chain_supervisor.py`
- [x] T030 [US4] Deux lecteurs concordants dans `free/egress_ip.py`
- [x] T031 [US4] Incident et `evidence_ref` dans `free/ip_state.py`
- [x] T032 [US4] Régression `1.2.3.4` dans
  `tests/integration/test_sentinel_release.py`

**Checkpoint**: aucune libération sans preuve.

---

## Phase 7: User Story 5 - Rotation bornée (Priority: P2)

**Goal**: un seul réparateur, rotations plafonnées et coalescées.

**Independent Test**: 10 demandes en rafale → 1 chaîne, 9 coalescées.

- [x] T033 [P] [US5] Test de rafale dans
  `tests/unit/test_rotation_token_bucket.py`
- [x] T034 [US5] Token bucket `min_interval_s` et `max_per_hour` dans
  `scripts/fresh_chain_supervisor.py`
- [x] T035 [US5] Coalescence `(provider, station)` dans
  `free/repair_queue.py`
- [x] T036 [US5] Journal de rotation cause + preuve dans
  `scripts/fresh_chain.py`

**Checkpoint**: ≤ 6 rotations/h, ≤ 1/10 min/station.

---

## Phase 8: User Story 6 - Observabilité actionnable (Priority: P2)

**Goal**: voir buckets, sondes, rotations, classes de 429 et libérations.

**Independent Test**: `/api/egress/state` reflète les états réels.

- [x] T037 [P] [US6] Contrat `/api/egress/state` dans
  `tests/contract/test_egress_state.py`
- [x] T038 [P] [US6] Contrat `/api/egress/incidents` dans
  `tests/contract/test_egress_incidents.py`
- [x] T039 [P] [US6] Contrat `/api/egress/429` dans
  `tests/contract/test_egress_429.py`
- [x] T040 [US6] Implémentation des trois endpoints dans `dashboard/api.py`
- [ ] T041 [US6] Panneaux dashboard (états, classes, rotations)
- [~] T042 [US6] Métriques par client dans `observability/db.py`

**Checkpoint**: chaque décision est visible et justifiée.

---

## Phase 9: User Story 7 - Chaque client parle son protocole (Priority: P1)

**Goal**: Claude Code, Codex, Kilo Code, Hermes Agent, DeepSeek, Aider, SDK
OpenAI et client inconnu servis dans leur format.

**Independent Test**: 3/3 200 par client, stream et non-stream, sur IP neuve.

- [x] T043 [P] [US7] Test du registre et des capacités dans
  `tests/unit/test_client_registry.py`
- [x] T044 [P] [US7] Contrat Anthropic stream/non-stream dans
  `tests/contract/test_anthropic_stream.py`
- [x] T045 [P] [US7] Contrat OpenAI chat dans
  `tests/contract/test_openai_chat_stream.py`
- [x] T046 [P] [US7] Contrat Responses dans
  `tests/contract/test_openai_responses_stream.py`
- [x] T047 [US7] `free/clients/registry.json` + `capabilities.py`
- [x] T048 [US7] `free/clients/family_anthropic.py`
- [x] T049 [US7] `free/clients/family_openai_chat.py`
- [x] T050 [US7] `free/clients/family_openai_responses.py`
- [x] T051 [US7] `free/clients/family_passthrough.py` + `custom`
- [x] T052 [US7] Fixtures golden par client dans
  `free/clients/fixtures/<client>/`
- [x] T053 [US7] `docs/CLIENTS.md` + contrats `option-map`

**Checkpoint**: chaque client de la matrice est servi dans son format.

---

## Phase 10: User Story 8 - Politique d'outils explicite (Priority: P1)

**Goal**: autorisés, interdits, alias, shims et mapping retour.

**Independent Test**: `Read`/`Bash`/`TodoWrite`/outil MCP → wire conforme et
retour au nom d'origine.

- [x] T054 [P] [US8] Contrat de la grille officielle dans
  `tests/contract/test_official_tool_grid.py`
- [x] T055 [P] [US8] Test aller-retour des noms d'outils dans
  `tests/unit/test_tool_roundtrip.py`
- [x] T056 [US8] `free/clients/tool_policy.json`
- [x] T057 [US8] Healing d'un outil inconnu (`text_plan`, refus)
- [x] T058 [US8] Extras MCP bornés (`client_extras_max`)

**Checkpoint**: 0 appel non déclaré relayé.

---

## Phase 11: User Story 9 - Fidélité de flux et d'erreurs (Priority: P1)

**Goal**: SSE et erreurs traduits par famille, sans perte.

**Independent Test**: 100 flux par famille, séquences complètes.

- [x] T059 [P] [US9] Tests de fidélité SSE dans
  `tests/contract/test_sse_fidelity.py`
- [x] T060 [P] [US9] Tests de mapping d'erreurs dans
  `tests/unit/test_error_mapping.py`
- [x] T061 [US9] `free/clients/respond.py` : adapters JSON/SSE, usage
- [~] T062 [US9] Erreur mid-stream traduite et flux client terminé proprement

**Checkpoint**: aucun événement manquant, dupliqué ou réordonné.

---

## Phase 12: User Story 10 - Wire identique sur les quatre providers (Priority: P2)

**Goal**: le provider ne change pas l'empreinte observée.

**Independent Test**: même requête via les quatre chemins → même JA3/JA4,
même ordre, même `wire_fp`.

- [x] T063 [P] [US10] Test JA3/JA4 inter-provider
- [x] T064 [P] [US10] Test d'ordre des en-têtes inter-provider
- [x] T065 [US10] Test `wire_fp` identique + hook CI

**Checkpoint**: quatre chemins, un seul profil.

---

## Phase 13: Polish & Cross-Cutting Concerns

- [x] T066 [P] Multimodal, budget de contexte et `count_tokens` par famille
  **LIVRÉ le 2026-11-22** — `respond.count_tokens(body, family)` dispatche
  vers chaque famille (`system`/`messages`/`tools`, `instructions`/`input`/
  `tools`, passthrough) ; `protocol.tokens._media_token_cost` couvre déjà le
  multimodal. Tests `tests/unit/test_count_tokens_family.py` (7 cas).
- [x] T067 [P] `scripts/capacity_probe.py` : capacité réelle par bucket
- [ ] T068 [P] Benchmarks : surcoût p50, TTFB, mémoire, 100 flux
- [x] T069 [P] Chaos par provider (tunnel, 429 mid-stream, sentinel, proxy mort)
  **LIVRÉ le 2026-11-22** — sentinel/concordance (`tests/test_023_chaos.py`),
  429 après commit forwardé sans rejeu
  (`tests/test_retry_until_success.py::test_mid_stream_429_frame_is_forwarded_without_retry`),
  matrice tunnel coupé/proxy mort (`tests/test_egress_public_adapter.py::
  test_dead_tunnel_matrix_never_promotes`).
- [x] T070 [P] `docs/CLIENTS.md`, `CHANGELOG`, `option-map`
- [ ] T071 Gate complète : `pytest -k "not docker"`, `config_audit --strict`,
  `platform_audit --strict`, `test_option_map`, ruff, `py_compile`
- [x] T073 [P] Outillage d'exploitation : scripts/client_smoke.py,
  scripts/egress_state.py, scripts/egress_toggle.py
- [ ] T072 Soak live 24 h : SC-001 à SC-014

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)** : aucune dépendance.
- **Foundational (Phase 2)** : dépend du Setup ; **bloque** toutes les stories.
- **US1 (Phase 3)** : dépend du Foundational — MVP.
- **US3, US4, US7, US8, US9 (P1)** : dépendent du Foundational ; US7 dépend de
  US3 pour la construction du wire.
- **US2 (P1)** : dépend du Foundational ; peut avancer en parallèle de US1.
- **US5, US6, US10 (P2)** : dépendent des P1.
- **Polish (Phase 13)** : dépend de toutes les stories retenues.

### User Story Dependencies

- **US1** : aucune dépendance sur une autre story.
- **US2** : indépendante ; utilise `egress_provider.py` du Foundational.
- **US3** : indépendante ; fournit le wire à US7.
- **US4** : indépendante ; utilise `ip_state` et `classify_429`.
- **US5** : indépendante ; utilise `repair_queue`.
- **US6** : dépend des états produits par US1/US4.
- **US7** : dépend de US3 (wire) ; sinon testable avec un wire stub.
- **US8** : dépend de US7 (familles) mais testable sur données seules.
- **US9** : dépend de US7 (adapters) ; testable sur fixtures.
- **US10** : dépend de US2 et US3.

### Within Each User Story

- Tests d'abord, échouants avant implémentation.
- Modèles/domaine avant services, services avant routes.
- Story terminée et validée avant la priorité suivante.

---

## Parallel Example: User Story 7

```bash
Task: "Contrat Anthropic stream/non-stream dans tests/contract/test_anthropic_stream.py"
Task: "Contrat OpenAI chat dans tests/contract/test_openai_chat_stream.py"
Task: "Contrat Responses dans tests/contract/test_openai_responses_stream.py"
Task: "Registre et capacités dans free/clients/registry.json + capabilities.py"
```

---

## Implementation Strategy

### MVP First (US1)

1. Setup + Foundational.
2. US1 : client servi sans 429.
3. **STOP et VALIDER** : 24 h à 0 429 tant que `client_reserve ≥ 1`.

### Incremental Delivery

1. US1 → validation → déploiement.
2. US3 + US7 + US8 + US9 → tous les clients servis proprement.
3. US2 + US4 + US5 → providers et rotation sûre.
4. US6 + US10 + Polish → observabilité, parité provider, performance.

---

## Notes

- `[P]` = fichiers différents, aucune dépendance.
- Chaque story est indépendamment testable et livrable.
- Une IP = un format (principe XVIII) : jamais d'A/B sur une IP usée.
- Aucune recréation de conteneur pour une rotation.
- Mettre à jour spec/plan/CHANGELOG/option-map dans le même lot.
