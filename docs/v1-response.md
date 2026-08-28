# Contrat V1 — réponse `/v1/messages`

> [plan v10 §11.5] Ce document fait foi : toute modification du code de
> conversion qui change un invariant ci-dessous doit (1) mettre à jour ce
> fichier ET (2) régénérer/réviser les golden fixtures concernées
> (`docs/v1-response-golden/`), sinon `tests/test_docs_drift.py` et
> `tests/test_conversion_golden.py` bloquent le merge.

## Invariants de la réponse V1 (forme Anthropic Messages)

Quelle que soit la branche empruntée (Anthropic direct payant, OpenAI-compatible
payant, station free), la réponse servie au client respecte :

1. **Enveloppe** : `{id: "msg_<hex24>", type: "message", role: "assistant",
   model: <modèle demandé>, content: [...], stop_reason, stop_sequence,
   usage}`.
2. **Ordre des blocs content** : `thinking`/`redacted_thinking` d'abord,
   puis `text`, puis `tool_use`. Un contenu vide produit un bloc unique
   `{"type": "text", "text": ""}` (jamais `content: []`).
3. **stop_reason** : `tool_use` si tool_calls présents ; sinon `max_tokens`
   si finish_reason=length ; sinon `end_turn`. `length` PRIORISE sur
   `tool_calls` (comportement figé par `resp_tool_calls.json`).
4. **usage** : `input_tokens ← prompt_tokens`,
   `output_tokens ← completion_tokens`,
   `cache_read_input_tokens ← prompt_tokens_details.cached_tokens`,
   `cache_creation_input_tokens = 0`.
5. **Streaming SSE** : événements Anthropic (`message_start`,
   `content_block_delta`, …) terminés par `[DONE]` côté traduction interne ;
   une erreur en stream reste indistinguable d'un succès côté client tant que
   l'audit §14.1.14 n'est pas corrigé (connu).
6. **Ids non-déterministes** : `msg_*`, `toolu_*` fallback, `chatcmpl-*` et
   `created` sont générés à chaque réponse — les goldens les normalisent.

## Golden fixtures

| Fixture | Fonction verrouillée | Sens |
|---|---|---|
| `req_simple.json` | `anthropic_to_openai` | requête client → upstream |
| `req_tools.json` | `anthropic_to_openai` | tools + tool_result multi-turn |
| `resp_text.json` | `openai_to_anthropic` | texte + usage + cache_read |
| `resp_tool_calls.json` | `openai_to_anthropic` | stop_reason mapping |
| `resp_reasoning.json` | `openai_to_anthropic` | reasoning → bloc thinking |
| `oai_request_to_anthro.json` | `openai_to_anthropic_request` | client OpenAI → upstream |
| `anthro_response_to_openai.json` | `anthropic_to_openai_response` | /v1/chat/completions |
| `responses_api_entry.json` | `openai_responses_to_anthropic` | /v1/responses entrant |
| `sse_deltas.json` | `_responses_sse_to_chat_deltas` | streaming /v1/responses |

Régénération (uniquement si changement VOLONTAIRE et relu) :
`python scripts/gen_golden_fixtures.py`

## Écarts connus vs spec officielle (à corriger, ne PAS « figer »)

- **§14.1.6** : blocs `image` Anthropic → OpenAI perdus silencieusement.
- **§14.1.13/14.1.14** : `/v1/responses` faux-streaming + erreurs sans event.
- **§14.3.25** : body stocké muté après capture sur /v1/responses.
