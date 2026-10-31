# Audit d'alignement — proxy `opencode-proxy` ↔ client officiel OpenCode

**Date de l'audit :** 2026-10-20
**Client de référence :** dépôt `anomalyco/opencode` (repo id `975734319`), release CLI **v1.19.3** (publiée 2026-10-16)
**Installations locales constatées :** desktop `@opencode-aidesktop` **2.0.32** (en cours d'exécution, bundle `opencode-cli.exe` 2.0.32) ; CLI npm `opencode-ai@1.18.31` (en retard d'une release)
**Périmètre :** wire HTTP, routage modèle→endpoint, corps de requête, identité, erreurs/reprises, streaming/comptage, catalogue, maintenabilité
**Hors périmètre :** UI dashboard, backend SQLite, flotte VPN (traités comme infrastructure existante)

> Ce document est l'**audit**. Le plan d'exécution est dans [`PLAN_ALIGNEMENT_2026-10-20.md`](PLAN_ALIGNEMENT_2026-10-20.md).
> Les sources officielles téléchargées et les mesures sont dans `_research/official-1.19.3/`.

---

## 0. Synthèse — les 12 constats qui commandent tout le reste

1. **Le référentiel a bougé de répertoire.** `packages/ai/` n'existe plus en 1.19.3 : le client LLM est dans `packages/llm/src/`, les schémas d'API dans `packages/protocol/src/`. Toutes les citations `packages/ai/...` et `model-request.ts` des audits antérieurs (`PLAN_ALIGNEMENT_CLIENT_OFFICIEL_2026-10-09.md`, `COMPARAISON_PROXY_OFFICIEL_2026-10-13.md`) sont des chemins morts. Le contenu a été repris dans `packages/core/src/session/runner/llm.ts` et `packages/llm/src/protocols/*`.
2. **Le catalogue est piloté par le serveur, pas par le client.** Le client fait `GET https://opencode.ai/console/api/config` à chaque démarrage (`packages/core/src/plugin/provider/opencode.ts:199-221`) et reçoit l'URL d'API, le package npm, les `options.body`, les `options.headers`, **et par modèle un override `provider.npm` + `provider.api`**. Le proxy, lui, porte une table statique. C'est l'écart structurant n°1.
3. **Les bases d'URL ont été découpées par protocole.** Live aujourd'hui : `https://opencode.ai/inference/openai/v1` (chat), `.../inference/anthropic/v1` (messages), `.../inference/google/v1beta` (gemini), `.../inference/go/openai/v1` (payant). Le proxy cible encore les bases plates `.../inference/v1` et `.../zen/go/v1`. **Les deux répondent encore** (vérifié), donc ce n'est pas une casse immédiate — c'est une surface dépréciée.
4. **La règle de routage officielle est par modèle, pas par provider.** Le proxy traite `npm` comme une propriété de provider (`config/models_catalog.py`, `config/npm_routing.py`) ; l'officiel le surcharge modèle par modèle. Conséquence mesurée : **1 modèle routé vers le mauvais protocole** (`minimax-m3.1-flash-free`), **2 ids routés vers la mauvaise jambe** (`minimax-m2.5`, `qwen3.6-plus`, présents côté free seulement) et **10 modèles gemini non routables**. *Corrigé le 2026-10-20 : l'estimation initiale de « 5 mauvais protocoles » venait d'une comparaison qui ignorait la jambe (voir §3.3).*
5. **Le corps officiel a changé de champ.** 1.19.3 émet `max_tokens` (`packages/llm/src/protocols/openai-chat.ts:361`) ; le proxy émet `max_completion_tokens` (hérité du bundle ai-sdk de 1.18.31). Un seul des deux est le wire actuel.
6. **`tool_choice` : le proxy est aligné — corrigé par la mesure.** Le code officiel `llm.ts:225` suggère l'absence de `tool_choice`, mais **la capture réelle du CLI 1.19.3 montre `tool_choice: "auto"` dès qu'il y a des outils** (§2.3). Le proxy fait donc la même chose (`opencode.py:5092,5114`). Le `"none"` du dernier step existe sur le chemin *natif*, pas sur le chemin *ai-sdk*.
7. **`reasoning_content` doit être renvoyé au tour suivant.** L'officiel le ré-émet explicitement (`openai-chat.ts:257-260`). Le proxy le perd : **erreur 400 reproduite en live** le 2026-10-20 (`The reasoning_content in the thinking mode must be passed back to the API`).
8. **Politique de reprise officielle : 2 tentatives**, backoff 500 ms ×2^n avec jitter 0.8–1.2, plafond 10 s, `retry-after`/`retry-after-ms` honorés, reprise sur 429/503/504/529 et 5xx (`packages/llm/src/route/executor.ts:36-38,91,345-352,266`). Le proxy est à 4 (config) et **8 sur la jambe free**, avec rotation de station sur 403.
9. **La clé payante du `.env` est expirée** : `GET /console/api/config` et la jambe Go renvoient `401 ApiKeyExpired`. Le proxy ne peut donc plus ni charger son catalogue distant, ni servir la jambe payée. Deux scrapers dépendants ont aussi cassé au boot (`model limits from docs`, `quota OpenCode Go`).
10. **Le modèle de la porte « free tier » du proxy est périmé sur un point et exact sur deux.** Mesuré le 2026-10-20 : la porte exige **(a)** une IP de sortie « résidentielle » (tunnel), **(b)** l'ensemble d'en-têtes `x-opencode-*` + `Bearer public`, **(c)** un corps `stream:true` **avec** un outil `read` à définition réaliste. Le **numéro de version du User-Agent n'est pas discriminant** : `opencode/prod/2.0.32/cli` passe (200), alors que le code affirme le contraire (`opencode.py:4969`) et fige l'UA sur 1.18.31.
11. **La jambe free est fonctionnelle mais fragile** : latences 2,7 s → 24,9 s sur des requêtes identiques, et des flux terminés par `error: upstream produced no output` renvoyés en **HTTP 200**.
12. **La dette de duplication de configuration est mesurable** : `https://opencode.ai/inference/v1` apparaît **4 fois en dur dans `opencode.py`** et **40 fois dans le dépôt** ; `/zen/go/v1` **23 fois**. Deux résolveurs coexistent et se contredisent (le log montre un resolver qui annonce la nouvelle base et un post qui part sur l'ancienne).

> **CORRECTION 2026-10-20 — « 4 URLs en dur » était faux pour l'essentiel.** Les trois bases principales (`openai_base`, `anthropic_base`, `free_base`) venaient **déjà** de `config.yaml`, et l'endpoint `systemone` était **déjà** surchargeable par variable d'environnement. Il n'y avait **aucune** affectation de littéral `https://opencode.ai/...` dans `opencode.py`. Le seul écart réel était plus étroit : les deux endpoints **Responses** étaient les seuls non surchargeables (`config/settings.py`). Ils sont désormais dans `config.yaml`, et un test interdit le retour de la forme fautive.

---

## 1. Méthode et sources de vérité

| Source | Nature | Usage |
|---|---|---|
| `api.github.com/repos/anomalyco/opencode/git/trees/v1.19.3?recursive=1` | arbre git (7 401 fichiers) | localiser les modules réels |
| `raw.githubusercontent.com/anomalyco/opencode/v1.19.3/...` | 196 fichiers téléchargés (`_research/official-1.19.3/`) | lecture du code officiel |
| `https://models.opencode.ai/api.json` | 231 providers, instantané 2026-10-20 | vérité catalogue (`models-truth.json`) |
| Sondes live `opencode.ai` | `Bearer public` + tunnels + curl_cffi | comportement réel de la passerelle |
| **Capture du vrai client 1.19.3** | `opencode.exe` (release officielle, installé dans `_research/oc-1.19.3/bin`), configuré sur un serveur de capture local (`_research/capture_server.js`) | **wire exact mesuré** (`_research/oc-1.19.3/capture.jsonl`) |
| `pyproject.toml` / `tests/` (167 fichiers) | suite existante | non-régression |
| `logs/debug.log` | traces d'exécution du proxy | comportement observé du proxy |

**Limites assumées :**
- `GET /console/api/config` (la configuration de provider servie par l'éditeur) **n'a pas pu être récupérée** : clé expirée. On en connaît le schéma par le code (l. 199-221) mais pas les valeurs courantes (`options.body`, `options.headers` par modèle).
- La capture du client a été faite sur un **provider custom** (`@ai-sdk/openai-compatible` → `/chat/completions`), qui emprunte le chemin *ai-sdk*. Le chemin *natif* (provider `opencode` intégré, UA `channel/version/client`) n'a pas été capturé : les deux chemins existent et n'émettent pas le même wire (§2.2).
- Les mesures de la porte free ont été faites via le tunnel SOCKS existant (stations du proxy), pas depuis une IP vierge.

---

## 2. Photographie de l'officiel au 2026-10-20

### 2.1 Deux canaux de version

| Canal | Version installée | Où |
|---|---|---|
| Desktop (Electron) | **2.0.32** | `%LOCALAPPDATA%\Programs\@opencode-aidesktop\` |
| CLI npm | **1.18.31** (1.19.3 disponible) | `%APPDATA%\npm\opencode.ps1` |

L'UA officiel encode `channel/version/client` : `opencode/<channel>/<version>/<client>` (`packages/core/src/models-dev.ts:23`), p. ex. `opencode/prod/2.0.32/cli`. Le proxy est figé sur `opencode/prod/2.0.28/cli` (`opencode.py:4740`) pour la jambe payée et sur `opencode/1.18.31 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14` (`opencode.py:4719`) pour la jambe free.

### 2.2 Deux chemins d'émission, deux wires

Le client 1.19.3 possède **deux** chemins qui n'émettent pas la même identité :

| Chemin | Déclencheur | UA | En-têtes de session |
|---|---|---|---|
| **ai-sdk** (`packages/core/src/aisdk.ts`) | provider configuré avec un package `npm` (`@ai-sdk/*`) | `opencode/<version> ai-sdk/provider-utils/<x> runtime/bun/<y>` | `x-opencode-session-id`, `x-session-affinity`, `x-session-id` |
| **natif** (`packages/core/src/session/runner/llm.ts:200-225`) | provider natif (`type: "native"`) | `opencode/<channel>/<version>/<client>` | `x-opencode-session-id`, `x-opencode-parent-session-id`, `x-session-affinity`, `X-Session-Id`, `x-parent-session-id` |

C'est ce qui explique la coexistence apparente de deux schémas d'UA dans la littérature du proxy : ce n'est pas une erreur, c'est **deux chemins**. Le proxy doit savoir lequel il imite.

**Le provider `opencode` (les deux jambes) est servi par `@ai-sdk/openai-compatible`** (catalogue §2.5) : c'est donc le **chemin ai-sdk** qui décrit le wire pertinent pour la jambe free et pour la jambe payante.

#### Capture du wire réel (CLI 1.19.3, provider `@ai-sdk/openai-compatible`)

Mesuré le 2026-10-20 (`_research/oc-1.19.3/capture.jsonl`), ordre exact :

```
Authorization: Bearer <clé>
Content-Type: application/json
User-Agent: opencode/1.19.3 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14
x-opencode-session-id: ses_ea779ff5affefpeTQFsgLFj3NX
x-session-affinity:    ses_ea779ff5affefpeTQFsgLFj3NX
x-session-id:          ses_ea779ff5affefpeTQFsgLFj3NX
Connection: keep-alive
Accept: */*
Host: …
Accept-Encoding: gzip, deflate, br, zstd
Content-Length: 51404
```

**Constat : les quatre en-têtes `x-opencode-client` / `-project` / `-request` / `-session` que le proxy émet ne sont PAS envoyés par le client 1.19.3.**

### 2.3 Corps officiel `/chat/completions` — mesuré

Capture réelle du CLI 1.19.3 (requête principale, 10 outils, 51 404 octets) :

```jsonc
{
  "model": "test",
  "max_tokens": 32000,                    // ← PAS max_completion_tokens
  "messages": [ … ],                      // system prompt de 28 577 caractères
  "tools": [ "bash", "edit", "glob", "grep", "read", "skill", "task", "todowrite", "webfetch", "write" ],
  "tool_choice": "auto",                  // ← présent dès qu'il y a des outils
  "stream": true,
  "stream_options": { "include_usage": true }
}
```

**Champs absents** sur ce chemin : `store`, `temperature`, `top_p`, `prompt_cache_key`, `reasoning_effort`, `max_completion_tokens`.

Requête secondaire capturée (génération de titre de conversation) : 3 messages, **aucun outil, aucun `tool_choice`**, `max_tokens: 32000`, mêmes `stream`/`stream_options`. Le client émet donc aussi des appels annexes que le proxy ne modélise pas.

Lecture du code correspondant (`packages/llm/src/protocols/openai-chat.ts:335-370`) : cohérente — sauf que le code ne mentionne pas `tool_choice: "auto"`, qui vient de la couche agent (le choix d'outil est décidé en amont), pas du protocole.

**Écarts proxy ↔ mesure :**

| Champ | Officiel (mesuré) | Proxy | Verdict |
|---|---|---|---|
| `max_tokens` | 32 000 | `max_completion_tokens` (free) | **écart** (P1-2) |
| `tool_choice` | `"auto"` si outils | `"auto"` si outils | **aligné** |
| `store` | absent | `true` | **écart** (P2-7) |
| `stream` / `stream_options` | `true` / `{include_usage:true}` | identique | **aligné** |
| `temperature`, `top_p` | absents | absents (sauf demande client) | **aligné** |

### 2.4 Reprises et erreurs

`packages/llm/src/route/executor.ts` :

| Paramètre | Valeur officielle |
|---|---|
| `MAX_RETRIES` | **2** |
| `BASE_DELAY_MS` | 500 |
| `MAX_DELAY_MS` | 10 000 |
| Backoff | `500 × 2^attempt`, jitter ×0,8–1,2, plafonné |
| `retry-after-ms` / `retry-after` (s ou date HTTP) | honoré, plafonné à `MAX_DELAY_MS` |
| Statuts repris | 429, 503, 504, 529 **et** tout `>= 500` |
| Statuts non repris | 400/401/403/404/409/413/422 (classés, jamais rejoués) |

Classification (`statusReason`, l. 250-273) : `InvalidRequest` (400/409/413/422, avec `classification: "context-overflow"`), `ProviderInternal` (5xx + statuts retryables), `UnknownProvider` (le reste).

### 2.5 Catalogue live — vraie règle de routage

`_research/official-1.19.3/models-truth.json` (instantané 2026-10-20).

| Provider | `npm` (niveau provider) | `api` (niveau provider) | Modèles |
|---|---|---|---|
| `opencode` (jambe anonyme) | `@ai-sdk/openai-compatible` | `https://opencode.ai/inference/openai/v1` | 128 |
| `opencode-go` (jambe payante) | `@ai-sdk/openai-compatible` | `https://opencode.ai/inference/go/openai/v1` | 33 |

**Surcharges par modèle (`provider.npm` + `provider.api`)** — c'est la règle qui compte :

| `provider.npm` du modèle | `provider.api` | Protocole effectif | Occurrences jambe `opencode` |
|---|---|---|---|
| `@ai-sdk/anthropic` | `…/inference/anthropic/v1` | `/messages` | 24 |
| `@ai-sdk/openai` | `…/inference/openai/v1` | `/responses` | 32 |
| `@ai-sdk/google` | `…/inference/google/v1beta` | gemini | 9 |
| `@ai-sdk/mistral` | `…/inference/openai/v1` | `/chat/completions` | 1 |
| *(absent → hérite)* | `…/inference/openai/v1` | `/chat/completions` | 63 |

Jambe `opencode-go` : 21 chat, 6 messages, 5 responses, 1 gemini.

Autres observations du catalogue : aucune entrée ne porte `headers`, `options` ni `variants` dans cet instantané — les modificateurs de corps viennent donc de la configuration **serveur** (`/console/api/config`), pas du catalogue public. C'est cohérent avec le constat n°2 : **le wire exact du client ne peut pas être déduit du catalogue seul.**

### 2.6 Fin de vie des « variants » catalogue

Note de release v1.19.3 : *« Configure model reasoning options to generate reasoning variants, replacing catalog variants while preserving your configured variant overrides. »* Les `variants` statiques disparaissent au profit de variantes générées depuis les options de raisonnement. Le proxy n'a pas de notion de variante (il a `custom_routes`).

---

## 3. Photographie du proxy

### 3.1 Résolution d'endpoint (chemin réel)

`config/settings.py:397-399` charge trois bases ; `:824-851` résout :

```
explicit `endpoint:` (go|free|URL)  >  api: responses  >  suffixe -free  >  protocol
```

`MODELS` est construit statiquement (`:854-863`), puis enrichi par un fetch de catalogue en tâche de fond (`:870-884`).

Trois constantes **échappent à la configuration** (`:820-821`) : `_RESPONSES_ENDPOINT`, `_RESPONSES_FREE_ENDPOINT`. Un changement de `upstream.free_base` ne les met pas à jour.

### 3.2 Duplication d'URL

| Littéral | `opencode.py` | Dépôt |
|---|---|---|
| `https://opencode.ai/inference/v1` | 4 | 40 |
| `https://opencode.ai/zen/go/v1` | 0 | 23 |
| `https://opencode.ai/inference/openai/v1` | 0 | 0 (hors `_research`) |
| `https://opencode.ai/inference/anthropic/v1` | 0 | 0 |
| `https://opencode.ai/inference/google` | 0 | 0 |

Preuve du conflit de résolveurs, `logs/debug.log`, 2026-10-20 :
```
[chat] route: 'deepseek-v4.1-flash-free' → … endpoint=https://opencode.ai/inference/openai/v1/chat/completions
[free-stream] posting to https://opencode.ai/inference/v1/chat/completions (pooled)
```
Le routeur annonce la nouvelle base (parce que `free_base` avait été modifié pour le test), le POST part sur l'ancienne (littéral en dur).

### 3.3 Table statique vs catalogue

62 modèles configurés (`config.yaml:31-435`), plus `custom_routes` (relances) et `free_model_map` (alias payant→free).

| Mesure | Résultat |
|---|---|
| Jambe `opencode` couverte | **46 / 128** |
| Jambe `opencode-go` couverte | **32 / 33** |
| Mismatches de protocole (free) | **1**, pas 5 — voir la correction ci-dessous |
| Modèles gemini officiels non routables | **10** (9 free + 1 go) — le proxy ne connaît que `openai` et `anthropic` |
| Ids proxy absents du catalogue | 3 (`jev-1.13`, `jev-1.13-free` = exception interne documentée ; `qwen3.7-max` = alias client) |
| Ids `-free` du catalogue non exposés | **23 / 43** — mais la mesure du 2026-10-20 montre qu'ils ne sont **pas** une réserve : sur 81 ids non exposés testés en direct, **1 seul** répond 200 (voir ci-dessous) |

> **CORRECTION 2026-10-20 — les « 5 mismatches » étaient 1 vrai + 2 erreurs de jambe + 2 faux positifs.**
>
> La mesure initiale comparait la vérité du catalogue sur la jambe **free** au routage du proxy, qui vise la jambe **payante** pour les ids sans suffixe `-free`. Deux populations différentes. Vérification faite, id par id, sur les deux jambes :
>
> | Id | Free (`opencode`) | Payant (`opencode-go`) | Routage proxy | Verdict |
> |---|---|---|---|---|
> | `minimax-m2.7` | chat | **messages** | payant `messages` | ✅ **correct** — l'audit avait tort |
> | `minimax-m3` | chat | **messages** | payant `messages` | ✅ **correct** — l'audit avait tort |
> | `qwen3.8-max` | chat | **messages** | payant `messages` | ✅ **correct** — l'audit avait tort |
> | `minimax-m2.5` | chat | *absent* | payant `messages` | ❌ **erreur de jambe** : le modèle n'existe que côté free |
> | `qwen3.6-plus` | **messages** | *absent* | payant `chat` | ❌ **erreur de jambe** : idem |
> | `minimax-m3.1-flash-free` | chat | *absent* | free `anthropic` | ❌ **vrai bug de protocole** — corps Anthropic sur un endpoint chat |
>
> Le même id peut donc porter une famille **différente** selon la jambe. Toute comparaison config ↔ catalogue qui ignore la jambe produira des faux positifs : c'est exactement ce qui s'est passé ici.

> **CORRECTION 2026-10-20 — les 81 ids free non exposés ne sont pas une réserve de modèles.**
>
> Tentant de lire l'écart « 46/128 couverts » comme 82 modèles à gagner. La mesure dit non. Un POST réel par id absent de `config.yaml` (81 ids), corps conforme à la porte, via tunnel, sur le chemin de la famille déclarée :
>
> | Réponse | Nombre | Sens |
> |---|---|---|
> | **200** | **1** (`big-pickle`) | réellement servi anonymement |
> | 401 `Missing API key` | 32 | réservé aux comptes authentifiés |
> | 400 `Model is unavailable` | 21 | annoncé, non servi |
> | timeout / erreur tunnel | 27 | **non conclus** — re-testés : ~2 s, 401 ou 400 |
>
> Les 27 « non conclus » n'ont **aucun** 200 après re-test : les timeouts venaient de la charge sur les tunnels, pas d'une indisponibilité. Conclusion : le catalogue dit ce que l'éditeur **propose**, la porte dit ce qu'il **sert**. Les 24 modèles `messages` (dont 17 Claude) répondent `401 Missing API key` même sur `/inference/anthropic/v1` avec un corps Anthropic valide — le chemin était bon, le modèle fermé.

### 3.4 Identité émise

| Jambe | En-têtes | Source |
|---|---|---|
| Free | `Authorization: Bearer public`, `Content-Type`, `UA`, `x-opencode-client/project/request/session`, `Connection`, `Accept`, `Host`, `Accept-Encoding` (11 + `Content-Length`) | `opencode.py:5389-5480` |
| Payée | UA v2, `x-opencode-client/project/session`, `x-session-affinity`, `X-Session-Id`, `x-opencode-session-id` | `opencode.py:5754-5820` |
| Geo/navigateur | **supprimé le 2026-10-25** : identité client officiel (UA v2 + Bun TLS) sur la jambe geo, plus de profil navigateur ni d'`extra_headers` | `opencode.py:_apply_identity`, `vpn/manager.py:_build_identity_pool` |

La jambe free **force** `stream: true` et injecte des outils « shim » (`read`, `bash`) dont les définitions imitent les outils natifs (`opencode.py:4983-5044`, `:5047+`).

### 3.5 Corps émis

- `max_completion_tokens` (hérité ai-sdk 1.18.31), pas `max_tokens`.
- `tool_choice: "auto"` **injecté** dès qu'il y a des outils (`:5092`, `:5114`), alors que l'officiel ne l'émet que sur demande.
- `store: true`, `stream_options.include_usage: true` : alignés.
- `reasoning_content` assistant : **perdu** à la conversion.

---

## 4. Mesures live (2026-10-20)

### 4.1 État de la jambe payante

| Sonde | Résultat |
|---|---|
| `GET /console/api/config` (clé du `.env`) | **401** `ApiKeyExpired` |
| `POST /inference/go/openai/v1/chat/completions` + clé invalide | **401** `Invalid credential` |
| Sans en-tête de session | **400** `MissingSessionID` |
| `x-opencode-session` **ou** `x-session-affinity` **ou** `X-Session-Id` | **401** (donc session acceptée → la clé est le seul blocage) |
| `x-opencode-session-id` seul | **400** `MissingSessionID` |
| `POST /zen/go/v1/...` (ancienne base) | **400** `MissingSessionID` (base encore vivante) |

### 4.2 État de la jambe gratuite

| Sonde | Résultat |
|---|---|
| `POST /inference/v1/chat/completions`, `Bearer public`, wire complet, corps minimal | **403** `FreeTierError` |
| idem + `stream:true` + `tools[read]` | idem **403** en direct |
| idem via tunnel SOCKS | **200** ✅ |
| wire complet + `stream:true` + `tools[bash]` seul | **403** |
| wire complet + `stream:true` sans `tools` | **403** |
| wire complet, corps minimal (sans stream ni tools) | **403** |
| wire **sans** `x-opencode-session` (et sans autre en-tête de session) | **403** |
| `Authorization` + `Content-Type` + `UA` seulement | **403** |
| **en-têtes exacts du client 1.19.3** (`x-opencode-session-id` + `x-session-affinity` + `x-session-id`, UA 1.19.3) | **200** ✅ |
| idem avec UA 1.18.31 | **200** ✅ |
| wire proxy 12 en-têtes + `stream+tools[read]` | **200** ✅ |

**Modèle de la porte, vérifié :** `IP_réputation ∧ (au moins un en-tête d'identité de session) ∧ (stream:true ∧ tools contient un « read » réaliste)`. Le **numéro de version de l'UA n'est pas un facteur**. Le commentaire `opencode.py:4966-4979` qui affirme `« UA 1.19.0 ou v2/prod/* → 403 »` est **falsifié** par cette mesure.

**Conséquence majeure :** les quatre en-têtes `x-opencode-client/project/request/session` **ne sont pas nécessaires**. La porte accepte le jeu du client 1.19.3 (200). La jambe free peut donc être alignée sur le wire officiel sans perte de capacité — la seule divergence qui reste obligatoire est le **corps** (shim `read` + `stream:true`).

### 4.3 Comportement runtime du proxy

| Mesure | Résultat |
|---|---|
| `GET /v1/models` | 200, 62 ids |
| `POST /v1/messages` (non-stream) | 200, 24,9 s (premier appel), puis 2,7–3,5 s |
| `POST /v1/chat/completions` free | 200, 2,7 s / 3,4 s / 11 s / 17,4 s |
| `POST /v1/messages` stream | 200 `text/event-stream`, séquence conforme (`message_start` → `content_block_*` → `message_delta` → `message_stop`), puis **dégradation** : `event: error` `{"type":"api_error","message":"upstream produced no output"}` en **HTTP 200** |
| Modèles free anthropic (`minimax-m2.5-free`, `minimax-m3-free`, `qwen3.6-plus-free`) | **404** `Model not found` (non exposés) |
| Boot | `Failed to fetch model limits from docs` · `Quota fetch failed … page format may have changed` · `[balance] all toggle actions failed` · `[docker-events] … Accès refusé` |
| `/health` de l'instance de production (`:4000`) | 200 · `upstream: "ok"` · clé de circuit breaker = `https://opencode.ai/inference/v1/chat/completions` (l'**ancienne** base, en dur) |
| Circuit breaker en production (09:37) | `[cb] closed → open (failures=5/5)` puis reprise — la free est instable en continu, pas seulement pendant l'audit |
| Suite de tests (167 fichiers) | **1 échec**, purement environnemental : `test_docker_throwaway_kill9_recovery` (pipe Docker inaccessible) |

### 4.4 Erreur de production reproduite

```
HTTP 400 — invalid_request_error
"Upstream request failed: The `reasoning_content` in the thinking mode must be passed back to the API."
```
sur `deepseek-v4.1-flash-free`, corps de 359 Ko, 24 outils. C'est exactement ce que l'officiel évite en réinjectant `reasoning_content` (`openai-chat.ts:257-260`).

---

## 5. Écarts P0 — cassent ou bloquent

> **Statut de remédiation :** cet audit décrit l'état **au 2026-10-20 avant travaux**. L'avancement réel, point par point et avec ses preuves, est tenu dans le **journal d'exécution** du plan ([`PLAN_ALIGNEMENT_2026-10-20.md`](PLAN_ALIGNEMENT_2026-10-20.md) §10). Ne pas agir sur un écart sans vérifier d'abord s'il est déjà coché là-bas.

### P0-1 — La jambe payante est hors service
`OPENCODE_API_KEY` du `.env` est expirée (401 `ApiKeyExpired`). Conséquences en cascade : plus de `/console/api/config` (donc plus de catalogue serveur), `[balance] all toggle actions failed`, quota Go illisible.
**Preuve :** sonde §4.1 ; `config.yaml:7-8` suppose une clé valide.
**Alignement :** aucun — c'est un incident d'exploitation. Mais il **bloque la mesure** de la moitié des écarts (voir plan V1).

### P0-2 — `reasoning_content` non réémis → **CORRIGÉ, énoncé précisé par la mesure**
**Preuve :** 400 live §4.4 ; officiel `openai-chat.ts:257-260`.
**Ce que la mesure a montré (et qui change l'énoncé) :** le proxy **ne perd pas** `reasoning_content` — un message assistant qui en porte un le transmet **tel quel** sur la jambe free (vérifié par sonde). Le défaut était plus étroit : le remplissage que DeepSeek exige (« chaque message assistant doit porter `reasoning_content`, même vide ») existait dans `app/protocol/mapping.py:3591` (`ensure_deepseek_reasoning`) mais **n'était pas appliqué au corps de la jambe free**. Les deux chemins avaient divergé : la conversion connaissait la règle, le wire free non.
**Effet réel :** toute conversation multi-tours sur un modèle `deepseek-*` free cassait au 2ᵉ tour dès que le client n'émettait pas lui-même le champ.
**Correctif appliqué :** `_free_wire_body` applique `ensure_deepseek_reasoning` (import paresseux, jamais bloquant). Vérifié en live : le 400 ne se reproduit plus (**200**), et le champ fourni par le client reste intact.

### P0-3 — La règle de routage ignore les overrides par modèle
Le proxy applique « `npm` de niveau provider → famille d'API » (`config/models_catalog.py:44-50`, `config/npm_routing.py:15-19`). L'officiel applique **`provider.npm` du modèle**, avec repli sur le provider.
**Effet mesuré :** 5 modèles sur le mauvais protocole, 10 modèles gemini non servis.
**Alignement :** la table doit devenir `modèle → (npm, api)` issue du catalogue, et la résolution doit accepter 4 protocoles.

### P0-4 — Deux résolveurs d'endpoint qui se contredisent
`config/settings.py:824-851` (config) vs littéraux en dur dans `opencode.py` (4 occurrences de `inference/v1`, `_RESPONSES_*` non configurables).
**Preuve :** §3.2 (log contradictoire), §3.1.
**Effet :** toute correction d'endpoint est appliquée à moitié — c'est un piège de régression, pas un simple défaut de style.

### P0-5 — Bases d'URL dépréciées
`upstream.openai_base` pointe `/zen/go/v1`, `free_base` pointe `/inference/v1`. Elles répondent encore, mais le catalogue officiel ne les mentionne plus.
**Risque :** disparition silencieuse, comme `/zen/v1` en octobre.
**Alignement :** basculer sur les bases par protocole (§2.5) après V1 (mesure).

---

## 6. Écarts P1 — divergence de comportement

### P1-1 — ~~`tool_choice: "auto"` injecté~~ → **ALIGNÉ (mesuré)**
Le proxy injecte `tool_choice: "auto"` dès qu'il y a des outils (`opencode.py:5092,5114`). La capture du client 1.19.3 (§2.3) montre exactement le même champ, dans la même condition. **Rien à corriger.** Le seul point résiduel : le proxy n'implémente pas le `tool_choice: "none"` du dernier step, qui n'existe que sur le chemin natif — hors périmètre.

### P1-2 — ~~`max_completion_tokens` vs `max_tokens`~~ → **NON-ÉCART (mesuré)**
La sonde du corps free réel (`_free_wire_body`) montre que le proxy émet **`max_tokens`**, dans l'ordre officiel `model, max_tokens, messages, tools, tool_choice, stream`. L'affirmation « le proxy émet `max_completion_tokens` » était fausse.
Ce qui existe réellement : une conversion **conditionnelle** dans `app/protocol/mapping.py:285` (`_wants_max_completion_tokens`) qui ne s'applique qu'aux modèles dont l'amont l'exige (famille o-series / gpt-5), avec un test dédié (`tests/test_max_completion_tokens.py`). C'est une adaptation à une contrainte amont documentée, pas une divergence de wire.
**Rien à corriger.**

### P1-3 — Budget de reprise surdimensionné et mal ciblé
Proxy : `upstream.max_retries: 4` (`config.yaml:21`), **8** sur la jambe free (`_free_bound`), rotation de station sur 403. Officiel : **2**, `retry-after` honoré, 4xx non rejoués.
**Effet :** amplification de charge et latences en escalier (17–25 s observés) sur des erreurs définitives.

### P1-4 — UA free figé sur une contrainte qui n'existe pas
`opencode.py:4719` + commentaire `:4969` qui affirme que l'UA 1.19.x serait rejeté. Mesure : l'UA **1.19.3 comme 1.18.31** passent (200). La contrainte est auto-imposée, et le commentaire impose une re-capture mitm à chaque release — une charge de maintenance sans bénéfice.
**Ce qu'il faut retenir de la mesure :** le bon UA pour le chemin *ai-sdk* est `opencode/1.19.3 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14`. Le proxy émet la bonne **forme**, avec une version périmée (1.18.31). Alignement = changer un numéro, risque nul.

### P1-5 — Modèle de porte free incomplet dans le code
Le code modélise la porte comme « UA + wire + corps » (`:4966-4982`) sans nommer le facteur **IP de sortie** comme un facteur à part entière, alors que c'est le seul qui explique les 403 résiduels et qui justifie l'existence de la flotte de tunnels. Il exige en outre des en-têtes (`x-opencode-client/project/request/session`) dont la mesure démontre qu'ils ne sont **pas** nécessaires.
**Effet :** impossible de diagnostiquer proprement un 403 (IP ? wire ? corps ?), et on porte une contrainte inutile.

### P1-6 — Flux terminés en erreur mais renvoyés en HTTP 200
`event: error` + `upstream produced no output` après un `200 OK`. Pour un client qui ne lit pas le SSE jusqu'au bout (non-stream reconstitué), cela produit une réponse vide « valide ».

### P1-7 — Scrapers cassés au boot
`model limits from docs` (table HTML changée) et `quota OpenCode Go` (page changée). Fonctions de confort, mais elles alimentent la tableau de bord et les limites de sortie : leur panne silencieuse dégrade la qualité de service sans alerte.

### P1-8 — Catalogue free sous-exposé
20 ids `-free` servis sur 43 disponibles. Notamment absents : `minimax-m2.5-free`, `minimax-m3-free`, `qwen3.6-plus-free` (les trois modèles free à protocole **anthropic** — précisément ceux que le proxy ne sait pas router), `glm-5-free`, `hy3-free`, `kimi-k2.5-free`, `nemotron-3-super-free`.

### P1-9 — Identité : quatre en-têtes que l'officiel n'envoie plus, et un qui manque
`x-opencode-client`, `x-opencode-project`, `x-opencode-request`, `x-opencode-session` sont émis sur la jambe payée (`:5754+`) **et** sur la jambe free. La mesure (§2.2) montre que le client 1.19.3 n'en envoie **aucun**, et la mesure (§4.2) montre que la porte free **les accepte absents** (200). Il manque en revanche `x-opencode-session-id` sur la jambe free du proxy, alors que c'est l'en-tête que le client officiel émet réellement.
**Effet :** divergence gratuite et charge de maintenance ; l'en-tête réellement émis par le client est absent.

---

## 7. Écarts P2 — cosmétiques ou théoriques

| # | Écart | Note |
|---|---|---|
| P2-1 | `Connection`/`Host`/`Accept-Encoding` posés explicitement pour imiter l'ordre du client | Fidélité pure ; `libcurl` replace `Connection` en dernier de toute façon (`test_free_wire_complet.py`) |
| P2-2 | `x-opencode-session` conservé | Vérifié non requis (l'officiel utilise `x-session-affinity`) |
| P2-3 | `promptCacheKey` non émis | L'officiel l'émet dans `providerOptions.openai` ; absent du proxy → pas de cache de prompt côté passerelle |
| P2-4 | Pas de `x-opencode-parent-session-id` / `x-parent-session-id` | Sous-sessions non modélisées dans le proxy |
| P2-5 | `qwen3.7-max` exposé comme modèle alors que c'est un alias | Devrait vivre en `custom_routes`, pas dans `models:` |
| P2-6 | `jev-1.13*` hors catalogue public | Exception interne documentée (`config/models_catalog.py:36`) — à garder |
| P2-7 | ~~`store: true` émis par le proxy~~ → **NON-ÉCART (mesuré)** | `grep '"store"' opencode.py` : aucune occurrence. Le proxy n'émet **jamais** `store` ; le client non plus (capture officielle). Les deux sont donc alignés par absence |
| P2-8 | Le client émet un appel annexe de **génération de titre** (3 messages, sans outils) | Non modélisé par le proxy ; sans impact sur la parité, mais à connaître pour lire les traces |

---

## 8. Ce qui est déjà aligné — ne pas « corriger »

- `stream: true` + `stream_options: {include_usage: true}` : conforme — `stream_options` est posé par les handlers de streaming (`opencode.py:12377,13774,15709`), pas par le constructeur de corps (qui ne fait que l'ordonner).
- `store` : **absent des deux côtés** (mesuré) — voir P2-7.
- `tool_choice: "auto"` quand il y a des outils : **conforme** (mesuré, §2.3).
- `max_tokens` (et non `max_completion_tokens`) sur le wire free : **conforme** (mesuré) — voir P1-2.
- Plafond de sortie par défaut 32 000 : **conforme** (mesuré).
- Ordre des outils préservé (pas de tri) : conforme depuis le commit `6392dd3`, et l'officiel ne trie pas non plus.
- `additionalProperties` non réécrit : conforme à `tool-schema.ts` (projection seulement sous `compatibility.toolSchema`).
- Cache Anthropic (`cache_control`) : le proxy ne fabrique pas de breakpoint, l'officiel non plus sur la voie compatible.
- Compte d'usage : `cache_read_input_tokens` / `cache_creation_input_tokens` remontés (vérifié dans les réponses non-stream).
- Conversion Anthropic↔OpenAI pour clients tiers : c'est la raison d'être du proxy, pas un écart.
- `Connection` / `Accept` / `Host` / `Accept-Encoding` dans l'ordre du client : **conforme à la capture** (§2.2).

---

## 9. Divergences volontaires — et l'analyse de la jambe gratuite

L'utilisateur a demandé explicitement de **garder la jambe gratuite** et de l'améliorer. Voici ce qui est structurellement divergent, et ce que valent ces divergences.

### 9.1 Inventaire des divergences assumées

| Divergence | Pourquoi elle existe | Verdict |
|---|---|---|
| Pool de tunnels SOCKS (stations) | le facteur « IP de sortie » de la porte (§4.2) | **load-bearing**, à garder et à industrialiser |
| Shim `read`/`bash` dans le corps | facteur « corps » de la porte | **load-bearing** pour `read` ; `bash` seul ne suffit pas |
| `stream: true` forcé | facteur « corps » | **load-bearing**, mais coûteux (reconstitution JSON pour les clients non-stream) |
| `Bearer public` | la jambe free est anonyme | conforme au client |
| Conversion Anthropic→OpenAI systématique sur la free | un seul base URL configuré | **contournable** : le catalogue expose `/inference/anthropic/v1` |
| Quartet `x-opencode-client/project/request/session` | croyance en une contrainte de porte | **inutile** — mesure : le wire 1.19.3 sans eux passe (200). À supprimer |
| UA v1 figé sur 1.18.31 | croyance en une contrainte UA | **faux** — 1.19.3 et 1.18.31 passent tous deux. Version à aligner |
| Rejeu TLS Bun officiel (`ja3` + sigalgs + H1, sans GREASE) | cohérence de l'empreinte | **load-bearing** : le ClientHello navigateur est remplacé par celui du client. `impersonate=chrome131` n'est qu'un porteur interne ; le warning `JA3 fingerprint was altered` est attendu (ja3 surchargé volontairement) |
| 8 reprises sur la free | rotation de stations sur 403 | à remplacer par une classification fine (403 ≠ 429 ≠ 5xx) |

### 9.2 Ce que la jambe free coûte aujourd'hui

| Symptôme | Preuve | Cause probable |
|---|---|---|
| Latences 2,7 → 24,9 s sur requêtes identiques | §4.3 | reprise en cascade + rotation de station + reconstruction de flux |
| `upstream produced no output` en 200 | §4.3 | flux amont vide non distingué d'un succès |
| 403 non diagnostiquables | §4.2 | modèle de porte incomplet (IP vs wire vs corps) |
| 23 modèles free non servis | §3.3 | table statique + protocole anthropic non routé sur la free |
| `reasoning_content` 400 | §4.4 | perte du champ au 2ᵉ tour |

### 9.3 Ce que la jambe free apporte

- Elle sert des modèles **gratuits** que l'officiel ne sert que dans son client.
- Elle fournit un **fallback** quand la jambe payante tombe (aujourd'hui : clé expirée → c'est la seule jambe vivante).
- Elle permet à des clients **hors OpenCode** (SDK Anthropic/OpenAI, Hermes, DSH) de consommer l'infrastructure — observé dans `logs/debug.log`.

**Conclusion :** la jambe free n'est pas une dette à résorber, c'est un **produit** à industrialiser. Le plan lui consacre un volet dédié (§3 du plan).

---

## 10. Dette de maintenabilité (mesurée)

| Métrique | Valeur | Commentaire |
|---|---|---|
| `opencode.py` | **1 003 790 octets**, 20 074 lignes | fichiers de 1 Mo : navigation, revue, tests unitaires difficiles |
| `app/protocol/mapping.py` | 267 Ko | idem |
| `dashboard/api.py` | 204 Ko | idem |
| `free/pool.py` | 134 Ko | cœur de la jambe free |
| Littéraux d'URL upstream | 40 + 23 dans le dépôt | source de divergence §3.2 |
| Résolveurs d'endpoint | ≥ 2 (config + littéraux) | contradiction prouvée |
| Commentaires de contrainte périmés | ex. `:4969` (UA) | falsifié par mesure |
| Tests | 167 fichiers, 1 échec environnemental | bonne base de non-régression |
| Docs d'audit antérieures | 193 Ko + 36 Ko, chemins de code morts | à archiver, pas à empiler |

---

## 11. Ce que le proxy fait et que l'officiel ne fait pas (à préserver)

1. **Conversion de protocole** Anthropic ↔ OpenAI ↔ Responses, y compris SSE.
2. **Alias et relances** (`custom_routes`, `free_model_map`) : un client qui demande un modèle payant peut être servi par un free.
3. **Web search / web fetch** natifs.
4. **Compaction** de contexte (pipeline router/transport/summarizer).
5. **Rotation de clés, quotas, tableau de bord, historique SQLite**.
6. **Flotte VPN + stations** avec rotation d'identité.
7. **Découverte de modèles** free et persistance.

Aucun de ces éléments ne doit être supprimé au nom de l'alignement : ils constituent la valeur ajoutée du proxy. L'alignement porte sur le **fil** et sur **les décisions de routage**, pas sur le périmètre fonctionnel.

---

## 12. Registre de preuves (reproductible)

| # | Commande | Attendu |
|---|---|---|
| 1 | `node _research/fetch_models.js` | 231 providers, `opencode` 128 modèles, `opencode-go` 33 |
| 2 | `node _research/fetch_remote_config.js` | `401 ApiKeyExpired` (clé morte) |
| 3 | `python _research/compare_routing.py` | 46/128, 5 mismatches, 10 gemini non routables |
| 4 | `python _research/free_catalog_gap.py` | 43 free au catalogue, 20 exposés |
| 5 | `python _research/gate_direct_vs_tunnel.py` | direct 403 / tunnel 200 ; `bash` seul 403 |
| 6 | `python _research/gate_headers_need.py` | sans `x-opencode-session` → 403 |
| 7 | `python _research/gate_raw_test.py` | UA v2 + corps minimal → 403 ; UA v2 + stream+tools[read] → 200 |
| 8 | `node _research/session_header_test.js` | `x-session-affinity`/`X-Session-Id` satisfont la passerelle, `x-opencode-session-id` non |
| 9 | `python -m pytest tests -q` | 1 échec (Docker), reste OK |
| 11 | `node _research/dl_client.js` puis `Expand-Archive _research/oc-1.19.3/opencode-windows-x64.zip` | binaire 1.19.3 (59,3 Mo) ; `opencode.exe --version` → `1.19.3` |
| 12 | `node _research/capture_server.js` + `opencode run --model capture/test "hi"` (avec `OPENCODE_CONFIG=_research/oc-1.19.3/oc.json`) | capture du wire réel : UA `opencode/1.19.3 …`, `max_tokens: 32000`, `tool_choice: "auto"`, 10 outils |
| 13 | `node _research/analyze_capture2.js` | en-têtes ordonnés + clés du corps + noms d'outils |
| 14 | `python _research/gate_official_headers.py` | jeu d'en-têtes officiel 1.19.3 → **200** (le quartet est inutile) |

> Note d'installation : l'installation **globale** (`npm install -g opencode-ai@1.19.3`) n'a pas pu aboutir dans le bac à sable (`EPERM` sur `%APPDATA%\npm` et le cache npm). Le binaire officiel 1.19.3 a donc été installé **dans l'espace de travail** (`_research/oc-1.19.3/bin/opencode.exe`, `XDG_*` redirigés) ; il s'exécute et répond `1.19.3`. Le desktop officiel 2.0.32 est déjà installé et à jour au niveau machine.

---

*Fin de l'audit. Plan d'exécution : [`PLAN_ALIGNEMENT_2026-10-20.md`](PLAN_ALIGNEMENT_2026-10-20.md).*
