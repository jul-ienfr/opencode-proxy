# Audit 09 — Configuration, scripts, dette documentée, hygiène du dépôt

Branche : `019-audit-optimisation-flotte-free`
Périmètre : `config/` (17 fichiers, 8 181 l.), `config.yaml` (1 645 l., 45 sections, 625 feuilles non dynamiques), `.env.example` vs `.env` (noms de clés uniquement), `scripts/` (104 `.py`), fichiers orphelins et backups de la racine, répertoires suspects, `docs/DEBT.md`, `CHANGELOG.md`, `README.md`, `AGENTS.md`, `CLAUDE.md`, les `PLAN_*.md`/`RAPPORT_*.md` racine, `.github/workflows/ci.yml`, les modules > 1 500 lignes de la zone.

Méthode : **lecture seule** (aucun fichier du dépôt modifié hors ce rapport). Preuves par `Grep` ciblé, extraction AST réelle de `scripts/config_audit.py` (import du module puis appel de `cles_du_fichier()` / `_litteraux_yaml_get()`), exécution des gates (`config_audit --strict`, `config_coverage`, `platform_audit --strict`, `module_size_audit`, `ci_contracts.py`, `debt_gate.py`). `logs/requests.db` et `logs/debug.log` **jamais lus**. Aucun secret divulgué : seuls les **noms** de clés sont cités.

---

## 1. Résumé exécutif

1. **Le gate « zéro option morte » est structurellement permissif.** `scripts/config_audit.py:266` déclare une clé « vivante » dès que son **nom de feuille** apparaît entre guillemets n'importe où dans le code. Résultat mesuré : **392 des 625 clés (62 %)** ne sont jamais lues par un `yaml_get` littéral et ne sont retenues que par ce filet. Le gate ne peut pas distinguer un vrai consommateur d'une coïncidence de nom.
2. **Le différentiel réel est toutefois propre** : après vérification AST des deux formes d'appel (`yaml_get("section","clé")` et `yaml_get("section.clé")`), **0 clé morte** n'est prouvable — y compris les 20 clés `ip_registry.*` que les rapports 00-V-27 et 04 déclarent mortes : leurs consommateurs **existent** (`scripts/fresh_chain*.py`, `rotation/infrastructure/config/schema.py`), mais **dans des modules eux-mêmes peu/pas branchés**. Le vrai défaut est donc « consommateur non atteignable en production », pas « clé sans consommateur ».
3. **Deux clés sont réellement mortes** et tolérées par la baseline : `thinking.effort_caps.ling-3.0-flash-fin` et `thinking.effort_caps.muse_spark` (`config.yaml:928,926`) — 0 occurrence dans les 901 fichiers Python de production. `config_coverage.py` les compte (2 MORTE / 600) et les acquitte via `scripts/config_coverage.baseline.json`.
4. **Six clés « legacy » sont auto-déclarées à retirer depuis le 2026-09-08 et sont toujours là** (`ip_rotation.dual_station`, `vpn_proxy_port_2`, `socks5_proxy_port_2`, `state_file_2`, `compose_service_2`, `credentials_file`, `config.yaml:135,720,660,664,118,126`). Elles figurent dans la liste **informative** du baseline : le gate ne les verra jamais. Le « plan de retrait avec ADR » annoncé n'a jamais eu lieu.
5. **La constitution nomme deux gates qui n'existent pas** : `option-map` comme gate exécutable (`constitution.md:219,230,237`) n'est qu'un fichier markdown `specs/013-egress-catalog/contracts/option-map.md` + `tests/test_option_map.py` dont la portée est `egress_catalog` **uniquement** (`tests/test_option_map.py:19-20`) — soit **109 clés sur 1 341** ; `scripts/option_map.py` **n'existe pas**. Et `platform_audit --strict` n'est câblé **nulle part** (0 occurrence dans `ci.yml`, `gate.ps1`, `quality_gate.py`) alors qu'il tourne proprement à la main (0 violation / 279 fichiers).
6. **16 fichiers orphelins sur 22 sont supprimables immédiatement**, dont 10 ignorés par git (donc sans `git rm`) : `ddgtest.py`, `wstest.py`, `test_free_quota.py` (racine), `=` (fichier vide de 0 octet), `full_sweep_2026-10-12.log`, `debug.log`, `can1..can5.json`. Les 12 `*_reverify.xml` + `full_sweep_2026-10-12.xml` (592 ko) sont **suivis** et ne servent que de preuve historique d'un plan de septembre.
7. **Les 4 shims racine sont vivants** (`vpn_manager.py`, `free_ip_pool.py`, `latency_rotation.py`, `shared_rotation.py`, `shared_state.py`, `protocol_mapping.py`) — ADR-006 §5, Phase 9. `free_discovery.py` (51 l.) et `traffic_capture.py` (26 l.) n'ont **aucun consommateur de production** : candidats à suppression immédiate.
8. **38 scripts sur 104 sont morts ou jetables** (6 throwaway gitignorés + 32 sans référence hors `scripts/`), et 14 paires sont des doublons ou des versions successives. Aucun n'est référencé par la CI, les runbooks ou le CHANGELOG.
9. **L'hygiène du dépôt est le point le plus dégradé** : 114 entrées `git status` (75 modifiés, 3 supprimés, 30 non suivis, 5 ajoutés), un dépôt git imbriqué `gluetun/.git` (31 Mo) masqué par `.git/info/exclude` (pas de `.gitmodules`), 2 worktrees d'audit **dans** le dépôt (13 Mo + 14 Mo) bien que `git worktree list` les liste, 27 backups racine (434 ko) tous gitignorés, et **toute la spec 023 non versionnée** (`free/classify_429.py`, `free/ip_state.py`, `free/wire_profile.py`, `free/clients/`, `specs/023-*`, `tests/unit/`, `tests/contract/`, `config/store.py`…) alors que ces modules sont **exécutés par le proxy en cours**.
10. **`docs/DEBT.md` n'est pas le registre que la constitution cite.** Le gate `scripts/debt_gate.py:17` lit `specs/002-vpn-catalog-rotation/debt.md` + `debt_status.json` (25 dettes, 4 P1/P2 ouvertes) ; `docs/DEBT.md` (état 2026-11-10) n'est lu par **aucun** outil et **n'est pas câblé en CI**. Deux registres de dette coexistent, et `docs/DEBT.md` ne liste ni les 4 P0 du rapport 06 (double import, `NameError`), ni le churn `tunnel_down` (26 102 incidents).

---

## 2. Différentiel complet config ↔ code

### 2.1 Méthode et compteurs

| Mesure | Valeur | Source |
|---|---:|---|
| Sections de premier niveau `config.yaml` | 45 | `grep -c '^[a-z_]*:' config.yaml` |
| Feuilles aplaties (hors sections dynamiques) | **625** | `config_audit.cles_du_fichier()` |
| Clés lues par un `yaml_get` **littéral** (AST) | **288** (233 avec point + 42 sections + 13 doublons) | `config_audit._litteraux_yaml_get()` |
| Clés retenues **uniquement** par l'heuristique « nom de feuille cité » | **392 / 625 = 62 %** | `config_audit.auditer()` + `:266` |
| Verdict `config_audit --strict` | **0 dead / 0 missing**, exit 0 | exécuté |
| Verdict `config_coverage.py` | 600 clés → **407 CONSOMMÉE, 191 PARTIELLE, 2 MORTE** | exécuté |
| `config_coverage.py --baseline` | exit 0 (les 2 MORTE sont dans `morte_connue`) | exécuté |
| Clés lues mais **absentes** de `config.yaml` | 0 (hors `_fail_closed`, lu par `.get()` sans littéral, et `ip_rotation.control_api_key`, volontairement scrubbé) | différentiel `IP_ROTATION.get()` |

**Le défaut de méthode, prouvé.** `scripts/config_audit.py:260-268` :

```python
feuille = cle.rsplit(".", 1)[-1]
if cle in lues_exactement:
    continue
if re.search(rf"[\"']{re.escape(feuille)}[\"']", source):
    continue
mortes.append(cle)
```

Conséquence mesurée : `retry_until_success.enabled` est « vivant » parce que la chaîne `'enabled'` apparaît **348 fois** ailleurs (348 hits `'"enabled"'` dans les 901 fichiers de production) ; `circuit_breaker.ttfb_watchdog.timeout_s` l'est grâce à `'timeout_s'` (16 hits, tous d'autres sections). **Un `config_audit --strict` vert ne prouve donc rien sur 62 % des clés.** Le seul garde-fou réel est `STRICT_SECTIONS = {"ip_registry"}` (`:79`), qui force la lecture explicite de la feuille pour cette section.

### 2.2 Verdict par section — les 45 sections

Colonne « lecture explicite » = nombre de clés de la section lues par un `yaml_get` littéral. « Heuristique » = nombre de clés retenues seulement par le filet du nom de feuille. Un `—` signifie « section entière lue en bloc » (légitime : `yaml_get("background", default={})`).

| Section | Feuilles | Lecture explicite | Heuristique seule | Consommateur principal (fichier:ligne) | Verdict |
|---|---:|---:|---:|---|---|
| `server` | 3 | 0 | 3 | `opencode.py` (section entière) | lecture en bloc — OK |
| `upstream` | 13 | 8 | 5 | `opencode.py:580`, `config/settings.py` | OK |
| `routing` | 6 | 3 | 3 | `config/settings.py:2402` (`load_routes`) | OK |
| `free_model_map` | 6 | — | — | **section dynamique** (données) | exclue par design |
| `dashboard_trust` | 2 | 0 | 2 | `trust.py:319` (via `_yaml_get`) | **heuristique** — voir P1-3 |
| `client_auth` | 2 | 0 | 2 | `trust.py:319` | **heuristique** — voir P1-3 |
| `supervisor` | 5 | 0 | 5 | `ops/supervisor.py:33` | **heuristique** |
| `stations` | 42 | 0 | 42 | `ops/supervisor.py`, `free/quality.py:99` | **heuristique intégrale** — voir P1-3 |
| `ip_rotation` | 168 | 0 | 211* | `vpn/manager.py`, `free/pool/__init__.py`, `opencode.py` | **0 lecture explicite** — voir P1-2 |
| `geo` | 6 | 3 | 3 | `config/geo.py:54` | OK |
| `custom_routes` | 11 | — | — | **section dynamique** | exclue par design |
| `api_keys` | 0 | 0 | 0 | `config/settings.py:2345` | OK |
| `pricing` | 3 | — | — | **section dynamique** | exclue par design |
| `web_search` | 7 | 7 | 0 | `opencode.py:19721-19735` | **pleinement explicite** ✅ |
| `web_search_native` | 3 | 0 | 3 | `config/settings.py` (`WEB_SEARCH_NATIVE_MODELS`) | OK (liste) |
| `web_fetch` | 7 | 7 | 0 | `opencode.py:19868-19908` | **pleinement explicite** ✅ |
| `cache` | 3 | 0 | 3 | `opencode.py` (section entière) | OK |
| `server_compaction` | 21 | 0 | 21 | `config/settings.py:627-690` (`get_server_compaction`) | lecture typée par clé — OK |
| `circuit_breaker` | 8 | 0 | 8 | `opencode.py` | **heuristique** |
| `rate_limit` | 3 | 0 | 3 | `server/throttle.py` | **heuristique** |
| `key_pause` | 4 | 0 | 4 | `core/keys.py:520` | **heuristique** |
| `database` | 8 | 0 | 8 | `observability/db.py` | **heuristique** |
| `background` | 8 | 0 | 8 | `opencode.py:4174` (section) | OK |
| `context_guard` | 1 | 0 | 1 | `opencode.py` | **heuristique** |
| `streaming` | 5 | 0 | 5 | `streaming/*.py` | **heuristique** |
| `debug` | 4 | 0 | 4 | `dashboard/display.py:87,92` | **heuristique** |
| `thinking` | 6 (+ maps) | 0 | 6 | `config/reasoning_options.py:60`, `config/effort_caps.py` | **heuristique** (maps = identifiants) |
| `cache_rewrite_models` | 6 | 0 | 6 | `opencode.py` (liste) | OK (liste) |
| `dashboard` | 1 | 0 | 1 | `dashboard/display.py` | **heuristique** |
| `free_discovery` | 5 + probe | 0 | 5 | `config/discovery.py:747`, `config/settings.py:1463` | lecture par sous-bloc — OK |
| `models_catalog` | 2 | 0 | 2 | `config/models_catalog.py:11` | **heuristique** |
| `live_models` | 4 | 0 | 4 | `config/live_models.py:60` | **heuristique** |
| `traffic` | 4 | 0 | 4 | `observability/capture.py` | **heuristique** |
| `protocol` | 1 | 0 | 1 | `app/protocol/mapping.py` | **heuristique** |
| `timeouts` | 1 | 0 | 1 | `opencode.py` | **heuristique** |
| `retry_until_success` | 6 | 0 | 6 | `opencode.py` | **heuristique** (cf. §2.1) |
| `free` | 8 + shims | 0 | 8 | `opencode.py:10918`, `config/free_shims.py` | **heuristique** |
| `canary` | 4 | 0 | 4 | `observability/canary.py` | **heuristique** |
| `free_prewarm` | 3 | 0 | 3 | `opencode.py` | **heuristique** |
| `suspect_tiny_output` | 2 | 0 | 2 | `opencode.py` | **heuristique** |
| `observability` | 6 | 0 | 6 | `observability/slo.py` | **heuristique** |
| `egress_catalog` | 81 | 0 | 81 | `free/egress_runtime.py`, `free/egress_adapters/*` | **0 lecture explicite** — couvert par `option-map` (109 clés) |
| `control_plane` | 11 | 0 | 11 | `free/control_plane/config.py:187` | **heuristique** |
| `ip_registry` | 62 | 20 | 42 | `free/ip_registry/config.py:33-139` | **STRICT_SECTIONS** — les 20 lues le sont explicitement |
| `models` | 34 | — | — | **section dynamique** | exclue par design |

\* `ip_rotation` compte 168 feuilles mais 211 entrées « heuristique » : l'écart vient des cartes par identifiant (`bad_ttl_by_cause`, `latency_rotation`, `socks5_proxies`, `identity_profiles`, `ip_check_urls`) comptées à part.

**Synthèse** : sur 45 sections, **3 seulement** (`web_search`, `web_fetch`, et `ip_registry` par exception) ont une lecture littérale complète. Toutes les autres reposent sur l'heuristique, sur une lecture de section entière, ou sur un lecteur typé (`get_server_compaction`, `free/ip_registry/config.py`) — ce dernier cas est **sain** mais invisible pour `config_audit`.

### 2.3 Les 2 clés réellement mortes

| Clé | Déclaration | Consommateur | Verdict |
|---|---|---|---|
| `thinking.effort_caps.ling-3.0-flash-fin` | `config.yaml:928` | **AUCUN** — 0 hit sur 901 fichiers de prod (`needle='0-flash-fin'` → 0) | **MORTE**, acquittée par baseline |
| `thinking.effort_caps.muse_spark` | `config.yaml:926` | **AUCUN** — 0 hit (`needle='muse_spark'` → 0) | **MORTE**, acquittée par baseline |

Note : `config.effort_caps` lit la table **en bloc** (`config/effort_caps.py`), donc une clé ajoutée devient active sans lecture explicite. La baseline les classe « données de la table » (`config_coverage.baseline.json:_morte_comment`) — défendable, mais alors **`muse-spark` (avec tiret, `config.yaml:925`) et `muse_spark` (avec underscore, `:926`) coexistent** : deux entrées pour un même modèle, une seule peut matcher.

### 2.4 Les 6 clés « legacy » auto-déclarées à retirer

| Clé | `config.yaml` | Lecture en prod | Verdict |
|---|---|---|---|
| `ip_rotation.dual_station` | `:135` | `config/loader.py:84` (`cfg.get("dual_station")`), `free/pool/__init__.py:7957` | **lue** (rétro-compat) — mais `resolved_station_count` privilégie `station_count` |
| `ip_rotation.vpn_proxy_port_2` | `:720` | **aucune** (0 hit hors `dashboard/api.py:1012` qui la relaie à l'UI) | **MORTE de fait** |
| `ip_rotation.socks5_proxy_port_2` | `:660` | **aucune** (idem `dashboard/api.py:1011`) | **MORTE de fait** |
| `ip_rotation.state_file_2` | `:664` | **aucune** (`dashboard/api.py:1015`) | **MORTE de fait** |
| `ip_rotation.compose_service_2` | `:118` | **aucune** (`dashboard/api.py:1014`) | **MORTE de fait** |
| `ip_rotation.credentials_file` | `:126` | `free/pool/__init__.py` via `dashboard/api.py:981` | partiellement lue |

Ces 6 clés sont dans `legacy_a_retirer` du baseline — liste **explicitement informative** (`"_legacy_comment": "Informative (non utilisée par le gate)"`). **Le gate ne les verra jamais échouer.** Cinq sont relayées par `dashboard/api.py:972-1015` vers un mapping GUI legacy : l'UI expose donc des réglages sans effet runtime, ce qui est précisément la violation XII que la constitution interdit.

### 2.5 Clés lues par le code mais absentes du fichier

| Clé | Où elle est lue | Statut |
|---|---|---|
| `ip_rotation.control_api_key` | `config/settings.py:126-132,533` | **volontaire** — scrubbé au chargement (`SECRET_KEYS` dans `config_audit.py:83`), vit dans `.env`/`credentials.env` |
| `ip_rotation._fail_closed` | `.get("_fail_closed")` | lecture défensive, pas un réglage |
| toutes les autres | — | **0 `missing` réel** — vérifié par AST et par `config_audit --json` |

### 2.6 `.env.example` vs `.env` — différentiel des noms de clés

**30 clés présentes dans `.env` et absentes de `.env.example`** (le template ne les documente pas) :

`CLIENT_AUTH_TOKEN`, `COMPOSE_PROJECT_NAME`, `OPENCODE_ALLOW_CONSOLE_CREDENTIAL`, `OPENCODE_CONSOLE_UA`, `OPENCODE_GO_AUTH_COOKIE`, `OPENCODE_GO_WORKSPACE_ID`, `OPENVPN_PROTOCOL`, `OPENVPN_PROTOCOL_STATION1..6`, `SERVER_COUNTRIES_STATION1..6`, `SERVER_HOSTNAMES_STATION1..6`, `VPN_TYPE_STATION1..7`.

**9 clés documentées dans `.env.example` et absentes de `.env`** :

`DASHBOARD_REQUIRE_TOKEN` (défaut fail-closed assumé — `dashboard/api.py:195`), `OPENCODE_CLIENT_NAME`, `OPENCODE_GO_USE_BALANCE`, `OPENCODE_HOST`, `OPENCODE_JA3`, `OPENCODE_OFFICIAL_UA`, `OPENCODE_OFFICIAL_UA_RESPONSES`, `OPENCODE_PROJECT`, `OPENCODE_TLS_IMPERSONATE`.

| Finding | Preuve | Verdict |
|---|---|---|
| `CLIENT_AUTH_TOKEN` lu par **aucun** code | `grep -rn "CLIENT_AUTH_TOKEN"` → **1 seul hit : la ligne `.env` elle-même** | **P2 — variable morte dans le fichier d'environnement** |
| `OPENCODE_CONSOLE_UA` : 1 seul hit | `grep` → 1 occurrence | à confirmer comme mort |
| `OPENVPN_PROTOCOL_STATION1..6` : 4 hits chacune | `docker-compose.yml` (interpolation) | légitimes |
| `VPN_TYPE_STATION7` : 4 hits | `docker-compose.yml`, `ops/env_store.py` | **vestige** — `station_count: 6` (`config.yaml:667`), le rapport 00 §7.1 note que les services `vpn-gluetun-7..10` sont toujours déclarés |
| `COMPOSE_PROJECT_NAME` | 6 hits (compose + scripts) | légitime |
| `.env.example:2-4` affirme « YAML = réglages, ENV = secrets seuls » | le même fichier définit `OPUS_MAP_MODEL`/`SONNET_MAP_MODEL`/`HAIKU_MAP_MODEL` (`:13-15`) et `OPENCODE_HOST`/`OPENCODE_PORT` (`:18-19`) | **auto-contradiction** (déjà relevée par l'auditeur docs) |

---

## 3. Verdict fichier par fichier — orphelins de la racine et répertoires suspects

### 3.1 Modules Python orphelins (16 fichiers)

| Fichier | LOC | Références trouvées (hors `Lib/`, `_research/`, `logs/`, `.audit-*`) | Verdict |
|---|---:|---|---|
| `shared_state.py` | 36 | **~40 sites** : `opencode.py` (30+), `dashboard/api.py` (11), `dashboard/routers/*` (5), `free/`, `gui/tray.py:291` | **GARDER** — seam anti-import-circulaire, très utilisé |
| `vpn_manager.py` | 118 | `opencode.py:2563,2648,3424,3625`, `config/settings.py`, `dashboard/api.py:1228`, `free/pool/__init__.py:3248`, ~12 tests | **GARDER** — shim ADR-006 §5, cible Phase 9 |
| `latency_rotation.py` | 28 | `opencode.py` ×5, `free/pool/__init__.py` ×5, `ops/supervisor.py:198,208`, `dashboard/routers/*` ×4 | **GARDER** — surface réellement appelée |
| `shared_rotation.py` | 28 | `opencode.py:3423`, `tests/test_shared_rotation.py:27`, `tests/test_rotation_n_stations.py:22` | **GARDER court terme** — cible Phase 9 |
| `free_ip_pool.py` | 25 | `opencode.py:3422`, `scripts/smoke_todo10.py:73`, **20 fichiers de tests** | **GARDER** — le lifespan doit migrer d'abord |
| `protocol_mapping.py` | 171 | Prod : `opencode.py:5312` **uniquement** ; `scripts/bench_perf.py:218`, `scripts/gen_golden_fixtures.py:26` ; **26 fichiers de tests** | **GARDER court terme**, cible : faire lire `app.protocol.mapping` par `_build_metrics_text` |
| `trust.py` | 376 | `opencode.py:5027` (`ClientAuthMiddleware`), `dashboard/api.py:266,2274`, `server/throttle.py:209`, 2 tests | **GARDER** — mais 376 l. ; candidat découpage |
| `docker_events.py` | 212 | `opencode.py:3756` (`DockerEventWatcher`), `tests/test_docker_events.py`, `tests/test_coverage_boost.py:9` | **GARDER** |
| `station_repair.py` | 463 | `free/pool/__init__.py:63` (`RepairPolicy`), `free/repair.py:11` (`import *`) | **GARDER** — consommé |
| `ip_latency.py` | 167 | `free/rotation.py:62` (`IpLatencyTracker`), `ops/supervisor.py:33`, 2 tests | **GARDER** — module pur, consommé |
| `station_supervisor.py` | 27 | `opencode.py:2331` (`sync_supervisors`), `tests/test_station_isolation.py:15`, `tests/test_plan30_optimisation.py:17` | **GARDER** |
| `free_discovery.py` | 51 | **`tests/test_coverage_boost.py:22` uniquement** — `grep -rn "free_discovery"` ne renvoie que des occurrences de `free_discovery_urls` (fonction de `config/discovery.py`, sans rapport) | **SUPPRIMER** (façade sans consommateur de prod) |
| `traffic_capture.py` | 26 | `tests/test_traffic_capture.py`, `tests/test_coverage_boost.py:15` — le canonique `observability.capture` est importé directement par `dashboard/api.py:41` | **SUPPRIMER** (shim non consommé en prod) |
| `ddgtest.py` | 19 | **AUCUNE** — gitignoré (`.gitignore:53`) | **SUPPRIMER** — scratch de debug |
| `wstest.py` | 7 | **AUCUNE** — gitignoré (`.gitignore:54`) | **SUPPRIMER** — scratch de debug |
| `test_free_quota.py` | 145 | **AUCUNE** — gitignoré (`.gitignore:65`) ; `testpaths = ["tests"]` (`pyproject.toml:53`) donc **jamais collecté** ; ce n'est pas dans `tests/` | **SUPPRIMER** ou déplacer dans `tests/` si le contenu a de la valeur |

### 3.2 Fichiers racine non-Python

| Fichier | Taille | Suivi ? | Verdict |
|---|---:|---|---|
| `=` | **0 octet** | non, gitignoré (`.gitignore:32`) | **SUPPRIMER** — artefact d'un `>` mal formé |
| `debug.log` | 306 o | non, gitignoré | **SUPPRIMER** |
| `full_sweep_2026-10-12.log` | 6,5 ko | non, gitignoré | **SUPPRIMER** |
| `full_sweep_2026-10-12.xml` | **407 ko** | **OUI** | **ARCHIVER puis `git rm`** — preuve d'un sweep du 2026-10-12, cité par `PLAN_ALIGNEMENT_CLIENT_OFFICIEL_2026-10-09.md:1348` |
| `c1_reverify2.xml` … `z_reverify.xml` (11) | 1,6–35 ko | **OUI** | **ARCHIVER puis `git rm`** — cités par `PLAN_ALIGNEMENT_CLIENT_OFFICIEL_2026-10-09.md:1282`. Preuves périmées d'un plan de septembre (audit 2026-10-09). |
| `can1.json` … `can5.json` | 89–98 o | non, gitignorés (`.gitignore:50`) | **SUPPRIMER** |
| `api_keys.json` | **2 octets (`{}`)** | non, gitignoré | **GARDER** — fichier d'état du dashboard (`config/settings.py:2345`, `save_api_keys`) |
| `custom_routes.json` | 968 o | **OUI** | **ARCHIVER puis `git rm`** — voir P2-4 : source de vérité déplacée dans `config.yaml:757` (11 clés vs 8), lu seulement en repli (`config/settings.py:2282`) |
| `gunicorn.conf.py` | 1,5 ko | OUI | **GARDER** — `ExecStart` de `opencode.service:15` + check CI. Noter : inutilisable en `__main__` |
| `Start.bat` | 604 o | OUI | **FUSIONNER** avec `lancer-proxy.bat` — cherche OpenVPN sur l'hôte, obsolète (`Dockerfile:5-6`) |
| `lancer-gui.bat`, `lancer-proxy.bat` | 1,2/1,6 ko | OUI | **GARDER** |
| `lancer-silent.vbs` | 874 o | OUI | **GARDER** — mais chemin `Python312\pythonw.exe` **en dur** (non portable) |
| `setup.bat`, `setup.ps1` | 1,3/3,7 ko | OUI | **FUSIONNER** — les deux installent/vérifient OpenVPN hôte, supprimé du projet |
| `opencode.service` | 955 o | OUI | **GARDER** — déploiement Linux documenté (`CLAUDE.md:41-81`) |
| `Dockerfile`, `.dockerignore`, `docker-compose.yml` | 5,5 ko / 373 o / — | OUI | **GARDER** — ⚠️ `Dockerfile:67` `COPY vpn/` contredit `.dockerignore:22` qui exclut `vpn/` : build probablement cassé |
| `geo_policies.example.yaml` | 1,5 ko | OUI | **GARDER** — le docstring dit lui-même que le loader lit `config.yaml:geo.policies` (vérifié `config/settings.py:705-707`) |
| `pyproject.toml` | 3,4 ko | OUI | **GARDER** |
| `requirements.txt`, `requirements-dev.txt` | — | OUI | **GARDER** |
| `.gitleaks.toml` | 770 o | OUI | **GARDER** — allowlist ciblée sur `config.yaml` (clé locale by-design) |
| `skills-lock.json` | 782 o | OUI | **SUPPRIMER** — lock d'outillage agent (3 skills `vercel-labs/*`, `mattpocock/*`), sans rapport avec le proxy. Non vérifiable (hash scheme inconnu) |
| `nginx/nginx.conf` | 3,2 ko | OUI | **SUPPRIMER ou DOCUMENTER** — `upstream opencode-proxy:4000` ; non référencé par `docker-compose.yml` ni par `README.md` |

### 3.3 Backups de la racine — verdict de conservation

| Motif | Nombre | Taille | Gitignoré ? | Verdict |
|---|---:|---:|---|---|
| `.env.bak-*` | **17** | ~40 ko | OUI (`.gitignore:3`) | **SUPPRIMER les 16 datés < 2026-11-11**, garder le plus récent |
| `config.yaml.bak-*` | **11** | ~250 ko | OUI (`.gitignore:31`) | **SUPPRIMER** — aucun code ne les lit |
| `config.yaml.v*.bak.*`, `config.yaml.w-all.bak.*` | **7** | ~110 ko | partiellement : **5 sont SUIVIS** | **`git rm` les 5 suivis** (`v1/v3/v4/v5/v6.bak.2026-10-*`) — l'un est cité par `PLAN_ALIGNEMENT...:1282` comme preuve de sha256 |
| `credentials.env.bak-*` | **10** | ~10 ko | OUI (`.gitignore:89`) | **SUPPRIMER les 9 anciens** — le seul producteur est `scripts/refresh_nord_credentials.py:81` |
| **Total** | **45** | **434 ko** | — | Réduire à **1 par motif** |

Aucun de ces backups n'est lu par du code : `grep -rn "\.env\.bak-\|config\.yaml\.bak-"` ne renvoie que des **créateurs** (`.artifacts/audit-2026-11-05/*.py`, `scripts/refresh_nord_credentials.py:81`), jamais de lecteur.

### 3.4 Répertoires suspects

| Répertoire | Taille | Statut | Verdict |
|---|---:|---|---|
| `.audit-wt/` | **13 Mo** | **worktree git enregistré** (`git worktree list` → `.audit-wt 110d587 [audit-fixes-2026-09-10]`), gitignoré | **SUPPRIMER** — branche du 2026-09-10, `git worktree remove` |
| `.audit-logs-wt/` | **14 Mo** | **worktree git enregistré** (`110d587 [audit-logs-fixes-2026-09-10]`), gitignoré | **SUPPRIMER** — idem |
| `gluetun/` | **31 Mo** | **dépôt git IMBRIQUÉ** (`.git`, remote `github.com/jul-ienfr/gluetun.git` + upstream `passteque/gluetun`), **0 fichier suivi par le parent**, **pas de `.gitmodules`**, masqué par `.git/info/exclude:62` (`/gluetun/`) | **DÉCIDER** : soit `git submodule add` formel, soit `git rm --cached` + documenter. En l'état, un `git clone` du dépôt **ne le récupère pas** et le fork Gluetun est un livrable non reproductible. C'est un **P1** (principe XIV/XVII : reproductibilité). |
| `.kilo/` | 3 684 fichiers | gitignoré (`.gitignore` + `.git/info/exclude`), `node_modules/` + `plans/` | **GARDER hors périmètre** — outillage agent |
| `.oc-client/` | vide | gitignoré | **SUPPRIMER** (répertoire vide) |
| `.opencode/` | 114 ko | partiellement gitignoré (`.opencode/.gitignore`, `node_modules/`) | **GARDER** — outillage agent |
| `_research/` | 275 `.py`, **1 seul fichier suivi** (`_research/ghq.ps1`) | gitignoré (`.gitignore:48`) | **GARDER hors périmètre** — matériel de recherche, mais **275 fichiers non versionnés** = dette de reproductibilité |
| `Lib/` | **2 827 `.py`** | gitignoré (`/Lib/`), **0 suivi** | **SUPPRIMER** — environnement Python égaré (dépendance de pywebview) |
| `logs/` | **15 Go**, **110 `.py`** | gitignoré | **PURGER** : 13 Go dans `logs/backups/` (188 fichiers), 110 scripts d'investigation one-shot (`check_*.py`, `diag_*.py`, `find_visible_spawn*.py`). ⚠️ `logs/find_visible_spawn.py` **réimplémente** le gate XVI alors que le gate officiel est `tests/test_no_visible_spawn.py` |
| `.artifacts/` | **42 Mo**, 1 141 fichiers | gitignoré | **GARDER hors périmètre** |
| `.npm-cache/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, `__pycache__/`, `scripts/__pycache__/` (118 `.pyc`) | — | gitignorés | **PURGER** (aucun effet fonctionnel) |
| `.env.lock`, `config.yaml.lock` | **0 octet** | **NON gitignorés** → polluent `git status` (2 des 30 `??`) | **AJOUTER au `.gitignore`** — verrous persistants by-design (`config/settings.py:255` interdit de les supprimer) |

---

## 4. Findings

### P0

**P0-1 — Toute la spec 023 tourne en production sans être versionnée**
- **Fichiers** : `free/classify_429.py`, `free/ip_state.py`, `free/egress_ip.py`, `free/probe_budget.py`, `free/provider_capabilities.py`, `free/rotation_policy.py`, `free/wire_admission.py`, `free/wire_profile.py`, `free/clients/`, `config/store.py`, `specs/023-client-usable-egress/`, `tests/unit/` (9 fichiers), `tests/contract/` (2), `docs/CLIENTS.md`, `free/captures/2.3.0.json`
- **Preuve d'exécution** : `opencode.py:16021` (`from free import classify_429`), `:16022` (`from free import ip_state`), `:8265` (`from free import wire_profile`), `free/ip_registry/adapters/probe.py:110,111,112,180,181,182,254,255` ; `scripts/fresh_chain_supervisor.py:405` (`from free.egress_ip import is_global_ip`), `:45` (`from free.rotation_policy import RotationGuard`)
- **Preuve de non-versionnement** : `git status --porcelain` → `?? free/classify_429.py`, `?? free/ip_state.py`, `?? free/wire_profile.py`, `?? free/clients/`, `?? specs/023-client-usable-egress/`, `?? tests/unit/`, `?? tests/contract/`, `?? config/store.py`
- **Impact** : un `git clean -fd` ou un `git stash` **efface des modules que le proxy en cours d'exécution importe**. Aucune revue n'a eu lieu sur `wire_profile`/`ip_state` alors que `specs/023-client-usable-egress/tasks.md:30` reconnaît lui-même que les couches « budget de sondes et rotation sont testés mais pas encore branchés ». Aggravant : `tests/unit/` (11 fichiers) et `tests/contract/` (2) sont **non suivis**, donc `pytest -m "not docker"` en CI **ne les collecte pas** (`testpaths = ["tests"]` collecte bien `tests/unit` **si le dossier existe dans le checkout** — or il n'y est pas).
- **Recommandation** : `git add` immédiat du socle 023 + des 13 fichiers de tests, dans un commit dédié référençant `specs/023-*`. Effort **S**.
- **Constitution** : XIV (strangler : nouveau chemin à côté de l'ancien, **shadow, canary, puis décommissionnement** — un chemin non versionné ne peut être ni revu ni décommissionné), XVII (chaque exigence a un test **exécuté**).

**P0-2 — `platform_audit --strict` et `option-map` : deux gates nommés par la constitution ne sont câblés nulle part**
- **Fichiers** : `.specify/memory/constitution.md:328-330` (« Gates obligatoires avant merge : `pytest -k "not docker"`, `config_audit --strict`, **`platform_audit --strict`**, **`test_option_map.py`**, `npm run check` (si UI touchée), et la **matrice CI Windows/Linux** »)
- **Preuve `platform_audit`** : `grep -rn "platform_audit" .github/workflows/ci.yml scripts/gate.ps1 scripts/quality_gate.py` → **0 résultat**. Le script existe (242 l.), tourne proprement (0 violation / 279 fichiers, exécuté), mais n'est atteint qu'indirectement via `tests/test_platform_audit.py`.
- **Preuve `option-map`** : `scripts/option_map.py` **n'existe pas** (`ls` → No such file) ; seul `tests/test_option_map.py` existe, avec `SCOPED_SECTIONS = ("egress_catalog",)` et `SCOPED_IP_ROTATION = ("public_proxies", "egress_adaptive")` (`tests/test_option_map.py:19-20`) — soit **109 clés sur 1 341** (le gate passe : 4 tests). Le fichier `specs/013-egress-catalog/contracts/option-map.md` est la référence, mais **aucun script ne vérifie que 0 ligne manque** au sens de la constitution.
- **Preuve `npm run check`** : `grep -c "npm" .github/workflows/ci.yml` → **0**.
- **Impact** : le principe XV (aucune fenêtre intempestive, chemins non portables) et le principe XII (aucune option inerte) sont **déclarés** opposables mais **non gardés automatiquement** en CI.
- **Recommandation** : ajouter 2 pas bloquants à `ci.yml` (`python scripts/platform_audit.py --strict`, `npx --prefix dashboard-ui npm run check`), et étendre `SCOPED_SECTIONS` par lots. Effort **S** (platform_audit) / **L** (option-map complet).
- **Constitution** : XII, XV, XVII.

### P1

**P1-1 — `config_audit` est permissif sur 62 % des clés : le gate « zéro option morte » ne prouve presque rien**
- **Fichier** : `scripts/config_audit.py:260-268`
- **Preuve** : sur 625 clés aplaties, **288** sont lues par un `yaml_get` littéral et **392 (62 %)** ne sont retenues que par `re.search(rf"[\"']{feuille}[\"']", source)`. Exemples de faux positifs structurels : `retry_until_success.enabled` (348 hits de `'enabled'` ailleurs), `circuit_breaker.ttfb_watchdog.timeout_s` (16 hits de `'timeout_s'`).
- **Impact** : une faute de frappe sur 62 % des clés passe le gate (le nom de feuille existe ailleurs) ; une option supprimée du code reste « vivante » indéfiniment. Le rapport 00-V-27 a d'ailleurs conclu à tort « zéro consommateur » pour `ip_registry.selection.*` en cherchant dans `opencode.py dashboard/ free/` **sans** `rotation/` — le gate fait la même erreur par excès inverse.
- **Recommandation** : remplacer l'heuristique par l'obligation de lire la feuille explicitement, section par section, en commençant par `dashboard_trust`, `client_auth`, `supervisor`, `stations`, `retry_until_success` (5 sections = 56 clés). Effort **M**.
- **Constitution** : XII, XVII.

**P1-2 — `ip_rotation` (168 feuilles) n'a **aucune** lecture explicite : les 211 clés de la section la plus critique sont gardées par heuristique**
- **Fichier** : `config.yaml:92-739` ; consommateurs réels : `vpn/manager.py` (4572, 1244, 4533), `free/pool/__init__.py`, `opencode.py`
- **Preuve** : `config_audit._litteraux_yaml_get()` → **0 clé `ip_rotation.*`** dans l'ensemble lu (les 246 clés à point appartiennent à `ip_registry`, `egress_catalog`, `thinking`, `geo`, `web_*`…). Les 211 entrées de la section sont retenues par nom de feuille (`watchdog_interval` → 9 hits, `switch_delay` → 7 hits, `dual_station` → 11 hits).
- **Impact** : c'est la section qui porte la flotte (rotation, fallback, repair, canary, quotas). Un `ip_rotation.watchdog_intervall` (typo) serait accepté en silence.
- **Recommandation** : `STRICT_SECTIONS |= {"ip_rotation"}` puis corriger les remontées — le `--strict` du baseline les acquittera d'abord. Effort **M**.
- **Constitution** : XII, XVII.

**P1-3 — `config.yaml` versionné contient 12 lignes WARP IPv4 `enabled: false` que le runtime réécrit toutes les 300 s**
- **Fichiers** : `config.yaml:313,341,369,425,453,481` (v4 désactivées) ; `free/pool/__init__.py:4968-4995` (`endpoint.enabled = want` + `yaml_set(...)`)
- **Preuve** : confirmé par `docs/audits/2026-11-20-audit-complet/00-verifications-croisees.md` §V-5 (le runtime **écrase** puis **persiste**)
- **Impact** : conflit d'autorité config ↔ runtime. L'opérateur ne peut pas désactiver durablement une option ; `git status` montre `config.yaml` modifié en permanence (126 insertions / 74 suppressions vs HEAD) — les commits `chore(config): record current WARP egress leases` (4 dans les 25 derniers) sont le symptôme.
- **Recommandation** : sortir les `egress_ip`/`enabled` **dérivés** du fichier versionné vers un état runtime (`logs/`), et laisser `config.yaml` porter l'intention. Effort **M**.
- **Constitution** : III (une seule source de vérité), XII (« le GUI affiche la valeur effective »).

**P1-4 — `docs/DEBT.md` n'est pas le registre de dette que la constitution cite, et n'est câblé nulle part**
- **Fichiers** : `.specify/memory/constitution.md:137-139` (« La dette est suivie dans `debt.md` ») ; `scripts/debt_gate.py:17` → `specs/002-vpn-catalog-rotation/debt.md` + `debt_status.json` ; `docs/DEBT.md` (état 2026-11-10)
- **Preuve** : `grep -rn "docs/DEBT.md"` → **aucun outil** (seuls des `.md` de specs le citent) ; `grep -n "debt_gate"` dans `ci.yml`/`gate.ps1`/`quality_gate.py` → **0** ; `python scripts/debt_gate.py --strict` → « 25 debts, 0 P0 open » (exit 0) mais **le gate n'est pas en CI**.
- **Impact** : deux registres coexistent (25 dettes dans `specs/002`, 12 dans `docs/DEBT.md`) ; `docs/DEBT.md` **ne liste pas** les 4 P0 du rapport 06 (double import `opencode.py`, `NameError` sur `_resp_json_hors_boucle`, `UnboundLocalError` sur `/v1/responses` streaming, fenêtre de perte de données SQLite du rapport 08) ni le churn `tunnel_down` (26 102 incidents, rapport 00 §4.5). Un nouveau P0 peut donc apparaître **sans dette listée**, ce que XIV interdit.
- **Recommandation** : un seul registre (`specs/002-.../debt.md` est déjà outillé), fusionner `docs/DEBT.md`, et ajouter `python scripts/debt_gate.py --strict` à `ci.yml`. Effort **S**.
- **Constitution** : XIV, XVII.

**P1-5 — `gluetun/.git` : dépôt imbriqué de 31 Mo, non déclaré, non reproductible**
- **Fichier** : `gluetun/.git` (remote `github.com/jul-ienfr/gluetun.git`, upstream `passteque/gluetun`, HEAD `59b722d3`)
- **Preuve** : `find . -name '.git' -maxdepth 4` → `./gluetun/.git` ; `git ls-files gluetun | wc -l` → **0** ; `ls .gitmodules` → **absent** ; `git check-ignore -v gluetun` → `.git/info/exclude:62:/gluetun/`
- **Impact** : le fork Gluetun (dont dépendent `control_plane.base_url`, `Dockerfile` gluetun, `docker-compose.yml`) **n'est pas dans le dépôt** : un `git clone` ne le récupère pas, et un `git clean -xfd` le détruit. Le masquage par `.git/info/exclude` (fichier **local, non versionné**) le rend invisible à tout autre poste.
- **Recommandation** : décider explicitement — soit `git submodule add` avec `.gitmodules` versionné, soit documenter le fork comme prérequis hors-dépôt. Effort **S**.
- **Constitution** : XV (multiplateforme par construction — un prérequis non déclaré casse la reproductibilité), XVII (fiabilité : « un redémarrage de l'OS restitue l'état persisté »).

**P1-6 — 38 scripts sur 104 sont morts ou jetables ; 14 paires sont des doublons**
- **Répertoire** : `scripts/` (104 `.py`, 122 entrées)
- **Morts (aucune référence hors `scripts/`)** : `_canary_n1.py`, `_canary_n1b.py`, `_hist.py`, `_last_rows.py`, `debug_dash.py`, `diag_curl_cffi.py`, `quota_replay.py`, `test_mimo_v25.py`, `test_selection_cycle.py`, `test_session_id.py`, `test_socks_pool.py`, `classify_nordvpn.py`, `classify_observed.py`, `migrate_quota_ledger.py`, `migrate_state_json.py`, `recover_hostnames.py`, `probe_catalog.py`, `retest_known_bad.py`, `verify_100.py`, `db_audit.py`, `log_audit.py`, `audit_shim_surface.py`, `smoke_todo41.py`, `bench_backends.py`, `fetch_nordlynx_key.sh`, `build_nuitka.ps1`, `build_nuitka.sh`, `clean-vscode-server.sh`, `install-vscode-cleanup.sh`, `quota_watch.bat`, `quota_reset_capture.bat`, `quota_reset_capture.py`, `configure_claudecode_proxy.ps1`, `station_snapshot.py`
- **Preuve** : `rg -l '\b<NAME>\b' . -g '!scripts/**' -g '!Lib/**' -g '!_research/**' -g '!logs/**' -g '!.git/**' -g '!module-baseline.json'` → vide pour chacun. Les 4 `_*.py` sont déjà gitignorés par `scripts/_*.py` (`.gitignore:115`).
- **⚠️ Piège méthodologique** : les 34 morts **apparaissent tous** dans `specs/014-nordvpn-exhaustive-rotation/module-baseline.json` — un **recensement de taille** généré par `module_size_audit.py --write-baseline`, **pas** une référence d'usage. Ne pas s'y fier.
- **`verify_100.py` est en plus FAUX** : `:64` affirme `station_count == 4` alors que `config.yaml:667` = 6.
- **Doublons (survivant recommandé)** : `test_socks_pool.py` → `test_proxy_pool.py` ; `verify_100.py` → `verify_free_leg.py` ; `test_mimo_v25.py` → `vpn_e2e_smoke.py` ; `smoke_todo41.py` → `smoke_todo10.py` ; `recover_hostnames.py` → `backfill_ip_hostnames.py` ; `migrate_quota_ledger.py`+`migrate_state_json.py` → fusion ; `measure_nordvpn_wg_sessions.py` → `measure_nordvpn_sessions.py` ; `bench_pool.py` → `bench.py` ; `station_snapshot.py` → `station_audit.py` ; `station_soak.py` → `station_slo_watch.py` ; `quota_replay.py`+`quota_reset_capture.py` → aucun ; `clean-vscode-server.sh`+`install-vscode-cleanup.sh` → aucun ; `build_nuitka.*` → aucun.
- **À NE PAS toucher** : `rotate_db.ps1`/`rotate_db.sh` = paire multiplateforme **documentée** (`docs/tuning.md:110`).
- **Impact** : 104 scripts sans marquage vivant/mort ; la moitié du répertoire est du bruit qui masque les 7 scripts réellement appelés par la CI.
- **Recommandation** : supprimer les 34 morts + 6 jetables, créer `scripts/archive/` (déjà existant, 1 fichier) pour les 8 à valeur historique. Effort **S**.
- **Constitution** : XVII (maintenabilité).

### P2

**P2-1 — `docs/` : 13 `PLAN_*`/`RAPPORT_*` à la racine (528 ko) + 75 markdown dans `docs/` = documentation historique non indexée**
- **Preuve** : `ls *.md` → 18 fichiers ; `ls docs/*.md` → 75 ; `git ls-files '*.md'` → 314.
- Les 14 `PLAN_*.md`/`RAPPORT_*.md` racine sont **tous suivis**. `PLAN_AUDIT_CONVERSIONS_2026-09-10.md` (146 ko) et `RAPPORT_AUDIT_CONVERSIONS_2026-09-10.md` (86 ko) sont d'anciens audits dont les findings sont largement périmés.
- **Recommandation** : `git mv` vers `docs/archive/plans-2026-09/`. Effort **S**.

**P2-2 — `skills-lock.json` et `nginx/nginx.conf` : 2 fichiers suivis sans rapport avec le proxy**
- `skills-lock.json` (782 o) : lock de 3 skills d'agent (`vercel-labs/agent-browser`, `vercel-labs/skills`, `mattpocock/skills`), aucun consommateur.
- `nginx/nginx.conf` (3,2 ko) : `upstream opencode-proxy:4000`, non référencé par `docker-compose.yml` ni `README.md`.
- **Recommandation** : `git rm` le premier ; documenter ou `git rm` le second. Effort **S**.

**P2-3 — `.env.lock` et `config.yaml.lock` (0 octet) ne sont pas gitignorés**
- **Preuve** : `git status --porcelain` → `?? .env.lock`, `?? config.yaml.lock` ; `git check-ignore` → non ignorés.
- Ce sont des verrous **persistants by-design** (`config/settings.py:255` : « Ne JAMAIS supprimer config.yaml.lock »).
- **Recommandation** : ajouter `*.lock` (ou les 2 noms) au `.gitignore`. Effort **S**.

**P2-4 — `custom_routes.json` : 2ᵉ source de vérité, divergente**
- **Fichier** : `custom_routes.json` (968 o, **suivi**)
- **Preuve** : `config.yaml:757-803` porte **11** routes, `custom_routes.json` en porte **8** ; `mimov25.model` = `mimo-v2.6-flash` (YAML) vs `muse-spark-1.2-contributor` (JSON). Le code ne lit le JSON qu'en **repli** si `config.yaml:custom_routes` est vide (`config/settings.py:2282-2292`) — il ne l'est pas.
- **Recommandation** : `git rm` ou renommer en `.example`. Effort **S**.
- **Constitution** : III (une seule source de vérité).

**P2-5 — 2 clés `effort_caps` mortes + 1 doublon `muse_spark`/`muse-spark`**
- `config.yaml:926` `muse_spark: max` (0 hit) vs `:925` `muse-spark: max` (lu) — la même intention écrite 2 fois, une seule active.
- `config.yaml:928` `ling-3.0-flash-fin: max` (0 hit).
- **Recommandation** : supprimer les 2 mortes et le doublon. Effort **S**.

**P2-6 — 5 clés `*_2` exposées dans le GUI sans effet runtime**
- `dashboard/api.py:972-1015` mappe `dual_station`, `credentials_file`, `socks5_proxy_port_2`, `vpn_proxy_port_2`, `compose_service_2`, `state_file_2` vers l'UI ; 5 d'entre elles n'ont **aucun** consommateur runtime.
- **Recommandation** : soit retirer du mapping GUI, soit marquer « affichage seul » dans l'option-map. Effort **S**.
- **Constitution** : XII.

**P2-7 — `VPN_TYPE_STATION7` + services `vpn-gluetun-7..10` : vestiges d'une flotte à 10**
- **Preuve** : `.env` porte `VPN_TYPE_STATION7` (4 hits compose/env_store) ; `config.yaml:667` `station_count: 6` ; le rapport 00 §7.1 confirme les services `vpn-gluetun-7..10` toujours déclarés dans `docker-compose.yml`.
- **Recommandation** : purger `.env`, `docker-compose.yml` et `ops/env_store.py`. Effort **S**.

### P3

**P3-1 — `config/` : 17 fichiers, 8 181 lignes, un seul au-dessus du seuil constitutionnel**
- `config/settings.py` = **2 782 l.** (> 1 500, XIV). Les autres sont sains : `live_models.py` 1 157, `discovery.py` 1 022, `models_catalog.py` 549, `model_tier.py` 359, `effort_policy.py` 347, `geo.py` 300, `reasoning_options.py` 249, `store.py` 213, `limits.py` 213, `free_gate.py` 198, `loader.py` 187, `effort_caps.py` 168, `free_shims.py` 159, `free_policy.py` 116, `__init__.py` 113, `npm_routing.py` 49.
- **Ratchet** : `config/settings.py` a **grossi** de 2 753 → 2 782 depuis la baseline (`module_size_audit --ratchet` → `{'file': 'config/settings.py', 'lines': 2782, 'baseline': 2753, 'reason': 'grew'}`), soit une violation de la règle « les monolithes existants sont extraits par strangler, **pas agrandis** » (XIV).
- **Recommandation** : voir §7.

**P3-2 — 30 scripts en doublon fonctionnel déjà couverts par un module de production**
- `scripts/station_snapshot.py` **collisionne de nom** avec `free/qualification/apply.py:station_snapshot` (fonction vivante) — risque de confusion en lecture.
- **Recommandation** : renommer ou supprimer. Effort **S**.

**P3-3 — `config.yaml` porte des adresses IPv6 WARP et des `egress_ip` qui sont de l'état, pas du réglage**
- `config.yaml:330,358,386,414,442,470,498,508,527,546,596,624,652` — 13 `egress_ip` IPv6 + 12 `egress_ip` IPv4.
- Les commits récents (`chore(config): record current WARP egress leases`, ×4 dans les 25 derniers) montrent que ces valeurs sont réécrites en boucle.
- **Recommandation** : sortir vers un état runtime. Effort **M**. (Constitution III.)

---

## 5. État de `docs/DEBT.md` — dette par dette

`docs/DEBT.md` (état **2026-11-10**) — 12 entrées. Vérification point par point contre le code au 2026-11-20.

| ID | Dette déclarée | Preuve de l'état actuel | Verdict |
|---|---|---|---|
| — | « **P0 : aucun.** Les gates de release passent » | `config_audit --strict` exit 0 ✅ ; `platform_audit --strict` 0 violation ✅ ; `ci_contracts.py` 522 tests / 0 échec ✅ ; `debt_gate.py` 25 dettes / 0 P0 ✅ — **mais** le rapport 06 documente 4 P0 (double import `opencode.py`, `NameError`, `UnboundLocalError`, fenêtre SQLite) **non listés ici** | **FAUX par omission** — voir P1-4 |
| D-014-01 | Canary de qualification rouge sur cutover/transports | `logs/canary-014.json` : `passed: false`, `generated_at: 2026-11-10T22:12`, checks `cutover-committed`, `transports-exercised`, `6-stations-usable`, `slo-six-of-six`, `watchdog-no-open-incidents` présents | **OUVERTE** (preuve datée du 2026-11-10, aucune nouvelle exécution) |
| D-014-02 | Soak 24 h en cours (PID 33280) | `tasklist` → **PID 33280 absent**. Aucun rapport de soak trouvé. | **OUVERTE / ABANDONNÉE** — le process est mort, aucun rapport livré |
| D-014-04 | Slices S1–S8, S11–S13 non extraites | `free/pool/` contient `selection.py`, `quota.py`, `rotation.py`, `public_pool.py`, `accounting.py`, `config.py` + `__init__.py` (**8 362 l.**) → **S6/S7 extraits** ; `opencode.py` = **33 618 l.** (ratchet : 31 861 → 33 618, **+1 757**) | **PARTIELLEMENT RÉSOLUE, RÉGRESSÉE** — les handlers HTTP n'ont pas bougé (rapport 06 §6) et le monolithe a **grossi** |
| D-014-07 | IP d'egress épuisées avant probe ; hostname legacy re-lié | Rapport 00 §4.1 : `claims` → `no_egress` **94,4 %** (9 244 / 9 788), `ok` **4 (0,04 %)** — **aggravé** vs 2026-11-14 | **OUVERTE, AGGRAVÉE** |
| D-014-09 | Boucle de contrôle quota ; P0 appliqué 2026-11-10, reste à valider 6/6 en continu | Rapport 00 §3 : **4/6 stations en `vpn=error`**, `healthy: 1` | **OUVERTE** — la validation 6/6 continu n'a pas eu lieu |
| D-014-10 | `qualification_tick` synchrone → **résolu** (`daemon_max_hosts: 1`) | `config.yaml:1196` `daemon_max_hosts: **40**` — **la valeur a été remise à 40** | **RÉGRESSÉE** — le correctif est annulé en config (relevé par `specs/015-egress-uptime-slo/audit.md:127` C-12) |
| D-014-11 | Binds simultanés → `AUTH_FAILED` en cascade → **résolu** (`rebind_limit_per_min=3`) | `config.yaml:1295` `rebind_limit_per_min: **8**` ; rapport 00 §4.5 : `auth_failed` **3 137** incidents | **RÉGRESSÉE** — la limite est remontée de 3 à 8 |
| D-014-12 | Capacité free structurelle : 429 après 20–40 req/IP ; « implémenter T076 (standby) ou ajouter une source d'egress » | `free/qualification/apply.py:27` porte `standby_size: int = 1` ; `config.yaml:1223` `station_standby_size: 1` → **partiellement implémenté**. Rapport 00 §4.2 : `quota_buckets` **88,9 % en 429** | **OUVERTE** — l'infrastructure existe, l'effet n'est pas mesuré |
| D-014-06 (P2) | Job CI Linux officiel jamais exécuté sur le runner GitHub | `.github/workflows/ci.yml:5` `branches: [main, one, one-nvpn-align]` — la branche courante `019-*` **n'est pas déclenchée par push** (seulement par PR). Aucun run archivé trouvé. | **OUVERTE** |

### Le second registre — `specs/002-vpn-catalog-rotation/`

`scripts/debt_gate.py` (outillé, mais **pas en CI**) lit `specs/002-.../debt.md` + `debt_status.json` : **25 dettes, 4 ouvertes** (`D-02`, `D-04`, `D-16`, `D-20`).

| ID | Dette | Preuve actuelle | Verdict |
|---|---|---|---|
| D-02 | Monolithes | `opencode.py` 33 618 l. (ratchet : +1 757), `vpn/manager.py` 11 471 l. (+86), `free/pool/__init__.py` 8 362 l. (+313) | **OUVERTE, AGGRAVÉE** |
| D-04 | Options GUI non prouvées | `test_option_map.py` couvre 109 / 1 341 clés (8 %) ; `platform_audit` non câblé | **OUVERTE** |
| D-16 | Exceptions silencieuses | Rapport 06 §5 : **367 / 1 225 `except: pass` nus (30 %)** | **OUVERTE, MESURÉE** |
| D-20 | Dashboard lourd | `dashboard/api.py` 3 047 l. | **OUVERTE** |

**Conclusion sur la dette** : les deux registres sont **incomplets et en retard**. `docs/DEBT.md` déclare 3 dettes « résolues » (D-014-10, D-014-11, et implicitement D-014-09) qui sont **régrédiées en configuration** (`daemon_max_hosts: 40`, `rebind_limit_per_min: 8`). C'est le finding le plus actionnable de cette section : **une dette ne doit pas être fermée sur un correctif qui vit dans `config.yaml`, un fichier réécrit par le runtime** (cf. P1-3).

---

## 6. Écarts doc ↔ réalité

### 6.1 `CLAUDE.md` (5 929 o) — écarts vérifiés

| Affirmation | Réalité | Verdict |
|---|---|---|
| `:89` « `config/__init__.py` — Exports: … `WEB_PORT` … » | `WEB_PORT` **n'existe plus** : `config/settings.py:825` « WEB_PORT supprimé » ; `config/__init__.py` n'exporte que `HOST`/`PORT` | **FAUX** |
| `:16` `--no-gui` « used by systemd/Docker » | Docker ✅ (`Dockerfile:91`). systemd ❌ — `opencode.service:15` lance `gunicorn -c gunicorn.conf.py opencode:app` | **FAUX (moitié systemd)** |
| `:41-81` section « Deployment (Ubuntu Server) » avec systemd + Docker | `:137-142` du **même fichier** : « Pas de serveur distant, pas de SSH, pas de systemd/Docker » | **CONTRADICTION INTERNE** |
| `:134` « `logs/requests.db` (3 Go) » | 167 Mo (mesuré : 166 977 536 octets) | **PÉRIMÉ (×18)** |
| `:117` « Static files in `static/` directory » | `static/dist/` existe ; la source est `dashboard-ui/` (Vite). `dashboard/api.py:2292` documente encore un repli `static/index.html` **absent** | **PÉRIMÉ** |
| `:112` `logs/requests.db` | `opencode.py:758` ✅ | OK |
| `:11-16` `python opencode.py` = GUI par défaut | `opencode.py:33524` `use_gui = not _cli_args.no_gui` ✅ | OK |
| `:146-149` `vpn/manager.py:request_docker_autostart`, `/api/vpn-status.docker_daemon` | `vpn/manager.py:304,1919-1924`, `dashboard/routers/vpn_status_routes.py:239` ✅ | OK |

### 6.2 `README.md` (26 559 o) — écarts les plus graves

| Affirmation | Réalité | Verdict |
|---|---|---|
| `:341-342` `GET/POST /api/config/tool-capabilities` | **0 hit** repo-wide | **FAUX — endpoint inexistant** |
| `:349` `GET /api/tools` | **0 hit** repo-wide | **FAUX — endpoint inexistant** |
| `:91` verrou mono-instance = `logs/opencode.lock` | réel : `logs/opencode-{PORT}.lock` (`opencode.py:33513`) | **FAUX** |
| `:199` discovery frappe `https://opencode.ai/zen/v1/models` | réel : `https://opencode.ai/inference/v1/models` (`config/discovery.py:72-89`) | **FAUX** |
| `:200` `muse-*`/`spark-*` → `https://opencode.ai/zen/v1/responses` | réel : `.../inference/go/openai/v1/responses` (`config.yaml:9-10`) | **FAUX** |
| `:318` `/v1/systemone` → `zen/v1/systemone` | réel : `https://opencode.ai/inference/v1/systemone` (`config.yaml:1431,1434`) | **FAUX** |
| `:104-129` échantillon `ip_rotation` : `quota_per_ip: 300`, `switch_delay: 5`, `circuit_breaker_threshold: 3`, `_recovery: 300`, `server_countries: Germany` | `config.yaml:280` = 200, `:702` = 1, `:117` = 1000, `:116` = 5, `:299` = `''` | **PÉRIMÉ sur 5 clés** — un utilisateur qui copie l'échantillon change le comportement |
| `:204` « 5 free models couverts » : `mimo-v2.5-free`, `nemotron-3-ultra-free`, `muse-spark-1.2/1.3-contributor-free` | live : 4/5 absents ; la cible réelle de `mimo-v2.5` est `deepseek-v4-flash-free` (`config.yaml:33`) | **PÉRIMÉ** |
| `:230` « 56 ids free, 32 payants rejetés, liste stricte de 10 » | live : `candidates 76, detected 16, listed 15` | **PÉRIMÉ** |
| `:423` « ~740 tests » | **6 385 collectés** (rapport 10 §2) | **PÉRIMÉ (×8,6)** |
| `:438` « passer `dashboard_trust.mode` en mode **strict** » | `trust.py:107-119` n'accepte que `open`, `lan`, sinon loopback-only. **Pas de mode `strict`** | **FAUX** |
| `:375` `vpn/configs/*.ovpn` suivis | le répertoire n'existe pas ; aucun `.ovpn` suivi (`.gitignore:96`) | **FAUX** (conclusion correcte par accident) |
| `:374` masquage « `dashboard/api.py:912, 1093` » | les vraies lignes sont `dashboard/routers/config_routes.py:111-143`, `dashboard/api.py:329` | **PÉRIMÉ** |
| `:388-415` modules racine décrits comme des implémentations | ce sont des **shims** de ré-export (`vpn_manager.py:1`, `free_ip_pool.py:1`, `traffic_capture.py:1`, `station_supervisor.py:1`, `latency_rotation.py:1`, `shared_rotation.py:1`, `protocol_mapping.py:1`, `free_discovery.py:1-10`) | **PÉRIMÉ (structure)** |
| `:424` `custom_routes.json # Custom route mappings (persistent)` | source de vérité = `config.yaml:757` ; le JSON n'est qu'un repli (`config/settings.py:2282`) et diverge | **PÉRIMÉ** |
| `:59-60` `OPENCODE_GO_WORKSPACE_ID` / `OPENCODE_GO_AUTH_COOKIE` dans le flux « copier `.env.example` » | lus par le code (`config/settings.py:509-510`) mais **absents de `.env.example`** | **PÉRIMÉ (template)** |

### 6.3 `AGENTS.md` (15 586 o)

Le document est une **spécification d'assistant** (§0 règles free + §1-10 role/values/policy), pas une doc technique. La seule affirmation vérifiable — `:13` « six stations client-utilisables » — est **cohérente** avec `config.yaml:1159` (`max_active_stations: 6`) et 10 stanzas gluetun dans `docker-compose.yml`. **Aucun écart détecté.** Le §0 contredit en revanche l'état live (rapport 00 §3 : 4/6 en `vpn=error`) — mais c'est un **objectif** déclaré, pas une affirmation de fait.

### 6.4 `CHANGELOG.md` (89 565 o, 179 lignes)

| Constat | Preuve | Verdict |
|---|---|---|
| Section `[Unreleased]` couvre `2026-11-02` → `2026-11-18` (17 entrées datées) | `grep -oE '\[2026-11-[0-9]{2}\]' \| sort -u` → dernière = `2026-11-18` | **À JOUR au 2026-11-18** |
| **Aucune entrée pour les changements du 2026-11-19/20** (114 entrées `git status`, dont `config/store.py`, `specs/023-*`, `free/clients/`) | `grep -c "2026-11-19\|2026-11-20"` → 0 | **EN RETARD de 2 jours** sur un arbre de travail massivement modifié |
| **Aucune entrée pour 78 fichiers modifiés** (5 713 insertions / 2 929 suppressions) | `git diff --stat` → 78 fichiers, dernier commit = `2026-11-16` | **CONSTITUTION XVII** : « Le changement met à jour dans le même lot : spec, plan, `CHANGELOG`, option-map, config et documentation » |
| `scripts/gate.ps1` jamais mentionné | `grep -c "gate.ps1" CHANGELOG.md` → 0, alors que c'est la **seule gate locale** | écart mineur |
| Une version stable existe (`## [2026-08-23] — Audit initial`) mais **tout le reste est en `[Unreleased]`** | `grep -n '^## \['` → 2 sections seulement | 3 mois de travail non versionné |

### 6.5 `PLAN_*.md` / `RAPPORT_*.md` racine (14 fichiers suivis, 528 ko)

Aucun n'est un document vivant : ce sont des plans de septembre-octobre 2026 (`PLAN_AUDIT_CONVERSIONS_2026-09-10.md`, 146 ko ; `RAPPORT_AUDIT_CONVERSIONS_2026-09-10.md`, 86 ko ; `PLAN_AUDIT_LOGS_2026-09-10.md`, 55 ko). Le seul effet mesurable : `full_sweep_2026-10-12.xml` et les 11 `*_reverify.xml` (592 ko, **suivis**) sont les artefacts JUnit de ces plans et **n'ont aucune valeur au-delà de la citation historique**. Aucun n'est lu par du code.

---

## 7. État de la CI vs gates constitutionnelles

### 7.1 Ce que `ci.yml` exécute (job unique `lint-test`, `ubuntu-latest`, timeout 25 min)

| Pas | Bloquant | Ligne |
|---|---|---|
| `ruff check .` | ✅ | `:28` |
| `ruff format --check .` | ❌ `continue-on-error: true` | `:35-36` |
| `mypy` (19 fichiers prod) | ✅ | `:41-48` |
| `mypy .` (progressif) | ❌ `continue-on-error: true` | `:50-51` |
| `python scripts/ci_contracts.py` | ✅ | `:57` |
| `python scripts/config_audit.py --strict` | ✅ | `:64` |
| `pytest tests/test_zero_429.py test_client_reserve.py test_reserve_settings.py` | ✅ | `:70` |
| `pytest -q -m "not docker" --cov --cov-fail-under=60` | ✅ | `:76` |
| `python scripts/quality_gate.py --with-soak` | ✅ | `:81` |
| Assets pré-compressables | ✅ | `:96-112` |
| Budget d'import du boot (< 1200 ms) | ✅ | `:113-129` |
| httpx différé hors du boot | ✅ | `:130-137` |
| `pip-audit --desc --local` | ✅ | `:139` |
| `gitleaks` | ✅ | `:143` |
| `docker compose config \|\| echo skipped` | ❌ **avalé par `\|\|`** | `:148` |
| `gunicorn --check-config \|\| echo skipped` | ❌ **avalé par `\|\|`** | `:150` |

### 7.2 Gates constitutionnelles → câblage réel

| Gate citée par la constitution | Câblée en CI ? | Câblée ailleurs ? | Écart |
|---|---|---|---|
| `pytest -k "not docker"` (`:328`) | ✅ (via `-m "not docker"`) | ✅ `gate.ps1:44` | ⚠️ `gate.ps1:44` utilise `-k` (filtre **par nom**) au lieu de `-m` (marqueur) → **désélectionne 46 tests** dont `test_docker_events.py` (7), `test_docker_autostart.py` (7), `test_no_docker_exec_hot_path.py` (3) |
| `config_audit --strict` (`:328`) | ✅ `ci.yml:64` | ✅ `quality_gate.py:158` | ⚠️ le gate est permissif sur 62 % des clés (P1-1) |
| **`platform_audit --strict`** (`:329`) | ❌ **absent** | ❌ `gate.ps1` **et** `quality_gate.py` : 0 occurrence | **P0-2** — le script existe et passe (0 violation / 279 fichiers) mais n'est atteint qu'indirectement par `tests/test_platform_audit.py` |
| **`test_option_map.py`** (`:329`) | ✅ (via `pytest` global) | ✅ `quality_gate.py:105` | ⚠️ couvre 109 / 1 341 clés (8 %) ; `scripts/option_map.py` **n'existe pas** |
| **`npm run check` (si UI touchée)** (`:329`) | ❌ **0 occurrence de `npm` dans `ci.yml`** | ❌ | **Écart** — `dashboard-ui/` a `npm run check` (`README:174`) mais la CI ne le lance jamais |
| **matrice CI Windows/Linux** (`:161-163`) | ❌ `runs-on: ubuntu-latest` seul, `grep "matrix\|windows\|macos"` → **0 résultat** | ❌ | **P0** (déjà relevé par le rapport 10 P0-1) |
| `debt_gate` (implicite via XIV) | ❌ **absent** | ✅ script existe, testé par `tests/test_debt_gate.py` | **P1-4** |
| `module_size_audit --ratchet` (implicite via XIV) | ❌ absent de `ci.yml` | ✅ `quality_gate.py:112-171` (donc **atteint** via `ci.yml:81`) | OK de fait |
| `dependency_graph --fail-on-cycles` | ❌ absent de `ci.yml` | ✅ `quality_gate.py:180-182` (scopé `free/qualification`) | OK de fait, **portée étroite** |
| `config_coverage --baseline` | ❌ **absent** | ✅ script + baseline existent (`docs/adr/ADR-007:9`) | **Écart** — le gate O1 « zéro clé morte » documenté dans le CHANGELOG n'est pas câblé |
| `scripts/option_map.py` | ❌ | ❌ **le fichier n'existe pas** | **P0-2** |

**Deux passes silencieuses à corriger dans `ci.yml`** : `:148` (`docker compose config || echo "skipped"`) et `:150` (`gunicorn --check-config || echo "skipped"`) avalent tout échec. Un `docker-compose.yml` cassé (or `.dockerignore:22` exclut `vpn/` que `Dockerfile:67` `COPY`) passerait la CI.

---

## 8. Modules > 1 500 lignes dans la zone + plan de découpage

`module_size_audit.py` (exécuté) : **9 violations** sur 755 fichiers.

| Fichier | Lignes | Baseline | Δ | Dans la zone ? |
|---|---:|---:|---:|---|
| `opencode.py` | **33 618** | 31 861 | **+1 757** | non (rapport 06) |
| `vpn/manager.py` | 11 471 | 11 385 | +86 | non |
| `free/pool/__init__.py` | 8 362 | 8 049 | +313 | non |
| `app/protocol/mapping.py` | 6 329 | 6 045 | +284 | non |
| `scripts/fresh_chain_supervisor.py` | 3 294 | 2 091 | **+1 203** | **OUI** |
| `scripts/fresh_chain.py` | 3 242 | 2 678 | **+564** | **OUI** |
| `dashboard/api.py` | 3 047 | — | — | non (rapport 08) |
| **`config/settings.py`** | **2 782** | 2 753 | **+29** | **OUI** |
| `scripts/gen_golden_fixtures.py` | 1 588 | — | — | **OUI** (au-dessus du seuil, non dans la baseline) |

### 8.1 Plan de découpage — `config/settings.py` (2 782 l. → cible ≈ 700 l.)

| Lot | Contenu | Lignes | Destination | Effort |
|---|---|---:|---|---|
| C1 | **Store YAML/ENV** : `load_yaml_config`, `save_yaml_config`, `yaml_get`, `yaml_set`, `load_env_file`, `save_env`, `_config_yaml_mtime`, `_yaml_data` | ~450 | **`config/store.py` (existe déjà, 213 l.)** — le module T027 est en place, il suffit d'y déplacer le store. Bloquant : les tests rebindent `settings._yaml_data`/`settings.CONFIG_PATH` → garder une façade de ré-export | **M** |
| C2 | **Protection des sections** : `_PROTECTED_SECTIONS`, `_restore_missing_keys`, `_yaml_for_disk`, scrub `control_api_key` | ~200 | `config/protect.py` | **S** |
| C3 | **Routes et modèles** : `load_routes`, `ROUTES`, `SORTED_ROUTES`, `MODEL_API_FAMILIES`, `get_model_config`, `get_model_api`, `get_model_capabilities`, `FAMILY_CAPABILITIES`, `UNKNOWN_MODEL_CAPABILITIES` | ~500 | `config/routes.py` (existe : `config/npm_routing.py` en est l'ébauche) | **M** |
| C4 | **API keys** : `API_KEYS`, `save_api_keys`, `_merge_preserved_api_keys`, `API_KEY_ROUTING` | ~250 | `config/keys.py` | **S** |
| C5 | **Compaction** : `get_server_compaction`, `_SERVER_COMPACTION_DEFAULTS` | ~180 | `config/compaction.py` | **S** |
| C6 | **Free models / discovery** : `FREE_MODELS`, `FREE_MODEL_MAP`, `FREE_MODEL_POOL`, `_ensure_free_models_sync`, `_free_endpoint_for`, hooks de hot-reload | ~600 | `config/free_models.py` (le `config/discovery.py` de 1 022 l. porte déjà la logique de fetch) | **L** |
| C7 | **Géo** : `GEO_*`, `resolve_geo`, `geo_strict_union`, `_normalize_geo_list`, `_resolve_geo_extends` | ~250 | `config/geo.py` (existe, 300 l.) — **déplacement pur** | **S** |
| C8 | **Hot-reload** : `maybe_reload_custom_routes`, watcher mtime, thread `upstream-models-fetch` | ~200 | `config/hot_reload.py` | **M** |
| C9 | **Reste** : façade `config/settings.py` (ré-exports) + `config/__init__.py` | ~150 | — | **S** |

**Ordre** : C7 (déjà partiel) → C5 → C4 → C2 → C3 → C8 → C1 → C6 → C9. **Un lot = un commit**, `pytest -k "not docker"` vert entre chaque (règle ADR-006 §6).
**Garde-fou** : `tests/test_config_save_preserves_disk_keys.py`, `tests/test_config_store.py`, `tests/test_hot_reload_phase4.py`, `tests/test_free_discovery.py` **rebindent les globaux de `settings`** — les lots C1/C6/C8 doivent conserver des ré-exports vivants, pas des copies.

### 8.2 Plan de découpage — `scripts/fresh_chain_supervisor.py` (3 294 l.) et `fresh_chain.py` (3 242 l.)

Ces deux scripts **ne sont pas dans le seuil** au sens strict (« aucun **nouveau** module > 1 500 ») mais ils ont **grossi de +1 203 et +564** depuis la baseline, et ce sont les seuls scripts de la zone à dépasser le seuil. Ils sont **vivants** (`opencode.py:2396` lance `fresh_chain_supervisor.py`).

| Lot | Contenu | Destination | Effort |
|---|---|---|---|
| F1 | **Lecteurs de config `ip_registry.allocator.*`** (`fresh_chain.py:1976-2096`, `fresh_chain_supervisor.py:2300-2354`) | `free/ip_registry/config.py` — **18 clés y sont déjà lues** ; ces scripts les relisent à la main, en double | **S** |
| F2 | **Budget de rotation** (`RotationBudgetStore`, `rotate_batch_*`) | `free/rotation_policy.py` (existe, consommé par le superviseur) | **M** |
| F3 | **Boucle de flotte** (`_fleet_usable`, gating 6/6, grace) | `free/fleet_gate.py` | **L** |
| F4 | **Probes et verdicts** | `free/probe_loop.py` | **L** |

**F1 est le lot le plus rentable** : il supprime une **double lecture** de la même section (18 clés dans `free/ip_registry/config.py` **et** ~14 relues à la main dans les 2 scripts) — c'est aussi la cause racine de la conclusion erronée du rapport 00-V-27.

---

## 9. Zones non auditées / incertitudes

1. **`config_coverage.py` n'a pas pu produire sa liste nominative des 2 MORTE** dans le budget de temps (l'analyse coûte ~5 min sur 901 fichiers). Les 2 clés ont été identifiées **indépendamment** par reproduction de `cc.needle_for()` + `cc.search()` sur les candidats du baseline : `thinking.effort_caps.ling-3.0-flash-fin` et `thinking.effort_caps.muse_spark`. La correspondance avec le compteur « 2 MORTE » est cohérente mais non prouvée à l'identique.
2. **Le comportement de `config_coverage.py --baseline`** a bien été exécuté (exit 0) — mais son `--baseline` n'est câblé **ni** en CI **ni** dans `gate.ps1`/`quality_gate.py` : le gate O1 annoncé par `CHANGELOG.md:80` et `docs/adr/ADR-007:9` **n'existe pas en pratique**.
3. **`free/wire_admission.py` (81 l.)** : 0 consommateur de production (`grep` → module + `tests/unit/test_wire_admission.py` seulement). Non supprimable dans ce lot : le fichier est **non suivi** (`??`) et appartient à la spec 023 (P0-1). Idem `free/provider_capabilities.capacity()`/`is_reserve_eligible()` (0 appelant, rapport 04 §4).
4. **`rotation/infrastructure/config/schema.py`** : contient les consommateurs de `ip_registry.selection.*` (5 clés) mais n'est importé **que** par `tests/test_rotation_config.py` — donc ces 5 clés sont « lues » par du code de production **non atteint**. Ni `config_audit` (qui les voit lues) ni le rapport 00-V-27 (qui conclut « zéro consommateur » en excluant `rotation/`) ne décrivent correctement cet état. **Le verdict correct est : consommateur existant, non branché.**
5. **`config.yaml` est réécrit en continu par le runtime** (P1-3) : toute mesure « config ↔ code » est un instantané. Les 625 clés mesurées valent pour l'état du 2026-11-20 ; `git diff config.yaml` montre 126 insertions / 74 suppressions vs HEAD sur la même journée.
6. **`logs/` (15 Go, 927 fichiers) n'a pas été audité en détail** (hors périmètre, traité par le rapport 08 §P2-6/7/8). Les 110 `.py` de `logs/` sont comptés mais non classés un par un.
7. **`_research/` (275 `.py`, 1 seul suivi) et `.artifacts/` (42 Mo, 1 141 fichiers)** : hors périmètre. Le premier est une dette de reproductibilité (275 fichiers non versionnés sur lesquels s'appuient des décisions documentées), le second est un cache d'audit.
8. **Le contenu de `gluetun/` (31 Mo, fork complet)** n'a pas été audité — seul son statut git a été établi (dépôt imbriqué, non déclaré, non suivi).
9. **`npm run check` sur `dashboard-ui/`** n'a pas été exécuté (Node non requis pour cet audit) ; le constat est limité à « absent de `ci.yml` ».
10. **`skills-lock.json`** : les `computedHash` ne correspondent ni au SHA-256 brut ni au SHA-256 normalisé LF des `SKILL.md` correspondants — impossible de trancher entre « schéma de hash différent » et « lock périmé ». Le verdict de suppression repose sur l'absence de consommateur, pas sur l'intégrité du lock.

---

## Annexe — Commandes de reproduction

```bash
# Différentiel config ↔ code (le gate officiel)
python scripts/config_audit.py --strict          # → 0 dead / 0 missing, exit 0
python scripts/config_audit.py --json            # → {"total_cles": 625, "cles_lues_par_yaml_get": 288, "dead": [], "missing": []}

# Le gate O1 (non câblé) — ~5 min
python scripts/config_coverage.py                 # → 600 clés : 407 CONSOMMÉE, 191 PARTIELLE, 2 MORTE
python scripts/config_coverage.py --baseline scripts/config_coverage.baseline.json   # → exit 0

# Gates constitutionnelles non câblées
python scripts/platform_audit.py --strict         # → 0 violation / 279 fichiers (mais absent de ci.yml)
python scripts/debt_gate.py --strict              # → 25 dettes, 0 P0 open (mais absent de ci.yml)

# Ratchet de taille
python scripts/module_size_audit.py                                        # → 9 violations > 1500
python scripts/module_size_audit.py --ratchet specs/014-nordvpn-exhaustive-rotation/module-baseline.json  # → 7 'grew'

# Contrats d'alignement (bloquant CI)
python scripts/ci_contracts.py                    # → 522 tests, 0 échec, 47 s

# Preuves d'absence de consommateur (modèle pour tout verdict « mort »)
rg -l --no-heading -g '!Lib/**' -g '!_research/**' -g '!logs/**' -g '!.git/**' \
   -g '!**/__pycache__/**' -g '!module-baseline.json' -g '!scripts/**' \
   -g '!.audit-wt/**' -g '!.audit-logs-wt/**' '\b<NAME>\b' .
# ⚠️ NE PAS compter module-baseline.json comme référence d'usage (recensement de taille).

# Hygiène
git worktree list                                 # → 7 worktrees dont .audit-wt, .audit-logs-wt
git -C gluetun remote -v                          # → fork non déclaré (pas de .gitmodules)
git check-ignore -v .env.lock config.yaml.lock    # → non ignorés (polluent git status)
git status --porcelain | wc -l                    # → 114
```
