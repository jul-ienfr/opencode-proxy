# Tasks: WARP primaire + replis d'egress free

**Input**: Design documents from `specs/007-warp-egress-fallback/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`,
`contracts/`, `quickstart.md`

**Tests**: inclus, car la spec exige un test automatisé ou une preuve live par
exigence (FR-022).

**Organization**: tâches groupées par user story, dans l'ordre de priorité.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallélisable (fichiers différents, pas de dépendance)
- **[Story]**: US1, US2, US3, US4, US5
- Chemins exacts dans chaque description.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: préparer la config, les fixtures et la documentation de preuve.

- [ ] T001 Ajouter les sections `ip_rotation.fallback` et `ip_rotation.egress_providers` avec valeurs par défaut désactivées dans `config.yaml`.
- [ ] T002 [P] Créer `docs/QUOTA_GRANULARITY_2026-11-07.md` (gabarit : baseline, A/B, `/64`, modèle, go/no-go).
- [ ] T003 [P] Créer les fixtures de test `tests/fixtures/egress_providers.py` (WARP, fallback, gateway, 403, 429).
- [ ] T004 [P] Ajouter la matrice `option → clé → consommateur → effet → test` dans `specs/007-warp-egress-fallback/contracts/fallback-policy.md`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: taxonomie d'erreurs, abstraction provider, champs endpoint,
persistance et dédup. Bloque toutes les user stories.

**⚠️ CRITICAL**: ne pas commencer US1–US5 avant cette phase.

- [ ] T005 Créer `free/errors.py` avec `GATE_403`, `QUOTA_429`, `RATE_LIMIT_429`, `TRANSIENT_5XX`, `ACCOUNT_OR_GLOBAL`, `AUTH`, et une fonction `classify_error(status, body, headers)`.
- [ ] T006 [P] Créer `free/egress_provider.py` avec le protocole `EgressProvider` et `EgressRotationResult` conformes à `contracts/egress-provider.md`.
- [ ] T007 [P] Étendre `Socks5Endpoint` dans `free/pool.py` avec `provider`, `block_key`, `quota_axis`, `reset_at`, en gardant `provider=warp` par défaut si absent.
- [ ] T008 Ajouter la migration `registry.db` (colonnes `provider`, `block_key`, `quota_axis`, `reset_at`) dans `rotation/infrastructure/db/migrate.py`, avec backup `VACUUM INTO` avant migration.
- [ ] T009 [P] Ajouter `EgressBlock` et `QuotaObservation` dans `free/quota_ledger.py` (ou un module voisin) ; `reset_at` vient uniquement du fournisseur.
- [ ] T010 [P] Ajouter le chargement/validation de `fallback.order`, `max_fallback_stations`, `probe_interval_s`, `recovery_successes`, `cooldown_s`, `max_block_s`, `fail_closed` dans `config/settings.py`.
- [ ] T011 Implémenter `measure_egress(endpoint)` dans `free/egress_provider.py` : famille IPv6/IPv4, normalisation `/64`/`/32`, rejet `socks5:*`.
- [ ] T012 [P] Écrire `tests/test_error_taxonomy.py` : 403, 429 quota, 429 rate-limit, 5xx, account/global.
- [ ] T013 [P] Écrire `tests/test_egress_provider.py` : famille, bloc, doublon, `current_ip` jamais stale, probe `False`.
- [ ] T014 [P] Écrire `tests/test_endpoint_provider.py` : compatibilité config sans `provider`, migration des valeurs, validation des champs.

**Checkpoint**: fondation prête ; US1–US5 peuvent démarrer.

---

## Phase 3: User Story 1 - WARP reste primaire malgré les blocs épuisés (Priority: P1) 🎯 MVP

**Goal**: parker ou tourner les blocs WARP épuisés sans utiliser une IP avant
`reset_at`, et sans casser WARP.

**Independent Test**: injecter un `429` sur une station, vérifier park/rotate,
absence d'utilisation avant reset, puis `200` sur un bloc frais.

### Tests for User Story 1

- [ ] T015 [P] [US1] Test park strict : `tests/test_quota_park.py` — aucune utilisation avant `reset_at`, probe après reset.
- [ ] T016 [P] [US1] Test rotation vs park : reset proche + autre bloc → park ; reset lointain + capacité manquante → rotate.
- [ ] T017 [P] [US1] Test doublon WARP : `tests/test_warp_rotation.py` — deux endpoints même `/64`, un seul servi.
- [ ] T018 [P] [US1] Test non-régression : un `429` quota ne bloque pas les autres modèles si le bucket est par modèle (selon Phase 0).

### Implementation for User Story 1

- [ ] T019 [US1] Implémenter la politique park/rotate dans `free/pool.py` (`rotate_exhausted_managed`, `_station_usable`) en lisant `reset_at`.
- [ ] T020 [US1] Ajouter le park dans `free/quota_ledger.py` : `parked`, `reset_at`, `next_probe_at`, `max_block_s=0` par défaut.
- [ ] T021 [US1] Brancher `WarpDockerProvider` sur `EgressProvider` dans `free/warp_rotation.py` sans changer le comportement existant.
- [ ] T022 [US1] Faire respecter le reset dans la sélection `free/pool.py` : aucune station `parked` avant `reset_at`, même si `selectable` est vrai.
- [ ] T023 [US1] Mettre à jour `dashboard/api.py` pour exposer `parked`, `reset_at`, `next_probe_at` (lecture seule).
- [ ] T024 [US1] Documenter la preuve live park/rotate dans `docs/evidence/007-warp-park.md`.

**Checkpoint**: US1 fonctionnelle et testable indépendamment.

---

## Phase 4: User Story 2 - Repli gratuit automatique puis retour à WARP (Priority: P1)

**Goal**: basculer 1–2 stations vers un repli gratuit ordonné, puis revenir à
WARP après deux succès.

**Independent Test**: WARP épuisé → fallback 10/10 `200` ; 2 probes WARP `200`
→ retour ; 403 → aucune bascule.

### Tests for User Story 2

- [ ] T025 [P] [US2] `tests/test_fallback_policy.py` : ordre, max 2 stations, déclencheurs, non-déclencheurs 403/429 court/5xx.
- [ ] T026 [P] [US2] `tests/test_fallback_return.py` : retour après 2 succès + cooldown, pas de flapping.
- [ ] T027 [P] [US2] `tests/test_fallback_fail_closed.py` : pool vide → `degraded`, aucun direct silencieux.

### Implementation for User Story 2

- [ ] T028 [US2] Créer `free/fallback.py` : `FallbackPolicy`, `evaluate()`, `select_provider()`, `should_return_to_warp()`, journalisation de la cause.
- [ ] T029 [US2] Créer `free/warp_alt_provider.py` : support MicroWARP/usque (HTTP/SOCKS5), même pool, santé et rotation.
- [ ] T030 [US2] Créer `free/gluetun_provider.py` : Proton Free (1 station), Windscribe/Privado en option, health/rotation via contrôle gluetun.
- [ ] T031 [US2] Brancher `fallback.py` dans `free/pool.py` (`_request_egress_rotation`, `rotate_exhausted_managed`) avec dispatch provider.
- [ ] T032 [US2] Ajouter les déclencheurs seuils dans `config.yaml` et le statut `fallback_active` dans `dashboard/api.py`.
- [ ] T033 [US2] Implémenter le drain avant bascule : aucune requête en vol interrompue (`free/pool.py`).
- [ ] T034 [US2] Ajouter le retour WARP après `recovery_successes` probes `200` et `cooldown_s` dans `free/fallback.py` et `free/pool.py`.
- [ ] T035 [US2] Documenter la preuve live fallback/retour dans `docs/evidence/007-fallback.md`.

**Checkpoint**: US1 + US2 fonctionnelles ; WARP majoritaire, fallback gratuit prouvé.

---

## Phase 5: User Story 3 - Valider la granularité réelle du quota (Priority: P2)

**Goal**: produire un go/no-go mesuré avant tout achat de capacité.

**Independent Test**: exécuter le protocole sur deux modèles et produire le
document avec preuves brutes.

### Implementation for User Story 3

- [ ] T036 [P] [US3] Créer `scripts/quota_granularity_probe.py` : baseline 403/429, A/B même `/64`, `/64` distinct, contrôle IPv4, deux modèles.
- [ ] T037 [US3] Ajouter le mode `--dry-run` et la journalisation JSON dans `docs/evidence/quota-granularity/`.
- [ ] T038 [US3] Exécuter le protocole sur `deepseek-v4.1-flash-free` et `mimo-v2.6-flash-free` ; consigner `Retry-After`, heure UTC, recovery réelle.
- [ ] T039 [US3] Rédiger `docs/QUOTA_GRANULARITY_2026-11-07.md` avec les résultats et la décision (adresse / `/64` / ASN / compte / modèle).
- [ ] T040 [US3] Vérifier la box/ISP (`/56`/`/48`), Oracle et Proton ; consigner la disponibilité et le coût dans `docs/QUOTA_GRANULARITY_2026-11-07.md`.
- [ ] T041 [US3] Mettre à jour `docs/REPOS_12_UNLIMITED_2026-11-06.md` si les mesures contredisent une conclusion.

**Checkpoint**: granularité connue ; le choix de capacité est documenté.

---

## Phase 6: User Story 4 - Voir et piloter provider, bloc et quota (Priority: P2)

**Goal**: rendre provider, bloc, reset, 429 et fallback visibles et pilotables.

**Independent Test**: configurer un fallback, provoquer une bascule, vérifier
l'affichage et l'effet réel du toggle.

### Tests for User Story 4

- [ ] T042 [P] [US4] `tests/test_vpn_status_provider.py` : `/api/vpn-status` expose provider, block, reset_at, last_429, fallback_active.
- [ ] T043 [P] [US4] `tests/test_gui_option_contract.py` : chaque option fallback a un consommateur runtime.

### Implementation for User Story 4

- [ ] T044 [US4] Étendre `dashboard/api.py` (`/api/vpn-status`, `/api/free-health`, `/api/free-egress/diag`) avec provider/bloc/reset/fallback.
- [ ] T045 [US4] Mettre à jour `dashboard-ui/src/pages/vpn/VpnStatus.svelte` : provider, bloc, dernier 429, reset, état fallback.
- [ ] T046 [US4] Mettre à jour `dashboard-ui/src/pages/vpn/VpnAdvanced.svelte` : ordre des replis, max stations, probes, cooldown, VPS opt-in.
- [ ] T047 [US4] Ajouter les métriques blocs distincts/épuisés/429/403/rotations/doublons dans `dashboard/api.py` et `dashboard-ui/src/pages/vpn/VpnStatus.svelte`.
- [ ] T048 [US4] Ajouter les boutons « tester » et « tourner » par endpoint dans `dashboard-ui/src/pages/vpn/VpnStatus.svelte`.

**Checkpoint**: visibilité complète ; chaque option a un effet réel.

---

## Phase 7: User Story 5 - Gateway IPv6 VPS optionnel (Priority: P3)

**Goal**: ajouter un gateway `/48` payant, opt-in, rotation < 10 s, sans
collision.

**Independent Test**: gateway actif, 3 rotations, 3 `/64` distincts, 3 × `200`,
persistance après redémarrage.

### Tests for User Story 5

- [ ] T049 [P] [US5] `tests/test_ipv6_gateway.py` : allocation, pas de chevauchement, pas de réseau `::`, rotation, release.
- [ ] T050 [P] [US5] `tests/test_ipv6_provider.py` : client API, famille, `/64`, gateway down → bad-mark.

### Implementation for User Story 5

- [ ] T051 [US5] Créer `scripts/ipv6_egress_gateway.py` : listener SOCKS5 asyncio, bind source, relay, drain.
- [ ] T052 [US5] Implémenter l'allocateur `/64` + SQLite dans `scripts/ipv6_egress_gateway.py`.
- [ ] T053 [US5] Implémenter l'API `/health`, `/allocate`, `/rotate`, `/release` avec token, conformément à `contracts/gateway-api.md`.
- [ ] T054 [US5] Créer `free/ipv6_provider.py` : client HTTP du gateway, `rotate`, `refresh_egress`, `health`.
- [ ] T055 [US5] Brancher `Ipv6GatewayProvider` dans `free/pool.py` et `free/egress_provider.py`.
- [ ] T056 [US5] Ajouter la config `egress_providers.vps_ipv6` (désactivée par défaut) dans `config.yaml`.
- [ ] T057 [US5] Écrire `docs/runbooks/ipv6-gateway.md` : setup VPS, `ip_nonlocal_bind`, route, systemd, nftables, SSH/WireGuard.
- [ ] T058 [US5] Soak 24 h et rollback WARP documentés dans `docs/evidence/007-vps-soak.md`.

**Checkpoint**: VPS optionnel prouvé ; WARP reste primaire.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [ ] T059 [P] Mettre à jour `spec.md`, `plan.md`, `contracts/` et `config.yaml` si une décision change.
- [ ] T060 [P] Compléter la matrice GUI (`option → clé → consommateur → effet → test`) pour 100 % des options dans `specs/007-warp-egress-fallback/contracts/fallback-policy.md`.
- [ ] T061 [P] Ajouter les métriques de soak et les preuves dans `docs/evidence/`.
- [ ] T062 Mettre à jour `debt.md` : aucune P0 ouverte, P1 planifiée, dette nouvelle listée.
- [ ] T063 `python -m pytest -k "not docker" -q` et `python -m ruff check .` : exit 0.
- [ ] T064 Exécuter `quickstart.md` de bout en bout et consigner les résultats.
- [ ] T065 Commit final et push ; mettre à jour `.specify/feature.json` si nécessaire.
- [ ] T066 [P] Test sécurité `tests/test_fallback_policy.py` : aucun proxy public, Tor ou VPN Gate ne peut être activé pour des credentials.
- [ ] T067 [P] Test axe compte/clé `tests/test_error_taxonomy.py` : aucun empilement de comptes par défaut ; un 429 sur bloc frais est classé `ACCOUNT_OR_GLOBAL`.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: aucune dépendance.
- **Foundational (Phase 2)**: dépend du Setup ; **bloque US1–US5**.
- **US1 (Phase 3)**: après Foundational.
- **US2 (Phase 4)**: après Foundational ; peut intégrer US1 mais reste testable seule.
- **US3 (Phase 5)**: après Foundational ; indépendante, à faire avant tout achat.
- **US4 (Phase 6)**: après US1/US2 pour afficher les bons états.
- **US5 (Phase 7)**: après Phase 0 go/no-go et US1/US2.
- **Polish (Phase 8)**: après les stories retenues.

### Within Each Story

- Tests avant implémentation.
- Modèles/entités avant services.
- Services avant intégration pool/dashboard.
- Preuve live avant de cocher la story.

### Parallel Opportunities

- T002–T004, T006–T007, T009–T010, T012–T014.
- US1 tests T015–T018 en parallèle.
- US2 tests T025–T027 en parallèle ; T029/T030 providers en parallèle.
- US3 T036/T037 puis T038–T041.
- US4 T042/T043 puis T044–T048.
- US5 T049/T050 puis T051–T058.

## Implementation Strategy

### MVP First (US1)

1. Phase 1 + Phase 2.
2. US1 (park/rotate WARP).
3. Valider `quickstart.md` §5.
4. Déployer.

### Incremental Delivery

1. US1 → WARP park/rotate stable.
2. US3 → Phase 0 go/no-go (avant fallback de capacité).
3. US2 → fallback gratuit (MicroWARP/usque, Proton).
4. US4 → visibilité/pilotage.
5. US5 → VPS optionnel si nécessaire.
6. Polish → soak, docs, dette.

### Parallel Team Strategy

- Dev A: US1 + US2 (pool/rotation/fallback).
- Dev B: US3 (Phase 0) + docs.
- Dev C: US4 (dashboard/API).
- Dev D: US5 (gateway VPS) après go/no-go.

## Notes

- [P] = fichiers différents, pas de dépendance.
- [US] relie la tâche à la user story.
- Chaque story doit être testable seule.
- Vérifier que les tests échouent avant implémentation.
- Commiter après chaque groupe logique.
- Ne pas activer le VPS avant le go/no-go Phase 0.
