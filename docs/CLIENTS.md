# Clients supportés — 023-client-usable-egress

Ce document décrit la couche de compatibilité client : quel protocole chaque
client parle, comment il est converti vers le wire officiel, et comment en
ajouter un sans toucher au code.

## 1. Vocabulaire

| Couche | Nom | Contenu |
|---|---|---|
| L0 | Empreinte TLS | ClientHello, JA3/JA4, ALPN, HTTP/2 (`curl_cffi`) |
| L1 | En-têtes | UA, `x-opencode-*`, `b3`/`traceparent`, quartet de session |
| L2 | Parité wire | 6 clés de corps, 13 outils, `max_completion_tokens`, `stream_options` |
| L3 | Identité | session/trace/`project`/`prompt_cache_key` par conversation et par IP |
| L4 | Protocole | conversion Anthropic Messages ↔ OpenAI Chat ↔ Responses |
| L5 | Outils | autorisés, interdits, alias, shims, mapping retour |
| L6 | Capacités | streaming, tools, raisonnement, vision, cache, `count_tokens` |

« Impersonation » ne désigne que L0-L2. L'ensemble s'appelle **émulation
client** (L0-L3) et **couche de compatibilité client** (L4-L6).

## 2. Matrice

| Client | Famille | Endpoint | Auth | Stream | Tools | Spécificités |
|---|---|---|---|---|---|---|
| Claude Code | `anthropic_messages` | `/v1/messages`, `/v1/messages/count_tokens` | `x-api-key` + `anthropic-version` | SSE `message_start` → `message_stop` | `input_schema`, `tool_use` | thinking, `cache_control`, betas |
| Codex | `openai_responses` | `/v1/responses` | Bearer | `response.created` → `response.completed` | function tools | `reasoning.effort`, `prompt_cache_key` |
| Kilo Code | `openai_chat` | `/v1/chat/completions` | Bearer | `data:` / `[DONE]` | function tools | extension VS Code |
| Hermes Agent | `openai_chat` | `/v1/chat/completions` | Bearer | `data:` / `[DONE]` | function tools | prompt système long |
| DeepSeek | `openai_chat` | `/v1/chat/completions` | Bearer | `data:` / `[DONE]` | function tools | `reasoning_content` obligatoire |
| Aider | `openai_chat` | `/v1/chat/completions` | Bearer | souvent non-stream | aucun | éditions par diff |
| SDK OpenAI | `openai_chat` / `openai_responses` | selon | Bearer | SSE | function tools | en-têtes `x-stainless-*` tolérés |
| Inconnu | `custom` | `/v1/chat/completions` | Bearer ou `x-api-key` | oui | oui | 422 local si non convertible |

La source de vérité est `free/clients/registry.json`.

## 3. Politique d'outils

`free/clients/tool_policy.json` :

- `official_grid` : les 13 noms exigés par le gate ;
- `forbidden` : jamais envoyés (`subagent_fork`) ;
- `aliases` : `Read → read`, `Bash → shell`, `Task → subagent`, etc. ;
- `shims` : `todowrite → text_plan`, `subagent_fork → reject` ;
- `client_extras_max` : nombre maximal d'outils client ajoutés ;
- `unknown_tool` : `text_heal` (rendu en texte) ou `drop`.

Règles :

1. un outil client au nom officiel garde **son** schéma exécutable ;
2. un outil officiel absent reçoit un placeholder « ne pas appeler » ;
3. un outil interdit ne part jamais ;
4. au retour, tout appel est re-mappé vers le nom client d'origine ;
5. un appel non déclaré n'est jamais relayé tel quel.

## 4. Ajouter un client

1. Ajouter une entrée dans `free/clients/registry.json` :

```json
"mon-client": {
  "family": "openai_chat",
  "endpoints": ["/v1/chat/completions"],
  "auth": {"bearer": true},
  "capabilities": {"stream": true, "tools": true, "max_context": 128000},
  "tool_policy": "openai"
}
```

2. Déposer un exemple de requête dans
   `free/clients/fixtures/mon-client/request.json`.
3. Ajouter le client à la liste `CLIENTS` de
   `tests/contract/test_client_families.py`.
4. Lancer les contrats :

```powershell
python -m pytest tests/contract -q
```

Aucun code Python n'est nécessaire : la famille fournit la conversion, le
registre fournit les capacités.

## 5. Validation live

```powershell
python scripts/client_smoke.py --client claude-code --stream
python scripts/client_smoke.py --client codex --stream
python scripts/client_smoke.py --client kilo-code --stream
python scripts/client_smoke.py --client hermes-agent --stream
python scripts/client_smoke.py --client deepseek
python scripts/client_smoke.py --client aider
```

Attendu : 3/3 `200` avec contenu par client, sur une IP prouvée.

## 6. Règle d'IP

Une IP ne porte qu'un seul format (constitution XVIII). Un A/B de wire exige
**une IP neuve par variante** :

```powershell
python scripts/wire_ab.py --station-official 1 --station-converted 3 --samples 20
```

Le script refuse deux variantes sur la même station. Le rapport est écrit dans
`docs/evidence/wire-ab-*.json`.

## 7. Erreurs

| Situation | Réponse |
|---|---|
| capacité absente | `503 capacity_exhausted` avec `reset_at` |
| conversion impossible | `422 wire_incompatible` avec le champ fautif |
| corps trop gros | `413` |
| 429 quota amont | `429` avec `Retry-After` réel |
| 5xx/timeout amont | `502`/`504` traduit |
