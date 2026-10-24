# PLAN — Alignement du proxy sur le client officiel OpenCode

> Date : 2026-10-09 · Auteur : analyse comparative proxy ↔ client officiel
> Statut : **plan de corrections, rien d'appliqué**

---

## 1. Méthode et sources de vérité

| Source | État |
|---|---|
| Client officiel `sst/opencode` | cloné HEAD `3884062`, version **1.18.35** (`@ai-sdk/provider-utils` 4.0.51, `@ai-sdk/openai` 3.0.88, `@ai-sdk/anthropic` 3.0.111, `@ai-sdk/openai-compatible` 2.0.41) |
| Catalogue modèles **live** | `https://models.opencode.ai/api.json` récupéré le 2026-10-09 — provider `opencode-go` (`https://opencode.ai/zen/go/v1`, **36 modèles**) et `opencode` (`https://opencode.ai/zen/v1`, **120 modèles**) |
| Doc officielle Go | `packages/web/src/content/docs/go.mdx` — table modèles lignes 415-448, règles session/UA lignes 121-123 |

**La règle de routage officielle est dérivée du catalogue, pas codée en dur** —
`packages/core/src/session/runner/model.ts:141-162` :

| `provider.npm` du catalogue | endpoint officiel |
|---|---|
| `@ai-sdk/anthropic` | `/v1/messages` |
| `@ai-sdk/openai` | `/v1/responses` |
| `@ai-sdk/openai-compatible` | `/v1/chat/completions` |

Notre proxy, lui, déduit endpoint + protocole d'une **table statique** `config.yaml`
(`config/settings.py:822-861`) avec repli sur la source unique
`_resolve_model_endpoint` (`config/settings.py:1557-1581`, anciennement le littéral
`{API_BASE_OPENAI, "openai"}` en `:1522-1527`, corrigé par la carte N4). C'est la
cause racine de tous les écarts P0 ci-dessous.

### Photographie V0 — capture du 2026-10-09 (lecture seule)

Produite par `scripts/_probe_catalog_diff.py` (n'écrit rien : ni `config.yaml`,
ni base). Sortie console, à rejouer pour rafraîchir.

| Mesure | Valeur |
|---|---|
| Providers dans `api.json` | **225** — nous n'en utilisons que 2 |
| `opencode-go` (`/zen/go/v1`) | 36 modèles (3 `-free`), npm racine `@ai-sdk/openai-compatible` |
| `opencode` (`/zen/v1`) | 120 modèles (36 `-free`), npm racine `@ai-sdk/openai-compatible` |
| Ids de `config.yaml` confortés à **leur** jambe | **34** (sur 59 entrées `models:`) |
| Ids **hors** de leur jambe | **12** (11 Go + 1 free) |
| Ids divergents (endpoint effectif ≠ catalogue) | **8** — exactement les cibles de la V1 |
| Ids `deprecated` | **5** |
| Catalogue → `config.yaml` | `opencode-go` **0/36** absent · `opencode` **80/120** absent |

Deux règles de lecture, sans lesquelles le diff ment (quatre classes de faux
positifs éliminées) :

1. **Jambe par jambe** : un id sans `-free` se confronte à `opencode-go`, un id
   suffixé à `opencode`. Comparer chaque id aux deux providers faisait apparaître
   `minimax-m2.7`, `minimax-m3`, `qwen3.7-plus`, `qwen3.8-flash`, `qwen3.8-max`
   comme divergents alors qu'ils sont corrects.
2. **Sur l'endpoint effectif**, pas sur les clefs : sur la jambe payée,
   `api: messages` et `protocol: anthropic` désignent le **même** endpoint (§3).

État de foi du remplacement (§14.1) : `npm` du modèle s'il en porte un, sinon
celui du provider ; npm hors de la table `@ai-sdk/anthropic` / `@ai-sdk/openai` /
`@ai-sdk/openai-compatible` → endpoint **indéductible** (9 modèles, écart N3).

#### Re-vérification des ancrages (3ᵉ case de la V0)

L'arbre est modifié non commité : les lignes bougent, donc tout ancrage cité au
§18 a été relu sur l'arbre **courant**. Résultat : **tous tiennent**, sauf deux
corrections de lignes, reportées dans les cartes concernées.

| Ancre | Cité | Relevé | État |
|---|---|---|---|
| `_set_output_token_limit` (E2) | `mapping.py:319/322/368/376` | idem | ✅ |
| `get_model_api` | `config/settings.py:693` | idem | ✅ |
| `_RESPONSES_ENDPOINT` | `config/settings.py:818` | idem | ✅ |
| `_resolve_model_endpoint` | `config/settings.py:822-849` | idem | ✅ |
| `get_model_config` (repli via source unique, B1/B2/N4) | `config/settings.py:1557` | idem (corrigé depuis `:1523`) | ✅ |
| lecture `routing.*_model` (B1) | `config/settings.py:1128-1140` | 1131/1135/1139 | ✅ |
| entrée `claude-haiku-5-5` (B2) | `config.yaml:161-162` | idem | ✅ |
| `qwen3.6-plus` / `qwen3.7-max` (A2) | `config.yaml:59-60`, `:63-64` | idem | ✅ |
| `minimax-m2.5` + `routing.haiku_model` (A3) | `config.yaml:57-58`, `:25` | idem | ✅ |
| grok/luna (A1) | `config.yaml:81, 105, 143, 147` | idem | ✅ |
| `retry_attempts` (G2) | `config.yaml:505` | `:507` | ⚠️ corrigé §9 — doublé de `upstream.max_retries: 2` (nouvelle clé, même valeur) |
| `effort_caps` (C1) | `config.yaml:519-532` | `:521-534` | ⚠️ corrigé §5 |

Deux fichiers de test cités n'existent pas (« `test_free_model_map.py` »,
« `test_web_search_native.py` » dans la carte N2) : remplacés par le vrai nom
(`tests/test_free_model_map_http.py`) et, là où le test n'existe pas, marqués
**à créer** plutôt que cités à tort.

---

## 2. Synthèse des écarts

| # | Écart | Sévérité | Lot |
|---|---|---|---|
| A1 | `grok-4.5/4.6/4.7`, `gpt-5.6-luna`, `gpt-6-luna` routés en `/chat/completions` alors que le client officiel utilise `/responses` | **P0** | A |
| A2 | `qwen3.6-plus`, `qwen3.7-max` routés en protocole Anthropic `/messages` alors que le catalogue dit `@ai-sdk/openai-compatible` → `/chat/completions` | **P0** | A |
| A3 | `minimax-m2.5` (route **haiku** par défaut) a quitté le catalogue Go et est passé en `@ai-sdk/openai-compatible` | **P0** | A/B |
| B1 | `sonnet` → `glm-5.1` et `haiku` → `minimax-m2.5` ne sont **plus dans le plan Go** (36 modèles) ; seul `opus` → `kimi-k2.6` l'est encore | **P0** | B |
| B2 | `claude-haiku-5-5` (seul modèle Go absent de notre config) retomberait sur le défaut OpenAI | **P0** | B |
| C1 | Effort : nous n'avons qu'un **plafond** par modèle, le client officiel a une **allow-list** de valeurs (`reasoning_options`) → niveaux interdits envoyables, niveaux officiels inatteignables | P1 | C |
| C2 | `anthropic-beta: thinking-binding-controls` alors que l'officiel envoie `thinking-binding-controls-2026-08-01` | P1 | D |
| D1 | UA figée sur `opencode/1.18.31 … provider-utils/4.0.23` (officiel 1.18.35 / 4.0.51) | P1 | D |
| D2 | Jambe payante : `x-opencode-session-id` jamais envoyé (officiel l'envoie sur toutes les requêtes) | P2 | D |
| E1 | `tool_choice: "auto"` émis systématiquement ; l'officiel l'omet quand le client n'en demande pas | P2 | E |
| E2 | `max_tokens` : défaut figé à 16384 (`mapping.py:322`) et **aucun plafond** ; l'officiel envoie `min(model.limit.output, 32 000)` sur les trois protocoles (`transform.ts:18,1481` → `anthropic-messages.ts:546`, `openai-chat.ts:361`, `openai-responses.ts:493`) | **P1** | E |
| E3 | Outils transmis dans l'ordre client ; l'officiel les **trie** par nom (déterminisme + préfixe cache) | P2 | E |
| F1 | Événement `error` d'un flux OpenAI **avalé silencieusement** sur P1oai et Chat-passthrough | P1 | F — **fait 2026-10-10** (hors vérif live) |
| F2 | `cache_read_input_tokens` du `message_delta` non compté sur la jambe P1 Anthropic | P2 | F — **fait 2026-10-10** (hors vérif live) |
| G1 | Aucun classement des `error.code` amont (`context_length_exceeded`, `insufficient_quota`, …) | P2 | G — **fait 2026-10-10** (hors vérif live) |
| G2 | Budget de retries 2 vs 5 chez l'officiel | P2 | G — **fait 2026-10-10** (hors vérif live) : budget exposé en config, défaut inchangé |
| H1 | 12 modèles de `config.yaml` sont hors de leur jambe de catalogue, dont 5 `deprecated` ; aucun rafraîchissement du catalogue | P2 | H | **fait** — job lecture seule + 5 morts purgés, preuve de neutralité ; purge durable au restart `pythonw` |

**Écarts découverts par la photographie V0 (2026-10-09)** — absents de l'analyse
initiale, classés au §14.6 :

| # | Écart | Sévérité | Lot |
|---|---|---|---|
| **N1** | Les **trois** routes par défaut tapent des ids morts en payant : `glm-5.1` (`sonnet`) et `minimax-m2.5` (`haiku`) sont absents des 36 modèles `opencode-go` (ils n'existent que sur la jambe anonyme), `kimi-k2.6` (`opus`) y est `deprecated` | **P0** | B |
| **N2** | `free_model_map` envoie **6 ids payants** vers `mimo-v2.5-free`, désormais `deprecated` sur `opencode` ; `muse-spark-1.2-contributor-free` (utilisé par `web_search_native`) aussi | P1 | H |
| **N3** | 9 modèles du catalogue ont un `npm` hors de notre table de correspondance (`@ai-sdk/google` ×8, `@ai-sdk/mistral` ×1) → endpoint indéductible par notre règle de routage | P2 | H |
| **N4** | 41 modèles de la jambe anonyme atteindraient le **mauvais** endpoint faute d'entrée dans `config.yaml` (25 en `/responses`, 16 en `/messages`) : le repli implicite les envoie sur `/zen/go/v1/chat/completions` | P2 | B/H |

Faits établis par la V0 (dernière trace de trafic payant : tous les `minimax-m2.5`,
`glm-5.1`, `kimi-k2.6` des 48 h ont répondu **401**, ce qui masque toute erreur
d'existence de modèle ; les 48 h de trafic réel sont **100 % jambe libre** —
`muse-spark-1.3-contributor-free` 21 988 requêtes, `jev-1.13-free` 19 636,
`step-5-preview-free` 716, `mimo-v2.6-flash-free` 1).

---

## 3. Lot A — Catalogue & endpoints (P0)

### A1 — Cinq modèles doivent passer en `/responses`

**Constat** (catalogue Go live) :

| Modèle | `provider.npm` officiel | endpoint officiel | chez nous |
|---|---|---|---|
| `grok-4.5` | `@ai-sdk/openai` | `/responses` | `/chat/completions` (`config.yaml:81`) |
| `grok-4.6` | `@ai-sdk/openai` | `/responses` | `/chat/completions` (`config.yaml:105`) |
| `grok-4.7` | `@ai-sdk/openai` | `/responses` | `/chat/completions` (`config.yaml:143`) |
| `gpt-5.6-luna` | `@ai-sdk/openai` | `/responses` | `/chat/completions` (`config.yaml:79`) |
| `gpt-6-luna` | `@ai-sdk/openai` | `/responses` | `/chat/completions` (`config.yaml:147`) |

**Correction** : ajouter `api: responses` à ces 5 entrées — **aucun code nouveau**,
le mécanisme existe déjà (`get_model_api` → `_RESPONSES_ENDPOINT`,
`config/settings.py:687-712`, `814-819`). C'est ce que fait déjà `muse-spark-*`.

**Risque** : le corps part alors dans le convertisseur Responses
(`app/protocol/mapping.py:4342-4740`) qui a ses propres limitations connues
(faux-streaming §14.1.13 *du plan d'audit v10*, erreurs indistinguables §14.1.14 *idem*, `tool_choice` limité à
la chaîne `"auto"`). Mitigation : canary sur un modèle avant de basculer les 5.

### A2 — `qwen3.6-plus` et `qwen3.7-max` doivent repasser en OpenAI

Catalogue Go : `@ai-sdk/openai-compatible` → `/chat/completions`.
Nous : `protocol: anthropic` → `/v1/messages` (`config.yaml:59-64`).
`qwen3.7-plus`, `qwen3.8-max`, `qwen3.8-flash` restent bien en Anthropic (vérifié :
`@ai-sdk/anthropic`), **ne pas y toucher**.

**Correction** : `protocol: openai` sur les deux entrées.
**Risque** : la jambe Anthropic-relver était un passthrough (zéro conversion) ;
la nouvelle jambe passe par `anthropic_to_openai` — plus de surface, mais chemin
déjà massivement testé (`tests/test_e2e_protocol_matrix.py`).

### A3 — `minimax-m2.5` a changé de protocole ET de catalogue

Le catalogue **Go** ne contient plus `minimax-m2.5` ; il n'existe plus que dans le
provider `opencode` (`/zen/v1`, `@ai-sdk/openai-compatible`). Notre config le route
en `protocol: anthropic` → `/zen/go/v1/messages` (`config.yaml:57-58`).
`minimax-m2.7` et `minimax-m3` restent `@ai-sdk/anthropic` sur Go → **inchangés**.

**Correction** : voir lot B (décision de route), pas un simple changement de
protocole — le modèle n'est de toute façon plus dans le plan Go.

---

## 4. Lot B — Routes par défaut `opus` / `sonnet` / `haiku` (P0)

`config.yaml:23-25` → `config/settings.py:1128-1140`.

| Route | Modèle actuel | Dans le catalogue Go ? |
|---|---|---|
| opus | `kimi-k2.6` | ⚠️ présent (`/chat/completions`) mais **`deprecated`** |
| sonnet | `glm-5.1` | ❌ **non** — seulement dans `opencode` (`/zen/v1`) |
| haiku | `minimax-m2.5` | ❌ **non** — seulement dans `opencode` |

Les **trois** routes par défaut sont donc mortes ou mourantes en payant (N1,
§14.6) : deux modèles n'existent plus sur le plan Go, le troisième est déprécié.
C'est un durcissement de B1 par rapport à l'analyse initiale — la photographie V0
a comparé chaque id à **sa** jambe, ce que la première lecture ne faisait pas.

**Porte d'entrée** : il faut d'abord vérifier en live si
`https://opencode.ai/zen/go/v1/{chat/completions,messages}` accepte encore
`glm-5.1` / `minimax-m2.5` / `kimi-k2.6` (le catalogue peut être en avance ou en
retard sur le gateway). Ne jamais basculer à l'aveugle. La dernière trace de
trafic payant des 48 h indique **401** sur les trois modèles (clé absente), ce qui
ne prouve rien sur leur existence côté gateway : le test live reste entier.

**Candidats de remplacement, tous dans le catalogue Go :**

| Route | Candidat | endpoint officiel | ctx / out |
|---|---|---|---|
| sonnet | `glm-5.3` | `/chat/completions` | 1 000 000 / 131 072 |
| sonnet (alt.) | `glm-5.2` | `/chat/completions` | 1 000 000 / 131 072 |
| haiku | `claude-haiku-5-5` | `/messages` | 1 000 000 / 128 000 |

`claude-haiku-5-5` est le choix naturel pour la route haiku : c'est le **seul modèle
du catalogue Go absent de `config.yaml`** (B2), son protocole est Anthropic — la
jambe passthrough de Claude Code reste donc identique — et la correspondance
sémantique avec l'alias `haiku` de Claude Code est exacte.

**Corrections B2** : ajouter `claude-haiku-5-5: {protocol: anthropic}` sous
`models:` — sinon un client qui demande ce nom retombe sur le défaut de la source
unique (`config/settings.py:1557-1581`, anciennement `:1522-1527`) et tape
`/chat/completions`.

**Non-régression** : `tests/test_go_only_routing.py`, `tests/test_model_catalog.py`.

---

## 5. Lot C — Effort / reasoning (P1)

### C1 — Plafond seul vs allow-list officielle

Le client officiel dérive les variantes d'effort du champ `reasoning_options` du
catalogue (`packages/opencode/src/provider/transform.ts:1717-1762`), puis traduit :

- `@ai-sdk/openai-compatible` → `{reasoningEffort}` (`transform.ts:1829-1839`)
- `@ai-sdk/anthropic` → `thinking`/`effort` selon le modèle (`transform.ts:1852-1864`)
- budget → `{thinking:{type:"enabled", budgetTokens}}`, plafonné à
  `min(max_catalogue, limit.output - 1, 31999)` (`transform.ts:1749-1751`, `1876-1882`)

Nous n'avons qu'un plafond (`config/effort_caps.py` + `config.yaml:521-534`,
défaut `high`). Deux erreurs symétriques en découlent :

| Modèle | Liste officielle | Notre comportement |
|---|---|---|
| `deepseek-v4-pro` | `high`, `max` | `medium`/`low` envoyables → 400 probable |
| `glm-5.3`, `glm-5.3-flash` | `low`, `high`, `max` | plafond `high` → `max` inatteignable |
| `kimi-k3` | `max` uniquement | plafond `high` → `high` envoyé, hors liste |
| `muse-spark-1.2/1.3` | `minimal`…`xhigh` | plafond `max` → `max` envoyé, hors liste |
| `space-bunny` | `low`,`medium`,`high`,`xhigh`,`max` | plafond `high` → `xhigh`/`max` inatteignables |
| `kimi-k2.6`, `minimax-m2.7` | `[]` (aucune variante) | `high` envoyé par défaut |
| `hy4-preview` | `none`, `high` | `low`/`medium` envoyables |
| `hy3` | `none`, `low`, `high` | `medium` envoyable |

**Correction** : introduire une table `reasoning_options` par modèle dans
`config.yaml` (clé = allow-list + `budget_tokens.max` optionnel), lue par
`config/effort_policy.py`. Nouvelle sémantique de clamp :
`niveau_émis = plus grande valeur ≤ demandée dans l'allow-list` (repli sur la plus
petite valeur autorisée), `none`/`off` inchangés. Les `effort_caps` existants
restent comme filet de compatibilité (intersection des deux) — **à lire avec la
précision « Implémenté 2026-10-10 » plus bas : ce n'est pas l'intersection
stricte, l'ordre des deux clamps change le résultat sur `kimi-k3`**.

**Non-régression** : `tests/test_effort_caps.py`, `tests/test_effort_policy.py`,
`tests/test_thinking_effort_mapping.py`.

**Implémenté 2026-10-10 — précisions sur la sémantique exacte.** Fait dans cet
ordre : `clamp_level` (plafond local) **puis** allow-list, et non l'intersection
stricte de la table ci-dessus, parce que les deux ordres ne se valent pas sur un
cas du §5. `kimi-k3` ne catalogue que `max` ; notre plafond vaut `high`. En
intersection stricte on repart avec `high`, hors liste, donc 400 — la divergence
qu'on veut justement corriger. Le clamp appliqué est donc :

    plafonné = min(demandé, effort_caps)      # filet local, inchangé
    émis     = plus grande autorisée ≤ plafonné
               (repli : la plus petite autorisée, même si elle dépasse le plafond)

L'allow-list ne monte donc **jamais** au-dessus du plafond local quand elle
contient un niveau ≤ ce plafond (cas `muse-spark`, plafond `high` → reste `high`)
; elle ne le dépasse que quand l'intersection est vide, c'est-à-dire quand s'y
tenir garantirait le 400 (`kimi-k3`). Une entrée `[]` (`kimi-k2.6`,
`minimax-m2.7`) est traitée comme **absence d'information** — pas de clamp, le
plafond local reste seul juge ; désactiver le raisonnement est une décision trop
forte et trop silencieuse pour la fonder sur une liste vide ambiguë. À revoir en
vérif live si un vrai refus le contredit. Le tout est derrière
`thinking.allow_list_enabled` (défaut `false`) ; à `false`, `resolve_effort` est
strictement celui d'avant, tests différentiels à l'appui.
Le `budget_tokens.max` du client officiel (`min(max_catalogue, limit.output - 1,
31999)`) **reste à faire** : il dépend de la table `limit.output` de la V3, donc
d'une vague suivante.

### C2 — Libellé du beta Anthropic

Nous envoyions `thinking-binding-controls` (`opencode.py:344`, avant correctif) ; l'officiel envoie
`thinking-binding-controls-2026-08-01` (patch `@ai-sdk%2Fanthropic@3.0.111.patch:133`,
attesté par `transform.test.ts:1105`). Un beta sans date risque un 400 côté gateway.
**Corrigé 2026-10-09** : constante désormais en `opencode.py:350`.

**Correction** : constante + `tests/test_opencode_client_parity_helpers.py:282-290`.
**Risque** : si le gateway accepte aujourd'hui la forme non datée, la corriger ne
casse rien ; si un modèle ancien exigeait l'ancienne forme, prévoir une émission
des deux, séparées par virgule (l'union est déjà le comportement de
`_ensure_anthropic_beta`, `opencode.py:358-404`).

---

## 6. Lot D — Identité filaire (P1/P2)

### D1 — User-Agent périmé

Nous : `opencode/1.18.31 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14`
(`opencode.py:4682-4699`, avec variante `4.0.40` pour `/responses`).
Officiel : **1.18.35 / provider-utils 4.0.51**.

**Correction** : mettre à jour les deux constantes **après re-mesure** sur le vrai
client 1.18.35 (`scripts/_probe_official_vs_proxy.py`,
`scripts/_probe_useragents.py` existent déjà). La doc Go dit explicitement
« identify itself with its own user agent » (`go.mdx:121-122`) : garder la mimétique
est un choix déjà pris, mais il doit être à jour — une version en retard de 4
releases est une signature trivialement détectable.

### D2 — `x-opencode-session-id` absent de la jambe payante

Officiel (v1, chemin par défaut) : `x-opencode-session-id` sur **toutes** les
requêtes + `x-opencode-parent-session-id` si sous-session
(`packages/opencode/src/session/llm/request.ts:188-189`), et pour les providers
`opencode*` en plus : `x-opencode-project`, `x-opencode-session`,
`x-opencode-request`, `x-opencode-client`, `User-Agent` (`request.ts:190-197`).

Nous (`_enrich_paid_wire_headers`, `opencode.py:5407-5465`) : `x-opencode-client`,
`x-opencode-project`, `x-opencode-session`, `x-opencode-request`, `User-Agent` —
**il manque `x-opencode-session-id`**.

**Correction** : ajouter `x-opencode-session-id` = même valeur que
`x-opencode-session` (c'est la sémantique officielle). Sur la jambe free, **ne rien
changer sans re-mesure** : la capture 243 requêtes du client 1.18.31 verrouille le
fil complet à 11 en-têtes ordonnés (`EXPECTED_ORDER`,
`tests/test_official_client_parity.py:70-85`), dont le bloc `x-opencode-*` des
7 premiers.

> **Statut 2026-10-10 — implémenté (vague V5, jambe payante seule).** Bloc
> `setdefault` ajouté dans `_enrich_paid_wire_headers` (`opencode.py:5448-5459`) :
> `x-opencode-session-id` recopie la valeur de `x-opencode-session` — **deux noms,
> un sessionID** (`request.ts:188-189` l'émet avant les `opencode*`, `:190-197`).
> Le retour anticipé existant de la fonction (« ni `/responses` ni
> `/chat/completions` ») borne déjà le lot à la jambe payante OpenAI-protocol :
> chemin Anthropic et jambe free exclus sans code supplémentaire. Si le `ses_` n'a
> pas pu être minté, l'en-tête est **absent** plutôt qu'orphelin. 8 tests ajoutés à
> `tests/test_paid_wire_identity.py` (15 au total), tous verts, plus les voisins du
> fil free. **Reste ouvert** : `x-opencode-parent-session-id` (sous-sessions) — non
> implémenté, le proxy n'a pas de notion de session parente, et la jambe free
> reste sous moratoire de re-mesure. Voir carte §18 D2.

---

## 7. Lot E — Corps de requête (P2)

### E1 — `tool_choice` émis même sans demande client

Officiel : `tool_choice` n'est envoyé que si le client en a mis un
(`packages/llm/src/protocols/openai-chat.ts:348`). Nous : `tc = body.get("tool_choice", "auto")`
puis émission systématique dans le bloc `if "tools" in body`
(`app/protocol/mapping.py:1757-1799`).

**Correction** : n'émettre `tool_choice` que si la clé existait dans le corps client.
**Risque** : certains gateways changent de comportement sans le champ — d'où un
flag de retour arrière (`protocol.emit_default_tool_choice`, défaut `false`).

### E2 — Défaut de `max_tokens`

Officiel : `generation.maxTokens ?? limit.output` (`anthropic-messages.ts:506-553`,
`openai-chat.ts:344-370`). Nous : défaut 16384 (`mapping.py:319-377`).
Impact faible (Claude Code envoie toujours `max_tokens`), mais à aligner via une
table `output_limit` dérivée du catalogue — même table que le lot H.

> **Statut 2026-10-09 — implémenté (vague V3).** Table `models.<id>.output_limit`
> peuplée (51/59 entrées, relevé live du 2026-10-09) et clamp câblé dans
> `_set_output_token_limit` : `limit = min(limit, output_limit)` après résolution
> client/16384, avant la branche `max_completion_tokens`. **Le plafond dur à
> 32 000 du client officiel n'est pas repris** — §14.3 et carte §18 E2. Détail,
> démonstration d'inertie et rollback dans la carte V3 (§17).

### E3 — Ordre des outils

Officiel : outils triées par nom (`request.ts:184`, `toSorted(localeCompare)`).
Nous : ordre client préservé. Un ordre non déterministe casse le préfixe de cache
et donc le taux de `cache_read_input_tokens`. Correction triviale et sans risque :
tri sur `function.name` dans `_sanitize_chat_tools` (`mapping.py:3760-3804`).

---

## 8. Lot F — Streaming et comptage (P1/P2)

### F1 — Événement `error` amont avalé (P1)

Un flux OpenAI qui échoue en cours de génération émet `data: {"error": {...}}`.
Sur P1oai (`opencode.py:13605-13614`) et sur le passthrough Chat
(`opencode.py:15579-15602`), cette frame est **silencieusement ignorée** : le client
ne reçoit qu'un `stop_reason: "error"` synthétisé à l'EOF, sans message ni code.
Officiel : `providerError` typé, y compris la classification `context-overflow`
(`anthropic-messages.ts:795-812`, `openai-responses.ts:891-921`).

**Correction** : sur réception d'une frame `error`, propager l'enveloppe Anthropic
`{"type":"error","error":{"type":"api_error","message":…}}` (l'infrastructure existe :
`_stream_error_response`, `opencode.py:9651`) et classer le `code` (lot G1).
**Non-régression** : `tests/test_streaming_sse.py`,
`tests/test_responses_stream_contract.py`.

### F2 — `cache_read_input_tokens` du `message_delta` non compté (P2)

Sur la jambe P1 (relais Anthropic), `stream_cache` n'est lu qu'au `message_start`
(`opencode.py:12268-12275`), alors que le convertisseur émet aussi
`cache_read_input_tokens` au `message_delta` (`chat_sse_to_anthropic.py`
`finalize_events`). Même oubli sur la boucle de conversion P4.
Résultat : écart entre ce que le client voit et ce que le dashboard/DB comptent.
**Correction** : cumuler les deux sources (le `message_delta` écrasant le
`message_start` s'il est présent). **Fait 2026-10-10** — helper partagé
`opencode._stream_cache_delta` (écrasement par la **différence**, jamais
addition : un `usage` cumulatif ne doit pas être compté deux fois), voir §18.

---

## 9. Lot G — Erreurs et reprises (P2)

### G1 — Classement des codes d'erreur

Officiel lit le corps pour classer : `context_length_exceeded` → overflow (non
retry), `insufficient_quota`/`usage_not_included`/`invalid_prompt` → non retry,
`server_is_overloaded`/`server_error` → retry
(`packages/opencode/src/provider/error.ts:102-154`, bibliothèque de motifs
`packages/llm/src/provider-error.ts:4-38`).

Nous : seuls 429/529 sont typés (`rate_limit_error`/`overloaded_error`), tout le
reste devient `api_error` (`opencode.py:4467-4497`).

**Correction** : mapper au minimum `context_length_exceeded` →
`invalid_request_error` (Claude Code déclenche alors sa propre compaction) et
`insufficient_quota` → pause clé longue plutôt que retry. Le proxy a déjà un module
compaction (`app/compaction/`) : il faut lui laisser la priorité côté client quand
l'amont est explicite.

**Fait 2026-10-10** (`core/errors.py:165-273`, câblé dans `opencode.py`) : table de
8 motifs ⇒ type d'erreur, rejeu et pause de clé lus dans le **corps**, plus le
**statut jamais réécrit** par le classement. Les 4 sites `except UpstreamError`
(relais payants), l'enveloppe Anthropic forgée à la main du relais OpenAI et les
8 sites de pause crédit — jusque-là 8 copies en dur du même teste — passent par la
table unique. Un statut ambigu `{400,404,408,409,422,425}` dont le corps déclare
une surcharge se rejoue (borné), jamais sur 401/403/429/499 pour ne pas
contrecarrer le failover de clé. Détail et déviation documentés au §18.

### G2 — Budget de reprises

Officiel : 5 tentatives, base 2 s, ×2, cap 30 s sans en-tête, `retry-after`
honoré (`packages/opencode/src/session/retry.ts:26-31`). Nous : 2 tentatives
(`config.yaml:507`), base 1 s, cap 600 s.
Le proxy compense par du failover de clé, du hedging et un TTFB watchdog — c'est
une divergence **assumée**, pas un défaut. Action : exposer le budget en config
(`upstream.max_retries`) sans changer le défaut, pour pouvoir monter à 3-4 si les
taux de 502 le justifient.

---

## 10. Lot I — Particularités de la jambe gratuite (contraintes dures)

La jambe free **n'a pas d'équivalent dans le client officiel** : celui-ci exige
toujours `OPENCODE_API_KEY`. Tout ce qui suit est donc du patrimoine proxy à
préserver, et il contraint plusieurs lots ci-dessus.

### I1 — Le catalogue de référence de la jambe free est `opencode` (`/zen/v1`), pas `opencode-go`

Nos modèles `-free` résolvent vers `API_BASE_FREE = https://opencode.ai/zen/v1/chat/completions`
(`config/settings.py:396-399`, `822-849`, clé de sélection = suffixe `-free`).
La comparaison lot A doit donc se faire contre le provider **`opencode`**
(120 modèles), jamais contre `opencode-go`.

Vérification faite le 2026-10-09 sur les 19 modèles que nous routons en anonyme :

| Constat | Détail |
|---|---|
| Alignés | 16 modèles « zen seulement » → tous en `/chat/completions` (`@ai-sdk/openai-compatible`), comme chez nous. `muse-spark-1.2/1.3-contributor-free` → `/responses`, comme chez nous. |
| `minimax-m2.5` | En ZEN il est `@ai-sdk/openai-compatible` → `/chat/completions` ; nous le routons en Anthropic `/messages` (**confirme A3/B**). |
| `qwen3.5-plus` | En ZEN il est `@ai-sdk/anthropic` → `/messages` (seul cas Anthropic du lot) — cohérent avec notre `protocol: anthropic`. |

### I2 — Trois modèles `-free` sont AUSSI dans le catalogue Go (piège)

`step-5-preview-free`, `longcat-2.5-preview-free`, `space-bunny-free` figurent dans
les 36 modèles du plan Go → le client officiel les atteint sur
`/zen/go/v1/chat/completions` **avec une clé payante**. Notre heuristique de
suffixe les envoie sur l'anonyme.

**Décision : divergence volontaire, ne pas « corriger ».** Router ces modèles sur
la jambe payante consommerait le quota Go pour rien — c'est précisément l'intérêt
de la jambe anonyme. À consigner comme écart assumé, pas comme défaut.

### I3 — Contraintes gateway qui brident les lots D, E et A

| Contrainte | Source | Lot impacté |
|---|---|---|
| `stream: true` **imposé** même en non-streaming client (sinon 403) | `_free_wire_body(force_stream=True)`, `opencode.py:4937-5016` ; bisection `opencode.py:4895-4911` | interdit tout passthrough non-streamant sur free |
| `tools[]` doit contenir `bash` **et** `read` (shims injectés, idempotents) | même bisection ; `opencode.py:11807` | **E3** : le tri des outils change l'ordre du fil vs la capture de référence |
| Fil à **7 en-têtes exacts**, dans l'ordre, sans `Accept` explicite | `tests/test_official_client_parity.py:70-100` ; `_official_free_headers` `opencode.py:5273-5347` | **D1/D2** |
| Replay TLS Bun (JA3 + sigalgs + H1 + `default_headers: false`) | `_free_fp_override`, `opencode.py:4790-4885` | **D1** : UA et empreinte TLS sont couplées |
| Invariant A.0 : aucun artefact payant ne sort sur l'anonyme | `_free_request_headers`, `opencode.py:4636-4649` | tout ajout d'en-tête |
| Idle 120 s (vs 300 s payant), retry 0,5 s × tentative plafonné à 2 s | `opencode.py:13486-13491`, `7832-7847` | **G2** |

**Conséquence sur le séquencement** : les lots **D1, D2, E1, E3** doivent être
appliqués **avec un flag de jambe** — payante d'abord, free seulement après
re-mesure du client 1.18.35 sur le capteur local. Modifier l'UA sans re-mesurer
casserait le couplage UA/TLS qui fait passer la jambe anonyme.

---

## 11. Lot H — Hygiène du catalogue (P2)

**Modèles de `config.yaml` absents du catalogue de leur jambe** (12 ids — le compte
de 5 de la version initiale ne tenait que des deux jambes confondues et des clefs
au lieu des endpoints ; la liste complète est celle de la photographie V0) :

| Classe | Ids (ligne `config.yaml`) |
|---|---|
| **Hors du provider payant** (11) | `glm-5.1` (`:33`), `glm-5` (`:35`), `kimi-k2.5` (`:37`), `mimo-v2-pro` (`:45`), `mimo-v2-omni` (`:47`), `minimax-m2.5` (`:57`), `hy3-preview` (`:77`), `qwen3.5-plus` (`:65`), `omen-alpha` (`:125`), `deepseek-flash` (`:127`), `jev-1.13` (`:134`) |
| **Hors du provider anonyme** (1) | `jev-1.13-free` (`:131`) |
| **`deprecated` sur sa jambe** (5) | `kimi-k2.6` (`:39`, payant), `mimo-v2.5-free` (`:95`), `deepseek-v4-flash-free` (`:93`), `fledge-alpha-free` (`:151`), `muse-spark-1.2-contributor-free` (`:88`) |

`glm-5.1` et `minimax-m2.5` sont **vivants sur la jambe anonyme** (`opencode`,
120 modèles) : leur absence du plan Go est le cœur de N1, pas une simple question
d'hygiène. Cas à part : `jev-1.13` / `jev-1.13-free` (`:131-136`) pointent sur
`/zen/v1/systemone`, endpoint interne assumé — à **conserver** mais documenter
comme non-catalogue (ils échappent donc à la règle des 12).

**Pas de rafraîchissement du catalogue.** Officiel refetch
`models.opencode.ai/api.json` (TTL disque 5 min, refresh 60 min, UA dédié, repli sur
snapshot bundlé — `packages/core/src/models-dev.ts:160-258`). Notre dérive
(`minimax-m2.5`, grok/luna, qwen) est exactement ce que ce mécanisme évite.

**Correction** : job optionnel en **lecture seule** qui écrit un diff proposé
(`models_catalog.diff.json`) sans jamais écrire `config.yaml` — l'application
reste manuelle, derrière revue. Alternative minimale : rejouer le script de
comparaison en CI hebdo.

---

## 12. Déjà aligné — ne surtout pas « corriger »

| Point | Proxy | Officiel |
|---|---|---|
| Auth Anthropic | `x-api-key` + `anthropic-version: 2023-06-01` (`opencode.py:322-335`) | `anthropic-messages.ts:845-853`, `providers/anthropic.ts:13-18` |
| Beta interleaved-thinking / fine-grained-tool-streaming | `opencode.py:341-342` | `plugin/provider/anthropic.ts:13-15` |
| `stream_options: {include_usage: true}` | `mapping.py:1611-1623`, `opencode.py:13165-13170` | `openai-chat.ts:361` |
| Breakpoints de cache ≤ 4, placement outils/system/messages | `mapping.py:617-720` | `cache-policy.ts:238-251`, `534-538` |
| `reasoning_content` relayé en delta **et** rejoué dans l'historique | `chat_sse_to_anthropic.py:413`, `mapping.py:1243,1544` | `openai-chat.ts:419-427`, `257-260` |
| Projection du schéma d'outils (`anyOf` aplati, `additionalProperties:false`) | `mapping.py:940-1184` | `tool-schema.ts:48-64` |
| `prompt_cache_key` **non** envoyé en openai-compatible | absent | `transform.ts:1323-1335` (jamais pour ce npm) |
| `input_tokens` = total inclus cache (OpenAI) | `mapping.py:2080-2149` | `sumTokens`, `openai-chat.ts:117-131` |
| IDs d'outils stables `toolu_`/`call_` | `mapping.py:91-119` | `tool-stream.ts` |
| `stop_reason` mapping + `error` si EOF nu | `chat_sse_to_anthropic.py:46-71,292-358` | idem |

---

## 13. Divergences volontaires — ne jamais aligner

- Jambe free anonyme `Bearer public`, replay TLS Bun, jeu de 4 en-têtes, `stream:true` forcé : spécifiques au proxy, verrouillés par `tests/test_official_client_parity.py` (le client officiel exige `OPENCODE_API_KEY`).
- Les trois modèles `-free` **aussi** présents dans le catalogue Go (`step-5-preview-free`, `longcat-2.5-preview-free`, `space-bunny-free`) : rester sur l'anonyme préserve le quota Go (lot I2).
- Hedging, circuit breaker par endpoint, TTFB watchdog, rotation IP/VPN, politique geo, compaction serveur : extensions que le client officiel n'a pas.
- Non-streaming sur jambe payante (l'officiel streame toujours) : voie validée en production, laisser tel quel.
- Multi-clés / failover de clé : sans équivalent officiel.

---

## 14. Triage — contrainte légitime vs alignement obligatoire

Un écart avec le client officiel n'est pas un défaut par nature. Le proxy n'est
pas le client officiel : il est **mandataire** (il parle à des clients hétérogènes,
pas seulement au TUI opencode) et il est **multiplexé** (une instance, N clients,
M stations VPN). Certains écarts sont donc imposés par notre rôle, d'autres par
l'amont, et seule la troisième catégorie est un vrai défaut.

### 14.1 Règle de décision

Trois questions, dans cet ordre :

1. **L'amont impose-t-il ce comportement ?** (le catalogue, le contrat de la
   passerelle, la grammaire du protocole, un code d'erreur normalisé) → *contrainte
   amont partagée*. L'officiel la respecte parce qu'il parle au même amont ; ne pas
   la respecter n'est pas une liberté, c'est un bug. **À aligner.**
2. **Notre rôle de proxy l'impose-t-il ?** (jambe gratuite anonyme, rotation
   d'IP/VPN, hedging, compaction serveur, multi-clés, non-streaming en payant) →
   *contrainte proxy légitime*. L'officiel ne peut pas l'avoir, il n'est pas dans la
   même position. **Ne pas aligner.**
3. **Ni l'un ni l'autre** → c'est un choix arbitraire pris chez nous sans
   contrepartie. **Défaut à corriger.**

### 14.2 Triage des écarts du §2

| # | Contrainte amont ? | Contrainte proxy ? | Verdict | Action |
|---|---|---|---|---|
| A1 protocole `/responses` | **oui** — le endpoint parle un protocole donné | non | **À aligner** | Lot A, config seule |
| A2 protocole qwen | **oui** | non | **À aligner** | Lot A, config seule |
| A3 `minimax-m2.5` | **oui** — le modèle a changé de catalogue *et* de protocole | non | **À aligner** | Lot A + B |
| B1 routes mortes | **oui** — un modèle hors catalogue renvoie 404 | non | **À aligner** | Lot B |
| B2 `claude-haiku-5-5` | **oui** | non | **À aligner** | ajout config |
| C1 allow-list reasoning | **oui** — le modèle rejette un effort qu'il ne porte pas | non | **À aligner** | Lot C |
| C2 beta daté | **oui** — la valeur d'en-tête est un contrat serveur | non | **À aligner** | trivial |
| D1 UA périmé | non — c'est **notre** en-tête, sa dérive est de la maintenance | non | **Défaut chez nous** | Lot D |
| D2 `x-opencode-session-id` absent | non | non (l'envoyer n'a aucune contrepartie proxy) | **Défaut chez nous** | Lot D |
| E1 `tool_choice: "auto"` non demandé | non | non | **Défaut chez nous** | Lot E |
| E2 `max_tokens` sans plafond | **oui, en partie** — la limite du modèle est réelle (voir 14.3) | non | **Défaut chez nous, à aligner sur l'amont** | Lot E |
| E3 outils non triés | non | non | **Défaut chez nous** | trivial |
| F1 `error` amont avalé | **oui** — l'erreur existe en amont, on la masque | non | **Défaut chez nous** — **corrigé 2026-10-10** (`app/protocol/chat_sse_to_anthropic.py`) | Lot F |
| F2 `cache_read_input_tokens` non compté | **oui** — le champ existe en amont (`message_delta`) | non | **Défaut chez nous** — **corrigé 2026-10-10** (§18) | Lot F |
| G1 codes d'erreur non classés | **oui** — `context_length_exceeded`, `insufficient_quota`, … | non | **Défaut chez nous, alignement amont** | Lot G |
| G2 budget de reprises | **en partie** — les *codes* relançables sont amont (`retry.ts:33-41`) | **oui, en partie** — le *nombre* : un proxy N→1 ne doit pas réessayer 5× sous peine d'amplification | **Mixte** | aligner les codes + l'en-tête `retry-after`, **pas** le compteur brut |
| H1 modèles morts / pas de refresh | **oui** | non | **Défaut chez nous** | Lot H |
| I1–I3 jambe gratuite | non | **oui** — contrat de la passerelle anonyme | **Contrainte proxy légitime** | ne pas aligner |
| §13 (free, hedging, breaker, watchdog, rotation, geo, compaction, non-streaming payant, multi-clés) | non | **oui** | **Contrainte proxy légitime** | ne pas aligner |

### 14.3 Le cas `max_tokens` — pourquoi le plafond officiel n'est pas arbitraire

C'est l'exemple qui montre que la question se posait dans le bon sens. **Le
plafond existe côté officiel, et il est dur** :

- `provider/transform.ts:18` — `export const OUTPUT_TOKEN_MAX = 32_000`
- `provider/transform.ts:1481-1483` —
  `maxOutputTokens(model) = Math.min(model.limit.output, outputTokenMax) || outputTokenMax`
- `session/llm/request.ts:129` → `session/llm.ts:238,320` : la valeur calculée est
  celle qui part sur le fil, sur les trois protocoles
  (`anthropic-messages.ts:546`, `openai-chat.ts:361`, `openai-responses.ts:493`)
- `effect/runtime-flags.ts:52` — le 32 000 est un **défaut** réglable via
  `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX`, pas une loi de l'amont
- `session/overflow.ts:8,16-19` — le même plafond conditionne le budget de compaction
  (`COMPACTION_BUFFER = 20_000`)
- `provider/transform.ts:1750` — le budget de pensée obéit au même plafond :
  `Math.min(max ?? 32 000 - 1, model.limit.output - 1, 32 000 - 1)`

Chez nous : `app/protocol/mapping.py:322` fixe le **défaut** à 16384, et il n'existe
**aucun** clamp sur les jambes principales — `mapping.py:368` et `:376` recopient la
valeur du client telle quelle. Le seul plafond du dépôt est celui du résumeur de
compaction (`app/compaction/summarizer.py:116-144`, `summary_clamp_max_tokens: 4096`),
qui ne s'applique qu'à nos requêtes internes.

Deux lectures, qu'il faut séparer :

- **`model.limit.output`** : contrainte amont réelle. Envoyer au-delà, c'est un 400
  ou une troncature silencieuse selon les modèles — et pour nous un `stop_reason:
  max_tokens` que nous ne saurions pas distinguer d'une vraie fin (§ G1). **À aligner**,
  mais dépendant du Lot H : `config.yaml` ne porte aujourd'hui aucune limite de
  sortie par modèle (`thinking.min_tokens` / `effort_caps` ne sont qu'un sous-ensemble
  partiel). Pas de limites catalogue → pas de clamp possible.
- **`32 000`** : c'est le défaut *du client officiel*, pas une règle de la passerelle.
  Il est d'ailleurs réglable par variable d'environnement. Le copier telquel serait
  une régression pour nos autres clients : un modèle qui annonce 64 000 en sortie doit
  pouvoir les prendre, et nos clients ne sont pas tous le TUI opencode. **Ne pas
  aligner par défaut** ; exposer le plafond en configuration (`max_tokens_cap`, vide
  = aucun) si le besoin s'en fait sentir.

Conclusion sur l'exemple : l'instinct était bon — le plafond existe bel et bien côté
officiel, il est dur, et nous n'en avons aucun. Mais l'anomalie n'est pas « l'officiel
plafonne et pas nous » : c'est que **nous sommes incohérents dans les deux sens** —
trop permissifs (aucun clamp : on transmet tel quel un `max_tokens` de 64 000 vers un
modèle qui en annonce 32 000, au risque d'un 400 ou d'une troncature) et trop
restrictifs (défaut figé à 16384 là où l'officiel envoie `min(limit.output, 32 000)`,
soit 32 000 pour la plupart des modèles du catalogue). La seule partie non négociable
est la limite du modèle ; le 32 000 n'est qu'un défaut de client.

### 14.4 Pour les écarts futurs

Tout nouvel écart découvert plus tard se classe avec les trois questions de 14.1,
dans cet ordre, et se range dans le lot correspondant. Un écart qui ne passe aucune
des deux questions mais dont la correction touche `config.yaml` passe quand même en
premier : c'est le plus réversible et le seul à ne pas pouvoir casser de code.

### 14.5 Deux enseignements structurels de la spécification du client officiel

La spécification complète de la couche LLM du client officiel (un unique
`streamText`, `session/llm.ts:280-353`) apporte deux clarifications qui recadrent
le triage :

**1. Les transformations du client se fondent sur `model.api.id`, pas sur
l'alias.** `provider/transform.ts` décide de `temperature` / `topP` / `topK` (omis
pour tout id contenant `claude`, `transform.ts:533`, `:549-574`), du préréglage
`thinking` par famille (`transform.ts:1040-1164`) et de l'invite système
(`session/system.ts:28-51`) d'après l'**id du modèle** tel que le client l'a tiré
du catalogue. Or nos clients parlent en alias (`opus` / `sonnet` / `haiku`) et nous
remappons vers `kimi-k2.6` / `glm-5.1` / `minimax-m2.5` : l'id que voit l'officiel
n'est pas celui que nous envoyons. Une divergence de ce type (« l'officiel n'envoie
pas `temperature` sur les ids `claude*` ») est donc une **contrainte proxy
légitime** : la reproduire exigerait de renoncer au remappage, pas de corriger un
défaut.

**2. Le client officiel ne réessaie pas au niveau du SDK** (`maxRetries: 0`,
`session/llm.ts:323`) mais porte sa propre couche de reprise au-dessus (6
tentatives, backoff 2 s → 30 s, `session/retry.ts:25-31,47-83`). Cela confirme le
verdict **mixte** de G2 (§14.2) : est amont ce qui relève des *codes* relançables
et du respect de `retry-after` / `retry-after-ms` ; est proxy ce qui relève du
*nombre* de tentatives, seul poste où l'amplification N→1 se joue.

### 14.6 Registre des écarts nouveaux (photographie V0, 2026-10-09)

Quatre écarts absents de l'analyse initiale. Conformément à 14.4, chacun passe
d'abord par les trois questions de 14.1 — et obtient une carte au §18 avant toute
implémentation. Aucun des quatre ne se classe « contrainte proxy légitime ».

| # | Les trois questions de 14.1 | Verdict | Action |
|---|---|---|---|
| **N1** routes par défaut mortes en payant (`glm-5.1`, `minimax-m2.5` hors Go ; `kimi-k2.6` `deprecated`) | Q1 **oui** — un modèle hors catalogue renvoie 404 | Q2 non | **À aligner** — même nature que B1, mais plus grave : ce ne sont plus 2 des 3 routes qu'il faut déplacer, c'est **les 3**. Réécrit la carte B1 (§4) |
| **N2** `free_model_map` → `mimo-v2.5-free` `deprecated`, `web_search_native` → `muse-spark-1.2-contributor-free` `deprecated` | Q1 **oui** — le statut `deprecated` est déclaré par le catalogue | Q2 non (la contrainte proxy porte sur *la* jambe libre, pas sur *quel* modèle libre remplacer) | **Défaut chez nous** — rediriger vers des modèles vivants (§10 I1/I2 fournissent les candidats sains) |
| **N3** 9 modèles du catalogue à `npm` hors table (`@ai-sdk/google` ×8, `@ai-sdk/mistral` ×1) | Q1 **oui** — sans `npm` connu, l'endpoint est indéductible | Q2 non | **Défaut de conception chez nous** — notre règle de routage ne couvre que 3 `npm`. Socle du lot H (matrice `npm` → endpoint explicite, alinéa N3) |
| **N4** 41 modèles de la jambe anonyme absents de `config.yaml` qui atteindraient le mauvais endpoint via le repli implicite | Q1 **oui** — l'endpoint requis est dicté par le catalogue | Q2 non | **Défaut chez nous** — en soi, ne pas référencer 41 modèles optionnels n'est pas un défaut ; le défaut est que **le repli implicite est silencieux et faux** pour 41 d'entre eux. Deux options conservatoires au §18 : soit les déclarer, soit durcir le repli (refus explicite plutôt qu'endpoint inventé) |
| **N5** `cache_read_input_tokens` systématiquement nul sur `mimo-v2.6-flash-free` (jambe chat anonyme) — canary N2 du 2026-10-12 | Q1 **oui** — l'amont ne fait pas de prompt caching sur cette cible/jambe : corps brut `prompt_tokens_details: {cached_tokens: 0, cache_write_tokens: 0}`, y compris à préfixe 1247 tokens répété à l'identique (2 passes, `cache_creation` 0 les deux fois) ; historique 6/6 à zéro contre 181 780 lignes cachées côté spark. La mémoire d'un cache « géré différemment » côté Mimo est confirmée par la mesure, pas contredite : `cache_rewrite_models` (`config.yaml:635-641`) liste `mimo-v2.6-flash` nue mais pas la cible `-free` (membership exacte, `mapping.py:792`), et de toute façon la réécriture ne créerait pas une écriture que l'amont refuse | Q2 non | **Contrainte amont partagée — ne pas aligner** : la conversion est fidèle (extraction en place des 3 côtés : `mapping.py:210-211`, `:2177`, `:4986`), l'officiel face au même amont lirait le même zéro ; inventer un `cache_read` serait un bug, pas une liberté. Le triplet canary se réduit pour cette cible à 200 + `usage` + texte exact. Pas d'implémentation, carte §18 constat seule. |
| **N6** canary payant A1/A2/B2 du 2026-10-12 : 5/8 ids refusés par l'amont **sur jambe payée** (`grok-4.5` 400 `Model is unavailable.` ; `grok-4.6`/`grok-4.7`/`gpt-6-luna`/`qwen3.6-plus` 403 `An active OpenCode Go subscription is required to use Go models.`) ; les 3 autres 200 **sur jambe libre** (shadows `custom_routes` + alias haiku, voir :830) — **0 × 200 jambe payée observé** | Q1 **oui** — le refus vient du catalogue/contrat de la passerelle (modèle indisponible, abonnement Go requis), pas de notre mapping : lignes DB `model`=id demandé + `paid_status` 400/403, corps amont verbatim | Q2 non | **Contrainte amont partagée — ne pas aligner** : `config.yaml` est déjà correct (protocols/endpoints alignés V1), §16 ne s'applique pas (400/403 amont préexistants, pas nouveaux de notre changement) → on consigne, on ne touche pas `config.yaml`. Point ouvert laissé à l'arbitrage : purger les 5 ids refusés (lot H1) ou les garder en l'état — même nature que A3 (reporté). Pas d'implémentation, carte §18 constat seule. |

Conséquence sur le séquençage : N1 remonte B1 en **P0 bloquant** du lot B
(§4) — la V4 devient la première vraie migration de trafic payant, et doit
précéder tout changement de protocole qui supposerait un modèle payant vivant.

---

## 15. Séquençage et vérification

Ordre proposé, du plus mécanique au plus risqué, chaque étape réversible
indépendamment :

1. **H2/B2 (config seule)** : ajouter `claude-haiku-5-5`, corriger `api: responses` sur les 5 modèles grok/luna, `protocol: openai` sur qwen3.6-plus/3.7-max.
2. **E3 + C2** : tri des outils, libellé du beta — code trivial, tests ciblés.
3. **Canary live obligatoire** (avant tout lot B) : une requête minimale par modèle
   candidat, en streaming et en non-streaming, sur
   `/zen/go/v1/{chat/completions,messages}` pour `glm-5.1`, `minimax-m2.5`,
   `glm-5.3`, `claude-haiku-5-5` → confirmer 200 + usage + `cache_read_input_tokens`.
4. **B1** : basculer `sonnet_model` / `haiku_model` uniquement si l'étape 3 confirme
   que les modèles actuels sont refusés OU que les candidats répondent mieux.
5. **C1** : table `reasoning_options` + nouveau clamp, derrière
   `effort.allow_list_enabled` (défaut `false`) puis bascule.
6. **D1/D2** : re-mesure du vrai client 1.18.35 puis UA + `x-opencode-session-id`
   — **jambe payante uniquement** ; la jambe free attend sa propre re-mesure (lot I3).
7. **E1, E2, F1, F2, G1, G2** : un par un, chacun avec son test. E1/E3 désactivables
   par jambe tant que la free n'est pas re-mesurée. **F1, F2, G1 et G2 sont
   faits** (2026-10-10) — ce sont ceux de ce groupe qui ne demandent aucune
   fenêtre d'exploitation pour la partie code (G1 ne lit que le corps déjà reçu,
   il ne change aucun statut émis ; **G2 ne change aucun défaut** : il expose
   seulement le budget en config, `upstream.max_retries` → `streaming.retry_attempts`
   → `2`). Leur point « live » reste ouvert dans leur carte.
8. **H1** : nettoyage des modèles morts + job de diff catalogue en lecture seule.
   Le diff doit comparer **les deux providers** (`opencode-go` pour le payant,
   `opencode` pour la jambe free) — lot I1.

**Commandes de non-régression à passer à chaque étape :**

```
pytest tests/test_official_client_parity.py tests/test_opencode_client_parity_helpers.py tests/test_paid_wire_identity.py
pytest tests/test_model_catalog.py tests/test_go_only_routing.py tests/test_effort_caps.py tests/test_effort_policy.py tests/test_effort_mapping.py
pytest tests/test_protocol_matrix.py tests/test_e2e_protocol_matrix.py tests/test_conversion_golden.py
pytest tests/test_streaming_sse.py tests/test_responses_stream_contract.py tests/test_responses_stream_e2e.py
python scripts/gen_golden_fixtures.py && git diff --stat docs/v1-response-golden/
```

**Règle d'or** : `config.yaml` d'abord, code ensuite. Chaque changement de
`config.yaml` est réversible en une ligne et ne touche aucun chemin de conversion.

---

## 16. Critères d'arrêt / rollback

- Toute régression de `tests/test_conversion_golden.py` ou
  `tests/test_e2e_protocol_matrix.py` → rollback immédiat de l'étape.
- Tout 400/403 nouveau sur un modèle en production → revenir au `protocol`/`api`
  précédent dans `config.yaml` (pas de rollback code).
- `cache_read_input_tokens` qui s'effondre après le tri des outils → revert E3 et
  investiguer (le tri change le préfixe, donc le taux de cache).
- La doc `go.mdx` et le catalogue live doivent être **d'accord** avant tout
  changement de route par défaut ; en cas de désaccord, ne rien changer et
  consigner l'écart dans ce document.

---

## 17. Feuille de route (road map)

Huit vagues, du plus mécanique au plus risqué. Le §15 donnait l'ordre technique ;
cette section le transforme en **vagues vérifiables et réversibles isolément**.
Aucune vague ne présuppose la suivante : chacune livrée seule laisse le proxy
strictement meilleur, et chacune peut être abandonnée sans conséquence sur les
autres.

| Vague | Contenu | Écarts | Dépend de | Porte | Risque | Effort |
|---|---|---|---|---|---|---|
| **V0** ✅ | Photographie du catalogue live | — | — | aucune (lecture seule) | nul | 0,5 j |
| **V1** | `config.yaml` uniquement | A1, A2, B2, **N2** (retenu) | V0 | 8 modèles + jambe libre | faible | 0,5 j |
| **V1b** | Tests seulement — shadow `custom_routes` | **Z** | V1 | aucun code, aucun config | nul | 0,25 j — **fait 2026-10-09** |
| **V2** ✅ | Code trivial | E3, C2 | V1 | payante | faible | 0,5 j — **faite 2026-10-09**, hors mesure live |
| **V3** | Table des limites de sortie | E2 (socle) | V2 | payante | moyen | 1 j — **implémentée 2026-10-09**, hors vérif live |
| **V4** | Routes par défaut + effort | B1, **N1**, C1, A3 | V1 + V3 | 3 alias + effort | moyen | 1,5 j |
| **V5** | Identité filaire | D1, D2 | V2 | **payante seulement** | moyen | 0,5 j — **D2 fait 2026-10-10** (hors vérif live), **reste D1** (re-mesure du client 1.18.35) |
| **V6** | Flux, erreurs, reprises, hygiène | ~~F1~~, ~~F2~~, ~~G1~~, ~~G2~~, H1, **N3**, **N4** | V3 | payante | moyen | 1 j — **entamée 2026-10-10 : F1, F2, G1 et G2 faits** (reste H1, N3, N4 ; F2 à confirmer en fenêtre d'exploitation) |

**Hors vagues — carte P0 (§18) :** la dette de suite, traitée le 2026-10-10
sur consigne (« corrige préexistant au passage »). Les 4 échecs chroniques que
chaque vague rencontrait et laissait en l'état sont réglés ; la suite entière
sort maintenant en `PYTEST_EXIT=0`. Ce n'est pas une vague de parité : deux
points sont des tests qui ne pouvaient pas tourner (dossier `logs/` gitignoré,
libellé périmé), le troisième rétablit au contraire une règle commitée qu'une
modification non commitée avait désarmée.

N2 rejoint la V1 pour la même raison que A1/A2/B2 : c'est une correction de
`config.yaml` seule, donc la plus réversible (§14.4). La différence est la
**porte** : N2 touche la jambe libre, qui porte 100 % du trafic réel des 48 h
(41 341 requêtes), là où A1/A2/B2 touchent des modèles payants en 401 — son
canary est donc le plus important de la vague, et il se fait sur la jambe libre.
**Il reste retenu** (§18) : la cible n'est pas arbitrée.

**V1b** n'existait pas au premier jet de la feuille de route : elle est née de la
découverte §19, faite en vérifiant A2. Elle ne touche **ni code de production ni
config** — uniquement un fichier de test — et elle débloque la seule rouge du
lot, donc elle passe avant tout ce qui prétend s'appuyer sur
`test_e2e_protocol_matrix.py`. **Livrée le 2026-10-09** (carte V1b ci-dessus) : la matrice est
verte et la déviation est verrouillée par `tests/test_custom_routes_shadow.py`.

**Total indicatif : ~6,25 jours-homme étalés.** Rien n'oblige de tout faire.

Le reste du document ne bouge pas : les lots §3–§11 restent l'analyse, cette
section et la suivante sont l'exécution.

### V0 — Photographie (lecture seule, 0 risque)

- [x] Diff des **deux** catalogues (`opencode-go` 36 modèles, `opencode` 120) contre `config.yaml` — sélection par suffixe `-free` pour l'anonyme (lot I1).
- [x] Capture datée dans §1 ; tout écart nouveau classé par les trois questions de §14.1 et consigné en §14.6 (**N1–N4**, avec 4 cartes au §18).
- [x] Re-vérifier les ancrages de ligne cités au §18 (l'arbre est modifié non commité : **les lignes bougent**) → tableau de re-vérification en §1 : 12 ancrages relus, 2 corrections de lignes (`:505`→`:507`, `:519-532`→`:521-534`), 2 noms de tests corrigés.
- [x] Sortie : §2 **complété** (4 écarts nouveaux N1–N4, 3 lignes réécrites : A3, B1, H1) · §11 complété (12 hors-jambe + 5 `deprecated`, table) · §14.6 ajouté · 4 cartes §18 ajoutées.

> **V0 terminée le 2026-10-09.** Outil : `scripts/_probe_catalog_diff.py`
> (`--json` / `--effort` / `--limits` / `--all-providers`). Aucun fichier du
> projet modifié hors ce document : `config.yaml` intact, aucun test lancé,
> aucun code touché.

### V1 — Configuration seule (`config.yaml`, zéro ligne de code)

- [x] **A1** — `api: responses` sur `grok-4.5`, `grok-4.6`, `grok-4.7`, `gpt-5.6-luna`, `gpt-6-luna`. **Fait 2026-10-09** (pilote `grok-4.7` d'abord). Probe locale `DIVERGE`→`OK` ; `grok-4.5` reste `DEPRECATED` upstream (endpoint aligné, seul le statut changé).
- [x] **A2** — `protocol: openai` sur `qwen3.6-plus`, `qwen3.7-max` (**ne pas** toucher `qwen3.7-plus`, `qwen3.8-max`, `qwen3.8-flash`). **Fait 2026-10-09.** Probe `DIVERGE`→`DEPRECATED` (endpoint aligné) ; `qwen3.7-plus`/`qwen3.8-*` revérifiés toujours alignés, non touchés.
- [x] **B2** — `claude-haiku-5-5` en `protocol: anthropic`. **Fait 2026-10-09** (`config.yaml:166-167` ; ancrage antérieur `:161-162`, décalé par l'insertion de `grok-4.7`/`gpt-6-luna`). Probe `DIVERGE`→`OK`.
- [x] **N2** — `free_model_map` (12 entrées au total) et `web_search_native` recâblées hors `deprecated` (carte §18) — **fait le 2026-10-12, arbitrage utilisateur `spark-1.3-free`** : 6 × `mimo-v2.5-free` + `default_target` déjà à `mimo-v2.6-flash-free` dans l'arbre, 2 derniers sites `muse-spark-1.2-contributor-free` recâblés vers `muse-spark-1.3-contributor-free` (map + retrait `web_search_native`).
- [x] Vérification **offline** A1/A2/B2 (2026-10-09) : `scripts/_probe_catalog_diff.py` — plus aucun `DIVERGE` sur les 8 ids cibles, `qwen3.7-plus`/`qwen3.8-*` confirmés non touchés.
- [ ] Canary live obligatoire (§15 étape 3) sur les modèles touchés, streaming **et** non-streaming → 200 + `usage` + `cache_read_input_tokens`. **PENDANT — exécuté le 2026-10-12, NON SOLDÉ** : ordre live reçu le même jour (`Lancer les canarys live`), exécution via `/v1/messages` (proxy vivant localhost:4000, auth `none`) — **0 × 200 sur jambe payée** : `grok-4.5` HTTP 400 `Model is unavailable.` ; `grok-4.6`/`grok-4.7`/`gpt-6-luna`/`qwen3.6-plus` HTTP 403 `An active OpenCode Go subscription is required to use Go models.` (lignes DB `model`=id demandé, `paid_status` 400/403) ; `gpt-5.6-luna`/`qwen3.7-max` 200 `BONJOUR` MAIS jambe libre via shadow exact `custom_routes` (`gpt56luna`/`qwen37max` → `muse-spark-1.3-contributor` → `muse-spark-1.3-contributor-free`, DB `model=muse-spark-1.3-contributor-free`) ; `claude-haiku-5-5` 200 `BONJOUR!` MAIS jambe libre via alias haiku (`claude-haiku-5-5` → `minimax-m2.5` → `mimo-v2.6-flash-free`, DB `model=mimo-v2.6-flash-free`, log `route: 'claude-haiku-5-5' → minimax-m2.5`). Stream : mêmes 3 × 200 libres (gpt-5.6-luna 6 events, qwen3.7-max 6 events, haiku 16 events), les 5 refusés ne streamant pas. Portée réelle confirmée : 7 ids sur 8, car `gpt-5.6-luna` est **ombé** par `custom_routes.gpt56luna` (§19.3) — la ligne A1 existe mais le chemin runtime passe ailleurs, et son **intention** (repli sur `/responses`) est déjà satisfaite par héritage. Aucun modèle A1/A2/B2 utilisé par le routage opus/sonnet/haiku (`kimi-k2.6`/`glm-5.1`/`minimax-m2.5` intacts). §16 : 400/403 amont préexistants, pas nouveaux de notre changement → on consigne (N6 §14.6), on ne touche pas `config.yaml`. *Re-mesuré partiellement le 2026-10-12 21:03 UTC (ordre live `payant sans clé / gratuit sans clé`) : `grok-4.5` HTTP 400 `Model is unavailable.` (DB rowid 425914 `paid_status` 400) ; `gpt-6-luna` amont 403 (DB rowid 425917 `paid_status` 403 — le client voit 503 via `_auth_window_message`, cf. N6 §18) — N6 inchangé, case toujours non cochée.*
- [x] Canary **jambe libre** sur les cibles N2 (c'est là que passe le trafic réel) — **repositionné post-recâblage le 2026-10-12** : `muse-spark-1.3-contributor-free` + `mimo-v2.6-flash-free`, streaming et non-streaming → 200 + `usage` + `cache_read_input_tokens`. **SOLDÉ le 2026-10-12** : ordre live reçu le même jour (`Lancer les canarys live`), exécution via `/v1/messages` (proxy vivant localhost:4000, auth `none`, VPN station 1 `connected` remaining 232/500) — `muse-spark-1.3-contributor-free` **2/2 VERT** (non-stream : 200 `BONJOUR` `input 602 / output 248 / cache_read 369` ; stream : 200 `BONJOUR` 6 events `output 275 / cache_read 369`) ; `mimo-v2.6-flash-free` **200 + usage mais `cache_read_input_tokens: 0`** (non-stream : 200 `BONJOUR` `input 163 / output 42 / cache_read 0` ; stream : 200 `BONJOUR` 17 events `output 40 / cache_read 0` ; répétition non-stream à l'identique : toujours `cache_read 0` — pas un effet première-requête ; 2 passes à préfixe 1247 tokens répété : `cache_creation` 0 les deux fois — ni effet taille, ni amorçage). **Case cochée au critère N5** : spark au triplet complet, mimo à 200 + `usage` + texte exact (N5 §14.6 — contrainte amont partagée : corps brut `{cached_tokens: 0, cache_write_tokens: 0}`, historique 6/6 à zéro ; conversion fidèle, rien à aligner, rien ajouté à `cache_rewrite_models`). *Re-mesuré le 2026-10-12 21:03-21:04 UTC (ordre live `payant sans clé / gratuit sans clé`) : spark-free non-stream 200 `BONJOUR !` `input 598 / output 381 / cache_read 113` + stream 8 events ; mimo-free non-stream 200 `input 158 / output 41 / cache_read 0` + stream 23 events — constat N5 inchangé (zéro cache côté mimo, caching actif côté spark).*
- [x] `pytest tests/test_model_catalog.py tests/test_go_only_routing.py tests/test_docs_drift.py tests/test_free_model_map_http.py` — **38 passed** (+ 59 sur `test_compaction_free_class`/`test_hot_reload`, soit 97 verts), identique au vert d'avant édit. *Revérifié le 2026-10-12 : 45 collectés, 45 passés, EXIT=0 (l'écart avec 38 tient aux tests ajoutés depuis le 2026-10-09). Revérifié post-recâblage N2 le 2026-10-12 : 57/57, EXIT=0 (`c3_n2_post.xml` : trio + `test_free_model_map_http` + contrôle N2 `test_free_map_targets_live`).*
- [x] Rollback : restaurer `config.yaml.v1.bak.2026-10-09` (SHA256 vérifié à la création). **Ne PAS `git checkout -- config.yaml`** : l'arbre était déjà modifié vs HEAD sur ~30 fichiers avant cette session — `checkout` détruirait ce travail.

> **Point ouvert N2 — RÉSOLU le 2026-10-12** (arbitrage utilisateur : `spark-1.3-free` ; recâblage fait, carte §18 cochée, trio + contrôle 57/57 `c3_n2_post.xml`). Relevé à la résolution :
> - `tests/test_go_only_routing.py::test_web_search_native_parity_13` imposait les 4 ids muse-spark → **test réécrit** (3 ids + assert d'absence du deprecated), comme prévu ici.
> - `tests/test_compaction_free_class.py:291-294` visait **déjà** `mimo-v2.6-flash-free` à la relecture du 2026-10-12 (le 6 × mimo était recâblé dans l'arbre avant cette fenêtre) → le verrou redouté n'existait plus, asserts intacts, rien à toucher.
> - `docs/models-protocol.md:19-27` → **resynchronisé** (mimo → `mimo-v2.6-flash-free`, muse-spark-1.2 → `muse-spark-1.3-contributor-free` avec ancre N2).
> - Cible tranchée : `muse-spark-1.3-contributor-free` pour les 2 sites muse-spark ; `mimo-v2.6-flash-free` pour les 6 sites mimo + `default_target` (déjà en place). Test `tests/test_free_map_targets_live.py` **créé et levé de son xfail** (vert = état attendu).


### V1b — Tests seulement : le shadow `custom_routes` (éclos en vérifiant A2)

Née de la découverte §19 : en validant A2/B2 sur la matrice de protocole, deux
tests sont passés au rouge — non pas parce que la config mentait, mais parce que
`custom_routes` réécrit l'id **avant** la résolution d'endpoint. La vague se
coche dans l'ordre ci-dessous ; les trois premières cases sont la découverte, pas
du travail.

> **Vague terminée 2026-10-09** (Z1 + Z2 arbitré + Z3 + pytest + rollback
> documenté). Aucune ligne de production, aucun `config.yaml`, aucun changement de
> comportement : la déviation est désormais écrite et verrouillée par un test.

- [x] **Constater** le mécanisme : `app/router/__init__.py:55-153`, étape 0 — si `name in models`, la route custom dont un `match` est **exactement égal** à l'id remplace la résolution (`res = r_custom`). Passe **unique**, donc non transitive (pas de second passage sur le `model` réécrit).
- [x] **Constater** la conséquence vivante : `_route_for("muse-spark-1.3-contributor")["model"] == "step-5-preview-free"` → la demande sort sur `https://opencode.ai/zen/v1/chat/completions`, jambe **libre**. La session en cours roule donc sur la jambe libre, pas sur `muse-spark-1.3-contributor`.
- [x] **Constater** l'effet collatéral sur A1 : la clef `models:` de `gpt-5.6-luna` (`config.yaml:79-81`) est **inerte au runtime** à cause de `custom_routes.gpt56luna` (`config.yaml:399-402`), alors que l'intention d'A1 (`/responses`) est déjà satisfaite sur la cible par héritage → rien à réparer, juste à ne pas conclure « config inutile ».
- [x] **Z1** — Dans `tests/test_e2e_protocol_matrix.py`, `_install_seams` (~426-499) : neutraliser le shadow pour les cas qui ne le visent pas, via `monkeypatch.setattr(_cfg_settings, "SORTED_CUSTOM_ROUTES", [])` (même forme que `tests/test_proxy.py:601`). La case ne se coche qu'avec le test passé.
  - [x] **Fait 2026-10-09** — patch posé, accompagné d'un `ROUTE_VERSION + 1` : `_route_cache` ne se vide qu'au changement de version (`app/router/__init__.py:61-64`), sans quoi une entrée empoisonnée par un test précédent survivait au seam. Résultat : `tests/test_e2e_protocol_matrix.py` → **43 tests, 0 échec** ; les deux rouges de §19.3 (`test_p5_responses_to_openai_nonstream` :695, `::stream` :834) sont verts. Aucune ligne de production, aucun `config.yaml`.
- [x] **Z2** — Repointer `PAID_RESPONSES` vers un modèle non ombré : **écarté**, et pour une raison de fond. Le seul candidat (`grok-4.7`) n'a aucun équivalent free, donc l'assertion `_resolve_free_model(PAID_RESPONSES) is not None` (`:1343`) aurait dû devenir `is None` — or elle protège une propriété réelle : P5 s'exerce sur un modèle qui **a** un équivalent free, si bien que le stub `_try_free_model_first` y est portant (sinon le swap free pourrait masquer le chemin de conversion testé). La rendre verte en la détruisant est exactement le geste interdit (§20 règle 5). À la place : le commentaire **faux** (`:62-63`, « aucun équivalent free dans FREE_MODEL_MAP » alors que `muse-spark-1.3-contributor` y figure, `config.yaml:176`) a été réécrit et pointe vers Z1 + le test dédié Z3. Z1 en place, le shadow ne peut plus toucher la matrice : `PAID_RESPONSES` garde sa valeur, et personne n'a à choisir un modèle « parce qu'il n'est pas ombré ».
- [x] **Z3** — `tests/test_custom_routes_shadow.py` (nouveau, 6 tests) **verrouille** la découverte au lieu de la nier : `_route_for("muse-spark-1.3-contributor")["model"] == "step-5-preview-free"`, `get_model_config("step-5-preview-free")["endpoint"] == https://opencode.ai/zen/v1/chat/completions`, l'inertie au runtime de l'entrée payée (qui annonce pourtant `/responses`), la non-transitivité du passage, l'effet `gpt56luna` sur A1, et deux contre-épreuves (`grok-4.7`, `glm-5` non ombrés). Cache local vierge à chaque appel (`route_cache={}`, `seen_version=-1`) : le fichier lit la config déployée sans dépendre de l'ordre des tests.
- [x] `pytest tests/test_e2e_protocol_matrix.py tests/test_custom_routes_shadow.py tests/test_go_only_routing.py tests/test_docs_drift.py` (+ `tests/test_proxy.py tests/test_hot_reload.py`, consommateurs du contrat `route_for`) — **186 tests collectés, 0 échec** (exit 0). *Revérifié le 2026-10-12 à décompte identique : 186 collectés, 185 passés + 1 xfail connu, EXIT=0.*
- [x] Rollback : revert des **deux** fichiers de test (`tests/test_e2e_protocol_matrix.py` patché, `tests/test_custom_routes_shadow.py` nouveau — le supprimer suffit pour lui). `config.yaml`, le code de production et `custom_routes` ne sont pas touchés par cette vague.

> **Ce que V1b ne fait PAS.** Il ne supprime pas `custom_routes.musespark13contributor`,
> il ne redirige pas la jambe libre, il ne touche aucun comportement de production.
> Si un comportement doit changer (ex. « muse-spark-1.3-contributor doit rester payé »),
> ce n'est plus un lot de tests : ça retourne en §14.1 pour être classé, puis ça
> passe par une vague qui touche `config.yaml`, avec son propre canary.


### V2 — Code trivial (deux correctifs d'une ligne)

- [x] **E3** — tri des outils par `function.name` dans `_sanitize_chat_tools`.
- [x] **C2** — libellé du beta daté (`thinking-binding-controls-2026-08-01`).
- [x] `pytest tests/test_tool_name_sanitization.py tests/test_p3_tool_names.py tests/test_opencode_client_parity_helpers.py tests/test_conversion_golden.py` — **vert 2026-10-09**, puis **élargi** aux 9 fichiers qui consomment le tri ou le beta (les 7 d'origine — `test_tool_order_parity.py`, `test_conversion_cache.py`, `test_e2e_protocol_matrix.py`, `test_custom_routes_shadow.py` — plus `test_official_client_parity.py`) : **210 tests collectés, 0 échec**, 1 xfail attendu (exit 0). *Chiffre corrigé : la version précédente de cette ligne annonçait « 203 tests » ; le décompte réel de ces fichiers est 162 pour les 7 d'origine, 210 avec `test_official_client_parity.py` (compté au `--collect-only`).* *Revérifié le 2026-10-12 à décompte identique : 210 collectés, 209 passés + 1 xfail connu, EXIT=0.*
- [ ] `cache_read_input_tokens` comparé avant/après E3 sur un même prompt (critère d'arrêt §16) — **seule case encore ouverte de V2**, et elle est ouverte **par nature** : elle demande un appel réel, donc la fenêtre d'exploitation (proxy local vivant). Aucune case cochée ici sans mesure ; aucune case décochée non plus, puisque le correctif est par construction l'ordre exact du client officiel.
- [x] Rollback : **documenté, par carte et par site** (carte E3 + carte C2 ci-dessus). Rien à exécuter : rien n'est cassé. Le rollback n'est pas un revert d'un fichier entier — `app/protocol/mapping.py` porte E3 seul, `opencode.py` porte C2 seul, et chacun de leurs tests se supprime avec eux. `config.yaml` n'est pas dans le périmètre de V2.

> **Vague terminée 2026-10-09** — deux fichiers de production seulement
> (`app/protocol/mapping.py`, `opencode.py`), aucun `config.yaml`, aucun
> comportement hors les deux points visés.
>
> **E3 — le tri n'est PAS un tri de code points.** Premier jet livré avec
> `sort(key=name)`, puis **corrigé le jour même** après mesure contre le vrai
> collateur (`node -e "[...].toSorted((a,b)=>a.localeCompare(b))"`).
> `localeCompare` (ICU) met la ponctuation avant les alphanumériques, compare les
> lettres **insensiblement à la casse**, et n'utilise la casse **qu'à égalité
> totale du niveau primaire**, minuscule avant majuscule. Un tri de code points
> met `Zed` avant `alpha` et `a_b` avant `A` — c'est-à-dire qu'il reproduisait,
> sur tout jeu d'outils à casse mixte (donc sur tous les jeux réels :
> `Bash`/`bash_tool`), l'écart de préfixe que E3 est censé faire disparaître.
> Le piège exact : la casse est un **niveau** de la collation, pas un poids par
> caractère. `_tool_sort_key` (`mapping.py:3760`) implémente les deux niveaux, et
> est vérifié contre le collator réel sur **395 noms tirés de l'alphabet complet
> `[A-Za-z0-9_-]`** (chaînes aléatoires comprises) : ordre **identique**.
>
> Le tri vit à trois endroits, et les trois sont verrouillés par
> `tests/test_tool_order_parity.py` (**13 tests**) :
> 1. `_sorted_chat_tools` — le tri + son garde-fou (liste malformée renvoyée
>    **inchangée**, jamais de réordonnancement partiel) ;
> 2. `_sanitize_chat_tools` — trie **même sur le fast-path** (aucun nom à
>    raccourcir) : l'ordre officiel est une propriété du fil, pas un effet de bord
>    du raccourcissement ;
> 3. `_free_wire_body` — les shims `bash`/`read` sont triés **avec** le reste
>    (jambe Chat uniquement). La jambe Responses est laissée telle quelle : son
>    ordre n'a **jamais été mesuré** chez le client officiel, on n'invente pas
>    (même principe que l'ordre des clés).
>
> `pytest tests/test_tool_order_parity.py tests/test_tool_name_sanitization.py
> tests/test_p3_tool_names.py tests/test_conversion_cache.py
> tests/test_conversion_golden.py tests/test_e2e_protocol_matrix.py
> tests/test_custom_routes_shadow.py tests/test_official_client_parity.py`
> → **210 tests collectés, 0 échec**, 1 xfail attendu (exit 0 ; les 7
> premiers fichiers seuls : **162 tests**).
> **Deux réserves honnêtes sur les chiffres de cette carte** :
> 1) la version antérieure de cette ligne annonçait « 203 tests » — le
>    décompte `--collect-only` réel est **162** pour ces fichiers, 210 avec
>    `test_official_client_parity.py`. Chiffre corrigé ici ;
> 2) le vert d'origine omettait `tests/test_official_client_parity.py` — or
>    c'est là que vivait la collision E3. Un run vert qui omet le fichier
>    concerné ne prouve rien : le vert avait été enregistré alors que la
>    vague cassait déjà un contrat.
>
> **C2** — `_ANTHROPIC_BINDING_BETA = "thinking-binding-controls-2026-08-01"`
> (`opencode.py:350`), la forme que le SDK patché émet réellement. La forme nue
> n'est pas un nom de beta reconnu : un amont strict y répond 400. Le test est
> passé en assertion **exacte par token** (`_beta_tokens`) et non plus
> substring — `"thinking-binding-controls" in "thinking-binding-controls-2026-08-01"`
> étant vrai, l'ancienne assertion ne pouvait plus distinguer les deux formes.
> Un test voisin verrouille en plus que l'union de `_ensure_anthropic_beta`
> conserve une forme **nue réclamée par le client** : si un ancien modèle
> l'exigeait, elle s'ajoute, elle ne se substitue pas.
> `pytest tests/test_opencode_client_parity_helpers.py tests/test_official_client_parity.py`
> → **48 tests, 0 échec** (exit 0).
>
> **Collision E3 trouvée puis tranchée — 2026-10-09.** Le premier full-suite
> après la vague a révélé un échec **déterministe** que les deux lots ci-dessus
> ne voyaient pas : `tests/test_official_client_parity.py::test_free_wire_body_chat_keeps_client_tools`,
> en `assert wire["tools"][0] is mine  # ordre préservé, shims ajoutés en fin`.
> Le test datait d'avant E3 et verrouillait l'ancien contrat ; le tri E3 de la
> jambe free faisait passer le shim `bash` devant l'outil client `Bash`
> (minuscule avant majuscule au niveau tertiaire de la collation). Les deux
> assertions ne pouvaient pas être vraies en même temps.
>
> Tranché par la **source du client officiel**, pas par préférence
> (`packages/opencode/src/session/llm/request.ts`, HEAD `3884062` / v1.18.35) :
> il y a **un seul point de tri**, ligne 184 —
> `tools: Object.fromEntries(Object.entries(tools).toSorted(([a], [b]) => a.localeCompare(b)))`
> — et il est appliqué **après** que tous les outils sont réunis, shims de
> compatibilité inclus : la ligne 165 injecte elle-même un `tools["_noop"]`
> pour les providers qui exigent un champ tools, et ce `_noop` traverse le tri
> comme les autres (en tête, qui plus est : la ponctuation passe avant les
> alphanumériques en collation ICU). Donc les shims `bash`/`read` sont triés
> **avec** le reste, et c'est le test qui était faux. Corrigé, avec la
> justification mesurée dans sa docstring.
>
> Bilan de ce lot après correction :
> `pytest tests/test_official_client_parity.py tests/test_tool_order_parity.py tests/test_opencode_client_parity_helpers.py`
> → **61 tests, 0 échec** (exit 0).
>
> **Suite entière repassée après correction — 2026-10-09.**
> `python -m pytest -p no:cacheprovider -q` sur les **2650 tests collectés**
> (`--collect-only`, somme des comptes par fichier) : **4 échecs, tous
> préexistants et hors périmètre V2** —
> `tests/test_tool_schema_strict.py::TestStrictSubsetGuarantee::test_wire_schemas_strict_subset`
> et `::test_wire_schemas_idempotent` (déterministes, dépendent d'un dump
> `logs/free400_msg_323ba97cfd00-2b7.json` absent),
> `tests/test_free_response_sse_interface.py::test_route_responses_still_500s_without_sse_interface`
> (attend un libellé français, reçoit un JSON anglais),
> `tests/test_max_completion_tokens.py::test_helper_ignores_non_positive_and_non_int_limits`.
> **La collision E3 a disparu de la suite** et aucun nouvel échec n'est apparu.
> Les deux rouges d'ordre/environnement relevés au passage
> (`test_protocol_matrix.py::test_axis_min_tokens_floor_p3_is_wired`,
> `test_thinking_e2e.py::test_T4_streaming_400_no_retry_after_started`)
> sont verts dans ce run : ils ne sont donc pas stables d'un run à l'autre.
> `PYTEST_EXIT=1` **uniquement** à cause des 4 préexistants — pas de vert
> fabriqué, pas de case cochée sur un run qui omet le fichier concerné.
>
> **Rollback** : revert d'un fichier par carte — `app/protocol/mapping.py` seul
> pour E3 (la ligne `_ANTHROPIC_BINDING_BETA` pour C2 reste alors, ce qui est
> sans effet), `opencode.py` seul pour C2. Les deux nouveaux fichiers de test se
> suppriment. `config.yaml` n'est **pas** touché par cette vague.

### V3 — Table des limites de sortie (socle d'E2 et de H)

- [x] Nouveau module pur `config/limits.py` (pattern `config/effort_caps.py` : lecture live via `yaml_get`, aucun import projet). **Fait 2026-10-09** — `LIMIT_KEY`, `output_limit()` (lookup exact puis insensible à la casse), `clamp_output_token_limit()` ; `logger = "config.settings"` ; `config/settings.py` n'y fait **aucune** référence (règle anti-cycles `config/*`).
- [x] Peuplement depuis le catalogue live (`limit.output` des 36 Go, puis des modèles free), **daté** dans `config.yaml`. **Fait 2026-10-09** — 59 entrées, **51 avec `output_limit`**, 8 sans (`deepseek-flash`, `hy3-preview`, `jev-1.13`, `jev-1.13-free`, `mimo-v2-omni`, `mimo-v2-pro`, `minimax-m2.5`, `omen-alpha` : aucune entrée catalogue). Relevé du catalogue `https://models.opencode.ai/api.json` (`opencode-go` primaire, `opencode` pour les ids `-free`), 10 valeurs distinctes, toutes ≥ 32 000.
- [x] Câblage du clamp dans `_set_output_token_limit` — conditionné à la présence d'une entrée : table vide = comportement d'origine. **Fait 2026-10-09** — `limit = _clamp_output_token_limit(limit, model)` juste après résolution client/16384, donc **avant** la branche `max_completion_tokens` : la valeur écrite est déjà bornée quelle que soit la forme émise.
- [x] `pytest tests/test_max_completion_tokens.py tests/test_suspect_tiny_output.py tests/test_conversion_golden.py tests/test_e2e_protocol_matrix.py` — **Fait 2026-10-09** : ces quatre fichiers plus `tests/test_output_limit_clamp.py`, puis **suite entière** — 2 675 tests collectés, **4 échecs préexistants, aucun nouveau** (bilan dans la carte §18 E2). *Revérifié le 2026-10-12 : 180 collectés, 179 passés + 1 xfail connu, EXIT=0.*
- [ ] Live sur les 5 modèles les plus utilisés : vérifier qu'aucune réponse ne se retrouve tronquée à la limite. — **PENDANT** : fenêtre d'exploitation (comme la mesure de cache de V2). Déjà acquis côté code : le clamp ne peut qu'**abaisser** une demande, jamais l'élever, et il ne s'applique qu'aux modèles dont le catalogue donne une limite — 8 de nos 59 ids n'en ont aucune, dont `minimax-m2.5`, la route haiku, qui reste donc exactement comme avant.
- [ ] Rollback : vider la table → comportement d'origine. **Ne PAS `git checkout -- config.yaml`** : l'arbre est déjà modifié vs HEAD sur ~30 fichiers (règle V1) ; le rollback config passe toujours par une copie de sauvegarde `config.yaml.v<N>.bak.<date>` prise avant la vague.

**Pourquoi la clé ne casse rien (démontré).** `MODELS` (`config/settings.py:815-874`) est construit sur un jeu de clés **fixe** (`endpoint`, `protocol`, `api`, `capabilities`) : `output_limit` y est ignoré, donc invisible à la résolution d'endpoint, à la découverte et au dashboard. Mais `config/limits.py` lit via `yaml_get("models")`, qui rend le `_yaml_data` **brut** (`config/settings.py:296-315`) : la clé est donc bien lue par le clamp, et nulle part ailleurs. Hot-reload sans redémarrage (swap de `_yaml_data` par référence, `config/settings.py:1357-1379`). Aucun écrivain ne peut la perdre : `persist_free_mappings` (`config/discovery.py:489-494`) est **additionnel** (`if mid not in yaml_data["models"]`) et n'écrit que `{"protocol": proto}` pour un id nouveau.

**Le plafond à 32 000 du client officiel n'est pas repris** (§14.3) : c'est un défaut réglable de ce client, pas une loi de l'amont. Ce qui est borné, c'est la limite réelle du modèle. Aucune valeur de catalogue n'est sous 16 384 (minimum 32 000), donc le clamp ne peut **jamais** abaisser le défaut interne — il ne fait que rabattre une demande client au-dessus de la limite réelle.

### V4 — Routes par défaut + effort allow-list

- [ ] **B1** — `sonnet` → `glm-5.3`, `haiku` → `claude-haiku-5-5`, **uniquement** si le canary de la V1 confirme que `glm-5.1` / `minimax-m2.5` sont refusés ou moins bons (§4 : ne jamais basculer à l'aveugle).
- [x] **C1** — clé `thinking.reasoning_options` + clamp allow-list, derrière `thinking.allow_list_enabled` (défaut `false`), puis bascule — **fait 2026-10-10** : module + table + câblage en dernière étape de `resolve_effort` + 56 tests. **Le flag reste à `false`** : la bascule est la case « Live » de la carte §18 C1, pas celle-ci.
- [x] `pytest tests/test_effort_caps.py tests/test_effort_policy.py tests/test_effort_mapping.py tests/test_thinking_effort_mapping.py tests/test_free_leg_effort_recompute.py` — **vert 2026-10-10**, élargi au nouveau `tests/test_reasoning_options.py` : **365 tests, exit 0**. *Revérifié le 2026-10-12 : 253 collectés, 253 passés, EXIT=0 (l'écart avec 365 tient aux fichiers ajoutés/retirés depuis le 2026-10-10).*
- [ ] Live : un niveau d'effort par modèle concerné par le tableau du §5 (les 9 lignes) — **PENDANT**, et conditionné à la bascule du flag : la vague livrée est désarmée.
- [x] Rollback : `routing.*` → modèles précédents ; `allow_list_enabled: false` → ancien comportement — **opérationnel 2026-10-10** (c'est l'état livré). Copie `config.yaml.v4.bak.2026-10-10` prise. **Jamais** `git checkout -- config.yaml` (arbre déjà modifié vs HEAD, règle V1).

### V5 — Identité filaire (jambe payante uniquement)

- [ ] **D1** — re-mesure du vrai client **1.18.35** (`scripts/_probe_official_vs_proxy.py`, `scripts/_probe_useragents.py`), puis mise à jour des deux constantes.
- [x] **D2** — ajout de `x-opencode-session-id` dans `_enrich_paid_wire_headers` — **fait 2026-10-10**, voir carte §18 D2 (15 tests dans `test_paid_wire_identity.py`, jambe free intacte).
- [x] `pytest tests/test_paid_wire_identity.py tests/test_official_client_parity.py` — **vert 2026-10-10**, élargi aux voisins du fil free : `test_opencode_client_parity_helpers.py`, `test_free_wire_complet.py`, `test_free_multi_attempt.py`, `test_go_only_routing.py`. *Revérifié le 2026-10-12 : 112 collectés, 112 passés, EXIT=0.*
- [ ] **Jambe free : aucune modification** (jeu de 7 en-têtes verrouillé, couplage UA ↔ empreinte TLS — lot I3).
- [ ] Rollback : revert des deux constantes / d'une ligne d'en-tête.

### V6 — Flux, erreurs, reprises, hygiène

- [x] **F1** — relais de l'événement `error` amont au lieu de l'avaler — **fait 2026-10-10**, voir carte §18 F1 (un seul correctif dans `app/protocol/chat_sse_to_anthropic.py`, qui couvre les deux sites d'appel ; 4 classes de tests, 100 tests verts sur ce fichier).
- [x] **F2** — comptage du `cache_read_input_tokens` du `message_delta` sur la jambe Anthropic — **fait 2026-10-10**, voir carte §18 F2 (helper `opencode._stream_cache_delta` partagé par les deux boucles ; `tests/test_stream_cache_delta.py`, 15 tests dont le non double-comptage sur `usage` cumulatif). Reste la confirmation Live.
- [x] **G1** — classement des codes d'erreur amont dans `core/errors.py`. **Fait 2026-10-10** (§18 : 13 sites câblés, 73 tests, suite complète verte).
- [x] **G2** — budget de reprises exposé (`upstream.max_retries`), **défaut inchangé** — **fait 2026-10-10** (§18 : un seul point de lecture `opencode._upstream_max_retries()`, 5 sites migrés, `upstream/retry.py:111 resolve_max_retries` borné `[1,10]` et sans raise ; `tests/test_upstream_retry_budget.py` + `tests/test_retry_helper.py`). Documenté dans `docs/tuning.md`.
- [x] **H1** — purge des modèles morts + job de diff catalogue en lecture seule. (Décompte revu dans la carte §18 : **10** réellement morts, `jev-1.13*` et 2 modèles encore vivants sur la jambe anonyme exclus de la purge.) **Fait le 2026-10-11.** Le décompte prévu de « 10 réellement morts » ne tenait pas : re-vérifié contre les deux jambes du catalogue, il n'y en a que **5** — `mimo-v2-pro`, `mimo-v2-omni`, `hy3-preview`, `omen-alpha`, `deepseek-flash` (carte 1253). Les 7 autres hors-jambe des 12 annoncés se répartissent en 5 ids **vivants côté anonyme** et routés côté payé (`glm-5`, `glm-5.1`, `kimi-k2.5`, `minimax-m2.5`, `qwen3.5-plus` — conservés, carte 1254) et 2 ids à endpoint interne `systemone` (`jev-1.13`, `jev-1.13-free` — conservés, carte 1256) ; 5 + 5 + 2 = 12, l'inventaire boucle. Purge tenue après restart grâce aux tombeaux `free_discovery.purged_ids` (cinq points de garde, carte 1261) et fenêtre d'exploitation franchie par l'utilisateur le 2026-10-11 (carte 1262) : 54 entrées dans `models:`, 5 ids absents, 49 tests verts sur les deux fichiers de verrou, suite complète verte (3121 passés). Job `scripts/check_models_catalog.py` en lecture seule, désactivé par défaut, aucun accès en écriture à `config.yaml`. Seule carte encore marquée ouverte sous H1 : les commentaires de `config.yaml` (carte 1263), cochtée au sens où son livrable est la **décision documentée** — il ne reste aucun travail en attente.
- [x] `pytest tests/test_chat_sse_to_anthropic.py tests/test_streaming_sse.py tests/test_retry_helper.py tests/test_upstream_retry_budget.py tests/test_faux_429_veridique.py tests/test_server_compaction.py tests/test_protocol_matrix.py tests/test_e2e_protocol_matrix.py tests/test_conversion_golden.py` — **roulant** : F1/F2/G1/G2 verts — **vert 2026-10-12 : 385 collectés (384 passés + 1 xfail strict, EXIT=0). H1, N3, N4 faits depuis : la queue « reste H1, N3, N4 » est close.**
- [ ] Live : erreur provoquée (modèle saturé, prompt trop long) et vérification que le **client** la classe correctement.
- [ ] Rollback : chaque carte du §18 porte le sien ; F1 et G1 se retirent par drapeau ou revert d'un bloc ; **G2 par simple suppression de `upstream.max_retries`** (l'historique `streaming.retry_attempts` reprend la main, valeur inchangée).

---

## 18. Cartes d'implémentation — cases à cocher

Une carte par écart du §2. Chaque case se coche **quand le livrable existe et est
vérifié**, jamais quand le code est seulement écrit. Toutes les lignes de code
citées sont re-vérifiées le **2026-10-09** sur l'arbre de travail modifié (non
commité) ; si une ligne a bougé, la carte **se re-vérifie** — **le lot, lui, ne
change pas**.

Légende des verdicts : *contrainte amont → aligner*, *contrainte proxy → ne pas
aligner*, *défaut chez nous → corriger* (§14.2).

### A1 — Cinq modèles vers `/responses`

- **Réf.** §2 A1 (P0) · §3 · verdict §14.2 : contrainte amont, **à aligner**
- **Ancrages.** `config.yaml:79-81` `gpt-5.6-luna`, `:82-84` `grok-4.5`, `:107-109` `grok-4.6`, `:146-148` `grok-4.7`, `:151-153` `gpt-6-luna` (positions **post-édition** ; le plan citait `:79/:81/:105/:143/:147` avant l'insertion des modèles V1) · mécanisme existant `config/settings.py:693 get_model_api` + `:818 _RESPONSES_ENDPOINT` · convertisseur `app/protocol/mapping.py:4342-4740`
- **Faire.** Ajouter `api: responses` aux 5 entrées, `protocol: openai` inchangé. Canary sur **un** modèle d'abord (`grok-4.7`), puis les 4 autres.
- **Piège.** Le convertisseur Responses a des limites connues (§3) : `tool_choice` limité à la chaîne `"auto"`, erreurs indistinguables → ne pas y router un modèle utilisé avec outils nommés.
- [x] Les 5 entrées portent `api: responses` (diff relu) — **Fait 2026-10-09**
- [ ] Canary live 200 + `usage` + `cache_read_input_tokens` — **PENDANT** : subordonné au restart ci-dessous (fenêtre d'exploitation). Porter le canary sur les **4 modèles opérationnels par leur propre ligne** : `grok-4.5`, `grok-4.6`, `grok-4.7`, `gpt-6-luna`. `gpt-5.6-luna` est *shadowé* par `custom_routes.gpt56luna` (§19.3) : il atteint déjà `/go/v1/responses` via `muse-spark-1.3-contributor`, sa ligne ajoutée est inerte mais son intention est satisfaite — le vérifier quand même, mais ne pas y voir une preuve de la ligne ajoutée.
- [x] `pytest tests/test_model_catalog.py tests/test_go_only_routing.py tests/test_responses_stream_contract.py tests/test_responses_stream_e2e.py` — **vert 2026-10-09** (168 passed en y ajoutant `test_protocol_matrix.py` + `test_conversion_golden.py`). *Revérifié le 2026-10-12 (variante élargie à 6 fichiers) : 199 collectés, 199 passés, EXIT=0.*
- [x] `python scripts/gen_golden_fixtures.py && git diff --stat docs/v1-response-golden/` → aucun delta inattendu — **vérifié 2026-10-09 : BYTE-IDENTIQUE** (régénération puis `git status --porcelain` vide : ajouter une clé `api:` sous `models:` ne touche aucune fonction de conversion pure, donc le golden reste un contrôle de régression valide)
- [ ] Restart local (tuer `pythonw opencode.py --gui`, relancer) + requête réelle Claude Code sur `grok-4.7` — **PENDANT** : fenêtre d'exploitation, le proxy est vivant et porte la session courante
- [ ] **Rollback** : retrait des 5 lignes `api: responses`

### A2 — `qwen3.6-plus` / `qwen3.7-max` en OpenAI

- **Réf.** §2 A2 (P0) · §3 · contrainte amont, **à aligner**
- **Ancrages.** `config.yaml:59-60` `qwen3.7-max`, `:63-64` `qwen3.6-plus` · convertisseur `anthropic_to_openai`
- **Faire.** `protocol: openai` sur les deux entrées ; `qwen3.7-plus`, `qwen3.8-max`, `qwen3.8-flash` restent en Anthropic (vérifié catalogue).
- **Piège.** La jambe Anthropic était un passthrough sans conversion ; la nouvelle passe par `anthropic_to_openai` — plus de surface, mais chemin déjà massivement testé.
- [x] Les 2 entrées en `protocol: openai` ; les 3 voisines intactes — **Fait 2026-10-09**
- [ ] Canary live 200 sur les deux modèles, streaming et non-streaming — **PENDANT** : subordonné au restart ci-dessous (fenêtre d'exploitation)
- [x] `pytest tests/test_model_catalog.py tests/test_protocol_matrix.py tests/test_e2e_protocol_matrix.py tests/test_conversion_golden.py` — **vert 2026-10-10** (154 passed + 1 xfailed, `PYTEST_EXIT=0`), en isolation aussi pour le fichier isolé. **Le blocage annoncé ici n'était pas A2** : `test_e2e_protocol_matrix.py` était rouge à cause du shadow `custom_routes.musespark13contributor`, cause **externe à A1/A2/B2** et désormais traitée dans le Lot Z §19.5 (seam + `tests/test_custom_routes_shadow.py`). Preuve que la cause était bien étrangère à A2 : elle figure déjà dans `config.yaml.v1.bak.2026-10-09`, backup d'**avant** les éditions de route. → case A2 cochée sur son propre mérite, pas sur le shadow. *Revérifié le 2026-10-12 : 165 collectés, 164 passés + 1 xfail connu, EXIT=0 (l'écart avec 154 tient aux tests ajoutés depuis le 2026-10-10).*
- [ ] Restart local + une requête réelle avec outils sur `qwen3.7-max` — **PENDANT** : fenêtre d'exploitation
- [ ] **Rollback** : `protocol: anthropic` (1 ligne chacun)

### A3 — `minimax-m2.5` (sorti du plan Go)

- **Réf.** §2 A3 (P0) · §3 · contrainte amont, **à aligner avec le lot B**
- **Ancrages.** `config.yaml:57-58` · `routing.haiku_model` `config.yaml:25`
- **Faire.** Pas de simple changement de protocole : le modèle n'est plus dans le catalogue Go. Décision de route (V4 / carte B1) — c'est `minimax-m2.7` qui reprend la place en Anthropic si la route haiku doit rester en passthrough.
- [x] Décision explicite consignée ici — **arbitrage utilisateur du 2026-10-12 : garder la route qui marche** (`routing.haiku_model: minimax-m2.5` → jambe libre `mimo-v2.6-flash-free`, 200 mesuré) ; bascule 1b vers `claude-haiku-5-5` **refusée en connaissance de cause** (le flip appliqué puis reverté le même jour via `config.yaml.v7.bak.2026-10-12`, SHA256 vérifié : sans clé payante valide, chaque appel haiku rendrait 403 au lieu du 200 via free). `claude-haiku-5-5` reste appelable directement. A3 re-reportée.
- [x] Si conservé : route free vérifiée en live — **fait le 2026-10-12** : canary N2 `mimo-v2.6-flash-free` 200 + `usage` (non-stream `input 163 / output 42`, stream 17 events ; `cache_read 0` relevant de N5, pas d'un défaut de route).
- [ ] Si basculé : carte B1 complète
- [ ] **Rollback** : retour à la ligne précédente de `routing.haiku_model` / `models.minimax-m2.5`

### B1 — Routes par défaut mortes

- **Réf.** §2 B1 + **N1** (P0 bloquant) · §4 · verdict §14.6 : contrainte amont, **à aligner**
- **Ancrages.** `config.yaml:23-25` → lecture `config/settings.py:1128-1140`
- **Faire.** Les **trois** routes sont touchées (N1) : `sonnet` → `glm-5.3` (1 M / 131 k), `haiku` → `claude-haiku-5-5` (Anthropic, 1 M / 128 k), et `opus` → à trancher : `kimi-k2.6` est encore dans le catalogue Go mais `deprecated`, donc à garder **provisoirement** ou à remplacer par `mimo-v2.6-pro` / `mimo-v2.5-pro` (tous deux `/chat/completions`, 1 M). **Condition** : le canary live doit d'abord établir si `/zen/go/v1` refuse encore `glm-5.1` / `minimax-m2.5` / `kimi-k2.6`. Ne jamais basculer à l'aveugle.
- **Piège.** `glm-5.3` change le comportement perçu de tous les clients `sonnet` (formatage, outils). À valider sur une vraie session, pas sur un ping. Le fait que tout le trafic payant récent soit en **401** masque complètement l'état réel du plan Go : B1 ne peut pas être validé sans une clé payante valide, donc sans décision explicite de l'exploitant.
- [ ] Canary live `glm-5.1` + `minimax-m2.5` + `kimi-k2.6` sur `/zen/go/v1` → verdict explicite (accepté / refusé / 401-clé)
- [ ] Candidats (`glm-5.3`, `claude-haiku-5-5`, remplaçant `opus`) testés en streaming et non-streaming
- [ ] Bascule des 2 puis 3 lignes `routing.*` (les deux premières d'abord, `opus` seulement si le canari est net)
- [ ] Session Claude Code complète sur `sonnet` puis `haiku` (outils + thinking)
- [x] `pytest tests/test_go_only_routing.py tests/test_model_catalog.py tests/test_lot6_config.py` — **vert 2026-10-12 : 43 collectés, 43 passés, EXIT=0** (la carte B1 reste verrouillée par son canary live, mais la ligne pytest est vérifiée indépendamment).
- [ ] **Rollback** : `routing.opus_model: kimi-k2.6`, `routing.sonnet_model: glm-5.1`, `routing.haiku_model: minimax-m2.5` — **et `.env` pareil** (`OPUS_MAP_MODEL`/`SONNET_MAP_MODEL`/`HAIKU_MAP_MODEL`, piège ENV-OVER-YAML carte N6 : l'env gagne sur le YAML).

### B2 — `claude-haiku-5-5` mal routé

- **Réf.** §2 B2 (P0) · §4 · contrainte amont, **à aligner**
- **Ancrages.** `config.yaml:166-167` `claude-haiku-5-5` (entrée **déjà présente**, passée de `protocol: openai` à `protocol: anthropic` le 2026-10-09 — c'était l'erreur) · défaut de secours `config/settings.py:1557 get_model_config` (l'ancre `:1523` du plan d'origine est périmée)
- **Faire.** Passer l'entrée à `protocol: anthropic` : le catalogue dit `@ai-sdk/anthropic` → `/v1/messages`. Sans ça, un client qui demande ce nom tape `/chat/completions`.
- [x] `protocol: anthropic` sous `models.claude-haiku-5-5` — **Fait 2026-10-09** (`config.yaml:166-167`)
- [ ] Canary live 200 sur `/zen/go/v1/messages` + `cache_read_input_tokens` non nul — **PENDANT** : subordonné au restart (fenêtre d'exploitation)
- [x] `pytest tests/test_model_catalog.py tests/test_go_only_routing.py` — **vert 2026-10-09** (compris dans les 38 + 97 verts du V1)
- [ ] **Rollback** : 1 ligne

### C1 — Effort : allow-list officielle

- **Réf.** §2 C1 (P1) · §5 · contrainte amont (le modèle rejette un effort qu'il ne porte pas), **à aligner**
- **Ancrages.** patterns longest-prefix à recopier : `config/effort_caps.py:69 get_effort_caps`, `:75-108 get_max_effort_for_model` · point d'entrée unique `config/effort_policy.py:225 resolve_effort` (+ `:207 clamp_level`) · graines du tableau §5 (9 modèles)
- **Faire.** 1) Nouvelle clé `thinking.reasoning_options` (modèle → liste autorisée, `budget_tokens.max` optionnel), même matching longest-prefix, insensible casse. 2) `resolve_effort` intersecte allow-list et plafond : `niveau_émis = plus grande valeur ≤ demandée dans l'allow-list`, repli sur la plus petite autorisée ; `none`/`off` inchangés. 3) Derrière `thinking.allow_list_enabled` (défaut `false`) : à `false`, comportement **identique** à aujourd'hui. 4) Les `effort_caps` existants restent en filet.
- **Piège (§14.5).** L'officiel raisonne sur `model.api.id`, nous remappons les alias → la table doit être indexée sur l'**id amont** (`kimi-k2.6`, `glm-5.1`…), jamais sur `opus`/`sonnet`/`haiku`.
- [x] Clé `thinking.reasoning_options` en `config.yaml`, valeurs issues du catalogue, datées — **Fait 2026-10-10**. 9 entrées (celles du §5), relevé du champ `reasoning_options` de `https://models.opencode.ai/api.json`. Sauvegarde `config.yaml.v4.bak.2026-10-10`. Flag `allow_list_enabled: false` (défaut).
- [x] Lecture live + clamp dans `resolve_effort`, flag `allow_list_enabled` câblé — **Fait 2026-10-10**. Nouveau module pur `config/reasoning_options.py` (pattern `config/effort_caps.py` : lecture live `yaml_get`, zéro import projet, `logger = "config.settings"`, `config/settings.py` ne le référence jamais). Câblé en **dernière** étape de `resolve_effort`, **après** `clamp_level` — et dans la branche `MODEL_MAX`, sur le plafond résolu.
- [x] `pytest tests/test_effort_caps.py tests/test_effort_policy.py tests/test_effort_mapping.py tests/test_thinking_effort_mapping.py tests/test_free_leg_effort_recompute.py` — **vert 2026-10-10** : ces cinq fichiers plus le nouveau `tests/test_reasoning_options.py` → **365 tests, 0 échec, exit 0**. *Revérifié le 2026-10-12 : 253 collectés, 253 passés, EXIT=0, JUnit `tests=253 failures=0 errors=0 skipped=0` (l'écart avec 365 tient aux fichiers ajoutés/retirés depuis le 2026-10-10).* **Suite entière** : 2 732 tests collectés, `PYTEST_EXIT=1` avec **exactement les 4 échecs préexistants, aucun nouveau** — `test_free_response_sse_interface::test_route_responses_still_500s_without_sse_interface`, `test_max_completion_tokens::test_helper_ignores_non_positive_and_non_int_limits`, `test_tool_schema_strict::TestStrictSubsetGuarantee::test_wire_schemas_strict_subset`, `::test_wire_schemas_idempotent`. Ce sont ceux relevés dès la fin de la V1 : la vague C1 n'en ajoute aucun et n'en corrige aucun (règle : on ne rattrape pas le rouge d'autrui pendant une vague de parité).
- [x] Test explicite du flag à `false` = comportement historique bit pour bit — **Fait 2026-10-10**, et c'est le **premier** test du fichier : `test_flag_off_is_bit_for_bit_the_table_absent` compare, pour 12 modèles × 15 formes de requête, `resolve_effort` avec la table **peuplée** + flag `false` contre la table **absente** — les 4 champs de `EffortDecision`, pas seulement `level`. Plus deux gardes structurelles sur le vrai `config.yaml` : le flag livré doit valoir `false`, et le clamp ne doit jamais sortir de l'allow-list quelle que soit la table du jour.
- [ ] Live : un niveau par modèle du tableau §5, aucun 400 — **PENDANT** : fenêtre d'exploitation. Rien à vérifier tant que le flag est à `false` : la vague est livrée désarmée, la bascule est l'objet de cette case.
- [ ] **Rollback** : `allow_list_enabled: false` (le clamp se court-circuite) — **c'est déjà l'état livré**, donc rien à défaire ; la copie `config.yaml.v4.bak.2026-10-10` couvre le cas où le hot-reload aurait abîmé le fichier. **Ne PAS `git checkout -- config.yaml`** (règle V1/V3 : l'arbre est déjà modifié vs HEAD sur ~30 fichiers).

### C2 — Beta Anthropic daté

- **Réf.** §2 C2 (P1) · §5 · contrainte amont (valeur d'en-tête = contrat serveur), **à aligner**
- **Ancrages.** `opencode.py:350 _ANTHROPIC_BINDING_BETA` · union `opencode.py:358 _ensure_anthropic_beta`
- **Faire.** Constante → `thinking-binding-controls-2026-08-01`. Si un modèle ancien exigeait la forme nue, émettre les deux séparées par une virgule — l'union est déjà le comportement de `_ensure_anthropic_beta`.
- [x] Constante corrigée en la forme **datée** — **Fait 2026-10-09** (`opencode.py:350`). La forme nue n'est pas un nom de beta reconnu : un amont strict y répond 400. Ce que le SDK patché émet réellement (`@ai-sdk%2Fanthropic@3.0.111.patch:133`) est `thinking-binding-controls-2026-08-01`.
- [x] Assertions dans `tests/test_opencode_client_parity_helpers.py` — **Fait 2026-10-09**, et **renforcées après mesure** : l'assertion d'origine était `assert "…-2026-08-01" in h["anthropic-beta"]`, ce qui passait **aussi** avant le correctif, parce que `"thinking-binding-controls" in "thinking-binding-controls-2026-08-01"` est vrai. Elle ne pouvait donc pas distinguer les deux formes — le genre d'assertion qui rend un correctif vert sans avoir rien prouvé. Elle est remplacée par `_beta_tokens(h)` (tokens **exacts**, séparés par des virgules), plus un test d'idempotence (`.count(...) == 1`, pas de doublon au rappel) et un test d'union : `_ensure_anthropic_beta` conserve en plus une forme **nue réclamée par le client** (`_current_client_anthropic_betas`). L'union reste le filet, comme le prévoyait la carte.
- [x] `pytest tests/test_opencode_client_parity_helpers.py tests/test_official_client_parity.py` — **vert 2026-10-09** (48 tests, 0 échec, exit 0). *Revérifié le 2026-10-12 : 48 collectés, 48 passés, EXIT=0, JUnit `tests=48 failures=0 errors=0 skipped=0` — décompte identique.* **Historique de ce lot, parce qu'il a failli devenir un faux vert** : ce run de 48 était passé alors que `tests/test_official_client_parity.py::test_free_wire_body_chat_keeps_client_tools` échouait déjà de façon déterministe sous l'effet du tri E3 de la jambe free — le vert avait été enregistré avant l'ajout du tri dans `_free_wire_body`, pas après. L'échec a été trouvé au full-suite suivant, puis tranché par la source officielle (un seul point de tri, après injection des shims de compatibilité → les shims se trient avec le reste, c'est le test qui était faux). Voir le détail dans la carte V2.
- [ ] Live : une requête avec thinking binding → pas de 400 gateway — **PENDANT** : fenêtre d'exploitation (le proxy est vivant et porte la session courante ; pas de restart de mon initiative)
- [ ] **Rollback** : 1 ligne de constante (`opencode.py:350`) — testable sans restart puisqu'aucune config n'est touchée

### D1 — User-Agent à jour

- **Réf.** §2 D1 (P1) · §6 · **défaut chez nous** (maintenance de notre propre en-tête)
- **Ancrages.** `opencode.py:4684` (UA `/chat/completions`), `:4692` (variante `/responses`) · re-mesure `scripts/_probe_official_vs_proxy.py`, `scripts/_probe_useragents.py`
- **Faire.** **Re-mesurer d'abord** sur le vrai client 1.18.35 (UA exact + version `provider-utils` + runtime pour les deux endpoints), puis mettre à jour les deux constantes dans le même commit.
- **Piège.** Sur la jambe free, UA et empreinte TLS Bun sont **couplées** (lot I3) : ne jamais toucher un UA sans l'autre.
- [ ] Capture du UA 1.18.35 conservée (date + source)
- [ ] Les 2 constantes à jour
- [x] `pytest tests/test_paid_wire_identity.py tests/test_official_client_parity.py` — **vert 2026-10-12 : 40 collectés, 40 passés, EXIT=0** (la carte D1 reste verrouillée par la re-mesure 1.18.35, mais la ligne pytest est vérifiée indépendamment).
- [ ] Live : une requête payante par endpoint, 200
- [ ] **Rollback** : revert des 2 constantes

### D2 — `x-opencode-session-id`

- **Réf.** §2 D2 (P2) · §6 · **défaut chez nous**
- **Ancrages.** `opencode.py:5407 _enrich_paid_wire_headers` (envoie déjà `x-opencode-client` / `-project` / `-session` / `-request`) · `:5369 _paid_session_id` (source du `ses_`)
- **Faire.** Ajouter `x-opencode-session-id` = même valeur que `x-opencode-session` (sémantique officielle). **Jambe free : rien tant qu'elle n'est pas re-mesurée** — la capture verrouille un jeu de 7 en-têtes (`tests/test_official_client_parity.py:70-100`).
- [x] En-tête ajouté dans `_enrich_paid_wire_headers` — **fait 2026-10-10** : bloc `setdefault` calé juste après `x-opencode-session`, qui en recopie la valeur (`request.ts:188-189` l'émet avant les `opencode*`, `:190-197` — deux noms, un sessionID). Borné aux endpoints OpenAI-protocol : le retour anticipé existant exclut déjà chemin Anthropic et jambe free. Si le `ses_` n'a pas pu être minté, l'en-tête est **absent** plutôt qu'orphelin. `tests/test_paid_wire_identity.py` : **8 tests ajoutés** (15 au total), dont égalité avec `x-opencode-session` sur les deux jambes OpenAI, affinité par clé de conversation, miroir d'un `ses_` fourni par l'appelant, non-réécriture d'une valeur déjà posée, absence sur la jambe Anthropic, absence si minting en échec, insensibilité à la casse de la clé entrante, jambe free inchangée.
- [x] `pytest tests/test_paid_wire_identity.py tests/test_official_client_parity.py` — **vert 2026-10-10**, élargi à `test_opencode_client_parity_helpers.py`, `test_free_wire_complet.py`, `test_free_multi_attempt.py`, `test_go_only_routing.py` (voisins du fil free verrouillé) : **exit 0**.
- [ ] Live : en-tête présent sur une requête payante, absent sur free — **PENDANT** (fenêtre live).
- [x] **Rollback** : suppression du seul bloc `if "x-opencode-session-id" not in lowered:` (`opencode.py:5448-5459`).

### E1 — `tool_choice` émis sans demande

- **Réf.** §2 E1 (P2) · §7 · **défaut chez nous**
- **Ancrages.** `app/protocol/mapping.py:1757 tc = body.get("tool_choice", "auto")` puis bloc d'émission `:1757-1799` (dont la branche `parallel_tool_calls`)
- **Faire.** N'émettre `tool_choice` que si la clé existait dans le corps client. `disable_parallel_tool_use` continue de fonctionner seul. Drapeau de repli `protocol.emit_default_tool_choice` (défaut `false`).
- **Risque.** Certains gateways changent de comportement sans le champ — d'où le drapeau.
- [x] Émission conditionnée à la présence de la clé client — **Fait 2026-10-12** : site A (`app/protocol/mapping.py`, bloc `tools`) ne pose plus `tool_choice: "auto"` d'office ; `tc = None` → champ absent. Sites B/C (directions inverses) hors scope, notés. `tests/test_tool_choice_emit.py` (11 tests).
- [x] Drapeau câblé + test du drapeau à `true` (ancien comportement) — **Fait 2026-10-12** : `protocol.emit_default_tool_choice` (défaut `false`, lu via `yaml_get` au moment de la conversion, hot-reload OK) ; clé réelle ajoutée en queue de `config.yaml`, feuille documentée dans `docs/models-protocol.md`, clé verrouillée dans `docs/_drift_manifest.json`. `disable_parallel_tool_use` lu sur la valeur brute : `parallel_tool_calls: false` survit aux corps sans clé.
- [x] `pytest tests/test_protocol_matrix.py tests/test_conversion_golden.py tests/test_tool_compat.py tests/test_tool_name_sanitization.py` — **vert 2026-10-12 (EXIT=0)**, élargi à `test_tool_choice_emit.py` + voisins (`test_tools_matrix.py`, `test_tool_choice_responses_shape.py`, `test_p3_tool_names.py`, `test_docs_drift.py`, `test_conversion_cache.py`), + `ruff check` vert. Notes : (1) `test_tool_compat.py` = script live httpx, **0 test collecté** — non compté ; (2) 5 goldens (`req_tools_complex`, `p2_cache_control_{breakpoint_limit,tools}`, `p2_tools_{invalid_schema,long_name_strict_schema}`) figeaient l'ancien défaut → **régénérés via `scripts/gen_golden_fixtures.py`, diff revu : seule la ligne `"tool_choice": "auto"` disparaît des `expected`** (changement volontaire E1, revue manuelle §11.5 faite). **Suite complète post-E1 2026-10-12 : tests=3188, failures=0, errors=0, skipped=5 (`--junit-xml`, EXIT=0)** — aucun autre test n'assertait l'ancien défaut.
- [ ] Live : requête sans `tool_choice` → champ absent du corps amont ; requête avec → inchangé
- [ ] **Rollback** : `emit_default_tool_choice: true`

### E2 — `max_tokens` : défaut figé, aucun plafond

- **Réf.** §2 E2 (P1) · §7 · verdict §14.3 : **défaut chez nous, incohérent dans les deux sens**
- **Ancrages.** `app/protocol/mapping.py:319 _set_output_token_limit`, `:322 default: int = 16384`, `:368` et `:376` (recopie telle quelle) · table à créer · seule limite existante du dépôt : `app/compaction/summarizer.py` + `config.yaml:466 summary_clamp_max_tokens`
- **Faire.** 1) Table `models.<id>.output_limit` alimentée par `limit.output` du catalogue live (36 Go + sous-ensemble free), datée. 2) Dans `_set_output_token_limit`, **après** résolution client/16384 : si une entrée existe, `limit = min(limit, output_limit)`. 3) Le clamp s'applique avant la branche `max_completion_tokens` (`config.yaml:543-547`). 4) **Ne pas** introduire de plafond global à 32 000 (§14.3 : c'est un défaut du client officiel, réglable par variable d'environnement, pas une loi de l'amont — le copier régresserait nos autres clients).
- **Condition.** Table vide ⇒ comportement d'origine : pas de clamp. Le clamp n'existe que là où le catalogue donne une limite.
- [x] Table peuplée et datée dans `config.yaml` — **Fait 2026-10-09**, voir carte V3 (59 entrées, 51 avec limite, 8 sans entrée catalogue).
- [x] Clamp câblé, testé avec une valeur cliente absurde (ex. 200 000) — **Fait 2026-10-09** : `kimi-k2.6` (65 536) et `glm-5.1` (131 072) rabattus, `minimax-m2.5` (hors table) laissé à 16384. `tests/test_output_limit_clamp.py`, 25 tests, dont les pièges booléen/float/chaîne et « clamp jamais ascendant ».
- [x] `pytest tests/test_max_completion_tokens.py tests/test_suspect_tiny_output.py tests/test_conversion_golden.py tests/test_e2e_protocol_matrix.py` — **vert 2026-10-09**, plus la **suite entière** : `python -m pytest -p no:cacheprovider -q` sur **2 675 tests collectés** (2 650 + les 25 du nouveau fichier) ⇒ **4 échecs, tous préexistants et hors périmètre V3** — `test_free_response_sse_interface.py::test_route_responses_still_500s_without_sse_interface`, `test_max_completion_tokens.py::test_helper_ignores_non_positive_and_non_int_limits`, et les deux `test_tool_schema_strict.py::TestStrictSubsetGuarantee`. **Aucun nouvel échec.** `PYTEST_EXIT=1` uniquement à cause de ces 4-là. *Revérifié le 2026-10-12 : 155 collectés, 154 passés + 1 xfail connu, EXIT=0, JUnit `tests=155 failures=0 errors=0 skipped=1`.*
- [ ] Live : 5 modèles les plus utilisés, aucune troncature nouvelle, `stop_reason` conforme
- [ ] **Rollback** : vider la table

### E3 — Outils non triés

- **Réf.** §2 E3 (P2) · §7 · **défaut chez nous**, trivial
- **Ancrages.** `app/protocol/mapping.py:3760 _sanitize_chat_tools` (ordre client préservé) · contrainte free `opencode.py:11807` (shims `bash`/`read`)
- **Faire.** Trier la liste finale sur `function.name` (`toSorted(localeCompare)` côté officiel, `request.ts:184`).
- **Piège.** Le tri change le préfixe → peut faire varier `cache_read_input_tokens`. C'est un critère d'arrêt explicite (§16). Sur la jambe free, les shims `bash`/`read` doivent rester triés avec le reste.
- [x] Tri appliqué — **Fait 2026-10-09**, sur **trois** sites et non un seul (le détail dans la carte V2) :
  - `_sorted_chat_tools` (`app/protocol/mapping.py`, au-dessus de `_sanitize_chat_tools`) — le tri, avec son garde-fou (liste malformée renvoyée **inchangée**) ;
  - `_sanitize_chat_tools` — trie **même sur le fast-path** (aucun nom à raccourcir), parce que l'ordre officiel est une propriété du fil et pas un effet de bord du raccourcissement ;
  - `_free_wire_body` (`opencode.py`) — shims `bash`/`read` triés **avec** le reste, jambe Chat uniquement.
- [x] **`localeCompare`, pas `<`** — c'est le point dur de la carte, trouvé et corrigé le jour même. Voir la carte V2 pour la mesure contre le vrai collateur (395 noms, ordre identique) et les deux modèles de collation qui échouent.
- [x] **Shims triés AVEC le reste : confirmé par la source officielle** (`packages/opencode/src/session/llm/request.ts`, HEAD `3884062` / v1.18.35, récupéré le 2026-10-09). Le client a **un seul** point de tri, `:184` — `Object.fromEntries(Object.entries(tools).toSorted(([a], [b]) => a.localeCompare(b)))` — appliqué **après** que tous les outils sont réunis : `resolveTools` (filtre permissions/utilisateur), puis `strict: false` forcé pour la famille OpenAI (`:152-158`), puis l'injection d'un shim de compatibilité `tools["_noop"]` pour les providers qui exigent un champ tools (`:159-175`). Ce `_noop` est donc trié comme les autres, et arrive en tête (la ponctuation passe avant les alphanumériques en collation ICU). Un shim injecté dans la map ne part donc **pas** en fin de liste — c'est exactement ce que fait `_free_wire_body`. Ce point a été tranché ici après qu'un test d'avant E3 eut affirmé le contraire : voir « Collision E3 trouvée puis tranchée » dans la carte V2.
- [x] `pytest tests/test_tool_name_sanitization.py tests/test_p3_tool_names.py tests/test_conversion_cache.py tests/test_conversion_golden.py` — **vert 2026-10-09** (compris dans les 210 tests verts de la carte V2 — 162 pour les 7 fichiers d'E3 seuls, plus `test_official_client_parity.py` — qui couvrent en plus `test_tool_order_parity.py`, 13 tests, et `tests/test_e2e_protocol_matrix.py` + `tests/test_custom_routes_shadow.py`)
- [ ] Mesure avant/après du taux de cache sur un prompt réel (pas de effondrement) — **PENDANT** : fenêtre d'exploitation. C'est le seul point de la carte qui exige une mesure live, donc le seul qui reste ouvert ; le correctif étant par construction exactement l'ordre du client officiel, le préfixe — et donc le cache — devrait être **partagé avec lui** au lieu d'être propre à chaque ordre d'appel.
- [ ] **Rollback** : suppression du tri, **mais par site** — retirer l'appel dans `_sanitize_chat_tools` restaure l'ordre du client sur la jambe Chat ; retirer celui de `_free_wire_body` suffit à rendre son ordre antérieur. Les deux sont indépendants, et `_sorted_chat_tools` + `_tool_sort_key` se suppriment avec eux (`tests/test_tool_order_parity.py` aussi).

### F1 — Événement `error` amont avalé

- **Réf.** §2 F1 (P1) · §8 · contrainte amont (l'erreur existe, on la masque), **à aligner**
- **Ancrages.** `opencode.py:12181` relais d'un `event: error` nu (l'infrastructure existait) · deux sites d'avalement, tous les deux par le **même entonnoir** : jambe libre `opencode.py:12212` et P4 `opencode.py:16752` appellent `chat_sse_to_anthropic_events`, qui ne reçoit que des lignes `data:` (le filtre `_is_data_line` est interne)
- **Faire.** Sur réception d'une frame `{"error": …}` : propager l'enveloppe Anthropic `{"type":"error","error":{"type":…,"message":…}}` au lieu du `stop_reason: "error"` synthétisé à l'EOF, puis classer le `code` (carte G1).
- **Piège.** Ne pas inventer de forme : l'enveloppe Anthropic d'erreur en flux est terminée par la fermeture du flux, sans `message_stop`.
- [x] **Relais effectif sur les deux sites** — 2026-10-10. Un seul correctif, dans `app/protocol/chat_sse_to_anthropic.py`, couvre les deux appels (`feed_chunk` n'a qu'un appelant, `app/protocol/chat_sse_to_anthropic.py:573`). Le défaut était que `chat.completion.chunk` ne définit **aucune** clé `error` : sa présence est donc non ambiguë. La ligne était avalée par la branche « pas de choices » (`first_choice`/`delta` → `{}`, `feed_chunk` → `[]`), et le client n'apprenait la panne que par un `stop_reason: "error"` nu à l'EOF — sans `event: error`, sans message, sans type : impossible de classer (rate_limit / overloaded / api_error) ni de décider d'un retry.
  - Bloc F1 en tête de module : `_STATUS_ERROR_TYPES` (**`MappingProxyType`** — la table est au niveau module, `test_pas_detat_mutable_au_niveau_module` l'exige), `_TEXT_ERROR_TYPES`, `_status_error_type` (le `bool` est exclu : `True` ne doit pas se lire comme le statut 1), `_text_error_type`, `_upstream_error_shape`.
  - `ChatSseToAnthropicState._upstream_error_events` : `event: error` **puis** la queue de finalize habituelle. L'ordre est le point à ne pas inverser — un client qui ignore `event: error` hérite quand même d'une clôture propre (blocs fermés, `message_delta` avec `stop_reason: error`, `message_stop`) au lieu d'un bloc jamais fermé qui lui ferait jeter tout le texte déjà reçu. Idempotent via `finished`.
  - Branche dans `feed_chunk`, **après** la reprise d'usage : une passerelle qui rend son usage final à côté de l'erreur ne le perd pas. Le garde de vérité fait que `error: null`/`""`/`[]`/`{}`/`0`/`false` ne déclenchent rien.
  - Classification : `type` → `code` → `status`/`status_code` → message en texte libre → `api_error`. Les motifs texte **ne contiennent aucun chiffre nu**, pour que « 500 tokens écrits » ne devienne pas une surcharge.
  - `_upstream_error_shape` rend aussi une valeur scalaire nue (`{"error": 503}`) : elle porte une information, on la classe au lieu de la réduire à « upstream stream error ». (Trouvé par le test, pas par relecture.)
  - **Enveloppe déjà Anthropique** (`data: {"type":"error","error":{…}}` sans `event:`) : elle a une clé `error` de premier niveau et déclenche le même chemin — on la relaie **telle quelle**. La reclasser perdrait `param`/`request_id` et afficherait au client une erreur qui n'est pas la sienne.
- [x] `pytest tests/test_streaming_sse.py tests/test_responses_stream_contract.py tests/test_chat_sse_to_anthropic.py tests/test_sse_coalesce.py tests/test_sse_keepalive.py` — **vert 2026-10-10** (207 tests), plus la suite complète `tests/` : **2791 tests, 0 échec, exit 0**. *Revérifié le 2026-10-12 : 166 collectés, 166 passés, EXIT=0, JUnit `tests=166 failures=0 errors=0 skipped=0` (collect-only par fichier : 100+49+6+8+3 ; l'écart avec 207 tient aux tests ajoutés/retirés depuis le 2026-10-10).* 4 classes ajoutées à `tests/test_chat_sse_to_anthropic.py` (porté de 624 à 943 lignes, 100 tests) : `TestF1ErreurAmontDansLeFlux`, `TestF1ClassificationDuType` (paramétrée, 26 cas), `TestF1FormeDuWire`, et les garde-fous — miroir « une ligne sans clé `error` ne produit jamais d'`event: error` » (c'est lui qui garantit l'absence de doublon sur le relais `:12181`), enveloppe anthropique relayée telle quelle, erreur tardive après finalize.
- [ ] Live : provoquer une erreur en cours de flux → le client affiche le message, pas un silence — **PENDANT** : fenêtre d'exploitation.
- [ ] **Rollback** : retrait de la seule branche `feed_chunk` (bloc réversible, `_upstream_error_events` et les helpers partent avec). Tout est dans un fichier, aucun site d'appel modifié — le retour au `stop_reason: "error"` à l'EOF est la suppression de 3 lignes.

### F2 — `cache_read_input_tokens` du `message_delta`

- **Réf.** §2 F2 (P2) · §8 · **défaut chez nous**
- **Ancrages.** `opencode.py:12275 stream_cache = usage.get("cache_read_input_tokens", 0)` (lecture au `message_start` seulement) · second site : boucle de conversion P4, `opencode.py` `cache_read` lu au `message_start` puis rendu au `message_stop` · émetteur côté client `app/protocol/chat_sse_to_anthropic.py` `finalize_events` (le `message_delta` porte `cache_read_input_tokens`)
- **Faire.** Cumuler les deux sources : le `message_delta` écrase le `message_start` s'il porte une valeur.
- **Piège.** Ne pas **additionner** : plusieurs gateways envoient un `usage` cumulatif au delta. L'écrasement est la sémantique sûre, l'addition double-compterait.
- [x] Lecture du `message_delta` câblée (écrasement, pas addition) — 2026-10-10.
  - Helper pur **`opencode.py:10112 _stream_cache_delta(usage, current) -> (nouveau, delta) | None`** — un seul point de décision pour les deux sites : `None` si la clé est absente, si la valeur n'est pas un `int` (str, float, **bool** — `True` vaudrait 1 token), ou si elle est identique au courant. Le **delta** et pas la valeur nue : c'est lui qui rend l'écrasement comptablement juste sur un `usage` cumulatif.
  - **Site A** (boucle de suivi partagée, `elif etype == "message_delta"`) : crédite `delta` sous `_token_lock` (le total cumulé suit l'écrasement par la différence), puis `stream_cache = nouveau`. Ce que le `message_start` avait déjà crédité est déduit → un `usage` cumulatif n'est compté qu'une fois.
  - **Site B** (boucle de conversion P4) : `cache_read = nouveau`, écrasement seul — `cache_read` n'y étant crédité qu'au `message_stop`, il n'y a aucun cumul à corriger.
  - Les deux sites sont couverts d'un coup : le `message_delta` lu est le même, que le flux arrive en Anthropic natif ou via le convertisseur de la jambe libre (qui émet lui aussi le cache au delta).
- [x] Test de non double-comptage explicite — 2026-10-10, `tests/test_stream_cache_delta.py` (228 lignes, 15 tests) :
  - `test_pas_de_double_comptage_sur_usage_cumulatif` : start 1200 puis delta qui **répète** 1200 → crédit 1200, pas 2400.
  - `test_cumul_qui_progresse_ne_compte_que_les_increments` (100, 100, 100, 700, 700 → 700, l'addition naïve donnerait 2700), `test_zero_au_start_puis_chiffre_reel_au_delta`, `test_usage_sans_cache_puis_delta_qui_l_apporte`.
  - Contre-épreuves de robustesse : bool/float/str/`None`/liste → `None` (le helper ne lève jamais), valeur qui baisse → delta **négatif** (corrige un excédent au lieu de le figer).
  - Aller-retour convertisseur : le `message_delta` émis par la jambe libre (`prompt_tokens_details.cached_tokens: 850`) alimente le suivi comme un flux natif, et `test_les_deux_sites_appellent_le_helper` verrouille les deux sites d'appel contre un révert partiel.
- [x] `pytest tests/test_chat_sse_to_anthropic.py tests/test_usage_display.py tests/test_streaming_sse.py tests/test_stream_cache_delta.py tests/test_sse_coalesce.py tests/test_sse_keepalive.py tests/test_e2e_protocol_matrix.py tests/test_cache_contract.py` — **vert 2026-10-10** (335 tests, 0 échec, exit 0), plus la suite complète `tests/` : **2806 tests, 0 échec, exit 0** (2791 avant + les 15 de `tests/test_stream_cache_delta.py`, soit exactement l'ajout — aucun autre test ajouté, aucun retiré). *Revérifié le 2026-10-12 : 231 collectés, 230 passés + 1 xfail connu, EXIT=0, JUnit `tests=231 failures=0 errors=0 skipped=1` (collect-only : 100+26+3+15+6+8+43+30 ; l'écart avec 335 tient au périmètre compté le 2026-10-10, l'arbre actuel contenant davantage de tests que HEAD, pas moins).*
- [ ] Live : comparer chiffres dashboard/DB vs chiffres client sur une même session — **PENDANT** : fenêtre d'exploitation.
- [ ] **Rollback** : retour à la seule lecture du `message_start` (suppression du bloc `[F2]` dans les deux boucles + du helper ; aucun site d'appel restant).

### G1 — Classement des codes d'erreur

- **Réf.** §2 G1 (P2) · §9 · contrainte amont, **à aligner**
- **Ancrages.** `core/errors.py:74 anthropic_error` (+ alias `:86`, importé `opencode.py:3685`) · statuts déjà typés `opencode.py:11687-11697` · réécriture de statut `opencode.py:4467 _safe_client_status` · motifs officiels `packages/opencode/src/provider/error.ts:102-154`, `packages/llm/src/provider-error.ts:4-38`
- **Fait le 2026-10-10.** Helper pur `classify_upstream_error(status, body)` + `upstream_retry_signal` + `is_credit_exhaustion` dans `core/errors.py`, table de 8 motifs compilée une fois à l'import (`_UPSTREAM_MOTIFS` / `_MOTIF_RES`, `\b…\b` + `IGNORECASE`, premier motif trouvé gagne).
  - **Le statut n'est JAMAIS réécrit par le classement** : la fenêtre d'auth (401/403 → 503) et la parité 429/529 restent la propriété de `_safe_client_status`. Seul le **type** change. C'est ce qui rend la carte sans risque pour l'existant : aucun site ne voit son statut bouger.
  - **Câblage (5 sites d'émission)** : les 4 `except UpstreamError` des relais payants (`opencode.py:11555`, `:11626`, `:13011`, `:13147`) et l'enveloppe Anthropic forgée à la main du relais OpenAI (`:13209`, qui portait `"type": "api_error"` en dur).
  - **Câblage (8 sites de pause crédit)** : les `any(x in resp.text for x in ("Insufficient balance", "Monthly usage limit"))` → `_is_credit_exhaustion(...)`, y compris les deux formes `err.decode(errors="ignore")` des jambes flux. Les motifs officiels `insufficient_quota` / `usage_not_included` entrent dans la même famille : une seule table, un seul endroit à faire évoluer (avant, 8 copies divergentes).
  - **Câblage rejeu** : `_motif_retryable` (`opencode.py:4500`) appelé dans `_do_request_with_retry` (`:9466`) — un statut ambigu `{400, 404, 408, 409, 422, 425}` dont le corps déclare `server_is_overloaded` / `server_error` se rejoue, toujours borné par `max_retries` et `attempt < max_retries - 1`.
  - **Exclusions de sécurité du rejeu** : 401 / 403 / 429 / 499 ne sont **jamais** rejoués par motif (fenêtre d'auth → failover de clé, 429 → quota, 499 → déconnexion). Un test verrouille la constante `_MOTIF_RETRY_STATUSES` pour qu'elle ne dérive pas.
  - **Branche `_is_compaction` intacte** (`:13165`) : elle relaie le corps amont **verbatim** (contrat Phase 2) — volontairement PAS reclassée, et un test l'interdit.
- **Déviation assumée et documentée.** `invalid_prompt` et `context_length_exceeded` sont **non-rejoués SANS pause de clé**, contrairement à la formule de la carte (« pause clé longue »). Raison : mettre toutes les clés en pause parce qu'une conversation dépasse le contexte provoquerait une panne auto-infligée — un overflow ne dit rien de la santé de la clé. Les 4 motifs de crédit (`insufficient_quota`, `usage_not_included`, `insufficient balance`, `monthly usage limit`) portent, eux, la pause longue : un quota épuisé est un problème de **compte**, pas de réseau. Comportement strictement identique à l'existant sur les 2 textes historiques — G1 ajoute les 2 motifs officiels à la même famille, il ne retire rien.
- **Piège.** Le classement doit primer **côté client** quand l'amont est explicite : ne pas laisser notre propre compaction avaler un overflow que le client sait déjà traiter (§9).
- [x] Helper ajouté + tests unitaires des motifs — `tests/test_classify_upstream_error.py` (73 tests : chaque motif, frontières `\b` anti `server_errors_total` / `not_server_error`, priorités de table, entrées dégradées bytes/dict/None/`True` sans raise, statut conservé ou normalisé en 502, liste d'exclusion, refus libre classé, 4 gardes anti-révert partiel sur les sites d'appel)
- [x] Câblage aux sites d'erreur amont payants (4 `except UpstreamError` + 1 enveloppe OpenAI + 8 sites crédit + rejeu par motif)
- [x] `pytest tests/test_faux_429_veridique.py tests/test_server_compaction.py tests/test_protocol_matrix.py tests/test_e2e_protocol_matrix.py` — **vert** (176 tests) ; fichier neuf vert ; suite complète `tests/` en cours. *Revérifié le 2026-10-12 : 181 collectés, 180 passés + 1 xfail connu, EXIT=0, JUnit `tests=181 failures=0 errors=0 skipped=1` (l'écart avec 176 tient aux tests ajoutés depuis le 2026-10-10).*
- [ ] Live : prompt volontairement hors contexte → le client compacte (pas de boucle de retry) — **fenêtre d'exploitation**
- [ ] **Rollback** : revert du helper (les sites retrouvent leur `api_error`) — révertable en un seul commit, aucun statut touché

### G2 — Budget de reprises

- **Réf.** §2 G2 (P2) · §9 · verdict §14.2 : **mixte** — aligner les codes et `retry-after`, **pas** le compteur
- **Ancrages.** 5 sites de lecture `yaml_get("streaming","retry_attempts",2)` — **migrés** : `opencode.py` sites 9294 (relais non-stream) + 11990 / 13412 / 15322 / 16623 (bornes des 4 flux), tous ramenés à `_upstream_max_retries()` · helper pur existant `upstream/retry.py:24 parse_retry_after`, `:61 compute_retry_delay`, **+ neuf** `:111 resolve_max_retries` + `:108 RETRY_BUDGET_MAX`
- **Faire.** Exposer `upstream.max_retries` (défaut **2**, inchangé) et router les 5 sites dessus, pour pouvoir monter à 3-4 si les 502 le justifient. `retry-after` déjà honoré via `upstream/retry.py`.
- **Piège.** Jambe free : retry 0,5 s plafonné à 2 s, idle 120 s (lot I3) — le nouveau réglage ne doit jamais abaisser ce plancher.
- [x] Clé ajoutée (`config.yaml` `upstream.max_retries: 2`) + 5 sites migrés (un seul `yaml_get` restant, factorisé dans `_upstream_max_retries()`) — la résolution est `upstream.max_retries` → l'historique `streaming.retry_attempts` → `2` : qui n'a réglé que l'ancienne clé garde sa valeur, bit pour bit
- [x] Défaut vérifié identique à aujourd'hui (test de non-changement) — `tests/test_upstream_retry_budget.py` recalcule l'attendu **depuis le disque** (pas en dur) + chaîne de résolution (nouvelle > historique > 2, clé vide ne masque pas l'historique, config illisible → 2 sans raise) + 4 gardes source : un seul point de lecture, lecture historique factorisée, plancher `len(API_KEYS)` intact sur les 4 sites libres, politique de délais free non captée
- [x] `pytest tests/test_retry_helper.py tests/test_upstream_retry_budget.py tests/test_free_ip_pool_disconnect_retry.py tests/test_streaming_sse.py` — **vert** (63 tests) ; voisins verts : `test_suspect_tiny_output.py`, `test_classify_upstream_error.py` (153 cumulés) ; `test_docs_drift` / `test_hot_reload` / `test_phase0_contracts` verts ; documenté dans `docs/tuning.md` (§ HTTP pool httpx). *Revérifié le 2026-10-12 (grappe + voisins + contrats en un seul run) : 192 collectés, 192 passés, EXIT=0, JUnit `tests=192 failures=0 errors=0 skipped=0` (l'écart avec 63+153 tient aux tests ajoutés/retirés depuis le 2026-10-10).*
- [ ] Live : taux de 502 avant/après, si le budget est monté à 3-4 — **fenêtre d'exploitation** (le défaut n'ayant pas changé, il n'y a rien à observer aujourd'hui)
- [x] **Rollback** : remettre `retry_attempts: 2` (supprimer `upstream.max_retries` → l'historique reprend la main) ou revert du câblage — un seul commit, aucun comportement changé au défaut

### H1 — Hygiène du catalogue

- **Réf.** §2 H1 (P2) · §11 · **défaut chez nous**
- **Ancrages.** modèles hors de leur jambe : **12 ids** (tableau §11) — `glm-5.1` `:33`, `glm-5` `:35`, `kimi-k2.5` `:37`, `mimo-v2-pro` `:45`, `mimo-v2-omni` `:47`, `minimax-m2.5` `:57`, `qwen3.5-plus` `:65`, `hy3-preview` `:77`, `omen-alpha` `:125`, `deepseek-flash` `:127`, `jev-1.13` `:134`, `jev-1.13-free` `:131` · cas documenté à conserver : `jev-1.13` / `jev-1.13-free` `:131-136` (endpoint interne `/zen/v1/systemone`) · `deprecated` sur leur jambe : `kimi-k2.6` `:39`, `mimo-v2.5-free` `:95`, `deepseek-v4-flash-free` `:93`, `fledge-alpha-free` `:151`, `muse-spark-1.2-contributor-free` `:88`
- **Faire.** 1) Inventaire complet des 12 hors-jambe avant toute suppression : `glm-5.1` et `minimax-m2.5` sont **vivants sur la jambe anonyme** → les traiter en B1/N1 (migration de route), pas en H1 (suppression). Ne retirer que les 10 réellement morts, `jev-1.13*` excepté. 2) Job **lecture seule** qui écrit un diff proposé (`models_catalog.diff.json`) sans jamais écrire `config.yaml`. 3) Le diff compare **les deux providers** (`opencode-go` pour le payant, `opencode` pour l'anonyme — lot I1).
- **Piège.** `jev-1.13*` n'est pas dans les catalogues mais pointe sur un endpoint interne qui marche : le supprimer casserait une route vivante. À documenter, pas à retirer. Même prudence pour les modèles dont la route par défaut dépend encore (voir N1 : rien ne se supprime avant que B1 soit tranché).
- [x] ** module pur** `config/models_catalog.py` : 5 verdicts (`mort`, `hors-jambe`, `deprecated`, `non-catalogue-interne`, `ok`),classification par **endpoint résolu** via la source unique `config.settings._resolve_model_endpoint` (le suffixe `-free` ne décide PAS de la jambe : 3 ids de la jambe payée finissent par `-free`), import paresseux de `settings` (importer `config` au chargement lancerait le thread de découverte)
- [x] 5 entrées réellement mortes retirées de `config.yaml` : `mimo-v2-pro`, `mimo-v2-omni`, `hy3-preview`, `omen-alpha`, `deepseek-flash` — absentes des **deux** jambes, sans endpoint interne. La purge est prouvée neutre : leur entrée d'avant était `{protocol: openai}` et rien d'autre, elle résolvait déjà exactement comme le repli implicite ; `capabilities` (seul consommateur : le gate `web-search` d'`opencode.py`) était faux dans les deux états, et aucun consommateur ne lit `api`. Verrouillé par `tests/test_models_catalog.py` (30 tests) — dont un test source anti-révert qui casse si un **second** site lit `capabilities`.
- [x] 5 hors-jambe **conservés** : `glm-5`, `glm-5.1`, `kimi-k2.5`, `minimax-m2.5`, `qwen3.5-plus` (routés `go`, hébergés côté anonyme) → lot B1, pas H1 ; `glm-5.1`/`minimax-m2.5` = `routing.sonnet_model`/`haiku_model`. Présence **revérifiée le 2026-10-11 après restart** : les 5 ids sont toujours dans `models:` (`True` sur les 5, section à 54 entrées). La formule « documentés en commentaire dans `config.yaml` » qui figurait ici ne vaut **plus** : les commentaires ont été effacés deux fois par `save_yaml_config()` (2026-10-10 17:59, puis 2026-10-11 08:56:04 — première écriture du nouveau process). L'inventaire ne vit plus que dans `docs/models-protocol.md` et dans cette carte ; la coche porte sur la **conservation des ids**, pas sur le commentaire perdu.
- [x] 8 `deprecated` sur leur jambe routée conservés (`kimi-k2.6`, `mimo-v2.5-free`, `deepseek-v4-flash-free`, `fledge-alpha-free`, `muse-spark-1.2-contributor-free`, `space-bunny-free`, `exo-free`, `step-5-preview-free`) : toujours servis, la dépréciation catalogue n'est pas une mort
- [x] `jev-1.13` / `jev-1.13-free` conservés et documentés comme **non-catalogue-interne** (endpoint `/zen/v1/systemone`, en dehors des bases connues → jamais déclarés morts)
- [x] Bloc de 22 lignes de commentaire ajouté sous `models:` (ligne 31) pour que l'inventaire ne dépende plus d'un job à lancer à la main — **fait le 2026-10-10, PERDU depuis** : effacé par `save_yaml_config()` le 2026-10-10 à 17:59, puis de nouveau le 2026-10-11 à 08:56:04 (0 ligne `#` sur le fichier aujourd'hui). La coche signifie que le travail a été fait, **pas que l'état tient** — c'est exactement le cas que la carte suivante (« Commentaires de `config.yaml` : perdus, sans filet ») actée. L'inventaire fait désormais autorité dans `docs/models-protocol.md`, qu'aucun dumper YAML ne touche.
- [x] Job de diff en lecture seule (`scripts/check_models_catalog.py`), désactivé par défaut (`models_catalog.enabled: false`), sortie horodatée UTC `logs/models_catalog.diff.<horodatage>.json`, `--force` / `--out` / `--json`, aucun accès en écriture à `config.yaml`
- [x] `pytest tests/test_model_catalog.py tests/test_go_only_routing.py tests/test_docs_drift.py tests/test_free_discovery.py` — **vert** (47 tests). *Revérifié le 2026-10-12 : 54 collectés, 54 passés, EXIT=0 (l'écart avec 47 tient aux tests ajoutés aux fichiers depuis le 2026-10-11). **Tenue config revérifiée le 2026-10-12 (rechargement YAML direct) : `models:` à 54 entrées, 5 morts absents, 8 gardés présents (`glm-5`, `glm-5.1`, `kimi-k2.5`, `minimax-m2.5`, `qwen3.5-plus`, `space-bunny`, `jev-1.13`, `jev-1.13-free`), `free_discovery.purged_ids` portant toujours les 5 tombeaux — le process vivant n'a rien ressuscité.** **Fenêtre franchie le 2026-10-12 (restart par l'utilisateur) : PID 25688 démarré à 16:02:37, `config.yaml` réécrit à 16:05:19 (persist routine), 54 entrées / 0 mort / 5 tombeaux confirmés par rechargement YAML direct, `GET /api/stats` STATS_OK.***
- [x] **Rollback** : restaurer la copie de sauvegarde prise avant la vague (`config.yaml.v<N>.bak.<date>`) ; le job se désactive par config. **Ne PAS `git checkout -- config.yaml`** (règle V1 : l'arbre est déjà modifié vs HEAD sur ~30 fichiers).
- [x] **Durabilité de la purge par tombeaux `free_discovery.purged_ids`** — **fait 2026-10-10**. Cause racine exacte : la découverte auto est **add-only à deux étages**, donc un retrait manuel ne tenait que jusqu'au refresh suivant. `apply_discovered_free_models` ([config/discovery.py:384](../config/discovery.py#L384)) ajoute tout id annoncé en amont ; `persist_free_mappings` ([:511](../config/discovery.py#L511)) fusionne `MODELS` dans `yaml_data["models"]` **sans jamais retirer** → le process ré-écrivait l'id mort sur le disque (c'est ce qui a recréé les 5 entrées en fin de `models:`, lignes 207-215 avant cette reprise). Mécanisme calqué sur `go_only_ids`, appliqué en **quatre** points : (1) filtre `free_ids -= purged` **avant** le delta-check, sinon un id purgé présent dans les deux ensembles passerait le test d'égalité ; (2) retrait de `models` / `FREE_MODELS` / `FREE_MODEL_POOL`, **y compris sur le chemin no-delta** (early return `added == 0` : un refresh où rien d'autre ne change laissait l'id mort en place) ; (3) garde au persist — `pop` de `yaml_data["models"]` et saut des purgés dans la boucle de fusion ; (4) rechargement à chaud dans `maybe_reload_custom_routes()` (branche `cfg_changed`, set muté en place : les tombeaux restent vivants sans restart, comme `go_only_ids`). Les trois chargements sont durcis par `config/discovery._normalize_ids` (`isinstance(x, str)` + garde collection) : une entrée YAML `null`/`42` ou une clé mal typée ne peut plus créer le faux tombiveau `"none"`, qui barrerait un jour un id réel collant à cette chaîne. Verrouillé par `tests/test_purged_ids_tombstone.py` (16 tests — ids factices `acme-*`, seuls 2 lisent le `config.yaml` réel).
- [x] **Durabilité de la purge — fenêtre d'exploitation** (`config.yaml` est ré-écrit par le process vivant). `save_yaml_config()` (`config/settings.py:226`) dumpe **tout** l'état en mémoire via `yaml.dump(_yaml_data, sort_keys=False)` : les commentaires manuscrits partent, et les ids purgés **reviennent**. Seuls `_PROTECTED_SECTIONS` (`dashboard_trust`, `client_auth`, `supervisor`) et `_PROTECTED_NESTED` (`ip_rotation.latency_rotation`) survivent à la fusion — `models:` n'est pas protégé. Appelants : `config/settings.py:315` (`yaml_set`), `:1210` (`save_custom_routes`), `dashboard/api.py:1689`/`:1711` (politiques géo), `dashboard/api.py:900`/`:919`/`:1010`. **Constat du 2026-10-10 à 17:59:28** : le `pythonw` (PID 39216, démarré 08:52:59 — donc chargé *avant* la purge) a réécrit `config.yaml`, fait revenir les 5 ids morts (regroupés en fin de `models:`, lignes 210-219) et effacé les 22 lignes de commentaire (0 ligne de commentaire sur le fichier, contre 3 dans la sauvegarde v5). **Reprise du 2026-10-10 (19:48)** : les 5 ids ont été ré-retirés de `models:` (ils ne restent que dans `free_discovery.purged_ids`), mais le restart est **toujours nécessaire et ne suffit même pas à lui seul** : le `pythonw` vivant exécute le code d'*avant* le garde du persist et son `MODELS` en mémoire porte encore les 5 ids — son prochain `persist_free_mappings()` (thread de découverte, `free_discovery.interval`) peut donc les ré-écrire sur le disque. Fenêtre d'exploitation : tuer le process, vérifier qu'aucun des 5 ids n'est dans `models:`, relancer `pythonw opencode.py --gui` — pas ici (le process vivant conserve par ailleurs du code antérieur aux correctifs de cette série ; il n'y a pas de rechargement à chaud du Python, seul `config.yaml` est relu). **Résurrection re-constatée le 2026-10-11 05:06:38 — diagnostic désormais complet** : `config.yaml` vient d'être ré-écrit par le `pythonw` **PID 39216** (démarré le 2026-10-10 08:52:59, donc bien avant le garde du persist écrit à 19:48) et les 5 ids morts sont **revenus dans `models:`** (`config.yaml:214-218` — `hy3-preview`, `omen-alpha`, `deepseek-flash`, chacune en `{protocol: openai}`), repoussés en fin de section comme la fois d'avant. Le mécanisme de tombeaux est pourtant **armé et correct côté disque** : `config.yaml:655-666` porte `free_discovery.enabled: true`, `interval: 3600`, `auto_persist: true` et `purged_ids:` listant exactement les 5 id. Le défaut n'est donc ni dans la configuration ni dans le code neuf : le process vivant exécute l'**ancien** code, son `MODELS` en mémoire porte encore les 5 id et son `persist_free_mappings()` — sans le garde — les ré-écrit à chaque refresh de découverte. PID 39216 est le **seul** écrivain de ce `config.yaml` (les trois autres processus `python` de la machine sont un projet sans rapport, `passerelle-parc/gateway.py`, avec leur propre `config.yaml`). Conséquence vérifiée : `tests/test_models_catalog.py::test_ids_purges_absents_de_models` et les 4 cas `test_lentree_purgee_resolvait_deja_comme_le_repli` sortent **rouges** dans cet état — la même commande qui rendait 106/106 verts plus tôt dans la session rend 5 rouges, sans qu'aucun code ni aucun test n'ait changé. Le verrou de purge fait donc son travail : il signale une résurrection réelle. Rééditer le fichier à la main ne tiendrait pas — le prochain persist du process vivant ré-écrirait. **Étape requise, celle déjà décrite ci-dessus** : tuer PID 39216, vérifier qu'aucun des 5 id n'est dans `models:`, relancer `pythonw opencode.py --gui` — **non fait ici**, le process vivant porte le trafic de cette session. **Gardé du disque revérifié le 2026-10-11 (boucle fermée)** : le code de l'arbre porte bien les quatre points, aux lignes exactes — (1) filtre `free_ids -= purged` avant le delta-check ([config/discovery.py:427-435](../config/discovery.py#L427)) ; (2) retrait de `models` / `free_models` **y compris sur le chemin no-delta** ([:444-454](../config/discovery.py#L444)) ; (3) garde au persist — `pop` de `yaml_data["models"]` puis saut dans la boucle de fusion ([:554-579](../config/discovery.py#L554)) ; (4) les **trois** voies d'ajout auto sont bouchées, y compris celle qu'on ne soupçonne pas : le fetch du catalogue amont ([:88-98](../config/discovery.py#L88) drop en mémoire, [:116-121](../config/discovery.py#L116) saut à l'ajout), fil de `purged_ids_fn` à résolution tardive parce que `PURGED_IDS` n'existe pas encore au lancement du thread ([config/settings.py:877-881](../config/settings.py#L877), `PURGED_IDS` défini plus bas en [:924-930](../config/settings.py#L924)). Le défaut est donc **uniquement** dans le process vivant : rien à corriger dans l'arbre, la reprise se résume au restart. **Détail qui achève la preuve** : les entrées ressuscitées sont **strictement identiques** à celles de HEAD (`git show HEAD:config.yaml` → `mimo-v2-pro:` / `protocol: openai`, deux lignes, rien d'autre, à l'octet près) : c'est une résurrection fidèle de l'état d'avant purge, pas une dérive de forme. Corollaire pour la lecture des tests : `test_lentree_purgee_resolvait_deja_comme_le_repli` assert d'abord la neutralité de la **résolution** (`resolved_endpoint(avant) == resolved_endpoint({})`, vraie dans les deux états), puis l'égalité `get_model_config(id) == {endpoint, protocol}` qui ne tient que lorsque l'id est **absent** de `models:` — une entrée présente, même nue, fait passer `get_model_config` par sa branche riche (`endpoint` + `api: chat` + `capabilities`), vérifié en direct sur `mimo-v2-pro`. C'est précisément cette seconde assertion qui rougit, et c'est le comportement attendu d'un verrou de purge. **Fenêtre franchie le 2026-10-11 (restart par l'utilisateur)** : PID 39216 tué, **PID 25012** démarré à 08:56:00 (`pythonw opencode.py --gui`) ; `config.yaml` réécrit à 08:56:04, soit **4 s après le démarrage** — c'est cette première écriture qui a effacé les commentaires (0 ligne `#` sur 15 418 octets, cf. carte suivante), les ids étant eux correctement absents. Vérifié par rechargement YAML direct : `models:` compte **54 entrées** (59 avant — les 5 morts en moins), les 5 ids sont tous à `False` dans `models:`, et `free_discovery.purged_ids` porte toujours les 5 tombeaux. Les tests rejoués immédiatement après : `tests/test_models_catalog.py` + `tests/test_purged_ids_tombstone.py` = **49 tests, 49 verts**, y compris les 5 qui rougissaient à 05:06. Le nouveau process exécute bien le code gardé puisqu'il n'a pas ressuscité les ids lors de son propre persist. Santé de service confirmée : `GET /api/stats` répond (les 3 premiers essais en `000` = le temps que le mode GUI binde le port). **Suite complète rejouée après le restart** (exit code 0) : 3125 issues au total — **3121 passés, 3 skippés, 1 xfail, aucun échec**. Le décompte est relevé sur les lignes de progression de `pytest -q` (44 lignes, `[100%]` atteint) : la redirection `> $TEMP/suite_full.txt` a perdu la ligne de synthèse finale de pytest, mais l'exit code 0 et l'absence de tout `F`/`E` dans les 3125 caractères d'état font foi.
- [x] **Commentaires de `config.yaml` : perdus, sans filet.** Le `save_yaml_config()` du process vivant a effacé **tout** commentaire du fichier (0 ligne `#` sur les 15 Ko, y compris le bloc d'inventaire de 22 lignes). L'inventaire faisant autorité est désormais `docs/models-protocol.md` (aucun dumper YAML n'y touche). Remettre le bloc dans `config.yaml` est **inutile** : il serait effacé au prochain appel, et la coche deviendrait fausse. Une vraie solution demanderait un dumper préservant les commentaires (`ruamel.yaml` = nouvelle dépendance pour tout le chemin d'écriture) → **documenté, pas fait** ; l'inventaire vit dans les docs, la config ne garde que du exécutable. *Coche cochée le 2026-10-11 au sens où la carte se lit elle-même : son livrable est la **décision documentée**, pas une écriture. Il ne reste aucun travail en attente ici — voir la carte 1257, corrigée le même jour pour que ses deux affirmations fausses (le bloc de 22 lignes, la mention « documentés en commentaire » de la carte 1254) ne survivent pas comme des `[x]` trompeurs.*

### N2 — Cibles du repli libre dépréciées

- **Réf.** §2 N2 (P1) · §11 · verdict §14.6 : **défaut chez nous** (Q1 amont)
- **Ancrages.** `config.yaml:168-180` (`free_model_map`, 12 entrées) · `config.yaml:438-442` (`web_search_native`) · survie de `muse-spark-1.3-contributor-free` : photographie V0 (`status` != `deprecated` côté `opencode`)
- **Faire.** Rediriger les 6 ids payants qui pointent sur `mimo-v2.5-free` (désormais `deprecated`) vers un modèle libre **vivant**, et retirer `muse-spark-1.2-contributor-free` de `web_search_native` (`muse-spark-1.3-contributor-free` la remplace). Candidat par défaut déjà présent côté config : `muse-spark-1.3-contributor-free` (utilisé par `custom_routes.musespark13contributor`).
- **Piège.** Le choix doit rester **par classe de capacité** : `muse-spark-1.3` gère effort `minimal…xhigh` et contexte 1 M, pas tous les modèles remplacés. Pas de cible unique aveugle.
- **TENU (bloqué) — décision utilisateur requise.** Aucune case cochée tant que la **cible libre** n'est pas arbitrée. Les deux candidats sont vivants côté catalogue et déjà présents en config :
  - **défaut** `muse-spark-1.3-contributor-free` (§11, `config.yaml:118-122`, `api: responses`) : même famille que les modèles remplacés, mais **contexte 1 M + effort jusqu'à `xhigh`** — sur-dimensionné pour `mimo-v2.5` / `glm-5.1` / `kimi-k2.6`, et déjà chargé par `custom_routes.musespark13contributor` (§19), donc mis en concurrence avec une route vivante ;
  - **alternatif** `mimo-v2.6-flash-free` (`config.yaml:140-141`) : cible de `free_model_map` pour `mimo-v2.6-flash`, déjà **éprouvée en prod sur la jambe libre**, capacité plus modeste.

  À trancher avant d'écrire quoi que ce soit : le câblage touche 6 routes payantes utilisées **par la session en cours** (`glm-5.1`, `kimi-k2.6`, `minimax-m2.5`, `mimo-v2.5`, `qwen3.7-max`, `deepseek-v4-flash`).

  **Rappel sur `muse-spark-1.2-contributor-free`** : point N2 séparé (retrait de `web_search_native`), **sans lien** avec le shadow `custom_routes` (§19) ; ne pas les confondre dans la même correction.
- [x] 6 entrées `free_model_map` + 1 entrée `web_search_native` recâblées sur des modèles vivants — **fait le 2026-10-12, arbitrage utilisateur `spark-1.3-free`**. État réel trouvé : 6 × `free_model_map` → `mimo-v2.5-free` et `free_discovery.default_target` → `mimo-v2.5-free` étaient **déjà** recâblés vers `mimo-v2.6-flash-free` dans l'arbre ; restaient 2 sites en `muse-spark-1.2-contributor-free` : `free_model_map[muse-spark-1.2-contributor]` → `muse-spark-1.3-contributor-free` (`config.yaml:217`), et `web_search_native[1]` retiré (liste à 3 ids : `muse-spark-1.2-contributor` payant conservé + `muse-spark-1.3-contributor` + `muse-spark-1.3-contributor-free`). Fallbacks durcis pour qu'aucun défaut ne ressuscite l'id : `config/settings.py` (charge + hot-reload `maybe_reload_custom_routes`) et `opencode.py::_is_web_tool_native` ne listent plus `muse-spark-1.2-contributor-free`. `tests/test_go_only_routing.py::test_web_search_native_parity_13` adaptée (3 ids présents + assertion d'absence du 4e). Marqueur `xfail(strict=True)` de `tests/test_free_map_targets_live.py::test_config_reel_ne_vise_aucune_cible_depreciee` **levé** (le vert est désormais l'état attendu). `docs/models-protocol.md` § aliases resynchronisé. Vérifié : contrôle 12/12 (`tests=12 failures=0 errors=0 skipped=0`, `c2_reverify.xml`) ; contrôle + trio **54/54** (`tests=54 failures=0 errors=0 skipped=0`, `c1_reverify2.xml`) ; `ruff check` vert sur les 4 fichiers Python touchés (dont 2 pré-existants corrigés au passage dans `opencode.py` : UP041 `:13764` tuple `TimeoutError` redondant, I001 `:531` ordre `overflow`/`plan` — voir point suivant) ; `sha256sum config.yaml` avant `7d098279…8bd9d6` (= backup `config.yaml.v6.bak.2026-10-12`) → après `e72cc385…9308fd0`. Reload à chaud : les deux clés sont relues par `maybe_reload_custom_routes` (branche `cfg_changed`), aucun restart requis ; le process vivant `pythonw` (PID 25688) reprend les 2 sites au prochain `cfg_changed`.
- [x] Un contrôle lit `config.yaml` et refuse toute cible `deprecated` — **fait le 2026-10-11** : `tests/test_free_map_targets_live.py`, 12 issues, **11 passés + 1 xfail** (`xfail(strict=True)`, cf. `tests/test_tools_matrix.py` : l'XPASS strict lève le marqueur au câblage). `ruff check` vert. **Le contrôle est rouge aujourd'hui, et c'est son état attendu** : le recâblage de la carte reste bloqué sur l'arbitrage de la cible libre, donc le test nomme les 9 sites en faute au lieu de les masquer — un vert ici serait un vert presque faux. **Le périmètre réel est plus large que ce que la carte compte** : la carte dit « 6 entrées `free_model_map` + 1 entrée `web_search_native` », le relevé croisé trouve **9 sites et 2 ids dépréciés distincts** sur la jambe anonyme — 6 × `free_model_map[...]` → `mimo-v2.5-free`, **1 de plus que la carte** : `free_model_map[muse-spark-1.2-contributor]` → `muse-spark-1.2-contributor-free` (le pendant payant pointait déjà sur sa cible `-free` dépréciée), `web_search_native[1]` → `muse-spark-1.2-contributor-free`, et **1 site que la carte ne nomme pas du tout** : `free_discovery.default_target` → `mimo-v2.5-free`, le repli de la découverte auto. `_sites()` couvre les trois familles d'une seule lecture, pour qu'un futur site soit déclaré plutôt que découvert par un rouge. **La règle juge la jambe routée, pas le nom** : chaque id candidat passe par `mc.resolved_endpoint()` → la source unique `_resolve_model_endpoint`, appliquée au `models:` **parsé depuis `config.yaml`** et non à l'état du process (que le thread de découverte mute 0,5 s après l'import). D'où deux exclusions prouvées par des tests synthétiques : `space-bunny-free` (déprécié côté payé, vivant côté anonyme — `endpoint:` explicite gagne) et `jev-1.13*` (endpoint interne `/zen/v1/systemone` → `endpoint_leg()` rend `None`, jamais comparé à un statut). Hermétique comme le reste de `tests/` : le statut live est figé en constante (`CIBLES_DEPRECIEES`), rafraîchissable par `python scripts/check_models_catalog.py` (lecture seule) — même régime que `PURGES` dans `tests/test_models_catalog.py`. Vérifié : `python -m pytest tests/test_free_map_targets_live.py` → 11 passés + 1 xfail, puis `--runxfail` qui rend la trace complète avec les 9 sites ; trio de la carte suivante (`tests/test_free_model_map_http.py tests/test_go_only_routing.py tests/test_model_catalog.py`) → **42 passés** ; `sha256sum config.yaml` identique avant/après (`c453e9e7…88b`) — rien n'a écrit le fichier réel, le défaut de suite étant celui que `tests/test_free_model_map_http.py` documente en tête.
- [x] `pytest tests/test_free_model_map_http.py tests/test_go_only_routing.py tests/test_model_catalog.py` — **vert 2026-10-12 : 42 collectés, 42 passés, EXIT=0** (re-joué : le 42 verts du 2026-10-11 n'avait jamais été coché. La carte N2 reste bloquée sur l'arbitrage de la cible libre, mais la ligne pytest est vérifiée indépendamment).
- [x] **Rollback** : restaurer `config.yaml.v6.bak.2026-10-12` (SHA256 `7d098279…8bd9d6` vérifié identique à la création — c'est l'état pré-câblage avec les 2 sites en `muse-spark-1.2-contributor-free`). **Ne PAS `git checkout -- config.yaml`** : l'arbre était déjà modifié vs HEAD sur ~30 fichiers avant cette session — `checkout` détruirait ce travail (même règle que V1). (L'ancienne ancre `config.yaml.v1.bak.2026-10-09` est périmée : elle précède la purge H1 et les 6 recâblages mimo.)

### N3 — `npm` hors table : endpoint indéductible

- **Réf.** §2 N3 (P2) · verdict §14.6 : **défaut de conception** (Q1 amont)
- **Ancrages.** documentation de la règle amont `scripts/_probe_catalog_diff.py:51-56` (`NPM_TO_API`) · règle réelle `config/settings.py:822-849` (`_resolve_model_endpoint`) · matrice réellement implémentée `config/models_catalog.py` (`NPM_TO_API`, `api_family_for_npm`)
  - **Correction 2026-10-11.** `npm` est une clé de **niveau provider**, absente au niveau modèle, et le catalogue ne porte **aucun** indicateur de protocole par modèle. Les 9 ids cités ici (8 `gemini-*` + `mistral-large-4`) sont servis par la **jambe `opencode`**, dont le `npm` `@ai-sdk/openai-compatible` est connu (→ `chat`) ; leur `npm` d'origine (`@ai-sdk/google`, `@ai-sdk/mistral`) n'est pas la route — ces providers n'ont même pas d'`api` dans le catalogue. Le « npm inconnu » ne concerne donc pas ces 9 ids.
- **Faire.** Étendre la table des `npm` connus (matrice `npm` → endpoint + protocole) et faire échouer **explicitement** un `npm` inconnu, plutôt que de retomber sur `/chat/completions` par défaut.
- **Piège.** On ne sait pas aujourd'hui quels endpoints ces providers exposent via la passerelle opencode : aucune entrée ne doit être devinée. Tant que la matrice n'est pas complète, l'échec explicite vaut mieux qu'un endpoint inventé — c'est le principe déjà appliqué au repli de N4.
- [x] Matrice `npm` → endpoint documentée dans `docs/` + code — fait le 2026-10-11 : `docs/models-protocol.md` § « Matrice `npm` → endpoint, et périmètre de fourniture (N3) » + les 6 entrées de `config/models_catalog.py`. **Règle de périmètre posée par l'utilisateur** : le proxy ne fournit que les modèles servis par le provider opencode, `jev-1.13*` excepté. Vérifié : 30 tests `tests/test_models_catalog.py`, 30 `.`, exit 0 ; smoke live du module contre le catalogue.
- [x] `npm` inconnu → erreur explicite (plus de repli silencieux) — fait le 2026-10-11 : `config/models_catalog.py` (`hosts_of`, `refusal_reason`) produit le message de refus, qui **nomme** l'id, les providers tiers qui l'hébergent et le `npm` de chacun (`no npm declared` / `outside the known table: no endpoint can be derived` / `npm …, api …`), liste bornée à `MAX_NAMED_HOSTS = 3` hôtes ; `core/errors.py` porte le type `ModelOutOfScope(model_id, reason)`. Relevé live : 3977 ids distincts, **132 servis** (91 hébergés aussi chez un tiers → servis quand même, la route opencode gagne), **3845 uniquement chez un tiers → refusés**, les 2 internes exceptés. **Reste ouvert et assumé :** le refus n'est pas câblé nulle part dans le routeur (aucun site d'appel à `ModelOutOfScope`) ; et au moment de N3, `get_model_config()` retombait encore sur la jambe payée pour absolument tout id non déclaré. La seconde moitié a été traitée depuis par la carte N4 (le repli repasse par la source unique), dont le piège — un repli qui protège les non-catalogués et les internes — est désormais documenté dans `docs/models-protocol.md`, avec sa limite résiduelle (un id au nom muet part encore payé avant la découverte auto). Vérifié : 30 tests `tests/test_models_catalog.py`, 30 `.` ; smoke live du module contre le catalogue + catalogue synthétique pour les branches que le live n'expose pas.
- [x] `pytest tests/test_model_catalog.py tests/test_go_only_routing.py` + test **à créer** `tests/test_npm_routing.py` — fait le 2026-10-11. (Le plan écrit `test_model_catalog` au singulier ; le fichier réel est `tests/test_models_catalog.py`.) Fichier créé : `tests/test_npm_routing.py`, 15 tests, aucun réseau — il verrouille les branches que le catalogue live n'expose pas (provider sans `npm`, `npm` hors table, plus d'hôtes que la borne, entrée de provider non-dict) et les invariants : partition servi/refus (aucun id servi **et** refusé), liste d'hôtes triée donc indépendante de l'ordre d'insertion du JSON amont, nos deux jambes jamais listées comme hôtes tiers, matrice `NPM_TO_API` fermée à 3 valeurs, `INTERNAL_IDS` réduit à la paire `jev-1.13*` (un élargissement doit être explicite), et le point de couture N4 : `ModelOutOfScope` transporte `model_id` + `reason` et se dégrade sans raison fournie. Vérifié : 15 `.` seuls, puis 66 `.` sur `test_npm_routing` + `test_models_catalog` + `test_go_only_routing` + `test_docs_drift`, exit 0 ; `ruff check` vert sur les trois fichiers touchés. *Revérifié le 2026-10-12 à décompte identique : 66 collectés, 66 passés, EXIT=0, JUnit `tests=66 failures=0 errors=0 skipped=0`.*
- [x] **Rollback** : revert du module de routage (config inchangée) — vérifié le 2026-10-11, sans exécuter le revert (le travail est conservé). N3 est **purement additif**, donc réversible par suppression : `config/models_catalog.py` (3 blocs — `MAX_NAMED_HOSTS`, `hosts_of`, `refusal_reason`), `core/errors.py` (la classe `ModelOutOfScope` + son entrée `__all__`), `tests/test_npm_routing.py`, les paragraphes de doc. **Config inchangée, prouvé :** `git diff config.yaml` ne contient aucun contenu N3 (le diff affiché, 127 insertions / 16 suppressions, est celui des cartes précédentes, antérieur à N3). **Le revert ne casse rien :** `opencode.py` importe `core.errors` **par nom** (`opencode.py:3692-3697`, `:7871-7875`), jamais par `*`, et n'importe `config.models_catalog` nulle part — donc aucun site d'appel à satisfaire, aucun redémarrage du serveur local, aucune restauration de config. **Ne PAS `git checkout -- config.yaml`** (règle V1, ligne 1281) : l'arbre était déjà modifié vs HEAD sur ~30 fichiers avant cette session.

### N4 — Repli implicite faux pour 41 modèles anonymes

- **Réf.** §2 N4 (P2) · verdict §14.6 : **défaut chez nous** (Q1 amont)
- **Ancrages.** repli implicite `config/settings.py:1557` (`get_model_config` ; l'ancre `:1523` du plan d'origine était périmée) · photographie V0, sens 2, jambe `opencode` : 80/120 absents = 25 `/responses` + 16 `/messages` + 9 inconnus + 30 replis corrects · **option « refuser » déjà outillée** (2026-10-11, N3) : `config/models_catalog.py` (`refusal_reason`) + `core/errors.py` (`ModelOutOfScope`)
- **Faire.** Pour un id du catalogue demandé **explicitement** par un client et absent de `config.yaml`, ne plus inventer l'endpoint : soit déclarer les 41 ids (généré par le job de H1), soit refuser avec un message nommant l'id et son `npm`.
- **Piège.** Le repli implicite protège aujourd'hui les modèles **non catalogués** et internes (`jev-1.13*` avec `endpoint:` explicite en échappe déjà). Un durcissement doit donc rester conditionné à l'absence d'`endpoint:` et se limiter aux ids qui ressemblent à des ids de catalogue — sinon on casse des routes internes bien vivantes.
- [x] Règle de repli durcie + bornes (pas d'`endpoint:`, pas d'id interne) — fait le 2026-10-11 : `get_model_config` (`config/settings.py:1557`) ne choisit plus la jambe à la main, il repasse par `_resolve_model_endpoint` (`config/settings.py:822`), la **source unique** qui bâtait déjà `MODELS` à l'import. Un id `-free` non déclaré retombe donc sur la jambe **anonyme** (et sur `/responses` quand la famille `muse`/`spark` s'applique) au lieu de `API_BASE_OPENAI` — la jambe payée — quoi que demande le client. **Les trois bornes du piège sont tenues :** (1) toute écriture dans `MODELS` porte un `endpoint` explicite, donc `{**defaults, **cfg}` rend les ids déclarés **à l'identique** (vérifié clé par clé sur tout `MODELS`) ; (2) le repli ne sait rendre que l'un des 5 endpoints que `config.yaml` déclare (`upstream.*`) — jamais un hôte inventé, seulement la mauvaise jambe d'un hôte connu ; (3) `jev-1.13*` garde son `endpoint:` interne `/zen/v1/systemone` (le suffixe `-free` de `jev-1.13-free` ne le détourne pas). La même correction est reportée dans `tests/test_purged_ids_tombstone.py::test_purged_id_not_resolvable_after_drop` (id mort `-free` → `API_BASE_FREE`, plus un compagnon non suffixé qui verrouille la règle du suffixe). Vérifié : **51 passed** sur la commande de la carte ci-dessous, exit 0 (le compte de points, 59, est gonflé par les `.` des lignes d'avertissement : se fier à la ligne de résumé) ; `ruff check` vert.
- [x] Test : id catalogue anonyme en `/responses` → bon endpoint ou refus nommé — fait le 2026-10-11 : 6 tests ajoutés à `tests/test_model_catalog.py` (§ `[PLAN N4]`), aucun réseau, ids synthétiques `acme-*` gardés par `assert model_id not in st.MODELS` (la découverte auto ajoute tout id annoncé en amont 0,5 s après l'import) et `get_model_config.cache_clear()` posé des deux côtés de chaque appel. Verrouille : (a) `acme-muse-9.9-contributor-free` → `_RESPONSES_FREE_ENDPOINT`, `endpoint_leg == FREE_PROVIDER`, identique à ce qu'aurait rendu une entrée déclarée ; (b) `acme-longcat-9.9-free` → `API_BASE_FREE` (famille `chat`, seul le suffixe parle) ; (c) **la limite honnête** : `acme-longcat-9.9`, au nom muet, reste sur `API_BASE_OPENAI` — le repli ne lit que le nom, et la jambe réelle arrive par la découverte auto (`free_discovery_urls` réunit les deux `/models`, `detect_free_ids` classe sur `pricing`/`free`) qui écrit un `endpoint:` explicite, donc gagnant ; le repli couvre la fenêtre d'avant découverte et le offline ; (d) les ids déclarés ressortent inchangés clé par clé ; (e) `jev-1.13` / `jev-1.13-free` verbatim sur `/zen/v1/systemone`, `endpoint_leg is None` ; (f) l'autre moitié de la carte, le **refus nommé** : `refusal_reason` nomme l'id, les hôtes tiers et leur `npm` (`api chat` / `outside the known table: no endpoint can be derived`), `ModelOutOfScope` transporte la phrase, et un id que la jambe anonyme sert n'est jamais refusable — là où le nom est honnête, la jambe du catalogue live et celle du repli concordent.
- [x] `pytest tests/test_model_catalog.py tests/test_go_only_routing.py tests/test_free_discovery.py` — fait le 2026-10-11 : commande tenue à l'identique (le plan écrit `test_model_catalog`, et le fichier existe bien sous ce nom — à ne pas confondre avec `tests/test_models_catalog.py`, l'inventaire H1, que cette commande ne charge pas). **51 passed, exit 0.** *Revérifié le 2026-10-12 à décompte identique : 51 collectés, 51 passés, EXIT=0, JUnit `tests=51 failures=0 errors=0 skipped=0`.* Compter les points de progression est trompeur ici : les lignes `RequestsDependencyWarning` et le chemin absolu du fichier de collecte contiennent des `.` qui gonflent le décompte (59 au lieu de 51) — seule la ligne de résumé est fiable, et `pytest -q` la perd. `ruff check tests/test_model_catalog.py` vert.
- [x] **Rollback** : revert du module de routage (config inchangée) — vérifié le 2026-10-11, sans exécuter le revert (le travail est conservé). Le diff N4 propre tient en **un seul bloc** : le corps de `get_model_config` (`config/settings.py:1557`) — 3 lignes de calcul (`protocol`, puis `defaults` via `_resolve_model_endpoint`) qui remplacent le littéral `{"endpoint": API_BASE_OPENAI, "protocol": "openai"}` — plus son docstring. Rien d'autre au tour : pas de nouvelle dépendance, pas de nouvel import, pas de clé de config. **Config inchangée, prouvé :** `git diff config.yaml` filtré sur les marqueurs N4 (`get_model_config`, `_resolve_model_endpoint`, `endpoint:`) ne rend **aucune** ligne ; les 127 insertions / 16 suppressions du diff de `config.yaml` sont celles des cartes antérieures (purge, tombeaux `purged_ids`), antérieures à N4. **Le revert ne casse rien :** `_resolve_model_endpoint` existait avant N4 (elle bâtait déjà `MODELS` à l'import, verrouillée par `test_resolve_endpoint_*`), le revert ne retire donc aucun symbole ; `config/models_catalog.py` n'est importé par **aucun** module d'exécution (seulement `tests/` et `scripts/`, plus une mention en docstring de `core/errors.py`), et `core.errors` n'est jamais importé par `*` — donc aucun site d'appel orphelin, aucun redémarrage nécessaire au-delà du process local `pythonw opencode.py --gui`. **Ne PAS `git checkout -- config.yaml`** (règle V1, ligne 1281).

---

### N5 — `cache_read` nul sur `mimo-v2.6-flash-free` : contrainte amont, rien à aligner

- **Réf.** §14.6 N5 · canary jambe libre V1 (ligne 830).
- **Ancrages.** extraction `app/protocol/mapping.py:210-211` (`_extract_cache_tokens`), `:2177` (chat non-stream), `:4986` (responses→anthropic) · réécriture `mapping.py:785-813` (membership exacte ; `config.yaml:635-641` liste `mimo-v2.6-flash` nue, pas la cible `-free`) · seuil `config/settings.py:393` (`CACHE_MIN_PROMPT_SIZE` 2000, non discriminant ici : le préfixe testé faisait ~4680 caractères, c'est bien la membership qui exclut la cible).
- **Faire.** Rien : l'amont ne fait pas de prompt caching sur cette cible/jambe — corps brut `{cached_tokens: 0, cache_write_tokens: 0}`, 2 passes à préfixe 1247 tokens répété (`cache_creation` 0 les deux fois), historique 6/6 à zéro contre 181 780 lignes cachées côté spark. Ne pas ajouter la cible à `cache_rewrite_models` sans arbitrage : changement config = canary propre, et il ne créerait pas une écriture que l'amont refuse.
- [x] Canary documenté + classé — **fait le 2026-10-12** : 200 + `BONJOUR` + `usage` en non-stream et stream (ligne 830), N5 au §14.6, triplet réduit à 200 + usage + texte pour cette cible.
- [x] **Rollback** : sans objet — aucun fichier modifié (ni config, ni code, ni test ; seul le plan).

---

### N6 — Canary payant A1/A2/B2 : 0 × 200 jambe payée, contrainte amont, rien à aligner

- **Réf.** §14.6 N6 · canary payant V1 (ligne 830, non cochée).
- **Ancrages.** `/v1/messages` proxy vivant localhost:4000 · `custom_routes` (`config.yaml:441-452`, priorité exact-match `app/router/__init__.py:108-119`, single-pass non-transitive V1b) · `free_model_map` (:210-222, `minimax-m2.5 → mimo-v2.6-flash-free`) · alias haiku `routing.haiku_model=minimax-m2.5` (`config/settings.py:1154-1157`) · lignes DB `model` (servi) vs `original_model` (demandé) + `paid_status` · log `route: 'claude-haiku-5-5' → minimax-m2.5` + `trying free model 'mimo-v2.6-flash-free' instead of 'minimax-m2.5'`.
- **Piège ENV-OVER-YAML (constat du 2026-10-12).** Les trois routes `opus`/`sonnet`/`haiku` sont construites par `_env("OPUS_MAP_MODEL", yaml_get(...))` (`config/settings.py:1146-1157`, `_env` = `os.getenv` sinon défaut, `config/loader.py:34-39`) : **le `.env` du process gagne sur `config.yaml`**. Le canary N6 a donc mesuré `.env HAIKU_MAP_MODEL=minimax-m2.5`, pas `config.yaml routing.haiku_model` — et le flip 1b appliqué puis reverté le même jour (backup v7) était **inerte au runtime** tant que le `.env` n'était pas aligné. Toute future bascule `routing.*` doit aligner les deux (YAML + `.env`), ou le canary mesurera autre chose que l'édit.
- **Faire.** Rien : **0 × 200 sur jambe payée** — `grok-4.5` 400 `Model is unavailable.` ; `grok-4.6`/`grok-4.7`/`gpt-6-luna`/`qwen3.6-plus` 403 `An active OpenCode Go subscription is required to use Go models.` (DB `paid_status` 400/403 ; côté client le 403 amont est réécrit en 503 par `_auth_window_message` — `opencode.py:4519-4534`, acte intentionnel du FIX classement harness : le code reste dans les logs et le statut HTTP) ; les 3 × 200 observés sont jambe libre (`gpt-5.6-luna`/`qwen3.7-max` → shadow exact → `muse-spark-1.3-contributor-free` ; `claude-haiku-5-5` → alias haiku → `minimax-m2.5` → `mimo-v2.6-flash-free`), en non-stream comme en stream. `config.yaml` est déjà correct (V1 alignée) ; §16 ne s'applique pas (refus amont préexistants, pas nouveaux de notre changement). Point ouvert laissé à l'arbitrage : purger les 5 ids refusés (lot H1) ou les garder — même nature que A3 (reporté). **Arbitrage utilisateur du 2026-10-12 : garder les 5 ids en l'état** (2a) — cause attribuée par l'exploitant : **pas de clé payante valide configurée**, donc les 400/403 sont attendus, pas un défaut de mapping ; réévaluer quand une clé valide sera en place.
- [x] Canary documenté + classé — **fait le 2026-10-12** : 8 non-stream + 3 stream, :830 + N6 au §14.6, case :830 **non cochée** (aucun triplet payant réuni).
- [x] **Rollback** : sans objet — aucun fichier modifié (ni config, ni code, ni test ; seul le plan).

---

### P0 — Dette de suite : les 4 échecs préexistants (hors parité)

> **Consigne du 2026-10-10 : « corrige préexistant au passage ».** Décide du
> traitement des 4 rouges chroniques que toutes les vagues (V1 → V5)
> rencontraient et laissaient en l'état. Cette carte **ne relève d'aucun
> écart de parité** : elle ne change aucun comportement destiné au client,
> sauf un — le retour au garde-fou `> 0` de `_set_output_token_limit`, qui
> rétablit la règle commitée contre une modification de l'arbre de travail
> que rien n'attestait. Conserver la règle « on ne rattrape pas le rouge
> d'autrui pendant une vague de parité » : c'est ici, dans une carte dédiée,
> qu'on le fait. *Revérifié le 2026-10-12 : les 3 fichiers complets (`test_tool_schema_strict.py`, `test_max_completion_tokens.py`, `test_free_response_sse_interface.py`) — 114 collectés, 112 passés + 2 skips attendus (dumps `free400_msg_*.json` absents de `logs/`, gitignoré), 0 échec, EXIT=0. Les 4 points restent donc soldés : 2 passent en vrai, 2 skippent par construction.*

- [x] `tests/test_tool_schema_strict.py::TestStrictSubsetGuarantee::test_wire_schemas_strict_subset` et `::test_wire_schemas_idempotent` — **Fait 2026-10-10** : `pytest.skip()` quand `logs/free400_msg_323ba97cfd00-2b7.json` est absent, au lieu d'un `assert wire is not None`. Le dossier vit dans `logs/`, couvert par `.claudeignore` et donc jamais présent sur une machine fraîche ou en CI : le test ne peut structurellement pas tourner hors de cette machine. Le voisin `test_condemned_fragments_covered` portait déjà la mention « auto-porteur (tourne même sans `logs/`) » : les deux tests dépendants du dossier suivent maintenant la même discipline. **Ils repasseront pour de vrai** (et non plus en skip) dès qu'un dump 400 free sera recapturé.
- [x] `tests/test_free_response_sse_interface.py::test_route_responses_still_500s_without_sse_interface` — **Fait 2026-10-10** : le test exigeait le libellé français `"Erreur interne du serveur"`, alors que `debug_exception` (`opencode.py:3228-3242`) façonne l'enveloppe selon le protocole de la route — anglais + `type`/`code` côté OpenAI, via `core/errors.openai_error` (commit `d93f7b4`, délibéré). Le test reste **falsifiable** sur le point qui le motive (absence d'`aiter_lines` ⇒ 500) et assert désormais l'**invariant** au lieu d'une chaîne : statut 500, `error.type == "invalid_request_error"`, `error.code == "500"`, message non vide, et **aucune traceback** dans la réponse. Une chaîne de libellé n'est pas une garantie ; la forme de l'erreur et l'absence de fuite en sont une.
- [x] `tests/test_max_completion_tokens.py::test_helper_ignores_non_positive_and_non_int_limits` — **Fait 2026-10-10**, côté **code** cette fois. La boucle de `_set_output_token_limit` (`app/protocol/mapping.py:361-367`) avait été modifiée dans l'arbre de travail (`>= 0`) pour laisser passer `max_tokens: 0` comme « valeur explicite (pre-warm cache) ». **Cette justification ne tient pas** : `prewarm_after_rotate` (`free/rotation.py:498`) concerne la rotation de clé, pas une limite à zéro ; aucune source du client officiel n'est sur le disque (ni `packages/*.ts`, ni la capture `logs/_capture_official.jsonl` — 3 lignes, en-têtes seuls, **0 occurrence** de clé `*token*`) pour l'étayer. Or relayer `max_tokens: 0` est un 400 à coup sûr sur les upstreams stricts, là où le défaut 16384 fait passer la requête. Retour à `> 0` (règle commitée `b6c6543`, test du 2026-09-11), **le garde-fou booléen est conservé** — `True`/`False` sont des `int` en Python, `max_tokens: true` serait devenu une limite de 1 token. Test renforcé au passage : `True` et `False` ajoutés à la liste des valeurs absurdes. Le clamp V3/E2 (`config/limits.py`) ne dépend pas du `>= 0` : `tests/test_output_limit_clamp.py` reste vert avec cette borne.
- [x] `python -m pytest -p no:cacheprovider -q` — **Fait 2026-10-10** : **suite entière verte, `PYTEST_EXIT=0`** — 2 740 tests au cumul des marqueurs de progression : 2 736 passés, 3 skips, 1 xfail, **0 échec**. Avant cette carte, les quatre rouges ci-dessus suffisaient à faire sortir la suite en `PYTEST_EXIT=1` à chaque vague. Les 3 skips sont : les 2 dossiers absents de `test_tool_schema_strict` + 1 skip conditionnel préexistant ailleurs dans la suite (hors périmètre). Contrôle négatif : la vague D2 n'avait introduit aucun rouge de plus, et cette carte n'en retire aucun comportement — elle remet la suite dans un état où un prochain `PYTEST_EXIT=1` voudra vraiment dire quelque chose. **Relancée après le cinquième point (garde-fou statique inclus) : `PYTEST_EXIT=0`, 3 125 tests cumulés — 3 121 passés, 3 skips, 1 xfail, 0 échec**, soit exactement le total de `pytest --co -q` (3 125, dont 161 cas du garde-fou). Les 3 skips et l'xfail sont inchangés ; l'écart 2 740 → 3 125 tient aux 161 cas du garde-fou **plus** les fichiers de tests ajoutés entre-temps (`test_classify_upstream_error`, `test_docker_autostart`, `test_models_catalog`, `test_reasoning_options`, `test_retry_helper`, `test_upstream_retry_budget`, `test_tool_order_parity`, `test_output_limit_clamp`, `test_purged_ids_tombstone`, `test_custom_routes_shadow`, `test_stream_cache_delta`). Dénombrement fait sur les marqueurs de progression du journal des seules lignes de progression (`^[.FEsxX]+`), et non sur un résumé final : le récapitulatif de fin de pytest (`===== N passed =====`) est absent du journal de ce run, le cumul des marqueurs a donc été recoupé avec `pytest --co -q` pour garantir qu'aucun caractère n'a été perdu ni emprunté au texte d'un warning. **Suite entière relancée avec `--junit-xml` : `tests=3125 failures=0 errors=0 skipped=4`, `PYTEST_EXIT=0`** — le total et les trois catégories confirment le décompte ci-dessus. Le `skipped=4` du JUnit range les `xfail` parmi les skips, d'où 3 skips + 1 xfail. Les 3 skips : les 2 dossiers absents de `test_tool_schema_strict` + 1 conditionnel d'environnement ; l'unique xfail est **`tests/test_e2e_protocol_matrix.py:2045` — `test_p2_jambe_free_endpoint_responses_stream_sans_cle_payante`**, « TROU 6 », un **défaut mesuré et documenté** (la jambe free appelée depuis le rattrapage `AllKeysPausedError` part en `stream: true` vers un `/responses` free, la réponse SSE n'est pas exploitable par `_try_free_model_first` qui lit `{}` et rend 503 alors que l'amont a répondu 200). Ce n'est pas un rouge à rattraper : c'est un xfail **non strict**, volontairement laissé en l'état, qui deviendra un vrai test le jour où le trou sera bouché. **Portée du « 0 échec » à lire avec la fenêtre d'exploitation H1 (constat du 2026-10-11)** : le code de sortie de la suite n'est **pas** une propriété stable de l'arbre, il est couplé à l'état **vivant** de `config.yaml`. `tests/test_models_catalog.py` lit `config.settings.MODELS`, chargé à l'import depuis le `config.yaml` du dépôt sans détour de `CONFIG_PATH` — volontairement, le métier du verrou de purge (`test_ids_purges_absents_de_models`) étant précisément de voir le fichier réel. Quand le `pythonw` vivant a réintroduit les 5 ids morts, les 5 tests correspondants passent rouges **sans qu'aucun code ni test n'ait bougé** (re-constaté le 2026-10-11 à 05:06). Le `PYTEST_EXIT=0` de cette carte vaut donc **pour l'état du `config.yaml` au moment du run** ; il ne garantit pas la reproductibilité d'un run à l'autre tant que la fenêtre H1 n'est pas soldée par le restart. Les deux seuls autres rouges ouverts ce jour étaient du lint et sont réglés (voir la carte qualité ci-dessous). **Suite entière revérifiée le 2026-10-12 avec `--junit-xml` : `tests=3188 failures=0 errors=0 skipped=5`, `PYTEST_EXIT=0` (`full_sweep_2026-10-12.xml`).** Le `skipped=5` range les xfails parmi les skips (3 skips environnement/dumps + 2 xfails connus). L'écart 3125 → 3188 tient aux tests ajoutés depuis. Réserve honnête : le run a paru figé à 99 % plusieurs minutes (log bloqué à 10:28:31, CPU pytest immobile) avant de terminer à 10:33:16 — probablement un teardown réseau/VPN lent, pas un test fautif (la queue finale VPN/websearch/compaction rejouée isolément sort EXIT=0).
- [x] **Découverte en clôture : deux tests écrivaient le VRAI `config.yaml` du dépôt.** `tests/test_free_model_map_http.py` et `tests/test_lot6_config.py::test_persist_vpn_config_returns_error_string` détournaient le chemin d'écriture en réaffectant `api.__file__` — ce qui arrangeait l'époque où `_persist_free_model_map` recalculait son chemin par `os.path.dirname(os.path.dirname(__file__))`. Depuis que le module porte la constante unique `api.__CONFIG_YAML_PATH` (`dashboard/api.py:208`), ce détour est **devenu inopérant** : les deux écrivains visaient le fichier du dépôt. Le piège est le silence — **verts en exécution isolée** (les assertions relisent ce que le test vient d'écrire), **rouges en suite complète** (la copie `tmp_path` n'a jamais été créée), et dans les deux cas `free_model_map` passait de 12 entrées à 1. **Fait 2026-10-10** : (1) `free_model_map` **restauré** à ses 12 entrées depuis `config.yaml.v*.bak.*`, vérifié contre les 4 sauvegardes **et** contre `git show HEAD:config.yaml` — seul `space-bunny` est l'ajout de l'utilisateur, rien d'autre n'a bougé ; (2) les deux tests redirigent la constante (`_redirect_yaml_path` pour le premier, `monkeypatch.setattr(api, "__CONFIG_YAML_PATH", ...)` pour le second) ainsi que `_config_yaml_known_mtime` ; (3) **`tests/test_no_real_config_write.py`** — garde-fou **statique AST**, 160 fichiers de test scannés, deux règles : interdiction de réaffecter `api.__file__`, obligation de neutraliser tout écrivain YAML réel (`save_yaml_config` / `save_custom_routes` / `yaml_set` → `CONFIG_PATH` ; `_persist_vpn_config` / `_persist_free_model_map` → `__CONFIG_YAML_PATH`), auto-contrôle inclus ; (4) chemin faisant, **`test_save_custom_routes_no_parallel_json` fuitait** une route sans clé `model` dans les globaux `CUSTOM_ROUTES` / `ROUTES` → `KeyError: 'model'` en aval, restauré en `finally`. Vérification ciblée : les 5 fichiers concernés **verts, `PYTEST_EXIT=0`**, y compris dans l'ordre exact qui échouait avant, et sous 4 permutations d'ordre (pytest-randomly n'est pas installé : `-p no:randomly` est un no-op, l'ordre est déterministe) ; **contrôle négatif** — remettre `test_lot6_config.py` à `HEAD` reproduit le `KeyError`, donc la fuite est bien préexistante et non introduite ici. La garde est **statique et non à l'horloge** : le `pythonw` vivant réécrit légitimement `config.yaml`, donc surveiller le fichier pendant la suite donnerait des faux positifs — on vérifie la **cause**, pas le symptôme. **Contrôle final : suite entière relancée, `PYTEST_EXIT=0`, 3 125 tests cumulés — 3 121 passés, 3 skips, 1 xfail, 0 échec** (détail du dénombrement au quatrième point ci-dessus). `config.yaml` vérifié intact après les runs : `free_model_map` à 12 entrées, 31 clés de premier niveau.
- [x] **Passe lint / qualité (2026-10-11)** : `python -m ruff check .` rendait **15 anomalies**, trois corrigées et revérifiées. `config/models_catalog.py:236` **C420** — `{v: 0 for v in VERDICTS}` → `dict.fromkeys(VERDICTS, 0)`, annotation `dict[str, int]` conservée (les ints sont immuables, le `+= 1` qui suit rebinde la clé, pas l'objet) ; `tests/test_models_catalog.py` **F841** — `cli = _load_cli()` jamais lu dans `test_rapport_texte_couvre_toutes_les_entrees`, `_load_cli()` est un pur `importlib` sans effet de bord, ligne retirée ; `tests/test_server_compaction.py:710` **E731** — `est = lambda t: ...` → `def est(t)`. Contrôle : `ruff check` **All checks passed** sur les trois fichiers, mypy n'ajoute **aucune** erreur (`config/models_catalog.py` n'apparaît pas parmi les 10 erreurs préexistantes de `config/loader.py`, `core/errors.py`, `app/protocol/mapping.py`, qui viennent des imports suivis, pas de ce changement). **Triage des 12 restantes** : **F811 `tests/test_purged_ids_tombstone.py:329` est un faux positif** — `isolated_yaml` y est un **paramètre de fixture** pytest qui masque l'import du même nom (`from test_hot_reload import isolated_yaml`), usage idiomatique ; la fixture redirige bel et bien `config.settings.CONFIG_PATH` vers `tmp_path` (`tests/test_hot_reload.py:96-118`), donc le test est hermétique et le garde-fou statique de la carte précédente ne le signale pas — rien à corriger. Les 11 autres (I001 imports non triés, UP041, UP017, F541, C408) sont cosmétiques et hors périmètre. **`ruff format` n'est PAS appliqué, et il ne faut pas l'appliquer** : `ruff format --check .` rend **162 fichiers non conformes sur 270** — dont `tests/test_server_compaction.py` **dès `HEAD`**, vérifié sur la version commitée. Le formateur n'a jamais été un garde sur cet arbre : le passer créerait un diff de 162 fichiers sans changer un seul comportement. Seul `ruff check` fait foi. **Revérifié le 2026-10-12 : `ruff check .` rendait 9 anomalies — 8 cosmétiques déjà triées hors périmètre le 2026-10-11 (I001 ×4, UP041, UP017 ×2, F541) laissées intactes, 1 F841 nouvelle (`tests/test_free_quota_vs_transient.py:147`, walrus `st_dummy` assigné jamais lu) corrigée en `[object()]`, `ruff check` propre sur le fichier + 13 tests EXIT=0. Ordre `corrige` exécuté le même jour : les 7 erreurs restantes signalées par `ruff check .` sont corrigées — `ruff --fix` en réalise 9 sur 5 fichiers (`app/compaction/__init__.py` I001 : tri d'import ; `scripts/check_models_catalog.py` : UP017 ×2 `datetime.now(timezone.utc)` → `datetime.now(UTC)` + F541 f-string sans placeholder ; `tests/test_classify_upstream_error.py`, `tests/test_free_quota_vs_transient.py`, `tests/test_stream_cache_delta.py` : I001 lignes vides / ordre d'imports). Contrôle : `ruff check .` → `All checks passed!`, 129/129 EXIT=0 sur les fichiers touchés (`c4_ruff.xml`, temporaires nettoyés), `ast.parse` du script OK, `import app.compaction` OK. Le contrat `ruff format` reste inchangé : jamais lancé, pas un gate.**
- [ ] **Rollback** : revert d'un fichier par point — `tests/test_tool_schema_strict.py` (les 2 skips), `tests/test_free_response_sse_interface.py` (l'invariant), et **`app/protocol/mapping.py` + `tests/test_max_completion_tokens.py` ensemble** pour le troisième : remettre `>= 0` sans retirer `True`/`False` du test le ferait échouer. Attention, `app/protocol/mapping.py` porte aussi tout le travail V3/E2 non commité : le revert se fait à la main sur la seule condition de la boucle, jamais avec `git checkout --`. **Pour le cinquième point (herméticité), ne PAS `git checkout -- config.yaml`** : le fichier porte le travail V3/E2 non commité **plus** l'ajout `space-bunny` — la restauration se fait depuis `config.yaml.v<N>.bak.<date>`. Reverts ciblés : `tests/test_no_real_config_write.py` (suppression pure), `tests/test_lot6_config.py` (2 tests) et `tests/test_free_model_map_http.py` (`_redirect_yaml_path`) ; revert du **seul** garde-fou en gardant les redirections donne un garde-fou qui ne teste plus rien, revert des **seules** redirections ramène le défaut silencieux.

---

## 19. Lot Z — Découverte d'implémentation : le shadow `custom_routes`

> **Trouvé le 2026-10-09, pendant la vérification de la carte A2.** Aucune case
> de la V1 n'est invalidée par cette section, mais elle change la **lecture** de
> deux d'entre elles (A1 partiellement, A2 pour ses tests) et elle explique deux
> échecs de test **antérieurs à toute édition de cette session**.

### 19.1 Le mécanisme, dans l'ordre du code

1. `opencode.py:11195` — `route = _route_for(original_model)`.
2. `app/router/__init__.py:108-119` — si l'id demandé est une clé exacte de
   `models:`, on part de la route identité, **puis** on balaie
   `SORTED_CUSTOM_ROUTES` et toute route personnalisée dont un `match` est
   **égal** à l'id (`m == name`, pas une sous-chaîne) **remplace** le modèle
   résolu.
3. `opencode.py:11201-11204` — `model_id = route["model"]`, puis
   `get_model_config(model_id)` : c'est l'entrée `models:` **de la cible de la
   route personnalisée** qui est lue, jamais celle de l'id demandé.

Deux conséquences mécaniques :

- **Le passage est unique, non transitif.** `gpt-5.6-luna` → `muse-spark-1.3-contributor`
  ne repasse pas dans `custom_routes`.
- **Une route personnalisée peut shadower l'entrée `models:` de l'id qu'elle
  matche**, y compris pour un modèle absent de tout `custom_routes` par ailleurs.

### 19.2 Le shadow actif

`config.yaml:419-422` :

```yaml
musespark13contributor:
  match:
  - muse-spark-1.3-contributor
  model: step-5-preview-free
```

`step-5-preview-free` (`config.yaml:164-165`) n'a ni `api:` ni `endpoint:` et
porte le suffixe `-free` → `_resolve_model_endpoint` rend
`https://opencode.ai/zen/v1/chat/completions` (jambe anonyme).

### 19.3 Ce que ça change, concrètement

| Fait | Portée |
|---|---|
| **La session courante roule sur la jambe libre.** Toute requête nommant `muse-spark-1.3-contributor` est réécrite en `step-5-preview-free` **avant** résolution d'endpoint. L'entrée `models.muse-spark-1.3-contributor` (`api: responses`, `geo: meta-muse-spark`, `thinking.min_tokens: 4096`) n'est **jamais lue** pour cet id. | Toutes les observations « live » faites jusque-là sur ce modèle portent en réalité sur `step-5-preview-free`. |
| **`muse-spark-1.3-contributor` n'est pas un modèle payé à l'usage** : il est routé vers l'anonyme. | Ça ne casse rien (le modèle répond), mais tout raisonnement du type « Go est refusé / accepté pour muse-spark-1.3 » est hors sujet. |
| **La ligne A1 ajoutée sur `gpt-5.6-luna` est inerte à l'exécution** : `gpt56luna` (`config.yaml:399-402`) réécrit l'id en `muse-spark-1.3-contributor`, dont l'entrée portait **déjà** `api: responses` avant l'édition. L'endpoint effectif reste `/go/v1/responses` — donc **l'intention d'A1 est satisfaite pour ce modèle, mais pas par la ligne ajoutée**. | A1 : 4 modèles sur 5 sont opérationnels par leur propre ligne (`grok-4.5`, `grok-4.6`, `grok-4.7`, `gpt-6-luna` — aucun n'est matché par un `custom_routes`). Le 5ᵉ est correct « par héritage ». |
| **Les 2 échecs de `tests/test_e2e_protocol_matrix.py` s'expliquaient exactement par ça** (ancrages revérifiés 2026-10-10 : `test_p5_responses_to_openai_nonstream` :713, `::test_p5_responses_to_openai_stream` :852 ; `PAID_RESPONSES` :73), parce que `PAID_RESPONSES` valait `muse-spark-1.3-contributor`, *shadowé* → réécrit en `step-5-preview-free`. | Rouge **avant** cette session — **réparé depuis** (Lot Z §19.5, option A) : seam `SORTED_CUSTOM_ROUTES` vidée à `:467`, shadow verrouillé par `tests/test_custom_routes_shadow.py`. Fichier vert, en isolation comme dans la combinaison des 4 fichiers. |
### 19.4 Priors de non-régression (pour le lot à part)

- **Ne pas toucher `custom_routes` en prod.** `musespark13contributor` est une
  route vivante, choisie (coût / quota) : la retirer change le routage réel de la
  session, pas seulement un test.
- **Ne pas « réparer » le test en assouplissant l'assertion.** Une case cochée
  sur un test dont on a déplacé le curseur ne vaut rien (§20 règle 2).
- **Ne pas `git checkout -- config.yaml`** pour ces lots (règle V1, §18).

### 19.5 Deux réparations légitimes, à faire dans une vague dédiée

- [x] **Option A (recommandée) — FAITE 2026-10-10.** Seam cousue dans `_install_seams`
  (`tests/test_e2e_protocol_matrix.py:426-499`) :
  `monkeypatch.setattr(_cfg_settings, "SORTED_CUSTOM_ROUTES", [])` → **ligne 467**,
  précédent `tests/test_proxy.py:601` cité en commentaire `:464`. Le fichier mesure
  alors la **matrice de protocoles**, ce qu'il prétend mesurer, et le shadow sort du
  champ du test. Marqueur `Z1 (plan 19.5, lot Z)` posé en tête de fichier (`:63`).
- [ ] **Option B — non retenue.** Re-pointer `PAID_RESPONSES` (`:73`, toujours
  `muse-spark-1.3-contributor`) sur `grok-4.7` changerait le **comportement runtime**
  de la suite entière, pas seulement un test : la seam d'A isole déjà le shadow sans
  toucher la config. À ne déterrer que si A se révèle insuffisante.
- [x] **Test dédié qui acte le shadow — FAIT** : `tests/test_custom_routes_shadow.py`
  (fichier neuf). Il lit la config **telle qu'elle est déployée** (pas de seam),
  résout `muse-spark-1.3-contributor` via `route_for` frais, et verrouille
  `_route_for(...)["model"] == "step-5-preview-free"` + `get_model_config` de la
  cible rendant la jambe anonyme. La déviation cesse d'être invisible.
- [x] `pytest tests/test_e2e_protocol_matrix.py tests/test_proxy.py tests/test_hot_reload.py tests/test_custom_routes_shadow.py` — **vert 2026-10-10** (`PYTEST_EXIT=0`, 1 xfailed préexistant, 0 échec). Vérifié aussi en isolation et dans la combinaison des 4 fichiers de la case A2. *Revérifié le 2026-10-12 : 165 collectés, 164 passés + 1 xfail connu, EXIT=0, JUnit tests=165 failures=0 errors=0 skipped=1.*
- [ ] **Rollback** : revert du fichier de test (config inchangée).

### 19.6 Fin du shadow (2026-10-12) — la route est redevenue une identité

La route `musespark13contributor` ne réécrit plus `muse-spark-1.3-contributor`
vers `step-5-preview-free` : `config.yaml` déclare désormais match = model =
`muse-spark-1.3-contributor` (**identité**). Conséquences :

- `_route_for("muse-spark-1.3-contributor")["model"] == "muse-spark-1.3-contributor"` ;
  l'entrée `models.muse-spark-1.3-contributor` (`api: responses`, geo
  `meta-muse-spark`) **est** lue au runtime.
- La jambe free passe par `FREE_MODEL_MAP`
  (`muse-spark-1.3-contributor → muse-spark-1.3-contributor-free`, `api: responses`,
  endpoint `https://opencode.ai/zen/v1/responses` anonyme), pas par `custom_routes`.
- `tests/test_custom_routes_shadow.py` a été **réécrit** (7 tests) : il verrouille
  l'identité, la cible free via `FREE_MODEL_MAP`, la jambe responses anonyme, la
  non-transitivité, l'effet `gpt56luna` et les deux contre-épreuves. Un retour du
  shadow casserait bruyamment au lieu de passer inaperçu.
- Les observations « live » antérieures au 2026-10-12 portant sur
  `muse-spark-1.3-contributor` décrivaient en réalité `step-5-preview-free`
  (jambe chat anonyme) ; les observations postérieures portent sur le vrai chemin
  (jambe responses anonyme via `FREE_MODEL_MAP`).

---

## 20. Comment utiliser ce document

1. On coche la vague (§17), puis les cases de chaque carte (§18), **dans l'ordre**.
2. Une case ne se coche pas sans la commande de test correspondante passée.
3. Tout écart nouveau retourne en §14.1 pour être classé, puis gagne une carte ici.
4. Les ancrages sont datés : s'ils ont bougé, on corrige la carte, on ne devine pas.
5. Ce document reste un **plan** : rien de ce qu'il décrit n'est appliqué tant qu'une
   vague n'est pas ouverte explicitement.

