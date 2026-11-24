# Audit 10 — Suite de tests & CI

**Date** : 2026-11-20
**Périmètre** : `tests/` (489 fichiers `.py`), `test_free_quota.py` (racine), `pyproject.toml`, `.github/workflows/ci.yml`, `scripts/*_audit.py`, `free/clients/fixtures/`
**Constitution opposable** : `.specify/memory/constitution.md` v1.3.1
**Mode** : lecture seule sur le code ; exécution pytest autorisée.

---

## 1. Résumé exécutif

1. **La suite est saine et rapide à l'échelle du dépôt** : 6 385 tests collectés, 6 379 passés, **1 échec**, 5 skips, **527 s** (8 min 48). Le taux de réussite est de 99,98 %.
2. **L'unique échec n'est pas un flake** : `tests/test_boot_phase3b_lazy.py:135` `test_opencode_import_budget` échoue **3 fois sur 3 en isolation** (mesuré 1 345–1 721 ms contre un budget de 1 600 ms). Le budget est dépassé par la machine, pas par le code.
3. **P0 — La CI n'exécute pas le gate multiplateforme.** `.github/workflows/ci.yml` ne tourne que sur `ubuntu-latest`, sans matrice. Le principe XV exige explicitement « la gate non-Docker tourne sur **Windows et Linux** (matrice CI) ». Aucun workflow ne teste Windows.
4. **P0 — Le gate Linux local désélectionne silencieusement 46 tests.** `scripts/gate.ps1:44` et `:46` utilisent `pytest -k "not docker"` (filtre par **nom**) au lieu de `-m "not docker"` (filtre par **marqueur**). 46 tests légitimes sont écartés — dont `test_docker_events.py` (7), `test_docker_autostart.py` (7), `test_no_docker_exec_hot_path.py` (3), et 29 autres.
5. **P0 — `scripts/platform_audit.py --strict` n'est invoqué par aucune CI ni par aucun script de gate.** Il tourne bien (0 violation, 279 fichiers), mais uniquement parce que `tests/test_platform_audit.py` le rappelle indirectement. La commande que la constitution liste comme gate obligatoire (`constitution.md:329`) n'existe dans aucun pipeline.
6. **P1 — 7 tests tautologiques (`assert ... or True`) qui ne peuvent pas échouer** (cf. §5).
7. **P1 — La couverture nommée par la constitution est partielle** : `test_option_map.py` ne couvre que `egress_catalog.*` + 2 clés `ip_rotation.*` — soit **109 clés sur 1 341** du `config.yaml`. La règle XII (« toute option GUI a un effet réel ») n'est gardée que sur un périmètre étroit.
8. **P1 — 18 modules de production sans aucun test**, dont `observability/crashguard.py` (feature 021, exigence d'acceptation T008) et `ops/updater.py`.
9. **P2 — Le seuil de couverture CI est bas** : `--cov-fail-under=60` contre 87 % réellement mesuré sur `free/qualification`. Marge de 27 points qui laisse passer une régression massive.
10. **P2 — 5 tests skippés dont 2 dépendent de fichiers gitignorés** (`logs/`), donc jamais exécutés ni en CI ni sur un clone neuf.

---

## 2. État chiffré de la suite

### 2.1 Collecte et exécution

| Métrique | Valeur | Commande / preuve |
|---|---|---|
| Tests collectés (gate par défaut) | **6 385** | `python -m pytest --collect-only` → `6385/6393 tests collected (8 deselected)` |
| Tests collectés (tout, `-m docker` inclus) | **6 393** | idem (8 désélectionnés par `addopts = -m 'not docker'`) |
| Passés | **6 379** | `.artifacts/results.xml` : `tests="6385" failures="1" errors="0" skipped="5"` |
| Échoués | **1** | `test_boot_phase3b_lazy.py::test_opencode_import_budget` |
| Erreurs | **0** | idem |
| Skippés | **5** | cf. §2.3 |
| Durée totale | **527,8 s** (8 min 48) | `results.xml` attribut `time` |
| Fichiers de test | 476 racine + 9 `unit/` + 2 `contract/` = **487** | `ls tests/test_*.py \| wc -l` |
| Fichiers `.py` dans `tests/` | **489** | `find tests -name "*.py" \| wc -l` |
| Lignes de test | ~106 900 | `wc -l` cumulé |
| Moyenne | 10,0 tests / fichier | 4 756 `def test_` / 476 fichiers |

`tests/integration/` **existe mais est vide** (0 fichier) — répertoire mort.
`tests/fixtures/` ne contient que `error_classes.yaml`.
`free/clients/fixtures/` contient 8 `request.json` (aider, claude-code, codex, custom, deepseek, hermes-agent, kilo-code, openai-sdk), tous consommés par `tests/contract/test_client_families.py:9`.

### 2.2 Top 20 des tests les plus lents (suite complète)

| # | Durée | Test |
|---|---|---|
| 1 | 19,63 s | `test_config_audit.TestTheAuditIsCleanRightNow::test_no_dead_keys` |
| 2 | 12,05 s | `test_docker_autostart::test_cooldown_limits_relaunch` |
| 3 | 9,55 s | `test_config_audit.TestTheAuditDetectsAFreshDeadKey::test_a_key_added_to_the_config_is_reported` |
| 4 | 9,29 s | `test_config_audit…::test_strict_mode_passes_when_the_baseline_already_knows_the_key` |
| 5 | 9,29 s | `test_concurrency_guards::test_keypauser_save_snapshot_under_concurrent_mutation` |
| 6 | 9,08 s | `test_config_audit…::test_ip_registry_uses_strict_leaf_reads` |
| 7 | 9,01 s | `test_config_audit…::test_strict_mode_fails_on_a_new_dead_key` |
| 8 | 8,17 s | `test_graceful_aurora.TestEgressStates::test_tunnel_up_socks_down_is_degraded_routable` |
| 9 | 8,01 s | `test_docker_autostart::test_request_started_and_worker_clears_flag` |
| 10 | 7,00 s | `test_platform_audit::test_no_platform_violation_in_the_repository` |
| 11 | 6,55 s | `test_no_visible_spawn::test_no_visible_subprocess_in_runtime` |
| 12 | 5,08 s | `test_review_fixes::test_await_rotation_rejects_probe_bad_marked_fresh_ip` |
| 13 | 4,53 s | `test_restart_churn::test_watchdog_tick_churn_recovers_immediately` |
| 14 | 4,45 s | `test_boot_phase3b_lazy::test_opencode_import_budget` |
| 15 | 4,22 s | `test_gluetun_control_api.TestControlPinCountry::test_catchup_waits_for_real_ip_through_tunnel` |
| 16 | 4,09 s | `test_graceful_aurora.TestWaitHealthyTolerant::test_late_tunnel_up_recovers_no_runtime_error` |
| 17 | 4,07 s | `test_graceful_aurora.TestWaitHealthyTolerant::test_tunnel_down_returns_none` |
| 18 | 4,06 s | `test_gluetun_control_api.TestControlPinCountry::test_pin_warns_stopped_once_per_pin` |
| 19 | 4,04 s | `test_docker_autostart::test_running_desktop_is_never_relaunched` |
| 20 | 4,02 s | `test_boot_fanout::test_fanout_does_not_block_lifespan` |

**Fichiers les plus coûteux (somme)** : `test_config_audit.py` (56,7 s), `test_docker_autostart.py` (24,1 s), `test_no_real_config_write.py` (23,9 s), `test_gluetun_control_api.py` (13,4 s), `test_concurrency_guards.py` (11,2 s).

**Cause racine de la lenteur de `test_config_audit.py`** : chaque test ré-analyse tout le dépôt via AST (`audit.auditer()`), sans mémoïsation entre tests. 5 tests × ~9 s = 56 s pour une même analyse. Le `test_no_real_config_write.py` (23,9 s) fait aussi un parcours AST complet.

### 2.3 Les 5 skips

| Test | Raison déclarée | Verdict |
|---|---|---|
| `test_catalog_model_routing::test_model_api_falls_back_to_the_provider` | « ce modèle porte désormais une surcharge : choisir un autre témoin » | **Skip permanent déguisé** — le test ne s'exécutera plus jamais tel quel |
| `test_date_bounds.TestDateBoundToUtc::test_exact_utc2_values` | `GMTOFF != 7200` — « exact values assume UTC+2 (summer) » | **Skip saisonnier** : rouge en hiver, vert en été. Dépend de l'horloge réelle |
| `test_rotation_n_stations::test_fake_vpn_manager_n_stations` | `FakeVPNManager not mockable N=2: unsupported operand type(s) for /: 'NoneType' and 'str'` | **Skip sur erreur d'infrastructure de test** — le double est cassé, on skippe au lieu de corriger |
| `test_tool_schema_strict.TestStrictSubsetGuarantee::test_wire_schemas_strict_subset` | `dump logs/free400_msg_*.json absent (logs/ est gitignoré)` | **Jamais exécuté** en CI ni sur clone neuf |
| `test_tool_schema_strict.TestStrictSubsetGuarantee::test_wire_schemas_idempotent` | idem | idem |

---

## 3. Liste des échecs avec cause racine

### Échec unique : `test_opencode_import_budget`

- **Fichier** : `tests/test_boot_phase3b_lazy.py:135-154`
- **Assertion** : `assert ms < 1600, f"import opencode = {ms:.0f} ms (budget 1600 ms, passes={vals})"`
- **Message réel** : `AssertionError: import opencode = 1723 ms (budget 1600 ms, passes=[1723.0, 1994.0])`
- **Reproductibilité** : **3 échecs sur 3** en isolation (`pytest tests/test_boot_phase3b_lazy.py::test_opencode_import_budget`), donc **pas un flake**.
- **Mesure directe** (`python -c "import opencode"` × 3) : **1 345 ms / 1 525 ms / 1 721 ms** — variance de 376 ms sur la même machine, à froid.
- **Cause racine** : le test prend `min()` de 2 passes et compare à un budget de 1 600 ms. Le commentaire du test (`:138-142`) affirme « mesuré ~450-500 ms sur un poste au repos », ce qui n'est plus vrai sur cette machine (x3). Le budget est calibré pour une machine de référence, pas pour l'environnement réel.
- **Nuance importante** : ce n'est **pas** une régression d'import du code. C'est un **budget de performance non portable** — exactement le type de test que la constitution XVII qualifie de « flaky » à isoler ou corriger, jamais à ignorer.
- **Recommandation (S)** : soit relever le budget à 2 500 ms avec justification, soit marquer le test `@pytest.mark.skipif` sur la charge machine, soit le sortir du gate par défaut et le mettre dans un job dédié (`-m perf`). La troisième option est la seule qui respecte XVII.

---

## 4. Tableau de couverture : module → test → verdict

Méthode : analyse statique par nommage (nom de module présent dans le contenu ou les chemins des fichiers `tests/`), croisée avec les imports réels. Le `.coverage` présent date du **11 nov** (périmé) et ne couvre que `free/qualification` (2 751 statements, 87 %).

### 4.1 Synthèse par package

| Package | Modules | Sans aucun test | % non testé | Verdict global |
|---|---|---|---|---|
| `free/` | 152 | 7 | 5 % | **Testé (bon)** |
| `vpn/` | 3 | 0 | 0 % | **Testé** |
| `observability/` | 10 | 1 | 10 % | **Testé (1 trou)** |
| `ops/` | 17 | 1 | 6 % | **Testé (1 trou)** |
| `config/` | 17 | 0 | 0 % | **Testé** |
| `app/` | 23 | 0 | 0 % | **Testé** |
| `core/` | 5 | 0 | 0 % | **Testé** |
| `protocol/` | 1 | 0 | 0 % | **Testé** |
| `rotation/` | 19 | 0 | 0 % | **Testé** |
| `streaming/` | 7 | 0 | 0 % | **Testé** |
| `upstream/` | 7 | 0 | 0 % | **Testé** |
| `server/` | 5 | 1 | 20 % | **Partiel** |
| `dashboard/` | 51 | 1 | 2 % | **Testé** |
| `gui/` | 5 | 2 | 40 % | **Partiel** |
| `scripts/` | 105 | 69 | 66 % | **AUCUN (attendu : outils opérateur)** |
| racine | 18 | 3 | 17 % | **Partiel** |

### 4.2 Modules de production SANS AUCUN test (liste nominative)

**`free/` (7) — priorité haute, c'est le périmètre constitutionnel**

| Module | Lignes | Consommateur prod | Risque |
|---|---|---|---|
| `free/chain_repair.py` | 132 | `scripts/fresh_chain.py:37-44` (6 imports) | **P1** — le docstring dit « extracted … so the rules are testable without Docker », mais aucun test ne l'importe. Ses symboles sont testés via `fresh_chain`, jamais directement |
| `free/chain_registry.py` | 73 | `scripts/fresh_chain.py:409` | P2 |
| `free/console_auth.py` | 55 | `free/identities.py:471`, `free/runtime_identity.py:108`, `opencode.py:10179` | **P1** — chemin d'authentification free non testé |
| `free/hunter.py` | 191 | script autonome | P3 |
| `free/provider_capabilities.py` | 100 | déclaration provider | **P1** — « rotation, bucket, quota_source, santé » non testés |
| `free/clients/family_passthrough.py` | 18 | famille `passthrough` (023 T051) | P2 |
| `free/qualification/repair_store.py` | 23 | watchdog + store | P2 |

**`observability/` (1)**

| Module | Consommateur | Risque |
|---|---|---|
| `observability/crashguard.py` | `opencode.py:34` (`from observability.crashguard import install`) | **P1** — spec 021 T008, critère d'acceptation explicite (`specs/021-app-crash-resilience/tasks.md:67` : « une exception non gérée produit une entrée horodatée dans `logs/app-crash.log` »). **Aucun test ne le vérifie.** |

**`ops/` (1)**

| Module | Risque |
|---|---|
| `ops/updater.py` | **P1** — logique de rollback global best-effort de la flotte (pull d'image + recréation séquentielle). Un rollback non testé est un rollback qui ne marche pas |

**`server/` (1)**

| Module | Risque |
|---|---|
| `server/accesslog.py` | P2 — middleware ASGI pur, `log_fn` injecté. Facile à tester, non testé |

**`gui/` (2)**

| Module | Risque |
|---|---|
| `gui/_webview_main.py`, `gui/icon.py`, `gui/tray.py` (3 en fait) | P3 — dépendances GUI, non testables headless (justifiable, mais à documenter) |

**`dashboard/routers/read_models.py`** — P2.

**Racine (3)** : `ddgtest.py`, `wstest.py` (scripts live), `gunicorn.conf.py` (config) — P3, acceptable.

**`scripts/` (69)** : outils opérateur (migrations, probes, canaries, benches). Non testés par conception, **sauf** ceux qui portent une gate : `scripts/ci_contracts.py` est testé (`test_ci_contracts_gate.py`), `scripts/config_audit.py` est testé (`test_config_audit.py`), `scripts/platform_audit.py` est testé (`test_platform_audit.py`). Le modèle est bon.

### 4.3 Modules sous-testés (import absent des tests, ≤3 mentions)

| Module | Mentions | Verdict |
|---|---|---|
| `free/reserve_settings.py` | 1 | **P1** — spec 022, réglages de la réserve client. Le CI exécute `test_reserve_settings.py` (le nom matche) mais l'import n'apparaît pas → couverture indirecte seulement |
| `free/rotation_policy.py` | 1 | **P1** — `tests/unit/test_rotation_policy.py` existe (7 tests) mais n'importe pas le module |
| `free/wire_admission.py` | 1 | **P1** — `tests/unit/test_wire_admission.py` (6 tests), même pattern |
| `free/clients/family_anthropic.py` | 2 | Partiel — via `tests/contract/test_client_families.py` |
| `free/clients/family_openai_chat.py` | 2 | Partiel |
| `free/clients/family_openai_responses.py` | 2 | Partiel |
| `ops/lifecycle_evidence.py` | 1 | P2 |
| `app/compaction/lean.py`, `app/compaction/shapes.py` | 2 | P2 |
| 8 × `dashboard/routers/*.py` | 2–3 | P2 — testés par leurs routes, pas par le module |

**Note méthodologique** : les fichiers `tests/unit/test_*.py` (9 fichiers, 61 tests) et `tests/contract/test_*.py` (2 fichiers, 31 tests) portent les mêmes noms que des modules de production mais les testent via une façade. Ce n'est pas un défaut — c'est un choix — mais cela signifie que la couverture « par nom » est optimiste pour ces 17 modules.

---

## 5. Findings

### P0

---

**P0-1 — La CI ne tourne que sur Linux : le principe XV n'est pas tenu**

- **Fichier** : `.github/workflows/ci.yml:10` (`runs-on: ubuntu-latest`)
- **Preuve** : `grep -n "windows\|matrix\|macos" .github/workflows/ci.yml` → **aucun résultat**. Un seul job, un seul OS.
- **Texte constitutionnel violé** : `constitution.md:161-163` — « La gate non-Docker tourne sur **Windows et Linux** (matrice CI) ; un test spécifique à un OS est marqué et a une contrepartie ou une justification écrite. Aucune régression ne peut être "corrigée" en désactivant un OS. »
- **Impact** : 4 tests sont Windows-only (`test_docker_autostart.py:17` `skipif(sys.platform != "win32")`, `test_workspace_alias.py:12,24` `skipif(os.name != "nt")`, `test_station_identity.py:291`). Sur `ubuntu-latest`, ces 7 tests (dont 7 pour `test_docker_autostart`) sont **skippés**. Le chemin Windows — celui qui tourne réellement en production selon `CLAUDE.md` — n'est **jamais validé par une CI**. Le gate local `scripts/gate.ps1` existe mais n'est pas automatisé.
- **Effet secondaire** : `test_workspace_alias.py` utilise des **jonctions NTFS**, sans contrepartie POSIX. `ensure_workspace_alias` (`free/station_identity.py:329`) retourne tôt si `os.name != "nt"` — le comportement POSIX n'est ni testé ni spécifié.
- **Recommandation (M)** : ajouter un job `windows-latest` en matrice (`strategy.matrix.os: [ubuntu-latest, windows-latest]`). Au minimum, un job Windows qui exécute `python -m pytest -m "not docker" -q` + `python scripts/platform_audit.py --strict`.

---

**P0-2 — `gate.ps1` désélectionne 46 tests avec `-k` au lieu de `-m`**

- **Fichier** : `scripts/gate.ps1:44` — `python -m pytest -k "not docker" --cov=. --cov-fail-under=60`
- **Preuve** : comparaison des node IDs collectés :
  ```
  python -m pytest --collect-only -q -v            → 6385 node ids
  python -m pytest --collect-only -q -v -k "not docker" → 6339 node ids
  DROPPED: 46
  ```
  Contre 8 seulement avec `-m "not docker"` (les 8 vrais tests `@pytest.mark.docker`).
- **Tests écartés** (extrait, 46 au total) : `test_docker_events.py` (7/7), `test_docker_autostart.py` (7/7), `test_no_docker_exec_hot_path.py` (3/3), `test_boot_reconcile.py` (3), `test_boot_phase6_gate.py` (4), `test_warp_rotation.py` (3), `test_gluetun_control_api.py` (3), `test_zero_429.py::test_reserve_scope_is_docker_stations_only`, et 15 autres.
- **Cause** : `-k` filtre sur le **nom** du test. Tout test dont le nom contient « docker » est écarté — y compris ceux qui testent le comportement *quand Docker est absent*, ce qui est précisément le cas à tester.
- **Impact** : le gate local Windows (celui qui tourne réellement sur cette machine) ne valide pas 46 tests. `test_zero_429.py::test_reserve_scope_is_docker_stations_only` — une gate de la spec 022 — n'est jamais exécutée par `gate.ps1`.
- **Note** : la CI, elle, utilise correctement `-m "not docker"` (`.github/workflows/ci.yml:76`). L'incohérence est uniquement dans le gate local.
- **Recommandation (S)** : remplacer `-k "not docker"` par `-m "not docker"` dans `scripts/gate.ps1:44` et `:46` (ainsi que dans le commentaire `:7`).

---

**P0-3 — `platform_audit --strict` n'est invoqué par aucun pipeline**

- **Fichiers** : `.github/workflows/ci.yml` (aucune mention), `scripts/quality_gate.py:138-224` (aucune mention), `scripts/gate.ps1` (aucune mention), `scripts/ci_contracts.py` (aucune mention)
- **Preuve** : `grep -rn "platform_audit" --include="*.yml" --include="*.ps1" --include="*.sh"` → **0 résultat**. Les seules occurrences sont dans la constitution, le CHANGELOG, des specs et `tests/test_platform_audit.py`.
- **Texte constitutionnel violé** : `constitution.md:328-330` — « **Gates obligatoires avant merge** : `pytest -k "not docker"`, `config_audit --strict`, `platform_audit --strict`, `test_option_map.py`, … »
- **Ce qui tourne quand même** : `tests/test_platform_audit.py:38-42` appelle `audit.audit()` et assert `findings == []`. Donc le contrôle a bien lieu — **via pytest**. J'ai vérifié manuellement : `python scripts/platform_audit.py --strict` → exit 0, 279 fichiers scannés, 0 violation.
- **Impact** : le résultat est correct aujourd'hui, mais la gate nommée par la constitution n'existe pas comme étape de pipeline. Si `test_platform_audit.py` était renommé ou son import cassé, `ci_contracts.py` ne le détecterait pas (il n'est pas dans `CONTRATS`) et la CI resterait verte. C'est exactement le scénario que `test_ci_contracts_gate.py` a été écrit pour prévenir — mais il ne couvre que les fichiers déclarés dans `ci_contracts.CONTRATS`.
- **Recommandation (S)** : ajouter une étape `python scripts/platform_audit.py --strict` dans `ci.yml` (à côté de `config_audit --strict`, ligne 64) et dans `quality_gate.py`.

---

### P1

---

**P1-1 — 7 assertions tautologiques (`assert ... or True`)**

| Fichier:ligne | Assertion | Effet |
|---|---|---|
| `tests/test_cache_anthropic_conformance.py:247` | `assert "cache_control" in out["messages"][-1] or True  # breakpoints message OK` | **Ne peut jamais échouer** |
| `tests/test_chaos_lifecycle.py:75` | `assert mgr.calls.get("compose_up", 0) == 0 or True, "aucun compose post-cancel"` | idem — la garde « aucun compose post-cancel » ne garde rien |
| `tests/test_coverage_boost.py:11` | `assert hasattr(docker_events, "DockerEventWatcher") or True` | idem |
| `tests/test_lot7_ops.py:118` | `assert isinstance(text, str) and "vpn_station_connected" in text or True` | idem — le test ne vérifie plus que « ça ne crash pas » |
| `tests/test_tool_schema_normalization.py:623` | `assert "additionalProperties" not in json.dumps(...) or True` | idem |

**Note sur `tests/test_socks5_quality_rotation.py:64`** : `calls.append((station.pid, cause)) or True` — ce n'est **pas** un bug. C'est une lambda qui doit retourner une valeur truthy tout en enregistrant l'appel ; le `or True` force le retour à `True`. Faux positif, à ne pas corriger.

**Impact** : 5 contrats mesurés (cache_control, compose post-cancel, DockerEventWatcher, métriques ops, schémas d'outils) sont déclarés testés mais ne le sont pas. Un `git blame` montrerait probablement un debug laissé en place.
**Recommandation (S)** : retirer les `or True` et corriger les assertions, ou supprimer les tests s'ils ne sont plus pertinents.

---

**P1-2 — `test_tool_compat.py` ne teste rien (script live sans fonction `test_*`)**

- **Fichier** : `tests/test_tool_compat.py` (335 lignes, **0 assertion, 0 fonction `test_*`**)
- **Preuve** : `for f in tests/test_*.py; do grep -cE "^\s*(assert|with pytest\.raises)" $f; done` → seul `test_tool_compat.py` ressort à 0. `grep -n "^def "` → uniquement `main()`, `_send_request`, `_check_tool_use`, etc.
- **Ce que c'est** : un script opérateur qui envoie de vraies requêtes à `http://localhost:4000` sur 10 modèles × 9 outils, avec `asyncio.sleep(1.0)` entre chaque et `await asyncio.sleep(2)` entre modèles.
- **Impact** : pytest collecte 0 test depuis ce fichier (il ne matche pas `python_functions = ["test_*"]` faute de fonction, et `test_free_quota` n'est pas appelé). Le fichier occupe une place dans `tests/` et donne l'illusion d'une couverture « tool compatibility » qui n'existe pas. Il est de plus **dangereux** : lancé via `python tests/test_tool_compat.py`, il consomme du quota free réel (10 modèles × 9 outils = 90 requêtes), ce qui viole le principe XVIII (chaque requête lie l'IP à un format).
- **Recommandation (S)** : déplacer vers `scripts/` (son vrai foyer) et le retirer de `tests/`.

---

**P1-3 — `test_option_map.py` ne couvre que 8 % du `config.yaml`**

- **Fichier** : `tests/test_option_map.py:19-20` — `SCOPED_SECTIONS = ("egress_catalog",)`, `SCOPED_IP_ROTATION = ("public_proxies", "egress_adaptive")`
- **Preuve** : `config.yaml` contient **1 341 feuilles** ; `egress_catalog.*` en compte **109**. Le test ne vérifie donc que **109 clés sur 1 341** (8,1 %).
- **Texte constitutionnel violé** : `constitution.md:114-120` (XII) — « Chaque contrôle du GUI doit être décrit par un contrat `option → clé config → consommateur runtime → effet observable → test`. … Aucune option inerte n'est acceptée. » Le principe n'est pas borné à `egress_catalog`.
- **Côté GUI** : `dashboard-ui/src/` contient **184 contrôles interactifs** (`<button|<input|<select|<textarea`) et seulement **17 attributs `data-option-map`**. Les 167 contrôles restants ne sont pas couverts par le contrat.
- **Ce qui rattrape partiellement** : `scripts/config_audit.py --strict` couvre les 625 clés hors sections dynamiques (0 dead / 0 missing, vérifié : exit 0). Mais `config_audit` prouve seulement que la clé est **lue** — pas qu'elle a un **effet observable** ni un **test**. C'est la moitié du contrat XII.
- **Recommandation (L)** : étendre `SCOPED_SECTIONS` par lots (priorité : `dashboard_trust`, `ip_rotation` hors `public_proxies`, `retry_until_success`, `client_reserve`) et ajouter les `data-option-map` sur les contrôles GUI.

---

**P1-4 — `crashguard.py` : exigence d'acceptation de la spec 021 sans test**

- **Fichier** : `observability/crashguard.py` (module non testé) ; installé en production à `opencode.py:34`
- **Exigence** : `specs/021-app-crash-resilience/tasks.md:65-70` — T008, statut **« fait »**, « **Acceptation** : une exception non gérée produit une entrée horodatée dans `logs/app-crash.log` ».
- **Preuve de l'absence** : `grep -rn "crashguard\|app-crash" tests/*.py` → **0 résultat**.
- **Texte constitutionnel violé** : XVII, « Chaque exigence fonctionnelle a un test automatisé ou une preuve live référencée ».
- **Impact** : la spec 021 a été diagnostiquée sur un incident réel (`specs/021-app-crash-resilience/spec.md:30` : « Aucun crashguard/faulthandler n'est actif : pas de pile de crash natif »). La correction est marquée faite, mais rien n'empêche une régression de la retirer.
- **Recommandation (S)** : test `tmp_path` qui appelle `install()`, lève une exception non gérée dans un thread/fork et vérifie l'écriture horodatée dans le fichier redirigé.

---

**P1-5 — `free/console_auth.py` : chemin d'authentification free non testé**

- **Fichier** : `free/console_auth.py` (55 lignes), consommé par `free/identities.py:471`, `free/runtime_identity.py:108`, `opencode.py:10179`
- **Preuve** : aucune mention dans `tests/`.
- **Impact** : c'est le module qui porte le passage du « Bearer public » anonyme au credential Console du client 2.2.x — un changement de sécurité documenté en tête de fichier. Trois chemins d'appel distincts, aucun test.
- **Recommandation (S)**.

---

**P1-6 — `ops/updater.py` : rollback de flotte non testé**

- **Fichier** : `ops/updater.py`, consommé par le lifecycle de la flotte
- **Preuve** : `grep -rn "ops.updater\|from ops import updater" tests/*.py` → 0 résultat.
- **Impact** : le docstring décrit un scénario d'échec précis (« les suivantes voyaient le même digest et ne recréaient jamais leur conteneur ») et un « rollback global best-effort sur l'image précédente ». Un rollback non testé est un rollback qui ne marche pas le jour où il sert.
- **Recommandation (M)** — testable sans Docker via un double de l'exécuteur Docker.

---

**P1-7 — `free/chain_repair.py` : extrait « pour être testable », jamais testé**

- **Fichier** : `free/chain_repair.py:2-4` — *« Extracted from `scripts/fresh_chain.py` so the legacy monolith does not grow and the rules are testable without Docker or a station. »*
- **Preuve** : le module n'apparaît dans aucun test ; les tests existants (`test_fresh_chain_lock.py`, `test_fresh_chain_candidates.py`) importent `from scripts import fresh_chain` et testent les **ré-exports**, pas le module extrait.
- **Impact** : la justification de l'extraction (testabilité) n'a pas été honorée. Le module porte 6 symboles (`pid_alive`, `filter_candidates_for_transport`, `acquire_lock`, `release_lock`, `exhausted_egress`, `transport_parts`) dont la logique de verrou et d'exhaustion d'egress.
- **Recommandation (S)** — réorienter les tests vers `free.chain_repair` directement, ou acter que `fresh_chain` est la surface testée et le documenter.

---

**P1-8 — Le seuil de couverture CI (60 %) est très en dessous du réel (87 %)**

- **Fichiers** : `.github/workflows/ci.yml:76` (`--cov-fail-under=60`), `scripts/gate.ps1:44` (idem)
- **Preuve** : le `.coverage` du 11 nov donne **87 %** sur `free/qualification` (2 751 statements, 345 manquants). `scripts/quality_gate.py:220` exige `--fail-under=85` **sur `free/qualification` uniquement**.
- **Impact** : une régression qui fait chuter la couverture de 87 % à 61 % passe la CI sans bruit. Le commentaire du workflow assume ce choix (« Palier audit 2026-10-04 (mesuré 62 %) ») mais le chiffre de référence a 6 semaines et n'a pas été relevé.
- **Recommandation (S)** : mesurer la couverture réelle et relever le seuil à 85 % (ou au palier mesuré moins 2 points), puis ratchet.

---

### P2

---

**P2-1 — Deux skips dépendent de fichiers gitignorés (`logs/`)**

- **Fichier** : `tests/test_tool_schema_strict.py:307` et `:318`
- **Preuve** : `pytest.skip(f"dump {self.DUMP} absent (logs/ est gitignoré)")` avec `DUMP = "logs/free400_msg_323ba97cfd00-2b7.json"` (`:247`).
- **Impact** : ces 2 tests ne s'exécutent **jamais** en CI ni sur un clone neuf. Le contrat « les schémas wire sont un sous-ensemble strict » et « la normalisation est idempotente » n'est vérifié que sur la machine qui possède le dump.
- **Recommandation (S)** : committer un dump anonymisé dans `tests/fixtures/` et retirer le skip.

---

**P2-2 — `test_db_wal.py` : 4 tests conditionnés à la présence d'une DB locale**

- **Fichier** : `tests/test_db_wal.py:11-17, 23, 36, 54` — `pytest.skip("no DB file (fresh clone)")`
- **Impact** : le tuning WAL/mmap/busy_timeout (F-M4) n'est vérifié que sur une machine qui a déjà tourné. Le test `test_db_wal_mode` accepte de plus `("wal", "delete", "memory")` — donc il passe quelle que soit la valeur, y compris sans WAL. **Il ne peut pas échouer.**
- **Recommandation (S)** : créer une DB temporaire et vérifier que le code d'ouverture applique bien WAL ; durcir l'assertion à `mode == "wal"`.

---

**P2-3 — Skip saisonnier dans `test_date_bounds.py`**

- **Fichier** : `tests/test_date_bounds.py:57` — `@pytest.mark.skipif(GMTOFF != 7200, reason="exact values assume UTC+2 (summer)")`
- **Impact** : le test est vert en été, skippé en hiver. Le comportement testé (bornes de date en UTC+2) n'est donc vérifié que la moitié de l'année.
- **Recommandation (S)** : forcer le fuseau via `monkeypatch` sur `time.timezone`/`time.altzone` plutôt que de dépendre du fuseau système.

---

**P2-4 — Skip sur erreur d'infrastructure de test**

- **Fichier** : `tests/test_rotation_n_stations.py:60` — `pytest.skip(f"FakeVPNManager not mockable N={n}: {e}")`
- **Impact** : le test skippe parce que son propre double est cassé (`unsupported operand type(s) for /: 'NoneType' and 'str'`). Un skip sur exception de setup est un test qui a échoué et se tait.
- **Recommandation (S)** : corriger le double ; si N=2 n'est pas un cas valide, retirer le paramètre.

---

**P2-5 — Skip permanent déguisé en condition**

- **Fichier** : `tests/test_catalog_model_routing.py:63` — `pytest.skip("ce modele porte desormais une surcharge : choisir un autre temoin")`
- **Impact** : le témoin choisi n'est plus valide et personne n'en a choisi un autre. Le test ne s'exécutera plus jamais.
- **Recommandation (S)**.

---

**P2-6 — Duplication : `test_protocol_matrix.py` vs `test_e2e_protocol_matrix.py`**

- **Fichiers** : `tests/test_protocol_matrix.py` (654 lignes, 33 tests) vs `tests/test_e2e_protocol_matrix.py` (2 292 lignes, 36 tests)
- **Preuve** : les 69 noms de tests sont **disjoints** (`shared names: 0`). Ce n'est donc pas de la duplication de tests, mais une **frontière floue** : les deux fichiers couvrent la « matrice protocole » à deux niveaux (unitaire vs e2e). Le fichier e2e est le plus gros de la suite (2 292 lignes).
- **Recommandation (M)** : clarifier la frontière dans les docstrings, ou fusionner.

---

**P2-7 — Duplication de helpers entre fichiers de test**

- **Preuve** : `grep -rhoE "^def _[a-z_]+\(|^def [a-z_]+\(|^class [A-Z][A-Za-z]*"` sur `tests/test_*.py` :
  - `def _app(` × **36** (avec des signatures différentes : `test_config_routes.py:29`, `test_proxy_routes.py:12`, …)
  - `class FakeManager` × **18**
  - `def _pool(` × 15, `def _candidate(` × 14, `def _body(` × 12, `class FakePool` × 10
  - `def free_env(monkeypatch)` × **4** (définitions distinctes : `test_free_leg_effort_probe_isolation.py:265`, `test_free_leg_effort_recompute.py:167`, `test_free_leg_protocol_parity.py:135`, `test_invariant_a0.py:173`)
  - `def test_token_rejection(` × 5, `def test_routes_are_registered(` × 7
- **Impact** : maintenance — corriger `FakeManager` exige 18 éditions. Un `tests/_doubles.py` partagé (à l'image de `tests/_tls_helpers.py`, utilisé par 2 fichiers) résoudrait cela.
- **Recommandation (M)**.

---

**P2-8 — Couplage inter-fichiers de test (20 imports croisés)**

- **Preuve** : `grep -rn "^from test_\|from test_free_multi_attempt import" tests/*.py` → 20+ occurrences. Exemples : `test_apply_station_count.py:21-22` importe depuis `test_vpn_freshness` et `test_vpn_stack_nstation` ; `test_graceful_aurora.py:19-20` depuis `test_vpn_freshness` ; `test_no_valid_keys_guard.py:37` depuis `test_free_multi_attempt`.
- **Impact** : renommer `test_vpn_freshness.py` casse 8 fichiers. Ce couplage est **volontaire et documenté** (`pyproject.toml:17-24` a deux `per-file-ignores` F811/F401 pour ce pattern), mais il crée un ordre de collecte implicite.
- **Recommandation (M)** : extraire `FakeVPNManager`/`_cfg` dans `tests/_doubles.py`.

---

**P2-9 — `tests/test_config_audit.py` : 56 s pour 5 tests, sans mémoïsation**

- **Fichier** : `tests/test_config_audit.py` (les 5 tests appellent `audit.auditer()` indépendamment)
- **Preuve** : top 20 des plus lents — 5 des 7 premières places, cumul 56,7 s sur 527 s (**10,7 % de la suite**).
- **Cause** : `audit.auditer()` parcourt tout le dépôt en AST à chaque appel. 5 appels = 5 parcours.
- **Recommandation (S)** : fixture de session `@pytest.fixture(scope="session")` qui mémoïse le résultat, ou `lru_cache` dans `config_audit.auditer()`.

---

**P2-10 — `.coverage` périmé et trompeur**

- **Fichier** : `.coverage` (11 nov 01:36), portée `free/qualification` uniquement (2 751 statements).
- **Impact** : `coverage report --data-file=.coverage` affiche « TOTAL 87 % » qui n'est **pas** la couverture du projet — seulement celle de `free/qualification`. Un lecteur pressé conclurait que tout le projet est à 87 %.
- **Recommandation (S)** : gitignorer `.coverage` ou documenter sa portée.

---

### P3

- **P3-1** — `tests/integration/` est un répertoire **vide**. Soit le supprimer, soit y placer les tests d'intégration annoncés.
- **P3-2** — `tests/test_coverage_boost.py` : nom révélateur (« coverage boost »), contenu = 5 tests d'import (`test_docker_events_import` avec son `or True`, `test_app_packages_import`). Ces tests mesurent la couverture, pas le comportement. `test_docker_events_import` est de plus écarté par `-k "not docker"` (P0-2).
- **P3-3** — `tests/test_tool_compat.py` (cf. P1-2) : risque de consommation de quota réel si lancé directement.
- **P3-4** — `scripts/gate.ps1:7` : le commentaire documente `pytest -k "not docker"`, propageant l'erreur de P0-2 dans la documentation.
- **P3-5** — Aucun `conftest.py` dans `tests/unit/`, `tests/contract/`, `tests/integration/`. Un seul conftest pour 489 fichiers (15 fixtures `autouse`).
- **P3-6** — `pip list | grep -E "randomly|order|xdist|timeout|forked"` → **aucun plugin d'ordre aléatoire ni de timeout**. Les 6 385 tests tournent dans l'ordre de collecte. `conftest.py` documente **4 incidents d'ordre** corrigés par des fixtures `autouse` (`_reset_free_runtime_globals`, `_reset_global_429`, `_reset_free_model_rr`, `_reset_web_caches`). Le risque résiduel n'est pas mesuré.
- **P3-7** — `tests/fixtures/` ne contient qu'`error_classes.yaml` ; `tests/fixtures/protocol_corpus/` est référencé comme **inexistant** dans le docstring de `tests/test_e2e_protocol_matrix.py:38`.

---

## 6. Inventaire des tests morts / désactivés / flaky

### 6.1 Tests qui ne peuvent pas échouer

| Fichier:ligne | Pattern | Preuve |
|---|---|---|
| `tests/test_cache_anthropic_conformance.py:247` | `assert ... or True` | cf. §5 P1-1 |
| `tests/test_chaos_lifecycle.py:75` | `assert ... or True` | idem |
| `tests/test_coverage_boost.py:11` | `assert ... or True` | idem |
| `tests/test_lot7_ops.py:118` | `assert ... or True` | idem |
| `tests/test_tool_schema_normalization.py:623` | `assert ... or True` | idem |
| `tests/test_db_wal.py:31` | `assert mode.lower() in ("wal", "delete", "memory")` | Accepte toutes les valeurs possibles |
| `tests/test_db_wal.py:40-45` | `assert isinstance(cache, int)`, `assert isinstance(mmap, int)` | Vrai par construction de `sqlite3` |

**Recherches négatives (bon signe)** :
- `grep -rn "assert True" tests/*.py` → **0 résultat**
- Corps de test vide (`pass` seul) → **0 résultat**
- Test commenté (`# def test_`) → **0 résultat**

### 6.2 Tests désactivés / skippés

| Type | Nombre | Fichiers |
|---|---|---|
| `pytest.skip` conditionnel | 33 sites | cf. §2.3 pour les 5 effectifs à l'exécution |
| `@pytest.mark.skipif` | 7 sites | `test_date_bounds.py:57`, `test_free_wire_complet.py:112,123,132,141`, `test_workspace_alias.py:12,24`, `test_docker_autostart.py:17` |
| `pytest.importorskip` | 8 sites | `test_config_store.py:156,193`, `test_empty_stream_asgi.py:17`, `test_impersonate_support.py:27,31`, `test_public_egress_worker.py:151`, `test_public_proxy_mitm.py:19,20`, `test_sse_contract.py:26`, `test_usage_parity_anthropic.py:25` |
| `@pytest.mark.xfail(strict=False)` | 2 sites | `test_e2e_protocol_matrix.py:2206`, `test_plan_011_live.py:112` |
| `@pytest.mark.docker` | 9 tests | `test_chaos_lifecycle.py:189`, `test_plan_011_live.py:60,67,79,87,94,104,111` |

**Les 2 `xfail(strict=False)` — points d'attention** :
- `tests/test_e2e_protocol_matrix.py:2206` — **défaut réel documenté, non corrigé**. Le message est explicite : *« TROU 6 (défaut mesuré, non corrigé ici — opencode.py hors périmètre) : sur P2, la jambe free appelée depuis le rattrapage AllKeysPausedError part avec stream:true vers un endpoint free /responses ; la réponse SSE n'est pas exploitable par `_try_free_model_first` (opencode.py ~6593-6601) → le handler lit {} et rend 503 "All API keys exhausted" alors que l'amont free a répondu 200. »* Avec `strict=False`, ce test **passe silencieusement** si le défaut est corrigé comme s'il persiste. C'est un bug produit connu, gelé par un xfail. **P1** — la constitution XVII exige « un test de non-régression » pour chaque bug corrigé ; ici le bug **n'est pas corrigé** et le test ne le signalera pas quand il le sera.
- `tests/test_plan_011_live.py:112` — marqué `@pytest.mark.docker` **et** `xfail(strict=False)`. Doublement désactivé. Il teste « 6/6 stations NordVPN client-utilisables » — une exigence de la spec 011. Avec `strict=False`, il ne signalera jamais ni succès ni échec.

### 6.3 Tests flaky — risques identifiés

| Fichier:ligne | Pattern | Risque | Verdict |
|---|---|---|---|
| `tests/test_boot_phase3b_lazy.py:135` | budget de temps machine (1 600 ms) | **ÉLEVÉ — échoue 3/3 en isolation** | **P0/P1 réel** |
| `tests/test_config_store.py:208,219` | `time.sleep(0.05)` | Faible (timeout d'écriture) | Acceptable |
| `tests/test_dashboard_db_decoupling.py:106` | `time.sleep(0.4)` | Moyen — attente d'un état asynchrone | À surveiller |
| `tests/test_hot_reload.py:175,244` | `_time.sleep(0.01)  # mtime granularity NTFS` | **Windows-only** — la granularité mtime NTFS est ~15 ms, 10 ms peut être insuffisant | À surveiller |
| `tests/test_hot_reload_phase4.py:599` | `time.sleep(0.25)` | Moyen | À surveiller |
| `tests/test_proxy.py:1295` | `time.sleep(0.05)` | Faible | Acceptable |
| `tests/test_vpn_freshness.py:580` | `time.sleep(0.05)` | Faible | Acceptable |
| `tests/test_egress_public_adapter.py:318` | `time.sleep(0.02)` | Faible | Acceptable |
| `tests/test_boot_restore_async.py:26` | `time.sleep(0.01)` | Faible | Acceptable |
| `tests/test_db_migrate_isolation.py:86` | `time.sleep(0.002)` | Faible | Acceptable |
| `tests/test_gluetun_control_api.py:927` | `logs/vpn_state*.json` — chemin partagé | Moyen — isolé par les fixtures `autouse` ? | À vérifier |
| `tests/test_app_supervisor.py:39,62` | `HTTPServer(("127.0.0.1", 0))` | **Faible** — port 0 = éphémère, bonne pratique | OK |
| `tests/test_canary_runner.py:84` | `ThreadingHTTPServer(("127.0.0.1", 0))` | Faible — idem | OK |
| 83 fichiers | mention de `localhost`/`127.0.0.1` | **Majoritairement des constantes de test**, pas des connexions | OK après inspection |

**Recherches négatives (bon signe)** :
- `random` non seedé : **0 usage** dans `tests/`
- Ports fixes réels : **0** (les `port=4000` trouvés sont des arguments de doubles, `test_config_routes.py:20,29,105`)
- Réseau réel : seuls `test_plan_011_live.py:117` (`api.ipify.org`, docker-marked) et `test_nordvpn_sources_manifest.py:79,148` (URLs en **chaîne de caractères**, pas d'appel) sortent du mock. **201 fichiers** utilisent `respx`/`monkeypatch`/`MockTransport`.

**Verdict flaky global** : la suite est **remarquablement hermétique** pour 6 385 tests. Le conftest neutralise 15 états globaux (`_reset_free_runtime_globals`, `_reset_global_429`, `_reset_curl_pool`, `_reset_web_caches`, `_hermetic_client_reserve`, `_isolate_pool_state_file`, `_isolate_key_pauser_file`, `_isolate_ip_identity_vault`, `_freeze_config_hot_reload`, `_reset_free_model_rr`, `_reset_station_quality_registry`, `_clear_free_header_cache`, `_reset_module_logger_propagation`, `_reset_disable_mapping`, `_cap_retry_wait`), chacun documenté par l'incident qu'il verrouille. Le seul vrai problème de stabilité est le budget d'import.

---

## 7. Matrice « principe constitutionnel → gate automatisée »

| # | Principe | Gate automatisée existante ? | Fichier / preuve | Verdict |
|---|---|---|---|---|
| **I** | La disponibilité se mesure | Partiel | `tests/test_slo_latency.py`, `tests/test_station_slo_report.py`, `tests/test_incidents.py`, `tests/test_qualification_metrics.py` | **Partiel** — les fenêtres 1h/24h/7j/30j et les verdicts `ok/degraded/breached` ne sont pas tous couverts par un test nommé |
| **II** | Trois ressources, trois cycles de vie | Partiel | `tests/unit/test_ip_state.py` (9), `tests/test_egress_store.py`, `tests/test_chain_state.py` | **Partiel** |
| **III** | Une seule source de vérité | **Oui** | `tests/test_chain_state.py:1` (« registry.db — no JSON source of truth »), `tests/test_docs_drift.py`, `tests/test_repair_queue.py:11` | **Oui** |
| **IV** | Reset fournisseur = barrière dure | **Oui** | `tests/test_zero_429.py`, `tests/test_quota_audit_p0.py`, `tests/test_free_quota_vs_transient.py` | **Oui** |
| **V** | Une IP = une station | **Oui** | `tests/test_egress_store.py` (UNIQUE), `tests/test_station_isolation.py`, `tests/test_catalog_claims.py`, `tests/test_ip_registry*.py` | **Oui** |
| **VI** | Indépendance stations / transports | **Oui** | `tests/test_rotation_n_stations.py`, `tests/test_free_station_reliability.py`, `tests/test_profile_transport.py` | **Oui** |
| **VII** | Aucune preuve, aucun succès | **Oui** | `tests/test_empty_stream_asgi.py`, `test_empty_stream_counter.py`, `test_empty_stream_http_status.py`, `test_error_frame_parity.py`, `test_suspect_tiny_output.py`, `test_ip_registry_probe.py` | **Oui — bien couvert** |
| **VIII** | Un seul réparateur | **Oui** | `tests/test_repair_queue.py`, `tests/test_qualification_repair_adapter.py`, `tests/test_station_allocator_supervisor.py` | **Oui** |
| **IX** | Le score ordonne, les portes décident | **Oui** | `tests/test_ip_registry_scoring.py`, `tests/test_qualification_scoring.py` | **Oui** |
| **X** | Dégradation explicite, free-only | **Oui** | `tests/test_free_leg_effort_recompute.py`, `tests/test_graceful_aurora.py`, `tests/test_capacity.py`, `tests/test_leg_status.py` | **Oui** |
| **XI** | Fallbacks multiples | **Oui** | `tests/test_fallback_correlation.py`, `tests/test_geo_direct_fallback.py`, `tests/test_b2_stream_double_failover.py` | **Oui** |
| **XII** | Toute option GUI a un effet réel | **Partiel — 8 % du périmètre** | `tests/test_option_map.py` — `SCOPED_SECTIONS = ("egress_catalog",)` → 109 clés / 1 341 ; 17 `data-option-map` / 184 contrôles | **P1 — cf. P1-3** |
| **XIII** | Données traçables, migrables, sauvegardées | **Oui** | `tests/test_db_archive.py`, `tests/test_db_cleanup_batched.py`, `tests/test_claims_migration.py`, `tests/test_db_migrate_isolation.py`, `tests/test_quota_ledger*.py` | **Oui** |
| **XIV** | Modularité, strangler, ≤1 500 lignes | **Oui** | `tests/test_qualification_module_size.py` (vérifié : 9 passed), `tests/test_architecture_reuse.py`, `tests/test_dependency_graph` via `quality_gate.py:182` | **Oui** |
| **XV** | Multiplateforme, matrice CI Win/Linux | **NON** | `ci.yml:10` `runs-on: ubuntu-latest` seul ; `grep windows\|matrix\|macos` → **0 résultat** ; `platform_audit` invoqué nulle part | **P0 — cf. P0-1, P0-3** |
| **XVI** | Aucune fenêtre intempestive | **Oui** | `tests/test_platform_audit.py` (3 tests), `tests/test_no_visible_spawn.py` (6,55 s) | **Oui** — mais pas comme étape CI nommée |
| **XVII** | Fiabilité/perf/qualité/modularité | **Partiel** | `config_audit --strict` (exit 0 vérifié) ✅ ; `test_option_map.py` ✅ (4 tests) ; `quality_gate.py` (couverture 85 % sur `free/qualification`) ✅ ; **budget d'import en échec** ❌ ; **zéro flaky** ❌ (budget machine) | **P0/P1 — cf. §3** |
| **XVIII** | Une IP se brûle au premier format non conforme | **Oui** | `tests/test_free_body_conformite.py` (753 lignes), `tests/test_free_official_payload.py`, `tests/unit/test_wire_profile.py`, `tests/unit/test_wire_admission.py`, `tests/test_tool_schema_normalization.py`, `tests/unit/test_tool_policy.py`, `tests/test_free_wire_complet.py` | **Oui — le mieux couvert** |

**Gates nommées par `constitution.md:328-330`** :

| Gate exigée | Existe | Exécutée en CI | Exécutée localement |
|---|---|---|---|
| `pytest -k "not docker"` | oui | ✅ (avec `-m`, correct) | ⚠️ `gate.ps1` avec `-k` (**P0-2**) |
| `config_audit --strict` | oui (`scripts/config_audit.py`) | ✅ `ci.yml:64` | ✅ `gate.ps1` non — `quality_gate.py:158` |
| `platform_audit --strict` | oui (`scripts/platform_audit.py`) | ❌ **non** | ❌ **non** (**P0-3**) |
| `test_option_map.py` | oui | ✅ (via pytest global) | ✅ |
| `npm run check` (si UI) | à vérifier | ❌ non trouvé dans `ci.yml` | — |
| Matrice CI Windows/Linux | ❌ | ❌ | — (**P0-1**) |

---

## 8. État de la CI vs exigences constitutionnelles

### 8.1 Ce que la CI exécute (`.github/workflows/ci.yml`, job unique `lint-test`, `ubuntu-latest`, timeout 25 min)

| Étape | Bloquant ? | Ligne |
|---|---|---|
| `ruff check .` | ✅ | :28 |
| `ruff format --check .` | ❌ `continue-on-error: true` | :35-36 |
| `mypy` (19 fichiers prod) | ✅ | :41-48 |
| `mypy .` (progressif) | ❌ `continue-on-error: true` | :50-51 |
| `python scripts/ci_contracts.py` | ✅ | :57 |
| `python scripts/config_audit.py --strict` | ✅ | :64 |
| `pytest tests/test_zero_429.py test_client_reserve.py test_reserve_settings.py` | ✅ | :70 |
| `pytest -q -m "not docker" --cov --cov-fail-under=60` | ✅ | :76 |
| `python scripts/quality_gate.py --with-soak` | ✅ | :81 |
| Assets pré-compressables | ✅ | :96-112 |
| Budget d'import du boot (< 1200 ms) | ✅ | :113-129 |
| httpx différé hors du boot | ✅ | :130-137 |
| `pip-audit --desc --local` | ✅ | :139 |
| `gitleaks` | ✅ | :143 |
| `docker compose config \|\| echo skipped` | ❌ **avalé par `\|\|`** | :148 |
| `gunicorn --check-config \|\| echo skipped` | ❌ **avalé par `\|\|`** | :150 |

### 8.2 Écarts avec la constitution

| Exigence constitutionnelle | Réalité CI | Sévérité |
|---|---|---|
| XV : « matrice CI Windows/Linux » | **`ubuntu-latest` seul** | **P0** |
| XV : « un test spécifique à un OS est marqué **et a une contrepartie ou une justification écrite** » | `test_docker_autostart.py` (Windows-only, 7 tests) **n'a pas de contrepartie Linux** ; `test_workspace_alias.py` (jonctions NTFS) non plus. Justification écrite absente | **P1** |
| XVII : « `platform_audit --strict` » dans les gates obligatoires | Absent de `ci.yml` | **P0** |
| XVII : « Zéro test instable toléré » | 1 test échoue 3/3 (budget machine) ; 2 `xfail(strict=False)` non budgétés | **P0/P1** |
| XVII : « Zéro option morte : `option-map` à 0 ligne manquante » | Périmètre 109/1341 clés | **P1** |
| `ci_contracts.py` — « le gate CI ne se vide pas » | Protégé par `test_ci_contracts_gate.py` ✅ | OK |
| Contrats d'alignement : 330 tests, ~5 s | Vérifié comme bloquant ✅ | OK |

### 8.3 Ce qui est exemplaire

- **`scripts/ci_contracts.py` + `tests/test_ci_contracts_gate.py`** : le gate liste 15 contrats en dur, et un test vérifie que **chaque fichier déclaré existe**. C'est exactement la protection qu'il faut contre un gate qui pourrit en silence. Les 90 fichiers de `quality_gate.QUALIFICATION_TESTS` existent tous (vérifié : `missing: 0`).
- **`tests/test_constitution_rules.py`** : vérifie que la constitution déclare la bonne version, que les principes XV–XVIII sont présents et que **leurs gates exécutables existent** (`core/process.py`, `scripts/platform_audit.py`, `tests/test_platform_audit.py`, `tests/test_option_map.py`). C'est un méta-test de la constitution — rare et précieux.
- **`tests/test_no_real_config_write.py`** : garde statique (AST) qui empêche tout test d'écrire le `config.yaml` du dépôt. Documente un incident réel (2026-10-10) et explique pourquoi la vérification est statique et non horloge.
- **`tests/test_no_docker_exec_hot_path.py`** : vérifie par AST que le chemin chaud n'importe jamais `subprocess`/`shlex`.
- **`conftest.py`** : 15 fixtures `autouse`, chacune documentée par l'incident qu'elle verrouille (`_reset_global_429` cite même la commande de reproduction de l'échec d'ordre).

---

## 9. Zones non auditées / incertitudes

1. **Couverture de code réelle du projet** : non mesurée. Le `.coverage` présent ne porte que sur `free/qualification` (87 %). Mesurer la couverture complète exigerait de relancer la suite avec `--cov=.` (~10 min) — faisable mais coûteux. **Toutes les analyses de couverture de ce rapport sont statiques par nommage**, donc optimistes (un module « mentionné » n'est pas un module testé).
2. **Non-déterminisme d'ordre** : aucun plugin d'ordre aléatoire installé. Les 6 385 tests tournent dans l'ordre de collecte. Le conftest documente 4 incidents d'ordre corrigés par des fixtures `autouse` ; l'hypothèse qu'il n'en reste aucun n'est **pas vérifiée**. Un run `pytest -p randomly` (à installer) trancherait.
3. **Tests Windows non exécutés ici** : `test_docker_autostart.py` (7 tests) et `test_workspace_alias.py` (2 tests) ont été collectés et comptés dans les 6 385, mais je n'ai pas isolé leur résultat du run complet. Ils tournent sur cette machine (win32) mais pas en CI.
4. **`free/qualification/`** : couverture 87 % connue (rapport du 11 nov, périmé de 9 jours). Je ne l'ai pas re-mesurée.
5. **Le contenu de `free/clients/fixtures/*/request.json`** : vérifié comme consommé par `tests/contract/test_client_families.py` (8 clients × 2 tests = 16 des 25 tests du fichier). Je n'ai pas validé que les corps sont conformes au profil officiel (c'est l'objet de la spec 003, pas de cet audit).
6. **`scripts/` (69 modules non testés)** : je les ai classés « attendu : outils opérateur » sur la base de leur nom et de leurs docstrings. Je n'ai pas vérifié individuellement qu'aucun n'est importé par le chemin de production. Contre-exemple connu : `scripts/fresh_chain.py` **est** importé par `scripts/fresh_chain_supervisor.py` et testé — donc « dans `scripts/` » n'implique pas « outil jetable ».
7. **`.github/workflows/`** : un seul fichier. Aucun workflow de release, de scheduled run ou de sécurité au-delà de `gitleaks`.
8. **Les 46 tests désélectionnés par `-k "not docker"`** : je les ai identifiés par nom. Je n'ai pas vérifié lesquels échoueraient si exécutés.

---

## Annexe — Commandes de reproduction

```bash
# Collecte
python -m pytest --collect-only -q                    # 6385/6393 (8 deselected)
python -m pytest --collect-only -q -v                 # 6385 node ids

# Suite complète avec rapport machine
python -m pytest -q --junitxml=.artifacts/results.xml

# L'échec, en isolation (3/3)
python -m pytest tests/test_boot_phase3b_lazy.py::test_opencode_import_budget -q

# Mesure directe de l'import
python -c "import time;t=time.perf_counter();import opencode;print('MS: %.0f' % ((time.perf_counter()-t)*1000))"

# Écart -k vs -m
python -m pytest --collect-only -q -v | wc -l              # 6385
python -m pytest --collect-only -q -v -k "not docker" | wc -l  # 6339  → 46 perdus

# Gates nommées par la constitution
python scripts/config_audit.py --strict     # exit 0 (0 dead / 0 missing)
python scripts/platform_audit.py --strict   # exit 0 (279 fichiers, 0 violation) — NON branché en CI
python -m pytest tests/test_option_map.py -q  # 4 passed

# Recherches négatives
grep -rn "windows\|matrix\|macos" .github/workflows/     # 0 résultat
grep -rn "platform_audit" --include="*.yml" --include="*.ps1" --include="*.sh"  # 0 résultat
grep -rn "assert True" tests/*.py                        # 0 résultat
grep -rn "crashguard\|app-crash" tests/*.py              # 0 résultat
```
