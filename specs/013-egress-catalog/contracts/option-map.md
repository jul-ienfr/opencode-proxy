# Option map — Catalogue d'egress

**Principe (constitution XII/XVII)** : aucune option GUI ou `config.yaml`
n'est décorative. Chaque ligne a un consommateur runtime, un effet observable
et un test. Une option sans ligne est un bug de gate.

## Portée du gate

Le gate `tests/test_option_map.py` couvre :

1. toutes les clés `egress_catalog.*` ;
2. toutes les clés `ip_rotation.public_proxies.*` ;
3. toutes les clés `ip_rotation.egress_adaptive.*` ;
4. les contrôles `data-option-map` des pages catalogue ;
5. la présence réelle du test référencé (fichier + fonction).

Une exception ponctuelle se justifie par le statut `display-only` (affichage
sans effet) ou `planned` (ticket daté). Aucune ligne `active` ne peut pointer
un test absent.

## Format

| Surface | Clé / sélecteur | Défaut | Consommateur | Effet observable | Test | Statut |
|---|---|---|---|---|---|---|
| config | `egress_catalog.enabled` | `true` | `free/egress_runtime.py:build_runtime()` | runtime construit ou `None` | `test_egress_runtime.py::test_disabled_catalog_returns_none` | active |
| config | `egress_catalog.update_jitter_pct` | `15` | `free/egress_scheduler.py:EgressScheduler()` | délai du prochain run varie | `test_egress_scheduler.py::test_next_run_uses_jitter_and_never_goes_below_interval` | active |
| config | `egress_catalog.retention_days` | `7` | `free/egress_store.py:EgressStore.purge()` | observations anciennes purgées | `test_egress_store.py::test_retention_purges_old_observations_and_dead_options` | active |
| config | `egress_catalog.cycle_candidate_budget` | `500` | `PublicEgressAdapter.cycle_budget` | borne les candidats testés par cycle | `test_egress_public_adapter.py::test_default_cycle_budget_bounds_the_cycle` | active |
| config | `egress_catalog.cycle_timeout_s` | `600` | `PublicEgressAdapter.cycle_timeout_s` | borne la durée d'un cycle public | `test_egress_public_adapter.py::test_cycle_timeout_is_bounded` | active |
| config | `egress_catalog.denylist` | `[]` | `PublicEgressAdapter._is_denied()` | aucun candidat listé n'est contacté | `test_egress_public_adapter.py::test_denylisted_candidate_is_never_probed` | active |
| config | `egress_catalog.threat_feeds.enabled` | `true` | `free/egress_runtime.py:build_runtime()` | construit `ThreatIntel` et sa boucle | `test_egress_runtime.py::test_threat_feeds_enabled_builds_runtime` | active |
| config | `egress_catalog.threat_feeds.refresh_interval_s` | `21600` | `ThreatIntel.run_forever()` | cadence de rafraîchissement des flux | `test_egress_runtime.py::test_threat_feeds_enabled_builds_runtime` | active |
| config | `egress_catalog.threat_feeds.urls[].id` | — | `ThreatIntel.feeds` | identifiant de flux | `test_threat_intel.py::test_feed_config_is_parsed` | active |
| config | `egress_catalog.threat_feeds.urls[].url` | — | `ThreatIntel.refresh_once()` | source téléchargée | `test_threat_intel.py::test_feed_config_is_parsed` | active |
| config | `egress_catalog.threat_feeds.urls[].enabled` | `true` | `ThreatIntel.__init__()` | flux inclus/exclu | `test_threat_intel.py::test_feed_config_is_parsed` | active |
| config | `...public.enabled` | `true` | `free/egress_runtime.py:_enabled()` | adapter public construit ou non | `test_egress_runtime.py::test_disabled_provider_is_not_built` | active |
| config | `...public.discovery_interval_s` | `21600` | `PublicEgressAdapter.discovery_interval_s` | sources re-téléchargées seulement après ce délai | `test_egress_runtime.py::test_public_discovery_is_cached_between_revalidations` | active |
| config | `...public.revalidate_interval_s` | `1800` | `build_runtime()` → intervalle scheduler | cadence de re-test des options actives | `test_egress_public_adapter.py::test_active_usable_options_are_revalidated_each_cycle` | active |
| config | `...public.max_active` | `64` | `egress_rotation.public_pool_rows()` | nombre d'endpoints publics actifs | `test_egress_rotation.py::test_public_pool_rows_are_ready_for_the_worker` | active |
| config | `...public.max_options` | `20000` | `PublicEgressAdapter.refresh()` | borne les candidats testés | `test_egress_public_adapter.py::test_max_candidates_bounds_the_cycle` | active |
| config | `...public.concurrency` | `24` | `PublicEgressAdapter.concurrency` | parallélisme des probes | `test_egress_catalog.py::test_policy_reads_config_and_clamps` | active |
| config | `...public.ttl_usable_s` | `43200` | `egress_catalog.ttl_for()` | une option `usable` expire après 12 h | `test_egress_catalog.py::test_200_makes_the_option_usable_with_a_ttl` | active |
| config | `...public.ttl_quota_max_s` | `86400` | `egress_catalog.ttl_for()` | plafond de quarantaine 429 | `test_egress_catalog.py::test_429_is_quota_with_the_provider_reset_not_dead` | active |
| config | `...public.ttl_dead_s` | `604800` | `egress_catalog.ttl_for()` | TTL d'un proxy mort | `test_egress_catalog.py::test_timeout_is_dead_with_a_short_lived_ttl` | active |
| config | `...public.sources_file` | `config/egress_sources.yaml` | `egress_sources.load_source_file()` | catalogue de sources externe chargé | `test_egress_runtime.py::test_sources_file_is_loaded` | active |
| config | `...warp.enabled` | `true` | `free/egress_runtime.py:_enabled()` | adapter WARP construit ou non | `test_egress_runtime.py::test_disabled_provider_is_not_built` | active |
| config | `...warp.discovery_interval_s` | `1800` | `build_runtime()` → intervalle scheduler | cadence WARP | `test_egress_runtime.py::test_enabled_catalog_builds_scheduler_with_configured_intervals` | active |
| config | `...warp.max_active` | `24` | `egress_rotation.select_many()` | nombre de `/64` proposés | `test_egress_rotation.py::test_select_many_returns_distinct_blocks_only` | active |
| config | `...warp.max_options` | `24` | `WarpEgressAdapter.refresh()` | borne les conteneurs observés | `test_egress_warp_adapter.py::test_two_blocks_one_usable_one_quota` | active |
| config | `...warp.concurrency` | `6` | `WarpEgressAdapter` policy | parallélisme des probes | `test_egress_catalog.py::test_policy_reads_config_and_clamps` | active |
| config | `...warp.ttl_usable_s` | `43200` | `egress_catalog.ttl_for()` | TTL d'un `/64` | `test_egress_catalog.py::test_200_makes_the_option_usable_with_a_ttl` | active |
| config | `...warp.ttl_quota_max_s` | `86400` | `egress_catalog.ttl_for()` | plafond 429 WARP | `test_egress_catalog.py::test_429_is_quota_with_the_provider_reset_not_dead` | active |
| config | `...warp.ttl_dead_s` | `604800` | `egress_catalog.ttl_for()` | TTL d'un bloc mort | `test_egress_warp_adapter.py::test_recreated_container_expires_the_old_block` | active |
| config | `...nordvpn.enabled` | `true` | `free/egress_runtime.py:_enabled()` | adapter NordVPN construit ou non | `test_egress_runtime.py::test_disabled_provider_is_not_built` | active |
| config | `...nordvpn.discovery_interval_s` | `1800` | `build_runtime()` → intervalle scheduler | cadence NordVPN | `test_egress_runtime.py::test_enabled_catalog_builds_scheduler_with_configured_intervals` | active |
| config | `...nordvpn.max_active` | `6` | `NordVpnEgressAdapter.max_active_stations` | plafond de sessions | `test_egress_nordvpn_adapter.py::test_session_cap_observes_more_ips_than_it_probes` | active |
| config | `...nordvpn.max_options` | `10` | `NordVpnEgressAdapter.refresh()` | borne les IP connues | `test_egress_nordvpn_adapter.py::test_200_429_and_auth_are_distinct_states` | active |
| config | `...nordvpn.max_active_stations` | `6` | `build_runtime()` | plafond de sessions transmis | `test_egress_nordvpn_adapter.py::test_session_cap_observes_more_ips_than_it_probes` | active |
| config | `...nordvpn.concurrency` | `4` | `NordVpnEgressAdapter` policy | parallélisme des probes | `test_egress_catalog.py::test_policy_reads_config_and_clamps` | active |
| config | `...nordvpn.ttl_usable_s` | `43200` | `egress_catalog.ttl_for()` | TTL d'une IP NordVPN | `test_egress_catalog.py::test_200_makes_the_option_usable_with_a_ttl` | active |
| config | `...nordvpn.ttl_quota_max_s` | `86400` | `egress_catalog.ttl_for()` | plafond 429 NordVPN | `test_egress_catalog.py::test_429_is_quota_with_the_provider_reset_not_dead` | active |
| config | `...nordvpn.ttl_dead_s` | `604800` | `egress_catalog.ttl_for()` | TTL d'une IP morte | `test_egress_catalog.py::test_timeout_is_dead_with_a_short_lived_ttl` | active |
| config | `ip_rotation.public_proxies.enabled` | `false` | `FreeIPPool.configure_public_proxies` | worker/public pool actif | `test_public_proxies_pool.py::test_configure_disabled_clears_public_rows` | active |
| config | `ip_rotation.public_proxies.feed_path` | `working.jsonl` | `FreeIPPool._load_public_feed` | feed importé | `test_public_proxies_pool.py::test_configure_public_proxies_loads_feed` | active |
| config | `ip_rotation.public_proxies.max_active` | `64` | `FreeIPPool.set_public_proxies` | taille du pool public | `test_public_proxies_pool.py::test_set_public_proxies_dedupes_egress_and_builds_transport` | active |
| config | `ip_rotation.public_proxies.max_per_24` | `2` | `FreeIPPool._select_public_rows` | diversité /24 | `test_public_proxies_pool.py::test_public_proxies_cap_per_24` | active |
| config | `ip_rotation.public_proxies.isolation` | `process` | `FreeIPPool.set_public_proxies` | process vs container | `test_public_proxies_pool.py::test_set_public_proxies_can_use_container_isolation` | active |
| config | `ip_rotation.public_proxies.container_image` | digest | `start_worker_container` | image utilisée | `test_public_egress_container.py::test_container_command_is_locked_down` | active |
| config | `ip_rotation.public_proxies.refresh_interval_s` | `21600` | `FreeIPPool._public_refresh_loop` | cadence de revalidation | `test_public_proxies_pool.py::test_public_refresh_ticker_starts_and_stops` | active |
| config | `ip_rotation.public_proxies.free_probe_model` | `deepseek...` | `_revalidate_public_proxies` | modèle du gate free | `test_public_proxies_pool.py::test_opencode_public_validator_maps_statuses` | active |
| config | `ip_rotation.public_proxies.free_probe_concurrency` | `4` | `_revalidate_public_proxies` | parallélisme du gate | `test_public_proxies_pool.py::test_opencode_public_validator_maps_statuses` | active |
| config | `ip_rotation.egress_adaptive.enabled` | `true` | `FreeIPPool._select_egress_provider` | modulation 429 | `test_public_proxies_pool.py::test_adaptive_selection_avoids_blocked_provider` | active |
| config | `ip_rotation.egress_adaptive.window_s` | `300` | `FreeIPPool._adaptive_health` | fenêtre des 429 | `test_public_proxies_pool.py::test_adaptive_health_uses_provider_outcome_log` | active |
| config | `ip_rotation.egress_adaptive.min_samples` | `3` | `free/adaptive_weights.py` | seuil d'échantillons | `test_adaptive_weights.py::test_provider_without_samples_keeps_base_weight` | active |
| config | `ip_rotation.egress_adaptive.recent_429_penalty` | `0.25` | `free/adaptive_weights.py` | pénalité par 429 | `test_adaptive_weights.py::test_recent_429_reduces_the_provider_share` | active |
| config | `ip_rotation.egress_adaptive.min_weight_pct` | `2` | `free/adaptive_weights.py` | plancher de part | `test_adaptive_weights.py::test_parse_config_clamps_values` | active |
| GUI | `public-pool-toggle` | — | `PublicProxies.svelte:refresh()` | active/désactive le pool | `test_option_map.py::test_gui_controls_have_a_contract` | active |
| GUI | `public-pool-reload` | — | `PublicProxies.svelte:refresh()` | recharge le feed | `test_option_map.py::test_gui_controls_have_a_contract` | active |
| GUI | `public-pool-revalidate` | — | `PublicProxies.svelte:revalidate()` | met à jour les statuts | `test_option_map.py::test_gui_controls_have_a_contract` | active |
| GUI | `public-adaptive-card` | — | `GET /api/egress/adaptive` | base/effectif/429/bloqués | `test_option_map.py::test_gui_controls_have_a_contract` | active |
| GUI | `catalog-summary` | — | `GET /api/egress/catalog` | connues/utilisables/quota/auth/mortes/blocs/rotation/âge | `test_option_map.py::test_gui_controls_have_a_contract` | active |
| GUI | `catalog-refresh` | — | `POST /api/egress/catalog/refresh` | lance un cycle et rafraîchit la table | `test_option_map.py::test_gui_controls_have_a_contract` | active |
| GUI | `catalog-filter-provider` | — | `GET /api/egress/catalog/options?provider=` | filtre la table par provider | `test_option_map.py::test_gui_controls_have_a_contract` | active |
| GUI | `catalog-filter-status` | — | `GET /api/egress/catalog/options?status=` | filtre la table par statut | `test_option_map.py::test_gui_controls_have_a_contract` | active |
| GUI | `catalog-filter-kind` | — | `GET /api/egress/catalog/options?kind=` | filtre la table par protocole (https/socks5) | `test_option_map.py::test_gui_controls_have_a_contract` | active |
| GUI | `catalog-sources` | — | `GET /api/egress/catalog/sources` | liste les sources déclaratives | `test_option_map.py::test_gui_controls_have_a_contract` | active |
| GUI | `catalog-source-id` | — | `POST /api/egress/catalog/sources` | identifiant de la nouvelle source | `test_option_map.py::test_gui_controls_have_a_contract` | active |
| GUI | `catalog-source-type` | — | `POST /api/egress/catalog/sources` | parser de la nouvelle source | `test_option_map.py::test_gui_controls_have_a_contract` | active |
| GUI | `catalog-source-url` | — | `POST /api/egress/catalog/sources` | URL de la nouvelle source | `test_option_map.py::test_gui_controls_have_a_contract` | active |
| GUI | `catalog-source-add` | — | `POST /api/egress/catalog/sources` | enregistre la source sans redéploiement | `test_option_map.py::test_gui_controls_have_a_contract` | active |
| GUI | `catalog-threat-feeds` | — | `GET /api/egress/catalog` (`threat_intel`) | nombre d'IP/CIDR et état des flux | `test_option_map.py::test_gui_controls_have_a_contract` | active |
| GUI | `catalog-threat-refresh` | — | `POST /api/egress/catalog/threat-feeds/refresh` | recharge les flux et met à jour la denylist | `test_option_map.py::test_gui_controls_have_a_contract` | active |
| GUI | `catalog-block-ip` | — | `POST /api/egress/catalog/block` | retire une IP du catalogue et l'ajoute à la denylist | `test_option_map.py::test_gui_controls_have_a_contract` | active |

## Qualification 014

| Surface | Sélecteur | Défaut | Consommateur | Effet | Test | Statut |
|---|---|---|---|---|---|---|
| config | `egress_catalog.qualification.enabled` | `true` | `free/qualification/runtime.py` | active le control plane | `test_qualification_runtime.py::test_disabled_runtime_returns_none` | active |
| config | `egress_catalog.qualification.shadow_mode` | `false` | `free/qualification/sweep.py` | aucun bind en shadow | `test_qualification_sweep.py::test_run_epoch_shadow_releases_and_does_not_bind` | active |
| config | `egress_catalog.qualification.station` | `1` | `runtime.py` | station du balayage | `test_qualification_runtime.py::test_runtime_builds_ingests_and_summarizes` | active |
| config | `egress_catalog.qualification.max_hosts` | `200` | `sweep.run_epoch()` | borne le nombre de claims | `test_qualification_runtime.py::test_invalid_values_fall_back_to_defaults` | active |
| config | `egress_catalog.qualification.max_attempts_per_host_per_epoch` | `1` | `sweep.qualify()` | évite les boucles par host | `test_qualification_sweep.py::test_max_attempts_skips_without_binding` | active |
| config | `egress_catalog.qualification.lease_s` | `300` | `store.claim_next()` | bail de claim | `test_qualification_store.py::test_claim_skips_future_quota_reset_and_marks_claimed` | active |
| config | `egress_catalog.qualification.infra_backoff_s` | `300` | `domain.infra_backoff()` | délai avant retry infra | `test_qualification_domain.py::test_infra_backoff_is_bounded` | active |
| config | `egress_catalog.qualification.infra_backoff_max_s` | `3600` | `domain.infra_backoff()` | plafond du backoff | `test_qualification_domain.py::test_infra_backoff_is_bounded` | active |
| config | `egress_catalog.qualification.preflight_timeout_s` | `5` | `PreflightService` | timeout par échantillon | `test_qualification_runtime.py::test_runtime_builds_preflight_and_scheduler` | active |
| config | `egress_catalog.qualification.quota_retry_jitter_s` | `60` | `sweep.qualify()` | jitter déterministe après reset 429 | `test_qualification_sweep.py::test_quota_reset_uses_deterministic_jitter` | active |
| config | `egress_catalog.qualification.retention_days` | `7` | `store.purge_attempts()` | purge des historiques | `test_qualification_store.py::test_retention_purge_and_wal_checkpoint` | active |
| config | `egress_catalog.qualification.api_cache` | `logs/nordvpn_api.json` | `source.NordVpnCatalogSource` | cache API NordVPN | `test_qualification_source.py::test_parses_online_server_and_transports` | active |
| config | `egress_catalog.qualification.latency_budget_ms` | `250` | `scoring.py` | normalise le RTT | `test_qualification_domain.py::test_order_is_potential_quota_rtt_load_hostname` | active |
| config | `egress_catalog.qualification.quota_estimate` | `500` | `rotation.StationSnapshot` | headroom de quota | `test_qualification_rotation.py::test_headroom_percentage` | active |
| config | `egress_catalog.qualification.freshness_ttl_h` | `24` | `scoring.py` | poids de fraîcheur | `test_qualification_domain.py::test_order_is_potential_quota_rtt_load_hostname` | active |
| config | `egress_catalog.qualification.weights.reliability` | `0.30` | `scoring.potential_score()` | poids fiabilité | `test_qualification_domain.py::test_order_is_potential_quota_rtt_load_hostname` | active |
| config | `egress_catalog.qualification.weights.latency` | `0.30` | `scoring.potential_score()` | poids RTT | `test_qualification_domain.py::test_order_is_potential_quota_rtt_load_hostname` | active |
| config | `egress_catalog.qualification.weights.quota` | `0.20` | `scoring.potential_score()` | poids quota | `test_qualification_domain.py::test_order_is_potential_quota_rtt_load_hostname` | active |
| config | `egress_catalog.qualification.weights.load` | `0.15` | `scoring.potential_score()` | poids charge | `test_qualification_domain.py::test_order_is_potential_quota_rtt_load_hostname` | active |
| config | `egress_catalog.qualification.weights.freshness` | `0.05` | `scoring.potential_score()` | poids fraîcheur | `test_qualification_domain.py::test_order_is_potential_quota_rtt_load_hostname` | active |
| config | `egress_catalog.qualification.transport_strategy` | `auto` | `transport.build_plan()` | cycle auto ou manuel | `test_qualification_transport.py::test_manual_uses_only_chosen_transport` | active |
| config | `egress_catalog.qualification.transport_order[]` | `["wireguard"]` | `transport.build_plan()` | ordre du cycle | `test_qualification_transport.py::test_auto_uses_all_supported_transports_in_order` | active |
| config | `egress_catalog.qualification.transport_retry_rounds` | `1` | `transport.TransportPlan` | reboucle après échec | `test_qualification_transport.py::test_auto_wraps_to_wireguard_after_all_fail` | active |
| config | `egress_catalog.qualification.transport_retry_backoff_s` | `60` | `sweep.qualify()` | backoff entre rounds | `test_qualification_sweep.py::test_all_fail_then_wireguard_round_two` | active |
| config | `egress_catalog.qualification.transport_health_min_attempts` | `100` | `transport_health.py` | seuil `degraded` | `test_qualification_readmodel.py::test_transport_degraded_after_min_attempts_without_success` | active |
| config | `egress_catalog.qualification.auth_backoff_s` | `3600` | `free/qualification/sweep.py` | AUTH reste station-local | `test_qualification_sweep.py::test_auth_stops_without_host_death` | active |
| config | `egress_catalog.qualification.daemon_enabled` | `false` | `scripts/fresh_chain_supervisor.py` | lance un cycle qualification | `test_qualification_daemon_control.py::test_daemon_once_command_shape` | active |
| config | `egress_catalog.qualification.daemon_interval_s` | `60` | `scripts/fresh_chain_supervisor.py` | cadence du cycle | `test_qualification_daemon_control.py::test_daemon_live_command_carries_paths` | active |
| config | `egress_catalog.qualification.daemon_max_hosts` | `40` | `free/qualification/supervisor_bridge.py` | borne le sweep (tick superviseur court) | `test_qualification_daemon_control.py::test_daemon_once_command_can_bound_the_sweep` | active |
| config | `egress_catalog.qualification.discovery_interval_s` | `21600` | `discovery.DiscoveryService` | cadence de découverte | `test_qualification_discovery.py::test_is_due_uses_interval` | active |
| config | `egress_catalog.qualification.discovery_stale_max_s` | `86400` | `discovery.DiscoveryService` | marque `stale` | `test_qualification_discovery.py::test_removed_host_is_skipped` | active |
| config | `egress_catalog.qualification.watchdog_stations[]` | `[1..6]` | `watchdog.WatchdogService` | stations surveillées | `test_qualification_watchdog.py::test_run_once_records_heartbeat_and_status` | active |
| config | `egress_catalog.qualification.max_repair_attempts` | `3` | `watchdog.WatchdogService` | escalade après N | `test_qualification_watchdog.py::test_repair_failure_escalates_after_max_attempts` | active |
| config | `egress_catalog.qualification.repair_backoff_s` | `300` | `watchdog.WatchdogService` | pas de réparation en boucle | `test_qualification_watchdog.py::test_repair_backoff_skips_repeated_repairs` | active |
| config | `egress_catalog.qualification.watchdog_enabled` | `true` | `free/qualification/runtime.py` | watchdog actif seulement avec sonde | `test_qualification_watchdog.py::test_missing_probe_is_disabled_not_unhealthy` | active |
| config | `egress_catalog.qualification.watchdog_probe_interval_s` | `30` | `free/qualification/scheduler.py` | cadence des sondes | `test_qualification_scheduler.py::test_watchdog_interval_is_respected` | active |
| config | `egress_catalog.qualification.proactive_rotation_headroom_pct` | `20` | `dashboard/routers/qualification_routes.py` | seuil de rotation proactive | `test_qualification_api.py::test_rotate_headroom_config_changes_decision` | active |
| config | `egress_catalog.qualification.last_station_protection` | `true` | `free/qualification/rotation.py` | jamais 0/6 | `test_qualification_rotation.py::test_last_station_is_protected_without_standby` | active |
| config | `egress_catalog.qualification.cutover_timeout_ms` | `5000` | `free/qualification/cutover.py` | borne chaque étape | `test_qualification_cutover.py::test_cutover_timeout_bounds_prepare` | active |
| config | `egress_catalog.qualification.per_station_workers` | `true` | `free/qualification/runtime.py` | un sweep par station | `test_qualification_runtime.py::test_runtime_scheduler_builds_per_station_workers` | active |
| config | `egress_catalog.qualification.daemon_shadow` | `true` | `free/qualification/supervisor_bridge.py` | force le tick superviseur en shadow, même si `shadow_mode=false` | `test_qualification_daemon_control.py::test_daemon_shadow_wins_when_qualifiers_are_idle` | active |
| config | `egress_catalog.qualification.qualifiers.enabled` | `false` | `free/qualification/qualifiers.py` | démarre/arrête les slots dédiés 7–10 | `test_qualification_qualifiers.py::test_apply_starts_and_stops_the_slots` | active |
| config | `egress_catalog.qualification.qualifiers.count` | `0` | `free/qualification/qualifiers.py` | nombre de slots actifs (0–`max_count`) | `test_qualification_qualifiers.py::test_config_clamps_count_and_station_range` | active |
| config | `egress_catalog.qualification.qualifiers.max_count` | `4` | `free/qualification/qualifiers.py` | plafond technique de slots | `test_qualification_qualifiers.py::test_config_clamps_count_and_station_range` | active |
| config | `egress_catalog.qualification.qualifiers.recommended_max` | `2` | `free/qualification/qualifiers.py` | avertit au-delà (sessions NordVPN) | `test_qualification_qualifiers.py::test_snapshot_warns_above_recommended_max` | active |
| config | `egress_catalog.qualification.qualifiers.station_base` | `7` | `free/qualification/qualifiers.py` | première station dédiée (7–10) | `test_qualification_qualifiers.py::test_config_clamps_count_and_station_range` | active |
| config | `egress_catalog.qualification.qualifiers.transport` | `wireguard` | `free/qualification/qualifiers.py` | transport des slots dédiés | `test_qualification_qualifiers.py::test_default_epoch_builds_a_live_sweep` | active |
| config | `egress_catalog.qualification.qualifiers.max_hosts_per_epoch` | `40` | `free/qualification/qualifiers.py` | borne l’époque live par slot | `test_qualification_qualifiers.py::test_default_epoch_builds_a_live_sweep` | active |
| config | `egress_catalog.qualification.qualifiers.interval_s` | `30` | `free/qualification/qualifiers.py` | cadence de la boucle par slot | `test_qualification_qualifiers.py::test_apply_starts_and_stops_the_slots` | active |
| config | `egress_catalog.qualification.qualifiers.max_attempts_per_host` | `1` | `free/qualification/qualifiers.py` | une tentative par host et par époque | `test_qualification_qualifiers.py::test_config_clamps_count_and_station_range` | active |
| config | `egress_catalog.qualification.qualifiers.transport_rounds` | `1` | `free/qualification/qualifiers.py` | pas de second round WireGuard | `test_qualification_qualifiers.py::test_config_clamps_count_and_station_range` | active |
| config | `egress_catalog.qualification.parallel_rotations` | `true` | `free/qualification/rotation_batch.py` | rotations concurrentes bornées | `test_qualification_rotation_batch.py::test_build_batch_reads_parallel_rotations` | active |
| config | `egress_catalog.qualification.station_standby_size` | `1` | `free/qualification/rotation.py` | standby promu seulement si taille > 0 | `test_qualification_rotation.py::test_zero_standby_size_ignores_standby` | active |
| config | `egress_catalog.qualification.global_pause_on_429` | `false` | `free/qualification/scheduler.py` | pause globale opt-in only | `test_qualification_scheduler.py::test_global_pause_on_429_is_opt_in` | active |

## Gate

```text
scripts/config_audit.py --strict
+ pytest tests/test_option_map.py
+ npm run check
```

Le gate échoue avec la clé ou le contrôle manquant. Une ligne `planned` doit
être convertie en `active` dans la même livraison que son implémentation.
