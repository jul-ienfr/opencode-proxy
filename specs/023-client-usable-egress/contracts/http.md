# Contrats HTTP — 023-client-usable-egress

Deux familles d'endpoints : ceux exposés aux clients (formats natifs) et ceux
exposés à l'opérateur (état et pilotage). Tous les contrats sont testés dans
`tests/contract/`.

## 1. Endpoints clients

### 1.1 Anthropic Messages

`POST /v1/messages`

En-têtes : `x-api-key`, `anthropic-version`, `anthropic-beta` (optionnel),
`content-type: application/json`.

Corps (extrait) :

```json
{
  "model": "deepseek-v4.1-flash",
  "max_tokens": 1024,
  "system": [{"type": "text", "text": "..."}],
  "messages": [{"role": "user", "content": "..."}],
  "tools": [{"name": "Read", "description": "...", "input_schema": {"type": "object"}}],
  "stream": true
}
```

Réponses :

- `200` JSON ou SSE : `message_start`, `content_block_start`,
  `content_block_delta`, `content_block_stop`, `message_delta`, `message_stop`.
- `200` avec `tool_use` : `content_block_start` type `tool_use`, nom re-mappé
  vers le nom client d'origine, `id` préservé.
- `422 wire_incompatible` : capacité absente ou conversion impossible ; aucun
  egress.
- `429` : uniquement si l'amont a réellement répondu 429 ; corps traduit,
  `Retry-After` conservé.
- `503 capacity_exhausted` : aucune capacité locale ; corps
  `{"error":{"type":"capacity_exhausted","reset_at":"..."}}`.

`POST /v1/messages/count_tokens` : `200 {"input_tokens": <int>}` ; jamais
d'egress si le comptage est local.

### 1.2 OpenAI Chat Completions

`POST /v1/chat/completions`

En-têtes : `Authorization: Bearer <client-key>`, `content-type`.

Corps (extrait) :

```json
{
  "model": "deepseek-v4.1-flash",
  "messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}],
  "tools": [{"type": "function", "function": {"name": "read", "parameters": {}}}],
  "stream": true,
  "stream_options": {"include_usage": true}
}
```

Réponses :

- `200` JSON : `choices[].message`, `tool_calls` (noms re-mappés),
  `finish_reason`, `usage`.
- `200` SSE : chunks `data: {...}`, `[DONE]`, `usage` final si demandé.
- erreurs : mêmes règles que 1.1, traduites au format OpenAI
  (`{"error":{"type":"...","message":"..."}}`).

### 1.3 OpenAI Responses

`POST /v1/responses`

Corps (extrait) : `input` (liste d'items), `tools` (function), `reasoning`
(`{"effort":"..."}`), `prompt_cache_key`, `stream`.

Réponses : SSE `response.created`, `response.output_item.*`,
`response.completed` (avec `usage`) ou `response.failed`.

### 1.4 Règles communes

- Aucun 429 synthétique : un 429 n'existe que si l'amont l'a émis.
- Aucun corps non conforme n'atteint un bucket `proven`/`serving`.
- Les clés d'authentification client ne sont jamais réémises vers l'amont.
- Le streaming n'est jamais mis en tampon.

## 2. Endpoints opérateur

### 2.1 `GET /api/egress/state`

```json
{
  "providers": {
    "nordvpn": {"enabled": true, "buckets": {"proven": 2, "serving": 1, "spent": 3, "burned": 0}},
    "warp":    {"enabled": true, "buckets": {"proven": 4, "serving": 2, "spent": 1, "burned": 0}},
    "proxies": {"enabled": true, "buckets": {"proven": 1, "serving": 0, "spent": 0, "burned": 0}},
    "direct":  {"enabled": false, "buckets": {"proven": 0, "serving": 0, "spent": 0, "burned": 0}}
  },
  "client_reserve": 3,
  "wire_fp": "…",
  "updated_at": "2026-11-17T22:00:00Z"
}
```

### 2.2 `GET /api/egress/config` et `POST /api/egress/config`

Lit et écrit `egress.providers.*`, `egress.strategy`, `clients.*`. Toute
écriture est journalisée avec l'auteur et l'effet attendu ; la désactivation
prend effet en < 60 s.

### 2.3 `GET /api/egress/incidents?since=…`

Liste les libérations, rotations, burns et mismatches avec `reason`,
`evidence_ref`, `provider`, `station`, `ip`, `created_at`. Une libération sans
`evidence_ref` est un bug et échoue le test de contrat.

### 2.4 `GET /api/egress/429?since=…`

Liste les 429 avec `class` (`format|quota|session|upstream`), `ip`, `provider`,
`wire_fp`, `retry_after`, `reset_at`, corps amont tronqué. Un 429 sans classe
échoue le test de contrat.

## 3. Codes et mapping d'erreurs

| Situation | Client Anthropic | Client OpenAI |
|---|---|---|
| capacité absente | `503 capacity_exhausted` + `reset_at` | idem, format `error` |
| conversion impossible | `422 wire_incompatible` | idem |
| corps trop gros | `413` | `413` |
| 429 quota amont | `429` + `Retry-After` | idem |
| 401/403 amont | `401`/`403` traduit | idem |
| 5xx/timeout amont | `502`/`504` traduit | idem |
| outil non déclaré | rendu texte, `stop_reason=end_turn` | `finish_reason=stop` |
