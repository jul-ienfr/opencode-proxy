# Plan refonte architecture — zéro régression

> Statut : **Phase 0 terminée** (2026-09-07), **Phase 2 terminée** (2026-09-07,
> exécutée avant la Phase 1 — modules purs sans dépendance config),
> **Phase 3 terminée** (2026-09-07), **Phase 4 terminée** (2026-09-07),
> **Phase 5 terminée** (2026-09-07), **Phase 6 terminée** (2026-09-07),
> **Phase 7 terminée** (2026-09-07, partielle assumée : handlers en Phase 9),
> **Phase 8 terminée** (2026-09-07, partielle assumée : closures en Phase 9),
> **Phase 1 terminée** (2026-09-07, store conservé — voir §5),
> **Phase 9 terminée** (2026-09-07, partielle assumée — voir §13).
> Refonte 0–9 close : programme follow-up §13 pour la composition finale.
> Socle : tag `pre-refonte` (= `0c7c27b`), commit Phase 0 `d8871fe`,
> harnais `tests/test_phase0_contracts.py` (25 tests verts),
> contrats `docs/adr/ADR-006-refonte-contrats-geles.md`.

## 1. Constat (mesuré 2026-09-07)

| Module | Taille | Problème |
|---|---|---|
| `opencode.py` | **14 906 lignes / 706 Ko / ~198 defs / 21 classes** | God file : routing + clés + SSE + cache + rate-limit + circuit-breakers + web-search + endpoints + lock mono-instance, tout couplé |
| `vpn_manager.py` | 6 723 lignes / 353 Ko | 2ème god file (compose, stations, watchdog, geo, rotation) |
| `dashboard/api.py` | 4 584 lignes | API dashboard mélangée au proxy |
| `protocol_mapping.py` | 2 578 lignes | OK en soi, mais importé en spaghetti depuis `opencode.py` |
| `free_ip_pool.py` | 2 123 lignes | Logique free + rotation + VPN entremêlées |
| `config/settings.py` | 1 919 lignes | Config + discovery + fetch upstream + geo + side-effects à l'import (thread background) |
| `app/` | 5 sous-packages vides (`router`, `protocol`, `streaming`, `db`, `quotas`) | Extraction amorcée (ADR-004) mais jamais remplie |
| `tests/` | 85+ fichiers | Bon filet, sanctuarisé par la Phase 0 |

Domaines enchevêtrés dans `opencode.py` : web-search/fetch, api-key routing +
pauser, DB, orjson fast-path, curl pool, clients HTTP par rôle, VPN/rotation,
geo-gate, response cache, traffic-capture, rate-limit, access-log,
circuit-breakers (dont global 429), identity rotation, fallback corrélé,
métriques, watchdog TTFB, streaming SSE, routing fast-cache, protocol mapping,
handlers, mono-instance lock.

**Objectif : passer de 4 god files à ~15 packages à responsabilité unique,
`opencode.py` réduit à un shim d'entrée ~100 lignes, sans changer un seul
comportement observable.**

## 2. Principes non-négociables (anti-régression)

1. **Strangler, jamais big-bang** : 1 module extrait = 1 PR, tests verts entre.
2. **Shims de compat** : chaque extraction garde un re-export à l'ancien chemin
   (`from config import X`, `import opencode` continuent de marcher) pendant
   toute la refonte.
3. **Contrats gelés (ADR-006)** : chemins FastAPI, format SSE, schéma SQLite
   `logs/requests.db` (34 colonnes `requests`, INSERT 32 placeholders,
   11 colonnes `free_model_usage`), clés `config.yaml` + `.env`,
   `custom_routes.json`, `api_keys.json`, ports (`OPENCODE_PORT` 4000),
   comportement GUI tray + `--no-gui`, lock `logs/opencode-{PORT}.lock`,
   `docker-compose.yml`.
4. **Pas de changement fonctionnel dans les PR de déplacement** : déplacement
   pur → `git diff --stat` + `ruff` + `mypy` + `pytest -k "not docker"` verts.
   La refactorisation vient *après*, dans un 2ème commit.
5. **Side-effects d'import interdits dans le nouveau code** : le thread
   `upstream-models-fetch`, `load_env_file()` + `load_yaml_config()` au
   top-level de `config/settings.py` migrent vers un `lifespan` explicite.

## 3. Architecture cible

```
opencode.py                  # shim ~100l : parse args, lifespan, uvicorn.run (re-export pour compat)
app/
  composition.py             # create_app() : FastAPI + lifespan + DI via app.state
core/
  routing.py                 # _route_for, routing fast-cache, SORTED_ROUTES
  keys.py                    # API_KEYS, round-robin/failover, _key_pauser, AllKeysPausedError
  errors.py                  # error helpers standardisés
config/
  settings.py                # découpé : loader.py (yaml/env) + geo.py + discovery.py + store.py
protocol/
  anthropic_openai.py        # depuis protocol_mapping.py (convertisseurs purs, testés golden)
  responses.py               # /v1/responses (muse/spark)
  tokens.py                  # count_tokens, tiktoken
upstream/
  clients.py                 # clients HTTP par rôle + curl pool + web_fetch partagé
  breaker.py                 # circuit-breakers per-endpoint + global 429
  quotas.py                  # fetch_quotas, cache 429, watchdog TTFB
free/
  pool.py                    # depuis free_ip_pool.py
  rotation.py                # shared_rotation.py + latency_rotation.py fusionnés proprement
  identity.py                # identity rotation (curl_cffi profils)
vpn/
  manager.py                 # depuis vpn_manager.py, découpé : stations.py, compose.py, watchdog.py, geo.py
streaming/
  sse.py                     # SSE handlers, keepalive, coalesce
server/
  cache.py                   # response cache non-streaming
  throttle.py                # rate-limit token bucket
  accesslog.py               # middleware access log
  websearch.py               # DDG cache/sem + fetch handler v3.3
observability/
  db.py                      # SQLite WAL, batch Queue (depuis app/db)
  metrics.py                 # compteurs diagnostic perf
  capture.py                 # traffic_capture.py
dashboard/                   # inchangé d'interface : api.py découpé en routes/, display.py, quota.py, events.py
gui/                         # tray.py, window.py inchangés d'interface
ops/
  lock.py                    # mono-instance lock
  supervisor.py              # station_supervisor.py
```

## 4. Phase 0 — Gel + harnais ✅ TERMINÉE (2026-09-07)

- [x] T0.1 : `git status` propre, tag `pre-refonte` posé.
- [x] T0.2 : `tests/test_phase0_contracts.py` — 25 tests verts (routes
      proxy + dashboard, façades `config`/`dashboard`/`opencode`, schéma DB,
      `CONFIG_KEYS`, lock, 13 modules satellites).
- [x] T0.3 : `docs/adr/ADR-006-refonte-contrats-geles.md` rédigé.
- [x] Gate : `ruff check .` → All checks passed ; suite complète
      `pytest -k "not docker"` exécutée à 100 % (1 skip, 0 échec métier).
- Commit : `d8871fe docs(refonte): ADR-006 contrats gelés + harnais phase0`.

⚠️ Leçons apprises (à réutiliser phases 1–9) :
- `config.yaml` est modifié en effet de bord par certains runs de tests
  (`ovpn_protocol` tcp→udp) — **toujours `git checkout -- config.yaml`
  après chaque gate**, et vérifier `git status` avant de commiter.
- Les tests à fixture `tmp_path` exigent un vrai terminal (sandbox DSH
  bloquant) — lancer les gates dans PowerShell normal.

## 5. Phase 1 — Config (2-3 PR) ✅ TERMINÉE (2026-09-07, 1 PR + arbitrage store)

- `config/loader.py` créé : résolveurs sans état (`_env*`,
  `normalize_429_action`, `resolved_station_count`,
  `_normalize_free_parallel`, `_ensure_auto_max_free_attempts_warn`).
- `config/geo.py` créé : moteur géo + cache epoch (propriétaire),
  snapshots injectés par wrappers `settings` (mêmes signatures).
- `config/discovery.py` créé : fetch upstream (+ `start_background_fetch`
  explicite, lifespan-ready Phase 9, appelé au même point),
  free-discovery (urls/detect/fetch/endpoint/apply/persist, état en
  paramètres) ; `_ensure_free_models_sync/async` + spawns restent hôtes.
- ARBITRAGE (prouvé par contrats tests) : le STORE (`_yaml_data`,
  `CONFIG_PATH`, snapshots, `_reload_lock`) RESTE dans `settings.py` —
  tests/runtime rebindent `settings._yaml_data`/`CONFIG_PATH` directement
  (`test_free_discovery.py`, `test_hot_reload_phase4.py`, `opencode.py`,
  `dashboard/api.py`). `config/__init__.py` INCHANGÉE (48 symboles OK).
- `settings.py` : 1 919 → 1 367 lignes (−552). `_CREATE_NO_WINDOW`,
  `random`/`subprocess`/`re` top-level devenus inutiles → supprimés ;
  `_FREE_DISCOVERY_URLS_CACHE` mort (+ `global` mort) supprimé.
- 1 correction en cours de route : `fetch` oubliait `api_base_anthropic`
  (endpoint `None` — attrapé avant gate), +2 corps devinés réécrits à
  l'identique après lecture (`is True` strict, suffixe sensible à la casse).
- Gate : `ruff check .` vert, `mypy` vert, `pytest -k "not docker"` vert à
  100 % (0 échec), sentinelles phase 1 + phase 0 vertes.

## 6. Phase 2 — Infra pure (2 PR) ✅ TERMINÉE (2026-09-07, avant Phase 1)

Modules purs extraits de `opencode.py` (déplacement pur, zéro comportement),
`observability/` + `server/` créés :

- PR-A `server/` : `server/cache.py` (`ResponseCache` ← `_ResponseCache`,
  `debug_fn`/`dumps_str_fn` injectés), `server/throttle.py` (`Bucket` ←
  `_Bucket`, `RateLimitMiddleware` rate/burst/stale_ttl + `debug_fn`/`dumps_fn`
  injectés, `RequestBodyLimitMiddleware` inchangé), `server/accesslog.py`
  (`AccessLogMiddleware`, `log_fn` injecté). `opencode.py` garde les alias
  `_ResponseCache`/`_Bucket`/`_response_cache` + lectures `RATE_LIMIT_*`/`yaml`.
- PR-B `observability/` : `observability/capture.py` (canonique, copie octet
  de `traffic_capture.py`, singleton `capture` partagé), `observability/db.py`
  (canonique, copie de `app/db`) ; `traffic_capture.py` et `app/db` deviennent
  des shims de re-export (surface vérifiée identique). `opencode.py` +
  `dashboard/api.py` importent les chemins canoniques.
- NON fait (décision) : `observability/metrics.py` repoussé en Phase 3 —
  `_build_metrics_text` dépend des compteurs failover upstream (pas pur).
- Ordre des 9 middlewares vérifié identique à `pre-refonte` (au renommage
  `_RequestBodyLimitMiddleware` → `RequestBodyLimitMiddleware` près).
- Gate : `ruff check .` vert, `mypy` vert (scope), `pytest -k "not docker"`
  vert à 100 %, sentinelles `test_db_*`, `test_dashboard_db_decoupling`,
  `test_traffic_capture`, `test_static_cache_asgi`, `test_proxy` vertes.
- Boot local non concluant en sandbox (lifespan `ensure_docker_running`
  65 s + egress bloquée — pré-existant, indépendant de la refonte) ;
  ordre middlewares + suite complète tiennent lieu de gate boot.
- `opencode.py` : 14 906 → 14 666 lignes (−240 net : −1550/+114 tracked).

Cible initiale (pour mémoire) : `observability/db.py` (WAL + batch, depuis
`app/db`), `server/cache.py`, `server/throttle.py`, `server/accesslog.py`,
`observability/capture.py` (depuis `traffic_capture.py`). Modules purs :
faciles, gros gain de lignes sorties de `opencode.py`.
Tests sentinelles : `test_db_*`, `test_dashboard_db_decoupling`,
`test_traffic_capture`, `test_static_cache_asgi`.

## 7. Phase 3 — Upstream HTTP (2 PR) ✅ TERMINÉE (2026-09-07)

`upstream/` créé (pur, DI, état mutable possédé par l'hôte et passé À
L'APPEL — les tests rebindent/mutent les globaux `oc.*`) :

- PR-C `upstream/breaker.py` : `CircuitBreaker` (machine à états, seuils +
  `debug_fn` injectés, hook `_probe_enabled()`), `CircuitOpenError`,
  `Global429State` + `Global429BackoffMiddleware` (`remaining_fn` injecté).
  `opencode.py` garde `_CircuitBreaker` (SOUS-CLASSE seam : seuils yaml +
  flag `_cb_half_open_probe_enabled()` résolu à l'appel — construction
  directe + monkeypatch par `test_proxy.py`/`test_perf_lot3_regressions.py`),
  le registre `_circuit_breakers` + wrappers `_get_cb`/`_cb_*` (rebind par
  `test_streaming_sse.py`), l'instance `_g429` + wrappers.
- PR-C `upstream/clients.py` : `CurlSessionSlot/Pool` (tel quel),
  `evict_later`/`evict_idle_pools`/`close_all_pools`/`swap_pools_for_proxy`
  (dict hôte en paramètre), `RoleClientStore` (`clients` aliasé par
  `_role_clients`, MÊME objet ; `bound_url`/`grace_s` à l'appel),
  `aclose_role_client_after`, `build_fresh_client`. Wrappers d'une ligne
  (`_get_pooled_curl_session` inchangé — factory curl_cffi + métrique).
- PR-D `upstream/quotas.py` : `fetch_quotas_cached`, `bump_ttfb_failover`,
  `alias_for_api_key`, `TTFBWatchdogTimeout`, `clamp_ttfb_timeout`.
  Lectures yaml (`_ttfb_watchdog_*`) + orchestre TTFB (stages/504,
  `test_perf_lot3_regressions.py`) restent hôtes.
- PR-D `observability/metrics.py` : `fallback_cause`, bumps/reset (état en
  paramètre, fail-soft `None` explicite), rings (`observe/latency_snapshot/
  percentile`), `MetricsSnapshot` + `render_*` + `build_metrics_text`
  (byte-identique ; extraction `shared_state`/`protocol_mapping` en garde-fous
  hôtes). `_build_metrics_text` devient assembleur + rendus.
- NON déplacés (décision) : `_ensure_http_client` + `_client`/`_transport`
  (seam d'identité `oc._client`, `test_http_client_self_heal.py` — Phase 7),
  `_execute_web_fetch` (Phase 7), `_role_tunnel_url` (Phase 6),
  `_do_request_with_retry`/`_forward_post`/`UpstreamError` (chemins requête).
- Gate : `ruff check .` vert, `mypy` vert (`upstream/`+`observability/`),
  `pytest -k "not docker"` vert à 100 %, sentinelles phase 3 vertes
  (faux-429, b2-double-failover, curl-pool, pool-failure, self-heal, o3,
  proxy, role-clients, lot3, lot7) + phase 0. Ordre des 9 middlewares
  re-vérifié identique. Boot sandbox : même réserve qu'en Phase 2 (lifespan
  Docker/egress — pré-existant).
- `opencode.py` : 14 666 → 14 235 lignes (−431 ; cumul refonte −671).

Cible initiale (pour mémoire) : `upstream/clients.py` (curl pool, clients par
rôle), `upstream/breaker.py` + `upstream/quotas.py`.
Vérifier : plan 429 (faux-429, double-failover B2).
Tests sentinelles : `test_faux_429_veridique`,
`test_b2_stream_double_failover`, `test_curl_session_pool`,
`test_pool_connection_failure`, `test_http_client_self_heal`,
`test_o3_fallback_metrics`.

## 8. Phase 4 — Protocol (1-2 PR) ✅ TERMINÉE (2026-09-07, 1 PR)

`app/protocol/mapping.py` = domicile canonique (copie octet de
`protocol_mapping.py`, seul l'en-tête change) ; `protocol_mapping.py`
devient shim de re-export exhaustif (vérifié par diff `dir()` : seule
`tiktoken` — import conditionnel jamais consommé via ce chemin — manque).
`app/protocol/__init__.py` (9 noms) et le bloc re-export `opencode.py`
(24 noms) repointés vers `.mapping` (mêmes objets).

Points durs :
- état mutable partagé (`_anthropic_cache`, `_redacted_thinking_cache`,
  `_responses_tool_cache`… — mutés via `pm.*` par les tests) : MÊMES objets
  via le shim, `conversion_cache_stats()` lit les globaux canoniques ;
- `anthropic_to_openai` défini 2× (lignes 521/915, le 2nd gagne) : préservé
  tel quel par la copie (pas de refactor dans un déplacement pur) ;
- couplages `config`/`dashboard.display`/`orjson`/`tiktoken` du module
  CONSERVÉS (le fichier est « OK en soi » — la DI viendra après la Phase 9
  si besoin) ; la découpe fine cible (`anthropic_openai.py`/`responses.py`/
  `tokens.py`) est repoussée au même titre.
- Gate : `ruff check .` vert, `mypy` vert, `pytest -k "not docker"` vert à
  100 %, sentinelles phase 4 vertes (golden, cache, tool_×4, thinking_e2e,
  response-created, lot_h, effort) + phase 0. Golden tests NON étendus
  (déplacement pur — à faire en PR follow-up dédiée si souhaité).

Cible initiale (pour mémoire) : remplir `app/protocol/` depuis
`protocol_mapping.py` sans le casser (convertisseurs purs). Étendre golden
tests.
Tests sentinelles : `test_conversion_golden`, `test_conversion_cache`,
`test_tool_*`, `test_thinking_e2e`, `test_response_created_envelope`,
`test_lot_h_protocol`, `test_effort_mapping`.

## 9. Phase 5 — Routing + clés (1 PR) ✅ TERMINÉE (2026-09-07)

- Volet routing DÉJÀ FAIT ([P5 tranche 3] antérieure) : `app/router.route_for`
  pur + wrapper `opencode._route_for` — vérifié, inchangé.
- `core/keys.py` créé (pur, DI) : `KeyPauser` (230 lignes, mémo préfixes +
  alias/debug/log injectés), `AllKeysPausedError`, sélecteurs purs
  (`select_next_key` failover-sticky/round-robin + `enabled_keys`,
  `env_key_or_raise`, `find_alternative_key`, `has_usable_paid_key`,
  `build_alias_cache` — état en paramètres, mis à jour retourné, même
  pattern que `app/router.route_for`).
- `opencode.py` garde TOUS les globaux (`API_KEYS`, `_key_pauser`,
  `_key_failover_index`, `_key_cycle_*`, `_key_alias_cache`…) + wrappers
  d'une ligne ; `_KeyPauser` = SOUS-CLASSE seam (max_pause yaml, lambdas
  résolues à l'appel — `_debug`/`_log` définis après la classe !) ;
  `import yaml` top-level devenu inutile → supprimé (yaml vit dans
  `core/keys.py`).
- `get_model_config` + lru_cache : reste côté `config` (territoire Phase 1,
  pas touché).
- Gate : `ruff check .` vert, `mypy` vert, `pytest -k "not docker"` vert à
  100 %, sentinelles phase 5 vertes (proxy, rotation_concurrency,
  key_pauser, no_valid_keys_guard, proxy_session, role_clients, lot3,
  hot_reload) + phase 0.
- `opencode.py` : 14 235 → 13 944 lignes (−291 ; cumul refonte −962).

Cible initiale (pour mémoire) : remplir `app/router/` (`_route_for`,
`get_model_config` + lru_cache) et `core/keys.py` (cycle, failover, pauser).
Tests sentinelles : `test_proxy.py`, `test_rotation_concurrency`,
`test_key_pauser`, `test_no_valid_keys_guard`, `test_proxy_session`,
`test_role_clients`.

## 10. Phase 6 — Free + VPN (3-4 PR, la plus risquée) ✅ TERMINÉE (2026-09-07, 3 chantiers séquentiels)

- ✅ Chantier 1 (2026-09-07) : `free/pool.py` ← `free_ip_pool.py` (copie
  octet, seul l'en-tête change ; zéro `global` dans le module — état 100 %
  instance, shim sans divergence possible). `free_ip_pool.py` = shim
  (4 noms). `opencode.py` (lifespan) + `vpn/__init__.py` repointés vers
  `free.pool`. Gate : ruff + mypy + `pytest -k "not docker"` verts à 100 %.
- ✅ Chantier 2 (2026-09-07) : `free/rotation.py` ← `shared_rotation.py`
  (registre IP + curseur, 433 l) + `latency_rotation.py` (moteur adaptatif,
  369 l) fusionnés byte-identique. Seuls ajustements : en-tête, `__future__`
  remonté, imports dédupliqués, loggers nommés explicitement (noms
  historiques préservés), `ROOT` ré-ancré racine repo (le fichier vit dans
  `free/` — SANS ce fix, `logs/shared_rotation.json` perdu en prod).
  Équivalence prouvée (États + décisions moteur ancien vs nouveau,
  identiques). Les 2 anciens fichiers = shims (mêmes objets, singleton
  `_ENGINE` partagé via `get_engine()`). Internes repointés
  (`free/pool.py`, `opencode.py`, `dashboard/api.py`,
  `station_supervisor.py`). Gate : ruff + mypy + suite complète verts.
- ✅ Chantier 3 (2026-09-07) : `vpn/manager.py` ← `vpn_manager.py` (6 723 l,
  copie + 3 ajustements à comportement identique : `ROOT` ré-ancré racine
  repo — sinon `logs/vpn_state*.json` + `vpn_configs/` perdus —, loggers
  figés à `"vpn_manager"` — `attach_module_logger` G7 anti-silence —).
  `vpn_manager.py` = shim (39 noms ; 4 scalaires rebindés via `global`
  exclus, AUCUN lecteur en repo — vérifié). `vpn/__init__.py` → façade
  PARESSEUSE (PEP 562 — les imports eager créaient un cycle fatal
  `config → vpn_manager → vpn → free → vpn_manager` partiel).
  3 VRAIES régressions trouvées par les tests puis corrigées (leçon §14) :
  shim sans `os`/`asyncio` (patchés via `vm.*`), imports par appel
  repointés canonique (patch `VPNManager` stub invisible), `_docker_cli`
  canonique invisible au patch (résolution tardive via le shim).
  Gate : ruff + mypy + `pytest -k "not docker"` verts à 100 % (parité
  baseline prouvée par double run complet ; 1 flaky canari pré-existant
  identique des deux côtés).
- `opencode.py` : ~inchangé (le code VPN ne vivait pas dedans) ; cumul
  refonte −962 (13 944 l).

Cible initiale (pour mémoire) : `free/pool.py` ← `free_ip_pool.py`,
`free/rotation.py` ← `shared_rotation.py` + `latency_rotation.py`, `vpn/` ←
`vpn_manager.py` découpé (stations/compose/watchdog). Garder
`vpn_manager.py` comme shim de re-export pendant 2 versions.
Ne pas paralléliser : 1 seul chantier à la fois (état global + threads).
Vérifier : tests `test_vpn_*`, `test_free_*`, `test_station_*`,
`test_geo_*`, `test_invariant_a0`, gate `not docker` + 1 passe `docker`
manuelle sur machine avec daemon.

## 11. Phase 7 — Streaming + endpoints (2 PR) ✅ TERMINÉE (2026-09-07, scope réduit : handlers → Phase 9)

- `streaming/sse.py` créé (cible archi) : `sse_pump` pure (100 l, asyncio
  seul). Wrappers `_sse_keepalive`/`_sse_coalesce` + alias `_sse_pump`
  conservés hôtes (défauts yaml + seams tests). `app/streaming/__init__`
  re-pointe vers `streaming.sse` (fini le re-export à l'envers depuis
  `opencode`).
- `server/websearch.py` créé : `normalize_query`/`format_ddg` (purs),
  `is_safe_fetch_url` (SSRF, `blocked_nets` injecté), `execute_ddg_search`
  (cache/locks/sem + fns injectés), `execute_web_fetch` (`role_client_fn`/
  `safe_fn`/`sem` injectés — patch-visibility préservée),
  `strip_web_tool` (`normalize_fn`/`debug_fn` injectés). Hôte garde état
  (`_DDG_*`, `FETCH_SEM`, `_BLOCKED_NETS`) + wrappers d'une ligne ;
  `import copy`/`socket` top-level supprimés (déménagés).
- NON déplacés (décision) : handlers `/v1/messages`,
  `/anthropic/v1/messages`, `count_tokens` + orchestrateurs
  `_handle_web_search/_handle_web_fetch` (résolvent les callees via globaux
  hôtes patchés par `test_proxy.py` — extraction = Phase 9 composition).
- Gate : `ruff check .` vert, `mypy` vert (+1 `assert` d'invariant, chemin
  sinon inatteignable), `pytest -k "not docker"` vert à 100 % (0 échec),
  sentinelles phase 7 vertes + phase 0.
- `opencode.py` : 13 944 → 13 666 lignes (−278 ; cumul refonte −1 240).

Cible initiale (pour mémoire) : `streaming/sse.py` (keepalive, coalesce),
`server/websearch.py`, puis handlers `/v1/messages`,
`/anthropic/v1/messages`, `count_tokens`.
Tests sentinelles : `test_streaming_sse`, `test_sse_*`,
`test_proxy_session`, `test_plan30_optimisation`.

## 12. Phase 8 — Dashboard/GUI/Ops (2 PR) ✅ TERMINÉE (2026-09-07, partielle : closures → Phase 9)

- `ops/lock.py` créé : verrou mono-instance pur (`log_fn`/`debug_fn`
  injectés au site `__main__`). Alias NU (pas de wrapper — `getsource`
  doit voir le message FATAL, contrat phase 0, vérifié). Smoke live OK.
- `ops/supervisor.py` ← `station_supervisor.py` (copie octet, zéro
  `global`) ; `station_supervisor.py` = shim (6 noms, `WARMUP_*` inclus).
- `dashboard/routes/` amorcé : `routes/static.py` ← pré-compression gzip +
  `StaticCacheMiddleware` (stdlib/starlette seuls) ; `dashboard/api.py`
  ré-importe (mêmes objets, montage inchangé).
- NON découpé (décision) : les ~40 routes sont des closures capturant
  l'état de `register_dashboard` — leur extraction exige la DI `app.state`
  = Phase 9. GUI (`gui/`) inchangée d'interface. Vérif `systemd`/`Docker`
  impossible en sandbox (egress/daemon absents — inchangé, aucun fichier
  déploiement touché).
- Gate : `ruff check .` vert, `mypy` vert, `pytest -k "not docker"` vert à
  100 % (0 échec), sentinelles (dashboard_×2, static_cache, plan30,
  station_isolation, proxy_session) + phase 0 vertes.
- `opencode.py` : 13 666 → 13 622 lignes (−44) ; `dashboard/api.py` :
  4 586 → 4 513 (−73). Cumul refonte −1 357.

Cible initiale (pour mémoire) : découper `dashboard/api.py` en
`dashboard/routes/` sans changer les URLs, `ops/lock.py`,
`ops/supervisor.py`.
Vérifier : `test_dashboard_*`, boot GUI + `--no-gui`, `systemd` + `Docker`
(build + run avec `.env` monté).

## 13. Phase 9 — Découpe finale ✅ PARTIELLE (2026-09-07 — extractions sûres faites, composition documentée en follow-up)

Fait :
- `core/errors.py` créé : `UpstreamError`, `FreeRefusal`,
  `FreeQuotaExhausted`, `anthropic_error`/`openai_error` (purs),
  `free_refusal_response` (`redact_fn` injecté ; wrapper hôte inchangé).
- `protocol/tokens.py` créé : `estimate_tokens` (pur),
  `estimate_input_tokens` (encoding/extract/log injectés),
  `elapsed_ms` (pur). Mêmes objets ré-exportés (`test_proxy.py`,
  `test_no_valid_keys_guard.py` OK).
- Gate : `ruff check .` vert, `mypy` vert, `pytest -k "not docker"` vert à
  100 % (0 échec), ordre des 9 middlewares re-vérifié identique.
- `opencode.py` : 13 622 → 13 458 lignes (−164 ; cumul refonte −1 448).

NON FAIT (décision, avec preuve — pas un report paresseux) :
1. `opencode.py` → shim ~100 lignes + `app/composition.py` (`create_app`) :
   IMPOSSIBLE sans réécrire le chemin requête. Les routes sont des
   `@app.*` décorateurs entrelacés sur 13 k lignes avec des centaines de
   références aux globaux du module ; extraire `create_app` exigerait de
   déplacer lifespan + handlers + état d'un bloc (big-bang interdit par
   §2.1), ou un wrapper-théâtre sans valeur architecturale (refusé).
2. Suppression des shims : BLOQUÉE — prouvé par `scripts/audit_shim_surface.py`
   + scan des imports : CHAQUE shim est épinglé par les tests (lecture,
   mutation en place, voire rebind `vpn_manager.VPNManager/_docker_cli`) et
   les imports par appel du runtime DOIVENT rester en chemins historiques
   (règle §14 — visibilité des patchs). Supprimer un shim aujourd'hui =
   casser des tests verts, sans aucun gain runtime (mêmes objets).
3. Boot local + dashboard http://localhost:4000 : inchangé et non
   régressé (aucun comportement touché), mais non re-vérifiable en sandbox
   (lifespan Docker/egress — réserve permanente depuis la Phase 2).

Programme follow-up (hors refonte, prérequis test-migration) :
  a. Migrer les tests des chemins historiques vers les canoniques
     (`import vpn.manager`, `import free.pool`…), en gardant 1–2 tests par
     shim (filet de compat explicite, pas toute la suite).
  b. Re-pointer alors les imports par appel runtime vers les canoniques
     (mécanique, audit en preuve).
  c. Extraire les groupes de routes dashboard vers `dashboard/routes/`
     avec DI `app.state` (les closures actuelles en sont incapables).
  d. Extraire les handlers `/v1/*` + lifespan vers `app/composition.py`
     (`create_app`) — SEULEMENT quand (a–c) tiennent.
  e. Supprimer les shims un par un (chaque suppression = 1 PR, gate verte).

## 14. Garde-fous à chaque PR

```powershell
ruff check . ; mypy .
python -m pytest -k "not docker" -q
python scripts/audit_shim_surface.py  # surface lue par les tests ⊆ surface des shims
python opencode.py --no-gui   # + curl /v1/messages streaming + non-streaming
git diff --stat               # relire : déplacement seul, pas de logique métier
git checkout -- config.yaml   # purger l'effet de bord des tests (cf. §4)
```

⚠️ Leçons Phase 6 (shims + tests qui patchent — À RELIRE avant chaque PR
à shim, phases 7–9) :
- Les tests patchent via le namespace HISTORIQUE (`monkeypatch.setattr(vm,
  "VPNManager"|"_docker_cli")`, `vm.os`/`vm.asyncio`) : le shim doit exposer
  les MÊMES objets, Y COMPRIS les modules importés (`os`, `asyncio`…).
- Règle imports : **par appel → chemins historiques (shims)**, top-level
  (1×) → chemins canoniques. Un import par appel repointé canonique rend
  les patchs de tests invisibles (cas réel : stub VPNManager ignoré).
- Rebind via `global` + lecture externe du scalaire = DIVERGENCE shim
  garantie : soit le nom n'a aucun lecteur (l'exclure, documenté), soit le
  canonique doit résoudre via le shim à l'appel (cas réel : `_docker_cli`).
- Façade package eager + shim = cycle fatal (`config → vpn_manager → vpn →
  free → vpn_manager` partiel) : façade PARESSEUSE (PEP 562).
- En cas de doute sur une régression : double run complet (arbre + baseline
  stashée) et comparaison des sets FAILED — les tests timing/docker sont
  flaky dans la sandbox des deux côtés (canari WG).

## 15. Risques principaux

- `config/settings.py` : side-effects à l'import → mocker dans tests,
  migrer vers lifespan tôt (phase 1).
- `vpn_manager.py` : état global + threads → ne pas paralléliser son
  découpage, 1 seul chantier à la fois, tests `docker` exclus du gate par
  défaut (`-k "not docker"`).
- Imports circulaires (`config` ↔ `vpn_manager` ↔ `dashboard`) → sens unique
  imposé : `config` ne doit importer ni `vpn_manager` ni `dashboard`
  (injection via `app.state` ; `save_yaml_config` importe `dashboard.api`
  en lazy — à inverser proprement).
- Windows (`msvcrt`, `CREATE_NO_WINDOW`) vs Linux systemd → garder les
  branches OS dans `ops/`, tester les deux chemins.
