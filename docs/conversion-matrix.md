# Matrice de conversion Anthropic ↔ OpenAI

> [plan v10 §11.3/§10.1 Lot D] Dérivée du CODE réel (`protocol_mapping.py`,
> lignes vérifiées le 2026-08-24) et des références officielles :
> - Messages API : <https://platform.claude.com/docs/en/api/messages>
> - Chat Completions : <https://platform.openai.com/docs/api-reference/chat>
> - Responses API : <https://platform.openai.com/docs/api-reference/responses>

## Fonctions (source de vérité : `protocol_mapping.py`)

| Fonction | Sens | Verrou golden |
|---|---|---|
| `anthropic_to_openai(body, model)` | requête `/v1/messages` → Chat Completions upstream | `req_simple`, `req_tools` |
| `openai_to_anthropic(resp, model)` | réponse Chat → contrat V1 (`/v1/messages` free/payé-B) | `resp_text`, `resp_tool_calls`, `resp_reasoning` |
| `openai_to_anthropic_request(oai_body)` | requête client OpenAI → upstream Anthropic | `oai_request_to_anthro` |
| `anthropic_to_openai_response(anthro, model)` | réponse Anthropic → client OpenAI | `anthro_response_to_openai` |
| `openai_responses_to_anthropic(body)` | requête `/v1/responses` → upstream Anthropic | `responses_api_entry` |
| `anthropic_to_openai_responses(anthro, model)` | réponse Anthropic → format Responses | — |
| `openai_chat_to_responses(chat_resp, model)` | réponse Chat → format Responses | — |
| `_responses_to_chat_response / _to_anthropic_response` | réponses internes Responses ↔ cibles | — |
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
| `max_tokens` | `max_tokens` | |
| `stop_sequences` | `stop` | |
| `temperature`, `top_p` | identiques | |
| block `image` | **PERDU silencieusement** | bug §14.1.6 — à corriger |

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
