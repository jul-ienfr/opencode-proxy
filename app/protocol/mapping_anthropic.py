"""
Protocol mapping — OpenAI→Anthropic, requête/ réponse Anthropic, signatures locales et thinking synthétique.

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

def _local_signature(text: str) -> str:
    """[v10 PLAN-raisonnement 2.1] Signature LOCALE (HMAC SHA256 base64).

    La signature Anthropic authentique est cryptographique côté modèle — le
    proxy ne peut pas la forger. Les clients Anthropic-compatibles exigent
    néanmoins un champ `signature` non vide sur les blocs thinking (sinon
    abandonnés en multi-tours). On signe localement : les clients stockent et
    re-transmettent sans valider ; on ne renvoie jamais ces blocs synthétiques
    aux upstreams stricts (strip multi-tours, PLAN-raisonnement Phase D)."""
    import base64
    import hashlib
    import hmac

    key = b"opencode-proxy-local-thinking-signature-v1"
    return base64.b64encode(hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()).decode()


# [P5.1 perf] LRU bornée pour _is_local_signature — les historiques multi-tours
# ré-émettent les mêmes blocs → hit-rate élevé, zéro changement sémantique.
_is_local_sig_cache: OrderedDict[bytes, bool] = OrderedDict()
_IS_LOCAL_SIG_CACHE_MAX = 2048


def _is_local_signature(text: str, signature: str) -> bool:
    """[PLAN-raisonnement Phase D] Détecte une signature FORGÉE par le proxy.

    Provenance stateless : on recalcule le HMAC local du texte et on compare.
    Une signature authentique (Anthropic) ne peut pas correspondre — elle
    n'est pas produite par notre clé. Un bloc thinking re-émis par le client
    avec NOTRE signature est donc identifiable sans table d'état.

    [P5.1 perf] Mémoïsation LRU bornée (clé blake2b(text+signature), 2048 entrées)
    pour éviter le recalcul HMAC par bloc/requête."""
    if not isinstance(signature, str) or not signature:
        return False
    if not isinstance(text, str) or not text:
        return False
    try:
        # clé 128-bit blake2b : collision négligeable, calcul rapide
        h = hashlib.blake2b(digest_size=16)
        h.update(text.encode("utf-8"))
        h.update(b"\x00")
        h.update(signature.encode("utf-8"))
        key = h.digest()
        cached = _is_local_sig_cache.get(key)
        if cached is not None:
            _is_local_sig_cache.move_to_end(key)
            return cached
        import hmac as _hmac

        result = _hmac.compare_digest(_local_signature(text), signature)
        _is_local_sig_cache[key] = result
        _is_local_sig_cache.move_to_end(key)
        if len(_is_local_sig_cache) > _IS_LOCAL_SIG_CACHE_MAX:
            _is_local_sig_cache.popitem(last=False)
        return result
    except Exception:
        return False


def _looks_encrypted_reasoning(text: str) -> bool:
    """Heuristique `reasoning_content` chiffré/binaire → redacted_thinking.

    Un raisonnement réel contient espaces et ponctuation ; un blob chiffré
    (base64) est une longue chaîne sans espace, multiple de 4, charset base64.
    """
    if not isinstance(text, str):
        return False
    t = text.strip()
    if len(t) < 64 or len(t) % 4 != 0 or " " in t:
        return False
    return re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", t) is not None


def strip_synthetic_thinking(body: dict) -> int:
    """[PLAN-raisonnement Phase D.3] Strip sélectif dans l'historique Anthropic.

    Retire des messages les blocs `thinking` dont la signature est une
    signature LOCALE du proxy (blocs synthétisés par conversion reasoning_content).
    Ces blocs ne partent JAMAIS vers un upstream : seul Anthropic direct valide
    cryptographiquement les signatures au tour suivant, et on ne lui ment pas.

    Les blocs ORIGINAUX (signature authentique du modèle source) et
    `redacted_thinking` (donnée chiffrée authentique) passent intacts.
    Retourne le nombre de blocs strippés."""
    stripped = 0
    for msg in body.get("messages", []) or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        kept = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "thinking"
                and _is_local_signature(block.get("thinking", ""), block.get("signature", ""))
            ):
                stripped += 1
                _debug(
                    "  [thinking] DROP bloc thinking à signature LOCALE de l'historique "
                    "(jamais transmis aux upstreams, PLAN-raisonnement D)"
                )
                continue
            kept.append(block)
        msg["content"] = kept
    # [audit 2026-10-30] Removing a synthetic thinking block can leave an
    # assistant message with content:[] (reasoning-only turn). Anthropic
    # rejects empty content in 400, so re-filter the emptied messages.
    if stripped:
        try:
            body["messages"] = filter_empty_anthropic_messages(body.get("messages") or [])
        except Exception:
            pass
    return stripped


def openai_to_anthropic(
    resp: dict,
    model: str,
    name_map: dict | None = None,
    tool_aliases: dict | None = None,
) -> dict:
    """Convertit une réponse Chat en réponse Anthropic.

    ``name_map`` ([Lot L4 — A8]) est la map ``{short: original}`` produite à
    l'aller par ``_sanitize_chat_tools`` : sans elle, un client qui avait envoyé
    un nom d'outil > 64 caractères recevrait le nom *raccourci* dans le bloc
    ``tool_use``, c'est-à-dire un outil qu'il ne reconnaît pas. Restaure le nom
    d'origine tel quel quand aucune map n'est fournie (jamais de renommage).

    ``tool_aliases`` ([2026-11-12]) remappe le nom **officiel** émis par
    l'amont (``bash``, injecté par la jambe free pour franchir le gate) vers
    l'outil réellement déclaré par le client Anthropic (``Bash``, ``shell``…).
    Sans cela, Claude Code répond « No such tool available: bash ».
    """
    choice = resp.get("choices", [{}])[0]
    msg = choice.get("message", {})
    usage = resp.get("usage", {})

    blocks = []
    dropped: list[tuple[str, object]] = []
    if reasoning := msg.get("reasoning_content") or msg.get("reasoning"):
        if _looks_encrypted_reasoning(reasoning):
            # [PLAN-raisonnement Phase B.2] raisonnement chiffré/binaire de
            # l'upstream → bloc redacted_thinking (pas de signature forgée sur
            # une donnée qu'on ne peut pas signer)
            blocks.append({"type": "redacted_thinking", "data": reasoning})
        else:
            blocks.append(
                {
                    "type": "thinking",
                    "thinking": reasoning,
                    "signature": _local_signature(reasoning),
                }
            )
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        _name = restore_tool_name(fn.get("name", ""), name_map)
        _args = fn.get("arguments", "{}")
        if tool_aliases:
            # [2026-11-14] Un nom que le client ne déclare pas (``todowrite``
            # sans outil todo) produisait « No such tool available ». On retire
            # l'appel ; le tour reçoit un texte de repli s'il devient vide.
            if not tool_call_supported(_name, tool_aliases):
                dropped.append((_name, _args))
                continue
            # [2026-11-12] Nom officiel amont (``bash``) → nom déclaré par le
            # client (``Bash``/…), clés d'arguments adaptées au schéma cible.
            # Le repli shell couvre les familles absentes du runtime client
            # (``webfetch`` sans ``WebFetch`` chez Claude Code) : sans lui,
            # l'appel partait sous un nom que le client ne connaît pas.
            _name, _args = adapt_tool_call(_name, _args, tool_aliases)
        try:
            inp = _json_loads(
                _args if isinstance(_args, str) else _json_dumps_str(_args)
            )
        except Exception:
            inp = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": _tool_id_or_stable(tc.get("id"), "toolu_", len(blocks)),
                # [Lot L4 — A8] Restore : le client doit retrouver le nom qu'il a
                # envoyé (la limite 64 est une contrainte de l'amont Chat, pas
                # du client Anthropic, qui autorise 200).
                "name": _name,
                "input": inp,
            }
        )

    if not blocks:
        if dropped:
            blocks.append({"type": "text", "text": dropped_tool_call_text(*dropped[0])})
        else:
            blocks.append({"type": "text", "text": ""})

    stop = "tool_use" if any(b.get("type") == "tool_use" for b in blocks) else "end_turn"
    if choice.get("finish_reason") == "length":
        stop = "max_tokens"

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": blocks,
        "model": model,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": anthropic_usage_from_openai(usage),
    }


def openai_to_anthropic_request(oai_body: dict) -> dict:
    """Convert OpenAI Chat Completions request → Anthropic Messages format."""
    system_text = ""
    pending_tool_results = []
    emitted_tool_ids: set[str] = set()
    anthro_messages = []

    for msg in oai_body.get("messages", []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")

        if role == "system":
            # [audit 2026-10-30] OpenAI allows several system/developer
            # messages. The previous code overwrote ``system_text`` and kept
            # only the last one; preserve order instead.
            _sys_text = _extract_text(msg.get("content", ""))
            if _sys_text:
                system_text = f"{system_text}\n\n{_sys_text}" if system_text else _sys_text
            continue

        if role == "developer":
            # Parité SDK : developer = system sous un autre nom. Sans cela le
            # message tombait dans le filtre `not in ("user","assistant")`
            # plus bas → consigne système silencieusement perdue.
            _dev_text = _extract_text(msg.get("content", ""))
            if _dev_text:
                system_text = f"{system_text}\n\n{_dev_text}" if system_text else _dev_text
            continue

        if role == "tool":
            # tool_result multimodal : texte + images préservés (pas d'aplat
            # _extract_text qui perdait les bytes image).
            _o_tool_content = msg.get("content", "")
            _o_tr_blocks: list = []
            if isinstance(_o_tool_content, str):
                if _o_tool_content:
                    _o_tr_blocks = [{"type": "text", "text": _o_tool_content}]
            elif isinstance(_o_tool_content, list):
                for _o_b in _o_tool_content:
                    if isinstance(_o_b, str):
                        if _o_b:
                            _o_tr_blocks.append({"type": "text", "text": _o_b})
                    elif isinstance(_o_b, dict):
                        _o_bt = _o_b.get("type", "")
                        if _o_bt == "text" and _o_b.get("text"):
                            _o_tr_blocks.append({"type": "text", "text": _o_b["text"]})
                        elif _o_bt == "image_url":
                            _o_url = (_o_b.get("image_url") or {}).get("url", "")
                            if not _o_url:
                                continue
                            if _o_url.startswith("data:"):
                                try:
                                    _o_h, _o_b64 = _o_url.split(",", 1)
                                    _o_m = (
                                        _o_h.split(";")[0].split(":")[1] if ";" in _o_h else "image/png"
                                    ) or "image/png"
                                except ValueError:
                                    _o_m, _o_b64 = "image/png", ""
                                if not _o_b64:
                                    continue
                                _o_tr_blocks.append(
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": _o_m,
                                            "data": _o_b64,
                                        },
                                    }
                                )
                            else:
                                _o_tr_blocks.append(
                                    {
                                        "type": "image",
                                        "source": {"type": "url", "url": _o_url},
                                    }
                                )
                        elif _o_bt == "image":
                            _o_tr_blocks.append(_o_b)
                        elif _o_bt == "file":
                            _o_f = _o_b.get("file") or {}
                            if not isinstance(_o_f, dict):
                                continue
                            _o_fdata = _o_f.get("file_data", "") or ""
                            _o_ffid = _o_f.get("file_id", "") or ""
                            _o_fname = _o_f.get("filename", "") or ""
                            if isinstance(_o_fdata, str) and _o_fdata.startswith("data:"):
                                try:
                                    _o_fh, _, _o_fd = _o_fdata[5:].partition(",")
                                    _o_fm = (_o_fh.split(";")[0] or "").strip() or "application/pdf"
                                except Exception:
                                    _o_fm, _o_fd = "application/pdf", ""
                                if not _o_fd:
                                    continue
                                _o_doc: dict = {
                                    "type": "document",
                                    "source": {"type": "base64", "media_type": _o_fm, "data": _o_fd},
                                }
                                if _o_fname:
                                    _o_doc["title"] = _o_fname
                                _o_tr_blocks.append(_o_doc)
                            elif _o_ffid:
                                _o_tr_blocks.append(
                                    {"type": "document", "source": {"type": "file", "file_id": _o_ffid}}
                                )
                            elif isinstance(_o_fdata, str) and (
                                _o_fdata.startswith("http://") or _o_fdata.startswith("https://")
                            ):
                                # Anthropic ne garantit document-URL que pour les PDF.
                                if _o_fname.lower().endswith(".pdf"):
                                    _o_doc2: dict = {
                                        "type": "document",
                                        "source": {"type": "url", "url": _o_fdata},
                                    }
                                    _o_doc2["title"] = _o_fname
                                    _o_tr_blocks.append(_o_doc2)
                                else:
                                    _debug(f"  [convert] DROP tool file URL non-PDF {_o_fdata!r} → placeholder")
                                    _o_tr_blocks.append({"type": "text", "text": f"[document:url:{_o_fdata}]"})
                            else:
                                _debug("  [convert] DROP tool file sans file_data ni file_id → skip")
                        elif _o_bt == "input_audio":
                            _debug("  [convert] DROP tool input_audio → Anthropic sans audio (placeholder)")
                            _o_tr_blocks.append({"type": "text", "text": "[audio:unsupported-by-anthropic]"})
            # Contrat historique : texte seul → string (pas de liste à 1 bloc).
            if len(_o_tr_blocks) == 1 and _o_tr_blocks[0].get("type") == "text" and isinstance(_o_tool_content, str):
                _o_tr_content: str | list = _o_tr_blocks[0]["text"]
            else:
                _o_tr_content = _o_tr_blocks or _extract_text(_o_tool_content)
            # [audit 2026-10-30] Symmetric ID scrubbing + orphan guard.
            # ``tool_use`` ids are scrubbed/anonymised by _tool_id_or_stable;
            # the result must carry the SAME id. A result whose call was
            # skipped (empty tool name) or never emitted is dropped instead
            # of becoming an Anthropic 400.
            _tid_raw = msg.get("tool_call_id", "")
            _tid = scrub_claude_tool_id(_tid_raw) if isinstance(_tid_raw, str) and _tid_raw.strip() else ""
            if not _tid:
                _debug(f"  [convert] SKIP tool_result without tool_call_id ({role})")
                continue
            if _tid not in emitted_tool_ids:
                _debug(f"  [convert] SKIP orphan tool_result id={_tid!r} (no emitted tool_use)")
                continue
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": _tid,
                    "content": _o_tr_content,
                }
            )
            continue

        if role not in ("user", "assistant"):
            continue

        blocks = []

        # [audit 2026-10-30] Anthropic requires every tool_use to be followed
        # IMMEDIATELY by a user message carrying its tool_result. A Chat
        # history is assistant(tool_calls) → tool → assistant(final): the old
        # code only flushed on the next ``user`` message, so the final
        # assistant was emitted before the tool_result and upstream returned
        # 400. Flush as a user turn before ANY non-tool message.
        if pending_tool_results:
            if role == "user":
                blocks.extend(pending_tool_results)
            else:
                anthro_messages.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results = []

        # Convert content
        content = msg.get("content", "")
        if isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for block in content:
                t = block.get("type", "")
                if t == "text":
                    blocks.append({"type": "text", "text": block.get("text", "")})
                elif t == "image_url":
                    url_obj = block.get("image_url", {})
                    url = url_obj.get("url", "") if isinstance(url_obj, dict) else ""
                    if not url:
                        continue
                    if url.startswith("data:"):
                        try:
                            header, b64 = url.split(",", 1)
                            media_type = header.split(";")[0].split(":")[1] if ";" in header else "image/png"
                            if not media_type:
                                media_type = "image/png"
                        except ValueError:
                            media_type = "image/png"
                            b64 = ""
                        if not b64:
                            continue
                        blocks.append(
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": media_type, "data": b64},
                            }
                        )
                    else:
                        blocks.append(
                            {
                                "type": "image",
                                "source": {"type": "url", "url": url},
                            }
                        )
                elif t == "file":
                    _cf = block.get("file") or {}
                    if not isinstance(_cf, dict):
                        continue
                    _cdata = _cf.get("file_data", "") or ""
                    _cfid = _cf.get("file_id", "") or ""
                    _cname = _cf.get("filename", "") or ""
                    if isinstance(_cdata, str) and _cdata.startswith("data:"):
                        try:
                            _ch, _, _cd = _cdata[5:].partition(",")
                            _cm = (_ch.split(";")[0] or "").strip() or "application/pdf"
                        except Exception:
                            _cm, _cd = "application/pdf", ""
                        if _cd:
                            _cb: dict = {
                                "type": "document",
                                "source": {"type": "base64", "media_type": _cm, "data": _cd},
                            }
                            if _cname:
                                _cb["name"] = _cname
                            blocks.append(_cb)
                    elif _cfid:
                        blocks.append({"type": "document", "source": {"type": "file", "file_id": _cfid}})
                    elif isinstance(_cdata, str) and (_cdata.startswith("http://") or _cdata.startswith("https://")):
                        # Anthropic ne garantit document-URL que pour les PDF.
                        if _cname.lower().endswith(".pdf"):
                            _cb2: dict = {
                                "type": "document",
                                "source": {"type": "url", "url": _cdata},
                            }
                            _cb2["name"] = _cname
                            blocks.append(_cb2)
                        else:
                            _debug(f"  [convert] DROP file URL non-PDF {_cdata!r} → placeholder")
                            blocks.append({"type": "text", "text": f"[document:url:{_cdata}]"})
                    else:
                        _debug("  [convert] DROP file sans file_data ni file_id → skip")
                elif t == "input_audio":
                    _debug("  [convert] DROP input_audio → Anthropic sans audio (placeholder)")
                    blocks.append({"type": "text", "text": "[audio:unsupported-by-anthropic]"})
                elif t not in ("video", "video_url"):
                    _debug(f"  [convert] DROP chat part type={t!r} → placeholder")
                    blocks.append({"type": "text", "text": f"[{t or 'unknown'}]"})
                else:
                    _debug("  [convert] DROP video → aucune API (placeholder)")
                    blocks.append({"type": "text", "text": "[video:unsupported]"})

        # Convert tool_calls (assistant only)
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
            _tname = fn.get("name", "") if isinstance(fn.get("name"), str) else ""
            if not _tname.strip():
                # Parité officielle (symétrique du sens inverse) : `name` minLength 1.
                # Un tool_call sans nom ne doit jamais produire un bloc invalide.
                _debug(f"  [convert] SKIP tool_call with empty name id={tc.get('id', '?')}")
                continue
            try:
                inp = _json_loads(fn.get("arguments", "{}"))
            except Exception:
                inp = {}
            _tool_id = _tool_id_or_stable(tc.get("id"), "toolu_", len(blocks))
            emitted_tool_ids.add(_tool_id)
            blocks.append(
                {
                    "type": "tool_use",
                    "id": _tool_id,
                    "name": _tname,
                    "input": inp,
                }
            )

        # [Lot H2 — remplace Phase D.3] reasoning_content historique → bloc
        # thinking avec signature locale, symétrique du sens réponse
        # (openai_to_anthropic). Sécurité : les blocs à signature LOCALE sont
        # reconnus et retirés par strip_synthetic_thinking (appelé sur la voie
        # Anthropic, opencode.py) avant l'envoi à un upstream strict qui valide
        # cryptographiquement les signatures — jamais de 400 forgé ; vers un
        # upstream compatible le raisonnement voyage au lieu d'être perdu.
        _hist_reasoning = msg.get("reasoning_content") or msg.get("reasoning")
        if role == "assistant" and isinstance(_hist_reasoning, str) and _hist_reasoning.strip():
            blocks.insert(
                0,
                {
                    "type": "thinking",
                    "thinking": _hist_reasoning,
                    "signature": _local_signature(_hist_reasoning),
                },
            )

        # Ensure at least one block
        if not blocks:
            blocks.append({"type": "text", "text": ""})

        anthro_messages.append({"role": role, "content": blocks})

    # Trailing tool_results (edge case)
    if pending_tool_results:
        anthro_messages.append({"role": "user", "content": pending_tool_results})

    # Parité OpenCodeClient : filtre des contenus vides côté requête Anthropic
    # (remplace le pis-aller `text:""` qui partait en 400). Garde : si tout est
    # vide, on conserve l'original (un `messages: []` serait pire).
    if not _PARITY_DISABLED:
        try:
            _filtered = filter_empty_anthropic_messages(anthro_messages)
            if _filtered:
                anthro_messages = _filtered
        except Exception:
            pass

    result = {
        "model": oai_body.get("model", ""),
        "messages": anthro_messages,
        "stream": oai_body.get("stream", False),
    }
    # [Lot L14 — A17] Lit `max_tokens` ET `max_completion_tokens` : la forme
    # moderne était ignorée, la limite du client remplacée par le défaut 16384.
    # Destination Anthropic → toujours `max_tokens`.
    _set_output_token_limit(result, oai_body, oai_body.get("model", ""), target_protocol="anthropic")

    if system_text:
        result["system"] = system_text

    # Map simple params. [audit 2026-10-30] Anthropic validates these more
    # strictly than Chat: ``stop`` is an array (a Chat string must be wrapped),
    # temperature/top_p are 0..1. Forwarding a valid Chat value verbatim
    # produced a 400 on every Anthropic target.
    if "temperature" in oai_body and isinstance(oai_body["temperature"], (int, float)):
        result["temperature"] = max(0.0, min(1.0, float(oai_body["temperature"])))
    if "top_p" in oai_body and isinstance(oai_body["top_p"], (int, float)):
        result["top_p"] = max(0.0, min(1.0, float(oai_body["top_p"])))
    if "stop" in oai_body:
        _stop = oai_body["stop"]
        if isinstance(_stop, str):
            result["stop_sequences"] = [_stop]
        elif isinstance(_stop, list):
            result["stop_sequences"] = [s for s in _stop if isinstance(s, str)]

    # Parité SDK : metadata/service_tier valides côté Anthropic, user → user_id.
    _relay_anthropic_optional_fields(result, oai_body)

    # Convert tools - v3.3: preserve server tools B4
    if "tools" in oai_body:
        model = oai_body.get("model", "")
        anthro_tools = []
        for t in oai_body["tools"]:
            t_type = t.get("type", "")
            # Server tools (web_search/web_fetch) - preserve
            if isinstance(t_type, str) and t_type.startswith("web_"):
                # OpenAI server tool format: {"type":"web_search_2025_03_05", "name":"web_search"} or similar
                name = t.get("name") or t.get("function", {}).get("name", "web_search")
                # normalize
                if "web_search" in t_type:
                    name = "web_search"
                elif "web_fetch" in t_type:
                    name = "web_fetch"
                anthro_tools.append(
                    {
                        "name": name,
                        "description": t.get("description", ""),
                        "input_schema": t.get("input_schema", t.get("parameters", {})),
                    }
                )
                continue
            if t.get("type") == "function":
                fn = t.get("function", {})
                if not fn.get("name"):
                    _debug("  [convert] SKIP function tool without name")
                    continue
                schema = _normalize_tool_schema(fn.get("parameters", {}) or {}, model)
                anthro_tools.append(
                    {
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "input_schema": schema,
                    }
                )
            elif "function" in t:
                fn = t["function"]
                if not fn.get("name"):
                    continue
                schema = _normalize_tool_schema(fn.get("parameters", {}) or {}, model)
                anthro_tools.append(
                    {
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "input_schema": schema,
                    }
                )
        if anthro_tools:
            result["tools"] = anthro_tools

        # Convert tool_choice (formes Chat valides : none/auto/required + dict
        # function — la forme objet Anthropic {type} est émise, jamais de string
        # nue que l'amont rejetterait en 400).
        tc = oai_body.get("tool_choice", "auto")
        if isinstance(tc, dict):
            tc_type = tc.get("type", "auto")
            if tc_type == "function":
                result["tool_choice"] = {
                    "type": "tool",
                    "name": tc.get("function", {}).get("name", ""),
                }
            elif tc_type in ("any", "auto", "none"):
                result["tool_choice"] = {"type": tc_type}
            else:
                _debug(f"  [convert] tool_choice Chat dict inconnu {tc_type!r} → auto")
                result["tool_choice"] = {"type": "auto"}
        elif isinstance(tc, str):
            if tc == "required":
                result["tool_choice"] = {"type": "any"}
            elif tc in ("auto", "any", "none"):
                result["tool_choice"] = {"type": tc}
            else:
                _debug(f"  [convert] tool_choice string inconnu {tc!r} → auto")
                result["tool_choice"] = {"type": "auto"}
        else:
            result["tool_choice"] = {"type": "auto"}
        # [audit 2026-10-30] Chat/Responses `parallel_tool_calls:false` maps
        # to Anthropic's disable_parallel_tool_use on the tool_choice object.
        if oai_body.get("parallel_tool_calls") is False and isinstance(result.get("tool_choice"), dict):
            result["tool_choice"]["disable_parallel_tool_use"] = True

    # [Lot H4 / Lot L2] Sens inverse de _effort_to_reasoning : un client OpenAI
    # qui envoie reasoning_effort doit obtenir une config thinking Anthropic
    # quand la requête est routée vers un upstream Anthropic (P4/P26).
    #
    # [Lot L2] On passe par la SOURCE UNIQUE : ``resolve_effort`` lit
    # ``reasoning_effort`` mais aussi ``output_config.effort``, ``effort``,
    # ``reasoning.effort`` et ``thinking.*`` — un client peut donc envoyer
    # n'importe laquelle de ces formes sur cette porte (A1). L'ancien dict codé
    # en dur ``{low:4096, medium:10000, high:16000}`` repliait ``xhigh``/``max``
    # sur 16000 (A2) : il est supprimé.
    #
    # [Hotfix A14/A23] La forme {type:"enabled", budget_tokens:N} est
    # abandonnée : dépréciée sur Claude 4.6, rejetée en 400 à partir de 4.7,
    # et son ratio 16000/10000/4096 pouvait dépasser les max_tokens du client
    # (Anthropic exige max_tokens > budget_tokens). Cible : adaptive +
    # output_config.effort, qui ne porte aucun budget.
    _decision = _resolve_effort(oai_body, result.get("model", ""))
    if _decision.wants and _decision.level:
        _apply_anthropic_effort(result, _decision.level, result.get("model", ""), source=_decision.source)

    return result


def anthropic_to_openai_response(anthro: dict, model: str) -> dict:
    """Convert Anthropic Messages response → OpenAI Chat Completions format."""
    content_blocks = anthro.get("content", [])
    text_parts: list = []
    reasoning_text = ""
    tool_calls: list = []

    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        t = block.get("type", "")
        if t == "text":
            text_parts.append(block.get("text", ""))
        elif t == "thinking":
            reasoning_text = block.get("thinking", "")
        elif t == "tool_use":
            tool_calls.append(
                {
                    "id": _tool_id_or_stable(block.get("id"), "call_", block.get("name", ""), len(tool_calls)),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": _json_dumps_str(block.get("input", {}), ensure_ascii=False),
                    },
                }
            )

    # Determine finish_reason
    sr = anthro.get("stop_reason", "")
    if sr == "max_tokens":
        finish = "length"
    elif sr == "tool_use":
        finish = "tool_calls"
    elif sr in ("refusal", "content_filter"):
        # [audit 2026-10-30] A safety refusal is not a normal stop.
        finish = "content_filter"
    else:
        finish = "stop"

    # Usage mapping. [audit 2026-10-30] Anthropic reports NON-cached input;
    # OpenAI Chat reports an INCLUSIVE prompt total. The old code copied the
    # non-cached value, so a 97 + 87 552-cache response became prompt_tokens
    # = 97 (cached_tokens > prompt_tokens) and clients under-estimated their
    # context by ~900x, defeating auto-compaction.
    usage = anthro.get("usage", {}) if isinstance(anthro.get("usage"), dict) else {}
    _non_cached = _usage_int(usage.get("input_tokens"))
    cache_read = _usage_int(usage.get("cache_read_input_tokens"))
    cache_creation = _usage_int(usage.get("cache_creation_input_tokens"))
    prompt_tokens = _non_cached + cache_read + cache_creation
    completion_tokens = _usage_int(usage.get("output_tokens"))
    oai_usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if cache_read:
        oai_usage["prompt_tokens_details"] = {"cached_tokens": cache_read}

    message: dict[str, Any] = {"role": "assistant"}
    if text_parts:
        message["content"] = "\n".join(text_parts)
    else:
        message["content"] = ""
    if reasoning_text:
        message["reasoning_content"] = reasoning_text
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish,
            }
        ],
        "usage": oai_usage,
    }


def _responses_part_to_anthropic(part: dict) -> dict | None:
    """Convertit une part Responses input_image/input_file → bloc Anthropic
    natif (image/document + title). Factorise la logique de
    openai_responses_to_anthropic (boucle principale + function_call_output
    liste). None si inconvertible. Défensif : jamais d'exception."""
    if not isinstance(part, dict):
        return None
    btype = part.get("type", "")
    if btype == "input_image":
        _r_img_url = part.get("image_url", "")
        _r_img_b64 = part.get("image_base64", "")
        _r_img_fid = part.get("file_id", "")
        if not isinstance(_r_img_url, str):
            _r_img_url = ""
        if not isinstance(_r_img_b64, str):
            _r_img_b64 = ""
        if not isinstance(_r_img_fid, str):
            _r_img_fid = ""
        if _r_img_url.startswith("data:"):
            try:
                _r_h, _, _r_d = _r_img_url[5:].partition(",")
                _r_m = (_r_h.split(";")[0] or "").strip() or "image/png"
            except Exception:
                _r_m, _r_d = "image/png", ""
            if _r_d:
                return {
                    "type": "image",
                    "source": {"type": "base64", "media_type": _r_m, "data": _r_d},
                }
            return None
        elif _r_img_url:
            return {"type": "image", "source": {"type": "url", "url": _r_img_url}}
        elif _r_img_b64:
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part.get("mime_type", "image/png"),
                    "data": _r_img_b64,
                },
            }
        elif _r_img_fid:
            return {"type": "image", "source": {"type": "file", "file_id": _r_img_fid}}
        return None
    if btype == "input_file":
        _r_fdata = part.get("file_data", "")
        _r_ffid = part.get("file_id", "")
        _r_furl = part.get("file_url", "")
        _r_fname = part.get("filename", "") or ""
        if not isinstance(_r_fdata, str):
            _r_fdata = ""
        if not isinstance(_r_ffid, str):
            _r_ffid = ""
        if not isinstance(_r_furl, str):
            _r_furl = ""
        if _r_fdata.startswith("data:"):
            try:
                _r_fh, _, _r_fd = _r_fdata[5:].partition(",")
                _r_fm = (_r_fh.split(";")[0] or "").strip() or "application/pdf"
            except Exception:
                _r_fm, _r_fd = "application/pdf", ""
            if _r_fd:
                _r_doc: dict = {
                    "type": "document",
                    "source": {"type": "base64", "media_type": _r_fm, "data": _r_fd},
                }
                if _r_fname:
                    _r_doc["title"] = _r_fname
                return _r_doc
            return None
        elif _r_fdata:
            _r_doc2: dict = {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": part.get("mime_type", "application/pdf"),
                    "data": _r_fdata,
                },
            }
            if _r_fname:
                _r_doc2["title"] = _r_fname
            return _r_doc2
        elif _r_ffid:
            return {"type": "document", "source": {"type": "file", "file_id": _r_ffid}}
        elif _r_furl:
            # [audit 2026-10-30] Anthropic only guarantees URL documents for
            # PDF; sending a .txt/.docx URL is a 400. Mirror P4: placeholder.
            _furl_path = str(_r_furl).split("?", 1)[0].lower()
            if not _furl_path.endswith(".pdf"):
                _debug(f"  [convert] DROP document URL non-PDF {_r_furl!r} → placeholder")
                return {"type": "text", "text": f"[document:url:{_r_furl}]"}
            _r_doc3: dict = {
                "type": "document",
                "source": {"type": "url", "url": _r_furl},
            }
            if _r_fname:
                _r_doc3["title"] = _r_fname
            return _r_doc3
        return None
    return None


