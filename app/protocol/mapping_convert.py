"""
Protocol mapping — conversion Anthropic→OpenAI (cœur) + cache de conversion (hit/miss, LRU bytes).

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

def anthropic_to_openai(body: dict, model: str, raw: bytes | None = None) -> dict:
    # ``raw`` : bytes bruts du client, consommés uniquement par le wrapper de
    # cache (plus bas) — l'implémentation d'origine n'en a pas besoin.
    # Parité OpenCodeClient : filtre des contenus vides côté requête Anthropic
    # (l'amont rejette les contenus vides en 400). Copie défensive, jamais vide.
    body = filter_anthropic_request_body(body)
    thinking = isinstance(body.get("thinking"), dict) and body["thinking"].get("type") in (
        "enabled",
        "adaptive",
    )
    # GLM-5.x models don't support cache_control — skip it
    supports_cache_control = not model.startswith("glm-5")

    messages: list[dict[str, Any]] = []

    # System prompt — always add cache_control for prefix caching
    system_val = body.get("system", "")
    if isinstance(system_val, list):
        text = _extract_text(system_val)
        if text:
            text = _strip_billing_header(text)
            msg: dict[str, Any] = {"role": "system", "content": text}
            if supports_cache_control:
                msg["cache_control"] = _system_cache_control(system_val)
            messages.append(msg)
    elif system_val:
        msg = {"role": "system", "content": _strip_billing_header(system_val)}
        # Always add cache_control to system messages for prefix caching
        if supports_cache_control:
            msg["cache_control"] = {"type": "ephemeral"}
        messages.append(msg)

    for msg in body.get("messages", []):
        role, content = msg["role"], msg.get("content", "")
        is_asst = role == "assistant"

        # Simple string content
        if isinstance(content, str):
            out = {"role": role, "content": content}
            if thinking and is_asst:
                out["reasoning_content"] = " "
            messages.append(out)
            continue

        if not isinstance(content, list):
            continue

        text_parts: list = []
        tool_calls: list = []
        thinking_parts: list = []
        tool_results: list = []
        image_parts: list = []
        last_cache_control = None

        for block in content:
            if isinstance(block, str):
                text_parts.append(block)
                continue
            if not isinstance(block, dict):
                continue

            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
                if "cache_control" in block:
                    last_cache_control = block["cache_control"]
            elif btype == "thinking":
                # [Correctif parité multi-tours — remplace Phase D.2] les blocs
                # SYNTHÉTIQUES (signature locale) voyagent désormais comme les
                # ORIGINAUX : leur texte devient reasoning_content, exactement
                # ce que l'upstream recevrait sans le proxy. Les signatures ne
                # transitent jamais vers openai-compatible (seul le texte).
                thinking_parts.append(block.get("thinking", ""))
            elif btype == "redacted_thinking":
                # [Lot H3 — remplace Phase D.4] donnée chiffrée authentique :
                # préservée telle quelle vers Anthropic (passthrough). Vers les
                # autres upstreams le bloc ne peut pas transiter (non déchiffrable,
                # non interprétable) mais il est CONSERVÉ dans
                # _redacted_thinking_cache au lieu d'être perdu définitivement.
                _rt_data = block.get("data", "")
                if isinstance(_rt_data, str) and _rt_data:
                    _rt_key = hashlib.sha256(_rt_data.encode("utf-8", "ignore")).hexdigest()
                    _redacted_thinking_cache[_rt_key] = block
                    _redacted_thinking_cache.move_to_end(_rt_key)
                    if len(_redacted_thinking_cache) > _REDACTED_THINKING_CACHE_MAX:
                        _redacted_thinking_cache.popitem(last=False)
                _debug(
                    "  [convert] CACHE redacted_thinking → upstream non-Anthropic "
                    "(conservé pour réinjection vers Anthropic)"
                )
                continue
            elif btype == "image":
                src = block.get("source", {})
                if not isinstance(src, dict):
                    continue
                stype = src.get("type", "")
                if stype == "base64":
                    media_type = src.get("media_type", "image/png")
                    data = src.get("data", "")
                    if not data:
                        continue
                    url = f"data:{media_type};base64,{data}"
                elif stype == "url":
                    url = src.get("url", "")
                    if not url:
                        continue
                else:
                    # type "file" ou inconnu → pas de fidélité OpenAI :
                    # placeholder honnête + debug, jamais de drop silencieux.
                    _debug(f"  [convert] DROP image source type={stype!r} → no OpenAI fidelity")
                    text_parts.append(f"[image:{stype or 'unknown'}]")
                    continue
                image_parts.append({"type": "image_url", "image_url": {"url": url}})
            elif btype == "document":
                src = block.get("source", {})
                if not isinstance(src, dict):
                    continue
                stype = src.get("type", "")
                if stype == "base64" and src.get("data"):
                    media_type = src.get("media_type", "application/pdf")
                    image_parts.append(
                        {
                            "type": "file",
                            "file": {
                                "file_data": f"data:{media_type};base64,{src['data']}",
                                # [A25] Le champ client est ``title`` côté
                                # Anthropic (``DocumentBlockParam`` : source,
                                # type, cache_control, citations, context,
                                # title) — PAS ``name``. Lire ``name`` seul
                                # faisait donc TOUJOURS retomber sur le défaut :
                                # le nom de fichier du client était perdu en
                                # silence (``rapport.pdf`` → ``document.pdf``).
                                # ``name`` reste lu en second pour ne pas casser
                                # les corps non conformes déjà acceptés.
                                "filename": block.get("title") or block.get("name") or "document.pdf",
                            },
                        }
                    )
                elif stype == "url" and src.get("url"):
                    # Chat Completions n'a pas de part file-par-URL : un
                    # file_data=url serait un data URI mensonger → placeholder
                    # honnête + debug, jamais de faux octets.
                    _debug(f"  [convert] DROP document url {src['url']!r} → Chat exige file_data base64 ou file_id")
                    text_parts.append(f"[document:url:{src['url']}]")
                elif stype == "file" and src.get("file_id"):
                    image_parts.append(
                        {
                            "type": "file",
                            "file": {"file_id": src["file_id"]},
                        }
                    )
                elif stype == "text" and src.get("text"):
                    import base64 as _b64mod

                    _raw = src["text"].encode("utf-8", "ignore")
                    _enc = _b64mod.b64encode(_raw).decode("ascii")
                    image_parts.append(
                        {
                            "type": "file",
                            "file": {
                                "file_data": f"data:text/plain;base64,{_enc}",
                                # [A25] même défaut que la branche base64 ci-dessus :
                                # le champ client est ``title``, pas ``name``.
                                "filename": block.get("title") or block.get("name") or "document.txt",
                            },
                        }
                    )
                else:
                    _debug(f"  [convert] DROP document source type={stype!r} → no fidelity")
                    text_parts.append(f"[document:{stype or 'unknown'}]")
                    continue
            elif btype == "tool_use":
                _tool_name = block.get("name", "")
                if not isinstance(_tool_name, str) or not _tool_name.strip():
                    _debug(f"  [convert] SKIP tool_use with empty name id={block.get('id', '?')}")
                    continue
                tool_calls.append(
                    {
                        "id": _tool_id_or_stable(
                            block.get("id"),
                            "call_",
                            _tool_name.strip(),
                            # Position du tool_use DANS ce message : déterministe
                            # (même body → même id) et unique (deux appels
                            # identiques du même outil reçoivent 0 puis 1).
                            len(tool_calls),
                        ),
                        "type": "function",
                        "function": {
                            "name": _tool_name.strip(),
                            "arguments": _json_dumps_str(block.get("input", {})),
                        },
                    }
                )
            elif btype == "tool_result":
                tid = block.get("tool_use_id", "")
                if not tid:
                    # Defensive: skip tool_result with missing/empty tool_use_id
                    # (can happen after context compaction loses the id)
                    _debug("  [compact] SKIP tool_result with missing tool_use_id in anthropic_to_openai")
                    continue
                # tool_result multimodal : texte + images préservés en
                # content-list OpenAI (la Responses API accepte input_text
                # + input_image dans function_call_output.output).
                _tr_texts: list[str] = []
                _tr_images: list[dict] = []
                _tr_raw = block.get("content", "")
                _tr_blocks = _tr_raw if isinstance(_tr_raw, list) else [_tr_raw]
                for _tr_b in _tr_blocks:
                    if isinstance(_tr_b, str):
                        if _tr_b:
                            _tr_texts.append(_tr_b)
                        continue
                    if not isinstance(_tr_b, dict):
                        continue
                    _tr_t = _tr_b.get("type", "")
                    if _tr_t in ("text", "thinking"):
                        _tr_texts.append(_tr_b.get("text" if _tr_t == "text" else "thinking", ""))
                    elif _tr_t == "image":
                        _tr_src = _tr_b.get("source", {})
                        if not isinstance(_tr_src, dict):
                            continue
                        _tr_st = _tr_src.get("type", "")
                        if _tr_st == "base64" and _tr_src.get("data"):
                            _tr_images.append(
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{_tr_src.get('media_type', 'image/png')};base64,{_tr_src['data']}"
                                    },
                                }
                            )
                        elif _tr_st == "url" and _tr_src.get("url"):
                            _tr_images.append(
                                {
                                    "type": "image_url",
                                    "image_url": {"url": _tr_src["url"]},
                                }
                            )
                        else:
                            _debug(f"  [convert] DROP tool_result image source type={_tr_st!r} → no fidelity")
                            _tr_texts.append(f"[image:{_tr_st or 'unknown'}]")
                    elif _tr_t == "document":
                        _tr_dsrc = _tr_b.get("source", {})
                        if not isinstance(_tr_dsrc, dict):
                            continue
                        _tr_dst = _tr_dsrc.get("type", "")
                        if _tr_dst == "base64" and _tr_dsrc.get("data"):
                            _tr_images.append(
                                {
                                    "type": "file",
                                    "file": {
                                        "file_data": f"data:{_tr_dsrc.get('media_type', 'application/pdf')};base64,{_tr_dsrc['data']}",
                                        # [A25] champ client ``title`` (cf. branche
                                        # document homologue plus haut).
                                        "filename": _tr_b.get("title") or _tr_b.get("name") or "document.pdf",
                                    },
                                }
                            )
                        elif _tr_dst == "file" and _tr_dsrc.get("file_id"):
                            _tr_images.append({"type": "file", "file": {"file_id": _tr_dsrc["file_id"]}})
                        else:
                            # url / text / inconnu : pas de part file-par-URL en
                            # Chat → placeholder honnête, jamais de faux octets.
                            _debug(f"  [convert] DROP tool_result document source type={_tr_dst!r} → no Chat fidelity")
                            _tr_texts.append(f"[document:{_tr_dst or 'unknown'}]")
                    elif _tr_t == "input_audio":
                        _debug("  [convert] DROP tool_result audio → no Chat tool fidelity (placeholder)")
                        _tr_texts.append("[audio:unsupported-in-chat-tool-result]")
                    else:
                        _debug(f"  [convert] DROP tool_result block type={_tr_t!r} → placeholder")
                        _tr_texts.append(f"[{_tr_t or 'unknown'}]")
                _tr_content: str | list = "\n".join(_tr_texts)
                if _tr_images:
                    _tr_list: list[dict] = []
                    if _tr_content:
                        _tr_list.append({"type": "text", "text": _tr_content})
                    _tr_list.extend(_tr_images)
                    _tr_content = _tr_list
                # Parité officielle : `is_error: true` doit survivre à la conversion.
                # Chat n'a pas de flag d'erreur — on préfixe le contenu pour que
                # l'amont voie l'échec au lieu d'un succès silencieux qui casse
                # la boucle agentique (l'agent croit que l'outil a réussi).
                try:
                    _is_err = block.get("is_error") is True
                except Exception:
                    _is_err = False
                if _is_err:
                    if isinstance(_tr_content, str):
                        _tr_content = "[tool_error] " + _tr_content if _tr_content else "[tool_error]"
                    elif isinstance(_tr_content, list):
                        _tr_content = [{"type": "text", "text": "[tool_error]"}] + _tr_content
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": _tr_content,
                    }
                )
                if "cache_control" in block:
                    last_cache_control = block["cache_control"]
            else:
                # Parité officielle : jamais de drop silencieux. Les blocs serveur
                # (server_tool_use, web_search_tool_result, code_execution,
                # mcp_tool_use/result, container_upload, search_result, etc.)
                # n'ont pas d'équivalent Chat : on les matérialise en texte pour
                # que le contexte agentique survive, au lieu de les perdre et de
                # forcer un `continue` manuel sur un tour amnésique.
                try:
                    _ubt = block.get("type", "?") if isinstance(block, dict) else type(block).__name__
                except Exception:
                    _ubt = "?"
                _debug(f"  [convert] server/block type={_ubt!r} sans équivalent Chat → placeholder texte")
                try:
                    _ph = (
                        _json_dumps_str(block, ensure_ascii=False)[:2000]
                        if isinstance(block, dict)
                        else str(block)[:2000]
                    )
                except Exception:
                    _ph = str(_ubt)
                text_parts.append(f"[{_ubt}: {_ph}]")

        # Emit tool_result messages first (must immediately follow assistant's tool_calls)
        messages.extend(tool_results)

        # Then emit the main message (text + tool_calls + thinking + images)
        joined_thinking = "\n".join(thinking_parts) if thinking_parts else ""

        # Si images présentes → content en liste mixte OpenAI (text + image_url)
        if image_parts:
            content_list: list[dict] = []
            joined_text = "\n".join(text_parts) if text_parts else ""
            if joined_text:
                content_list.append({"type": "text", "text": joined_text})
            content_list.extend(image_parts)
            if tool_calls:
                out = {
                    "role": role,
                    "content": content_list,
                    "tool_calls": tool_calls,
                }
                if joined_thinking:
                    out["reasoning_content"] = joined_thinking
                elif thinking and is_asst:
                    out["reasoning_content"] = " "
                if last_cache_control and not is_asst and supports_cache_control:
                    out["cache_control"] = last_cache_control
                messages.append(out)
            elif content_list or thinking_parts or (thinking and is_asst):
                # Pas de tool_calls mais images (+ éventuellement texte)
                if len(content_list) == 1 and content_list[0].get("type") == "text":
                    # Seul du texte → garder le format string simple (compat)
                    out = {"role": role, "content": content_list[0]["text"]}
                else:
                    out = {"role": role, "content": content_list}
                if joined_thinking:
                    out["reasoning_content"] = joined_thinking
                elif thinking and is_asst:
                    out["reasoning_content"] = " "
                if last_cache_control and not is_asst and supports_cache_control:
                    out["cache_control"] = last_cache_control
                messages.append(out)
        elif tool_calls:
            out = {
                "role": role,
                "content": "\n".join(text_parts) if text_parts else "",
                "tool_calls": tool_calls,
            }
            if joined_thinking:
                out["reasoning_content"] = joined_thinking
            elif thinking and is_asst:
                out["reasoning_content"] = " "
            if last_cache_control and not is_asst and supports_cache_control:
                out["cache_control"] = last_cache_control
            messages.append(out)
        elif text_parts or thinking_parts or (thinking and is_asst):
            out = {"role": role, "content": "\n".join(text_parts) if text_parts else ""}
            if joined_thinking:
                out["reasoning_content"] = joined_thinking
            elif thinking and is_asst:
                out["reasoning_content"] = " "
            if last_cache_control and not is_asst and supports_cache_control:
                out["cache_control"] = last_cache_control
            messages.append(out)

    # ── Orphan filter: drop role:tool without preceding tool_calls id ──
    # (désactivé si chaînage serveur : l'appariement vit côté upstream)
    messages = _drop_orphan_tool_messages(messages, body)

    # Add cache_control to the last user message for optimal prefix caching
    # (Anthropic best practice: cache system + last user turn).
    # DIVERGENCE ASSUMÉE vs OpenCodeClient (applyCaching : système + 2 derniers
    # tours) : le contrat est verrouillé par
    # test_breakpoint_injection_is_bounded_and_under_the_anthropic_limit (≤2
    # breakpoints injectés, budget 4 préservé pour les breakpoints clients).
    if supports_cache_control:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                # [Lot L11 — B3] On n'ÉCRASE pas un breakpoint client : le TTL
                # explicite d'un client (`{"ttl":"1h"}`) doit survivre à notre
                # ajout, et il ne doit pas consommer deux fois le quota de 4.
                messages[i].setdefault("cache_control", {"type": "ephemeral"})
                _cache_control_to_openai_breakpoint(messages[i])
                break

    # Build request
    oai = {
        "model": model,
        "messages": messages,
        "stream": body.get("stream", False),
    }
    # Parité officielle : en streaming, demander l'usage réel à l'amont.
    # Sans `stream_options.include_usage`, l'amont Chat n'envoie jamais `usage`
    # et le client SSE garde l'estimation locale pour toujours (compaction
    # faussée → `continue` manuel). L'officiel reçoit toujours le vrai usage.
    if oai.get("stream") is True:
        try:
            _so = oai.get("stream_options")
            if not isinstance(_so, dict):
                oai["stream_options"] = {"include_usage": True}
            else:
                _so.setdefault("include_usage", True)
        except Exception:
            pass
    # [Lot L14 — B2/A17] Forme du champ de limite adaptée à l'upstream :
    # `max_tokens` par défaut (mesuré 1.19.0 : le client émet `max_tokens`
    # = 32000) ; `max_completion_tokens` vers o-series/gpt-5 uniquement.
    _set_output_token_limit(oai, body, model)

    for key, oai_key in [
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stop_sequences", "stop"),
    ]:
        if key in body:
            oai[oai_key] = body[key]

    # [Lot L4 — A8] Map de renommage des outils pour CETTE conversion.
    # Initialisée AVANT le bloc conditionnel ``tools`` : la référencer plus bas
    # (marqueur de retour) sur un chemin sans outils lèverait un UnboundLocalError.
    _chat_name_map: dict = {}
    if "tools" in body:
        # Support both Anthropic format (name at top level) and OpenAI format (function.name)
        # v3.3: preserve server tools (web_search/web_fetch) natively, fix else branch B4
        oai_tools = []

        def _carry_cc(dst: dict, src: dict) -> dict:
            """[Lot L3 — A5] Reporte le breakpoint de cache d'un outil.

            La boucle reconstruisait chaque outil sans reporter ``cache_control`` :
            un client posant un breakpoint sur un outil le perdait
            silencieusement, alors que le même breakpoint posé sur un message est
            bien reporté. On aligne les deux comportements — la perte silencieuse
            est le seul vrai défaut ici (§9.3 : l'amont OpenAI ignore ce champ,
            il n'est donc pas nuisible de le transporter).

            [Lot L11 — B3] Il n'y a PAS d'équivalent OpenAI émis ici, et c'est une
            décision, pas un oubli : ``_cache_control_to_openai_breakpoint`` est un
            no-op assumé (voir sa docstring). B3 place ``prompt_cache_breakpoint``
            sur les **content parts**, jamais sur une définition d'outil — or c'est
            une définition d'outil qu'on traite ici. Inventer le champ à ce niveau
            risquerait un 400 chez un amont strict pour un gain nul. Seul
            ``cache_control`` est donc transporté, ce qui suffit aux amonts
            Anthropic-compatibles ; la traduction par part est rattachée à L15.
            """
            cc = src.get("cache_control") if isinstance(src, dict) else None
            if cc:
                dst["cache_control"] = cc
                _cache_control_to_openai_breakpoint(dst)
            return dst

        for t in body["tools"]:
            # Server tools: preserve natively if type is web_*
            t_type = t.get("type", "")
            if isinstance(t_type, str) and t_type.startswith("web_"):
                # Keep server tool as-is (e.g., web_search_2025_03_05)
                # If converting to OpenAI and target is anthropic-native, preserve; otherwise keep type
                if "name" in t and t.get("name"):
                    oai_tools.append(
                        _carry_cc(
                            {
                                "type": t_type,
                                "name": t.get("name"),
                                "description": t.get("description", ""),
                                "input_schema": t.get("input_schema", {}),
                            },
                            t,
                        )
                    )
                else:
                    # type without name, e.g., {"type":"web_search_2025_03_05"} -> keep
                    oai_tools.append(_carry_cc({"type": t_type, "name": t.get("name", "web_search")}, t))
                continue
            if "name" in t:
                # Anthropic format: {"name": "...", "description": "...", "input_schema": {...}}
                params = _normalize_tool_schema(t.get("input_schema", {}) or {}, model)
                oai_tools.append(
                    _carry_cc(
                        {
                            "type": "function",
                            "function": {
                                "name": t["name"],
                                "description": t.get("description", ""),
                                "parameters": params,
                            },
                        },
                        t,
                    )
                )
            elif "function" in t:
                # OpenAI format: {"type": "function", "function": {"name": "...", ...}}
                fn = t["function"]
                params = _normalize_tool_schema(fn.get("parameters", {}) or {}, model)
                oai_tools.append(
                    _carry_cc(
                        {
                            "type": "function",
                            "function": {
                                "name": fn.get("name", ""),
                                "description": fn.get("description", ""),
                                "parameters": params,
                            },
                        },
                        t,
                    )
                )
            else:
                # Unknown format: B4 fix - don't produce {"name":""}; check if web_* type without name
                if isinstance(t_type, str) and t_type.startswith("web_"):
                    oai_tools.append(_carry_cc({"type": t_type, "name": t.get("name", "web_search")}, t))
                elif t.get("name"):
                    params = _normalize_tool_schema(t.get("input_schema", t.get("parameters", {})) or {}, model)
                    oai_tools.append(
                        _carry_cc(
                            {
                                "type": "function",
                                "function": {
                                    "name": t.get("name", ""),
                                    "description": t.get("description", ""),
                                    "parameters": params,
                                },
                            },
                            t,
                        )
                    )
                else:
                    # skip invalid tool without name
                    _debug(f"  [convert] SKIP tool without name type={t_type!r}")
                    continue
        if oai_tools:
            # [Lot L4 — A8] Sanitize vers la limite Chat (64) : sans cela un nom
            # Anthropic valide (>64, jusqu'à 200) partait tel quel et l'amont
            # Chat répondait 400. La map repart au client via _TOOL_NAME_MAP_KEY.
            oai["tools"] = _sanitize_chat_tools(oai_tools, _chat_name_map)
        # [PLAN E1 2026-10-12] L'officiel n'envoie `tool_choice` que si le
        # client en a posé un : émission conditionnée à la présence de la clé.
        # `disable_parallel_tool_use` continue de fonctionner seul (lu sur la
        # valeur brute, pas sur le défaut). Repli `protocol.emit_default_tool_choice`
        # (défaut `false`) pour les gateways qui changent de comportement sans le champ.
        _raw_tc = body.get("tool_choice")
        _emit_default_tc = bool(yaml_get("protocol", "emit_default_tool_choice", False))
        tc = _raw_tc if _raw_tc is not None else ("auto" if _emit_default_tc else None)
        # Parité officielle : `disable_parallel_tool_use: true` → `parallel_tool_calls: false`.
        # Sans ça, un client qui interdit le parallèle voit l'amont appeler 2-3 outils
        # d'un coup, ce qui casse la boucle agentique (tour invalide → `continue` manuel).
        _no_parallel = False
        if isinstance(_raw_tc, dict) and _raw_tc.get("disable_parallel_tool_use") is True:
            _no_parallel = True
        if tc is None:
            pass  # [PLAN E1] pas de clé client, pas de défaut : champ absent.
        elif isinstance(tc, dict):
            tc_type = tc.get("type", "auto")
            if tc_type == "tool":
                oai["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}
            elif tc_type == "any":
                oai["tool_choice"] = "required"
            elif tc_type == "none":
                # `none` ≠ `auto` : interdiction d'outil, jamais choix libre.
                oai["tool_choice"] = "none"
            elif tc_type == "function" and isinstance(tc.get("function"), dict):
                # Forme Chat déjà : passthrough (le rename suit plus bas).
                oai["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tc["function"].get("name", "")},
                }
            elif tc_type == "auto":
                oai["tool_choice"] = "auto"
            else:
                _debug(f"  [convert] tool_choice Anthropic inconnu {tc_type!r} → auto")
                oai["tool_choice"] = "auto"
        elif isinstance(tc, str):
            # `any` n'existe pas côté Chat → `required` (équivalent officiel).
            # `required` (forme Chat) passe tel quel — sinon il tombait en `auto`.
            oai["tool_choice"] = {"auto": "auto", "any": "required", "none": "none", "required": "required"}.get(
                tc, "auto"
            )
            if oai["tool_choice"] == "auto" and tc != "auto":
                _debug(f"  [convert] tool_choice string inconnu {tc!r} → auto")
        else:
            oai["tool_choice"] = "auto"
        if _no_parallel:
            oai["parallel_tool_calls"] = False
        # [Lot L4 — A8] Le tool_choice nommé doit désigner le nom RÉELLEMENT
        # envoyé, sinon l'amont cherche un outil inexistant (400/422).
        if isinstance(oai.get("tool_choice"), dict):
            oai["tool_choice"] = _remap_chat_tool_choice(oai["tool_choice"], _chat_name_map)

    # Parité SDK : metadata/user/service_tier valides côté Chat — hors du
    # bloc tools (ils existent aussi sans outils).
    _relay_chat_optional_fields(oai, body)

    # [Lot L4 — A8] Historique : les tool_calls des tours précédents suivent le
    # même rename que tools[] (hors ce bloc : l'historique peut porter des noms
    # même quand cette requête n'envoie aucun outil — short défensif).
    _chat_hist = oai.get("messages")
    if isinstance(_chat_hist, list) and _chat_hist:
        _remap_chat_history_names(_chat_hist, _chat_name_map)

    # Convert Anthropic thinking/effort → OpenAI reasoning parameters.
    # [Lot L2] SOURCE UNIQUE : ``config.effort_policy.resolve_effort`` lit toutes
    # les formes clientes (``output_config.effort`` [A13], ``effort``,
    # ``reasoning_effort`` relais P6, ``thinking.type``/``budget_tokens``) et
    # applique le plafond modèle de la config. Plus de table locale : un
    # ``effort: high`` produit le MÊME résultat sur les 6 chemins (A1).
    _decision = _resolve_effort(body, model)
    if _decision.wants and _decision.level:
        oai["reasoning_effort"] = _decision.level
        _debug(
            f"  [thinking] {model}: reasoning_effort={_decision.level} "
            f"(source={_decision.source}, explicit={_decision.explicit})"
        )

    # ── Parité OpenCodeClient (normalizeMessages / sampling) ──
    # Post-traitement du corps converti, AVANT la clôture cache pour que le
    # plafond 4 couvre aussi les breakpoints ajoutés. No-op sur les entrées
    # déjà propres (sanitize/scrub ne touchent que l'invalide).
    try:
        if not _PARITY_DISABLED:
            _apply_opencode_client_parity(oai, body, model)
    except Exception:
        pass

    # ── [Lot L11 — A20] Clôture cache : top-level + plafond 4 ──
    # L'ancien retour anticipé sur la source ``reasoning_effort`` est supprimé :
    # il court-circuitait cette clôture, donc un client relayant
    # ``reasoning_effort`` échappait au plafond de breakpoints ET à la
    # réécriture de cache. Le seul effet visé (« ne pas restructurer deux fois »)
    # est conservé par le fait qu'on ne restructure plus qu'une fois ici.
    def _close_cache(out: dict) -> dict:
        _apply_top_level_cache_control(out, body)
        _enforce_cache_breakpoint_limit(out)
        return out

    # ── [Lot L13 — B1] Marqueur de repli « reasoning_content » ──
    # ``reasoning_content`` n'est dans AUCUNE spec OpenAI : c'est une convention
    # vendeur (DeepSeek/GLM/Kimi…). Un upstream STRICT peut donc le rejeter en
    # 400/422. On pose ici un marqueur interne (jamais sérialisé sur le wire,
    # cf. ``_serialize_body``) pour que le handler puisse rejouer UNE fois la
    # requête sans le champ, au lieu de casser le tour entier (texte + tool
    # calls) pour tous les clients routés sur cet endpoint. Même mécanisme que
    # le retry-once des items ``reasoning`` de ``/responses``.
    # Le raisonnement est un ENRICHISSEMENT, jamais un bloquant : on dégrade
    # (perte de la mémoire du raisonnement) plutôt que d'échouer.
    # [audit 2026-10-30] _restructure_for_cache adds a system breakpoint
    # AFTER _close_cache enforced the 4-breakpoint cap. Re-enforce after.
    _final = _enforce_cache_breakpoint_limit(_restructure_for_cache(_close_cache(oai), model))
    # Parité officielle : jamais de drop silencieux des champs top-level sans
    # équivalent Chat. Un seul `_debug` groupé (pas de bruit par requête OK).
    try:
        _ignored = [
            k
            for k in (
                "top_k",
                "container",
                "mcp_servers",
                "context_management",
                "inference_geo",
                "diagnostics",
            )
            if k in body
        ]
        _oc = body.get("output_config") if isinstance(body.get("output_config"), dict) else None
        if isinstance(_oc, dict) and any(k in _oc for k in ("format", "task_budget")):
            _ignored.append("output_config.format/task_budget")
        if _ignored:
            _debug(f"  [convert] top-level sans équivalent Chat ignoré: {','.join(_ignored)}")
    except Exception:
        pass
    if isinstance(_final, dict) and any(
        isinstance(_m, dict) and _m.get("reasoning_content")
        for _m in _final.get("messages", [])
    ):  # fmt: skip
        _final[_HAS_SYNTHETIC_REASONING_KEY] = True
    # [Lot L4 — A8] Map de restauration aller→client, transportée par requête
    # (clé privée, jamais globale) ; stripée au dernier kilomètre par
    # ``_serialize_json_body`` — l'amont ne la voit jamais. Posée seulement si
    # un renommage a eu lieu (sinon aucune restauration n'est nécessaire).
    if isinstance(_final, dict) and _chat_name_map:
        _final[_TOOL_NAME_MAP_KEY] = _chat_name_map
    return _final


_orig_anthropic_to_openai = anthropic_to_openai
# [P4] LRU borné : OrderedDict move-to-end + drop-oldest (l'ancien dict
# gelerait le contenu à 512 entrées — plus aucun nouveau body mis en cache).
_anthropic_cache: OrderedDict = OrderedDict()
_anthropic_cache_max = 512
# [audit 2026-10-30] Bound by BYTES too: 512 entries x 10 MB bodies (images/
# documents base64) could retain ~5 GB. 64 MB cap keeps the RSS bounded.
_anthropic_cache_bytes = 0
_anthropic_cache_max_bytes = 64 * 1024 * 1024

# [plan Lot 0] compteurs hit-rate exposition /metrics (fail-soft, zéro lock :
# incréments GIL-atomiques suffisent pour de l'observabilité).
_conversion_hits = 0
_conversion_misses = 0


def conversion_cache_stats() -> dict:
    """Snapshot des compteurs hit/miss du cache de conversion (Lot 0)."""
    hit = _conversion_hits
    miss = _conversion_misses
    total = hit + miss
    return {
        "hit": hit,
        "miss": miss,
        "hit_rate": (hit / total) if total else 0.0,
        "size": len(_anthropic_cache),
        "bytes": _anthropic_cache_bytes,
        "max_bytes": _anthropic_cache_max_bytes,
    }


def _conversion_epoch() -> int:
    """[P4 correctesse] version de routage mélangée à la clé de conversion :
    tout hot-reload touchant les règles (ROUTE_VERSION++ via save_env /
    save_custom_routes / reload mtime) rend les entrées précédentes
    introuvables — fini la staleness permanente après rechargement."""
    try:
        return int(_cfg_settings.ROUTE_VERSION)
    except Exception:
        return 0


def _anthropic_cache_key(model: str, body: dict, raw: bytes | None = None) -> str:
    """[C1 perf/correctesse] clé de cache blake2b(epoch ‖ body_bytes ‖ model).

    Remplace ``hash(json.dumps(body))`` : plus de dumps complet par requête
    quand les bytes bruts sont disponibles chez l'appelant, et surtout plus
    de collisions ``hash()`` Python (randomisé + 64-bit collisionnable →
    une MAUVAISE conversion pouvait être servie). blake2b-128 : collision
    pratiquement impossible ; le model est mélangé séparément (séparateur
    nul) pour éviter toute ambiguïté de concaténation.
    """
    h = hashlib.blake2b(digest_size=16)
    h.update(f"{_conversion_epoch()}\x00".encode("ascii", "replace"))
    if raw is not None:
        h.update(raw)
    else:
        h.update(_json_dumps(body))
    h.update(b"\x00")
    h.update(model.encode("utf-8", "replace"))
    return h.hexdigest()


def anthropic_to_openai(body: dict, model: str, raw: bytes | None = None) -> dict:  # type: ignore[no-redef]
    # ^ [P4] wrapper de cache volontairement rebaptisé du même nom que
    # l'implémentation d'origine (ligne ~308) — pattern décorateur manuel ;
    # l'originale reste joignable via _orig_anthropic_to_openai.
    global _conversion_hits, _conversion_misses
    try:
        # B2c: invalidate cache if body contains role None (poison)
        for _m in body.get("messages", []) or []:
            if isinstance(_m, dict) and _m.get("role") is None:
                _debug("  [cache] skip cache — role None detected")
                return _orig_anthropic_to_openai(body, model)
        key = _anthropic_cache_key(model, body, raw)
        hit = _anthropic_cache.get(key)
        if hit is not None:
            # [P4] LRU : le hit rafraîchit la position (move-to-end).
            _anthropic_cache.move_to_end(key)
            _conversion_hits += 1
            # [C1] shallow copy TOP-LEVEL uniquement : audit plan §3 — les
            # mutations post-conversion des callers touchent des clés racine
            # (model / stream_options / min_tokens), jamais les structures
            # imbriquées partagées. Fini les deepcopy hit ET miss.
            return dict(hit)
        _conversion_misses += 1
        res = _orig_anthropic_to_openai(body, model)
        # Objet stocké JAMAIS exposé tel quel (le caller reçoit une copie
        # racine) → le cache reste pristine sans deepcopy.
        _anthropic_cache[key] = res
        while len(_anthropic_cache) > _anthropic_cache_max:
            _anthropic_cache.popitem(last=False)  # drop-oldest
        return dict(res)
    except Exception:
        return _orig_anthropic_to_openai(body, model)


