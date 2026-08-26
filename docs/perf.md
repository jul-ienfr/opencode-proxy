# Perf — mesures cache/pool/mmap (Phase3 P3.4)

> Bench: `python scripts/bench_pool.py` et `python scripts/bench.py --url http://localhost:4000`

## Cache prompt

- `cache.max_size 1000` TTL 300, `min_prompt_size 2000` — hit rate mesuré via `scripts/bench.py`
- Bench: 2000 tokens < min → no cache, 8000 chars → split à ~4000, hit +25% sur prefix stable
- `cache_size 64000` (~256M) vs `mmap 268M` — mmap pour scans séquentiels, cache pour lookups aléatoires

## Pool httpx

- `max_connections 64` / `max_keepalive 32` / `keepalive_expiry 30` — bench_pool.py mesure rps sous 200/400/800 concurrents ([P2.5 perf] 500/200→64/32)
- Backpressure: >64 → queue 5s → 503, évite OOM

## DB WAL

- `mmap 268M` vs `cache_size 64000` — bench via `scripts/rotate_db.sh --vacuum` + `SELECT COUNT(*)`
- WAL checkpoint 3600s — `PRAGMA wal_checkpoint(TRUNCATE)` évite WAL 4M → 100M

## Résultats (2026-08-23, 4c/8G)

- `CACHE_MIN_PROMPT_SIZE 2000` + `max_size 1000` → 300 entries, hit 42% sur 500 req
- Pool 500/200/30 → 1200 rps sans 503 (vs 500/100/15 → 800 rps, 5% 503)
- mmap 268M → scan `requests` 3.3G en 1.2s (vs 2.1s sans mmap)

Voir `docs/tuning.md` pour les tradeoffs complets.

---

# Cycle audit-perf-qualité (2026-08-26)

## Baseline

| Mesure | Valeur |
|---|---|
| `bench_perf.py --baseline` | figé dans `logs/bench_baseline.json` (lock 0.001 ms, atomic write 2.51 ms, sqlite idx 0.006 ms) |
| Live `/health` 300 req, conc 20 (serveur GUI en cours) | **241 rps**, p50 **17.7 ms**, p95 **835 ms** |

Le re-bench live nécessite un redémarrage du serveur (le process `pythonw opencode.py --gui`
charge le code à l'import) : relancer `python scripts/bench.py --url http://localhost:4000
--concurrency 20 --requests 300` après restart et comparer. Le micro-bench post-changes est
**dans le bruit** (exit 0, aucune régression >20 % — règle §13.5).

## Phase 1 — Hot path proxy

1. **Debug gating + `_truncate` borné** : `_redact(_truncate(oai_body))` (dumps orjson complet +
   4 regex sur 2000+ chars) n'exécute plus que sous `DEBUG` (opencode.py sites messages/responses) ;
   `_truncate` ne sérialise plus en `indent=2` complet (orjson compact puis slice). Doublon de
   littéral supprimé ; `ensure_min_tokens` gated.
2. **Pré-sérialisation orjson des requêtes** : les 6 sites `json=body` passent par
   `_serialize_json_body()` (bytes réutilisés entre retries dans `_do_request_with_retry`) ;
   Content-Type assuré via `_with_json_content_type()`.
3. **Parse réponses via orjson** : `_resp_json_or_empty()` remplace `resp.json()` (stdlib json)
   sur les 7 sites chauds non-stream.
4. **Dédup guard orphelins** : bloc dupliqué back-to-back supprimé (un seul passage).
5. **`_KeyPauser._prefix` mémoïsé** (`_key_prefix_cache`, invalidé dans `_rebuild_key_cache`) :
   SHA-256 ×N appels/requête → dict hit.
6. **Pool curl** : checkout avec `wait_for(5 s)` + session overflow hors quota M (auto-réduite au
   checkin) au lieu d'attente infinie ; imports curl_cffi au niveau module (classe résolue par
   attribut à l'appel pour préserver le seam de test).
7. **Traffic capture LAZY** : boot en passthrough pur (`enabled=False`) ; activée uniquement
   pendant qu'un onglet Traffic regarde (TTL viewer 15 s, poll UI 3 s). Zéro viewer = zéro coût
   middleware. Toggle utilisateur mémorisé (`/api/traffic/status` expose `user_enabled`).
8. **Ordre middlewares** : TrafficCapture déplacé en PREMIER ajouté → plus interne ; 401/413/429
   court-circuitent avant capture.
9. **`resolve_geo` mémoïsé** : clé = (epoch, geo, server_countries triés, id(GEO_POLICIES)) ;
   epoch bumpé au hot-reload/save_env ; copies défensives des sets retournés.

## Phase 2 — Dashboard / DB

* **Connexion READ-ONLY dédiée** (WAL : lecteurs concurrents du writer) pour toutes les lectures
  dashboard (`_db_read_sync`) — une agrégation lourde ne tient plus le lock writer. Fallback sûr :
  connexion partagée SOUS lock (tests :memory:, DB absente). Test non-régression :
  `tests/test_dashboard_db_decoupling.py::test_inserts_not_blocked_by_slow_dashboard_read`.
* **Timeseries** : TTL cache 2 s + granularité forcée `day` au-delà de 7 jours.
* **History COUNT** caché (TTL 2 s par where).
* **Filters** cachés longue durée + tools fournis par le writer (`tools_provider`,
  registre `_tools_used_seen`) — plus de scan JSON full-table par poll ; invalidés par DELETE.
* **Index** `idx_original_model`.
* **free-model-usage** : agrégation SQL GROUP BY (fini 5000 lignes → Python par poll).
* **debug/logs** : seuil full-read aligné rotation 10 Mo + no-op si (size, mtime, page) inchangés.
* **Frontend** : `refreshAll` throttlé ≥ 1 s indépendamment du garde SSE ; timeseries fetché
  seulement onglet stats visible.

## Phase 3 — Cadence VPN / IP-rotation

* **Watchdog sain** 5 s → **20 s** (config.yaml) ET refresh docker lourd skippé sans events
  conteneur depuis le tick (compteur `_docker_events_since_tick`, watcher déjà temps réel) :
  ≈ 16 subprocess/5 s → ~quart du CPU hôte ×4 stations.
* **`_wait_healthy`** backoff exponentiel 0.5→2 s (×1.5).
* **Pin contrôle** : UN SEUL `docker logs` par itération partagé auth+TLS (`_scan_auth_tls`),
  seam `_auth_check_supports_text` préservé pour les stubs.
* **`get_public_ip` sticky-first** : endpoint sticky SEUL en nominal (1 GET) ; sweep parallèle
  borné uniquement en fallback ; client httpx RÉUTILISÉ par socks5_url (plus de handshake par probe).
* **Updater stagger** : premier check décalé de station×90 s (fini N× `docker compose pull`
  simultanés).

## Phase 4 — Caches conversion & correctesse

* **Cache conversion LRU+epoch** : OrderedDict move-to-end/drop-oldest (fini gel à 512),
  clé blake2b mélangée à `ROUTE_VERSION` (staleness post-hot-reload corrigée), copie racine
  seule (contrat shallow documenté).
* **Bypass cache si contenu muté** : résultats DDG/web_fetch injectés après capture des bytes
  bruts → ContextVar `_web_nondet_injected` → re-clé sur le contenu courant (fini partage des
  résultats du 1er client).
* **État SSE Responses-API par stream** (`ResponsesSseState` passé aux 5 sites) : fini le
  partage global tool_cache/reasoning_seen entre streams concurrents et la fuite d'un stream
  avorté. Duplication pré-existante des globals (l.148 vs l.1592) supprimée.
* **`get_model_config.cache_clear()`** après découverte upstream (modèles visibles immédiatement).
* **maybe_reload hors loop** : regen .env (subprocess ≤10 s) en thread daemon — ne gèle plus
  toutes les streams.
* **Race boot `_reload_lock`** : défini avant spawn threads discovery.
* **Race .env** : `_apply_ovpn_protocol` sous `_ENV_RW_LOCK` (flag de possession, release en finally).

## Phase 6 — Hygiène & qualité

* Supprimés : `middleware.py`, `nordvpn_api.py`, `server_scorer.py` (0 import prod),
  `connect_wait` (vpn_manager), calcul mort `age_sec` ; `note_free_stream_start/end` WIRÉS
  (garde auto-update désormais effectif sur les streams libres tunnel).
* **WEB_PORT supprimé partout** (rien n'écoutait sur :8082) — GUI/dashboard/port unique ;
  i18n nettoyée ; test hot-reload réécrit sur `port`.
* requirements.txt : redis retiré, `portalocker==3.2.0` ajouté (import réel settings.py),
  trafilatura épinglé ==2.0.
* pyproject : ignore dédoublonné, **E722/B904 retirés des ignores et corrigés** (bare except
  restant → except Exception ; 5 raise...from) ; first-party mis à jour.
* CI : mypy bloquant sur modules sains (traffic_capture, docker_events, trust, shared_rotation,
  shared_state, config/__init__) ; progressif VISIBLE (continue-on-error) sur le reste — fini
  `|| true` ; pip-audit bloquant ; gitleaks volontairement non bloquant tant que SEC-1 (secret
  historique git) est ouvert.
* Tests : docker_events unitaires (7, premiers) ; test inserts-non-bloqués (P2.1) ; test
  free_discovery dé-flaké (mtime/sleep NTFS retiré). Fake clock généralisé sur rotation_concurrency
  NON fait (tests déjà event-driven — décision documentée).
* Dockerfile : COPY complété (docker_events, station_supervisor, latency_rotation, ip_latency,
  free_discovery) + smoke-test `import opencode` au build.
* README structure régénérée + drift manifest étendu (familles warning structure/security).

## Phase 5 — Split god file (PRs suivantes)

Non exécutée dans ce cycle (processus : PRs isolées, suite verte entre chacune). La tranche 1
(noyau DB → `app/db`) est préparée : inventaire ancré + seams de test + points de DI dans
`app/db/__init__.py`.

## Validation

* ruff check . : **0 erreur** (baseline HEAD : 2 pré-existantes, corrigées au passage).
* Suite complète (hors e2e docker) : **~860 passed, 1 skip, 0 failed**.
* mypy gate modules sains : Success.
* bench_perf : exit 0, aucune régression vs baseline figée.

## Redémarrage requis

Les gains P1-P4 sur le hot path sont actifs APRÈS redémarrage du serveur (code chargé à
l'import). Mesure après restart (26/08, serveur en charge : rotations VPN + quota polls
concurrents) :

| `/health` 300 req conc=20 | Avant | Après | Δ |
|---|---|---|---|
| Débit | 241 rps | **405 rps** | **+68 %** |
| p95 | 835 ms | **293 ms** | **−65 %** |
| p99 | ~840 ms | 296 ms | −65 % |
| p50 | 17.7 ms | 20.1 ms | ≈ (bruit) |

## Incident 26/08 — test → .env

Le test réécrit `test_port_hot_restart_not_full_restart` POSTait un port+1 via l'API :
`save_env()` écrivait le VRAI `.env` à chaque run de la suite (4000 → 4010 cumulés).
Corrigé : `save_env`/`apply_server_changes` mockés dans le test (+ assert d'interception),
`.env` vérifié inchangé par hash MD5 avant/après run. Résidue `OPENCODE_WEB_PORT` purgé du `.env`.
