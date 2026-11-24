## T146
# Analysis of `_stream_has_yielded` in opencode.py

## Function Definition (line 18932)

```python
def _stream_has_yielded(started, open_blocks, stream_out, line_buf: str = "") -> bool:
    """True once any SSE byte has been flushed to the client — retry must not concat."""
    try:
        has_blocks = bool(open_blocks)
    except Exception:
        has_blocks = False
    try:
        has_out = int(stream_out or 0) > 0
    except Exception:
        has_out = bool(stream_out)
    return bool(started or has_blocks or has_out or (isinstance(line_buf, str) and bool(line_buf.strip())))
```

The function returns `True` if any of these conditions are met:
- `started` flag is set
- `open_blocks` is non-empty (thinking blocks still open)
- `stream_out` has emitted bytes (>0)
- `line_buf` has non-whitespace content

## Call Sites (28 blocks with `return` at indent+4)

All 28 call sites follow the same pattern: check `_stream_has_yielded(...)`, record failure, then either terminate or retry.

### Distribution across code regions:

| Block Range | Context | Status |
|---|---|---|
| 21296-21308 | Free model streaming, 429 retry decision | 12 calls |
| 21314-21326 | Free model streaming, 429 retry decision | 2 calls |
| 21391-21403 | Free model streaming, 429 retry + 499 handling | 3 calls |
| 21409-21421 | Free model streaming, 499 handling | 1 call |
| 21436-21448 | Free model streaming, 400 credit error handling | 1 call |
| 21741-21753 | Free model streaming exception handler | 1 call |
| 21766-21778 | Free model streaming exception handler | 1 call |
| 21803-21815 | Free model streaming exception handler | 1 call |
| 21830-21842 | Free model streaming exception handler | 1 call |
| 22914-22925 | Free model streaming | 2 calls |
| 22933-22944 | Free model streaming | 1 call |
| 23016-23027 | Free model streaming | 2 calls |
| 23033-23044 | Free model streaming | 2 calls |
| 23055-23066 | Free model streaming | 1 call |
| 23112-23148 | **OpenAI Responses streaming** (critical) | 1 call |
| 23625-23633 | Anthropic-to-OpenAI streaming | 1 call |
| 23645-23653 | Anthropic-to-OpenAI streaming | 1 call |
| 23678-23686 | Anthropic-to-OpenAI streaming | 1 call |
| 28729-28734 | Anthropic streaming retry | 1 call |
| 28740-28745 | Anthropic streaming retry | 1 call |
| 28762-28767 | Anthropic streaming retry | 1 call |
| 28839-28844 | Anthropic streaming retry | 1 call |
| 28852-28857 | Anthropic streaming retry | 1 call |
| 28872-28877 | Anthropic streaming retry | 1 call |
| 29156-29161 | Anthropic streaming termination | 1 call |
| 29174-29179 | Anthropic streaming termination | 1 call |
| 29198-29203 | Anthropic streaming termination | 1 call |
| 29218-29223 | Anthropic streaming termination | 1 call |

## Core Behavioral Pattern

When `_stream_has_yielded` returns `True`:

1. `_cb_record_failure(endpoint)` is called to record the failure
2. Either:
   - `async for ev in _terminate_after_started(...)` yields graceful termination with `stop_reason: "error"` and thinking delta if applicable
   - Or a chunk `{"type": "stop"}` + `data: [DONE]\n\n` is yielded
3. The function then `return`s, ending the current code path

## Key Observations

- **Sentinel role**: `_stream_has_yielded` is the reliable sentinelle for "has any content been emitted", distinct from `started` which may be set later in the stream
- **Cross-model consistency**: The same pattern appears in free streaming, paid streaming, Anthropic→OpenAI bridging, and OpenAI Responses streaming
- **Error handling**: When content has already been yielded, the code gracefully terminates rather than concatenating retry content
- **23112 block**: This is the only OpenAI Responses streaming site — uses `_stream_has_yielded` to decide whether to synthesize `finish_reason` or emit an error event

## Failure Scenarios Triggered

The pattern is invoked for these conditions:
- 429 quota exhaustion during retry
- 499 upstream disconnect
- 400 credit exhaustion
- Network errors (disconnect, timeout)
- Empty upstream stream (no content emitted)
- Stream truncated without terminal event
- General stream exceptions on first attempt

## T221
## Périmètre exécuté (lot T221 restant, vérifié par grep hors docs/tests/scripts/artefacts)

### Supprimé (prod sans appelant)
- `free/fallback.py` : `FallbackPolicy.note_probe`, `evaluate_error`, `active_count`, `active_provider` retirés (API morte ; `evaluate`/`snapshot`/`reset_station`/`select_provider` conservés).
- `free/egress_rotation.py` : `public_pool_rows`, `provider_status` retirés ; `__all__` nettoyé. `rows_from_options`/`select_many`/`select_option`/`rotation_plan` conservés (utilisés par `free/egress_runtime.py`).
- `free/provider_capabilities.py` : `capacity`, `is_reserve_eligible` retirés. `normalize_ipv6_64` conservé (utilisé par `bucket_key`).
- `free/egress_ip.py` : `IpObservation`, `Agreement`, `MIN_SOURCES`, `agree`, `normalize` retirés ; `is_global_ip`/`SENTINELS` conservés (`scripts/fresh_chain_supervisor.py` utilise `is_global_ip`).
- `free/quota_ledger.py` : alias rétro-compat `_is_free_usage_error` retiré ; usage interne migré vers `is_free_usage_error`.
- `free/quality.py` : `QualityRegistry.slow_station_count` retiré (définition seule).
- `free/pool/__init__.py` : `_egress_order_prefers_socks5`, attributs `_last_selection`/`_spare_ready` et leurs écritures mortes retirés (jamais lus).

### Tests adaptés (même lot)
- `tests/test_fallback_policy.py` : tests des méthodes mortes remplacés par couverture live (`evaluate`/`snapshot`, fail-closed, quota trigger).
- `tests/test_egress_rotation.py` : `public_pool_rows` remplacé par `select_many` + `rows_from_options`.
- `tests/unit/test_provider_capabilities.py` : `capacity`/`is_reserve_eligible` recâblés sur `capabilities().reserve_eligible`/`quota_source` et sémantique `bucket_key`.
- `tests/unit/test_egress_ip.py` : réduit à `is_global_ip` (seule surface vivante).
- `tests/test_auto_egress_config.py` : suppression du test `test_order_prefers_socks5_when_first_provider_is_warp` (méthode supprimée).
- `tests/test_free_station_reliability.py` : suppression des assertions sur `_last_selection` (attribut mort).

### Non supprimé (volontairement, décision/contrat requis)
- Rapport 01 vivants/contractuels : `QuotaLedger.parked_ips`/`headroom`, `EgressBlock`/`QuotaObservation` (contrat feature 007, tests dédiés), `IpQuotaRegistry.prune`/`ranking_key`/`headroom`/`state`, `IpQuotaLease.blocked`, `StationHealth.disable`/`enable`/`repair_failed`/`prune` (`tests/test_station_isolation.py` utilise `repair_failed`), `CanaryRouter.note_choice`/`shadow_divergences` (rapport: migrer/brancher), `free/wire_admission.py` (spec 023 T220 le déclare câblé ; `tests/test_wire_admission_wiring.py` est déjà rouge sur `_note_current_wire_fp` absent — pas un retrait silencieux), `free/repair.py` shim et `free/profile_verifier.py` (consommés par tests/compat).
- Rapport 06 (`opencode.py`, 33k lignes) : symboles à zéro appelant prod (`_db_vacuum_if_needed`, `_CANARY_SYSTEM`, `_FB_FALLBACK_CAUSES`, `_LAT_RING_MAX`, `_PATH_TOOL_NAMES`, `_PATH_ARG_ALIASES`, `_get_enabled_keys`, `_env_key_or_raise`, `_workspace_for_key`, `_truncate_body_for_storage`, `_with_client_anthropic_betas`, `_free_request_headers`, `_is_web_search_forced`, `_strip_web_search_tool`, `_geo_pinned_*`, etc.) non retirés dans ce lot — retrait à faire au lot L17/strangler (édition monolithe hors périmètre sûr ici). `_JSON_LIB`/`_sse_keepalive`/`_sse_coalesce` ne sont pas morts au grep actuel.
- `free_discovery.py` / `traffic_capture.py` : encore référencés (Dockerfile, tests, scripts) ; pas des suppressions sûres sans migration plus large.

### Vérification
- `python -m py_compile` OK sur tous les fichiers modifiés.
- `pytest tests/test_fallback_policy.py tests/test_egress_rotation.py tests/unit/test_provider_capabilities.py tests/unit/test_egress_ip.py tests/unit/test_ip_state.py tests/unit/test_rotation_policy.py -q` → **32 passed**.
- `pytest tests/test_auto_egress_config.py -q` → **17 passed**.
- `pytest tests/test_free_station_reliability.py -q` → **4 failed / 5 passed** ; mêmes 4 échecs reproduits sur baseline stashée (non régression de ce lot).
- `pytest tests/unit/ -q` (prescrit) → **193 passed, 2 failed** ; échecs préexistants non liés aux suppressions : `tests/unit/test_wire_admission.py::test_admit_many_picks_the_first_allowed`, `tests/unit/test_wire_profile.py::test_note_current_wire_fp_sets_the_attempt_context` (`AttributeError: module 'opencode' has no attribute '_note_current_wire_fp'`).

### Fichiers touchés
- `c:\Users\julie\Downloads\opencode-proxy-one\free\fallback.py`
- `c:\Users\julie\Downloads\opencode-proxy-one\free\egress_rotation.py`
- `c:\Users\julie\Downloads\opencode-proxy-one\free\provider_capabilities.py`
- `c:\Users\julie\Downloads\opencode-proxy-one\free\egress_ip.py`
- `c:\Users\julie\Downloads\opencode-proxy-one\free\quota_ledger.py`
- `c:\Users\julie\Downloads\opencode-proxy-one\free\quality.py`
- `c:\Users\julie\Downloads\opencode-proxy-one\free\pool\__init__.py`
- `c:\Users\julie\Downloads\opencode-proxy-one\tests\test_fallback_policy.py`
- `c:\Users\julie\Downloads\opencode-proxy-one\tests\test_egress_rotation.py`
- `c:\Users\julie\Downloads\opencode-proxy-one\tests\unit\test_provider_capabilities.py`
- `c:\Users\julie\Downloads\opencode-proxy-one\tests\unit\test_egress_ip.py`
- `c:\Users\julie\Downloads\opencode-proxy-one\tests\test_auto_egress_config.py`
- `c:\Users\julie\Downloads\opencode-proxy-one\tests\test_free_station_reliability.py`
- `c:\Users\julie\Downloads\opencode-proxy-one\specs\024-audit-optimisation-globale\tasks.md` (T221 mis à jour avec livré/reste/vérification)

## T213
## T213 — `app/protocol/mapping.py` split (facade + submodules)

### What changed
- Split `app/protocol/mapping.py` (6,329 lines) into 8 submodules + 1 facade. Function bodies kept identical to the original monolith (same logic/order), with each submodule carrying the same shared prelude (imports/json/encoding helpers) and a docstring header.
- New modules (all ≤ 1500 lines, INV-10):
  - `app/protocol/mapping_io.py` (1425) — orjson/json helpers, encoding singleton, tool-id stable helper, usage/cache token extraction/normalization, effort mapping, schema normalization/profiles, Anthropic cache-control helpers, `_cfg_settings` fallback.
  - `app/protocol/mapping_convert.py` (888) — `anthropic_to_openai` + `_orig_anthropic_to_openai`, conversion cache (LRU bytes/hit-miss) wiring.
  - `app/protocol/mapping_anthropic.py` (943) — OpenAI/Responses→Anthropic, Anthropic request normalization, local signatures, synthetic thinking.
  - `app/protocol/mapping_responses.py` (578) — Responses API ↔ Anthropic conversions.
  - `app/protocol/mapping_tools.py` (605) — tool-name sanitize/restore, Claude tool-id scrubbing, model modality support, sampling defaults.
  - `app/protocol/mapping_relay.py` (861) — optional-field relay, history/tool remap, orphan filtering, Responses input cleanup.
  - `app/protocol/mapping_responses_request.py` (632) — chat/responses request/response construction.
  - `app/protocol/mapping_stream.py` (1103) — Responses SSE state/emitter/stream events.
- `app/protocol/mapping.py` is now a 101-line facade: imports submodules, injects merged cross-module symbols into each submodule `__globals__` (forward/backward refs resolved at runtime, no function-body rewrites), re-exports all symbols, and installs a `_MappingFacade(ModuleType)` that propagates `setattr` on the facade to submodules (needed for monkeypatch compatibility, e.g. `test_tool_choice_emit` patching `yaml_get` on the facade).
- `app/protocol/__init__.py` unchanged; legacy `protocol_mapping.py` shim still resolves (`import protocol_mapping` OK). No caller changes.

### Verification
- `python -m py_compile app/protocol/mapping*.py` — OK.
- Smoke: explicit imports from `app.protocol.mapping` (28 names including `anthropic_to_openai`, `_orig_anthropic_to_openai`, `CACHE_REWRITE_MODELS`, `ResponsesSseState`, `conversion_cache_stats`, etc.) — OK; `app.protocol` package exports OK; `chat_sse_to_anthropic` import OK.
- Required tests (requested):  
  `python -m pytest -q tests/contract/test_anthropic_messages.py tests/test_protocol_matrix.py tests/test_p4_synthetic_thinking.py tests/test_p6_orphan_and_thinking.py`  
  **80 passed**.
- Additional mapping-importing/affected tests run: `tests/test_alignment_conversion_p0.py`, `tests/test_tool_choice_emit.py` (the monkeypatch case that motivated facade `__setattr__` propagation) — passed.
- Size gate check (app/protocol only): no violations; all new mapping modules under 1500 lines.
- Broader protocol/contract runs surfaced only unrelated pre-existing failures observed in this worktree: `tests/contract/test_wire_provider_parity.py::test_provider_headers_parity` (User-Agent constant mismatch), `tests/test_module_size_gate.py::test_no_module_grows_beyond_the_frozen_baseline` (reports `free/pool/__init__.py` over stale baseline; file not modified by this change), and 3 `tests/integration/test_client_reserve_flow.py` failures (`AttributeError: module 'free.pool' has no attribute '_PROVEN_RESERVE_CACHE'`) — not caused by protocol split.

### Task tracking
- Updated `specs/024-audit-optimisation-globale/tasks.md` T213 to `[x]` with delivered split details and validation note.

## T214
## `dashboard/api.py` (3036 lines, fully read)

- **Path**: `c:\Users\julie\Downloads\opencode-proxy-one\dashboard\api.py`
- **Role**: Dashboard FastAPI wiring monolith; most endpoint logic extracted into routers, `api.py` retains shared helpers, caches, state, and `register_dashboard(...)` composition.

### Structure

- **Imports / constants / globals** (top): FastAPI/Starlette, shared config/state, pricing, quota, events, VPN/SOCKS helpers; module caches/state like `_stats_cache`, `_filters_cache`, `_db_lock`, `_ro_db_path`, `_shared_conn`, `_tools_provider`, `_control_clients`, `_index_cache` (in `register_dashboard`), traffic capture flags.
- **Static/cache middleware**: `_StaticCacheMiddleware` (ASGI, module-level) serves precompressed static assets (`.br/.gz`) with cache-control; avoids global `GZipMiddleware` to preserve SSE.
- **DB helpers**: `_db_read_sync`, `_db_query_sync`, `_build_where`, `_normalize_date_bound`, `_period_minutes/_period_rates`, `_TS_FMT`; read-only DB path resolution; shared DB lock integration.
- **Config/env helpers**: env save/apply, YAML mtime tracking, dashboard token checks, API key merge/preserve, custom routes validation/save, server apply helpers.
- **VPN/SOCKS/egress helpers**: proxy mode detection, SOCKS5/HTTPS payload/probe/apply/fallback/auto-rotate state, NordVPN country/server helpers, docker diag/compose config (`_docker_diag`, `_docker_compose_config`), egress capacity/observed/inventory/audit helpers, retry-until-success config persistence.
- **IP/stats helpers**: `_free_model_id_set`, `_ip_stats_db` (cached 10s, merges both stations’ `_ip_history`, reads request identity/rotation), pricing/cost helpers.
- **Traffic capture helpers**: `_traffic_capture`, mark/viewer/lazy/apply/user-enabled helpers.

### `register_dashboard(app, static_dir, conn, server_manager_getter=None, token_usage=None, token_lock=None, db_lock=None, tools_provider=None)` (L2228-L3036)

Bootstraps dashboard on the FastAPI app:

1. **State wiring**: sets DB lock/conn/tools provider/read-only DB path.
2. **Middleware/static**: precompressed static load (`load_precompressed`), `_StaticCacheMiddleware`, `DashboardTrustMiddleware` (loopback/LAN trust, same-host anti-CSRF for mutations, token/rate-limit behavior), mounts `/static`.
3. **Boot side effect**: `free.hunter.ensure_started()` best-effort.
4. **Index serving**: cached `index.html` loader preferring `static/dist/index.html`, fallback `static/index.html`, mtime-based reload; registered via `dashboard.routers.root_routes.register_root_route`.
5. **Router registration (dependency-injected lambdas/callables)**:
   - `config_routes`, `route_table_routes`, `geo_routes`, `config_extra_routes`
   - `proxy_routes`, `stats_routes`, `debug_routes`, `event_routes`
   - `quota_routes`, `usage_routes`
   - VPN/egress/station: `station_logs_routes`, `vpn_status_routes`, `free_routes`, `pool_routes`, `retry_routes`, `vpn_config_routes` (read), `capacity_routes`, `station_routes`, `vpn_config_update_routes`, `vpn_fleet_routes`, `vpn_health_routes`, `vpn_heal_routes`, `vpn_next_routes`, `vpn_update_routes`, `vpn_state_routes`, `station_control_routes`, `vpn_jobs_routes`
   - `cost_routes`, `vpn_control_routes`, `vpn_import_routes`
   - Proxy backend/adaptive/catalog/qualification: `proxy_backend_routes`, `adaptive_routes`, `egress_catalog_routes`, `qualification_routes` (+ launcher/apply wiring, shadow/live config), `egress_catalog_mutations`, `egress_mode_routes`
   - NordVPN/diagnostics: `nordvpn_routes`, `vpn_diagnostic_routes`
   - Traffic/history: `traffic_routes`, `history_routes`

### Notable patterns / risks to be aware of

- **Router extraction is dependency-injected**: `api.py` owns stateful helpers/caches; routers receive getters/setters/checkers. Behavior changes often require updating both helper contracts and router call sites.
- **DB access serialized** through `_db_lock`/`_db_read_sync`; dashboard uses shared conn/read-only path resolution.
- **Static index cache** is process-local dict; rebuild invalidation is mtime-based.
- **Qualification wiring** reads YAML config at registration time for defaults/shadow flags; `start_epoch`/`rotate_apply` closures capture root/db/config.
- **Control-plane clients cached** in `_control_clients` by station.
- **SSE-sensitive**: no global gzip middleware; static compression handled by custom ASGI middleware.

Related files present: `dashboard/routers/*.py` (many), `dashboard/routes/static.py`, `dashboard/events.py`, `dashboard/quota.py`, `dashboard/quality_validation.py`, `dashboard/qualification_launcher.py`.

## T215
## Changements

- **Créé** `c:\Users\julie\Downloads\opencode-proxy-one\config\egress_config.py` (nouveau sous-module pur)
  - Extrait la logique EGRESS/CLIENTS de `config/settings.py` sans import projet et sans état global.
  - API pure: `PROVIDER_DEFAULTS`, `STRATEGIES`, `CLIENT_FAMILIES`, `provider_default`, `provider_weight_default`, `strategy_default(yaml_data)`, `get_egress_config(yaml_data, yaml_get)`, `get_clients_config(yaml_data)`.
  - Le store (`_yaml_data`) et `yaml_get` sont injectés par l’hôte à l’appel, donc le comportement live/hot-reload est préservé.

- **Modifié** `c:\Users\julie\Downloads\opencode-proxy-one\config\settings.py`
  - Remplacé le bloc inline EGRESS/CLIENTS (023 T017) par des wrappers minces qui délèguent à `config.egress_config`.
  - API historique conservée: `EGRESS`, `CLIENTS`, `get_egress_config()`, `get_clients_config()`, `_egress_provider_default()`, `_egress_provider_weight_default()`, `_egress_strategy_default()`, `_EGRESS_PROVIDER_DEFAULTS`, `_EGRESS_STRATEGIES`, `_CLIENT_FAMILIES`.
  - `EGRESS`/`CLIENTS` restent des snapshots d’import; `get_egress_config()`/`get_clients_config()` relisent le YAML live via injection, comme avant.

## Vérification

- `python -m py_compile config/egress_config.py config/settings.py` — OK
- Smoke import: `from config.settings import EGRESS, CLIENTS, get_egress_config, get_clients_config...` — OK (`4 providers`, `anthropic_messages`)
- `python -m pytest tests/contract/test_egress_config.py -v` — **8 passed**
- `python -m pytest tests/test_egress_config.py tests/contract/test_egress_config.py -v` — **12 passed, 1 failed**
  - Échec: `tests/test_egress_config.py::test_egress_disabled_provider_not_in_runtime` avec `AttributeError: 'EgressRuntime' object has no attribute 'providers'`. Reproduit sur baseline (stash de `config/settings.py`, test identique, puis restauration) ; non lié à l’extraction EGRESS/CLIENTS.

## VERIFY
## Verification report (read-only)

### Scope
- Repo: `c:\Users\julie\Downloads\opencode-proxy-one`
- Branch: `019-audit-optimisation-flotte-free` (ahead of origin by 2 commits)
- No files modified during verification (removed temporary `.tmp_pytest_verify.txt`)

### 1) Working tree status (`git status --short`)
- **Modified (tracked):** 67 files total in diff stat; Python-modified include `app/protocol/mapping.py`, `config/settings.py`, `dashboard/api.py`, `dashboard/routers/egress_catalog_routes.py`, `free/{catalog_claims,egress_ip,egress_rotation,fallback,ip_registry/sqlite.py,ip_registry/sqlite_registry.py,pool/__init__.py,provider_capabilities.py,qualification/store.py,quality.py,quota_ledger.py,rotation.py,warp_rotation.py}`, `observability/db.py`, `opencode.py`, `ops/{health.py,providers/base.py,providers/nordvpn.py,providers/warp.py}`, `rotation/infrastructure/db/{claims.py,retention.py}`, `scripts/{config_audit.py,make_credentials_env.py,module_size_audit.py}`, `tests/{test_auto_egress_config.py,test_coverage_boost.py,test_egress_rotation.py,test_fallback_policy.py,test_free_station_reliability.py,test_plan_011_baseline.py,test_plan_011_consolidation.py,unit/test_egress_ip.py,unit/test_provider_capabilities.py}`, `vpn/{auth.py,manager.py}`, plus specs/config.yaml.
- **Deleted (tracked):** `app/quotas/__init__.py`, `app/streaming/__init__.py`, `logs/probe_socks5.py`, `logs/probe_vacuum.py`, `ops/lifecycle.py`, `ops/lifecycle_evidence.py`, `ops/lifecycle_policy.py`, `rotation/application/{__init__.py,repair.py,sweep.py,warm_pool.py}`, `tests/{test_rotation_repair.py,test_rotation_sweep.py,test_rotation_warm_pool.py}`.
- **Untracked (relevant):** new split modules `app/protocol/mapping_{anthropic,convert,io,relay,responses,responses_request,stream,tools}.py`, `config/egress_config.py`, `dashboard/{cost_models.py,quality_validation.py}`, `observability/{auth_pause.py,fleet_slo.py,pins.py,station_state.py}`, `scripts/{diagnostics/*,maintenance_purge.py,preflight.py,slo_monitor.py,warp_bound_fleet.py}`, and many new `tests/test_*.py` files including `tests/test_egress_config.py`.

### 2) Diff stat (`git diff --stat`)
- `67 files changed, 941 insertions(+), 8129 deletions(-)`
- Largest changes: `app/protocol/mapping.py` (+6398/- massive rewrite/split footprint reported as 6398+------------------- in stat), `opencode.py` (-49), `config/settings.py` (+71/-), `free/fallback.py` (-80), `scripts/config_audit.py` (+244/-), `free/qualification/store.py` (+240/-), deleted lifecycle/rotation-application modules and rotation tests.

### 3) `py_compile` (all modified + untracked `*.py` present on disk)
- Command: compiled 100 files via `py_compile.compile(..., doraise=True)`
- Result: **PASS** — `COMPILING 100 files` / `FAILURES: 0`

### 4) Import smoke test (split/extracted modules)
- Imported: `app.protocol.mapping`, `app.protocol.mapping_{anthropic,convert,io,relay,responses,responses_request,stream,tools}`, `config.egress_config`, `config.settings`, `dashboard.cost_models`, `dashboard.quality_validation`
- Result: **PASS** — `IMPORT OK: 13 / 13`

### 5) Targeted pytest
- Command:
  `python -m pytest -q -p no:cacheprovider tests/test_proxy.py tests/unit/ tests/test_fallback_policy.py tests/contract/test_anthropic_messages.py tests/test_config_store.py tests/test_vpn_endpoints.py tests/test_free_model_map_http.py tests/test_egress_config.py`
- Observed progress: `325` collected items (by dot count: 72+72+72+72+37), **2 failures**, no summary line captured in redirected output (known pytest-summary-on-redirect behavior; counts derived from progress dots and failure list).

#### Failures (raw)

```text
================================== FAILURES ===================================
_____________ test_note_current_wire_fp_sets_the_attempt_context ______________
tests\unit\test_wire_profile.py:82: in test_note_current_wire_fp_sets_the_attempt_context
    fp = oc._note_current_wire_fp({"model": "m", "messages": []})
         ^^^^^^^^^^^^^^^^^^^^^^^^
E   AttributeError: module 'opencode' has no attribute '_note_current_wire_fp'
________________ test_egress_disabled_provider_not_in_runtime _________________
tests\test_egress_config.py:82: in test_egress_disabled_provider_not_in_runtime
    assert Provider.PUBLIC not in runtime.providers
                                  ^^^^^^^^^^^^^^^^^
E   AttributeError: 'EgressRuntime' object has no attribute 'providers'
=========================== short test summary info ===========================
FAILED tests/unit/test_wire_profile.py::test_note_current_wire_fp_sets_the_attempt_context
FAILED tests/test_egress_config.py::test_egress_disabled_provider_not_in_runtime
```

#### Failure classification

1. **`tests/unit/test_wire_profile.py::test_note_current_wire_fp_sets_the_attempt_context` — pre-existing / not caused by these changes**
   - `opencode.py` has no `_note_current_wire_fp` symbol (`grep` only finds the test reference).
   - `git diff -- opencode.py` contains no `wire_fp` changes.
   - Matches the pre-existing failure already noted in repo context.

2. **`tests/test_egress_config.py::test_egress_disabled_provider_not_in_runtime` — failure in untracked new test / test-API mismatch (not a tracked-test regression)**
   - `tests/test_egress_config.py` is **untracked** (`??`), alongside untracked `config/egress_config.py`; `free/egress_runtime.py` is tracked/unmodified in working tree status.
   - The test asserts `Provider.PUBLIC not in runtime.providers`, but `free.egress_runtime.EgressRuntime` exposes `policies`/scheduler/store fields and has no `providers` attribute (`c:\Users\julie\Downloads\opencode-proxy-one\free\egress_runtime.py` defines `policies: dict[str, ProviderPolicy]`, no `providers`).
   - This indicates the new untracked test does not match the current runtime API; it is not evidence of a regression in the existing tracked targeted tests.

### 6) Overall verdict
- **Compile/import:** clean for touched Python files.
- **Targeted pytest:** 323/325 passed by dot count, **2 failures**; **0 failures attributed to regressions in pre-existing tracked tests** from the requested targeted set. The two failures are (a) known pre-existing wire-profile test, and (b) an untracked new egress-config test with an API mismatch against `EgressRuntime`.