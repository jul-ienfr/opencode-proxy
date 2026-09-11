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

from app.protocol.mapping import _json_dumps_str, restore_tool_name

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
        "content_filter": "end_turn",
    }
)


def anthropic_stop_reason(finish_reason: object, has_tool_use: bool = False) -> str:
    """Traduit un ``finish_reason`` Chat en ``stop_reason`` Anthropic.

    ``has_tool_use`` sert de repli quand l'amont n'a rien signalé : c'est la
    règle de ``opencode.py:8610/8637`` (``stop_reason = "tool_use" if
    tool_block_idx else "end_turn"``).
    """
    if isinstance(finish_reason, str) and finish_reason:
        mapped = _STOP_REASON_MAP.get(finish_reason)
        if mapped is not None:
            return mapped
        return "end_turn"
    return "tool_use" if has_tool_use else "end_turn"


def _sse(event: str, payload: dict) -> str:
    """Forme exacte de ``opencode.py:7945-7946`` (``_sse``), en ``str``.

    ``_sse`` retourne des ``bytes`` (``.encode()``) : ici on garde la chaîne
    pour que l'appelant choisisse son encodage (même charge utile JSON,
    ``ensure_ascii=False``, double saut de ligne terminal).
    """
    return f"event: {event}\ndata: {_json_dumps_str(payload, ensure_ascii=False)}\n\n"


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
        return "".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


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
        "tool_block_idx",
        "open_blocks",
        "input_tokens",
        "input_tokens_estimate",
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
        # index Chat du tool_call → index de bloc Anthropic.
        self.tool_block_idx: dict[int, int] = {}
        # Blocs ouverts, dans l'ordre d'ouverture (ordre de clôture).
        self.open_blocks: list[int] = []
        self.input_tokens: int = 0
        # [réf. opencode.py:10584] ``message_start`` doit annoncer un
        # ``input_tokens`` : l'usage amont n'arrive qu'en fin de flux (voire
        # jamais sans ``stream_options.include_usage``), donc trop tard pour un
        # événement émis au premier chunk. La référence P2 émet pour cette
        # raison une ESTIMATION locale (``stream_in_est``) et non l'usage réel ;
        # on accepte donc l'estimation de l'appelant. Un usage amont réel
        # arrivé dans le même chunk que le premier delta reste prioritaire.
        self.input_tokens_estimate: int = max(0, int(input_tokens_estimate or 0))
        self.output_tokens: int = 0
        self.finish_reason: str | None = None

    def reset(self) -> None:
        """Réinitialise l'état sans changer d'identité de stream (id conservé)."""
        self.started = False
        self.finished = False
        self.next_block_idx = 0
        self.text_block_idx = None
        self.thinking_block_idx = None
        self.tool_block_idx.clear()
        self.open_blocks.clear()
        self.input_tokens = 0
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

    def message_start_events(self) -> list[str]:
        """``message_start`` — payload identique à ``opencode.py:8556-8609``.

        Émis paresseusement au premier chunk exploitable, ou en tête de la
        séquence de clôture si rien n'est jamais arrivé (``opencode.py:8588``).
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
                            "input_tokens": self.input_tokens or self.input_tokens_estimate,
                            "output_tokens": 0,
                            "cache_read_input_tokens": 0,
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
        # (La ``signature_delta`` de ``_finalize_stream`` est locale au proxy
        # et hors périmètre : ici le thinking vient d'un amont Chat, sans
        # signature à rejouer — cf. section « divergences » du rapport.)
        for idx in self.open_blocks:
            events.append(_sse("content_block_stop", {"type": "content_block_stop", "index": idx}))

        # opencode.py:8633-8640 — message_delta : stop_reason puis usage.
        stop_reason = anthropic_stop_reason(self.finish_reason, has_tool_use=bool(self.tool_block_idx))
        events.append(
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": self.output_tokens},
                },
            )
        )

        # opencode.py:8641 — message_stop.
        events.append(_sse("message_stop", {"type": "message_stop"}))
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

        usage_in, usage_out = _usage_tokens(chunk)
        if usage_in is not None:
            self.input_tokens = usage_in
        if usage_out is not None:
            self.output_tokens = usage_out

        choices = chunk.get("choices")
        first_choice = choices[0] if isinstance(choices, list) and choices else {}
        if not isinstance(first_choice, dict):
            first_choice = {}
        delta = first_choice.get("delta")
        if not isinstance(delta, dict):
            delta = {}

        # opencode.py:10561-10590 — ``message_start`` au premier chunk exploitable.
        if not self.started and (delta or usage_in is not None or usage_out is not None):
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
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            if self.thinking_block_idx is None:
                self.thinking_block_idx, opened = self._open_block({"type": "thinking", "thinking": ""})
                events.extend(opened)
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
                tc_id = tc.get("id", _new_tool_id())
                # opencode.py:10660-10666 — [Lot L4 — A8] le nom raccourci pour
                # l'amont Chat (≤64) redevient celui que le client a envoyé.
                tc_name = restore_tool_name(function.get("name", ""), self.tool_name_map)
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
            # opencode.py:10684-10693 — fragments d'arguments concaténables.
            args = function.get("arguments", "")
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
