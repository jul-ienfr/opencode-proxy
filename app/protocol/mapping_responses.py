"""
Protocol mapping — conversions Responses API ↔ Anthropic (requests/ responses, chat→responses).

Découpe de app.protocol.mapping (T213).
"""

import copy
import hashlib
import json
import re
import time
import uuid
from collections import OrderedDict
from typing import Any

# [Plan perf-fiabilité Lot 1, décision 2026-09-06] FAIL FAST : orjson est
# épinglé en dur (requirements.txt:19 orjson==3.11.7) — import direct, crash
# net au boot si absent. Un fallback silencieux sur stdlib json rendrait le
# proxy 5-10x plus lent sans aucun signal.
import orjson as _orjson  # type: ignore

from config import CACHE_MIN_PROMPT_SIZE, yaml_get

# [Lot L2] Source unique de vérité pour l'effort (A1/A2/A13/A14/A15/A23).
# ``config.effort_policy`` n'importe que ``config.effort_caps`` (lui-même
# réduit à ``yaml_get``) + stdlib : aucun coût de boot (contrainte plan §7.4),
# aucun risque de cycle ``config/*``.
from config.effort_policy import resolve_effort as _resolve_effort
from config.limits import clamp_output_token_limit as _clamp_output_token_limit
from dashboard.display import debug as _debug
from dashboard.display import log as _log

from .tool_aliases import adapt_tool_call, dropped_tool_call_text, tool_call_supported

_cfg_settings: Any
try:
    import config.settings as _cfg_settings  # type: ignore[assignment]
except ImportError:  # pragma: no cover

    class _CfgFallback:
        DEBUG = False

    _cfg_settings = _CfgFallback()

# tiktoken — LAZY (Phase 3 chantier boot) : ce module n'utilise plus
# _encoding (doublon mort du singleton d'opencode.py) ; get_encoding()
# coûtait 1-2 s à froid à CHAQUE import. Nom conservé pour compat.
_encoding: Any = None


def _get_encoding() -> Any:
    global _encoding
    if _encoding is None:
        try:
            import tiktoken

            _encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _encoding = None
    return _encoding


# ── orjson fast-path (5-10x vs stdlib json on large bodies) ──
# (import fail-fast en tête de fichier — cf. note Lot 1 ci-dessus)


def _json_loads(b: bytes | str, **kw):
    if isinstance(b, str):
        b = b.encode()
    return _orjson.loads(b)


def _json_dumps(obj, **kw) -> bytes:
    if kw.get("indent") is not None:
        return json.dumps(obj, ensure_ascii=False, indent=kw.get("indent"), default=str).encode()
    return _orjson.dumps(obj)


def _json_dumps_str(obj, **kw) -> str:
    if kw.get("indent") is not None:
        return json.dumps(obj, ensure_ascii=False, indent=kw.get("indent"), default=str)
    if kw:
        return _orjson.dumps(obj).decode()
    return _orjson.dumps(obj).decode()


_JSON_LIB = "orjson"

def openai_responses_to_anthropic(body: dict) -> dict:
    """Convert OpenAI Responses API request → Anthropic Messages format.

    [2026-10-25] Deux formes très répandues étaient silencieusement PERDUES :
      * ``"input": "texte"`` (raccourci d'une requête utilisateur) — la boucle
        itérait la chaîne caractère par caractère, chacun écarté par le test
        ``isinstance(item, dict)``, donc zéro message ;
      * ``"content": "texte"`` dans un item — même effet.
    Résultat mesuré : le modèle recevait une requête vide et répondait
    « Bonjour, comment puis-je vous aider ? ». On normalise les deux formes
    en amont de la boucle.
    """
    _raw_input = body.get("input", [])
    if isinstance(_raw_input, str):
        _raw_input = [{"role": "user", "content": _raw_input}] if _raw_input else []
    elif not isinstance(_raw_input, list):
        _raw_input = []
    _norm_input: list = []
    for _it in _raw_input:
        if isinstance(_it, dict) and _it.get("role") and isinstance(_it.get("content"), str):
            _txt = _it["content"]
            _it = {
                **_it,
                "content": [{"type": "input_text", "text": _txt}] if _txt else [],
            }
        _norm_input.append(_it)

    # [audit 2026-10-30] ``instructions`` is the canonical Responses
    # system prompt (OpenAI SDK / Codex). It was never read, so the whole
    # system prompt vanished when the target was Anthropic.
    system_text = ""
    _instructions = body.get("instructions")
    if isinstance(_instructions, str) and _instructions.strip():
        system_text = _instructions
    pending_tool_results: list = []
    anthro_messages: list = []

    # Pre-scan tool call ids so a function_call_output can be matched even
    # if the client emitted it before the call item (defensive ordering).
    _declared_tool_ids: set[str] = set()
    for _it in _norm_input:
        if isinstance(_it, dict) and _it.get("type") == "function_call":
            _raw_id = _it.get("call_id") or _it.get("id")
            if isinstance(_raw_id, str) and _raw_id.strip():
                _declared_tool_ids.add(scrub_claude_tool_id(_raw_id))

    for item in _norm_input:
        if not isinstance(item, dict):
            continue
        role = item.get("role", item.get("type", "user"))

        if role in ("system", "developer"):
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") in ("input_text", "text", "output_text"):
                    _sys_part = block.get("text", "")
                    if _sys_part:
                        system_text = f"{system_text}\n\n{_sys_part}" if system_text else _sys_part
            continue

        if item.get("type") == "function_call_output":
            # output str → contrat golden (string seule reste string) ;
            # output liste (extension tolérée par Zen/go, pas schéma officiel
            # strict) → chaque part mappée vers le bloc Anthropic natif.
            _fco_out = item.get("output", "")
            if isinstance(_fco_out, list):
                _fco_blocks: list = []
                for _fp in _fco_out:
                    if isinstance(_fp, str):
                        if _fp:
                            _fco_blocks.append({"type": "text", "text": _fp})
                        continue
                    if not isinstance(_fp, dict):
                        continue
                    _ft = _fp.get("type", "")
                    if _ft in ("input_text", "output_text", "text"):
                        _fco_blocks.append({"type": "text", "text": _fp.get("text", "")})
                    elif _ft == "input_image":
                        _fco_mapped = _responses_part_to_anthropic(_fp)
                        _fco_blocks.append(_fco_mapped or {"type": "text", "text": "[image:unmapped]"})
                    elif _ft == "input_file":
                        _fco_mapped = _responses_part_to_anthropic(_fp)
                        _fco_blocks.append(_fco_mapped or {"type": "text", "text": "[document:unmapped]"})
                    elif _ft == "input_audio":
                        _debug("  [convert] DROP fco input_audio → Anthropic sans audio (placeholder)")
                        _fco_blocks.append({"type": "text", "text": "[audio:unsupported-by-anthropic]"})
                    else:
                        _fco_blocks.append({"type": "text", "text": _fp.get("text", str(_fp))})
                _fco_out = _fco_blocks or ""
            _fco_raw_id = item.get("call_id") or item.get("id")
            _fco_tid = (
                scrub_claude_tool_id(_fco_raw_id)
                if isinstance(_fco_raw_id, str) and _fco_raw_id.strip()
                else ""
            )
            if not _fco_tid:
                _debug("  [convert] SKIP function_call_output without call_id")
                continue
            if _fco_tid not in _declared_tool_ids:
                _debug(f"  [convert] SKIP orphan function_call_output id={_fco_tid!r}")
                continue
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": _fco_tid,
                    "content": _fco_out,
                }
            )
            continue

        # Convert previous-turn function_call items to assistant tool_use blocks
        if item.get("type") == "function_call":
            _fc_name = item.get("name", "")
            if not isinstance(_fc_name, str) or not _fc_name.strip():
                _debug(f"  [convert] SKIP function_call with empty name id={item.get('call_id', '?')}")
                continue
            try:
                inp = _json_loads(item.get("arguments", "{}"))
            except Exception:
                inp = {}
            _fc_raw_id = item.get("call_id") or item.get("id")
            _fc_tid = _tool_id_or_stable(
                _fc_raw_id,
                "toolu_",
                _fc_name,
                len(anthro_messages),
            )
            anthro_messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": _fc_tid,
                            "name": _fc_name,
                            "input": inp,
                        }
                    ],
                }
            )
            continue

        if role not in ("user", "assistant"):
            continue

        blocks = []
        # [audit 2026-10-30] Same immediate tool_result rule as P4: an
        # assistant final answer must not precede the tool result.
        if pending_tool_results:
            if role == "user":
                blocks.extend(pending_tool_results)
            else:
                anthro_messages.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results = []

        for block in item.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype in ("input_text", "text", "output_text"):
                # [audit 2026-10-30] Responses assistant history uses
                # ``output_text``. It fell through every branch and was
                # replaced by an empty text block, erasing every previous
                # assistant answer from the model's context.
                blocks.append({"type": "text", "text": block.get("text", "")})
            elif btype == "refusal":
                _refusal = block.get("refusal", "")
                if isinstance(_refusal, str) and _refusal:
                    blocks.append({"type": "text", "text": _refusal})
            elif btype in ("input_image", "input_file"):
                # Schéma Responses : input_image = image_url (data URI ou
                # https) ou file_id ; input_file = file_data (data URI) +
                # filename, file_id ou file_url. Formes historiques encore
                # acceptées en lecture (payloads pré-correctif, tests).
                _mapped = _responses_part_to_anthropic(block)
                if _mapped is not None:
                    blocks.append(_mapped)
            elif btype == "input_audio":
                # Anthropic n'a pas d'audio : placeholder honnête + debug.
                _debug("  [convert] DROP input_audio → Anthropic sans audio (placeholder)")
                blocks.append({"type": "text", "text": "[audio:unsupported-by-anthropic]"})
            elif btype == "reasoning":
                # [PLAN-raisonnement Phase D.3] pas de thinking forgé vers
                # l'upstream Anthropic (signature cryptographique exigée) —
                # le summary est omis de l'historique.
                _enc = block.get("encrypted_content")
                if isinstance(_enc, str) and _enc:
                    # [audit 2026-10-30] Authentic encrypted reasoning is
                    # replayable as redacted_thinking; dropping it lost the
                    # reasoning across a protocol switch.
                    blocks.append({"type": "redacted_thinking", "data": _enc})
                else:
                    _summary = block.get("summary") or []
                    _has_text = any(isinstance(s, dict) and s.get("text") for s in _summary)
                    if _has_text:
                        _debug(
                            "  [convert] DROP reasoning summary historique → upstream Anthropic (pas de signature forgée)"
                        )

        if not blocks:
            blocks.append({"type": "text", "text": ""})
        anthro_messages.append({"role": role, "content": blocks})

    if pending_tool_results:
        anthro_messages.append({"role": "user", "content": pending_tool_results})

    result = {
        "model": body.get("model", ""),
        "messages": anthro_messages,
        "stream": body.get("stream", False),
    }
    # [Lot L15 — B5] NB : `store`/`truncation` ne sont **pas** relayés ici. La
    # sortie de P6 part soit vers un upstream Anthropic (qui rejetterait ces
    # clés inconnues par un 400), soit vers la chaîne P2→P5 du handler
    # `/v1/responses`. C'est donc au handler — seul endroit qui connaît la
    # destination — de les replacer quand la cible est bien un endpoint
    # Responses (cf. `_relay_responses_storage_fields` appelé là-bas).
    # [Lot L14 — A17] Même lecture unifiée des formes de limite ; destination
    # Anthropic → toujours `max_tokens`.
    _set_output_token_limit(result, body, body.get("model", ""), target_protocol="anthropic")
    if system_text:
        result["system"] = system_text
    if "temperature" in body:
        result["temperature"] = body["temperature"]
    if "top_p" in body:
        result["top_p"] = body["top_p"]

    # Convert tools (strip "type": "function" wrapper)
    if "tools" in body:
        model = body.get("model", "")
        result["tools"] = []
        for t in body["tools"]:
            if isinstance(t, dict) and t.get("type") == "function":
                tool = {"name": t["name"], "description": t.get("description", "")}
                tool["input_schema"] = _normalize_tool_schema(t.get("input_schema") or t.get("parameters") or {}, model)
                result["tools"].append(tool)
        tc = body.get("tool_choice", "auto")
        if isinstance(tc, dict):
            tc_type = tc.get("type", "auto")
            if tc_type == "function":
                # Forme Responses {"type":"function","name"} + forme Chat
                # {"type":"function","function":{"name"}} : les deux arrivent ici.
                _nm = tc.get("name", "") or (tc.get("function", {}) or {}).get("name", "")
                result["tool_choice"] = {"type": "tool", "name": _nm}
            elif tc_type in ("any", "auto", "none"):
                result["tool_choice"] = {"type": tc_type}
            else:
                _debug(f"  [convert] tool_choice Responses dict inconnu {tc_type!r} → auto")
                result["tool_choice"] = {"type": "auto"}
        elif isinstance(tc, str):
            # Formes Chat valides vers objet Anthropic (jamais de string nue).
            if tc == "required":
                result["tool_choice"] = {"type": "any"}
            elif tc in ("auto", "any", "none"):
                result["tool_choice"] = {"type": tc}
            else:
                _debug(f"  [convert] tool_choice string inconnu {tc!r} → auto")
                result["tool_choice"] = {"type": "auto"}
        else:
            result["tool_choice"] = {"type": "auto"}
        # [audit 2026-10-30] Same mapping as P4.
        if body.get("parallel_tool_calls") is False and isinstance(result.get("tool_choice"), dict):
            result["tool_choice"]["disable_parallel_tool_use"] = True

    # Convert Anthropic thinking/effort -> model-specific reasoning parameter.
    # [Lot L2] SOURCE UNIQUE, comme P2 et P4. ``resolve_effort`` couvre
    # ``reasoning.effort`` (natif Responses, préservé par _sanitize + handler),
    # ``output_config.effort``, ``effort``, ``reasoning_effort`` et
    # ``thinking.*`` — et applique le plafond modèle de la config. La 3ᵉ table
    # budget→niveau locale est supprimée (A1) ainsi que l'écrasement de
    # ``xhigh``/``max`` (A2).
    #
    # BUG drop 2026-09-09 préservé : sur /v1/responses natif, l'effort était
    # perdu ici même — le test de non-régression correspondant reste vert.
    #
    # [Hotfix A15] Le champ de sortie est ``output_config.effort`` : jamais
    # ``reasoning_effort``, nom de champ OPENAI qu'un upstream Anthropic
    # ignorerait au mieux, rejetterait au pire.
    _decision = _resolve_effort(body, result.get("model", ""))
    if _decision.wants and _decision.level:
        _apply_anthropic_effort(result, _decision.level, result.get("model", ""), source=_decision.source)
    # Parité SDK : metadata/service_tier valides côté Anthropic, user → user_id.
    _relay_anthropic_optional_fields(result, body)

    return result


def anthropic_to_openai_responses(anthro: dict, model: str, name_map: dict | None = None) -> dict:
    """Convert Anthropic Messages response → OpenAI Responses API format."""
    content_blocks = anthro.get("content", [])
    output_items: list[dict[str, Any]] = []
    reasoning_items: list[dict[str, Any]] = []
    text_content: list = []
    function_calls: list = []

    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "text":
            text_content.append({"type": "output_text", "text": block.get("text", "")})
        elif btype == "thinking":
            # [Lot L12 — A19] `display: "omitted"` : le bloc existe et porte une
            # signature, mais son texte est vide. Émettre un item `reasoning`
            # avec un `summary_text` vide ferait afficher à chaque tour un bloc
            # « réflexion » vide côté client. On n'émet l'item que s'il y a
            # réellement quelque chose à résumer.
            _thinking_text = block.get("thinking") or ""
            if _thinking_text.strip():
                reasoning_items.append(
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": _thinking_text}],
                    }
                )
            else:
                _debug("  [convert] thinking display=omitted (texte vide) → pas d'item reasoning vide émis")
        elif btype == "redacted_thinking":
            # Raisonnement chiffré authentique : préservé rejouable
            # (encrypted_content) au lieu d'être droppé en silence.
            _data = block.get("data", "")
            if isinstance(_data, str) and _data:
                reasoning_items.append({"type": "reasoning", "encrypted_content": _data, "summary": []})
            else:
                _debug("  [convert] redacted_thinking sans data → droppé")
        elif btype == "tool_use":
            function_calls.append(
                {
                    "type": "function_call",
                    "call_id": _tool_id_or_stable(block.get("id"), "call_", block.get("name", ""), len(function_calls)),
                    # [TROU 3 — A8] Symétrie avec ``_responses_to_anthropic_response``
                    # / ``_responses_to_chat_response`` : le nom RÉELLEMENT émis au
                    # client est le nom d'origine (la map est construite à l'aller
                    # par ``sanitize_tool_names``). Sans ce restore, un nom
                    # raccourci pour l'amont ressortirait raccourci et le client ne
                    # pourrait plus faire correspondre ses propres outils.
                    "name": restore_tool_name(block.get("name", ""), name_map),
                    "arguments": _json_dumps_str(block.get("input", {}), ensure_ascii=False),
                    "status": "completed",
                }
            )

    # [audit 2026-10-30] Preserve reasoning order (two thinking blocks used
    # to come out reversed because each was inserted at index 0).
    output_items = reasoning_items + output_items
    if text_content:
        output_items.append({"type": "message", "role": "assistant", "content": text_content})
    output_items.extend(function_calls)

    # Status mapping
    sr = anthro.get("stop_reason", "")
    if sr in ("max_tokens", "refusal"):
        status = "incomplete"
    else:
        status = "completed"

    # Usage mapping. [audit 2026-10-30] Same inclusive-total rule as Chat:
    # Responses ``input_tokens`` includes cached tokens.
    usage = anthro.get("usage", {}) if isinstance(anthro.get("usage"), dict) else {}
    _non_cached_resp = _usage_int(usage.get("input_tokens"))
    _cache_read_resp = _usage_int(usage.get("cache_read_input_tokens"))
    _cache_write_resp = _usage_int(usage.get("cache_creation_input_tokens"))
    in_t = _non_cached_resp + _cache_read_resp + _cache_write_resp
    out_t = _usage_int(usage.get("output_tokens"))
    oai_usage = {
        "input_tokens": in_t,
        "output_tokens": out_t,
        "total_tokens": in_t + out_t,
        "input_tokens_details": {
            "cached_tokens": _cache_read_resp,
        },
        "output_tokens_details": {
            # [Lot L12 — A18] Était codé en dur à 0 : la part facturée la plus
            # chère d'un modèle de raisonnement était toujours affichée nulle.
            # Borné par `out_t` : la ventilation est un sous-ensemble de la sortie.
            "reasoning_tokens": _extract_reasoning_tokens(usage, out_t),
        },
    }

    return {
        "id": f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "status": status,
        "model": model,
        "output": output_items,
        "usage": oai_usage,
    }


def openai_chat_to_responses(chat_resp: dict, model: str, name_map: dict | None = None) -> dict:
    """Convert OpenAI Chat Completions response directly to OpenAI Responses API format.

    Bypasses the intermediate Anthropic format to avoid data loss and unnecessary conversion.

    [TROU 3 — A8] ``name_map`` (``{short: original}`` construit à l'aller par
    ``sanitize_tool_names``, transporté sous ``_TOOL_NAME_MAP_KEY``) restaure le
    nom d'origine des outils RÉELLEMENT émis : sans lui, un nom raccourci pour la
    borne 64 de l'amont ressortait raccourci au client, qui ne pouvait plus faire
    correspondre ses propres outils (asymétrie avec ``_responses_to_chat_response``
    / ``_responses_to_anthropic_response``, qui avaient déjà le paramètre).
    """
    choice = chat_resp.get("choices", [{}])[0]
    msg = choice.get("message", {})
    usage = chat_resp.get("usage", {})

    output_items: list[dict[str, Any]] = []

    # Reasoning content -> reasoning item
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if reasoning:
        output_items.insert(
            0,
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": reasoning}],
            },
        )

    # Text content -> message item with output_text
    content = msg.get("content", "")
    if isinstance(content, list):
        # [audit 2026-10-30] Some upstreams return Chat content as a list of
        # parts; copying it verbatim produced `text: [dict]` (invalid).
        content = "".join(
            str(p.get("text", "")) for p in content if isinstance(p, dict)
        )
    elif content is None:
        content = ""
    if content:
        output_items.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": str(content)}],
            }
        )

    # Tool calls -> function_call items
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
        _tc_name = restore_tool_name(fn.get("name", ""), name_map)
        if not isinstance(_tc_name, str) or not _tc_name.strip():
            _debug("  [convert] SKIP tool_call with empty name (responses response)")
            continue
        output_items.append(
            {
                "type": "function_call",
                "call_id": _tool_id_or_stable(tc.get("id"), "call_", fn.get("name", ""), len(output_items)),
                # [TROU 3 — A8] Restore-retour : le client reçoit le nom qu'il a
                # envoyé, jamais le raccourci interne de la borne 64.
                "name": restore_tool_name(fn.get("name", ""), name_map),
                "arguments": fn.get("arguments", "{}"),
                "status": "completed",
            }
        )

    # Status mapping
    finish = choice.get("finish_reason", "")
    if finish == "length":
        status = "incomplete"
    else:
        status = "completed"

    # Usage mapping
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    cached = _extract_cache_tokens(usage)
    oai_usage = {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "input_tokens_details": {
            "cached_tokens": cached,
        },
        "output_tokens_details": {
            # [Lot L12 — A18] Idem : remonte la ventilation réelle du raisonnement,
            # bornée par le total de sortie.
            "reasoning_tokens": _extract_reasoning_tokens(usage, completion_tokens),
        },
    }

    return {
        "id": f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "status": status,
        "model": model,
        "output": output_items,
        "usage": oai_usage,
    }


