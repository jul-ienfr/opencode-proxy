# Contrat — Wire free 2.2.5

**Référence**: `free/captures/2.2.5.json`, `free/official_payload.json`,
`tests/test_official_wire_e2e.py`.  
**Portée**: une requête `POST /inference/openai/v1/chat/completions` émise par
le proxy en mode free.

## En-têtes

Ordre exact (16), vérifié end-to-end :

```
Authorization: Bearer public
Content-Type: application/json
User-Agent: <UA du profil>
b3: <trace>-<span>-1-<parent>
traceparent: 00-<trace>-<span>-01
x-opencode-client: cli
x-opencode-project: <40 hex ou global>
x-opencode-session: ses_<12 hex><14 base62>
x-opencode-session-id: ses_<12 hex><14 base62>
x-session-affinity: ses_<12 hex><14 base62>
x-session-id: ses_<12 hex><14 base62>
Connection: keep-alive
Accept: */*
Host: opencode.ai
Accept-Encoding: gzip, deflate, br, zstd
Content-Length: <n>
```

Invariants :

- `Authorization` est **toujours** `Bearer public` ; aucune clé client ne part.
- Pas de `x-opencode-org-id` (free anonyme), pas de `x-opencode-request`
  (absent de la capture 2.2.5).
- `x-opencode-session-id`, `x-session-affinity`, `x-session-id` portent la
  **même** valeur que `x-opencode-session`.
- L'ordre est imposé par `_OPENCODE_HEADER_ORDER_225` ; libcurl ajoute
  `Connection` en dernier, écart structurel connu et accepté.

## UA

| Source | Valeur |
|---|---|
| Capture 2.2.5 | `opencode/latest/2.2.5/cli` |
| Vault actuel | `opencode/1.19.5 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4` |
| Gate | ne lit que la forme `opencode/…/cli` |

**Divergence ouverte `T-401`** : le corps et l'ordre sont 2.2.5, l'UA est
1.19.5. Ne pas changer l'UA sans re-capture complète.

## Corps

Clés attendues, dans l'ordre 2.2.5 :

```
model, messages, tools, stream, stream_options, max_completion_tokens
```

- `stream: true` **toujours** (le client non-stream est replié côté proxy).
- `stream_options: {"include_usage": true}`.
- `max_completion_tokens` ≤ 32 000 ; `max_tokens` est renommé.
- `tool_choice` absent ; `none` retiré.
- `tools` : 13 outils officiels + extras client (max 9), placeholders avec
  `properties` non vide.

## Gate invariants

1. Au moins un message système porte `<env>…Working directory:…</env>`.
2. Le premier `<env>` est valide (voir `env-guard.md`).
3. Un outil nommé exactement `read` est présent.
4. `stream: true`.
5. Les 13 outils officiels ont une `description` string et un schéma objet.

## Preuve

- `tests/test_official_wire_e2e.py` : ordre 16/16.
- `tests/test_free_body_conformite.py` : clés, outils, `<env>`, `tool_choice`.
- `specs/003-official-client-parity/measurements.md` : clean-room.
