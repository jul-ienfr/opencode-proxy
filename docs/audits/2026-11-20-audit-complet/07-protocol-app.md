# Audit 07 — Couche protocole / conversion / compaction / streaming (`app/`, `streaming/`, `upstream/`, `server/`, `core/`, `protocol/`)

- **Date** : 2026-11-20
- **Objet** : 49 fichiers, 19 086 lignes — `app/protocol/` (8 818 l.), `app/compaction/` (3 150 l.), `streaming/` (2 136 l.), `upstream/` (1 381 l.), `server/` (1 061 l.), `core/` (1 754 l.), `protocol/tokens.py` (283 l.), `protocol_mapping.py` (171 l.), `app/router|db|quotas` (266 l.)
- **Mode** : **lecture seule**, aucun fichier de code modifié. Un seul livrable écrit : ce rapport.
- **Méthode** : `Read` par tranches sur `app/protocol/mapping.py` (6 329 l.), greps ciblés dépôt entier hors `Lib/`, `_research/`, `.audit-wt/`, `.audit-logs-wt/`, `node_modules/`, `logs/`, vérification par exécution Python (`python -I`) sur chaque point litigieux, corrélation `git diff main`.
- **Correction vérifiée** : le correctif du 2026-10-26 (`anthropic_usage_from_openai`) est **complet sur le chemin Chat** et a été propagé aux chemins SSE (`chat_sse_to_anthropic.py:710`), `openai_to_anthropic_response` (`mapping.py:2844-2853`) et `_responses_to_anthropic_response` (`mapping.py:3346-3352`). **Quatre chemins refont encore l'erreur ou en perdent l'information** : P0-2 (`_extract_cache_tokens` aveugle à la forme Responses), P0-3 (même fonction, `TypeError` non rattrapé sur 11 sites), P0-4 (`openai_chat_to_responses` met l'usage Responses à zéro), P2-1 (création de cache imbriquée non soustraite).

---

## 1. Résumé exécutif

1. **P0 — Le plafond mémoire du cache de conversion n'existe pas.** `_anthropic_cache_max_bytes = 64 MB` est déclaré et **exposé au dashboard** mais jamais incrémenté ni vérifié (`mapping.py:2046` : seule autre occurrence = `mapping.py:2065`, en lecture). Le cache retient jusqu'à 512 bodies de 10 Mo = ~5 Go de RSS, exactement le risque que le commentaire dit avoir corrigé.
2. **P0 — `_extract_cache_tokens` ne lit pas la forme Responses.** Testé : `{'input_tokens':1000,'input_tokens_details':{'cached_tokens':900}}` → **0** au lieu de 900. Sur `/v1/responses` et la jambe free Responses, `message_start`/`message_delta` annoncent `cache_read_input_tokens: 0` alors que le contexte est massivement caché — le correctif anti-thrashing du 2026-10-26 est **annulé sur ces portes**.
3. **P0 — `_extract_cache_tokens` / `_extract_cache_creation_tokens` lèvent `TypeError` sur un `details` non-dict.** `{'prompt_tokens':10,'prompt_tokens_details':5}` → `TypeError: argument of type 'int' is not iterable`. **11 des 30 sites d'appel sont sans aucun `try` ancêtre** (scan AST, `opencode.py:20306, 21289, 22956, 22960, 27628, 27630, 29121, 31540, 31673, 32165, 32166`) → 500 sur une réponse amont par ailleurs valide, sur les trois portes et les chemins free/paid.
4. **P0 — `openai_chat_to_responses` perd intégralement l'usage d'un amont Responses.** Testé : `{'input_tokens':1000,'output_tokens':20,'input_tokens_details':{'cached_tokens':900}}` → `{input_tokens: 0, output_tokens: 0, cached_tokens: 0}`. Deux sites stockent le `usage` **brut** avant conversion (`opencode.py:32388-32458`, `32950-33032`) puis l'injectent dans un dict Chat — `_finalize_stream_tokens` (qui ne lit que les clés Chat, `19096-19103`) retombe alors sur l'estimation `len//3`.
5. **P1 — Asymétrie `stop_reason` : `content_filter` → `end_turn` en non-stream, `refusal` en streaming.** `mapping.py:2838-2842` gère `refusal`/`content_filter` → `content_filter` (finish OpenAI) ; `mapping.py:2334-2336` (Chat→Anthropic) n'a **que** `length` et `tool_use`, tout le reste tombe sur `end_turn`. Vérifié par exécution. Le client Anthropic ne distingue donc pas un refus de sécurité d'une fin normale sur la porte non-stream.
6. **P1 — `retry_until_success` neutralise le délestage 429 global.** Le middleware `Global429BackoffMiddleware` (index 4) est **à l'intérieur** de `RetryUntilSuccessMiddleware` (index 1, vérifié par introspection) ; `_LOCAL_RATE_LIMIT_MARKERS` (`retry_until_success.py:129-136`) ne contient aucun marqueur du body émis par le délesteur (`upstream/breaker.py:249`) → chaque 429 local est rejoué pendant les 120 s du budget.
7. **P1 — `outer["committed"]` n'est jamais écrit.** `retry_until_success.py:709` l'initialise à `False` ; greps : 5 lectures, **0 écriture**. La branche 735-807, le keepalive `_sleep_with_keepalive:565` et la branche `outer_committed` de `_finish_with_error:650` sont **inatteignables** — un client peut attendre 120 s sans recevoir le moindre octet.
8. **P1 — `empty_guard` accumule les keepalives sans borne et rescanne le buffer en O(n²).** `empty_guard.py:361` (`state["raw"] += body`) + `:238` (re-split depuis 0 à chaque chunk).
9. **Le correctif d'usage est le bon, mais il est dupliqué quatre fois** (`mapping.py:248`, `chat_sse_to_anthropic.py:710`, `opencode.py:20307`, `opencode.py:29550`) avec des variantes divergentes — c'est structurellement ce qui a permis aux trous ci-dessus d'exister.
10. **Aucun module ne dépasse 1 500 lignes** dans le périmètre (max : `retry_until_success.py` 961, `core/errors.py` 877). `app/protocol/mapping.py` (6 329 l.) est **hors périmètre de la règle** uniquement parce qu'il est antérieur — c'est le vrai monolithe protocolaire et le premier candidat au découpage.

---

## 2. Carte des modules

### 2.1 Table (rôle, LOC, appelants)

| Fichier | LOC | Rôle | Appelants réels |
|---|---:|---|---|
| `app/protocol/mapping.py` | 6 329 | **Cœur conversion** Anthropic↔Chat↔Responses, SSE Responses, cache conversion, sanitize tools | `opencode.py` (~40 symboles), `protocol_mapping.py` (shim), `app/protocol/__init__.py`, 28 tests |
| `app/protocol/chat_sse_to_anthropic.py` | 976 | SSE Chat→Anthropic (état par stream) | `opencode.py:12178`, `21852`, `29520` |
| `app/protocol/tool_aliases.py` | 1 093 | Remap des noms d'outils amont→vocabulaire client | `mapping.py` (3 symboles), `opencode.py`, `chat_sse_to_anthropic.py` |
| `app/protocol/gemini.py` | 420 | Dialecte Gemini natif ↔ Chat | `opencode.py:12023` (import local) |
| `app/router/__init__.py` | 145 | `route_for` pur + DI | `opencode.py:708`, `dashboard/quota.py:198` |
| `app/db/__init__.py` | 88 | **SHIM** vers `observability.db` | `tests/*` uniquement en direct ; l'hôte importe `observability.db` |
| `app/quotas/__init__.py` | 33 | **STUB** : `from opencode import …` sous `try/except ImportError` | **aucun** (seul `tests/test_coverage_boost.py:31` l'importe) |
| `app/streaming/__init__.py` | 11 | **SHIM** vers `streaming.sse` | **aucun** (seul `tests/test_coverage_boost.py:33`) |
| `app/compaction/` (13 fichiers) | 3 150 | Compaction serveur unifiée | `opencode.py:689-707` (12 symboles), 6 tests |
| `streaming/` (7 fichiers) | 2 136 | Middlewares SSE : deadline, empty-guard, retry, pompe, healing | `opencode.py:1354`, `5735-5778` |
| `upstream/` (7 fichiers) | 1 381 | Breaker, pools curl, classification d'échecs, quotas, retry | `opencode.py:1996-2235`, `5610-5824` |
| `server/` (5 fichiers) | 1 061 | Accesslog, cache réponse, throttle, websearch | `opencode.py:2955-5356`, `19602` |
| `core/` (5 fichiers) | 1 754 | Erreurs HTTP, clés/pause, lazy import, process | `opencode.py:220-308`, `5815-5824`, `dashboard`, `free` |
| `protocol/tokens.py` | 283 | Estimation de tokens (pur, DI) | `opencode.py:20516` |
| `protocol_mapping.py` (racine) | 171 | **SHIM** Phase 4 (re-export intégral) | 18 tests, `scripts/bench_perf.py`, `scripts/gen_golden_fixtures.py` |

### 2.2 Flux de conversion

```
Client Anthropic (Claude Code)
  │  POST /v1/messages  { model, system, messages[], tools[], max_tokens, thinking, … }
  ▼
opencode.py  handler `messages`
  ├─ _route_for(model) ──────────────► app/router.route_for  (pur, cache ROUTE_VERSION)
  ├─ detect_compaction / is_compaction_shape ──► app/compaction.router   (exclusion cache + relais intact)
  ├─ filter_anthropic_request_body ──► mapping.py:3998   (retire text:"" / thinking vides)
  ├─ stop_reason natif : strip_synthetic_thinking ──► mapping.py:2210
  ▼
  ┌─ protocole Chat ──────────────────────────────────────────────────────┐
  │  anthropic_to_openai(body, model, raw)   mapping.py:1336 (impl)       │
  │     ↑ wrapper cache  mapping.py:2102  (blake2b key, LRU 512, copie racine)
  │  system → role:system + cache_control                                  │
  │  tool_use → tool_calls[] ; tool_result → role:tool (avant le message)  │
  │  image/document → image_url / file (data-URI)                          │
  │  thinking → reasoning_content ; redacted_thinking → _redacted_thinking_cache │
  │  stream → stream_options.include_usage = true                          │
  └───────────────────────────────────────────────────────────────────────┘
  ┌─ protocole Responses ─────────────────────────────────────────────────┐
  │  _anthropic_to_responses_request  mapping.py:5170                     │
  │  _chat_to_responses_request       mapping.py:4771                     │
  └───────────────────────────────────────────────────────────────────────┘
  ┌─ protocole Gemini ────────────────────────────────────────────────────┐
  │  chat_to_gemini / gemini_endpoint  app/protocol/gemini.py             │
  └───────────────────────────────────────────────────────────────────────┘
  ▼
Upstream (opencode provider)
  │
  ▼  Réponse
  ├─ non-stream Chat   → openai_to_anthropic            mapping.py:2252
  ├─ non-stream Resp   → _responses_to_anthropic_response mapping.py:5249
  ├─ non-stream Anthro → passthrough (+adapt_tool_call)
  └─ stream
      ├─ Chat   → ChatSseToAnthropicState.feed_chunk  chat_sse_to_anthropic.py:691
      ├─ Resp   → _responses_sse_to_chat_deltas       mapping.py:5507
      │            puis ResponsesStreamEmitter        mapping.py:5930
      └─ Anthro → passthrough SSE (+_stream_cache_delta opencode.py:19213)
  ▼
  usage ─► anthropic_usage_from_openai  mapping.py:248  (input − cache, cf. §4)
  ▼
Pompe SSE  streaming/sse.py:38  (coalesce 64 KiB, ping 15 s, idle_timeout)
  ▼
Client Anthropic
```

---

## 3. Matrice de conversion

| Champ | Sens | Verdict | Preuve |
|---|---|---|---|
| `usage.input_tokens` (Chat→Anthropic) | ⇢ | ✅ | `mapping.py:233-245` `anthropic_input_tokens` soustrait cache_read + cache_creation imbriqué ; testé |
| `usage.input_tokens` (Responses→Anthropic) | ⇢ | ⚠️ | `mapping.py:279` lit `input_tokens_details` pour le **cached_tokens** ✅, mais `nested_creation` (290-294) ne cherche que dans `prompt_tokens_details` → une création imbriquée dans `input_tokens_details` **n'est pas soustraite** (testé : somme client 950 ≠ total 1000) |
| `usage.cache_read_input_tokens` | ⇢ | ❌ | `_extract_cache_tokens` (`mapping.py:215-223`) ne teste ni `input_tokens_details.cached_tokens` ni `prompt_cache_hit_tokens` → **0** sur la forme Responses (testé) ; 11 sites d'appel, dont 11 hors `try` (cf. P0-3) |
| `usage.cache_creation_input_tokens` | ⇢ | ❌ | `_extract_cache_creation_tokens` (`mapping.py:493-516`) : idem, `input_tokens_details.cache_creation_tokens` jamais lu (testé → 0) |
| `usage.cache_creation_input_tokens` (stream, DB) | ⇢ | ❌ | `_finalize_stream_tokens` (`opencode.py:19105`) calcule `final_cache_write` mais **ne le retourne pas** (19127) |
| `usage.output_tokens` | ⇢ | ✅ | `mapping.py:275` ; `_usage_int` (226) écarte bool/negatifs |
| `usage.reasoning_tokens` | ⇢ | ⚠️ | `_extract_reasoning_tokens` (`mapping.py:308`) correct et borné — mais **`opencode.py:32871` écrit encore `reasoning_tokens: 0` en dur** (chemin `/v1/responses` streamé) |
| `usage` Anthropic→Chat | ⇠ | ✅ | `mapping.py:2853` `prompt_tokens = non_cached + read + creation` (total inclusif) — correctif 2026-10-30 présent |
| `stop_reason` Chat→Anthropic (non-stream) | ⇢ | ❌ | `mapping.py:2334-2336` : seuls `length`→`max_tokens` et tool_use→`tool_use`. `content_filter` → **`end_turn`** (testé). `refusal` non géré |
| `stop_reason` Chat→Anthropic (stream) | ⇢ | ✅ | `_STOP_REASON_MAP` (`chat_sse_to_anthropic.py:59-67`) mappe `content_filter`→`refusal` ; `opencode.py:29652-29658` idem |
| `stop_reason` Anthropic→Chat | ⇠ | ✅ | `mapping.py:2833-2842` (`refusal`/`content_filter`→`content_filter`) |
| `stop_reason` Anthropic→Responses | ⇠ | ⚠️ | `mapping.py:3340-3344` : `max_tokens`/`refusal`→`incomplete`, mais `content_filter` non listé |
| `stop_reason: pause_turn` | ⇠/⇢ | ⚠️ | **jamais produit ni traduit** (grep dépôt : 0 hit hors tests) |
| `tool_use` ↔ `tool_calls` | ⇄ | ✅ | `mapping.py:1507` / `2619` ; id stable `_tool_id_or_stable` (115) ; nom vide → skip (1509) |
| `tool_result` ↔ `role:tool` | ⇄ | ✅ | `mapping.py:1530` ; multimodal préservé (1540-1634) ; `is_error` → préfixe `[tool_error]` (1619-1627) |
| Appariement tool_use/tool_result | ⇄ | ✅ | `_drop_orphan_tool_messages` (146) + flush avant tout non-tool (2513) + `emitted_tool_ids` (2490) |
| `cache_control` (client) | ⇄ | ✅ | `_system_cache_control` (1320) préserve `ttl` ; `_carry_cc` reporte sur les outils |
| Plafond 4 breakpoints | ⇢ | ✅ | `_enforce_cache_breakpoint_limit` (772) appelé en 1987/2002 |
| Images base64 / url | ⇄ | ✅ | `mapping.py:1427-1448` ; round-trip `openai_to_anthropic_request:2530` |
| `document` (pdf / url / file_id / text) | ⇄ | ✅ | `mapping.py:1449-1506` ; `title` lu en priorité sur `name` (1470, A25) |
| Vidéo / audio | ⇄ | ⚠️ | placeholder texte assumé et journalisé (2599-2604, 2472-2474) — jamais de drop silencieux, mais perte réelle |
| `thinking` → `reasoning_content` | ⇢ | ✅ | `mapping.py:1402-1408` |
| `reasoning_content` → `thinking` + signature | ⇠ | ✅ | `mapping.py:2277-2290` ; signature locale HMAC (2136) ; détection chiffré (2196) |
| `redacted_thinking` | ⇄ | ✅ | cache borné 512 (523) ; `_is_local_signature` LRU 2048 (2155) |
| Ordre thinking en tête | ⇠ | ✅ | `mapping.py:2639` `blocks.insert(0, …)` — mais **deux** blocs thinking seraient inversés (cf. 3332-3334, corrigé seulement côté Responses) |
| `system` string vs liste | ⇄ | ✅ | `mapping.py:1352-1366` ; liste aplatie via `_extract_text` |
| `system` vide | ⇄ | ✅ | `filter_anthropic_request_body:4016-4017` (`""` → clé absente) |
| Plusieurs `system` (OpenAI) | ⇠ | ✅ | `openai_to_anthropic_request:2362-2378` préserve l'ordre + `developer` |
| Contenu vide | ⇄ | ✅ | `filter_empty_anthropic_messages:3649` ; garde « jamais un corps sans messages » (4020) |
| `null` vs absent | ⇄ | ⚠️ | `_usage_int` (226) traite `None` ✅ ; `msg.get("content")` `None` → `""` (2291) ✅ ; mais `content: null` explicite côté Anthropic n'est pas testé |
| `max_tokens` vs `max_completion_tokens` | ⇄ | ✅ | `_set_output_token_limit:411` lit les 3 formes, écrit selon la destination, borne par le catalogue (481) ; garde bool (467) |
| `stream_options.include_usage` | ⇢ | ✅ | `mapping.py:1749-1758` — posé si `stream:true` |
| `tool_choice` (4 formes) | ⇄ | ✅ | `mapping.py:2754-2776` + `disable_parallel_tool_use` (2779) |
| `tool_choice` Responses | ⇄ | ✅ | `_normalize_responses_tool_choice:4716` |
| Noms d'outils > 64 | ⇄ | ✅ | `sanitize_tool_names:3504` + `restore_tool_name` sur **tous** les retours (2323, 3326, 3440, 5208, 5275) |
| `is_error` | ⇄ | ✅ | `mapping.py:1619-1627` |
| Erreur amont milieu de flux | ⇄ | ✅ | `chat_sse_to_anthropic.py:659` émet `event: error` **puis** la clôture complète (idempotent) |
| `message_stop` toujours émis | ⇄ | ✅ | `chat_sse_to_anthropic.py:620` ; `opencode.py:20422`, `19267` |
| Déconnexion client | ⇄ | ⚠️ | `streaming/retry_until_success.py:708` (`_wait_disconnect`) ✅ ; la pompe `sse.py:198-206` annule les tâches ✅ — mais l'annulation de l'upstream n'est pas propagée (cf. `deadline.py:20-21`, assumé) |
| Ordre des événements | ⇄ | ✅ | `message_start` → blocs → `message_delta` → `message_stop` (vérifié 3 émetteurs) |
| `event: ping` | ⇄ | ✅ | `sse.py:197` (`event: ping` + data, pas un simple commentaire) |

---

## 4. Findings

### P0

#### P0-1 — Le plafond mémoire du cache de conversion n'est jamais appliqué
- **Fichier** : `app/protocol/mapping.py:2044-2047`, `2065`
- **Preuve** : `grep -n "_anthropic_cache_bytes" app/protocol/mapping.py` → `2046: _anthropic_cache_bytes = 0` et `2065: "bytes": _anthropic_cache_bytes` (lecture). **Aucun `+=`**, aucune comparaison à `_anthropic_cache_max_bytes` dans la boucle d'éviction (`2129-2130`, qui ne teste que `len()`).
- **Impact** : 512 entrées × 10 Mo (images/PDF base64) = **jusqu'à ~5 Go de RSS**. Le dashboard affiche `bytes: 0` et `max_bytes: 67108864` en permanence — la métrique ment. Le commentaire `2044-2045` décrit exactement ce risque comme « corrigé ».
- **Recommandation** : accumuler la taille réelle (`len(_json_dumps(res))`) à l'insertion, décrémenter au `popitem`, et boucler sur `_anthropic_cache_bytes > _anthropic_cache_max_bytes` en plus du compte d'entrées.
- **Effort** : S

#### P0-2 — `_extract_cache_tokens` ne lit pas la forme Responses → cache perdu côté client
- **Fichier** : `app/protocol/mapping.py:215-223` (fonction), 11 sites d'appel dans `opencode.py` : `12641`, `12647`, `19104`, `22484`, `22956`, `27189`, `27597`, `27628`, `31792`, `32165`, `32856`
- **Preuve** (exécution) :
  ```
  _extract_cache_tokens({'prompt_tokens':1000,'prompt_tokens_details':{'cached_tokens':900}})      -> 900
  _extract_cache_tokens({'input_tokens':1000,'input_tokens_details':{'cached_tokens':900}})        -> 0   ← BUG
  ```
  La fonction ne teste que `prompt_tokens_details.cached_tokens`, `usage.cached_tokens`, `usage.cache_read_input_tokens`. Or `anthropic_usage_from_openai` (`mapping.py:279`) lit **les deux** conteneurs — la divergence est prouvée par le code lui-même.
- **Impact** : sur `/v1/responses` (et la jambe free Responses), `_finalize_stream_tokens` reçoit `final_cache = 0` → `client_in = anthropic_input_tokens(final_in, 0, …)` = **total complet**. Le client Anthropic reçoit `input_tokens` = contexte total **et** `cache_read_input_tokens: 0`. Le correctif anti-thrashing du 2026-10-26 est annulé sur ces portes ; en plus le dashboard sous-compte le cache.
- **Recommandation** : réécrire `_extract_cache_tokens` en déléguant à `anthropic_usage_from_openai(usage)["cache_read_input_tokens"]` (source unique, principe III) — ou au minimum ajouter la boucle `for key in ("prompt_tokens_details","input_tokens_details")` déjà présente dans `chat_sse_to_anthropic.py:304`.
- **Effort** : S

#### P0-3 — `_extract_cache_tokens` / `_extract_cache_creation_tokens` lèvent `TypeError` sur `details` non-dict, sur **11 sites sans aucun `try` ancêtre**
- **Fichier** : `app/protocol/mapping.py:216` (`details = usage.get("prompt_tokens_details") or {}` puis `if "cached_tokens" in details`), `507` (idem)
- **Preuve** (exécution) :
  ```
  _extract_cache_tokens({'prompt_tokens':10,'prompt_tokens_details':5})   -> TypeError: argument of type 'int' is not iterable
  _extract_cache_tokens({'prompt_tokens':10,'prompt_tokens_details':'x'}) -> TypeError: string indices must be integers
  ```
- **Sites d'appel** (scan AST complet de `opencode.py`, classant chaque `Call` par son `Try` ancêtre et sa position *dans le corps* vs *dans un handler*) :
  ```
   20306 _extract_cache_creation_tokens   *** UNPROTECTED ***
   21289 _extract_cache_creation_tokens   *** UNPROTECTED ***
   22956 _extract_cache_tokens            *** UNPROTECTED ***
   22960 _extract_cache_creation_tokens   *** UNPROTECTED ***
   27628 _extract_cache_tokens            *** UNPROTECTED ***
   27630 _extract_cache_creation_tokens   *** UNPROTECTED ***
   29121 _extract_cache_creation_tokens   *** UNPROTECTED ***
   31540 _extract_cache_creation_tokens   *** UNPROTECTED ***
   31673 _extract_cache_creation_tokens   *** UNPROTECTED ***
   32165 _extract_cache_tokens            *** UNPROTECTED ***
   32166 _extract_cache_creation_tokens   *** UNPROTECTED ***
  ```
  Les 19 autres sites sont dans le corps d'un `try`. Vérification du contexte des 11 non protégés : `NO try ancestor` pour chacun (aucun n'est dans un handler `except`, donc aucun n'est « rattrapé en aval »).
- **Impact** : un upstream renvoyant `prompt_tokens_details: 0` / `"x"` / `null` (ou `input_tokens_details` idem) fait lever à l'intérieur des trois handlers principaux. Ces lignes étant hors de tout `try`, l'exception remonte au `@app.exception_handler(Exception)` (`opencode.py:4962`) → **500 « Internal server error »** au client, sur une réponse par ailleurs valide. Les 11 sites couvrent les trois portes (`/v1/messages`, `/v1/chat/completions`, `/v1/responses`) et les chemins free, paid et passthrough.
- **Recommandation** : `details = usage.get("prompt_tokens_details"); if not isinstance(details, dict): details = {}` — le motif correct existe déjà dans le **même fichier** à `mapping.py:277-280`. Idem `_extract_cache_creation_tokens:507`. Ajouter un test paramétré sur `details ∈ {0, "x", None, [], True}` pour les deux fonctions.
- **Effort** : S

#### P0-4 — Usage d'un amont Responses ignoré : `openai_chat_to_responses` le met à zéro, `_finalize_stream_tokens` retombe sur l'estimation
- **Fichier** : `opencode.py:19096` (`final_in = actual_usage.get("prompt_tokens") or final_in`), `19097-19103` (`completion_tokens`/`total_tokens`), `19104-19105` (cache)
- **Preuve** : la fonction ne lit que les clés Chat (`prompt_tokens`, `completion_tokens`, `total_tokens`) et délègue le cache à `_extract_cache_tokens` (lui-même aveugle à la forme Responses, cf. P0-2). Sur un `actual_usage` de forme Responses (`{input_tokens, output_tokens, input_tokens_details}`), `prompt_tokens` est absent → `final_in` **retombe sur l'estimation locale** `est_input`, `final_out` sur `stream_out`, `final_cache` sur `0`.
- **Nuance importante (vérifiée)** : sur le chemin streamé principal (`opencode.py:28241`), `actual_usage` est bien **Chat-shaped** — il provient du chunk déjà converti par `_responses_sse_to_chat_deltas`, qui fabrique `prompt_tokens`/`completion_tokens` (`mapping.py:5863-5869`). Le défaut ne mord donc **pas** là. Il mord sur les sites qui stockent le `usage` **brut** avant conversion : `opencode.py:32388-32390` (`final_usage`, utilisé à `32458`) et `32950-32952` (utilisé à `33032`).
- **Impact** : sur ces chemins, le tour est comptabilisé avec une estimation `len//3` au lieu de l'usage réel de l'amont — compteur de coût faux, et le seuil de compaction (`keep_tokens`) raisonne sur une valeur inventée. C'est la même famille que P0-2/P2-3.
- **Recommandation** : normaliser `actual_usage` par `anthropic_usage_from_openai` (qui lit **les deux** formes) avant tout calcul, plutôt que de lire des clés Chat en direct. Le correctif de P0-2 (source unique) résout aussi ce point.
- **Effort** : S

### P1

#### P1-1 — `content_filter` → `end_turn` sur le chemin non-stream Chat→Anthropic
- **Fichier** : `app/protocol/mapping.py:2334-2336`
- **Preuve** (exécution) :
  ```
  openai_to_anthropic({... 'finish_reason':'content_filter'}) -> stop_reason = 'end_turn'
  anthropic_stop_reason('content_filter')                     -> 'refusal'
  ```
  Deux tables divergentes pour la même entrée selon la porte (stream vs non-stream).
- **Impact** : un refus de sécurité est indistinguable d'une fin normale pour un client Anthropic non-streamé. Claude Code ne déclenche ni alerte ni politique de retry.
- **Recommandation** : extraire `_STOP_REASON_MAP` de `chat_sse_to_anthropic.py:59` vers `mapping.py` et l'utiliser des deux côtés (principe III : une source de vérité). Traiter aussi `finish_reason: "refusal"` (forme OpenAI moderne).
- **Effort** : S

#### P1-2 — Le délestage 429 global est défait par la boucle de retry
- **Fichier** : `streaming/retry_until_success.py:129-136` (`_LOCAL_RATE_LIMIT_MARKERS`), `:811` (garde), `upstream/breaker.py:249-262` (body émis)
- **Preuve** : ordre des middlewares vérifié par introspection (`oc.app.user_middleware`) : `0 RequestDeadline → 1 RetryUntilSuccess → … → 4 Global429Backoff → …`. Le délesteur émet `{"error":{"message":"upstream 429 storm — global backoff active","type":"rate_limit_error"}}` ; `_LOCAL_RATE_LIMIT_MARKERS` = `{"rate limit exceeded. please retry shortly.", '"code":"rate_limit_exceeded"', '"code": "rate_limit_exceeded"', "free quota exhausted on all vpn stations"}` → **aucun ne matche** → `_looks_local_rate_limit` = False → `is_retryable_status(429)` = True → rejeu.
- **Impact** : l'anti-thundering-herd ne peut pas délester ; chaque client attend le budget entier (`max_wait_s: 120`) au lieu d'un 429 immédiat avec `Retry-After`. Aggrave la tempête qu'il devait amortir.
- **Recommandation** : ajouter `"global backoff active"` (ou `"upstream 429 storm"`) à `_LOCAL_RATE_LIMIT_MARKERS` — le mécanisme de sortie existe déjà, il manque le marqueur.
- **Effort** : S

#### P1-3 — `outer["committed"]` jamais écrit : 3 branches mortes, keepalive jamais envoyé
- **Fichier** : `streaming/retry_until_success.py:709` (init), `735` (lecture), `565` (keepalive), `650` (branche), `927`, `939`, `946` (lectures)
- **Preuve** : `grep -n 'outer\["committed"\]' streaming/retry_until_success.py` → 1 init + 5 lectures, **0 écriture**. `_commit` (581-587) et `_commit_buffered` (589-594) ne le positionnent pas.
- **Impact** : (a) la branche 735-807 (gestion « en-têtes déjà partis ») est inatteignable ; (b) `_sleep_with_keepalive` ne peut jamais envoyer de keepalive (`committed and is_stream` toujours faux) → un client attendant un retry pendant 120 s **ne reçoit aucun octet** et peut conclure à un gel ; (c) la branche `outer_committed` de `_finish_with_error` (650) est morte.
- **Recommandation** : poser `outer["committed"] = True` dans `_commit`/`_commit_buffered`, ou supprimer les 3 branches mortes si le comportement voulu est bien « jamais de keepalive ». Le commentaire de la branche 735-737 décrit une intention qui n'est pas implémentée.
- **Effort** : S

#### P1-4 — `empty_guard` : buffer de keepalives non borné + rescan O(n²)
- **Fichier** : `streaming/empty_guard.py:361` (`state["raw"] += body`), `:238` (`_split_complete_sse_events(raw)`)
- **Preuve** : tant que `_first_meaningful_event_kind` rend `None` (que des keepalives/commentaires), chaque chunk est concaténé et le **buffer entier** est re-splitté depuis l'offset 0 à chaque nouveau chunk.
- **Impact** : un amont qui émet des pings en continu (précisément le cas de gel que ce garde existe pour traiter) fait croître `state["raw"]` sans borne et consomme un CPU quadratique. Violation XVII (« rien de lourd dans le chemin de requête », mémoire bornée).
- **Recommandation** : ne retenir qu'un préfixe borné (ex. 8 KiB) une fois qu'un keepalive est vu, et conserver un curseur de split.
- **Effort** : M

#### P1-5 — `retry_until_success` bufferise tout le corps même quand la garde de taille ne peut pas se déclencher
- **Fichier** : `streaming/retry_until_success.py:293-300` (`_content_length`), `:692` (garde), `:520-533` + `:704` (`_read_body` + `b"".join`)
- **Preuve** : `_content_length(scope)` retourne `0` quand l'en-tête est absent → `if length > policy.buffer_max_bytes` (10 MiB) ne se déclenche jamais pour un corps chunked ; `_read_body` accumule alors **tous** les chunks.
- **Impact** : un corps chunked de taille arbitraire est intégralement bufferisé par requête, sur le chemin de requête — le plafond de 10 MiB est contournable trivialement.
- **Recommandation** : appliquer le plafond **pendant** la lecture (couper et passer à `self.app` en streaming) plutôt que sur l'en-tête.
- **Effort** : S

#### P1-6 — Tâches fire-and-forget sans référence forte ; `_drain` peut boucler indéfiniment
- **Fichier** : `upstream/clients.py:213`, `:291`, `:305` (`create_task` sans référence), `:296-302` (`while True: … if p.busy_count == 0: break; await asyncio.sleep(0.5)`)
- **Preuve** : les résultats de `create_task` sont jetés ; `_drain` ne se termine que quand `busy_count` atteint 0. `discard()` (`clients.py:183-188`) existe précisément pour les slots dont le `busy` a fuité sur un chemin d'annulation.
- **Impact** : fuite de sessions curl / descripteurs + une tâche à 2 Hz par rotation, à vie.
- **Recommandation** : garder les tâches dans un set avec `add_done_callback(discard)` ; borner `_drain` par un délai.
- **Effort** : M

#### P1-7 — `strip_web_tool` lève `AttributeError` → 500 sur un `tool_choice` non conforme
- **Fichier** : `server/websearch.py:460-464`
- **Preuve** : `tc_name` est passé tel quel à `normalize_fn` (l'hôte fait `raw.strip().lower()`, `opencode.py:19533`) ; `tc.get("function", {})` lève si `function` est une string. Repro réel : `tool_choice: {"type":"tool","name":123}` → `AttributeError: 'int' object has no attribute 'strip'`. Site d'appel `opencode.py:20715`/`27109`/`31224` **hors de tout `try`**.
- **Impact** : 500 générique au lieu du retrait de l'outil, sur les trois portes.
- **Recommandation** : `tc_name = str(tc_name or "")` et garde `isinstance(tc.get("function"), dict)` dans le module pur.
- **Effort** : S

#### P1-8 — `KeyPauser._save()` écrit le `.tmp` partagé hors du verrou
- **Fichier** : `core/keys.py:144-158`
- **Preuve** : `self._write_lock` n'entoure que `os.makedirs` (145-146) ; `open(tmp,"w")`/`fsync`/`os.replace` sont **dehors**, et `_write_yaml` tourne dans l'executor par défaut (`:162`) donc concurremment. Repro 8 threads : 7 échecs `PermissionError … paused.yaml.tmp -> paused.yaml`.
- **Impact** : sur POSIX, un écrivain tronque le tmp pendant qu'un autre le publie → YAML tronqué, et `load()` (`:178`) perd **tous** les pauses. Après restart, des clés mortes sont réutilisées.
- **Recommandation** : tenir `_write_lock` sur toute la séquence tmp+fsync+replace, ou un nom de tmp unique par appel.
- **Effort** : S

#### P1-9 — Sélection round-robin de clé bloquée / 500 sur une clé non-string
- **Fichier** : `core/keys.py:583-586`
- **Preuve** : `cycle_keys = [str(k) for k in current_ids]` puis comparaison `cycle_keys != current_ids` (str vs brut) ; `cycle_index % len(available)` appliqué à `available`. Repro : `api_keys=[{"alias":"A"},{"api_key":"b"},{"api_key":"c"}]` → `['A','A','A','A']`. Avec `{"api_key":123}` → `AttributeError` à `core/keys.py:101` (`_prefix`), soit 500 sur chaque requête.
- **Impact** : martèlement silencieux d'une seule clé (tempête 429) ou panne totale, sur un `api_keys.yaml` édité à la main. `load_api_keys` (`config/settings.py:2356-2361`) ne type-check pas, contrairement à `save_api_keys` (2382-2386).
- **Recommandation** : normaliser `api_key = str(k.get("api_key") or "")` en tête de `select_next_key`/`_prefix` et comparer sur la même liste normalisée.
- **Effort** : S

#### P1-10 — `core/errors.py` est un adaptateur HTTP dans le domaine (règle XIV)
- **Fichier** : `core/errors.py:20` (`from fastapi.responses import JSONResponse`), `:111-266` (construction et mutation d'en-têtes)
- **Preuve** : `anthropic_error`/`openai_error`/`model_deprecated_error`/`free_refusal_response` construisent des `JSONResponse` et écrivent `resp.headers["Retry-After"]`. Constitution XIV : « le domaine est pur, les dépendances passent par des ports ». Coût mesuré : `import fastapi.responses` = 374 ms contre `import yaml` = 52 ms.
- **Impact** : le module domaine tire FastAPI dans tout importateur (dont `free/`, `upstream/`) ; la règle XIV est violée.
- **Recommandation** : retourner `(status, dict, headers)` et laisser l'hôte construire la `JSONResponse`.
- **Effort** : M

#### P1-11 — `core/__init__.py` annule le bénéfice du chargement paresseux
- **Fichier** : `core/__init__.py:26` (`from core import errors, keys, lazy`)
- **Preuve** : mesuré `python -c "import core.lazy"` = **439 ms** (dont `core` cumulé 364 ms) alors que `core/lazy.py:1-19` justifie `LazyModule` par une économie de ~81 ms. `opencode.py:115`, `dashboard/quota.py:21`, `upstream/clients.py:44` importent `core.lazy` et héritent du coût.
- **Impact** : le boot paie FastAPI au moment où le lazy devait l'éviter.
- **Recommandation** : supprimer les ré-exports du `__init__` ou passer par `__getattr__` de module.
- **Effort** : S

#### P1-12 — `/readyz` et `/metrics` sont rate-limités (429 au scrapeur)
- **Fichier** : `server/throttle.py:97` (`_SKIP_PREFIXES = ("/api/", "/static/", "/health")`)
- **Preuve** : vérifié sur le middleware réel — `/healthz`→200, `/readyz`→200, **`/startupz`→429**, **`/metrics`→429**. Le body du 429 est au format OpenAI même pour ces chemins (`:144` ne matche ni `"/messages"` ni `"/anthropic"`).
- **Impact** : Prometheus et un orchestrateur partageant l'IP d'un client bavard reçoivent 429 sur les endpoints faits pour être pollés. `/readyz` est l'endpoint de verdict opérateur documenté (`opencode.py:4501`).
- **Effort** : S

#### P1-13 — Le 413 du middleware n'a pas l'enveloppe du protocole
- **Fichier** : `server/throttle.py:211-218`
- **Preuve** : `{"error": "payload_too_large", "message": …}` alors que le limiteur du **même fichier** argumente explicitement (`:142-165`) qu'une forme nue rend l'erreur illisible par le SDK Anthropic. Les 413 des handlers utilisent la bonne enveloppe (`opencode.py:20579-20583`).
- **Impact** : un `POST /v1/messages` trop gros reçoit un cadre d'erreur non-Anthropic avant que FastAPI ne tourne.
- **Effort** : S

### P2

| # | Titre | Fichier:ligne | Preuve / impact | Effort |
|---|---|---|---|---|
| P2-1 | Création de cache imbriquée dans `input_tokens_details` non soustraite | `mapping.py:290-294` | Testé : usage Responses avec `cache_creation_tokens:50` imbriqué → `input_tokens=50` mais `cache_creation_input_tokens=0` → somme client 950 ≠ total réel 1000 (sous-comptage, pas sur-comptage : dégradé mais non thrashing) | S |
| P2-2 | `reasoning_tokens: 0` encore codé en dur | `opencode.py:32871` | Le correctif A18 (`mapping.py:3365`, `3467`) n'a pas atteint ce site `/v1/responses` streamé | S |
| P2-3 | `openai_chat_to_responses` ne lit pas la forme Responses | `mapping.py:3454-3455` | Testé : usage `input_tokens`/`output_tokens` → `input_tokens: 0, output_tokens: 0` (tout l'usage perdu). Appelé à `opencode.py:32458` avec `final_usage` — qui peut être de forme Responses (32388-32390). **Même racine que P0-4** ; traité par le même correctif (source unique `anthropic_usage_from_openai`) | S |
| P2-4 | `_normalize_tool_schema` : deepcopy complet, non mémoïsé, par requête | `mapping.py:1110` (+ `1127`, `1131`, `1156`, `1227`) | Mesuré : 30 outils × 20 itérations = **5,48 ms/requête** (schémas réalistes). Aucun `lru_cache` dans le fichier (`grep '@lru_cache\|@cache'` → 0). Violation XVII | M |
| P2-5 | Aucun `cache_control` sur les blocs `tool_result` côté client Anthropic ? Non — mais `_cache_control_to_openai_breakpoint` est un no-op assumé | `mapping.py:868-889` | Documenté L11/B3 ; le commentaire dit « la traduction par part est rattachée à L15 » — dette non tracée | S |
| P2-6 | Le `message_start` annonce une estimation, jamais corrigée si l'amont ne fournit pas d'usage | `chat_sse_to_anthropic.py:481-483` | `client_input_tokens()` rend l'estimation `len//3`-like ; sans `usage` amont, le client garde une valeur fausse à vie. Le garde `stream_options.include_usage` (mapping.py:1749) réduit le risque mais ne l'élimine pas (upstream qui ignore l'option) | S |
| P2-7 | `_responses_to_chat_response` : `finish_reason` déduit de `status != completed` → `length` | `mapping.py:5234-5238` | Un `status: "failed"` produit `finish_reason: "length"` — mensonge sur la cause | S |
| P2-8 | `_looks_encrypted_reasoning` : heuristique base64 | `mapping.py:2196-2207` | Un raisonnement légitime sans espace et multiple de 4 de ≥64 chars serait classé `redacted_thinking` (perte du texte) | S |
| P2-9 | `server/cache.py` re-parse chaque corps avec `json.loads` | `server/cache.py:33-46` | Mesuré 50 puts de 200 Ko = 15,1 ms. Ne détecte qu'un `"error"` **de premier niveau** → un 200 non-JSON (`<html>502…`) est mis en cache et resservi | S |
| P2-10 | `ResponseCache.get()` rend le dict d'en-têtes par référence | `server/cache.py:120` | Vérifié : muter le retour mute l'entrée. Latent (les appelants copient) | S |
| P2-11 | Construction synchrone d'une session curl sous le verrou asyncio | `upstream/clients.py:125`, `:135` | `CurlSessionSlot(factory())` sous `async with self._cond` → bloque l'event loop entier sur un checkout manqué | M |
| P2-12 | `max_wait_s: 0` désactive à la fois le retry et le deadline | `retry_until_success.py:713` + `opencode.py:5793-5796` | Une ligne de config suffit à créer une boucle sans borne ; le seul filet est le plafond de délai | S |
| P2-13 | Chaque rejeu re-consomme le bucket de rate-limit du client | `retry_until_success.py:875-881` | `RateLimitMiddleware` (index 11) est dans la boucle → un client déjà en retry s'auto-étrangle | S |
| P2-14 | `Retry-After` perdu sur le chemin Chat exhausté | `retry_until_success.py:630-649` | `_sse_headers()` n'copie pas `last.headers` (le chemin Anthropic le fait, `:660`) | S |
| P2-15 | `_looks_encrypted_reasoning` / `apply_anthropic_thinking_display` non testés directement | `mapping.py:3773` | `grep -rln apply_anthropic_thinking_display tests/` → **0** ; appelé seulement en `mapping.py:727` | S |
| P2-16 | `_MODEL_INPUT_MODALITIES` est une table codée en dur, doublon du catalogue | `mapping.py:3688-3709` | `_register_model_modalities()` (0 ref externe) construit une table que `config.settings.get_model_capabilities` rend déjà — source de vérité double (principe III) | S |
| P2-17 | `_extract_cache_tokens` : `usage.get("cached_tokens")` (forme non standard) jamais atteint si `prompt_tokens_details` existe | `mapping.py:216-220` | Cascade d'`if` sans fallback : un usage avec **les deux** clés prend la première | S |
| P2-18 | `cache_creation_input_tokens` top-level compté **sans** être soustrait | `mapping.py:295-303` | Décision documentée (ligne 295-297) : un upstream Anthropic-compat l'expose hors total. Mais `anthropic_usage_from_openai` traite alors la même clé différemment selon qu'elle est imbriquée ou non — asymétrie réelle si un upstream l'expose **dans** le total | M |
| P2-19 | `app/quotas/__init__.py` : stub `from opencode import …` | `app/quotas/__init__.py:12-26` | Aucun consommateur réel (`grep -rn "app.quotas"` → 0 hors tests). Import circulaire latent si un jour appelé | S |
| P2-20 | `protocol/tokens.py` : pas de `__init__.py` dans `protocol/` | `protocol/` (contenu : `tokens.py`, `__pycache__/`) | Paquet sans `__init__.py` → namespace package implicite. Fonctionne mais fragile ; `app/streaming/__init__.py` shim existe alors que `protocol/` n'a pas d'équivalent | S |

### P3

| # | Titre | Fichier:ligne | Note |
|---|---|---|---|
| P3-1 | Champs `_Attempt` en écriture seule | `retry_until_success.py:446-447` (`content`, `local_rate_limit`) | Écrits en 587/594/804, jamais lus |
| P3-2 | `_send_start` non idempotent | `retry_until_success.py:571-579` | Repose sur la discipline des appelants ; un second appel produirait un double `http.response.start` |
| P3-3 | Plafond de corps d'erreur codé en dur 65536 | `retry_until_success.py:731,741,762,789,815,829` | Contredit `policy.buffer_max_bytes` (10 MiB) |
| P3-4 | `_clone_scope` ne réinitialise pas les contextvars | `retry_until_success.py:451-458` | `CLIENT_TOOL_NAMES`/`CLIENT_TOOL_PATH_KEYS` (`tool_paths.py:29,34`) persistent entre tentatives — bénin aujourd'hui (même corps), docstring fausse |
| P3-5 | Constantes dupliquées `PATH_TOOL_NAMES`/`PATH_ARG_ALIASES` | `streaming/tool_paths.py:15-18` vs `opencode.py:9065-9068` | Duplication verbatim |
| P3-6 | Import de fonction dans la boucle SSE + regex reconstruites | `streaming/tool_paths.py:126`, `:134,139` | `from free.station_identity import translate_paths` par ligne `data:` ; `re.search`/`re.sub` avec motif construit par appel |
| P3-7 | `tool_path_healing` réécrit CRLF en LF et copie chaque chunk | `streaming/tool_path_healing.py:69-77` | Sans effet sur SSE, mais incohérent |
| P3-8 | `empty_guard` n'attribue jamais le modèle | `opencode.py:5739` (passe `"?"`) | Les incidents « 200 → 5xx » ne sont pas ventilables par modèle |
| P3-9 | Doc/code divergents | `upstream/breaker.py:222` (« 503 » vs `status_code=429`) ; `upstream/__init__.py:12` (« AUCUN import du projet » alors que `clients.py:44` importe `core.lazy`) ; `upstream/breaker.py:175-180` (le compteur est alimenté par les pauses de quota payantes, pas les 429 amont) | |
| P3-10 | `assert` sur le chemin de requête | `server/websearch.py:294` | Disparaît sous `-O` |
| P3-11 | `re as _re` réimporté par appel | `core/errors.py:397` | Lookup inutile à chaque classification |
| P3-12 | `Retry-After` non fini/négatif accepté | `core/errors.py:213-222` | Vérifié : `inf`, `1e400`, `-5` passent (docstring dit le contraire) |
| P3-13 | `PARITY_DISABLED = False` : drapeau mort | `mapping.py:3627` (+ 714, 1974, 2661, 4006) | `grep` dépôt : jamais réassigné, aucun test ne le pose. Soit c'est un seam voulu (à documenter), soit du code mort |
| P3-14 | `_has_server_side_history` désactive le filtre orphelin sans validation | `mapping.py:134-144` | Si `body` porte `previous_response_id`, **aucun** filtrage → un historique déséquilibré part tel quel et provoquera un 400 amont |
| P3-15 | `filter_anthropic_request_body` : `except Exception: pass` sur le chemin de requête | `mapping.py:4030-4031`, `2666-2667` | Un échec du filtre vide passe silencieusement |

---

## 5. Inventaire du code mort

Filtre appliqué à chaque commande : `grep -rn … | grep -v "^./Lib/" | grep -v "_research" | grep -v "audit-wt" | grep -v "audit-logs-wt" | grep -v node_modules`.

| Symbole | Fichier:ligne | Preuve (commande → résultat) | Verdict |
|---|---|---|---|
| `_anthropic_cache_bytes` (logique) | `mapping.py:2046` | `grep -n "_anthropic_cache_bytes" app/protocol/mapping.py` → init + 1 lecture | **Code mort fonctionnel** (P0-1) |
| `_PARITY_DISABLED` | `mapping.py:3627` | `grep -rn "PARITY_DISABLED" --include=*.py .` → 6 hits, tous dans `mapping.py`, aucun dans `tests/` | Jamais réassigné |
| `apply_anthropic_thinking_display` | `mapping.py:3773` | `grep -rn "apply_anthropic_thinking_display" --include=*.py .` → 2 hits (déf + appel interne `:727`), 0 test | Non testé |
| `_register_model_modalities` | `mapping.py:3688` | `grep -rn "_register_model_modalities" --include=*.py .` → 2 hits (déf + `:3709`) | Interne, jamais appelé ailleurs |
| `plan_for_conversation` | `app/compaction/router.py:75` | `grep -rn "plan_for_conversation" --include=*.py .` → déf, `__all__`, `__init__.py` ×2, 0 appelant | **DEAD** (pur délégué à `summarizer_plan`) |
| `detect_compaction` | `app/compaction/router.py:31` | `grep -rn "detect_compaction"` → déf + `__init__` + `tests/test_compaction_unified.py:121,146` | **DEAD en prod** (test-only) |
| `should_use_tunnel` | `app/compaction/transport.py:127` | `grep -rn "should_use_tunnel"` → déf + `__all__` + `__init__` ×2 + `tests/test_compaction_unified.py:187-191` | **DEAD en prod** (test-only) |
| `clamp_ttfb_timeout` | `upstream/quotas.py:101` | `grep -rn "clamp_ttfb_timeout" --include=*.py .` → `quotas.py:101,118` + `docs/PLAN_REFONTE.md:201` | **DEAD** (l'hôte inline son propre clamp, `opencode.py:17970`) |
| `upstream.retry.is_retryable_status` | `upstream/retry.py:123` | `grep -rln "is_retryable_status"` → `streaming/retry_until_success.py` (copie propre), `tests/test_retry_parity.py`, `upstream/retry.py` | **DEAD en prod** (l'hôte utilise `RETRYABLE_STATUSES`, `opencode.py:2226`) |
| `DEFAULT_ROLE_TIMEOUT` (shim lazy) | `upstream/clients.py:435-445` | `grep -rn "DEFAULT_ROLE_TIMEOUT"` → `clients.py` seulement | **DEAD** |
| `_CurlSessionSlot`, `_CircuitBreaker` (alias) | `upstream/clients.py:83`, `upstream/breaker.py:172` | `grep -rn "_CircuitBreaker\b"` → l'hôte définit sa **propre** sous-classe (`opencode.py:5613`, base = `_CircuitBreakerBase`) | **DEAD** |
| `popen_hidden`, `which`, `IS_WINDOWS`, `CREATE_NO_WINDOW` | `core/process.py:84,109,25,29` | `rg -n "\bpopen_hidden\b" .` → `core/process.py:84,119` ; `rg -n "process\.which"` → 0 (`shutil.which` utilisé en direct) | **DEAD** |
| `core.lazy.load` | `core/lazy.py:115` | `rg -n "from core\.lazy import load\|lazy\.load\(" .` → `tests/test_boot_phase3b_lazy.py:90` | **DEAD en prod** |
| `classify_free_error` + `FREE_ERROR_*` | `core/errors.py:775-784` | `rg -n "classify_free_error\|FREE_ERROR_" .` → `core/errors.py` + `tests/test_free_error_taxonomy.py` ; **0 hit dans `free/`** | **DEAD en prod** (l'unique consommateur prévu, `free/fallback.py:231`, n'est lui-même appelé que par un test) |
| `_anthropic_error`/`_openai_error` (alias module) | `core/errors.py:129,154` | `rg -n "errors\._anthropic_error\|errors\._openai_error"` → 0 | **DEAD** |
| `_KeyPauser` | `core/keys.py:444` | `rg -n "from core\.keys import _KeyPauser"` → 0 (l'hôte importe `KeyPauser as _KeyPauserBase`) | **DEAD** |
| `_ResponseCache`, `_Bucket`, `_RequestBodyLimitMiddleware` (alias) | `server/cache.py:212`, `server/throttle.py:86,227` | `rg -n "from server\.cache import _ResponseCache"` etc. → 0 | **DEAD** |
| `server/__init__.py` `__all__` (12 noms) | `server/__init__.py:19-42` | `rg -n "from server import \|^import server$"` → uniquement `from server import websearch as _ws_mod` (`opencode.py:19602`) | **DEAD** (racine de paquet jamais importée) |
| `_normalize_query`, `_format_ddg`, `_is_safe_fetch_url`, `_execute_ddg_search`, `_execute_web_fetch`, `_strip_web_tool` (formes pures) | `server/websearch.py:122,138,178,299,445,488` | `rg -n "from server\.websearch import _"` → 0 (l'hôte redéfinit ses propres wrappers homonymes, `opencode.py:19603-19677`) | Test-only |
| `_get_enabled_keys`, `_env_key_or_raise` | `opencode.py:224,228` | `rg -n "_get_enabled_keys\|_env_key_or_raise"` → définition seule | **DEAD** |
| `summarize_tool_outputs` | — | `grep -rn "summarize_tool_outputs"` → **0 hit** (n'existe pas ; mentionné ici pour clore l'hypothèse) | N/A |
| `_orig_anthropic_to_openai` | `mapping.py:2039` | Appelé 3× dans le wrapper `2102-2133` | Vivant (pattern décorateur manuel) |
| `protocol_mapping.py` (racine, 171 l.) | — | 18 tests + 2 scripts ; **0 import de production** (`opencode.py:5312` fait `import protocol_mapping as _pm` pour `conversion_cache_stats`) | **Dette de shim** — un seul site de prod |

**TODO/FIXME/XXX/HACK, `if False`, code commenté** : aucun dans `streaming/`, `upstream/`, `server/`, `core/` (vérifié). Aucun dans `app/protocol/` ni `app/compaction/`.

---

## 6. Modules sans test + plan de découpage

### 6.1 Couverture

**Sans aucun test dédié :**

| Module | Note |
|---|---|
| `server/accesslog.py` | `rg -n "AccessLog" tests/` → **0** |
| `server/throttle.py` — `RateLimitMiddleware` | `rg -n "RateLimitMiddleware" tests/` → **0** ; `consume_sync` non testé ; `Bucket.consume` (async) test-only |
| `server/cache.py` — `make_key`, `stats`, `_evict` par octets, expiration TTL | `rg -n "make_key" tests/` → **0** |
| `upstream/breaker.py` | Aucun test n'importe le module ; indirect via la sous-classe hôte. Non couverts : `Global429BackoffMiddleware.__call__` (forme de réponse), `Global429State.remaining`, `get_status`, `record_failure` half_open→open |
| `upstream/clients.py` | Aucun test direct. Non couverts : `close_all_pools`, `swap_pools_for_proxy` (dont `_drain`), `build_fresh_client` (toujours monkeypatché), `default_role_timeout`, chemin d'overflow du checkout, `discard` |
| `upstream/quotas.py` | Aucun test. Non couverts : TTL de `fetch_quotas_cached`, `alias_for_api_key` |
| `app/compaction/classify.py` | `grep "compaction.classify" tests/` → 0 fichier (couvert indirectement via `test_compaction_free_class.py`) |
| `app/compaction/overflow.py` | 0 fichier dédié (couvert indirectement via `test_server_compaction.py`) |
| `app/compaction/plan.py` | 0 fichier dédié |
| `app/compaction/router.py` | 0 fichier dédié (`test_compaction_unified.py` couvre `detect_compaction` seul) |
| `app/compaction/shapes.py` | 0 fichier dédié |
| `app/compaction/streaming.py` | 0 fichier dédié (couvert via `test_compaction_tiny_retry.py`) |
| `app/compaction/transport.py` | 0 fichier dédié |
| `app/compaction/summarizer.py` | Couvert (2 fichiers) mais `has_required_outline`, `should_clamp_summary`, `cap_summary_max_tokens`, `extract_previous_summary` sans test direct |
| `streaming/empty_guard.py` | `_split_complete_sse_events`, `_is_keepalive_event`, `_first_meaningful_event_kind` non testés directement |
| `streaming/tool_paths.py` | Branche `free.station_identity.translate_paths` et repli `PATH_TOOL_NAMES` non testés |
| `streaming/retry_until_success.py` | `classify_error_frame`, `classify_json_body`, `parse_retry_after` exportés mais jamais référencés par un test ; la branche morte `outer["committed"]` n'est évidemment pas testée |
| `app/protocol/mapping.py` | 28 fichiers de test — le module le mieux couvert du périmètre. **Trous ciblés** : `_extract_cache_tokens` sur forme Responses (P0-2), `_extract_cache_creation_tokens` sur forme Responses (P0-3), `apply_anthropic_thinking_display`, `_looks_encrypted_reasoning`, `openai_chat_to_responses` avec usage Responses (P2-3), `content_filter` non-stream (P1-1) |
| `core/process.py` | `popen_hidden`, `which` non testés (aussi morts) |

**Exigences constitution XVII non couvertes** : délestage 429 global (aucun test d'intégration de la boucle retry×breaker), 413 middleware (forme du corps), `Retry-After` sur le chemin Chat exhausté, concurrence de `KeyPauser._save`.

### 6.2 Plan de découpage de `app/protocol/mapping.py` (6 329 l.)

Le module est **3,2× la borne** de la règle XIV (1 500 l.). Découpage proposé, dans l'ordre de risque croissant, chaque étape étant un déplacement pur (aucun changement de comportement) vérifié par la suite golden existante (`test_conversion_golden.py`, `test_e2e_protocol_matrix.py`) :

| Étape | Contenu | Plage actuelle | LOC | Risque |
|---|---|---|---:|---|
| 1 | `app/protocol/usage.py` — `_extract_cache_tokens`, `_extract_cache_creation_tokens`, `_usage_int`, `anthropic_input_tokens`, `anthropic_usage_from_openai`, `_extract_reasoning_tokens` | 215-352, 493-516 | ~160 | **Faible** — corrige au passage P0-2/P0-3 en centralisant |
| 2 | `app/protocol/sse_responses.py` — `ResponsesSseState`, `_responses_item_delta`, `_responses_sse_to_chat_deltas`, `ResponsesStreamEmitter`, `responses_stream_events`, `responses_stream_sse` | 5334-6329 | ~995 | Faible — déjà isolé par état par stream |
| 3 | `app/protocol/tool_names.py` — `_short_tool_name`, `sanitize_tool_names`, `restore_tool_name`, `_inverse_tool_name_lookup`, `_register_defensive_short`, `_remap_*`, `_sorted_chat_tools`, `sanitize_chat_tool_names`, `restore_chat_response_tool_names` | 3498-3580, 4034-4378 | ~430 | Faible — `tool_aliases.py` existe déjà comme voisin |
| 4 | `app/protocol/schemas.py` — `_resolve_schema_profile`, `_normalize_tool_schema`, `_rewrite_unicode_properties`, `_SCHEMA_PROFILES`, `_strip_billing_header` | 959-1320 | ~360 | Moyen — c'est ici qu'ajouter le `lru_cache` (P2-4) |
| 5 | `app/protocol/cache_control.py` — `_cc_is_set`, `_count_cache_breakpoints`, `_enforce_cache_breakpoint_limit`, `_apply_top_level_cache_control`, `_cache_control_to_openai_breakpoint`, `_restructure_for_cache`, `_system_cache_control` | 745-958, 1320-1335 | ~250 | Moyen — logique de contrat (4 breakpoints) |
| 6 | `app/protocol/thinking.py` — `_local_signature`, `_is_local_signature`, `_looks_encrypted_reasoning`, `strip_synthetic_thinking`, `apply_anthropic_thinking_display`, `anthropic_modern_adaptive_thinking`, `anthropic_binds_thinking`, `_effort_to_*`, `_apply_anthropic_effort` | 605-744, 2136-2250, 3743-3790 | ~330 | Moyen — état de cache mutable partagé avec le shim |
| 7 | `app/protocol/responses_request.py` — `_normalize_responses_input_items`, `_sanitize_native_responses_request`, `_relay_responses_*`, `_chat_to_responses_request`, `_anthropic_to_responses_request` | 4378-5186 | ~810 | Élevé — c'est le chemin le moins testé |
| 8 | `app/protocol/chat_request.py` — `anthropic_to_openai` (impl + wrapper cache), `_system_cache_control`, helpers d'image/document | 1336-2055 | ~720 | Élevé — chemin le plus chaud |
| 9 | `app/protocol/chat_response.py` — `openai_to_anthropic`, `openai_to_anthropic_request`, `anthropic_to_openai_response`, `_responses_part_to_anthropic`, `openai_responses_to_anthropic`, `anthropic_to_openai_responses`, `openai_chat_to_responses`, `_responses_to_*_response` | 2252-3497, 5186-5334 | ~1 400 | Élevé |

Après l'étape 9, `mapping.py` ne contient plus que les helpers partagés (~400 l.) et redevient une façade de re-export, exactement comme `protocol_mapping.py` aujourd'hui.

---

## 7. Zones non auditées / incertitudes

1. **`opencode.py` (33 618 l.)** — seuls les sites d'appel des symboles du périmètre ont été lus. Les handlers `messages`/`chat_completions`/`responses` ne sont pas audités en propre (cf. rapport 06).
2. **Chemins free** (`free/`) — non audités ; ils consomment `app/compaction` et `core.errors` par leurs propres chemins.
3. **`dashboard/`, `observability/`** — hors périmètre (rapport 08).
4. **La forme exacte des `usage` réellement renvoyés par chaque upstream opencode** n'est pas vérifiée sur le fil : les P0-2/P0-3 sont prouvés **au niveau de la fonction** (exécution) et par la divergence interne du code (`anthropic_usage_from_openai` lit deux conteneurs, `_extract_cache_tokens` un seul). Si aucun upstream ne renvoie jamais `input_tokens_details`, l'impact réel est nul — mais alors `anthropic_usage_from_openai:279` serait lui aussi du code inutile, ce qui est peu probable étant donné qu'il a été ajouté explicitement.
5. **La granularité du `cache_creation` top-level vs imbriqué** (P2-18) dépend du comportement des upstreams Anthropic-compat ; la décision documentée (`mapping.py:295-297`) est assumée mais son asymétrie n'est pas testée.
6. **`_drain` de `upstream/clients.py`** : le chemin d'annulation qui ferait fuir un `busy` n'a pas été identifié concrètement — l'exposition est réelle (`discard()` existe pour ça) mais non prouvée en conditions de production.
7. **`/v1/chat/completions/` avec slash final** : `retry_until_success.py:630` teste `path.endswith("/chat/completions")` ; je n'ai pas vérifié si l'hôte enregistre la variante avec slash.
8. **Le nombre de tests exécutés** : je n'ai pas lancé la suite complète (volontairement). Les sous-agents ont lancé des tests ciblés (tous passants) sur `streaming/`, `upstream/`, `server/`, `core/` — aucun échec signalé, ce qui signifie que les défauts ci-dessus ne sont **pas** couverts par la suite actuelle.
9. **`app/protocol/mapping.py` sections 7 000+** : le fichier fait 6 329 lignes ; la totalité a été cartographiée par grep de sections et les zones 1 336-2 133, 2 252-3 500, 3 688-3 800, 4 000-5 334, 5 190-5 340, 5 930-6 329 ont été lues en détail. Les zones 3 800-4 000, 4 400-4 700, 5 500-5 930 n'ont été lues qu'en extraits ciblés.
