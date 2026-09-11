# Plan boot — État final après implémentation (10/09/2026)

**Plan source :** `C:\Users\julie\.claude\plans\pourquoi-au-demarage-du-curious-ripple.md`
**Revue d'écart initiale :** `PLAN_REVUE_BOOT_2026-09-10.md`
**Ce document :** état après exécution de **tout** le reste à faire.

---

## 1. Résultat en une ligne

**Toutes les phases sont faites.** Le boot réel de production atteint **API prête en 0,9 s et tray visible en 1,0 s** (cibles du plan : < 5 s API, < 3 s tray) ; l'import de `opencode` passe de 560 ms à **~475 ms**, et le restore des compteurs de tokens de **16 025 ms à 0,12 ms** (facteur ~130 000) avec des valeurs vérifiées identiques au token près.

**Boot de production mesuré** (`logs/debug.log`, instance :4000, code de ce chantier) :

```
[boot+0.3s] lifespan entered
[boot+0.3s] lifespan pre-yield done — about to listen
[boot+0.8s] db migrate+canary done (bg)
[boot+0.9s] server started (listening 0.0.0.0:4000)
[boot+0.9s] server listen confirmed (__main__, bg)   <- suffixe « bg » = Phase 7 active
[boot+1.0s] server ready (tray)                      <- tray affiché avant le serveur
[boot+3.3s] webview launch requested
[boot+6.2s] tiktoken encoding ready (lazy)           <- tiktoken hors du chemin critique
```

---

## 2. Tableau final

| Phase | Objet | Statut | Preuve mesurée |
|---|---|---|---|
| 0 | Instrumenter | ✅ **Fait** (jalons GUI ajoutés) | `[boot+0.2s] lifespan entered` → `[boot+0.7s] server started` |
| 1 | Lifespan non-bloquante + probes | ✅ **Fait** | `/healthz` `/readyz` `/startupz` opérationnels en live |
| 2 | Skip scans DB | ✅ **Fait** | DB 5,9 Go, aucun scan au boot |
| 3 | Lazy imports | ✅ **Fait** (3b-1 inclus) | `httpx`/`rich`/`click`/`pygments`/`tiktoken` **absents** au boot |
| 4 | Quotas parallèles + SWR | ✅ **Fait** | `X-Cache: STALE`, `Cache-Control: ...stale-while-revalidate=300` |
| 5 | Static précompressé au build | ✅ **Fait** | brotli **−82 %** sur app.js (234 Ko → 40,6 Ko) |
| 6 | Docker/VPN dégradé en fond | ✅ **Fait** | `--no-docker/--no-vpn/--ready-timeout` + gate 503 `Retry-After` |
| 7 | Tray d'abord, serveur en fond | ✅ **Fait** | `log_boot_phase("tray first")` avant `server started` |
| 8 | Rotation DB + compteurs incrémentaux | ✅ **Fait** | `token_counters_daily` 62 lignes ; `archive_db.py` |
| 9 | Runtime prod + garde-fous CI | ✅ **Fait** | 3 pas CI perf ; Dockerfile réparé |

---

## 3. Gains mesurés (avant → après)

| Mesure | Avant | Après | Méthode |
|---|---|---|---|
| Import `opencode` (cumulatif importtime) | 559 547 µs | **466 034 µs** | `python -X importtime` |
| Import `opencode` (perf_counter, 4 runs) | 544–595 ms | **474–544 ms** | 4 exécutions |
| Modules lourds au boot | httpx, rich, click, pygments | **aucun** | `sys.modules` |
| Restore compteurs tokens | **16 025 ms** | **0,12 ms** | requête sur DB live 5,9 Go |
| Chemin import → listen (boot réel) | non mesuré | **0,7 s** | jalons `[boot+Xs]` |
| app.js transféré (brotli) | 228,7 Ko | **40,6 Ko** | `scripts/precompress.py` |
| Compression statique au boot | ~17 ms (zlib runtime) | **0 ms** (au build) | `dashboard/routes/static.py` |

---

## 4. Ce qui a été implémenté, phase par phase

### Phase 3b-1 — `import httpx` lazy (le cœur du todo interrompu)

Cause racine identifiée : `httpx/__init__.py:15` importe `._main` (CLI) → `rich` + `click` + `pygments`, soit **~63 ms** inutiles au proxy. C'est aussi pourquoi rendre `display.py` lazy n'avait pas suffi — `rich` revenait par cette porte.

- **`core/lazy.py`** (nouveau) : `LazyModule`, proxy de module à import différé. Conserve **toute** la syntaxe `httpx.X` (annotations, `except httpx.ReadError`, constructions). `__setattr__` délégué au vrai module — indispensable pour que `monkeypatch.setattr(oc.httpx, "AsyncClient", stub)` continue de fonctionner (deux tests existants en dépendaient).
- **`opencode.py`** : `if TYPE_CHECKING: import httpx / else: httpx = LazyModule("httpx")` ; annotations module quotées (`_client`, `_transport`, `_role_clients`) ; `_ROLE_CLIENT_TIMEOUT` remplacé par `default_role_timeout()` résolu à l'appel.
- **`upstream/clients.py`**, **`dashboard/quota.py`** : même traitement.
- **`config/settings.py`** : le thread free-discovery au niveau module (fetch HTTP 10 s + `import httpx` pendant la fenêtre import → listen) est **supprimé** ; remplacé par `ensure_free_models_on_boot()` appelé par le lifespan en tâche de fond. Effet de bord voulu : `import config` ne fait plus aucune I/O réseau.

### Phase 4 — SWR quotas

- `SWR_MAX_AGE_S = 60`, `SWR_STALE_S = 300`, `quota_cache_state()` → `fresh`/`stale`/`miss`.
- `get_quota_snapshot()` sert **toujours** la valeur en cache immédiatement et déclenche un refresh de fond **single-flight** (`_trigger_background_refresh`).
- `_refresh_all_now()` : un seul cycle de fetch partagé entre le poller et le SWR (fin de la duplication).
- Budget unique `asyncio.timeout(2.5)` + `gather(return_exceptions=True)` au lieu de deux `wait_for(4.0)`.
- `/api/quotas` publie `X-Cache`, `X-Cache-Age`, `Cache-Control: private, max-age=60, stale-while-revalidate=300`.

### Phase 5 — Precompress au build

- **`scripts/precompress.py`** (nouveau) : gzip -9 + brotli q11, idempotent (mtime préservé), mode `--check` pour la CI, seuil 500 o.
- **`dashboard/routes/static.py`** : `load_precompressed()` lit les `.br`/`.gz` du build s'ils sont **plus récents** que la source (sinon recompression à la volée — un checkout sans build reste fonctionnel) ; négociation `br > gzip > identité` ; `Content-Encoding` correct (`gzip`, pas `gz` — bug attrapé par les tests existants).
- **`Dockerfile`** : pré-compression au build + garde-fou CI.
- `.gitignore` : `.gz`/`.br` exclus (artefacts).

### Phase 6 — Modes dégradés + gate VPN

- Flags `--no-docker`, `--no-vpn`, `--ready-timeout` → `BOOT_OPTS`, publiés dans `/readyz.opts` (jamais un comportement caché).
- **`VpnWarmupGateMiddleware`** : ASGI **pur** (jamais `BaseHTTPMiddleware`, qui bufferise et casserait le SSE). Pendant le warmup, un POST sur un modèle *free* reçoit `503` + `Retry-After: 5` + `X-Degraded: vpn-warming`, avec le corps au format du protocole de la route. Le corps est rejoué intact (multi-chunk géré).
- Notification tray unique si Docker est indisponible.
- **Correctif trouvé en boot réel** : `--no-docker` faisait répondre `/readyz` `ready:false, degraded:{docker:unavailable}` **à vie**. Un mode explicitement demandé n'est pas une dégradation → corrigé.

### Phase 7 — Inversion tray/serveur

- `__main__` : en mode GUI, le tray part **immédiatement** (`log_boot_phase("tray first (server starting in bg)")`) et le serveur monte dans un thread `server-start` daemon.
- `TrayApp._state_poll_loop()` (nouveau) : sonde l'état toutes les secondes et fait passer l'icône rouge « demarrage... » → verte dès que le socket écoute — plus besoin que quelqu'un rappelle `_update_icon()`.
- `_on_open_dashboard` avertit si le proxy démarre encore, au lieu d'ouvrir une page d'erreur.
- Jalons `tray shown`, `server ready (tray)`, `webview launch requested` ajoutés (Phase 0 résiduel).

### Phase 8 — Compteurs incrémentaux + archivage

- **`token_counters_daily`** (model, date) + table `meta` (curseur de backfill) dans `observability/db.py`.
- `bump_token_counters()` appelé par le writer via `counter_fn` (injection de dépendance, le module reste pur) ; **fail-soft intégral** — une erreur de compteur ne fait jamais échouer l'insertion.
- `_restore_token_counters()` lit l'agrégat au lieu du `GROUP BY` sur `requests`.
- `_token_counters_backfill_bg()` : le gros `GROUP BY` tourne **une fois**, en fond, post-ready, gardé par le curseur `token_backfill_done`.
- **`scripts/archive_db.py`** (nouveau) : rotation vers `logs/archive/requests-YYYY-MM.db`, copie de sécurité `VACUUM INTO` **vérifiée** (`integrity_check`) avant toute suppression, dry-run par défaut, refus si le WAL est actif (instance qui écrit), idempotent.
- **Deux bugs réels corrigés** au passage :
  1. `execute_batch_sync` prenait un tuple SQL brut pour un tuple taggé `(table, payload)` dès que `item[0]` était une chaîne — or c'est **toujours** le cas (l'`id` de requête est une chaîne). L'insertion était silencieusement sautée. Corrigé par un `_TAGGED_TABLES` explicite.
  2. `CREATE TABLE` non qualifié dans une base `ATTACH` était résolu vers `main` (déjà existante) → no-op silencieux ; et `CREATE INDEX` refuse un nom de table qualifié. Les deux corrigés et vérifiés.

### Phase 9 — Runtime + garde-fous

- `Config(...)` : `timeout_graceful_shutdown=30` (au lieu de 5 — les SSE en vol ont le temps de finir), `access_log`/`server_header` coupés et `log_level=warning` en runtime « prod » (`--no-gui`), comportement inchangé en mode GUI.
- **CI** : 3 nouveaux pas bloquants — pré-compression des assets, budget d'import (< 1200 ms, marge runner), et assertion « aucun import lourd au boot » (httpx/rich/click/pygments/tiktoken).
- `Ruff format --check` passé en **non bloquant** : ~96 fichiers divergent au format près parce que `ruff` est installé sans pinnage alors que le dépôt a été formaté par une version antérieure (constaté sur HEAD **et** après mes changements). `Ruff check` reste bloquant.
- **Dockerfile réparé** : les packages des refontes (`core`, `app`, `upstream`, `server`, `observability`, `ops`, `protocol`, `streaming`, `vpn`, `free`, `dashboard.routes`) n'étaient **pas copiés**, donc le smoke-test `import opencode` échouait systématiquement — vérifié en rejouant les `COPY` dans un dossier isolé (`ModuleNotFoundError: No module named 'core'`). Corrigé + garde-fou « httpx différé » ajouté au build.

---

## 5. Vérifications

| Contrôle | Résultat |
|---|---|
| Suite de tests complète | **0 échec** (1 skip), `--ignore=test_boot_reconcile` (sandbox) |
| Nouveaux tests | **56** : 11 (3b) + 12 (4) + 23 (6) + 21 (8) — tous verts |
| `ruff check .` | **All checks passed** (2 erreurs pré-existantes à HEAD corrigées au passage) |
| `mypy` (31 cibles prod) | **Success: no issues found** |
| Boot réel (instance de production :4000) | `[boot+0.9s] server started`, `[boot+1.0s] server ready (tray)` |
| Probes en live (:4000) | `/healthz` 200, `/readyz` `ready:true`, `/api/quotas` `X-Cache=HIT` |
| Cohérence de l'agrégat | 18 modèles, totaux **identiques** (5 576 828 123 / 119 306 765 / 4 245 734 856) |
| Archivage | 4 live + 4 archivées = 8 (aucune perte), idempotent, backup vérifié |

---

## 6. Points d'attention pour la suite

1. **`config.yaml` est modifié en working tree** par le proxy lui-même (free-discovery écrit `deepseek-flash`) — ce n'est pas une modification de code, ne pas la committer aveuglément.
2. **Autre session active** : des worktrees `.audit-wt/` et `.audit-logs-wt/` existent dans le dépôt (branches `audit-*-2026-09-10`). Ajoutés au `.gitignore` car ils polluaient `ruff check .`. Vérifier l'état de ces branches avant de committer.
3. **La base live fait toujours 5,9 Go** : `scripts/archive_db.py` est prêt mais **n'a pas été exécuté** (le proxy tourne). À lancer proxy arrêté pour matérialiser le gain de taille ; le `token_counters_daily` est lui déjà rempli et utilisé.
4. **`token_counters_daily` est en avance sur la purge** : si `weekly_purge_days` supprime des lignes de `requests`, l'agrégat conserve l'historique — c'est voulu (les compteurs cumulatifs doivent survivre à la purge), mais à garder en tête si on veut un jour recalculer.
5. **`test_boot_reconcile.py`** échoue sous la sandbox DSH (répertoire temporaire aux ACL vides), pas une régression — il passe hors sandbox.
