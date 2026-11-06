# Contract — Shared routing parameters

**Feature**: `006-three-egress-modes`

## Principe

Une clé commune est lue depuis `ip_rotation` par les deux backends. Il n'existe
pas de copie `nordvpn.*` ou `warp.*` pour ces paramètres. Une modification GUI
doit être effective à chaud dans le mode actif et dans l'autre mode après
bascule.

## Table de contrat

| Clé | API PATCH | NordVPN | WARP | Test |
|---|---|---|---|---|
| `strict_free` | `{strict_free: bool}` | toutes les stations tentées | tous les endpoints tentés | `test_free_attempt_tuning`, `test_socks5_*` |
| `on_429_action` | `{on_429_action: "both"|"cooldown"|"rotate"}` | cooldown/rotation Docker | cooldown/rotation WARP | `test_free_quota_vs_transient`, `test_socks5_failover_guard` |
| `station_bad_ttl_s` | `{station_bad_ttl_s: int}` | TTL bad-mark Docker | TTL bad-mark WARP | `test_free_station_reliability` |
| `auto_max_free_attempts` | `{auto_max_free_attempts: bool}` | budget auto | budget auto | `test_free_attempt_tuning` |
| `max_free_attempts` | `{max_free_attempts: int}` | plancher manuel | plancher manuel | `test_free_attempt_tuning` |
| `free_exception_fallback` | `{free_exception_fallback: "station-first"|"direct"}` | stratégie de panne | stratégie de panne | tests free/stream |
| `free_parallel.enabled` | `{free_parallel:{enabled}}` | sélection multi-stations | sélection multi-endpoints | `test_socks5_affinity`, `test_free_multi_attempt` |
| `free_parallel.routing` | `{free_parallel:{routing}}` | round-robin/failover | round-robin/failover | `test_socks5_affinity` |
| `free_parallel.mode` | `{free_parallel:{mode}}` | load-balance/strict/hedge | load-balance/strict/hedge | `test_socks5_affinity` |
| `free_parallel.hedge_delay_ms` | `{free_parallel:{hedge_delay_ms}}` | stagger hedge | stagger hedge | `test_stream_hedge` |
| `free_parallel.hedge_max_attempts` | `{free_parallel:{hedge_max_attempts}}` | borne hedge | borne hedge | `test_free_multi_attempt` |
| `retry_until_success.enabled` | `/api/retry-config` | retry global | retry global | `test_retry_*` |
| `retry_until_success.max_wait_s` | `/api/retry-config` | budget retry | budget retry | `test_retry_*` |
| `quality_preselection.enabled` | `{quality_preselection:{enabled}}` | seuil qualité | seuil qualité | quality tests |
| `quality_preselection.fast_tps` | nested | seuil tier A | seuil tier A | quality tests |
| `quality_preselection.slow_tps` | nested | seuil tier C | seuil tier C | quality tests |
| `quality_preselection.tier_b_quota` | nested | quota tier B | quota tier B | quality tests |
| `quality_preselection.tier_c_quota` | nested | quota tier C | quota tier C | quality tests |
| `quality_rotation.enabled` | `{quality_rotation:{enabled}}` | rotation Docker | rotation WARP managée | `test_socks5_quality_rotation`, `test_warp_rotation` |
| `quality_rotation.min_requests` | nested | seuil | seuil | quality tests |
| `quality_rotation.min_samples` | nested | seuil | seuil | quality tests |
| `quality_rotation.trend_pct` | nested | tendance | tendance | quality tests |
| `quality_rotation.ttfb_ceiling_ms` | nested | plafond | plafond | quality tests |
| `quality_rotation.max_per_hour` | nested | cap | cap | quality tests |
| `quality_rotation.global_slow_guard` | nested | guard flotte | guard flotte | quality tests |
| `latency_rotation.*` | `{latency_rotation:{...}}` | hard/soft cool | hard/soft cool | `test_latency_rotation` |
| `quota_per_ip` | `{quota_per_ip: int}` | quota IP | quota IP | `test_quota_*` |
| `free_prewarm.*` | config | session chaude | session chaude | `test_free_prewarm` |

## Effets interdits

- Un paramètre commun ne doit jamais être un no-op silencieux dans un mode.
- Un paramètre commun ne doit pas ouvrir une porte dure (reset, unicité IP,
  preuve de contenu).
- Un paramètre commun ne doit pas provoquer un fallback implicite
  NordVPN ↔ WARP ↔ Direct.
- Un paramètre `max_load` (NordVPN-only) ne doit pas être présenté comme
  commun : WARP n'a pas de charge API par station.

## Observabilité

Chaque application doit exposer au minimum :

```text
mode, key, value, consumer, applied_at
```

Les endpoints `/api/vpn-config` et `/api/free-egress/diag` exposent la valeur
effective, pas seulement la valeur enregistrée.
