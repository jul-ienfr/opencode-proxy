# Audit 06 — Le monolithe `opencode.py`

- **Date** : 2026-11-20
- **Objet** : `opencode.py` (33 618 lignes, 1 598 087 octets, 27 046 lignes de code / 4 279 commentaires / 2 294 blanches)
- **Périmètre élargi** : `protocol_mapping.py`, `free_discovery.py`, `free_ip_pool.py`, `latency_rotation.py`, `shared_rotation.py`, `shared_state.py`, `traffic_capture.py`, `ip_latency.py`, `trust.py`, `custom_routes.json`, `api_keys.json`, `gunicorn.conf.py`, `Start.bat`, `lancer-gui.bat`, `lancer-proxy.bat`, `lancer-silent.vbs`, `setup.bat`, `setup.ps1`
- **Mode** : lecture seule, aucune modification de code
- **Méthode** : table des matières AST, lecture par tranches de ~300 lignes, greps ciblés dans tout le dépôt (hors `Lib/`, `_research/`, `.audit-wt/`, `.audit-logs-wt/`, `node_modules/`), vérification par exécution Python sur les points litigieux, corrélation avec `logs/debug.log` (jamais de `Read` dessus).

---

## 1. Résumé exécutif

1. **Le monolithe ne peut pas recevoir de nouveau code** (constitution XIV) et a déjà dépassé toute borne : 33 618 lignes, 5 fonctions de plus de 1 000 lignes, 24 routes HTTP, 13 middlewares, ~1 190 lignes de `lifespan` et 394 lignes de `ServerManager` mêlés au même fichier.
2. **P0 confirmé — double exécution du corps de module.** `opencode.py` exécuté en `__main__` est ensuite **réimporté** sous le nom `opencode` par `gui/tray.py:253` (via `gui/__init__.py`) et `dashboard/api.py:1995`. Le corps de module s'exécute deux fois dans le même processus. Preuve directe dans `logs/debug.log` : `SQLite connection established` **2×** (l. 83340 et 83417), `[db] restored …` **74 lignes pour 37 modèles distincts** (2×), `free_model_usage + token_counters_daily tables ready` **2×**, `Debug mode enabled` **2×**. Conséquences mesurables : **deux objets `sqlite3.Connection` ouverts sur `logs/requests.db`**, **deux dicts `_token_usage`/`_free_model_cooldowns`**, **deux `_free_ip_pool`** — le second namespace a `_free_ip_pool is None` et `_geo_block_total == 0` à vie. Les lecteurs du dashboard (`vpn_status_routes.py:327,339`, `geo_routes.py:136`) lisent **ce second namespace vide** : la télémétrie géo du dashboard est structurellement fausse.
3. **P0 confirmé — `_resp_json_hors_boucle` est défini APRÈS le bloc `if __name__ == "__main__"`** (`opencode.py:33605` vs bloc `33459-33598`). Le bloc `__main__` se termine par `run_gui(...)`/`run_terminal_loop(...)`, tous deux **bloquants** (`pystray.Icon.run()` bloque jusqu'à `stop()`). La définition à la ligne 33605 n'est donc **jamais exécutée dans le namespace `__main__`**. Les deux seuls appelants (`_gemini_chat_request` l. 12044, handler `responses` l. 31664) lèvent `NameError`, **avalé** par un `except Exception` : la jambe Anthropic non-stream de `/v1/responses` répond **502 « Upstream returned non-JSON response »** sur une réponse amont valide. Le commentaire `# [A11] … l'ordre du module est sans effet` (l. 33601-33604) est **factuellement faux** : la résolution se fait à l'appel, mais l'*exécution* de la définition dépend bien de l'ordre.
4. **P0 confirmé — `/v1/responses` en streaming sur route géo : `UnboundLocalError` → 500, et la route tunnel payante n'est jamais empruntée.** `opencode.py:31591` lit `_actual_model` avant toute liaison (aucun global `_actual_model` n'existe, aucun `except` du bloc ne l'attrape) → `@app.exception_handler(Exception)` rend un 500 générique. **Au même site**, le `elif _geo_tunnel:` de la l. 31596 est un **test dupliqué** du l. 31590 : toujours faux quand il est évalué, donc le chemin `_open_via_pool` (l. 31598-31605) — la route tunnel payante voulue par « Axe A: geo-restricted paid » — **est mort**, et la requête payante part en direct (l. 31607), contournant la porte géo.
5. **P1 confirmé — 30 % des gestionnaires d'exception sont des `except …: pass` nus** (367 sur 1 225). Ce taux explique pourquoi P0-2 reste silencieux depuis 2026-09-15 (commit `f73cce56`, qui a déplacé `_resp_json_hors_boucle` sous le bloc `__main__`).
6. **Le plan de découpage strangler existant (spec 014, D-014-04) est inachevé** : les modules `app/compaction/`, `app/protocol/`, `app/router/`, `core/`, `server/`, `streaming/`, `upstream/`, `observability/` ont été extraits, mais **les 5 gros handlers HTTP n'ont jamais bougé** — ce sont eux, et eux seuls, qui portent le monolithe.
7. Le lot le plus rentable est le **lot 2 (corps de requête commun aux 3 handlers)** : ~165 lignes strictement dupliquées, extraites une fois, supprimées trois fois. Combiné aux lots 8 et 9 (boucle d'essais free unifiée, helpers de flux), il résorbe ~1 850 des ~2 800 lignes dupliquées identifiées.

---

## 2. Table des matières annotée du monolithe

Le fichier suit un ordre non topologique : les imports sont éclatés sur ~40 sites (`# noqa: E402`), les définitions apparaissent parfois après leur usage. Plages issues de l'AST (`opencode.py`).

### 2.1 Prologue et état global (l. 1-3300)

| Plage | LOC | Rôle |
|---|---|---|
| 1-45 | 45 | Imports stdlib, crashguard, `_APP_STARTED_AT`, compteurs 429 client |
| 48-104 | 57 | `_client_429_reason()` / `_count_client_429()` — classification des 429 renvoyés au client (022 T009/T010) |
| 106-178 | 73 | `httpx` **différé** via `LazyModule` ; imports `config.*` ; fallback `get_socks5_proxy_url` (jamais le chemin actif) |
| 180-213 | 34 | Primitives web search/fetch : `_DDG_CACHE`, `FETCH_SEM`, `_BLOCKED_NETS` (SSRF) |
| 207-441 | 235 | **Routage de clés API** : `get_next_api_key`, `_find_alternative_key`, `_alias_for_key`, `_has_usable_paid_key`, `_KeyPauser`, `_get_auth_headers` |
| 447-555 | 109 | Betas Anthropic (`_ANTHROPIC_BETA`, `_ANTHROPIC_BINDING_BETA`, `_ANTHROPIC_COMPACT_BETA`) |
| 561-648 | 88 | `BOOT_T0`, `BOOT_OPTS`, `_PROD_RUNTIME`, `_get_encoding` (tiktoken lazy), `_fast_id` |
| 650-773 | 124 | `BASE_DIR`/`STATIC_DIR`/`LOG_DIR`, seams DB (`_truncate_body_for_storage`, `_DbRowRaw`) |
| 776-1226 | 451 | **Couche SQLite** : `_db_migrate_bg`, `_db_insert_sync`, `_db_execute_batch_sync`, `_db_writer_loop` (writer unique batché) |
| 1228-1296 | 69 | Attribution d'hôte (`_host_attribution_module`, `_attribute_server_host`) |
| 1298-1796 | 499 | `_save_request` (285 l.), `_log_free_model_usage`, restore compteurs, `_wal_checkpoint` |
| 1803-1960 | 158 | Helpers JSON (`_json_loads`/`_json_dumps`/`_serialize_json_body`) et HTTP (`_build_http_limits`/`_build_http_timeout`) |
| 1961-2187 | 227 | **Pool curl_cffi** : `_ensure_curl_cffi`, `_get_pooled_curl_session`, `_evict_idle_curl_pools`, `_ensure_http_client` |
| 2199-2320 | 122 | **Clients par rôle** (`_role_client`), `_sync_station_supervisors`, watchdogs superviseurs |
| 2341-2512 | 172 | Superviseurs externes (`fresh_chain`, `station_slo`) et stagger de boot |
| 2515-2860 | 346 | **`_apply_station_count`** — hot-reload upscale/downscale de la flotte VPN |
| 2862-3012 | 151 | Sanitisation (`_sanitize_headers`, `_truncate`, `_redact`), namespace de cache de réponse |
| 3018-3296 | 279 | Boucles `live-models` et catalogue d'egress (`_live_models_loop`, `_catalog_*`) |

### 2.2 Boot (l. 3300-4495)

| Plage | LOC | Rôle |
|---|---|---|
| 3300-4487 | **1 188** | **`lifespan(app)`** — Docker, flotte VPN, pool d'IP, qualifieurs, ~25 `create_task` de fond, arrêt propre |

### 2.3 Probes et application FastAPI (l. 4490-5060)

| Plage | LOC | Rôle |
|---|---|---|
| 4490-4495 | 6 | `app = FastAPI(lifespan=lifespan)` |
| 4497-4551 | 55 | `/healthz` (liveness), `/readyz` (readiness warmup) |
| 4573-4872 | 300 | **`VpnWarmupGateMiddleware`** (503 + Retry-After pendant le warmup) |
| 4882-4883 | 2 | `/startupz` |
| 4894-4922 | 29 | Middleware de capture de trafic |
| 4925-4950 | 26 | `EmptyUpstreamStream`, `UnsupportedModelFamily` |
| 4963-4981 | 19 | Handler global d'exception (`debug_exception`) |
| 4985-5000 | 16 | `_server_manager`, `register_dashboard(app, …)` |

### 2.4 Observabilité (l. 5038-5810)

| Plage | LOC | Rôle |
|---|---|---|
| 5038-5346 | 309 | **`_build_metrics_text`** (Prometheus, 292 l.) + `/metrics` |
| 5361-5475 | 115 | `GeoWarningMiddleware`, `ModelDeprecationNoticeMiddleware` |
| 5481-5565 | 85 | En-têtes de tier (`_tier_headers`, `ModelTierHeaderMiddleware`) |
| 5577-5723 | 147 | **Circuit breakers** (`_CircuitBreaker`, `_get_cb`, `_is_free_cb_endpoint`) |
| 5725-5810 | 86 | Middlewares de garde (`Global429Backoff`, `EmptyStreamGuard`, `ToolPathHealing`, `RetryUntilSuccess`, `RequestDeadline`) |

### 2.5 Porte géo (l. 5827-6600)

| Plage | LOC | Rôle |
|---|---|---|
| 5827-5901 | 75 | `_geo_i18n`, `_notify_geo_tray_throttled` |
| 5904-6050 | 147 | `_geo_headers`, `_geo_block_response`, `_geo_sse_error` |
| 6053-6594 | **542** | **`_enforce_geo_gate`** — porte unique fail-closed des 3 handlers |

### 2.6 Identité et wire free (l. 6604-11050)

| Plage | LOC | Rôle |
|---|---|---|
| 6604-6674 | 71 | `_safe_client_status`, `_auth_window_message`, `_forward_post` |
| 6688-6810 | 123 | Identité free (`_current_free_identity`, `_apply_identity`, `_free_request_headers`) |
| 6892-7100 | 209 | **Identité client-officiel** : UA mesurés, JA3, ordres d'en-têtes (v1.18.31 / 1.19.3 / 2.2.5) |
| 7109-7435 | 327 | `_free_fp_override`, empreinte TLS, payload officiel, shims d'outils |
| 7435-8276 | 842 | **Budget et conformation du corps free** : `_free_wire_body` (486 l.), `_trim_free_wire_messages` (184 l.), `_sanitize_free_messages`, `_free_is_summary_request` |
| 8279-8765 | 487 | `_free_wire_body` (corps wire officiel exact) |
| 8796-9078 | 283 | Parseurs SSE et réparation de tool_calls (`_iter_sse_data`, `_repair_tool_arguments_prefix`, `_heal_tool_call_paths`) |
| 9081-9404 | **324** | `_ChatToolCallHealer` (classe d'état de streaming) |
| 9407-9609 | 203 | Collecteurs bufferisés (`_collect_chat_completion`, `_collect_responses_object`) |
| 9627-9878 | 252 | Mint d'IDs OpenCode (`_mint_oc_id`), sessions (`_free_session_id`, `_conversation_session_id`) |
| 9881-10184 | **304** | **`_official_free_headers`** (reconstruction de la face Bun officielle) |
| 10197-10635 | 439 | Sonde free (`_free_probe_*`, `_free_probe_new_worker`) |
| 10636-11047 | 412 | Session payante, `_enrich_paid_wire_headers`, identité par station, path-masking |

### 2.7 Transport free (l. 11050-14630)

| Plage | LOC | Rôle |
|---|---|---|
| 11050-11305 | 256 | `_do_free_request_curl_cffi` (POST non-stream via tunnel) |
| 11308-11375 | 68 | `_revalidate_public_proxies`, `_do_free_direct_request` |
| 11378-11605 | 228 | Plan de compaction (`_compaction_summarizer_*`, wrappers DI) |
| 11623-12018 | 396 | **Compaction proactive** (`_maybe_proactive_condense`, 225 l.) |
| 12021-12345 | 325 | Handlers Gemini (`_gemini_chat_request`, `_handle_*_gemini`) |
| 12348-12494 | 147 | `_compaction_normalize_response`, `_huge_context_verdict`, `_try_free_with_tiny_retry` |
| 12497-13001 | 505 | `_buffered_compaction_stream`, hedge (`_free_parallel_should_hedge`, `_hedged_fetch`) |
| 13035-13319 | 285 | Adaptateurs `curl_cffi` (`_CurlCffiResponse`, `_CurlCffiStreamResponse`), erreurs typées |
| 13322-13664 | 343 | Budget d'essais free, admission (`_FreeAdmissionResponse`), hedge de flux |
| 13668-14486 | **819** | **`_open_free_stream_hedged` + `_open_free_stream`** (640 l.) |
| 14490-14626 | 137 | `_open_via_pool` (POST payant via tunnel géo) |

### 2.8 Cooldowns et préflight free (l. 14637-16340)

| Plage | LOC | Rôle |
|---|---|---|
| 14637-14810 | 174 | Probes d'egress (`_get_cached_public_ip`, `_get_direct_ip`, `_direct_country`) |
| 14812-14934 | 123 | Cooldowns (`_free_cooldown_key`, `_free_429_cooldown_seconds`, `_free_bucket_retry_after`) |
| 14937-15263 | 327 | Substitution de modèles free (`_free_candidate_chain`, `_resolve_free_model`) |
| 15266-15340 | 75 | Compteurs d'incidents (`_note_free_429`, `_note_empty_stream`, sweep de cooldowns) |
| 15340-15482 | 143 | Cooldowns trial-mort, qualité de station |
| 15485-15590 | 106 | `_record_station_quality`, `_record_free_completion` |
| 15591-15904 | 314 | Égress candidats, préflight (`_free_preflight_retry_after`, `_free_preflight_gate`) |
| 15907-16178 | 272 | `_mark_free_stations_429` (239 l.), failover épuisé |
| 16181-16341 | 161 | Callback pool→hôte, 429 en flux, réponses de refus |

### 2.9 L'essai free-first (l. 16344-17660)

| Plage | LOC | Rôle |
|---|---|---|
| 16344-16471 | 128 | Collecte de réponse free, marqueurs de contexte (`_mark_*`) |
| 16474-17650 | **1 177** | **`_try_free_model_first`** — la plus grosse fonction du fichier |

### 2.10 Métriques, retry et finalisation de flux (l. 17653-19290)

| Plage | LOC | Rôle |
|---|---|---|
| 17653-17746 | 94 | Datapolicy (`_unwrap_responses_envelope`, `_check_datapolicy_guard`) |
| 17748-17896 | 149 | Contexte de fallback (`_FALLBACK_CTX`), compteurs, quotas |
| 17916-18058 | 143 | Watchdog TTFB, `_status_word`, `_correlated_403_message` |
| 18061-18341 | **281** | **`_do_request_with_retry`** (jambe payante) |
| 18344-18539 | 196 | `_update_token_usage`, `_save_and_log_request`, `_log_and_save_error` |
| 18545-18817 | 273 | `_make_stream_retry_loop`, `_stream_error_response`, `_persist_free_400_wire` |
| 18819-19072 | 254 | Refus SSE free, décisions 429, bookkeeping |
| 19075-19267 | 193 | Finalisation de tokens, gardes de flux vide, `_terminate_after_started` |
| 19289-19402 | 114 | Wrappers SSE (`_sse_keepalive`, `_sse_coalesce`), `_note_sse_abort` |

### 2.11 Routage et outils (l. 19414-20560)

| Plage | LOC | Rôle |
|---|---|---|
| 19414-19488 | 75 | `_route_for` (délégué à `app.router`), réponse 410 des modèles retirés |
| 19491-19554 | 64 | Extraction/normalisation d'outils |
| 19557-19959 | 403 | Web search/fetch natifs (`_handle_web_search`, `_handle_web_fetch`, `_is_web_tool_native`) |
| 19966-20054 | 89 | **Ré-exports de compat** (`app.protocol.*` → noms historiques) |
| 20056-20257 | 202 | Collecteurs SSE bufferisés (`_sse_lines_from_buffered_json`, `_responses_nonstream_payload`) |
| 20260-20470 | 211 | `_finalize_and_close_stream`, `_clamp_native_output_limit` |
| 20473-20561 | 89 | `ensure_min_tokens`, estimation de tokens, `_get_client_ip` |

### 2.12 Les handlers HTTP (l. 20564-33055)

| Plage | LOC | Route |
|---|---|---|
| 20564-24223 | **3 657** | `messages` — `POST /v1/messages`, `/anthropic/v1/messages` |
| 24237-24995 | 759 | Canary (`_canary_*`) et route `POST /api/canary/run` |
| 24998-25315 | 318 | `GET /health` (+ `_health_status_reasons`), `GET/POST /api/circuit-breakers*`, `GET/POST /api/key-pauses*` |
| 25319-25706 | 388 | `GET /api/drift`, `GET/POST /api/free-egress/*`, `GET/POST /api/egress/*` |
| 25710-25972 | 263 | `GET /api/free-health`, `_free_quota_states`, `_free_model_inventory` |
| 25976-26483 | 508 | `GET /api/free-models`, `/api/free/hunter`, `/api/free/chain`, `POST /api/free/quota/*`, `POST /api/free-discovery/refresh` |
| 26486-26782 | 297 | Capacités de modèle (`_model_tier_for`, `_model_api_item`) |
| 26786-26929 | 144 | `GET /v1/models` |
| 26923-26929 | 7 | `POST /v1/messages/count_tokens` |
| 26933-29898 | **2 966** | `chat_completions` — `POST /v1/chat/completions` |
| 29901-30523 | **623** | `_systemone_free_via_pool` |
| 30527-30977 | **451** | `systemone` — `POST /v1/systemone` |
| 30981-31048 | 68 | `POST /v1/responses/compact` |
| 31082-33052 | **1 970** | `responses` — `POST /v1/responses` |

### 2.13 Runtime de processus (l. 33055-33619)

| Plage | LOC | Rôle |
|---|---|---|
| 33055-33449 | **395** | `ServerManager` (cycle de vie uvicorn, socket dual-stack, hot-restart, `full_restart` par `os.execv`) |
| 33459-33598 | 140 | **Bloc `if __name__ == "__main__"`** (argparse, lock mono-instance, GUI/tray ou terminal) |
| 33605-33617 | 13 | **`_resp_json_hors_boucle` — défini APRÈS le bloc `__main__` (P0, §3.1)** |

---

## 3. Findings

### P0

#### P0-1 — Le corps de module s'exécute DEUX FOIS dans le même processus

- **Preuve code** : `opencode.py:33459` (`if __name__ == "__main__":`) → l. 33580 `run_gui(mgr, HOST, PORT)`. `gui/__init__.py:1` fait `from .tray import TrayApp` ; `gui/tray.py:253` fait `import opencode as _oc` dans `TrayApp._log_boot`, appelé l. 245 (`self._log_boot("tray shown")`) donc **à l'intérieur de `run_gui`**. Idem `dashboard/api.py:1995` (`import opencode as oc`).
- **Preuve runtime** (`logs/debug.log`, jamais lu par `Read`) :
  - `SQLite connection established` → **2 occurrences** (l. 83340 et 83417)
  - `[db] restored <modèle>` → **74 lignes pour 37 modèles distincts** (`grep '\[db\] restored' | wc -l` = 74 ; `sort -u` sur le nom = 37)
  - `free_model_usage + token_counters_daily tables ready` → **2** ; `Debug mode enabled` → **2**
  - `[boot+0.0s] imports done (key cache rebuilt)` (l. 83415) arrive **après** `[lifespan] background tasks created` (l. 83410) — la seconde exécution a lieu pendant que le serveur tourne déjà.
- **Mécanisme** : `python opencode.py` enregistre le module sous `sys.modules["__main__"]`. Un `import opencode` ultérieur ne le trouve pas sous ce nom et **re-exécute le fichier** sous `sys.modules["opencode"]` (reproduit en isolation : voir §10, protocole de vérification).
- **Impact** :
  1. **Deux `sqlite3.Connection` sur `logs/requests.db`** (l. 759 exécuté 2×) — la connexion `__main__` est celle du writer, l'autre reste ouverte sans usage.
  2. **Deux jeux de globaux** : `_token_usage`, `_token_lock`, `_free_model_cooldowns`, `_free_cooldown_lock`, `_free_ip_pool`, `_geo_breaker`, `_geo_block_total`, `_direct_ip_cache`. Vérifié en exécution : le namespace `opencode` a `_free_ip_pool is None`, `_geo_block_total == 0`, `_direct_country_cache == {'country':'','ts':0.0,'ip':''}`.
  3. **Télémétrie dashboard structurellement fausse** : `dashboard/routers/vpn_status_routes.py:327` lit `opencode_module._geo_block_total` (toujours 0), l. 339 `_geo_breaker` (toujours `{}` → `queue_depth` à 0) ; `dashboard/routers/geo_routes.py:136` lit `_direct_country_cache` / `_direct_ip_cache` (jamais alimentés).
  4. **Coût de boot** : `_restore_token_counters_async()` (l. 1786, module-level) lance un thread de restore **deux fois** ; `_rebuild_key_cache()` (l. 646) tourne 2×.
  5. **Divergence potentielle** : `_apply_station_count` (importé par `dashboard/routers/egress_mode_routes.py:222`, `vpn_config_update_routes.py:253,270`, `vpn_heal_routes.py:30`) écrit dans `shared_state`, donc reste correct ; mais `_direct_country_cache` / `_geo_*` non — deux vérités selon le chemin.
- **Recommandation** : `sys.modules.setdefault("opencode", sys.modules["__main__"])` **avant** toute exécution du bloc `__main__` (ou, plus propre, sortir `__main__` dans un `main.py` de 150 lignes qui importe `opencode`). Ajouter un test qui assert `len({id(m) for m in sys.modules if m in ('opencode','__main__')}) == 1` après boot.
- **Effort** : **S** (5-20 lignes) — **Risque** : faible, mais à valider par un boot complet (le second import disparaît : les tests qui comptent sur le second namespace via `import opencode` en `pytest` ne sont pas affectés, pytest n'exécute jamais le bloc `__main__`).

#### P0-2 — `_resp_json_hors_boucle` défini après le bloc `__main__` → `NameError` avalé → 502 sur `/v1/responses` non-stream Anthropic

- **Preuve code** : bloc `__main__` = l. **33459-33598** ; définition = l. **33605-33617**. Les appelants sont `_gemini_chat_request` l. **12044** et le handler `responses` l. **31664** (dans la branche `if protocol == "anthropic"` ouverte l. 31289).
- **Preuve que la définition n'est pas atteinte** : `run_gui` → `pystray.Icon.run()` est documenté bloquant (`This method is blocking until :stop: is called. It must be called from the main thread.`) ; `run_terminal_loop` (`dashboard/display.py:510-520`) boucle `while stop(): … time.sleep(0.5)`. Aucune des deux ne rend la main.
- **Preuve du `NameError` avalé** : `responses` l. 31663-31668 :
  ```python
  try:
      data = await _resp_json_hors_boucle(resp)
  except Exception:
      _debug(f"  ✗ non-JSON response from {endpoint}")
      _log(f"  UPSTREAM DECODE ERROR: non-JSON response from {endpoint}")
      return _openai_error(502, "Upstream returned non-JSON response")
  ```
  Un `NameError` y est indiscernable d'un corps illisible. Idem `_gemini_chat_request` l. 12044 dans un `try/except Exception` qui **rejoue** la requête en payant (l. 12045).
- **Preuve par contre-épreuve** : en import (`pytest`, `gunicorn`), la fonction existe (`hasattr(opencode, '_resp_json_hors_boucle') == True`) — c'est pourquoi `tests/test_a11_parse_hors_boucle.py` passe. Le bug ne se manifeste **que** dans le chemin `python opencode.py`, celui du runtime local (CLAUDE.md : « Le proxy tourne EN LOCAL sur cette machine : `pythonw opencode.py --gui` »).
- **Reachabilité** : `GET /v1/models` expose `haiku → minimax-m2.5` (`protocol == "anthropic"`, vérifié en exécution) ; le handler `responses` atteint donc la branche l. 31289 pour tout client Codex/réponses demandant `haiku` en non-stream. Aucun modèle routé ne pointe vers un upstream `protocol == "gemini"` (vérifié : liste vide), donc le site l. 12044 est aujourd'hui non atteignable — le site l. 31664, si.
- **Le commentaire qui protège le bug est faux** (l. 33601-33604) : « La resolution se faisant a l'appel, l'ordre du module est sans effet ». L'ordre est **sans effet pour la *résolution*** (les appels sont dans des fonctions) mais **déterminant pour l'*exécution*** de la définition. C'est exactement le piège que le commentaire prétendait écarter.
- **Recommandation** : déplacer la définition **avant** le bloc `__main__` (par exemple juste après `ensure_min_tokens`, l. 20511), et **remplacer** le commentaire par un garde-fou testable. Ajouter un test qui exécute `opencode.py` en sous-processus `__main__` et vérifie que tous les noms référencés par les handlers existent (`compile` + `ast` suffisent : chercher tout `Name` en `Load` non résolu dans le namespace module).
- **Effort** : **S** (déplacement + commentaire) — **Risque** : très faible. **Effort du garde-fou** : **M**.

#### P0-3 — `/v1/responses` en streaming, route géo : `UnboundLocalError` → 500, ET route tunnel payante jamais empruntée

Deux défauts au **même site** (`opencode.py:31587-31607`).

- **Preuve code** (l. 31587-31607) :
  ```python
  if free_result is not None:
      resp, _, _actual_model, _actual_ip = free_result     # 31588
      model_id = _actual_model                             # 31589
  elif _geo_tunnel:
      model_id = _actual_model                             # 31591  <-- lecture avant liaison
  elif _paused_sans_cle:
      return _anthropic_error(503, "All API keys paused and no free model available.")   # 31595
  elif _geo_tunnel:                                        # 31596  <-- TEST DUPLIQUÉ
      async with _open_via_pool(endpoint, anthro_body, a_headers, is_stream=False, …) as resp:   # 31598-31605
          a_headers = dict(resp.headers)
  else:
      resp, a_headers = await _do_request_with_retry(endpoint, anthro_body, a_headers, "anthropic")   # 31607
  ```
- **Défaut A — `UnboundLocalError` → 500.** `_actual_model` n'est lié qu'aux l. 31319, 31423 (bloc `if not is_stream:`, qui **se termine par `return`** l. 31566, donc jamais traversé) et 31588 (branche `free_result is not None`). Aucun global `_actual_model` au niveau module (vérifié : `grep '^_actual_model'` = vide). Quand `free_result is None` **et** `_geo_tunnel` vrai, la l. 31591 lit une variable locale non liée. Les deux `except` du `try` (l. 31608-31611) sont `FreeRefusal` et `UpstreamError` : **aucun ne l'attrape**. Aucun `try` englobant dans `responses` (vérifié par AST). L'exception remonte donc à `@app.exception_handler(Exception)` (`debug_exception`, l. 4963-4981) → **500** `{"error":{"message":"Internal server error"}}`.
- **Défaut B — le `elif _geo_tunnel` de la l. 31596 est mort.** `elif` évalue dans l'ordre : si `_geo_tunnel` est vrai, la l. 31590 le capture ; s'il est faux, la l. 31596 est fausse aussi. **La route tunnel payante (`_open_via_pool`) des l. 31598-31605 n'est jamais empruntée.** Une requête payante Anthropic géo-restreinte tombe donc dans le `else` de la l. 31607 → `_do_request_with_retry` **en direct**, hors tunnel.
- **Reachabilité** : `config.yaml` a `geo.enabled: true` avec la politique `meta-muse-spark` en `require_vpn: true` ; `_enforce_geo_gate` pose `request.state._geo_force_tunnel = True` (l. 6228, 6262) quand l'IP directe n'est pas dans le pays autorisé. Le chemin est donc atteint dès qu'un client demande un modèle `muse-spark-*` en streaming depuis un pays non autorisé, sans clé payante utilisable. **Constitution XVII** : la porte géo est fail-closed par conception ; ce `else` direct la contourne.
- **Vérification** : le motif exact a été reproduit en isolation (`UnboundLocalError: cannot access local variable …`), et la structure `if/elif/elif` a été extraite par AST (chaîne : `free_result is not None` → `_geo_tunnel` → `_paused_sans_cle` → `_geo_tunnel` — le 4ᵉ test est une copie du 2ᵉ).
- **Recommandation** : remplacer les deux `elif _geo_tunnel:` par un seul, et décider explicitement du sort du chemin tunnel géo. La branche tunnel `_open_via_pool` (l. 31598-31605) semble être **l'intention d'origine** (« Axe A: geo-restricted paid → must route through tunnel station ») : la restaurer sous un test qui n'a pas déjà été consommé (par ex. un `else` interne, ou tester `_geo_tunnel` une seule fois en amont). Ajouter un test avec `_geo_force_tunnel=True` + `_try_free_model_first → None`.
- **Effort** : **S** — **Risque** : moyen (la correction change le routage géo d'une jambe payante ; à valider par un test de porte géo).

### P1

#### P1-1 — 367 `except …: pass` nus (30 % des 1 225 gestionnaires)

- **Preuve** : analyse AST sur `opencode.py` — 1 225 `ExceptHandler`, dont **367** avec un corps réduit à `pass` (30 %). Le reste est majoritairement `except Exception:` suivi d'un seul `_debug(...)` (1027 occurrences de `except Exception`).
- **Impact** : c'est ce qui rend P0-2 invisible depuis 2026-09-15. Aucun signal (ni compteur, ni warning non-DEBUG) pour une classe entière d'erreurs — y compris des `NameError`/`AttributeError`/`UnboundLocalError` de programmation, qui ne sont **pas** des erreurs d'environnement.
- **Recommandation** : introduire un helper `_swallow(exc, where)` qui compte dans `_MISC_COUNTERS` et logge en `warning` **une fois par site** (anti-flood), puis migrer par lots les `except Exception: pass` des chemins de requête (handlers, `_try_free_model_first`, `_open_free_stream`, `_do_request_with_retry`). Cible : 0 `except Exception: pass` dans `opencode.py`, tolérés uniquement sur les lectures best-effort d'observabilité.
- **Effort** : **L** (mécanique mais volumineux, à faire lot par lot) — **Risque** : moyen (le bruit de log peut noyer `debug.log` si mal throttlé).

#### P1-2 — Deux conversions free concurrentes : `_free_wire_body` et `_systemone_free_via_pool`

- **Preuve** : `_free_wire_body` (l. 8279-8765, 486 l.) construit le corps wire officiel complet (grille d'outils, system, env block, trim). `_systemone_free_via_pool` (l. 29901-30523, 623 l.) reconstruit **sa propre** mécanique : boucle multi-station, cooldown, rotation, `_mark_free_stations_429`, repli direct — en dupliquant la logique de `_try_free_model_first` (l. 16474-17650). Le docstring l. 29906-29909 l'admet : « sans les conversions chat/responses, ni la grille tools, ni effort : le hedge free_parallel et la boucle multi-station sont **partagés avec la jambe générique** » — mais le code de la boucle est **recopié**, pas partagé.
- **Impact** : deux chemins à corriger pour chaque correctif de 429/rotation/cooldown. Toute divergence (ex. `_free_retry_delay_seconds` appliqué dans l'un et pas l'autre) est invisible.
- **Recommandation** : extraire la boucle d'essais (cooldown → pick station → POST → classer 429 → cooldown → retry) dans `free/attempts.py`, paramétrée par un `wire_builder` et un `response_reader`. `_try_free_model_first`, `_open_free_stream`, `_systemone_free_via_pool` et `_hedged_fetch` deviennent des appelants.
- **Effort** : **L** — **Risque** : élevé (chemin le plus critique du système, cf. §7).

#### P1-3 — Quatre blocs d'extraction d'usage quasi identiques

- **Preuve** : `usage.get("input_tokens") or usage.get("prompt_tokens")` apparaît à l. 12631, 17461, 20805, 20901, 21173, 21283, 28974, 29118, 31320… ; `usage.get("prompt_tokens")` à l. 12125, 12195, 12305, 17402, 22482, 22954, 27187, 27595, 27626. Le pattern complet (extraire in/out/cache + `_update_token_usage` + `_save_and_log_request` + réponse) est répété **15 fois** (`if free_result is not None:` → l. 12567, 20801, 20946, 22478, 22580, 27179, 27272, 28970, 29036, 31318, 31422, 31587, 31778, 31891, 32221).
- **Impact** : une correction d'usage (par ex. `cache_creation_input_tokens` non lu sur une branche) doit être appliquée 15 fois. Le finding CLAUDE.md « l'usage OpenAI était relayé en `input_tokens` **et** `cache_read_input_tokens` → autocompact » est exactement cette classe.
- **Recommandation** : `usage.py::anthropic_usage_from_openai(usage, protocol)` (déjà partiellement dans `app/protocol/mapping.py`) **plus** un helper `record_free_result(req_id, free_result, ctx)` qui absorbe le triptyque extraction/`_update_token_usage`/`_save_and_log_request`.
- **Effort** : **M** — **Risque** : moyen (le logging est un contrat avec le dashboard).

#### P1-4 — 37 sites de reprise de flux dupliqués (`_terminate_after_started`)

- **Preuve** : `_stream_has_yielded(...)` apparaît **37 fois** et `_terminate_after_started(...)` **23 fois** (`grep -c`), avec 10 occurrences du bloc littéral :
  ```python
  if _stream_has_yielded(started, open_blocks, stream_out, _line_buf):
      _cb_record_failure(endpoint)
      _log(f"  stream_retry_suppressed_after_started attempt={_attempt + 1}")
      async for ev in _terminate_after_started(
          open_blocks, stream_out,
          thinking_idx=thinking_block_idx,
          thinking_sig=_local_signature(thinking_acc) if thinking_block_idx is not None and thinking_acc else "",
      ):
          yield ev
      return
  ```
  (ex. l. 21610-21622, 21687-21700, 21705-21718, 21732-21745, 22040, 22065, 22102, 22129, 23214, 23233, 23316, 23333, 23355, 23925, 23945, 23978, 24130, 24146).
- **Impact** : la sémantique « ne jamais concaténer un retry après des octets émis » dépend de 23 copies. Une copie oubliée = flux client corrompu (deux `message_start`). C'est la famille de bugs la plus coûteuse à diagnostiquer.
- **Recommandation** : helper unique `maybe_abort_started(ctx) -> AsyncIterator[bytes] | None` où `ctx` est un petit dataclass `StreamCtx(open_blocks, stream_out, thinking_idx, thinking_acc, line_buf, endpoint, attempt)`. Les 23 sites deviennent `if (ev := abort(ctx)): async for c in ev: yield c; return`.
- **Effort** : **M** — **Risque** : moyen (le comportement doit rester byte-identique ; les tests de flux existent).

#### P1-5 — Trois constructions de la tête d'en-têtes free, divergentes

- **Preuve** : `_open_free_stream` l. 14110-14127, le repli direct l. 14345-14348, et `_do_free_request_curl_cffi` l. 11110-11127 — les trois répètent `_station_free_headers` / `_official_free_headers` → `_guard_free_wire_headers` → `_get_free_fp_kwargs` → `_free_wire_body` → `_note_current_wire_fp`, avec des variantes de calcul de `_ident_version` :
  - l. 11114-11125 : repli `_ip_vault().get(egress).version` puis `_FREE_CANONICAL_VERSION`
  - l. 14116 : `_station_runtime.version` ou `(_inst_snap or {}).get("version")`
  - l. 14346 : `(_direct_inst or {}).get("version")`
- **Impact** : **constitution XVIII** (« Une IP se brûle au premier format non conforme »). Trois calculs de version différents pour le même objectif de parité ⇒ risque de wire hybride (l'anti-pattern explicitement interdit, documenté l. 6861-6872). C'est aussi l'invariant A.0 (`tests/test_invariant_a0.py`, 780 lignes) que ces chemins doivent tenir.
- **Recommandation** : une seule fabrique `free_wire.build_request(station, egress_ip, body, endpoint, conversation_key) -> FreeWire(headers, impersonate, fp, body, version)` utilisée par les 3 sites.
- **Effort** : **M** — **Risque** : élevé (invariant A.0, brûlure d'IP).

#### P1-6 — `_geo_forced_pool` global écrit, jamais lu

- **Preuve** : `opencode.py:2308` déclaration ; `global _geo_forced_pool` l. 6061 ; écritures l. 6282 et 6592. **Aucune lecture** du global (toutes les lectures passent par `request.state._geo_forced_pool` — 40+ sites). Idem `_geo_pinned_country` (l. 2305, écrit l. 6453, **jamais lu**) et `_geo_pinned_station` (l. 2306, **jamais lu ni écrit**).
- **Impact** : état global mort qui donne l'illusion d'un partage inter-requêtes. Sur un proxy concurrent, un global `_geo_forced_pool` **serait** une fuite inter-requêtes (une requête verrait le pool géo d'une autre) — heureusement inoffensif puisque non lu, mais c'est un piège pour le prochain lecteur.
- **Recommandation** : supprimer les 3 globaux et le `global` l. 6061 ; garder uniquement `request.state`.
- **Effort** : **S** — **Risque** : faible.

#### P1-7 — Mutable globaux mutés sans verrou depuis le chemin de requête

- **Preuve** (mutations hors verrou, toutes atteignables par requête concurrente) :
  - `_geo_breaker[(c, 0)] = …` — l. 6533, 6537, 6540, 6544, 6561, 6564 (dict global, aucun verrou ; lu l. 6360)
  - `_geo_tray_last[key] = now` — l. 5862 (croissance non bornée : une clé par `model`, bornée en pratique par le nombre de modèles)
  - `_free_model_rr[paid_model] = idx + 1` — l. 15239 (round-robin, borné par le nombre de payants)
  - `_free_429_by_model[model] = …` — l. 15269 (compteur, borné par les modèles)
  - `_CONVERSATION_SESSIONS[key] = sid` — l. 9805 (LRU bornée 512, l. 9806-9807 — OK)
  - `_tools_used_seen` — alimenté depuis le **thread writer** (`observability/db.py:205`) et lu depuis la boucle (`opencode.py:4995`, `lambda: set(_tools_used_seen)`) : `set` non protégé, lu pendant mutation → `RuntimeError: Set changed size during iteration` possible.
- **Impact** : sous GIL les opérations atomiques survivent, mais `set(_tools_used_seen)` **itère** un set muté par un autre thread. Le `lambda` est appelé par le dashboard (`/api/history/filters`) — un 500 sporadique du dashboard est possible, jamais du proxy.
- **Recommandation** : `_tools_used_seen` sous un `threading.Lock` (ou copie sous lock) ; `_geo_breaker` sous `_free_cooldown_lock` ou un lock dédié ; documenter les autres comme intentionnellement non synchronisés (GIL + clés bornées).
- **Effort** : **S** — **Risque** : faible.

### P2

#### P2-1 — I/O réseau dans le chemin de requête (`_enforce_geo_gate`)

- **Preuve** : `_enforce_geo_gate` (l. 6053-6594) appelle `await _direct_country()` (l. 6181, 6248) et `await _get_direct_ip()` (l. 6186, 6253), qui font un `GET https://api.ipify.org` (`_get_cached_public_ip`, l. 14637-14663, `timeout=10 if expect_tunnel else 5`) avec cache 60 s. La porte est appelée **en tête de chacun des 3 handlers** (l. 20732, 27062, 31195) et **à chaque tentative free**.
- **Impact** : **constitution XVII** (« Aucune I/O réseau … dans le chemin de requête »). Le cache 60 s amortit, mais une requête sur cache froid paie jusqu'à **10 s** avant de forwarder. `_direct_country()` (l. 14731-14789) a sa propre logique de probe.
- **Recommandation** : préchauffer les deux caches dans une tâche de fond (comme `_free_prewarm_loop`) et faire la porte **fail-open sur cache vide** plutôt que d'attendre un probe.
- **Effort** : **M** — **Risque** : moyen (la porte est fail-closed par conception ; à ne pas affaiblir).

#### P2-2 — `_build_metrics_text` lit un fichier de log à chaque scrape Prometheus

- **Preuve** : l. 5053-5070 : `open(logs/app-supervisor.log)` + itération complète sur le fichier à **chaque** appel de `GET /metrics` (route l. 5336). Aucun cache.
- **Impact** : Prometheus scrape toutes les 15-30 s ; `app-supervisor.log` est append-only et croît (59 Ko aujourd'hui, mais sans borne). Le fichier est relu intégralement, sur la boucle d'événements (pas de `to_thread`).
- **Recommandation** : lire l'offset une fois, mémoriser `(offset, restarts, health_fail)`, reprendre à l'offset suivant. Ou déporter dans `observability/metrics.py` avec un cache TTL.
- **Effort** : **S** — **Risque** : faible.

#### P2-3 — `_db_queue_bytes` peut rester faux (dérive du cap mémoire)

- **Preuve** : incrément l. 1539 `_db_queue_bytes += _row_sz` **après** `put_nowait` (l. 1517). Si `put_nowait` lève `QueueFull` (l. 1518), on passe au fallback synchrone **sans** incrémenter — correct. Mais si `_materialize_db_row` / `_db_insert_sync` (fallback) réussissent, aucune compensation n'est nécessaire — correct aussi. **En revanche** : le décrément se fait dans `_db_writer_loop` via `_db_item_bytes(item)` qui, pour un item dont les corps ont été **droppés** (l. 1499-1512), renvoie **0** alors que `_row_sz` valait 0 aussi — cohérent. Le vrai défaut : `_db_row_bytes` (l. 855-868) renvoie `4096` pour tout corps non-str/bytes, alors que l'incrément utilise la même fonction — cohérent. **Conclusion : pas de bug**, mais le compteur n'est **pas** remis à zéro au reboot du writer (l. 4283 le fait dans le lifespan). Zone à surveiller, pas un finding ferme.
- **Statut** : **rétracté** après vérification ligne à ligne. Conservé ici comme trace de la vérification.

#### P2-4 — Regex et imports compilés/chargés dans les chemins chauds

- **Preuve** : `re.match(r"…")` sans `re.compile` à l. 10980, 13028, 17698, 19860 (le module cache interne de Python amortit, mais le lookup est répété). Imports locaux dans les fonctions : **164** au total, dont `import uuid as _uuid` (l. 6783) et `import re as _re` (l. 6776) **après** l'échec d'I/O dans `_proxy_session_id` (une seule fois par processus, acceptable) ; `from config.limits import …` dans `_clamp_native_output_limit` (l. 20464, appelé **à chaque requête** des 3 handlers).
- **Recommandation** : hisser `from config.limits import clamp_output_token_limit` au niveau module (l'import est peu coûteux mais le `try/except` par requête ne l'est pas) ; précompiler les 4 regex en constantes de module.
- **Effort** : **S** — **Risque** : faible.

#### P2-5 — Trois horloges de session persistées en fichier, logique copiée-collée

- **Preuve** : `_proxy_session_id` (l. 6761-6794), `_free_session_id` (l. 9698-9745), `_paid_session_id` (l. 10636-10671) — même structure : cache mémoire → lecture fichier → validation → mint → écriture atomique `.tmp` + `os.replace` → fail-soft. Trois fichiers sous `logs/` (`_proxy_session_id`, `_free_session_id`, `_paid_session_id`).
- **Impact** : duplication à 3 exemplaires ; le contrôle de format diffère (`re.fullmatch` l. 6778 ; `startswith("ses_") and len == 30` l. 9727-9730 ; à vérifier l. 10650).
- **Recommandation** : `core/session_file.py::PersistedSession(path, mint_fn, validate_fn, rotate_s)`. Gain : ~90 lignes, un seul endroit pour l'écriture atomique.
- **Effort** : **S** — **Risque** : faible.

#### P2-6 — `_ENV`/`.env` lus à chaque boot dans le lifespan (lignes 3376-3400, 3878)

- **Preuve** : `open(config.yaml)` l. 3376, deux passes sur `_creds_path` l. 3387 et 3400, `open(_path)` l. 3878. Tous au boot (hors chemin de requête) mais synchrones dans le lifespan async → bloquent la boucle pendant le boot.
- **Impact** : contribue au « boot complet mesuré ~8,5 s » (commentaire l. 33060). Non bloquant pour les requêtes (le serveur n'écoute pas encore), mais retarde `/readyz`.
- **Effort** : **S** (`await asyncio.to_thread(...)`) — **Risque** : faible.

### P3

#### P3-1 — `ensure_min_tokens` : docstring transformée en statement mort

- **Preuve** : `opencode.py:20473-20476` —
  ```python
  def ensure_min_tokens(body: dict, default: int = None) -> dict:
      if default is None:
          default = yaml_get("thinking", "default_min_tokens", 256)
      """Ajuste max_output_tokens pour les modèles thinking afin qu'il
      reste des tokens pour la réponse après le reasoning."""
  ```
  Vérifié par AST : `docstring present: False` (la docstring est le **2ᵉ** statement, donc une expression constante évaluée puis jetée). Aucun `__doc__`.
- **Impact** : purement cosmétique (pas de comportement), mais `help()`/IDE/docs perdent la description. Même défaut à `_truncate` (l. 2872-2875).
- **Effort** : **S** — **Risque** : nul.

#### P3-2 — `_WARN` de contexte énorme appelé avant lecture du corps

- **Preuve** : l. 20572, 26939, 31088 — `_warn_huge_context(request)` est appelé **avant** `await request.body()` (l. 20575, 26942, 31091). La fonction (l. 916-942) lit `Content-Length` de l'en-tête : correct, aucun coût. Pas un bug — noté car la lecture est O(1) et c'est **volontaire**. Rétracté.

#### P3-3 — Commentaires-dates et marqueurs historiques non nettoyés

- **Preuve** : 98 marqueurs `# [AAAA-MM-JJ]` (le plus fréquent : `2026-11-12` ×14, `2026-10-25` ×14, `2026-10-26` ×12) ; 8 `# [FIX trou]` ; 26 occurrences de `legacy` ; 17 de `shim`. Aucun n'est un bug, mais ils racontent l'historique du fichier et alourdissent la lecture (4 279 lignes de commentaires, 16 % du fichier).
- **Recommandation** : lors de chaque extraction, migrer les commentaires vers le module cible et n'en garder qu'un résumé à l'ancien emplacement (ou rien). Ne pas faire de passe globale : le risque de perdre une décision tracée est réel.

---

## 4. Inventaire du code mort

Chaque ligne est prouvée par un grep repo-entier (hors `Lib/`, `_research/`, `.audit-*`).

| Symbole | Ligne | Nature | Preuve | Verdict |
|---|---|---|---|---|
| `_db_vacuum_if_needed` | 1014 | Fonction | Aucune référence hors sa définition et `logs/probe_vacuum.py` (script de sonde) | **Supprimer** (14 l.) |
| `_CANARY_SYSTEM` | 24234 | Constante | `grep -c` = 1 (la définition) | **Supprimer** |
| `_FB_FALLBACK_CAUSES` | 17821 | Constante | `grep -c` = 1 ; `observability/metrics.py:439` a son propre `FALLBACK_CAUSES` | **Supprimer** |
| `_LAT_RING_MAX` | 17854 | Constante | `grep -c` = 1 ; `observability/metrics.py:93` a son propre `LAT_RING_MAX` (paramètre par défaut) | **Supprimer** |
| `_PATH_TOOL_NAMES` | 9065 | Constante | `grep -c` = 1 ; `streaming/tool_paths.py` a sa propre liste | **Supprimer** |
| `_PATH_ARG_ALIASES` | 9068 | Constante | `grep -c` = 1 ; idem | **Supprimer** |
| `_JSON_LIB` | 1823 | Constante | `grep -c` = 1 dans `opencode.py` ; le vrai est `app/protocol/mapping.py:92` (exposé par `protocol_mapping.py:33`) | **Supprimer** |
| `_get_enabled_keys` | 224 | Wrapper | `grep -c` = 1 ; `_keys_mod.enabled_keys` est appelé directement par `core/keys.py` | **Supprimer** |
| `_env_key_or_raise` | 228 | Wrapper | `grep -c` = 1 | **Supprimer** |
| `_workspace_for_key` | 420 | Helper | `grep -c` = 1 | **Supprimer** |
| `_truncate_body_for_storage` | 726 | Wrapper | `grep -c` = 1 (le consommateur réel est `_app_db.truncate_body_for_storage`) | **Supprimer** |
| `_with_client_anthropic_betas` | 904 | Helper | `grep -c` = 1 | **Supprimer** |
| `_free_request_headers` | 6797 | Helper | `grep -c` = 1 ; l'invariant A.0 est tenu par `_official_free_headers` / `_guard_free_wire_headers` | **Supprimer** (après confirmation A.0) |
| `_is_web_search_forced` | 19557 | Helper | `grep -c` = 1 | **Supprimer** |
| `_strip_web_search_tool` | 19676 | Wrapper | `grep -c` = 1 (`_strip_web_tool` l. 19671 est le vrai) | **Supprimer** |
| `_geo_pinned_country` | 2305 | Global | Écrit l. 6453, jamais lu | **Supprimer** |
| `_geo_pinned_station` | 2306 | Global | Jamais lu ni écrit | **Supprimer** |
| `_geo_forced_pool` | 2308 | Global | Écrit l. 6282/6592, jamais lu (tout passe par `request.state`) | **Supprimer** |
| `elif _geo_tunnel` (2ᵉ occurrence) | 31596 | Branche | Test identique au l. 31590, déjà consommé → **toujours faux** ; le corps l. 31598-31605 (`_open_via_pool`) est inatteignable | **Corriger** (P0-3 défaut B) |
| `_sse_keepalive` / `_sse_coalesce` | 19289 / 19380 | Wrappers | Appelés **uniquement** par `tests/test_sse_keepalive.py` et `tests/test_sse_coalesce.py` ; le code de prod appelle `_sse_pump` directement (l. 20851, 21818, 22412, 24218, 28900, 29893) | **Migrer vers un module de test** (46 l. hors prod) |
| `protocol_mapping.py` (fichier entier) | — | Shim | 26 fichiers de tests + `scripts/bench_perf.py:218` + `scripts/gen_golden_fixtures.py:26` ; **un seul consommateur de prod** : `opencode.py:5312` (`import protocol_mapping as _pm` dans `_build_metrics_text`) | **Migrer** : faire lire `app.protocol.mapping.conversion_cache_stats` directement à `_build_metrics_text`, puis supprimer le fichier en Phase 9 (déjà planifié dans son propre docstring) |
| `free_discovery.py` | — | Facade | Consommateur unique : `tests/test_coverage_boost.py:22` | **Supprimer** (51 l.) |
| `traffic_capture.py` | — | Shim | Consommateurs : `tests/test_traffic_capture.py`, `tests/test_coverage_boost.py` | **Supprimer** (26 l.) |
| `free_ip_pool.py` | — | Shim | Prod : `opencode.py:3422` (chemin « historique » du lifespan) ; + 20 fichiers de tests | **Garder court terme**, cible Phase 9 |
| `shared_rotation.py` | — | Shim | Prod : `opencode.py:3423` ; + `vpn/manager.py:42`, 4 tests | **Garder court terme**, cible Phase 9 |
| `latency_rotation.py` | — | Shim | Prod : `opencode.py:3525, 13179, 17486, 30400`, `free/pool/__init__.py` ×4, `ops/supervisor.py:198,208`, 3 routers dashboard | **Garder** (fortement consommé) |
| `_compact_body_stub`, `_quick_body_size` | 737, 733 | Wrappers | Utilisés (`_app_db` délègue) | **Garder** |
| `api_keys.json` | — | Fichier | Contenu `[]` (2 octets) ; `config/settings.py:2345` le lit, `save_api_keys` (l. 2374) l'écrit | **Garder** (fichier d'état, vide = aucune clé JSON ; la clé vit dans `.env`) |
| `_resp_json_hors_boucle` | 33605 | Fonction | **Vivante mais inatteignable en `__main__`** → voir P0-2 | **Déplacer** |

---

## 5. Duplications internes

| # | Sujet | Plages / sites | Lignes dupliquées (est.) |
|---|---|---|---|
| D1 | Extraction d'usage + `_update_token_usage` + `_save_and_log_request` + réponse, bloc `if free_result is not None:` | 12567, 20801, 20946, 22478, 22580, 27179, 27272, 28970, 29036, 31318, 31422, 31587, 31778, 31891, 32221 | ~15 × 45 = **675** |
| D2 | Bloc « retry supprimé après octets émis » (`_stream_has_yielded` + `_terminate_after_started`) | 23 sites (21610, 21687, 21705, 21732, 22040, 22065, 22102, 22129, 23214, 23233, 23316, 23333, 23355, 23925, 23945, 23978, 24130, 24146, …) | ~23 × 12 = **276** |
| D3 | Construction de la tête d'en-têtes free (`_station_free_headers`/`_official_free_headers` → `_guard_free_wire_headers` → `_get_free_fp_kwargs` → `_free_wire_body` → `_note_current_wire_fp`) | 11110-11127, 14093-14108, 14345-14348 | ~3 × 18 = **54** |
| D4 | Bloc de préambule des handlers (`req_id`, `start_time`, `client_ip`, ContextVars, `body_bytes`, 413, parse JSON, `_mark_requested_model`, `tool_names`, `request_body`, `_install_client_tool_schemas`, détection compaction, `DEBUG` dumps) | 20567-20624 / 26934-26980 / 31083-31125 | ~3 × 55 = **165** |
| D5 | Garde « pas de clé payante utilisable » (`_has_usable_paid_key` + message + refus) | 23823-23830, 27282-27290, 27904-27912, 28395-28403 | ~4 × 12 = **48** |
| D6 | Collecteurs SSE bufferisés (`_collect_chat_completion` l. 9407, `_collect_responses_object` l. 9575, `_collect_responses_sse_object` l. 16344, `_chat_completion_from_any_json` l. 20076, `_sse_lines_from_buffered_json` l. 20091, `_responses_output_has_visible_content` l. 20174) | 9407-9600, 16344-16397, 20076-20257 | **~450** (6 variantes du même besoin) |
| D7 | Boucle d'essais free (cooldown → station → POST → 429 → rotation) | `_try_free_model_first` 16474-17650, `_open_free_stream` 13846-14486, `_systemone_free_via_pool` 29901-30523, `_hedged_fetch` 12772-13001 | **~900** |
| D8 | Persistance de session en fichier (`cache → read → validate → mint → tmp+replace`) | 6761-6794, 9698-9745, 10636-10671 | **~90** |
| D9 | Hachage des en-têtes sensibles pour le namespace de cache | `_response_cache_namespace` 2975-3012 (`blake2b`) ; `_note_current_wire_fp` 8258-8276 | **~20** |
| D10 | Table de correspondance `finish_reason` ↔ `stop_reason` | 67 occurrences de `finish_reason`, 41 de `stop_reason` ; les tables vivent dans `app/protocol/mapping.py` **et** dans les handlers (ex. l. 20411, 24151, 32797) | **~120** |

**Total dupliqué estimé : ~2 800 lignes** (≈ 10 % du code du fichier).

---

## 6. Violations de la constitution

| Principe | Texte | Violation | Preuve |
|---|---|---|---|
| **XIV** | « Aucun nouveau module ne dépasse 1 500 lignes ; les monolithes existants sont extraits par lots. » | `opencode.py` **33 618 l.**, `vpn/manager.py` **11 471**, `free/pool/__init__.py` **8 362**, `app/protocol/mapping.py` **6 329**, `dashboard/api.py` **3 047**, `config/settings.py` **2 782** — 6 fichiers au-delà de la borne, dont 4 **hors** baseline de monolithe | `scripts/module_size_audit.py:15` (`DEFAULT_MAX_LINES = 1500`) ; `wc -l` |
| **XIV** | « La dette est suivie dans `debt.md` : 0 P0 ouverte aux gates, P1 planifiée, pas de nouvelle dette non-listée. » | `docs/DEBT.md` (état 2026-11-10) **ne liste pas** : le double import (P0-1), le `NameError` (P0-2), ni l'extraction des handlers HTTP (D-014-04 mentionne « S1–S8 et S11–S13 non extraites » mais sans nommer les 3 handlers) | `docs/DEBT.md` (fichier est à `docs/DEBT.md`, pas `debt.md`) |
| **XVII** | « Aucune I/O réseau, aucun `sleep`, aucun scan lourd dans le chemin de requête. » | `_enforce_geo_gate` → `_direct_country()`/`_get_direct_ip()` → `GET api.ipify.org` (timeout 10 s) en tête des 3 handlers (l. 6181, 6186, 6248, 6253) | P2-1 |
| **XVII** | idem | `_build_metrics_text` lit intégralement `logs/app-supervisor.log` à chaque `/metrics` (l. 5068-5072) | P2-2 |
| **XVII** | idem | `asyncio.sleep(wait + 0.25)` dans `_free_preflight_gate` (l. 15893) : sommeil **jusqu'à 30 s** (`free.preflight_inline_wait_s`) dans le chemin de requête, avant tout forward | l. 15874-15904 |
| **X** | « La dégradation est explicite et reste free » | P0-3 défaut B : la requête payante géo-restreinte part **en direct** (`_do_request_with_retry`, l. 31607) au lieu du tunnel `_open_via_pool`, à cause d'un `elif` mort — la porte géo est contournée sans le dire | l. 31590-31607 |
| **XVII** | « Toute écriture critique est transactionnelle » | Deux `sqlite3.Connection` sur la même base dans le même processus (P0-1) : l'une écrit (writer), l'autre est ouverte sans usage et sans `PRAGMA` — deux propriétaires pour un seul cycle de vie | P0-1, preuve `logs/debug.log` |
| **XVI** | « Aucune fenêtre intempestive, exécution silencieuse » | `Start.bat` lance `python opencode.py` **sans** `--gui` explicite ni `pythonw` dans la branche de repli (`start "" /B pythonw opencode.py` sinon `python opencode.py`) — sur un poste sans `pythonw` dans le PATH, une console s'ouvre. `setup.bat` fait `python opencode.py` en fin de script (console assumée pour l'install). | `Start.bat:16-21`, `setup.bat:44` |
| **III** | « Une seule source de vérité par cycle de vie » | `custom_routes.json` et `config.yaml:custom_routes` **divergent** : 8 clés vs 11, cibles `muse-spark-1.2-contributor` vs `{deepseek-v4.1-flash, glm-5.3-flash-free, mimo-v2.6-flash}`. `load_custom_routes` (l. 2275-2290) lit **YAML d'abord**, JSON en repli — le JSON n'est donc jamais lu en pratique, mais reste un piège (un opérateur qui l'édite ne change rien) | `config/settings.py:2275-2290`, comparaison par script |

---

## 7. Options / configuration sans consommateur

Croisement automatique des 93 clés `yaml_get(section, key)` de `opencode.py` contre `config.yaml` : **91/93 présentes**, 2 absentes (défauts appliqués) :

| Clé | Statut | Preuve |
|---|---|---|
| `ip_rotation.control_api_key` | Absente de `config.yaml`, lue avec défaut | `grep yaml_get("ip_rotation", "control_api_key"` |
| `latency_rotation.prewarm_after_rotate` | Absente de `config.yaml`, lue avec défaut | idem |

**Clés présentes dans `config.yaml` sans consommateur identifié dans `opencode.py`** (consommées ailleurs ou mortes) :

| Clé | Consommateur | Verdict |
|---|---|---|
| `background.quota_fetch_interval` | `dashboard/quota.py` (hors `opencode.py`) | OK |
| `background.free_models_refresh_interval` | `config/settings.py` | OK |
| `web_search.*` / `web_fetch.*` | `server/websearch.py` | OK |
| `models_catalog.*` | `scripts/check_models_catalog.py` seulement | **Orphelin de prod** : aucun consommateur dans le proxy |
| `api_keys: []` | `config/settings.py:2356` (repli YAML) | OK (vide par conception) |
| `egress_catalog.*` | `config/settings.py:526`, `dashboard/api.py:2881` | OK |
| `control_plane.*` | `free/control_plane/config.py` | OK |
| `observability.slo_targets` | `opencode.py:25225` | OK |

**Option GUI sans effet réel (constitution XII)** : `free.max_client_tools` est passé à **0** le 2026-11-18 (CHANGELOG) et le code lit bien la clé (`_free_max_client_tools`, l. 7519-7524), mais `_select_free_client_tools` (l. 7527) retourne `[]` dès que `max_tools <= 0` — la valeur 0 **désactive** silencieusement une fonctionnalité au lieu de la borner. Un opérateur qui met `3` réactive une grille d'outils **non conforme** au gate (risque XVIII). La valeur 0 devrait être refusée par la validation de config.

**`--gui` est un no-op documenté** (`opencode.py:33480` : `help="(default, no-op for backward compat)"`) — acceptable, mais `lancer-gui.bat` et `lancer-silent.vbs` le passent explicitement, ce qui laisse croire qu'il active quelque chose.

---

## 8. Fichiers racine orphelins : verdict

| Fichier | LOC | Consommateurs (hors `Lib/`, `_research/`, `.audit-*`) | Verdict |
|---|---|---|---|
| `protocol_mapping.py` | 171 | Prod : `opencode.py:5312` **uniquement** (`conversion_cache_stats`). Tests : 26 fichiers. Scripts : `bench_perf.py:218`, `gen_golden_fixtures.py:26` | **Fusionner** : faire lire `app.protocol.mapping` directement par `_build_metrics_text`, migrer les tests vers `app.protocol.mapping`, **supprimer** |
| `free_discovery.py` | 51 | `tests/test_coverage_boost.py:22` seulement | **Supprimer** (facade sans consommateur de prod) |
| `free_ip_pool.py` | 25 | Prod : `opencode.py:3422` (chemin historique du lifespan) ; 20 fichiers de tests ; `scripts/smoke_todo10.py` | **Garder court terme** (le lifespan doit migrer vers `free.pool` d'abord), cible Phase 9 |
| `latency_rotation.py` | 28 | Prod : `opencode.py` ×4, `free/pool/__init__.py` ×4, `ops/supervisor.py` ×2, `dashboard/routers/*` ×3 ; tests ×3 | **Garder** (surface réellement utilisée) — envisager d'unifier les imports vers `free.rotation` pour supprimer le shim |
| `shared_rotation.py` | 28 | Prod : `opencode.py:3423`, `vpn/manager.py:42` ; tests ×3 | **Garder court terme**, cible Phase 9 |
| `shared_state.py` | 36 | Prod : ~30 `import shared_state` dans `opencode.py` + `dashboard/` + `free/` | **Garder** — c'est le seam anti-import-circulaire, documenté et utilisé |
| `traffic_capture.py` | 26 | `tests/test_traffic_capture.py`, `tests/test_coverage_boost.py` | **Supprimer** (le canonique `observability.capture` est importé directement par `opencode.py:4894-4895`) |
| `ip_latency.py` | 167 | Prod : `free/rotation.py:62`, `ops/supervisor.py:33` ; tests ×2 | **Garder** (module pur, consommé) |
| `trust.py` | 376 | Prod : `opencode.py:5027` (`ClientAuthMiddleware`), `dashboard/api.py:266, 2274`, `server/throttle.py:209` ; tests ×2 | **Garder** — mais **c'est un module >150 l. qui viole XIV s'il est compté comme nouveau** ; il est antérieur à la règle. Cible : découper en `trust/` (auth, csrf, rate-limit) |
| `custom_routes.json` | 51 | **Aucun consommateur effectif** : `config/settings.py:2282` ne le lit qu'en **repli** si `config.yaml:custom_routes` est vide — il ne l'est pas | **Supprimer** (ou renommer en `.example` et documenter) — voir constitution III |
| `api_keys.json` | 0 (2 octets : `[]`) | `config/settings.py:2345` (lecture), `save_api_keys` (écriture) | **Garder** (fichier d'état du dashboard) |
| `gunicorn.conf.py` | 27 | `opencode.service:15` (`ExecStart=gunicorn -c gunicorn.conf.py opencode:app`) ; mentionné dans `config/settings.py:146`, `opencode.py:580`. **Non utilisé** par Dockerfile (`CMD ["python","opencode.py","--no-gui"]`) ni par le runtime local Windows | **Garder** (déploiement Linux documenté) — noter qu'il est **inutilisable en `__main__`** (donc immunisé contre P0-1/P0-2, `opencode:app` étant un import propre) |
| `Start.bat` | 604 o | Aucun autre script ne l'appelle | **Fusionner** avec `lancer-proxy.bat` (les deux lancent `pythonw opencode.py` ; `Start.bat` ajoute la recherche d'OpenVPN, obsolète depuis l'audit 2026-10-24 qui a retiré openvpn du Dockerfile) |
| `lancer-gui.bat` | 1 167 o | Aucun | **Garder** (chemin GUI + superviseur de chaîne) |
| `lancer-proxy.bat` | 1 611 o | Aucun | **Garder** (chemin sans GUI) |
| `lancer-silent.vbs` | 874 o | Aucun | **Garder** — mais **chemin Python 3.12 en dur** (`C:\Users\julie\AppData\Local\Programs\Python\Python312\pythonw.exe`) : non portable, à documenter |
| `setup.bat` | 1 276 o | Aucun | **Fusionner** avec `setup.ps1` (les deux installent les deps + OpenVPN) |
| `setup.ps1` | 3 748 o | Aucun | **Fusionner** — `setup.bat` est le sous-ensemble non-PowerShell du même flux |
| `ddgtest.py`, `wstest.py` | 19, 7 | Aucun | **Supprimer** (scratch de debug à la racine) |
| `test_free_quota.py` | 145 | Aucun (ce n'est pas dans `tests/`, pytest ne le collecte pas) | **Déplacer** dans `scripts/` |
| `vpn_manager.py` | 118 | Prod : `opencode.py:2648, 3424` ; `config/settings.py`, `dashboard/api.py` ; tests | **Garder** (shim ADR-006 §5, Phase 9) |

---

## 9. Plan de découpage strangler

**Règles du plan** : chaque lot est un PR autonome avec tests verts entre ; la façade `opencode.<nom>` est conservée (re-export) pour ne casser aucun des 145 fichiers de tests qui référencent `oc.*` ; les modules cibles respectent la borne 1 500 l. ; l'ordre est choisi pour que **chaque lot supprime du code du monolithe** (pas seulement le déplace).

| # | Lot | Plage source | Module cible | Dépendances | Ordre | Effort | Risque |
|---|---|---|---|---|---|---|---|
| **L1** | **Corriger les P0** (aucune extraction) : `sys.modules` alias, déplacer `_resp_json_hors_boucle`, corriger le `elif _geo_tunnel` dupliqué | 31587-31607, 33459-33617 | — (patch sur place) | — | **0 — avant tout** | **S** | **Faible** (le routage géo change : valider par test) |
| **L2** | **Prologue de requête commun** : `req_id`, ContextVars, garde 413, parse JSON, détection compaction, `DEBUG` dumps, `_mark_requested_model` | 20567-20624, 26934-26980, 31083-31125 | `server/request_preamble.py` | `core.errors`, `config`, `dashboard.display` | 1 | **S** | **Faible** (pur, testable hors FastAPI) |
| **L3** | **HTTP/transport** : pools httpx/curl, `_build_http_*`, `_ensure_http_client`, `_role_client*`, `_CurlCffiResponse`, `_CurlCffiStreamResponse`, adaptateurs | 1881-1935, 1961-2294, 13004-13319 | `upstream/transport.py` (déjà `upstream/clients.py` existe : **étendre**) | `core.lazy`, `config` | 2 | **M** | **Moyen** (seams de test `oc._client`/`oc._transport` — 25 fichiers de tests) |
| **L4** | **Couche SQLite** : `_db_*`, `_save_request`, `_log_free_model_usage`, restore compteurs, migrations | 726-1226, 1298-1796 | `observability/db_host.py` (déjà `observability/db.py` porte la logique : **déplacer les wrappers**) | `observability.db` | 3 | **M** | **Moyen** (thread writer, 74 tests de DB) |
| **L5** | **Wire free** : identité officielle, `_free_wire_body`, budget/trim/sanitize, `_official_free_headers`, shims d'outils, path-map | 6892-8765, 9881-10184, 10901-11047 | `free/wire.py` + `free/wire_budget.py` + `free/headers.py` (3 modules < 1 500 l.) | `config.free_shims`, `free.impersonation`, `free.identities` | 4 | **L** | **Élevé** (invariant A.0 + XVIII : le wire est mesuré byte-exact) |
| **L6** | **Sessions et IDs** : `_mint_oc_id`, `_proxy/_free/_paid_session_id`, `_conversation_session_id`, `_free_affinity_key` | 9627-9878, 10636-10671 | `free/sessions.py` (persistance) + `core/session_file.py` (mécanique commune) | `free.identities` | 5 | **S** | **Faible** |
| **L7** | **Cooldowns et préflight** : `_free_*cooldown*`, `_trial_dead_*`, `_free_preflight_*`, `_free_stations_exhausted`, sweep | 14812-14934, 15313-15482, 15591-15904 | `free/cooldown.py` + `free/preflight.py` (déjà `app/quotas` ébauche) | `free.pool`, `shared_state` | 6 | **M** | **Moyen** (état global `_free_model_cooldowns` + lock — garder la possession côté hôte, DI) |
| **L8** | **Boucle d'essais free unifiée** (résorbe D7, P1-2) | 12772-13001, 13668-14486, 16474-17650, 29901-30523 | `free/attempts.py` + `free/hedge.py` | L3, L5, L6, L7 | 7 | **L** | **Élevé** (chemin critique ; exige un harnais de non-régression parité 429) |
| **L9** | **Streaming : helpers et finalisation** (résorbe D2, D6) | 8796-9078, 9407-9609, 16344-16471, 19075-19402, 20056-20470 | `streaming/collectors.py` + `streaming/stream_ctx.py` (déjà `streaming/sse.py` porte la pompe) | `app.protocol.*` | 8 | **M** | **Moyen** (23 sites de reprise ; exiger des tests de flux bout-en-bout avant) |
| **L10** | **Porte géo** | 5827-6600 | `geo/gate.py` + `geo/i18n.py` | `config.settings`, `shared_state` | 9 | **M** | **Moyen** (fail-closed ; préchauffer les caches, cf. P2-1) |
| **L11** | **Observabilité HTTP** : `/health`, `/metrics`, `_build_metrics_text`, routes `/api/egress/*`, `/api/free-*`, `/api/drift`, `/api/key-pauses`, `/api/circuit-breakers` | 5038-5346, 24998-26483 | `dashboard/routers/health_routes.py`, `metrics_routes.py`, `egress_routes.py`, `free_routes.py` (le dossier `dashboard/routers/` existe : 40 fichiers) | `observability.metrics` | 10 | **M** | **Faible** (routes de lecture, déjà partiellement dans le dashboard) |
| **L12** | **Handler `systemone`** (le plus autonome : pas de conversion, pas de tools) | 29901-30977 | `server/systemone.py` | L3, L7 | 11 | **M** | **Moyen** |
| **L13** | **Handler `responses`** | 31051-33052 | `server/handlers/responses.py` | L1-L10 | 12 | **L** | **Élevé** |
| **L14** | **Handler `chat_completions`** | 26933-29898 | `server/handlers/chat.py` | L1-L10 | 13 | **L** | **Élevé** |
| **L15** | **Handler `messages`** (le plus gros, le plus central) | 20564-24223 | `server/handlers/messages.py` | L1-L10 | 14 | **L** | **Très élevé** |
| **L16** | **Boot et runtime de processus** : `lifespan`, `ServerManager`, bloc `__main__` | 3300-4495, 33055-33598 | `server/lifespan.py` + `server/manager.py` + `main.py` (≤150 l.) | tout | 15 | **L** | **Moyen** (le bloc `__main__` sort du module ⇒ **P0-1 et P0-2 disparaissent structurellement**) |
| **L17** | **Nettoyage** : code mort (§4), shims racine (§8), globaux morts (P1-6) | dispersé | — | tout | 16 | **S** | **Faible** |

**Après L16, `opencode.py` doit être réduit à une façade de ré-export** (≤ 400 lignes), et la règle XIV (« aucun nouveau module > 1500 lignes ») devient respectée pour tout le dépôt sauf les 5 monolithes hors périmètre (`vpn/manager.py`, `free/pool/__init__.py`, `app/protocol/mapping.py`, `dashboard/api.py`, `config/settings.py`), à traiter dans des audits séparés.

**Ordre critique** : L1 (P0) → L2/L6/L11 (lots faciles, dérisquent) → L3/L4 (infrastructure) → L5/L7 (domaine free) → L8/L9 (cœur) → L10 → L12-L15 (handlers) → L16 (boot). Les lots L13-L15 **dépendent** de L2 et L8-L10 : les attaquer avant signifie réécrire du code qu'on va ensuite déplacer.

**Effort total estimé** : L (≈ 4-6 PR par lot volumineux, ~30 PR au total).

---

## 10. Protocole de vérification des findings

Les trois P0 ont été vérifiés autrement que par lecture :

1. **P0-1 (double exécution)** — reproduit en isolation avec un couple de modules minimal (`target.py` + `other.py`) : le fichier `target.py` affiche `module body executed, __name__=__main__` **puis** `module body executed, __name__=target` dans le même PID. Corrélé au runtime réel par `logs/debug.log` (2× `SQLite connection established`, 74 `restored` pour 37 modèles distincts, 2× `Debug mode enabled`, 2× `free_model_usage + token_counters_daily tables ready`).
2. **P0-2 (`NameError` avalé)** — `ast.parse` + `ast.unparse` : le bloc `__main__` occupe l. 33459-33598, `_resp_json_hors_boucle` est la **seule** définition après lui (index 891 sur 893 dans `tree.body`). `pystray.Icon.run` et `run_terminal_loop` sont bloquants (source inspectée). En import, `hasattr(opencode, '_resp_json_hors_boucle') is True` — ce qui explique que les tests passent et que le bug soit invisible hors `python opencode.py`.
3. **P0-3 (`UnboundLocalError` + `elif` mort)** — vérifié en deux temps. (a) La chaîne `if/elif/elif` a été extraite par AST : les 2ᵉ et 4ᵉ tests sont la **même expression** `_geo_tunnel` (aucune réassignation entre les deux — vérifié en parcourant les `Assign` du bloc). (b) Le motif exact a été reproduit en isolation : `UnboundLocalError: cannot access local variable 'model_id' where it is not associated with a value`. (c) Les deux seuls `except` du `try` (l. 31608-31611) sont typés `FreeRefusal`/`UpstreamError`, aucun `try` englobant n'existe dans `responses`, et `@app.exception_handler(Exception)` (l. 4963) rend un 500 générique. (d) Reachabilité confirmée par configuration : `config.yaml:geo.enabled = true`, politique `meta-muse-spark` en `require_vpn: true`, et `_enforce_geo_gate` pose `_geo_force_tunnel = True` (l. 6228, 6262).

---

## 11. Zones non auditées / incertitudes

| Zone | Pourquoi | Impact sur ce rapport |
|---|---|---|
| `vpn/manager.py` (11 471 l.), `free/pool/__init__.py` (8 362 l.), `app/protocol/mapping.py` (6 329 l.), `dashboard/api.py` (3 047 l.), `config/settings.py` (2 782 l.) | Hors périmètre de cet audit (fichier cible : `opencode.py`) | Le plan de découpage §9 ne traite que `opencode.py` ; ces 5 monolithes resteront > 1 500 l. après L16 et **doivent** faire l'objet d'audits dédiés (le `debt.md` les couvre partiellement via D-014-04) |
| Le contenu de `logs/requests.db` (plusieurs Go) | Interdit par la consigne (jamais de `Read`) | Les compteurs runtime (nombre de 502 sur `/v1/responses` non-stream) n'ont pas pu être mesurés ; `sqlite3` est indisponible sur ce poste |
| Le contenu de `logs/debug.log` au-delà des extraits filtrés | Fichier de 22 Mo ; lu uniquement par `grep`/`tail` ciblés | Les fenêtres citées sont exactes ; la couverture historique complète ne l'est pas |
| `static/dist/` (bundle du dashboard) | Binaire minifié | Impossible de confirmer par le front que `/api/drift`, `/api/canary/run`, `/api/egress/incidents`, `/api/egress/429`, `/api/free-egress/diag` sont réellement appelés (`grep` = 0 occurrence hors `tests/`). Ces routes pourraient être mortes côté client |
| `_research/`, `.audit-wt/`, `.audit-logs-wt/`, `Lib/` | Exclus par la consigne | Les copies de `trust.py` dans `.audit-*` ne sont pas des doublons de prod |
| Le comptage runtime des 500 sur `/v1/responses` (P0-3) et des 502 « non-JSON » (P0-2) | `logs/requests.db` interdit en lecture ; `sqlite3` indisponible sur ce poste ; `logs/debug.log` ne contient ni `NameError` ni `UPSTREAM DECODE ERROR` sur la fenêtre disponible (22 Mo, non couverte intégralement) | Les deux P0 sont prouvés **structurellement** (AST + reproduction isolée), pas par fréquence d'occurrence en production |
| Le comportement de `_apply_station_count` sous concurrence réelle | Nécessite un cluster Docker multi-station + GUI | Le lot L16 devra être validé par un soak, pas par lecture |
| La parité wire exacte (octets) après extraction | Nécessite des captures réseau | Les tests `test_free_body_conformite.py` (753 l.), `test_official_wire_e2e.py`, `test_free_wire_complet.py` (358 l.), `test_invariant_a0.py` (780 l.) sont la seule preuve disponible ; ils **doivent** être verts à chaque lot |

---

## Annexe A — Ordre de lecture recommandé pour un repreneur

1. `opencode.py:33459-33617` — le bloc `__main__` et le piège d'ordre (P0-2).
2. `opencode.py:20566-20760` — la tête du handler `messages` : 90 % des motifs du fichier y apparaissent.
3. `opencode.py:21330-21450` — l'ouverture de `anthropic_stream` : le motif `_track_model` / `_resolve_free_model` / `_open_free_stream`.
4. `opencode.py:16474-16680` — la tête de `_try_free_model_first` : la résolution free et les gardes strict_free.
5. `opencode.py:8279-8400` — la tête de `_free_wire_body` : le wire officiel.
6. `opencode.py:13846-13940` — la tête de `_open_free_stream` : la boucle multi-station.
7. `opencode.py:6053-6120` — la tête de `_enforce_geo_gate` : la porte unique.

## Annexe B — Métriques brutes du fichier

| Mesure | Valeur |
|---|---|
| Lignes totales | 33 618 |
| Lignes de code | 27 046 (80 %) |
| Lignes de commentaire | 4 279 (13 %) |
| Lignes blanches | 2 294 (7 %) |
| Définitions de niveau module | 355 (fonctions + classes) |
| Définitions imbriquées | 105 |
| Routes HTTP | 24 (dont 2 sur `/v1/messages`) |
| Middlewares | 13 |
| `except Exception` | 1 027 |
| `except …: pass` | 367 (30 % des 1 225 gestionnaires) |
| `# noqa` | 122 |
| Marqueurs `# [AAAA-MM-JJ]` | 98 |
| Imports locaux (dans les fonctions) | 164 |
| Fonctions > 250 lignes | 20 |
| Fonctions > 1 000 lignes | 5 (`messages`, `chat_completions`, `responses`, `stream_gen`, `lifespan`) |
| Plus grosse fonction | `messages` (3 657 l.) |
| `global` | 25 |
| `contextvars.ContextVar` | 15 |
| `asyncio.Lock()` au niveau module | 2 (`_apply_station_lock`, `_health_lock`) |
| `threading.Lock()` | 13 |
| `asyncio.Semaphore` au niveau module | 3 (`_DDG_SEM`, `FETCH_SEM`, +1) |
