"""Conversion SSE Chat Completions → événements SSE Anthropic (module autonome).

Extraction de la boucle de conversion mesurée en réel qui vivait inline dans
``opencode.py`` :

- ``opencode.py:10561-10710`` — boucle par delta : ``message_start``,
  blocs texte, blocs thinking, ``tool_calls`` → ``tool_use`` avec
  ``input_json_delta`` et restauration des noms longs (A8) ;
- ``opencode.py:8556-8660`` — ``_finalize_stream`` : séquence de clôture
  (``content_block_stop`` par bloc ouvert → ``message_delta`` → ``message_stop``) ;
- ``opencode.py:7945-7946`` — ``_sse`` : forme exacte des chaînes émises.

Cas d'usage cible : la « jambe free » (``anthropic_stream``,
``opencode.py:9144+``) relaie aujourd'hui les chunks Chat **bruts** au client
Anthropic (``opencode.py:9465-9466``) — le client reçoit des
``chat.completion.chunk`` sur un endpoint SSE Anthropic. Ce module fournit le
convertisseur manquant, réutilisable et testable hors du corps de la route.

État : **un ``ChatSseToAnthropicState`` par stream, obligatoire**.
``app/protocol/mapping.py:3720-3726`` documente le bug historique de globals
partagés entre streams concurrents (un stream B voyait l'état du stream A ;
un stream avorté fuyait vers le suivant). Aucun global mutable ici : ``state``
est requis et ``None`` lève ``ValueError`` — jamais d'état implicite.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from types import MappingProxyType

from app.protocol.mapping import (
    _json_dumps_str,
    _local_signature,
    anthropic_usage_from_openai,
    restore_tool_name,
)

__all__ = [
    "ChatSseToAnthropicState",
    "chat_sse_to_anthropic_events",
    "anthropic_stop_reason",
]

# Correspondance des ``finish_reason`` Chat → ``stop_reason`` Anthropic.
# Absent / inconnu → ``end_turn`` (cf. spécification du convertisseur).
# [P4 correctesse] Vue immuable : une table de correspondance constante ne doit
# pas pouvoir être mutée par un appelant — l'état mutable est PAR STREAM
# (``ChatSseToAnthropicState``), jamais module-level.
_STOP_REASON_MAP: Mapping[str, str] = MappingProxyType(
    {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "content_filter": "refusal",
    }
)


def anthropic_stop_reason(finish_reason: object, has_tool_use: bool = False) -> str:
    """Traduit un ``finish_reason`` Chat en ``stop_reason`` Anthropic.

    ``has_tool_use`` sert de repli quand l'amont n'a rien signalé : c'est la
    règle de ``opencode.py:8610/8637`` (``stop_reason = "tool_use" if
    tool_block_idx else "end_turn"``). S'applique aussi aux finish inconnus
    non-vides (ex. `refusal`) : un tour d'outil ne doit jamais rendre
    `end_turn` et faire rater l'exécution.
    """
    if isinstance(finish_reason, str) and finish_reason:
        mapped = _STOP_REASON_MAP.get(finish_reason)
        if mapped is not None:
            return mapped
        return "tool_use" if has_tool_use else "end_turn"
    return "tool_use" if has_tool_use else "end_turn"


def _sse(event: str, payload: dict) -> str:
    """Forme exacte de ``opencode.py:7945-7946`` (``_sse``), en ``str``.

    ``_sse`` retourne des ``bytes`` (``.encode()``) : ici on garde la chaîne
    pour que l'appelant choisisse son encodage (même charge utile JSON,
    ``ensure_ascii=False``, double saut de ligne terminal).
    """
    return f"event: {event}\ndata: {_json_dumps_str(payload, ensure_ascii=False)}\n\n"


# ── F1 : panne amont portée par une ligne ``data:`` ─────────────────────
# Une passerelle Chat n'a pas toujours l'exception : le HTTP est déjà en
# 200 et la panne arrive MILIEU de flux, sous forme de ligne
# ``data: {"error": {"message": …, "type": …}}``, suivie ou non d'un
# ``data: [DONE]``. ``chat.completion.chunk`` ne définit AUCUNE clé
# ``error`` : sa présence est donc sans ambiguïté un échec amont.
#
# Avant ce correctif, cette ligne tombait dans le cas « pas de ``choices`` » :
# ``first_choice`` et ``delta`` valaient ``{}`` et ``feed_chunk`` rendait
# ``[]``. Message ET type amont étaient perdus en route. Le client ne
# recevait, à la clôture, qu'un ``stop_reason: "error"`` nu — sans
# ``event: error``, donc sans moyen de classer la panne (rate_limit /
# overloaded / api_error) ni de décider d'un retry. Et dès qu'un peu de
# texte avait déjà coulé, le tour ressemblait à une troncature banale.

# Statut HTTP amont → type Anthropic. Table courte et FERMÉE : un statut
# inconnu ne devine rien (repli ``api_error``). ``MappingProxyType`` — cette
# table est module-level, donc en lecture seule (cf.
# ``test_pas_detat_mutable_au_niveau_module``).
_STATUS_ERROR_TYPES = MappingProxyType(
    {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        408: "api_error",
        413: "request_too_large",
        422: "invalid_request_error",
        429: "rate_limit_error",
        500: "overloaded_error",
        502: "overloaded_error",
        503: "overloaded_error",
        504: "api_error",
        529: "overloaded_error",
    }
)

# Libellé amont → type Anthropic. Volontairement SANS chiffre nu : « 500
# tokens written » ne doit pas devenir une surcharge. Ordre = priorité.
_TEXT_ERROR_TYPES = (
    (
        (
            "rate_limit",
            "rate limit",
            "ratelimit",
            "too many requests",
            "too_many_requests",
            "quota",
            "concurrency limit",
        ),
        "rate_limit_error",
    ),
    (
        (
            "overload",
            "capacity",
            "unavailable",
            "server_error",
            "internal_error",
            "internal server",
            "temporarily",
            "try again later",
        ),
        "overloaded_error",
    ),
    (
        ("context_length", "context length", "context window", "maximum context", "too long"),
        "invalid_request_error",
    ),
    (
        ("not_found", "does not exist", "unknown model", "model not found"),
        "not_found_error",
    ),
    (
        ("permission", "forbidden", "not allowed", "access denied"),
        "permission_error",
    ),
    (
        ("authentication", "unauthorized", "api_key", "api key", "invalid key"),
        "authentication_error",
    ),
    (
        ("invalid_request", "bad_request", "malformed", "unprocessable", "validation error"),
        "invalid_request_error",
    ),
)


def _status_error_type(status: object) -> str | None:
    """Statut amont → type Anthropic, ou ``None`` si non concluant.

    Même esprit que la table ``opencode.py:12127-12136`` (429 → rate_limit,
    529 → overloaded) : le statut est la signature la plus fiable, on la lit
    avant les libellés. ``bool`` est un ``int`` en Python — exclu
    explicitement pour qu'un ``True`` ne devienne pas le statut 1.
    """
    if isinstance(status, bool):
        return None
    if isinstance(status, str):
        digits = status.strip()
        if not (digits.isdigit() and len(digits) == 3):
            return None
        status = int(digits)
    elif isinstance(status, float):
        if not status.is_integer():
            return None
    elif not isinstance(status, int):
        return None
    return _STATUS_ERROR_TYPES.get(int(status))


def _text_error_type(text: object) -> str | None:
    """Libellé amont → type Anthropic, ou ``None`` si rien ne correspond."""
    if not isinstance(text, str) or not text.strip():
        return None
    low = text.lower()
    for needles, error_type in _TEXT_ERROR_TYPES:
        if any(n in low for n in needles):
            return error_type
    return None


def _upstream_error_shape(payload: object) -> tuple[str, str]:
    """Normalise une charge utile d'erreur amont en ``(type, message)``.

    Formes vues sur les passerelles Chat :
    ``{"error": {"message": …, "type": …, "code": …}}``,
    ``{"error": {"message": …, "status": 429}}``, ``{"error": "texte libre"}``,
    et parfois ``{"error": {"detail": …}}``. Tout ce qui n'est aucune de ces
    formes est rendu tel quel dans le message : mieux vaut un diagnostic
    illisible qu'un message vide.

    Priorité de classification : ``type`` puis ``code`` (champs dédiés), puis
    le statut HTTP, puis le message en dernier recours. Repli final :
    ``api_error``, le défaut de ``core/errors.py:74`` — jamais un type inventé.
    """
    if isinstance(payload, str):
        body = payload.strip()
        return _text_error_type(body) or "api_error", body or "upstream stream error"
    if not isinstance(payload, Mapping):
        # Valeur scalaire nue — typiquement un statut (`{"error": 503}`) ou un
        # code. Elle porte une information : on la classe et on la rend telle
        # quelle plutôt que de la réduire à « upstream stream error ».
        if isinstance(payload, bool) or payload is None:
            return "api_error", "upstream stream error"
        return _status_error_type(payload) or "api_error", str(payload)

    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        message = payload.get("detail")
    if not isinstance(message, str) or not message.strip():
        message = payload.get("error")
    if not isinstance(message, str) or not message.strip():
        # Rien d'exploitable : on rend la charge utile entière plutôt que de
        # taire la panne.
        try:
            message = _json_dumps_str(dict(payload), ensure_ascii=False)
        except Exception:
            message = "upstream stream error"

    error_type = (
        _text_error_type(payload.get("type"))
        or _text_error_type(payload.get("code"))
        or _status_error_type(payload.get("status"))
        or _status_error_type(payload.get("status_code"))
        or _text_error_type(message)
        or "api_error"
    )
    return error_type, message.strip()


def _new_id() -> str:
    """Identifiant de message, format ``msg_…`` comme ``_fast_id`` (opencode.py:398)."""
    return f"msg_{uuid.uuid4()}"


def _new_tool_id() -> str:
    """Identifiant de ``tool_use`` (repli quand l'amont n'en fournit pas).

    Même forme que ``_fast_id("toolu")`` (opencode.py:10659).
    """
    return f"toolu_{uuid.uuid4()}"


def _delta_text(content: object) -> str:
    """Texte d'un ``delta.content`` Chat — mirror de ``opencode.py:10593-10600``.

    ``str`` → tel quel ; liste de parts → concaténation des parts ``text``.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _usage_cache_read(chunk: dict) -> int | None:
    """Tokens cache-read lus du ``usage`` d'un chunk, sinon None.

    Formes amont : Chat (``prompt_tokens_details.cached_tokens``) et
    Responses (``input_tokens_details.cached_tokens``). Sans lui, le client
    SSE voyait toujours ``cache_read_input_tokens: 0`` alors que la DB
    (chemin inline) lisait le vrai cache — DB juste, client faux.
    """
    usage = chunk.get("usage")
    if not isinstance(usage, dict):
        return None
    for key in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(key)
        if isinstance(details, dict):
            cached = details.get("cached_tokens")
            if isinstance(cached, int):
                return cached
    return None


def _usage_tokens(chunk: dict) -> tuple[int | None, int | None]:
    """(input_tokens, output_tokens) lus du ``usage`` du chunk, sinon (None, None).

    Accepte les deux nommages rencontrés en amont : Chat Completions
    (``prompt_tokens`` / ``completion_tokens``) et Responses
    (``input_tokens`` / ``output_tokens``).
    """
    usage = chunk.get("usage")
    if not isinstance(usage, dict):
        return None, None
    tin = usage.get("prompt_tokens")
    if tin is None:
        tin = usage.get("input_tokens")
    tout = usage.get("completion_tokens")
    if tout is None:
        tout = usage.get("output_tokens")
    return (
        tin if isinstance(tin, int) else None,
        tout if isinstance(tout, int) else None,
    )


class ChatSseToAnthropicState:
    """État de conversion SSE Chat → événements Anthropic pour UN SEUL stream.

    Un état partagé entre deux streams concurrents reproduit le bug de globals
    documenté en ``app/protocol/mapping.py:3720-3726`` : instanciation
    **par stream**, aucun état module-level.
    """

    __slots__ = (
        "model",
        "message_id",
        "tool_name_map",
        "started",
        "finished",
        "next_block_idx",
        "text_block_idx",
        "thinking_block_idx",
        "thinking_text",
        "tool_block_idx",
        "pending_tool_args",
        "pending_tool_ids",
        "open_blocks",
        "input_tokens",
        "input_tokens_estimate",
        "usage_seen",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
        "finish_reason",
    )

    def __init__(
        self,
        model: str = "",
        message_id: str = "",
        tool_name_map: dict | None = None,
        input_tokens_estimate: int = 0,
    ) -> None:
        # Modèle annoncé au client (le modèle demandé, pas l'équivalent amont).
        self.model: str = model
        # ``message_start.message.id`` — généré une seule fois par stream.
        self.message_id: str = message_id or _new_id()
        # Restore-retour A8 : {short ≤64 envoyé à l'amont: nom d'origine client}.
        self.tool_name_map: dict | None = tool_name_map
        self.started: bool = False
        self.finished: bool = False
        # Indices de blocs alloués séquentiellement à partir de 0.
        self.next_block_idx: int = 0
        self.text_block_idx: int | None = None
        self.thinking_block_idx: int | None = None
        # Texte thinking accumulé : sert à forger la signature_delta de clôture
        # (parité chemin natif opencode.py) — sinon un client strict abandonne
        # le bloc en multi-tours.
        self.thinking_text: str = ""
        # index Chat du tool_call → index de bloc Anthropic.
        self.tool_block_idx: dict[int, int] = {}
        # Fragments d'args arrivés avant le vrai nom (gateway non-strict) :
        # bufferisés sans jamais émettre de bloc fantôme `tool_pending_*`.
        self.pending_tool_args: dict[int, str] = {}
        self.pending_tool_ids: dict[int, str] = {}
        # Blocs ouverts, dans l'ordre d'ouverture (ordre de clôture).
        self.open_blocks: list[int] = []
        # Input NON caché (sémantique Anthropic). Le total OpenAI arrive avec
        # le cache inclus : la conversion soustrait le cache pour que le client
        # n'additionne pas deux fois le même contexte (autocompact prématuré).
        self.input_tokens: int = 0
        self.usage_seen: bool = False
        # [réf. opencode.py:10584] ``message_start`` doit annoncer un
        # ``input_tokens`` : l'usage amont n'arrive qu'en fin de flux (voire
        # jamais sans ``stream_options.include_usage``), donc trop tard pour un
        # événement émis au premier chunk. La référence P2 émet pour cette
        # raison une ESTIMATION locale (``stream_in_est``) et non l'usage réel ;
        # on accepte donc l'estimation de l'appelant. Un usage amont réel
        # arrivé dans le même chunk que le premier delta reste prioritaire.
        self.input_tokens_estimate: int = max(0, int(input_tokens_estimate or 0))
        self.cache_read_input_tokens: int = 0
        self.cache_creation_input_tokens: int = 0
        self.output_tokens: int = 0
        self.finish_reason: str | None = None

    def reset(self) -> None:
        """Réinitialise l'état sans changer d'identité de stream (id conservé)."""
        self.started = False
        self.finished = False
        self.next_block_idx = 0
        self.text_block_idx = None
        self.thinking_block_idx = None
        self.thinking_text = ""
        self.tool_block_idx.clear()
        self.pending_tool_args.clear()
        self.pending_tool_ids.clear()
        self.open_blocks.clear()
        self.input_tokens = 0
        self.usage_seen = False
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0
        self.output_tokens = 0
        self.finish_reason = None

    def _open_block(self, content_block: dict) -> tuple[int, list[str]]:
        """Alloue l'index suivant, l'enregistre comme ouvert et émet le start."""
        idx = self.next_block_idx
        self.next_block_idx += 1
        self.open_blocks.append(idx)
        return idx, [
            _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": content_block,
                },
            )
        ]

    def client_input_tokens(self) -> int:
        """Input Anthropic à annoncer : réel non caché, sinon estimation totale.

        L'usage OpenAI contient le cache *dans* le total ; une fois l'usage vu,
        ``input_tokens`` a déjà été normalisé (cache soustrait) par
        ``feed_chunk``. Avant l'usage, on annonce l'estimation totale avec
        ``cache_read_input_tokens: 0`` — le ``message_delta`` final corrige.
        """
        if self.usage_seen:
            return max(0, self.input_tokens)
        return max(0, self.input_tokens or self.input_tokens_estimate)

    def message_start_events(self) -> list[str]:
        """``message_start`` — payload identique à ``opencode.py:8556-8609``.

        Émis paresseusement au premier chunk exploitable, ou en tête de la
        séquence de clôture si rien n'est jamais arrivé (``opencode.py:8588``).
        Parité officielle : inclut ``cache_creation_input_tokens`` (0 par défaut)
        pour que le SDK ne considère jamais le champ comme absent.
        """
        self.started = True
        return [
            _sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self.message_id,
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": self.model,
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": self.client_input_tokens(),
                            "output_tokens": 0,
                            "cache_read_input_tokens": self.cache_read_input_tokens,
                            "cache_creation_input_tokens": self.cache_creation_input_tokens,
                        },
                    },
                },
            )
        ]

    def finalize_events(self) -> list[str]:
        """Séquence de clôture — mirror de ``_finalize_stream`` (opencode.py:8556-8641).

        ``content_block_stop`` pour **chaque** bloc resté ouvert (ordre
        d'ouverture), puis ``message_delta`` (stop_reason + output_tokens),
        puis ``message_stop``. Idempotent : un second appel ne ré-émet rien
        (``finished``), pour que ``data: [DONE]`` ne soit traité qu'une fois.

        Parité officielle : si aucun ``finish_reason`` amont n'a été vu
        (coupure TCP/timeout/EOF nu), on NE déguise PAS en ``end_turn``.
        On clôt les blocs puis on émet ``stop_reason: error`` + ``message_stop``
        (même convention que ``_terminate_after_started`` côté opencode.py),
        pour que Claude Code retrye au lieu de rendre la main avec un tour
        tronqué qui force un `continue` manuel.
        """
        if self.finished:
            return []
        self.finished = True

        events: list[str] = []
        if not self.started:
            # opencode.py:8588-8609 — un flux clos sans aucune donnée reçoit
            # tout de même son ``message_start``.
            self.started = True
            events.extend(self.message_start_events())

        # opencode.py:8618-8632 — clôture des blocs ouverts, dans l'ordre.
        # Parité chemin natif : le bloc thinking reçoit sa `signature_delta`
        # (forgée locale — reconnue et strippée avant upstream strict par
        # strip_synthetic_thinking) au lieu d'être clos nu et abandonné par
        # un client strict au tour suivant.
        for idx in self.open_blocks:
            if idx == self.thinking_block_idx and self.thinking_text:
                events.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": idx,
                            "delta": {"type": "signature_delta", "signature": _local_signature(self.thinking_text)},
                        },
                    )
                )
            events.append(_sse("content_block_stop", {"type": "content_block_stop", "index": idx}))

        # Troncation sans terminal amont → `error`, jamais `end_turn` déguisé.
        if self.finish_reason is None:
            stop_reason = "error"
        else:
            stop_reason = anthropic_stop_reason(self.finish_reason, has_tool_use=bool(self.tool_block_idx))
        events.append(
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {
                        # Valeur finale non cachée : le client remplace
                        # l'estimation du start et additionne input + cache,
                        # soit exactement le total OpenAI d'origine.
                        "input_tokens": self.client_input_tokens(),
                        "output_tokens": self.output_tokens,
                        "cache_read_input_tokens": self.cache_read_input_tokens,
                        "cache_creation_input_tokens": self.cache_creation_input_tokens,
                    },
                },
            )
        )

        # opencode.py:8641 — message_stop.
        events.append(_sse("message_stop", {"type": "message_stop"}))
        return events

    def _upstream_error_events(self, payload: object, *, already_anthropic: bool = False) -> list[str]:
        """F1 — séquence émise quand l'amont porte l'erreur DANS le flux.

        ``event: error`` d'abord (type + message classés), puis la queue de
        finalize habituelle. Cet ordre est le point à ne pas inverser :

        - le client lit ``event: error`` et classe la panne (retry/backoff) ;
        - un client qui l'ignore hérite quand même d'une clôture propre —
          ``content_block_stop`` par bloc ouvert, ``message_delta`` avec
          ``stop_reason: error``, ``message_stop`` — au lieu d'un bloc jamais
          fermé qui lui ferait jeter tout le texte déjà reçu.

        Idempotent via ``finished`` : le ``[DONE]`` qui suit parfois l'erreur
        amont, et tout chunk tardif, ne réémettent rien.

        ``already_anthropic`` — certaines passerelles envoient l'erreur déjà
        formatée Anthropic (``data: {"type":"error","error":{…}}``) sans
        ``event: error``. La clé ``error`` de premier niveau fait alors
        déclencher ce même chemin. On relaie l'enveloppe TELLE QUELLE au lieu
        de la reclasser : reclasser perdrait les clés propres à l'amont (code,
        param, request_id…) sans rien gagner, et le client verrait une erreur
        qui n'est pas celle qu'il a reçue.
        """
        if already_anthropic and isinstance(payload, Mapping):
            frame = {"type": "error", "error": dict(payload)}
        else:
            error_type, message = _upstream_error_shape(payload)
            frame = {"type": "error", "error": {"type": error_type, "message": message}}
        events = [_sse("error", frame)]
        events.extend(self.finalize_events())
        return events

    def feed_chunk(self, chunk: dict) -> list[str]:
        """Convertit UN chunk Chat Completions parsé en 0..N chaînes SSE."""
        if self.finished:
            # Après ``message_stop``, le message est clos côté client : tout
            # chunk tardif (upstream bavard, ou donnée après ``[DONE]``) est
            # ignoré. Sans ce garde, un delta tardif rouvrait un bloc — donc un
            # ``content_block_delta`` sans ``content_block_start``, sur un
            # message déjà terminé.
            return []
        events: list[str] = []

        # OpenAI inclut le cache dans `prompt_tokens` / `input_tokens`.
        # Anthropic l'expose à part : on normalise ici pour que le client
        # (Claude Code) n'additionne pas deux fois le même contexte.
        _usage = chunk.get("usage")
        if isinstance(_usage, dict):
            _has_input = "prompt_tokens" in _usage or "input_tokens" in _usage
            _has_output = "completion_tokens" in _usage or "output_tokens" in _usage
            if _has_input or _has_output:
                _conv = anthropic_usage_from_openai(_usage)
                if _has_input:
                    self.input_tokens = _conv["input_tokens"]
                    self.usage_seen = True
                elif _conv["cache_read_input_tokens"] or _conv["cache_creation_input_tokens"]:
                    # Pas de total dans ce chunk, mais le cache est connu :
                    # l'estimation locale inclut déjà le cache, on le retire
                    # pour conserver la sémantique Anthropic.
                    self.input_tokens = max(
                        0,
                        self.input_tokens_estimate
                        - _conv["cache_read_input_tokens"]
                        - _conv["cache_creation_input_tokens"],
                    )
                    self.usage_seen = True
                self.cache_read_input_tokens = _conv["cache_read_input_tokens"]
                self.cache_creation_input_tokens = _conv["cache_creation_input_tokens"]
                if _has_output:
                    self.output_tokens = _conv["output_tokens"]

        # ── F1 : la panne amont passe AVANT tout le reste ──────────────
        # Une passerelle Chat signale un échec milieu de flux par une clé
        # ``error`` de premier niveau (le HTTP est déjà 200). Voir le bloc F1
        # en tête de module : sans cette branche la ligne était avalée et le
        # client n'apprenait la panne que par un ``stop_reason: error`` nu.
        # Placé APRÈS la reprise d'usage : une passerelle qui rend son usage
        # final à côté de l'erreur ne le perd pas.
        _upstream_error = chunk.get("error")
        if _upstream_error:
            return self._upstream_error_events(_upstream_error, already_anthropic=chunk.get("type") == "error")

        choices = chunk.get("choices")
        first_choice = choices[0] if isinstance(choices, list) and choices else {}
        if not isinstance(first_choice, dict):
            first_choice = {}
        delta = first_choice.get("delta")
        if not isinstance(delta, dict):
            delta = {}

        # opencode.py:10561-10590 — ``message_start`` au premier chunk exploitable.
        _usage_here = isinstance(chunk.get("usage"), dict)
        if not self.started and (delta or _usage_here):
            events.extend(self.message_start_events())

        # opencode.py:10592-10623 — bloc texte, ouvert paresseusement.
        text = _delta_text(delta.get("content"))
        if text:
            if self.text_block_idx is None:
                self.text_block_idx, opened = self._open_block({"type": "text", "text": ""})
                events.extend(opened)
            events.append(
                _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self.text_block_idx,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            )

        # opencode.py:10625-10650 — raisonnement → bloc ``thinking`` ouvert
        # paresseusement, delta ``thinking_delta`` (type relevé sur place,
        # ligne 10648 — jamais ``reasoning_delta``).
        # Parité officielle : `content_block_start` thinking inclut toujours `signature: ""`.
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            if self.thinking_block_idx is None:
                self.thinking_block_idx, opened = self._open_block(
                    {"type": "thinking", "thinking": "", "signature": ""}
                )
                events.extend(opened)
            self.thinking_text += reasoning
            events.append(
                _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self.thinking_block_idx,
                        "delta": {"type": "thinking_delta", "thinking": reasoning},
                    },
                )
            )

        # opencode.py:10652-10693 — tool_calls → blocs ``tool_use``.
        # Parité officielle : `name` minLength 1, `input_json_delta` toujours str.
        # Un premier fragment sans nom (gateway non-strict) ne doit jamais créer
        # un bloc `name: ""` invalide qui casse la boucle agentique.
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            api_idx = tc.get("index", 0)
            if not isinstance(api_idx, int):
                api_idx = 0
            function = tc.get("function")
            if not isinstance(function, dict):
                function = {}
            if api_idx not in self.tool_block_idx:
                tc_id = tc.get("id") or self.pending_tool_ids.get(api_idx) or _new_tool_id()
                # opencode.py:10660-10666 — [Lot L4 — A8] le nom raccourci pour
                # l'amont Chat (≤64) redevient celui que le client a envoyé.
                tc_name = restore_tool_name(function.get("name", ""), self.tool_name_map)
                if not tc_name:
                    # Parité officielle : jamais de bloc fantôme `tool_pending_*`.
                    # On bufferise id + args sans émettre ; le vrai nom arrivera
                    # au chunk suivant et le bloc sera créé avec le cumul.
                    _tc_id = tc.get("id")
                    if _tc_id:
                        self.pending_tool_ids[api_idx] = _tc_id
                    _pending_args = function.get("arguments", "")
                    if isinstance(_pending_args, dict):
                        try:
                            _pending_args = json.dumps(_pending_args, ensure_ascii=False)
                        except Exception:
                            _pending_args = ""
                    if isinstance(_pending_args, str) and _pending_args:
                        self.pending_tool_args[api_idx] = self.pending_tool_args.get(api_idx, "") + _pending_args
                    continue
                block_idx, opened = self._open_block(
                    {
                        "type": "tool_use",
                        "id": tc_id,
                        "name": tc_name,
                        "input": {},
                    }
                )
                self.tool_block_idx[api_idx] = block_idx
                events.extend(opened)
                # Flush des args bufferisés avant le nom.
                _flushed = self.pending_tool_args.pop(api_idx, "")
                self.pending_tool_ids.pop(api_idx, None)
                if _flushed:
                    events.append(
                        _sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": block_idx,
                                "delta": {"type": "input_json_delta", "partial_json": _flushed},
                            },
                        )
                    )
            # opencode.py:10684-10693 — fragments d'arguments concaténables.
            args = function.get("arguments", "")
            if isinstance(args, dict):
                try:
                    args = json.dumps(args, ensure_ascii=False)
                except Exception:
                    args = ""
            if isinstance(args, str) and args:
                events.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": self.tool_block_idx[api_idx],
                            "delta": {"type": "input_json_delta", "partial_json": args},
                        },
                    )
                )

        finish_reason = first_choice.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            self.finish_reason = finish_reason

        return events


def _is_data_line(line: str) -> bool:
    """True si la ligne SSE porte une charge ``data:`` exploitable.

    Lignes vides, commentaires (``:``) et champs ``event:``/``id:`` ignorés.
    """
    if not line or line.startswith(":"):
        return False
    return line.startswith("data:")


def chat_sse_to_anthropic_events(
    raw_line: str,
    parsed: dict | None = None,
    state: ChatSseToAnthropicState | None = None,
) -> list[str]:
    """Convertit UNE ligne SSE Chat Completions en 0..N chaînes SSE Anthropic.

    ``raw_line`` : ligne SSE brute (sans ``\\n``), p.ex. ``data: {...}``,
    ``data: [DONE]``, ``event: message``, ``: keep-alive`` ou ``""``.

    ``parsed`` : dict déjà désérialisé par l'appelant (évite un second parse) ;
    ignoré si ``None``.

    ``state`` : ``ChatSseToAnthropicState`` du stream courant — **requis**.
    ``None`` lève ``ValueError`` : c'est la garantie anti-fuite entre streams
    (cf. ``app/protocol/mapping.py:3720-3726``), on ne crée jamais d'état
    implicite.

    Retourne une liste de chaînes complètes ``event: …\\ndata: …\\n\\n`` prêtes
    à encoder (format de ``opencode.py:7945-7946``). Un ``data:`` au JSON
    invalide — ou de type non-objet — retourne ``[]`` : aucune exception ne
    remonte casser le flux.
    """
    if state is None:
        raise ValueError(
            "chat_sse_to_anthropic_events: state requis (un ChatSseToAnthropicState par stream) — "
            "jamais d'état implicite partagé entre streams"
        )
    if not _is_data_line(raw_line):
        return []

    payload = raw_line[5:].strip()
    if not payload:
        return []
    if payload == "[DONE]":
        # Séquence de clôture, une seule fois (idempotent).
        return state.finalize_events()

    if isinstance(parsed, dict):
        chunk = parsed
    else:
        try:
            chunk = json.loads(payload)
        except Exception:
            return []
        if not isinstance(chunk, dict):
            return []

    return state.feed_chunk(chunk)
