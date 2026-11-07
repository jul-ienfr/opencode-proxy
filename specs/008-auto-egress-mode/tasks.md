# Tasks: Auto-Egress — mode multi-provider automatique

**Input**: Design documents from `specs/008-auto-egress-mode/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`,
`contracts/`, `quickstart.md`, feature 007 (provider abstraction, erreurs,
ledger).

**Tests**: inclus, car la spec exige des tests pour chaque exigence.

**Scope**: uniquement les providers déjà configurés (NordVPN, WARP, proxys).
Aucun nouvel abonnement payant, aucun direct silencieux.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallélisable (fichiers différents, pas de dépendance)
- **[Story]**: US1–US5
- Chemins exacts dans chaque description.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: préparer la config, les libellés et les fixtures.

- [ ] T001 Ajouter la section `ip_rotation.auto_egress` (providers, weights, caps, hedge, direct, diversity, sticky) dans `config.yaml`.
- [ ] T002 [P] Ajouter les libellés `free_models.mode_auto_egress` et le sous-titre dans `dashboard-ui/src/lib/locales.ts` (FR + EN).
- [ ] T003 [P] Créer les fixtures providers (`warp`, `nordvpn`, `proxy`, `down`, `403`, `429`) dans `tests/fixtures/auto_egress_providers.py`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: mode enum, pool unifié, sélecteur, caps, attribution. Bloque US1–US5.

**⚠️ CRITICAL**: ne pas commencer US1–US5 avant cette phase.

- [ ] T004 Ajouter `auto_egress` à l'enum de mode et à `_free_proxy_mode` dans `opencode.py` et `free/pool.py`.
- [ ] T005 [P] Créer `free/auto_egress.py` : `AutoEgressConfig`, `UnifiedEndpoint`, `ProviderState`, `build_pool()`.
- [ ] T006 [P] Créer `free/provider_selector.py` : score (santé/quota/latence/succès/poids), portes dures, dédup.
- [ ] T007 Implémenter les sémaphores `max_concurrent` par provider dans `free/provider_selector.py`.
- [ ] T008 Implémenter le sticky par conversation (`sticky_ttl_s`) dans `free/provider_selector.py`.
- [ ] T009 Brancher le pool unifié dans `free/pool.py` : sélection, retry, hedge, caps.
- [ ] T010 [P] Ajouter la validation/normalisation des poids et caps dans `config/settings.py` (somme 100, warning si normalisée).
- [ ] T011 [P] Ajouter l'attribution par requête (provider, endpoint, bloc, statut, latence) dans `registry.db` via `rotation/infrastructure/db/migrate.py` + writer.
- [ ] T012 [P] Tests fondation : `tests/test_auto_egress_mode.py`, `tests/test_provider_selector.py` (score déterministe, dédup, caps, sticky).

**Checkpoint**: mode interne + pool unifié + sélecteur testés.

---

## Phase 3: User Story 1 - Activer Auto-Egress depuis le GUI (Priority: P1) 🎯 MVP

**Goal**: 4e mode visible, sélectionnable, persistant, switch sans interruption.

**Independent Test**: sélectionner Auto-Egress, reload, restart, vérifier le mode
et les providers listés.

### Tests for User Story 1

- [ ] T013 [P] [US1] `tests/test_auto_egress_ui.py` : bouton visible, valeur `auto_egress`, sélection persistée.
- [ ] T014 [P] [US1] `tests/test_auto_egress_mode_switch.py` : changement de mode avec requête en vol, 0 interruption.

### Implementation for User Story 1

- [ ] T015 [US1] Ajouter le bouton Auto-Egress dans `dashboard-ui/src/pages/vpn/VpnStatus.svelte` (à côté de NordVPN/WARP/Direct).
- [ ] T016 [US1] Étendre `POST /api/vpn/proxy-mode` à `auto_egress` dans `dashboard/api.py` et `opencode.py`.
- [ ] T017 [US1] Persister/restaurer le mode dans `config.yaml` et au boot.
- [ ] T018 [US1] Implémenter le drain des requêtes en vol avant bascule dans `free/pool.py`.
- [ ] T019 [US1] Vérifier le rendu FR/EN et l'absence de select vide dans `dashboard-ui/src/pages/vpn/VpnStatus.svelte`.

**Checkpoint**: US1 livrable seule (mode activable, sans sélection multi-provider complète).

---

## Phase 4: User Story 2 - Utiliser tous les providers en parallèle (Priority: P1)

**Goal**: pool unifié WARP + NordVPN + proxys, répartition, hedge, sticky.

**Independent Test**: 20 requêtes → ≥2 providers, >99 % de 200.

### Tests for User Story 2

- [ ] T020 [P] [US2] `tests/test_auto_egress_hedge.py` : premier provider lent, second 200, hedge gagné.
- [ ] T021 [P] [US2] `tests/test_auto_egress_weights.py` : répartition 60/30/10 à ±10 points sur 100 requêtes simulées.
- [ ] T022 [P] [US2] `tests/test_auto_egress_sticky.py` : conversation garde son egress tant qu'il est sain.

### Implementation for User Story 2

- [ ] T023 [US2] Construire `UnifiedEndpoint` depuis NordVPN managers, WARP endpoints et proxys dans `free/auto_egress.py`.
- [ ] T024 [US2] Brancher le score et les poids dans `free/provider_selector.py`.
- [ ] T025 [US2] Brancher le hedge/retry multi-provider dans `free/pool.py` (réutiliser `free_parallel` si possible).
- [ ] T026 [US2] Appliquer les caps de concurrence et la diversité douce dans `free/provider_selector.py`.
- [ ] T027 [US2] Journaliser la décision et les alternatives dans `registry.db` / `logs`.
- [ ] T028 [US2] Documenter la preuve live 20 requêtes dans `docs/evidence/008-auto-egress.md`.

**Checkpoint**: ≥2 providers servent, hedge/sticky/caps prouvés.

---

## Phase 5: User Story 3 - Isoler les pannes et les quotas (Priority: P1)

**Goal**: 403/429/down d'un provider n'affecte pas les autres ; fail-closed.

**Independent Test**: 403 NordVPN + 429 WARP + timeout proxy → les providers
restants servent, aucun 403 ne tourne.

### Tests for User Story 3

- [ ] T029 [P] [US3] `tests/test_auto_egress_failover.py` : provider down, 429 quota, 429 court, 5xx, 403.
- [ ] T030 [P] [US3] `tests/test_auto_egress_direct.py` : `allow_direct=false` → 0 direct, `degraded` explicite.
- [ ] T031 [P] [US3] `tests/test_auto_egress_quota.py` : park/rotate WARP ne touche pas NordVPN/proxy ; dédup de bloc.

### Implementation for User Story 3

- [ ] T032 [US3] Réutiliser `free/errors.py` pour classer chaque réponse dans `free/auto_egress.py`.
- [ ] T033 [US3] Propager park/rotate au provider concerné uniquement dans `free/pool.py` et `free/quota_ledger.py`.
- [ ] T034 [US3] Retirer un provider `down`/`unknown` de la sélection sans casser les autres dans `free/provider_selector.py`.
- [ ] T035 [US3] Implémenter `degraded` + raison (`no_provider_available`, `one_provider_only`) dans `free/auto_egress.py`.
- [ ] T036 [US3] Interdire le direct silencieux (`allow_direct=false`, `fail_closed=true`) dans `free/auto_egress.py`.
- [ ] T037 [US3] Ajouter la preuve live provider down/403/429 dans `docs/evidence/008-auto-egress.md`.

**Checkpoint**: isolation prouvée, fail-closed, 0 rotation sur 403.

---

## Phase 6: User Story 4 - Régler les poids et la diversité (Priority: P2)

**Goal**: activer/désactiver, poids, caps, hedge, direct, sticky — avec effet
runtime.

**Independent Test**: régler 60/30/10, envoyer 100 requêtes, vérifier la
répartition et les caps.

### Tests for User Story 4

- [ ] T038 [P] [US4] `tests/test_auto_egress_config.py` : validation poids/caps, normalisation, valeurs invalides.
- [ ] T039 [P] [US4] `tests/test_auto_egress_gui_contract.py` : chaque option a un consommateur runtime.

### Implementation for User Story 4

- [ ] T040 [US4] Exposer les options Auto-Egress dans `dashboard-ui/src/pages/vpn/VpnAdvanced.svelte`.
- [ ] T041 [US4] Brancher `weights`, `max_concurrent`, `hedge_*`, `allow_direct`, `diversity`, `sticky` dans `free/provider_selector.py`.
- [ ] T042 [US4] Ajouter la validation GUI/API (somme 100, bornes, confirmation direct) dans `dashboard/api.py`.
- [ ] T043 [US4] Persister les options et vérifier le reload/restart.
- [ ] T044 [US4] Documenter la matrice option → consommateur dans `specs/008-auto-egress-mode/contracts/mode-ui.md`.

**Checkpoint**: poids/caps/options effectifs et persistants.

---

## Phase 7: User Story 5 - Voir qui sert quoi (Priority: P2)

**Goal**: dashboard providers/santé/quota/attribution.

**Independent Test**: 20 requêtes → répartition et attribution visibles.

### Tests for User Story 5

- [ ] T045 [P] [US5] `tests/test_auto_egress_status.py` : `/api/vpn-status` expose providers, poids, santé, attribution.
- [ ] T046 [P] [US5] `tests/test_auto_egress_attribution.py` : chaque requête a provider/endpoint/bloc/statut.

### Implementation for User Story 5

- [ ] T047 [US5] Étendre `/api/vpn-status`, `/api/free-health`, `/api/free-egress/diag` dans `dashboard/api.py` et `opencode.py`.
- [ ] T048 [US5] Afficher providers/poids/santé/quota/répartition dans `dashboard-ui/src/pages/vpn/VpnStatus.svelte`.
- [ ] T049 [US5] Afficher l'attribution récente (provider, bloc, statut, latence) dans le dashboard.
- [ ] T050 [US5] Ajouter les métriques switches/hedges/429/403 dans `dashboard/api.py`.
- [ ] T051 [US5] Documenter la preuve live d'attribution dans `docs/evidence/008-auto-egress.md`.

**Checkpoint**: 100 % des requêtes récentes attribuées.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [ ] T052 [P] Mettre à jour `spec.md`, `plan.md`, `contracts/` si une décision change.
- [ ] T053 [P] Compléter la matrice GUI pour 100 % des options dans `specs/008-auto-egress-mode/contracts/mode-ui.md`.
- [ ] T054 Mettre à jour `debt.md` : aucune P0 ouverte, dette listée.
- [ ] T055 `python -m pytest -k "not docker" -q`, `python -m ruff check .`, `npm --prefix dashboard-ui run check` : exit 0.
- [ ] T056 Exécuter `quickstart.md` de bout en bout et consigner les résultats.
- [ ] T057 Soak 24 h avec providers réels, 403/429/down injectés, preuves dans `docs/evidence/008-auto-egress.md`.
- [ ] T058 Commit final et push ; mettre à jour `.specify/feature.json` si nécessaire.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup**: aucune dépendance.
- **Foundational**: dépend du Setup ; **bloque US1–US5**.
- **US1**: après Foundational (mode UI/persistance).
- **US2**: après US1 + Foundational (pool/selection).
- **US3**: après US2 (isolation/quota).
- **US4**: après US2 (poids/caps).
- **US5**: après US2/US3 (observabilité).
- **Polish**: après les stories retenues.

### Within Each Story

- Tests avant implémentation.
- Modèles/entités avant sélecteur.
- Sélecteur avant intégration pool.
- Preuve live avant de cocher.

### Parallel Opportunities

- T002–T003, T005–T006, T010–T012.
- US1 T013–T014.
- US2 T020–T022, T023/T024, T025/T026.
- US3 T029–T031, T032/T033.
- US4 T038–T039, T040/T041.
- US5 T045–T046, T047/T048.

## Implementation Strategy

### MVP

1. Phase 1 + Phase 2.
2. US1 (mode visible/persistant).
3. US2 (WARP + NordVPN, proxys ensuite).
4. Valider `quickstart.md` §2–§3.
5. Déployer.

### Incremental

1. US1 → mode activable.
2. US2 → multi-provider.
3. US3 → isolation/quotas.
4. US4 → poids/options.
5. US5 → observabilité.
6. Polish → soak/docs.

### Prerequisites

- Feature 007 doit être implémentée (provider abstraction, erreurs, ledger).
- NordVPN/WARP/proxys doivent être configurés ; aucun ajout payant.

## Notes

- [P] = fichiers différents, pas de dépendance.
- [US] relie la tâche à la user story.
- Chaque story doit être testable seule.
- Aucun nouvel abonnement, aucun direct silencieux.
