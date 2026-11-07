# Tasks — 010 Egress station sizing

**Feature**: `specs/010-egress-station-sizing/`
**Input**: spec.md, plan.md, contracts/capacity.md

## Phase 1: Setup

- [x] T001 Créer `specs/010-egress-station-sizing/` (spec, plan, contrat) et
  vérifier la cohérence avec 008 (mode/poids).
- [x] T002 Ajouter la section `egress_capacity` à `config.yaml` avec
  `target_slots: 6`, `reserve_policy: full`.

## Phase 2: Calculs purs (Foundational)

- [x] T003 Créer `free/capacity.py` : `ProviderCapacity`, `CapacityReport`.
- [x] T004 Implémenter `required_by_policy` (intégré à `evaluate_capacity`).
- [x] T005 Implémenter `proportional_allocation(target, weights, fixed)` par
  recherche exhaustive (minimise l'écart de charge/endpoint).
- [x] T006 Implémenter `per_endpoint` et `evaluate_capacity` avec les
  verdicts `ok|degraded|insufficient`.
- [x] T007 Tests unitaires : 50/50→3+3, 2:1→4+2, 3:1→4+2, 1:1:1→2+2+2,
  40/40/20→3+2+1, Direct fixe 1, déficit.
- [x] T008 Tests verdicts : full 6+6→ok, full 6+3→degraded, shared 3+3→ok,
  shared 2+3→insufficient, déséquilibre >2×→warning.

## Phase 3: Lecture de la configuration (US1)

- [x] T009 Lire `egress_capacity` en live depuis `IP_ROTATION` via
  `capacity_config` (défauts robustes 1–10, policy inconnue → `full`).
- [x] T010 Compter NordVPN depuis `resolved_station_count(IP_ROTATION)` +
  `usable_station_count()` du pool.
- [x] T011 Compter WARP/proxys depuis les endpoints du pool (`provider=warp`
  vs autres) et Direct = 1.
- [x] T012 Exposer `_egress_capacity_counts()` dans `dashboard/api.py` et
  `FreeIPPool.egress_endpoint_counts()` (sans Docker, sans probe).

## Phase 4: API (US1/US2)

- [x] T013 GET `/api/egress/capacity` : rapport + recommandations FR/EN.
- [x] T014 Endpoint séparé (pas de doublon dans `/api/vpn-status`) : le GUI
  interroge `/api/egress/capacity` et l'invalide avec le tag `vpn`.
- [x] T015 Tests API : schéma, 6+6, 3+3, warning `no_reserve`.
- [ ] T016 POST `/api/egress/capacity` (Phase 2) : persister `target_slots` et
  `reserve_policy` uniquement.

## Phase 5: GUI (US2/US3)

- [x] T017 Créer `VpnCapacity.svelte` : tableau providers, total, verdict,
  recommandation.
- [x] T018 Barre d'endpoints : segments par provider, largeur normalisée sur
  `target_slots`, marqueur de cible, déficit par provider.
- [x] T019 Intégrer dans `VpnStatus.svelte` dans le bloc Egress, après la
  liste des providers ; l'état reste porté par les requêtes `vpn`.
- [x] T020 i18n FR/EN : `capacity`, `configured`, `usable`, `reserve`,
  `deficit`, `insufficient`, `degraded`, `no_reserve`, `direct_single_ip`.
- [x] T021 Masquer la colonne Poids en Fallback ; l'afficher en Répartition.
- [x] T022 Vérifier `npm run check` (0 erreur/warning) et `npm run build`.

## Phase 6: Application contrôlée (US4 — P2)

- [ ] T023 Bouton « Appliquer le dimensionnement NordVPN » : appelle
  `_apply_station_count(target)` avec confirmation.
- [ ] T024 Activer/désactiver des endpoints WARP existants depuis la carte ;
  pas de création implicite.
- [ ] T025 Confirmation explicite pour toute création/suppression de
  conteneur ; audit dans les logs.
- [ ] T026 Brancher la santé réelle : `usable_station_count`, `parked_ips`,
  quarantaine 403, quota restant.

## Phase 7: Télémétrie (US5 — P3)

- [ ] T027 Compter les requêtes servies par provider (fenêtre glissante).
- [ ] T028 Afficher part observée vs attendue et charge/endpoint observée.
- [ ] T029 Alerte si un endpoint dépasse 2× la médiane ; proposer un
  reweight, jamais appliquer silencieusement.

## Dependencies & Execution Order

- Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5.
- Phase 6 après Phase 5 et après validation du conseiller.
- Phase 7 après Phase 6 (données réelles).
- T003–T008 sans dépendance Docker/réseau.
- T009–T012 dépendent de 008 (`egress_providers`, `egress_strategy`).
- T017–T022 dépendent de T013.

## MVP

1. T001–T015 (conseiller read-only + API GET).
2. T017–T022 (carte GUI).
3. Valider les scénarios 6+6, 6+3, 3+3, 4+2.
4. T016 et T023–T029 sont différés après validation du conseiller.

## Notes

- Aucun provider payant, aucun second compte, aucune attente.
- Les poids ne dimensionnent jamais les pools.
- Direct = 1 endpoint, jamais 6.
- Le mode Fallback ne consomme pas le quota du secondaire tant qu'il n'est
  pas appelé, mais les conteneurs doivent être chauds pour une bascule
  immédiate.
