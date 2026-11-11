# Option map — Qualification exhaustive NordVPN

**Principe (constitution XII)** : aucune option GUI ou `config.yaml` n'est
décorative. Chaque ligne a un consommateur runtime, un effet observable et un
test. Une option sans ligne est un bug de gate.

## Portée du gate

Le gate `tests/test_option_map.py` couvre :

1. toutes les clés `egress_catalog.qualification.*` ;
2. les contrôles `data-option-map` des pages qualification ;
3. la présence réelle du test référencé (fichier + fonction).

Une exception ponctuelle se justifie par le statut `display-only` ou `planned`
(ticket daté). Aucune ligne `active` ne peut pointer un test absent. Les lignes
`planned` de ce document sont converties en `active` par T059/T060.

## Format

| Surface | Clé / sélecteur | Défaut | Consommateur | Effet observable | Test | Statut |
|---|---|---|---|---|---|---|
| config | `egress_catalog.qualification.enabled` | `true` | `free/qualification/runtime.py:build_qualification()` | construit ou non le service | `test_qualification_runtime.py::test_disabled_returns_none` | planned (T026) |
| config | `egress_catalog.qualification.shadow_mode` | `true` | `QualificationSweep.run_epoch()` | ingère et score sans binder | `test_qualification_sweep.py::test_shadow_never_binds` | planned (T022) |
| config | `egress_catalog.qualification.epoch_interval_s` | `86400` | `QualificationScheduler` | cadence d'une nouvelle époque | `test_qualification_runtime.py::test_epoch_interval` | planned (T026) |
| config | `egress_catalog.qualification.max_attempts_per_host_per_epoch` | `3` | `QualificationSweep.claim_next()` | un host est `skipped` après N essais | `test_qualification_sweep.py::test_max_attempts_skips` | planned (T022) |
| config | `egress_catalog.qualification.sweep_slice_size` | `500` | `QualificationSweep.run_epoch()` | borne les hostnames par slice | `test_qualification_sweep.py::test_slice_size` | planned (T022) |
| config | `egress_catalog.qualification.sweep_time_budget_s` | `1800` | `QualificationSweep.run_epoch()` | arrêt `partial` avec raison | `test_qualification_sweep.py::test_time_budget` | planned (T022) |
| config | `egress_catalog.qualification.max_active_stations` | `6` | `QualificationSweep.workers()` | nombre de workers/sessions | `test_qualification_sweep.py::test_six_workers_max` | planned (T064) |
| config | `egress_catalog.qualification.preflight_samples` | `3` | `free/qualification/preflight.py` | médiane RTT sur N échantillons | `test_qualification_preflight.py::test_median` | planned (T047) |
| config | `egress_catalog.qualification.preflight_timeout_s` | `5` | `PreflightProber` | timeout par échantillon | `test_qualification_preflight.py::test_timeout` | planned (T047) |
| config | `egress_catalog.qualification.latency_budget_ms` | `250` | `scoring.potential_score()` | normalise le RTT | `test_qualification_domain.py::test_latency_score` | planned (T006) |
| config | `egress_catalog.qualification.throughput_budget_mbps` | `50` | `scoring.measured_score()` | normalise le débit | `test_qualification_domain.py::test_throughput_score` | planned (T006) |
| config | `...qualification.weights.reliability` | `0.30` | `scoring.potential_score()` | poids du taux de 200 | `test_qualification_domain.py::test_weights_normalized` | planned (T006) |
| config | `...qualification.weights.latency` | `0.30` | `scoring.potential_score()` | poids du RTT | `test_qualification_domain.py::test_weights_normalized` | planned (T006) |
| config | `...qualification.weights.quota` | `0.20` | `scoring.potential_score()` | poids du quota restant | `test_qualification_domain.py::test_weights_normalized` | planned (T006) |
| config | `...qualification.weights.load` | `0.15` | `scoring.potential_score()` | poids de la charge API | `test_qualification_domain.py::test_weights_normalized` | planned (T006) |
| config | `...qualification.weights.freshness` | `0.05` | `scoring.potential_score()` | poids de la fraîcheur | `test_qualification_domain.py::test_weights_normalized` | planned (T006) |
| config | `egress_catalog.qualification.infra_backoff_s` | `300` | `domain.infra_backoff()` | délai avant retry infra | `test_qualification_domain.py::test_infra_backoff_is_bounded` | implemented (T039) |
| config | `egress_catalog.qualification.preflight_timeout_s` | `5` | `PreflightService` | timeout par échantillon | `test_qualification_runtime.py::test_runtime_builds_preflight_and_scheduler` | implemented (T047) |
| config | `egress_catalog.qualification.quota_retry_jitter_s` | `60` | `sweep.qualify()` | jitter déterministe après reset 429 | `test_qualification_sweep.py::test_quota_reset_uses_deterministic_jitter` | implemented (T031) |
| config | `egress_catalog.qualification.infra_backoff_max_s` | `3600` | `store.mark_infra()` | plafond du backoff | `test_qualification_store.py::test_infra_backoff_cap` | planned (T039) |
| config | `egress_catalog.qualification.quota_retry_jitter_s` | `60` | `store.mark_quota()` | jitter après reset | `test_qualification_store.py::test_reset_jitter` | planned (T031) |
| config | `egress_catalog.qualification.catalog_ttl_s` | `21600` | `source.NordVpnCatalogSource` | refresh API toutes les 6 h | `test_qualification_source.py::test_cache_ttl` | planned (T013) |
| config | `egress_catalog.qualification.retention_days` | `7` | `store.purge()` | purge des tentatives | `test_qualification_store.py::test_retention` | planned (T072) |
| config | `egress_catalog.qualification.promote_on_200` | `true` | `sweep.promote_usable()` | un 200 crée une option `usable` | `test_qualification_sweep.py::test_200_promotes` | planned (T030) |
| config | `egress_catalog.qualification.rotate_on_429` | `true` | `sweep.rotate_on_429()` | rotation immédiate | `test_qualification_rotation.py::test_rotate_on_429` | planned (T031) |
| config | `egress_catalog.qualification.priority` | `service>repair>sweep` | `QualificationSweep.run_epoch()` | le balayage cède au service | `test_qualification_sweep.py::test_service_priority` | planned (T064) |
| config | `egress_catalog.qualification.dedicated_skip` | `true` | `source`/`sweep` | dédiés `skipped` | `test_qualification_source.py::test_dedicated_skipped` | planned (T043) |
| config | `egress_catalog.qualification.country_gate` | `false` | `store.claim_next()` | aucun pays en porte dure | `test_qualification_store.py::test_no_country_gate` | planned (T038) |
| config | `egress_catalog.qualification.max_queue_page` | `500` | `dashboard/api.py` | borne la pagination | `test_qualification_api.py::test_page_limit` | planned (T053) |
| config | `...qualification.per_station_workers` | `true` | `free/qualification/station.py` | un worker par station, pas de lock global | `test_qualification_station.py::test_parallel_workers` | planned (T070) |
| config | `...qualification.station_standby_size` | `1` | `station.py` | standby pré-qualifié par station | `test_qualification_cutover.py::test_standby_selected` | planned (T074) |
| config | `...qualification.proactive_rotation_headroom_pct` | `20` | `station.py` | rotation avant épuisement | `test_qualification_rotation.py::test_proactive_rotation` | planned (T074) |
| config | `...qualification.parallel_rotations` | `true` | `free/rotation_planner.py` | rotations concurrentes, rate limit seulement | `test_qualification_station.py::test_concurrent_rotations` | planned (T073) |
| config | `...qualification.global_pause_on_429` | `false` | `station.py` | aucun pause globale | `test_qualification_station.py::test_no_global_pause` | planned (T072) |
| config | `...qualification.last_station_protection` | `true` | `station.py` | jamais 0/6 par rotation | `test_qualification_station.py::test_last_station` | planned (T078) |
| config | `...qualification.station_availability_target_pct` | `99` | `station.py` | objectif par station | `test_qualification_station.py::test_availability_target` | planned (T082) |
| config | `...qualification.cutover_timeout_ms` | `5000` | `free/qualification/cutover.py` | borne le cutover | `test_qualification_cutover.py::test_timeout` | planned (T075) |
| config | `...qualification.cutover_target_p95_ms` | `2000` | `cutover.py` | cible p95 mesurée | `test_qualification_cutover.py::test_p95` | planned (T076) |
| config | `...qualification.allow_zero_downtime_cutover` | `true` | `cutover.py` | second tunnel si supporté | `test_qualification_cutover.py::test_zero_downtime` | planned (T076) |
| config | `...qualification.transport_strategy` | `auto` | `free/qualification/transport.py` | `auto` cycle, `manual` fixe | `test_qualification_transport.py::test_strategy` | planned (T085) |
| config | `...qualification.transport_order` | `["wireguard","openvpn-udp","openvpn-tcp"]` | `transport.py` | ordre du cycle | `test_qualification_transport.py::test_order` | planned (T085) |
| config | `...qualification.transport_retry_rounds` | `2` | `transport.py` | reboucle WG après échec | `test_qualification_transport.py::test_rounds` | planned (T089) |
| config | `...qualification.transport_retry_backoff_s` | `60` | `transport.py` | backoff entre rounds | `test_qualification_transport.py::test_backoff` | planned (T089) |
| config | `...qualification.transport_health_min_attempts` | `100` | `transport_health.py` | seuil `degraded` | `test_qualification_readmodel.py::test_transport_degraded_after_min_attempts_without_success` | implemented (T091) |
| config | `...qualification.auth_backoff_s` | `3600` | `free/qualification/sweep.py` | AUTH station-local, hostname pending | `test_qualification_sweep.py::test_auth_stops_without_host_death` | implemented (T077) |
| config | `...qualification.daemon_enabled` | `false` | `scripts/fresh_chain_supervisor.py` | cycle qualification optionnel | `test_qualification_daemon_control.py::test_daemon_once_command_shape` | implemented (T066) |
| config | `...qualification.daemon_interval_s` | `900` | `scripts/fresh_chain_supervisor.py` | cadence du cycle | `test_qualification_daemon_control.py::test_daemon_live_command_carries_paths` | implemented (T066) |
| config | `...qualification.watchdog_enabled` | `true` | `free/qualification/watchdog.py` | watchdog par station | `test_qualification_watchdog.py::test_enabled` | planned (T100) |
| config | `...qualification.watchdog_probe_interval_s` | `30` | `watchdog.py` | cadence des sondes | `test_qualification_watchdog.py::test_interval` | planned (T100) |
| config | `...qualification.max_repair_attempts` | `3` | `free/qualification/repair.py` | escalade après N | `test_qualification_repair.py::test_escalation` | planned (T105) |
| config | `...qualification.repair_backoff_s` | `300` | `free/qualification/watchdog.py` | pas de réparation en boucle | `test_qualification_watchdog.py::test_repair_backoff_skips_repeated_repairs` | implemented (T118) |
| config | `...qualification.watchdog_enabled` | `true` | `free/qualification/runtime.py` | pas de sondes sans adaptateur | `test_qualification_watchdog.py::test_missing_probe_is_disabled_not_unhealthy` | implemented (T118) |
| config | `...qualification.watchdog_probe_interval_s` | `30` | `free/qualification/scheduler.py` | cadence des sondes | `test_qualification_scheduler.py::test_watchdog_interval_is_respected` | implemented (T118) |
| config | `...qualification.proactive_rotation_headroom_pct` | `20` | `dashboard/routers/qualification_routes.py` | seuil de rotation proactive | `test_qualification_api.py::test_rotate_headroom_config_changes_decision` | implemented (T083) |
| config | `...qualification.last_station_protection` | `true` | `free/qualification/rotation.py` | jamais 0/6 | `test_qualification_rotation.py::test_last_station_is_protected_without_standby` | implemented (T083) |
| config | `...qualification.cutover_timeout_ms` | `5000` | `free/qualification/cutover.py` | borne prepare/switch/probe/rollback | `test_qualification_cutover.py::test_cutover_timeout_bounds_prepare` | implemented (T083) |
| config | `...qualification.per_station_workers` | `true` | `free/qualification/runtime.py` | un sweep par station | `test_qualification_runtime.py::test_runtime_scheduler_builds_per_station_workers` | implemented (T083) |
| config | `...qualification.parallel_rotations` | `true` | `free/qualification/rotation_batch.py` | rotations concurrentes bornées | `test_qualification_rotation_batch.py::test_build_batch_reads_parallel_rotations` | implemented (T083) |
| config | `...qualification.station_standby_size` | `1` | `free/qualification/rotation.py` | standby promu seulement si taille > 0 | `test_qualification_rotation.py::test_zero_standby_size_ignores_standby` | implemented (T083) |
| config | `...qualification.global_pause_on_429` | `false` | `free/qualification/scheduler.py` | pause globale opt-in only | `test_qualification_scheduler.py::test_global_pause_on_429_is_opt_in` | implemented (T083) |
| config | `...qualification.repair_backoff_s` | `300` | `repair.py` | backoff de réparation | `test_qualification_repair.py::test_backoff` | planned (T105) |
| config | `...qualification.discovery_interval_s` | `21600` | `free/qualification/discovery.py` | refresh API 6 h | `test_qualification_discovery.py::test_interval` | planned (T107) |
| config | `...qualification.discovery_stale_max_s` | `86400` | `discovery.py` | au-delà, `stale` explicite | `test_qualification_discovery.py::test_stale` | planned (T107) |
| config | `...qualification.repair_queue_authority` | `registry.db` | `repair.py` | pas de base séparée | `test_qualification_repair.py::test_single_authority` | planned (T103) |
| GUI | `qualification-stations` | — | `GET /api/egress/qualification/stations` | état/IP/standby/cutover | `test_option_map.py::test_gui_controls_have_a_contract` | planned (T112) |
| GUI | `qualification-watchdog` | — | `GET /api/egress/qualification/watchdog` | heartbeat/incidents | `test_option_map.py::test_gui_controls_have_a_contract` | planned (T112) |
| GUI | `qualification-discovery` | — | `GET /api/egress/qualification/discovery` | sources/runs/stale | `test_option_map.py::test_gui_controls_have_a_contract` | planned (T112) |
| GUI | `qualification-repairs` | — | `GET /api/egress/qualification/repairs` | jobs/attempts | `test_option_map.py::test_gui_controls_have_a_contract` | planned (T112) |
| config | `egress_catalog.qualification.shadow_mode` | `true` | `runtime` | aucun bind en shadow | `test_qualification_sweep.py::test_shadow_never_binds` | planned (T021) |
| config | `egress_catalog.qualification.threat_feeds` | hérité | `ThreatIntel` | denylist avant bind | `test_qualification_sweep.py::test_denylist_before_bind` | planned (T036) |
| GUI | `qualification-start` | — | `POST /api/egress/qualification/epochs` | démarre une époque | `test_qualification_api.py::test_start_epoch_shadow_and_conflict` | implemented (T055) |
| GUI | `qualification-pause` | — | `POST /api/egress/qualification/epochs/{id}/pause` | met en pause | `test_option_map.py::test_gui_controls_have_a_contract` | planned (T055) |
| GUI | `qualification-resume` | — | `POST /api/egress/qualification/epochs/{id}/resume` | reprend au curseur | `test_option_map.py::test_gui_controls_have_a_contract` | planned (T055) |
| GUI | `qualification-next` | — | `GET /api/egress/qualification/next` | top 20 candidats | `test_option_map.py::test_gui_controls_have_a_contract` | planned (T055) |
| GUI | `qualification-coverage` | — | `GET /api/egress/qualification/summary` | testés/total, 200/429/infra | `test_option_map.py::test_gui_controls_have_a_contract` | planned (T055) |
| GUI | `qualification-export` | — | `GET /api/egress/qualification/attempts?format=csv` | export CSV | `test_option_map.py::test_gui_controls_have_a_contract` | planned (T055) |
| GUI | `qualification-rotate` | — | `POST /api/egress/qualification/rotate` | rotation manuelle tracée | `test_option_map.py::test_gui_controls_have_a_contract` | planned (T055) |

## Règles de gate

1. Toute clé `egress_catalog.qualification.*` sans ligne échoue.
2. Toute ligne `active` sans test existant échoue.
3. Toute ligne `planned` doit avoir une date/tâche ; au-delà de la date, elle
   échoue.
4. Toute option GUI doit avoir un `data-option-map` et un consommateur.
5. `display-only` est interdit pour une option qui modifie le balayage ; une
   option d'affichage doit être explicitement marquée.

## Contrats de non-régression

- `egress_catalog.qualification.country_gate` doit rester `false` ; un test
  échoue si `country` apparaît dans une clause de porte dure.
- `promote_on_200` et `rotate_on_429` ne peuvent pas être désactivés sans
  documenter la pénurie ; un test vérifie l'état exposé.
- `max_active_stations` ne peut pas dépasser 6 ; un test échoue au-delà.
- `shadow_mode` doit être `true` par défaut au premier démarrage.
