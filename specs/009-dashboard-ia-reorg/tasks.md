# Tasks: Réorganisation des onglets Modèles Gratuits et Configuration

**Input**: Design documents from `specs/009-dashboard-ia-reorg/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`,
`contracts/`, `quickstart.md`

**Tests**: inclus, car la spec exige des tests de non-régression UI.

**Scope**: UI uniquement. Aucune API, aucune clé de config, aucun comportement
runtime ne change.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallélisable (fichiers différents)
- **[Story]**: US1–US5
- Chemins exacts dans chaque description.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: cartographier les réglages et préparer les conventions.

- [ ] T001 Cartographier chaque carte/réglage de `dashboard-ui/src/pages/FreeModels.svelte` et `dashboard-ui/src/pages/Config.svelte` vers un sous-onglet unique dans `specs/009-dashboard-ia-reorg/contracts/option-ownership.md`.
- [ ] T002 [P] Ajouter les clés i18n des sous-onglets et des sections dans `dashboard-ui/src/lib/locales.ts` (FR + EN).
- [ ] T003 [P] Définir la convention `data-testid="tab-<id>"` et les helpers de test dans `dashboard-ui/src/lib/testids.ts`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: barre de sous-onglets, routes, alias, lazy, dirty guard. Bloque
US1–US5.

**⚠️ CRITICAL**: ne pas commencer US1–US5 avant cette phase.

- [ ] T004 Créer `dashboard-ui/src/lib/components/SubTabs.svelte` : `role=tablist`, `role=tab`, `aria-selected`, clavier, scroll horizontal.
- [ ] T005 Étendre `dashboard-ui/src/lib/router.svelte.ts` pour supporter `#free-models/<tab>` et `#config/<tab>` (lecture/écriture, défaut, reload).
- [ ] T006 [P] Créer la table d'alias dans `dashboard-ui/src/lib/router.svelte.ts` (`#config`→server, `#free-models`→overview, `#vpn`→egress, inconnu→défaut).
- [ ] T007 [P] Ajouter un helper de lazy loading (`dynamic import`) dans `dashboard-ui/src/lib/lazy.ts`.
- [ ] T008 [P] Créer un composable `useDirtyGuard` dans `dashboard-ui/src/lib/dirty.svelte.ts`.
- [ ] T009 [P] Tests fondation : `dashboard-ui/src/lib/__tests__/router.test.ts`, `SubTabs.test.ts` (routes, alias, clavier, dirty).

**Checkpoint**: navigation de sous-onglets opérationnelle, alias et dirty
guard testés.

---

## Phase 3: User Story 1 - Modèles Gratuits par sous-onglets (Priority: P1) 🎯 MVP

**Goal**: 5 sous-onglets, chaque section accessible en ≤2 clics, 0 doublon.

**Independent Test**: cliquer chaque sous-onglet, reload, vérifier l'URL et
l'absence de section perdue.

### Tests for User Story 1

- [ ] T010 [P] [US1] `dashboard-ui/src/pages/free/__tests__/tabs.test.ts` : 5 onglets, défaut overview, reload.
- [ ] T011 [P] [US1] `dashboard-ui/src/pages/free/__tests__/no-dup.test.ts` : aucune clé de config dans deux onglets.

### Implementation for User Story 1

- [ ] T012 [US1] Créer `dashboard-ui/src/pages/free/Overview.svelte` (inventaire, counts, policy).
- [ ] T013 [US1] Créer `dashboard-ui/src/pages/free/Egress.svelte` (VpnStatus + AllocatorStatus + checkboxes providers).
- [ ] T014 [US1] Créer `dashboard-ui/src/pages/free/Routing.svelte` (VpnRouting + free attempts/strict/fallback).
- [ ] T015 [US1] Créer `dashboard-ui/src/pages/free/Quality.svelte` (quality/latency/timeouts/post-rotation).
- [ ] T016 [US1] Créer `dashboard-ui/src/pages/free/Advanced.svelte` (WARP-only, HTTPS, diagnostics).
- [ ] T017 [US1] Remplacer le contenu de `dashboard-ui/src/pages/FreeModels.svelte` par la coquille + `SubTabs` + lazy loading.
- [ ] T018 [US1] Vérifier chaque carte déplacée et supprimer les imports morts.

**Checkpoint**: Modèles Gratuits navigable, 0 section perdue.

---

## Phase 4: User Story 2 - Configuration par domaines (Priority: P1)

**Goal**: 5 sous-onglets, chaque réglage dans un seul onglet, sauvegardes OK.

**Independent Test**: cliquer chaque sous-onglet, vérifier les réglages et la
sauvegarde.

### Tests for User Story 2

- [ ] T019 [P] [US2] `dashboard-ui/src/pages/config/__tests__/tabs.test.ts` : 5 onglets, défaut server, reload.
- [ ] T020 [P] [US2] `dashboard-ui/src/pages/config/__tests__/save.test.ts` : sauvegarde par sous-onglet, valeurs conservées.

### Implementation for User Story 2

- [ ] T021 [US2] Créer `dashboard-ui/src/pages/config/Server.svelte` (serveur, port, bind, restart, debug).
- [ ] T022 [US2] Créer `dashboard-ui/src/pages/config/Models.svelte` (mapping, routes custom, catalogue).
- [ ] T023 [US2] Créer `dashboard-ui/src/pages/config/Keys.svelte` (API keys, token).
- [ ] T024 [US2] Créer `dashboard-ui/src/pages/config/Web.svelte` (web search/fetch).
- [ ] T025 [US2] Créer `dashboard-ui/src/pages/config/Maintenance.svelte` (updates, logs, backups).
- [ ] T026 [US2] Remplacer le contenu de `dashboard-ui/src/pages/Config.svelte` par la coquille + `SubTabs` + lazy loading.
- [ ] T027 [US2] Vérifier les sauvegardes et supprimer les doublons de cartes.

**Checkpoint**: Configuration navigable, 0 réglage perdu/dupliqué.

---

## Phase 5: User Story 3 - Anciens liens et deep links (Priority: P2)

**Goal**: 100 % des anciens hashes redirigent ; liens profonds partageables.

### Implementation for User Story 3

- [ ] T028 [US3] Implémenter les alias dans `dashboard-ui/src/lib/router.svelte.ts` et `dashboard-ui/src/lib/components/SubTabs.svelte`.
- [ ] T029 [US3] Gérer le hash inconnu → défaut sans erreur.
- [ ] T030 [US3] Vérifier précédent/suivant du navigateur.
- [ ] T031 [P] [US3] `dashboard-ui/src/lib/__tests__/aliases.test.ts` : chaque ancien hash, hash inconnu, back/forward.

**Checkpoint**: anciens liens et deep links fonctionnels.

---

## Phase 6: User Story 4 - Recherche et dirty guard (Priority: P2)

**Goal**: trouver un réglage et protéger les formulaires modifiés.

### Implementation for User Story 4

- [ ] T032 [US4] Ajouter l'index de recherche des réglages dans `dashboard-ui/src/lib/search.ts`.
- [ ] T033 [US4] Ajouter la barre de recherche + saut/highlight dans `dashboard-ui/src/lib/components/SubTabs.svelte` ou un composant dédié.
- [ ] T034 [US4] Brancher `useDirtyGuard` sur les formulaires de `dashboard-ui/src/pages/config/*` et `dashboard-ui/src/pages/free/*`.
- [ ] T035 [P] [US4] Tests : recherche « web search », dirty guard sauvegarder/annuler/quitter.

**Checkpoint**: recherche et dirty guard opérationnels.

---

## Phase 7: User Story 5 - Accessibilité, responsive, i18n (Priority: P3)

**Goal**: clavier, 375 px, FR/EN.

### Implementation for User Story 5

- [ ] T036 [US5] Finaliser ARIA/clavier dans `dashboard-ui/src/lib/components/SubTabs.svelte` (flèches, Home/End, focus visible).
- [ ] T037 [US5] Ajouter le scroll horizontal des sous-onglets à 375 px dans `dashboard-ui/src/lib/components/SubTabs.svelte`.
- [ ] T038 [US5] Vérifier les libellés FR/EN dans `dashboard-ui/src/lib/locales.ts`.
- [ ] T039 [P] [US5] Tests manuels clavier + responsive + captures dans `docs/evidence/009-dashboard-ia.md`.

**Checkpoint**: accessibilité et responsive validés.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [ ] T040 [P] Mettre à jour la matrice `option-ownership.md` après déplacement.
- [ ] T041 [P] Vérifier le premier rendu ≤ +20 % (lazy loading, pas de composant lourd au boot).
- [ ] T042 [P] Documenter les alias et le dirty guard dans `docs/evidence/009-dashboard-ia.md`.
- [ ] T043 Mettre à jour `debt.md` : aucune P0, dette listée.
- [ ] T044 `npm --prefix dashboard-ui run check` et `npm --prefix dashboard-ui run build` : 0 erreur.
- [ ] T045 Exécuter `quickstart.md` de bout en bout et consigner les résultats.
- [ ] T046 Commit final et push ; mettre à jour `.specify/feature.json` si nécessaire.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup**: aucune dépendance.
- **Foundational**: dépend du Setup ; **bloque US1–US5**.
- **US1/US2**: après Foundational ; peuvent avancer en parallèle (fichiers
  différents).
- **US3**: après US1/US2 (alias des routes).
- **US4**: après US1/US2 (recherche/dirty).
- **US5**: après US1/US2 (a11y/responsive).
- **Polish**: après les stories.

### Within Each Story

- Cartographie avant déplacement.
- Composants avant coquille.
- Coquille avant routes.
- Tests avant validation.

### Parallel Opportunities

- T002–T003, T006–T009.
- US1 T010–T011, T012–T016.
- US2 T019–T020, T021–T025.
- US3 T031.
- US4 T035.
- US5 T039.

## Implementation Strategy

### MVP

1. Phase 1 + Phase 2.
2. US1 (Modèles Gratuits) + US2 (Configuration).
3. Valider `quickstart.md` §1–§2.
4. Déployer.

### Incremental

1. Coquille + routes.
2. Modèles Gratuits.
3. Configuration.
4. Alias/deep links.
5. Recherche/dirty.
6. A11y/responsive.

## Notes

- UI uniquement : aucune API/config/runtime.
- Un réglage = un seul sous-onglet.
- Anciens hashes = alias, pas de 404.
- Lazy loading pour les composants lourds.
- `data-testid` stables.
