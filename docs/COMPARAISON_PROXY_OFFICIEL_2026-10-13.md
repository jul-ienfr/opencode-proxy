# Comparaison exhaustive proxy ↔ client officiel OpenCode — 2026-10-13

> Périmètre : proxy `opencode-proxy-one` (état arbre au 2026-10-13) vs client officiel
> **anomalyco/opencode** (ex-`sst/opencode`, branche `dev`, version **2.0.28**, package `@opencode/ai 2.0.28`).
> Le plan existant `docs/PLAN_ALIGNEMENT_CLIENT_OFFICIEL_2026-10-09.md` visait la **v1.18.35** (HEAD `3884062`) :
> tout ce qui suit re-vérifie ses ancrages contre la v2 et signale les dérives de version.
> Méthode : 4 cartographies proxy par sous-agents + lecture directe des sources officielles
> téléchargées dans `_research/official/` via l'API GitHub (+ `gh search code` quand le fichier
> n'était pas téléchargé). Le TLS sortant direct (schannel/curl/Python) est cassé sur cette
> machine ; `gh` (TLS propre) fonctionne — le catalogue live `models.opencode.ai/api.json`
> n'a donc **pas** pu être re-mesuré, on s'appuie sur les fixtures officielles + le plan V0.

Références officielles exactes (fichier:ligne, branche `dev` au 2026-10-13) et proxy
(fichier:ligne, arbre courant). Verdicts selon la règle du plan §14.1 :
**Q1** contrainte amont ? **Q2** contrainte proxy légitime ? sinon défaut.

---

## 0. Ce qui a changé côté officiel depuis le plan (v1.18.35 → v2.0.28) — lire d'abord

| # | Sujet | v1.18.35 (base du plan) | v2.0.28 (constaté) | Conséquence |
|---|---|---|---|---|
| V-1 | Dépôt | `sst/opencode` | `anomalyco/opencode` (redirection GitHub, `gh repo view sst/opencode` → owner `anomalyco`) | Mettre à jour les URLs/ancrages du plan |
| V-2 | Layout paquets | `packages/opencode/*`, `packages/llm/*`, `packages/web/.../go.mdx` | `packages/ai/src/*` (protocoles, route, schema), `packages/core/src/session/*`, doc `services/www/src/docs/content/console/go.mdx` | Tous les ancrages `transform.ts`/`request.ts`/`retry.ts` du plan sont périmés en chemin (sémantique globalement conservée) |
| V-3 | User-Agent | `opencode/1.18.31 ai-sdk/provider-utils/4.0.23|4.0.40 runtime/bun/1.3.14` (mesuré au captureur) | `App.useragent(app)` = ``opencode/<channel>/<version>/<name>`` (`packages/core/src/app.ts:24-26`) — plus de `ai-sdk/provider-utils`, plus de `runtime/bun`, plus de double version chat/responses | **D1 à rouvrir** : la mimétique UA actuelle imite un format que l'officiel n'émet plus |
| V-4 | Tri des outils | **Trié** : `Object.fromEntries(Object.entries(tools).toSorted(([a],[b]) => a.localeCompare(b)))` (`request.ts:184` v1, un seul point de tri après shims — a tranché E3) | **Non trié** : `flattenTools`/`flattenToolRequest` préservent l'ordre (`protocols/shared.ts:301-321`) ; `toSorted`/`localeCompare` n'apparaissent plus que pour des listes UI (`plugin/provider/opencode.ts:613` orgs) — grep `sort` sur tout le corpus officiel : 1 seul match, hors outils | **E3 à rouvrir** : le tri ICU du proxy était aligné v1, il est divergent v2. Voir §6 |
| V-5 | Plafond output | `OUTPUT_TOKEN_MAX = 32_000` (`transform.ts:18` v1) | `OUTPUT_TOKEN_FALLBACK = 32_000`, `OUTPUT_TOKEN_MAX = 256_000`, `SUMMARY_OUTPUT_MAX = 32_000`, `OUTPUT_TOKEN_MIN = 1_024`, `ESTIMATE_ERROR = 0.15` (`session/model-request.ts:49-57`) + `DEFAULT_MAX_TOKENS = 32_000` côté protocole (`anthropic-messages.ts:39`) | Le « 32 000 dur » du plan §14.3 n'existe plus : c'est devenu un **fallback**, le cap est 256k, et le calcul est dynamique (place restante). Voir §5 |
| V-6 | Base d'inférence | `https://opencode.ai/zen/go/v1` + `https://opencode.ai/zen/v1` | Provider `opencode` : `baseURL https://opencode.ai/inference/v1` (`ai/src/providers/opencode.ts:6`) ; la passerelle Zen (`console/app/src/lib/inference-proxy.ts`) mappe `POST /zen/v1/*` et `/zen/go/v1/*` vers `/openai|/anthropic|/systemone|/google` internes — les bases `/zen/*` restent l'adresse publique, `/inference/v1` est la nouvelle adresse native | Pas de changement d'adresse à faire côté proxy, mais le plan §1 (« 2 providers catalogue ») doit être relu |
| V-7 | Temperature/Claude | Plan §14.5 : omission `temperature`/`topP`/`topK` pour ids contenant `claude` (`transform.ts:533,549-574` v1) | **Aucune omission trouvée** : `temperature: generation?.temperature` envoyée telle quelle sur Anthropic (`:1079`) comme sur Chat (`openai-chat.ts:848`) | Côté proxy : le relais client→amont est intact et `claude→{}` (aucun défaut inventé) est aligné ; **reste** les défauts codés des autres familles (`sampling_defaults_for_model`, `mapping.py:3608-3642`), sans équivalent officiel — voir §2.2 |

---

## 1. Routage modèle → endpoint (le plus structurant)

### 1.1 Officiel : catalogue dynamique → package npm → route déclarative
- Résolution session : `session.model{providerID,id}` cherché dans `available()` (catalogue), sinon `ModelUnavailableError` ; puis `resolver.resolveModel(selected, variant)` (`core/session/runner/model.ts:75-91`).
- Sans modèle : `models.default()`, sinon 1er dispo avec package texte (résolveur).
- `package` npm → `Provider.packageName()` → `loadPackage()` → `module.model(id, settings)` construit le `LanguageModel` ; **l'id API peut différer de l'id catalogue** (`models-dev.ts:85-94`, `model-resolver`).
- Double identité conservée : `Resolved = {model (route, id API), ref (catalogue stable), capabilities, cost, limit, compaction, transport}` ; `withVariant` merge un overlay (settings/headers/body) **sans changer l'identité**.
- Endpoint déclaratif par route `{baseURL, path (string|fn), query}` (`ai/route/endpoint.ts:21-25`). Seul hook runtime : `session.model.request` peut changer **`baseURL` uniquement** (`model-request.ts:304-320`).
- Catalogue : plugin Console fusionne `/api/v2/config` (providers/models/variants/cost/limit/compat, `canonical`, `modelID`) (`plugin/provider/opencode.ts:258-328`) ; **sans clé : `apiKey: "public"`, modèles payants désactivés** (`:330-355`) — l'anonyme officiel existe donc (trial), mais côté Console, pas côté passerelle Zen.
- Refresh catalogue : snapshot bundlé `models-dev/snapshot.txt` + cache disque TTL 5 min + poll périodique (`models-dev.ts:281-436`). Dérive impossible par construction.
- `reasoning_options` catalogue : `{type: effort, values: [null, none, low, medium, high, xhigh]}`, `{type: budget_tokens, min, max}`, `{type: toggle}` (fixtures `models-dev.json`, `models-dev-reasoning.json`) → dérivés en **variantes** (`models-dev.ts:197`, `modal/models.ts`).

### 1.2 Proxy : table statique + heuristiques
- Source unique `_resolve_model_endpoint` (`config/settings.py:822-849`), pré-calculée dans `MODELS` à l'import (`:852-861`), relue via `get_model_config` (`:1555-1578`, défaut `protocol: openai`).
- Priorité : 1) `endpoint:` explicite (`go`→payant, `free`→libre, sinon URL verbatim) 2) `api: responses` 3) suffixe `-free` → free 4) `protocol` → base payante chat/messages.
- Familles `get_model_api` : muse/spark → responses (`settings.py:687-690`).
- Bases : payant `https://opencode.ai/zen/go/v1/{chat/completions,messages,responses(dur :818)}`, libre `https://opencode.ai/zen/v1/{chat/completions,responses(dur :819)}`, systemone verbatim par modèle. Les 2 bases responses sont **codées en dur** (non surchargeables YAML) — anomalie n°7 du routeur.
- `_route_for` (`app/router/__init__.py:25-144`) : exact O(1) → substring longest-first → wildcard ; `None` jamais caché ; `custom_routes` (**étape 0**) réécrivent le modèle avant `get_model_config` — ex. `kimik26`, `glm51`, `gpt56luna`, `musespark13contributor` → `muse-spark-1.3-contributor` (responses) : le trafic réel ≠ `models:` affiché.
- Routes `opus/sonnet/haiku` → `kimi-k2.6` / `glm-5.1` / `minimax-m2.5` (`config.yaml:23-26`, ENV override prioritaire) ; `free_model_map` : les 3 → **un seul** `mimo-v2.6-flash-free` (point de contention) ; `default_target` identique.
- Endpoints exposés : `POST /v1/messages` + `/anthropic/v1/messages` (même handler `messages()`, `opencode.py:11370`), `POST /v1/chat/completions` (`:14728`), `POST /v1/responses` (`:18128`), `POST /v1/systemone` (`:17782`), `POST /v1/messages/count_tokens` (estimation locale, jamais forwardé, `:14718`), `GET /v1/models`, `/health*`, `/metrics`, `/api/*` dashboard (même app :4000).

### 1.3 Écarts routage
| # | Écart | Verdict |
|---|---|---|
| R1 | Table statique vs catalogue dynamique + refresh (dérive `minimax-m2.5`, grok/luna, qwen — lots A/H/N du plan) | **À aligner** (Q1). Au minimum : job diff lecture seule (fait H1 : `scripts/check_models_catalog.py`) + purge durable (faite) ; au mieux : refresh périodique + snapshot bundlé à la `models-dev.ts` |
| R2 | `custom_routes` réécrivent l'identité avant résolution (traçabilité/coûts cassés : DB `model` = cible, pas l'id demandé — constaté canary N6) | **À aligner** (ni Q1 ni Q2) : passer en overlay variant (identité catalogue conservée), ou au minimum logger les deux ids |
| R3 | Alias opus/sonnet/haiku codés en dur (n'existe pas côté officiel) | **Divergence assumée** (Q2, rôle mandataire multi-clients) — documenter, pas aligner. Mais les **cibles** doivent être vivantes (N1 : les 3 cibles actuelles sont mortes/mourantes côté Go — **P0 inchangé**) |
| R4 | `systemone` comme base de chat (`jev-1.13*`) ; côté officiel SystemOne = route **d'évaluation**, pas de chat (`providers/opencode.ts:15-26`, `system-one.ts:104-110`) | **À vérifier** : soit endpoint interne assumé (documenter, cas H), soit à retirer |
| R5 | Match substring (`m in name`), fallback `get_model_config` inatteignable (404 avant), alias `nimo:mimo` suspect, secret `control_api_key` en clair dans `config.yaml:263` | Défauts maison (ni Q1 ni Q2), hors alignement mais à corriger |

---

## 2. Appels modèles — endpoints et paramètres (ce qui part sur le fil)

### 2.1 Endpoints effectifs
| Modèle (proxy) | Proxy → | Officiel catalogue (plan V0, à re-mesurer — live inaccessible ici) |
|---|---|---|
| `glm-5.1`, `kimi-k2.6` | payant `/chat/completions` | Go : absents/`deprecated` (N1) — **404/401 probables en payant** |
| `minimax-m2.5` | payant `/messages` (anthropic) | Go : absent ; Zen : `@ai-sdk/openai-compatible` → `/chat/completions` (A3) |
| `qwen3.6-plus`, `qwen3.7-max` | payant `/chat/completions` (corrigé A2 ✅) | `/chat/completions` ✅ |
| `qwen3.7-plus`, `qwen3.8-max/flash` | payant `/messages` (inchangés, vérifiés) | `/messages` ✅ |
| `grok-4.5/4.6/4.7`, `gpt-5.6-luna`, `gpt-6-luna` | payant `/responses` (corrigé A1 ✅, sauf ombrage `gpt56luna` → free) | `/responses` ✅ (mais canary N6 : 4/5 **refusés amont** 400/403 — contrainte amont, pas de rollback config) |
| `muse-spark-*-free` | libre `/responses` ✅ | `/responses` ✅ |
| `mimo-v2.6-flash-free` et ~16 modèles free | libre `/chat/completions` ✅ | `/chat/completions` ✅ (canary N2 soldé : 200 + usage, `cache_read: 0` = contrainte amont N5) |
| `qwen3.5-plus` | payant `/messages` ✅ | Zen `@ai-sdk/anthropic` → `/messages` (seul cas Anthropic libre, I1) ✅ |
| `claude-haiku-5-5` | payant `/messages` (ajouté B2 ✅) | `/messages` ✅ |
| 41 modèles anonymes sans entrée `config.yaml` | repli implicite → **mauvais** endpoint (N4) | endpoint dicté par catalogue |

### 2.2 Paramètres du corps
| Paramètre | Officiel | Proxy | Verdict |
|---|---|---|---|
| `tool_choice` | **Omis** si pas d'outils ou pas de toolChoice (`anthropic-messages.ts:1052`, `openai-chat.ts:841`, `openai-responses.ts:210-214`) ; `disable_parallel_tool_use` mappé ; `generate` force outil nommé | **Partiellement aligné** : sens Anthropic→Chat conditionnel depuis E1 (`mapping.py:1778-1793`, filet `emit_default_tool_choice=false`) ✅ MAIS sens Chat→Anthropic force encore le défaut `"auto"` (`mapping.py:2551` → `{"type":"auto"}` systématique) et Responses idem (`:2930`) | **Reste à aligner** : conditionner les 2 autres sens (vrai E1 complet) |
| `max_tokens` / `max_completion_tokens` | Protocole Anthropic : `generation?.maxTokens ?? 32_000` (`:1070`). Chat : **aucun défaut**, champ détecté `detectMaxTokensField` (deepseek/moonshot/together/zai/nvidia/cerebras/chutes/cloudflare → `max_tokens`, sinon `max_completion_tokens`, `:683-715`) + override `compatibility.maxTokensField` | Défaut **16384** (`mapping.py:320-324`), priorité `max_tokens>max_completion_tokens>max_output_tokens`, raisonnement (`o1/o3/gpt-5…`) → `max_completion_tokens`, clamp `output_limit` catalogue (V3 ✅). **Pas de détection par provider** | **À aligner** : défaut Anthropic 16384→32000 ; détection `max_tokens` vs `max_completion_tokens` par provider/URL (E4 nouveau) ; le clamp catalogue est un **plus** (l'officiel ne clamp pas, il calcule — voir §5) |
| `temperature`/`top_p`/`top_k` | Envoyés tels quels sur les deux protocoles (Anthropic `:1079-1081`, Chat `:848-849`) ; **aucune exception Claude en v2** | Deux mécanismes : (1) relais client→amont intact (`:1650`, `:2487`, `:2916`) ✅ ; (2) `sampling_defaults_for_model` pose des **défauts par famille quand le client n'a rien envoyé** — `claude→{}` (aucun défaut, `:3615-3616`), autres familles : valeurs codées (north 1.0, gemini/guid, glm-4.6/4.7, minimax-m2, kimi-k2…) (`:3608-3642`) | **À réévaluer** : le cas `claude→{}` est aligné v2 (rien d'inventé) ; les défauts codés pour les autres familles (temperature 1.0/0.6, top_p, top_k) n'ont **aucun équivalent officiel** — soit à documenter comme tuning maison assumé, soit à retirer. Le port affiché `transform.ts` date de la v1 |
| `store` / `strict` / `prompt_cache_key` | `store:false` envoyé si supporté même sans option (Chat `:795-799`, `detectSupportsStore` exclut opencode/non-standard) ; `strict:false` si supporté (`:340`) ; `prompt_cache_key` **seulement si** `compatibility.supportsPromptCacheKey` (`:792-793`) — défaut **off** (commentaire `:789-791` : providers stricts 400 sur champs inconnus) | À vérifier : si le proxy envoie `prompt_cache_key` sans gate, ou n'envoie jamais `store:false` | **À aligner** : gater `prompt_cache_key` par compat (E13) ; `store:false` bénin dans les deux sens |
| `stream_options.include_usage` | Chat : si supporté, défaut détecteur = true (`:824,843`) ; Anthropic : rien | Forcé en stream (`mapping.py:1636-1644`, `opencode.py:13165`) ✅ | Aligné |
| `reasoning_effort` / `thinking` | Chat : `reasoning_effort` passé tel quel si présent (`:801`, string libre `options.ts:21`) ; Anthropic : `thinking` + `output_config.effort`, budget fitté < max_tokens et ≥ 1024 (`:1030-1083`) | `reasoning_effort` écrit depuis `resolve_effort` (`mapping.py:1842`) ; inverse : `output_config.effort` + `thinking adaptive`, jamais `budget_tokens` | Fond aligné, seuils à revoir (§4) |
| `tool_stream` (ZAI/GLM) | `tool_stream:true` si `detectZaiToolStream` + outils (`:844`, sauf glm-4.5*) | Non émis (à vérifier) | **À aligner** si GLM/ZAI en production (gains streaming outils) |

---

## 3. Traductions / conversions de formats

### 3.1 Matrice (proxy `app/protocol/mapping.py` ~270 Ko + `chat_sse_to_anthropic.py`)
| Conversion | Proxy | Officiel | Verdict |
|---|---|---|---|
| Anthropic→Chat (requête) | `anthropic_to_openai` (`:1224/1968`), cache_control→no-op côté OpenAI, outils + `cache_control` transporté | `flattenToolRequest` puis `lowerMessages`/`lowerTool` par protocole ; cache = politique de breakpoints, pas d'émission vers OpenAI (implicit prefix caching) | Aligné en effet |
| Chat→Anthropic (réponse + SSE) | `openai_to_anthropic` (`:2110`), stop mapping + `error` si EOF nu, `reasoning_content` relayé en delta **et** rejoué historique, `cache_read` depuis `prompt_tokens_details` | `sumTokens` (input = nonCached+read+write), reasoning = `thinking_tokens`, `reasoning_content` + détails Kimi relayés, round-trip thinking/redacted | Aligné (lots F1/F2 ✅) |
| Responses | Convertisseur dédié (`:4342+`) : **faux-streaming** (rejeu réponse complète par tranches 64 car.), `tool_choice` normalisé à `"auto"`, `max_output_tokens>max_tokens>max_completion_tokens`, `reasoning{summary,effort}` + `include encrypted_content` si `store:false` | `openai-responses.ts` + `openai-compatible-responses.ts` (défaut `store:false + include reasoning.encrypted_content` `:20`) ; `openai-beta: websocket` forcé (`:315`) ; `resolveEffortUpdates` | Faux-streaming = **divergence assumée** (Q2 : contrainte free `stream:true` imposé + clients non-streamants) ; `openai-beta` websocket à envisager sur Responses |
| `count_tokens` | Estimation locale (`tokens.py:108-182`, tiktoken sinon len//3 + coûts médias) — jamais forwardé | Pas d'équivalent (l'officiel connaît les tokens via `inputTokens {measured, estimated}` pour `outputLimit`) | Divergence assumée (Q2 : endpoint maison pour clients) — documenter « estimation » |
| Anthropic→Anthropic (passthrough payant) | Relais + `_ensure_anthropic_beta`, `x-api-key` + `anthropic-version: 2023-06-01` | `anthropic-version: 2023-06-01` (`endpoint headers :1673`), betas fusionnées | Aligné |

### 3.2 Outils : tri, schémas, IDs — **le point à retrancher**
| Point | v1.18.35 (plan) | v2.0.28 (actuel) | Proxy | Verdict |
|---|---|---|---|---|
| Ordre | **Trié** `localeCompare` après shims (`request.ts:184`) | **Préservé** (`flattenTools`, aucun tri — vérifié : 1 seul `localeCompare` hors outils dans tout le corpus) | **Trié** collation ICU `_tool_sort_key` (`mapping.py:3760`, 395 noms vérifiés, 3 sites dont `_free_wire_body`) | Le proxy est aligné **v1** et divergent **v2**. L'argument cache-préfixe du plan reste valable physiquement, mais ce n'est plus « l'ordre officiel ». **Rouvrir E3** : soit on garde le tri comme optimisation maison documentée (Q2 : mandat multi-clients hétérogènes), soit on le retire derrière flag (déjà désactivable par jambe ? à vérifier). Ne pas le présenter comme « parité » sans re-mesure d'un client v2 |
| Schémas | `tool-schema.ts` (flatten/allOf ?) | `objectRoot` (type object, drop `not:null`/`anyOf` vide) + projections gemini/moonshot, **openai/responses = passthrough**, jamais `additionalProperties:false` | `anyOf/oneOf/allOf` null fusionnés, résiduels strippés, **`additionalProperties:false` forcé partout** (`:1069-1108`) | **À aligner** (Q1 en partie : Moonshot rejette `enum` sans `type`, cf. commentaire `:12`) : adopter `objectRoot` + projection moonshot (nos modèles kimi/mimo en ont besoin), **retirer le forçage `additionalProperties:false`** (E5 nouveau, risque 400 sur providers stricts — c'est exactement ce que le commentaire officiel `:789-791` interdit) |
| IDs | `tool-stream.ts` (stables) | `scrubToolCallID = id.replace(/[^a-zA-Z0-9_-]/g,"_")` (`openai-chat.ts:578-592`), sans préfixage | Le **scrub existe** (`scrub_claude_tool_id` `:3485-3489` = port exact `[^a-zA-Z0-9_-]→_`, `scrub_mistral_tool_id` `:3492`, appliqués par `scrub_openai_tool_ids` `:3512` — mais **seulement si le modèle est claude/mistral**, `:3524`) ; le préfixage `toolu_`/`call_` + sha1 (`:92-120`, `:2149`, `:2618`) sert le **côté client** (Claude Code attend des `toolu_`) | **Nuancé** : étendre le scrub à tous les modèles (l'officiel ne conditionne pas au nom) ; le préfixage aval est une compatibilité descendante, pas un écart amont — garder en documentant la distinction |
| Shims free (`bash`/`read` injectés) | `_noop` injecté puis trié (v1) | Rien (pas de shims) | Injectés + triés avec le reste (jambe Chat ; Responses non triée — « ordre jamais mesuré », position honnête) | Contrainte proxy (Q2, bisection gateway I3) — garder, documenter |

### 3.3 Cache (déjà aligné, écart résiduel nul)
- Cap 4 des deux côtés (`BREAKPOINT_CAP`, `ANTHROPIC_BREAKPOINT_CAP=4`), auto = dernier outil + premier/dernier system + tail 1 message, ordre tools→system→messages, manuels comptés, surplus droppé + warning, TTL 5m/1h. Proxy `mapping.py:617-760` ✅ (plan §12).
- Finasseries v2 à porter si besoin : politiques par route (`RESPECTS_INLINE_HINTS`, `gatewayPolicy` QWEN/NONE, `cacheFormat` anthropic/alibaba), `effortUpdate` marqueurs exclus du tail, `supportsEffortUpdates`. Bénin aujourd'hui (nos cibles : openai-compat + anthropic).

### 3.4 Betas Anthropic (reste)
- Officiel : toujours `interleaved-thinking-2025-05-14` + `compact-2026-01-12` / `mid-conversation-output-config-2026-07-01` / `THINKING_BINDING_BETA` conditionnels (`:1644-1660`), merge avec existants, `?beta=true` si provider anthropic. Proxy : `interleaved` + `fine-grained-tool-streaming` (`opencode.py:341-342`) + binding daté C2 ✅. **Reste** : `compact-*` (compaction serveur maison — à émettre quand on compacte ?), `mid-conversation-output-config` (si `output_config` émis), `?beta=true` (jambe Anthropic directe).

---

## 4. Effort / reasoning (à repenser, pas à patcher)

- Officiel : **pas de plafond, pas d'allow-list**. Échelle 7 niveaux (`ReasoningEfforts`, `options.ts:194`) → **variantes catalogue par package** (`variant.ts`, table PROTOCOLS) : ex. Chat `low/medium/high→reasoningEffort`, Responses `none…xhigh→reasoningEffort+summary+include`, Claude `manual→budget_tokens` (3.7/4.x<4.6) sinon adaptive+effort, ZAI/GLM `low/high/max+thinking`, MiniMax-m2 `[]`, Kimi-k2 toggle/`[]`, Alibaba budget cap 64000, `manualThinking=min(16000,output-1)`, `ANTHROPIC_OUTPUT_TOKEN_MAX=32000`.
- Proxy : **plafond** (`effort_caps`, défaut `high`) **puis** allow-list (`reasoning_options`, flag `false` défaut = passthrough) + table budget↔niveau maison `(16000,xhigh)…(1,low)`.
- Écarts : (a) à `false`, l'effort client passe bit-par-bit — un niveau hors catalogue = 400 potentiel (C1) ; (b) l'allow-list derrière flag désarmé ne protège rien par défaut ; (c) l'échelle budget maison ne correspond à aucun seuil officiel ; (d) `MIN_THINKING_BUDGET=1024` non appliqué ; (e) `temperature` vs effort : aucun lien officiel.
- **À aligner** (Q1 : le modèle rejette ce qu'il ne porte pas) : porter la table `variant.ts` par famille (au minimum GLM/ZAI, MiniMax, Kimi, Muse/Spark, DeepSeek), activer l'allow-list par défaut une fois la table fiable, jeter l'échelle budget maison au profit des seuils par protocole, plancher budget 1024. Le flag `allow_list_enabled` reste le bon véhicule de bascule (V4).

---

## 5. `max_tokens` — le dossier à corriger (plan §14.3 périmé)

Formule officielle v2 (`model-request.ts:88-98`) :
```
model = min(catalogue.output ou 32_000 [fallback], 256_000 [cap])
requested = kind==compaction ? min(model, 32_000) : model
si inputTokens connus et context>0 :
    room = context - mesuré - ceil(estimé × 1.15)
    return min(requested, max(1_024, room))
```
Protocole Anthropic : défaut `32_000` si `generation.maxTokens` absent. Chat : **aucun défaut** (champ `undefined` si absent — le tableau montre `max_completion_tokens: generation?.maxTokens` pouvant être `undefined`).
- Proxy : défaut 16384 + clamp `output_limit` (V3 ✅) = **l'inverse de l'officiel** : trop restrictif par défaut (16k vs 32k), trop permissif sans catalogue (aucun fallback 32k, aucun cap 256k, aucun ajustement au contexte, aucun plancher 1024), et le clamp **abaisse** là où l'officiel **calcule**.
- **À aligner** : défaut Anthropic 32000 ; fallback 32000 quand pas de limite catalogue ; cap 256000 ; ajustement à la place restante quand les tokens d'entrée sont connus (on les a : estimation locale + usage réel) avec marge 15 % et plancher 1024 ; ne rien inventer côté Chat quand le client n'envoie rien (laisser `undefined`, comme l'officiel). Le 16384 devient un plancher de compat, pas un défaut.

---

## 6. Identité filaire (headers) — réouvert par la v2

### 6.1 Fil officiel exact (`model-request.ts:282-295`, `app.ts:24-26`, `generate.ts`, tests)
```
x-opencode-session-id: session.id
x-opencode-parent-session-id: parentID (si sous-session)
x-session-affinity: affinity
X-Session-Id: affinity
x-parent-session-id: parentID (si sous-session)
User-Agent: opencode/<channel>/<version>/<name>   (ex. canal prod, version semver, nom "cli")
x-opencode-project: session.projectID
x-opencode-session: affinity
x-opencode-client: app.name
+ promptCacheKey: affinity (dérivé ses_ hex64 → slice(4))
```
Notes : `x-opencode-session-id` ≠ `x-opencode-session` en v2 (id session vs affinité — la recopie proxy `session-id = session` validée en v1/D2 reste une approximation) ; `x-session-affinity` + `X-Session-Id` **nouveaux** (absents du plan) ; `x-opencode-request` n'apparaît plus dans ce fichier (la passerelle Zen relaie `x-opencode-request(-id)` si présent — `inference-proxy.ts:57-58` — mais le client v2 ne l'émet plus ici) ; `x-opencode-session` reste exigé par la doc Go (`go.mdx:83-86`).
- Console (hors inférence) : `User-Agent: App.useragent` + `authorization: Bearer` + `x-org-id` optionnel (`plugin/provider/opencode.ts:185-187,385,545-550`).

### 6.2 Proxy
- Payant (`_enrich_paid_wire_headers`, `opencode.py:5437-5497`, scope chat+responses) : UA double 1.18.31 (4.0.23 chat / 4.0.40 responses) + `x-opencode-client=cli` + `x-opencode-project` (git) + `x-opencode-session` (ses_ 30 car., tourné 30 min) + `x-opencode-session-id` (= recopie, D2 ✅) + `x-opencode-request` (= msg_id). Sémantique `setdefault`.
- Libre (wire octet-exact, `opencode.py:5320-5391`) : `Bearer public` + 4 `x-opencode-*` (**sans** session-id, moratoire I3) + transport explicite ; hygiène A.0 (strip auth payante, cookies, stainless).

### 6.3 Écarts
| # | Écart | Verdict |
|---|---|---|
| H-UA | UA 1.18.31 double-version : format abandonné en v2 (`opencode/<channel>/<version>/<name>`) | **À aligner** (D1 réouvert) : `opencode/prod/2.0.28/cli` (canal/version/nom à confirmer au captureur v2) ; la double version ai-sdk disparaît ; **jambe free seulement après re-mesure** (couplage UA↔TLS I3 inchangé) |
| H-AFF | `x-session-affinity` + `X-Session-Id` jamais envoyés ; `x-opencode-session` = ses_ local au lieu d'une affinité persistée ; `session-id` = recopie au lieu de l'id distinct | **À aligner** (Q1 : la doc Go dit router/cacher par session) : persister une affinité par conversation, envoyer les 4 headers session distincts |
| H-PAR | `x-opencode-parent-session-id` / `x-parent-session-id` absents (pas de notion de sous-session) | Bénin (pas de sous-sessions proxy) — envoyer quand connu, sinon rien |
| H-REQ | `x-opencode-request` (= msg_id) émis ; officiel v2 ne l'émet plus ici mais la passerelle le relaie | **Neutre** : garder (traçabilité, relayé par Zen), ne pas en faire un défaut |
| H-PROJ/CLI | `x-opencode-client=cli`, `x-opencode-project` dérivé git | Aligné en esprit (noms exacts ✅) |

---

## 7. Streaming, comptage, tokens

| Point | Officiel | Proxy | Verdict |
|---|---|---|---|
| Streaming obligatoire | `stream: true` codé (`openai-chat.ts:842`) ; retry après `outputStarted` interdit (sauf `Continue`) | Payant : non-streaming supporté (voie validée) ; libre : `stream:true` **forcé** (sinon 403, I3) | Divergences assumées Q2 des deux côtés — garder |
| `error` mid-stream | `onError` typé (`anthropic :1485-1491`, `openai :1051-1056`), finish `error/network_error` → erreur provider | Relayée (F1 ✅) + classée (G1 ✅) | Aligné |
| Usage | Somme nonCached+read+write, reasoning `thinking_tokens` / details | `message_start` + `message_delta` cumulés sans double-compte (F2 ✅), usage réel SSE sinon estimation chars//3 | Aligné |
| Faux-streaming Responses | N/A (vrai stream) | Rejeu tranches 64 car. | Assumé Q2 — garder, documenter |
| Compaction | `outputLimit` minoré 32k + betas `compact-*`, `supportsEffortUpdates`, marqueurs `Message.effort`, `CONTINUE_AFTER_INCOMPLETE_STREAM` | Compaction serveur maison (`app/compaction/`, résumeur clamp 4096) + `X-Session-Id` affinité non portée | Extensions maison (Q2) — porter les **marqueurs d'effort** et le **continue-after-interrupt** si compaction multi-tours, pas le reste |

---

## 8. Erreurs et reprises — le plus gros reliquat

### 8.1 Classification : proxy proche, 5 trous
Table proxy (`core/errors.py:205-230`) vs officielle (`provider-error.ts:16-178,235-305`) :
| Signal | Officiel | Proxy | Verdict |
|---|---|---|---|
| Overflow | ~35 regex + 3 codes + exclusions throttle, `400/413 (no body)` inclus | 1 motif `context_length_exceeded` | **À aligner** : importer la batterie regex + `payload-too-large` (413) + exclusions rate-limit |
| Quota | 402, 6 codes (`insufficient_quota`, `usage_not_included`, `billing_error`, `gousagelimiterror`, `freeusagelimiterror`, `creditlimitexceeded`), 429+texte quota ; type **`QuotaExceeded` non-retryable** | 4 motifs → `permission_error` + pause clé | **À aligner** : ajouter `billing_error`, `gousagelimiterror`, `freeusagelimiterror`, `creditlimitexceeded`, le 402, le label gateway `[code]` (`GATEWAY_CODE_LABEL`) ; typer `quota`/`QuotaExceeded`, **non rejouable** (aujourd'hui le type `permission` peut induire un failover inutile) |
| Auth | 401/403, 4 codes, 400+`incorrect api key` | 401→pause 3600s | Aligné en effet ; ajouter `permission_denied`, `api_key_invalid` |
| RateLimit | 429, codes `*rate_limit*/too_many_requests/throttlingexception`, texte | 429 + anti-faux-429 `is_free_quota_failure` (garde précieuse, sans équivalent officiel) | Garder la garde ; ajouter les codes |
| Interne | 408/409/5xx + 8 codes + texte `try again/at capacity/…`, `retryAfterMs` conservé | `overloaded_error` rejouable | **À aligner** : 408/409→interne-retryable, tout-4xx-restant→`InvalidRequest` non-retryable, `Unknown→retryable` (principe officiel : l'inconnu réessaie) |
| Policy | 6 codes + texte usage-policy | Absent | **À aligner** : `ContentPolicy` non-retryable (sinon boucles sur refus) |
| Preuve | `GATEWAY_CODE_LABEL` : Zen réécrit les codes mais garde `[code]` en tête de message | Non exploité | **À aligner** : extraire le label avant classification (sinon Zen masque tout en `server_error`) |

### 8.2 Retry : le défaut 2 est intenable face à l'officiel
- Officiel : **jusqu'à 10 retries** (≈11 tentatives), gaps 2/4/8 puis 10s ×7 (~84s + jitter), `retryAfterMs` honoré **borné 15 min** (RateLimit/ProviderInternal), **timeouts capés à 3**, hook `session.retry`, pas de retry après output, pas sur overflow (`retry.ts:32-92`, `step.ts:187-275`).
- Proxy : défaut **2**, clamp [1,10], backoff `base×2^att` + jitter, `parse_retry_after` borné (paid 600s = `key_pause.max_pause`, free 60s), breaker 5/60s single-probe + global 429, hedging + TTFB watchdog + failover clés (sans équivalent officiel — Q2).
- **À aligner** (Q1 pour les codes, Q2 partiel pour le nombre) : adopter `isRetryable` (Unknown→oui, Timeout/Quota/Auth/Policy/Invalid→non, `x-should-retry` prioritaire), borner `retry-after` à 15 min, caper les timeouts à 3, interdire le retry après bytes reçus (vérifier les boucles `opencode.py` — si rejouent après output, corriger), monter le défaut 2→**4-5** (le 10 officiel inclut le hook d'annulation ; un proxy N→1 ne doit pas amplifier à 10 — compromis documenté, G2 mixte confirmé). Breaker/hedging/watchdog/failover : **garder** (Q2).

---

## 9. Catalogue, refresh, hygiène (lots H/N — partiellement faits)

Faits (V6/H1 ✅ 2026-10-11) : 5 morts purgés (`mimo-v2-pro`, `mimo-v2-omni`, `hy3-preview`, `omen-alpha`, `deepseek-flash`), tombeaux `free_discovery.purged_ids`, job lecture seule `scripts/check_models_catalog.py`, 54 entrées `models:`, N2 recâblé (`spark-1.3-free`, `mimo-v2.6-flash-free`).
Restent : **N1 (P0)** — les 3 routes par défaut tapent des ids morts/mourants en payant (`sonnet→glm-5.1` et `haiku→minimax-m2.5` absents de Go, `opus→kimi-k2.6` deprecated) : canary payant N6 montre 0×200 payant (400/403 amont) — **arbitrage à prendre** (candidats plan : `sonnet→glm-5.3`, `haiku→claude-haiku-5-5`) ; **N3** — matrice `npm→endpoint` explicite (9 modèles `@ai-sdk/google`/`@ai-sdk/mistral` indéductibles) ; **N4** — 41 modèles anonymes sans entrée → repli silencieux faux (déclarer ou refuser explicite) ; refresh périodique + snapshot bundlé (modèle `models-dev.ts`).

---

## 10. Traductions — ce que le proxy fait et que l'officiel ne fait pas (garder)

Jambe free anonyme (`Bearer public`, replay TLS Bun, 7→11 en-têtes verrouillés, `stream:true` forcé, shims `bash`/`read`, invariant A.0), rotation IP/VPN gluetun multi-stations + identités TLS/UA tournantes, hedging + breaker + TTFB watchdog, politique geo, compaction serveur, websearch natif (DDG+fetch, garde SSRF), dashboard :4000 + SQLite, multi-clés/failover, non-streaming payant, `count_tokens` local, alias opus/sonnet/haiku, `custom_routes` (à transformer en overlays, pas à supprimer sans arbitrage). **Rien de tout cela n'existe côté officiel — c'est le patrimoine, pas la dette.**

---

## 11. Déjà aligné — ne pas toucher (§12 du plan, re-vérifié v2)

Auth Anthropic (`x-api-key` + `2023-06-01`) ✅ · betas interleaved/fine-grained ✅ · `include_usage` ✅ · cache ≤4 + positions ✅ · `reasoning_content` relayé ✅ · `input_tokens` total ✅ · stop mapping + `error` si EOF nu ✅ · C2 (beta daté) ✅ · E3 (tri — aligné **v1**, voir §3.2) · D2 (`session-id`, approximation v1) ✅ · F1/F2/G1/G2 (code, hors live) ✅ · V1 (A1/A2/B2 config) ✅ · V1b (shadow `custom_routes` verrouillé) ✅ · V3 (table `output_limit` + clamp) ✅ · C1 (allow-list derrière flag) ✅ · H1/N2 (purge + recâblage free) ✅.

---

## 12. Plan d'alignement proposé (garde les particularités, reprend les vagues)

| Vague | Contenu | Écarts | Risque / porte | Effort |
|---|---|---|---|---|
| **W1 — config seule** | N1 : basculer `sonnet→glm-5.3`, `haiku→claude-haiku-5-5` (après canary payant avec clé), `opus` : arbitrer `kimi-k2.6` deprecated (garder vs remplacer) | N1/B1 | moyen / payante | 0,5 j |
| **W2 — corps (code petit)** | défaut Anthropic 32000 ; détection `max_tokens` vs `max_completion_tokens` ; gate `prompt_cache_key` ; scrub IDs (remplace `toolu_/call_`) ; retire `additionalProperties:false` + projection moonshot ; `tool_stream` ZAI | E2/E4/E5/E6/E13 + §2.2 | faible-moyen / payante, golden tests | 1 j |
| **W3 — output dynamique** | `outputLimit` v2 (fallback 32k, cap 256k, place restante ×1.15, plancher 1024), Chat : ne rien inventer | §5 | moyen / payante | 0,5 j |
| **W4 — effort** | Table variantes par famille + seuils budget, allow-list défaut `true`, plancher 1024, `temperature` : retirer l'omission Claude | §4 | moyen / payante | 1 j |
| **W5 — erreurs** | Batterie regex overflow + 402 + codes quota/auth/policy + label `[code]` Zen + `ContentPolicy` + 4xx-restant→Invalid + Unknown→retryable | §8.1 | faible / lecture corps seul | 0,5 j |
| **W6 — retry/headers** | `isRetryable` + cap retry-after 15 min + cap timeouts 3 + pas de retry après output + défaut 2→4/5 ; UA `opencode/prod/2.0.28/cli` + 4 headers session (affinité persistée) — **payante d'abord**, free après re-mesure v2 | §8.2 + §6 | moyen / fil amont | 1 j |
| **W7 — E3 arbitration** | Trancher tri outils : garder comme optimisation maison documentée OU retirer derrière flag ; dans les deux cas cesser de le présenter comme parité sans capture v2 | §3.2/V-4 | faible / décision | 0,25 j |
| **W8 — hygiène** | N3 (matrice npm), N4 (41 anonymes : déclarer ou refus explicite), refresh périodique + snapshot, betas `compact/output_config/?beta=true`, `openai-beta` Responses, `custom_routes` → overlays avec double identité | §9 + §3.4 + R2 | faible-moyen | 1 j |

Règle d'or (plan §15, inchangée) : `config.yaml` d'abord, code ensuite, canary live par vague, rollback par vague. Critères d'arrêt §16 inchangés + 1 nouveau : **tout 400/403 amont préexistant (N6) ne déclenche pas de rollback config**.
Non-objectifs : jambe free (moratoire jusqu'à re-mesure v2), hedging/breaker/VPN/geo/compaction/dashboard (patrimoine Q2), non-streaming payant (voie validée).

*Sources : `_research/official/` (25 fichiers, branche `dev` 2026-10-13, `5b22e91`), `gh search code` (model-request, useragent, toSorted), doc Go `go.mdx`, passerelle `inference-proxy.ts`, fixtures `models-dev*.json`, plan du 2026-10-09 et ses cartes §18, cartographies proxy (routage, conversions, wire amont). Catalogue live non re-mesuré (TLS machine cassé) — W1/W8 exigent une re-mesure depuis une machine saine.*
