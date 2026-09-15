# Matrice de conversion Anthropic ↔ OpenAI

> [plan v10 §11.3/§10.1 Lot D] Dérivée du CODE réel (`protocol_mapping.py`,
> lignes vérifiées le 2026-08-24) et des références officielles :
> - Messages API : <https://platform.claude.com/docs/en/api/messages>
> - Chat Completions : <https://platform.openai.com/docs/api-reference/chat>
> - Responses API : <https://platform.openai.com/docs/api-reference/responses>

## Fonctions (source de vérité : `protocol_mapping.py`)

| Fonction | Sens | Verrou golden |
|---|---|---|
| `anthropic_to_openai(body, model)` | requête `/v1/messages` → Chat Completions upstream | `req_simple`, `req_tools`, `req_tools_complex`, `p2_effort_*`, `p2_cache_control_*`, `p2_max_completion_tokens_o3`, `p2_tools_*`, `p2_documents_*`, `p2_image_base64_and_url` |
| `openai_to_anthropic(resp, model)` | réponse Chat → contrat V1 (`/v1/messages` free/payé-B) | `resp_text`, `resp_tool_calls`, `resp_reasoning`, `openai_to_anthropic_usage_cache_creation` |
| `openai_to_anthropic_request(oai_body)` | requête client OpenAI → upstream Anthropic | `oai_request_to_anthro`, `p4_max_completion_tokens_input`, `p4_tools_strict_schema`, `p4_tool_result_media`, `p4_thinking_strip_local_signature`, `p4_request_effort_relay` |
| `anthropic_to_openai_response(anthro, model)` | réponse Anthropic → client OpenAI | `anthro_response_to_openai` |
| `openai_responses_to_anthropic(body)` | requête `/v1/responses` → upstream Anthropic | `responses_api_entry`, `p6_tools_long_name_strict`, `p6_documents_file_forms`, `p6_images_forms`, `p6_reasoning_history_dropped`, `p6_request_tool_choice_required`, `p6_request_effort_relay` |
| `anthropic_to_openai_responses(anthro, model)` | réponse Anthropic → format Responses | `anthro_to_responses_tool_use`, `anthro_to_responses_thinking_omitted` |
| `openai_chat_to_responses(chat_resp, model)` | réponse Chat → format Responses | `chat_to_responses_reasoning_and_usage` |
| `_chat_to_responses_request(chat)` | requête client Chat → body Responses (P5 aller, sanitize natif ; **et P3** quand l'endpoint amont est `/responses`) | `p5_request_chat_to_responses_media`, `p5_request_native_responses_passthrough`, `p3_chat_to_responses_bounds_stop_tools` |
| `_responses_to_chat_response(resp, model, name_map)` | réponse Responses interne → cible Chat | `responses_to_chat_response_cache_usage`, `responses_to_chat_restore_tool_name` |
| `_responses_to_anthropic_response(resp, model, name_map)` | réponse Responses interne → cible Anthropic | `responses_to_anthropic_response_tool_use` |
| `sanitize_tool_names` | noms d'outils > 64 car. → `name[:57]+sha1[:6]` + map de restauration | `sanitize_tool_names_long_and_server` |
| `strip_synthetic_thinking(body)` | historique Anthropic : retire les blocs `thinking` à signature locale — appelé par les handlers **P1**, **P4** et (depuis le 15/09/2026) **P6** | `strip_synthetic_thinking`, `multiturn_thinking_strip` |
| `_responses_sse_to_chat_deltas(raw_line)` | 1 ligne SSE Responses (sans `data:`) → delta chat, `[DONE]`→None | `sse_deltas` |

## Mapping champs — requête Anthropic → Chat Completions

| Anthropic | OpenAI Chat | Note |
|---|---|---|
| `system: str\|[{text}]` | message `role=system` (+`cache_control ephemeral`) | pas de rôle system côté Anthropic |
| `messages[].content: str` | `content: str` direct | |
| block `text` | `content` concaténé | |
| block `image` (`source.type` = `base64` ou `url`) | `image_url` : data-URI / URL, octets **intacts** | ✅ **pas de perte** — ni fetch d'URL, ni ré-encodage, ni redimensionnement |
| block `image` (tout autre `source.type`, ex. `file` + `file_id`) | **texte `[image:file]`** — l'image ne survit pas | ⚠️ **dégradation** : journalisée côté serveur, **jamais signalée au client** (voir ci-dessous) |
| assistant block `tool_use {id,name,input}` | `tool_calls[] {id,type:"function",function:{name,arguments:json}}` | arguments = JSON compacté |
| user block `tool_result {tool_use_id,content}` | message `role=tool {tool_call_id,content}` | bufferisé puis émis après l'assistant |
| `tools[] {name,description,input_schema}` | `tools[] {type:"function",function:{name,description,parameters}}` | |
| `tool_choice auto/any/tool{name}/none` | `"auto"/"required"/{"type":"function",...}/"none"` | |
| nom d'outil **≤ 200 car.** (limite Anthropic) | nom **≤ 64 car.** (limite Chat/OpenAI) | ✅ **corrigé (lot L4)** — raccourci puis restauré, voir ci-dessous |
| `max_tokens` | `max_tokens` | |
| `stop_sequences` | `stop` | |
| `temperature`, `top_p` | identiques | |
| `thinking{type}` / `output_config.effort` | `reasoning_effort` | voir « Effort » ci-dessous |

> **Images — la ligne « PERDU silencieusement » était trop large, et la voici
> remplacée par une formulation exacte.** L'ancienne version de ce document portait,
> pour le bloc `image`, la mention « **PERDU silencieusement** — bug §14.1.6 ». Elle
> était **fausse dans le cas courant et vraie dans un cas étroit**, et elle avait été
> **supprimée** lors de la refonte de ce document (commit `b6c6543`) sans être
> remplacée par une formulation correcte. Elle est rétablie ici, corrigée.
>
> - **Cas courant — aucune perte.** Un `source.type` `base64` devient un data-URI et
>   un `url` est transmis tel quel (`mapping.py:1155-1164`, émission `:1171`). Les
>   octets traversent intacts : **ni fetch d'URL, ni ré-encodage, ni
>   redimensionnement** — vérifié, aucun code de ce type n'existe sur ces chemins.
>   Idem en P5/P6 (`mapping.py:3286-3298`, `:2345-2388`) et en P1/P3, où le corps du
>   client part **verbatim** sur la jambe payante (`opencode.py:9042`, `:11536`) —
>   la jambe free, elle, peut convertir selon son endpoint, cf. la note A8 plus bas.
> - **Cas étroit — dégradation réelle.** Tout **autre** `source.type` (typiquement
>   `file` avec `file_id`) n'a pas d'équivalent Chat : l'image est remplacée par le
>   **texte** `[image:file]` (`mapping.py:1165-1169`), et de même pour une image
>   imbriquée dans un `tool_result` (`mapping.py:1290-1292`). Le serveur **trace**
>   l'événement (`_debug("DROP image source type=…")`, `:1168`) : ce n'est donc pas
>   une perte invisible côté exploitation — mais **le client n'en est jamais
>   informé**, le placeholder entrant dans le prompt et non dans la réponse.
>   Verrouillé par
>   `tests/test_conversion_images.py::test_anthropic_file_image_becomes_placeholder_not_drop`.
> - **Trou latent, direction réponse.** Les convertisseurs de réponse
>   (`anthropic_to_openai_response`, `mapping.py:2281-2295` ;
>   `anthropic_to_openai_responses`, `:2633-2668`) ne traitent que `text`,
>   `thinking` et `tool_use` : un bloc `image` renvoyé par un amont Anthropic serait
>   **ignoré sans trace**, et le flux amont ne traite que `text_delta` /
>   `input_json_delta` (`opencode.py:12954-12968`). **Non observé en pratique** — ce
>   dépôt ne produit aucune image en sortie — donc trou latent, pas panne reproduite.
> - **Chemins non vérifiés.** **P1 et P3 n'ont aucun test d'image.** Le corps y est
>   transmis verbatim, ce qui rend une perte très improbable, mais elle n'est **pas
>   verrouillée**. Décompte des tests portant réellement une image : **P2 = 5,
>   P4 = 1, P5 = 1, P6 = 2**.
>
> **`tool_choice` : l'amont n'accepte que `"auto"` — mesuré.** Toute autre valeur
> est refusée par l'amont avec un `400` explicite, dont le message, cité mot pour
> mot, est : *« only `"auto"` is supported for `tool_choice`. `"none"`,
> `"required"`, and named function choices are not currently supported »*.
> C'est une **limitation de l'amont, pas un défaut de conversion** : la traduction
> du proxy est conforme (forme nommée `{"type":"function","name":…}` côté
> Responses). Reproduction : **3/3 rondes sur les deux protocoles** — `auto` →
> `200`, `required` et la forme nommée → échec, **avec un nom d'outil court comme
> avec un nom long** : la longueur du nom n'est donc pas la variable. Le refus est
> retenté sur 5 stations, puis le repli payant se heurte au `403 DataPolicyError`
> du compte, d'où un `503` composite (`free=400` + `paid=403`) — ce que confirment
> les colonnes `free_status`/`paid_status` de `logs/requests.db`.
>
> Conséquence pratique : **le remap du `tool_choice` nommé apporté par A8 n'est pas
> exerçable en réel sur cet amont**, toute forme nommée étant refusée avant
> d'atteindre le modèle. Il demeure correct et couvert hors ligne.
>
> **Noms d'outils : 200 caractères (Anthropic) vs 64 (Chat/OpenAI) — écart A8
> CORRIGÉ (lot L4).** `sanitize_tool_names` (raccourcissement déterministe + map
> de restauration) est désormais câblé sur **tous** les chemins, et non plus
> seulement sur les chemins Responses (`_sanitize_native_responses_request`,
> `_chat_to_responses_request`).
>
> - Vers une cible **Anthropic** : aucun raccourcissement n'est nécessaire (200
>   caractères sont légitimes) — comportement correct, couvert par
>   `test_tools_matrix.py::test_axis_long_name_is_legal_towards_anthropic`.
> - Vers une cible **Chat via Responses** : le nom est raccourci à 64 avec map de
>   restauration — correct, couvert par
>   `test_axis_long_name_is_sanitized_towards_chat_via_responses`.
> - Vers une cible **Chat sur P2** (`/v1/messages` → Chat) : **corrigé**.
>   `_sanitize_chat_tools` projette les noms (qui vivent sous `function.name`
>   côté Chat) vers la forme attendue par `sanitize_tool_names` et réutilise la
>   **même** logique — une seule source de vérité, pas de deuxième règle de
>   raccourcissement à maintenir. L'historique
>   (`assistant.tool_calls[].function.name`) et le `tool_choice` nommé suivent le
>   même rename, et le nom d'origine est **restauré au client**, non-stream
>   (`openai_to_anthropic`) **et** streaming (branche `tool_calls` du flux P2).
>   Couvert par `test_axis_long_name_is_sanitized_towards_chat_on_p2` et les
>   tests `test_a8_*`.
>
> **Ce que la correction a changé dans les tests de référence.** Le test de l'axe
> portait un `xfail(strict=True)` tant que le défaut était là : il est passé en
> **XPASS** dès le câblage, ce qui a **forcé** la levée du marqueur — le garde-fou
> a joué son rôle. Le golden `p2_tools_long_name_strict_schema.json` figeait
> explicitement le défaut (« nom long conservé tel quel côté Chat ») : il a été
> **régénéré**, et il est le **seul** des 46 goldens à changer (vérifié par
> empreinte avant/après).
>
> **Preuve sur le fil, en conditions réelles (proxy `:4000`) — diagnostic AVANT
> correction.** Le défaut n'a pas été déduit hors ligne : il a été **observé sur
> le fil**, et c'est cette observation qui a motivé le lot L4. Requête P2
> (`POST /v1/messages` → amont Chat, modèle
> `deepseek-v4-flash-free`) ; le dump de la requête réellement émise vers l'amont
> (`logs/free400_msg_aa3ac6d9fd40-31e.json`) contient :
>
> ```json
> {"model":"deepseek-v4-flash-free","messages":[…],
>  "tools":[{"type":"function","function":{"name":"aa…(80 car.)",
>            "parameters":{…,"additionalProperties":false}}}],
>  "tool_choice":"auto"}
> ```
>
> Soit **80 caractères, non sanitizé**, vers un vrai amont : c'était le défaut.
> Depuis le lot L4, cette même requête émet un nom **≤ 64** (raccourci
> déterministe) et transporte la map de restauration. À noter que
> `additionalProperties` avait été ajouté au schéma par `_normalize_tool_schema` —
> le proxy **traitait** donc les outils sans toucher au nom, ce qui rendait le
> défaut discret.
>
> **Contre-épreuve, même nom, autre jambe (avant correction).** Via l'API
> **Responses** (jambe free), le même nom de 80 caractères ressortait
> **sanitizé à 64** :
> `"name":"aaaa…(57)-86f336"`, soit `a`×57 + `-` + `sha1("a"×80)[:6]`. Digest
> recalculé : **`86f336`** — identique **au bit près** à la sortie de
> `sanitize_tool_names`. La sanitize **fonctionne donc en production** sur cette
> jambe, ce qui valide en réel le comportement verrouillé hors ligne par
> `test_axis_long_name_is_sanitized_towards_chat_via_responses`.
>
> **La question restée ouverte — « un amont rejette-t-il vraiment un nom > 64 ? » —
> a été mesurée. Réponse : NON, pas celui-ci.** Et la mesure n'a de valeur que
> faite sur le **seul chemin où elle est sans ambiguïté** : un modèle free dont
> l'endpoint n'est **pas** `/responses`.
>
> - **Pourquoi ce chemin précis.** La sanitize de la jambe free dépend de l'**URL**
>   de la jambe : `_is_responses = "/responses" in free_endpoint`
>   (`opencode.py:6183`). Si oui, les trois branches convertissent et sanitizent
>   (`_sanitize_native_responses_request`, `_anthropic_to_responses_request`,
>   `_chat_to_responses_request` — ce dernier appelle `sanitize_tool_names` en
>   `mapping.py:3568`). Si non, la branche `else` (`opencode.py:6197-6199`) recopie
>   simplement le corps : **passthrough nu**. Les modèles **muse/spark** prennent
>   l'endpoint `/responses` ; les autres prennent `free_base` =
>   `.../chat/completions` (`tests/test_free_discovery.py:13`).
> - **Sur ce chemin, aucune échappatoire.** Ni conversion, ni sanitize, ni map de
>   restauration : le nom du client part **verbatim** et celui de l'amont revient
>   **verbatim**. Un nom rendu long ne peut donc **pas** être un artefact de
>   restauration.
> - **Le résultat.** `mimo-v2.5-free`, nom d'outil de **72 caractères** : **`200`
>   et nom rendu IDENTIQUE (72 car.), 3/3 rondes**, chacune avec son **témoin** à
>   nom court (11 car.) vert dans la même ronde. Les deux autres modèles sondés
>   sont **exclus** comme non concluants — jambe morte : `deepseek-v4-flash-free`
>   → `503`, `nemotron-3-ultra-free` → `MissingSessionID`. Sans témoin vert, rien
>   n'est imputable.
> - **Corroboration historique.** Le seul cas connu où un nom > 64 est parti **non
>   sanitizé** vers un amont réel et a été **refusé** est
>   `logs/free400_msg_aa3ac6d9fd40-31e.json` (nom de 80 car.) : le corps d'erreur
>   est *« Upstream request failed: **Model is unavailable** »* — le refus ne
>   portait **pas** sur le nom.
>
> **Ce que la première rédaction de cette note surévaluait — corrigé ici.** Un
> premier jet a présenté « 9/9 en `200` sur P3 » et « 24/24 noms identiques » comme
> la preuve. C'était **trop large** : la plupart de ces essais passaient par une
> jambe free muse/spark en `/responses`, donc **sanitizée à l'aller et restaurée
> au retour** — exactement le piège déjà rencontré une fois dans cet audit (lire un
> `200` comme une acceptation d'un nom long). Ces mesures gardent leur valeur comme
> preuve que **le proxy se comporte correctement** ; elles ne disaient rien de la
> tolérance de l'amont. Seule la mesure sur `mimo-v2.5-free` en parle, et elle est
> sans échappatoire.
>
> **Lacune résiduelle, découverte à cette occasion et non corrigée.** Sur la jambe
> free à endpoint non-`/responses`, les noms d'outil ne sont **pas** sanitizés
> (`opencode.py:6197-6199`) : un client qui envoie un nom > 64 y part **tel quel**.
> C'est la même classe d'écart que A8, sur une autre branche. Elle n'a **aucun
> effet observable ici** — l'amont tolère, c'est précisément ce que la mesure
> ci-dessus établit — et elle est donc **déclarée comme écart de conformité
> résiduel**, pas comme panne. Toute correction éventuelle devrait réutiliser
> `sanitize_tool_names` sur cette branche, pour ne pas créer une seconde règle.
>
> **Conséquence, à assumer : la prémisse de A8 n'est pas vérifiée sur cet amont.**
> Le correctif reste justifié — 64 caractères est la limite du contrat OpenAI/Chat
> et d'autres amonts l'appliquent — mais il se lit comme une **mise en conformité
> et une portabilité**, pas comme la réparation d'une panne reproduite. Ce qui est
> acquis, et reste verrouillé hors ligne + mutation-testé (6 mutations, 6 tests qui
> mordent), c'est que le proxy **applique** désormais la limite vers Chat et
> **restaure** le nom d'origine au client.

## Effort et raisonnement — règle unique (décision produit)

`config/effort_policy.py` est **la** source unique : les 6 chemins y délèguent,
donc une même demande produit le même résultat quelle que soit la porte.

Ordre de priorité des champs d'entrée : `output_config.effort` → `effort` →
`reasoning_effort` → `reasoning.effort` → `thinking.budget_tokens` → `thinking.type`.

| Demande du client | Niveau retenu |
|---|---|
| `output_config.effort: <niveau>` (forme actuelle) | ce niveau, borné au plafond du modèle |
| `reasoning_effort` / `effort` / `reasoning.effort` | ce niveau, borné au plafond du modèle |
| `thinking.budget_tokens: N` | dérivé par `BUDGET_TO_LEVEL_TABLE` (≥16000→xhigh, ≥10000→high, ≥4000→medium, sinon low) |
| `thinking: {type:"adaptive"\|"enabled"}` **sans budget** | **le maximum du modèle** (`max_level_for_model`) |
| `thinking: {type:"disabled"}`, effort `none`/`disabled` | aucun raisonnement |
| rien du tout | aucun raisonnement (aucun défaut imposé) |

Le plafond par modèle vient de `config.yaml → thinking.effort_caps`
(matching plus long préfixe). Exemples : `deepseek-v4-flash` → `max`,
`muse-spark-1.3-contributor` → `xhigh`, `glm-5` / modèle inconnu → `high`.
Un niveau **explicite** n'est jamais relevé au maximum ; seul le cas
« raisonnement demandé sans niveau » vise le plafond.

## Mapping champs — réponse Chat → contrat V1

| OpenAI Chat | Anthropic V1 | Note |
|---|---|---|
| `choices[0].message.content` | bloc `text` | vide → `{"type":"text","text":""}` |
| `message.reasoning_content\|reasoning` | bloc `thinking` en tête | ordre §v1-response.md |
| `message.tool_calls[]` | blocs `tool_use` (arguments parsés, fallback `{}`) | id conservé si fourni |
| `finish_reason: length` | `stop_reason: max_tokens` | priorise sur tool_calls |
| tool_calls présents (sinon) | `stop_reason: tool_use` | |
| sinon | `stop_reason: end_turn` | |
| `usage.prompt_tokens/completion_tokens` | `input_tokens/output_tokens` | |
| `prompt_tokens_details.cached_tokens` | `cache_read_input_tokens` | `cache_creation=0` |

## Jambe free — parité de protocole (défaut mesuré, corrigé le 11/09/2026)

Le proxy route vers la « jambe free » quand le modèle payant routé a un équivalent
dans `free_model_map` (`_try_free_model_first`, `opencode.py:6075`). L'endpoint free
dépend du **modèle free** : `/responses` pour `muse-*`/`spark-*`, `/chat/completions`
pour tous les autres (`config/discovery.py:324-333`).

**Défaut (mesuré le 11/09 sur le proxy `:4000`)** : quand le modèle payant déclare
`protocol: anthropic` **et** que son équivalent free est un endpoint
`/chat/completions`, le corps Anthropic partait **verbatim** vers un endpoint Chat, et
la réponse Chat était rendue **verbatim** au client Anthropic — sous un **HTTP 200**.

Cas vivant : route `haiku` → `minimax-m2.5` (`protocol: anthropic`) →
`mimo-v2.5-free` (endpoint Chat). Conséquences mesurées :

- **system prompt perdu** — un endpoint Chat ne lit pas le champ `system` top-level
  Anthropic ;
- `tools[].input_schema` non traduit (un endpoint Chat attend
  `tools[].function.parameters`) ;
- réponse `{"choices":[…]}` (non-stream) ou chunks `chat.completion.chunk` (stream)
  servis à un client Anthropic : ni `content`, ni `stop_reason`, ni
  `usage.input_tokens`.

**Repère de contrôle** : la route `opus` (→ `kimi-k2.6`, `protocol: openai`) utilise
**le même** modèle free et le même endpoint, et fonctionnait — c'est donc la
déclaration de protocole du modèle **payant**, et non le modèle free, qui déclenchait
le défaut.

**Correctif** — l'aller et le retour sont convertis, non-stream **et** stream :

| Jambe | Aller (Anthropic → Chat) | Retour (Chat → Anthropic) |
|---|---|---|
| non-stream | `anthropic_to_openai` (`opencode.py:6205`) | `openai_to_anthropic` + restauration des noms d'outils (`opencode.py:6669`) |
| stream | `anthropic_to_openai` (`opencode.py:9190`) | `chat_sse_to_anthropic_events` (`app/protocol/chat_sse_to_anthropic.py`) |

Le convertisseur de flux est un module autonome, à état **par stream** (aucun global
mutable — cf. le bug documenté en `app/protocol/mapping.py:3720-3726`). Il reprend les
événements du chemin P2 déjà mesuré en réel : `message_start`, blocs `text`/`thinking`,
`tool_use` + `input_json_delta`, `message_delta`, `message_stop`.

**Périmètre** : les 7 sites appelant la jambe free avec `protocol="anthropic"`
(chemins P1, P4, P6) consomment tous la réponse comme de l'Anthropic ; le correctif les
aligne tous. Le chemin client Chat (P3) est inchangé — un témoin de non-régression le
verrouille.

**P4 stream — corrigé le 14/09/2026.** Le défaut était double, et le second volet
n'était pas visible dans le code seul : l'aller envoyait le corps Anthropic à un endpoint
Chat (`opencode.py:12742`), et le flux Chat revenait à un parseur qui n'exploite que des
événements Anthropic (`opencode.py:13026` — `if not line.startswith("data:"): continue`,
puis `ev["type"]`) : le client recevait **0 octet sous HTTP 200**. Corrigé en renvoyant le
**corps client** (déjà de forme Chat, `opencode.py:12533`) et en insérant la conversion
`Chat SSE → Anthropic SSE` avant le parseur, avec un état **frais par tentative**.

**Le test complice (classe A25)** : `test_free_model_subpath_p4_stream` stubait
`ANTHRO_SSE_LINES` — une forme que l'endpoint free **ne produit jamais** — et n'assertait
aucun contenu : il ne pouvait que passer. Réécrit avec `CHAT_CHUNK_LINES` et des assertions
de contenu, il a échoué immédiatement (`''`), en reproduisant exactement le défaut qu'il
aurait dû détecter.

**A27 — 500 au lieu de 503 (mesuré le 14/09/2026)** : toutes les clés Anthropic en pause ⇒
`_get_auth_headers("anthropic")` rend `None` **sans** lever `AllKeysPausedError` ; la jambe
free échoue (429) ; le repli payant propage ce `None` ; `opencode.py:12647` faisait
`None.get(...)` ⇒ **HTTP 500** sur P4 non-stream, là où la branche streaming rend un 503
propre (`opencode.py:12560`). Corrigé par un garde 503. Le motif jumeau dans le second
handler **reste non corrigé**.

**Validation en réel (proxy `:4000`, 14/09/2026)** — même sonde avant/après redémarrage :

| Chemin (`model: haiku`) | Avant | Après |
|---|---|---|
| P1 non-stream `/v1/messages` | 500 | 200 Anthropic, `content='Bonjour'` |
| P1 stream `/v1/messages` | 200, 16 007 o de Chat non converti | 200 Anthropic (`message_start` + `content_block_delta`) |
| P4 non-stream `/v1/chat/completions` | 500 (A27) | 200, `choices[0].message.content='bonjour'` |
| P4 stream `/v1/chat/completions` | 200, **0 octet** | 200, 7 559 o de Chat, texte reçu |
| Témoin `opus` (`protocol: openai`) | 200 Anthropic | 200 Anthropic |

Sur P4, la sortie `chat.completion.chunk` est le format **correct** (le client a appelé
`/v1/chat/completions`) ; sur P1, ce même format était le symptôme du défaut.

**Verrous** : `tests/test_free_leg_protocol_parity.py` (5 cas, non-stream),
`tests/test_chat_sse_to_anthropic.py` (48 cas, convertisseur),
`tests/test_e2e_protocol_matrix.py::test_free_model_subpath_p1_stream_converts_chat_to_anthropic`
et `::test_free_model_subpath_p4_stream` (bout en bout stream, P1 et P4). Mutations : **6/6
mordent** (2 non-stream, 2 stream P1, 2 P4), fichiers restaurés à l'identique (sha256).

> ⚠️ **Correction (15/09/2026)** — cette liste annonçait « 7/7 mordent … 1 A27 » et citait un
> test `::test_p4_nonstream_sans_cle_anthropic_est_503_pas_500` qui **n'existe plus**
> (remplacé par deux témoins). La mutation d'A27 **ne mord pas** : dépouillement seul, garde
> seul, ou retour **complet** au code d'origine laissent les témoins verts, alors qu'une
> sonde prouve que le site fautif est atteint. Voir plan §12.1.

## Corrections du 15/09/2026 — vérité par axe

| Axe | État avant | État après | Preuve |
|---|---|---|---|
| **A27** — en-têtes `None` de la jambe free écrasant des en-têtes valides | HTTP **500** nu (mesuré en production) | 14 sites de dépouillement homogènes + garde 503 en défense en profondeur | mesure live ; **témoins non mordants**, déclaré (plan §12.1) |
| **P4** — bloc `thinking` à signature **locale forgée** vers l'amont | bloc signé par le proxy parti en amont (non-stream **et** stream) | `strip_synthetic_thinking` appelé au passage unique des deux jambes | `tests/test_p4_synthetic_thinking.py` ; mutations **2/2 mordent**, témoin P1 vert |
| **P6** — signature forgée **et** garde orphelin contourné | `anthro_body` construit **avant** la garde ; bloc `pass` déguisé en garde | garde appliquée à l'objet réellement envoyé (resynchronisation depuis le corps **filtré**) + strip sur P6 | `tests/test_p6_orphan_and_thinking.py` ; mutation **mord** ; le strip est une défense en profondeur, **non prouvé nécessaire** (déclaré) |
| **`supports_cache_control`** | 2 sites sur 4 sans garde (`mapping.py:1363`, `1390`) | garde sur les quatre sites | témoin **par site** ; mutations **2/2 mordent** avec discrimination |
| **`cache_control` → `prompt_cache_breakpoint`** | no-op, mais une docstring affirmait l'émission | décision tranchée, docstring corrigée, comportement verrouillé | `tests/test_cache_control_support.py` |
| **`thinking` racine → `reasoning`** | déclaré « jamais copié » | **déclaration réfutée** : converti, et le budget pilote l'effort (256→low, 8000→medium, 32000→high) | 8 cas, `tests/test_thinking_effort_mapping.py` |
| **A8 sur P3** — noms d'outils > 64 car. non raccourcis à l'aller, non restaurés au retour | nom de 102 car. parti **verbatim** vers un amont plafonné à 64 ; le retour rendait le nom raccourci (le non-stream rendait même les **octets amont verbatim**, le stream réémettait la **ligne brute**) | `sanitize_chat_tool_names` / `restore_chat_response_tool_names` câblés aux deux retours | `tests/test_p3_tool_names.py` (8 cas) ; 5 échecs sans le correctif, **5 mutations mordent** ; 3 aides étaient du **code mort** |
| **A8 sur P5/P6** — `name_map` absent des convertisseurs Responses | retour P5 : le client recevait le nom **raccourci** | `anthropic_to_openai_responses` / `openai_chat_to_responses` prennent `name_map` (symétrie des jumelles), carte extraite et propagée aux **7 sites** | `tests/test_trou3_a8_responses_restore.py` ; mutations A et C **mordent**, **B ne mord pas** (point inerte, déclaré) ; mesure : l'aller P5 raccourcit bien, l'aller **P6 envoie verbatim** (trou distinct, ouvert) |
| **Jambe free — effort** | l'`reasoning_effort` décidé pour le modèle **payant** partait tel quel vers le modèle **free** | recalculé avec le modèle free via la source unique `config.effort_policy` (ne peut que rabaisser) | `tests/test_free_leg_effort_recompute.py` ; mutation **mord** (`'max'` → `'high'`) ; prouvé sur le chemin Chat seul, **inerte** sur `/responses` (clamp aval), déclaré |
| **Jambe free `/responses` en flux SSE** | un **200** du modèle free était lu comme « vide » (le corps SSE n'est pas du JSON) → repli payant → **503** client alors que le free avait répondu | corps collecté (`.text`, à défaut lignes asynchrones) et objet reconstruit depuis `response.completed` | mesuré en E2E ; **reste ouvert** : le repli rend encore du **JSON à un client streaming** (`is_stream` non consulté, `opencode.py:10051-10067`) — `xfail` déclaré |
| **A11 — P5/P6 bufferisés** | TTFB = durée totale de génération | **non corrigé** | mesuré **en live** : TTFB/total = **1,00** sur P6 (26,11 s) contre 0,86 sur P1 — réserve : les deux passent par la jambe free, le chemin payant est inmesurable ici. Exige un convertisseur SSE→SSE Responses inexistant |
| **P2 → `/responses`** (trou 6) | chemin absent de la matrice : **aucune couverture** | 2 témoins (1 vert, 1 `xfail`), et **2 défauts neufs** dont 1 corrigé : un **200 du modèle free lu comme « vide »** (le corps SSE n'est pas du JSON) partait en repli payant, soit un **503 client** alors que le free avait répondu | mesuré en E2E ; le **cadrage SSE** du repli reste ouvert (le handler rend du JSON sans consulter `is_stream`) — déclaré |
| **Jambe free de P6** (trou 13) | aucun test | 3 témoins, et un défaut révélé au passage : **503 annonçant une jambe free qui n'avait pas lieu** (corrigé) | 4 mutations en worktree jetable, **une déclarée non mordante** |
| **A27 — garde 503 non atteinte** (trou 1, suite) | garde en « défense en profondeur » réputée vérifiée | **non atteinte** : `a_headers` y est **toujours un `dict`** (sonde), et ses deux conditions sont **mutuellement exclusives** ; restaurer le défaut aux 14 sites ne casse aucun test | mesuré ; le témoin existant est **complice** — trou **ouvert**, remédiation identifiée côté correctif (plan §12.17) |
| **A11 — P5/P6 bufferisés** (trou 9, suite) | correctif borné (ping initial) envisagé | **impossible en l'état** : mesuré TTFB/total = **1,00** ; `StreamingResponse` construit à t = 625 ms, **après** la libération de l'amont à 610 ms — le handler n'atteint jamais son `return` | refus justifié (un ping tardif **masquerait** le watchdog TTFB) ; défaut neuf : `resp.json()` **sans `await`** (plan §12.17) |
| **P3 → `/responses`** | aucun golden | 48ᵉ golden ; perte de `stop`/`stop_sequences`/`stream_options` **mesurée** et figée | 47/47 goldens préexistants inchangés ; mutations **3/3 mordent** |
