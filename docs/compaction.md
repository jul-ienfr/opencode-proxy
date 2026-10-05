# Compactage — voie free / voie payante (référence proxy)

> Source de vérité code : `app/compaction/` (classify, plan, router, transport,
> shapes, summarizer, react, truncate) + délégués fins `opencode.py`
> (`_has_free_leg`, `_compaction_is_free_class`, `_compaction_summarizer_plan[_full]`,
> `_compaction_summarizer_headers/request/normalize`). Config live :
> `config.yaml:server_compaction` via `config/settings.py:get_server_compaction`.

## 1. Principe

**Le compactage suit la même voie que la conversation.** Conversation servie en
free → résumé en free (même modèle free résolu, même endpoint free, mêmes
stations/VPN/hedge/geo/identités, `Bearer public`, jamais de clé payante).
Conversation payante → résumé payant (mêmes clés, même endpoint `go`, même
tunnel geo si `require_vpn`). Jamais de bascule silencieuse. Échec = fail-open.

## 2. Entrées client (3 portes)

| Porte | Shapes détectées (`is_compaction_shape`, `is_native_compaction`) |
|---|---|
| `POST /v1/messages` | officielle (messages 100% user, sans tools/system, ≥ `min_chars_implicit`), marqueur `<conversation-checkpoint>` / `context compaction` (tous rôles, sans borne, sans tools), natif Anthropic (`context_management.edits[].type~compact`, `{"compaction":{"type":"summarize"}}`, bloc `{"type":"compaction"}` rejoué) |
| `POST /v1/chat/completions` | user-only, marqueur Hermes `[CONTEXT COMPACTION – REFERENCE ONLY]` + `tools=[]` + `model=deepseek-v4-flash`, pas de natif server-side en Chat |
| `POST /v1/responses` | `input` user-only, natif OpenAI (`context_management.compact_threshold`, item `compaction`/`compaction_trigger` opaque, `POST /responses/compact` standalone, chaînage `previous_response_id` ou input-array, `store=false` ZDR) |

Tout compactage détecté → exclusion response-cache (`cache_key=None`), relais
intact des 429/401/403 (jamais de 503), log `[compaction] … native=0/1`.

## 3. Sorties provider (selon protocole routé)

| Destination | Aller | Retour |
|---|---|---|
| Anthropic (`protocol:anthropic`, `go/messages`) | passthrough verbatim + `strip_synthetic_thinking` seul ; forward `context_management`, beta `compact-*`, blocs `compaction` ; `pause_after_compaction` → `stop_reason=compaction` | passthrough ; bloc `compaction` en 1 seul delta ; `usage.iterations=[compaction,message]` relayé |
| OpenAI Chat (`protocol:openai`, `go/chat` ou `free/chat`) | entrée Anthropic → `anthropic_to_openai` + `sanitize_tool_names` (64) + `_free_wire_body` côté free ; entrée Chat → remap `model` + `ensure_min_tokens` + effort recalculé plafond free ; résumeur : `build_summarizer_body` (`messages`, `stream:false`, cap 4096, sans tools/system/thinking) | client Anthropic → `openai_to_anthropic` + restore noms ; client Chat → `choices` verbatim ; 400 → `is_overflow` |
| Responses (`api:responses`, `go/responses` ou `free/responses`) | Chat → `_chat_to_responses_request`, Anthropic → `_anthropic_to_responses_request`, natif → `_sanitize_native_responses_request` ; forward `compact_threshold`, `conversation`, `previous_response_id`, `prompt_cache_key` ; résumeur : `build_summarizer_responses_body` (`input`, `store:false`) | `openai_chat_to_responses` / `_responses_to_anthropic_response` / `_responses_to_chat_response` + `name_map` ; item `compaction` opaque relayé tel quel, sortie `/responses/compact` = fenêtre canonique (ne jamais élaguer) |

`jev/systemone` (`{model,state,questions}`) : jamais de corps résumeur chat
dessus → repli `FREE_DISCOVERY_DEFAULT_TARGET` chat-compatible.

## 4. Classe free/payante + réseau

`classify.has_free_leg` = id `-free`/pool découvert OU clé `FREE_MODEL_MAP` OU
valeur `FREE_MODELS`. `plan.summarizer_plan` + `class_policy` (`auto` défaut,
`free_only`/`paid_only` forcent après plan auto). `transport` :
- free = `_official_free_headers` + `_compaction_free_pool_request`
  (même `_try_free_model_first` que la conversation : stations, hedge,
  cooldowns, repli direct, `forced_pool` geo ; `seed` = nom d'origine pour la
  même résolution) — jamais de direct résidentiel hors pool, jamais de payant
  silencieux (pool épuisé → `UpstreamError` 502 → `None` → relais intact) ;
- paid = `_get_auth_headers` + `_do_request_with_retry`.
Condense-and-retry sur les 3 portes (`/v1/messages` branches anthropic +
openai-via, `/v1/chat/completions`, `/v1/responses` backend OpenAI) :
non-stream uniquement, 1 tentative, retry tunnel-aware (`_open_via_pool` si
`_geo_force_tunnel`), skip put cache (`_condensed_ok` / `_r6_condensed_ok`),
forme du retry selon l'endpoint (`messages` vs `input` + checkpoint adapté).
`native_passthrough:true` (défaut) = natifs relayés verbatim, jamais résumés
côté client. `summary_paid_bypass:false` (défaut OFF) = résumé client détecté
ne contourne pas `strict_free` (correctif Hermes opt-in).

## 5. Config

```yaml
server_compaction:
  enabled: false            # condense-and-retry serveur (défaut OFF = passthrough)
  summary_max_tokens: 2048  # capé à 4096 (cap client officiel)
  keep_recent_pairs: 4      # paires complètes gardées verbatim (frontières outil)
  summarizer_model_override: null  # souverain quand posé
  timeout_s: 60
  max_attempts: 1           # jamais de récursion
  min_chars_implicit: 1000  # borne anti-faux-positif shapes implicites
  overflow_markers: null    # null = défauts (prompt too long, context_length_exceeded…)
  class_policy: auto        # auto|free_only|paid_only
  native_passthrough: true
  keep_tail: true
  summary_paid_bypass: false
  tiny_retry: true          # stream compactage sans outils : fetch bufferisé +
                            # 1 refetch station fraîche sur sortie tiny (free-only)
  summary_lean: true        # jambe free : sorties d'outils tronquées sur trafic
                            # compactage détecté (l'amont digère mal les Mo)
  summary_lean_max_chars: 2000
```

### Streams de compactage (tiny-retry)

Les boucles live émettent chaque chunk dès réception : sur micro-sortie
amont, le client a déjà reçu l'octet défectueux et aucun retry same-stream
n'est possible. Pour un stream de compactage (y compris WITH-tools : les
compactions Claude Code rejouent l'historique avec outils), le proxy fetche
en bufferisé (stream OFF : même `_try_free_model_first` — stations, hedge,
cooldowns), rejoue UNE fois sur station fraîche (cooldown 60 s) si la
sortie est tiny (`in>=40k, out<100`, sans tool_use effectif), puis émet le
SSE reconstruit (`streaming.py`, mêmes champs que le live). Fidélité :
tout final non-texte (outil, thinking, bloc natif) retombe en live —
jamais de résumé amputé. Échec quelconque → `None` → live streaming
inchangé (fail-open total, jamais de payant silencieux). TTFB = durée
totale (tâche de fond, pas d'interactivité).

## 6. Modules

- `classify.py` : `has_free_leg`, `is_free_class` (pur, DI).
- `plan.py` : `summarizer_plan` → `(model,endpoint,protocol,api,is_free,seed)` (pur, DI).
- `router.py` : `detect_compaction` → `(is_compaction,is_native,kind)`, `plan_for_conversation` (pur, DI).
- `transport.py` : `summarizer_headers/request`, `normalize_free_responses`, `should_use_tunnel` (DI).
- `streaming.py` : `should_buffer_compaction`, `is_tiny_result`, `completion_text`, `chat/anthropic_sse_from_completion` (pur).
- `shapes.py` : `is_compaction_shape` (officielle+marqueur+natif), `is_native_compaction`.
- `summarizer.py` : `build_summarizer_body/_anthropic_body/_responses_body/_for_api`, `run_summarizer(...,api)`, `_extract_text` (chat+anthropic+responses, item compaction ignoré).
- `react.py` : `maybe_condense(...,api)` ; `truncate.py` : coupe aux frontières de paires + `build_checkpoint_input` / `build_condensed_input` (fenêtre Responses).
