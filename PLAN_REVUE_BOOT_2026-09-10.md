# Revue du « Plan — Démarrage proxy <5s API + <3s tray » — état au 10/09/2026 11:00

**Plan source :** `C:\Users\julie\.claude\plans\pourquoi-au-demarage-du-curious-ripple.md`
(présenté et accepté le 10/09 à 00:25 locale, session `03cac024-4187-43d6-b3aa-5ff0541a63f4`)

**Base de comparaison :** `HEAD = 110d587` « perf(boot): chemin import vers listen non-bloquant (DB, tiktoken, rich, docker) » — 10/09 10:14.
Commits postérieurs au plan : `110d587`, `7323db5` (golden), `9af3934`/`febea1e` (VPN, sans lien boot).

**Méthode :** lecture du plan ligne à ligne, vérification dans le code (`opencode.py`, `config/`, `dashboard/`, `vpn/`, `free/`, `gui/`, `.github/`), mesures `python -X importtime` + 3 chronométrages `import opencode`, tests boot rejoués, inspection de la session Claude qui a produit le plan.

---

## 1. Verdict en une ligne

**Phases 0, 1 et 2 : faites et commitées. Phase 3 : faite à ~60 % (tiktoken, rich, client httpx, curl_cffi lazy) — les items `httpx`/`config`/`vpn.manager` du todo « 3b » n'ont jamais été commencés. Phases 4 à 9 : partiellement ou pas faites, la Phase 8 (chantier DB) est le plus gros reste.**

Et un point qui change les priorités : **l'objectif chiffré de la Phase 3 (`import opencode` < 800 ms) est déjà atteint — 544 à 595 ms mesurés — sans la Phase 3b.** Le gain résiduel identifié est de ~63 ms, pas de 2–4 s.

---

## 2. Tableau de synthèse

| Phase | Objet | Statut | Preuve |
|---|---|---|---|
| 0 | Instrumenter (`BOOT_T0`, jalons, `/healthz` `/readyz` `/startupz`) | ✅ **Fait** (manque jalons GUI) | `opencode.py:325,328,2695-2735` |
| 1 | Lifespan non-bloquante + probes | ✅ **Fait** | `opencode.py:1935-2564` |
| 2 | Supprimer les 2 full scans DB au boot | ✅ **Fait** | `opencode.py:463-501,1015-1081` ; `tests/test_boot_db_phase2.py` |
| 3 | Lazy imports | 🟡 **Partiel (~60 %)** | tiktoken/rich/httpx-client/curl_cffi faits ; `import httpx`, `config/__init__`, `vpn.manager`, `_UA_BY_IMPERSONATE` **non faits** |
| 4 | Budgets quotas parallèles + SWR | 🟡 **Partiel** | parallèle + fond faits ; SWR/`X-Cache: STALE` absents |
| 5 | Static précompressé au build | ❌ **Non fait** | compression runtime toujours en place |
| 6 | Docker/VPN « degraded » en fond | 🟡 **Partiel** | déport en fond fait ; flags CLI, retry expo, 503 `Retry-After` VPN absents |
| 7 | GUI : tray < 3 s + webview différée | ❌ **Non fait** | `mgr.start()` toujours avant `run_gui()` |
| 8 | Rotation DB + compteurs incrémentaux | ❌ **Non fait** | DB live = **5,86 Go** |
| 9 | Runtime Windows + garde-fous CI | ❌ **Non fait** | `Config(...)` inchangé, CI sans gate perf |

---

## 3. Détail phase par phase

### Phase 0 — Instrumenter ✅ FAIT

- `BOOT_T0 = time.monotonic()` (`opencode.py:325`) + `log_boot_phase()` (`:328`, log + retour de l'élapsed).
- Jalons posés : imports/config (`:391`), DB ouverte sans scan (`:471`), migrate+canary bg (`:499`), compteurs restaurés (`:1057`), tiktoken lazy (`:358`), entrée lifespan (`:1938`), pré-yield (`:2561`), serveur écoute (`:13851`), listen confirmé `__main__` (`:13961`).
- Probes : `/healthz` (`:2695`, aucune I/O), `/readyz` (`:2700`, `200 {ready, degraded, boot_s}` + header `X-Degraded: warming`, jamais 503 global), `/startupz` (`:2733`).

**Reste :**
1. **Jalons GUI manquants** : `tray shown` et `webview ready` du plan ne sont pas instrumentés (aucun `log_boot_phase` dans `gui/`).
2. **Aucune mesure de boot réelle n'a été enregistrée** (le plan demandait 3 runs Docker chaud + Docker froid, notés). Voir § 5, point opérationnel : l'instance en cours ne sert même pas ces routes.

### Phase 1 — Lifespan non-bloquante + probes ✅ FAIT

Tout ce que le plan listait est parti en tâche de fond, avec gates :

| Élément | Emplacement |
|---|---|
| `yield` après config minimale + `app.state._boot_ready` | `:1939-1941`, `:2561-2564` |
| Toggle « Use Balance » 6 s → fond | `:1950-1967` |
| `ensure_docker_running(timeout=60)` → fond + gate `shared_state.docker_ready` | `:1979-1994` |
| Reconcile orphelins → fond (`shared_state.reconcile_task`) | `:2140-2158` |
| Fan-out stations (stagger 5 s, `Semaphore(2)`) → fond, ordre reconcile→start préservé | `:2167-2218` |
| Watcher docker events → fond | `:2225-2242` |
| Quota `_startup_fetch` (4 s) → fond | `dashboard/quota.py:738,759` |
| Restore compteurs → thread daemon | `:1066-1081` |
| Migrations + canary → fond post-ready | `:474-501`, `:2558` |
| `ServerManager.start` attend le vrai `server.started` | `:13844-13848` |
| Annulation des tâches au shutdown | `:2582-2584` |

Tests dédiés : `tests/test_boot_lifespan_nonblocking.py`, `tests/test_boot_fanout.py`, `tests/test_boot_restore_async.py` (15 tests rejoués ✅ verts).

**Reste :**
1. **Deux lectures disque inline sur le chemin pré-yield** : `config.yaml` (`yaml.safe_load(open(...))`, `:2003-2005`) et `credentials.env` (`:2007-2029`). Le plan demandait explicitement « une seule lecture `config.yaml` (supprimer la double relecture lifespan) » — toujours là. Coût faible (config.yaml = 11,4 Ko) mais c'est du synchrone bloquant mal placé.
2. **Construction de la flotte VPN en pré-yield** (`:2040-2128` : `SharedRotationState`, N × `VPNManager`, `FreeIPPool`, moteur latence). Vérifié : `VPNManager.__init__` (`vpn/manager.py:944+`) ne fait **aucun** subprocess, donc c'est peu coûteux — mais le plan Phase 6 voulait la flotte **après** `ready`. À trancher : la garder (dépendances chaudes dès le 1er hit VPN) ou la déporter.
3. Shutdown : la liste de cancel (`:2582`) couvre `_balance_task`, `_docker_task`, `_watcher_task`, `_db_migrate_task` — **pas** `_boot_fanout_task` ni `reconcile_task`.
4. Pas de `503 Retry-After` sur les routes VPN-dépendantes pendant le warmup (le plan le prévoyait en Phase 1 et Phase 6).

### Phase 2 — Supprimer les 2 full scans DB ✅ FAIT

- `init_requests_schema_fast()` : PRAGMAs + `CREATE TABLE IF NOT EXISTS` seulement (`opencode.py:459-471`, `app/db/__init__.py`). Les `ALTER`, les index et le canary `COUNT(*)` sont dans `migrate_and_canary()` appelé par `_db_migrate_bg()` en `to_thread` après `ready` (`:474-501`, sérialisé par `_db_commit_lock`).
- `_restore_token_counters()` lit sur une **connexion RO dédiée** (`file:...?mode=ro`, `:1024`) dans un **thread daemon** (`:1066-1081`), avec `_token_counters_stale` exposé sur `/v1/models` (`:10887`).
- Vérifié : la DB fait **5,86 Go** (+ WAL 4,1 Mo) et l'ouverture au boot ne la scanne plus. C'est le gain le plus tangible du chantier.

**Reste :** rien sur cette phase — le reste du sujet DB est la Phase 8.

### Phase 3 — Lazy imports 🟡 PARTIEL (le cœur du todo « 3b »)

#### Ce qui est fait

| Item | Emplacement | Mesure |
|---|---|---|
| `tiktoken` lazy (singleton `_get_encoding()`) | `opencode.py:340-358`, `app/protocol/mapping.py:47-53`, usage `:8431` | tiktoken **absent** de l'importtime ✅ |
| `rich` lazy (`_rich(name)` à l'appel) | `dashboard/display.py:12-36` | plus tiré par `dashboard.display` ✅ |
| Client/transport httpx construits lazy | `opencode.py:1258-1285` (`_client`/`_transport` = `None` jusqu'au 1er `_ensure_http_client()`) | ~256 ms économisés (commentaire source) |
| `curl_cffi` lazy | `opencode.py:1297-1307` | ~80 ms économisés |

#### Ce qui reste — les 3 items du todo interrompu

**3b-1 — `import httpx` lazy dans `opencode.py` (builder + annotations) : NON FAIT.**
`import httpx` est toujours en tête de module (`opencode.py:28`) et sert les annotations `httpx.AsyncClient | None` (`:1264-1265`) et `_build_shared_http_client()` (`:1269-1285`).

Coût réel mesuré — et **la raison pour laquelle cet item vaut la peine**, que le plan n'avait pas identifiée :

```
import httpx seul                     = 151 ms
  dont httpx._main                    =  62,7 ms   ← machinery CLI
         rich.console                 =  19,6 ms
         rich.progress                =  12,6 ms
         click                        =  10,5 ms
         pygments                     =   0,4 ms
Dans la chaîne de opencode : httpx    =  81,1 ms (self 0,5 ms)
```

Autrement dit : **`import httpx` tire `httpx._main`, donc `rich` et `click`, à l'import** — exactement ce que la Phase 3 croyait avoir supprimé en rendant `display.py` lazy. Gain attendu : **~63 à 81 ms**.

**3b-2 — `vpn.manager` / `config` lazy : NON FAIT (et pour partie inutile).**

| Sous-item du plan | État | Mesure / remarque |
|---|---|---|
| `config/__init__.py` PEP 562 `__getattr__` | ❌ **Non fait** — 101 lignes de réexport eager (`config/__init__.py:1-49`) | `config.settings` self = 51,7 ms |
| `get_settings()` cachée | ❌ inexistant (aucun `def get_settings`) | — |
| `config/settings.py:385-386` hors import | ❌ non traité (store + I/O toujours à l'import) | — |
| `config/geo.py` import `vpn.manager` | ❌ toujours top-level (`config/geo.py:24-28`), hors `TYPE_CHECKING` | `vpn.manager` = **4,3 ms** cumulé |
| `config/settings.py:23-27` import `vpn.manager` | ❌ idem | idem |
| `from vpn.manager import _UA_BY_IMPERSONATE` | ❌ toujours top-level (`opencode.py:4049`), consommé `:4098` et par `tests/test_invariant_a0.py` | négligeable |
| `free/__init__.py:10` tire `vpn.manager` | ⚠️ **Faux besoin** — `free` n'est **pas** importé au boot par `opencode.py` ; il n'arrive que dans le lifespan (`:2040`) et dans les shims | `free` **absent** de l'importtime |

**Conclusion sur 3b-2** : la seule partie qui compte vraiment est `config/__init__.py` (51,7 ms de `config.settings` self + câblage). Les imports de `vpn.manager` valent 4,3 ms — le plan les surestimait. `free` est un non-sujet.

**3b-3 — Vérifier non-régression + `importtime < 800 ms` : OBJECTIF DÉJÀ ATTEINT.**

```
python -X importtime -c "import opencode"   → opencode cumulé = 559 547 µs
python -c "... import opencode"             → 595 ms / 544 ms / 557 ms (3 runs)
```

L'objectif < 800 ms est donc **déjà satisfait**. Le reste du coût n'est plus dans les imports lourds évitables, mais dans le **code module-level d'`opencode.py`** (self ≈ 459 ms : parsing YAML, ouverture DB, caches de clés) et dans la chaîne FastAPI/Pydantic (≈ 240 ms cumulés, incompressible sans changer de framework).

**Recommandation :** faire 3b-1 (`import httpx` sous `TYPE_CHECKING` + `import httpx` dans `_build_shared_http_client`/`_ensure_http_client`) — gain mesuré ~63 ms, risque faible, et ça supprime le dernier import lourd évitable. Faire `config/__getattr__` (PEP 562) **seulement si** on veut de la marge : 15-20 ms pour un chantier à risque (réexports masqués, mypy). **Abandonner** les items `vpn.manager`/`_UA_BY_IMPERSONATE`/`free` du 3b-2 : ROI ~0.

### Phase 4 — Budgets quotas parallèles + SWR 🟡 PARTIEL

- **Fait** : `_startup_fetch` lance `fetch_model_limits()` et `fetch_available_models()` en `asyncio.gather` sous `wait_for(4.0)` chacun, en `create_task` non bloquant (`dashboard/quota.py:715-766`) ; le toggle balance est déporté en fond avec `wait_for(6.0)` (`opencode.py:1950-1967`).
- **Reste** :
  - pas de **SWR** (`max-age=60, SWR=300`) ni de header `X-Cache: STALE` sur les quotas. Les `X-Cache: HIT/MISS` présents dans `opencode.py` (8689, 8856, 9614…) concernent le **cache de réponses**, pas les quotas ;
  - pas de budget unique `asyncio.timeout(2.5)` : ce sont toujours deux `wait_for(4.0)` + un `wait_for(6.0)` séparés ;
  - pas de client `AsyncClient` unique partagé entre les deux fetches de démarrage.

Valeur résiduelle : faible depuis que la Phase 1 a sorti ce chemin du boot (0 s sur le chemin critique). À faire « quand on touche au dashboard », pas en priorité.

### Phase 5 — Static précompressé au build ❌ NON FAIT

- `_precompress_static_assets()` compresse **au démarrage** en `zlib.compressobj(6, ...)` (`dashboard/routes/static.py:17-43`), appelé depuis `dashboard/api.py:1441` et passé au `StaticCacheMiddleware`.
- **Aucun** `.gz`/`.br` généré au build, **aucun** `scripts/precompress.py`, **aucune** dépendance `brotli`, pas de `PrecompressedStaticFiles` (c'est un middleware maison `StaticCacheMiddleware`).

Reste : script de build + service `.br > .gz > origine` avec `Content-Encoding`/`Vary` + `Cache-Control: immutable` sur les assets hashés. Gain ~1–3 s au boot, 2 h de travail.

### Phase 6 — Docker/VPN « degraded » en fond 🟡 PARTIEL

- **Fait** (le principal) : plus aucune attente docker sur le chemin import → listen ; `ensure_docker_running` + reconcile + watcher + fan-out sont en tâches de fond, avec `shared_state.docker_ready` comme gate et `/readyz` qui publie `degraded: {docker: warming|unavailable, vpn: starting}`.
- **Reste** :
  - flags `--no-docker`, `--no-vpn`, `--ready-timeout` : **absents** (`argparse` ne déclare que `--no-gui`, `--gui`, `--port`, `opencode.py:13916-13922`) ;
  - pas de « un seul `docker ps` timeout 2 s en `to_thread` » pré-listen (choix différent : rien du tout pré-listen — acceptable, voire mieux) ;
  - pas de notif tray « Docker réveil en cours » (seul `notify_geo` existe, `gui/tray.py:7-16`) ;
  - pas de retry exponentiel explicite documenté pour la sonde docker (le `ensure_docker_running` a sa boucle interne) ;
  - **pas de `503 Retry-After` sur les routes VPN-dépendantes** pendant `degraded` — le plan y tenait (Phase 1 + Phase 6) ; aujourd'hui les routes répondent leur erreur métier habituelle.

### Phase 7 — GUI : tray < 3 s + webview différée ❌ NON FAIT

C'est le **plus gros gain perçu restant** et il n'a pas été touché :

- `__main__` démarre le serveur **puis** la GUI : `mgr.start()` (`opencode.py:13960`) → `run_gui(mgr, HOST, PORT)` (`:13978`). L'inversion prévue par le plan n'a pas eu lieu.
- `gui/__init__.py:4-7` : `TrayApp(...)` puis `app.run()` ; `TrayApp.run()` finit sur `self._icon.run()` bloquant (`gui/tray.py:158-176`), sans `run_detached` ni `ServerManager.start()` en fond.
- Pas de splash `tkinter overrideredirect`, pas de `Popen` de `gui/_webview_main.py` à la 1re ouverture.
- **Point positif déjà en place** : `import webview` est local à `gui/_webview_main.py:53`, et `gui` n'est importé que dans la branche `use_gui` (`:13965-13976`) → `--no-gui` n'importe ni `pystray` ni `webview` (exigence du plan respectée).

### Phase 8 — Rotation DB + compteurs incrémentaux ❌ NON FAIT — **le plus gros reste**

- DB live : **`logs/requests.db` = 5,86 Go**, WAL 4,1 Mo. Objectif du plan : live < 100 Mo.
- Pas de `requests-YYYY-MM.db`, pas de table `token_counters_daily`, pas de `scripts/archive_db.py`, pas de `VACUUM INTO` d'archivage.
- Ce qui existe est **autre chose** : `scripts/rotate_db.ps1` / `.sh` (outil **manuel** one-shot, `[plan 30/08 Lot B1]`) et `_db_maintenance_loop()` in-app (`opencode.py:2424-2469`) = dimanche 03:00, checkpoint TRUNCATE + purge `weekly_purge_days` (défaut 90) + VACUUM.
- Conséquence directe : `_restore_token_counters()` reste un `GROUP BY` sur des millions de lignes (désormais en fond, mais toujours lent), et le VACUUM hebdo travaille sur 6 Go.

Le plan annonçait 3–5 jours pour cette phase — c'est le poste de travail structurel qui reste.

### Phase 9 — Runtime Windows + garde-fous ❌ NON FAIT

`Config(...)` actuel (`opencode.py:13826-13839`) vs plan :

| Plan | Actuel |
|---|---|
| `timeout_graceful_shutdown=30` | `=5` |
| `lifespan="on"` explicite | absent (défaut `auto`) |
| `access_log=False` | `log_level="info"`, access log actif |
| `--log-level warning --no-server-header` en prod | absents |
| `PYTHONDONTWRITEBYTECODE=1` + `compileall` | absents |
| CI : fail si `import opencode` > 1,5 s ou `/healthz` > 5 s | **aucun step perf** dans `.github/workflows/ci.yml` (ruff, mypy, pytest/cov, pip-audit, gitleaks, compose, gunicorn) |

`loop=uvloop`/`http="httptools"` : déjà en place (avec fallback `asyncio` sur win32) ✅.

---

## 4. Écarts entre le plan et la réalité (à corriger dans le plan)

1. **Phase 3 annonçait « gain ~2–4 s »** : le gain réel disponible était ≤ 0,4 s, et il est déjà largement pris. Le plan surestimait les imports lourds parce qu'il n'avait pas mesuré.
2. **`free/__init__.py:10` (item du 3b-2) n'existe pas comme problème** : `free` n'est pas sur le chemin d'import de `opencode`.
3. **`vpn.manager` (item du 3b-2)** : 4,3 ms cumulés, pas un levier.
4. **Le vrai import lourd restant n'était pas dans la liste du plan** : `httpx._main` → `rich` + `click` (62,7 ms), tiré par `import httpx`.
5. **Le coût dominant du boot n'est plus les imports** mais (a) le code module-level d'`opencode.py`, (b) l'ordre GUI (serveur avant tray), (c) la taille de la DB.
6. La Phase 2 est celle qui a le mieux fonctionné : sur 6 Go, c'était le vrai gain.

---

## 5. Points opérationnels constatés pendant la revue

1. **L'instance qui tourne ne sert pas les probes.** Port 4000 = `pythonw.exe` pid 25540, démarré **10/09 00:01** — donc **avant** le commit `110d587` (10:14). Conséquence vérifiée en live : `/healthz`, `/readyz`, `/startupz` → **404** ; `/v1/models` et `/api/config` → 200. Autrement dit, le travail des Phases 0–2 est commité mais **pas actif** : un redémarrage est nécessaire avant toute mesure de boot réelle.
2. **Le todo « 3b » n'a jamais été exécuté.** Dans la session qui a produit le plan, la dernière écriture de fichier est à **02:24** locale (`opencode.py`, L3488 du transcript) ; tout ce qui suit (02:24 → 10:26) est de la lecture/analyse, et la session se termine par `stop_reason: error` à **10:26:28** locale, en pleine phase de réflexion « inventorying httpx usages in opencode ». Les trois items 3b-1/3b-2/3b-3 sont donc restés au stade `in_progress`/`pending` sans une seule édition.
3. **Le commit `110d587` vient d'une autre fenêtre Claude** (session `04de3bbc…`, son message de commit liste explicitement « tiktoken et rich en lazy ; docker ensure, reconcile, fan-out stations et watcher en taches de fond » — sans mention de `httpx`/`config`). Utile à savoir si tu cherches d'où vient le travail : ce n'est pas la session du plan qui a committé.
4. **Tests boot rejoués** : `test_boot_db_phase2.py` + `test_boot_fanout.py` + `test_boot_lifespan_nonblocking.py` → **15 passed**. `test_boot_reconcile.py` échoue en `PermissionError: [WinError 5]` sur le répertoire temporaire du sandbox — **artefact d'exécution, pas une régression** (les 14 tests concernés passent hors sandbox).

---

## 6. Ce qu'il reste à faire, par ordre de valeur

| Priorité | Chantier | Gain | Effort | Pourquoi maintenant |
|---|---|---|---|---|
| **P0** | **Redémarrer l'instance** et mesurer le boot réel (`/healthz`, `/readyz`, jalons `[boot+Xs]`) | rend visible tout le travail des Phases 0–2 | 5 min | rien n'est mesurable tant que le vieux process tourne |
| **P1** | **Phase 7 — inversion tray/serveur** | gain perçu ~3–5 s (tray < 3 s) | ~1 j | le plus gros gain utilisateur restant, jamais commencé |
| **P2** | **Phase 8 — rotation DB mensuelle + `token_counters_daily`** | pérennise −20 s et débloque le VACUUM | 3–5 j | 5,86 Go, chantier structurel |
| **P3** | **Phase 3b-1 seule** (`import httpx` lazy) | **~63 ms** mesurés | 1–2 h | supprime le dernier import lourd évitable ; risque faible |
| **P4** | **Phase 9 — petit lot** (`access_log=False`, `timeout_graceful_shutdown=30`, gate CI `import opencode` < 800 ms / `/healthz`) | marge + garde-fou anti-régression | ~2 h | cheap, et évite de re-perdre les gains acquis |
| **P5** | **Phase 6 — les compléments** (flags `--no-docker/--no-vpn/--ready-timeout`, `503 Retry-After` VPN, notif tray docker) | confort de debug + contrat degraded | ~0,5 j | utile au prochain incident docker |
| **P6** | **Phase 5 — precompress au build** | ~1–3 s | 2 h | indépendant, sans risque |
| **P7** | **Phase 4 — SWR quotas** | 0 s sur le chemin critique (déjà déporté) | 0,5 j | cosmétique depuis la Phase 1 |
| ❌ | **3b-2 (hors `config/__getattr__`)** — `vpn.manager`, `_UA_BY_IMPERSONATE`, `free` | ~4 ms | — | **à abandonner** : ROI nul, mesuré |

---

## 7. Rappel des objectifs chiffrés du plan vs état

| Étape | Plan (cible) | Mesuré aujourd'hui |
|---|---|---|
| Baseline | ~60–100 s (Docker chaud) | non mesuré (ancien process) |
| 1 lifespan yield | ~15 s | fait, non mesuré en live |
| 2 skip scans DB | ~5–8 s | fait (DB 5,86 Go, ouverture sans scan) |
| 3 lazy imports | imports **< 800 ms** | **544–595 ms → ✅ objectif atteint** |
| 5 precompress | < 5 s API | non fait |
| 6 docker fond | < 5 s API même Docker froid | fait (probe sortie du chemin critique) |
| 7 tray immédiat | tray < 3 s, API < 5 s | non fait |
| 8 rotation DB | < 4 s stables | non fait (DB 5,86 Go) |

---

*Revue produite le 10/09/2026 à partir de `HEAD = 110d587`, working tree propre (hors `PLAN_AUDIT_LOGS_2026-09-10.md`, `scripts/db_audit.py`, `scripts/log_audit.py` non suivis).*
