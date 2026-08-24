# Compatibilité clients

> [plan v10 §11.6 Lot D] Clients consommant le proxy et contrats garantis.

## Endpoints servis

| Endpoint | Format parlé | Clients typiques |
|---|---|---|
| `POST /v1/messages` | Anthropic Messages (+`stream: true`) | **Claude Code**, SDK `anthropic`, tout client Anthropic |
| `POST /v1/chat/completions` | OpenAI Chat (+SSE) | SDK `openai`, LiteLLM, outils génériques |
| `POST /v1/responses` | OpenAI Responses | clients Responses API |
| `GET /health`, `/api/*` | dashboard/ops | navigateur LAN (trust zéro-friction §14.0.3) |

## Garanties par client

- **Claude Code** (`ANTHROPIC_BASE_URL` → proxy) : la réponse respecte le
  contrat V1 (`docs/v1-response.md`) — enveloppe `msg_*`, ordre
  thinking→text→tool_use, stop_reason mapping, usage cache_*. Les tool_use /
  tool_result multi-tours passent (verrouillé par `req_tools.json`).
- **SDK OpenAI** : `anthropic_to_openai_response` sert des choix Chat
  standard ; `reasoning_content` exposé pour les modèles thinking.
- **Clients Responses** : conversion via `openai_responses_to_anthropic` ;
  streaming limité au faux-streaming actuel (§14.1.13 connu).

## Authentification (décision v9)

- Dashboard : zéro friction en loopback + LAN (`dashboard_trust.lan_cidrs`),
  aucune saisie jamais demandée ; anti-CSRF même-host sur les mutations ;
  token opt-in (`require_token`) pour une exposition WAN éventuelle.
- `/v1/*` : `client_auth.mode: none` par défaut (bind 0.0.0.0 voulu) ;
  `lan`/`key` disponibles en opt-in.

## Limites connues (audit §14, ne pas « documenter comme normal »)

- images Anthropic perdues vers upstream OpenAI (§14.1.6)
- erreurs de stream /v1/responses non distinguables d'un succès (§14.1.14)
