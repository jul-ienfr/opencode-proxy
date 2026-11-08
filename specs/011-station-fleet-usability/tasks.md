# Tasks — 011 Station fleet usability

**Feature** : `specs/011-station-fleet-usability/`  
**Inputs** : `spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/station-lifecycle.md`, `quickstart.md`  
**Format** : `- [ ] TNNN [P] [USn] Description avec chemin exact`

## Phase 1: Setup

- [ ] T001 Créer `specs/011-station-fleet-usability/` (spec, plan, research, data model, contrat, quickstart, checklist) et vérifier la cohérence avec 005/007/010.
- [ ] T002 Sauvegarder `logs/registry.db`, `.env`, `config.yaml` avant toute modification (`logs/backups/`).
- [ ] T003 Ajouter les flags `station_lifecycle.mode` (`shadow|canary|enforce`) et `station_lifecycle.canary` dans `config.yaml`.
- [ ] T004 Ajouter `station_lifecycle.max_restarts_per_hour`, `max_recreates_per_hour`, `max_rotations_per_hour`, `evidence_ttl_s` dans `config.yaml`.
- [ ] T005 Documenter le vocabulaire station/slot/endpoint/bucket/session/lease dans `docs/runbooks/station-lifecycle.md`.

## Phase 2: Foundational (bloquant)

- [x] T006 Créer `ops/desired_state.py` : `DesiredStationSpec`, `DesiredEndpointSpec`, validation provider/stack/transport/port/generation.
- [x] T007 [P] Créer `tests/test_desired_state.py` : validation, génération monotone, hash, refus des conflits provider/station.
- [ ] T008 Ajouter les tables `desired_state` et `rotation_jobs` + colonnes `provider`/`generation` dans `rotation/infrastructure/db/schema.sql`.
- [ ] T009 Implémenter la migration versionnée et le backfill dans `rotation/infrastructure/db/migrate.py` (config/.env/socks5_proxies → desired state).
- [ ] T010 [P] Créer `tests/test_registry_migration_011.py` : dry-run, apply, idempotence, rollback sur backup.
- [x] T011 Créer `ops/reconciler.py` : diff desired/actual, décisions `create/recreate/start/stop/noop`, aucune suppression sans preuve d'obsolescence.
- [x] T012 [P] Créer `tests/test_reconciler.py` : table de décisions, conteneur conforme, stack divergent, génération obsolète, orphelin, port occupé, Docker down.
- [ ] T013 Brancher `ops/reconciler.py` derrière `station_lifecycle.mode=shadow` dans `opencode.py` (aucune action réelle en shadow).
- [x] T014 Corriger `vpn/manager.py:11190-11324` : le reconcile ne supprime plus un conteneur conforme au desired state ; une divergence déclenche un seul recreate planifié.
- [ ] T015 Corriger `vpn/manager.py:5394-5485` : `_compose_env` lit la même génération/stack que le desired state, sans override mémoire divergent.
- [ ] T016 [P] Créer `tests/test_reconcile_non_destructive.py` : 100 cycles, 0 `rm -f` injustifié, 1 recreate sur divergence.
- [ ] T017 Ajouter `/healthz` et le mode dégradé explicite dans `server/` (proxy up même si une station est down).
- [ ] T018 [P] Créer `tests/test_healthz_degraded.py` : station down, proxy up, verdict dégradé, pas de faux 6/6.

## Phase 3: US1 — Voir la vérité par station (P1)

- [ ] T019 Étendre `free/station_health.py` avec les états `created/starting/tunnel_up/dns_ok/egress_ok/quota_ok/client_usable/degraded/parked/failed`.
- [ ] T020 Ajouter `last_evidence`, `evidence_ttl_s`, `client_usable` dans `station_state` (`rotation/infrastructure/db/schema.sql`).
- [ ] T021 Implémenter la preuve client (contenu + hash + taille) dans `free/station_health.py` et `free/probe.py`.
- [ ] T022 [P] Créer `tests/test_station_lifecycle.py` : transitions, preuve obligatoire, TTL, `connected` ≠ `usable`.
- [x] T023 Créer `GET /api/stations` dans `dashboard/api.py` (lecture registre, pas mémoire).
- [x] T024 Créer `GET /api/stations/{provider}/{station}` et `/evidence` dans `dashboard/api.py`.
- [ ] T025 [P] Créer `tests/test_api_stations.py` : schéma, 12 stations, 401 sans token, 404 inconnu.
- [ ] T026 Créer `dashboard-ui/src/pages/vpn/VpnStations.svelte` : tableau par station, état, IP, bucket, quota, preuve, prochaine action.
- [ ] T027 Ajouter i18n FR/EN dans `dashboard-ui/src/lib/locales.ts`.
- [ ] T028 [P] Vérifier `npm run check` et `npm run build` dans `dashboard-ui/`.

## Phase 4: US2 — Créer exactement la flotte (P1)

- [x] T029 Créer `free/warp_provisioner.py` : `plan()` / `apply()` create/update/delete, noms, ports, image, `ipv6`, `managed`, `provider`, `block`, `generation`, options (`-e`, limites, labels, devices).
- [x] T030 Ajouter le préflight ports/noms/image et l'unicité `/64` dans `free/warp_provisioner.py`.
- [x] T031 Ajouter le rollback sur échec de `docker run`, le bind `127.0.0.1` et la réapplication des options dans `free/warp_provisioner.py`.
- [x] T032 [P] Créer `tests/test_warp_provisioner.py` : plan vide, création, doublon `/64`, port occupé, rollback, 409.
- [ ] T033 Rendre `_ensure_container` idempotent par génération dans `vpn/manager.py` (pas de recréation si conforme).
- [ ] T034 Ajouter le préflight des ports et le cleanup des conteneurs partiels dans `vpn/manager.py`.
- [ ] T035 [P] Créer `tests/test_station_creation_idempotent.py` : 2 apply = 1 création, port occupé propre, station 2 recréée.
- [ ] T036 Créer `scripts/station_migrate_011.py` : adopter les 12 stations existantes, créer les manquantes, ne rien supprimer sans confirmation.
- [ ] T037 [P] Créer `tests/test_station_migrate_011.py` : 5+6 → 6+6, labels conservés, 0 doublon.

## Phase 5: US3 — Maintenir et réparer sans boucle (P1)

- [ ] T038 Classifier les causes `auth/server_dead/dns/transport/quota_429/healthcheck/proxy_down` dans `station_repair.py`.
- [ ] T039 Associer une action par cause (`restart/recreate/repin/rotate/park/wait`) dans `station_repair.py`.
- [ ] T040 Rendre le superviseur seul propriétaire dans `scripts/fresh_chain_supervisor.py` ; le watchdog `vpn/manager.py` devient observateur.
- [x] T041 Rendre la maintenance indépendante de `egress_providers` dans `scripts/fresh_chain_supervisor.py:1236-1241`.
- [ ] T042 Activer `station_repair.enabled: true`, `repair_owner: supervisor`, budgets cohérents dans `config.yaml`.
- [ ] T043 Implémenter le budget persisté par station/cause dans `rotation/infrastructure/db/repositories.py` et `station_repair.py`.
- [ ] T044 [P] Créer `tests/test_station_repair_causes.py` : 7 causes → 7 actions, une seule exécution, park après échecs.
- [ ] T045 [P] Créer `tests/test_single_repair_owner.py` : watchdog ne recrée pas, superviseur agit, pas de double job.
- [ ] T046 Ajouter le re-pin automatique sur host mort (DNS/TTL) dans `vpn/manager.py` et `free/ip_registry/`.
- [ ] T047 [P] Créer `tests/test_repin_dead_host.py` : `us10506` mort → host vivant, pas de restart infini.
- [ ] T048 Ajouter le mode `parked` avec `parked_until`/`cause` et la reprise après `reset_at` dans `free/station_health.py`.

## Phase 6: US4 — Tourner sans perdre de slot (P1)

- [x] T049 Persister `LAST_ROTATION` et le budget horaire dans `registry.db` et `free/warp_rotation.py` ; une seule recréation par rotation.
- [x] T050 Faire lire `station_max_rotations_per_hour` par `WarpRotator` et refuser une rotation au-delà du budget ; backoff d'échec.
- [x] T051 Créer `free/egress_reconcile.py` : refresh périodique live → `egress_ip`/`block`/ledger, détection de dérive.
- [x] T052 Brancher la réconciliation WARP sur événement Docker et sur timer dans `free/pool.py`.
- [x] T053 Renforcer l'unicité `/64` à la création et au refresh dans `free/warp_provisioner.py` et `free/warp_rotation.py`.
- [ ] T054 Écrire `rotation_jobs` de façon transactionnelle dans `free/warp_rotation.py` et `vpn/manager.py`.
- [ ] T055 [P] Créer `tests/test_warp_rotation_budget.py` : budget, restart, backoff, refus, persistance.
- [ ] T056 [P] Créer `tests/test_egress_reconcile.py` : IP changée hors rotation, config périmée, doublon détecté, ledger corrigé.
- [ ] T057 Implémenter la rotation préventive quota/latence/âge dans `free/rotation.py` avec seuils `config.yaml`.
- [ ] T058 [P] Créer `tests/test_preventive_rotation.py` : seuils, pas de rotation silencieuse, slot préservé.
- [ ] T059 Ajouter le respect strict de `reset_at` et le refus de probe avant reset dans `free/quota_ledger.py` et `free/pool.py`.
- [ ] T060 [P] Créer `tests/test_reset_barrier.py` : 429 avec RA, pas de probe avant, reprise après reset, 0 réutilisation.

## Phase 7: US5 — Capacité sur preuves (P2)

- [x] T061 Étendre `free/capacity.py` : `unique_buckets`, `client_usable`, `deficit`, verdict basé sur les buckets.
- [x] T062 Étendre `dashboard/api.py:1549-1610` et `free/pool.py:4198-4220` pour compter les buckets uniques.
- [x] T063 [P] Mettre à jour `tests/test_capacity.py` : 6 endpoints/5 blocs → `unique_buckets=5`, `degraded`; 6/6 → `ok`.
- [ ] T064 [P] Créer `tests/test_capacity_api_011.py` : GET/POST, warning `duplicate_block`, `client_usable` par provider.
- [ ] T065 Mettre à jour `dashboard-ui/src/pages/vpn/VpnCapacity.svelte` : buckets uniques, déficit, avertissement doublon.
- [x] T066 Créer `scripts/station_audit.py` : audit CLI JSON, `--fail-on`, `--provider`, `--fix --dry-run`.
- [x] T067 [P] Créer `tests/test_station_audit.py` : 12/12, 11 buckets, exit codes 0/1/2/3.
- [ ] T068 Ajouter l'alerte `duplicate_block` et `unique_buckets` dans `dashboard/api.py` et `free/capacity.py`.

## Phase 8: US6 — Sécurité (P3)

- [x] T069 Forcer le bind `127.0.0.1` des endpoints WARP dans `free/warp_provisioner.py` et `free/warp_rotation.py`.
- [ ] T070 Exiger `DASHBOARD_TOKEN` si `server.host != 127.0.0.1` dans `config/settings.py` et `server/`.
- [ ] T071 Épingler l'image WARP par digest dans `config.yaml` et `free/warp_provisioner.py`.
- [ ] T072 [P] Créer `tests/test_security_bind.py` et `tests/test_dashboard_token.py`.
- [ ] T073 Auditer les logs de secrets (`credentials.env`, `vpn_configs/wireguard.env`, `api_keys.json`) et ajouter un test de non-fuite.

## Phase 9: Polish et validation

- [x] T074 Mettre à jour `docs/runbooks/station-lifecycle.md` : création, maintenance, rotation, incident, rollback.
- [ ] T075 Mettre à jour `docs/AUDIT_STATIONS_NORDVPN_WARP_2026-11-07.md` avec les preuves après correction.
- [ ] T076 Produire `docs/evidence/011-fleet-usability.md` : 12/12, 12 buckets, 24 h, MTTR, 0 doublon, 0 `rm -f`.
- [ ] T077 [P] Exécuter le chaos : tunnel tué, host mort, quota, stack divergent, Docker down, proxy down.
- [ ] T078 [P] Exécuter le soak 24 h avec `scripts/station_slo_watch.py --duration 86400 --interval 30`.
- [ ] T079 Mettre à jour `.specify/feature.json`, `CHANGELOG.md` et les statuts des specs 001/002/005/007/010.
- [ ] T080 Faire une revue de cohérence `spec.md` ↔ `plan.md` ↔ `tasks.md` et fermer les tâches obsolètes.

## Phase 10: NordVPN hardening complémentaire (P1/P2)

- [ ] T081 [US3] Un propriétaire de démarrage explicite : `start()` connecte les stations désirées même si `repair_owner=supervisor` (`vpn/manager.py:2093-2147`, `opencode.py:3431-3433`).
- [x] T082 [US3] Un seul writer `.env` atomique avec lock inter-processus et tmp unique (`vpn/manager.py`, `scripts/fresh_chain_supervisor.py`, `scripts/fresh_chain.py`).
- [ ] T083 [US3] Préflight ports/compose et rollback complet de l'upscale en échec (`opencode.py:2370-2712`).
- [ ] T084 [US3] Remplacer `depends_on: service_healthy` de la station 1 par `service_started` + healthcheck applicatif (`docker-compose.yml:181-183`).
- [ ] T085 [US3] Faire lire toutes les clés `station_repair` par le propriétaire et rendre `repair_owner` hot-reloadable (`station_repair.py`, `vpn/manager.py:4611-4950`).
- [ ] T086 [US4] Faire passer `/api/vpn/next` et `pin_country` par le superviseur, avec verrou par station (`dashboard/api.py`, `free/pool.py`).
- [ ] T087 [US6] Déplacer le secret de control hors de `.env` et exiger le token sur `/api/vpn/diagnostic` (`docker-compose.yml`, `dashboard/api.py:5801-5876`).
- [ ] T088 [US6] Rafraîchir `vpn_configs/wireguard.env` dans `scripts/refresh_nord_credentials.py` et redémarrer les stations WG de façon contrôlée.
- [ ] T089 [US6] Aligner l'updater NordVPN sur l'image digest-pinnée (`vpn/manager.py:9308-9493`, `ops/updater.py`).
- [ ] T090 [US1] Ne plus compter les `disabled_stations` dans `configured_stations` (`free/pool.py:6910-6923`).
- [ ] T091 [US2] Purger les pins `SERVER_HOSTNAMES_STATIONn`/`SERVER_COUNTRIES_STATIONn` au-delà du `station_count` (`.env`, `vpn/manager.py:7638-7698`).
- [ ] T092 [US5] Exposer `current_host` comme serveur et garder le conteneur séparé (`vpn/manager.py:2320-2323, 2579-2582`).
- [ ] T093 [US3] Lire `server.host`/`server.port` dans le superviseur et les chaînes au lieu de `127.0.0.1:4000` (`scripts/fresh_chain_supervisor.py:372-383`, `scripts/fresh_chain.py:2539`).
- [ ] T094 [US4] Aligner la séquence de backoff auth sur les valeurs configurées (`station_repair.py:150-157`).
- [ ] T095 [US2] Documenter et sécuriser le downscale tail-only / slot désactivé (`opencode.py:2639`).
- [ ] T096 [P] Créer `tests/test_nordvpn_hardening.py` : G1, G2, G7, G18, G19, G4, G9, G10, G11, G6.
- [x] T113 [US1] Dériver `usable_stations`, `unique_usable_ips`, `active_stations_with_quota` et `capacity_degraded` du même ensemble `client_usable` que `station_health` (`free/pool.py`, `dashboard/api.py`).
- [x] T114 [P] Ajouter le test de contrat anti-faux-6/6 : `usable_stations == count(station_health usable)` et pas de 6/6 avec `boot_error` 0/6 (`tests/test_plan_011_live.py`, `tests/test_api_stations.py`).

## Phase 11: Consolidation inter-providers (US7)

- [x] T097 [US7] Créer `ops/lifecycle.py` : `SlotSpec`, `SlotObservation`, `SlotState`, `LifecycleEngine` (reconcile/repair/rotate).
- [x] T098 [US7] Créer `ops/lifecycle_policy.py` : santé, réparation, rotation, budgets, quota partagés.
- [x] T099 [US7] Créer `ops/lifecycle_evidence.py` : preuve, incident, log, `SlotStatus` commun.
- [x] T100 [US7] Créer `ops/providers/base.py` : `SlotProvider` Protocol (`specs/observe/create/recreate/delete/rotate/probe/persist`).
- [x] T101 [US7] Créer `ops/providers/nordvpn.py` : adapter Gluetun/Compose/auth/session, sans dupliquer la policy.
- [x] T102 [US7] Créer `ops/providers/warp.py` : adapter `WarpProvisioner`/`WarpRotator`/SOCKS, sans dupliquer la policy.
- [x] T103 [US7] Remplacer `block_for_ip` et `block_for_egress` par `quota_block()` dans `free/quota_ledger.py` ; mettre à jour les appelants.
- [x] T104 [US7] Remplacer les deux résolveurs d'egress par `free/egress_probe.py` ; mettre à jour `free/pool.py` et `free/warp_rotation.py`.
- [ ] T105 [US7] Appliquer `RepairPolicy` aux deux providers via les adapters ; supprimer les branches ad hoc.
- [ ] T106 [US7] Appliquer `RotationBudget` commun aux deux providers ; persister par slot.
- [ ] T107 [US7] Unifier `SlotStatus` dans `dashboard/api.py`, `free/pool.py` et `vpn/manager.py` ; `provider_details` pour le spécifique.
- [ ] T108 [US7] Unifier les logs/preuves dans `ops/lifecycle_evidence.py` et rattacher `free.pool`, `free.warp_rotation`, `station_repair`.
- [ ] T109 [P] Créer `tests/test_lifecycle_contract.py` : moteur commun, mêmes décisions pour les deux providers.
- [ ] T110 [P] Créer `tests/test_provider_parity.py` : même panne → même état/cause/action/preuve.
- [ ] T111 [P] Créer `tests/test_consolidation_no_duplicates.py` : une seule fonction de bloc, un seul résolveur, un seul statut, un seul budget.
- [ ] T112 [US7] Mettre à jour `specs/011.../spec.md`, `plan.md`, `data-model.md` et `docs/runbooks/station-lifecycle.md` avec la carte de consolidation.
- [x] T115 [P] [US7] Écrire les tests de baseline et de consolidation `tests/test_plan_011_baseline.py` et `tests/test_plan_011_consolidation.py` ; résultat : 6 passed, 22 xfailed.
- [x] T116 [P] [US7] Écrire les preuves live `tests/test_plan_011_live.py` ; résultat : 1 passed, 6 xfailed (dont faux 6/6).
- [x] T117 [US7] Mettre à jour `plan.md` avec le test baseline, la consolidation et les résultats ; ajouter US7/FR-043–FR-053/SC-017–SC-021.

## Dependencies & Execution Order

- Phase 1 → Phase 2 (T006–T018) : bloquant.
- T014–T016 dépendent de T011/T012.
- Phase 3 (T019–T028) dépend de T008/T020.
- Phase 4 (T029–T037) dépend de T006/T011.
- Phase 5 (T038–T048) dépend de T019/T020 et T040.
- Phase 6 (T049–T060) dépend de T021/T051.
- Phase 7 (T061–T068) dépend de T019/T051.
- Phase 8 (T069–T073) dépend de T029.
- Phase 9 (T074–T080) dépend de toutes les phases.
- Phase 10 (T081–T096) dépend de Phase 2 et peut être parallélisée avec US1–US6.
- Phase 11 (T097–T112) dépend de T006–T013 (desired state/reconciler) et peut démarrer avant les adapters de provisioning.

## Parallel Opportunities

- T007, T010, T012, T016, T018 : tests purs.
- T024/T025 et T026/T027 : API et GUI en parallèle après T023.
- T032/T035/T037 : provisioning et migration.
- T044/T045/T047 : réparation.
- T055/T056/T058/T060 : rotation et quota.
- T063/T064/T067 : capacité et audit.
- T072/T073 : sécurité.

## Implementation Strategy

1. **P0 d'abord** : T014–T018, T040–T042. Sans cela, toute autre tâche est
   annulée par la boucle de reconcile.
2. **MVP US1/US2** : T006–T037. Le système voit la vérité et crée exactement
   6+6.
3. **US3/US4** : T038–T060. La flotte se répare et tourne sans perdre de slot.
4. **US5/US6** : T061–T073. La capacité et la sécurité sont prouvées.
5. **Validation** : T074–T080. Aucune phase ne se ferme sans preuve live.

## MVP

- 6 NordVPN + 6 WARP créés et visibles.
- 12 buckets uniques.
- 0 `rm -f` injustifié.
- 1 rotation WARP et 1 rotation NordVPN sans perte de slot.
- Proxy `:4000` + `/healthz`.
- Rapport `station_audit.py --json` avec `verdict=ok`.
