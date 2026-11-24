# 08 — Audit dashboard / GUI / observabilité / ops / rotation

**Date** : 2026-11-20
**Périmètre** : `dashboard/` (51 fichiers, 11 895 l.), `dashboard-ui/` + `static/`, `gui/` (5 fichiers, 484 l.), `observability/` (10 fichiers, 3 389 l.), `ops/` (17 fichiers, 2 150 l.), `rotation/` (19 fichiers, 1 448 l.), `scripts/app_supervisor.py`, `scripts/opencode_client_sync.py`, `trust.py`.
**Méthode** : lecture seule. Chaque constat est adossé à un `fichier:ligne` et, pour le code mort, à l'absence de résultat d'un grep sur tout le dépôt (hors `Lib/`, `_research/`, `.audit-wt/`, `.audit-logs-wt/`, `node_modules/`, `__pycache__/`).
**Aucune base n'a été lue** (`logs/requests.db`, `logs/debug.log`) : les constats de schéma viennent du code de création.

---

## 1. Résumé exécutif

L'architecture est **nettement au-dessus de la moyenne** sur les points que la constitution rend opposables : rétention/purge/WAL/backup de `requests.db` sont réels et testés, le middleware de confiance réseau couvre tous les `/api/*` en un point unique, la couche HTTP du frontend a un timeout et une gestion d'erreur centralisés, et **aucun doublon d'endpoint** n'existe entre `dashboard/` et `opencode.py` (135 vs 34 chemins, intersection vide).

Trois défauts structurels dominent :

1. **Le contrat du principe XII est appliqué sur 1 page sur 15.** Le gate `tests/test_option_map.py:107` ne vérifie `data-option-map` que sur les pages contenant déjà `data-option-map` ou la chaîne « Catalogue egress » (`test_option_map.py:104-106`) — soit **`PublicProxies.svelte` uniquement**. Les **171 autres contrôles interactifs** (dont 52 dans `Config.svelte`) n'ont aucun contrat vérifié automatiquement. Le gate est circulaire : il ne peut pas échouer sur une page qu'il n'inspecte pas.
2. **`rotation/application/*` (221 l.) est du code mort** — jamais importé hors `tests/test_rotation_*.py`. La migration strangler s'est arrêtée à mi-chemin : `rotation/infrastructure/db/*` est bien branché (`free/ip_registry/registry_db.py:63`, `free/quota_ledger.py:616`, `observability/incidents.py:24`, `scripts/fresh_chain*.py`), mais la couche application ne l'est pas.
3. **61 endpoints dashboard n'ont aucun appelant frontend**, dont **16 strictement morts** (aucun appelant UI, script ou test). Les familles `/api/vpn/station/{sid}/*` (15 routes) et `/api/vpn/jobs*` (3 routes) ont été construites pour une UI de contrôle par station qui n'existe pas — la doc (`docs/runbook-docker.md:21`) décrit encore un « bouton Logs » et un « bouton dédié » qui n'existent plus dans le front Svelte.

S'y ajoute un **P0 de fiabilité** : la connexion SQLite de boot ne crée que 4 des 25 colonnes ajoutées par migration (`observability/db.py:538-542` vs `:268-307`), et le writer démarre **avant** la migration de fond (`opencode.py:4284` puis `:4289`). Un insert dans cette fenêtre échoue en `OperationalError` et la ligne est **silencieusement jetée** (`observability/db.py:818`), avec un simple `debug_fn`.

---

## 2. Carte des modules

### 2.1 Backend dashboard

| Module | LOC | Rôle | Appelants |
|---|---:|---|---|
| `dashboard/api.py` | 3 047 | Orchestrateur : caches, helpers DB, redaction, enregistrement des 43 routeurs | `opencode.py:630` (`register_dashboard`) |
| `dashboard/routers/*.py` (43 f.) | ~6 500 | Routes HTTP extraites, DI par callables | `dashboard/api.py:2322-3035` |
| `dashboard/quota.py` | 1 105 | Fetch/parse quota OpenCode Go (scraping HTML), capacités & limites modèles | `dashboard/api.py:47-55` |
| `dashboard/display.py` | 523 | Rendu Rich terminal + buffer `log_lines` | `opencode.py:634-642`, `dashboard/api.py:44` |
| `dashboard/events.py` | 178 | Pub/sub SSE, coalescing `vpn_event` 500 ms | `dashboard/api.py:46`, `routers/event_routes.py` |
| `dashboard/qualification_launcher.py` | 67 | Lance `qualification_daemon.py --once` en subprocess | `dashboard/api.py:2867` |
| `dashboard/routes/static.py` | 248 | Pré-compression br/gz, middleware de cache | `dashboard/api.py:66-74` |

### 2.2 Frontend

| Élément | Taille | Rôle |
|---|---:|---|
| `dashboard-ui/src/pages/*.svelte` (7) | — | Onglets : Stats, Logs, Quotas, FreeModels, Config, Traffic, Debug |
| `dashboard-ui/src/pages/vpn/*.svelte` (8) | — | Sous-vues VPN, toutes montées depuis `FreeModels.svelte:20-26` |
| `dashboard-ui/src/lib/*.ts` | ~1 500 | `api.ts` (HTTP), `realtime.svelte.ts` (SSE), `query.svelte.ts` (cache), `router.svelte.ts` (hash) |
| `dashboard-ui/src/lib/locales.ts` | 1 463 | i18n |
| `static/dist/` | ~490 Ko | Build Vite **commité**, daté 2026-11-11 08:56 |

### 2.3 Observabilité / ops / rotation

| Module | LOC | Rôle | Branché ? |
|---|---:|---|---|
| `observability/db.py` | 1 248 | Schéma, writer batché, maintenance | ✅ `opencode.py:537` |
| `observability/socket_guard.py` | 438 | Résilience `accept` Windows, sortie code 70 | ✅ patch `Server.serve` |
| `observability/capture.py` | 590 | Capture trafic | ✅ `dashboard/api.py:41` |
| `observability/metrics.py` | 535 | Rendu Prometheus | ✅ `opencode.py:17829`, `qualification_routes.py:344` |
| `observability/canary.py` / `drift.py` / `slo.py` | 131/100/144 | Canary, dérive catalogue, SLO | ✅ `opencode.py:24278`, `:25332`, `:25216` |
| `observability/incidents.py` | 120 | Incidents persistés | ✅ `free/ip_registry/status.py:374` |
| `observability/crashguard.py` | 68 | faulthandler + excepthooks | ✅ |
| `ops/supervisor.py` | 387 | `StationSupervisor` par station | ✅ via shim `station_supervisor.py` |
| `ops/lock.py` | 162 | Verrou mono-instance | ✅ `opencode.py:14376` (équiv.) |
| `ops/fleet.py`, `reconciler.py`, `desired_state.py`, `health.py`, `jobs.py`, `updater.py`, `providers/*` | 1 180 | Flotte, reconcile, santé, jobs, update | ✅ partiel (voir §7) |
| `ops/lifecycle.py` + `lifecycle_policy.py` | 278 | Noyau cycle de vie 011 | ❌ **test-only** |
| `ops/env_store.py` | 55 | Façade `.env` | ✅ délègue à `config.store` |
| `rotation/infrastructure/db/*` | ~700 | Registre cible (schema, retention, maintenance) | ✅ |
| `rotation/application/*` | 221 | Repair / Sweep / WarmPool | ❌ **mort** |
| `rotation/domain/`, `ports/`, `interfaces/` | 3 | Coquilles (docstring seule) | ❌ **vides** |

---

## 3. Inventaire des endpoints HTTP

### 3.1 Doublons `dashboard/` ↔ `opencode.py`

**Intersection vide.** 135 chemins déclarés côté dashboard (`@app.get/post/...` dans `dashboard/api.py`, `dashboard/routers/*.py`, `dashboard/routes/static.py`), 34 côté monolithe. Aucun chemin n'est déclaré deux fois.

Les deux familles sont **complémentaires**, pas redondantes :

| Monolithe (`opencode.py`) — cœur proxy | Dashboard — exploitation |
|---|---|
| `/v1/messages`, `/anthropic/v1/messages`, `/v1/chat/completions`, `/v1/responses`, `/v1/models`, `/v1/messages/count_tokens`, `/v1/systemone`, `/v1/responses/compact` | 135 routes `/api/*` + `/` + `/static` |
| `/healthz`, `/readyz`, `/startupz`, `/health`, `/metrics` | |
| `/api/canary/run`, `/api/drift`, `/api/circuit-breakers*`, `/api/key-pauses*`, `/api/egress/{state,config,incidents,429}`, `/api/free-{health,models,chain,hunter,discovery}`, `/api/free-egress/*`, `/api/live-models*`, `/api/free/quota/*` | |

⚠️ **Friction de nommage** : les routes `/api/free-*` (monolithe, avec trait d'union) et `/api/free/*` (dashboard, avec slash) coexistent — `/api/free-models` (monolithe, `opencode.py:25975`) vs `/api/free-model-usage` (dashboard, `usage_routes.py:122`) vs `/api/free/allocator/status` (dashboard, `free_routes.py:37`). Aucune collision, mais la découverte d'API est ambiguë. P2.

### 3.2 Endpoints dashboard sans appelant frontend

61 chemins n'apparaissent ni dans `dashboard-ui/src/` ni dans `static/dist/assets/*.js`. Classement par gravité :

**16 strictement morts** (aucun appelant UI, script **ni** test) :

| Endpoint | Source |
|---|---|
| `/api/egress/qualification/candidates/{hostname}/pause` | `qualification_routes.py:370` |
| `/api/egress/qualification/candidates/{hostname}/resume` | `qualification_routes.py:374` |
| `/api/vpn/jobs/fleet/{action}` | `vpn_jobs_routes.py:26` |
| `/api/vpn/jobs/{job_id}` | `vpn_jobs_routes.py:46` |
| `/api/vpn/station/{sid}/{start,stop,restart,connect,disconnect}` | `station_control_routes.py:293-314` |
| `/api/vpn/station/{sid}/{pin_country,probe,soft-rotate,stack}` | `station_control_routes.py:165-251` |
| `/api/vpn/station/{sid}/control/{state,events,jobs/{id},switch,rotate,rollback,transport/reset}` | `station_control_routes.py:343-497` |
| `/api/vpn/station/{station_id}/logs` | `station_logs_routes.py:25` |

> **Preuve** : `grep -rn "vpn/station" dashboard-ui/src/` → 0 résultat ; `grep -o "station/" static/dist/assets/*.js` → 0 résultat ; `grep -rn "vpn/station/" --include=*.py .` hors `station_control_routes.py` → seuls `scripts/fresh_chain_supervisor.py:689` (`/probe`), `scripts/nordvpn_free_pin.py:143` (`/pin_country`) et `tests/` les référencent.

**45 endpoints « UI-less » mais consommés par script/test** — à conserver, mais leur statut « API documentée » doit être explicite (ex. `/api/pool-status` ← `scripts/capacity_report.py`, `scripts/fresh_chain.py` ; `/api/egress/qualification/{discovery,rotations,stations,watchdog}` ← `scripts/canary_qualification.py:23` ; `/api/costs`, `/api/routes`, `/api/free-model-usage` ← tests uniquement).

⚠️ **Divergence doc/code** : `docs/runbook-docker.md:21` documente « Carte station (VPN tab) → bouton **Logs** » et `:23` « bouton dédié → `POST /api/vpn/health-check/{id}` ». Ni l'un ni l'autre n'existe dans `dashboard-ui/`. Le principe XVII (« un écart doc/config est un bug ») est enfreint.

---

## 4. Tableau du contrat `option → consommateur` (principe XII)

### 4.1 Constat quantitatif

| Mesure | Valeur |
|---|---:|
| Contrôles interactifs (`<button|input|select|textarea>`) dans `dashboard-ui/src/pages/` | **184** |
| Marqueurs `data-option-map` | **17** |
| Pages portant un marqueur | **1 / 15** (`vpn/PublicProxies.svelte`) |
| Pages inspectées par le gate `test_gui_controls_have_a_contract` | **1 / 15** |

**Cause racine** — `tests/test_option_map.py:102-106` :
```python
def _catalog_pages() -> list[Path]:
    for path in PAGES.rglob("*.svelte"):
        text = path.read_text(...)
        if "data-option-map" in text or "Catalogue egress" in text:
            pages.append(path)
```
La liste des pages à contrôler est dérivée de la présence du marqueur que le contrôle vérifie. Une page sans marqueur n'est jamais inspectée. Le gate passe donc à vide pour 14 pages sur 15.

### 4.2 Contrats vérifiés (les 17 marqueurs, page `PublicProxies.svelte`)

| Contrôle | Endpoint | Clé config | Consommateur | Verdict |
|---|---|---|---|---|
| refresh catalogue | `POST /api/egress/catalog/refresh` | `egress_catalog.*` | `egress_catalog.py` | ✅ |
| refresh threat-feeds | `POST /api/egress/catalog/threat-feeds/refresh` | idem | idem | ✅ |
| block IP | `POST /api/egress/catalog/block` | `egress_catalog.blocklist` | idem | ✅ |
| sources | `POST /api/egress/catalog/sources` | `egress_catalog.sources` | idem | ✅ |
| refresh public-proxies | `POST /api/vpn/public-proxies/refresh` | `ip_rotation.public_proxies` | `free/pool` | ✅ |
| revalidate | `POST /api/vpn/public-proxies/revalidate` | idem | idem | ✅ |
| adaptive toggles | `GET /api/egress/adaptive` | `ip_rotation.egress_adaptive` | `free/auto_egress.py` | ✅ |

### 4.3 Contrôles **non couverts** (échantillon représentatif, 171 au total)

| Page | Contrôles | Endpoint(s) | Clé config | Verdict |
|---|---:|---|---|---|
| `Config.svelte` | **52** | `/api/config`, `/api/config/{custom-routes,web-search,web-fetch,api-keys}` | `server.*`, `routing.*`, `web_search.*`, `web_fetch.*` | ⚠️ **non vérifié** |
| `VpnAdvanced.svelte` | **40** | `/api/vpn-config`, `/api/free/hunter`, `/api/vpn/socks5*`, `/api/vpn/https-proxies*` | `ip_rotation.*`, `quality_*`, `post_rotation_probe.*` | ⚠️ **non vérifié** |
| `VpnRouting.svelte` | 18 | `/api/vpn-config`, `/api/retry-config` | `routing.*`, `retry_until_success.*` | ⚠️ **non vérifié** |
| `VpnStatus.svelte` | 13 | `/api/vpn/toggle`, `/api/vpn/proxy-mode`, `/api/vpn-config`, `/api/geo-policies` | `ip_rotation.{enabled,proxy_mode,latency_rotation,rotation_planner,quota_per_ip}`, `geo.*` | ⚠️ **non vérifié** |
| `VpnServers.svelte` | 13 | `/api/vpn-config`, `/api/vpn/{connect,next,disconnect,upload-config}` | `vpn.*` | ⚠️ **non vérifié** |
| `VpnCapacity.svelte` | 5 | `/api/egress/capacity`, `/api/vpn-config`, `/api/vpn/socks5/toggle` | `ip_rotation.*` | ⚠️ **non vérifié** |
| `QualificationStatus.svelte` | 13 | `/api/egress/qualification/{epochs,qualifiers,rotate}` | `qualification.*` | ⚠️ **non vérifié** |
| `Traffic.svelte` | 4 | `/api/traffic/{config,clear}` | `traffic_capture.*` | ⚠️ **non vérifié** |
| `Logs.svelte` | 10 | `/api/history*` | — (lecture) | ⚠️ non vérifié |
| `Debug.svelte` | 1 | `/api/debug` | `OPENCODE_DEBUG` | ⚠️ **non vérifié** |

**Vérifications ponctuelles effectuées manuellement** (les contrôleurs que j'ai pu tracer de bout en bout) :

| Contrôle | Clé | Consommateur runtime | Effet observable | Verdict |
|---|---|---|---|---|
| `Config.svelte:624` port | `OPENCODE_PORT` | `ServerManager.restart` | redémarrage | ✅ réel |
| `Config.svelte:628` host | `OPENCODE_HOST` | idem | idem | ✅ réel |
| `Config.svelte:635` routing | `API_KEY_ROUTING` | `opencode.API_KEY_ROUTING` (`config_routes.py:198`) | sélection de clé | ✅ réel |
| `Config.svelte:653` debug | `OPENCODE_DEBUG` | `settings.DEBUG` + fichier | logs | ✅ réel |
| `Config.svelte:674` disable_mapping | `DISABLE_MAPPING` | `settings.DISABLE_MAPPING` | routage | ✅ réel |
| `Config.svelte:814-851` web_search | `web_search.*` | **non trouvé** | — | ❌ **à investiguer** |
| `Config.svelte:873-916` web_fetch | `web_fetch.*` | **non trouvé** | — | ❌ **à investiguer** |
| `VpnAdvanced.svelte:148` hunter | `free.hunter` | `free/hunter.py` | démarrage hunter | ✅ réel |
| `Traffic.svelte:146` capture | `traffic_capture.*` | `observability.capture` | capture active | ✅ réel |
| `VpnStatus.svelte:257` latency_rotation | `ip_rotation.latency_rotation` | `latency_rotation.py` | rotations | ✅ réel |

⚠️ **Deux alertes à instruire** : `web_search` / `web_fetch` sont écrits par `POST /api/config/web-search` (`config_extra_routes.py:94-120`) et `POST /api/config/web-fetch`, mais `grep -rn 'yaml_get("web_search"' --include=*.py` ne retourne **aucun consommateur** hors du routeur qui les lit pour l'affichage. Les occurrences trouvées (`app/protocol/mapping.py:1780`, `:2706`, `opencode.py:19527`) concernent le **nom d'outil** `web_search` côté protocole, pas la section de configuration. Si ce constat est confirmé, ce sont **~14 contrôles inertes** (mode, target_model, max_results, timeout, enabled, via_vpn, on_error × 2 sections) = violation directe du principe XII. À vérifier par un test runtime avant suppression.

> **⚠️ CORRECTION (auditeur principal, 2026-11-20 12:45)** — **Faux positif.**
> Les consommateurs existent, mais le grep de l'auditeur ne les a pas vus car
> ils sont dans `opencode.py` et non dans un module `free/` ou `config/` :
>
> - `opencode.py:19721` `if not yaml_get("web_search", "enabled", True):` →
>   strip de l'outil + log
> - `opencode.py:19731-19735` `mode`, `target_model`, `max_results`,
>   `timeout`, `via_vpn` — tous lus et utilisés dans `_handle_web_search`
>   (routage vers `target_model`, validation du mode, passthrough natif)
> - `opencode.py:19868` `if not yaml_get("web_fetch", "enabled", True):`
> - `opencode.py:19904-19908` `mode`, `target_model`, `timeout`,
>   `max_bytes`, `via_vpn` — lus et utilisés dans le handler `web_fetch`
>
> Seul `on_error` reste sans consommateur runtime identifié (écrit et relu
> pour l'affichage uniquement) — soit 2 contrôles sur 14, pas 14.
> **Verdict corrigé : les contrôles `web_search`/`web_fetch` sont réels.**
> La leçon à retenir est méthodologique : un grep d'absence de consommateur
> doit couvrir `opencode.py`, où vivent la majorité des consommateurs
> runtime.

### 4.4 Valeur effective vs valeur enregistrée (XII, 2ᵉ alinéa)

**Bons points observés** :
- `Traffic.svelte:112-115` — le toggle lit `status.user_enabled` (intention) et retombe sur `status.enabled` (effectif) ; le backend expose les deux (`traffic_routes.py:31-33`).
- `VpnStatus.svelte:501-508` — `free_model_map_effective` est utilisé pour griser les entrées inactives.
- `PublicProxies.svelte:570-582` — colonne « adaptive_effective » affichée à côté de la valeur de base.
- `Config.svelte:69-77` — `refresh_state()` appelé avant d'afficher `proxy_running` (self-heal du flag).

**Défauts** :
- `VpnRouting.svelte:72` — commentaire « L'API clampe le mode manuel à [1, 3] ; afficher la valeur effective évite… » : la valeur effective n'est **pas** relue après clamp, seulement commentée.
- Les 171 contrôles sans contrat ne peuvent pas être évalués sur ce critère faute de traçabilité — c'est une conséquence directe de §4.1.

---

## 5. Findings

### P0

---

**P0-1 — Fenêtre de perte de données silencieuse au boot (colonnes manquantes)**

- **Fichier** : `observability/db.py:538-542` (schéma de boot) vs `observability/db.py:268-307` (migrations complètes) ; ordonnancement `opencode.py:4284` puis `opencode.py:4289` ; avalement `observability/db.py:818`.
- **Preuve** :
  - `init_requests_schema_fast` n'exécute que 4 `ALTER TABLE ... ADD COLUMN` : `ttfb_ms`, `stop_reason`, `aborted`, `server_host` (`db.py:538-542`).
  - `_REQUEST_COLUMN_MIGRATIONS` en compte 25 (`db.py:268-307`). Diff programmatique : **21 colonnes** (`client_ip`, `account_alias`, `tools`, `tools_used`, `request_body`, `response_body`, `client_user_agent`, `free_model_ip`, `identity`, `geo_*` ×6, `station`, `free_status`, `paid_status`, `is_stream`, `protocol`, `thinking`, `effort`, `hedged`, `winner_station`, `client_request_id`, `attempt_no`, `final_status`) ne sont créées que par `migrate_and_canary`, lancée en tâche de fond (`opencode.py:4289`) **après** `_db_writer_loop` (`opencode.py:4284`).
  - `_INSERT_REQUESTS_SQL` (`db.py:309-317`) référence ces 21 colonnes → `OperationalError: table requests has no column named …`.
  - Le writer attrape l'exception par item et **jette la ligne** : `except Exception as e: debug_fn(f"  [db] batch item skipped ...")` (`db.py:818`) — aucun compteur, aucune remontée.
- **Impact** : sur une base neuve (ou un premier boot après `rm logs/requests.db`), **toute requête servie avant la fin de la migration de fond est perdue** pour l'observabilité. Sur une base existante, l'impact est nul (les colonnes sont déjà là) — c'est un bug de « première exécution » et de reprise après restauration, précisément les scénarios où l'observabilité compte le plus. Sur ~6 Go, la migration est justement ce qui a été sortie du chemin de boot, donc la fenêtre n'est pas courte.
- **Recommandation** : exécuter les 21 `ALTER TABLE ADD COLUMN` dans `init_requests_schema_fast` (ALTER = métadonnée, ~0 ms — c'est déjà l'argument du commentaire `db.py:536-537`) et réserver à `migrate_and_canary` les seuls `CREATE INDEX` + canary. Alternative : bloquer `_db_writer_loop` sur un `asyncio.Event` armé par `_db_migrate_bg`. Ajouter un test qui insère une ligne complète juste après `init_requests_schema_fast` sans appeler `migrate_and_canary`.
- **Effort** : **S**

---

**P0-2 — Le gate du principe XII ne couvre que 1 page sur 15 (contrôle circulaire)**

- **Fichier** : `tests/test_option_map.py:102-106` (`_catalog_pages`), `:107-129` (`test_gui_controls_have_a_contract`).
- **Preuve** :
  ```python
  def _catalog_pages() -> list[Path]:
      for path in PAGES.rglob("*.svelte"):
          text = path.read_text(...)
          if "data-option-map" in text or "Catalogue egress" in text:
              pages.append(path)
  ```
  Seules les pages contenant déjà `data-option-map` sont inspectées. Mesures : `grep -coE "<(button|input|select|textarea)\b" dashboard-ui/src/pages/*.svelte dashboard-ui/src/pages/vpn/*.svelte` → **184** contrôles ; `grep -rho 'data-option-map='` → **17** ; `grep -rl data-option-map dashboard-ui/src/pages/` → **`vpn/PublicProxies.svelte` seulement**. Le test passe actuellement (`pytest tests/test_option_map.py` → 4 passed).
- **Impact** : le principe XII (« toute option GUI a un effet réel… aucune option inerte n'est acceptée ») et la gate XVII (« `option-map` à 0 ligne manquante ») sont **structurellement inapplicables** à 171 contrôles, dont les 52 de `Config.svelte` et les 40 de `VpnAdvanced.svelte`. C'est le défaut le plus large du périmètre.
- **Recommandation** : inverser la logique — `PAGES.rglob("*.svelte")` sans filtre, avec une liste d'exclusion explicite et justifiée (`Logs.svelte`, `Stats.svelte` = lecture seule sans mutation). Ajouter une assertion de couverture : `assert controls_with_marker >= 0.9 * total_controls`.
- **Effort** : **M** (le code du gate est court ; le travail réel est de produire les ~171 lignes de contrat manquantes, ou de marquer explicitement « display-only »).

---

### P1

---

**P1-1 — `rotation/application/*` : 221 lignes de code mort, migration strangler inachevée**

- **Fichier** : `rotation/application/repair.py` (91 l.), `sweep.py` (58 l.), `warm_pool.py` (72 l.).
- **Preuve** : `grep -rn "RepairOrchestrator|SweepCursor|WarmPool|rotation.application" --include=*.py .` (hors `Lib/`, `.audit*`, `__pycache__`) → **uniquement** `tests/test_rotation_repair.py:8`, `tests/test_rotation_sweep.py:5`, `tests/test_rotation_warm_pool.py:5`. Aucun import dans `opencode.py`, `free/`, `dashboard/`, `ops/`, `scripts/`. Le `rotation/__init__.py` l'annonce lui-même : « *no runtime path is switched here yet* ».
- **Impact** : le principe XIV (strangler) est à mi-chemin — l'infrastructure est branchée (`free/ip_registry/registry_db.py:63`, `free/quota_ledger.py:616`, `observability/incidents.py:24`, `scripts/fresh_chain*.py`, `scripts/registry_maintenance.py:19`) mais la couche application non. `RepairOrchestrator.reconcile` (`repair.py:70-88`) duplique conceptuellement `ops/reconciler.py:79` et `ops/supervisor.py:366` (`sync_supervisors`) — trois façons de réconcilier des slots. `rotation/domain/`, `ports/`, `interfaces/` sont des coquilles d'une ligne.
- **Recommandation** : trancher. Soit brancher `RepairOrchestrator`/`WarmPool`/`SweepCursor` dans le runtime et décommissionner les équivalents `ops/`, soit supprimer `rotation/application/` + `domain/` + `ports/` + `interfaces/` et documenter que `rotation/` n'est qu'un adaptateur DB. La demi-mesure actuelle viole VIII (un seul réparateur) et XIV.
- **Effort** : **L**

---

**P1-2 — `ops/lifecycle.py` + `lifecycle_policy.py` (278 l.) : noyau de cycle de vie jamais branché**

- **Fichier** : `ops/lifecycle.py` (174 l.), `ops/lifecycle_policy.py` (104 l.).
- **Preuve** : `grep -rn "LifecycleEngine|SlotObservation|SlotStatus|StartupPolicy|RepairPolicy|RotationBudget" --include=*.py .` hors tests → `ops/lifecycle.py` lui-même, `free/pool/__init__.py:63` (importe un `RepairPolicy` **différent**, depuis `station_repair`), `free/warp_rotation.py:46` (`RotationBudgetStore`, classe distincte). Les seuls consommateurs réels sont `tests/test_plan_011_baseline.py` et `tests/test_plan_011_consolidation.py`. En revanche `ops/lifecycle_evidence.py:39` (`log_evidence`) **est** utilisé en production (`free/warp_rotation.py:23,331,524`).
- **Impact** : 278 lignes de domaine pur maintenues et testées sans effet runtime. Le principe XI/XIV promet « ajouter un provider = un adapter » ; ici la couche de politique existe mais l'orchestrateur est ailleurs (`free/pool`, `ops/supervisor`). Risque de divergence de sémantique entre `CAUSE_ACTIONS` (`lifecycle_policy.py:8-21`) et les décisions réellement appliquées.
- **Recommandation** : soit brancher `LifecycleEngine` comme source unique des transitions d'état de station (ce que VIII demande), soit supprimer et conserver uniquement `lifecycle_evidence`. Documenter le choix dans `debt.md`.
- **Effort** : **M**

---

**P1-3 — 16 endpoints strictement morts, dont 15 pour une UI de contrôle par station inexistante**

- **Fichiers** : `dashboard/routers/station_control_routes.py` (514 l. — 15 routes sans appelant), `station_logs_routes.py:25`, `vpn_jobs_routes.py:26,46`, `qualification_routes.py:370,374`.
- **Preuve** : `grep -rn "vpn/station" dashboard-ui/src/` → **0** ; `grep -o "station/" static/dist/assets/*.js` → **0** ; liste complète en §3.2. Seuls `scripts/fresh_chain_supervisor.py:689` (`/probe`), `scripts/nordvpn_free_pin.py:143` (`/pin_country`) et les tests les utilisent.
- **Impact** : 514 lignes de routeur (le 3ᵉ plus gros fichier de `dashboard/routers/`) exposent des mutations per-station (start/stop/restart/switch/rotate/rollback/transport-reset) **sans aucune interface**. C'est de la surface d'attaque non sollicitée, et un écart doc/code (`docs/runbook-docker.md:21-23` décrit des boutons disparus). Les principes XII (par symétrie : une action sans UI) et XVII (« zéro option morte ») sont concernés.
- **Recommandation** : décider par famille. (a) Les routes utilisées par les scripts (`/probe`, `/pin_country`) restent, documentées comme « API opérateur » dans un contrat explicite. (b) Les 13 autres (`/control/*`, `/stack`, `/soft-rotate`, start/stop/restart/connect/disconnect, `/logs`) : soit les brancher dans `VpnServers.svelte` (la doc les promet), soit les supprimer. Corriger `docs/runbook-docker.md` dans le même lot.
- **Effort** : **M**

---

**P1-4 — `web_search` / `web_fetch` : 14 contrôles GUI possiblement inertes**

- **Fichier** : `dashboard-ui/src/pages/Config.svelte:814-916` (contrôles), `dashboard/routers/config_extra_routes.py:57-120` (lecture/écriture `yaml_set("web_search", …)`).
- **Preuve** : `grep -rn 'yaml_get("web_search"|yaml_get("web_fetch"' --include=*.py .` → **aucun consommateur** hors `config_extra_routes.py`. Les occurrences de `web_search` ailleurs (`app/protocol/mapping.py:1780,1828,2706-2712`, `opencode.py:19527-19570`) portent sur le **nom d'outil du protocole Anthropic**, pas sur la section `config.yaml`. Le routeur écrit bien la config (`yaml_set`) et la relit pour l'afficher — boucle fermée sans effet runtime.
- **Impact** : si confirmé, ~14 contrôles (mode, target_model, max_results, timeout, enabled, via_vpn, on_error, pour les deux sections) sont **inertes** — violation littérale du principe XII. Un opérateur qui active `web_fetch.enabled` ou `via_vpn` croit modifier le comportement du proxy et ne le fait pas. Note : une partie est déjà marquée `on_error: strip` en lecture seule (`config_extra_routes.py:75`, `:67`).
- **Recommandation** : **avant toute suppression**, exécuter un test runtime (activer `web_search.mode=model`, observer si une requête avec outil `web_search` change de comportement). Si rien ne change : soit marquer « affichage seul » dans l'option-map et retirer les contrôles d'écriture, soit brancher le consommateur manquant. C'est exactement le cas d'usage que le gate P0-2 devrait attraper.
- **Effort** : **S** (investigation) → **M** (branchement ou retrait)

---

**P1-5 — `_filters_cache` sans expiration, invalidation partielle**

- **Fichier** : `dashboard/api.py:126-132` (déclaration), `dashboard/routers/history_routes.py:237` (lecture), `:256` (écriture), `:283` (invalidation).
- **Preuve** : `_filters_cache: dict | None = None` est un simple global, sans TTL (contraste avec `_TTLCache` à `api.py:79-122`). Seul `DELETE /api/history` l'invalide (`history_routes.py:283`). Aucun autre chemin ne le remet à `None`.
- **Impact** : les dropdowns de filtre (modèles, modèles originaux, comptes, outils) sont figés au premier chargement de la session proxy. Après l'arrivée de nouveaux modèles ou de nouveaux outils, l'opérateur ne peut plus les filtrer sans redémarrer le proxy. À l'inverse, la clé `histcount:*` **est** TTL-ée (2 s) — incohérence entre deux caches voisins.
- **Recommandation** : donner un TTL à `_filters_cache` (30-60 s) ou l'invalider sur `models_updated` / `free_models_updated` (événements déjà publiés, cf. `realtime.svelte.ts:193-211`). Le registre `tools_seen` (`db.py:203-205`) est déjà maintenu côté writer — l'invalider là est naturel.
- **Effort** : **S**

---

**P1-6 — `/api/history/filters` : trois `DISTINCT` non bornés sur la table complète**

- **Fichier** : `dashboard/routers/read_models.py:125-147`.
- **Preuve** :
  ```python
  "SELECT DISTINCT model FROM requests WHERE model IS NOT NULL ORDER BY model"
  "SELECT DISTINCT original_model FROM requests WHERE original_model IS NOT NULL ORDER BY original_model"
  "SELECT DISTINCT account_alias FROM requests WHERE account_alias IS NOT NULL ORDER BY account_alias"
  "SELECT tools_used FROM requests WHERE tools_used IS NOT NULL AND tools_used != '[]'"
  ```
  La 4ᵉ requête charge **toutes** les lignes non vides de `tools_used` en mémoire puis les parse en JSON côté Python (`merge_tool_names`, `read_models.py:150-164`).
- **Impact** : les index `idx_model` (`db.py:257`), `idx_original_model` (`:265`), `idx_account` (`:259`) couvrent les trois premiers `DISTINCT` — coût acceptable. Le 4ᵉ (`tools_used`) n'a **aucun index** et charge un volume proportionnel au trafic total (atténué par `_filters_cache`, mais cf. P1-5 : ce cache ne périme jamais et n'est peuplé qu'une fois). Sur une base à 60-90 j de rétention, c'est le scan le plus lourd du dashboard. Le registre `tools_seen` côté writer (`db.py:203-205`) existe précisément pour éviter ça, mais `merge_tool_names` continue de parser les lignes.
- **Recommandation** : lire les outils depuis `tools_seen` (maintenu en mémoire par le writer) au lieu de re-scanner la table ; à défaut, borner par `timestamp` sur la fenêtre de rétention et indexer `tools_used`.
- **Effort** : **S**

---

**P1-7 — `_query_identity` : window function sur la table entière**

- **Fichier** : `dashboard/api.py:2504-2515`.
- **Preuve** :
  ```sql
  SELECT free_model_ip, identity FROM (
    SELECT free_model_ip, identity,
           ROW_NUMBER() OVER (PARTITION BY free_model_ip ORDER BY timestamp DESC) AS rn
    FROM requests
    WHERE free_model_ip IS NOT NULL AND free_model_ip != ''
      AND identity IS NOT NULL AND identity != ''
    ORDER BY timestamp DESC LIMIT 1000
  ) WHERE rn = 1
  ```
  Le `LIMIT 1000` s'applique au **résultat de la window function**, pas à son entrée : SQLite doit partitionner et numéroter **toutes** les lignes éligibles avant de tronquer. `identity` n'a **aucun index** (`grep -n identity observability/db.py` → seuls `:281` migration et `:313` colonne d'INSERT).
- **Impact** : coût O(table) à chaque `_ip_stats_db` (appelé par `/api/vpn-status`, cf. `vpn_status_routes.py:2609`). Atténué par `_stats_cache.set("ip_stats", stats, ttl=10)` (`api.py:2558`) et par le fait que `idx_free_ip` (`db.py:261`) filtre en amont sur `free_model_ip` — mais le tri par `timestamp` dans chaque partition reste non couvert.
- **Recommandation** : index composite `(free_model_ip, timestamp DESC)` ou remplacer par une requête corrélée avec `MAX(timestamp)` par IP (couvert par `idx_free_ip` + `idx_timestamp`). Vérifier avec `EXPLAIN QUERY PLAN`.
- **Effort** : **S**

---

**P1-8 — Contrôles GUI sans vérification de la valeur effective**

- **Fichier** : `dashboard-ui/src/pages/vpn/VpnRouting.svelte:72`.
- **Preuve** : commentaire explicite « *L'API clampe le mode manuel à [1, 3] ; afficher la valeur effective évite…* » — mais la valeur affichée reste `cfg?.vpn_stack` / la valeur postée, sans relecture après clamp. Le principe XII impose « le GUI affiche la valeur effective, pas seulement la valeur enregistrée ».
- **Impact** : un opérateur qui saisit `5` verra `5` alors que le runtime applique `3`. Même classe de problème pour tout contrôle dont le backend clampe silencieusement — et sans contrat (P0-2), ces cas sont indétectables.
- **Recommandation** : après un `POST` de mutation, relire le champ depuis la réponse (les handlers renvoient déjà l'état, ex. `capacity_routes.py:51`) et repeindre depuis cette valeur. Généraliser : le contrat option-map devrait exiger un champ « valeur effective » quand le backend clampe.
- **Effort** : **S**

---

### P2

---

**P2-1 — `dashboard/api.py` : 3 047 lignes, au-dessus du seuil constitutionnel de 1 500**

- **Fichier** : `dashboard/api.py` (3 047 l.).
- **Preuve** : `wc -l` → 3 047. Seul module du périmètre au-dessus de 1 500 avec `opencode.py` (33 618 l.). Le principe XIV dit « aucun nouveau module ne dépasse 1 500 lignes ; les monolithes existants sont extraits par lots ».
- **Note positive** : l'extraction est **active et récente** — 43 routeurs déjà sortis (≈6 500 l. déplacées), le fichier ne contient plus que des helpers (`_build_where`, `_redact_compose_text`, `_persist_*`) et l'orchestrateur `register_dashboard` (`api.py:2240-3047`).
- **Recommandation** : voir plan de découpage §8.
- **Effort** : **M**

---

**P2-2 — Duplication `dashboard/api.py` ↔ `opencode.py` : helpers DB parallèles**

- **Fichiers** : `opencode.py:1014` (`_db_vacuum_if_needed`), `:1031` (`_db_cleanup_old_bodies`), `:1789` (`_wal_checkpoint`) vs `observability/db.py:833` (`vacuum_if_needed`), `:856` (`cleanup_old_bodies`), `:985` (`wal_checkpoint`).
- **Preuve** : les fonctions du monolithe sont de fines délégations (`opencode.py:1034` → `_app_db.cleanup_old_bodies(...)`), donc pas de duplication de logique. En revanche le couple `app/db/__init__.py` (shim, « *Suppression prévue Phase 9 après preuve de non-usage* ») et `observability/db.py` (canonique) reste en place.
- **Impact** : faible (délégation, pas de fork). Mais `app/db/__init__.py` re-exporte délibérément des internals (`_INSERT_*`, `_purge_old_rows_locked`) « consommés par `tests/test_phase0_contracts.py` » — la suppression est bloquée par un test de contrat, pas par du code runtime.
- **Recommandation** : exécuter la Phase 9 annoncée : `grep` de non-usage, mise à jour du test de contrat, suppression de `app/db/`. Idem pour les shims racine `station_supervisor.py`, `shared_rotation.py`, `latency_rotation.py`, `protocol_mapping.py`, `traffic_capture.py` (tous encore importés — cf. §6).
- **Effort** : **S**

---

**P2-3 — Deux systèmes de télémétrie pour les compteurs de tokens**

- **Fichiers** : `observability/db.py:343-369` (`token_counters_daily`, agrégat incrémental) et `dashboard/routers/read_models.py:12-38` (`STATS_TOTALS_SQL`, `SUM()` à la volée).
- **Preuve** : `token_counters_daily` est alimenté par le writer (`_UPSERT_TOKEN_COUNTERS_SQL`, `db.py:360`) et restauré au boot via `SELECT ... WHERE date >= ?` (`db.py:333-342`, « <10 ms »). Mais `GET /api/stats` calcule ses totaux par `SUM(tokens_input)` sur `requests` filtré par `WHERE timestamp >= ?` (`read_models.py:12-19` + `stats_routes.py:55-63`).
- **Impact** : les deux chemins doivent rester cohérents (même définition de « token ») sans test qui le vérifie. Les index `idx_timestamp`/`idx_ts_model` rendent le `SUM` acceptable, mais la table d'agrégat existe précisément pour l'éviter.
- **Recommandation** : soit `/api/stats` lit `token_counters_daily` pour les périodes alignées sur le jour, soit ajouter un test d'égalité entre les deux sources (c'est la seule façon de garantir qu'elles ne divergent pas).
- **Effort** : **S**

---

**P2-4 — Endpoints sans timeout explicite et erreurs avalées dans les handlers**

- **Fichiers** : `dashboard/routers/vpn_status_routes.py` (11 `except: pass`), `config_extra_routes.py` (9), `debug_routes.py:126-132`, `free_routes.py:20-27`.
- **Preuve** :
  - `debug_routes.py:126-132` : `except Exception as error: return {"logs": [], "total": 0, "has_more": False, "error": str(error)}` — l'erreur est retournée en HTTP **200** avec un corps « vide ». Le front (`Debug.svelte`) affichera une liste vide, indistinguable d'un log réellement vide.
  - `config_extra_routes.py:110-111` et `:119-120` : `except Exception: pass` autour de `int(body["max_results"])` — une valeur invalide est silencieusement ignorée, le front croit la config appliquée.
  - `vpn_status_routes.py` : 11 `except … pass` dans un handler qui agrège 5 managers.
- **Impact** : pas de blocage (les timeouts réseau sont posés là où il faut — `api.ts:19` côté front, `httpx.AsyncClient(timeout=8)` côté quota `quota.py:51`), mais des échecs **invisibles** pour l'opérateur. Le compteur global `db_maintenance_errors` (`db.py:37-43`) existe pour `observability/db.py` ; il n'a pas d'équivalent dashboard.
- **Recommandation** : pour `debug_routes.py`, retourner un statut HTTP non-2xx quand `error` est posé. Pour les `except: pass` de validation, journaliser au niveau `warning` (le `debug()` existe déjà et est injecté partout).
- **Effort** : **S**

---

**P2-5 — Aucun endpoint dashboard n'a de timeout de requête serveur**

- **Fichiers** : l'ensemble de `dashboard/routers/*.py`.
- **Preuve** : les handlers `async def` sans `await` long (ex. `config_routes.py:40` `get_config`, `stats_routes.py:25` `get_stats`) s'exécutent sur la boucle ; les lectures DB passent par `_db_read_sync` → `asyncio.to_thread` (`api.py:442-463`), ce qui est correct. Mais **rien ne borne** le temps total : si la connexion RO tombe en `busy` (le retry `time.sleep(0.05)` à `api.py:457` puis le fallback sous `_db_lock` à `:461`), le handler peut attendre le `busy_timeout` SQLite (5 s par défaut, `db.py:527`).
- **Impact** : un `/api/stats` peut dépasser le budget « UI p95 < 2 s » (XVII) sans qu'aucune alerte ne se déclenche, et sans que le client puisse distinguer « lent » de « bloqué » (le timeout front est à 20 s, `api.ts:19`).
- **Recommandation** : `asyncio.wait_for` autour des `db_read_sync` avec un budget par endpoint, et un compteur de dépassements exposé (le pattern `_bump_misc_counter` de `opencode.py:1219` est réutilisable).
- **Effort** : **M**

---

**P2-6 — 322 fichiers JSON + 927 entrées dans `logs/`, dont ~110 scripts Python jetables**

- **Preuve** : `ls logs/ | wc -l` → 927 ; `ls logs/*.json | wc -l` → 322 ; `ls logs/*.py` → ~110 fichiers (`inspect_*.py`, `query_*.py`, `peek_*.py`, `fix_022_*.py`, `mark_022_*.py`, `probe_*.py`…). Répertoires annexes : `.audit-wt` 13 M, `.audit-logs-wt` 14 M, `.artifacts` 42 M, `.kilo` 61 M, `logs` **15 G**.
- **Impact** : `logs/` est exclu de l'indexation par `.claudeignore` (règle projet) mais reste sur disque. Les scripts `logs/*.py` sont des outils d'investigation one-shot : ils ne sont ni suivis comme scripts, ni documentés, et **`logs/find_visible_spawn.py:25` réimplémente une version du gate XVI** — deux implémentations qui peuvent diverger (le gate officiel est `tests/test_no_visible_spawn.py`).
- **Recommandation** : purger `logs/*.py` et `logs/*.json` non référencés (l'archivage mensuel de `requests.db` est déjà en place, `db.py:1074`). Déplacer tout outil d'audit réutilisable dans `scripts/`. Vérifier que `logs/find_visible_spawn*.py` est bien mort (le gate officiel le remplace).
- **Effort** : **S**

---

**P2-7 — `logs/registry.db` (167 Mo) sans maintenance planifiée**

- **Fichiers** : `rotation/infrastructure/db/maintenance.py` (`run_once`), `rotation/infrastructure/db/retention.py:18-39` (`prune`, `integrity_ok`, `backup`).
- **Preuve** : la machinerie est **complète et de bonne qualité** — `prune` sur 5 tables avec des durées distinctes (`retention.py:9-15`), `checkpoint`, `integrity_ok` (`:46-48`), `backup` par `VACUUM INTO` (`:51-57`). Mais `run_once` n'est appelé que par `scripts/registry_maintenance.py:30` — or `grep -rn "registry_maintenance" --include=*.py --include=*.ps1 --include=*.bat --include=*.md .` ne retourne **aucune planification** (ni tâche planifiée, ni boucle dans `opencode.py`). Les seuls autres appelants sont `scripts/fresh_chain_supervisor.py:2243,3245` (mode chaîne fraîche).
- **Impact** : `logs/registry.db` fait **167 Mo** au 2026-11-20, contre 167 Mo pour `requests.db` — la base du registre pèse autant que la base d'observabilité, sans purge automatique. `logs/_catalog_copy.db` (106 Mo) et `logs/archive/requests-2026-10.db` (332 Mo) s'y ajoutent.
- **Recommandation** : planifier `run_once` (quotidien) depuis la boucle de maintenance de `opencode.py:4009` (`_db_maintenance_loop`), à côté du `cleanup_old_bodies` quotidien et du `weekly_maintain` hebdomadaire. Le code existe, seule la planification manque.
- **Effort** : **S**

---

**P2-8 — `logs/` : 15 Go, `requests.db` 167 Mo — cohérent avec la rétention, mais pas de garde-fou global**

- **Preuve** : `requests.db` = 167 133 184 o. `weekly_maintain` est bien câblé (`opencode.py:4044-4046`) avec `weekly_purge_days=90` et `archive_after_days=60`, et une alerte à >1 Go existe (`opencode.py:4061`). `cleanup_old_bodies` (rétention corps 7 j, suppression 30 j) tourne quotidiennement (`opencode.py:4000`). **Le principe XIII est donc respecté pour `requests.db`** — c'est le module le mieux tenu du périmètre.
- **Impact** : les 15 Go de `logs/` viennent des fichiers annexes (JSON, DB de travail, archives, répertoires de test), pas de la base principale. Aucun mécanisme ne borne l'ensemble.
- **Recommandation** : un `scripts/purge_logs.py --apply` existe déjà (mentionné `opencode.py:4063`) — l'inclure dans la maintenance hebdomadaire plutôt que de le laisser manuel.
- **Effort** : **S**

---

### P3

---

**P3-1 — `hedged` / `winner_station` : colonnes écrites mais jamais lues**

- **Preuve** : les colonnes existent (`db.py:284-285`), le code écrit `"hedged": True` (`opencode.py:13818`) et suit `winner_station` (`opencode.py:12829-12973`), mais aucune requête du dashboard ne les sélectionne (`grep -n "hedged\|winner_station" dashboard/routers/*.py dashboard/api.py` → 0). Même constat pour `client_request_id` / `attempt_no` / `final_status` (`db.py:304-306`), utilisés pour la corrélation mais pas exposés dans `/api/history` (`history_routes.py:68-74`).
- **Impact** : faible — ces colonnes servent au diagnostic en SQL direct. Mais elles contribuent à la taille de la table sans usage UI.
- **Effort** : **S** (documenter ou exposer)

---

**P3-2 — `dashboard/routers/read_models.py:120` : `SELECT *` sur `requests`**

- **Preuve** : `"SELECT * FROM requests WHERE id = ?"`. La table a ~40 colonnes dont `request_body`/`response_body` (jusqu'à 100 000 caractères chacun, `db.py:875-878`).
- **Impact** : un seul enregistrement, donc borné — mais charge potentiellement ~200 Ko de corps pour un détail de requête. `history_routes.py:221-226` les parse ensuite en JSON.
- **Effort** : **S**

---

**P3-3 — Polling frontend : 3 boucles à 3 s + 1 à 5 s, toutes sans garde `visibilitychange` propre**

- **Preuve** : `Debug.svelte:24` (`AUTO_REFRESH_MS = 3000`, boucle `:58`), `Traffic.svelte:21` (`3000`, boucle `:120`), `VpnAdvanced.svelte:143` (`setInterval(… , 5000)`), `Quotas.svelte:52` (`TICK_MS = 1000` — horloge locale, pas de réseau, donc acceptable).
- **Impact** : `realtime.svelte.ts:268-277` ferme bien le flux SSE et le fallback quand l'onglet est masqué — **mais** ces `setInterval` de page ne sont pas couverts par cette logique (ils dépendent du montage du composant). Un onglet Debug laissé ouvert en arrière-plan continue d'appeler `/api/debug/logs` toutes les 3 s. Atténué par le `_cache` par signature (`debug_routes.py:76-79`), donc l'impact réel est faible.
- **Recommandation** : aligner ces boucles sur `document.hidden` (le helper `realtime.svelte.ts` peut exposer un `onVisible`).
- **Effort** : **S**

---

**P3-4 — `/api/vpn/export` expose l'historique d'IP complet**

- **Fichier** : `dashboard/routers/vpn_state_routes.py:88-105`.
- **Preuve** : `state = {"ip_history": manager._ip_history, "total_switches": …}` — l'intégralité de l'historique (IP + horodatage + serveur) est renvoyée telle quelle.
- **Impact** : pas de secret (pas de credential), mais c'est de la donnée d'egress en masse. Protégé par `token_checker` + le middleware `DashboardTrustMiddleware` (`trust.py:229`), donc non exposé hors LAN/token. Acceptable, à signaler.
- **Effort** : **S**

---

## 6. Inventaire du code mort

### 6.1 Mort confirmé (aucun appelant hors son propre fichier et hors tests)

| Élément | Fichier:ligne | Preuve grep | Verdict |
|---|---|---|---|
| `RepairOrchestrator`, `RepairRequest`, `RepairResult` | `rotation/application/repair.py:33,19,26` | `grep -rn "RepairOrchestrator"` → `tests/test_rotation_repair.py` seul | ❌ mort |
| `SweepCursor`, `SweepProgress` | `rotation/application/sweep.py:29,18` | `grep -rn "SweepCursor"` → `tests/test_rotation_sweep.py` seul | ❌ mort |
| `WarmPool`, `WarmPoolStatus` | `rotation/application/warm_pool.py:32,18` | `grep -rn "WarmPool"` → `tests/test_rotation_warm_pool.py` seul | ❌ mort |
| `LifecycleEngine`, `SlotObservation`, `SlotStatus` | `ops/lifecycle.py:54,69,116` | `grep` hors tests → `ops/lifecycle.py` seul | ❌ mort |
| `StartupPolicy`, `RepairPolicy`, `RotationBudget` | `ops/lifecycle_policy.py` | `grep` hors tests → `lifecycle_policy.py` seul | ❌ mort |
| `rotation/domain/`, `ports/`, `interfaces/` | 1 l. chacune (docstring) | — | ❌ coquilles vides |
| 15 routes `/api/vpn/station/*` | `station_control_routes.py:165-497` | §3.2 | ❌ mortes |
| `/api/vpn/jobs`, `/api/vpn/jobs/{id}`, `/api/vpn/jobs/fleet/{action}` | `vpn_jobs_routes.py:26,46,56` | §3.2 | ❌ mortes |
| `pause`/`resume` candidats | `qualification_routes.py:370,374` | §3.2 | ❌ mortes |
| Colonnes `hedged`, `winner_station` (lecture) | `db.py:284-285` | `grep` dans `dashboard/` → 0 | ⚠️ écrites, non lues |
| ~110 scripts jetables | `logs/*.py` | hors index, non référencés | ⚠️ à purger |

### 6.2 Shims encore vivants (ne pas supprimer sans mise à jour de test)

| Shim | Importé par | Blocage |
|---|---|---|
| `station_supervisor.py` | `opencode.py:2331` | runtime |
| `shared_rotation.py` | `opencode.py:3423`, `vpn/manager.py:42` | runtime |
| `latency_rotation.py` | `pool_routes.py:15`, `vpn_control_routes.py:17`, `vpn_status_routes.py:266`, `free/pool/__init__.py:692` | runtime |
| `protocol_mapping.py` | `opencode.py:5312`, `scripts/bench_perf.py:218`, `scripts/gen_golden_fixtures.py:26`, 7 tests | runtime + tests |
| `traffic_capture.py` | **aucun import runtime** — 3 imports, tous dans `tests/` (`test_traffic_capture.py:26-27`, `test_coverage_boost.py:15`) | ✅ supprimable (avec mise à jour des 2 tests) |
| `app/db/__init__.py` | `tests/test_phase0_contracts.py` | test de contrat |

### 6.3 Orphelins frontend

| Fichier | Preuve | Verdict |
|---|---|---|
| `dashboard-ui/src/lib/components/Banner.svelte` | `grep -rn "Banner" dashboard-ui/src/` (hors le fichier) → **0** | ❌ orphelin |
| `dashboard-ui/src/lib/components/Field.svelte` | `grep -rn "Field" dashboard-ui/src/` (hors le fichier) → **0** | ❌ orphelin |
| `static/` (racine, hors `dist/`) | `find static -maxdepth 1 -type f` → **aucun fichier** ; `static/index.html` inexistant | ✅ déjà nettoyé |

**XSS / injection** : aucun `{@html}`, `innerHTML`, `outerHTML`, `document.write`, `eval(` dans `dashboard-ui/src/` (grep exhaustif → 0). Toutes les requêtes SQL paramétrées ; les seules f-strings SQL sont des `PRAGMA` et des `ALTER TABLE` sur des valeurs issues de constantes du module (`observability/db.py:527-531,545,580`). **Aucune injection trouvée.**

**Secrets** : aucun secret en clair dans `dashboard-ui/src/` ni dans `static/dist/assets/*.js`. Les endpoints masquent systématiquement (`config_routes.py:111-113,137-144`, `vpn_state_routes.py:31-37`, `config_extra_routes.py:294-300`). Le `prompt()` de `api.ts:75` stocke le token en `localStorage` — acceptable pour un dashboard LAN.

---

## 7. État réel de l'observabilité vs principe XIII

Principe XIII : « *La base a une rétention, une purge, un checkpoint WAL, un backup `VACUUM INTO` vérifié et un `integrity_check` régulier.* »

### 7.1 `logs/requests.db` (base principale)

| Exigence | État | Preuve |
|---|---|---|
| Clés primaires / index | ✅ | `id TEXT PRIMARY KEY` (`db.py:234`) + **13 index** (`db.py:256-265`, `:608`, `:356`, `:665-667`) |
| Migrations versionnées | ✅ | `_REQUEST_COLUMN_MIGRATIONS` (25 entrées, `db.py:268-307`) + canary `LIMIT 1001` (`db.py:616`) |
| Rétention | ✅ | `cleanup_old_bodies` corps 7 j / lignes 30 j (`db.py:856-982`), appelée quotidiennement (`opencode.py:4000`) |
| Purge | ✅ | `weekly_maintain` 90 j (`db.py:1177-1204`, `WEEKLY_PURGE_DAYS=90`) planifiée hebdo (`opencode.py:4044-4046`) |
| Checkpoint WAL | ✅ | `wal_checkpoint(TRUNCATE)` (`db.py:985-986`) toutes les 3600 s (`opencode.py:3801-3802`) + `PASSIVE` après chaque batch de cleanup (`db.py:891`) |
| **Backup `VACUUM INTO` vérifié** | ⚠️ **partiel** | `VACUUM` simple dans `vacuum_if_needed` (`db.py:851`, non vérifié) ; l'archivage `archive_old_rows` (`db.py:1074-1174`) fait bien `INSERT` + `COUNT` avant `DELETE` (`db.py:1150-1156`) mais **ne produit pas de backup** — le commentaire l'assume : « *sans copie de sécurité (la purge hebdo supprime déjà sans backup — archiver avant purger est strictement plus sûr)* » (`db.py:1077-1081`). Le `VACUUM INTO` **vérifié** existe dans `scripts/archive_db.py:172-180` (`integrity_check` avant suppression) mais c'est un **outil manuel**, non planifié. |
| `integrity_check` régulier | ❌ **absent** pour `requests.db` | `grep -rn "integrity_check"` → `rotation/infrastructure/db/retention.py:47`, `scripts/archive_db.py:180`, `scripts/migrate_timestamps_utc.py:103` (tous hors `observability/db.py`). Aucun `PRAGMA integrity_check` périodique sur `requests.db`. |
| Croissance bornée | ✅ | `weekly_purge_days=90` + `archive_after_days=60` + alerte >1 Go (`opencode.py:4061`) |
| WAL borné | ✅ | `journal_size_limit=67108864` (`db.py:534`) |

**Verdict** : **excellent sur 6 critères sur 8**. Les deux manques sont le backup vérifié automatique et l'`integrity_check` régulier — tous deux **implémentés ailleurs dans le dépôt** (`retention.py:46-57`) mais pas câblés sur `requests.db`. C'est un raccordement, pas un développement.

### 7.2 `logs/registry.db`

| Exigence | État | Preuve |
|---|---|---|
| Rétention / purge | ✅ codé | `prune()` 5 tables, durées distinctes (`retention.py:18-39`) |
| Checkpoint WAL | ✅ codé | `checkpoint()` (`retention.py:42-43`) |
| Backup `VACUUM INTO` | ✅ codé | `backup()` (`retention.py:51-57`) |
| `integrity_check` | ✅ codé | `integrity_ok()` (`retention.py:46-48`) |
| **Planification** | ❌ **absente** | §P2-7 : `run_once` appelé uniquement par `scripts/registry_maintenance.py:30` (manuel) |

**Verdict** : code conforme, **exécution non planifiée**. 167 Mo sans purge automatique.

### 7.3 Tables et croissance

| Table | Colonnes | Index | Bornée par |
|---|---:|---:|---|
| `requests` | 18 (fast) → 40 (post-migration) | 13 | purge 90 j + archivage 60 j |
| `free_model_usage` | 10 | 3 (`db.py:665-667`) | `cleanup_old_bodies` 30 j (`db.py:914-924`) |
| `token_counters_daily` | 6 | 1 (`db.py:356`) | ⚠️ **aucune purge** — croissance = (modèles × jours), ~10 modèles × 365 j/an = négligeable |
| `meta` | 2 | PK | bornée |
| `sweep_cursor`, `observations`, `fallback_events`, `incidents`, `repair_jobs`, `config_snapshots` | — | — | `prune()` — **non planifié** |

---

## 8. Plan de découpage des modules > 1 500 lignes

Seul **`dashboard/api.py` (3 047 l.)** dépasse le seuil dans le périmètre (`opencode.py` à 33 618 l. est hors périmètre mais reste le monolithe de référence). L'extraction est déjà engagée (43 routeurs sortis) ; il reste à vider `api.py` de ses *helpers*.

| Lot | Contenu à extraire | Lignes estimées | Destination proposée |
|---|---|---:|---|
| 1 | Helpers SQL/pagination : `_build_where` (`:496`), `_date_bound_to_utc` (`:548`), `_normalize_date_bound` (`:555`), `daysAgo` (`:562`), `_period_minutes` (`:570`), `_period_rates` (`:592`) | ~110 | `dashboard/routers/query_filters.py` (déjà partiellement dans `read_models.py`) |
| 2 | Coûts & tarification : `_pricing_config` (`:666`), `_compute_costs` (`:674`), `_free_model_id_set` (`:619`) | ~120 | `dashboard/costs.py` |
| 3 | Persistance config : `_persist_vpn_config` (`:941`), `_persist_retry_until_success` (`:1286`), `_persist_free_model_map` (`:1360`), `_write_config_yaml_atomic` (`:1278`), `_config_transaction` (`:734`), `_write_credentials_env` (`:1394`) | ~280 | `dashboard/config_persist.py` |
| 4 | Backends proxy (socks5/https) : `_socks5_payload` (`:1482`), `_https_payload` (`:1771`), `_apply_socks5_rows` (`:1698`), `_apply_https_rows` (`:1809`), `_socks5_probe` (`:1905`), `_https_probe` (`:2007`), `_proxy_url` (`:1973`), `_stored_proxy_credentials` (`:1858`), `_*_fallback_state` (`:1844,1875,1889`) | ~500 | `dashboard/proxy_backends.py` |
| 5 | NordVPN (API externe) : `_nordvpn_api_fetch` (`:2057`), `_nordvpn_countries` (`:2103`), `_nordvpn_servers_by_country` (`:2124`) | ~120 | `dashboard/nordvpn_api.py` |
| 6 | Egress & capacité : `_egress_*` (`:1545-1667`), `_as_index` (`:1667`), `_egress_audit` (`:1677`) | ~180 | `dashboard/egress_views.py` |
| 7 | Diagnostics Docker : `_docker_diag` (`:2158`), `_redact_compose_text` (`:2197`), `_docker_compose_config` (`:2211`) | ~90 | `dashboard/docker_diag.py` |
| 8 | Caches & verrous : `_TTLCache` (`:79`), `_run_db_locked` (`:369`), `_db_query_sync` (`:375`), `_get_ro_conn` (`:406`), `_db_read_sync` (`:442`), `_get_local_ips` (`:466`) | ~200 | `dashboard/db_access.py` |
| 9 | Validation & auth : `_check_dashboard_token` (`:258`), `_merge_preserved_api_keys` (`:300`), `_validate_vpn_config_payload` (`:817`), `_quality_blocks_errors` (`:775`) | ~200 | `dashboard/validation.py` |
| 10 | `register_dashboard` (`:2240-3047`) — reste l'orchestrateur | ~800 | conservé |

**Résultat** : `api.py` ≈ 800-900 l. (orchestrateur seul), 9 modules de 90-500 l., tous sous le seuil. Ordre d'exécution : lots 1→9, un lot = un commit, `pytest -k "not docker"` vert entre chaque (règle ADR-006 §6).

**Autres candidats sous le seuil mais proches** : `observability/db.py` (1 248 l.) — l'extraction est déjà en cours de fait (`rotation/infrastructure/db/` reprend retention/backup/maintenance). `dashboard/quota.py` (1 105 l.) — mélange scraping HTML (`_read_object_literal`, `_js_string_to_json`, `_normalize_js_object`, `_parse_window`, `_extract_field`, `parse_quota_html` : `:536-697`) et logique de capacité (`MODEL_CAPABILITIES`, `_MODEL_LIMITS_FALLBACK`, `_model_tier_payload`). Coupure naturelle : `dashboard/quota_scrape.py` (~200 l.).

---

## 9. Zones non auditées / incertitudes

1. **`opencode.py` (33 618 l.)** — hors périmètre, lu uniquement pour établir l'absence de doublons d'endpoints et tracer les consommateurs runtime. Les 34 routes du monolithe n'ont pas été auditées individuellement.
2. **`free/`** (pool, rotation, egress, ip_registry) — touché seulement pour prouver les consommateurs de `rotation/` et `ops/`. Non audité en profondeur.
3. **`vpn/manager.py`** — lu sur les sites `subprocess` et `_docker_run` uniquement.
4. **Bases de données** — jamais ouvertes (règle projet). Les tailles viennent de `ls -la` ; les schémas du code de création. Aucune mesure de temps de requête réelle, aucun `EXPLAIN QUERY PLAN` exécuté.
5. **`P1-4` (web_search/web_fetch inertes)** — conclusion fondée sur un grep d'absence de consommateur. **Non confirmée par un test runtime.** Le consommateur peut exister sous une forme indirecte (ex. injection via `app.state`, lecture par un module non-`.py`, ou nom de clé différent). **À instruire avant toute suppression.**
6. **`P0-1` (fenêtre de perte de données)** — le raisonnement repose sur l'ordre de création des tâches (`opencode.py:4284` puis `:4289`) et sur l'absence de colonnes dans `init_requests_schema_fast`. Non reproduit en exécution : sur la base actuelle (déjà migrée), le bug est invisible. Reproductible sur base neuve : `rm logs/requests.db` puis boot + requête immédiate.
7. **`data-option-map` et le gate** — je n'ai pas exécuté `npm run build` ; l'analyse porte sur les sources `dashboard-ui/src/`, qui sont **plus récentes ou égales** au build committé (aucun fichier de `src/` n'est plus récent que `static/dist/index.html`, daté 2026-11-11 08:56). Le dist est donc à jour — point vérifié.
8. **`P2-5` (absence de timeout serveur)** — fondé sur la lecture du code ; je n'ai pas mesuré de dépassement réel du budget p95 < 2 s.
9. **`scripts/opencode_client_sync.py` (904 l.)** — lu superficiellement. Les 5 sites `subprocess.run` sans `creationflags` (`:88,251,427,728,876`) sont hors du périmètre du gate XVI (`tests/test_no_visible_spawn.py:18-24` ne liste que `opencode.py`, `scripts/fresh_chain*.py`, `free`, `vpn`, `dashboard`, `observability`). Le fichier est un outil opérateur (CLI), donc **probablement exempté** par la règle « scripts purement interactifs » (`constitution.md:180-182`) — mais il est aussi planifié via `schtasks` (`:728`), ce qui en ferait un processus non-interactif. **À trancher explicitement.**
10. **`gui/tray.py:164` et `gui/window.py:41`** — utilisent `kwargs["creationflags"]` (spread), donc invisibles pour le gate AST (`test_no_visible_spawn.py:68-72` teste `kw.arg in kwargs`). Le comportement est **correct** (flags posés sous `win32`) mais la détection statique est contournable par construction. `gui/` n'est de toute façon pas dans `RUNTIME_ROOTS`.
11. **Principe VIII — cohérence superviseur/reconciler** : `ops/reconciler.py` est *pur* (décide sans exécuter, `reconciler.py:1-6`) et appelé par `station_routes.py:132` ; `ops/supervisor.py` répare (restart/rotation) ; `observability/socket_guard.py` **ne redémarre plus** (sortie code 70, `socket_guard.py:72-91`) ; `scripts/app_supervisor.py` est le propriétaire du cycle de vie du **processus app** (`app_supervisor.py:249-265`). La séparation est **cohérente** sur ce que j'ai lu : un réparateur de processus (app_supervisor), un réparateur de stations (ops/supervisor), un décideur pur (reconciler). Le chevauchement résiduel est `rotation/application/repair.py` (mort, cf. P1-1). **Verdict : VIII respecté**, sous réserve que `ops/lifecycle.py` (P1-2) ne soit jamais branché comme second réparateur.
12. **Principe XVI — verdict** : `python scripts/platform_audit.py` → **0 violation sur 279 fichiers runtime**. Le gate officiel (`tests/test_no_visible_spawn.py`) passe. Tous les sites `subprocess` du périmètre runtime ont bien `creationflags=CREATE_NO_WINDOW` (`vpn/manager.py:218,269,282,354,381,432,5347,11216` ; `dashboard/qualification_launcher.py:44-55` ; `dashboard/routers/station_logs_routes.py:45` ; `scripts/app_supervisor.py:260-262`). **Conforme.** Les seuls sites sans policy sont dans `scripts/` (hors gate, CLI opérateur) — cf. incertitude 9.

---

## 10. Synthèse des compteurs

| Sévérité | Nombre |
|---|---:|
| P0 | 2 |
| P1 | 8 |
| P2 | 8 |
| P3 | 4 |
| **Total** | **22** |

| Thème | Findings |
|---|---|
| Constitution XII (options GUI) | P0-2, P1-4, P1-8 |
| Constitution XIII (données) | P2-7, §7 (backup vérifié + integrity_check manquants sur `requests.db`) |
| Constitution XIV (modularité) | P1-1, P1-2, P2-1 |
| Constitution XVII (perf) | P1-5, P1-6, P1-7, P2-5, P3-3 |
| Fiabilité | P0-1 |
| Code mort | P1-3, §6 |
| Constitution VIII (un seul réparateur) | ✅ respecté (§9.11) |
| Constitution XVI (fenêtres) | ✅ respecté (§9.12) |
| Injection / XSS / secrets | ✅ aucun trouvé (§6.3) |
| Doublons d'endpoints | ✅ aucun (§3.1) |
