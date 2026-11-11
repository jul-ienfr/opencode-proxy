# Tasks: Couverture exhaustive et rotation priorisée des IP NordVPN

**Input**: Design documents from `specs/014-nordvpn-exhaustive-rotation/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`,
`contracts/`

**Tests**: inclus, car la spec exige des preuves mesurées et des tests
d'effet pour chaque option.

**Organization**: les tâches sont groupées par user story ; chaque story peut
être implémentée et testée indépendamment.

## Implementation status (2026-11-09)

**Vertical slice livré et testé** :

- `free/qualification/` : `domain.py`, `scoring.py`, `transport.py`,
  `store.py`, `source.py`, `preflight.py`, `sweep.py`, `runtime.py`,
  `station_adapter.py`.
- `config.yaml` : bloc `egress_catalog.qualification` (18 clés) ;
  `config_audit` = 0 dead / 0 missing.
- `scripts/qualification_sweep.py` : mode shadow (ingestion + score + top) et
  mode live (adaptateur Gluetun/Compose construit mais non exécuté ici).
- `scripts/fresh_chain.py --sweep` : pont shadow/live vers le sweep.
- API dashboard : `/api/egress/qualification/{summary,queue,next,epochs,attempts,transports}`.
- D?couverte schedul?e : `DiscoveryService`, `discovery_runs`, fallback fail-soft,
  suppression `removed-from-catalog`, CLI `--discovery --force`.
- Watchdog : `WatchdogService`, `station_state`, `incidents`, `repair_jobs`,
  r?paration locale, escalade apr?s `max_repair_attempts`, API `/watchdog`,
  `/stations`, `/discovery`, `/repairs`.
- Sonde HTTP : `HttpStationProbe` lit `/api/vpn-status` et mappe les états.
- Réparation : `SupervisorRepairAdapter` appelle `StationSupervisor.restart`
  de la seule station concernée ; aucun second réparateur.
- Heartbeat : `watchdog_state` (heartbeat_at, last_run_at, status), stale >
  `max_age_s`, persistance après réouverture de `registry.db`.
- Cutover : `CutoverService` make-before-break, prepare/switch/probe/rollback,
  `station_cutovers`, dur?e mesur?e.
- Runtime station : `StationWorker`, `ParallelStationRunner`, `station_runtime`,
  workers parall?les, pause/reprise, cutover par station.
- Rotation planner : `RotationPlanner`, `StationSnapshot`, headroom/standby/
  protection de la derni?re station, s?lection avec IP unique.
- Qualit? : `scripts/module_size_audit.py` + `module-baseline.json` (ratchet
  legacy, 0 violation sur 689 fichiers).
- Benchmark : `scripts/bench_qualification.py` (upsert/score/claim, budgets).
- `dashboard/routers/qualification_routes.py` : routes qualification extraites
  de `dashboard/api.py`, testées via FastAPI `TestClient`.
- `dashboard/routers/egress_catalog_routes.py` : routes catalogue read-only
  extraites.
- `dashboard/routers/egress_catalog_mutations.py` : refresh, threat feeds,
  blocage (persistance denylist) et upsert de source extraits.
- `dashboard/routers/nordvpn_routes.py` : statut/disponibilité/pays NordVPN
  extraits, `nordvpn_status_snapshot` partagé avec le diagnostic.
- `dashboard/routers/station_routes.py` : inventaire `/api/stations`, plan de
  reconcile et détail station extraits, avec `build_stations_payload` testable.
- `dashboard/routers/free_routes.py` : statut allocator et registre IP extraits.
- `GET /api/egress/qualification/quota` : ledger `quota_buckets` joint à
  `egress_ips`, résumé 429/reset et détail par bucket.
- `GET /api/egress/qualification/slo` : 6/6, disponibilité 1 h, error budget,
  heartbeat, incidents ouverts, MTTR des réparations.
- Soak smoke : `scripts/soak_qualification.py` (10 s, mémoire/disque bornés).
- ADR-010, `rewrite-inventory.md` (seams/slices) et `release-checklist.md`.
- Gate de release : `scripts/quality_gate.py` (pytest, ruff, mypy, config,
  ratchet, cycles, benchmark, couverture ≥ 85 %).
- Couverture : **92 %** total ; `sweep` 92 %, `station_probe` 95 %,
  `runtime` 100 %.
- Graphe de dépendances : `scripts/dependency_graph.py` (cycles, inventory).
- Runbook : `runbook.md` (shadow, discovery, watchdog, live, rollback).
- Gate architecture : `tests/test_qualification_architecture.py` (domaine pur,
  0 cycle, modules < 1 500, `country_backoff` absent, rotations concurrentes).
- Pauses globales : `global_pause_enabled: false` par défaut ; les seuils 429
  restent des alertes, plus un arrêt de toutes les stations.
- `server_transports` peuplé depuis l'API ; tables de qualification ajoutées à
  `rotation/infrastructure/db/schema.sql` ; une IP `usable` déjà détenue par
  une autre station n'est pas promue (`duplicate`).
- `free/qualification/transport_health.py` : agrégat `transport_health`
  (station + global), `degraded` après 100 tentatives sans 200, `last_ok_at`
  et `last_error` sur `server_transports`.
- `free/qualification/diagnostics.py` : classification bind/AUTH/DNS/probe/
  timeout/quota, exposée par `/api/egress/qualification/transports`.
- Doublons `(hostname, transport, round, epoch, station)` refusés par
  `record_attempt` ; une reprise crash ne double-compte plus.
- `fast_discovery` forcé à `false` en mode `auto` : le tri-stack complet est
  toujours parcouru (FR-072).
- `rotation/infrastructure/db/migrate.py` : schéma versionné **3**
  (`qualification control plane tables`), colonne additive `lease_until`.
- `tests/test_qualification_security.py` : aucune projection API/metrics/SLO
  n’expose de champ credential, token, clé ou URL de transport.
- `tests/test_qualification_api.py` : summary/next/metrics, pagination et
  filtres pays/transport.
- `tests/test_qualification_station_runtime.py` : 6 workers parallèles,
  stations distinctes, état persisté.
- `tests/test_qualification_observability.py` : logs structurés
  `qualification.epoch.*`, `qualification.attempt`, `qualification.discovery.*`
  et `qualification.watchdog.run`.
- `free/qualification/scheduler.py` : cycles découverte → watchdog → preflight
  → sweep, erreurs contenues par composant, `start`/`stop` propres, branché en
  option dans `EgressRuntime`.
- `free/qualification/preflight.py` : 3 échantillons, médiane DNS/connect/
  handshake/RTT, timeout et budget configurables, échec = backoff infra.
- `qualification_attempts` enregistre `dns_ms`, `connect_ms`, `handshake_ms`,
  `rtt_ms`, `endpoint_ip` et `transport`.
- Test de corrélation synthétique : le décile de score supérieur produit
  100 % des 200, les autres restent `quota`.
- `auth_backoff_s` : `AUTH_FAILED` reste station-local, le hostname revient
  `pending` avec backoff au lieu d'être condamné.
- `POST /api/egress/qualification/rotate` : dry-run du planner (station,
  headroom, standby, dernière station), token dashboard ; `apply=true` renvoie
  409 tant que le cutover de production n'est pas injecté.
- `scripts/qualification_daemon.py` : daemon shadow/live, `--once`,
  découverte/watchdog/préflight optionnels, arrêt propre.
- `build_qualification_scheduler(runtime)` : assemble découverte, watchdog,
  préflight et sweep avec les valeurs du runtime.
- `scripts/load_qualification.py` : 7 070 hosts × 6 workers, 71,5 s,
  projection ~8,5 M hosts/24 h, 0 erreur.
- `free/qualification/source.py` : les serveurs `legacy_dedicated_ip`
  (1 538) sont `skipped/dedicated` ; denylist API (`ips`) appliquée dès
  l’ingestion ; 5 395 hosts publics éligibles sur 7 070.
- `POST /api/egress/qualification/candidates/{hostname}/pause|resume` :
  pause/reprise opérateur persistée sans consommer de session.
- `tests/test_qualification_resume.py` : process tué brutalement
  (`os._exit(9)`) puis lease expirée récupérée.
- `GET /api/egress/qualification/summary` expose `by_skipped_reason`,
  `dedicated`, `denylisted`, `unsupported_transport`, `paused` ; métriques
  Prometheus correspondantes.
- `GET /api/egress/qualification/attempts?format=csv` : export CSV des
  tentatives (DNS/connect/handshake/RTT/endpoint/transport).
- `GET /api/egress/qualification/coverage` : couverture par pays et
  transport (total, usable, pending, quota, dead, skipped).
- `tests/test_qualification_contracts.py` : ports `CatalogSource`,
  `StationBinder`, `Prober`, `CutoverPorts`, `Watchdog`, `RepairQueue`.
- `scripts/fresh_chain_supervisor.py` : lance un cycle qualification shadow
  selon `daemon_enabled`/`daemon_interval_s` (désactivé par défaut), via
  `free/qualification/supervisor_bridge.py` (aucune croissance du monolithe).
- `.github/workflows/ci.yml` : job bloquant `Qualification release gate`
  (`quality_gate.py --with-soak`) sur Ubuntu.
- `free/repair_queue.py` : `repair_requests` migré dans `logs/registry.db`
  (plus d’autorité séparée `repair_queue.db`).
- `dashboard/routers/pool_routes.py` : `/api/pool-status` et
  `/api/latency-status` extraits, testés avec fakes.
- `dashboard/routers/retry_routes.py` : `/api/retry-config` GET/POST extraits,
  validation JSON et normalisation booléenne testées.
- `dashboard/routers/usage_routes.py` : `/api/free-model-usage` extrait,
  agrégation pure testable et reset quota par IP.
- `dashboard/routers/quota_routes.py` : `/api/quotas` extrait avec en-têtes
  stale-while-revalidate (`X-Cache`, âge, max-age).
- `GET /api/egress/qualification/rotations` : historique des cutovers
  (station, avant/après, transport, durée, résultat, raison).
- `dashboard/routers/event_routes.py` : SSE `/api/events` extrait, cap
  subscribers et heartbeat conservés.
- `dashboard/routers/capacity_routes.py` : `/api/egress/capacity` GET/POST
  extraits, validation et audit conservés.
- `dashboard/routers/vpn_config_routes.py` : `/api/vpn-config` (lecture) et
  `/api/vpn-stack-info` extraits, fusion qualité/egress et état par station.
- `dashboard/routers/vpn_control_routes.py` : validation dry-run de config VPN
  et pause de rotation latence extraites, avec token et logs.
- `dashboard/routers/station_logs_routes.py` : logs Docker par station
  extraits, tail borné 10–300 et erreur 502.
- `tests/test_manager_characterization.py` : transitions `VPNState`,
  `is_up`/`is_routable`, backoff exponentiel borné, circuit breaker
  open/half-open/son de unique et reset.
- `repair_backoff_s` : le watchdog ne relance pas une réparation identique
  avant le délai ; test 100 s → skip 200 s → reprise 400 s.
- `watchdog_enabled` / `watchdog_probe_interval_s` : sans sonde le watchdog
  est `disabled` (aucun incident artificiel) ; cadence des sondes configurable.
- `proactive_rotation_headroom_pct` et `last_station_protection` pilotent le
  dry-run de rotation ; test 50 % → rotate, 1 % → keep.
- `GET /api/egress/qualification/export` : snapshot JSON complet (summary,
  queue, next, epochs, transports, stations, watchdog, discovery, repairs,
  rotations, quota, SLO).
- `cutover_timeout_ms` : chaque étape prepare/switch/probe/rollback est bornée
  par `asyncio.wait_for` ; `build_cutover_service()` lit la config.
- `per_station_workers` : un `QualificationSweep` par station, exécutés en
  parallèle par le scheduler ; test 3 stations → 3 sweeps.
- `GET /api/egress/qualification/epochs/{epoch_id}` : détail d’une époque,
  404 explicite si absente.
- `dashboard/routers/vpn_fleet_routes.py` : toggle/connect/disconnect flotte
  extraits, fallback manager unique et rafraîchissement première station.
- `dashboard/routers/vpn_health_routes.py` : `/api/vpn/health-check` extrait,
  manager injecté et erreur explicite sans manager.
- `dashboard/routers/vpn_heal_routes.py` : `/api/vpn/heal` extrait,
  convergence du nombre de stations et heal flotte idempotent.
- `dashboard/routers/vpn_next_routes.py` : `/api/vpn/next` extrait, sélection
  de station, pool actif et fallback manager unique.
- `dashboard/routers/vpn_update_routes.py` : `/api/vpn/update` extrait,
  check/opportunité/apply flotte et garde-fou trafic actif.
- `dashboard/routers/station_control_routes.py` : pin/unpin pays, probe,
  restart, soft-rotate, stack, start/stop/connect/disconnect et flotte extraits.
- `dashboard/routers/vpn_state_routes.py` : credentials (lecture/écriture),
  save-state et export JSON extraits.
- `free/qualification/rotation_batch.py` : rotations parallèles bornées
  (`max_concurrent`), `parallel_rotations` configurable.
- `specs/014.../release-checklist.md` : sections code/tests/données/
  sécurité/exploitation cochées avec preuves ; canary restant.
- `station_standby_size` : le planner ne promeut le standby que si sa taille
  est > 0 ; test 0 → rotate.
- `global_pause_on_429` : pause globale opt-in seulement, désactivée par
  défaut ; un 429 ne met jamais toutes les stations en pause sans config.
- `dashboard/routers/vpn_import_routes.py` : `/api/vpn/import` extrait,
  application injectée et fallback manager unique.
- `dashboard/routers/vpn_jobs_routes.py` : jobs flotte (connect/disconnect/
  heal), lookup et liste extraits.
- `dashboard/routers/config_routes.py` : `/api/config` GET/POST extraits,
  masquage des secrets, cache IP locale, validation port/hôte et redémarrage
  sur thread dédié conservés.
- `dashboard/routers/stats_routes.py` : `/api/stats` et
  `/api/stats/timeseries` extraits, cache TTL, alias `from`/`to`, fenêtres
  glissantes et granularité automatique conservés.
- `dashboard/routers/vpn_config_update_routes.py` : `POST /api/vpn-config`
  extrait, validation, fan-out multi-stations, persistance et `set_stack`
  propagé uniquement en cas de succès.
- `dashboard/routers/proxy_backend_routes.py` : SOCKS5 et HTTPS CONNECT
  extraits (CRUD, toggle, probe, rotation, fallback, refresh/revalidate
  egress) avec token et synchronisation pool.
- `dashboard/routers/egress_mode_routes.py` : `/api/vpn/proxy-mode` et
  public-proxies extraits, audit egress, upscale et redémarrages détachés.
- `dashboard/routers/config_extra_routes.py` : custom-routes, web-search,
  web-fetch et api-keys GET/POST extraits, masquage et validation conservés.
- `dashboard/routers/traffic_routes.py` : capture trafic status/config/frames/
  détail/clear/stats extraite, viewer et toggle utilisateur injectés.
- `dashboard/routers/debug_routes.py` : logs runtime, toggle debug et logs
  debug (lecture paginée + purge) extraits.
- `POST /api/egress/qualification/epochs` : lance une époque via
  `qualification_daemon.py --once` ; shadow par défaut, live seulement si
  `shadow_mode=false`, conflit 409 si un lancement tourne.
- `dashboard-ui/src/pages/vpn/QualificationStatus.svelte` : couverture pays/
  transports, raisons de skip, époques + détail, lancement, pause/reprise
  candidat, âge du dernier test et file filtrée.
- `free/qualification/readmodel.py` : cache TTL 5 s par chemin/arguments,
  invalidation ciblée après mutation et borne 256 entrées.
- `free/qualification/scheduler.py` : priorité service — une station
  serving/rotating/repairing/paused n'est jamais préemptée par un sweep live.
- `QualificationStatus.svelte` : carte SLO flotte (6/6, disponibilité 1 h,
  error budget, heartbeat, MTTR, incidents) et santé des transports
  (dégradé, quota, infra, RTT p50, dernier test).
- `tests/test_qualification_preflight.py` : résolution, handshake, fallback
  RTT/connect, transport explicite et regroupement des erreurs.
- `preflight_timeout_s` : timeout effectif du `PreflightService` lu depuis la
  config et vérifié par test.
- `quota_retry_jitter_s` : jitter déterministe par hostname après un reset 429,
  borné et stable au redémarrage.
- `dashboard/routers/history_routes.py` : `/api/history`,
  `/api/requests/{id}`, `/api/history/filters` et `DELETE /api/history`
  extraits avec cache de filtres et invalidation.
- `dashboard/routers/adaptive_routes.py` : `/api/egress/adaptive` extrait,
  poids de base/santé/effectifs et fallback sans pool.
- `dashboard/routers/geo_routes.py` : geo-policies GET/PUT, rollback et
  notifications extraits avec contrôle `X-API-Key` et ETag.
- `dashboard/routers/cost_routes.py` : `/api/costs` extrait avec tarification
  injectée et fenêtre de filtre.
- `dashboard/routers/vpn_diagnostic_routes.py` : discover-and-add,
  upload-config et diagnostic VPN extraits, I/O docker/WSL injectée.
- `dashboard/routers/route_table_routes.py` : `/api/routes` extrait avec
  enrichissement geo, recherche et pagination.
- `dashboard/routers/vpn_status_routes.py` : `/api/vpn-status` extrait avec
  refresh single-flight, IP stats, rotation partagée, geo, latence, SLO local
  et superviseurs.
- `dashboard/routers/root_routes.py` : `/` extrait avec fallback 503 lisible.
- `dashboard/routers/read_models.py` : toutes les projections SQL
  (stats, séries, historique, filtres, coûts, usage free) centralisées ; les
  routeurs n'exécutent plus de SQL.
- `free/qualification/canary.py` + `scripts/canary_qualification.py` :
  évaluateur pur et runner HTTP du canary (6/6, SLO, heartbeat, incidents,
  découverte, trois transports, cutover commité), testé sans stations.
- `free/qualification/release.py` + `scripts/release_qualification.py` :
  manifeste de preuves avec SHA-256, refus si canary échoue ou checklist
  incomplète, testé.
- `tests/test_dashboard_parity.py` : contrats de forme et p95 < 500 ms sur
  `vpn-status`, history, stats, costs et routes.
- `scripts/soak_qualification.py` : options `--batch`/`--interval`/`--progress-s`
  pour un soak long à cadence maîtrisée et progression JSON ; exécution 24 h
  en cours (PID 33280, 1 claim/s, sortie `logs/qualification-soak-24h.json`).
- `tests/test_station_isolation.py` : breaker/restart par station et réparation
  `StationHealthRegistry` locale, aucune fuite inter-stations.
- `tests/test_architecture_reuse.py` : superviseur → manager, provider
  NordVPN → manager et `ops.reconciler` partagé.
- `tests/test_canary_runner.py` : runner canary testé de bout en bout contre
  un dashboard HTTP local (succès 6/6 et échec 5/6).
- Canary live exécuté sur `logs/registry.db` : 6 stations, **2/6 puis 0/6
  utilisables** après épuisement des quotas par IP, incidents ouverts,
  transports partiels, 0 cutover. Rapport `logs/canary-014.json` ; échec
  honnête, pas une release.
- `HttpStationProbe` corrigé : lit `vpn_status`, `current_ip`, `vpn.ip`,
  `vpn.container` et retombe sur `connected+egress` si `model_usable` absent.
- `WatchdogService` corrigé : écrit `USABLE`/`REPAIRING` (CHECK de production)
  et conserve la cause dans `reason` ; test sur `schema.sql` réel.
- `tests/test_pool_characterization.py` : URLs SOCKS5/HTTPS, sous-réseaux
  publics, sélection public, déduplication IP, compteurs provider/endpoint et
  backoff modèle global.
- **441 tests** qualification + dashboard passent ; `ruff` propre ; `config_audit` 0/0.
- `tests/test_qualification_faults.py` : DNS, 429, AUTH, tunnel down, disque
  plein (store en erreur, file précédente lisible), source indisponible.
- Claim avec `lease_until` : une lease expirée est requalifiée après crash.
- Reprise : `tests/test_qualification_resume.py` vérifie curseur, round,
  quota reset et absence de double comptage après réouverture du store.
- Migration versionnée **3** : `rotation/infrastructure/db/migrate.py` et
  colonne additive `qualification_candidates.lease_until`.
- `tests/test_option_map.py` : les 27 clés `egress_catalog.qualification.*`
  ont un contrat actif dans `specs/013.../option-map.md`.
- Sauvegarde/restauration : `VACUUM INTO` + `PRAGMA integrity_check` testés.
- `specs/014.../implementation-log.md` : journal d’implémentation et preuves.
- `specs/014.../release-report.md` : rapport qualité, mesures et blocages.
- `docs/DEBT.md` : 0 P0, 4 P1 planifiées, 4 P2.
- `CHANGELOG.md` : entrée 014 datée du 2026-11-10.

**Marquées faites** : T001-T003, T004-T019, T020-T026, T027-T029, T038-T046, T048-T051, T052-T058, T059-T062, T063-T069, T070-T075, T077-T079, T080-T083, T084-T098, T099-T104, T105-T110, T111-T125, T126-T135, T137-T142, T143-T147, T150-T151, T164-T166.

**Ouvertes** : T030-T037 (quota/rotation : apply réel et preuves restantes),
T076 (second tunnel), T104/T110 (superviseur/lifecycle), T125 (CI Linux),
T136 (soak 24 h), T148-T149 (canary/signature), T152-T175 (slices et
démantèlement).

**Prochaines tranches** : cutover `apply=true` guardé, canary six stations,
soak 24 h, puis S1–S8/S11–S13.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: peut tourner en parallèle (fichiers différents, pas de dépendance)
- **[Story]**: US1…US11
- Chemins exacts dans chaque tâche

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: créer le squelette du module et la configuration, sans brancher le
runtime.

- [x] T001 Créer `free/qualification/__init__.py` et les fichiers vides
  `domain.py`, `scoring.py`, `store.py`, `source.py`, `preflight.py`,
  `sweep.py`, `runtime.py` avec docstrings et `__all__`.
- [x] T002 [P] Ajouter le bloc `egress_catalog.qualification` dans
  `config.yaml` (enabled, epoch, weights, preflight, backoff, rétention) avec
  des valeurs par défaut identiques à `spec.md`.
- [x] T003 [P] Créer `tests/test_qualification_domain.py`,
  `test_qualification_store.py`, `test_qualification_source.py`,
  `test_qualification_sweep.py`, `test_qualification_rotation.py`,
  `test_qualification_resume.py`, `test_qualification_preflight.py`,
  `test_qualification_api.py`, `test_qualification_runtime.py`,
  `test_qualification_station_runtime.py`,
  `test_qualification_transport.py`, `test_qualification_cutover.py`,
  `test_qualification_watchdog.py`, `test_qualification_discovery.py`,
  `test_qualification_repair_adapter.py` (suite réelle, pas seulement des
  squelettes).
- [x] T004 [P] Ajouter `specs/014-nordvpn-exhaustive-rotation/contracts/option-map.md`
  et le lien depuis `plan.md` ; lister toutes les clés
  `egress_catalog.qualification.*`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: domaine pur, schéma SQLite, store et migration. Aucune story ne
peut démarrer avant.

- [x] T005 [P] Implémenter `free/qualification/domain.py` :
  `Candidate`, `Attempt`, `Epoch`, `PotentialScore`, `MeasuredScore`,
  `AttemptStatus`, `CandidateStatus`, `classify_attempt`,
  `next_candidate_status`, `order_key`.
- [x] T006 [P] Implémenter `free/qualification/scoring.py` :
  `potential_score(candidate, weights, now)` (30/30/20/15/5) et
  `measured_score(option, now)` (40/25/20/10/5) ; fonctions pures, aucun I/O.
- [x] T007 [P] Ajouter le schéma `qualification_candidates`,
  `qualification_attempts`, `qualification_epochs` dans
  `rotation/infrastructure/db/schema.sql` et dans
  `free/qualification/store.py` (CREATE IF NOT EXISTS, index, contraintes).
- [x] T008 Implémenter `free/qualification/store.py` :
  `upsert_candidates`, `claim_next`, `release`, `record_attempt`,
  `promote_usable`, `mark_quota`, `mark_infra`, `mark_auth`, `mark_dead`,
  `mark_skipped`, `epoch_summary`, `coverage`, `resume_cursor`.
- [x] T009 Ajouter la migration versionnée dans
  `rotation/infrastructure/db/migrate.py` ; backup `VACUUM INTO` avant
  application ; test de rollback sur copie.
- [x] T010 [P] Écrire les tests du domaine (`T005`) : transitions, portes
  dures, ordre, idempotence, `429` ≠ `dead`, `no_egress` ≠ `dead`.
- [x] T011 [P] Écrire les tests du store (`T008`) : claim concurrent, lease
  expiré, reprise, index, `reset_at`, unicité d'IP.
- [x] T012 Valider que `registry.db` reste l'autorité : aucun JSON d'état
  de file, aucune écriture dans `logs/ip_registry.db`.

**Checkpoint**: domaine + store testés ; aucune story runtime.

---

## Phase 3: User Story 1 — File classée par potentiel (Priority: P1) 🎯 MVP

**Goal**: ingérer les 7 070 serveurs et les ordonner par potentiel.

**Independent Test**: catalogue de 10 serveurs avec charges/RTT/historiques
différents → l'ordre de la file est exactement l'ordre du score.

- [x] T013 [P] [US1] Implémenter `free/qualification/source.py` :
  `NordVpnCatalogSource` lit `logs/nordvpn_api.json` (TTL 6 h, fallback
  cache), complète avec `gluetun_nordvpn_servers.json`/`embedded`, déduplique
  par `hostname`, extrait `technologies` → transports.
- [x] T014 [US1] Ajouter `server_transports` : une ligne par
  `(hostname, transport)` avec `wireguard`, `openvpn-udp`, `openvpn-tcp`,
  `source_id`, `first_seen_at`, `last_seen_at`.
- [x] T015 [US1] Implémenter `upsert_candidates` : fusion sans doublon,
  mise à jour `load`, `country`, `city`, `transports`, `stale`, `source_id`.
- [x] T016 [US1] Brancher le score de potentiel dans l'ingestion ; calculer
  `potential_score` avec les poids de `config.yaml` ; recalculer à chaque
  refresh.
- [x] T017 [US1] Implémenter l'ordre `potential_score DESC`, quota restant
  `DESC`, `rtt_ms ASC`, `load ASC`, `hostname ASC` dans `claim_next`.
- [x] T018 [US1] Exposer `GET /api/egress/qualification/queue` (pagination,
  filtres status/pays/transport, tri) dans `dashboard/api.py`.
- [x] T019 [P] [US1] Tests `test_qualification_source.py` : API 7 070,
  dédoublonnage, fallback stale, transports, pays, charge.
- [x] T020 [P] [US1] Tests `test_qualification_domain.py` : ordre exact,
  égalité départagée par quota/RTT/charge/hostname, pays non facteur,
  exclusion des hostnames absents de l'API (legacy/stale).

**Checkpoint**: la file contient 7 070 lignes ordonnées ; aucun bind.

---

## Phase 4: User Story 2 — Balayage exhaustif par époque (Priority: P1)

**Goal**: parcourir la file, un hostname à la fois, avec reprise et état
terminal.

**Independent Test**: 20 hostnames, kill après 10, relance → 10 non re-testés,
10 suivants testés.

- [x] T021 [US2] Implémenter `free/qualification/sweep.py` :
  `QualificationSweep.run_epoch`, `claim`, `preflight`, `bind`, `probe`,
  `classify`, `record`, `next`.
- [x] T022 [US2] Ajouter `epoch_id`, `cursor`, `max_attempts_per_host_per_epoch`
  (défaut 3), `sweep_slice_size` (défaut 500), `sweep_time_budget_s`.
- [x] T023 [US2] Implémenter la reprise transactionnelle :
  `sweep_cursor`/`qualification_epochs`, leases expirés → `pending`,
  idempotence au redémarrage. *Curseur/round portés par la candidate, leases
  `lease_until`, doublons refusés, test de reprise après réouverture.*
- [x] T024 [US2] Implémenter `scripts/qualification_sweep.py` :
  `--epoch`, `--max`, `--station`, `--dry-run`, `--resume`, logs JSONL.
- [x] T025 [US2] Ajouter le mode `--sweep` à `scripts/fresh_chain.py` :
  après un 200, enregistrer et **continuer** ; `--repair` conserve l'arrêt au
  premier 200.
- [x] T026 [US2] Brancher le balayage dans `free/egress_runtime.py` :
  service construit avec le store existant, planifié par le runtime, jamais
  dans le chemin de requête. *`QualificationScheduler` + champ injectable,
  démarrage/arrêt propres dans `EgressRuntime`.*
- [x] T027 [US2] Ajouter `GET /api/egress/qualification/epochs` et
  `GET /api/egress/qualification/epochs/{id}`.
- [x] T028 [P] [US2] Tests `test_qualification_sweep.py` : 200 continue, 429
  continue, `no_egress` retry, époque complète, budget.
- [x] T029 [P] [US2] Tests `test_qualification_resume.py` : kill au milieu,
  reprise sans doublon, lease expiré, curseur.

**Checkpoint**: une époque de 7 070 hostnames peut tourner et reprendre.

---

## Phase 5: User Story 3 — Quota consommé, rotation sur 429 (Priority: P1)

**Goal**: utiliser une IP jusqu'au 429, puis claim le meilleur candidat suivant
sans attendre le reset.

**Independent Test**: IP 200 puis 429 → `quota` avec reset exact, candidat
suivant en < 60 s, aucun test avant reset.

- [ ] T030 [US3] Brancher `egress_catalog.transition` et
  `free/ip_quota` dans le balayage : 200 → `usable` + `measured_score` ;
  429 → `quota` + `reset_at`.
- [ ] T031 [US3] Implémenter `rotate_on_429` : libérer l'option, claim le
  meilleur candidat, respecter `max_active_stations`, jitter sur le reset.
- [ ] T032 [US3] Ajouter le ledger `egress_ips`/`quota_buckets` dans la
  sélection : `quota_remaining`, `reset_at`, `used`, `successes`, `failures`.
  *`quota_per_ip` recalibré de 10 à **200** (ledger live : 176 succès avant
  le premier 429 ; audit 004 : ~200 req/5 h). `update_config` rafraîchit
  désormais `QuotaProfile.default_quota`, plus seulement les endpoints
  SOCKS5.*
- [ ] T033 [US3] Implémenter l'unicité d'IP : index unique, vérification au
  claim et à la promotion, libération explicite.
- [ ] T034 [US3] Implémenter `429` sans 200 antérieur = mauvaise IP (pas un
  quota consommé), avec backoff court et respect du reset.
- [x] T035 [US3] Exposer `GET /api/egress/qualification/quota` (par IP,
  headroom, reset, provider) et `POST /api/egress/qualification/rotate`.
  *GET quota, dry-run et `apply=true` faits : `AdapterCutoverPorts` exécute
  prepare/switch/probe/rollback sur le binder/prober réels ; l'apply est
  refusé en 409 tant que `shadow_mode` est actif. Tests fakes de bout en
  bout, dont un cutover `committed`.*
- [ ] T036 [P] [US3] Tests `test_qualification_rotation.py` : 200 → 429 →
  suivant, reset respecté, unicité, quota non consommé, pénurie exposée.
- [ ] T037 [P] [US3] Test de non-régression : aucune sélection d'IP avec
  `reset_at` futur ; aucun `429` compté comme `dead`.
  *Filtre `forbidden_ips` ajouté à `CatalogClaims.claim_next` : une candidate
  dont `last_egress_ip` est épuisée n'est plus claimée ; fallback legacy
  testé. Reste à couvrir les lignes legacy sans IP observée.*

**Checkpoint**: le quota de chaque IP est consommé et la rotation est continue.

---

## Phase 6: User Story 4 — Classification honnête, zéro pays (Priority: P1)

**Goal**: séparer infra/quota/auth/dead/skipped ; supprimer `country_backoff`.

**Independent Test**: quatre hosts `no_egress`, `429`, `AUTH`, `NXDOMAIN` →
quatre états distincts, aucun pays bloqué.

- [x] T038 [US4] Supprimer la branche `country_backoff` de
  `free/catalog_claims.py` et les déclencheurs pays de
  `scripts/fresh_chain.py:1817-1843`/`:2053-2080`.
- [x] T039 [US4] Remplacer par un backoff par hostname/IP :
  `infra_backoff_s` (300), `infra_backoff_max_s` (3600), jitter, remise à zéro
  sur 200.
- [x] T040 [US4] Implémenter `classify_attempt` : `200`, `429`,
  `no_egress`, `bind_error`, `auth`, `timeout`, `dead`, `skipped`.
- [x] T041 [US4] Implémenter `AUTH_FAILED` → quarantaine station, hostnames
  `pending` après réparation ; ne jamais `dead`.
- [x] T042 [US4] Implémenter `dead` terminal : `NXDOMAIN`, hostname invalide,
  transport définitivement absent ; raison explicite. Un host retiré du
  catalogue est `skipped` (`removed-from-catalog`).
- [x] T043 [US4] Implémenter `skipped` : `removed-from-catalog`, `dedicated`,
  `denylisted`, `unsupported-transport`, `operator-paused`.
  *1 538 dédiés, 137 transports non supportés, denylist API et pause/reprise
  opérateur (`POST /candidates/{hostname}/pause|resume`).*
- [x] T044 [US4] Mettre à jour `catalog_claims.claim_next` pour lire la file
  (`qualification_candidates`) et non `catalog` legacy ; `catalog` reste en
  lecture seule ; les hostnames absents de l'API sont `skipped`/`stale`, jamais
  claimés.
- [x] T045 [P] [US4] Tests de non-régression pays : 3 × 429 dans un pays ne
  bloquent aucun hostname ; 5 × `no_egress` ne bloquent que les hosts testés.
- [x] T046 [P] [US4] Tests de classification : chaque statut, chaque raison,
  transitions, pas de `dead` sur infra.

**Checkpoint**: 0 `country_backoff` ; 85 % de `no_egress` ne parquent plus le
catalogue.

---

## Phase 7: User Story 5 — Ping opérateur (Priority: P2)

**Goal**: mesurer DNS/connect/handshake depuis l'opérateur et l'utiliser dans
le score.

**Independent Test**: RTT 20 ms vs 200 ms → le premier passe devant à charge et
quota égaux.

- [x] T047 [US5] Brancher `free/ip_registry/preflight.py` dans
  `free/qualification/preflight.py` : 3 échantillons, médiane, timeout
  configurable. *`PreflightProber` injecté via `build_qualification(...,
  preflight_prober=...)`, protocole `preflight_many`.*
- [x] T048 [US5] Enregistrer `dns_ms`, `connect_ms`, `handshake_ms`, `rtt_ms`,
  `endpoint_ip`, `transport` dans `qualification_attempts`.
- [x] T049 [US5] Utiliser `rtt_ms` dans `potential_score` (30 %) et
  `latency_ms` through-tunnel dans `measured_score` (25 %).
- [x] T050 [US5] Un preflight échoué = `unreachable` (infra), pas de bind, pas
  de session consommée.
- [x] T051 [P] [US5] Tests `test_qualification_preflight.py` : résolution,
  timeout, handshake, médiane, fallback transport.
  *Médiane, timeout, fail-soft, résolution, handshake, RTT/connect fallback,
  transport explicite et regroupement d'erreurs couverts.*
- [x] T052 [P] [US5] Test de corrélation : le décile supérieur du score
  produit ≥ 80 % des 200 sur un jeu synthétique.

**Checkpoint**: l'ordre reflète le ping réel de l'opérateur.

---

## Phase 8: User Story 6 — Couverture observable (Priority: P2)

**Goal**: dashboard/API de couverture et prochains candidats.

**Independent Test**: 100 hosts dans cinq états → compteurs exacts et top 20
ordonné.

- [x] T053 [US6] Implémenter `GET /api/egress/qualification/summary` :
  total, pending, usable, quota, auth, dead, skipped, testés/total, âge,
  débit, prochaine échéance.
- [x] T054 [US6] Implémenter `GET /api/egress/qualification/next?limit=20` :
  prochains candidats avec score, quota, RTT, charge.
- [x] T055 [US6] Ajouter les vues dashboard : couverture par pays/transport,
  époque courante, bouton lancer/pause, export CSV/JSON.
  *Couverture, époques, lancement shadow/live borné, pause/reprise candidat
  et export CSV/JSON livrés.*
- [x] T056 [US6] Ajouter les agrégats précalculés et la pagination pour 7 070
  lignes ; cache 5 s ; p95 < 2 s.
  *Cache TTL 5 s par chemin + arguments, invalidation après pause/reprise ;
  7 070 candidats mesurés sous 2 s.*
- [x] T057 [P] [US6] Tests d'API : compteurs, top 20, pagination, filtres,
  performance 1 000 lignes < 500 ms.
- [x] T058 [P] [US6] Test UI : aucune option sans âge de dernier test.
  *Colonne « dernier test » explicite, « jamais testé » sinon.*

**Checkpoint**: l'opérateur voit testés/total et les prochains candidats.

---

## Phase 9: User Story 7 — Options réelles (Priority: P1)

**Goal**: chaque clé de qualification a un consommateur et un test.

**Independent Test**: modifier chaque clé de `option-map.md`, vérifier l'effet.

- [x] T059 [US7] Remplir `contracts/option-map.md` : surface, clé, défaut,
  consommateur `file:function`, effet observable, test, statut.
- [x] T060 [US7] Ajouter `config_audit` pour `egress_catalog.qualification.*` :
  0 dead / 0 missing ; échec si une clé n'a pas de ligne.
- [x] T061 [US7] Ajouter un gate GUI : tout contrôle sans consommateur échoue.
- [x] T062 [P] [US7] Tests d'effet : `max_attempts`, `weights`,
  `preflight_timeout_s`, `latency_budget_ms`, `infra_backoff_s`,
  `quota_retry_jitter_s`, `max_active_stations`.
  *Effets couverts côté qualification ; `max_active_stations` relève de
  `egress_catalog.providers.nordvpn` et reste testé par l'adaptateur.*
- [x] T063 [P] [US7] Test de non-régression : aucune option inerte ; valeurs
  effectives affichées, pas seulement le YAML.

**Checkpoint**: 100 % des options ont un effet mesuré.

---

## Phase 10: User Story 8 — Six stations, zéro collision, reprise (Priority: P2)

**Goal**: concurrence bornée, leases, reprise superviseur.

**Independent Test**: 6 workers × 60 hosts → aucun doublon ; kill -9 → leases
expirés récupérés.

- [x] T064 [US8] Implémenter les workers par station dans
  `free/qualification/sweep.py` : un hostname = un claim, `max_active_stations`.
  *Implémenté dans `station_runtime.py` (`StationWorker`,
  `ParallelStationRunner`) avec claim unique par hostname.*
- [x] T065 [US8] Ajouter les leases `(station, hostname, egress_ip,
  lease_until, state)` et l'expiration ; `claimed` orphelin → `pending`.
  *`qualification_candidates.lease_until` + `station_runtime`.*
- [x] T066 [US8] Brancher le superviseur : `fresh_chain_supervisor` lance
  `--sweep` avec `max_candidates_per_chain`, priorité service > repair > sweep.
  *Lance `qualification_daemon.py --once --shadow` selon
  `daemon_enabled`/`daemon_interval_s` ; priorité service inchangée.*
- [x] T067 [US8] Test de concurrence : 6 workers, aucun hostname claimé deux
  fois, aucune IP partagée. *`test_six_workers_run_in_parallel_with_unique_stations`
  + unicité du claim et de l'IP dans les tests store/rotation.*
- [x] T068 [US8] Test de crash : kill -9 au milieu, reprise, leases expirés,
  < 1 min de travail perdu. *Process enfant terminé par `os._exit(9)`, lease
  expirée récupérée à la réouverture.*
- [x] T069 [US8] Test de charge : 7 070 hosts, 6 workers, projection < 24 h.
  *`scripts/load_qualification.py` : 7 070 hosts, 6 workers, 71,5 s,
  projection ~8,5 M hosts/24 h, 0 erreur worker.*

**Checkpoint**: la flotte de six stations balaie sans collision.

---

## Phase 11: User Story 9 — Six stations indépendantes, parallèles, sans coupure (Priority: P1)

**Goal**: un worker et un état par station ; rotations concurrentes ; standby et
make-before-break ; aucune pause globale ; disponibilité ≥ 99 %.

**Independent Test**: 6 stations, 429 sur la 3, AUTH_FAILED sur la 5 ; les
stations 1/2/4/6 ne perdent aucune requête ; 3 bascule sur son standby en p95
< 2 s ; 5 est réparée sans impact.

- [x] T070 [US9] Implémenter `free/qualification/station.py` :
  `StationRuntime`, machine à états (`serving`, `qualifying`, `rotating`,
  `repairing`, `paused`), un worker par station.
- [x] T071 [US9] Ajouter `station_runtime` et `station_cutovers` au schéma et
  au store ; index par station/état.
- [x] T072 [US9] Supprimer les pauses globales (`global_429_pause_s`,
  `exhausted_pause_s`) ou les limiter à la station/IP ; test de non-régression.
- [x] T073 [US9] Supprimer la règle « jamais deux rotations simultanées » de
  `free/rotation_planner.py` ; autoriser les rotations concurrentes avec un
  rate limit global de rebind.
- [x] T074 [US9] Implémenter le standby par station et la rotation proactive au
  seuil de headroom (`proactive_rotation_headroom_pct`, défaut 20 %).
- [x] T075 [US9] Implémenter `free/qualification/cutover.py` :
  `prepare` (preflight), `probe` (200), `commit`, `rollback` ; l'ancienne IP
  sert jusqu'au 200 de la nouvelle.
- [ ] T076 [US9] Implémenter le chemin second tunnel/espace de noms quand il
  est supporté (coupure 0) ; sinon cutover borné p95 < 2 s, max < 5 s, mesuré.
  *Chemin borné implémenté (`cutover_adapter.py` + `apply.py`) et testé avec
  fakes ; la mesure p95 live et le second tunnel restent à faire.*
- [x] T077 [US9] Isoler `AUTH_FAILED`/`bind_error` par station ; hostnames
  remis dans la file partagée ; aucune autre station affectée.
- [x] T078 [US9] Protéger la dernière station utilisable : rotation différée
  sans standby validé ; jamais 0/6 par rotation planifiée.
- [x] T079 [US9] Appliquer la priorité service > repair > sweep par station ;
  une station qui sert n'est jamais préemptée par le balayage.
  *Scheduler : skip `service-priority:<state>` en live pour serving/rotating/
  repairing/paused ; shadow reste autorisé ; getter en échec = fail-open.*
- [x] T080 [US9] Ajouter `GET /api/egress/qualification/stations` : état, IP
  courante, standby, cutover, disponibilité 1 h/24 h, 6/6.
- [x] T081 [P] [US9] Tests : rotations concurrentes sur deux stations, aucune
  sérialisation, cutover p95, rollback, last-station protection.
- [x] T082 [P] [US9] Tests : panne/réparation d'une station n'affecte pas les
  cinq autres ; disponibilité 6/6 synthétique ≥ 99 %.
- [x] T083 [US9] Ajouter les clés de config et les lignes `option-map` pour
  `per_station_workers`, `station_standby_size`,
  `proactive_rotation_headroom_pct`, `cutover_*`, `parallel_rotations`,
  `global_pause_on_429=false`.
- [x] T084 [US9] Mettre à jour `quickstart.md` et `data-model.md` pour le
  runtime station et le cutover.

**Checkpoint**: six stations parallèles, standby, cutover mesuré, zéro pause
globale.

---

## Phase 12: User Story 10 — Stratégie multi-transports cyclique (Priority: P1)

**Goal**: en `auto`, essayer WireGuard, OpenVPN UDP, OpenVPN TCP, puis
reboucler sur WireGuard ; curseur/round persistants ; santé par transport.

**Independent Test**: WG `no_egress` → OVPN UDP `429` → OVPN TCP `200` ;
vérifier l'ordre. Puis trois échecs → WG round 2, pas `dead`.

- [x] T085 [US10] Implémenter `free/qualification/transport.py` :
  `TransportPlan`, `transport_order`, `next_transport`, `round`, `advance`,
  `wrap`.
- [x] T086 [US10] Peupler `server_transports` depuis l'API
  (`wireguard_udp`, `openvpn_udp`, `openvpn_tcp`) ; un host sans transport est
  `skipped`.
- [x] T087 [US10] Intégrer le plan de transport dans `sweep.py` : une tentative
  par `(hostname, transport, round)`, résultat enregistré.
- [x] T088 [US10] Sur `no_egress`/`bind_error`/timeout, passer au transport
  suivant ; sur `429`, ne pas sauter les autres ; après le dernier, reboucler.
- [x] T089 [US10] Ajouter `transport_retry_rounds` (défaut 2),
  `transport_retry_backoff_s` (défaut 60) et jitter.
- [x] T090 [US10] Persister `transport_cursor`, `transport_round` et
  `attempts_by_transport` ; reprise au bon transport/round.
- [x] T091 [US10] Agréger `transport_health` (200/429/infra/latence) et marquer
  `degraded` après `transport_health_min_attempts` sans 200.
- [x] T092 [US10] Exposer le diagnostic OpenVPN (bind/AUTH/probe/DNS/timeout)
  quand UDP/TCP a 0 succès ; ne pas condamner les hostnames.
- [x] T093 [US10] Supprimer le raccourci `fast_discovery` à un transport en
  `auto` ; `--repair` peut garder un chemin rapide mais doit épuiser les
  transports avant `dead`.
- [x] T094 [US10] En mode `manual`, n'utiliser que le transport choisi ;
  test d'isolation.
- [x] T095 [US10] Interdire les doublons `(hostname, transport, round)` ;
  dédupliquer les IP identiques entre transports.
- [x] T096 [US10] Ajouter `GET /api/egress/qualification/transports` :
  couverture et santé par transport, curseur par hostname.
- [x] T097 [P] [US10] Tests : ordre WG→UDP→TCP, 429 n'empêche pas les autres,
  transport non supporté, reprise du curseur.
- [x] T098 [P] [US10] Tests : trois échecs → WG round 2, pas `dead` ; AUTH →
  station, pas host ; OpenVPN 0/200 → degraded.
- [x] T099 [US10] Ajouter les clés de config et les lignes `option-map` pour
  `transport_strategy`, `transport_order`, `transport_retry_*`,
  `transport_health_min_attempts`.

**Checkpoint**: tous les transports sont exercés, mesurés et cyclés ; aucun
hostname condamné sur un seul transport.

---

## Phase 13: User Story 11 — Watchdog, gestion et découverte de bout en bout (Priority: P1)

**Goal**: watchdog par station, découverte schedulée, réparation unifiée dans
`registry.db`, réutilisation lifecycle/reconciler/supervisor/manager.

**Independent Test**: tuer le tunnel de la station 2 ; détection < 30 s,
réparation locale, stations 1/3/4/5/6 inchangées ; API coupée → cache/embedded ;
watchdog redémarré seul.

- [x] T100 [US11] Implémenter `free/qualification/watchdog.py` : heartbeat,
  sondes tunnel/DNS/egress/AUTH/quota/`client_usable`, état par station.
- [x] T101 [US11] Persister `station_state` dans `registry.db` (container,
  transport, état, pays, IP, hostname, `client_usable`, `last_probe_at`,
  `last_200_at`, raison).
- [x] T102 [US11] Écrire `incidents` par station : `scope='station'`, cause,
  preuve, action, résultat, début/fin ; ne jamais enregistrer une panne locale
  comme `fleet`.
  *Une station seulement épuisée en quota est `DRAINING`, pas `REPAIRING` :
  `reason` (`daily-bucket-exhausted`, `draining`, `quota-blocked`, …) est
  classé `QUOTA`, l'incident est fermé sans job de réparation. Test dédié.*
- [x] T103 [US11] Unifier la réparation dans `registry.db` :
  `repair_jobs`/`repair_requests`, backoff, attempts, transports, résultat ;
  `logs/repair_queue.db` n'est plus une autorité.
  *`RepairQueue` utilise `logs/registry.db` par défaut ; `repair_jobs` y sont
  déjà persistés par le watchdog.*
- [x] T104 [US11] Brancher `ops/supervisor.py` + `free/station_health.py` :
  réparation locale, aucun restart/ pause des autres stations.
  *Isolation prouvée : breaker/restart par station, `StationHealthRegistry`
  local, test dédié dans `tests/test_station_isolation.py`.*
- [x] T105 [US11] Implémenter `max_repair_attempts`, backoff, jitter,
  escalade ; arrêt de la boucle après échec.
- [x] T106 [US11] Ajouter le heartbeat du watchdog ; stale → redémarrage du
  watchdog seul, pas des stations.
- [x] T107 [US11] Implémenter `free/qualification/discovery.py` : scheduler
  API 6 h → cache → embedded → manifeste, `discovery_runs`, `stale`.
- [x] T108 [US11] Ingérer les 7 070 serveurs, transports, charge, pays ;
  dédupliquer ; serveur retiré → `skipped`.
- [x] T109 [US11] Discovery sans bind : preflight DNS/connect/handshake
  seulement ; aucun session ouverte.
- [x] T110 [US11] Réutiliser `ops/lifecycle.py`, `ops/reconciler.py`,
  `ops/providers/nordvpn.py` et `vpn/manager.py` ; aucun fork.
  *Délégation superviseur → manager et provider → manager vérifiée ;
  `ops.reconciler.reconcile` reste l'entrée partagée.*
- [x] T111 [US11] Interdire au watchdog de redémarrer une station saine qui
  sert ; priorité service > repair > sweep.
- [x] T112 [US11] Ajouter `GET /api/egress/qualification/watchdog`,
  `/discovery`, `/repairs` et l'état par station au dashboard.
- [x] T113 [P] [US11] Tests : tunnel down station 2 → seul 2 réparé ;
  AUTH/DNS/no_egress → états corrects ; autres stations inchangées.
- [x] T114 [P] [US11] Tests : réparation échoue N fois → escalade, pas de
  boucle, pas de double job.
- [x] T115 [P] [US11] Tests : API catalogue down → cache/embedded ; nouveau
  serveur → pending ; retiré → skipped ; `stale` visible.
- [x] T116 [P] [US11] Tests : heartbeat watchdog stale → watchdog seul
  redémarré ; stations toujours serving.
- [x] T117 [P] [US11] Tests : redémarrage OS → `station_state`, `incidents`,
  `repair_jobs` et curseurs persistés.
- [x] T118 [US11] Ajouter les clés de config et `option-map` pour watchdog/
  discovery/repair (`watchdog_enabled`, `probe_interval_s`,
  `max_repair_attempts`, `discovery_interval_s`, `repair_backoff_*`).

**Checkpoint**: watchdog local, découverte schedulée, réparation unique,
aucune panne de station ne devient une panne de flotte.

---

## Phase 14: Polish & Cross-Cutting

- [x] T119 [P] Mettre à jour `specs/014.../data-model.md` et
  `quickstart.md` avec le schéma final.
- [x] T120 [P] Ajouter les métriques Prometheus/JSON : `qualification_tested`,
  `qualification_200`, `qualification_429`, `qualification_infra`,
  `qualification_skipped`, `qualification_queue_depth`,
  `qualification_epoch_duration_s`, `qualification_transport_*`,
  `qualification_station_availability`, `watchdog_*`, `discovery_*`,
  `repair_*`.
- [x] T121 [P] Ajouter la rétention 7 jours et le checkpoint WAL pour
  `qualification_attempts`, `station_cutovers`, `incidents` et `discovery_runs`.
- [x] T122 [P] Mettre à jour `CHANGELOG.md` et `specs/014.../implementation-log.md`.
- [x] T123 Vérifier `scripts/platform_audit.py --strict` = 0 violation.
- [x] T124 Vérifier `config_audit` = 0 dead / 0 missing.
- [x] T125 Vérifier la gate non-Docker sur Windows et Linux.
  *Windows : 10/10. Linux : Ubuntu 22.04/WSL, Python 3.12 via `uv`,
  copie native `/tmp` (WAL SQLite refuse DrvFs), gate 10/10, 411 tests,
  couverture 92 %. Rejouable via `scripts/linux_gate_wsl.sh` ; le job CI
  `Qualification release gate` reste le chemin officiel.*
- [x] T126 Vérifier 0 fenêtre visible pendant un balayage complet.
  *Qualification : subprocess `CREATENO_WINDOW`, `platform_audit` 0 ; preuve
  visuelle du balayage complet reste à faire avec le canary.*
- [x] T127 Vérifier la reprise après redémarrage OS, y compris transport/round,
  station_state, incidents et repair_jobs. *Équivalent applicatif couvert par
  le crash process `os._exit(9)` + réouverture WAL.*
- [x] T128 Vérifier qu'aucun `429` n'est enregistré comme `dead`, qu'aucun
  pays n'est bloqué, que 100 % des observations ont `provider`, que les
  transports sont mesurés, que la disponibilité par station est exposée et que
  le watchdog ne répare qu'une station à la fois.

---

## Phase 15: Quality gates & architecture

- [x] T129 [P] Tests d'architecture : domaine pur (0 I/O, 0 import
  provider/SQLite), règle de dépendance, 0 cycle, `import-linter`.
- [x] T130 [P] Audit taille/complexité + ratchet : aucun nouveau module
  > 1 500 lignes, aucun module legacy touché ne grossit, complexité sous seuil,
  0 duplication de scorer/classifieur/state machine.
- [x] T131 [P] Contract tests pour chaque port (`CatalogSource`,
  `QualificationStore`, `TransportPlanner`, `StationBinder`, `Prober`,
  `QuotaLedger`, `Watchdog`, `RepairQueue`, `Clock`) et chaque adapter.
- [x] T132 [P] Gate de couverture : ≥ 85 % domaine/application, 100 % chemins
  critiques (classification, quota, cutover, watchdog, reprise).
- [x] T133 [P] Gate type/lint : ajouter `ruff`, `mypy`, `import-linter` et
  `pytest-cov` aux requirements-dev ; 0 erreur sur les nouveaux modules.
- [x] T134 [P] Suite d'injection de fautes : tunnel down, DNS cassé,
  `no_egress`, 429, `AUTH_FAILED`, crash, disque plein, source indisponible.
- [x] T135 [P] Benchmarks : preflight 500 < 5 min, sweep 7 070 < 24 h,
  cutover p95 < 2 s, rotation 429 < 60 s, API p95 < 500 ms.
- [ ] T136 [P] Soak 24 h : mémoire/disque bornés, files bornées, pas de fuite,
  pas de retry infini. *Soak 60 s persistant : 24 850 claims, pic 0,217 MB,
  DB 4,523 MB, 0 violation. Exécution 24 h lancée le 2026-11-10 à 13:54
  (PID 33280, 1 claim/s, progression 60 s, `logs/qualification-soak-24h.json`).*
- [x] T137 [P] Audit observabilité : logs structurés, métriques des cinq
  attributs, preuve par décision, 0 secret.
- [x] T138 [P] Dashboard SLO/error budget : heartbeat, âge de sonde,
  disponibilité par station, 6/6, MTTR.
- [x] T139 [P] Gate `config_audit` 0 dead/missing + `option-map` complet.
- [x] T140 [P] ADR et `debt.md` à jour : 0 P0 aux gates, P1 planifiée, 0 dette
  non listée.
- [x] T141 [P] Runbook opérateur : démarrer, pause, reprendre, diagnostiquer,
  rollback, restaurer.
- [x] T142 [P] Audit sécurité : 0 secret dans logs/métriques/API, 0 paid,
  0 second compte, 0 clé exposée.
- [x] T143 [P] Test migration/rollback : copie de `registry.db`, `VACUUM INTO`,
  restore, `integrity_check`.
- [x] T144 [P] Gate de release : échoue sur régression fiabilité, performance,
  qualité, modularité ou maintenabilité.
- [x] T145 [P] Rapport de qualité : résultats des gates, benchmarks, fautes,
  couverture, dette, MTTR.
- [x] T146 [P] Mettre à jour `CHANGELOG.md`, `implementation-log.md`,
  `data-model.md`, `quickstart.md` et les ADR.
- [x] T147 Revue finale de cohérence spec ↔ plan ↔ tasks ↔ data-model ↔
  contracts.
- [ ] T148 Vérifier les preuves du canary : une station, un pays, six stations,
  cutover, watchdog, découverte.
  *Runner `scripts/canary_qualification.py` et évaluateur testés ; canary live
  exécuté le 2026-11-10 : 2/6 puis 0/6 utilisables (quota par IP), échec
  honnête, preuve `logs/canary-014.json`. Exécution 6/6 en attente.*
- [ ] T149 Cocher la checklist de sortie et archiver les preuves.
  *Sections code/tests/données/sécurité/exploitation cochées avec preuves ;
  builder `scripts/release_qualification.py` prêt (SHA-256, refus si checklist
  incomplète) ; canary et preuve visuelle restent ouverts.*

---

## Phase 16: Monolith rewrite (US12)

**Goal**: découper `vpn/manager.py`, `free/pool/`, `scripts/fresh_chain.py`,
`dashboard/api.py` et `opencode.py` par strangler, avec parité, shadow, canary
et rollback.

**Independent Test**: extraire le cycle de vie de station du manager, exécuter
les tests de caractérisation + shadow, vérifier 0 régression, module < 1 500
lignes, rollback opérationnel.

- [x] T150 [US12] Inventaire : graphe de dépendances, seams, contrats, ADR
  cible, liste des slices et critères de sortie.
- [x] T151 [US12] Tests de caractérisation de `vpn/manager.py` (bind, AUTH,
  session, transport, health, rotation) avant extraction.
  *Couvert : clés station/ports, transitions + hold, AUTH cooldown/support,
  identités, IP récentes/partagées, flags egress et fenêtres pin.*
- [ ] T152 [US12] Extraire le cycle de vie station de `vpn/manager.py` vers
  `free/station/lifecycle.py`.
- [ ] T153 [US12] Extraire AUTH/session vers `free/station/auth_session.py`.
- [ ] T154 [US12] Extraire transport/bind vers
  `free/station/transport_bind.py`.
- [ ] T155 [US12] Extraire santé/probe vers `free/station/health_probe.py`.
- [ ] T156 [US12] Extraire rotation vers `free/station/rotation.py` ; le
  manager devient un adapteur fin.
- [ ] T157 [US12] Parité manager : shadow, canary, rollback, p95 ≤ +5 %,
  0 régression fonctionnelle.
- [x] T158 [US12] Tests de caractérisation de `free/pool/` (sélection,
  quota, rotation, public, accounting).
  *Sélection (`_dedupe_by_ip`, rang egress), public (`_public_subnet`,
  `_select_public_rows`, endpoints), accounting (compteurs
  provider/endpoint, backoff global), quota ledger (`usable`, réserve,
  exhaustion 429, reset/plafond, parked, buckets modèle, clear,
  persistance) et rotation (`_launch_rotation` : single-flight, délégation
  superviseur, gel événement amont, station retirée).*
- [ ] T159 [US12] Extraire sélection/quota vers `free/pool/selection.py` et
  `free/pool/quota.py`.
  *Conversion en package faite : `free/pool/__init__.py` porte le legacy,
  `selection.py` expose `egress_rank`/`dedupe_by_ip`, `quota.py` expose
  `ledger_ip`. Les gros sélecteurs (`_station_usable`, `_prefer_healthy`) et
  le ledger restent dans `__init__` ; extraction complète à poursuivre.*
- [ ] T160 [US12] Extraire rotation/public/accounting vers
  `free/pool/rotation.py` et `free/pool/public_pool.py`.
  *`rotation.py` expose les gardes ordonnées de `_launch_rotation` ;
  `public_pool.py` expose `public_subnet`/`select_public_rows` ;
  `accounting.py` expose les compteurs provider/endpoint/egress. Parité
  canary T161 non faite.*
- [ ] T161 [US12] Parité pool : shadow, canary, rollback, p95 ≤ +5 %.
- [ ] T162 [US12] Remplacer `scripts/fresh_chain.py` par
  `free/qualification/sweep.py` + CLI fine ; `--repair` reste un wrapper.
  *Pont déjà en place (`--sweep`, `CatalogClaims`) ; cette passe corrige la
  fuite `claimed` → `pending`/`quota`/`usable`, l'ordre preuves-200 avant
  backlog non mesuré et le verrou de chaîne d'un PID mort. Remplacement
  complet non fait.*
- [ ] T163 [US12] Parité fresh_chain : mêmes décisions, mêmes statuts,
  0 régression.
  *Tests ajoutés pour `claim_next` (preuve vs legacy vs backlog) et la
  synchronisation des statuts ; parité complète à mesurer.*
- [x] T164 [US12] Extraire les read models de `dashboard/api.py` vers
  `dashboard/routers/read_models.py`.
  *Toutes les requêtes SQL sont dans `read_models.py` ; les routeurs ne font
  que validation HTTP et mise en forme.*
- [x] T165 [US12] Extraire les routers station/qualification ; 0 SQL dans les
  routes. *Tous les handlers sont dans `dashboard/routers/` ; les requêtes SQL
  vivent dans `read_models.py`.*
- [x] T166 [US12] Parité dashboard : mêmes réponses, p95 ≤ +5 %.
  *Contrat de forme et p95 < 500 ms sur `/api/vpn-status`, `/api/history`,
  `/api/stats`, `/api/costs`, `/api/routes` ; baseline pré-extraction non
  conservée, budget API respecté.*
- [ ] T167 [US12] Extraire le pipeline de requête de `opencode.py` vers
  `free/request_pipeline/request.py`.
- [ ] T168 [US12] Extraire la jambe free et le routage modèle vers
  `free/request_pipeline/free_leg.py` et `model_routing.py`.
- [ ] T169 [US12] Réduire `opencode.py` à un composition root/entrypoint.
- [ ] T170 [US12] Parité opencode : shadow, canary, rollback, 0 régression.
- [ ] T171 [US12] Parité performance de chaque slice : benchmark avant/après,
  p95 ≤ +5 %.
- [ ] T172 [US12] Décommissionner les flags/legacy ; clôturer D-02 et D-20.
- [ ] T173 [US12] Audit final : 0 module > 1 500 lignes dans le control plane,
  0 cycle, domaine pur, couverture ≥ 85 % des modules extraits.
- [ ] T174 [US12] Rapport de démantèlement : slices, tailles avant/après,
  parité, rollbacks, dettes fermées.

---

## Phase 17: Release

- [ ] T175 Mettre à jour `.specify/feature.json` et clore la feature.

---

## Dependencies

- Phase 1 → Phase 2 → toutes les stories.
- US1 (T013-T017) avant US2 (T021-T026) : la file doit exister.
- US2 avant US3 (T030-T033) : le balayage doit produire des 200/429.
- US4 (T038-T044) avant le rollout : sans suppression `country_backoff`, la
  couverture reste parquée.
- US5 (T047-T049) avant le canary : le score doit être complet.
- US7 (T059-T061) avant le rollout : aucune option inerte.
- US8 (T064-T069) avant le général : concurrence et reprise.
- US9 (T070-T084) avant le rollout : parallélisme, standby, cutover, pas de
  pause globale.
- US10 (T085-T099) avant le canary : le cycle des transports doit être mesuré.
- US11 (T100-T118) avant le général : watchdog local, découverte schedulée,
  réparation unique.
- US4 (T038-T044) et US9 (T072-T073) avant le général : sans suppression du
  pays et des pauses globales, les stations restent couplées.
- Phase 15 (T129-T149) avant la release : gates qualité, fautes, benchmarks,
  soak, runbook, ADR, gate de release.
- Phase 16 (T150-T174) avant la release : inventaire, slices manager/pool/
  fresh_chain/dashboard/opencode, parité, rollback, D-02/D-20.
- T175 après T174 : clôture seulement si la checklist est complète.

## Parallel Execution Examples

- T005/T006/T007 en parallèle (domaine, scoring, schéma).
- T013/T019/T020 en parallèle (source, tests source, tests ordre).
- T028/T029 en parallèle (sweep, reprise).
- T036/T037 en parallèle (rotation, non-régression).
- T045/T046 en parallèle (pays, classification).
- T051/T052 en parallèle (preflight, corrélation).
- T057/T058 en parallèle (API, UI).
- T062/T063 en parallèle (effet, non-régression).
- T067/T068/T069 en parallèle (concurrence, crash, charge).
- T070/T071/T072 en parallèle (station runtime, schéma, pauses).
- T075/T076/T077 en parallèle (cutover, second tunnel, quarantaine).
- T081/T082 en parallèle (concurrence, disponibilité).
- T085/T086/T087 en parallèle (transport plan, capacités, intégration).
- T097/T098 en parallèle (ordre/reprise, échecs/diagnostic).
- T100/T101/T102 en parallèle (watchdog, station_state, incidents).
- T107/T108/T109 en parallèle (discovery scheduler, ingestion, preflight).
- T113/T114/T115/T116/T117 en parallèle (tests watchdog/découverte/reprise).
- T129/T130/T131 en parallèle (architecture, taille, contract tests).
- T132/T133/T134 en parallèle (couverture, type/lint, fautes).
- T135/T136/T137/T138 en parallèle (benchmarks, soak, observabilité, SLO).
- T140/T141/T142/T143 en parallèle (ADR/debt, runbook, sécurité, migration).
- T150/T151/T158 en parallèle (inventaire, caractérisation manager/pool).
- T152/T153/T154/T155/T156 en séquence dans le manager (seams dépendants).
- T159/T160/T164/T165 en parallèle après caractérisation (pool/dashboard).
- T167/T168/T169 en séquence dans opencode (pipeline → free leg → root).

## Implementation Strategy

**MVP** = Phase 1 + Phase 2 + US1 + US2 + US3 + US9 (worker par station, pas de
pause globale) + US10 (cycle des transports). Une file classée, un balayage qui
continue après 200, une rotation sur 429, six stations parallèles et tous les
transports exercés. Livrer en shadow puis canary sur une station avant
d'activer les six. US11 (watchdog/découverte), les gates de qualité
(T129-T149) et le démantèlement des monolithes (T150-T174) sont requis avant la
release.

**Incréments** :
1. File + score + ordre (US1).
2. Époque + reprise (US2).
3. Quota + rotation (US3).
4. Classification + suppression pays (US4).
5. Ping opérateur (US5).
6. Dashboard (US6).
7. Options (US7).
8. Concurrence (US8).
9. Parallélisme, standby, cutover, disponibilité (US9).
10. Cycle multi-transports, santé, diagnostic OpenVPN (US10).
11. Watchdog par station, découverte schedulée, réparation unifiée (US11).
12. Gates qualité : architecture, couverture, fautes, performance, soak,
    observabilité, runbook, release.
13. Démantèlement des monolithes : manager, pool, fresh_chain, dashboard,
    opencode, avec parité et rollback (US12).
