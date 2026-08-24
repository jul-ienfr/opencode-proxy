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

## Modèles gratuits (stations VPN)

Upstream = endpoint OpenAI-compatible (`protocol: openai`). Le client parle
toujours Anthropic (`/v1/messages`) ; la conversion est verrouillée par les
goldens §v1-response.md. Aliases actuels (`free_model_map`) :

- `deepseek-v4-flash` → `deepseek-v4-flash-free`
- `glm-5.1`, `kimi-k2.6`, `minimax-m2.5` → `mimo-v2.5-free`
- `mimo-v2.5` → `mimo-v2.5-free`
- `qwen3.7-max` → `mimo-v2.5-free`
- `hy3` → `hy3-free`
- `muse-spark-1.2-contributor` → `muse-spark-1.2-contributor-free`

Modèles free directs : `x-preview-f-free`, `ox-alpha-free`,
`nemotron-3-ultra-free`, `nemotron-3.5-lightning-free`,
`deepseek-v4-flash-vision-exp`, `longcat-2.0`.

## Clés de config surveillées (drift)

Voir `docs/_drift_manifest.json` — toute clé listée doit exister dans
`config.yaml` ET être mentionnée dans la doc associée ; l'inverse n'est pas
bloquant tant que le manifeste ne la référence pas.
