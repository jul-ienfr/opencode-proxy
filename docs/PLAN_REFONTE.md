# Plan refonte architecture — zéro régression

> Statut : **Phase 0 terminée** (2026-09-07). Phases 1–9 à exécuter.
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

## 5. Phase 1 — Config (2-3 PR)

Extraire `config/settings.py` (1 919 l) → `config/loader.py` (yaml/env,
atomic save + portalocker), `config/geo.py` (resolve_geo, cache epoch),
`config/discovery.py` (upstream + free discovery + thread → lifespan).
Garder `config/__init__.py` comme façade (22 symboles figés par la Phase 0).
Vérifier : hot-reload `custom_routes.json` (poll 5 s), bannière divergence
`VPN_*/GEO_*`, dashboard config toujours fonctionnel.
Tests sentinelles : `test_secret_stability`, `test_env_staleness_guard`,
`test_hot_reload*`, `test_geo_*`, `test_free_discovery`.

## 6. Phase 2 — Infra pure (2 PR)

`observability/db.py` (WAL + batch, depuis `app/db`), `server/cache.py`,
`server/throttle.py`, `server/accesslog.py`, `observability/capture.py`
(depuis `traffic_capture.py`). Modules purs : faciles, gros gain de lignes
sorties de `opencode.py`.
Tests sentinelles : `test_db_*`, `test_dashboard_db_decoupling`,
`test_traffic_capture`, `test_static_cache_asgi`.

## 7. Phase 3 — Upstream HTTP (2 PR)

`upstream/clients.py` (curl pool, clients par rôle), `upstream/breaker.py` +
`upstream/quotas.py`.
Vérifier : plan 429 (faux-429, double-failover B2).
Tests sentinelles : `test_faux_429_veridique`,
`test_b2_stream_double_failover`, `test_curl_session_pool`,
`test_pool_connection_failure`, `test_http_client_self_heal`,
`test_o3_fallback_metrics`.

## 8. Phase 4 — Protocol (1-2 PR)

Remplir `app/protocol/` depuis `protocol_mapping.py` sans le casser
(convertisseurs purs). Étendre golden tests.
Tests sentinelles : `test_conversion_golden`, `test_conversion_cache`,
`test_tool_*`, `test_thinking_e2e`, `test_response_created_envelope`,
`test_lot_h_protocol`, `test_effort_mapping`.

## 9. Phase 5 — Routing + clés (1 PR)

Remplir `app/router/` (`_route_for`, `get_model_config` + lru_cache) et
`core/keys.py` (cycle, failover, pauser).
Tests sentinelles : `test_proxy.py`, `test_rotation_concurrency`,
`test_key_pauser`, `test_no_valid_keys_guard`, `test_proxy_session`,
`test_role_clients`.

## 10. Phase 6 — Free + VPN (3-4 PR, la plus risquée)

`free/pool.py` ← `free_ip_pool.py`, `free/rotation.py` ←
`shared_rotation.py` + `latency_rotation.py`, `vpn/` ← `vpn_manager.py`
découpé (stations/compose/watchdog). Garder `vpn_manager.py` comme shim de
re-export pendant 2 versions.
Ne pas paralléliser : 1 seul chantier à la fois (état global + threads).
Vérifier : tests `test_vpn_*`, `test_free_*`, `test_station_*`,
`test_geo_*`, `test_invariant_a0`, gate `not docker` + 1 passe `docker`
manuelle sur machine avec daemon.

## 11. Phase 7 — Streaming + endpoints (2 PR)

`streaming/sse.py` (keepalive, coalesce), `server/websearch.py`, puis
handlers `/v1/messages`, `/anthropic/v1/messages`, `count_tokens`.
Tests sentinelles : `test_streaming_sse`, `test_sse_*`,
`test_proxy_session`, `test_plan30_optimisation`.

## 12. Phase 8 — Dashboard/GUI/Ops (2 PR)

Découper `dashboard/api.py` en `dashboard/routes/` sans changer les URLs,
`ops/lock.py`, `ops/supervisor.py`.
Vérifier : `test_dashboard_*`, boot GUI + `--no-gui`, `systemd` + `Docker`
(build + run avec `.env` monté).

## 13. Phase 9 — Découpe finale (1 PR)

`opencode.py` → shim + `app/composition.py` (`create_app`, lifespan : DB,
VPN init, quota fetcher, discovery). Supprimer les shims temporaires un par
un, avec `grep` de non-usage.
Gate finale : ruff + mypy + pytest complet + boot local + dashboard
http://localhost:4000.

## 14. Garde-fous à chaque PR

```powershell
ruff check . ; mypy .
python -m pytest -k "not docker" -q
python opencode.py --no-gui   # + curl /v1/messages streaming + non-streaming
git diff --stat               # relire : déplacement seul, pas de logique métier
git checkout -- config.yaml   # purger l'effet de bord des tests (cf. §4)
```

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
