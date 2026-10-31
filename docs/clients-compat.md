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

## Gratuit ou payant : ce qu'un client peut lire

- `GET /v1/models` : chaque entrée expose `tier` (`free`/`paid`/`unknown`) et
  `proxy.{tier_source, route_leg, route_target, free_first, free_target,
  free_mapping, free_mapping_active, route_mismatch}` ; la réponse ajoute
  `counts`. Filtres `?tier=free|paid|unknown` et `?free_first=true|false`,
  appliqués **avant** la pagination.
- `tier` est la facturation ; `route_leg` est la jambe réellement utilisée.
  `route_target` nomme la cible d'un alias (`glm-5.1 → glm-5.3-flash-free`) ;
  si cette cible est free, l'alias reste `tier=paid` avec `free_target`.
  `free_first=true` sur un `tier=paid` signale un repli gratuit actif.
  `tier_source=unproven` signale un id seulement listé sur la jambe anonyme,
  sans preuve de gratuité : il est facturé `paid` par prudence.
- En-têtes par appel : `X-OpenCode-Proxy-Tier`, `-Requested-Tier`, `-Model`,
  `-Requested-Model`, `-Fallback`. `-Requested-Model` est l'id **client**
  (alias compris), `-Model` la cible réellement servie. Ils reflètent la jambe
  au **début** de la réponse ; un repli survenu en cours de stream ne les
  réécrit pas.
- `GET /api/config` expose les mêmes champs plus `model_counts` ;
  `GET /api/free-models` ajoute l'inventaire detected/listed/candidates/
  routed/served. `detected` n'est pas `free` : ce sont les candidats amont.
- Un client qui ne comprend pas ces champs ignore simplement les clés
  supplémentaires : le format Anthropic/OpenAI reste inchangé.

## Fenêtres de contexte (alignement proxy-side)

Chaque modèle a sa propre fenêtre (`limit.input`/`limit.context` et
`limit.output`). Le proxy lit cette fenêtre par modèle et applique la même
règle que le client officiel : `window − max(10 %, 16k)`, puis `keep = 15 000`.
Pour une route `free_first`, il utilise la fenêtre **effective** :
`min(modèle demandé, cible free)`. Il compacte donc à 180 000 tokens même si
le client croit encore à 1M — sans aucune modification du client officiel.

`GET /v1/models` expose la même fenêtre effective (`max_input_tokens`,
`max_tokens`) pour les clients API qui la lisent ; ce n’est pas requis pour
l’alignement, le proxy l’applique de son côté. Le plafond de **corps** free
(900 Ko) est une contrainte de transport, pas la fenêtre du modèle.

## Authentification (décision v9)

- Dashboard : zéro friction en loopback + LAN (`dashboard_trust.lan_cidrs`),
  aucune saisie jamais demandée ; anti-CSRF même-host sur les mutations ;
  token opt-in (`require_token`) pour une exposition WAN éventuelle.
- `/v1/*` : `client_auth.mode: none` par défaut (bind 0.0.0.0 voulu) ;
  `lan`/`key` disponibles en opt-in.

## Limites connues (audit §14, ne pas « documenter comme normal »)

- images Anthropic **`source.type: file`** (`file_id`) perdues vers un upstream
  OpenAI/Chat — remplacées par le texte `[image:file]`, sans que le client soit
  prévenu (corrigé le 11/09 : la formulation précédente, « images Anthropic perdues
  vers upstream OpenAI (§14.1.6) », était **fausse pour `base64` et `url`**, qui
  traversent intacts — voir `docs/conversion-matrix.md`, section « Mapping champs »)
- erreurs de stream /v1/responses non distinguables d'un succès (§14.1.14)
