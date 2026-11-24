"""
Protocol mapping — streaming Responses : état SSE, emitter, stream events.

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

# ── Responses API tool mapping cache (item_id/output_index → tool info) ──
# [P4 correctesse] état PAR STREAM : les trois structures ci-dessous étaient
# des globals partagés entre streams concurrents — un stream B voyait les
# item_ids/out_idx du stream A, et un stream avorté (pas de response.completed)
# fuyait son état vers le suivant. ``ResponsesSseState`` est instancié par
# stream et passé via ``state=`` ; les globals restent le fallback legacy
# (appelants sans state : fixtures golden, compatibilité).
_responses_tool_cache: dict = {}
_responses_tool_index_map: dict = {}  # output_index -> sequential tool index (0,1,2...)
# Track reasoning item_ids for which a delta was already emitted (dedupe delta vs done)
_reasoning_seen_ids: set = set()
# Texte déjà émis (par output_index/content_index) et arguments d'outil déjà
# émis (par output_index). Certains amonts n'envoient QUE le cycle de vie
# (created/output_item.done/completed) sans aucun delta : on reconstruit alors
# le contenu depuis ces événements sans dupliquer ce qui a déjà coulé.
_responses_text_seen: dict = {}
_responses_args_seen: dict = {}


class ResponsesSseState:
    """État de conversion SSE Responses-API pour UN stream."""

    __slots__ = ("tool_cache", "tool_index_map", "reasoning_seen", "tool_name_map", "text_seen", "args_seen")

    def __init__(self) -> None:
        self.tool_cache: dict = {}
        self.tool_index_map: dict = {}
        self.reasoning_seen: set = set()
        self.text_seen: dict = {}
        self.args_seen: dict = {}
        # Restore-retour stream : {short: original}, posée par requête par le
        # caller (même map que l'aller) ; None = pas de rename sur ce stream.
        self.tool_name_map: dict | None = None

    def reset(self) -> None:
        self.tool_cache.clear()
        self.tool_index_map.clear()
        self.reasoning_seen.clear()
        self.text_seen.clear()
        self.args_seen.clear()
        self.tool_name_map = None


def _responses_item_text(item: dict) -> str:
    """Texte visible porté par un item ``message`` complet."""
    content = item.get("content")
    text = ""
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                text += part.get("text") or ""
    elif isinstance(content, str):
        text = content
    if not text:
        raw = item.get("text")
        if isinstance(raw, str):
            text = raw
    return text


def _responses_seen_text(text_seen: dict, output_index: int) -> str:
    """Concatène le texte déjà émis pour un output_index (toutes parts)."""
    prefix = f"idx_{output_index}:"
    parts: list[tuple[int, str]] = []
    for key, value in text_seen.items():
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix) :]
        if suffix.isdigit() and isinstance(value, str):
            parts.append((int(suffix), value))
    return "".join(value for _, value in sorted(parts))


def _responses_set_seen_text(text_seen: dict, output_index: int, text: str) -> None:
    """Remplace tout le texte déjà émis d'un output_index par ``text``."""
    prefix = f"idx_{output_index}:"
    for key in [k for k in text_seen if k.startswith(prefix)]:
        text_seen.pop(key, None)
    text_seen[f"{prefix}0"] = text


def _responses_item_delta(
    item: dict,
    output_index: int,
    tool_cache: dict,
    tool_index_map: dict,
    text_seen: dict,
    args_seen: dict,
    tool_name_map: dict | None,
):
    """Delta Chat reconstruit depuis un item complet (flux « cycle de vie »).

    Ne remonte que la partie non déjà streamée : un amont qui émet à la fois
    les deltas et ``output_item.done`` ne doit jamais dupliquer le contenu.
    """
    if not isinstance(item, dict):
        return None
    itype = item.get("type")

    if itype == "message":
        full = _responses_item_text(item)
        if not full:
            return None
        seen = _responses_seen_text(text_seen, output_index)
        if full.startswith(seen):
            emit = full[len(seen) :]
        elif not seen:
            emit = full
        else:
            emit = ""
        _responses_set_seen_text(text_seen, output_index, full)
        if not emit:
            return None
        return {"choices": [{"delta": {"content": emit}, "finish_reason": None}]}

    if itype in ("function_call", "custom_tool_call", "local_shell_call"):
        raw_args = item.get("arguments")
        if isinstance(raw_args, dict):
            args = _json_dumps_str(raw_args, ensure_ascii=False)
        elif isinstance(raw_args, str):
            args = raw_args
        else:
            args = ""
        name = restore_tool_name(item.get("name", ""), tool_name_map)
        iid = item.get("id") or item.get("call_id") or ""
        info = tool_cache.get(iid) or tool_cache.get(f"idx_{output_index}")
        if info is None:
            if output_index not in tool_index_map:
                tool_index_map[output_index] = len(tool_index_map)
            tool_idx = tool_index_map[output_index]
        else:
            tool_idx = info["index"]
        key = f"idx_{output_index}"
        seen = args_seen.get(key, "")
        if args.startswith(seen):
            emit_args = args[len(seen) :]
        elif not seen:
            emit_args = args
        else:
            emit_args = ""
        args_seen[key] = args
        if info is None:
            call_id = _tool_id_or_stable(
                item.get("call_id") or item.get("id"),
                "call_",
                item.get("name", ""),
                output_index + 1,
            )
            tool_cache[iid] = {
                "index": tool_idx,
                "call_id": call_id,
                "name": name,
                "output_index": output_index,
            }
            tool_cache[f"idx_{output_index}"] = {
                "index": tool_idx,
                "call_id": call_id,
                "name": name,
            }
            if not name:
                return None
            return {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": tool_idx,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": name, "arguments": emit_args},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        if not emit_args:
            return None
        return {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": tool_idx, "function": {"arguments": emit_args}}]},
                    "finish_reason": None,
                }
            ]
        }

    return None


def _responses_sse_to_chat_deltas(raw_line: str, parsed=None, state: "ResponsesSseState | None" = None):
    """Convert one Responses API SSE data line to chat/completions delta chunks.

    Yields 0..N dicts in chat/completions streaming format:
      {"choices": [{"delta": {"content": "...", "reasoning_content": "..."}, "finish_reason": null}]}
    so the existing stream parser in stream_gen() works unchanged.

    [C2 perf] ``parsed``: dict déjà parsé par l'appelant (son propre
    _json_loads(data_str)) — évite un second parse identique par chunk SSE.
    Ignoré quand None (compatibilité appelants legacy / fixtures golden).

    [P4] ``state``: ResponsesSseState du stream courant. None → fallback
    legacy sur les globals module-level (comportement historique).

    Returns None if the line is [DONE] or not parseable.
    """
    if raw_line == "[DONE]":
        return None
    if parsed is not None and isinstance(parsed, dict):
        chunk = parsed
    else:
        try:
            chunk = _json_loads(raw_line)
        except Exception:
            return None
        if not isinstance(chunk, dict):
            return None

    if state is not None:
        tool_cache = state.tool_cache
        tool_index_map = state.tool_index_map
        reasoning_seen = state.reasoning_seen
        text_seen = state.text_seen
        args_seen = state.args_seen
    else:
        tool_cache = _responses_tool_cache
        tool_index_map = _responses_tool_index_map
        reasoning_seen = _reasoning_seen_ids
        text_seen = _responses_text_seen
        args_seen = _responses_args_seen

    def _clear_state() -> None:
        if state is not None:
            state.reset()
        else:
            _responses_tool_cache.clear()
            _responses_tool_index_map.clear()
            _reasoning_seen_ids.clear()
            _responses_text_seen.clear()
            _responses_args_seen.clear()

    _tool_name_map = state.tool_name_map if state is not None else None

    etype = chunk.get("type", "")
    if _cfg_settings.DEBUG:
        # [B4 perf] f-string par delta — construite seulement quand DEBUG on
        _debug(f"  [responses-sse] event type={etype!r} keys={list(chunk.keys())[:8]}")

    # response.output_text.delta — direct text delta (Responses API streaming format)
    if etype == "response.output_text.delta":
        text = chunk.get("delta", "")
        if not text:
            return None
        _out_idx = chunk.get("output_index", 0)
        _cidx = chunk.get("content_index", 0)
        _key = f"idx_{_out_idx}:{_cidx}"
        text_seen[_key] = text_seen.get(_key, "") + text
        return {"choices": [{"delta": {"content": text}, "finish_reason": None}]}

    # response.output_text.done — texte complet d'une part. Sur un flux qui
    # n'émet QUE le cycle de vie, c'est la seule source du contenu : on remonte
    # le suffixe non déjà streamé (aucun doublon si les deltas existaient).
    if etype == "response.output_text.done":
        text = chunk.get("text", "")
        if not isinstance(text, str) or not text:
            return None
        _out_idx = chunk.get("output_index", 0)
        _cidx = chunk.get("content_index", 0)
        _key = f"idx_{_out_idx}:{_cidx}"
        seen = text_seen.get(_key, "")
        if text.startswith(seen):
            emit = text[len(seen) :]
        elif not seen:
            emit = text
        else:
            emit = ""
        text_seen[_key] = text
        if not emit:
            return None
        return {"choices": [{"delta": {"content": emit}, "finish_reason": None}]}

    # response.content_part.delta — text or reasoning summary delta
    if etype == "response.content_part.delta":
        delta_obj = chunk.get("delta", {})
        dtype = delta_obj.get("type", "")
        text = delta_obj.get("text", "")
        if _cfg_settings.DEBUG:
            # [B4 perf] slice+repr par delta — construits seulement DEBUG on
            _debug(
                f"  [responses-sse] content_part.delta dtype={dtype!r} text={text[:80]!r} full_delta_keys={list(delta_obj.keys())[:10]}"
            )
        if not text:
            return None
        if dtype == "output_text":
            _out_idx = chunk.get("output_index", 0)
            _cidx = chunk.get("content_index", 0)
            _key = f"idx_{_out_idx}:{_cidx}"
            text_seen[_key] = text_seen.get(_key, "") + text
            return {"choices": [{"delta": {"content": text}, "finish_reason": None}]}
        elif dtype == "reasoning_summary_text":
            return {"choices": [{"delta": {"reasoning_content": text}, "finish_reason": None}]}
        return None

    # response.reasoning_summary_text.delta — thinking delta (alternative event name)
    if etype == "response.reasoning_summary_text.delta":
        text = chunk.get("delta", "")
        if not text:
            return None
        # per-summary_index dedupe (item_id:summary_index) — two parts of same item must not clobber each other
        _iid = chunk.get("item_id", "")
        _sidx = chunk.get("summary_index", 0)
        _key = f"{_iid}:{_sidx}" if _iid else f"delta:{_sidx}:{text[:8]}"
        if _key:
            reasoning_seen.add(_key)
        return {"choices": [{"delta": {"reasoning_content": text}, "finish_reason": None}]}

    # response.reasoning_summary_text.done — fallback when delta missing (short reasoning)
    if etype == "response.reasoning_summary_text.done":
        _iid = chunk.get("item_id", "")
        _sidx = chunk.get("summary_index", 0)
        _key = f"{_iid}:{_sidx}" if _iid else ""
        if _key and _key in reasoning_seen:
            _debug(f"  [responses-sse] reasoning_summary_text.done deduped (delta already emitted) key={_key!r}")
            return None
        text = chunk.get("text", "") or chunk.get("delta", "")
        if not text:
            return None
        if _key:
            reasoning_seen.add(_key)
        return {"choices": [{"delta": {"reasoning_content": text}, "finish_reason": None}]}

    # response.reasoning_summary_part.done — part-level summary (contains summary_text)
    if etype == "response.reasoning_summary_part.done":
        part = chunk.get("part", {}) if isinstance(chunk.get("part"), dict) else {}
        text = part.get("text", "") if isinstance(part, dict) else ""
        if not text:
            text = chunk.get("text", "")
        if not text:
            return None
        _iid = chunk.get("item_id", "")
        _sidx = chunk.get("summary_index", 0)
        _key = f"{_iid}:{_sidx}" if _iid else f"part:{text[:8]}"
        if _key and _key in reasoning_seen:
            return None
        if _key:
            reasoning_seen.add(_key)
        return {"choices": [{"delta": {"reasoning_content": text}, "finish_reason": None}]}

    # response.output_item.added — start of function_call (tool_use)
    if etype == "response.output_item.added":
        item = chunk.get("item", {}) if isinstance(chunk.get("item"), dict) else {}
        if item.get("type") == "function_call":
            out_idx = chunk.get("output_index", 0)
            # Map output_index to sequential tool index
            if out_idx not in tool_index_map:
                tool_index_map[out_idx] = len(tool_index_map)
            tool_idx = tool_index_map[out_idx]
            call_id = _tool_id_or_stable(
                item.get("call_id") or item.get("id"),
                "call_",
                item.get("name", ""),
                len(tool_index_map),
            )
            # Restore-retour : le client reçoit le nom original, jamais le
            # raccourci sanitizé envoyé à l'upstream.
            _sse_name_map = state.tool_name_map if state is not None else None
            name = restore_tool_name(item.get("name", ""), _sse_name_map)
            # Cache for later delta events (by item_id and output_index)
            iid = item.get("id") or call_id
            tool_cache[iid] = {
                "index": tool_idx,
                "call_id": call_id,
                "name": name,
                "output_index": out_idx,
            }
            tool_cache[f"idx_{out_idx}"] = {
                "index": tool_idx,
                "call_id": call_id,
                "name": name,
            }
            _debug(
                f"  [responses-sse] function_call start idx={tool_idx} (out_idx={out_idx}) name={name!r} call_id={call_id}"
            )
            return {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": tool_idx,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": name, "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }

    # response.function_call_arguments.delta — streaming tool arguments
    if etype == "response.function_call_arguments.delta":
        delta = chunk.get("delta", "")
        if not delta:
            return None
        out_idx = chunk.get("output_index", 0)
        iid = chunk.get("item_id", "")
        _key = f"idx_{out_idx}"
        args_seen[_key] = args_seen.get(_key, "") + delta
        # Lookup tool index
        info = tool_cache.get(iid) or tool_cache.get(f"idx_{out_idx}")
        if info is None:
            # Fallback: create entry if we missed the 'added' event
            if out_idx not in tool_index_map:
                tool_index_map[out_idx] = len(tool_index_map)
            tool_idx = tool_index_map[out_idx]
            _debug(
                f"  [responses-sse] function_call delta without prior added — fallback idx={tool_idx} out_idx={out_idx}"
            )
        else:
            tool_idx = info["index"]
        return {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": tool_idx, "function": {"arguments": delta}}]},
                    "finish_reason": None,
                }
            ]
        }

    # response.function_call_arguments.done — arguments complets. On ne
    # remonte que le suffixe non streamé ; sans deltas, c'est la source du
    # contenu de l'outil (mais seulement si l'item a déjà été ouvert, pour
    # connaître son id/call_id).
    if etype == "response.function_call_arguments.done":
        iid = chunk.get("item_id", "")
        out_idx = chunk.get("output_index", 0)
        args = chunk.get("arguments", "")
        name = chunk.get("name", "")
        info = tool_cache.get(iid) or tool_cache.get(f"idx_{out_idx}")
        if iid and iid in tool_cache and name:
            tool_cache[iid]["name"] = name
        if info is None or not isinstance(args, str) or not args:
            return None
        _key = f"idx_{out_idx}"
        seen = args_seen.get(_key, "")
        if args.startswith(seen):
            emit = args[len(seen) :]
        elif not seen:
            emit = args
        else:
            emit = ""
        args_seen[_key] = args
        if not emit:
            return None
        return {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": info["index"], "function": {"arguments": emit}}]},
                    "finish_reason": None,
                }
            ]
        }

    # response.output_item.done — reasoning item final with summary array (fallback + 100% guarantee)
    if etype == "response.output_item.done":
        item = chunk.get("item", {}) if isinstance(chunk.get("item"), dict) else {}
        if isinstance(item, dict) and item.get("type") == "reasoning":
            iid = item.get("id", "") or f"rs_{chunk.get('output_index', 0)}"
            summary = item.get("summary", [])
            # [Correctif B2] Pass-through 200% : même si un index a déjà été
            # streamé en delta (i vu → déjà visible côté client), le fallback
            # intégral doit TOUJOURS remonter la queue perdue (les i NON vus).
            # Pas de early-return ici — la dedup se fait au NIVEAU PART : chaque
            # part non vue est émise ci-dessous, les parts déjà vues sont droppées.
            reasoning = ""
            if isinstance(summary, list):
                for s in summary:
                    if isinstance(s, dict) and s.get("text"):
                        reasoning += s.get("text", "")
                    elif isinstance(s, dict) and s.get("type") == "summary_text":
                        reasoning += s.get("text", "")
            if not reasoning and isinstance(item.get("summary"), dict):
                reasoning = item["summary"].get("text", "")
            # 100% fallback: if no summary text but encrypted_content exists, synthesize placeholder so client always sees thinking
            if not reasoning:
                # vrai seulement : pas de placeholder synthétique — si pas de summary visible, on ne remonte rien (le vrai)
                _debug(
                    f"  [responses-sse] output_item.done no visible summary, skip (vrai seulement) iid={iid!r} encrypted={bool(item.get('encrypted_content'))}"
                )
                return None
            if reasoning:
                # N'émètre QUE les parts non vues (per-index) — évite doublon
                # (cas 1-part où delta déjà vu → on émet rien ; N-parts → on
                # émet seulement les indices jamais vus).
                _unseen = ""
                _seen_any = False
                if iid and isinstance(summary, list) and summary:
                    for i, s in enumerate(summary):
                        if f"{iid}:{i}" not in reasoning_seen:
                            if isinstance(s, dict):
                                _unseen += s.get("text", "") or ""
                    _seen_any = any(f"{iid}:{i}" in reasoning_seen for i in range(len(summary)))
                    for i in range(len(summary)):
                        reasoning_seen.add(f"{iid}:{i}")
                elif iid:
                    if iid in reasoning_seen:
                        return None
                    reasoning_seen.add(iid)
                    _unseen = reasoning
                else:
                    _unseen = reasoning
                # si tout le summary avait déjà été delta-streamé → rien à émettre
                if iid and isinstance(summary, list) and summary and not _unseen:
                    _debug(f"  [responses-sse] output_item.done fully deduped iid={iid!r}")
                    return None
                # cas particulier single-part : le client a déjà le texte complet
                # via delta — on ne renvoie PAS d'intégral redondant (même per-index
                # présent → done fully dedupé ci-dessus l'a déjà absorbé).
                _emit = _unseen if (iid and isinstance(summary, list) and summary and _seen_any) else reasoning
                # mais si le seen_any était seulement partiel (N>1), _emit==_unseen
                # (queue seule) ; si single-part seen → déjà return None ci-dessus.
                _debug(
                    f"  [responses-sse] output_item.done reasoning fallback len={len(_emit)} iid={iid!r} unseen={len(_unseen)} seen_any={_seen_any}"
                )
                return {"choices": [{"delta": {"reasoning_content": _emit}, "finish_reason": None}]}
        return _responses_item_delta(
            item,
            chunk.get("output_index", 0),
            tool_cache,
            tool_index_map,
            text_seen,
            args_seen,
            _tool_name_map,
        )

    # response.completed — final event with usage
    if etype == "response.completed":
        # Clear tool cache + reasoning dedupe for next request
        _clear_state()
        resp = chunk.get("response", {})
        usage = resp.get("usage", {})
        # Cache tokens come from input_tokens_details, NOT output_tokens_details
        _inp_details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
        _cached = _inp_details.get("cached_tokens", 0)
        chat_usage = {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", usage.get("input_tokens", 0) + usage.get("output_tokens", 0)),
        }
        if _cached:
            chat_usage["prompt_tokens_details"] = {"cached_tokens": _cached}
        # [FIX finish_reason] ``response.completed`` est l'événement TERMINAL du
        # protocole Responses : il doit permettre de conclure un tour. Sans raison
        # terminale, l'hôte « synthétisait » un faux ``finish_reason: "stop"``
        # (532 occurrences mesurées) — y compris pour des tours d'OUTIL, qui doivent
        # rapporter ``tool_calls``. On expose la raison réelle sous une clé PRIVÉE
        # ``_finish_reason`` : le contrat public (``choices: []``) est préservé, donc
        # aucun consommateur n'est cassé (l'``usage`` et ``got_response_completed``
        # des handlers restent capturés à l'identique).
        _finish_reason = "stop"
        _output = resp.get("output")
        if isinstance(_output, list):
            for _item in _output:
                if isinstance(_item, dict) and _item.get("type") in (
                    "function_call",
                    "custom_tool_call",
                    "local_shell_call",
                ):
                    _finish_reason = "tool_calls"
                    break
        return {"choices": [], "usage": chat_usage, "_finish_reason": _finish_reason}

    # response.incomplete — model didn't generate output, treat as stream end
    if etype == "response.incomplete":
        _clear_state()
        _debug("  [responses-sse] response.incomplete received — model produced no output")
        resp = chunk.get("response", {})
        usage = resp.get("usage", {}) if isinstance(resp.get("usage"), dict) else {}
        chat_usage = {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", usage.get("input_tokens", 0) + usage.get("output_tokens", 0)),
        }
        return {"choices": [], "usage": chat_usage, "_incomplete": True}

    # All other event types (response.created, response.in_progress,
    # response.output_item.done, response.content_part.added/done, etc.) — skip
    return None


# ── [Lot L5 — A11/A21] Émission incrémentale des événements Responses ──
#
# A21 (confirmé par la spec) : l'ensemble minimal consommé par un client est
# ``response.created`` → ``response.output_text.delta``* → ``response.completed``.
# Nous n'émettions QUE ``response.completed`` — sans même le ``response.created``
# initial. Un client qui attend ``response.created`` avant d'afficher quoi que ce
# soit reste donc bloqué jusqu'à la fin de la génération : la réponse n'apparaît
# pas progressivement, elle apparaît d'un coup à la fin.
#
# Ce module fournit les briques d'émission ; l'appelant (opencode.py) décide du
# moment. Chaque événement porte son ``sequence_number`` (B7), strictement
# croissant dans le stream — un client qui détecte un trou ou un doublon
# réinitialise son état.

# Champs exacts par type d'événement (B7) — rappel de la spec, appliqué par les
# méthodes ci-dessous :
#   - `output_text.delta` : {content_index, delta, item_id, logprobs[], output_index, sequence_number}
#   - `function_call_arguments.delta` : NI `content_index` NI `name`
#   - `reasoning_summary_text.delta` : `summary_index` (pas `content_index`)


class ResponsesStreamEmitter:
    """Construit la séquence d'événements SSE Responses d'une réponse.

    État PAR STREAM (comme ``ResponsesSseState``) : ``sequence_number`` et
    identifiants sont propres à un stream. Deux streams concurrents ne doivent
    jamais partager ces compteurs — c'était le défaut déjà corrigé pour le cache
    d'outils.

    ``output_index`` est **explicite** sur chaque méthode qui le concerne : le
    déduire d'un compteur interne produisait un index faux sur
    ``output_item.done`` (le compteur avait déjà été incrémenté par ``.added``),
    et un client qui corrèle les deux événements par index aurait perdu l'item.
    """

    __slots__ = ("response_id", "model", "sequence", "created_sent")

    def __init__(self, model: str, response_id: str | None = None) -> None:
        self.model = model
        self.response_id = response_id or f"resp_{uuid.uuid4().hex[:24]}"
        self.sequence = 0
        self.created_sent = False

    def _next_seq(self) -> int:
        seq = self.sequence
        self.sequence += 1
        return seq

    def _wrap(self, event_type: str, payload: dict) -> dict:
        return {"type": event_type, **payload, "sequence_number": self._next_seq()}

    # ── Cycle de vie ──

    def created(self, created_at: int | None = None, status: str = "in_progress") -> dict:
        """`response.created` — premier événement, OBLIGATOIRE (A21).

        Un client qui attend cet événement avant d'afficher resterait sinon
        bloqué jusqu'à la fin de la génération : le texte n'apparaît pas
        progressivement, il apparaît d'un bloc à la fin.
        """
        self.created_sent = True
        now = created_at if created_at is not None else int(time.time())
        return self._wrap(
            "response.created",
            {
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "created_at": now,
                    "status": status,
                    "model": self.model,
                    "output": [],
                }
            },
        )

    def in_progress(self) -> dict:
        """`response.in_progress` — transition, juste après ``created``."""
        return self._wrap(
            "response.in_progress",
            {
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "status": "in_progress",
                    "model": self.model,
                    "output": [],
                }
            },
        )

    def output_item_added(self, item: dict, output_index: int) -> dict:
        """`response.output_item.added` — ouvre un item de sortie, **vide**.

        Le contenu ne doit **pas** figurer ici : il arrive par les deltas, puis
        se referme complet sur ``.done``. Un client conforme qui initialise son
        accumulateur avec ``item`` et y ajoute ensuite les deltas obtiendrait le
        contenu **en double** si ``.added`` portait déjà tout le texte — cas
        d'autant plus pernicieux qu'un test qui ne relit que les deltas (la vue
        la plus naturelle) ne le voit jamais.

        C'est aussi la convention déjà supposée par notre propre parseur SSE :
        ``_responses_chunk_to_chat`` émet ``"arguments": ""`` sur l'événement
        ``.added`` d'un ``function_call`` puis accumule les deltas.
        """
        return self._wrap(
            "response.output_item.added",
            {"output_index": output_index, "item": _empty_item_for_added(item, output_index)},
        )

    def output_item_done(self, item: dict, output_index: int) -> dict:
        """`response.output_item.done` — ferme un item.

        C'est à ce moment que l'``encrypted_content`` d'un reasoning item est
        complet (B7) : le lire depuis ``.added`` donne une valeur partielle.
        """
        return self._wrap(
            "response.output_item.done",
            {"output_index": output_index, "item": {**item, "index": output_index}},
        )

    def content_part_added(self, item_id: str, output_index: int, content_index: int = 0) -> dict:
        """`response.content_part.added` — ouvre une part de contenu texte."""
        return self._wrap(
            "response.content_part.added",
            {
                "item_id": item_id,
                "output_index": output_index,
                "content_index": content_index,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        )

    def content_part_done(self, item_id: str, text: str, output_index: int, content_index: int = 0) -> dict:
        """`response.content_part.done` — ferme une part de contenu texte."""
        return self._wrap(
            "response.content_part.done",
            {
                "item_id": item_id,
                "output_index": output_index,
                "content_index": content_index,
                "part": {"type": "output_text", "text": text, "annotations": []},
            },
        )

    # ── Texte ──

    def text_delta(self, delta: str, item_id: str, output_index: int = 0, content_index: int = 0) -> dict:
        """`response.output_text.delta` — un fragment de texte.

        ``logprobs`` est présent (liste vide) : le champ est documenté dans le
        payload, et un client strict qui le lit ne doit pas recevoir ``None``.
        """
        return self._wrap(
            "response.output_text.delta",
            {
                "item_id": item_id,
                "output_index": output_index,
                "content_index": content_index,
                "delta": delta,
                "logprobs": [],
            },
        )

    def text_done(self, text: str, item_id: str, output_index: int = 0, content_index: int = 0) -> dict:
        """`response.output_text.done` — texte complet de la part."""
        return self._wrap(
            "response.output_text.done",
            {
                "item_id": item_id,
                "output_index": output_index,
                "content_index": content_index,
                "text": text,
            },
        )

    # ── Raisonnement ──

    def reasoning_summary_delta(self, delta: str, item_id: str, output_index: int = 0, summary_index: int = 0) -> dict:
        """`response.reasoning_summary_text.delta`.

        B7 : ce type utilise ``summary_index`` — PAS ``content_index``. Émettre
        ``content_index`` ici ferait ignorer le fragment par un client conforme.
        """
        return self._wrap(
            "response.reasoning_summary_text.delta",
            {
                "item_id": item_id,
                "output_index": output_index,
                "summary_index": summary_index,
                "delta": delta,
            },
        )

    # ── Appels d'outils ──

    def function_call_arguments_delta(self, delta: str, item_id: str, output_index: int = 0) -> dict:
        """`response.function_call_arguments.delta`.

        B7 : NI ``content_index`` NI ``name`` dans ce payload — les ajouter
        contredit la spec.
        """
        return self._wrap(
            "response.function_call_arguments.delta",
            {"item_id": item_id, "output_index": output_index, "delta": delta},
        )

    def function_call_arguments_done(self, arguments: str, item_id: str, output_index: int = 0) -> dict:
        """`response.function_call_arguments.done` — arguments complets."""
        return self._wrap(
            "response.function_call_arguments.done",
            {
                "item_id": item_id,
                "output_index": output_index,
                "arguments": arguments,
            },
        )

    # ── Terminaison ──

    def completed(self, response: dict) -> dict:
        """`response.completed` - terminal, porte l'``usage`` final."""
        return self._wrap("response.completed", {"response": response})

    def incomplete(self, response: dict) -> dict:
        """`response.incomplete` - terminal pour max_tokens/refus."""
        return self._wrap("response.incomplete", {"response": response})

    def failed(self, response: dict | None = None, message: str = "") -> dict:
        """`response.failed` — terminal cohérent pour un stream avorté.

        L5 exige qu'un stream en erreur garde un **terminal cohérent** : un
        client qui ne reçoit jamais d'événement terminal laisse sa connexion (et
        son UI) bloquée jusqu'au timeout.
        """
        payload = response or {
            "id": self.response_id,
            "object": "response",
            "status": "failed",
            "model": self.model,
            "output": [],
            "error": {"code": "stream_error", "message": message or "upstream stream failed"},
        }
        return self._wrap("response.failed", {"response": payload})


# Taille des fragments émis quand on rejoue une réponse complète sous forme
# d'événements. Assez petit pour que le client voie une progression réelle
# (l'objectif d'A11 : ne pas tout livrer d'un bloc), assez grand pour ne pas
# multiplier les trames SSE sur une longue réponse.
_RESPONSES_REPLAY_CHUNK = 64


def _empty_item_for_added(item: dict, output_index: int) -> dict:
    """Version **vide** d'un item, pour ``response.output_item.added``.

    Conserve l'identité de l'item (``type``, ``id``/``call_id``, ``name``,
    ``role``, ``status``) — ce qu'un client utilise pour décider *comment*
    accumuler — mais vide le **contenu** (``content``, ``arguments``,
    ``summary``, ``encrypted_content``), livré par les deltas puis complet sur
    ``.done``.

    Vider ``content`` plutôt que de l'omettre : un client strict qui itère
    ``item["content"]`` sans garde ne doit pas recevoir de ``KeyError``, et la
    liste vide est la valeur que la spec documente pour un item qui démarre.
    """
    if not isinstance(item, dict):
        return {"index": output_index}

    empty = {k: v for k, v in item.items() if k not in ("content", "arguments", "summary", "encrypted_content")}
    empty["index"] = output_index

    itype = item.get("type")
    if itype == "message":
        # Une part vide par part d'origine : le nombre de parts (et donc les
        # `content_index` à venir) reste annoncé dès `.added`.
        parts = item.get("content") or []
        empty["content"] = [
            {"type": "output_text", "text": "", "annotations": []}
            if isinstance(p, dict) and p.get("type") in ("output_text", "text")
            else (dict(p) if isinstance(p, dict) else p)
            for p in parts
        ]
    elif itype == "function_call":
        # Convention OpenAI (et de notre propre parseur) : arguments vides.
        empty["arguments"] = ""
    elif itype == "reasoning":
        summary = item.get("summary") or []
        empty["summary"] = [{"type": "summary_text", "text": ""} if isinstance(s, dict) else s for s in summary]

    return empty


def responses_stream_events(
    response: dict,
    model: str,
    emitter: ResponsesStreamEmitter | None = None,
    *,
    skip_lifecycle: bool = False,
    tool_aliases: dict | None = None,
    shell_kind: str = "powershell",
) -> list[dict]:
    """[Lot L5 — A11/A21] Séquence d'événements Responses conforme pour `response`.

    Transforme une réponse Responses **complète** (déjà convertie) en la
    séquence d'événements qu'un client conforme attend :

        response.created → response.in_progress
        → (par item) output_item.added → … deltas … → output_item.done
        → response.completed (avec `usage`)

    L'ensemble minimal documenté (A21) est
    ``response.created`` → ``response.output_text.delta``* → ``response.completed``.
    Nous n'émettions que ``response.completed`` : un client qui attend
    ``response.created`` avant d'afficher restait bloqué, et rien ne s'affichait
    progressivement.

    Les fragments de texte sont découpés en tranches de
    ``_RESPONSES_REPLAY_CHUNK`` caractères : l'objectif est la **progression
    perçue**, pas de simuler un vrai tokenizer. Le contenu final est identique à
    celui de ``response["output"]``, seul le découpage diffère.

    ``usage`` est reporté tel quel sur ``response.completed`` — c'est là que le
    client lit la consommation finale.

    ``skip_lifecycle=True`` n'émet ni ``response.created`` ni
    ``response.in_progress`` : l'appelant les a déjà envoyés et réutilise le
    même émetteur pour ne pas trouer ``sequence_number``.
    """
    emitter = emitter or ResponsesStreamEmitter(model, response.get("id"))
    events: list[dict] = [] if skip_lifecycle else [emitter.created(), emitter.in_progress()]

    for output_index, item in enumerate(response.get("output") or []):
        if not isinstance(item, dict):
            continue
        itype = item.get("type")

        if itype == "reasoning":
            # B7 : le raisonnement ne coule que si `reasoning.summary` est opt-in
            # côté client ; on émet les fragments de résumé disponibles.
            events.append(emitter.output_item_added(item, output_index))
            summary_dir = item.get("summary") or []
            text = "".join(s.get("text", "") for s in summary_dir if isinstance(s, dict) and s.get("text"))
            item_id = item.get("id") or f"rs_{uuid.uuid4().hex[:16]}"
            for start in range(0, len(text), _RESPONSES_REPLAY_CHUNK):
                chunk = text[start : start + _RESPONSES_REPLAY_CHUNK]
                if chunk:
                    events.append(emitter.reasoning_summary_delta(chunk, item_id, output_index))
            # `encrypted_content` n'est complet qu'ici (B7), donc `.done` après
            # tous les fragments.
            events.append(emitter.output_item_done(item, output_index))

        elif itype == "function_call":
            if tool_aliases:
                # [2026-11-12] Nom officiel amont (``bash``) → nom réellement
                # déclaré par le client (``Bash``/``shell``/``exec_command``).
                _adapted_name, _adapted_args = adapt_tool_call(
                    item.get("name", ""),
                    item.get("arguments") or "",
                    tool_aliases,
                    shell_kind=shell_kind,
                )
                _adapted_args = str(_adapted_args or "{}")
                if _adapted_name != item.get("name") or _adapted_args != (item.get("arguments") or ""):
                    item = {**item, "name": _adapted_name, "arguments": _adapted_args}
                    response["output"][output_index] = item
            events.append(emitter.output_item_added(item, output_index))
            item_id = item.get("id") or item.get("call_id") or f"fc_{uuid.uuid4().hex[:16]}"
            args = item.get("arguments") or ""
            for start in range(0, len(args), _RESPONSES_REPLAY_CHUNK):
                chunk = args[start : start + _RESPONSES_REPLAY_CHUNK]
                if chunk:
                    events.append(emitter.function_call_arguments_delta(chunk, item_id, output_index))
            events.append(emitter.function_call_arguments_done(args, item_id, output_index))
            events.append(emitter.output_item_done(item, output_index))

        elif itype == "message":
            events.append(emitter.output_item_added(item, output_index))
            item_id = item.get("id") or f"msg_{uuid.uuid4().hex[:16]}"
            content_index = 0
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") not in ("output_text", "text"):
                    content_index += 1
                    continue
                text = part.get("text", "") or ""
                events.append(emitter.content_part_added(item_id, output_index, content_index))
                for start in range(0, len(text), _RESPONSES_REPLAY_CHUNK):
                    chunk = text[start : start + _RESPONSES_REPLAY_CHUNK]
                    if chunk:
                        events.append(emitter.text_delta(chunk, item_id, output_index, content_index))
                events.append(emitter.text_done(text, item_id, output_index, content_index))
                events.append(emitter.content_part_done(item_id, text, output_index, content_index))
                content_index += 1
            events.append(emitter.output_item_done(item, output_index))

        else:
            # Type inconnu : on le transporte sans le perdre (un item qu'on ne
            # sait pas découper reste livré en un seul bloc).
            events.append(emitter.output_item_added(item, output_index))
            events.append(emitter.output_item_done(item, output_index))

    # [audit 2026-10-30] Do not report a truncated/refused response as
    # completed: clients that key on the terminal event would miss it.
    if str(response.get("status") or "") == "incomplete":
        events.append(emitter.incomplete(response))
    else:
        events.append(emitter.completed(response))
    return events


def responses_stream_sse(events: list[dict]) -> bytes:
    """Sérialise des événements Responses en corps SSE (``data: …`` + ``[DONE]``).

    ``[DONE]`` est conservé en fin de flux : nos clients existants s'en servent
    comme sentinelle de fin, et la spec Responses ne l'interdit pas.
    """
    parts = [f"data: {_json_dumps_str(ev, ensure_ascii=False)}\n\n" for ev in events]
    parts.append("data: [DONE]\n\n")
    return "".join(parts).encode()
