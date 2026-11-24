"""
Protocol mapping — construction des requêtes et réponses Responses ↔ Chat/Anthropic.

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

def _chat_to_responses_request(chat: dict) -> dict:
    if "input" in chat and "messages" not in chat:
        # Verbatim natif Responses : sanitize quand même (tools + historique
        # + tool_choice, P0-1/P0-2), sinon fuite sur la jambe payée.
        return _sanitize_native_responses_request(dict(chat))
    inp = []
    _has_reasoning_items = False
    # Parité SDK (systemMessageMode='developer' pour les modèles de raisonnement) :
    # le system Chat devient un item developer côté Responses, jamais system.
    _is_reas = _is_responses_reasoning_model(chat.get("model", ""))
    for m in chat.get("messages", []) or []:
        role = m.get("role", "user")
        if role == "system" and _is_reas:
            role = "developer"
        content = m.get("content", "")
        # Preserve cache_control from the chat message for prefix caching
        cache_ctrl = m.get("cache_control")
        # Tool results must be function_call_output only — never a "role": "tool" input_text (invalid for Responses).
        # output accepte str ou liste de parts (input_text/input_image) :
        # content-list multimodale préservée au lieu d'être str()-ifiée.
        if role == "tool":
            cid = m.get("tool_call_id", "")
            if not cid:
                continue
            if isinstance(content, str):
                _tool_out: str | list = content
            elif isinstance(content, list):
                _tool_out_parts: list[dict] = []
                for _tb in content:
                    if not isinstance(_tb, dict):
                        continue
                    if _tb.get("type") == "text" and _tb.get("text"):
                        _tool_out_parts.append({"type": "input_text", "text": _tb["text"]})
                    elif _tb.get("type") == "image_url":
                        _turl = (_tb.get("image_url") or {}).get("url", "")
                        if not _turl:
                            continue
                        # Schéma Responses : input_image = image_url (URL https
                        # ou data URI base64 tel quel) ou file_id — jamais
                        # image_base64/mime_type (400 upstream sinon).
                        _tool_out_parts.append({"type": "input_image", "image_url": _turl})
                    elif _tb.get("type") == "file":
                        # mypy : `_tb.get("file")` répété ne se narrow pas ; on
                        # passe par un temporaire annoté `dict` pour que les
                        # accès ci-dessous soient typés (union-attr/index).
                        _raw_tf = _tb.get("file")
                        _tf: dict = _raw_tf if isinstance(_raw_tf, dict) else {}
                        if _tf.get("file_id"):
                            _tool_out_parts.append({"type": "input_file", "file_id": _tf["file_id"]})
                        elif isinstance(_tf.get("file_data"), str) and _tf["file_data"]:
                            _tfd = _tf["file_data"]
                            if _tfd.startswith("http://") or _tfd.startswith("https://"):
                                _tfp: dict = {"type": "input_file", "file_url": _tfd}
                                if _tf.get("filename"):
                                    _tfp["filename"] = _tf["filename"]
                                _tool_out_parts.append(_tfp)
                            else:
                                _tfp2: dict = {
                                    "type": "input_file",
                                    "file_data": _tfd,
                                    "filename": _tf.get("filename") or "document.pdf",
                                }
                                _tool_out_parts.append(_tfp2)
                    elif _tb.get("type") == "input_audio":
                        # fco n'a pas de fidélité audio Chat : placeholder.
                        _debug("  [convert] DROP tool input_audio → fco sans audio (placeholder)")
                        _tool_out_parts.append(
                            {"type": "input_text", "text": "[audio:unsupported-in-chat-tool-result]"}
                        )
                _tool_out = _tool_out_parts or ""
            else:
                _tool_out = str(content)
            inp.append(
                {
                    "type": "function_call_output",
                    "call_id": cid,
                    "output": _tool_out,
                }
            )
            continue
        # [Correctif parité multi-tours] raisonnement du tour précédent :
        # re-émis comme item reasoning plaine-texte, même représentation que
        # celle que le proxy produit dans SES réponses Responses et que
        # l'upstream nous renvoie (summary[].text ; pas d'id ni
        # encrypted_content). Inséré IMMÉDIATEMENT AVANT le message assistant
        # porteur du reasoning_content — jamais avant un function_call_output.
        _reasoning_txt = m.get("reasoning_content") or ""
        if role == "assistant" and isinstance(_reasoning_txt, str) and _reasoning_txt.strip():
            inp.append(
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": _reasoning_txt}],
                }
            )
            _has_reasoning_items = True
        if isinstance(content, str):
            if content:
                ctype = "output_text" if role == "assistant" else "input_text"
                item = {"role": role, "content": [{"type": ctype, "text": content}]}
                if cache_ctrl:
                    # `cache_control` n'existe pas au schéma Responses officiel :
                    # ne jamais l'émettre (400 sur upstream strict). Le cache
                    # Responses passe par `prompt_cache_key` (jambe free) — log
                    # seul, pas de traduction inventée (cf. TROU 10 / L15).
                    _debug("  [convert] DROP cache_control → item Responses (champ inconnu du schéma)")
                inp.append(item)
        elif isinstance(content, list):
            parts: list[dict] = []
            # [FIX tour vide] Un placeholder de texte doit suivre le rôle : sur un
            # message `assistant` l'upstream /responses refuse `input_text`
            # (« content type `input_text` is not valid on `assistant` messages »)
            # et la requête part en 400, avalé en tour vide sur la jambe free.
            _ph_type = "output_text" if role == "assistant" else "input_text"
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    parts.append(
                        {
                            "type": "output_text" if role == "assistant" else "input_text",
                            "text": b["text"],
                        }
                    )
                elif b.get("type") == "image_url":
                    _img_url = (b.get("image_url") or {}).get("url", "")
                    if not _img_url:
                        continue
                    # Schéma Responses : input_image porte image_url (URL https
                    # ou data URI base64 tel quel) — jamais image_base64 /
                    # mime_type (rejet 400 upstream : "requires either
                    # image_url or file_id").
                    # [FIX tour vide] `input_image` n'est valide que sur un
                    # message non-assistant ; sur un assistant on replie en
                    # placeholder typé (même règle que les autres placeholders).
                    if role == "assistant":
                        _debug("  [convert] DROP image_url sur assistant → placeholder (input_image invalide)")
                        parts.append({"type": _ph_type, "text": "[image]"})
                    else:
                        parts.append({"type": "input_image", "image_url": _img_url})
                elif b.get("type") == "file":
                    _fobj = b.get("file") or {}
                    if not isinstance(_fobj, dict):
                        continue
                    if role == "assistant":
                        # [FIX tour vide] `input_file` invalide sur un assistant :
                        # repli en placeholder typé plutôt qu'un 400 avalé.
                        _debug("  [convert] DROP file sur assistant → placeholder (input_file invalide)")
                        parts.append({"type": _ph_type, "text": "[file]"})
                        continue
                    if _fobj.get("file_id"):
                        parts.append({"type": "input_file", "file_id": _fobj["file_id"]})
                    elif _fobj.get("file_data"):
                        _fdata = _fobj["file_data"]
                        if not _fdata:
                            continue
                        # Schéma Responses : input_file = file_data (data URI
                        # tel quel) + filename — jamais mime_type / file_url.
                        _fname = _fobj.get("filename") or "document.pdf"
                        if _fdata.startswith("data:"):
                            _fdata_norm = _fdata
                        elif _fdata.startswith("http://") or _fdata.startswith("https://"):
                            # input_file.file_url EXISTE dans le schéma
                            # Responses officiel → mapping direct, pas de DROP.
                            _fpart: dict = {"type": "input_file", "file_url": _fdata}
                            if _fobj.get("filename"):
                                _fpart["filename"] = _fobj["filename"]
                            parts.append(_fpart)
                            continue
                        else:
                            _fhead, _, _fraw = _fdata.partition(",")
                            _fdata_norm = (
                                f"data:application/pdf;base64,{_fraw or _fdata}"
                                if ";" not in (_fhead or "")
                                else _fdata
                            )
                        parts.append(
                            {
                                "type": "input_file",
                                "file_data": _fdata_norm,
                                "filename": _fname,
                            }
                        )
                elif b.get("type") == "input_audio":
                    # Chat → Responses : input_audio {data: base64 brut,
                    # format: wav|mp3 uniquement} → passthrough validé.
                    _ca = b.get("input_audio") if isinstance(b.get("input_audio"), dict) else {}
                    _cdata = (_ca or {}).get("data", "") or ""
                    _cfmt = str((_ca or {}).get("format", "") or "").lower()
                    if _cdata and _cfmt in ("wav", "mp3") and role != "assistant":
                        parts.append({"type": "input_audio", "input_audio": {"data": _cdata, "format": _cfmt}})
                    elif _cdata and _cfmt in ("wav", "mp3"):
                        # [FIX tour vide] `input_audio` invalide sur un assistant.
                        _debug("  [convert] DROP input_audio sur assistant → placeholder")
                        parts.append({"type": _ph_type, "text": f"[audio:{_cfmt}]"})
                    else:
                        _debug(
                            f"  [convert] DROP input_audio format={_cfmt!r} → Chat n'accepte que wav|mp3 (placeholder)"
                        )
                        parts.append({"type": _ph_type, "text": f"[audio:{_cfmt or 'unknown'}]"})
                elif b.get("type") in ("video", "video_url"):
                    # Aucune des trois API n'accepte la vidéo : placeholder
                    # honnête (frames + transcript côté client, cf. plan).
                    _debug("  [convert] DROP video → aucune API (placeholder)")
                    parts.append({"type": _ph_type, "text": "[video:unsupported]"})
                elif b.get("type") not in ("text", "image_url", "file"):
                    _debug(f"  [convert] DROP chat part type={b.get('type')!r} → placeholder")
                    parts.append({"type": _ph_type, "text": f"[{b.get('type') or 'unknown'}]"})
            if parts:
                item = {"role": role, "content": parts}
                if cache_ctrl:
                    # Idem branche str : champ inconnu du schéma Responses.
                    _debug("  [convert] DROP cache_control → item Responses (champ inconnu du schéma)")
                inp.append(item)
        for tc in m.get("tool_calls", []) or []:
            fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
            _fn_name = fn.get("name", "")
            if not isinstance(_fn_name, str) or not _fn_name.strip():
                _debug(
                    f"  [convert] SKIP function_call with empty name call_id={tc.get('call_id') or tc.get('id', '?')}"
                )
                continue
            _cid = _tool_id_or_stable(tc.get("call_id") or tc.get("id"), "call_", _fn_name.strip(), len(inp))
            inp.append(
                {
                    "type": "function_call",
                    "call_id": _cid,
                    "name": _fn_name.strip(),
                    "arguments": fn.get("arguments", "{}"),
                }
            )
    # Orphan filter for Responses input
    # (désactivé si chaînage serveur : l'appariement vit côté upstream)
    inp = _drop_orphan_responses_input(inp, chat)
    # Parité client officiel : strip des `id` d'items (provider.ts, comme Codex).
    # `call_id`/`encrypted_content`/`item_reference` préservés (cf. helper).
    inp = _strip_responses_item_ids(inp)

    # Guard: Responses input must be non-empty; log original chat for audit if empty
    if not inp:
        _debug(
            f"  [free] _chat_to_responses_request empty input for model {chat.get('model', '')} — original messages={len(chat.get('messages', []))} — injecting fallback"
        )
        inp.append({"role": "user", "content": [{"type": "input_text", "text": "hello"}]})
    req = {"model": chat.get("model", ""), "input": inp, "stream": bool(chat.get("stream", False))}
    # [Correctif parité multi-tours] marqueur pour le retry-once : si l'upstream
    # rejette les items reasoning synthétiques (400/422), /responses retente
    # une fois sans eux (même payload sinon) au lieu de casser le tour entier.
    # Interne uniquement : stripé au dernier kilomètre (_serialize_json_body),
    # jamais sur le wire (inconnu de l'upstream → 400).
    if _has_reasoning_items:
        req[_HAS_SYNTHETIC_REASONING_KEY] = True
    # [Lot L14 — A17] Lecture unifiée des trois formes de limite. Avant, seule
    # `max_tokens` était relue : un modèle de raisonnement à qui P2 venait
    # d'écrire `max_completion_tokens` (B2) voyait sa limite **disparaître** à
    # cette étape — le client demandait 512 tokens, l'upstream n'en recevait
    # aucune borne, soit exactement le coût non borné que B2 cherche à éviter.
    #
    # Priorité inchangée pour les deux formes historiques : `max_output_tokens`
    # (forme Responses native) l'emporte sur `max_tokens` (héritée). La forme
    # moderne est ajoutée en dernier recours, donc aucun cas existant ne change.
    # (Divergence assumée vs l'ordre canonique §11.8 de `_set_output_token_limit`
    # — contrat figé par test_p5_preserves_historical_precedence ; ne s'active
    # que si le client envoie deux formes divergentes.)
    for _key in ("max_output_tokens", "max_tokens", "max_completion_tokens"):
        _val = chat.get(_key)
        if isinstance(_val, int) and _val > 0:
            req["max_output_tokens"] = _clamp_max_output_tokens(_val, chat.get("model", ""))
            break
    # [Lot L15 — B5] Relaie `store` et `truncation` : les défauts Responses sont
    # `store=true` (rétention ≥30 j côté upstream) et `truncation="disabled"`
    # (400 en dépassement de contexte, pas de troncature silencieuse). Sans
    # relais, le client ne contrôle ni sa confidentialité ni son mode d'échec.
    _relay_responses_storage_fields(req, chat)
    # Parité SDK : les champs optionnels (previous_response_id, prompt_cache_key,
    # service_tier, instructions...) suivent quand le client les envoie.
    _relay_responses_optional_fields(req, chat)
    # Structured output : response_format (Chat) → text.format (Responses).
    _relay_response_format(req, chat)
    # NB : `_ensure_encrypted_content_include` est appelé en fin de fonction,
    # APRÈS la pose de `req["reasoning"]` (il en dépend).
    # Parité SDK : temperature/top_p NON supportés par les modèles de
    # raisonnement (le SDK les supprime avec warning). Les émettre vers
    # muse/spark = 400 évitable ou consigne silencieusement ignorée.
    _drop_sampling = _is_responses_reasoning_model(chat.get("model", ""))
    for k in ("temperature", "top_p"):
        if k in chat:
            if _drop_sampling:
                _debug(f"  [convert] DROP {k} → modèle de raisonnement (SDK: non supporté)")
                continue
            req[k] = chat[k]
    # Forward reasoning parameters to Responses API format.
    # Le clamp config (thinking.effort_caps) s'applique ici aussi : ce forward
    # verbatim laissait passer un effort au-delà du plafond du modèle
    # (ex. max sur glm-5 → 400 upstream) sur la jambe /v1/responses.
    _chat_model = chat.get("model", "")
    if "reasoning_effort" in chat:
        effort = _effort_to_reasoning(chat["reasoning_effort"], _chat_model)
        if effort is None:
            # Désactivé (none/off) : AUCUN bloc reasoning (parité thinkingLevel off).
            _debug(f"  [thinking] {_chat_model}: effort désactivé → pas de reasoning")
        else:
            # Parité SDK : le défaut quand un effort est posé est 'detailed'
            # (summary riche), pas 'auto' (condensé). undefined = pas de summary.
            req["reasoning"] = {"summary": "detailed", "effort": effort}
    elif "reasoning" in chat:
        _raw_reasoning = chat["reasoning"]
        if isinstance(_raw_reasoning, dict):
            _raw_effort = _raw_reasoning.get("effort")
            if isinstance(_raw_effort, str) and _raw_effort:
                _clamped_effort = _effort_to_reasoning(_raw_effort, _chat_model)
                if _clamped_effort is None:
                    _debug(f"  [thinking] {_chat_model}: reasoning désactivé (effort={_raw_effort!r}) → clé retirée")
                else:
                    _clamped = dict(_raw_reasoning)
                    _clamped["effort"] = _clamped_effort
                    if "summary" not in _clamped:
                        _clamped["summary"] = "detailed"
                    req["reasoning"] = _clamped
            else:
                req["reasoning"] = _raw_reasoning
        else:
            req["reasoning"] = _raw_reasoning
    if "tools" in chat:
        model = chat.get("model", "")
        prof = _resolve_schema_profile(model)
        _is_strict = prof["strip_additional_props"]

        def _needs_fallback(p: dict) -> bool:
            if not isinstance(p, dict):
                return False
            if any(k in p for k in ("anyOf", "oneOf", "const", "title", "$schema", "$id")):
                return True
            for v in p.values():
                if isinstance(v, dict) and _needs_fallback(v):
                    return True
                if isinstance(v, list) and any(isinstance(x, dict) and _needs_fallback(x) for x in v):
                    return True
            return False

        tools = []
        for t in chat["tools"]:
            if isinstance(t, dict) and "function" in t:
                fn = t["function"]
                raw_params = fn.get("parameters", {}) or {}
                params = _normalize_tool_schema(raw_params, model)
                tool_entry: dict = {
                    "type": "function",
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": params,
                }
                # V5.1 sémantique : strict:false seulement si fallback nécessaire
                if _is_strict and _needs_fallback(raw_params):
                    tool_entry["strict"] = False
                tools.append(tool_entry)
        # [Lot L4 — A8] FUSION, jamais écrasement : ``anthropic_to_openai`` a pu
        # déjà poser une map (noms Chat raccourcis pour la limite 64). Elle est
        # lue depuis ``chat`` — l'ENTRÉE — et non depuis ``req``, qui est un dict
        # NEUF (cf. sa construction ``{"model":…, "input":…}``) : la chercher dans
        # ``req`` la perdait, et le client recevait un nom raccourci non
        # restaurable (défaut trouvé par test_anthropic_path_funnels_through_chat).
        _prev_map = chat.get(_TOOL_NAME_MAP_KEY)
        _name_map: dict = dict(_prev_map) if isinstance(_prev_map, dict) else {}
        if tools:
            # Sanitize-aller : l'upstream /responses refuse les noms >64 chars
            # (400 systématique → retry station fraîche inutile). La map est
            # transportée par requête (clé privée, jamais globale) pour le
            # restore-retour ; stripée avant sérialisation wire.
            tools, _fresh_map = sanitize_tool_names(tools)
            req["tools"] = tools
            _name_map.update(_fresh_map)
        # P0-1 [msg_18d502b1e0b40-e] : l'historique (input[].function_call.name)
        # suit le même rename — le tour N+1 rejoue sinon le nom original.
        _remap_responses_history_names(inp, _name_map)
        if _name_map:
            req[_TOOL_NAME_MAP_KEY] = _name_map
        tc = chat.get("tool_choice")
        if tc is not None:
            # [FIX tour vide] {"type":"auto"} → "auto" AVANT le remap nommé :
            # /responses refuse les objets à type non nommé (400 avalé en tour
            # vide sur la jambe free).
            tc = _normalize_responses_tool_choice(tc)
            # P0-2 : toutes les formes nommées suivent le rename.
            req["tool_choice"] = _remap_responses_tool_choice(tc, _name_map)
    else:
        # P0-1 sans définitions : l'historique reste remappé en défensif
        # (map locale, restore-retour préservé). [Lot L4 — A8] Fusion avec la
        # map éventuellement déjà posée à l'aller Chat (lue sur ``chat``, cf. supra).
        _prev_alone = chat.get(_TOOL_NAME_MAP_KEY)
        _alone: dict = dict(_prev_alone) if isinstance(_prev_alone, dict) else {}
        _remap_responses_history_names(inp, _alone)
        if _alone:
            req[_TOOL_NAME_MAP_KEY] = _alone
    # Parité SDK : store:false + reasoning → include reasoning.encrypted_content.
    # ICI (fin) car `req["reasoning"]` n'est posé qu'au milieu de la fonction.
    _ensure_encrypted_content_include(req)
    return req


def _anthropic_to_responses_request(anthro: dict) -> dict:
    if "input" in anthro and "messages" not in anthro:
        # Verbatim natif Responses : même sanitize que le chemin chat (P0-1/P0-2).
        return _sanitize_native_responses_request(dict(anthro))
    chat = anthropic_to_openai(anthro, anthro.get("model", ""))
    req = _chat_to_responses_request(chat)
    # [Lot L15 — B5] `anthropic_to_openai` ne transporte pas `store`/`truncation` :
    # on les relaie depuis le corps d'origine, sinon un client Anthropic ne peut
    # pas refuser la rétention ≥30 j ni choisir son mode de dépassement.
    # Même relais pour les autres champs Responses optionnels (parité SDK).
    _relay_responses_storage_fields(req, anthro)
    _relay_responses_optional_fields(req, anthro)
    _ensure_encrypted_content_include(req)
    return req


def _responses_to_chat_response(resp: dict, model: str, name_map: dict | None = None) -> dict:
    out = resp.get("output", []) or []
    texts = []
    reasoning = ""
    tool_calls = []
    for item in out:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message" and item.get("role") == "assistant":
            for blk in item.get("content", []) or []:
                if isinstance(blk, dict) and blk.get("type") == "output_text":
                    texts.append(blk.get("text", ""))
        elif item.get("type") == "reasoning":
            for s in item.get("summary", []) or []:
                if isinstance(s, dict):
                    reasoning += s.get("text", "")
        elif item.get("type") == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id", item.get("id", "")),
                    "type": "function",
                    "function": {
                        "name": restore_tool_name(item.get("name", ""), name_map),
                        "arguments": item.get("arguments", "{}"),
                    },
                }
            )
    # vrai seulement : pas de placeholder si pas de summary visible
    msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts)}
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = tool_calls
    usage = resp.get("usage", {}) if isinstance(resp.get("usage"), dict) else {}
    # Cache tokens come from input_tokens_details, NOT output_tokens_details
    _inp_details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    _cached = _inp_details.get("cached_tokens", 0) if isinstance(_inp_details, dict) else 0
    chat_usage = {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", usage.get("input_tokens", 0) + usage.get("output_tokens", 0)),
    }
    if _cached:
        chat_usage["prompt_tokens_details"] = {"cached_tokens": _cached}
    status = resp.get("status", "completed")
    # [audit 2026-10-30] A response carrying tool_calls must report
    # finish_reason="tool_calls", never "stop": several SDKs execute tools
    # only on that signal, so the turn silently ended without running them.
    finish = (
        "tool_calls"
        if tool_calls
        else ("length" if status != "completed" else "stop")
    )
    return {
        "id": resp.get("id", f"chatcmpl-{uuid.uuid4().hex[:8]}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": chat_usage,
    }


def _responses_to_anthropic_response(resp: dict, model: str, name_map: dict | None = None) -> dict:
    blocks = []
    for item in resp.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "reasoning":
            _seen_text = False
            for s in item.get("summary", []) or []:
                if isinstance(s, dict) and s.get("text"):
                    _seen_text = True
                    blocks.append(
                        {
                            "type": "thinking",
                            "thinking": s.get("text", ""),
                            "signature": _local_signature(s.get("text", "")),
                        }
                    )
            if not _seen_text and isinstance(item.get("encrypted_content"), str) and item["encrypted_content"]:
                # Pas de summary visible mais contenu chiffré authentique :
                # bloc opaque rejouable (parité SDK) au lieu d'une perte sèche.
                blocks.append({"type": "redacted_thinking", "data": item["encrypted_content"]})
        elif item.get("type") == "message":
            for blk in item.get("content", []) or []:
                if isinstance(blk, dict) and blk.get("type") == "output_text" and blk.get("text"):
                    blocks.append({"type": "text", "text": blk.get("text", "")})
        elif item.get("type") == "function_call":
            _fc_name = restore_tool_name(item.get("name", ""), name_map)
            if not isinstance(_fc_name, str) or not _fc_name.strip():
                _debug("  [convert] SKIP function_call with empty name (anthropic response)")
                continue
            try:
                inp = _json_loads(item.get("arguments", "{}"))
            except Exception:
                inp = {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": _tool_id_or_stable(
                        item.get("call_id") or item.get("id"), "toolu_", _fc_name, len(blocks)
                    ),
                    "name": _fc_name,
                    "input": inp,
                }
            )
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    usage = resp.get("usage", {}) if isinstance(resp.get("usage"), dict) else {}
    status = resp.get("status", "completed")
    stop = "end_turn" if status == "completed" else "max_tokens"
    has_tools = any(b.get("type") == "tool_use" for b in blocks)
    if has_tools:
        stop = "tool_use"
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": blocks,
        "model": model,
        "stop_reason": stop,
        "stop_sequence": None,
        # input_tokens doit être l'input NON caché : les clients Anthropic
        # additionnent input + cache_read + cache_creation pour le contexte.
        "usage": anthropic_usage_from_openai(usage),
    }


