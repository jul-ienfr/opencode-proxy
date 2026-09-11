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
| `_chat_to_responses_request(chat)` | requête client Chat → body Responses (P5 aller, sanitize natif) | `p5_request_chat_to_responses_media`, `p5_request_native_responses_passthrough` |
| `_responses_to_chat_response(resp, model, name_map)` | réponse Responses interne → cible Chat | `responses_to_chat_response_cache_usage`, `responses_to_chat_restore_tool_name` |
| `_responses_to_anthropic_response(resp, model, name_map)` | réponse Responses interne → cible Anthropic | `responses_to_anthropic_response_tool_use` |
| `sanitize_tool_names` | noms d'outils > 64 car. → `name[:57]+sha1[:6]` + map de restauration | `sanitize_tool_names_long_and_server` |
| `strip_synthetic_thinking(body)` | historique Anthropic : retire les blocs `thinking` à signature locale | `strip_synthetic_thinking`, `multiturn_thinking_strip` |
| `_responses_sse_to_chat_deltas(raw_line)` | 1 ligne SSE Responses (sans `data:`) → delta chat, `[DONE]`→None | `sse_deltas` |

## Mapping champs — requête Anthropic → Chat Completions

| Anthropic | OpenAI Chat | Note |
|---|---|---|
| `system: str\|[{text}]` | message `role=system` (+`cache_control ephemeral`) | pas de rôle system côté Anthropic |
| `messages[].content: str` | `content: str` direct | |
| block `text` | `content` concaténé | |
| assistant block `tool_use {id,name,input}` | `tool_calls[] {id,type:"function",function:{name,arguments:json}}` | arguments = JSON compacté |
| user block `tool_result {tool_use_id,content}` | message `role=tool {tool_call_id,content}` | bufferisé puis émis après l'assistant |
| `tools[] {name,description,input_schema}` | `tools[] {type:"function",function:{name,description,parameters}}` | |
| `tool_choice auto/any/tool{name}/none` | `"auto"/"required"/{"type":"function",...}/"none"` | |
| nom d'outil **≤ 200 car.** (limite Anthropic) | nom **≤ 64 car.** (limite Chat/OpenAI) | ✅ **corrigé (lot L4)** — raccourci puis restauré, voir ci-dessous |
| `max_tokens` | `max_tokens` | |
| `stop_sequences` | `stop` | |
| `temperature`, `top_p` | identiques | |
| `thinking{type}` / `output_config.effort` | `reasoning_effort` | voir « Effort » ci-dessous |

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
> **Ce qui reste non prouvé** (et ne l'était pas davantage avant la correction) :
> qu'un amont **rejette** effectivement un nom dépassant 64 caractères. Les
> `400`/`503` observés ont d'autres causes (session free manquante,
> `DataPolicyError` d'opt-in workspace, solde de crédits à zéro). Cette
> incertitude porte sur la **nécessité** de la limite, pas sur le fait que le
> proxy l'applique désormais : le correctif est verrouillé hors ligne et
> mutation-testé (6 mutations, 6 tests qui mordent). Le volet « rejet réel par
> l'amont » reste donc **déclaré comme risque**, pas reproduit.

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
