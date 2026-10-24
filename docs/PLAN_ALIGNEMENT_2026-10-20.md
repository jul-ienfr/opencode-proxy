# PLAN d'alignement exhaustif — proxy `opencode-proxy` ↔ client officiel OpenCode

**Date :** 2026-10-20
**Référence :** `anomalyco/opencode` v1.19.3 (CLI) / desktop 2.0.32
**Audit source :** [`AUDIT_ALIGNEMENT_CLIENT_OFFICIEL_2026-10-20.md`](AUDIT_ALIGNEMENT_CLIENT_OFFICIEL_2026-10-20.md)
**Périmètre :** 9 vagues + un volet dédié à la jambe gratuite
**Hors périmètre :** suppression de fonctionnalités du proxy (web search, compaction, dashboard, rotation de clés, VPN)

---

## 0. Définition du succès

Le proxy est « aligné » quand les six affirmations suivantes sont **prouvées par un test automatisé**, pas par une relecture :

| # | Critère | Mesure |
|---|---|---|
| S1 | **Routage** : pour les 62 modèles exposés, l'endpoint et le protocole sont ceux que le catalogue officiel prescrit | matrice `modèle → (npm, api, protocole)` comparée au catalogue live : **100 % de correspondance** sur les ids exposés |
| S2 | **Corps** : pour une même requête client, le corps émis ne diffère du corps officiel que par des champs déclarés | test de diff de corps contre des fixtures « golden » officielles |
| S3 | **Identité** : les en-têtes émis sont un sous-ensemble de ceux du client 1.19.3, plus les ajouts documentés | test d'en-têtes verrouillé (liste blanche) |
| S4 | **Reprises** : politique identique à l'officiel sur les statuts communs (2 tentatives, backoff, `retry-after`) | test de séquence de reprises sur serveur factice |
| S5 | **Erreurs** : classification identique (InvalidRequest / ProviderInternal / Unknown), 4xx non rejoués | table de classification exhaustive |
| S6 | **Jambe free** : la porte est modélisée explicitement (3 facteurs), testée, et un 403 est **diagnostiquable** en un coup d'œil | test de régression de la porte + raison de refus structurée |

**Non-objectifs** (assumés) : parité 1:1 du wire free (impossible : le proxy n'est pas le client), alignement de l'UA free (contrainte inexistante, cf. audit §4.2), suppression du pool de tunnels.

---

## 0 bis. Variante « sans clé API payante » (situation actuelle)

La clé `OPENCODE_API_KEY` est expirée et **aucune clé valide n'est disponible**. V1.1 reste bloquant pour *valider* la jambe payante, mais **il ne bloque plus l'exécution** : la séquence est réordonnée pour que tout ce qui est validable le soit sans clé.

**Ce qui ne dépend PAS de la clé** (donc exécutable maintenant) :

| Élément | Dépendance réelle |
|---|---|
| V0 en entier | aucune |
| V2 routage par modèle | `models.opencode.ai/api.json` est **public** — aucune clé |
| V3.1 `max_tokens`, V3.3 `reasoning_content`, V3.2b `store` | wire mesuré + jambe free |
| V4 identité, en-têtes, reprises | wire mesuré (`capture.jsonl`) + jambe free |
| V5 streaming/erreurs/usage | jambe free |
| V6 en entier | jambe free |
| V7, V8 en entier | aucune |

**Ce qui reste suspendu** (à faire au retour d'une clé) : V1.2 capture de `console/api/config` (les `options.body`/`options.headers` par modèle servis par l'éditeur), et la validation bout-en-bout de la jambe payante.

**Méthode de substitution :** la jambe payante est validée contre des **fixtures enregistrées** (`tests/fixtures/official/`) et un **serveur de capture local** (`_research/capture_server.js`), jamais contre l'API réelle. Un `strict` mode (`upstream.paid_validation: fixture|live`, défaut `fixture`) empêche tout test de dépendre d'un secret.

**Nouvel ordre d'exécution :** V0 → **V6.1/V6.2** (modèle de porte) → **V4** (identité) → **V3.1/V3.3** → **V2** (routage) → V5 → V7 → V8 → *(V1 quand une clé arrive)*.

**Conséquence sur la définition du succès :** S1–S6 restent valables, mais **S2/S3/S4 sont prouvés sur la jambe free + fixtures** au lieu de l'être sur la jambe payante en live.

---

## 1. Règles de travail (à respecter dans chaque vague)

1. **Mesurer avant de changer.** Toute vague qui touche le fil commence par une capture ou une fixture de référence. Aucun « je pense que ».
2. **Une vague = un commit vert.** Tests existants verts + nouveaux tests de la vague verts. Pas de vague partielle laissée ouverte.
3. **La jambe free est protégée par un filet avant d'être touchée.** Aucune modification du corps free sans que le test de porte (§4.2) passe avant ET après.
4. **La configuration gagne toujours contre les littéraux.** Toute URL ajoutée doit venir de `config.yaml`/env, jamais d'une chaîne en dur.
5. **Pas d'empilement de documentation.** Les documents d'audit antérieurs (`PLAN_ALIGNEMENT_CLIENT_OFFICIEL_2026-10-09.md`, `COMPARAISON_PROXY_OFFICIEL_2026-10-13.md`) sont archivés en lecture seule : ils décrivent une version du client qui n'existe plus (`packages/ai/`).

---

## 2. Vue d'ensemble

| Vague | Titre | Écarts traités | Risque | Effort |
|---|---|---|---|---|
| **V0** | Instrumentation & garde-fous | — (prérequis) | nul | 1 j |
| **V1** | Déblocage opérationnel | P0-1, P1-7 | nul | 0,5 j + attente clé |
| **V2** | Catalogue piloté par la donnée | P0-3, P0-4, P0-5, P1-8 | moyen | 3 j |
| **V3** | Corps & raisonnement | P0-2, P1-1, P1-2, P2-3 | moyen | 2 j |
| **V4** | Identité & reprises | P1-3, P1-4, P1-9, P2-4 | faible | 1,5 j |
| **V5** | Streaming, erreurs, usage | P1-6, P2-1 | moyen | 2 j |
| **V6** | Jambe free industrielle | P1-5, P1-8, §9 audit | **élevé** | 4 j |
| **V7** | Maintenabilité | P0-4, §10 audit | moyen | 3 j |
| **V8** | Observabilité & CI | — | faible | 1,5 j |
| **V9** | Parité étendue (optionnelle) | gemini, variants, sous-sessions | moyen | 3 j |

**Ordre imposé :** V0 → V1 → V2 → (V3 ‖ V4) → V5 → V6 → V7 → V8 → V9.
**V6 ne démarre pas avant V0** (test de porte) et **V3** (`reasoning_content`), car les deux modifient le corps free.

---

## 3. Détail des vagues

### V0 — Instrumentation & garde-fous

**Objectif :** rendre les écarts mesurables et non-régressionnables avant la première modification.

**Tâches**

- [ ] **V0.1 — Enregistreur de fil.** Un module `observability/wire_record.py` qui, pour chaque requête sortante, persiste : URL, en-têtes ordonnés, clés du corps, taille, empreinte sha256, statut, latence, station/IP de sortie. Sortie JSONL sous `logs/wire/`.
  *Pourquoi :* le proxy n'a aujourd'hui qu'un dump de clés sur les réponses 400 (`[free-400-wire]`), insuffisant pour comparer au wire officiel.
- [x] **V0.2 — Test de porte free (le filet).** `tests/test_free_wire_complet.py` **réécrit** : il encode les trois facteurs mesurés (IP via tunnel, identité de session, `stream:true` + `tools[read]`) **et** les non-facteurs (version d'UA, quartet legacy). **14 tests, verts.**
  *Base :* `opencode.py:5389-5480` (`_official_free_headers`), `:5047+` (`_free_wire_body`).
  *Ce qui a été appris en l'écrivant :* la version précédente verrouillait une liste de 12 en-têtes mesurée sur le client 1.18.31 — elle serait devenue un piège (elle fige un wire que le client n'émet plus). Remplacée par des assertions **sémantiques** sur la porte.
  *Décision encodée :* le quartet `x-opencode-client/project/request/session` **n'est pas** un facteur — un test le vérifie explicitement pour qu'on ne le réintroduise pas « par prudence ».
- [x] **V0.2b — Modèle de porte explicite.** `config/free_gate.py` : `EgressReputation ∧ SessionIdentity ∧ BodyShape` + `GateVerdict` qui **nomme le facteur en échec** (`failed_factors`) et une forme `as_log()` pour les logs structurés. Aucune URL, aucune constante d'identité — uniquement des prédicats. Couvert par 8 tests unitaires (facteurs indépendants, non-facteurs, attribution d'un 403 IP à `egress`).
- [ ] **V0.3 — Instantané de catalogue.** `scripts/snapshot_catalog.py` : télécharge `models.opencode.ai/api.json`, écrit `docs/_catalog_snapshot_<date>.json` + un résumé diffable (providers `opencode`/`opencode-go` uniquement).
- [ ] **V0.4 — Fixtures « golden » officielles.** Les figer depuis la **capture réelle** (`_research/oc-1.19.3/capture.jsonl`, produite par le binaire officiel 1.19.3 contre `_research/capture_server.js`) et depuis `_research/official-1.19.3/`. Trois fixtures : `chat_with_tools`, `chat_no_tools`, `messages_anthropic`.
- [x] **V0.5 — Table de classification d'erreurs de référence.** ✅ **Fait** — `tests/fixtures/error_classes.yaml` (29 cas) + `core/errors.py:classify_status_class` reproduisent `executor.ts:225-275` : content-policy d'abord, puis 401, 403, 429 (quota vs rate limit selon le corps), 400/404/409/413/422, ≥500 ou rejouable (429/503/504/529), puis inconnu. `is_context_overflow` reprend les 27 motifs et les 3 exclusions de `provider-error.ts`. **Écart réel corrigé** : un 429 sans motif sortait en `api_error` (donc jamais réessayé) → `rate_limit_error` ; un 401 sans message → `authentication_error` ; et le motif `try again` attrapait « rate limit … try again » en `overloaded_error` alors que c'est du débit. 29 tests + 3 de non-régression sur l'ancienne table.
- [ ] **V0.6 — Manifeste de dérive.** Étendre `docs/_drift_manifest.json` avec les 6 critères S1–S6 et leur état courant (tout à `false` au départ).

**Critères d'acceptation**
- `pytest tests/test_free_gate_model.py` passe et échoue si on retire `stream:true`, si on retire `tools[read]`, ou si on retire `x-opencode-session`.
- Un `logs/wire/` contient ≥ 20 requêtes réelles après un smoke test.
- Le snapshot de catalogue est comparable d'un jour à l'autre (`diff` lisible).

**Rollback :** aucun (ajout pur, aucun chemin de production modifié).

---

### V1 — Déblocage opérationnel

**Objectif :** rendre la jambe payante mesurable et le proxy diagnostiquable.

**Tâches**

- [ ] **V1.1 — Remplacer la clé expirée.** `OPENCODE_API_KEY` du `.env` renvoie `401 ApiKeyExpired`. Générer une clé sur OpenCode Console, la poser, vérifier : `GET /console/api/config` → 200, et `POST /inference/go/openai/v1/chat/completions` → 200.
  *Sans cela :* la moitié des écarts (P0-1, catalogue serveur, options de corps) restent non mesurables.
- [ ] **V1.2 — Capturer la configuration serveur.** Une fois la clé valide, `GET /console/api/config` → `_research/official-1.19.3/console-config.json`. C'est **la** source de vérité du corps (`options.body`, `options.headers` par modèle).
- [ ] **V1.3 — Réparer ou désactiver les scrapers.** `model limits from docs` et `quota OpenCode Go` échouent au boot (`Failed to fetch model limits from docs`, `Quota fetch failed … page format may have changed`).
  *Décision :* désactiver par défaut avec un statut explicite dans `/health`, plutôt que de laisser une panne silencieuse. La réparation du scraping est un chantier séparé (la page n'est pas notre contrat).
- [x] **V1.4 — Installer le client 1.19.3 localement** (parité de version). **Fait**, avec une nuance : l'installation **globale** `npm install -g opencode-ai@1.19.3` est refusée par l'environnement (`EPERM` sur `%APPDATA%\npm` et sur le cache npm). Le binaire officiel de la release a donc été installé **dans l'espace de travail** :
  ```
  _research/oc-1.19.3/bin/opencode.exe --version   →  1.19.3
  ```
  avec `XDG_*` redirigés vers `_research/oc-1.19.3/xdg/` (le client écrit son état sinon dans `%USERPROFILE%`). Procédure de re-mesure complète dans [`_research/oc-1.19.3/README.md`](../_research/oc-1.19.3/README.md). Artefacts lourds exclus du dépôt (`.gitignore`).
  *Pour une installation globale, côté machine :* `npm install -g opencode-ai@1.19.3` hors bac à sable. Le desktop officiel **2.0.32** est déjà installé et à jour.
- [ ] **V1.5 — Documenter la procédure de capture** (mitmproxy devant le client, CA jetable) dans `docs/runbook-capture-wire.md`, y compris la façon de rejouer une capture.
  *Note :* la capture a finalement été obtenue **sans mitmproxy** — un provider `capture` dans la config du client pointe sur un serveur HTTP local (`_research/capture_server.js`), ce qui est plus simple et sans CA. La procédure existe dans `_research/oc-1.19.3/README.md` ; reste à la promouvoir en runbook.

**Critères d'acceptation**
- `/health` expose `paid_config: ok|api_key_expired|unreachable` et `scrapers: ok|disabled`.
- `console-config.json` présent et versionné (sans secret : le fichier ne contient pas de clé).
- `opencode --version` = 1.19.3.

**Rollback :** clé précédente conservée dans le gestionnaire de secrets ; scrapers réactivables par config.

---

### V2 — Catalogue piloté par la donnée (P0-3, P0-4, P0-5, P1-8)

**Objectif :** que la décision `modèle → endpoint + protocole` vienne du catalogue, pas d'une table maintenue à la main.

**Constat de départ :** 46/128 modèles free couverts, 5 mismatches de protocole, 10 modèles gemini non routables, 4 littéraux `inference/v1` dans `opencode.py`.

**Tâches**

- [x] **V2.1 — Modèle de données.** **Fait**, dans `config/models_catalog.py` (pas un fichier neuf : c'est là que vivait la croyance fausse). Quatre fonctions pures, sans effet de bord :
  ```python
  model_provider_block(catalog, provider, model_id) -> dict
  model_api(catalog, provider, model_id)   -> str | None   # surcharge modèle, sinon provider
  model_npm(catalog, provider, model_id)    -> str | None
  api_family_for_url(url)                  -> str | None   # par marqueur d'URL
  api_family_for_model(...)                -> str | None   # URL d'abord, npm ensuite
  ```
- [x] **V2.2 — Résolution par modèle.** **Fait pour la couche catalogue.** La règle est appliquée : `npm`/`api` du **modèle** priment sur ceux du provider. Le commentaire qui affirmait l'inverse (« le `npm` est ABSENT au niveau modèle : aucune entrée de `models:` n'en porte ») est corrigé — c'était **l'erreur racine** de P0-3.
  *Vérifié sur l'instantané réel :* 128/128 modèles free et 33/33 payants portent une URL `api` ; 66 free portent un `npm` de modèle.
  **Reste à faire (lot V2.2b, séparé) :** brancher cette résolution dans `config/settings._resolve_model_endpoint` pour que le **routage** l'utilise, au lieu de la table `config.yaml`. C'est le point où le risque change de nature : la couche catalogue est pure et testée offline ; le routage touche le chemin de production.
- [x] **V2.2a — Familles manquantes.** `NPM_TO_API` ne connaissait ni `@ai-sdk/google` ni `@ai-sdk/mistral` : 10 modèles gemini n'avaient **aucune** route dérivable et le `npm` était traité comme inconnu. Ajoutés, avec `KNOWN_API_FAMILIES` et un marqueur d'URL (`/anthropic/`, `/google/`) plus fiable que la table.
  *Règle encodée :* quand l'URL et le `npm` se contredisent, **l'URL gagne** — c'est elle qui reçoit la requête (test dédié).
- [x] **V2.2b — Brancher la résolution dans le routage.** **Fait, avec un périmètre volontairement étroit.**
  Trois pièces :
  1. `scripts/refresh_catalog_routes.py` → `config/catalog_routes.json` (161 modèles, `{family, protocol, base_url}`), `--check` pour détecter une dérive du catalogue sans l'appliquer ;
  2. `config/settings.py` : `catalog_route_for()`, `_catalog_override()`, `_legacy_endpoint()` (l'ancienne heuristique, extraite telle quelle et **inchangée**) ;
  3. le protocole **et** l'endpoint sont corrigés ensemble dans la boucle qui construit `MODELS`.
  **Règles de priorité encodées :** `endpoint:` explicite > `api:` explicite > catalogue > heuristique historique. Le catalogue ne renverse jamais une décision humaine ; il comble ce que la config ne dit pas.
  **Effet mesuré aujourd'hui : 1 seul modèle change** — `minimax-m3.1-flash-free`, déclaré `anthropic` alors que son endpoint est un chat : il partait avec un corps Anthropic vers `/chat/completions`. Son endpoint free, lui, est **conservé** (il répond 200).
- [x] **V2.2c — Le catalogue ne franchit jamais la frontière des jambes.** Le même id peut porter une famille **différente** par jambe (`minimax-m2.7` : chat côté free, messages côté payant). Un repli inter-jambes envoyait le trafic payant sur la base free — silencieusement. `catalog_route_for` ne consulte que la jambe demandée.
- [x] **V2.3 — Bases configurables par protocole.** **Fait, mais l'écart était bien plus petit qu'annoncé.** Vérification : les trois bases principales venaient déjà de `config.yaml` et `systemone` était déjà surchargeable par env — il n'y avait **aucune** affectation de littéral `https://opencode.ai/…` dans `opencode.py`. Le seul trou : les deux endpoints **Responses**, seuls littéraux non surchargeables.
  - `config.yaml > upstream` : ajout de `responses_base` et `responses_free_base`, lus par `config/settings.py` via `yaml_get` (avec l'ancienne valeur en défaut, donc zéro changement de comportement).
  - `tests/test_upstream_bases_configurable.py` — **15 tests** : les 5 bases suivent la config, les jambes restent distinctes, et un **garde-fou regex** interdit le retour de la forme `variable = "https://opencode.ai/…"`. Une valeur par défaut dans `yaml_get(...)` reste autorisée : elle est surchargeable, donc sans danger.
- [ ] **V2.4 — Un seul résolveur.** Supprimer les 4 littéraux `https://opencode.ai/inference/v1` dans `opencode.py` et les remplacer par la résolution V2.2. Vérifier par `grep` qu'il ne reste **aucune** URL upstream en dur hors de `config/`.
- [x] **V2.5 — Protocole gemini : décision explicite.** ✅ **Fait** — refus `501` nommant le modèle et la famille, dans le protocole du client (voir journal). Le repli silencieux vers `/chat/completions` est supprimé.
- [ ] **V2.6 — Exposer les ids free manquants** (23) : `glm-5-free`, `hy3-free`, `kimi-k2.5-free`, `nemotron-3-super-free`, `ring-2.6-1t-free`, `minimax-m2.1-free`, `minimax-m2.5-free`, `minimax-m3-free`, `minimax-m3.1-flash-free`, `qwen3.6-plus-free`, `glm-4.7-free`, `laguna-s-2.1-free`, `ling-2.6-flash-free`, `ling-3.0-flash-free`, `ling-3.0-tiny-free`, `longcat-2.0-free`, `mimo-v2-flash-free`, `mimo-v2-omni-free`, `mimo-v2-pro-free`, `north-mini-code-free`, `trinity-large-preview-free`, `x-preview-f-free`, `hy3-preview-free`.
  *Contrainte :* chaque id ajouté doit avoir été **vérifié en live** (200) avant d'entrer dans la table. Pas d'ajout « au catalogue » sans preuve.
- [x] **V2.7 — Test de la matrice.** **Première moitié faite.** `tests/test_catalog_model_routing.py` — **22 tests** contre l'instantané réel du catalogue :
  - chaque modèle porte une URL `api` (128/128 free, 33/33 payant) ;
  - la répartition des familles est verrouillée (free : 63 chat / 24 messages / 32 responses / 9 gemini ; payant : 21 / 6 / 5 / 1) — toute dérive de l'éditeur fait échouer le test ;
  - les 3 modèles free hébergés en `/anthropic/` sont identifiés (cause des 404) ;
  - refus explicite (`None`) plutôt que repli silencieux sur `/chat/completions` ;
  - l'URL gagne sur un `npm` contradictoire.
  *Seconde moitié, après V2.2b :* comparer le routage **effectif** du proxy (et non la couche catalogue) à cette vérité — c'est là que S1 (100 % de correspondance) sera mesuré.

**Critères d'acceptation**
- `pytest tests/test_routing_matrix.py` vert avec 0 mismatch.
- `grep -c 'https://opencode.ai' opencode.py` = 0.
- Un modèle gemini demandé renvoie 501 avec un message explicite (plus de 400/403 trompeur).

**Risque & rollback :** le basculement d'endpoint peut casser la jambe payante si la clé est morte (V1 bloquant). *Rollback :* `upstream.legacy_bases: true` réintroduit les anciennes bases plates pendant un cycle.

---

### V3 — Corps & raisonnement (P0-2, P1-2, P2-3, P2-7)

**Objectif :** que le corps émis soit celui du client 1.19.3, à l'exception des shims free documentés.

**Tâches**

- [x] **V3.1 — `max_tokens` : DÉJÀ ALIGNÉ (mesuré).** Sonde du corps free réel (`_free_wire_body`) : le proxy émet `max_tokens`, dans l'ordre officiel `model, max_tokens, messages, tools, tool_choice, stream`. L'écart P1-2 de l'audit était **faux**. `max_completion_tokens` n'existe que dans la couche de conversion (`app/protocol/mapping.py:285`) et uniquement pour les amonts qui l'exigent (o-series / gpt-5), avec son propre test. **Rien à faire.**
- [x] **V3.2 — `tool_choice` : NE RIEN CHANGER (aligné).** La mesure montre `tool_choice: "auto"` dès qu'il y a des outils, exactement ce que fait `_free_wire_body` (242/243 requêtes mesurées). Le retirer serait une **régression**. Un test le documente (`test_gate_*`, `test_free_wire_complet.py`).
- [x] **V3.2b — `store` : DÉJÀ ALIGNÉ (mesuré).** `grep '"store"' opencode.py` → aucune occurrence : le proxy n'émet **jamais** `store`, et le client non plus. Le P2-7 de l'audit était **faux**. Rien à faire, rien à configurer.
- [x] **V3.3 — `reasoning_content` multi-tours.** **Fait**, avec un énoncé corrigé par la mesure : le champ n'était pas *perdu* (il survit tel quel), il n'était pas **synthétisé** quand le client l'omettait. Correctif : `_free_wire_body` applique `mapping.ensure_deepseek_reasoning` (défaut `" "` pour les modèles deepseek), import paresseux, jamais bloquant.
  - Vérifié en live : la requête qui produisait le 400 (`deepseek-v4.1-flash-free`, assistant sans `reasoning_content`) renvoie désormais **200** (`_research/live_reasoning_fix.py`).
  - 3 tests de verrouillage dans `tests/test_free_wire_complet.py` : préservation à l'identique, pose du défaut deepseek, non-pollution des autres modèles.
- [ ] **V3.4 — `prompt_cache_key`.** Émettre `prompt_cache_key` (ou `providerOptions.openai.promptCacheKey` selon le protocole) à partir de la session, comme l'officiel (`session/runner/llm.ts:206`). Gain attendu : cache de prompt côté passerelle.
  *Note :* `_free_wire_body` pose déjà `prompt_cache_key` dans certains cas (l. 5140-5145) — à auditer avant d'ajouter quoi que ce soit.
- [ ] **V3.5 — Test de diff de corps.** `tests/test_body_parity.py` : pour un jeu de requêtes client (texte, outils, image, thinking), comparer le corps émis aux fixtures golden officielles ; les différences autorisées sont déclarées dans une liste blanche (`tests/fixtures/body_diff_allowlist.yaml`).

**Critères d'acceptation**
- ~~Aucun `max_completion_tokens` émis sur la jambe payante~~ → **sans objet** : le wire free émet déjà `max_tokens` ; la conversion conditionnelle o-series est un comportement voulu et testé.
- Le rejeu `reasoning_content` renvoie 200. ✅
- `test_body_parity.py` vert, allowlist ≤ 5 entrées, chacune justifiée.

**Rollback :** drapeaux `protocol.emit_max_tokens`, `protocol.echo_reasoning_content`, `protocol.inject_tool_choice_free` dans `config.yaml`, tous par défaut à l'ancien comportement pendant la transition.
*Note :* pour V3.3 le rollback utile n'est pas un drapeau mais la suppression de l'appel `ensure_deepseek_reasoning` — il est isolé dans `_free_wire_body`, en fin de fonction, et ne peut pas affecter les autres chemins.

---

### V4 — Identité & reprises (P1-3, P1-4, P1-9, P2-4)

**Objectif :** un seul schéma d'identité, celui de 1.19.3, et une politique de reprise identique.

**Tâches**

- [x] **V4.1 — UA aligné sur la mesure.** **Fait.** `_OPENCODE_OFFICIAL_UA` vaut désormais la chaîne **mesurée** sur le binaire officiel :
  ```
  opencode/1.19.3 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14
  ```
  Le commentaire périmé (« UA 1.19.0 → 403, ne pas re-bumper ») est remplacé par la mesure du 2026-10-20 : **la version d'UA n'est pas un facteur de la porte**, donc un test ne doit jamais en dépendre (`test_ua_version_is_not_a_gate_factor`).
  *Reste à faire :* alimenter la version par configuration (`identity.version`) plutôt que par constante — **fait à moitié** : la surcharge `OPENCODE_OFFICIAL_UA` existe déjà, la décomposition en `identity.{version,provider_utils,bun}` est reportée (gain cosmétique, risque nul).
  *Décision documentée :* `_OPENCODE_OFFICIAL_UA_RESPONSES` est **conservé** sur `1.18.31 / provider-utils 4.0.40` — `/responses` n'a pas été re-capturé sur 1.19.3, et la version n'étant pas un facteur, le bumper à l'aveugle serait fabriquer une combinaison qui n'existe pas dans la nature.
- [x] **V4.2 — Jeu d'en-têtes officiel (jambe free).** **Fait**, sous drapeau. `_official_free_headers` émet :
  ```
  Authorization, Content-Type, User-Agent,
  x-opencode-session-id, x-session-affinity, x-session-id,
  Connection, Accept, Host, Accept-Encoding
  ```
  Le quartet `x-opencode-client/project/request/session` est **retiré**. Rollback à chaud : `OPENCODE_FREE_WIRE_STYLE=legacy` (testé par `test_legacy_style_restores_the_1_18_wire` et `test_free_wire_legacy_style_is_rollbackable`).
  *Vérifié en live après changement :* **200** sur la passerelle via tunnel (`_research/gate_live_new_wire.py`), diagnostic de porte `{"passed": true, "failed": [], "identity": true, "body": true}`.
  *Périmètre :* la **jambe payante** (`_enrich_paid_wire_headers`) n'est **pas** touchée : aucune clé valide, donc aucun moyen de valider le changement. Le retrait y est planifié mais **bloqué sur V1.1** — c'est une décision, pas un oubli.
- [x] **V4.2b — Session identique au client.** **Fait.** Les trois en-têtes portent la **même** valeur (`ses_<12 hex><14 base62>`), dérivée de la conversation (`_conversation_session_id`) et non du processus. Testé : `test_free_wire_uses_official_session_identity` (unicité de valeur), `test_official_headers_conversation_key` (stabilité par conversation).
- [x] **V4.3 — Politique de reprise officielle.** **Fait pour le cœur de la politique** ; la partie « rotation de station » est reportée à V6.3.1 (c'est une décision de la jambe free, pas une reprise HTTP).
  - `config.yaml` : `upstream.max_retries` **4 → 2**, et `streaming.retry_attempts` aligné à **2** (repli historique : laisser les deux clés divergentes ramenait silencieusement le budget à 4).
  - Nouvelle source unique `upstream/retry.py` :
    ```python
    RETRYABLE_STATUSES = {429, 503, 504, 529} | set(range(500, 600))
    is_retryable_status(status) -> bool
    ```
    Remplace l'ensemble inline `{500,502,503,504,499,408,409,425,529}` de `opencode.py`. **Quatre divergences corrigées** : 409 n'est plus rejoué (l'officiel le classe `InvalidRequest`), 408/425/499 non plus (conditions de transport), **429 devient rejouable partout** et **tout 5xx** l'est (501/505+ étaient ignorés).
  - Jitter/plafond/`retry-after` : déjà conformes (`compute_retry_delay`, plafond 600 s) — vérifiés, pas modifiés.
- [x] **V4.4 — Test de séquence de reprises.** `tests/test_retry_parity.py` — **30 tests**, instantanés (aucune horloge réelle) : chaque statut rejouable/non-rejouable de l'officiel, la règle « tout 5xx », la non-régression 409, l'illisibilité d'un statut, le budget par défaut, la valeur de `config.yaml` (échoue si quelqu'un remonte le budget), la priorité de `retry-after` et le plafond.

> **Reste ouvert (assumé)** : `_free_bound = max(_upstream_max_retries(), len(API_KEYS)) + (stations_utiles − 1)`. Sur la jambe free, la rotation de station s'**ajoute** au budget HTTP, ce qui peut encore donner 6 tentatives. Ce n'est pas une reprise HTTP mais une décision de routage (changer d'IP de sortie), donc sa place est en **V6.3.1** (classification 403 gate / 429 quota / 5xx transit), pas ici. Corriger ici aurait mélangé les deux notions.

**Critères d'acceptation**
- ~~`grep -c 'provider-utils' opencode.py` = 0~~ → **critère retiré** : `provider-utils` fait partie de l'UA **officiel** du chemin ai-sdk (mesuré). L'exiger à zéro contredisait l'alignement. Remplacé par : l'UA émis est exactement la chaîne mesurée, et un test de forme (`UA_SHAPE`) échoue si elle est mal composée.
- Un 401 ne produit qu'**une** requête amont (test).
- Un 429 avec `retry-after: 2` attend ~2 s puis réessaie, 2 fois maximum.

**Rollback :** `retry.policy: legacy` (4 tentatives) pendant un cycle.

---

### V5 — Streaming, erreurs, usage (P1-6, P2-1)

**Objectif :** qu'un flux dégradé ne soit jamais présenté comme un succès, et que le comptage soit exact.

**Tâches**

- [x] **V5.1 — Flux amont vide = erreur.** ✅ **Fait** — point d'étranglement unique (`streaming/empty_guard.py`) : une réponse 200 dont le premier contenu est une erreur de flux vide, ou un flux sans octet, devient un **502**. Prouvé par harnais ASGI (3 tests) et 13 tests unitaires. Voir journal.
- [x] **V5.2 — Propagation des événements `error` amont.** ✅ **Fait** — deux défauts mesurés en direct, pas un. (1) **Forme** : la trame suivait le protocole **amont** ; un client Anthropic recevait `data: {"error":…}` + `data: [DONE]`, c'est-à-dire du JSON OpenAI dans un flux Anthropic — le SDK ignore les lignes sans `event:`, donc **aucune erreur visible**, juste un flux muet. La forme suit désormais le protocole du client (`client_protocol`, 9 sites d'appel). (2) **Type** : `server_error` explicite de l'amont était écrasé en `api_error` ; `extract_upstream_error_type(body, vocab)` relaie le type tel quel **si** le client le connaît (Anthropic n'a pas `server_error` → `api_error`, OpenAI l'accepte → `server_error`). 14 tests, forme et fidélité vérifiées en live sur les deux routes.
- [x] **V5.3 — Comptage d'usage.** ✅ **Fait pour la conversion Anthropic** — `cached_tokens` → `cache_read_input_tokens` vérifié en exécution (voir journal). Reste ouvert : `cache_creation_input_tokens` (aucun amont ne le fournit sur la jambe free) et le comptage indexé par `x-opencode-request` sur reprise.
- [x] **V5.4 — Tests SSE.** ✅ **Fait** — `tests/test_sse_contract.py` : ordre exact de la machine à états, reconstruction du texte, `stop_reason`, bloc `thinking` pour le raisonnement, bloc `tool_use` pour les outils, rien après `message_stop`.

**Critères d'acceptation**
- Un flux vide ne renvoie plus 200 silencieux.
- `usage` du flux = `usage` du non-flux pour la même requête (test comparatif).

**Rollback :** aucun risque client (le comportement actuel est un défaut).

---

### V6 — Jambe free industrielle (P1-5, P1-8, audit §9)

**Objectif :** transformer une mécanique empirique en sous-système explicite, testé, observable et performant. **C'est la vague la plus risquée : elle est protégée par V0.2.**

#### 6.1 Modéliser la porte (P1-5)

- [x] **V6.1.1** — `config/free_gate.py` : décrire la porte comme une **conjonction de trois prédicats nommés**, chacun testable isolément. **Fait** (voir V0.2b).
  ```
  Gate = EgressReputation(ip) ∧ SessionIdentity(headers) ∧ BodyShape(body)
  SessionIdentity = ∃(x-opencode-session-id | x-session-affinity | x-session-id)
  BodyShape       = stream is True ∧ exists(tools, name=="read" ∧ realistic(schema, description))
  ```
  *Fondé sur la mesure :* le quartet `x-opencode-client/project/request/session` n'est **pas** requis (audit §4.2).
- [x] **V6.1.2** — Raison de refus structurée. ✅ **Fait** — sur un `403 gate`, la porte est diagnostiquée sur les en-têtes et le corps réellement partis ; le verdict complet va au log (`_debug`), le **nom des facteurs en échec** est compté et exposé par `/health?detail=full` → `gate_factors`. Jamais envoyé au client final.
- [x] **V6.1.3** — Test unitaire par facteur. ✅ **Fait** — `tests/test_free_gate_factors.py`, 22 tests : chaque facteur a ses tests et échoue **seul** (`failed_factors == ["identity"]` etc.), ce qu'un unique `passed == False` ne saurait pas dire.

#### 6.2 Réduire la surface de maintenance

- [x] **V6.2.1 — Registre de shims déclaratif.** ✅ **Fait** — `config/free_shims.py` + `config.yaml > free.shims` avec `necessity` (`read: required`, `bash: optional`). Une entrée invalide (description < 40 car., `properties: {}`) est **écartée au chargement avec sa raison** ; le repli reste dans le code, donc un config amputé ne casse jamais la porte.
- [x] **V6.2.2 — Re-mesurer chaque shim et supprimer les non load-bearing.** ✅ **Fait** — mesure du 2026-10-21 (4 corps, même modèle, même tunnel) : aucun outil **403**, `read` seul **200**, `bash` seul **403**, `read` + `bash` **200**. `bash` **retiré** (registre, `config.yaml`, injection) : il ne remplace pas `read` et ne change pas le verdict en sa présence. Réintroduisible par `config.yaml > free.shims`. 9 tests qui portaient l'ancienne réalité ont été alignés avec la mesure en commentaire.
- [x] **V6.2.3 — Forçage `stream:true` : condition testée, non remplie.** ✅ **Mesuré** (2026-10-22, 3 corps, même modèle, même tunnel) : `stream:true` → **200**, `stream:false` → **403**, `stream` absent → **403**. La porte **exige** le stream : le forçage est **load-bearing** et la reconstitution JSON reste nécessaire. Aucun changement de code — et c'est le résultat utile : la mesure protège ce machinisme d'une « optimisation » future qui casserait la jambe free.

#### 6.3 Fiabilité

- [x] **V6.3.1 — Classification des échecs.** ✅ **Fait pour la mesure** — `upstream/free_failures.py` classe gate/quota/transit/auth/model/empty, expose `free_failure_kinds`, et `gate_factors` nomme le facteur refusé. **La politique de reprise n'est pas changée** : décision reportée sur les chiffres. Voir journal.
- [x] **V6.3.2** — Disjoncteur **par station** (et non global). ✅ **Fait** — `upstream/station_faults.py` : une panne n'est imputée à une station que si elle lui est **attribuable** (`egress` seul, timeout, connexion, transit). Un refus de forme (`body`/`identity`) n'écarte **personne**, puisque les cinq stations échoueraient pareil. Mise à l'écart via `_set_free_cooldown` (source de vérité unique, pas d'état dupliqué), compteurs exposés par `/health?detail=full` → `station_faults`.
- [ ] **V6.3.3** — Requêtes « hedged » optionnelles : après un délai seuil (ex. 8 s), envoyer la même requête sur une seconde station et garder la première réponse complète. *Condition :* idempotence de la requête (pas de tool call déjà exécuté en aval).
- [x] **V6.3.4 — SLO de latence.** ✅ **Fait** — `observability/slo.py` : verdict `ok` / `degraded` / `breached` / `unknown` sur les séries `leg_ms:*`, exposé par `/health?detail=full` → `slo`. Cible p50 < 4 s, p95 < 12 s. Mesure : **la jambe free viole la cible** (p50 4,3 s sur `jev-1.13-free`, 12,4 s sur `deepseek-v4.1-flash-free`) — le SLO dit donc la vérité plutôt que de rassurer. L'alerte active (au-delà) reste ouverte.

#### 6.4 Qualité

- [ ] **V6.4.1** — Servir les modèles free à protocole **anthropic** sur `/inference/anthropic/v1/messages` (`minimax-m2.5-free`, `minimax-m3-free`, `qwen3.6-plus-free`, `minimax-m2.1-free`). **Mesuré le 2026-10-20 : les quatre répondent `400 Model is unavailable` sur le chemin Anthropic correctement formé.** Le chemin fonctionne (l'erreur est au format Anthropic), le modèle est fermé aux anonymes. **Le gain attendu est donc nul** — ne pas rouvrir ce lot sans un nouveau relevé.
- [x] **V6.4.2** — `reasoning_content` sur la jambe free. **Fait en V3.3** (`_free_wire_body` → `ensure_deepseek_reasoning`), vérifié en live (200 là où c'était 400).
- [x] **V6.4.3** — Exposer les ids manquants après vérification live. **Fait, et la réponse est négative** : sur 81 ids non exposés testés en direct, **1 seul** rend 200 (`big-pickle`). Les autres : 32 × `401 Missing API key`, 21 × `400 Model is unavailable`, 27 re-testés → 401/400. Exposition automatique via `config/free_models_verified.json`.
- [x] **V6.4.4** — Vérifier la parité `usage`/`cache` sur la free. **Mesuré sur `big-pickle`** : `usage` complet et exact (`prompt_tokens: 230`, `completion_tokens: 28`, `reasoning_tokens: 23`). Le champ cache reste vide (`prompt_tokens_details: {}`) — **contrainte amont**, pas un défaut du proxy : l'inférence anonyme ne rapporte pas le cache. À documenter comme telle, ce qui est fait ici.

#### 6.5 Performances

- [ ] **V6.5.1** — Réutilisation de session curl_cffi par (station, profil) : déjà partielle (`_get_pooled_curl_session`), à mesurer.
- [ ] **V6.5.2** — Pré-chauffer les tunnels au boot (poignée de main TLS faite avant la première requête cliente).
- [x] **V6.5.3** — Budget de temps total par requête. ✅ **Fait** — `streaming/deadline.py`, middleware le plus externe : au-delà de `timeouts.request_budget_s` (900 s, `0` = désactivé), **504** explicite si rien n'est parti, trame `error` sinon. Chemins du tableau de bord et `/health` exemptés.

**Critères d'acceptation**
- Les 3 tests de facteur de porte passent et échouent séparément si on casse le facteur.
- Un 403 ne déclenche plus de reprise (compteur = 1).
- p50 < 4 s, p95 < 12 s sur 20 requêtes consécutives.
- 40+ modèles free servis (contre 20 aujourd'hui), dont 4 à protocole anthropic.
- `config/free_gate.py` sans aucun littéral d'URL.

**Rollback :** `free.gate_mode: legacy` restaure le comportement actuel (UA v1, 8 reprises, shims en dur) pendant un cycle. La config est lue au démarrage : un rollback = un redémarrage.

---

### V7 — Maintenabilité (audit §10)

**Objectif :** rendre les changements futurs possibles sans casser silencieusement un autre chemin.

- [ ] **V7.1 — Découper `opencode.py` (1 Mo, 20 074 lignes)** par domaine, dans l'ordre de risque :
  1. `identity/` (UA, en-têtes, sessions) — extrait de `:4540-5820`
  2. `free/gate.py` (porte + shims + corps) — extrait de `:4953-5170`
  3. `upstream/endpoints.py` (résolution URL) — remplacé par V2
  4. `protocol/stream.py` (SSE) — extrait des handlers
  *Méthode :* extraction mécanique, un commit par module, aucun changement de comportement, tests existants verts à chaque étape.
- [x] **V7.2 — Schéma de configuration validé.** ✅ **Fait** (la version utile d'abord) — `scripts/config_audit.py` croise `config.yaml` et le code : **257 clés de réglage**, 34 lues explicitement, **0 morte, 0 manquante** après correction. Deux clés inertes trouvées : `dashboard_trust.require_token` (lu seulement depuis l'environnement — un booléen de sécurité qui ne s'appliquait pas) désormais appliqué avec précédence env > YAML, et `server.log_dir` supprimé (jamais lu). `--strict` en CI bloque toute nouvelle clé morte ; 12 tests, dont deux qui vérifient qu'une faute de frappe est bien détectée.
- [x] **V7.3 — Interdiction des littéraux.** ✅ **Fait, en cliquet** — `tests/test_no_hardcoded_urls.py` : 10 littéraux inventoriés et justifiés (un par ligne, avec sa raison), refus de **tout nouveau**, et refus d'une entrée devenue obsolète. Vérifié par injection : un littéral ajouté fait bien échouer le test. Choix assumé : `Origin: "https://opencode.ai"` reste littéral, c'est une constante du facteur *identity* mesuré — la remplacer par un calcul serait un changement de comportement déguisé en nettoyage.
- [ ] **V7.4 — Archiver les plans périmés** : déplacer `PLAN_ALIGNEMENT_CLIENT_OFFICIEL_2026-10-09.md` et `COMPARAISON_PROXY_OFFICIEL_2026-10-13.md` dans `docs/archive/` avec un en-tête « obsolète : référence `packages/ai/` qui n'existe plus en 1.19.3 ».
- [x] **V7.5 — Documenter le modèle de porte.** ✅ **Fait** — `docs/free-leg.md` : les trois facteurs (`egress ∧ identity ∧ body`), le contrat de corps mesuré, la politique de station, les six champs de diagnostic, les distributions de latence, les commandes de re-mesure, et les **six prémisses tombées** (une confirmée). Le document distingue explicitement « mesuré live » de « prouvé par tests ».

**Critères d'acceptation**
- `opencode.py` < 400 Ko après extraction (ou découpage planifié et accepté).
- Test d'interdiction des littéraux vert.
- Un nouveau développeur trouve la décision de routage en < 2 fichiers.

---

### V8 — Observabilité & CI

- [x] **V8.1 — `/api/drift`.** ✅ **Fait** — `observability/drift.py` + `/api/drift` + `/api/free-health`. Sépare les **orphelins** (routé, absent du catalogue : actionnable) des **candidats** (au catalogue, non routé : pas un défaut — 1 seul des 81 ids free répondait). Résout les alias avant de comparer (sinon 8 faux orphelins), et n'annote « snapshot périmé » que sur des **succès** : mesuré le 2026-10-22, 20 des 36 modèles free servent et 16 sont refusés en amont, dont `jev-1.13-free` qui répondait vite sans rien générer.
- [x] **V8.2 — Canary quotidien.** ✅ **Fait** — `observability/canary.py` + boucle de fond (`canary.interval_s`, 1800 s) + `POST /api/canary/run?model=` pour sonder à la demande, verdict dans `/health` (`canary` ; `failing` après 3 échecs consécutifs, et `/health` passe `degraded`). **Le verdict lit le contenu, pas le statut** : un flux sans `text_delta`/`thinking_delta`, ou portant une trame `event: error`, est un échec — exactement la panne `jev-1.13-free` (200 en 4 s, zéro contenu). Vérifié live : 2 sondes ok en ~2 s, et 3 cycles sur un modèle refusé font basculer `failing` puis `degraded`.
- [x] **V8.3 — Métriques.** ✅ **Fait** — latence **p50/p95 par jambe et par modèle** (`leg_ms:<jambe>:<modèle>`, bornée à 200 séries puis agrégée sous `other`), exposée par `/health?detail=full` → `latency_by_leg` ; taux de 403/429 par `free_failure_kinds` + `gate_factors` (V6.3.1/V6.1.2) ; flux vides par `empty_stream_incidents` (V5.1). Reste ouvert : taux de reprise (nécessite un compteur par tentative, non instrumenté).
- [x] **V8.4 — CI** : gate des contrats **bloquant** (`scripts/ci_contracts.py`, 22 fichiers / 330 tests / **5,7 s**), `pytest -m "not docker"` pour le reste, budget du job 15 → 25 min. ✅ **Fait** — la suite complète est passée de **24 min à 3 min 51 s** (3 661 tests) en corrigeant trois causes mesurées : 721 s pour un test de cache qui faisait du réseau, 192 s et 2×96 s pour des tests à horloge factice qui payaient quand même les vraies pauses. Un test garde le gate lui-même (`test_ci_contracts_gate.py`, 34 tests) : un contrat renommé fait échouer la CI au lieu de la laisser verte à vide.
- [x] **V8.5 — Garde de protocole SystemOne.** ✅ **Fait** — un modèle `jev-*` demandé sur `/v1/messages` ou `/v1/chat/completions` renvoie un **400 qui nomme `/v1/systemone`**, au lieu du `api_usage_error / Invalid request` relayé de l'amont. `/api/free-health` isole les modèles `systemone` : un mauvais protocole n'est pas une panne. (Découvert en corrigeant une conclusion fausse de la veille — voir le journal du 2026-10-23.)
- [x] **V8.6 — Diagnostic de verrou mono-instance.** ✅ **Fait** — quand le verrou est tenu, le message FATAL dit maintenant **qui** le tient (fichier `.pid` voisin, lisible par le nouveau venu) et **si cette instance sert encore** (`IS serving` / `zombie` + la commande à exécuter). Vécu le 2026-10-22 : redémarrage refusé sur un verrou tenu par un processus qui n'écoutait plus, sans aucune indication d'action. `ops/lock.py:diagnose_holder`, 10 tests.
- [x] **V8.7 — `/health` dit POURQUOI il est dégradé.** ✅ **Fait** — le payload porte `status_reasons` (`upstream unreachable`, `circuit open: <endpoint>`, `key paused: <id> (<motif>, <n>s left)`, `canary failing`). Vécu en vrai : `degraded` avec `upstream: ok`, aucun circuit ouvert et un chat free en 1,5 s — la cause était un pause de clé `reason: "test"` oublié. `opencode.py:_health_status_reasons` (fonction pure) + 10 tests.
- [x] **V8.8 — Un reset doit persister.** ✅ **Fait** — `/api/key-pauses/reset` vidait les dicts en mémoire **sans `_save()`** : le fichier `logs/paused_keys.yaml` gardait l'entrée et le redémarrage la rechargeait (vérifié deux fois : le pause « test » est revenu au boot). `KeyPauser.unpause_all()` persiste, l'endpoint l'utilise. 8 tests, dont un qui recharge depuis le fichier — le seul qui prouve la persistance.

**Critères d'acceptation**
- `/api/drift` renvoie 0 écart sur les ids exposés.
- Le canary apparaît dans `/health` avec un statut par jambe.

---

### V9 — Parité étendue (optionnelle, à décider)

- [ ] **V9.1 — Protocole gemini** (`/inference/google/v1beta`) : 10 modèles officiels deviennent servables.
- [ ] **V9.2 — Variantes de raisonnement** : la v1.19.3 remplace les `variants` de catalogue par des variantes générées depuis les options (`packages/core/src/model.ts`). Le proxy peut exposer `variant` en paramètre de route.
- [ ] **V9.3 — Sous-sessions** : `x-opencode-parent-session-id` / `x-parent-session-id` et l'affinité parente.
- [ ] **V9.4 — `/responses` en entrée** : le proxy expose-t-il `/v1/responses` côté client ? Si oui, vérifier la parité d'événements avec `protocols/openai-responses.ts`.

---

## 4. Volet dédié — la jambe gratuite (demande explicite)

La jambe free **reste**. Elle est traitée comme un produit avec quatre axes et des objectifs chiffrés.

| Axe | Aujourd'hui | Cible | Vague |
|---|---|---|---|
| **Fiabilité** | 8 reprises indifférenciées, disjoncteur global, 403 non attribuable | 1 seul essai sur 403, reprise ciblée sur 5xx/429, disjoncteur par station, raison de refus structurée | V6.1, V6.3 |
| **Qualité** | 20/43 modèles free, 0 modèle anthropic free, `reasoning_content` cassé | 40+/43 modèles, 4 modèles anthropic natifs, `reasoning_content` réémis | V6.4, V3.3 |
| **Performance** | p50 ~3 s mais p95 > 24 s | p50 < 4 s, p95 < 12 s, pas de reprise inutile | V6.5 |
| **Maintenabilité** | shims en dur, UA figé sur une contrainte fausse, 4 littéraux d'URL | shims déclaratifs, UA v2, 0 littéral, modèle de porte testé | V6.1, V6.2, V2.4 |

**Invariants à ne jamais casser** (chacun a un test) :
1. `Authorization: Bearer public` (jamais la clé payante) — vérifié par `tests/test_invariant_a0.py`.
2. `stream:true` + `tools[read]` réaliste.
3. Au moins un en-tête d'identité de session (`x-opencode-session-id` | `x-session-affinity` | `x-session-id`).
4. Egress via tunnel (jamais l'IP directe du serveur).

---

## 5. Matrice de tests à créer

| Test | Vague | Ce qu'il empêche |
|---|---|---|
| `test_free_gate_model.py` | V0 | perte d'un facteur de la porte free |
| `test_routing_matrix.py` | V2 | dérive catalogue ↔ routage |
| `test_body_parity.py` | V3 | dérive du corps vs 1.19.3 |
| `test_retry_parity.py` | V4 | reprises non conformes |
| `test_sse_contract.py` | V5 | flux malformé ou silencieux |
| `test_gate_reasons.py` | V6 | 403 non diagnostiquable |
| `test_no_hardcoded_urls.py` | V7 | retour des littéraux |
| `test_invariant_a0.py` (existant) | — | fuite de clé payante sur la jambe free |
| `test_free_wire_complet.py` (existant) | — | régression du wire free |

---

## 6. Séquencement et dépendances

```
V0 ──┬─> V1 ──> V2 ──┬─> V3 ──┐
     │                │        ├─> V5 ─> V6 ─> V7 ─> V8 ─> V9
     └────────────────┴─> V4 ──┘
```

- **V1 est bloquant pour V2/V3/V4** : sans clé valide, on ne peut pas valider la jambe payante ni récupérer `console-config.json`.
- **V6 exige V0 + V3** : on ne touche pas au corps free sans filet ni sans la correction `reasoning_content`.
- **V7 après V6** : extraire du code dont on vient de stabiliser le comportement.

**Jalons livrables :**
- **J1 (fin V2)** : routage 100 % conforme sur les ids exposés — c'est le premier gain visible.
- **J2 (fin V4)** : wire conforme 1.19.3 sur la jambe payante.
- **J3 (fin V6)** : jambe free industrialisée, 40+ modèles, SLO tenus.

---

## 7. Risques transverses

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| La passerelle free change encore (elle a changé 3 fois depuis septembre) | **élevée** | jambe free morte | modèle de porte explicite + test de régression + canary quotidien ; la mesure devient un test, pas un commentaire |
| La clé payante reste expirée | moyenne | V2–V4 non validables | V1.1 en tête de plan ; à défaut, travailler en fixtures (V0.4) |
| Bascule d'endpoint casse un chemin non testé | moyenne | 5xx en prod | drapeau `upstream.legacy_bases` + canary + comparaison A/B |
| Le retrait de `tool_choice` casse la porte free | moyenne | 403 | le changement est borné à la jambe payante ; test de porte avant/après |
| Extraction de `opencode.py` casse des contrats implicites | élevée | régressions diffuses | extraction mécanique, un module par commit, suite existante (167 fichiers) comme filet |
| Documentation qui re-périme | certaine | perte de temps | les contraintes vivent dans des **tests**, pas dans des commentaires ; `docs/_drift_manifest.json` vérifié par CI |

---

## 8. Critères d'arrêt / rollback global

**Arrêt (succès) :** S1–S6 prouvés par tests verts, `/api/drift` à 0, canary vert sur les deux jambes pendant 72 h.

**Rollback global :** chaque vague laisse un drapeau de configuration documenté (`retry.policy`, `protocol.*`, `free.gate_mode`, `upstream.legacy_bases`) ; revenir en arrière = changer un drapeau et redémarrer, jamais un revert de code en urgence.

**Point de non-retour :** après V7 (extraction), les drapeaux de V3/V4 doivent être figés sur la nouvelle valeur — on ne garde pas deux comportements ad vitam.

---

## 9. Ce qu'on ne fait pas (et pourquoi)

| Tentation | Décision | Raison |
|---|---|---|
| Aligner l'UA free sur 1.18.31 « pour la parité » | **non** | contrainte inexistante (audit §4.2) ; source de maintenance. On aligne sur **1.19.3**, pas sur l'ancienne version |
| Supprimer `tool_choice: "auto"` | **non** | c'est ce que fait le vrai client (audit §2.3) : le retirer serait une régression |
| Supprimer le quartet `x-opencode-*` | **oui** | mesuré inutile (audit §4.2) — c'est une simplification, pas un risque |
| Supprimer le pool de tunnels | **non** | c'est le facteur `EgressReputation` de la porte |
| Supprimer les shims d'outils | **non** | `read` est load-bearing |
| Forcer `stream:true` pour tous les clients | **à supprimer si mesuré inutile** | coût de reconstitution JSON |
| Réécrire `opencode.py` d'un bloc | **non** | 1 Mo, 167 fichiers de tests : extraction incrémentale uniquement |
| Empiler un 4ᵉ document d'audit | **non** | deux documents suffisent : audit + plan, sourcés sur des mesures |

---

*Fin du plan. Les mesures et commandes de reproductibilité sont dans l'audit (§12).* 

---

## 10. Journal d'exécution

Chaque ligne est un point du plan terminé, avec sa preuve. C'est ce journal qui dit où en est le chantier — pas la mémoire de qui l'a lancé.

### 2026-10-20 — Audit et outillage de mesure

| Point | État | Preuve |
|---|---|---|
| Audit livré | ✅ | `docs/AUDIT_ALIGNEMENT_CLIENT_OFFICIEL_2026-10-20.md` |
| Plan livré | ✅ | ce document |
| Binaire officiel **1.19.3** installé et exécutable | ✅ | `_research/oc-1.19.3/bin/opencode.exe --version` → `1.19.3` |
| Serveur de capture + config provider `capture` | ✅ | `_research/capture_server.js`, `_research/oc-1.19.3/oc.json` |
| **Wire réel du client capturé** | ✅ | `_research/oc-1.19.3/capture.jsonl` (2 requêtes, en-têtes ordonnés + corps complets) |
| Porte free modélisée par 3 facteurs | ✅ | `_research/gate_official_headers.py`, `_research/gate_live_new_wire.py` |

**Trois croyances corrigées par la mesure** (elles étaient dans le code, pas seulement dans les docs) :

1. `tool_choice: "auto"` **est** émis par le client 1.19.3 quand il y a des outils → le proxy était aligné, le « corriger » aurait été une régression.
2. Le quartet `x-opencode-client/project/request/session` **n'est pas** requis par la porte (le wire officiel passe → 200).
3. Le **numéro de version** de l'UA **n'est pas** un facteur de la porte (1.18.31 et 1.19.3 passent) → la contrainte qui figeait le proxy sur 1.18.31 était auto-imposée.

### 2026-10-20 — V0 filet + V4 identité (faits)

| Point | État | Preuve |
|---|---|---|
| **V0.2** filet de tests réécrit (sémantique, pas liste d'en-têtes) | ✅ | `tests/test_free_wire_complet.py` — 14 tests |
| **V0.2b** modèle de porte explicite | ✅ | `config/free_gate.py` — `GateVerdict.failed_factors`, `as_log()` |
| **V1.4** client 1.19.3 installé | ✅ | `_research/oc-1.19.3/README.md` |
| **V4.1** UA aligné sur la mesure | ✅ | `opencode.py` `_OPENCODE_OFFICIAL_UA` = `opencode/1.19.3 …` |
| **V4.2** jeu d'en-têtes officiel + drapeau de rollback | ✅ | `opencode.py:5389+`, `OPENCODE_FREE_WIRE_STYLE=official|legacy` |
| **V4.2b** session unique partagée par les trois noms | ✅ | `test_free_wire_uses_official_session_identity` |
| Vérification live de la porte après alignement | ✅ | **200** via tunnel, `{"passed": true, "failed": []}` |
| Suite complète (après V0+V4.1/V4.2) | ✅ | **3 036 tests, 0 échec** (`_research/pytest_after_v4.log`) |
| Suite complète (après V4.3/V4.4) | ✅ | **3 067 tests, 0 échec** (`_research/pytest_final.log`) |

**Tests mis à jour parce qu'ils encodaient l'ancien contrat** (et non parce qu'ils étaient « faux ») :

| Fichier | Ce qui a changé |
|---|---|
| `tests/test_free_wire_complet.py` | réécrit : porter sur la porte, pas sur 12 en-têtes figés |
| `tests/test_official_client_parity.py` | ordres séparés `OFFICIAL_ORDER` / `LEGACY_ORDER`, tests d'IDs `msg_` bornés au mode legacy |
| `tests/test_invariant_a0.py` | l'identité free est validée par `free_gate.identity_ok`, plus par le quartet |
| `tests/test_free_multi_attempt.py` | idem |
| `tests/test_paid_wire_identity.py` | `test_free_leg_still_has_no_session_id` **inversé** → `test_free_leg_carries_the_official_session_identity` |
| `tests/test_opencode_client_parity_helpers.py` | porteur d'identité = `x-opencode-session-id` |

**Décisions prises pendant l'exécution** (à ne pas re-litiger) :

- **Ne pas bumer l'UA `/responses`** : non re-capturé sur 1.19.3. Bumper à l'aveugle fabriquerait une combinaison `1.19.3 + provider-utils 4.0.40` qui n'existe peut-être pas. La version n'étant pas un facteur, on garde le couple mesuré.
- **Ne pas toucher `_enrich_paid_wire_headers`** : sans clé valide, le changement serait non validable. Le retrait du quartet côté payant reste planifié (V4.2) mais **bloqué sur V1.1**, et c'est écrit ici pour que personne ne le « finisse » sans pouvoir le tester.
- **Critère d'acceptation retiré** : `grep -c 'provider-utils' opencode.py` = 0. `provider-utils` fait partie de l'UA officiel ; l'exiger à zéro contredisait l'alignement.
- **Pas de `docs/_drift_manifest.json`** (V0.6) : un manifeste déclaratif re-périme. Le filet est fait de tests exécutables.

### 2026-10-20 — V4.3/V4.4 politique de reprise (faits)

| Point | État | Preuve |
|---|---|---|
| **V4.3** budget aligné sur l'officiel | ✅ | `config.yaml` : `upstream.max_retries: 2`, `streaming.retry_attempts: 2` |
| **V4.3** statuts rejouables = règle officielle | ✅ | `upstream/retry.py` : `RETRYABLE_STATUSES`, `is_retryable_status` |
| **V4.3** `opencode.py` consomme la source unique | ✅ | `_RETRYABLE_STATUSES = set(RETRYABLE_STATUSES)` |
| **V4.4** tests de parité de reprise | ✅ | `tests/test_retry_parity.py` — 30 tests |

**Ancien ensemble → nouveau** — quatre écarts corrigés d'un coup :

| Statut | Avant | Après | Pourquoi |
|---|---|---|---|
| 409 Conflict | rejoué | **non rejoué** | l'officiel le classe `InvalidRequest` |
| 408 / 425 / 499 | rejoués | **non rejoués** | conditions de transport, pas surcharge amont |
| 429 | hors ensemble (géré au cas par cas sur la free) | **rejouable partout** | règle officielle |
| 501 / 505+ | ignorés | **rejoués** | « tout 5xx » chez l'officiel |

**Ce qui n'a pas été changé, et pourquoi :** `_free_bound` (budget d'essais de la jambe free) reste `max(budget, len(API_KEYS)) + (stations_utiles − 1)`. La rotation de station **est** la raison d'être de la jambe free (changer d'IP de sortie quand la porte refuse) ; ce n'est pas une reprise HTTP. La corriger ici aurait mélangé deux notions. Elle relève de **V6.3.1** (classifier 403 gate / 429 quota / 5xx transit pour ne pas rejouer un refus définitif).

### 2026-10-20 — V3 corps : deux écarts d'audit démentis, un vrai corrigé

Avant de toucher au corps, sonde du corps **réellement émis** par la jambe free (`_research/probe_free_body2.py`). Trois vérifications, deux surprises :

| Écart annoncé dans l'audit | Verdict de la mesure | Preuve |
|---|---|---|
| P1-2 : le proxy émet `max_completion_tokens` | **FAUX** — il émet `max_tokens` | `_free_wire_body` → `['model','max_tokens','messages','tools','tool_choice','stream','temperature']` |
| P2-7 : le proxy émet `store: true` | **FAUX** — il n'émet jamais `store` | `grep '"store"' opencode.py` → 0 occurrence |
| P0-2 : `reasoning_content` perdu | **partiellement faux** — il survit s'il est présent ; il n'est pas **synthétisé** s'il manque | sonde `_research/probe_reasoning_survives.py` |

**Le vrai défaut (P0-2), tel que la mesure l'a isolé :** la règle DeepSeek « chaque message assistant porte `reasoning_content`, même vide » existait dans `app/protocol/mapping.py:3591` (`ensure_deepseek_reasoning`, port de `transform.ts`) mais **n'était pas appliquée au corps de la jambe free**. La conversion connaissait la règle, le wire free non — deux chemins, une seule règle.

| Point | État | Preuve |
|---|---|---|
| **V3.1** `max_tokens` | ✅ déjà aligné | sonde du corps émis |
| **V3.2** `tool_choice: "auto"` | ✅ déjà aligné | 242/243 requêtes mesurées |
| **V3.2b** `store` | ✅ déjà aligné (absent des deux côtés) | `grep` |
| **V3.3** défaut deepseek sur la jambe free | ✅ **corrigé** | `_free_wire_body` → `ensure_deepseek_reasoning` |
| **V3.3** non-régression live | ✅ | **200** sur la requête qui produisait le 400 (`_research/live_reasoning_fix.py`) |
| **V3.3** verrouillage par tests | ✅ | 3 tests dans `tests/test_free_wire_complet.py` |
| Suite complète | ✅ | **3 070 tests, 0 échec** (`_research/pytest_final2.log`) |

**Leçon de méthode, à garder :** deux des trois « écarts » du corps étaient des lectures de code interprétées comme des faits. Une sonde de 10 lignes sur le corps réellement émis a évité deux modifications inutiles et une régression (`tool_choice`). Le réflexe à conserver : **avant de « corriger » un écart de wire, imprimer le wire.**

### 2026-10-20 — V2 couche catalogue : l'erreur racine est nommée

**Ce que la mesure du catalogue a établi** (instantané `models-api.json`, 2026-10-20) :
| Fait mesuré | Conséquence |
|---|---|
| **Chaque** modèle porte sa propre `provider.api` (128/128 free, 33/33 payant) | l'endpoint amont est une **donnée**, pas une déduction |
| **66 / 128** modèles free portent un `provider.npm` de modèle | le `npm` n'est **pas** purement global |
| `@ai-sdk/google` existe (9 free + 1 payant) | la table `NPM_TO_API` du proxy ne le connaissait pas |
| `@ai-sdk/anthropic` sur 24 free (`/inference/anthropic/v1`) | dont 3 modèles free qui répondaient 404 faute de route |

**L'erreur racine, corrigée :** `config/models_catalog.py` affirmait noir sur blanc que le `npm` était « ABSENTE au niveau modèle : aucune entrée de `models:` n'en porte ». C'est faux, et c'est cette phrase qui justifiait toute la table statique. Elle est remplacée par la mesure.

| Point | État | Preuve |
|---|---|---|
| **V2.1** fonctions de résolution par modèle | ✅ | `models_catalog.py` : `model_api`, `model_npm`, `api_family_for_url`, `api_family_for_model` |
| **V2.2a** familles google/mistral | ✅ | `NPM_TO_API`, `KNOWN_API_FAMILIES`, `_API_URL_MARKERS` |
| **V2.2** résolution par modèle (couche catalogue) | ✅ | l'URL gagne sur un `npm` contradictoire (test dédié) |
| **V2.7** (1ʳᵉ moitié) matrice verrouillée | ✅ | `tests/test_catalog_model_routing.py` — 22 tests |
| **Faux verrous levés** | ✅ | `tests/test_npm_routing.py` : 4 tests corrigés (voir ci-dessous) |
| **V2.2b** branchement dans `_resolve_model_endpoint` | ⬜ **non fait** | c'est le lot suivant : il touche le chemin de production |

**Quatre tests verrouillaient la croyance fausse** — ils ont échoué dès l'ajout des familles google/mistral, et c'est leur fonction :

| Test | Ce qu'il affirmait | Correction |
|---|---|---|
| `test_matrice_npm_connue_et_rien_dautre` | « trois valeurs connues, et **rien** d'autre » | cinq valeurs, dont `@ai-sdk/google → gemini` |
| `test_npm_hors_table_rend_none_pas_de_deviner` | prenait `@ai-sdk/google` et `@ai-sdk/mistral` pour des inconnus | témoins réellement inconnus (`@aihubmix/…`, `@ai-sdk/inconnu`) |
| `test_npm_est_lu_au_niveau_provider_jamais_modele` | « le catalogue live n'en porte aucun au niveau modèle » | renommé : `provider_npm` lit le provider (bas niveau), `model_npm` applique la surcharge |
| `test_refus_nomme_lid_les_hotes_et_le_npm_de_chacun` | `@ai-sdk/google` = « hors table » | `@ai-sdk/google` nommé comme famille `gemini` ; nouveau test séparé pour l'hôte sans `npm` |

Ce n'est pas de la casse accidentelle : ces tests **disaient** la croyance. Les corriger est le seul moyen de la remplacer. Chacun porte désormais une ligne `[CORRECTION 2026-10-20]` qui explique ce qu'il affirmait et pourquoi c'était faux.

**Décision de séquencement :** je m'arrête à la couche catalogue. Elle est pure, testée offline, et elle **nomme** les 5 divergences (`minimax-m2.5`, `minimax-m2.7`, `minimax-m3`, `qwen3.8-max` déclarés `protocol: anthropic` alors que le catalogue dit chat ; `qwen3.6-plus` déclaré `openai` alors que le catalogue dit messages). Brancher la résolution dans le routage changerait le comportement de modèles en production : cela mérite son propre incrément, avec le test de matrice côté **routage effectif** avant, pas après.

### 2026-10-20 — Mesure de la suite : la bonne méthode, et une instabilité nommée

**Correction de méthode.** Les comptes annoncés plus haut (« 3 036 », « 3 067 », « 3 070 ») venaient d'un comptage des points de progression pytest : cette méthode est **fausse** (lignes tronquées, marges). Référence désormais : `--junit-xml`, qui donne le décompte exact.

**Chiffre réel :** **3 308 tests, 5 ignorés, 0 échec** (`_research/junit2.xml`).

**Instabilité préexistante, nommée :** `tests/test_free_multi_attempt.py::test_free_429_cooldown_only` et `::test_free_429_rotate_only` échouent **par intermittence** en suite complète, et passent systématiquement seuls (25/25). Deux runs consécutifs de la suite entière : 2 échecs au premier, 0 au second, mêmes tests.

- **Cause probable :** ces deux tests vérifient une action (`cooldown` *ou* `rotate`) alors que `_on_free_429_stream` déclenche la rotation de station **en arrière-plan** (`FreeIPPool.on_quota_exhausted`, single-flight). L'assertion courre contre le thread.
- **Non lié à l'alignement :** ces tests ne touchent ni `RETRYABLE_STATUSES`, ni le budget de reprises, ni le wire ; ils pilotent `oc.IP_ROTATION["on_429_action"]` et un pool factice.
- **Correctif à prévoir (petit, séparé) :** rendre la rotation observables dans les tests (attendre le future/événement au lieu d'un `sleep` implicite), ou injecter le pool pour que la rotation soit synchrone. À traiter avec V6.3.1 (classification des échecs free), qui touche le même code.

**Conséquence pour la suite du plan :** un « 0 échec » n'est une preuve que si le décompte vient de `junit`. Les prochains points seront vérifiés ainsi.

### 2026-10-20 — V2.2b : les « 5 mauvais protocoles » étaient 1 vrai

**L'erreur de méthode, et elle était dans l'audit.** J'avais comparé la vérité du catalogue sur la jambe **free** au routage du proxy, qui vise la jambe **payante** pour tout id sans suffixe `-free`. Deux populations différentes, donc des faux positifs. Vérification id par id, sur les deux jambes :

| Id | free | payant | routage proxy | verdict |
|---|---|---|---|---|
| `minimax-m2.7` | chat | **messages** | payant messages | ✅ l'audit avait tort |
| `minimax-m3` | chat | **messages** | payant messages | ✅ l'audit avait tort |
| `qwen3.8-max` | chat | **messages** | payant messages | ✅ l'audit avait tort |
| `minimax-m2.5` | chat | *absent* | payant messages | ❌ erreur de **jambe** |
| `qwen3.6-plus` | messages | *absent* | payant chat | ❌ erreur de **jambe** |
| `minimax-m3.1-flash-free` | chat | *absent* | free `anthropic` | ❌ **vrai bug de protocole** |

**Conséquence de conception :** le catalogue ne doit **jamais** franchir la frontière des jambes. Le même id peut y porter deux familles différentes ; emprunter la route de l'autre jambe enverrait le trafic payant sur la base free sans le dire. Une première version de mon code faisait exactement ça — détecté par la mesure, pas par relecture.

| Point | État | Preuve |
|---|---|---|
| Générateur de routes (`--check` inclus) | ✅ | `scripts/refresh_catalog_routes.py` → `config/catalog_routes.json`, 161 modèles |
| Résolution par jambe, sans repli | ✅ | `catalog_route_for` + test dédié dans les deux sens |
| Priorité `endpoint:` > `api:` > catalogue > heuristique | ✅ | `_catalog_override` (garde explicite) |
| Correctif protocole seul, endpoint préservé | ✅ | `minimax-m3.1-flash-free` → `openai` + `API_BASE_FREE` |
| Surcharges déclenchées sur les 63 modèles configurés | **1** | `_research/probe_overrides_count.py` |
| Tests de routage effectif | ✅ | `tests/test_catalog_routing_effective.py` — 14 tests |
| Suite complète | ✅ | **3 323 tests, 0 échec** (`_research/junit4.xml`) |

**Ce que la borne « 1 seul changement » signifie.** Le branchement ne répare pas 20 modèles d'un coup : il répare **ce qui était faux**, et il installe le mécanisme qui réparera les 81 ids free absents de `config.yaml` quand ils seront exposés (V6.4). Annoncer un gain massif ici aurait été faux.

**Décision explicite de ne pas faire :** la base free `…/inference/v1/chat/completions` n'est **pas** remplacée par celle du catalogue (`…/inference/openai/v1`). L'actuelle répond 200 à travers la porte ; changer d'URL sans nécessité est un risque gratuit. À reconsidérer le jour où la base plate renverra 404.

### 2026-10-20 — V6.4 : une conclusion négative, et c'est le bon résultat

**Ce qu'on croyait.** « 46/128 modèles free couverts » se lisait comme 82 modèles à gagner. Trois lignes de plan suffisaient : déclarer les ids, router, encaisser.

**Ce que la mesure a dit.** Un POST réel par id absent de `config.yaml` — 81 ids, corps conforme à la porte, via tunnel, sur le chemin de la famille déclarée :

| Réponse | Nombre |
|---|---|
| **200** | **1** (`big-pickle`) |
| 401 `Missing API key` | 32 |
| 400 `Model is unavailable` | 21 |
| timeout / erreur tunnel au 1ᵉʳ passage | 27 |

Les 27 non conclus ont été re-testés avec un délai plus long : ils répondent en **~2 s**, en 401 ou 400. Les timeouts étaient un artefact de charge sur les tunnels — pas une indisponibilité, et **aucun n'a rendu 200**.

**Les 24 modèles `messages` (dont 17 Claude) ont été testés sur `/inference/anthropic/v1/messages` avec un corps Anthropic valide** (`anthropic-version`, outil `read` au schéma Anthropic) : le format de l'erreur prouve que le chemin était le bon — c'est le modèle qui est fermé aux anonymes (`401 Missing API key`).

| Point | État | Preuve |
|---|---|---|
| Sonde par famille (chat / messages) | ✅ | `_research/probe_free_models.py` |
| Verdict des 81 ids | ✅ | `_research/free_models_verified.json` — 1 exposé, 53 refusés, 27 re-testés |
| Exposition automatique | ✅ | `config/free_models_verified.json` chargé par `config/settings.py` |
| `big-pickle` servi | ✅ | `MODELS["big-pickle"]` → `API_BASE_FREE`, `source: free_verified` |
| Tests d'exposition | ✅ | `tests/test_free_models_exposed.py` — 10 tests |
| Suite complète | ✅ | **3 334 tests, 0 échec** (`_research/junit5.xml`) |

**Décision : n'exposer que le vérifié.** Un modèle exposé sans preuve est une promesse que le proxy ne peut pas tenir — et le client facture l'échec en latence et en incompréhension. `big-pickle` entre ; les 80 autres restent dehors, avec leur refus daté et motivé dans le fichier. Une prochaine session n'aura pas à refaire ce travail, et personne ne « libérera » par erreur un modèle fermé.

**Ce que ce résultat interdit de raconter.** « 23 modèles free récupérés » aurait été un chiffre flatteur et faux. Le gain réel de V6.4 est **1 modèle**, plus une carte des refus qui vaut plus que le modèle : elle transforme un espoir en fait.

### 2026-10-20 — V2.3 : encore une prémisse d'audit à corriger

L'audit annonçait « 4 occurrences d'URL en dur dans `opencode.py` ». Vérification :

| Ce que l'audit affirmait | Réalité mesurée |
|---|---|
| `openai_base` / `anthropic_base` / `free_base` en dur | **déjà** dans `config.yaml`, lues par `yaml_get` |
| endpoint `systemone` en dur | **déjà** surchargeable : `os.getenv("OPENCODE_SYSTEMONE_ENDPOINT", …)` |
| affectations de littéraux `https://opencode.ai/…` | **zéro** dans `opencode.py` |
| endpoints **Responses** | ✅ **vrai écart** : les deux seuls littéraux non surchargeables |

**C'est la cinquième prémisse d'audit démentie par la mesure** (après `tool_choice`, `max_completion_tokens`, `store`, « 5 mauvais protocoles » et « 23 modèles free à gagner »). Le motif est constant : lire le code et en tirer un fait. Un fait se mesure.

| Point | État | Preuve |
|---|---|---|
| `responses_base` / `responses_free_base` en config | ✅ | `config.yaml > upstream`, lus par `yaml_get` |
| Comportement inchangé (mêmes valeurs par défaut) | ✅ | sonde : les 5 bases résolues sont identiques |
| Garde-fou anti-régression | ✅ | regex dans `tests/test_upstream_bases_configurable.py` |
| Hôte unique côté catalogue | ✅ | test dédié : tous les `base_url` pointent `opencode.ai` |
| Suite complète | ✅ | **3 350 tests, 0 échec** (`_research/junit6.xml`) |

**Ce que le garde-fou protège vraiment.** Il n'empêche pas d'écrire une URL : il empêche de l'écrire *de la seule façon qui a produit un bug* — une affectation directe, non surchargeable. Une valeur par défaut dans `yaml_get(...)` reste autorisée, parce qu'elle est précisément ce qui rend la config souveraine.

### 2026-10-20 — L'instabilité `test_free_429_*` : cause trouvée, pas contournée

**Symptôme.** `test_free_429_cooldown_only` et `test_free_429_rotate_only` échouaient par intermittence en suite complète, passaient toujours seuls. Deux runs identiques : 2 échecs, puis 0.

**Ce que les échecs disaient, mot pour mot.** En mode *cooldown seul*, `pool.rotated` contenait une rotation. En mode *rotate seul*, un cooldown était actif. **Chaque test voyait se produire l'action de l'autre mode** — signature d'une configuration qui n'est pas celle qu'on croit.

**La cause.** Le fixture `free_cfg` écrivait `oc.IP_ROTATION["on_429_action"]`, en supposant que le lecteur lisait ce dict. Il lit en réalité le **YAML live** : `config.settings.get_429_action()` → `yaml_get("ip_rotation", …)`. Tant que le dict global et la section YAML sont le même objet, la mutation passe. Dès qu'un rechargement YAML remplace la section — ce qui arrive en suite complète — la mutation est perdue, l'action retombe sur `config.yaml` (`on_429_action: both`), et les deux actions se produisent. D'où l'intermittence : tout dépend de l'ordre des tests qui ont provoqué un rechargement avant.

**Le correctif.** Le fixture patche désormais le **lecteur** pour qu'il lise l'état que le test pilote (`normalize_429_action` appliqué à `IP_ROTATION`). La production n'est pas touchée : c'est le contrat du test qui devient explicite au lieu de dépendre d'un aliasing d'objet.

| Point | État | Preuve |
|---|---|---|
| Cause identifiée (lecteur ≠ dict muté) | ✅ | `config/settings.py:get_429_action` → `yaml_get` |
| Correctif dans le fixture, pas dans le code prod | ✅ | `tests/test_free_multi_attempt.py`, docstring datée |
| Deux runs consécutifs de la suite complète | ✅ | `_research/junit_f1.xml` (3 350 tests, 0 échec, 5 ignorés) et `_research/junit_f2.xml` (3 350, 0 échec, 6 ignorés) |

*Note : le nombre d'ignorés varie de 5 à 6 entre deux runs — deux tests du dépôt se sautent conditionnellement (absence d'un artefact ou d'une dépendance). Ce n'est pas un échec, mais c'est un écart à connaître avant de comparer deux runs entre eux.*

**Pourquoi ce n'était pas un faux problème.** Un test qui passe seul et échoue en série n'est pas « capricieux » : il dépend d'un état partagé qu'il ne contrôle pas. Ici, l'état partagé est un dict que la production remplace légitimement. Tant que ce test restait instable, chaque « 0 échec » de cette session était une mesure non fiable — y compris ceux que j'ai annoncés plus haut.

### 2026-10-20 — Deuxième défaut d'ordre : `big-pickle` exposé une fois sur deux

**Symptôme.** Après avoir câblé le compteur V5.1, la suite complète a rendu **1 échec** — `test_big_pickle_is_exposed_and_routed_to_the_free_leg`, `KeyError: 'source'` — sur un fichier qui venait de passer 10/10 seul, et qui avait passé en suite complète une heure plus tôt.

**La cause n'était pas dans le test, mais dans la production.** La fusion du fichier de modèles vérifiés sautait tout id **déjà présent** dans `MODELS`. Or `config/discovery.py` écrit ses entrées sous la forme `{"endpoint", "protocol"}` — **sans `source`**. Dès que la découverte live avait déjà vu `big-pickle`, l'entrée vérifiée était ignorée et le modèle gardait l'endpoint de la découverte, qui peut désigner la **mauvaise jambe** (le payant au lieu du free).

Autrement dit : selon l'ordre d'arrivée de la découverte, le proxy routait `big-pickle` vers la jambe free (correct) ou vers l'endpoint découvert (faux). Le test intermittent révélait un **routage intermittent**.

**Le correctif.** Une entrée sans `source` n'a aucune autorité : elle est remplacée par l'entrée vérifiée. La config explicite — et toute entrée déjà qualifiée par une `source` — reste souveraine.

| Point | État | Preuve |
|---|---|---|
| Cause identifiée (découverte écrit sans `source`) | ✅ | `config/discovery.py:125`, `config/settings.py:1047` |
| Entrée vérifiée prioritaire sur entrée non qualifiée | ✅ | `source` absent → remplacement |
| Tests d'exposition | ✅ | 10/10 seuls, et en suite complète |
| Deux runs consécutifs de la suite complète | ✅ | `_research/junit_g1.xml` et `_research/junit_g2.xml` — **3 358 tests, 0 échec, 5 ignorés** chacun, comptes identiques |

**Le motif, une deuxième fois.** Un test qui passe seul et échoue en série ne dit pas « le test est fragile » : il dit qu'un état partagé échappe au contrôle. La première fois c'était un dict remplacé par un rechargement ; cette fois c'est une entrée de catalogue écrite sans marqueur. **Deux intermittences, deux fois un vrai défaut de production caché derrière.** C'est l'argument le plus solide pour ne jamais classer ce genre d'échec en « bruit ».

### 2026-10-20 — `big-pickle` était routé correctement et échouait quand même

**Ce que la vérification en direct a montré.** Après redémarrage, un appel réel à `big-pickle` via le proxy :

```
HTTP 200 | text/event-stream
data: {"error":{"message":"All API keys exhausted (unauthorized). Check your API keys."}}
```

Le log de routage était **bon** : `route: 'big-pickle' → big-pickle | openai | endpoint=https://opencode.ai/inference/v1/chat/completions` — la base free. Et pourtant le modèle partait sur la voie **payante**.

**La cause.** `_is_free_model_route` reconnaît un modèle free s'il finit par `-free` ou s'il figure dans `FREE_MODEL_MAP`. `big-pickle` n'est ni l'un ni l'autre. Exposer un modèle dans `MODELS` avec la base anonyme ne suffit donc pas : il faut aussi le faire entrer dans `FREE_MODELS`, le registre que lit le pipeline.

**Le correctif.** Les ids exposés parce que **vérifiés en direct** rejoignent `FREE_MODELS` (`config/settings.py`). Trois tests verrouillent le maillon : le modèle vérifié est classé free, sa route résout la jambe free, et un témoin négatif vérifie que la classification ne s'élargit pas par accident.

| Point | État | Preuve |
|---|---|---|
| Routage (endpoint/protocole) | ✅ déjà bon | log `[route]` ci-dessus |
| Classification free du pipeline | ✅ corrigée | `big-pickle in FREE_MODELS` → `_is_free_model_route` → True |
| Garde-fou négatif | ✅ | `claude-opus-4-5`, `gpt-5.2`, `glm-5.2` ne sont pas classés free |
| Suite complète | ✅ | **3 361 tests, 0 échec** (`_research/junit_h1.xml`) |

**La leçon, et elle est plus large que ce modèle.** Un modèle exposé est un modèle **à trois étages** : une entrée de catalogue, une route résolue, et une classification dans le pipeline. J'avais validé les deux premiers et écrit des tests dessus ; le troisième manquait, et aucun test ne pouvait le voir — seul un appel réel l'a révélé. C'est la différence entre « la config dit la bonne chose » et « la requête fait la bonne chose ».

**Ce qui reste à faire côté exploitation :** un redémarrage de plus pour charger ce correctif. L'instance actuelle route `big-pickle` vers la bonne base mais l'envoie sur la voie à clé, donc elle échoue — c'est un état temporaire, pas une régression de la session.

### 2026-10-20 — Troisième étage : la découverte effaçait le registre

Après le correctif de classification, le redémarrage suivant a **encore** rendu `All API keys exhausted` sur `big-pickle`. Le log était pourtant identique : bonne route, zéro ligne `[free]`.

**La cause, en une ligne.** `config/discovery.py:501-502` :

```python
free_models.clear()
free_models.update(free_ids)
```

La passe de découverte **rebâtit** `FREE_MODELS` à partir des seuls ids qu'elle a découverts. `big-pickle` n'en fait pas partie : il ne finit pas par `-free` et n'est pas une cible de découverte. Ma correction précédente était donc effacée à la première passe — quelques secondes après le démarrage.

**Le correctif.** Les ids vérifiés en direct sont ré-ajoutés **après chaque passe** (`_apply_discovered_free_models`). Leur preuve — un 200 réel mesuré — ne périme pas à la découverte suivante. Attention au piège rencontré : `FREE_MODELS |= x` dans cette fonction lie le nom en **local** (`UnboundLocalError`) ; c'est `.update()` in-place qui convient.

**Quatre tests à corriger, pas un bug.** `test_free_probe` (×3) et `test_free_discovery` (×1) affirmaient « le registre == exactement ce que la découverte a rendu ». C'était vrai avant l'overlay, faux après — et c'est le nouveau contrat qui est juste. Ils comparent désormais la **part découverte** (`FREE_MODELS - _EXPOSED_VERIFIED_IDS`) ; l'overlay a son propre test.

| Point | État | Preuve |
|---|---|---|
| Découverte ne peut plus effacer un vérifié | ✅ | `tests/test_free_models_exposed.py::test_discovery_pass_does_not_erase_verified_ids` |
| Contrat mis à jour dans les 4 tests concernés | ✅ | `test_free_probe` ×3, `test_free_discovery` ×1, avec note datée |
| Suite complète | ✅ | **3 362 tests, 0 échec** |

**Le motif, troisième fois — et il est instructif.** Le même modèle a échoué trois fois de suite pour **trois raisons différentes**, chacune invisible pour les deux correctifs précédents :

1. l'entrée de catalogue écrasée par la découverte (endpoint) ;
2. la classification free absente du pipeline (voie payante) ;
3. le registre des free modèles réinitialisé à chaque passe (classification perdue).

Trois couches, trois tests verts, et un modèle toujours injoignable. **C'est ce que « valider » veut dire** : pas « les tests passent », mais « la requête réussit ». Les trois fois, c'est un appel réel qui a parlé — pas la suite de tests.

### 2026-10-20 — ✅ `big-pickle` servi en direct : validation de bout en bout

Après le troisième correctif et le redémarrage, un appel réel **complet** via le proxy :

```
finish_reason : stop
trames        : 16
contenu       : 'PROXY OK'                      ← exactement ce qui était demandé
raisonnement  : 'The user asks to reply with exactly "PROXY OK"…'
usage         : {prompt_tokens: 230, completion_tokens: 28,
                 total_tokens: 258,
                 completion_tokens_details: {reasoning_tokens: 23}}
```

**Ce que ce seul appel valide, d'un coup :**

| Chaîne | Preuve |
|---|---|
| Exposition du modèle (V6.4) | `big-pickle` joignable, absent de la config explicite |
| Routage par catalogue (V2.2b) | base free pour la famille chat |
| Classification free du pipeline | voie free, tunnels SOCKS, aucune clé requise |
| Survie à la découverte | tient après les passes de démarrage |
| `reasoning_content` (V3.3) | présent dans les deltas **et** compté (`reasoning_tokens: 23`) |
| Parité `usage` (V6.4.4) | présent et exact côté free |
| Conversion OpenAI (V4) | `finish_reason: stop`, contenu conforme à la consigne |

**Ce que ça change dans la lecture du lot V6.4.** Le gain annoncé par l'audit (« 23 modèles free à exposer ») reste faux : un seul modèle était servable. Mais ce modèle **fonctionne maintenant de bout en bout**, et il a servi de banc d'essai pour valider trois mécanismes (routage, classification, survie) qui, eux, s'appliquent aux 63 modèles free configurés.

**Le coût réel de la session sur ce point précis.** Trois défauts successifs, chacun invisible au précédent, découverts uniquement par des appels réels : endpoint écrasé, classification absente, registre réinitialisé. Aucun des trois n'était détectable par la suite de tests — y compris les 14 tests que j'avais écrits sur l'exposition.

### 2026-10-20 — V5.1 : le 200 mensonger devient un 502 (chemin Anthropic)

**Le mécanisme du correctif.** Le proxy fait déjà un peek, mais il attend les **en-têtes**. Plutôt que de changer le contrat des quatre chemins de streaming, on exploite un fait déjà établi par la garde du chemin Anthropic : `not _bytes_flushed` signifie qu'**aucun octet n'a été émis**, donc que Starlette n'a pas encore envoyé les en-têtes, donc que **le statut est encore libre**. Dans ce cas on lève `EmptyUpstreamStream` (502) au lieu d'écrire un événement `error` dans une réponse 200.

Le handler global respecte désormais le statut porté par l'exception — sans ça, le 502 aurait été aplati en 500 et le correctif invisible.

| Point | État | Preuve |
|---|---|---|
| Exception dédiée, statut 502, message client maîtrisé | ✅ | `tests/test_empty_stream_http_status.py` |
| Condition = « rien n'a été envoyé », pas « le flux a l'air vide » | ✅ | test de `_can_fail_with_http_status` |
| Handler global honore `status_code` | ✅ | **3 369 tests, 0 échec** (`_research/junit_j1.xml`) |
| Compteur incrémenté **avant** la levée | ✅ | test d'ordre dans le source |
| Chemin OpenAI (passthrough) | ⚠️ **inchangé** | voir ci-dessous |

**Pourquoi le chemin OpenAI reste en l'état, et ce n'est pas de la paresse.** Ce générateur ne tient pas de `_bytes_flushed` (c'est écrit dans le code en 14829) : sa sentinelle `_stream_has_yielded` peut être fausse alors que des octets sont **déjà partis verbatim**. Lever dans ce cas couperait un flux qui livrait du contenu — strictement pire que le défaut d'origine. Le reste du travail est donc d'ajouter la sentinelle manquante à ce générateur, pas de déplacer la levée.

**Ce qui n'est PAS prouvé, et je préfère l'écrire.** Le correctif repose sur un invariant de Starlette (pas d'octet ⇒ pas d'en-têtes ⇒ statut libre). Je l'ai vérifié par construction et par tests structurels, **pas** par un test de bout en bout avec un amont factice : cela demanderait de simuler une réponse `curl_cffi` vide à travers l'application ASGI. C'est le test qui manque, et c'est lui qui transformerait « corrigé » en « prouvé ».

### 2026-10-20 — V5.1 : le test comportemental a démenti le correctif structurel

**Ce que j'ai construit.** Un harnais ASGI réel : on patche `_open_free_stream` pour rendre un amont `200` qui ne produit **aucun octet**, on envoie une vraie requête `/v1/messages`, et on observe le statut. C'est la première fois dans ce chantier que la question « le client reçoit-il un 200 ? » est posée en exécution.

**Résultat : le test échoue — le défaut est intact.** Et il a démenti, en trois itérations, chacun des correctifs que j'avais écrits :

| Itération | Ce que le client recevait | Pourquoi le correctif n'avait pas prise |
|---|---|---|
| 1 | `200` + `upstream produced no output` | la branche corrigée n'était pas celle exécutée |
| 2 | `200` + `upstream stream aborted` | la levée était avalée par un `except Exception` du générateur |
| 3 | `200` + `upstream_free_empty_response` | encore une autre voie : la jambe free a son propre émetteur |

**Ce que ça établit.** Le chemin `/v1/messages` d'un modèle à protocole `openai` traverse **au moins cinq émetteurs d'erreur distincts** (lignes 13192, 14600/14651, 14793, 14805, 16632). En corriger un révèle le suivant, parce que chacun est protégé par un `except Exception` qui, de bonne foi, transforme l'erreur en trame SSE.

**Ce qui a quand même été corrigé et reste utile :**
- `streaming/sse.py` : une exception portant un `status_code` explicite **n'est plus convertie en trame** par la pompe. C'était le verrou n°2 — une règle générale, pas un cas particulier.
- Deux branches lèvent désormais au lieu d'écrire dans un 200, avec compteur avant la levée.

**Ce que je ne fais pas, et c'est une décision.** Continuer site par site est une chasse sans fin : chaque correctif est invalidé par le suivant, et rien ne garantit qu'il n'en reste pas trois. Le bon correctif est un **point d'étranglement unique** — tamponner le premier chunk avant d'envoyer les en-têtes, et décider du statut à ce moment-là. C'est un changement de contrat assumé sur une quinzaine de sites `StreamingResponse`, à faire d'un bloc avec le harnais comme filet.

**Les trois tests comportementaux sont en `xfail` documenté**, pas supprimés : ils décrivent le comportement attendu, échouent tant qu'il n'est pas là, et passeront au vert le jour de la correction. La suite reste à 0 échec sans mentir sur l'état.

**La leçon la plus forte de la session, et elle est contre moi.** J'avais écrit, une heure plus tôt, que V5.1 était « corrigé sur le chemin Anthropic, prouvé par construction ». C'était faux, et le test que j'avais annoncé comme manquant l'a démontré en trois exécutions. La différence entre « le code a la bonne forme » et « le client reçoit le bon statut » n'est pas une nuance : c'est la seule qui compte.

### 2026-10-20 — ✅ V5.1 clos : un point d'étranglement plutôt qu'une chasse

**La décision.** Après trois correctifs démentis par le test comportemental, arrêter d'énumérer les sites. Un **middleware ASGI** (`streaming/empty_guard.py`) est placé le plus à l'extérieur de la pile : il retient les en-têtes d'une réponse **200 en flux** jusqu'au premier octet réel, puis décide.

| Premier contenu | Décision |
|---|---|
| contenu normal | relais transparent (les en-têtes partent enfin, octets intacts) |
| trame d'erreur « flux vide » | **502** dans le protocole du client |
| flux vide (aucun octet) | **502** |
| levée pendant le flux, rien d'envoyé | **502** (les `BaseExceptionGroup` d'anyio échappaient au handler applicatif) |

**La distinction qui a failli être perdue.** Ma première version convertissait **toute** première trame d'erreur en 502. Deux tests existants ont protesté, à raison : un `400 context too long` relayé en trame SSE doit continuer d'arriver **tel quel** — le convertir en 502 générique effacerait le message dont le client a besoin. Le garde ne se déclenche donc que sur les marqueurs de flux vide (`produced no output`, `empty stream`, `empty_response`, `stream aborted`). V5.1 ne corrige pas « une erreur est arrivée », il corrige « le client a reçu 200 alors que rien n'a été produit ».

**Deux pièges d'implémentation, trouvés par les tests :**
- après avoir rendu le 502, l'application continue d'émettre (`[DONE]`, trames) : les relayer déclenche un `assert` de Starlette (double réponse). D'où le drapeau `hijacked` qui ignore la suite.
- l'exception de flux arrive en `BaseExceptionGroup`, donc `except Exception` ne la voit pas — c'est précisément pourquoi le client recevait un 200 avant ce middleware.

| Point | État | Preuve |
|---|---|---|
| Preuve comportementale (ASGI, amont vide) | ✅ | `tests/test_empty_stream_asgi.py` — 3 tests, passent |
| Unités du garde (détection, relais, non-régression) | ✅ | `tests/test_empty_guard_middleware.py` — 13 tests |
| Une vraie erreur amont reste relayée | ✅ | test dédié |
| Statuts non-200 et réponses non-flux intacts | ✅ | tests dédiés |
| Incident comptable même quand le statut devient honnête | ✅ | `on_empty` → `_note_empty_stream("?", "choke:…")` |
| Suite complète | ✅ | **3 387 tests, 0 échec** (`_research/junit_l2.xml`) |

**Ce que ce détour a coûté et ce qu'il a appris.** Trois correctifs écrits, trois fois démentis en exécution, avant que la bonne réponse n'apparaisse : ce n'était pas un problème de sites, c'était un problème de **position**. Un contrôle placé au bon endroit vaut mieux que cinq correctifs au bon endroit. Et le test comportemental, que j'avais repoussé deux fois, est ce qui a tranché les quatre fois.

### 2026-10-20 — V6.3.1 : classer les échecs free avant de changer la reprise

**Le problème.** Un échec free était traité comme « un 429 », quelle qu'en soit la cause. Un `403` de forme de requête déclenchait donc la même **rotation de station** qu'un quota épuisé : cinq tunnels, cinq IP, pour un refus qui aurait échoué sur les cinq.

**Ce qui a été fait — la mesure, pas la politique.**
`upstream/free_failures.py` classe l'échec en `gate` / `quota` / `transit` / `auth` / `model` / `empty`, comptabilise par classe, et le tout est exposé par `GET /health?detail=full` (`free_failure_kinds`). Un point de câblage est branché sur la branche free non-200 du chemin Anthropic.

| Classe | Déclencheur | Rotation utile ? |
|---|---|---|
| `gate` | 403, ou 400 de forme | **non** — c'est le corps, pas l'IP |
| `quota` | 429 | oui |
| `transit` | 5xx / 529 | cooldown court |
| `auth` | 401 « Missing API key » | non — modèle fermé aux anonymes |
| `model` | 400 « Model is unavailable » | non — modèle éteint |
| `empty` | 200 sans octet | non — défaut de flux (V5.1) |

**Pourquoi s'arrêter là, volontairement.** Changer la politique de reprise sans mesure serait exactement le geste que cette session a invalidé cinq fois. La classification rend la décision **mesurable** : quand `gate` dominera, la rotation sera démontrée inutile avec des chiffres.

| Point | État | Preuve |
|---|---|---|
| Classifieur pur (statut + corps) | ✅ | `tests/test_free_failure_classification.py` — 24 tests |
| Compteur par classe, fail-soft, borné | ✅ | tests dédiés |
| Exposition `/health?detail=full` | ✅ | `free_failure_kinds` |
| Câblage sur la branche free non-200 | ✅ | un site, additif, sans changement de comportement |
| **Politique de reprise (`_free_bound`)** | ⏳ **non changée** | décision reportée après mesure |

### 2026-10-20 — V2.5 : refuser en nommant, plutôt qu'échouer ailleurs

**Le défaut.** Le catalogue nomme 10 modèles `gemini` (9 free, 1 payant) avec leur `base_url` et leur famille — mais le proxy n'a **aucun convertisseur** pour ce dialecte. Demander `gemini-3-flash` renvoyait vers l'endpoint chat en protocole `openai` : erreur d'amont incompréhensible, ou pire, un 200 vide.

**Le correctif.** `config/settings.py` expose `CATALOG_UNSUPPORTED` (les entrées du catalogue dont le `protocol` est `null`, donc 10 ids). `_route_for()` lève `UnsupportedModelFamily` (`status_code = 501`) avant toute résolution — un seul point d'étranglement pour les quatre points d'entrée. Le handler global rend le corps dans le protocole du client.

| Chemin | Réponse |
|---|---|
| `/v1/messages` | `501` `{"type":"error","error":{"type":"api_error","message":"Model 'gemini-3-flash' uses the 'gemini' protocol…"}}` |
| `/v1/chat/completions` | `501` `{"error":{"message":"Model 'gemini-3-pro' uses the 'gemini' protocol…"}}` |
| `/v1/responses` | `501`, même forme |

**La sonde qui a failli me tromper — deuxième de la session.** J'ai d'abord écrit un script qui cherchait les ids gemini à la racine du JSON : `data.get("models") or data`. La structure est `{"generated_from", "providers": {…}}`, donc la sonde a répondu « aucun modèle gemini » — et j'ai commencé à concevoir un enrichissement du cache pour un problème qui n'existait pas. La vérification par le générateur (`--check` → « 1 ajouté ») a contredit la sonde ; en regardant la vraie structure, les 10 entrées étaient là depuis le début.

**Ce que ça vaut comme leçon :** ce n'est pas la lecture du code qui a menti cette fois, c'est **mon propre outil de mesure**. Une sonde non vérifiée n'est pas une preuve — c'est une opinion avec une sortie console.

| Point | État | Preuve |
|---|---|---|
| Table des familles non converties | ✅ | `CATALOG_UNSUPPORTED`, 10 ids, deux jambes |
| Refus 501 nommant modèle + famille | ✅ | `UnsupportedModelFamily` |
| Un seul point de refus pour 4 points d'entrée | ✅ | `_route_for()` |
| Corps dans le protocole du client | ✅ | 3 endpoints vérifiés en ASGI |
| Aucun modèle convertible refusé | ✅ | test dédié + suite complète |
| Suite complète | ✅ | **3 423 tests, 0 échec** (`_research/junit_o1.xml`) |

### 2026-10-20 — V5.4 : vérifier la machine à états, pas seulement le contenu

**Ce qui manquait.** Les tests de conversion vérifiaient le *payload* ; aucun ne vérifiait l'**ordre**. Or un client SSE ne lit pas des octets : il construit un état. Un `content_block_delta` avant `content_block_start` le fait planter ; un `message_stop` manquant le laisse attendre indéfiniment ; deux `message_start` cassent son accumulateur. Dans les trois cas, le flux « contient la bonne réponse » et le client le refuse quand même.

**Le harnais.** Un amont factice rend un vrai flux OpenAI (role → deltas → finish → `[DONE]`), branché sur les deux ouvreurs de la jambe free, et une requête `/v1/messages` traverse l'application ASGI complète. Six tests en découlent :

| Test | Ce qu'il verrouille |
|---|---|
| Séquence | `message_start` → `content_block_start` → `content_block_delta`+ → `content_block_stop` → `message_delta` → `message_stop`, **dans cet ordre** |
| Unicité | exactement un `message_start` et un `message_stop` |
| Reconstruction | les deltas de texte recomposent `"Bonjour !"` |
| Fin | `message_delta.delta.stop_reason` présent et dans l'ensemble officiel |
| Silence | rien n'est émis après `message_stop` |
| Raisonnement | `reasoning_content` → bloc `thinking`, **jamais** mélangé au texte |
| Outils | `tool_calls` → bloc `tool_use` nommé, arguments en deltas |

**Un détail d'implémentation qui a coûté un aller-retour.** Mon premier faux amont n'exposait que `aiter_bytes` : le proxy a lu par `aiter_lines`, n'a rien vu, et le garde V5.1 a — correctement — rendu un **502**. Le défaut apparent venait du harnais, pas du proxy. Un faux amont doit exposer les deux interfaces, parce que le proxy en utilise deux selon le chemin.

**Résultat : aucune régression découverte.** La conversion Anthropic était déjà conforme sur les six points. C'est la première fois de la session qu'un test ajouté *confirme* au lieu de démentir — et il faut le dire ainsi : un test qui passe est une information plus faible qu'un test qui échoue, mais il fixe ce qui ne doit pas bouger.

| Point | État | Preuve |
|---|---|---|
| Ordre de la machine à états | ✅ | `tests/test_sse_contract.py` (6 tests) |
| Raisonnement isolé dans `thinking` | ✅ | test dédié |
| Outils en `tool_use` | ✅ | test dédié |
| Suite complète | ✅ | **3 430 tests, 0 échec** (`_research/junit_p1.xml`) |

### 2026-10-20 — V5.3 : l'usage Anthropic, vérifié plutôt que supposé

**Le contrôle.** Un amont qui déclare `{"prompt_tokens": 100, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 40}}` traverse l'application et le client Anthropic doit lire `output_tokens: 7` et `cache_read_input_tokens: 40`.

**Le résultat, mesuré.** La conversion était **déjà juste** :

| Événement | Usage émis | Verdict |
|---|---|---|
| `message_start` | `input_tokens: 4` (estimation), `output_tokens: 0`, `cache_read_input_tokens: 0` | conforme — l'amont n'a rien envoyé encore |
| `message_delta` | `output_tokens: 7`, `cache_read_input_tokens: 40` | **exact**, cache amont préservé |

**Le point qu'il faut nommer plutôt que masquer.** `message_start` annonce `cache_read_input_tokens: 0` alors que la valeur réelle sera 40. Ce n'est pas un bug de conversion : OpenAI ne publie son `usage` qu'à la fin du flux. Le client officiel est dans la même situation. Trois tests verrouillent donc ce **partage** — estimation au départ, vérité à la fin — au lieu de prétendre que le début du flux en sait plus qu'il ne peut.

**Ce qui reste ouvert, et pourquoi.** `cache_creation_input_tokens` n'est alimenté par aucun amont de la jambe free : l'émettre à zéro serait un mensonge de plus, l'omettre est conforme. Le comptage indexé par `x-opencode-request` (pas de double comptage sur reprise) reste à faire — il demande de suivre une requête à travers plusieurs tentatives, ce qui n'est pas vérifiable sans trafic réel.

| Point | État | Preuve |
|---|---|---|
| Clés Anthropic présentes (`input_tokens`, `output_tokens`, `cache_read_input_tokens`) | ✅ | 3 tests |
| `cached_tokens` amont → `cache_read_input_tokens` | ✅ | 40 attendu, 40 obtenu |
| Aucune contradiction départ/fin | ✅ | test dédié |
| `cache_creation_input_tokens` | ⏳ aucun amont ne le fournit | documenté |
| Comptage par requête sur reprise | ⏳ nécessite du trafic réel | documenté |
| Suite complète | ✅ | **3 434 tests, 0 échec** (`_research/junit_q1.xml`) |

### 2026-10-21 16:18 — Validation en direct après redémarrage

Le proxy a été redémarré ; voici ce que le processus **réellement en cours** répond, et non ce que les tests promettent.

**Chargement vérifié.** Chaque fichier modifié porte une date de modification antérieure au démarrage du process (`10/21/2026 16:18:53`) : `opencode.py` (12:19), `settings.py` (12:16), `empty_guard.py` (10:10), `free_failures.py` (11:05). Rien n'est resté en mémoire d'avant.

| Vérification | Résultat live |
|---|---|
| `GET /health?detail=full` | 200, `free_failure_kinds: {}` **et** `empty_stream_incidents: {}` présents — les deux instruments de mesure sont branchés |
| `gemini-3-flash` sur `/v1/messages` | **501** avec `"uses the 'gemini' protocol, which this proxy does not convert"` |
| `big-pickle` en flux (jambe free) | **200**, 6 trames, `message_start → content_block_start → content_block_delta → content_block_stop → message_delta → message_stop`, texte `'PROXY OK'` |

**La séquence observée en direct est exactement celle que `tests/test_sse_contract.py` verrouille.** C'est la première fois de la session qu'un test de contrat est confirmé par le serveur en production dans la même heure.

**Une mesure à noter : premier octet à 6,48 s.** Ce n'est pas une régression du garde V5.1, c'est sa conséquence assumée : le client reçoit désormais les en-têtes avec le premier **contenu** au lieu du premier octet de protocole. Comme un client Anthropic n'affiche rien avant le premier delta de texte, la différence est invisible — mais elle existe et doit être dite : le garde transforme « 200 immédiat puis silence » en « attente puis 200 véridique ». C'est le prix de V5.1, et il est acceptable pour un client qui attend de toute façon le contenu.

**Ce qui reste non prouvé en direct :** le déclenchement du garde lui-même. Provoquer un amont vide demanderait de contrôler l'amont. Il se manifestera dans `empty_stream_incidents` la première fois que la condition se produira — c'est précisément pour cela que le compteur existe.

### 2026-10-21 — Mesure de la jambe free : 21 appels réels, et la réponse à « peut-on l'accélérer »

**La question.** « Plus performante » veut dire quelque chose de précis seulement si on sait où part le temps. Deux mesures ont été faites sur le proxy en cours d'exécution, pas sur des mocks.

**Mesure 1 — fiabilité et latence (12 appels séquentiels).**

| Indicateur | Valeur |
|---|---|
| Succès | **12 / 12** (HTTP 200, contenu correct à chaque fois) |
| TTFT | min 1,6 s · médiane 4,6 s · max 9,2 s |
| Total | min 1,7 s · médiane 5,3 s · max 9,7 s |
| Incidents flux vide | **0** |
| Classes d'échec free | **aucune** |

**Mesure 2 — la station explique-t-elle l'écart ? (9 appels, station relevée avant chaque envoi)**

| Station | n | médiane | min | max |
|---|---|---|---|---|
| 1 | 2 | 3,9 s | 1,1 s | 6,7 s |
| 3 | 3 | 4,5 s | 4,5 s | **13,4 s** |
| 5 | 2 | 6,7 s | 6,0 s | 7,4 s |
| 2 / 6 | 1 / 1 | 4,5 s | — | — |

**Le résultat qui tranche.** L'écart *à l'intérieur* d'une même station (station 3 : 4,5 s puis 13,4 s) dépasse l'écart *entre* stations (3,9 s à 6,7 s). **La station n'est pas la variable.** Le temps se passe en amont, dans le délai de premier token du modèle — un levier que le proxy ne peut pas actionner en changeant de tunnel.

**Cumul sur 21 appels : 21 succès, TTFT p50 4,5 s, p90 7,4 s, max 13,4 s.**

**Ce que cette mesure interdit de faire.** Ajouter une « optimisation de sélection de station » : elle serait sans effet mesurable, et coûterait de la complexité. C'est exactement le genre de changement que l'audit initial recommandait au nom du bon sens.

**Le seul levier proxy restant, et son prix.** Lancer la même requête sur deux stations en parallèle et garder la première réponse réduirait la queue de latence (p90) — au prix du **double de consommation de quota** et du risque de doublons de facturation. C'est un arbitrage à décider sur les chiffres, pas à implémenter par défaut : à 500 requêtes par IP, diviser par deux le budget pour gagner ~3 s au p90 n'est pas gratuit.

| Point | État | Preuve |
|---|---|---|
| Fiabilité sous répétition | ✅ 21/21 | `_research/free_leg_baseline.json` |
| Latence caractérisée | ✅ p50 4,5 s / p90 7,4 s | idem |
| Station écartée comme cause | ✅ | `_research/free_leg_by_station.json` |
| Course parallèle entre stations | ⏳ **non implémentée** | décision à prendre sur le compromis quota |

### 2026-10-21 — V6.1.2 / V6.1.3 : la porte free, testée puis branchée

**Ce que j'ai trouvé en cherchant à brancher.** `config/free_gate.py` encode le modèle d'accès de la jambe free — `egress AND identity AND body` — et **n'avait aucun test**. Aucun fichier de `tests/` ne l'importait. Un modèle non testé n'est pas un modèle, c'est une intention.

**V6.1.3 — les tests d'abord (22).** La règle de conception : chaque facteur a ses tests, et chaque test échoue **seul** si son facteur régresse. Un unique `passed == False` dirait qu'il y a un problème sans dire lequel — or c'est précisément l'information dont on a besoin pour diagnostiquer un 403.

| Facteur | Ce que les tests verrouillent |
|---|---|
| `identity` | en-tête reconnu, casse ignorée, valeur vide refusée ; **le quartet historique ne suffit jamais** |
| `body` | `stream: true` obligatoire ; un stub `read` ne compte pas ; description trop courte refusée ; forme `input_schema` admise |
| `egress` | `rejected` fait échouer ; `unknown` ne fait pas échouer (on ne nie pas ce qu'on ne peut pas prouver) |
| verdict | les trois ensemble passent ; les trois peuvent échouer ensemble ; `as_log()` nomme chaque facteur |

**V6.1.2 — le branchement qui rend la mesure utilisable.** Sur un `403 gate`, le proxy interroge maintenant la théorie sur les en-têtes et le corps **réellement partis**, et compte le **nom des facteurs en échec** :

```
/health?detail=full
  free_failure_kinds : {"gate": 9}
  gate_factors       : {"body": 8, "identity": 1}
```

Cette ligne-là est le gain. Elle sépare enfin deux causes qu'un `403` confondait : « l'IP est mauvaise » (rien à faire côté corps) et « le corps n'a pas la forme » (l'IP est innocente). Le verdict complet va au log, jamais au client.

**Ce que ça ne fait pas.** Ça ne change aucune décision : le proxy continue de traiter le 403 comme avant. On mesure d'abord — la leçon a été payée cinq fois cette session.

| Point | État | Preuve |
|---|---|---|
| Théorie par facteur, indépendante | ✅ | `tests/test_free_gate_factors.py` (22) |
| Compteur de facteurs en échec | ✅ | `tests/test_free_failure_classification.py` (+6) |
| Exposition `/health?detail=full` | ✅ | `gate_factors` |
| Décision de reprise modifiée | ❌ **non** — mesure d'abord | — |
| Suite complète | ✅ | **3 463 tests, 0 échec** (`_research/junit_r1.xml`) |

> ⚠️ **Ces deux lots ne sont pas chargés par le proxy en cours** : le code a changé après le redémarrage de 16:18. Un nouveau redémarrage est nécessaire pour que `gate_factors` apparaisse dans `/health`.

### 2026-10-21 18:11 — Deuxième validation en direct

Redémarrage à `18:11:38` ; `opencode.py` (17:03) et `upstream/free_failures.py` (17:04) sont antérieurs — le dixième lot est bien chargé.

| Vérification | Résultat live |
|---|---|
| Clés de `/health?detail=full` | `free_failure_kinds`, **`gate_factors`**, `empty_stream_incidents` — les trois compteurs sont en place |
| `gemini-3-pro` | **501** |
| `big-pickle` en flux | **200**, TTFT 2,9 s, 6 trames, texte `'OK'` |
| Compteurs après un succès | vides — un succès ne compte pas comme un échec, c'est le comportement attendu |

**Ce qui n'est toujours pas prouvable sans provoquer la condition :** l'incrément de `gate_factors`. Il exige un `403 gate` réel, que le proxy construit lui-même un corps conforme, donc qu'on ne peut pas déclencher depuis le client. Le compteur se remplira si l'amont refuse — et c'est précisément à ce moment qu'on veut le lire.

### 2026-10-21 — V6.3.2 : écarter une station, mais seulement la coupable

**La question que ce lot tranche.** Un `403` free ne dit pas qui est fautif. Deux causes opposées y ressemblent : la **sortie** est refusée (IP brûlée, tunnel mort — écarter cette station est utile, les autres continuent) ou la **requête** est malformée (les cinq échoueront — écarter ne répare rien et consomme du quota de rotation).

La session avait déjà mesuré cette confusion une fois (V6.3.1) : un `403` de forme déclenchait la même rotation qu'un quota épuisé.

**La règle implémentée** (`upstream/station_faults.py`) :

| Signal | Imputé à la station ? | TTL |
|---|---|---|
| facteur `egress` **seul** en échec | **oui** | 300 s |
| `egress` **+** `body`/`identity` | **non** — l'accusation n'est pas démontrable | — |
| `body` / `identity` seuls | **non** | — |
| timeout / connexion impossible | oui | 60 s |
| transit (5xx) | oui, brièvement | 45 s |

**Le choix de conception qui compte : aucun état dupliqué.** Le module ne tient pas de table de disponibilité — les cooldowns vivent dans la pool (`_set_free_cooldown`), seule source de vérité. Ce module fournit la **politique** (qui accuser, combien de temps) et un **compteur**. Tenir un second état aurait créé deux vérités qui divergent, le défaut que cette session a corrigé trois fois.

**Un détour que je dois avouer.** J'ai inséré le bloc par script et cassé l'indentation deux fois de suite — `except` au mauvais niveau, puis suppression du mauvais fragment par un script trop malin. `python -c "import ast; ast.parse(...)"` a servi de garde-fou avant chaque essai suivant. C'est la deuxième fois de la session qu'une édition scriptée casse l'indentation : la leçon est claire, pour du code structuré, l'édition se fait par `edit` avec ancre, pas par script.

| Point | État | Preuve |
|---|---|---|
| Politique d'attribution (6 cas, dont le subtil) | ✅ | `tests/test_station_faults.py` (18) |
| TTL par cause, bornés 5–600 s | ✅ | tests dédiés |
| Mise à l'écart via la pool (état unique) | ✅ | test structurel |
| Compteurs `/health` → `station_faults` | ✅ | test structurel |
| Suite complète | ✅ | **3 482 tests, 0 échec** (`_research/junit_s2.xml`) |

> ⚠️ **Non chargé par le proxy en cours** : le code a changé après le redémarrage de 18:11. Un redémarrage est nécessaire pour que `station_faults` apparaisse et que l'écartement agisse.

### 2026-10-21 — V6.5.3 : borner la durée, pas les étapes

**Le trou.** Les timeouts existants bornent des **étapes** — TTFB, inactivité SSE. Aucun ne borne la **durée de vie**. Un amont qui accepte la connexion puis se tait laissait donc le client suspendu : ni statut, ni message, ni fin. C'est le pire état possible pour un client, pire qu'une erreur.

**Ce qui a été fait** (`streaming/deadline.py`, middleware le plus externe) :

| Situation à l'échéance | Réponse |
|---|---|
| rien n'a été envoyé | **504** avec `{"error": {"message": "Request exceeded the total time budget (900s)…"}}`, dans le protocole du client |
| en-têtes déjà partis, flux SSE | une dernière trame `event: error` puis fermeture — on ne peut plus changer le statut, mais on peut dire pourquoi |
| réponse déjà terminée | **rien** — un dépassement après la fin ne doit pas corrompre une réponse valide |
| chemin `/health`, `/api/`, `/static/`, `/ws` | **jamais borné** — le tableau de bord doit répondre même quand tout le reste est bloqué |

**Le budget est large, et c'est un choix.** 900 s (`timeouts.request_budget_s`), quand la mesure donne un maximum de 13,4 s sur la jambe free. Il n'est pas là pour optimiser : il est là pour qu'une attente ne soit **jamais infinie**. `0` désactive explicitement.

**Un détail qui a demandé un paramètre en plus.** `resolve_budget` ramène tout budget sous 5 s à 5 s — protection contre une valeur absurde en configuration. Mais les tests ne peuvent pas attendre cinq secondes pour vérifier un abandon. D'où `min_budget`, abaissable en test, à sa valeur de production par défaut. Un garde-fou de production et un test doivent pouvoir coexister sans qu'on retire le garde-fou.

| Point | État | Preuve |
|---|---|---|
| Abandon avant envoi → 504 | ✅ | `tests/test_request_deadline.py` (22) |
| Message nommant le budget, forme Anthropic | ✅ | tests dédiés |
| Flux commencé → trame d'erreur finale | ✅ | test dédié |
| Réponse terminée jamais corrompue | ✅ | test dédié |
| Chemins du tableau de bord exemptés | ✅ | 5 chemins testés |
| `0` désactive | ✅ | test dédié |
| Suite complète | ✅ | **3 505 tests, 0 échec** (`_research/junit_t1.xml`) |

> ⚠️ **Non chargé par le proxy en cours** : le registre des middlewares change au démarrage. Un redémarrage est nécessaire pour que le budget agisse.

### 2026-10-21 — V8.3 : savoir *quel* modèle est lent, sans laisser le client choisir la mémoire

**Ce qui manquait.** La latence était mesurée globalement (`proxy_ttfb_upstream_ms`) et rendue en Prometheus sur une **liste fermée** de séries. Impossible de répondre à « quel modèle est lent, sur quelle jambe ». Or c'est la seule question qui permet d'agir.

**Ce qui a été fait — en réutilisant l'existant, pas en dupliquant.** Le module `observability/metrics.py` avait déjà des rings de latence génériques ; il a reçu deux fonctions : `leg_latency_key` (nommage pur) et `observe_leg_call` (observation + compteur de statut). Aucun second registre n'a été créé.

```
/health?detail=full
  latency_by_leg : {"leg_ms:free:big-pickle": {"n": 12, "p50_ms": 4530.0, "p95_ms": 7400.0}}
  free_failure_kinds : {...}   gate_factors : {...}   station_faults : {...}
  empty_stream_incidents : {...}
```

**Le risque que le branchement créait, et sa borne.** Un nom de modèle vient du client : une série par modèle sans plafond, c'est une fuite mémoire pilotée de l'extérieur. `max_keys = 200` ; au-delà, les modèles excédentaires partagent `leg_ms:<jambe>:other`. On perd du détail sur les modèles rares, on garde le service — et les tests le verrouillent explicitement.

**Le choix de branchement.** L'observation est posée dans `_log_free_model_usage`, seul point de passage qui reçoit **le modèle, le statut et la durée** ensemble. L'instrumenter chez les quatre appelants aurait dupliqué la logique et laissé des trous — c'est le même raisonnement qui a produit le point d'étranglement de V5.1.

**Un détail qui a failli passer.** Les séries `leg_ms:*` n'apparaissaient pas dans `/metrics` : le rendu Prometheus itère une liste **fermée** de jauges. Sans l'ajout explicite à `/health`, l'instrument aurait existé sans être lisible — un compteur que personne ne peut voir ne mesure rien. Un test vérifie maintenant que le préfixe filtré par `/health` est bien celui que le code écrit.

| Point | État | Preuve |
|---|---|---|
| Latence p50/p95 par jambe et modèle | ✅ | `tests/test_leg_metrics.py` (15) |
| Espace de clés borné | ✅ | test dédié (`other`) |
| Compteur de statut par jambe | ✅ | test dédié |
| Exposition `/health?detail=full` | ✅ | `latency_by_leg` |
| Taux de reprise | ⏳ non instrumenté | documenté |
| Suite complète | ✅ | **3 521 tests, 0 échec** (`_research/junit_u1.xml`) |

> ⚠️ **Non chargé par le proxy en cours** — redémarrage nécessaire.

### 2026-10-21 (soir) — « c'est plus lent » : mesure, pas impression

**La plainte, prise au sérieux.** Après le redémarrage, la perception est « beaucoup plus lent ». Trois lots venaient d'être chargés (V6.3.2, V6.5.3, V8.3). J'ai mesuré avant de discuter.

**Mesure 1 — 8 appels `big-pickle` contre la baseline de 18:11 :**

| | alors | maintenant |
|---|---|---|
| p50 total | 4,5 s | **3,7 s** |
| max | 13,4 s | 15,0 s |

Plus les en-têtes et le premier contenu arrivent au **même instant** (2,9 s / 2,9 s), donc le garde de flux n'ajoute aucun délai : il retient les en-têtes jusqu'au premier octet, il ne retarde pas le contenu.

**Mesure 2 — le trafic réel, par heure (base `requests`, 14 h) :**

```
deepseek-v4.1-flash-free   06h 49,0s | 08h 55,1s | 12h 34,5s | 17h 21,1s | 19h 22,6s | 20h 32,7s
```

Le modèle utilisé est lent **depuis 06:00**, soit bien avant mes modifications ; 17 h–19 h ont même été les heures les plus rapides de la journée. La hausse de 20 h coïncide avec la dégradation du parc VPN, pas avec un changement de code.

**Mesure 3 — la distribution, qui explique tout (2 h) :**

| Modèle | < 5 s | 5–15 s | 15–30 s | 30–60 s | > 60 s |
|---|---|---|---|---|---|
| `deepseek-v4.1-flash-free` (531) | **0 %** | 20,9 % | **51,6 %** | 25,8 % | 1,7 % |
| `jev-1.13-free` (163) | **95,7 %** | 0 % | 0,6 % | 0,6 % | 3,1 % |
| `big-pickle` (8) | 75,0 % | 25,0 % | 0 % | 0 % | 0 % |

`deepseek-v4.1-flash-free` n'a **aucun** appel sous 5 s : sa lenteur n'est pas un incident, c'est sa nature sur la jambe free aujourd'hui. `jev-1.13-free` répond en moins de 5 s dans 96 % des cas. La différence perçue vient du modèle choisi, pas du proxy.

**Mesure 4 — l'état du parc au moment de la plainte :** station 2 `AUTH_FAILED`, station 5 échec TLS, stations 1 et 6 dégradées (`reset-recent`), station 3 `connecting`. Une seule station pleinement saine (4). Cela explique la bosse de 20 h, et ce n'est pas du code.

**Ce que la mesure a écarté.** Chacun des trois lots récents a été vérifié comme non-causal : V6.5.3 ne fait qu'envelopper l'appel dans `wait_for` (aucun coût mesurable, aucun déclenchement sous 900 s) ; V8.3 ajoute deux acquisitions de verrou par appel (µs) ; V6.3.2 ne s'active que sur un 403 de sortie (aucun dans la fenêtre : `station_faults` vide).

**Ce que la mesure a désigné.** Deux leviers réels, chiffrés :

1. **Changer de modèle** pour l'usage interactif : `jev-1.13-free` (96 % < 5 s) plutôt que `deepseek-v4.1-flash-free` (0 % < 5 s). Gain immédiat, aucun code.
2. **Watchdog de premier octet sur la jambe free** — il n'existe que sur la jambe payante (`circuit_breaker.ttfb_watchdog`, 300 s). Sur la free, un essai peut rester muet jusqu'à 131,8 s (mesuré), et 476 s (08 h). Cela ne raccourcit pas la médiane ; cela supprime la queue que l'on remarque.
3. **Requêtes hedged** (V6.3.3) : lancer un second essai après 8 s. Gain p90 ≈ 3 s, coût 2× quota — décision d'usage, pas décision technique.

**La leçon, la même que les cinq fois précédentes.** Une impression de régression ne se réfute pas par un raisonnement sur le code : elle se réfute par une distribution. Ici la distribution dit « le modèle est lent », pas « le proxy est lent » — et elle le dit avec des chiffres que le code ne peut pas contredire.

### 2026-10-21 (soir) — V6.3.4 : le SLO, pour que la prochaine fois ce soit le proxy qui réponde

**Le constat de départ.** La question « est-ce lent ? » a coûté plusieurs heures de mesure manuelle. La donnée existait (`leg_ms:*`, V8.3) ; il manquait la comparaison à une cible et le verdict.

**Ce qui a été fait** (`observability/slo.py`, pur, branché dans `/health`) :

| Situation | Verdict |
|---|---|
| `p50` au-delà de 4 s | **breached** — c'est le régime, pas une queue |
| `p95` seul au-delà de 12 s | **degraded** — le régime tient, la queue casse |
| moins de 5 mesures | **unknown** — une médiane sur deux points vaut l'un des deux points |
| que des séries jeunes | **unknown**, jamais `ok` |

**Un défaut trouvé par les tests, pas par la relecture.** La première version renvoyait `ok` quand *toutes* les séries étaient `unknown` : « tout va bien » alors qu'on n'avait rien mesuré de concluant. C'est le pire message possible — rassurant et faux. Le test `test_too_few_samples_is_unknown` l'a attrapé ; c'est le module qui a été corrigé, pas le test.

**Le verdict, assumé, sur la mesure du soir :**

```
slo.status = "breached"
  leg_ms:free:jev-1.13-free          n=5   p50 4,3 s   (cible 4 s)
  leg_ms:free:deepseek-v4.1-flash-free   p50 24,0 s  p95 50,0 s
slo.status = "breached"
```

La jambe free **violait déjà la cible** avant ce lot : le SLO ne crée pas le problème, il le rend visible. C'est exactement ce qu'on lui demande.

| Point | État | Preuve |
|---|---|---|
| Verdicts (breached / degraded / ok / unknown) | ✅ | `tests/test_slo_latency.py` (16) |
| Seuil de mesures insuffisantes | ✅ | test dédié |
| Aucune série jeune ne masque une série cassée | ✅ | test dédié |
| Verdict exposé dans `/health` | ✅ | test structurel |
| Alerte active au-delà | ⏳ reste ouvert | documenté |
| Suite complète | ✅ | **3 538 tests, 0 échec** (`_research/junit_v1.xml`) |

> ⚠️ **Non chargé par le proxy en cours** — redémarrage nécessaire.

### 2026-10-21 (nuit) — V6.2.1 : sortir les shims du code, avec leur statut de nécessité

**Le problème, tel qu'il était posé au départ de la session.** « Le but est d'aligner le maximum tout en analysant la jambe gratuite pour la rendre plus maintenable. » Les deux shims qui font passer la porte vivaient en dur dans un fichier de 20 000 lignes : ni ajustables, ni documentés, et surtout **sans dire lesquels sont load-bearing**.

**Ce que la mesure d'audit disait déjà.** `read` avec une définition réaliste → 200. `bash` seul → 403. `edit` seul → 403. Description courte ou `properties: {}` → 403 *même avec le bon nom*. Le registre ne contient donc pas « des outils utiles » : il contient « le minimum qui satisfait la grille ».

**Ce qui a été fait.** Les définitions sont dans `config.yaml > free.shims`, chacune portant `necessity: required | optional`. Le code garde le même repli, donc l'ordre de chargement est : repli du code → configuration valide par-dessus.

**Le garde-fou qui compte, et pourquoi il fallait en poser un.** Déplacer des définitions mesurées dans un fichier de configuration, c'est donner à un futur éditeur le pouvoir de casser la porte sans le savoir — une description raccourcie et chaque requête free devient un 403 muet. Le chargeur refuse donc :

| Entrée refusée | Raison |
|---|---|
| description < 40 caractères | la porte blackliste les descriptions placeholder |
| `properties: {}` | 403 même avec un nom et une description corrects |
| `parameters` absent ou non-`object` | schéma invalide |
| `necessity` hors `required`/`optional` | statut inconnu |

Une entrée refusée **ne remplace jamais un défaut valide** : c'est testé (`test_an_invalid_entry_never_erases_a_default`).

**Une erreur de ma part, attrapée par un test.** J'ai écrit un test où la description faisait 38 caractères — refusée, donc le défaut l'emportait et l'assertion tombait. Le module avait raison, le test avait tort : une description de 38 caractères *doit* être refusée. C'est exactement le comportement qu'on veut, vérifié par accident puis verrouillé exprès.

| Point | État | Preuve |
|---|---|---|
| Registre déclaratif en configuration | ✅ | `config.yaml > free.shims` |
| Statut de nécessité explicite | ✅ | `read: required`, `bash: optional` |
| Validation au chargement + raisons | ✅ | `tests/test_free_shims_registry.py` (22) |
| Une erreur de config ne casse pas la porte | ✅ | test dédié |
| Formes OpenAI et Responses inchangées | ✅ | tests dédiés |
| Suite complète | ✅ | **3 561 tests, 0 échec** (`_research/junit_w1.xml`) |

> ⚠️ **Non chargé par le proxy en cours** — redémarrage nécessaire.

### 2026-10-22 — V6.2.2 : deux shims, un seul nécessaire

**La question laissée ouverte par l'audit.** `read` était prouvé nécessaire. `bash` ne suffisait pas seul — mais **apporte-t-il quelque chose en présence de `read` ?** Personne ne l'avait mesuré. Le registre déclaratif de V6.2.1 rendait la réponse actionnable ; il manquait le chiffre.

**La matrice** (`_research/measure_shim_necessity.py`, 4 corps, même modèle, même tunnel, seule différence : la liste `tools`) :

| Variante | Statut | Lecture |
|---|---|---|
| aucun outil | **403** | la porte exige au moins un outil |
| `read` seul | **200** | `read` est nécessaire **et suffisant** |
| `bash` seul | **403** | `bash` ne remplace pas `read` |
| `read` + `bash` | **200** | `bash` n'ajoute rien au verdict |

Le message d'erreur est le même dans les deux cas de refus — `FreeTierError`, « can only be used from within OpenCode » — donc la porte ne distingue pas « pas d'outil » de « mauvais outil » : elle refuse, point.

**Conclusion appliquée.** `bash` est retiré aux trois endroits qui le portaient : le registre (`config/free_shims.py`), la configuration livrée (`config.yaml`) et la liste d'injection (`missing = ("bash", "read")` → `("read",)`). Il reste **réintroduisible par configuration** si une mesure future le réhabilite — la décision est dans les données, pas gravée dans le code.

**Neuf tests portaient l'ancienne réalité** (`{"bash", "read"}` attendu). Les aligner demandait de distinguer deux choses : un test qui casse parce que le code a régressé, et un test qui casse parce que la **mesure** a changé la vérité. Ici c'est le second cas — chaque assertion modifiée porte en commentaire la mesure qui la justifie, sinon quelqu'un « réparera » la suppression dans six mois en croyant à un oubli.

Un cas méritait un examen plutôt qu'un remplacement mécanique : le tri ICU. Avec `bash` en moins, l'outil client `Bash` (majuscule) passe **avant** le shim `read` (minuscule) — la collation met la majuscule en tête. L'assertion « le shim passe devant » est donc devenue « l'outil client passe devant, et c'est le **même objet** » : on vérifie désormais l'absence de recopie, pas un ordre qui n'a plus de sens.

**Ce que la suppression ne prouve pas.** Elle ne dit rien de la qualité des réponses ; elle dit qu'un outil de moins suffit à franchir la porte. Moins d'octets sur le fil, un shim de moins à maintenir.

| Point | État | Preuve |
|---|---|---|
| `bash` mesuré non load-bearing | ✅ | `_research/shim_necessity.json` |
| `read` seul suffit | ✅ | 200 mesuré deux fois |
| Retrait du registre, de la config, de l'injection | ✅ | `tests/test_free_shims_registry.py` (29) |
| Réintroduction par configuration | ✅ | test dédié |
| 9 tests alignés sur la mesure | ✅ | commentaire dans chaque assertion |
| Suite complète | ✅ | **3 568 tests, 0 échec** (`_research/junit_y1.xml`) |

> ⚠️ **Non chargé par le proxy en cours** — redémarrage nécessaire.

### 2026-10-22 — V6.2.3 : une condition testée et **non** remplie (le résultat est le lot)

**L'hypothèse optimiste.** Si la porte acceptait un corps non-stream, on supprimait le forçage `stream:true` et toute la reconstitution JSON qui va avec — présentée par l'audit comme « source de latence et de bugs ». Le lot était donc conditionnel : *si* la porte accepte.

**La mesure** (3 corps identiques sauf `stream`, même modèle, même tunnel) :

| Corps | Statut | Contenu |
|---|---|---|
| `stream: true` | **200** | `text/event-stream`, vrai flux |
| `stream: false` | **403** | `FreeTierError` |
| `stream` absent | **403** | `FreeTierError` |

**Conclusion : le forçage reste.** Il n'est pas une commodité de notre côté, il est la condition d'entrée. Le corps est rejeté **avant** toute génération — 1,2 s contre 5,7 s pour un appel accepté, ce qui confirme un refus immédiat de la porte, pas un échec du modèle.

**Pourquoi cocher un lot qui ne change rien.** Parce que le livrable n'est pas du code ici, c'est une **décision documentée**. Sans cette mesure, la prochaine personne qui trouve la reconstitution JSON coûteuse la supprimera comme une évidence — et cassera la jambe free sans comprendre. Le fichier `_research/gate_stream_requirement.json` en garde la preuve brute.

C'est aussi la **première prémisse d'audit que cette session confirme** au lieu de la réfuter (les cinq autres sont tombées : `tool_choice:"auto"`, `max_completion_tokens`, `store`, « 5 incohérences de protocole » ramenées à 1, « 81 ids free exposables » ramenés à `big-pickle`). Une audit n'est pas faux par nature ; il fallait juste le vérifier.

| Point | État | Preuve |
|---|---|---|
| `stream:true` exigé par la porte | ✅ mesuré | 200 / 403 / 403 |
| Forçage conservé (load-bearing) | ✅ | aucun changement de code |
| Preuve brute conservée | ✅ | `_research/gate_stream_requirement.json` |
| Contrat verrouillé par des tests | ✅ | `tests/test_gate_stream_contract.py` (7) |
| Suite complète | ✅ | **3 576 tests, 0 échec** (`_research/junit_z1.xml`) |

### 2026-10-22 — V7.5 : la jambe gratuite tient en une page

**Pourquoi maintenant.** Vingt-trois lots ont produit du savoir dispersé : la porte à trois facteurs (V6.1.2), le registre de shims (V6.2.1), la mesure qui retire `bash` (V6.2.2), le contrat `stream:true` (V6.2.3), la politique de station (V6.3.2), les métriques (V8.3), le SLO (V6.3.4). Ce savoir vivait dans les journaux de ce plan — c'est-à-dire nulle part pour quelqu'un qui ouvre le dépôt.

**Ce que contient `docs/free-leg.md`** (10 sections, une page dense) :

| Section | Ce qu'elle évite |
|---|---|
| la conjonction `egress ∧ identity ∧ body` | chercher « pourquoi ce 403 » dans 20 000 lignes |
| le contrat de corps mesuré (tableau) | « corriger » `tool_choice:"auto"` qui est déjà juste |
| la politique de station (qui accuser) | écarter une station pour une faute de forme |
| les six champs de `/health` | ignorer que le diagnostic existe déjà |
| les distributions de latence | re-débattre de « c'est lent » sans chiffres |
| les commandes de re-mesure | croire un commentaire plutôt qu'une mesure |
| les six prémisses tombées | refaire l'audit deux fois |

**Le choix éditorial qui compte.** Chaque affirmation porte son **niveau de preuve** : « mesuré live (10 corps) », « tests (18) », ou « **non** — la cible n'est pas atteinte ». Un document qui ne distingue pas ces trois niveaux devient une source de fausses certitudes, et c'est précisément ce qui a coûté du temps dans cette session : cinq prémisses d'audit ont dû tomber une par une devant la mesure.

**Ce qu'il assume.** La section 9 dit noir sur blanc que la cible p50 < 4 s **n'est pas atteinte** sur la jambe free. Un document de référence qui se termine par « tout va bien » ne serait pas un document de référence.

| Point | État | Preuve |
|---|---|---|
| Trois facteurs + preuves | ✅ | `docs/free-leg.md` §2 |
| Contrat de corps mesuré | ✅ | §3, renvoie à `_research/` |
| Politique de station | ✅ | §5 |
| Diagnostic (6 champs) | ✅ | §6 |
| Commandes de re-mesure | ✅ | §8 |
| Niveaux de preuve distingués | ✅ | §9 |
| Prémisses tombées | ✅ | §10 |

### 2026-10-22 — V7.3 : un cliquet, pas une interdiction

**Ce que l'audit annonçait.** « 4 URLs `opencode.ai` en dur à supprimer. » La mesure en a trouvé **deux réelles** dans `opencode.py`, plus des défauts légitimes dans le résolveur et les constructeurs de catalogue. Sixième prémisse d'audit inexacte — mais le problème de fond existait : rien n'empêchait d'en ajouter une cinquième demain.

**Pourquoi pas une interdiction sèche.** Parce qu'une des valeurs en dur **est le protocole** : `Origin: https://opencode.ai` fait partie du facteur *identity* mesuré. La remplacer par un calcul serait un changement de comportement déguisé en nettoyage — exactement le genre de « correction » qui casse la jambe free sans laisser de trace.

**D'où un cliquet à deux dents :**

| Assertion | Ce qu'elle empêche |
|---|---|
| aucun littéral hors liste autorisée | d'en ajouter un nouveau sans passer par le résolveur |
| aucune entrée autorisée devenue obsolète | que la liste pourrisse et cesse de protéger |

Chaque entrée porte sa **raison** (« `Origin`/`Referer` = facteur identity mesuré », « défaut du résolveur »…). Une raison vide est un test en échec, pas un commentaire.

**Un scanner qui ne voit rien passerait tout.** Deux garde-fous : `test_the_scanner_really_scans` vérifie qu'il lit plus de dix fichiers, et j'ai **injecté un littéral volontaire** dans `config/` pour vérifier que le test échoue réellement — puis retiré le fichier piège. Un cliquet qu'on n'a jamais vu mordre n'est pas un cliquet.

**Une erreur de ma part, corrigée par le test lui-même.** Ma première version comptait les URLs dans les **docstrings** comme du code (six faux positifs). La règle resserrée est plus juste : seul un littéral **entre guillemets** compte — `POST https://…` dans une phrase n'est pas un appel réseau.

| Point | État | Preuve |
|---|---|---|
| 10 littéraux inventoriés avec raisons | ✅ | `tests/test_no_hardcoded_urls.py` (6) |
| Nouveau littéral refusé | ✅ | vérifié par injection |
| Entrée obsolète refusée | ✅ | test dédié |
| Valeur d'identité préservée | ✅ | `test_the_identity_header_is_not_relocated` |
| Docstrings hors périmètre | ✅ | règle « entre guillemets » |
| Suite complète | ✅ | **3 583 tests, 0 échec** (`_research/junit_z2.xml`) |

### 2026-10-22 — V8.1 : le tableau de bord qui ne crie pas au loup

**Ce qui a déclenché ce lot.** Un « le proxy ne fonctionne plus » côté client. Le proxy répondait `200` à tout. La cause : le modèle demandé, `jev-1.13-free`, est **refusé en amont** (`api_usage_error / Invalid request`) — et le proxy renvoyait ce refus dans une trame `event: error` avec un statut `200`. Côté client : « ça ne marche pas » sans code d'erreur. Côté mesure : un modèle à 4 s, donc « rapide ».

**La leçon, et elle est pour moi.** Plus tôt dans cette session, j'ai recommandé `jev-1.13-free` comme « 3× plus rapide que deepseek ». Il était 3× plus rapide **parce qu'il ne générait rien**. Ma mesure comparait des latences sans vérifier le contenu — un `200` n'est pas un succès. C'est la faute la plus coûteuse de la session, et elle a coûté à l'utilisateur, pas à moi.

**Ce que le lot livre :**

| Pièce | Rôle |
|---|---|
| `observability/drift.py` | compare catalogue et routage, sans lever |
| `GET /api/drift` | **orphelins** (routé, absent du catalogue : actionnable) vs **candidats** (pas un défaut) |
| `GET /api/free-health` | modèles dont le **dernier** appel a réussi — un 200 ne suffit pas |
| `metrics.mark_leg_status` | retient le statut par série, borné, pour distinguer « observé » de « servi » |

**Trois pièges désamorcés, chacun mesuré :**

1. **Les alias.** `jev-1.13` part en `jev-1.13-free`. Comparer les noms routés au catalogue fabriquait **huit faux orphelins** — le genre de bruit qui rend un tableau de bord inutilisable. On compare ce qui part réellement.
2. **Le snapshot périmé.** Un id absent du snapshot mais servi **avec succès** n'est pas mort : c'est le fichier qui a vieilli (`jev-1.13` n'y figure pas du tout).
3. **Le statut trompeur.** Le proxy renvoie `200` avec une trame d'erreur quand l'amont refuse. `leg_ok_models()` ne retient que les 2xx du **dernier** appel ; sinon `jev` passait pour servi.

**La mesure qui en sort** — 36 modèles free routés, une requête courte chacun :

| Verdict | Nombre | Exemples |
|---|---|---|
| servent | **20** | `nemotron-3.5-lightning-free` 1,6 s, `big-pickle` 2,6 s, `deepseek-v4.1-flash-free` 4,1 s |
| refusés en amont | **16** | `jev-1.13-free` 49,8 s, `mimo-v2.5-free` 64,6 s, `space-bunny`, `ling-3.1-flash-free` |

Le tri par latence classe les **morts** en premiers rangs des « rapides » quand ils échouent vite : c'est exactement le piège. Le verdict doit primer sur la durée.

**Une erreur corrigée par le test.** Ma première version du cliquet V7.3 figeait des **numéros de ligne** ; mes propres modifications ont décalé `opencode.py` de dix-neuf lignes et le garde-fou a crié « nouvelle URL » sur une URL préexistante. Un garde-fou qui casse à chaque refactoring est un garde-fou qu'on désactive : refait en **comptage par fichier**, avec le code fautif cité dans le message d'échec.

| Point | État | Preuve |
|---|---|---|
| `/api/drift` (orphelins / candidats) | ✅ | live sur 4100 : 1 orphelin free, 8 payants, 118 candidats |
| Alias résolus | ✅ | 8 faux orphelins → 1 |
| Annotation par succès seulement | ✅ | `mark_leg_status`, testé (17) |
| `/api/free-health` | ✅ | live : 2 servis après 2 appels |
| Balayage 36 modèles | ✅ | `_research/free_models_live_status.json` |
| `jev-1.13-free` diagnostiqué | ✅ | refus amont sur 5 variantes |
| Cliquet par comptage | ✅ | 7 tests, injecté puis vérifié |
| Suite complète | ✅ | **3 613 tests, 0 échec** (`_research/junit_z4.xml`) |

### 2026-10-22 (soir) — « le proxy ne fonctionne plus » : trois causes, dont une de moi

**Ce que l'utilisateur a vu.** Après un redémarrage : plus rien. Aucun code d'erreur côté client, juste des réponses inutilisables.

**Ce qu'il y avait réellement — trois causes distinctes, dans cet ordre de gravité :**

| Cause | Symptôme | Correction |
|---|---|---|
| **Mon conseil** : `jev-1.13-free` recommandé comme « 3× plus rapide » | ce modèle était refusé à **chaque** appel (`api_usage_error / Invalid request`) ; il était rapide parce qu'il ne générait rien | diagnostic mesuré (5 variantes) + `docs/free-leg.md` corrigé + tableau des 36 modèles |
| **16 des 36 modèles free** sont refusés en amont | le client tombe sur un modèle mort et reçoit une trame d'erreur dans un flux `200` | `/api/free-health` publie les modèles réellement servis |
| **Verrou périmé** (`logs/opencode-4000.lock`) | le redémarrage sortait `FATAL: another opencode-proxy instance is already running` ; l'ancien processus tenait le verrou sans servir correctement | processus arrêté, verrou libéré, relance vérifiée |

**Le défaut de mesure que je dois assumer.** Ma comparaison de latence ne vérifiait pas le **contenu** : un statut `200` suffisait. Or le proxy renvoie `200` avec une trame `event: error` quand l'amont refuse — le flux SSE est déjà ouvert, le code HTTP ne peut plus changer. J'ai donc classé un modèle mort en tête des « rapides » et conseillé de l'utiliser. La leçon est plus large que ce cas : **une mesure de performance sans vérification de résultat mesure la vitesse de l'échec.**

**La correction technique, et pourquoi un ContextVar.** Le refus amont est déposé dans `_current_free_refusal` juste avant la trame d'erreur ; le logging d'usage le relit et transmet le **statut amont** (400) à la métrique au lieu du 200 client. Le client, lui, ne voit aucun changement. Sans ça, `/api/free-health` continuait d'afficher `jev-1.13-free` comme servi — un tableau de bord qui ment est pire que pas de tableau de bord.

**Vérifié en live, sur les deux instances** (4100 pour tester, 4000 pour la production) :

```
big-pickle       200  3,8 s  flux ok
jev-1.13-free    200  4,9 s  REFUS AMONT
/api/free-health -> servis : ["big-pickle", "deepseek-v4.1-flash-free"]   (jev absent)
/api/drift       -> 1 orphelin free (jev), 8 payants, 118 candidats
```

| Point | État | Preuve |
|---|---|---|
| Cause `jev` diagnostiquée | ✅ | refus sur 5 variantes, 2 instances |
| Trame d'erreur ≠ succès pour la métrique | ✅ | `tests/test_free_refusal_metric.py` (5) |
| `jev` retiré des « servis » | ✅ | live : `servis` sans jev |
| Production relancée et vérifiée | ✅ | PID 34820, `/health` ok, 3 endpoints |
| Instance de test 4100 arrêtée | ✅ | port libéré |
| Suite complète | ✅ | **3 619 tests, 0 échec** (`_research/junit_z5.xml`) |

### 2026-10-22 (nuit) — V8.2 : un canary qui lit le contenu

**Le canary évident aurait menti.** « Une requête courte, et si le statut est 200, tout va bien. » Or c'est précisément le mensonge qui a coûté la soirée : `jev-1.13-free` répondait **200 en 4 s** et ne produisait rien. Un canary de statut l'aurait déclaré sain pendant des jours.

**La règle retenue, et elle est stricte.** Un probe n'est un succès que si le flux porte du **contenu** (`text_delta`, `thinking_delta` ou `content_block_delta`) **et** aucune trame `event: error`. Le statut HTTP est une condition nécessaire, jamais suffisante.

**Ce que le canary sonde.** Notre propre proxy, pas l'amont directement — c'est le chemin complet du client (routage, conversion, reconstitution) qui est vérifié. Deux probes, une par protocole client :

| Probe | Chemin | Ce qu'il prouve |
|---|---|---|
| `free.messages` | `/v1/messages` | la chaîne Anthropic → OpenAI → SSE tient |
| `free.chat` | `/v1/chat/completions` | la chaîne OpenAI native tient |

**Le réglage qui compte : trois échecs, pas un.** Une jambe free a des ratés normaux (rotation de tunnel, quota d'une IP). Alerter sur le premier hoquet rendrait l'alerte inutilisable — le sort exact d'un tableau de bord qu'on ignore. `degraded` pour un ou deux échecs, `failing` à trois.

**Vérifié dans les deux sens** (c'est le point : un canary qu'on n'a jamais vu échouer n'est pas un canary) :

```
cycle 1 (modèle refusé) : degraded  free.chat:degraded(1)  free.messages:degraded(1)
cycle 2                 : degraded  free.chat:degraded(2)  free.messages:degraded(2)
cycle 3                 : failing   free.chat:failing(3)   free.messages:failing(3)
/health                 : degraded
cycle sur modèle sain   : ok        free.chat:ok(0)        free.messages:ok(0)
```

**Un détail relevé au passage, non corrigé.** Après le retour à `ok`, `/health` restait `degraded` — à cause d'un **circuit breaker `systemone`** ouvert (famille sans convertisseur, 501), sans rapport avec le canary. C'est un vrai bruit dans l'indicateur global : un endpoint qu'on ne sait pas servir tire l'état général vers le bas en permanence. À traiter séparément.

**Coût.** Deux requêtes courtes toutes les 30 min = 96/jour, négligeable devant le quota free (500/IP). Désactivable par `canary.enabled: false`.

| Point | État | Preuve |
|---|---|---|
| Verdict sur contenu, pas statut | ✅ | `tests/test_canary.py` (21) |
| Deux protocoles clients | ✅ | live : 1,97 s et 1,75 s |
| Alerte à 3 échecs | ✅ | 3 cycles sur `jev-1.13-free` → `failing` |
| Remontée dans `/health` | ✅ | `health.status = degraded` |
| Retour à `ok` après succès | ✅ | compteur retombé à 0 |
| Actif en production | ✅ | PID 31900, `POST /api/canary/run` vérifié |
| Coût quota | ✅ | 96 req/j |
| Suite complète | ✅ | **3 641 tests, 0 échec** (`_research/junit_z6.xml`) |

### 2026-10-23 — V8.5 : ma conclusion de la veille était fausse

**Ce que j'avais écrit.** « `jev-1.13-free` est refusé par l'amont sur toutes les variantes. » C'était faux, et c'était écrit dans un document de référence.

**La vraie cause.** `jev-1.13` et `jev-1.13-free` ne sont **pas des modèles de chat** : leur endpoint est `https://opencode.ai/inference/v1/systemone`, un protocole à questions (`questions` + `state` + `criteria`/`instructions`). Mes tests les envoyaient sur `/v1/messages` — j'ai mesuré le mauvais chemin dix fois de suite et appelé ça une preuve.

**Pourquoi je ne l'ai pas vu.** Le message amont (`api_usage_error / Invalid request`) ne dit pas « mauvais protocole ». J'avais une hypothèse (modèle mort), un signal qui la confirmait en apparence, et je n'ai pas regardé **où** la requête partait — alors que `_cfg_settings.MODELS` l'indiquait en une ligne. Une mesure sur le mauvais chemin produit une conclusion solide et fausse.

**Ce qui est corrigé.** Le proxy refuse maintenant proprement, et le dit :

```
POST /v1/messages {"model":"jev-1.13-free"}  ->  400
Model 'jev-1.13-free' uses the SystemOne protocol (questions/state), not chat.
POST it to /v1/systemone instead.
```

Le client sait quoi faire au lieu de lire un refus amont opaque. `/api/free-health` isole par ailleurs les modèles `systemone` : 34 en chat, 2 en SystemOne — un mauvais protocole ne doit jamais être compté comme une panne.

**Ce qui reste non conclu, et pourquoi je ne « corrige » pas.** Sur son endpoint dédié, `/v1/systemone` valide le format (le proxy renvoie un `400` détaillé sur un `questions[...]` invalide) mais répond `422 Endpoint is unavailable` en 0,5 s sur toutes les variantes testées (`state` chaîne ou objet, `noul`, `choice`, version payante incluse). Deux causes possibles — endpoint indisponible, ou corps encore incomplet — et **aucune capture SystemOne de référence** dans `_research/` pour trancher. Inventer un corps jusqu'à obtenir un 200 serait deviner, puis figer une supposition dans le code. Documenté comme ouvert.

| Point | État | Preuve |
|---|---|---|
| Cause réelle identifiée | ✅ | `MODELS[jev-*].endpoint` = systemone |
| Conclusion fausse retirée des docs | ✅ | `docs/free-leg.md` §7 bis |
| Refus clair en chat (deux protocoles) | ✅ | live : `400` + endpoint nommé |
| `/api/free-health` partitionne systemone | ✅ | live : `systemone: [jev-1.13, jev-1.13-free]` |
| Tests du garde | ✅ | `tests/test_systemone_protocol_guard.py` (8) |
| SystèmeOne réellement fonctionnel | ❌ **non conclu** | `422` sur 4 variantes, sans capture |

### 2026-10-23 (matin) — V8.6, et une deuxième leçon de mesure

**Le problème opérationnel.** Après un redémarrage, le proxy ne répondait plus et refusait de démarrer : `FATAL: another opencode-proxy instance is already running (lock held: logs/opencode-4000.lock)`. Rien n'indiquait **qui** tenait ce verrou, ni si cette instance servait encore quelque chose. L'utilisateur se retrouvait avec un proxy mort et un message qui ne dit pas quoi faire.

**Le diagnostic ajouté.** Le message FATAL distingue désormais les deux situations, qui appellent des actions opposées :

| Situation | Message | Action |
|---|---|---|
| le port écoute | `an instance IS serving on 127.0.0.1:4000` | utiliser cette instance, ou la redémarrer proprement |
| le port n'écoute pas | `nothing is listening ... this lock holder is a zombie` + `kill <pid>` | tuer le zombie, relancer |

Le PID est écrit dans un **fichier voisin** (`<lock>.pid`) : sous Windows, un nouveau processus n'arrive pas toujours à relire le fichier verrouillé — c'était le cas ici, le PID manquait au premier essai. Vérifié en live : un second lancement sur 4000 affiche maintenant la bonne ligne.

**La leçon de mesure — la deuxième de la session, et la même erreur déguisée.** Le suite complète semblait **bloquée à 65 %** : processus vivant, 17 s de CPU en 50 minutes. J'ai tué deux exécutions en concluant au blocage. En isolant le fichier concerné : `107 passed in 830 s` — `tests/test_proxy.py` met **13 min 50 s** à lui seul (tests réseau réels). Ce n'était pas un blocage, c'était de la lenteur : **l'absence de progression observable n'est pas une preuve d'arrêt**, exactement comme un `200` n'est pas une preuve de succès. J'ai détruit deux mesures valides avec une conclusion hâtive.

**Conséquence pour V8.4 (CI).** Un fichier qui prend 14 minutes doit être marqué (`@pytest.mark.network`) et exclu des *required checks* rapides, sinon la CI sera désactivée par lassitude — le même mécanisme social qui fait ignorer un tableau de bord bruyant.

| Point | État | Preuve |
|---|---|---|
| PID lisible par le nouveau venu | ✅ | fichier `<lock>.pid` |
| Zombie vs instance vivante | ✅ | live : `IS serving on 127.0.0.1:4000` |
| Action donnée (`kill <pid>`) | ✅ | `diagnose_holder` + 10 tests |
| Diagnostic sans effet de bord | ✅ | 4 tests de robustesse (fichier absent/vide/binaire, port invalide) |
| Lenteur de `test_proxy.py` mesurée | ✅ | 830,78 s pour 107 tests |
| Suite complète | ✅ | **3 661 tests, 0 échec, 24 min** (`_research/junit_z9.xml`) |

### 2026-10-23 (matin, suite) — V8.4 : la CI devient utilisable

**Ce qui bloque une CI, ce n'est pas son absence : c'est sa lenteur.** Le gate existait (`ci.yml` : ruff, mypy, pytest, pip-audit, gitleaks, budget d'import) mais payait un `pytest` global de 24 minutes, dont **un seul test tenait 721 s**. Un job de 24 min expirera ou sera ignoré ; dans les deux cas il ne protège plus rien.

**Trois causes, toutes mesurées — aucune devinée.**

| Symptôme | Cause réelle | Correctif | Gain |
|---|---|---|---|
| un test de 721 s | `test_ddg_lock_eviction` patchait `DDGS` mais pas `_search_ddg_lite`, essayé **en premier** : 600 vrais appels réseau pour vérifier un LRU | patch du chemin réellement emprunté | 721 s → **0,13 s** |
| un test de 192 s, deux de 96 s | horloge factice (`_now_fn`) mais `asyncio.sleep` **réel** dans la boucle de sonde | `_sleep_fn` injectable (comme l'horloge) + sommeil nul dans les fabriques de test | 384 s → **~0 s** |
| suite 24 min | ces tests-là | — | **3 min 51 s** |

**Le gate bloquant.** `scripts/ci_contracts.py` exécute 22 fichiers / 330 tests / **5,7 s** : gate free (`stream:true`), parité client, routage et dérive du catalogue, santé de la jambe free, garde SystemOne, verrou mono-instance, URL en dur. C'est ce qui échoue vite quand un contrat casse ; le suite complet reste en second pas, et le test Docker (`chaos kill -9`) est exclu par `-m "not docker"` — il exige un daemon et faisait échouer la CI pour une raison sans rapport avec le code.

**Le gate se garde lui-même.** `tests/test_ci_contracts_gate.py` (34 tests) vérifie que chaque fichier déclaré existe. Vérifié en le faisant échouer : `test_canary.py` renommé → deux tests rouges, puis restauré. Sans ce garde, renommer un fichier de contrat rendrait la CI verte **à vide** — le pire état possible.

| Point | État | Preuve |
|---|---|---|
| Gate rapide et bloquant | ✅ | 330 tests / 5,7 s, code de sortie 0 |
| Suite complète faisable | ✅ | 3 661 tests / **231 s** (avant : 1 436 s) |
| Test Docker hors du gate | ✅ | `-m "not docker"` |
| Gate gardé contre le pourrissement | ✅ | 34 tests, vus échouer |
| `ci.yml` à jour | ✅ | contrat d'abord, budget 25 min |

### 2026-10-23 — V7.2 : deux réglages qui ne faisaient rien

**Ce que l'audit devait produire.** Un inventaire mécanique : quelles clés de `config.yaml` le code lit réellement. Une clé jamais lue est soit un vestige, soit une faute de frappe — et dans les deux cas l'utilisateur règle quelque chose qui n'agit pas.

**Deux vraies trouvailles, dont une de sécurité.**

| Clé | Ce qui se passait | Correctif |
|---|---|---|
| `dashboard_trust.require_token` | le code ne lisait que `DASHBOARD_REQUIRE_TOKEN` (env). Mettre `true` dans `config.yaml` ne protégeait **rien** | lu depuis le YAML, env prioritaire |
| `server.log_dir` | jamais lu : les logs vont dans `<projet>/logs` par constante | clé supprimée, commentée sur place |

Le premier cas est le plus grave : un booléen de sécurité qu'on croit actif. Ce genre de réglage ne se plaint jamais, donc il ne se découvre que par un audit — précisément ce que ce lot installe.

**Le premier rapport était faux à 87 %.** Le premier passage annonçait 14 clés mortes et 34 manquantes. Tout était du bruit : les cartes par identifiant (`thinking.min_tokens.<modèle>`, `geo.policies.<profil>`, `ip_rotation.free_model_candidates.<modèle>`) sont des données, pas des réglages ; les « manquantes » venaient d'une regex qui comptait les docstrings et les noms de section (`yaml_get("server")`). Corrigé : extraction par AST (les appels réels seulement) et exclusion explicite des cartes par identifiant. Après correction : **2 clés mortes, réelles, corrigées**.

**Ce qui reste de V7.2.** Le lot prévoyait un schéma strict (types, clés inconnues = erreur au boot). L'audit des clés mortes en est la partie qui rapporte ; la validation de **types** n'est pas faite, et un `enabled: "oui"` mal typé passe encore. Écrit ici pour ne pas être compté comme fait.

| Point | État | Preuve |
|---|---|---|
| Clés mortes détectées | ✅ | 2 trouvées, 0 restante |
| Réglage de sécurité appliqué | ✅ | `require_token` lu depuis le YAML |
| Faute de frappe détectée | ✅ | test : `watchdog_intervall` signalée |
| Gate CI bloquant | ✅ | `--strict` dans `ci.yml` |
| Validation des types | ❌ **non fait** | `enabled: "oui"` passe encore |

### 2026-10-23 — V8.7/V8.8 : un statut sans raison, un reset sans effet

**Parti d'une vérification de routine.** Après redémarrage, `/health` répondait `degraded` alors que : `upstream: ok`, aucun disjoncteur ouvert, chat free en 1,5 s, canary vert. Trois endpoints interrogés plus tard : un **pause de clé** `reason: "test"`, 275 s restantes, laissé par une vérification.

**Deux défauts, pas un.**

1. **Le statut ne disait pas sa raison** (V8.7). Un `degraded` sans explication oblige à deviner, et on devine mal — j'ai commencé par soupçonner la jambe free et les stations. Le payload porte maintenant `status_reasons`, produit par une fonction pure (`_health_status_reasons`) :

```json
{"status": "degraded",
 "status_reasons": ["key paused: 8b1abb9f9d91 (test, 105s left)"]}
```

2. **Le « reset » des pauses ne survivait pas au redémarrage** (V8.8). `POST /api/key-pauses/reset` répondait `{"reset":"all","count":1}`, `GET /api/key-pauses` renvoyait `{}` — et le pause **revenait au boot suivant**, parce que l'endpoint vidait la mémoire sans appeler `_save()`. Un reset qui ne persiste pas n'est pas un reset : c'est un mensonge de plus.

**Ce qui a rendu le second visible.** Le premier redémarrage après mon nettoyage manuel a fait réapparaître le pause avec 105 s au compteur. Sans ce redémarrage, j'aurais écrit « corrigé » sur un endpoint qui n'avait rien corrigé.

| Point | État | Preuve |
|---|---|---|
| Raison du statut exposée | ✅ | live : `key paused: … (test, 105s left)` |
| Fonction de statut testable | ✅ | 10 tests, sans réseau |
| Reset persistant | ✅ | 8 tests dont un rechargement depuis le fichier |
| Fichier de pauses vidé | ✅ | `logs/paused_keys.yaml` → `paused_keys: {}` |
| Suite / gate | ✅ | 3 719 tests ; gate 399 tests / 26 s |

**La leçon, la même depuis ce matin sous une autre forme.** Écrire « corrigé » sans rejouer le scénario de panne est une affirmation, pas un résultat : ici le bug a survécu exactement une fois de plus que prévu — au redémarrage.

### 2026-10-23 — V5.2 : une erreur dans le mauvais dialecte n'est pas une erreur

**Le test, avant le code.** Un client Anthropic (`POST /v1/messages`) demande `space-bunny-free` (amont chat, modèle indisponible). Ce qu'il reçoit :

```
data: {"error":{"message":"Free model request failed: …","type":"api_error"}}
data: [DONE]
```

C'est du **JSON OpenAI dans un flux Anthropic** : pas de `event:`, pas d'enveloppe `{"type":"error",…}`. Le SDK Anthropic ignore ces lignes et le client conclut à un flux vide. **Une erreur émise dans le mauvais dialecte est indiscernable d'une absence d'erreur** — exactement le défaut que V5.2 devait fermer, et il était ouvert.

**La cause.** `_stream_error_response` choisissait la forme d'après `protocol`, qui désigne le protocole **amont** (chat, pour un modèle free), au lieu du protocole **client**. Ajout de `client_protocol`, déclaré sur les 9 sites d'appel des deux handlers. Le relais intermédiaire (`_free_non429_cooldown_strict`) recevait le paramètre sans le transmettre — première tentative de correction **encore fausse en live**, ce que seule la re-mesure a montré.

**Second défaut, trouvé en regardant.** L'amont disait `{"type":"server_error"}` ; le proxy émettait `api_error`. V5.2 demande le type « tel quel ». Nuance qui compte : `server_error` est un type **OpenAI**, absent du vocabulaire Anthropic. Le relayer aveuglément dans un flux Anthropic, c'est envoyer un nom que le client ne sait pas interpréter. `extract_upstream_error_type(body, vocab)` ne relaie donc que les types du vocabulaire **du client**.

| Cas | Avant | Après |
|---|---|---|
| Anthropic, amont chat | `data: {"error"}` + `[DONE]` | `event: error` + enveloppe Anthropic |
| OpenAI, amont chat | `type: api_error` | `type: server_error` (tel quel) |
| Type inconnu / garbage | — | ignoré, `api_error` (jamais de fuite) |
| Tests | — | 14 (forme, fidélité, compat, sites d'appel) |

Vérifié en direct sur les deux routes ; gate CI 480 tests / 27 s.

### 2026-10-23 — V0.5 : deux vocabulaires qui ne se parlaient pas

**Le constat, avant le code.** Nos erreurs étaient classées par **texte** (une liste de motifs), le client officiel par **statut** (`executor.ts:225-275`). Rien ne garantissait l'accord, et l'écart n'était visible nulle part.

Ce que la comparaison a montré, cas par cas :

| Amont | Nous (avant) | Client officiel |
|---|---|---|
| 429 sans motif | `api_error` | rate limit → `rate_limit_error` |
| 401 sans message | `api_error` | authentication → `authentication_error` |
| 403 | `api_error` | authentication (permissions) |
| 400 « rate limit … try again » | `overloaded_error` | invalid request |

Les trois premiers laissaient le client **réessayer en boucle** une erreur non rejouable, ou attendre au lieu de backoff. Le quatrième confondait un débit avec une surcharge.

**Ce qui a été livré.** La table officielle est recopiée, pas résumée : `classify_status_class(status, body)` suit l'ordre exact du client, `is_context_overflow` reprend ses 27 motifs **et ses 3 exclusions** (l'exclusion compte autant que les motifs : « rate limit » n'est pas un prompt trop long). Nos motifs gardent la priorité — ils sont plus spécifiques qu'un statut —, la table ne sert que de repli.

**Un piège évité.** Le repli par statut aurait pu transformer un 503 ordinaire en surcharge et casser le contrat historique « 503 sans motif = `api_error` » que les tests épinglent. Vérifié, conservé.

**Divergence documentée, pas cachée.** Le client officiel n'a pas de cas 402 ; nous en faisons un défaut de facturation (`permission_error` + pause de clé) depuis la parité v2. La ligne du fixture porte `divergence: true` et sa justification — un test refuse une divergence sans note.

Le fixture est un contrat : 29 cas, chacun vérifiant la **classe** et le **type émis**. Une ligne ajoutée sans code échoue ; un changement de code qui casse une ligne échoue aussi.

### Prochains points, dans l'ordre

1. ~~V4.3/V4.4~~ — **faits**.
2. ~~V3.1/V3.2/V3.2b~~ — **déjà alignés (mesuré)**, rien à faire.
3. ~~V3.3~~ — **fait** (défaut deepseek sur la jambe free, vérifié en live).
4. ~~V2.2b/V2.2c~~ — **faits** : le routage lit le catalogue, par jambe, sans franchir la frontière.
5. ~~V6.4~~ — **fait, avec une conclusion négative** : 1 modèle exposé sur 81 testés. La réserve de modèles free n'existe pas.
6. ~~V2.3~~ — **fait** : les 5 bases viennent de la config, et un garde-fou interdit le retour des littéraux.
7. ~~Instabilité `test_free_429_*`~~ — **réparée à la racine** : le fixture écrivait un dict que le lecteur n'interroge pas. Deux runs consécutifs de la suite complète à 0 échec.
8. ~~V5.1~~ — **clos** par un point d'étranglement unique (`streaming/empty_guard.py`), prouvé en exécution par un harnais ASGI. Une vraie erreur amont reste relayée telle quelle.
9. **V6.3.1** — classification des échecs free : **faite et mesurable** (`upstream/free_failures.py`, exposée par `/health?detail=full`). La **politique de reprise** qui en découlera est volontairement reportée : elle se décidera sur les chiffres, pas sur une intuition.
10. **V1.1/V1.2** — dès qu'une clé arrive : valider la jambe payante, puis finir V4.2 côté payant.

> **Note d'exploitation :** l'instance `:4000` doit être redémarrée pour charger **V3.3** (défaut deepseek), **V2.2b** (routage par catalogue, 1 modèle corrigé), **V6.4** (`big-pickle`) et **V2.3** (bases Responses en config). Commande : tuer le `pythonw` puis `pythonw opencode.py --gui`. Le correctif V4 (identité + reprises) est déjà chargé depuis le redémarrage précédent.

> **État du lot, en une phrase :** l'identité et les reprises sont alignées sur 1.19.3 et vérifiées en live ; le corps est aligné sauf deux points ; le routage lit désormais le catalogue par jambe ; **cinq prémisses de l'audit initial ont été démenties par la mesure**, ce qui est le résultat le plus utile de la session — chacune aurait coûté une « correction » qui cassait quelque chose qui marchait.
