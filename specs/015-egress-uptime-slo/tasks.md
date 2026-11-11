# Tasks — Disponibilité mesurée et bascule rapide des egress

**Feature**: `015-egress-uptime-slo`
**Input**: `spec.md`, `plan.md`, `audit.md`
**Date**: 2026-11-11

Légende : `[P]` = parallélisable avec les autres tâches de la même phase ;
`P0` = bloquant la release ; `P1` = requis pour la cible ; `P2` = qualité et
robustesse.

---

## Phase 0 — Arrêt de l'hémorragie (J0–J1)

### T000 — Verrouiller le dimensionnement à six stations (P0)
- **Fichiers** : `config.yaml:138,661`, `docker-compose.yml:563-613`,
  `vpn/manager.py` (canary), `scripts/fresh_chain_supervisor.py:1346-1358`.
- **Action** : `station_count: 6`, `egress_capacity.target_slots: 6` ; le canary
  WireGuard n'est plus une septième session ; le superviseur refuse toute
  station hors `station_count` ; le warm pool ne crée que des IP.
- **Acceptation** : AT-11 ; `/api/pool-status` contient exactement 6 stations ;
  un test de config avec `station_count=7` échoue explicitement.
- **Test** : `tests/test_station_count.py::test_exactly_six_and_no_canary_session`.

### T001 — Figer la baseline et démarrer la mesure (P0)
- **Fichiers** : `scripts/station_slo_watch.py`, `logs/station_slo.jsonl`,
  `free/availability.py` (squelette).
- **Action** : échantillonner toutes les 30 s `{ts, station, client_usable, ip,
  transport, reason, last_200_age_s}` ; conserver 30 jours.
- **Acceptation** : 10 échantillons consécutifs, calcul manuel = calcul outil.
- **Test** : `tests/test_availability.py::test_samples_are_raw_and_bounded`.

### T002 — Protéger un gagnant frais (P0)
- **Fichiers** : `scripts/fresh_chain_supervisor.py:1314-1343`.
- **Action** : ne jamais effacer un pin si `winner_at < 180 s`, si l'IP du
  winner égale l'IP live, ou si un 200 a été vu < 180 s. Journaliser la décision.
- **Acceptation** : AT-07 ; plus de ligne `stale pin ... cleared` dans les 180 s
  suivant un winner.
- **Test** : `tests/test_station_allocator_supervisor.py::test_fresh_winner_pin_is_never_cleared`.

### T003 — Retrouver le winner quel que soit le transport (P0)
- **Fichiers** : `scripts/fresh_chain_supervisor.py:1505-1510`.
- **Action** : `_winner_for` essaie transport effectif → `_winner_any` → config.
- **Acceptation** : un winner écrit par `_TRANSPORT_OVERRIDE` est adopté.
- **Test** : `test_winner_for_falls_back_to_any_transport`.

### T004 — Guard AUTH local, borné, décroissant (P0)
- **Fichiers** : `station_repair.py:290-345`, `vpn/manager.py:103,8483-8495`,
  `config.yaml:676`.
- **Action** : clé `(transport, scope)`, pause 30 s max 60 s, décroissance après
  2×pause sans échec, `clear()` sur tout 200 réel, WireGuard jamais gelé.
- **Acceptation** : AT-03 ; aucune pause > 60 s dans le log.
- **Test** : `tests/test_station_repair.py::test_auth_guard_is_transport_scoped_and_decays`.

### T005 — Un seul réparateur (P0)
- **Fichiers** : `free/pool/__init__.py` (`on_429_action`), `config.yaml:232`.
- **Action** : sur 429, enqueue une demande prioritaire et ne jamais recréer la
  station ; `repair_owner=supervisor` est une porte dure.
- **Acceptation** : un 429 produit exactement une demande `repair_requests`.
- **Test** : `tests/test_pool_repair_ownership.py::test_429_enqueues_only`.

### T006 — Ledger sans résurrection (P0)
- **Fichiers** : `free/ip_registry/registry_db.py:111-213`,
  `free/ip_quota.py`, `free/quota_ledger.py`.
- **Action** : `assign_observed` exige une preuve fraîche ; aucun `reset_at=NULL`
  sans `proven_at > last_429_at` ; `unknown` 429 → 60–120 s ; `max_block_s` ne
  tronque plus le reset fournisseur.
- **Acceptation** : AT-05 ; une IP épuisée n'est pas réassignée avant reset.
- **Test** : `test_registry_db_runtime.py::test_exhausted_ip_cannot_be_assigned_without_proof`.

### T007 — Config sanity (P0)
- **Fichiers** : `config.yaml:1270-1279`, `scripts/config_audit.py` (ou
  équivalent).
- **Action** : `no_egress_backoff_max_s >= no_egress_backoff_s` ;
  `auth_fallback_transport: wireguard` ; câbler ou supprimer
  `transport_cycle_timeout_s`, `transport_auth_backoff_s`,
  `auth_global_pause_s`.
- **Acceptation** : `config_audit --strict` = 0 dead / 0 missing ; test de
  cohérence des bornes.
- **Test** : `tests/test_config_sanity.py::test_backoff_max_ge_base`.

### T008 — Budget de probes (P0)
- **Fichiers** : `scripts/fresh_chain.py:1628-1644,2034-2064`.
- **Action** : liveness zéro quota ; confirmation seulement après un premier
  200 ; max 2 modèles de secours ; budget quotidien par IP.
- **Acceptation** : SC-006 ; un candidat ne consomme pas plus de 3 requêtes
  avant son premier 200.
- **Test** : `tests/test_fresh_chain_candidates.py::test_probe_budget_per_candidate`.

### T009 — Fail-closed sur les guards de flotte (P0)
- **Fichiers** : `scripts/fresh_chain_supervisor.py:563-576,1240-1253`.
- **Action** : si `/api/vpn-status` échoue, retenter puis refuser le bind ;
  jamais ∅ silencieux.
- **Acceptation** : aucune IP dupliquée dans un test de panne d'API.
- **Test** : `test_supervisor_fails_closed_on_status_error`.

### T009B — Rotation immédiate par station (P0)
- **Fichiers** : `scripts/fresh_chain_supervisor.py` (`_needs_chain`,
  `_supervise`), `free/repair_queue.py`, `free/station_health.py`.
- **Action** : 2 probes négatives ou un 429 bloquant → `draining` +
  `enqueue_repair(priority)` ; budget de progression 12 binds/300 s ; plus
  d'attente du cap 1200 s ; une station down n'attend pas les autres.
- **Acceptation** : station down → chaîne démarrée ≤ 5 s ; aucune famine parmi
  les six ; aucune station 7.
- **Test** : `tests/test_supervisor_immediate_rotation.py`.

### T010 — Locks et PID atomiques (P1)
- **Fichiers** : `free/chain_repair.py:77-80`,
  `scripts/fresh_chain_supervisor.py:1275-1278`.
- **Action** : `O_EXCL` + `os.replace`, vérifier PID + cmdline, `_stop_chain`
  vérifie le propriétaire.
- **Acceptation** : deux acquisitions concurrentes → un seul gagnant.
- **Test** : `tests/test_fresh_chain_lock.py::test_lock_is_atomic`.

---

## Phase 1 — Indépendance des transports (S1)

### T011 — Module `transport_ladder` (P1)
- **Fichiers** : `free/transport_ladder.py` (nouveau).
- **Action** : domaine pur : `attempts`, `budget`, `on_result`, `breaker`.
- **Acceptation** : table de vérité complète pour `auth/no_egress/bind_error/
  timeout/quota/host_gone`.
- **Test** : `tests/test_transport_ladder.py` (≥ 20 cas).

### T012 — Brancher le ladder dans `fresh_chain` (P1)
- **Fichiers** : `scripts/fresh_chain.py:1874-1941,2109-2124`.
- **Action** : un hostname parcourt WG → UDP → TCP → WG, budget par transport,
  `quota` passe au candidat suivant, `auth` n'ouvre que le breaker OVPN.
- **Acceptation** : AT-01, AT-02 ; un host échoué sur les trois transports est
  en backoff, pas mort.
- **Test** : `tests/test_fresh_chain_transport_ladder.py`.

### T013 — Brancher le ladder dans le manager (P1)
- **Fichiers** : `vpn/manager.py:7054-7212,7251-7274`.
- **Action** : permettre un flip WG → OVPN borné même avec `vpn_stack:
  wireguard` ; utiliser le même module, pas une copie.
- **Acceptation** : un tunnel WG mort bascule OVPN en ≤ 60 s ; retour WG après
  guérison.
- **Test** : `tests/test_manager_transport_flip.py`.

### T014 — Circuit breaker par `(station, transport)` (P1)
- **Fichiers** : `free/transport_ladder.py`, `vpn/auth.py`,
  `scripts/fresh_chain.py`.
- **Action** : open/half-open/closed, backoff 30/60/120 s, métriques.
- **Acceptation** : un transport en breaker ouvert n'est pas retesté avant le
  délai ; les autres transports continuent.
- **Test** : `test_breaker_half_open_after_backoff`.

### T015 — Sérialiser les binds cross-process (P1)
- **Fichiers** : `free/catalog_claims.py:500-519`, `scripts/fresh_chain.py`,
  `free/ip_registry/adapters/compose_binder.py`.
- **Action** : lease DB unique, 12–15 s entre binds, 30 s par station, canary
  compté.
- **Acceptation** : six chaînes parallèles ne dépassent jamais six sessions.
- **Test** : `tests/test_bind_budget.py::test_six_sessions_max`.

### T016 — Supprimer le gel global résiduel (P1)
- **Fichiers** : `scripts/fresh_chain_supervisor.py:641-652,1986-1994`.
- **Action** : la pause OpenVPN ne réduit plus `max_parallel` global ; elle
  réduit seulement les starts OpenVPN ; le stagger 60 s ne s'applique qu'aux
  transports concernés.
- **Acceptation** : AT-03 ; six stations réparables pendant une pause OVPN.
- **Test** : `test_supervisor_keeps_wireguard_repairs_during_auth_pause`.

---

## Phase 2 — Warm pool et cutover rapide (S2)

### T017 — Service `standby` (P1)
- **Fichiers** : `free/standby.py` (nouveau), `free/warm_pool.py`.
- **Action** : `reserve/take/promote/replenish` au-dessus de `warm_pool` ;
  claim atomique, TTL, score. Le service ne gère que des **IP spares** pour les
  six stations, jamais des stations.
- **Acceptation** : N+2 IP spares maintenues ; un take concurrent ne donne qu'un
  seul gagnant ; aucune station 7.
- **Test** : `tests/test_standby.py`.

### T018 — Qualifier les spares sans brûler le quota (P1)
- **Fichiers** : `free/standby.py`, `free/qualification/sweep.py`,
  `free/ip_registry/adapters/probe.py`.
- **Action** : liveness zéro quota, un seul 200 de promotion, pas de double
  sweep ; les spares sont des IP, pas des stations.
- **Acceptation** : un spare coûte ≤ 2 requêtes ; SC-006.
- **Test** : `test_spare_costs_at_most_two_requests`.

### T019 — Cutover make-before-break (P1)
- **Fichiers** : `free/standby.py`, `free/pool/__init__.py`,
  `scripts/fresh_chain_supervisor.py`.
- **Action** : claim → bind → egress → 200 → switch → release ; jamais libérer
  l'ancienne IP avant le 200.
- **Acceptation** : AT-04, AT-06 ; cutover p95 ≤ 20 s.
- **Test** : `test_cutover_never_releases_before_proof`.

### T020 — Superviseur : poll 5 s, priorité, 3 slots (P1)
- **Fichiers** : `scripts/fresh_chain_supervisor.py`, `free/repair_queue.py`.
- **Action** : poll 5 s, priorité serving > down > refresh, `max_parallel=3`,
  budget de progression 12 binds/300 s ; les six stations seulement, aucune
  station 7, aucune famine.
- **Acceptation** : 0/6 → 6/6 en ≤ 2 min sur fixture ; pas de monopolisation ;
  chaque station down reçoit un slot en ≤ 5 s.
- **Test** : `test_supervisor_recovers_six_stations_with_three_slots`.

### T021 — Chemin requête non bloquant (P1)
- **Fichiers** : `free/pool/__init__.py:6327-6362`.
- **Action** : standby immédiat ; sinon 503 + `Retry-After` ≤ 2 s ; jamais 5 s
  d'attente d'une rotation superviseur.
- **Acceptation** : une requête ne bloque jamais plus de 2 s sur une rotation.
- **Test** : `test_request_path_returns_503_with_retry_after`.

### T022 — Claims et leases atomiques (P1)
- **Fichiers** : `free/ip_registry/registry_db.py:111-213`,
  `scripts/fresh_chain.py:1452-1528`.
- **Action** : transaction unique, renewal à chaque poll, index unique.
- **Acceptation** : deux writers → un seul lease ; pas d'`IntegrityError` après
  écriture partielle.
- **Test** : `test_concurrent_assign_has_one_winner`.

### T023 — États temporels wall-clock (P1)
- **Fichiers** : `free/ip_quota.py:452-486`, `free/station_health.py:326-351`.
- **Action** : migrer `blocked_until/parked_until/last_ok_at` en epoch ; au
  boot, invalider si l'ancienne valeur est incohérente.
- **Acceptation** : AT-09 ; aucun état fantôme après reboot.
- **Test** : `test_reboot_clears_monotonic_state`.

### T024 — `/24` et `/64` (P2)
- **Fichiers** : `scripts/fresh_chain.py:1030-1038`,
  `free/quota_ledger.py`.
- **Action** : corrélation pour l'ordre et l'alerte ; pas de porte dure sans
  preuve ; filtre du ledger par jour/bucket.
- **Acceptation** : une rafale sur un /24 épuisé est détectée et évitée.
- **Test** : `test_slash24_correlation_prefers_other_blocks`.

---

## Phase 3 — WARP et proxylist en réserve (S3)

### T025 — Catalogue WARP live (P1)
- **Fichiers** : `free/egress_runtime.py:258-277,405`,
  `free/egress_adapters/warp.py:31-42`.
- **Action** : lire l'état des conteneurs à chaque cycle ; `healthy` = probe
  réelle ; rotation visible sans restart.
- **Acceptation** : une rotation WARP change l'IP dans l'API en ≤ 30 s.
- **Test** : `test_warp_catalog_reflects_rotation`.

### T026 — Probe par famille (P1)
- **Fichiers** : `opencode.py:3094-3140`, `free/egress_adapters/warp.py:83-87`.
- **Action** : forcer IPv4/IPv6 ; un 200 IPv4 ne promeut pas une ligne IPv6.
- **Acceptation** : AT WARP famille ; pas de faux vert.
- **Test** : `test_warp_family_proof_is_strict`.

### T027 — WARP healthcheck et rotation (P1)
- **Fichiers** : `free/warp_rotation.py:277,618-634`.
- **Action** : probe actif avec deadline si pas de `HEALTHCHECK` ; lock réduit à
  resolve/apply ; budget.
- **Acceptation** : une rotation sans healthcheck termine ≤ 90 s.
- **Test** : `test_warp_rotation_without_healthcheck`.

### T028 — Proxylist : infra vs dead (P1)
- **Fichiers** : `free/egress_adapters/public.py:74-83,511-517`.
- **Action** : erreurs locales → `infra` court ; `dead` long seulement sur
  preuve de non-fonctionnement.
- **Acceptation** : un worker mort n'évince pas 7 jours de proxys sains.
- **Test** : `test_public_infra_failure_does_not_mark_dead`.

### T029 — Denylist sur l'IP d'egress (P1)
- **Fichiers** : `free/egress_adapters/public.py:474-481,549-562`.
- **Action** : filtrer `outcome.egress_ip` avant promotion.
- **Acceptation** : une IP denylistée n'est jamais promue.
- **Test** : `test_denylist_applies_to_egress_ip`.

### T030 — Sources et workers stables (P2)
- **Fichiers** : `free/egress_sources.py`, `free/egress_store.py`,
  `free/pool/__init__.py:4258-4284`.
- **Action** : persister `last_error`, purger les sources retirées, ordre stable
  des workers.
- **Acceptation** : zéro restart de worker sur un cycle sans changement.
- **Test** : `test_worker_signature_is_order_stable`.

### T031 — Réserve WARP/proxys (P1)
- **Fichiers** : `free/pool/__init__.py:4903-5068`, `config.yaml:137-149`.
- **Action** : poids 0 en standby quand 6/6 NordVPN sain ; capacité réservée ;
  bascule en incident. WARP/proxys ne remplacent jamais une station et ne sont
  jamais comptés dans le 6/6.
- **Acceptation** : AT-08 ; 15 min sans NordVPN, service maintenu, 6/6 stations
  explicitement 0/6 avec cause `provider_down`.
- **Test** : `test_standby_reserve_is_not_consumed_when_healthy`.

---

## Phase 4 — SLO, alertes, crash (S4)

### T032 — `availability.py` complet (P1)
- **Fichiers** : `free/availability.py` (nouveau).
- **Action** : fenêtres 1 h/24 h/7 j/30 j, burn rate, budget, raw samples.
- **Acceptation** : SC-001/SC-002 ; calcul indépendant vérifié sur fixture.
- **Test** : `tests/test_availability.py` (≥ 15 cas).

### T033 — Un seul journal d'incidents (P1)
- **Fichiers** : `free/qualification/watchdog.py`, `observability/incidents.py`,
  `scripts/fresh_chain_supervisor.py`.
- **Action** : `registry.db.incidents` unique ; migration de
  `station_incidents.db` ; une seule ouverture/fermeture.
- **Acceptation** : un incident = une ligne, pas deux vérités.
- **Test** : `test_single_incident_journal`.

### T034 — API et métriques (P1)
- **Fichiers** : `dashboard/routers/qualification_routes.py`,
  `observability/metrics.py`, `opencode.py`.
- **Action** : `/api/availability`, `availability_1h/24h`, `six_of_six`,
  `cutover_p95`, `standby_size`.
- **Acceptation** : les valeurs de l'API = calcul des bruts.
- **Test** : `test_availability_api_matches_raw`.

### T035 — Alertes burn rate (P1)
- **Fichiers** : `scripts/vpn_alerts.py`, `docker-compose.yml:684-704`.
- **Action** : burn > 2× sur 1 h, 0/6 > 60 s, standby vide, pause > 60 s ;
  activer par défaut ou intégrer au superviseur ; corriger `pool_floor`.
- **Acceptation** : une coupure injectée déclenche l'alerte en ≤ 60 s.
- **Test** : `test_alert_on_slo_burn`.

### T036 — Dashboard (P2)
- **Fichiers** : `dashboard-ui/src/pages/vpn/QualificationStatus.svelte`,
  `VpnStatus.svelte`, `VpnCapacity.svelte`.
- **Action** : station, IP, transport, dernier 200, incident, SLO 1 h/24 h/7 j/
  30 j, cutover.
- **Acceptation** : zéro valeur « connecté » présentée comme usable.
- **Test** : `npm run check` + test de contrat API.

### T037 — Crash/reboot (P1)
- **Fichiers** : `opencode.py` (boot), `free/ip_registry/registry_db.py`,
  `free/chain_repair.py`.
- **Action** : reconcile leases, wall-clock, locks atomiques, warm pool
  restauré.
- **Acceptation** : AT-09 ; 0 lease orphelin après reboot.
- **Test** : `test_boot_reconcile_restores_leases`.

### T038 — Aligner config, docs, DEBT (P1)
- **Fichiers** : `config.yaml`, `docs/DEBT.md`, `docs/runbooks/egress-capacity.md`,
  `CHANGELOG`.
- **Action** : cible 99,99 %, `daemon_max_hosts`, `station_repair.enabled`,
  `repair_owner`, `rotation_planner`, option-map.
- **Acceptation** : `config_audit --strict` et `test_option_map.py` verts.
- **Test** : gates.

---

## Phase 5 — Chaos, soak, release (S5)

### T039 — Suite chaos (P1)
- **Fichiers** : `scripts/chaos_egress.py` (nouveau), `tests/chaos/`.
- **Action** : AT-01 à AT-11 automatisés, avec exactement six stations et
  vérification qu'aucune septième n'apparaît.
- **Acceptation** : tous verts en local et CI nightly.
- **Test** : `pytest tests/chaos -q`.

### T040 — Soak 24 h/72 h (P1)
- **Fichiers** : `scripts/soak_egress.py` (nouveau), rapport.
- **Action** : 6 stations, trafic synthétique, mesures SLO, incidents.
- **Acceptation** : SC-007 ; 72 h sans P0.
- **Test** : rapport signé.

### T041 — Gates et CI (P1)
- **Fichiers** : `.github/workflows/ci.yml`, `scripts/platform_audit.py`.
- **Action** : pytest, ruff, config_audit, platform_audit, option-map,
  Windows/Linux.
- **Acceptation** : CI verte sur les deux OS.
- **Test** : pipeline.

### T042 — Docs et release (P2)
- **Fichiers** : `docs/runbooks/egress-uptime.md`, `release-checklist.md`,
  `CHANGELOG`.
- **Action** : runbook incident, procédure de rollback, cibles SLO, preuves.
- **Acceptation** : release checklist complète ; rollback testé.

---

## Ordre d'exécution recommandé

1. **J0** : T000, T001, T002, T004, T005, T006, T007.
2. **J1** : T003, T008, T009, T009B, T010, puis validation 6/6 sur 1 h.
3. **S1** : T011 → T016.
4. **S2** : T017 → T023 (+ T024).
5. **S3** : T025 → T031.
6. **S4** : T032 → T038.
7. **S5** : T039 → T042.

## Statut Phase 0 — 2026-11-11 (commit `a3f6620`)

Implémenté et testé (pytest complet + ruff verts) :

- **T000** : `target_slots: 6`, `wg_canary_enabled: false`, clamp dur
  `MAX_STATIONS=6` dans `_scope()` ; le warm pool ne crée que des IP.
- **T002** : `WINNER_PROTECT_S=180` ; `_clear_stale_pins` et la boucle de
  persistance conservent un winner frais pendant l'adoption par le manager.
- **T003** : `_winner_for` retombe sur `_winner_any` après le transport effectif.
- **T004** : `GlobalAuthGuard` à clés `(transport, scope)`, décroissance
  `decay_s`, pause plafonnée à 60 s, `clear(transport=...)` sur un 200 réel.
- **T005** : `repair_owner=supervisor` reste une porte dure (429 délégué).
- **T007** : `no_egress_backoff_max_s=1800`, `auth_fallback_transport=wireguard`,
  `auth_global_pause_s` câblé via `_configure_auth_guard()`.
- **T008** : `max_fallback_models: 2` ; trois modèles de probe configurés.
- **T009B** : `ALLOCATOR_ONLY_GRACE_S=5`, poll 5 s, stagger AUTH ramené à 5 s,
  `max_parallel_repairs=3`.

Reste à valider sur le live (gate Phase 0) :

- 6/6 `client_usable` ≥ 95 % sur 1 h. Bloqué structurellement par le
  `FreeUsageLimitError` (quota journalier par IP, reset ~00:00 UTC) : la
  majeure partie des IP NordVPN est déjà brûlée le jour même ; la rotation
  retrouve des IP fraîches mais lentement (une par station à la fois).
- Mesure du temps « station down → chaîne démarrée » sur au moins 20
  événements pour confirmer ≤ 5 s.

T006 (ledger sans résurrection côté `registry_db`), T001 (échantillonneur SLO)
et T009 (fail-closed `/api/vpn-status`) restent à faire : le superviseur
proof-gate déjà ses assignations, mais le ledger et l'API-status n'ont pas
encore leurs tests dédiés.

## Gates de phase

- **Phase 0** : 0/6 < 2 min ; 6/6 ≥ 95 % sur 1 h ; aucune pause > 60 s ;
  exactement 6 stations (`station_count=6`, `target_slots=6`, aucune 7e) ;
  station down → rotation démarrée ≤ 5 s.
- **Phase 1** : AT-01/02/03 ; 6/6 ≥ 99 % sur 24 h.
- **Phase 2** : AT-04/05/06/07 ; cutover p95 ≤ 20 s ; 6/6 ≥ 99,9 % sur 24 h.
- **Phase 3** : AT-08 ; service maintenu, 6/6 stations explicitement 0/6,
  aucune 7e station.
- **Phase 4** : AT-09/10 ; SLO vérifiable ; reboot propre.
- **Phase 5** : SC-001 à SC-008 ; 72 h sans P0.
