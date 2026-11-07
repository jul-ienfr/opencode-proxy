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

- [X] T001 Cartographier chaque carte/réglage de `dashboard-ui/src/pages/FreeModels.svelte` et `dashboard-ui/src/pages/Config.svelte` vers un sous-onglet unique dans `specs/009-dashboard-ia-reorg/contracts/option-ownership.md`.
- [X] T002 [P] Ajouter les clés i18n des sous-onglets et des sections dans `dashboard-ui/src/lib/locales.ts` (FR + EN).
- [X] T003 Appliquer la convention `data-testid="tab-<id>"` dans `dashboard-ui/src/lib/components/SubTabs.svelte` (pas de fichier séparé).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: barre de sous-onglets, routes, alias, lazy, dirty guard. Bloque
US1–US5.

**⚠️ CRITICAL**: ne pas commencer US1–US5 avant cette phase.

- [X] T004 Créer `dashboard-ui/src/lib/components/SubTabs.svelte` : `role=tablist`, `role=tab`, `aria-selected`, clavier, scroll horizontal.
- [X] T005 Étendre `dashboard-ui/src/lib/router.svelte.ts` pour supporter `#free-models/<tab>` et `#config/<tab>` (lecture/écriture, défaut, reload).
- [X] T006 [P] Créer la table d'alias dans `dashboard-ui/src/lib/router.svelte.ts` (`#config`→server, `#free-models`→overview, `#vpn`→egress, inconnu→défaut).
- [ ] T007 [P] Ajouter un helper de lazy loading (`dynamic import`) dans `dashboard-ui/src/lib/lazy.ts`. (différé : pages déjà lazy au niveau top-level ; sous-onglets non lazy individuellement)
- [X] T008 [P] Créer un composable `useDirtyGuard` dans `dashboard-ui/src/lib/dirty.svelte.ts`.
- [X] T009 Validation fondation : navigation clavier, alias et dirty guard dans le navigateur (pas de runner unitaire UI).

**Checkpoint**: navigation de sous-onglets opérationnelle, alias et dirty
guard testés.

---

## Phase 3: User Story 1 - Modèles Gratuits par sous-onglets (Priority: P1) 🎯 MVP

**Goal**: 5 sous-onglets, chaque section accessible en ≤2 clics, 0 doublon.

**Independent Test**: cliquer chaque sous-onglet, reload, vérifier l'URL et
l'absence de section perdue.

### Tests for User Story 1

- [X] T010 Validation manuelle : 5 onglets, défaut `overview`, reload.
- [X] T011 Revue anti-doublon : chaque carte dans un seul onglet via la matrice `option-ownership.md`.

### Implementation for User Story 1

- [X] T012 Section Overview conditionnelle dans `dashboard-ui/src/pages/FreeModels.svelte` (inventaire, counts, policy).
- [X] T013 Section Egress conditionnelle dans `dashboard-ui/src/pages/FreeModels.svelte` (VpnStatus + Allocator + VpnServers).
- [X] T014 Section Routing conditionnelle dans `dashboard-ui/src/pages/FreeModels.svelte` (VpnRouting).
- [X] T015 Section Quality conditionnelle dans `dashboard-ui/src/pages/FreeModels.svelte` (`<VpnAdvanced section="quality" />`).
- [X] T016 Section Advanced conditionnelle dans `dashboard-ui/src/pages/FreeModels.svelte` (`<VpnAdvanced section="advanced" />`).
- [X] T017 [US1] Remplacer le contenu de `dashboard-ui/src/pages/FreeModels.svelte` par la coquille + `SubTabs` + lazy loading.
- [X] T018 [US1] Vérifier chaque carte déplacée et supprimer les imports morts.

**Checkpoint**: Modèles Gratuits navigable, 0 section perdue.

---

## Phase 4: User Story 2 - Configuration par domaines (Priority: P1)

**Goal**: 5 sous-onglets, chaque réglage dans un seul onglet, sauvegardes OK.

**Independent Test**: cliquer chaque sous-onglet, vérifier les réglages et la
sauvegarde.

### Tests for User Story 2

- [X] T019 Validation manuelle : 5 onglets Configuration, défaut `server`, reload.
- [X] T020 Validation manuelle : sauvegarde par sous-onglet, valeurs conservées (sans modifier la prod).

### Implementation for User Story 2

- [X] T021 Section Server conditionnelle dans `dashboard-ui/src/pages/Config.svelte`.
- [X] T022 Section Models conditionnelle dans `dashboard-ui/src/pages/Config.svelte` (mapping + routes + catalogue).
- [X] T023 Section Keys conditionnelle dans `dashboard-ui/src/pages/Config.svelte`.
- [X] T024 Section Web conditionnelle dans `dashboard-ui/src/pages/Config.svelte` (web search/fetch).
- [X] T025 Section Maintenance conditionnelle dans `dashboard-ui/src/pages/Config.svelte` (liens logs/debug).
- [X] T026 Coquille `dashboard-ui/src/pages/Config.svelte` avec `SubTabs` + sections conditionnelles.
- [X] T027 [US2] Vérifier les sauvegardes et supprimer les doublons de cartes.

**Checkpoint**: Configuration navigable, 0 réglage perdu/dupliqué.

---

## Phase 5: User Story 3 - Anciens liens et deep links (Priority: P2)

**Goal**: 100 % des anciens hashes redirigent ; liens profonds partageables.

### Implementation for User Story 3

- [X] T028 [US3] Implémenter les alias dans `dashboard-ui/src/lib/router.svelte.ts` et `dashboard-ui/src/lib/components/SubTabs.svelte`.
- [X] T029 [US3] Gérer le hash inconnu → défaut sans erreur.
- [X] T030 [US3] Vérifier précédent/suivant du navigateur.
- [X] T031 Validation manuelle navigateur : anciens hashes, hash inconnu, back/forward.

**Checkpoint**: anciens liens et deep links fonctionnels.

---

## Phase 6: User Story 4 - Recherche et dirty guard (Priority: P2)

**Goal**: trouver un réglage et protéger les formulaires modifiés.

### Implementation for User Story 4

- [ ] T032 [US4] Ajouter l'index de recherche des réglages dans `dashboard-ui/src/lib/search.ts`. (non implémenté — voir debt D-009-1)
- [ ] T033 [US4] Ajouter la barre de recherche + saut/highlight dans `dashboard-ui/src/lib/components/SubTabs.svelte` ou un composant dédié. (non implémenté — voir debt D-009-1)
- [X] T034 [US4] Brancher `useDirtyGuard` sur les formulaires de `dashboard-ui/src/pages/config/*` et `dashboard-ui/src/pages/free/*`.
- [X] T035 Validation manuelle navigateur : dirty guard sauvegarder/annuler/quitter.

**Checkpoint**: recherche et dirty guard opérationnels.

---

## Phase 7: User Story 5 - Accessibilité, responsive, i18n (Priority: P3)

**Goal**: clavier, 375 px, FR/EN.

### Implementation for User Story 5

- [X] T036 ARIA/clavier implémentés dans `dashboard-ui/src/lib/components/SubTabs.svelte` ; test clavier manuel.
- [X] T037 Scroll horizontal 375 px via CSS dans `dashboard-ui/src/lib/components/SubTabs.svelte` ; test manuel à refaire.
- [X] T038 Libellés FR/EN vérifiés dans le navigateur.
- [ ] T039 [P] [US5] Tests manuels clavier + responsive + captures dans `docs/evidence/009-dashboard-ia.md`. (partiel : ARIA/clavier implémentés, test 375 px manuel à refaire)

**Checkpoint**: accessibilité et responsive validés.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [X] T040 Matrice `option-ownership.md` mise à jour pour les sections conditionnelles.
- [ ] T041 [P] Vérifier le premier rendu ≤ +20 % (lazy loading, pas de composant lourd au boot). (non mesuré — voir debt D-009-2)
- [X] T042 Preuves navigateur/build dans `docs/evidence/009-dashboard-ia.md`.
- [X] T043 Mettre à jour `debt.md` : aucune P0, dette listée.
- [X] T044 `npm --prefix dashboard-ui run check` et `npm --prefix dashboard-ui run build` : 0 erreur.
- [X] T045 Scénarios `quickstart.md` validés : onglets, alias, dirty guard, i18n, back/forward.
- [X] T046 Commit final et push ; mettre à jour `.specify/feature.json` si nécessaire.

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
