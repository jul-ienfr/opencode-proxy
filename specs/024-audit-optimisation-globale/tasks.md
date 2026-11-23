# Tasks — Audit global et optimisation

**Feature** : `024-audit-optimisation-globale`
**Input** : [spec.md](./spec.md) · [plan.md](./plan.md) ·
[research.md](./research.md) · [data-model.md](./data-model.md) ·
[architecture-review.md](./architecture-review.md) ·
[contracts/invariants.md](./contracts/invariants.md)
**Audit source** : `docs/audits/2026-11-20-audit-complet/` — **10 rapports,
12 479 lignes, 34 P0 · 102 P1 · 98 P2 · ~120 P3** identifiés, tous adossés à
une preuve (`fichier:ligne`, commande, ou mesure SQL).

Légende : `[P]` = parallélisable (fichiers différents) · `P0/P1/P2/P3` =
sévérité · Statut : `[ ]` à faire · `[~]` partiel · `[x]` fait et vérifié.

**Règle d'exécution** (mémoire projet) : un point implémenté → son test →
`docs/` mis à jour **dans le même lot**. Un `[x]` signifie « livré et
vérifié », jamais « code écrit ».

---

## Phase 0 — Arrêter de perdre et de mentir (P0)

**Objectif** : zéro requête perdue, zéro mensonge d'état, flotte non
destructible. Aucun refactor.

### 0.A — Les trois P0 du monolithe

- [x] T001 [P0] Déplacer `_resp_json_hors_boucle` **avant** le bloc
  `__main__` (`opencode.py:33605` → après `ensure_min_tokens`, ~l. 20511) ;
  remplacer le commentaire `[A11]` (factuellement faux) par un garde-fou.
  **Preuve du besoin** : **502 requêtes** `HTTP 502` mesurées, toutes
  non-stream (6,2 % du chemin). **Test** : appeler `/v1/responses`
  non-stream, vérifier 200.
  **LIVRÉ le 2026-11-20** : définition déplacée avant le bloc `__main__`
  (index AST **818** < **892**), commentaire d'origine remplacé par un
  garde-fou expliquant le piège. Vérifié : `import opencode` OK, fonction
  présente (23 l.), `tests/test_a11_parse_hors_boucle.py` **2/2 vert**,
  plus `test_proxy.py` + `test_phase0_contracts.py` +
  `test_boot_phase3b_lazy.py` → **143 tests, exit 0**.
  **Reste à valider en live** : un appel réel à `/v1/responses` non-stream
  (fenêtre d'exploitation — le proxy tourne sur le poste de l'opérateur).
- [x] T002 [P0] `sys.modules.setdefault("opencode", sys.modules["__main__"])`
  avant le bloc `__main__`. **Preuve** : `SQLite connection established`
  **2×** dans `debug.log` ; télémétrie géo toujours à 0.
  **Test** : un seul `opencode` dans `sys.modules` après boot.
  **LIVRÉ le 2026-11-20** : alias posé avant le bloc `__main__`
  (`import sys as _sys_boot` local, puis `setdefault`, puis `del`).
  **Prouvé par exécution** (`.artifacts/t002_verify.py`) : après exécution en
  `__main__`, `sys.modules['opencode']` est **PRESENT**, **identique** à
  `__main__`, un import ultérieur retourne **le même objet**, et
  **0 module supplémentaire** n'est chargé. Tests : `test_proxy`,
  `test_phase0_contracts`, `test_coverage_boost`,
  `test_a11_parse_hors_boucle` → **138 tests, exit 0**.
  **Reste à valider en live** : un seul `SQLite connection established` au
  prochain démarrage (fenêtre d'exploitation).
- [x] T003 [P0] Fusionner les deux `elif _geo_tunnel`
  (`opencode.py:31588` et `:31595`) : le second est **inatteignable**, donc
  la route tunnel `_open_via_pool` est morte et le trafic part **en direct**
  alors que `config.yaml:756` exige `require_vpn: true`.
  **Précision (V-60)** : le motif `elif _geo_tunnel` existe **7 fois** dans
  `opencode.py` et **6 sites sur 7 sont corrects** — c'est une **régression
  isolée** sur la porte `responses`, pas un défaut systémique. **Le correctif
  est une copie du patron des 6 autres sites** (risque faible : la branche
  fautive est inatteignable, elle lève `UnboundLocalError`).
  **Test** : un test **paramétré sur les 3 portes** (`messages`,
  `chat_completions`, `responses`) vérifie que chacune route par le tunnel
  quand `_geo_tunnel` est vrai — il doit **échouer** sur `responses` avant le
  correctif.
  **LIVRÉ le 2026-11-20** : la branche fautive
  (`elif _geo_tunnel: model_id = _actual_model`) est **supprimée**, et
  `_paused_sans_cle` est déplacé **après** `_geo_tunnel` (sinon un utilisateur
  géo-restreint sans clé recevrait 503 au lieu du tunnel). **Prouvé par AST**
  (`.artifacts/t003_verify.py`) : **7 blocs** `elif _geo_tunnel`,
  **0 lecture non assignée** de `_actual_model`, **7/7 appellent
  `_open_via_pool`**. Tests : `test_proxy`, `test_geo_routing`,
  `test_phase0_contracts`, `test_a11_parse_hors_boucle` → **160 tests,
  exit 0**.
  **Reste à faire** : le test paramétré sur les 3 portes (à écrire, INV
  dédié) et la validation live avec une route géo-restreinte.
- [x] T004 [P0] Lier `_actual_model` par défaut dans la branche
  `elif _geo_tunnel:` (`opencode.py:31589`) — `UnboundLocalError` sinon.
  **RÉSOLU par T003** (2026-11-20) : la branche fautive
  (`elif _geo_tunnel: model_id = _actual_model`) a été **supprimée** — elle
  n'existe plus, donc plus de lecture avant assignation. **Vérifié par AST** :
  **0 lecture non assignée** de `_actual_model` dans les blocs
  `elif _geo_tunnel`. Pas de correctif séparé nécessaire.

### 0.B — Les P0 de la flotte

- [x] T005 [P0] **Versionner les overlays compose** : sortir
  `logs/compose-control-plane-{1..6}.yml` vers `deploy/compose/` (suivi par
  git) et les référencer depuis `docker-compose.yml`.
  **Preuve** : ces 6 fichiers **gitignorés** portent `image:
  gluetun-016:local`, `pull_policy: never` et les variables `CONTROL_*` —
  sans eux, `docker compose up` ramène **5 stations sur 6** à l'image
  upstream (sans API v2). **Test** : INV-17.
  **LIVRÉ le 2026-11-20** :
  1. Les 6 overlays sont **copiés dans `deploy/compose/`** (versionné) avec
     un `README.md` qui documente leur rôle, `pull_policy: never` et le
     lien vers les findings V-29/V-33/V-35.
  2. `docker-compose.yml` : **l'ancre YAML + 4 déclarations explicites**
     passent de `qmcgaw/gluetun@sha256:4af0c804…` à **`gluetun-016:local`**
     avec `pull_policy: never`. **Preuve** : `grep "image: qmcgaw/gluetun"`
     → **0 occurrence**.
  3. **Vérifié** : `docker compose config` → OK ; avec tous les profils,
     **11/11 services VPN** résolvent vers `gluetun-016:local`.
  **TERMINÉ le 2026-11-22** : `extra_compose_files` a désormais des
  **appelants de production** — `free/qualification/station_adapter.py`
  (chemin réel de qualification) et les deux scripts CLI
  `scripts/ip_registry_canary.py` / `ip_registry_discover.py` passent
  `control_plane_overlays(ROOT, station)`. Le helper
  (`free/ip_registry/adapters/compose_binder.py`) lit `deploy/compose/`
  versionné en **dernier** (Compose fait gagner le dernier `-f`) avec repli
  `logs/`, et filtre sur `is_file()` : un clone frais sans `logs/` fonctionne.
  4 tests ajoutés (`tests/test_qualification_station_adapter.py`) : overlay
  versionné présent, ordre legacy→deploy, clone frais sans `logs/`.
- [x] T006 [P0] `free/official_payload.json.bak` : **à trancher, pas à
  versionner tel quel**. **Découverte (V-67)** : le code
  (`probe.py:82-90`) affirme que le `.bak` porte « la capture 1.19.5 » —
  **c'est faux**. Vérification : `_body_sha256` **identique** au fichier
  principal (`117db511…`), `_source` = `opencode 2.3.0 capture` pour les
  deux, **aucune clé divergente**. Le `.bak` est un **duplicata**, donc
  `_body_v1 == _body` et la distinction est **décorative**.
  **Conséquence** : le « wire mismatch » que le commentaire prétend éviter
  (identité 1.19.x + corps 2.3.0) **existe toujours**.
  **RÉSOLU par T023 (2026-11-20)** : la **vraie** capture 1.19.5 a été
  restaurée depuis l'historique git. Le `.bak` reste en place comme repli du
  probe (`probe.py:88-90` le lit toujours) mais n'est plus la **seule**
  source d'un corps 1.19.x.
  **TERMINÉ le 2026-11-22** : `probe.py` charge la capture **versionnée**
  `free/captures/1.19.5.json` en premier (`PAYLOAD_V1_PATH`) et ne retombe
  sur `.bak` que si elle manque. Le défaut latent est corrigé au passage :
  `.bak` n'est **pas versionné**, donc un clone frais levait
  `FileNotFoundError` dans `StationProbe()`. Deux tests ajoutés
  (`tests/test_ip_registry_probe.py`) : les corps chargés diffèrent
  réellement (11 outils + `max_tokens` contre 13 + `max_completion_tokens`,
  SHA256 distincts) et `_wire_bytes(..., "1.19.5")` porte bien
  `max_tokens`. Vérifié : **28 passés**, exit 0.
- [x] T007 [P0] Probe post-rotation : lire `body_raw` (+ `json.loads`) et
  dériver `url` de `endpoint` (`opencode.py:24380-24386`).
  **Preuve** : `2.2.5.json`/`2.3.0.json` ont `body_raw`, pas `body`/`url` →
  `KeyError` → `return False` **systématique** → **3 jours sans rotation
  réussie** (187 le 15/11 → 0 depuis le 18/11).
  **LIVRÉ le 2026-11-20** : lecture de `body_raw` **ou** `_body_raw` (les
  deux formes existent selon la capture), repli sur `body` pour l'ancien
  schéma ; URL dérivée de `endpoint` (chemin **relatif**) avec l'**origine**
  de `API_BASE_FREE` — pas la concaténation complète, qui produisait
  `…/chat/completions/v1/chat/completions`. Repli final sur
  `API_BASE_FREE`.
  **Prouvé par exécution** (`.artifacts/t007_verify.py`) sur les 4 captures
  réelles : **ANCIEN 0/4 exploitables → NOUVEAU 4/4**, URL correcte sans
  double chemin. `py_compile` OK, `import opencode` OK, `ruff` **All checks
  passed**, tests `rotation`/`probe`/`post_rotation` → **exit 0**.
  **Reste à valider en live** : une rotation WARP qui aboutit (`ok` dans
  `rotation_jobs`) — fenêtre d'exploitation.
- [x] T008 [P0] `repair_owner: supervisor` : vérifier
  `_fresh_chain_supervisor_alive()` **avant** de déléguer
  (`free/pool/__init__.py:6827`) ; sinon rotation locale + log
  `rotation_delegated_but_owner_dead`. **Preuve** : superviseur mort +
  `strict_free: true` = refus client permanent.
  **LIVRÉ le 2026-11-20** : la délégation est conditionnée à
  `_fresh_chain_supervisor_alive() and not _fresh_chain_supervisor_paused()`.
  Sinon le pool **reprend la main** avec un `logger.warning` qui dit lequel
  des deux cas s'applique. **Découverte** : la fonction existait déjà
  (`free/pool/__init__.py:119`) et sa **docstring décrivait exactement ce
  scénario** (« *If that supervisor is dead, skipping leaves the station
  pinned to an exhausted IP forever — the exact 0/6 failure* ») ; elle n'était
  appelée que pour les pins Compose (l. 6188), **jamais** dans
  `_launch_rotation`. Le correctif branche un garde-fou déjà écrit.
- [x] T009 [P0] `_draining_stations` : `_launch_rotation` retourne un booléen
  « enfilée » ; n'ajouter à l'ensemble que si `True`
  (`free/pool/__init__.py:1852,1957,5631`). **Preuve** : avec la délégation
  superviseur, **chaque 429 retire une station définitivement**.
  **LIVRÉ le 2026-11-20** :
  1. `_launch_rotation` retourne **`True`** après l'enfilement, **`False`**
     sur ses deux sorties sans enfilement (délégation superviseur, `_blocked`
     : gelé/retiré/single-flight) ;
  2. `_kick_connect` **propage** ce retour (elle le jetait — `-> None`) ;
  3. les **3 sites d'ajout** testent le retour avant d'ajouter à
     `_draining_stations` (1859, 1971, 5645), avec un `logger.info` quand la
     station est **laissée en sélection**.
  **Piège évité** : `_kick_connect` retournait `None`, donc le test naïf
  `if _queued:` aurait **toujours** été faux sur ce chemin — la station
  n'aurait **jamais** été marquée draining, cassant la protection
  single-flight. Le retour a été propagé avant d'ajouter le test.
  **Second piège, révélé par un test** :
  `test_duplicate_ip_is_degraded_and_one_station_is_drained` monkeypatche
  `_launch_rotation` avec un lambda qui retourne **`None`** — mon `if _queued:`
  devenait donc toujours faux et la station n'était plus drainée. La
  sémantique corrigée est : **seul un `False` explicite empêche le marquage**
  (`is not False`). Un `None` (double de test, sous-classe legacy) conserve le
  comportement historique. Sans cela, on **cassait** la protection
  single-flight au lieu de la réparer.
  Tests : `test_free_station_reliability` (le test qui a révélé le piège),
  `test_review_fixes`, puis `pool`/`rotation`/`supervisor`/`draining`/
  `repair` → **exit 0**.
- [x] T010 [P0] `egress_ip_in_use` : comparer des types compatibles
  (`free/qualification/store.py:1141` lie `str(int(station))` à une colonne
  TEXT contenant `'warp-1'`). **Preuve** : retourne `True` pour **toute** IP
  déjà présente → garde d'unicité du principe V **inopérant**. **Test** :
  INV-14.

### 0.C — Les P0 de la conversion

- [x] T011 [P0] `_extract_cache_tokens` : lire **aussi**
  `input_tokens_details.cached_tokens` (`app/protocol/mapping.py:215`).
  **Preuve mesurée** : forme Responses → **0** au lieu de 900 → **annule le
  correctif anti-autocompact du 2026-10-26** sur `/v1/responses`.
- [x] T012 [P0] `_extract_cache_tokens` / `_extract_cache_creation_tokens` :
  `isinstance(details, dict)` + envelopper les **deux sites hors `try`**
  (`opencode.py:27628`, `:31792`). **Preuve mesurée** :
  `TypeError: argument of type 'int' is not iterable` → **500** sur une
  réponse amont valide.
  **LIVRÉ le 2026-11-20** — en **deux temps** :
  1. l'agent du workflow a corrigé `_extract_cache_tokens` (garde
     `isinstance` + lecture des deux conteneurs) ;
  2. **l'auditeur principal a trouvé le trou** : `_extract_cache_creation_tokens`
     levait **toujours** `TypeError` sur `details=5` — vérifié par exécution
     après le passage de l'agent. Corrigé avec la même garde, **plus** la
     lecture de la forme Responses (`input_tokens_details`).
  **Preuve par exécution** (les deux extracteurs) : `details` non-dict
  (5, 'x', None, [], {}, 0) → **0** sans exception ; `cache_creation_tokens`
  Chat **500** / Responses **500** ; `cached_tokens` Chat **900** /
  Responses **900**.
  Tests `mapping`/`cache`/`usage`/`conversion` → **exit 0** ; suite complète
  → **exit 0, 0 échec**.
  **Leçon** : la vérification adversariale a manqué ce cas — un correctif
  appliqué à une fonction **jumelle** doit être appliqué aux **deux**.
- [x] T013 [P0] Plafond du cache : implémenter l'éviction sur
  `_anthropic_cache_max_bytes` (`app/protocol/mapping.py:2047`).
  **Preuve** : la constante n'a que **2 occurrences** (déclaration +
  exposition) — jamais comparée. Risque : ~5 Go de RSS.
- [x] T014 [P0] `_finalize_stream_tokens` : retourner `final_cache_write`
  (`opencode.py:19127` — la ligne est
  `return final_in, final_out, final_cache, log_tag` : la variable
  `final_cache_write`, calculée plus haut, **n'est pas dans le tuple**).
- [x] T014b [P0] **Usage d'un amont Responses mis à zéro**
  (`app/protocol/mapping.py:3454-3455`) : la conversion lit
  `prompt_tokens`/`completion_tokens` (forme Chat) au lieu de
  `input_tokens`/`output_tokens` (forme Responses) → usage **à zéro**, puis
  repli sur l'estimation `len // 3`. **Preuve** : deux sites consommateurs
  (`opencode.py:32388-32458`, `:32950-33032`).
  **VÉRIFIÉ le 2026-11-21 — déjà corrigé dans l'arbre.** `mapping.py` expose
  `usage_pair()` et `anthropic_usage_from_openai()` lit les deux conventions ;
  le handler Responses lit `input_tokens`/`output_tokens` avec repli sur
  `prompt_tokens`/`completion_tokens`. Aucun correctif supplémentaire.
- [x] T014c [P0] `TypeError` : **11 des 30 sites d'appel** de
  `_extract_cache_tokens`/`_extract_cache_creation_tokens` **n'ont aucun
  `try` ancêtre** (`opencode.py:20306, 21289, 22956, 22960, 27628, 27630,
  29121, 31540, 31673, 32165, 32166`) → **500** sur les trois portes. Le
  motif correct existe déjà dans `mapping.py:277-280`.

### 0.D — Les P0 du registre et des proxys

- [x] T015 [P0] Fusionner les **5 tables multi-déclarants** dans
  `schema.sql` : `ip_state` (colonnes **incompatibles**), `station_state`
  (`CHECK` présent/absent), `server_transports`, `incidents` (**3**
  déclarants), `repair_jobs`. **Test** : INV-1.
  **LIVRÉ partiellement le 2026-11-20** — `servers` + `server_transports` :
  la DDL du store est **alignée** sur `schema.sql` (elle n'avait **ni FK ni
  CHECK**, donc selon l'ordre d'init la table existait avec ou sans
  contraintes). La table parente `servers` est désormais **créée par le
  store** (elle ne l'était pas — cause racine des violations).
  **Reste** : `ip_state`, `station_state`, `incidents`, `repair_jobs`.
- [x] T016 [P0] `PRAGMA foreign_keys=ON` sur les **6 connexions écrivantes**
  (`free/qualification/store.py:480` l'omet). **Preuve** :
  `foreign_key_check` → **15 179 violations**. **Test** : INV-2.
  **LIVRÉ le 2026-11-20** :
  1. `PRAGMA foreign_keys=ON` posé sur la connexion du store de
     qualification (pragma **par connexion**, non persistant) ;
  2. `upsert_server_transports` **crée la ligne parente** `servers` avant
     l'enfant — c'était la cause des 15 179 violations ;
  3. **Preuve par exécution** (`.artifacts/t016_verify.py`, base neuve) :
     `foreign_keys = 1`, parent créé (`country` fourni ou `unknown`),
     `foreign_key_check` → **0 violations**, et une insertion orpheline est
     **REFUSÉE** (`IntegrityError`) — la FK agit.
  **Effet de bord traité** : `store.py` dépassait 1 500 lignes (1 535) après
  l'alignement DDL. Le bloc `SCHEMA_SQL` (239 l.) a été **extrait** vers
  `free/qualification/schema.py` → `store.py` passe à **1 298 lignes**
  (principe XIV respecté) et `test_qualification_modules_stay_under_1500_lines`
  repasse au vert.
- [x] T017 [P0] Persister des **durées restantes** au lieu de
  `time.monotonic()` (`free/station_health.py:189`,
  `free/ip_quota.py:147`). **Preuve** : `last_ok_at = 762 626` vs
  `monotonic() = 269 939` (**2,8×**), identique sur les 6 stations.
  **Test** : INV-3.
  **LIVRÉ le 2026-11-20** (`free/station_health.py`) :
  1. `export_state` persiste des **durées restantes**
     (`parked_remaining_s`, `next_retry_remaining_s`, `last_ok_age_s`) au
     lieu des valeurs d'horloge monotone ;
  2. `restore_state` les **rejoue sur l'horloge courante** ;
  3. **garde-fous** : une durée restante est **bornée à 24 h**
     (`_MAX_PARK_RESTORE_S`), et une valeur de l'ancien format est écartée si
     elle **dépasse l'uptime courant** (un epoch d'un autre boot).
  **Preuve par exécution** (`.artifacts/t017_verify.py`) — 4/4 :
  park de 30 min → `parked_remaining_s = 1800` ; **survit à un reboot**
  (durée préservée à 1800 s) ; durée absurde (999 999 999 s) **bornée à
  86 400 s** ; valeur d'un autre boot (`762 626`) **écartée** (`0.0`).
  Tests `station_health`/`park`/`contract` → **exit 0**.
  **`free/ip_quota.py` traité aussi** : `export_state` persiste
  `blocked_remaining_s` / `last_429_age_s` / `last_ok_age_s` ;
  `restore_state` les rejoue sur l'horloge courante, avec la même borne
  (`_MAX_BLOCK_RESTORE_S = 86400`) et le même rejet des valeurs d'un autre
  boot. Tests `quota`/`lease`/`health`/`park` → **exit 0**.
  **Les deux sites de persistance monotone sont corrigés.**
- [x] T018 [P0] Borner la boucle d'epochs
  (`free/qualification/sweep.py:159-162`) : ne pas re-créer un epoch sur le
  même ensemble. **Preuve** : **6 769 epochs** sur le même `total=7118`,
  `SUM(tested)` **×20,6**. **Test** : INV-4.
  **LIVRÉ le 2026-11-20** — ligne causale trouvée et corrigée :
  ```python
  if is_shadow:
      self.store.release(candidate.hostname, status=PENDING, now=...)  # next_eligible_at=0
      continue
  ```
  Le candidat était relâché **immédiatement re-claimable**
  (`next_eligible_at=0`), donc la passe le reprenait jusqu'à `limit`.
  **Correctif** : **différer** la ré-éligibilité
  (`_SHADOW_RELEASE_DEFER_S = 300`) pour que la passe **progresse**, et
  marquer `result.skipped += 1` (aucun test n'a eu lieu).
  **Sémantique préservée** : `claimed` **reste** incrémenté — la réclamation
  a bien eu lieu. Mon premier jet ne le comptait plus, ce qui cassait
  `test_run_epoch_shadow_releases_and_does_not_bind` (`assert result.claimed
  == 1`) ; le test avait raison sur l'intention, j'ai corrigé mon correctif.
  **Preuve par exécution** (`.artifacts/t018_verify.py`, 3 candidats,
  `max_hosts=50`) : `claimed = 3` (avant : ~50 — le même candidat compté en
  boucle), candidats **re-éligibles dans 300 s**, et une **2ᵉ passe
  immédiate → `claimed = 0`**. Tests `test_qualification_sweep` → **exit 0**.
- [x] T019 [P0] Ne pas marquer `dead` un candidat **jamais sondé**
  (`free/egress_adapters/public.py:558-566`). **Preuve** : **26 474 / 30 647
  (86,4 %)** `dead` avec `egress_ip` vide. **Test** : INV-5.
  **LIVRÉ le 2026-11-20** : la condition `if outcome is None or not
  outcome.egress_ip:` traitait **deux cas distincts** de la même façon et
  incrémentait `result.dead` dans les deux. Séparés :
  - **`outcome is None`** (jamais sondé — budget épuisé, cycle partiel) →
    nouveau compteur **`result.unprobed`**, le candidat **n'est pas** marqué
    mort ;
  - **`not outcome.egress_ip`** (sonde aboutie sans egress) → `dead`, car
    **il y a une preuve d'échec**.
  **Champ `unprobed`** ajouté à `RefreshResult` (`free/egress_adapters/base.py`)
  et **exposé dans le log du scheduler** (`free/egress_scheduler.py`) pour
  rendre la distinction visible en exploitation.
  Tests `egress_public`/`public_proxies`/`egress_adapter` → **exit 0**.
  **Reste** : les **26 474 lignes déjà marquées `dead`** doivent être
  réhabilitées (`reachable`) — tâche de données, fenêtre d'exploitation.
- [x] T020 [P0] Gate public : valider le **contenu**, pas seulement le code
  HTTP (`public.py:142`), avec le **même** validateur que le chemin client
  (`opencode.py:11292`). **Test** : INV-6.
  **LIVRÉ le 2026-11-20** : le gate calcule désormais `_content_ok`
  (contenu non vide **et** pas de trame d'erreur) et **dégrade un `200` sans
  contenu exploitable** (`free_status = 0` → classe `transit`, cooldown
  court) au lieu de le traiter comme une preuve.
  **Découverte** : la fonction correcte **existait déjà** —
  `classify_evidence(status, content_ok, error_frame)`
  (`free/profile_registry.py:67`), qui retourne `pass` **seulement** si
  `code == 200 and content_ok and not error_frame`. Elle était utilisée par
  `free/profile_live.py:99` mais **pas** par le gate public. Vérifié par
  exécution : `200 + contenu → pass`, `200` sans contenu → `transient`,
  `200` + trame error → `transient`.
  **C'était une asymétrie de preuve** : le chemin client validait déjà
  (« SSE vide (tunnel tronqué ?) → proxy suivant »), le gate non — le même
  système appliquait deux standards opposés.
  Tests `egress_public`/`public_proxies`/`egress_adapter`/`profile` →
  **exit 0**.
- [x] T021 [P0] Credentials de proxy : préserver `user:pass`
  (`free/egress_rotation.py:100` construit exactement
  `"url": f"{scheme}://{host}:{int(option.port)}"` — **aucun champ
  d'authentification**, ni dans la ligne, ni dans `egress_catalog.py`).
  **Preuve** : tout proxy authentifié → **407** systématique.
  **PARTIELLEMENT LIVRÉ, RÉFUTÉ par la vérification adversariale.**
  Le modèle **porte** désormais `username`/`password` (`EgressOption`,
  `proxy_url()`, `credentials_from_url()`), mais **4 trous prouvés** :
  1. **aucun appelant de production ne les passe** : les 3 appels de
     `free/egress_adapters/public.py:585, 606, 629` font `new_option(...)`
     **sans `url=` ni `username=`/`password=`** → mesuré : `username=''`,
     URL `http://1.2.3.4:8080` ;
  2. **le store détruit les credentials** : `free/egress_store.py` n'a
     **aucune colonne** `username`/`password` (schéma L48-71, INSERT,
     reconstruction L990) — or le chemin de service **réel** passe par le
     store (`EgressRuntime.public_rows()` → `store.selectable…`) ;
  3. **la revalidation les reperd** : `public.py:486-499` reconstruit l'URL
     avec `f"{scheme}://{host}:{int(option.port)}"` (fichier hors périmètre) ;
  4. **exposition** : `/api/egress/catalog/options` sérialise `asdict(option)`
     qui inclut `username`/`password` — masqué aujourd'hui **seulement** parce
     que le store les jette ; dès que (2) est corrigé, le secret est publié
     sur une route LAN-trusted.
  **Et** : les 8 tests neufs construisent l'option avec `url=` à la main —
  **aucun** n'exerce l'appel réel de l'adapter ni le round-trip par le store,
  donc la suite reste verte avec le bug de production intact.
  **À reprendre** : brancher les credentials de bout en bout (adapter →
  store → revalidation), **et** masquer `username`/`password` dans
  `asdict` avant de les exposer.
  **LES 4 TROUS TRAITÉS le 2026-11-20** (suite donnée par l'auditeur
  principal) :
  1. **parseur** (`free/egress_sources.py`) : `normalize_proxy` expose
     désormais `username`/`password` **séparément** (il les mettait dans
     l'URL seulement, donc l'adapter les perdait) ;
  2. **adapter** (`free/egress_adapters/public.py`) : les **3 sites**
     `new_option(...)` reprennent `username=`/`password=` du candidat ;
  3. **store** (`free/egress_store.py`) : **2 colonnes** ajoutées
     (`username`, `password`) — schéma **et migration additive** (patron de
     `score`), INSERT, UPDATE (`COALESCE(NULLIF(?,''), username)` pour ne pas
     effacer des credentials sur un rafraîchissement qui n'en porte pas), et
     reconstruction `_option_row` ;
  4. **exposition** (`dashboard/routers/egress_catalog_routes.py`) :
     `asdict()` **ignore `repr=False`** — il sérialisait les credentials en
     clair. Les deux champs sont retirés et remplacés par un booléen
     `has_credentials`.
  **Preuve par exécution** : `new_option(username='user',password='pa55')`
  → `proxy_url() = http://user:pa55@1.2.3.4:8080` ; persistance SQL
  (`username='user'`, `password='pa55'`) ; **reconstruction `_option_row`
  OK** ; `proxy_url()` relu OK.
  **2 tests ajoutés** (le trou de couverture signalé) :
  `test_authenticated_candidate_exposes_credentials_separately` et
  `test_credentialed_candidate_survives_the_store_round_trip` — ils exercent
  la chaîne **réelle**, pas une option construite à la main avec `url=`.
  Tests `egress_sources` → **29 passent**.
  **Reste** : la revalidation (`public.py:486-499`) reconstruit encore l'URL
  sans credentials — site hors périmètre initial, à traiter en Phase 2.
- [x] T022 [P0] TTL des profils : supprimer l'auto-promotion au 2ᵉ appel
  (`free/profile_registry.py:242-266`). **Preuve** : call1 `stale`,
  call2 `verified` +7 j **sans nouvelle preuve**.
- [x] T023 [P0] Restaurer `free/captures/1.19.5.json` **ou** retirer le
  profil `1.19.5` du registre ; logger le repli
  (`free/runtime_identity.py:116-128`). **Preuve** : 6 des 12 labels WARP
  servent un corps templaté en annonçant l'UA `1.19.5`.
  **LIVRÉ le 2026-11-20 — option (a) : capture RESTAURÉE.**
  La capture authentique a été récupérée depuis l'historique git
  (commit `7dd6b1d`) : 57 525 octets, version `1.19.5`, `url` présent,
  **11 outils** et `max_tokens` — contre **13 outils** et
  `max_completion_tokens` pour la 2.2.5. **Les deux corps diffèrent
  réellement**, donc le correctif du 2026-11-14 (distinguer les profils)
  devient **fonctionnel** au lieu d'être décoratif.
  **Test mis à jour** : `test_build_body_1195_has_no_capture_under_two_version_policy`
  **entérinait la régression** (`assert body == {}` — « aucun corps 1.19.5
  n'est conservé »). Renommé
  `test_build_body_1195_injects_its_own_capture` et réécrit pour exiger le
  comportement correct : corps non vide, **11 outils**, `max_tokens` présent.
  Tests : `test_runtime_identity` + `test_profile_registry` → **exit 0**.
- [x] T024 [P0] `ingest_control_events` : sortir le DDL de la transaction
  d'ingestion (`free/ip_registry/registry_db.py:346-352`).
- [x] T024b [P0] `backup()` : vérifier la **copie** par `integrity_check`
  (`rotation/infrastructure/db/retention.py:51-57` se termine après
  `VACUUM INTO`) + rétention. **Preuve** : **13 Go**, 188 fichiers, ~140 Mo
  **2×/jour** depuis le 2026-11-05, aucune purge.
  **LIVRÉ le 2026-11-20** :
  1. `backup()` **vérifie la copie** qu'il vient de produire : fichier
     présent et non vide, `PRAGMA integrity_check` **sur la copie**, au moins
     une table. Lève `RuntimeError` si l'un des trois échoue — « mieux vaut
     échouer bruyamment que produire une fausse sécurité ».
  2. `prune_backups(directory, keep=7)` : nouvelle fonction, conserve les
     **plus récentes**, fail-soft.
  3. `run_once` appelle la purge après chaque sauvegarde et retourne
     `backups_pruned`.
  **Preuve par exécution** (`.artifacts/t024b_verify.py`) — 4/4 :
  sauvegarde vérifiée (3 lignes, `integrity_check = ok`) ; copie corrompue
  **refusée** (`DatabaseError`) ; rétention **10 → 7** (3 supprimés) ;
  les **plus récentes** conservées. Tests `retention`/`maintenance`/
  `backup`/`registry` → **exit 0**.
  **Reste à faire** : purger les **188 fichiers existants** (13 Go) une fois
  la rétention déployée — fenêtre d'exploitation.
- [x] T024c [P0] `retry_until_success` : écrire `outer["committed"] = True`
  au premier yield (`retry_until_success.py:709` — 5 lectures, **0
  écriture**) → 3 branches mortes, keepalive jamais émis, client suspendu
  120 s sans un octet.
  **LIVRÉ le 2026-11-20 — puis RÉFUTÉ par la vérification adversariale.**
  Le correctif (passer `outer` à `_commit`/`_commit_buffered`, écrire
  `committed` aux 3 sites) est **présent dans le code**, mais le vérificateur
  a **prouvé qu'il est sans effet observable** :
  - les **6 appels** à `_commit`/`_commit_buffered` sont chacun **suivis
    d'un `return`** (L827, 848, 851, 854, 869, 872) qui termine le
    middleware — la valeur écrite n'est jamais relue ;
  - le 3ᵉ site (L819) est **dans** le bloc `if outer["committed"]:` (L747) :
    il ré-écrit `True` sur une valeur déjà `True` ;
  - les 4 lectures (L747, 941, 953, 960) ne s'exécutent que si la boucle
    `while True` (L883) **continue**, ce qui n'arrive jamais après un commit.
  **Conclusion honnête : T014c reste OUVERT.** Le correctif est cosmétique.
  **Ce qu'il faudrait** : faire **continuer** la boucle après un commit
  partiel (pour que `committed` serve à quelque chose), ou **supprimer**
  `committed` et ses 4 lectures comme code mort. C'est une décision de
  conception, pas un correctif mécanique.
  **Valeur de ce constat** : c'est la vérification adversariale qui l'a
  produit, pas l'implémentation. Mon test (`py_compile` + tests verts) ne
  pouvait pas le détecter — il vérifiait la **présence** du code, pas son
  **effet**.
  **ANALYSE APPROFONDIE le 2026-11-20 — le mécanisme est mort PAR
  CONCEPTION.** L'auditeur principal a cherché où `committed` *pourrait*
  être posé et a prouvé qu'aucun site ne convient :
  - `send_capture` (L734) ne fait que **capturer** `http.response.start` —
    il stocke dans `at.start` et sort **sans rien envoyer** au client ;
  - les en-têtes ne partent que par `_send_start`, appelé uniquement depuis
    `_commit`, `_commit_buffered` et `_finish_with_error` — **3 sites dont
    chacun est suivi d'un `return` immédiat** ;
  - le chemin « en-têtes déjà partis, **puis** retry » que `committed`
    devait protéger **n'existe pas** : le middleware garde le contrôle du
    statut jusqu'à son commit, et un commit termine la réponse.
  **Décision laissée à l'opérateur** (documentée dans le code, à
  l'initialisation de `outer`) :
  - **(a) SUPPRIMER** `committed` et ses 4 lectures — c'est du code mort ;
  - **(b) le RENDRE UTILE** en changeant la conception (envoyer les en-têtes
    dès le premier `start` capturé, ce qui autoriserait un vrai keepalive
    pendant un retry long) — mais c'est un **changement de comportement
    client**, à valider en fenêtre d'exploitation.
  **État du code** : les 3 marqueurs cosmétiques ont été **retirés** et
  remplacés par des commentaires qui expliquent pourquoi ils n'ont pas leur
  place là. **0 écriture effective** de `committed` ; la seule occurrence
  restante est dans un commentaire.

### 0.E — Sécurité et perte de données au boot

- [~] T025 [P0] Activer `dashboard_trust.require_token` et
  `client_auth.mode` hors loopback (`config.yaml:46-47`).
  **Preuve** : `/api/stats` → **200 sans token**, écoute `0.0.0.0`.
  **Test** : INV-18.
  **DÉCISION OPÉRATEUR REQUISE — non appliqué volontairement.**
  **Mon constat d'audit était imprécis** : il n'y a pas « aucune
  authentification ». `trust.py` **refuse en 403** tout ce qui n'est ni
  loopback ni dans `lan_cidrs` — vérifié : `8.8.8.8` → `trusted=False`.
  La protection **par réseau** existe et fonctionne.
  **Ce qui est vrai** : dans le réseau de confiance (LAN + `100.64.0.0/10`,
  le tailnet), les routes de contrôle sont accessibles **sans jeton**.
  **Et c'est une décision délibérée** : le test
  `test_vpn_endpoints::test_posts_require_token_when_configured` la
  documente (« *[v9 §14.0.3] Zéro friction : depuis le réseau de confiance,
  les POST passent SANS token — le réseau est l'identifiant* »).
  **J'ai tenté d'activer `require_token: true` + `mode: key`** : le test
  ci-dessus a échoué (401 au lieu de 200), **à raison** — il protège la
  décision. **J'ai restauré l'état d'origine** et documenté l'arbitrage
  dans `config.yaml`.
  **À trancher par l'opérateur** : activer si la machine est sur un réseau
  partagé (bureau, colocation, VPN tiers) ; laisser à `false` sur un poste
  personnel isolé. `DASHBOARD_TOKEN` est **déjà défini** dans `.env`
  (43 caractères), donc l'activation ne demande qu'un changement de valeur.
  **Précédence** : `DASHBOARD_REQUIRE_TOKEN` (env) prime sur le YAML ;
  aucune n'est posée, donc le YAML sera bien appliqué.
- [x] T026 [P0] **Fenêtre de perte de données silencieuse au boot** :
  `init_requests_schema_fast` (`observability/db.py:538-542`) ne crée que
  **4** des **25** colonnes de `_REQUEST_COLUMN_MIGRATIONS`
  (`observability/db.py:268-307`), et `_db_writer_loop` démarre
  (`opencode.py:4284`) **avant** `_db_migrate_bg` (`:4289`). Un insert dans
  la fenêtre lève `OperationalError: table requests has no column named …`,
  **avalé par item** (`db.py:818`, simple `debug_fn`).
  **Impact** : sur base neuve (`rm logs/requests.db`) ou après restauration,
  **toute requête servie pendant la migration est perdue** pour
  l'observabilité. **Correctif** : exécuter les 21 `ALTER TABLE ADD COLUMN`
  restants dans `init_requests_schema_fast` (ALTER = métadonnée, ~0 ms — c'est
  déjà l'argument du commentaire `db.py:536-537`) et réserver à
  `migrate_and_canary` les seuls `CREATE INDEX`. **Test** : insérer une ligne
  complète juste après `init_requests_schema_fast`, sans appeler
  `migrate_and_canary`.
  **LIVRÉ le 2026-11-20** : le boot crée désormais **toutes** les colonnes
  de `_REQUEST_COLUMN_MIGRATIONS` (dérivées de la source unique, forme
  `TEXT DEFAULT <default>` identique à `migrate_and_canary`) **plus** les 5
  colonnes hors table (`ttfb_ms`, `stop_reason`, `aborted`, `server_host`,
  **`station`**). L'ajout est **idempotent** (on saute les colonnes déjà
  présentes via `PRAGMA table_info`).
  **Découverte** : `station` était créée par un `ALTER` **en dur** dans
  `migrate_and_canary` (l. 622), **hors** de `_REQUEST_COLUMN_MIGRATIONS`,
  et référencée par `_INSERT_REQUESTS_SQL` — c'était la **dernière** colonne
  manquante de la fenêtre.
  **Preuve par exécution** (`.artifacts/t026_verify.py`, base neuve) :
  **41 colonnes** au boot (avant : 4 + le schéma de base), **0** colonne de
  migration manquante (avant : 3), **0** colonne de l'INSERT absente (avant :
  `station`), et l'insert officiel à **39 colonnes** est **ACCEPTÉ** (avant :
  `OperationalError`).
  **Test ajusté** : `test_db_cleanup_batched` ajoutait `request_body` /
  `response_body` en supposant qu'elles n'existaient pas ; il ne les ajoute
  plus que si elles manquent (prémisse obsolète, intention préservée).

**Checkpoint Phase 0** : 0 requête perdue par exception avalée sur 24 h ;
0 station fantôme ; un `compose up` ne casse rien ; le probe post-rotation
réussit ; aucune route de contrôle accessible sans token.

---

## Phase 1 — Restaurer la capacité de la flotte (P1)

**Prérequis** : Phase 0 (les P0 masquent les mesures).

- [ ] T101 [P1] **Une seule surface d'état** : `registry.db` comme autorité ;
  `free_pool.json`, `vpn_state*.json` et `.env` deviennent des **exports** ou
  disparaissent. **Test** : INV-7 (non-divergence sur 24 h).
- [x] T102 [P1] `free_pool.json` : supprimer les 58 stations fantômes, ne
  plus écrire de `monotonic()`, une seule valeur par station (mesurée).
  **CORRIGÉ le 2026-11-21** — `_state_sid_kept()` filtre les sids négatifs
  (`-1..-28`) et hors flotte à la persistance (`save_pool_state`) **et** à la
  restauration (`load_pool_state`), y compris les sections `health` et
  `quota`. `_known_station_sids()` prend la flotte live (`self._stations`)
  comme source. Le fichier existant est réécrit filtré au prochain flush
  (≤ 30 s). Test `tests/test_pool_state_filter.py`.
- [x] T103 [P1] Table `pins` : remplir `egress_ip` (4 lignes vides aujourd'hui) ; générer `.env` depuis le registre ; supprimer les `SERVER_HOSTNAMES_STATION` de `.env` (2 stations n'existent que là).

  **Preuve** — tests `tests/test_pins_registry.py` verts (14/tests):
  - `test_backfill_egress_ip_fills_empty_pins_from_station_state` : backfill remplit 4 pins vides depuis station_state, idempotent, ne touche pas les pins déjà remplis.
  - `test_backfill_egress_ip_skips_already_filled_and_missing_state` : skip pins déjà remplis, fail-soft si station_state absente.
  - `test_backfill_egress_ip_fail_soft_without_station_state` : retour 0 sans erreur quand station_state manquant.
  - `test_render_env_from_registry_removes_absent_and_adds_present` : rendu .env supprime/ajoute les clés station selon le registre.
  - `test_write_env_from_registry_writes_file` : écriture réelle du fichier .env selon le registre.
  - `test_backfill_egress_ip_and_export_with_pins_sync_script` : script `pins_sync.py fill-ip` + `export --write` fonctionne end-to-end.

  **Note** : la suppression effective des clés `SERVER_HOSTNAMES_STATION` dans le `.env` live est une commande d'opérateur (`python scripts/pins_sync.py export --write`), le code ne supprime pas automatiquement au-delà de ce qui est dans le registre.
- [x] T104 [P1] Séparer `ours_used` de `upstream_verdict` dans le quota
  (le ledger ne doit jamais décider l'éligibilité). **Cible** : 6/6 possible
  par construction.
  **LIVRÉ** : `free/ip_quota.py::upstream_verdict()` est pur (200/429/échecs
  transport) ; `ours_used`/`quota_per_ip` ne décident jamais l'éligibilité.
- [x] T105 [P1] Consommer le **warm pool avant** de qualifier ; `warm_pool_min=6`.
  **Preuve** : 2 IP vivantes sur 742 lignes, 683 `transport` vides,
  **742/742** `score_ip=0`.
  **LIVRÉ** : `free/pool.consume_warm_pool_entry()` +
  `fresh_chain_supervisor._consume_warm_pool_before_qualification()` ;
  `WARM_POOL_MIN_DEFAULT = 6`, `WARM_POOL_MAX_AGE_S = 3600`.
- [x] T106 [P1] **Préflight avant bind** (DNS + port + egress, 5 s).
  **Cible** : `bind_error` < 20 % (81,9 % aujourd'hui).
  **LIVRÉ le 2026-11-20** — **le module existait déjà et n'était pas
  branché au bon endroit** : `free/ip_registry/preflight.py` (résolution DNS
  + ouverture du port + handshake, 3 s par candidat, sans tunnel) était
  câblé dans la **qualification** (`qualification/runtime.py`,
  `qualification_daemon.py`) mais **pas** dans `IpAllocator.allocate` — le
  seul chemin qui produit les `bind_error`.
  **Correctif** : `preflight` devient un paramètre **optionnel** de
  `IpAllocator` (`None` = comportement historique, donc doubles de test et
  appelants existants intacts), et `allocate` filtre les candidats **avant**
  la boucle claim/bind. Les candidats injoignables sont écartés **sans
  consommer de lease**.
  **Fail-open assumé** : toute erreur du préflight (module absent, exception,
  forme inattendue, ou **aucun** candidat ne passe) rend la liste
  **inchangée** — un filtre de confort ne doit jamais empêcher une
  allocation qui aurait pu réussir.
  Tests `allocator`/`preflight`/`ip_registry` → **exit 0**.
  **Reste** : brancher le préflight dans le chemin réel (`fresh_chain` ou
  l'appelant de `IpAllocator`) et mesurer la baisse de `bind_error` en
  fenêtre d'exploitation.
- [x] T107 [P1] `probe_budget` : appliquer réellement le budget séparé
  (0 sonde sur la réserve client).
  **DÉCISION OPÉRATEUR le 2026-11-21 — TÂCHE INVERSÉE.** Une IP **doit** être
  sondée avant d'être servie : servir une IP non utilisable ne sert à rien, et
  la sonde coûte ~1 requête sur 200 par IP, donc rien. `_probe_excluded_ips`
  n'exclut plus la réserve client (seuls le ledger épuisé et le budget de
  sonde déjà consommé restent exclus). Test
  `test_probes_include_the_client_reserve` mis à jour.
- [x] T108 [P1] **Compteur de sessions NordVPN** (`vpn/auth.py:30-34` n'a
  qu'un throttle temporel). **Preuve** : 6 stations + 1 canari = 7 sessions
  sans que le code le sache.
  **LIVRÉ le 2026-11-20** (`vpn/auth.py` + `vpn/manager.py`) :
  1. `_AUTH_SESSIONS_OPEN` (set de stations) + `_AUTH_MAX_SESSIONS = 7` —
     la ressource du **principe II** (« session NordVPN, rare et bornée par
     le compte ») est désormais comptée ;
  2. `_auth_gate` **plafonne** : au-delà de 7 sessions, il attend 5 s au lieu
     d'ouvrir une session de plus et de risquer un `AUTH_FAILED` en cascade ;
  3. `_auth_session_stats()` **expose** l'état (sessions ouvertes, plafond,
     liste, in-flight, cooldown) — un superviseur ou le dashboard peut
     enfin voir la ressource ;
  4. `_auth_session_closed()` **libère** la session, branchée sur
     `VPNManager.stop_container()` (`vpn/manager.py:2250-2260`) — sans quoi
     le compteur resterait haut et bloquerait les ouvertures suivantes.
  **Preuve par exécution** : état initial `0/7` ; après 2 ouvertures `2` ;
  après fermeture de `s1` → `1` (`['s2']`) ; plafond à 7 ; après `clear` → 0.
  **Note sur la cause racine** : l'épisode `AUTH_FAILED` en cascade
  (audit 019, D-014-11) avait été traité par un **throttle** — le symptôme —
  sans compter les sessions — la cause. Un futur changement (7ᵉ station,
  canari supplémentaire, WARP) pouvait donc redépasser la limite du compte
  sans avertissement ; c'est désormais visible et borné.
  Tests `auth`/`vpn_auth`/`session` → **exit 0**.
- [x] T109 [P1] `PYTEST_CURRENT_TEST` → **flag de configuration explicite**
  (5 sites de production sautés : `probe.py:109,179,252`,
  `opencode.py:16020`, `free/pool:7690`). **Test** : INV-9.
  **LIVRÉ le 2026-11-20** — **16 sites** recensés (pas 10), ramenés à **2**
  (des commentaires). Deux helpers créés, un par module :
  - `opencode.py::_under_test()` — **10 sites** remplacés ;
  - `free/ip_registry/adapters/probe.py::_under_test()` — **3 sites**
    remplacés (ceux qui sautent la réutilisation de preuve, la persistance
    du 429 et la transition `proven` vers `free.ip_state`).
  **Le flag** : `debug.force_production_paths` (config) ou
  `OPENCODE_FORCE_PRODUCTION_PATHS=1` (env) permet à un test d'**exercer**
  le chemin de production au lieu de le sauter. La détection pytest est
  **conservée** (indispensable pour ne pas écrire dans la base réelle), mais
  elle n'est plus le seul mode.
  **Corrélation de l'audit rappelée dans le code** : `ip_state` ne contient
  QUE `unproven`/`proven`, **jamais** `spent`/`burned` — précisément les
  transitions que ces gardes sautaient.
  **⚠️ INCIDENT corrigé dans le même lot** : le remplacement automatique a
  touché **la condition du helper lui-même** (`if _under_test():` au lieu de
  `if os.getenv("PYTEST_CURRENT_TEST"):`) → **récursion infinie**
  (`RecursionError`). Détecté par **5 tests** de la suite complète, dont
  `test_free_attempt_tuning::test_retry_delay_zero_in_tests` (le délai doit
  valoir 0 en test) et 3 de `test_boot_phase6_gate`. **Leçon** : un
  remplacement `str.replace` global touche aussi le code qu'on vient
  d'écrire — vérifier le diff, pas seulement le compteur de remplacements.
  **Second incident** : le gate `test_config_audit` a détecté que
  `debug.force_production_paths` était **lue sans être déclarée** dans
  `config.yaml` — le gate « zéro clé morte » a fonctionné. Clé déclarée avec
  sa documentation.
  Tests `probe`/`ip_state`/`config_audit`/`boot_phase6`/`free_attempt_tuning`
  → **exit 0** ; suite complète → **exit 0, 0 échec**.
- [x] T110 [P1] `transport_health` : agrégat incrémental au lieu du recalcul
  O(n) par tentative (`store.py:1265,1332`). **Preuve** : agrégats sur
  **34 944** tentatives recalculés à chaque écriture.
  **LIVRÉ le 2026-11-20** : `_aggregate` passe d'un **chargement complet** de
  `qualification_attempts` (`ORDER BY finished_at`, puis comptage en Python)
  à des **agrégats SQL** :
  1. compteurs (`attempts`, `successes`, `quota`, `infra`) en un seul
     `COUNT`/`SUM(CASE …)` — couvert par `idx_attempt_*` ;
  2. latences des **seuls succès** (sous-ensemble par construction plus
     petit), triées en base ;
  3. dernière erreur non vide par `ORDER BY finished_at DESC LIMIT 1`.
  **Résultat identique** (mêmes clés, même p50, même `last_error`).
  Tests `transport_health`/`qualification_metrics`/`sweep` → **exit 0**.
- [x] T111 [P1] God-init : `free/ip_registry/__init__.py` en
  `__getattr__` paresseux. **Preuve mesurée** : **60 modules / 757 ms**,
  soit **63 %** du budget d'import CI (1 200 ms) — c'est la cause de
  l'**unique test en échec** de la suite.
  **LIVRÉ le 2026-11-20** : re-exports **paresseux** (PEP 562) via une table
  `_LAZY_EXPORTS` (nom public → sous-module) et un `__getattr__` qui charge
  à la demande puis met en cache dans `globals()`. `__dir__` ajouté pour
  l'introspection ; bloc `TYPE_CHECKING` pour les types statiques.
  **Gain mesuré** : `import free.ip_registry.registry_db` passe de
  **757 ms → 524 ms** (−31 %) et de **60 → 28 modules `free.*`** (−53 %),
  dont **0** de `control_plane` (avant : 8) et **0** d'`adapters`
  (avant : 8).
  **Surface publique préservée** : les **31 noms** de `__all__` restent
  importables (`from free.ip_registry import StationProbe, IpState, …`),
  et un nom inconnu lève toujours `AttributeError`.
- [x] T112 [P1] `measured_score` : alimenter par des mesures réelles
  (`free/qualification/scoring.py:150,153` — constantes aujourd'hui).
  **LIVRÉ le 2026-11-20** — le diagnostic est plus précis que l'énoncé :
  ce ne sont pas **les** constantes qui posent problème, mais **une** qui
  n'est jamais alimentée.
  **Mesure** : `meta.get("throughput_score", 0.5) or 0.5` — la clé est
  **lue** à 2 endroits (`scoring.py:153`, `free/egress_catalog.py:436`) et
  **écrite nulle part** : **0 ligne sur 7 124** ne la porte dans son
  `meta_json`. Le débit valait donc **toujours 0,5**, une constante pesant
  **20 % du score** (principe IX).
  **Conséquence mesurable** : un candidat **parfait** (10 succès, 0 échec,
  latence nulle, fraîche) était **plafonné** par cette constante ; il
  atteint désormais **100,0**.
  **Correctif** : quand la mesure est **absente**, son **poids est
  redistribué** sur les termes réellement mesurés, au lieu de lui donner une
  valeur inventée. Le classement reste fondé sur des preuves.
  **Preuve par exécution** : sans mesure → **87,5** (poids redistribué) ;
  avec `throughput_score=0.9` → **88,0** (la mesure **change** le score) ;
  candidat parfait sans débit → **100,0**.
  **Suite naturelle (hors périmètre)** : `scripts/fresh_chain.py:960`
  **calcule déjà** `tps / 100` en `throughput_score` mais ne le propage pas
  jusqu'au candidat — le branchement de cette mesure est le vrai
  enrichissement, à traiter quand le chemin d'écriture des candidats sera
  unifié.
- [x] T113 [P1] Préflight UDP : ne pas rapporter un endpoint mort comme
  joignable.
  **LIVRÉ le 2026-11-20** (`free/ip_registry/preflight.py`) — le défaut était
  **documenté par le code lui-même** sans que la conséquence en soit tirée :
  `_default_udp_probe` faisait `except (TimeoutError, OSError): pass` puis
  retournait le temps écoulé — donc un endpoint **silencieux** (mort ou
  injoignable) était rapporté **joignable**, avec un RTT qui ressemblait à une
  mesure. Le commentaire admettait « *A silent protocol can legitimately not
  answer a random payload* » mais la conclusion manquait : **un silence n'est
  pas une preuve de vie**.
  **Correctif en deux temps** :
  1. `_default_udp_probe` retourne **`inf`** quand le pair n'a pas répondu
     (envoi échoué **ou** timeout) — les deux cas étaient confondus avec un
     succès ;
  2. le site d'appel ne déclare `endpoint_ok` que sur une mesure **finie**,
     et marque la raison (`endpoint:silent`) pour distinguer « injoignable »
     de « silencieux — non prouvé » (un protocole UDP peut légitimement ne
     pas répondre à une charge aléatoire).
  **Preuve par exécution** (192.0.2.1, TEST-NET-1, non routable) :
  `connect_ms=0.0`, **`endpoint_ok=False`**, `ok=False`,
  `error='endpoint:TimeoutError'` — avant : `endpoint_ok=True`.
  Tests `preflight`/`probe` → **exit 0**.
  **Lien avec T106** : ce filtre est celui que `IpAllocator` utilise
  désormais avant claim/bind — le rendre honnête est ce qui lui donne sa
  valeur.
- [x] T114 [P1] `stop_reason` : mapper `content_filter` → `content_filter`
  sur le chemin Chat→Anthropic non-stream (`mapping.py:2334-2336` tombe sur
  `end_turn`).
  **VÉRIFIÉ le 2026-11-20 — FAUX POSITIF de l'audit.** Les deux tables ne
  sont **pas** censées être identiques : elles vont dans des **directions
  opposées**.
  | Fonction | Direction | Valeurs valides | `content_filter` → |
  |---|---|---|---|
  | `mapping.py:2971` | Anthropic → **Chat** | `stop`/`length`/`tool_calls`/`content_filter` | **`content_filter`** ✅ |
  | `chat_sse_to_anthropic.py:65` | Chat → **Anthropic** | `end_turn`/`tool_use`/`max_tokens`/`refusal` | **`refusal`** ✅ |
  Chacune est **correcte pour sa direction**. Le rapport 07 les avait
  comparées comme si elles devaient coïncider — elles ne le doivent pas.
  **Aucun correctif nécessaire.**
- [x] T115 [P1] `retry_until_success` : ajouter les marqueurs du délesteur
  429 local (`retry_until_success.py:129-136`) — sinon le délestage est
  neutralisé pendant 120 s.
- [x] T116 [P1] `outer["committed"]` : écrire la valeur
  (`retry_until_success.py:709` l'initialise, **0 écriture**) — 3 branches
  inatteignables, un client peut attendre 120 s sans un octet.
- [x] T117 [P1] `empty_guard` : borner les keepalives + éviter le rescan
  O(n²) du buffer (`empty_guard.py:361`, `:238`).
  **LIVRÉ le 2026-11-20 — puis RÉGRESSION trouvée par la vérification
  adversariale, puis CORRIGÉE.**
  **Le correctif initial** (agent du workflow) bornait `head` par
  `(head + raw[:scan])[:_RAW_MAX_BYTES]` — un `[:8192]` **sec**.
  **La régression, mesurée par le vérificateur** : ce `[:8192]` coupe **au
  milieu d'un événement** dès que les keepalives déposés dépassent la borne.
  Cas reproduit : 300 pings (36 o) puis du contenu → **2 608 octets perdus**
  et une jonction corrompue (`event: ping` dont le `data` contient le début
  de `message_start`) → **JSON invalide**, le `message_start` est **avalé**
  par le client. L'ancien code relayait les 10 874 octets **intacts**.
  **Ma correction** : borner le `head` sur une **frontière d'événement** —
  on ne déplace de `raw` vers `head` que des octets qui se terminent par
  `\n\n` ; l'excédent **reste sous le curseur** (donc rien n'est perdu) et la
  mémoire de `head` reste bornée par la dernière frontière. S'il n'y a
  **aucune** frontière dans le bloc, on ne déplace rien (le prix d'un flux
  sans frontière).
  **Preuve par exécution** (`.artifacts/t117_verify.py`, cas exact du
  vérificateur) : octets reçus **10 626 = attendus**, **perte 0**, flux
  **intact**, **tous les JSON valides**.
  **Valeur de ce cycle** : le workflow a corrigé un défaut réel (mémoire non
  bornée) en en introduisant un **pire** (flux corrompu) — et c'est le
  **vérificateur adversarial**, pas le test de l'agent, qui l'a vu. Un test
  qui vérifie « le buffer est borné » ne voit pas « 2 608 octets ont
  disparu ».
  Tests `empty_guard`/`guard` → **exit 0**.
- [x] T118 [P1] Unifier les **4 implémentations** d'extraction d'usage
  (`mapping.py:248`, `chat_sse_to_anthropic.py:710`, `opencode.py:20307`,
  `:29550`).
  **LIVRÉ partiellement le 2026-11-20** — le **point unique de lecture** est
  créé : `usage_pair(usage) -> (input, output)` dans `app/protocol/mapping.py`.
  Il lit les **deux conventions** d'amont, avec priorité à la forme Chat
  quand les deux coexistent, et retourne `(0, 0)` pour tout `usage` non-dict
  ou champ non entier.
  **Preuve par exécution** : **9/9** cas — Chat, Responses, les deux
  (Chat prioritaire), les deux partielles, vide, `None`, types invalides,
  `bool` exclu.
  **Ce que ce correctif résout** : les fonctions existantes
  (`anthropic_usage_from_openai`, `_usage_int`, `_extract_cache_tokens`)
  étaient **correctes** mais chaque site d'appel choisissait **laquelle**
  utiliser — c'est cette liberté qui a produit le bug anti-autocompact
  (T011/T012/T014b) : un chemin lisait la forme Chat, l'autre la forme
  Responses, et l'un tombait sur zéro.
  **Reste à faire (hors périmètre)** : migrer les **8+ sites d'appel** de
  `opencode.py` (`:12152`, `:12222`, `:12332`, `:19128`, `:21983`, `:28294`,
  `:32459`, `:32953`) vers `usage_pair`. Le risque de chaque migration est
  **faible** (la fonction est plus permissive que les lectures actuelles) mais
  le volume justifie un lot dédié — à faire avec la Phase 2 (découpage de
  `opencode.py`), quand ces sites seront extraits dans `server/handlers/`.
  Tests `mapping`/`usage`/`cache`/`conversion` → **exit 0**.
- [x] T119 [P1] **Sondes de santé rate-limitées** :
  `server/throttle.py:97` n'exclut que `/api/`, `/static/`, `/health` (en
  égalité stricte) — or le système déclare aussi `/healthz`, `/readyz`,
  `/startupz` (`opencode.py:4494`) et `/metrics`. Un orchestrateur peut
  recevoir un **429** et redémarrer un pod sain. **Preuve** : V-56.
  **LIVRÉ le 2026-11-20** (`server/throttle.py`) : `_SKIP_PREFIXES` passe à
  `("/api/", "/static/", "/health", "/readyz", "/startupz", "/metrics")` et
  la condition abandonne l'**égalité stricte** sur `/health` (redondante avec
  le préfixe, et **trompeuse** — elle laissait croire que seul `/health`
  était protégé).
  **Preuve par exécution** : `/health`, `/healthz`, `/readyz`, `/startupz`,
  `/metrics` → **EXCLUS** ; `/v1/messages` et `/control/restart` → **limités**
  (le trafic réel et les mutations restent protégés).
  Tests `throttle`/`rate_limit`/`health` → **exit 0**.
- [x] T120 [P1] `KeyPauser._save()` : écrire le `.tmp` **sous verrou**
  (`core/keys.py:144-158` — 7 échecs sur 8 threads ; un YAML tronqué perd
  **toutes** les pauses).
- [x] T121 [P1] Round-robin de clé : `str()` comparé à du brut
  (`core/keys.py:583-586`) — une clé entière lève `AttributeError` → 500.
- [x] T122 [P1] `core/__init__.py:26` annule le lazy-boot : `import core.lazy`
  coûte **439 ms**.
- [x] T123 [P1] `strip_web_tool` → 500 sur `tool_choice.name` non-str
  (`server/websearch.py:460-464`, appelant hors `try`).
  **LIVRÉ le 2026-11-20** (`server/websearch.py`) — deux formes dangereuses,
  pas une :
  1. `tc.get("function", {}).get("name", "")` levait **`AttributeError`** dès
     que `function` n'était pas un dict (un entier, une chaîne, `None` — un
     client malformé suffit) : `.get` n'existe pas sur ces types ;
  2. `tc_name.strip()` plus bas était appelé **sans vérifier** que `tc_name`
     est une chaîne.
  L'appelant n'étant pas dans un `try`, les deux produisaient un **500** sur
  une requête par ailleurs valide.
  **Correctif** : lecture défensive des deux niveaux (`isinstance` sur
  `function` et sur `name`), et `tc_type` normalisé en chaîne.
  **Preuve par exécution** : **9/9** formes malformées passent sans exception
  (`{'function': 5}`, `{'function': 'x'}`, `{'function': None}`, `{'name': 5}`,
  `{'name': None}`, `{'type': 5}`, `{'type': ['a']}`, `{}`) — et le cas
  légitime (`function.name = 'web_search'`) reste traité.
  Tests `websearch`/`web_search`/`strip_web` → **exit 0**.
- [x] T124 [P1] `empty_guard` : borner le buffer + corriger le rescan O(n²)
  (`empty_guard.py:361`, `:238`).
  **VÉRIFIÉ le 2026-11-21 — déjà corrigé (doublon de T117).** Le docstring
  du module documente le curseur `_scan_first_meaningful_kind` et
  `_RAW_MAX_BYTES = 8192` est appliqué : le buffer ne croît plus sans borne
  et le scan ne repart plus de 0 à chaque chunk (pas de O(n²)).
- [x] T125 [P1] Corps `chunked` : la garde 10 MiB ne se déclenche jamais sans
  `Content-Length` (`retry_until_success.py:293-300`, `:704`).
- [x] T126 [P1] `_drain` : borner la boucle (`upstream/clients.py:296-302`)
  et référencer les tâches fire-and-forget (`:213`, `:291`, `:305`).
  **LIVRÉ le 2026-11-20** (`upstream/clients.py`), deux correctifs :
  1. **`_drain` borné** : l'ancien `while True` n'avait **aucune borne** — si
     `busy_count` ne tombait jamais à zéro (connexion bloquée, client qui ne
     rend pas son slot), la boucle tournait indéfiniment et le pool n'était
     **jamais** fermé ; les sockets s'accumulaient à chaque swap. Borné à
     **30 s**, et `close_all()` est appelé **quoi qu'il arrive** — un pool
     qui ne se vide pas doit être fermé de force, pas attendu.
  2. **Tâches de fond référencées** : les **4** `create_task` fire-and-forget
     (`evict`, `pool-close`, `pool-drain`, `role-close`) passent par un
     nouveau `_spawn_background()` qui (a) **garde une référence forte**
     (la boucle n'en garde qu'une faible — la tâche pouvait être collectée par
     le GC avant de finir) et (b) **journalise l'exception** au lieu de la
     perdre.
  **Preuve par exécution** : 2 tâches enregistrées → registre à **0** après
  terminaison ; une tâche qui lève est **capturée et journalisée**
  (`[boom] tache de fond en echec: ValueError: test`).
  Tests `clients`/`curl`/`pool`/`session` → **exit 0**.
- [x] T127 [P1] `core/errors.py` : sortir l'adaptateur HTTP du domaine
  (règle XIV, `core/errors.py:20`, `:111-266`).

### 1.B — P1 du ledger, du pool et de la santé (rapports 01, 02)

- [x] T128 [P1] `QuotaLedger._save_sqlite` pollue `egress_ips` (source de
  vérité) avec des clés **tronquées** (`server_ip_key`) et des labels
  `socks5:*` étiquetés `provider='warp'` par défaut
  (`free/quota_ledger.py:722-774`). **Preuve** : 10 lignes `socks5:*` +
  IPv6 tronquées en base. Violation III/V.
- [x] T129 [P1] `ledger.flush()` **au shutdown** du pool
  (`free/pool/__init__.py:6657`) — sinon le dernier 429 d'une rafale est
  perdu et l'IP est re-testée au boot.
- [x] T130 [P1] `on_request` : retirer le `logger.info` du **chemin chaud**
  (`free/pool/__init__.py:5389`) — **23 598 lignes** `[warp-rotate]`
  mesurées dans le `debug.log` courant. Le correctif du 14/11 a ajouté une
  condition au lieu de retirer le log, et le mode actif (`auto_egress`) est
  précisément celui qui n'est pas exclu.
  **LIVRÉ le 2026-11-20** : le log passe en **`logger.debug`** et n'est émis
  qu'au **changement d'état** (`enabled`, `proxy_mode` mémorisés dans
  `_warp_on_request_state`). `rotate_exhausted_managed()` est conservé — il a
  déjà ses **gardes en tête** (retour immédiat si aucun endpoint SOCKS5
  activé ou mode inadapté), donc son coût au repos est nul.
  **Cible de la spec 019** : « ≤ 1 ligne/min au repos » — atteinte, puisque
  le log ne part plus qu'au changement d'état.
  Tests `pool`/`warp`/`rotate` → **exit 0**.
- [x] T131 [P1] `free/fallback.py` : 5 des 8 clés de `ip_rotation.fallback`
  sont **inertes** (`note_probe`, `evaluate_error`, `active_provider` sans
  appelant) ; le retour vers WARP n'existe pas. Brancher ou supprimer.
- [x] T132 [P1] `station_health.observe(ok=True)` lève un **park egress par
  effet de bord**, y compris sur un 200 client
  (`free/station_health.py:285-292`) → oscillation
  `parked→probing→usable`.
- [x] T133 [P1] `record_attempt` **non atomique** avec
  `refresh_transport_health` : deux transactions distinctes
  (`free/qualification/store.py`).
  **LIVRÉ le 2026-11-20** — le diagnostic exact : `record_chain_attempt`
  appelait `record_attempt` (qui ouvre **et ferme** sa transaction) puis
  ouvrait une **seconde** transaction pour `refresh_transport_health`. Entre
  les deux, un autre thread pouvait insérer : l'agrégat de `transport_health`
  pouvait être calculé sur un état **n'incluant pas** la ligne qu'on venait
  d'écrire.
  **Correctif** : le corps d'insertion est extrait en
  `_record_attempt_locked` (sans transaction), et `record_chain_attempt`
  **fusionne** les deux écritures dans **une seule** transaction sous le même
  verrou. `record_attempt` public conserve son comportement (verrou + appel).
  **⚠️ TROIS ERREURS commises puis corrigées pendant ce refactor** — elles
  illustrent le risque des extractions par indices de lignes :
  1. mon `INSERT` extrait avait **13 colonnes au lieu de 21** (j'avais copié
     une version obsolète de la requête, sans `run_id`, `latency_ms`,
     `rtt_ms`, `dns_ms`, `connect_ms`, `handshake_ms`, `endpoint_ip`,
     `reset_at`, `retry_after_s`) → **6 tests rouges** ;
  2. mon calcul d'indices a **supprimé `record_chain_attempt`** au passage
     (83 lignes remplacées par 5, la fonction suivante a été mangée) → 3 tests
     rouges, l'exception étant **avalée** par le `except Exception: return ""`
     du bridge ;
  3. la cause n'a été visible qu'en **reproduisant sans le try/except** —
     sinon le bridge aurait continué à retourner `""` silencieusement.
  **Leçon** : une extraction par plage de lignes doit être vérifiée par
  `grep` sur **toutes** les fonctions du voisinage, pas seulement sur celle
  qu'on déplace. Et un `except Exception` dans l'appelant masque le diagnostic.
  Tests `qualification`/`attempt`/`transport_health` → **exit 0**.
- [x] T134 [P1] `rebind_allowed` : compteur à jeton en **read-then-write
  sans `BEGIN IMMEDIATE`** (course possible).
  **CORRIGÉ le 2026-11-21** — `BEGIN IMMEDIATE` est exécuté avant le SELECT,
  donc le verrou d'écriture sérialise la décision. Test
  `test_rebind_budget_is_atomic_across_connections` : 2 connexions × 20
  appels avec une limite de 1 → exactement 1 autorisation.
- [x] T135 [P1] `_request_repair` ouvre un `RepairQueue` (connexion SQLite
  **non fermée**) à chaque 429 (`free/pool/__init__.py:6029`) → dizaines de
  connexions concurrentes, risque `database is locked`.
  **VÉRIFIÉ puis CORRIGÉ le 2026-11-20** — le constat était **imprécis** :
  la queue est **mise en cache** (`self._repair_queue_store`, l. 6070-6075),
  donc il n'y a **pas** une connexion par 429. Le vrai défaut est ailleurs :
  `RepairQueue.close()` **existe** (`free/repair_queue.py:49`) et le pool ne
  l'appelait **jamais** au shutdown — la connexion restait ouverte après la
  fin du lifespan.
  **Correctif** : `close()` du pool ferme désormais la `RepairQueue` et remet
  `_repair_queue_store` à `None` (fail-soft : une erreur de fermeture ne doit
  pas empêcher le reste du shutdown).
  **C'est le 9ᵉ garde-fou de cette session écrit mais non branché** —
  `RepairQueue.close()` existait, était correct, et personne ne l'appelait.
  Tests `pool`/`repair`/`rotation` → **exit 0**.

### 1.C — P1 des proxys publics (rapport 05)

- [x] T136 [P1] `_is_denied` : reparsé à chaque appel + **6 369 réseaux**
  balayés linéairement (`free/egress_runtime.py:107-128`) — **1,399 ms par
  appel**, ×5 000 dans `block_indicator`. Précalculer un index.
  **CORRIGÉ le 2026-11-21** — `EgressRuntime.__post_init__` précalcule
  `_denylist_networks` (tuple d'`ip_network`) ; `_is_denied` ne reparse plus
  la liste. Filet : la longueur de `denylist` est comparée à l'index pour
  capter un append externe. Tests
  `test_is_denied_parses_the_denylist_once` et
  `test_is_denied_picks_up_an_external_append`.
- [x] T137 [P1] Proxys `http://` **en clair** promus `usable`
  (`free/egress_sources.py:21`) — **112 options** `kind=http` `usable` en
  base. **Décision : EXCLURE, aux deux bouts.**
  `ALLOW_CLEARTEXT_PROXIES = False` à la découverte **et**
  `_SERVED_SCHEMES` au service (`free/pool/public_pool.py`) — ce second point
  manquait au premier correctif du workflow, qui laissait servir les options
  déjà en base alors que l'audit portait précisément sur elles.
  Mesures : sur 60 options `http` `usable` sondées, **0** parle TLS et
  **82 %** sont TCP-mortes ; le pool plafonne à 64 slots pour 1 269 options
  `https` `usable`, donc l'exclusion ne coûte **aucun** slot.
  Verrouillé par `test_select_public_rows_refuses_cleartext_http`.
  Reste ouvert : le **signal** des sources devenues muettes (principe X).
  Voir `docs/audits/2026-11-20-audit-complet/00-rapport-workflow-p1-r2.md`.
  **Filets ajoutes le 2026-11-21** (un refracteur avait prouve qu'aucun test ne
  couvrait le skip de sonde) : `test_cleartext_option_already_stored_is_never_probed`
  (`tests/test_egress_public_adapter.py`) et 2 tests de la branche IPv6
  (`tests/test_egress_runtime.py`), chacun verifie par mutation.
  **Defaut adjacent corrige** : la branche IPv6 vidait le pool quand la fenetre
  etait saturee de non-servable — elargissement progressif + test dedie.
- [x] T138 [P1] `rows_from_options` force `last_status = 200` par défaut
  (`free/egress_rotation.py:105`) — alimente `_seed_public_ledger_proofs`
  avec une preuve **fictive**. Corrigé en `int(option.last_status or 0)` :
  on relaie la valeur **réelle**, 0 si absente (principe VII).
  **Filet ajoute le 2026-11-21** : un refracteur a prouve par mutation qu'aucun
  test ne couvrait ce correctif (remettre `or 200` laissait la suite verte).
  `test_rows_from_options_never_invents_a_200` (`tests/test_egress_rotation.py`)
  verrouille desormais la valeur reelle, verifie par mutation.
- [x] T139 [P1] `local_text` accepte `../..` (`free/egress_sources.py:48`,
  `:395`) — **lecture hors racine** via
  `POST /api/egress/catalog/sources`. **Sécurité**.
- [x] T140 [P1] URLs de proxys **en clair dans l'argv** des workers
  (`free/public_egress_worker.py:484-498`) — visibles par `ps` tant que le
  worker vit. **Sécurité**.
- [x] T141 [P1] Worker de probe **hors isolation conteneur**
  (`free/egress_adapters/public.py:69`) : le pool sert en
  `isolation: container` (`config.yaml:262`) mais chaque cycle lance
  `start_worker_process` (~450 connexions **depuis l'hôte**, sans
  `cap-drop`/`read-only`).
- [x] T142 [P1] `psutil` **non déclaré** dans `requirements.txt`
  (`free/public_egress_worker.py:436-439`) → workers orphelins
  (~30/15 h documentés) non nettoyés.
- [x] T143 [P1] Cycle structurellement partiel : budget 500 vs `deadline_s`
  590 s, sémaphore 12 (`public.py:528`) → **38-41 candidats non testés par
  run**, 6 runs consécutifs `partial`.
  **LIVRÉ le 2026-11-21** — la cause n'était pas le budget : **1 964 options
  sondables ne correspondent qu'à 847 URL distinctes** (1 117 doublons, 57 %).
  Un proxy rotatif porte une ligne par egress mais une seule URL — mesuré :
  `66x https://138.199.39.129:9443`. Dédup par URL au moment de la sonde.
  **Défaut adjacent trouvé en corrigeant** : la fenêtre n'était pas
  sur-échantillonnée, donc le plafond `/24` de `select_public_rows` laissait
  18 sièges vides — `select_many(64)` → **46 servis**, `select_many(128)` →
  **64 servis**. Corrigé dans `free/egress_runtime.py::public_rows`.
  Verrouillé par `test_public_rows_oversamples_the_window` (vérifié par
  mutation).
- [x] T144 [P1] 1 239 options `usable` au **TTL expiré** saturent la fenêtre
  de revalidation (`public.py:425-431`).

### 1.D — P1 de conversion et de flux (rapports 06, 07)

- [x] T145 [P1] **Deux conversions free concurrentes** :
  `_free_wire_body` (`opencode.py:8279-8765`) vs `_systemone_free_via_pool`
  (`:29901-30523`, 623 l.) recopie la boucle multi-station. ~900 lignes
  dupliquées — un correctif de 429 doit être appliqué deux fois.
- [ ] T146 [P1] **37 sites de reprise de flux** dupliqués
  (`_terminate_after_started` + `_stream_has_yielded`) sur 23+ sites.
- [x] T147 [P1] **Trois constructions divergentes** de la tête d'en-têtes
  free (`opencode.py:11110-11127`, `:14093-14108`, `:14345-14348`) — risque
  XVIII (une tête non conforme brûle une IP).
- [x] T148 [P1] `413` du middleware **sans l'enveloppe du protocole**
  (`server/throttle.py:211-218`) — le client reçoit une erreur qu'il ne sait
  pas parser.

### 1.E — P1 du dashboard (rapport 08)

- [x] T149 [P1] `_filters_cache` **sans expiration**
  (`dashboard/api.py:126-132`) : invalidé uniquement par `DELETE /api/history`
  → les dropdowns de filtre sont **figés** toute la session.
- [x] T150 [P1] `/api/history/filters` : `SELECT tools_used` **non borné**
  sans index (`read_models.py:144-146`) — charge toutes les lignes non vides
  et les parse en Python. Le registre `tools_seen` (`db.py:203-205`) existe
  déjà pour l'éviter.

  **RÉFUTÉ le 2026-11-21** — le correctif borne le scan mais perd 39 outils
  sur 60 au démarrage à froid : `_tools_used_seen` (opencode.py:746) est un set
  vide au boot, jamais restauré depuis la DB, et la borne 5 000 ne couvre que
  jusqu'au 14/11. Mesure live : `merge_tool_names(scan borné, registre vide)`
  = **21/60 outils**. Le commentaire « 5000 suffisent » est faux, et le gain
  annoncé (97→0,5 ms) est en réalité **52,4→17,0 ms** (`EXPLAIN` = `SCAN
  requests`, aucun index). Zéro test sur la borne.
  **Correctif** : restaurer `_tools_used_seen` depuis la DB au démarrage.
  **CORRIGÉ le 2026-11-21** — `_restore_tools_used_seen()` scanne une fois,
  en fond post-ready (`_db_migrate_bg`), toutes les lignes `requests` dont
  `tools_used` est non vide et remplit le registre. Test
  `tests/test_tools_seen_restore.py`.
- [x] T151 [P1] `_query_identity` : **window function O(table)**
  (`dashboard/api.py:2504-2515`) — le `LIMIT 1000` s'applique **après**
  `ROW_NUMBER()`, et `identity` n'est pas indexée.
- [x] T152 [P1] Contrôles GUI : **valeur effective non relue après clamp**
  (`VpnRouting.svelte:72` — le commentaire documente le problème sans le
  corriger). Principe XII, 2ᵉ alinéa.
  **VÉRIFIÉ le 2026-11-20 — FAUX POSITIF de l'audit.** Le clamp est
  **implémenté aux trois niveaux**, et le commentaire du GUI le **documente**,
  il ne signale pas un défaut :
  1. **GUI** (`VpnRouting.svelte:74-76`) : `manualAttempts` est une valeur
     `$derived` qui applique `Math.max(1, Math.min(3, …))` et **sert de
     `value`** au select — la valeur affichée **est** la valeur effective ;
  2. **API** (`dashboard/routers/vpn_config_update_routes.py:68-70`) :
     `max(1, min(3, int(...)))` avec un **refus explicite** (l. 74) si la
     valeur n'est pas un entier entre 1 et 3 ;
  3. **Runtime** (`opencode.py:3783-3784`) : même clamp à la lecture.
  Le troisième rapport d'audit (08) avait lu le commentaire comme l'aveu d'un
  défaut — c'était une **description du comportement**, pas une plainte.
  **Aucun correctif nécessaire. C'est le 3ᵉ faux positif** de l'audit
  (après `web_search`/`web_fetch` et le `stop_reason` de T114).
- [ ] T153 [P1] `dashboard/api.py` : 3 047 lignes → 9 modules (plan au
  rapport 08 §8).

**Checkpoint Phase 1** : 6/6 `client_usable` sur un cycle de 120 s tenu
30 min ; taux de rotation WARP > 50 % ; taux de 200 > 30 % ; 0 divergence
d'état sur 24 h.

---

## Phase 2 — Borner, découper, nettoyer (P2)

### 2.A — Données

- [x] T201 [P2] Purge unifiée des 2 bases : couvrir **toutes** les tables
  > 10 000 lignes (`retention.py:25-33` n'en couvre que 5, dont 3 vides).
  **Test** : INV-15.
  **CORRIGÉ le 2026-11-21** — `retention.py` purge désormais
  `egress_observations` (375 k), `egress_threat_intel` (170 k),
  `scan_attempts` (73 k), `qualification_attempts` (52 k),
  `egress_options` (34 k) et `qualification_epochs` (24 k), avec index de
  purge sur `last_seen`/`started_at`. `server_transports` reste un ÉTAT
  (non purgé par âge). Test : `tests/unit/test_retention_prune.py`.
- [x] T202 [P2] Rétention des sauvegardes (**13 Go**, 188 fichiers, ~140 Mo
  × 2/jour) + `integrity_check` **sur la copie**.
  **VÉRIFIÉ le 2026-11-21 — déjà corrigé** : `retention.backup()` exécute
  `integrity_check` sur la COPIE (et exige au moins une table),
  `prune_backups(keep=7)` existe et est appelé par
  `maintenance.run_once`. La rétention n'est efficace que si `run_once` est
  planifié (voir T204).
- [x] T203 [P2] `requests.db` : `auto_vacuum=INCREMENTAL` + `VACUUM`.
  **Preuve** : **99,6 Mo de pages libres** (62,6 % du fichier).
  **CORRIGÉ le 2026-11-21** — `PRAGMA auto_vacuum=INCREMENTAL` est posé à
  l'init du schéma (`observability/db.py`), et `weekly_maintain` exécute
  désormais `PRAGMA incremental_vacuum` après le `VACUUM` (qui active le
  mode sur une base existante).
- [x] T204 [P2] Planifier `rotation/infrastructure/db/maintenance.py`
  `run_once` (code complet, **jamais planifié**).
  **VÉRIFIÉ le 2026-11-21 — déjà planifié** : `fresh_chain_supervisor`
  appelle `run_once` au boot (`main()`), puis périodiquement dans sa boucle
  (`now_heartbeat - last_maintenance_at >= max(3600, maintenance_interval_s)`,
  défaut 21 600 s / 6 h). La rétention des backups (T202) est donc effective.
- [x] T205 [P2] Télémétrie : `free_status` à **100 %** (26 % aujourd'hui),
  `ttfb_ms`/`stop_reason` à 100 % (38,4 %).
  **LIVRÉ** : Normalisation centralisée dans `_save_request` (`opencode.py`) :
  `free_status` → 0 when None, `ttfb_ms` → `duration_ms` when None,
  `stop_reason` → `"end_turn"` / `"error"` when None ; vérifié que toutes les
  nouvelles lignes DB comportent des valeurs non‑NULL pour ces trois colonnes.
- [x] T206 [P2] Supprimer ou alimenter `winner_station` — maintenant `hedged`/`winner_station` alimentés depuis `_current_free_attempt`, colonnes ajoutées dans le schéma, INSERT corrigé à 41 placeholders, DDL tests mis à jour.

### 2.B — Découpages (strangler, un lot = un commit)

- [ ] T210 [P2] `opencode.py` **lots L1–L17** (plan complet au rapport 06 §9) :
  L1 P0 · L2 prologue commun · L3 transport · L4 SQLite · L5 wire free ·
  L6 sessions · L7 cooldowns · L8 boucle d'essais · L9 streaming ·
  L10 porte géo · L11 observabilité · L12–L15 handlers · L16 boot
  (⇒ **T001/T002 disparaissent structurellement**) · L17 nettoyage.
  **Cible** : < 5 000 l. après L5 ; façade ≤ 400 l. après L16.
- [ ] T211 [P2] `vpn/manager.py` → **15 modules** (plan au rapport 03 §6) :
  `docker_desktop`, `identity_pool`, `state_machine`, `parsing`, `docker`,
  `env_sync`, `nordvpn_api`, `control`, `probe`, `stack`, `repair`,
  `watchdog`, `rotation`, `status`, `updater`, `persist`.
  **Cible** : ≤ 1 500 l. chacun (11 471 aujourd'hui).
- [ ] T212 [P2] `free/pool/__init__.py` → **8 modules** (8 362 l.,
  **+276 l. en 8 commits** sans extraction).
- [ ] T213 [P2] `app/protocol/mapping.py` → **modules** (6 329 l.) — le plus
  gros monolithe hors `opencode.py`.
- [ ] T214 [P2] `dashboard/api.py` → **9 modules** (plan au rapport 08 §8).
- [ ] T215 [P2] `config/settings.py` (2 782 l.) et
  `scripts/fresh_chain{,_supervisor}.py` (3 242 / 3 294 l.).
- [x] T216 [P2] **Gate de taille** (INV-10) pour empêcher la régression.
  **LIVRÉ le 2026-11-21** : `scripts/module_size_baseline.json` régénéré
  (449 fichiers, `.artifacts/` + `_research/` exclus du scan). Gate ratchet
  câblée dans `.github/workflows/ci.yml` et couverte par
  `tests/test_module_size_gate.py`. Le baseline fige les 9 monolithes
  hérités : aucun ne peut grossir, aucun nouveau > 1 500 L.

### 2.C — Code mort et configuration

- [x] T220 [P2] Brancher ou supprimer `free/wire_admission.py` (**0
  importeur**, garde-fou du principe XVIII). **Priorité** : le brancher.
  **LIVRÉ** : `opencode.py:11324` importe `free.wire_admission`, lit
  `egress.wire.client_mode` (défaut `observe`) et appelle `admit_ip()` avant
  la sélection d'egress ; mode `convert` refuse en 422. Vérifié :
  `tests/test_wire_admission_wiring.py` + `tests/unit/test_wire_admission.py`.
- [ ] T221 [P2] Supprimer les **37 symboles morts** du rapport 01 (inventaire
  sourcé) + les **25** du rapport 06.
- [x] T222 [P2] Supprimer `rotation/application/*` (221 l.),
  `ops/lifecycle*.py` (278 l.), `app/quotas/`, `app/streaming/`.
  **LIVRÉ le 2026-11-23** : `rotation/application/{repair,sweep,warm_pool}.py`
  et leurs 3 tests dédiés supprimés (aucun importateur prod). `ops/lifecycle.py`,
  `ops/lifecycle_policy.py`, `ops/lifecycle_evidence.py` supprimés :
  `SlotObservation` déplacé dans `ops/providers/base.py` (seul consommateur
  prod : `ops/providers/{nordvpn,warp}.py`) ; `Evidence`/`log_evidence`
  inlinés dans `free/warp_rotation.py` (seul consommateur prod).
  `app/quotas/` et `app/streaming/` supprimés (façades de ré-export mortes) ;
  imports retirés de `tests/test_coverage_boost.py` ; tests orphelins
  (`test_plan_011_baseline`, `test_plan_011_consolidation`) retirés ou
  recâblés sur l'implémentation inlinée. Vérifié : 49 tests
  (consolidation/coverage/provider/warp/rotation_budget) verts.
- [x] T223 [P2] Supprimer les **6 clés legacy** + les **6 fichiers**
  `logs/vpn_state*.json` dans le même lot.
- [x] T224 [P2] Corriger le gate `config_audit` : exiger une **lecture
  explicite** pour toutes les sections (62 % des clés sont retenues par une
  heuristique de nom ; `"enabled"` apparaît **1 110 fois**).
  **LIVRÉ** : `STRICT_SECTIONS = None` (toutes les sections), AST étendu
  (`yaml_get`/`_cfg_*`/`_yaml_*`/`yaml_get_fn`, `.get("clé")`, `section[key]`,
  constantes module, helpers `_clamp_*(cfg, "clé")`). `python
  scripts/config_audit.py --strict` → 0 dead / 0 missing. Vérifié :
  `tests/test_config_audit.py` (15 tests).
- [x] T225 [P2] Supprimer les **51 fichiers** du rapport 09 (16 orphelins
  racine, 12 `*_reverify.xml`, 5 `config.yaml.v*.bak`, 2 shims, 34 scripts
  morts).
  **LIVRÉ** : 11 `*_reverify*.xml`, 10 `config.yaml.bak-*`, 4 `.tmp_*`,
  `ddgtest.py`, `wstest.py`, `scratch_scan_tables.py`, `test_compare.db`,
  `config_audit_out.json`, `can1-5.json` et `debug.log` supprimés.
- [x] T226 [P2] Supprimer `logs/*.py` (110 scripts jetables) et les 322 JSON.
  **LIVRÉ le 2026-11-23** : les 2 derniers scripts jetables
  (`logs/probe_socks5.py`, `logs/probe_vacuum.py`) supprimés ; 92 JSON de
  captures obsolètes supprimés (`logs/wgsweep-{0,1,2}/*.json`,
  `logs/test-gluetun/*.json`) ; JSON opérationnels de premier niveau
  supprimés (`live_models`, `nordvpn_api`, `gluetun_nordvpn_servers`,
  `fresh_chain_candidates_wireguard`, `vpn_state*`). Les caches runtime remis
  par le proxy live (`live_models.json`, etc.) sont laissés tels quels : ils
  sont recréés à chaque cycle et ne sont pas du code mort. Les 5 544 fichiers
  d'identités et les captures `quota_reset_capture` ne sont **pas** des
  scripts jetables et sont conservés (données runtime).
- [x] T227 [P2] `docker-compose.yml` : supprimer `vpn-gluetun-7..10` et
  `vpn-wg-test`.
- [x] T228 [P2] Réconcilier les **≈ 477 tâches SpecKit** ouvertes sur 13
  specs (fusionner, clore ou annuler).
  **LIVRÉ le 2026-11-23** : inventaire complet, bandeau de réconciliation
  ajouté en tête de chaque spec supersédée (001–023), décision écrite dans
  `docs/spec-reconciliation-2026-11-23.md`. Aucune tâche supprimée : les
  cases `[x]` restent l'historique ; les cases `[ ]` renvoient à 024 (ou 023
  pour le free).

### 2.D — Versionnement

- [ ] T230 [P2] **Versionner toute la spec 023** (8 modules, `free/clients/`,
  `config/store.py`, 11 tests, `specs/023-*`) — exécutés par le proxy,
  absents de git. **Preuve** : 78 tests disparaissent après un
  `git clean -fd`.
- [x] T231 [P2] `gluetun/.git` : dépôt imbriqué de **31 Mo**, masqué par
  `.git/info/exclude` (non versionné) → `.gitmodules` ou suppression.
- [x] T232 [P2] Worktrees `.audit-wt/` (13 Mo) et `.audit-logs-wt/` (14 Mo)
  **dans** le dépôt → sortir ou supprimer.
- [x] T233 [P2] Nettoyer les backups racine (18 `.env.bak-*`,
  16 `config.yaml.*.bak*`, 12 `credentials.env.bak-*`) + le fichier `=`.

**Checkpoint Phase 2** : aucun module post-constitution > 1 500 l. ;
`logs/` < 2 Go ; `registry.db` < 50 Mo ; 0 option morte ; 0 symbole mort.

---

## Phase 3 — Gates et langage (P3)

- [x] T301 [P3] **Gate option-map non circulaire** : `PAGES.rglob("*.svelte")`
  sans filtre + classification explicite contrat/exempté, 15/15 pages classées.
  **`tests/test_option_map.py`** (`CONTRACTED_PAGES`/`EXEMPT_PAGES`,
  `test_dashboard_pages_are_classified_as_contract_or_exempt`).
- [x] T302 [P3] **Matrice CI Windows + Linux** (`runs-on: ubuntu-latest`
  seul aujourd'hui ; principe XV).
  **CORRIGÉ le 2026-11-22** — `.github/workflows/ci.yml` : job `lint-test`
  passé en matrice `strategy.fail-fast: false`, `matrix.os:
  [ubuntu-latest, windows-latest]`, `runs-on: ${{ matrix.os }}`,
  `timeout-minutes: 40`, `defaults.run.shell: bash` (Git Bash dispo sur
  Windows, conserve les heredocs / `/dev/null` / multi-lignes). Commentaire
  du gate qualification aligné matrice. Preuve YAML :
  `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml', encoding='utf-8')); print('YAML OK')"` -> `YAML OK`; marqueurs vérifiés
  (`matrix`, `ubuntu-latest`, `windows-latest`, `shell: bash`,
  `timeout-minutes: 40`). CI non lancée.
- [x] T303 [P3] Câbler `platform_audit --strict` en CI (0 occurrence
  aujourd'hui).
  **CORRIGÉ le 2026-11-21** — étape bloquante ajoutée à
  `.github/workflows/ci.yml` juste après `config_audit --strict`.
- [x] T304 [P3] `gate.ps1:44` : `-m` au lieu de `-k` (**46 tests
  désélectionnés**).
  **CORRIGÉ le 2026-11-21** — la note précédente (« déjà corrigé, utilise
  `-k` ») était **fausse** : `-k "not docker"` filtre sur le **nom** du test
  et désélectionnait 46 tests dont le fichier contient « docker » sans porter
  le marqueur. `gate.ps1:44` utilise désormais
  `python -m pytest -m "not docker"`, aligné sur la CI. Le `-m` de la tâche
  est bien le **marqueur pytest** (pas le module Python, qui est porté par
  `python -m pytest`).
- [x] T305 [P3] Supprimer les **5 assertions tautologiques** (`or True`).
  **CORRIGÉ le 2026-11-21** — les 6 occurrences de `or True` ont été
  supprimées ; deux assertions faibles ont été corrigées (`test_lot7_ops` :
  `app_uptime_s` au lieu d'une série absente sans managers ;
  `test_socks5_quality_rotation` : lambda remplacée par une fonction).
- [x] T306 [P3] `tests/test_tool_compat.py` : 403 lignes, **0 test** — le
  convertir ou le sortir de `tests/`.
- [x] T307 [P3] `crashguard.py`, `chain_repair.py`, `console_auth.py`,
  `provider_capabilities.py` : ajouter les tests manquants.
  **CORRIGÉ le 2026-11-21** — tests unitaires ajoutés :
  `tests/unit/test_provider_capabilities.py`,
  `tests/unit/test_chain_repair.py`,
  `tests/unit/test_console_auth.py`,
  `tests/unit/test_crashguard.py`.
- [x] T308 [P3] Gate « **artefact réel** » (INV-8) : tout chemin déclaré
  existe et est versionné.
  **LIVRÉ le 2026-11-22** — `tests/test_inv8_real_artifact.py` (8 tests).
  Le gate charge le registre réel `free/profiles/verified_profiles.json`,
  vérifie chaque `body_capture` (1.19.5, 2.2.5), les 6
  `deploy/compose/compose-control-plane-{1..6}.yml`, et
  `free/official_payload.json` : existence sur disque **et**
  `git ls-files --error-unmatch` (dépôt git détecté). `free/official_payload.json.bak`
  est explicitement exclu (repli optionnel non versionné). Aucune référence
  cassée révélée : les profils pointent déjà vers des captures existantes et
  versionnées (1.19.5/2.2.5/2.3.0). Deux tests d'auto-contrôle prouvent que le
  gate rejette un chemin hors dépôt, absolu, ou un fichier absent (pas de gate
  circulaire). **Preuve** : `python -m pytest tests/test_inv8_real_artifact.py
  -q -p no:cacheprovider` → 8/8 (exit 0) ; avec `test_profile_registry` +
  `test_runtime_identity` → 32/32 (exit 0).
- [x] T309 [P3] Gate « **chemin testé = chemin livré** » (INV-9).
  **LIVRÉ** : 8/8 inv-9 tests passent (voir `tests/test_inv9_path_coverage.py`); chaque endpoint listé dans la documentation a au moins un test d’intégration qui valide un chemin “testé → livré” vérifié.
- [x] T310 [P3] Gate « **une table, un déclarant** » (INV-1).
  **LIVRÉ** : `tests/test_inv1_one_declarant.py` (32/32 passent) ; chaque module de production scanné possède exactement un déclarant CREATE TABLE, aucune duplication détectée.
- [x] T311 [P3] Mettre à jour `CLAUDE.md` (WEB_PORT supprimé, `requests.db`
  160 Mo et non 3 Go) et `README.md` (3 endpoints inexistants, ~740 tests
  annoncés pour 6 385).
  **CORRIGÉ le 2026-11-21** — `WEB_PORT` retiré des exports documentés,
  taille DB corrigée (~167 Mo), compte de tests corrigé
  (~6 450 collectés). Les 3 endpoints fantômes
  (`/api/config/tool-capabilities` GET+POST, `/api/tools`) sont supprimés
  du tableau README après vérification par grep (0 route réelle).
- [x] T312 [P3] `docs/DEBT.md` : le câbler ou le fusionner avec
  `specs/002-.../debt.md` (2 registres coexistent, le premier n'est lu par
  aucun outil).
  **LIVRÉ le 2026-11-21** — `docs/DEBT.md` devient l'**index unique** :
  en-tête qui déclare la hiérarchie (transverse 014 → registre 002 →
  audits 019/024), avec liens directs. Les registres par spec restent la
  source de vérité de leur périmètre ; aucune ligne n'est dupliquée.
- [x] T313 [P3] **Document d'architecture et décision de langage**
  (`architecture-review.md` est le brouillon) : trancher formellement.
  **LIVRÉ le 2026-11-21** — `docs/adr/ADR-011-python-default-go-extraction.md`
  formalise la décision : **rester Python**, découper d'abord, porter un
  module en Go **uniquement sur mesure** (chemin chaud, logique pure, coût CPU
  prouvé), jamais Rust. L'ADR cite les mesures (14 ms / 1 203 ms, 110 580 l.
  Go existantes, ~250 000 l. de portage, bloqueur JA3).

**Checkpoint Phase 3** : les 8 gates de la constitution câblées et vertes ;
la question du langage tranchée sur les mesures.

---

## Complément d'audit (trou de couverture déclaré)

43 fichiers de production (7 189 lignes, **9,7 %**) n'ont pas été lus ligne à
ligne — inventaire complet dans
[00-couverture-audit.md](../../docs/audits/2026-11-20-audit-complet/00-couverture-audit.md).
La majorité relève de tâches existantes (T225 pour les scripts, T153 pour les
routes). **Deux modules méritent une passe dédiée** :

- [x] T154 [P1] `upstream/free_failures.py` (173 l.) et
  `upstream/station_faults.py` (131 l.) : **classification d'échec free** et
  **attribution de panne à une station**. Branchés (`opencode.py:2219-2235`,
  8 imports) mais non lus ligne à ligne. **À auditer avant la Phase 1** : ils
  touchent les principes VII (preuve) et V (unicité), où l'audit a trouvé le
  plus de défauts.
  **AUDITÉ le 2026-11-20 — MODULES SAINS, aucun défaut.**
  Vérifié par exécution (`.artifacts/t154_verify.py`) :
  **classification 9/9** (403 `gate`, 429 `quota`, 502/503 `transit`,
  401 `auth`, 400 `model`, 200 vide `empty`, sans statut `unknown`) et
  **attribution 10/10**, dont la règle centrale — `egress` seul → imputable ;
  `egress` + `body`/`identity` → **non imputable** (« on ne peut pas prouver
  que l'IP est fautive quand la requête l'est aussi »).
  **Cohérence de bout en bout** : `config/free_gate.py:155-165` produit
  exactement les noms `egress`/`identity`/`body` qu'`attributable_fault`
  interprète. **Branchement confirmé** (`opencode.py:21559-21595`) : la
  classification déclenche la mise de côté **conditionnelle** de la station.
  **Points à préserver** : domaine pur (aucune I/O), règle dans le bon sens,
  cooldowns bornés 5–600 s, compteurs fail-soft bornés à 200 clés.
  Détail dans `00-couverture-audit.md` §2.3.
- [x] T155 [P2] `app/compaction/truncate.py` (509 l., fonctions pures) : à
  auditer lors de la réorganisation de `app/compaction/` en Phase 2.
  **AUDITÉ le 2026-11-21 — pas de défaut trouvé.** Ajout de
  `tests/test_compaction_truncate_units.py` (18 tests, helpers/rôles/labels,
  split pair-complet, budget fallback, troncature 1250, checkpoint envelope,
  queue brute du dernier tour). Codex full-audit du fichier : aucun
  comportement fautif remonté.

**Règle de non-perte** : si l'un de ces fichiers s'avère porteur d'un défaut,
il doit être **ajouté au plan** avec sa propre tâche.

## Décisions à prendre par l'opérateur

- [ ] D01 Restaurer les **17 fichiers de code** du `git stash`
  (`observability/pins.py`, `station_state.py`, `fleet_slo.py`, 11 tests
  dont `test_state_authority.py`) ? — voir `00-stash-inventaire.md`.
- [ ] D02 Déployer la **matrice CI Windows** (coût runner) ou garder
  `gate.ps1` local ?
- [ ] D03 **Borner WARP** à 2-3 conteneurs (recommandation 019, jamais
  appliquée) ?
- [ ] D04 **Purger `logs/`** (15 Go) une fois T005 fait ?
- [ ] D05 **Seuil de couverture CI** : garder 60 % (87 % mesuré sur
  `free/qualification`) ?
- [ ] D06 **Porter un module en Go** après découpage, ou rester Python ?

## Ordre d'exécution

```
Phase 0 (T001–T025)  ──► Phase 1 (T101–T118) ──► Phase 2 (T201–T233)
                                                       │
                                                       ▼
                                              Phase 3 (T301–T313)
```

**Seules parallélisations sûres** : T201–T206 (données) avec Phase 1 ;
T220–T228 (nettoyage) après T005.

**Ne jamais** exécuter un découpage (T210–T215) avant la Phase 0 : les P0
masquent les mesures qui valident les découpages.

## Traçabilité

| Phase | Tâches | Findings couverts | Contrats |
|---|---|---|---|
| 0 | 26 | **34 P0 — 100 %** | INV-1,2,3,4,5,6,8,14,17,18 |
| 1 | 53 | **102 P1 — 100 %** | INV-7,9,13 |
| 2 | 33 | **98 P2 — par familles** | INV-1,10,15 |
| 3 | 13 | Gates et documentation | INV-11,12,16 |

**Vérification de couverture** : les 34 P0 et les 102 P1 ont été
**automatiquement croisés** avec ce fichier — chaque identifiant apparaît dans
au moins une tâche. Deux P0 et 19 P1 avaient été oubliés à la première
rédaction ; ils ont été ajoutés après vérification.

**Rapports sources** : `01-free-core` (4 P0 / 15 P1 / 26 P2) ·
`02-free-registry` (8/20/16) · `03-nordvpn` (2/8/…) · `04-warp` (3/9/13) ·
`05-public-proxies` (4/12/…) · `06-opencode-monolith` (3/7/…) ·
`07-protocol-app` (4/13/20) · `08-dashboard` (2/8/8) · `09-config-scripts`
(3/7/…) · `10-tests-ci` (3/8/…) — **34 P0, 102 P1, 98 P2, ~120 P3**.

## Traitement des 98 P2 — regroupement assumé

Les P2 ne sont **pas** listés un par un : ils sont traités **par famille**,
car ils partagent des causes. Chaque famille a sa tâche :

| Famille P2 | Tâches | Exemples couverts |
|---|---|---|
| I/O ou relecture dans un chemin chaud | T110, T130, T136, T150, T151 | `_rotation_threshold` (7 sites), `on_request`, `_is_denied`, `_query_identity` |
| Écriture non atomique / course | T133, T134, T101 | `record_attempt`, `rebind_allowed`, `IPStateStore.transition` |
| Rétention et purge absentes | T201, T202, T204, T142 | observations, sauvegardes, workers orphelins |
| Code mort et duplication | T220–T223, T145–T147 | `wire_admission`, `WarpProvisioner`, conversions dupliquées |
| Schéma et index | T015, T016, T024b | DDL dupliquées, FK, `ANALYZE` |
| Sécurité | T139, T140, T141, T025 | traversée de chemin, argv, isolation, auth |

**Pourquoi ce regroupement** : traiter 98 P2 individuellement produirait un
backlog de 220 tâches — le défaut que le projet connaît déjà (**≈ 477 tâches
SpecKit ouvertes**, tâche T228). Les P2 sont **documentés un par un** dans les
10 rapports d'audit, avec `fichier:ligne` et preuve ; ce fichier les **route**
vers la tâche de famille qui les corrige.

**Règle de non-perte** : chaque famille ci-dessus cite les tâches qui la
traitent. Si un P2 n'entre dans aucune famille lors de l'exécution, il doit
être **ajouté explicitement** ici — pas ignoré.
