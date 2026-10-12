# Modèles × protocoles

> [plan v10 §11.3 Lot D] État réel au 2026-08-24, dérivé de `config.yaml`
> (source canonique : `ROUTES`, `free_model_map`, `MODELS`).
> Vérifié par `tests/test_docs_drift.py` via `docs/_drift_manifest.json`.

## Protocoles supportés (config `protocol`)

| Protocole | API cible | Conversion requête | Conversion réponse |
|---|---|---|---|
| `anthropic` | Anthropic Messages `/v1/messages` | aucune (passthrough) | aucune |
| `openai` | Chat Completions `/v1/chat/completions` | `anthropic_to_openai` | `openai_to_anthropic` |
| `openai-responses` | Responses `/v1/responses` | `anthropic_to_openai_responses` / `openai_responses_to_anthropic` | `_responses_to_anthropic_response` / `openai_chat_to_responses` |

### `tool_choice` et repli `protocol.emit_default_tool_choice` (PLAN E1 2026-10-12)

Parité officielle : `anthropic_to_openai` n'émet `tool_choice` que si le
client en a posé un — un corps sans la clé part sans le champ (plus de
`tool_choice: "auto"` posé d'office). `disable_parallel_tool_use: true`
continue de fonctionner seul (`parallel_tool_calls: false` émis même sans
clé `tool_choice`). Repli : `protocol.emit_default_tool_choice: true`
restaure l'ancien comportement (toujours émettre, défaut `false`).

## Modèles gratuits (stations VPN)

Upstream = endpoint OpenAI-compatible (`protocol: openai`). Le client parle
toujours Anthropic (`/v1/messages`) ; la conversion est verrouillée par les
goldens §v1-response.md. Aliases actuels (`free_model_map`) :

- `deepseek-v4-flash` → `mimo-v2.6-flash-free` (le `-free` homonyme est mort côté amont : 0 succès, que des 400 — voir config)
- `glm-5.1`, `kimi-k2.6`, `minimax-m2.5` → `mimo-v2.6-flash-free`
- `mimo-v2.5` → `mimo-v2.6-flash-free`
- `mimo-v2.6-flash` → `mimo-v2.6-flash-free`
- `qwen3.7-max` → `mimo-v2.6-flash-free`
- `muse-spark-1.2-contributor` → `muse-spark-1.3-contributor-free` (la cible `-free` homonyme est `deprecated` côté anonyme — recâblé N2 le 2026-10-12)
- `muse-spark-1.3-contributor` → `muse-spark-1.3-contributor-free`

Modèles free directs : `nemotron-3-ultra-free`,
`nemotron-3.5-lightning-free`, `deepseek-v4-flash-vision-exp`, `longcat-2.0`.

> Note : les ids morts (`kimi-k2.7`, `hy3-free`, `laguna-s-2.1-free`,
> `x-preview-f-free`, `ox-alpha-free`) ont été retirés du seed `config.yaml`
> (2026-09-09 — pings live 503 upstream) ; si la découverte auto les
> ré-annonce, elle les ré-ajoute (add-only) sans édition manuelle.

## Inventaire contre le catalogue live (alignement client officiel H1)

Rien ne comparait `models:` au catalogue **live** de l'éditeur : une entrée
pouvait survivre des mois après la disparition de son modèle et mourir au
premier appel, sans que rien ne le signale. Relevé du 2026-10-10 sur
<https://models.opencode.ai/api.json> (deux providers nous concernent :
`opencode-go` = jambe payée, `opencode` = jambe anonyme ; l'URL exige un
`User-Agent`).

**Le job** — `python scripts/check_models_catalog.py` (lecture seule, il
n'écrit jamais `config.yaml`). Désactivé par défaut (`models_catalog.enabled:
false`) ; `--force` pour une inspection ponctuelle, `--out`, `--json`. Sortie
horodatée UTC dans `logs/models_catalog.diff.<horodatage>.json`.

**Comment la jambe est décidée** — par l'**endpoint résolu**, pas par le nom :
`config.settings._resolve_model_endpoint` (source unique) fait gagner un
`endpoint:` explicite sur l'heuristique du suffixe `-free`. Trois ids de la
jambe payée finissent pourtant par `-free` (`longcat-2.5-preview-free`,
`space-bunny-free`, `step-5-preview-free`), et `space-bunny-free` est
`deprecated` côté payé mais actif côté anonyme : le verdict suit la jambe
**routée**. Verrouillé par `tests/test_models_catalog.py`.

| Verdict | Sens | Traitement |
|---|---|---|
| `mort` | absent des **deux** jambes, sans endpoint interne | purge |
| `hors-jambe` | routé d'un côté, hébergé de l'autre | lot B1 (migration de route), pas une suppression |
| `deprecated` | fin de vie **sur la jambe routée** | conservé : toujours servi |
| `non-catalogue-interne` | endpoint interne hors catalogue | **conservé** — ne jamais retirer |
| `ok` | conforme | — |

État après inventaire (54 entrées) :

- **Purgés (5)** — `mimo-v2-pro`, `mimo-v2-omni`, `hy3-preview`,
  `omen-alpha`, `deepseek-flash`. Leur entrée d'avant purge était
  `{protocol: openai}` et rien d'autre : `get_model_config()` retombe sur le
  repli de la source unique `_resolve_model_endpoint`, qui leur donne
  `{endpoint: https://opencode.ai/zen/go/v1/chat/completions, protocol:
  "openai"}` — à l'identique (vérifié le 2026-10-11 : ni suffixe `-free`, ni
  famille `responses`, donc jambe payée). Seul consommateur de `capabilities` :
  le gate `web-search` d'`opencode.py`, faux dans les deux états ; aucun
  consommateur ne lit `api`.
- **Conservés hors-jambe (5)** — `glm-5`, `glm-5.1`, `kimi-k2.5`,
  `minimax-m2.5`, `qwen3.5-plus`. `glm-5.1` et `minimax-m2.5` sont vivants côté
  anonyme **et** sont `routing.sonnet_model` / `routing.haiku_model` : les
  traiter en B1.
- **Conservés `deprecated` (8)** — `kimi-k2.6`, `mimo-v2.5-free`,
  `deepseek-v4-flash-free`, `fledge-alpha-free`,
  `muse-spark-1.2-contributor-free`, `space-bunny-free`, `exo-free`,
  `step-5-preview-free`.
- **Conservés internes (2)** — `jev-1.13`, `jev-1.13-free` (endpoint
  `/zen/v1/systemone`).

> **Piège de durabilité.** `save_yaml_config()` (`config/settings.py:226`)
> ré-écrit `config.yaml` **depuis la mémoire du process vivant** via
> `yaml.dump(_yaml_data, sort_keys=False)` : les commentaires manuscrits sont
> perdus, et les ids purgés peuvent revenir si le process n'a pas encore
> rechargé le fichier. Seuls `_PROTECTED_SECTIONS` (`dashboard_trust`,
> `client_auth`, `supervisor`) et `_PROTECTED_NESTED`
> (`ip_rotation.latency_rotation`) survivent à la fusion — `models:` n'est pas
> protégé. Cet inventaire est donc tenu **ici** plutôt que dans les
> commentaires de `config.yaml`.

## Matrice `npm` → endpoint, et périmètre de fourniture (N3)

**La règle de périmètre.** Le proxy ne fournit **que** les modèles servis par le
provider opencode — la jambe `opencode-go` ou la jambe `opencode` — plus
l'exception interne `jev-1.13` / `jev-1.13-free` (endpoint `/zen/v1/systemone`,
donc bien l'infra opencode, simplement hors catalogue public). Rien d'autre :
un id hébergé par un provider **tiers** n'est pas « servi par opencode », et sa
route passerait par un endpoint que la passerelle n'expose pas.

**Ce que le relevé live du 2026-10-11 a corrigé.** La carte N3 du plan partait
d'un `npm` **au niveau modèle** : il n'existe pas. Dans
<https://models.opencode.ai/api.json>, `npm` est une clé de **niveau provider**
— `opencode-go` et `opencode` déclarent tous deux `@ai-sdk/openai-compatible`,
et les 8468 entrées de `models:` n'en portent aucune. Il n'existe non plus
**aucun** indicateur de protocole au niveau modèle : les entrées ne contiennent
que de la métadonnée descriptive (`id`, `name`, `limit`, `cost`, `family`,
`reasoning_options`, …). La matrice ne peut donc pas être déduite par modèle ;
elle se lit sur le provider qui sert l'id.

| `npm` (niveau provider) | Famille d'API | Protocole | Connu ? |
|---|---|---|---|
| `@ai-sdk/openai-compatible` | `chat` | `openai` | oui — **nos deux jambes** (200 des 226 providers) |
| `@ai-sdk/anthropic` | `messages` | `anthropic` | oui (8 providers) |
| `@ai-sdk/openai` | `responses` | `openai` | oui (6 providers) |
| 24 autres valeurs | — | — | **non** |

**Le piège des 9 « npm inconnus », résolu.** Le plan comptait 8 `gemini-*` et
`mistral-large-4` comme « `npm` hors table ». Faux : ces 9 ids **sont** dans la
jambe `opencode`, dont le `npm` est `@ai-sdk/openai-compatible` — donc `chat`,
connu. Leur `npm` inconnu (`@ai-sdk/google`, `@ai-sdk/mistral`) est celui de
leur provider **d'origine**, qui n'est pas la route : `google` et `mistral`
n'ont même pas d'`api` dans le catalogue, pas plus que 25 providers sur 226.
Servir ces ids passe par la jambe opencode, jamais par leur provider d'origine.

**Échec explicite, pas d'endpoint inventé.** `api_family_for_npm()` rend `None`
pour un `npm` hors table : c'est une réponse, pas un silence. `refusal_reason()`
en tire le message de refus — il **nomme** l'id, les providers tiers qui
l'hébergent, et le `npm` de chacun, y compris quand ce `npm` est hors table :

> `model "glm-4.6v" is not provided by this proxy: no opencode provider serves
> it, it is hosted upstream by 302ai (npm @ai-sdk/openai-compatible, api chat),
> aihubmix (npm @aihubmix/ai-sdk-provider, outside the known table: no endpoint
> can be derived), llmgateway (…) and 3 more.`

La liste d'hôtes est bornée (`MAX_NAMED_HOSTS = 3`) : un id tiers peut être
hébergé par une demi-douzaine de providers, et au-delà la liste noie
l'information utile sous du bruit. `is_servable()` ferme l'autre côté : un id
qu'aucun provider opencode ne sert est refusé, sauf les deux internes.

**Relevé live du 2026-10-11** — 8468 entrées (`provider × id`) mais seulement
**3977 ids distincts**, un même id étant souvent hébergé par plusieurs
providers :

| Périmètre | Ids | Sortie |
|---|---|---|
| Servi par `opencode-go` ou `opencode` | 132 | fourni |
| … dont hébergé **aussi** par un provider tiers | 91 | fourni — la route opencode gagne |
| Uniquement chez un provider tiers | 3845 | **refusé**, message ci-dessus |

Soit 41 ids que seul opencode sert, et 3845 que le proxy ne fournira pas.

**Ce que N4 a fait du repli (2026-10-11), et ce qui reste ouvert.** Le repli ne
choisit plus la jambe à la main : `get_model_config()`
(`config/settings.py:1557`) repasse par `_resolve_model_endpoint`
(`config/settings.py:822`), la **source unique** qui bâtait déjà `MODELS` à
l'import — un id `-free` non déclaré visait `API_BASE_OPENAI`, la jambe
**payée**, y compris en famille `responses` ; il vise maintenant la jambe anonyme
(`/zen/v1/responses` pour `muse`/`spark`, sinon `API_BASE_FREE`). Le durcissement
est resté conditionné exactement comme le piège l'exigeait : parce qu'un
`endpoint:` explicite gagne dans `_resolve_model_endpoint`, les ids déclarés
ressortent à l'identique (vérifié clé par clé sur tout `MODELS`) et
`jev-1.13*` conserve son endpoint interne `/zen/v1/systemone`.

**Reste ouvert, et c'est une limite du nom, pas du routage.** Le repli est
**local** : il ne lit que le suffixe `-free` et la famille d'API. Un id que seule
la jambe anonyme sert mais dont le nom ne porte rien — pas de `-free`, pas de
famille `responses` — part encore payé avant la découverte auto. La jambe réelle
arrive ensuite par `free_discovery_urls` (qui réunit les `/models` des deux
jambes) et `detect_free_ids` (qui classe sur `pricing 0/0` / `free: true`), et
écrit un `MODELS[id] = {endpoint: …}` explicite — l'explicite gagne. Le repli
couvre donc la fenêtre d'avant découverte et le mode déconnecté ; c'est cette
limite que verrouille `test_repli_id_muet_reste_sur_la_jambe_payee`
(`tests/test_model_catalog.py`), sans l'arrondir. Du côté du périmètre en
revanche, la question a une réponse complète : `refusal_reason()` lit le
catalogue live et ne refuse donc **jamais** un id qu'un provider opencode sert.

Implémenté dans `config/models_catalog.py` (`NPM_TO_API`, `INTERNAL_IDS`,
`serving_provider`, `provider_npm`, `api_family_for_npm`, `hosts_of`,
`refusal_reason`, `is_servable`) et `core/errors.py` (`ModelOutOfScope`, qui
porte `model_id` + `reason`). Verrouillé par `tests/test_npm_routing.py` (le
périmètre, les trois formes du message, la borne, le tri des hôtes, le porteur
d'erreur) et `tests/test_models_catalog.py` (le classement des entrées
configurées, le job en lecture seule).

## Clés de config surveillées (drift)

Voir `docs/_drift_manifest.json` — toute clé listée doit exister dans
`config.yaml` ET être mentionnée dans la doc associée ; l'inverse n'est pas
bloquant tant que le manifeste ne la référence pas.
