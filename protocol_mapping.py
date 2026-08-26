"""
Protocol mapping: Anthropic <-> OpenAI <-> Responses
Extracted from opencode.py (P3.10) - pure move, no behavior change.
"""

import hashlib
import json
import re
import time
import uuid
from collections import OrderedDict

from config import CACHE_MIN_PROMPT_SIZE, yaml_get
from dashboard.display import debug as _debug
from dashboard.display import log as _log

try:
    import config.settings as _cfg_settings
except ImportError:  # pragma: no cover

    class _CfgFallback:
        DEBUG = False

    _cfg_settings = _CfgFallback()

try:
    import tiktoken

    _encoding = tiktoken.get_encoding("cl100k_base")
except Exception:
    _encoding = None

# ── orjson fast-path (5-10x vs stdlib json on large bodies) ──
try:
    import orjson as _orjson  # type: ignore

    def _json_loads(b: bytes | str, **kw):
        if isinstance(b, str):
            b = b.encode()
        return _orjson.loads(b)

    def _json_dumps(obj, **kw) -> bytes:
        if kw.get("indent") is not None:
            return json.dumps(
                obj, ensure_ascii=False, indent=kw.get("indent"), default=str
            ).encode()
        return _orjson.dumps(obj)

    def _json_dumps_str(obj, **kw) -> str:
        if kw.get("indent") is not None:
            return json.dumps(obj, ensure_ascii=False, indent=kw.get("indent"), default=str)
        if kw:
            return _orjson.dumps(obj).decode()
        return _orjson.dumps(obj).decode()

    _JSON_LIB = "orjson"
except ImportError:

    def _json_loads(b: bytes | str, **kw):  # type: ignore[no-redef]
        if isinstance(b, bytes):
            b = b.decode()
        return json.loads(b, **kw)

    def _json_dumps(obj, **kw) -> bytes:  # type: ignore[no-redef]
        if "indent" in kw:
            return json.dumps(
                obj, ensure_ascii=False, indent=kw.get("indent"), default=str
            ).encode()
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()

    def _json_dumps_str(obj, **kw) -> str:  # type: ignore[no-redef]
        if "indent" in kw:
            return json.dumps(obj, ensure_ascii=False, indent=kw.get("indent"), default=str)
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

    _JSON_LIB = "json"


def _drop_orphan_tool_messages(messages: list[dict]) -> list[dict]:
    """Filter role:tool messages whose tool_call_id has no preceding tool_calls id."""
    _seen_ids: set[str] = set()
    filtered: list[dict] = []
    for m in messages:
        if m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tid = tc.get("id")
                if tid:
                    _seen_ids.add(tid)
            filtered.append(m)
        elif m.get("role") == "tool":
            cid = m.get("tool_call_id", "")
            if cid in _seen_ids:
                filtered.append(m)
            else:
                _debug(
                    f"  [orphan] DROP tool output call_id={cid!r} — no preceding tool_call (compaction or empty-name skip)"
                )
        else:
            filtered.append(m)
    return filtered


def _drop_orphan_responses_input(inp: list[dict]) -> list[dict]:
    """Filter function_call_output items whose call_id has no preceding function_call."""
    _known: set[str] = set()
    _filt: list[dict] = []
    for it in inp:
        t = it.get("type")
        if t == "function_call":
            cid = it.get("call_id") or it.get("id") or ""
            if cid:
                _known.add(cid)
            _filt.append(it)
        elif t == "function_call_output":
            cid = it.get("call_id") or ""
            if cid in _known:
                _filt.append(it)
            else:
                _debug(
                    f"  [orphan] DROP function_call_output call_id={cid!r} — no preceding function_call"
                )
        else:
            _filt.append(it)
    return _filt


def _extract_cache_tokens(usage: dict) -> int:
    details = usage.get("prompt_tokens_details") or {}
    if "cached_tokens" in details:
        return details["cached_tokens"]
    if "cached_tokens" in usage:
        return usage["cached_tokens"]
    if "cache_read_input_tokens" in usage:
        return usage["cache_read_input_tokens"]
    return 0


_thinking_cfg = yaml_get("thinking", "min_tokens", {})
THINKING_MODELS = (
    {k: int(v) for k, v in _thinking_cfg.items()}
    if isinstance(_thinking_cfg, dict)
    else {
        "deepseek-v4-flash": 2048,
        "deepseek-v4-pro": 4096,
    }
)


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for i in content:
            if isinstance(i, str):
                parts.append(i)
            elif isinstance(i, dict):
                if i.get("type") == "text":
                    parts.append(i.get("text", ""))
                elif i.get("type") == "thinking":
                    parts.append(i.get("thinking", ""))
                elif i.get("type") == "image":
                    parts.append(f"[image:{i.get('source', {}).get('type', 'unknown')}]")
                else:
                    parts.append(i.get("text", str(i)))
        return "\n".join(parts)
    return str(content) if content else ""


# ── Cache restructuration for models without semantic caching ──
CACHE_REWRITE_MODELS = set(
    yaml_get(
        "cache_rewrite_models",
        default=["mimo-v2.5", "mimo-v2-pro", "mimo-v2-omni", "mimo-v2.5-pro"],
    )
)


def _find_split_point(text: str) -> int:
    """Find the best split point between static instructions and dynamic content.

    Looks for the last double-newline in the first 8000 chars to split cleanly.
    Search up to 75% of text (capped at 8000) so split near boundary (e.g. 8000/13000)
    is found, while still keeping prefix stable for cache.
    """
    search_limit = min(8000, max(2000, len(text) * 3 // 4))
    # +2 to include delimiter starting at exactly search_limit (rfind end is exclusive)
    last_double = text.rfind("\n\n", 0, search_limit + 2)
    if last_double > 500:
        return last_double
    last_newline = text.rfind("\n", 0, search_limit + 1)
    if last_newline > 500:
        return last_newline
    return 0


def _effort_to_reasoning(effort_level: str, model: str) -> str:
    """Map generic effort (xhigh/high/medium/low) to model-specific reasoning_effort.

    Deduplicates logic at 477 and 932 (audit F-M7): single source for glm-5/deepseek/mimo.
    """
    if model.startswith("glm-5"):
        if effort_level in ("xhigh", "max", "high"):
            return "high"
        if effort_level == "medium":
            return "medium"
        return "low"
    if model.startswith("deepseek-v4"):
        return "max" if effort_level in ("xhigh", "max") else "high"
    # mimo etc.
    if effort_level in ("xhigh", "max", "high"):
        return "high"
    if effort_level == "medium":
        return "medium"
    return "low"


def _restructure_for_cache(oai_body: dict, model_id: str) -> dict:
    """For models without semantic caching, split the system prompt.

    Keeps the static part (instructions + tools) as the system message with
    cache_control, and moves the dynamic part (conversation history) into
    the messages array so the prefix stays stable across requests.
    """
    if model_id not in CACHE_REWRITE_MODELS:
        return oai_body

    messages = oai_body.get("messages", [])
    if not messages:
        return oai_body

    # Find the system message
    sys_idx = None
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            sys_idx = i
            break

    if sys_idx is None:
        return oai_body

    sys_content = messages[sys_idx].get("content", "")
    if not isinstance(sys_content, str) or len(sys_content) < CACHE_MIN_PROMPT_SIZE:
        _debug(
            f"  [cache-restructure] skipped: sys_content len={len(sys_content) if isinstance(sys_content, str) else 0} < min={CACHE_MIN_PROMPT_SIZE}"
        )
        return oai_body  # Small prompt, no need to restructure

    split_point = _find_split_point(sys_content)
    if split_point <= 0:
        _debug(
            f"  [cache-restructure] skipped: no valid split point found in {len(sys_content)} chars"
        )
        return oai_body

    static_part = sys_content[:split_point].strip()
    dynamic_part = sys_content[split_point:].strip()

    # Rebuild: static system message with cache_control + dynamic as user message
    new_messages = [
        {
            "role": "system",
            "content": static_part,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    if dynamic_part:
        new_messages.append({"role": "user", "content": dynamic_part})

    # Append original messages (skip the old system message)
    for i, m in enumerate(messages):
        if i != sys_idx:
            new_messages.append(m)

    oai_body["messages"] = new_messages
    _debug(
        f"  [cache-restructure] split at point={split_point}: static={len(static_part)} dynamic={len(dynamic_part)} chars"
    )
    _log(
        f"  [cache] split system prompt: static={len(static_part)} dynamic={len(dynamic_part)} chars"
    )
    return oai_body


def _strip_billing_header(text: str) -> str:
    """Remove x-anthropic-billing-header from system prompt.

    Claude Code injects a billing header with a changing hash (cch=...) that
    breaks prompt caching by modifying the prefix on every request.
    """
    if not text.startswith("x-anthropic-billing-header:"):
        return text
    # Strip the first line (the header) and any trailing blank line
    first_nl = text.find("\n")
    if first_nl == -1:
        return text
    rest = text[first_nl + 1 :]
    if rest.startswith("\n"):
        rest = rest[1:]
    return rest


def anthropic_to_openai(body: dict, model: str) -> dict:
    thinking = isinstance(body.get("thinking"), dict) and body["thinking"].get("type") in (
        "enabled",
        "adaptive",
    )
    # GLM-5.x models don't support cache_control — skip it
    supports_cache_control = not model.startswith("glm-5")

    messages = []

    # System prompt — always add cache_control for prefix caching
    system_val = body.get("system", "")
    if isinstance(system_val, list):
        text = _extract_text(system_val)
        if text:
            text = _strip_billing_header(text)
            msg = {"role": "system", "content": text}
            if supports_cache_control:
                msg["cache_control"] = {"type": "ephemeral"}
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

        text_parts, tool_calls, thinking_parts, tool_results = [], [], [], []
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
                # [PLAN-raisonnement Phase D.4] donnée chiffrée authentique :
                # préservée telle quelle vers Anthropic (passthrough), strippée
                # vers les autres upstreams (non déchiffrable, non interprétable)
                _debug("  [convert] DROP redacted_thinking → upstream non-Anthropic")
                continue
            elif btype == "tool_use":
                _tool_name = block.get("name", "")
                if not isinstance(_tool_name, str) or not _tool_name.strip():
                    _debug(f"  [convert] SKIP tool_use with empty name id={block.get('id', '?')}")
                    continue
                tool_calls.append(
                    {
                        "id": block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
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
                    _debug(
                        "  [compact] SKIP tool_result with missing tool_use_id in anthropic_to_openai"
                    )
                    continue
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": _extract_text(block.get("content", "")),
                    }
                )
                if "cache_control" in block:
                    last_cache_control = block["cache_control"]

        # Emit tool_result messages first (must immediately follow assistant's tool_calls)
        messages.extend(tool_results)

        # Then emit the main message (text + tool_calls + thinking)
        joined_thinking = "\n".join(thinking_parts) if thinking_parts else ""
        if tool_calls:
            out = {
                "role": role,
                "content": "\n".join(text_parts) if text_parts else "",
                "tool_calls": tool_calls,
            }
            if joined_thinking:
                out["reasoning_content"] = joined_thinking
            elif thinking and is_asst:
                out["reasoning_content"] = " "
            if last_cache_control and not is_asst:
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
    messages = _drop_orphan_tool_messages(messages)

    # Add cache_control to the last user message for optimal prefix caching
    # (Anthropic best practice: cache system + last user turn)
    if supports_cache_control:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                messages[i]["cache_control"] = {"type": "ephemeral"}
                break

    # Build request
    oai = {
        "model": model,
        "messages": messages,
        "max_tokens": body.get("max_tokens", 16384),
        "stream": body.get("stream", False),
    }

    for key, oai_key in [
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stop_sequences", "stop"),
    ]:
        if key in body:
            oai[oai_key] = body[key]

    if "tools" in body:
        # Support both Anthropic format (name at top level) and OpenAI format (function.name)
        # v3.3: preserve server tools (web_search/web_fetch) natively, fix else branch B4
        oai_tools = []
        for t in body["tools"]:
            # Server tools: preserve natively if type is web_*
            t_type = t.get("type", "")
            if isinstance(t_type, str) and t_type.startswith("web_"):
                # Keep server tool as-is (e.g., web_search_2025_03_05)
                # If converting to OpenAI and target is anthropic-native, preserve; otherwise keep type
                if "name" in t and t.get("name"):
                    oai_tools.append({"type": t_type, "name": t.get("name"), "description": t.get("description", ""), "input_schema": t.get("input_schema", {})})
                else:
                    # type without name, e.g., {"type":"web_search_2025_03_05"} -> keep
                    oai_tools.append({"type": t_type, "name": t.get("name", "web_search")})
                continue
            if "name" in t:
                # Anthropic format: {"name": "...", "description": "...", "input_schema": {...}}
                oai_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t.get("input_schema", {}),
                        },
                    }
                )
            elif "function" in t:
                # OpenAI format: {"type": "function", "function": {"name": "...", ...}}
                fn = t["function"]
                oai_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": fn.get("name", ""),
                            "description": fn.get("description", ""),
                            "parameters": fn.get("parameters", {}),
                        },
                    }
                )
            else:
                # Unknown format: B4 fix - don't produce {"name":""}; check if web_* type without name
                if isinstance(t_type, str) and t_type.startswith("web_"):
                    oai_tools.append({"type": t_type, "name": t.get("name", "web_search")})
                elif t.get("name"):
                    oai_tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": t.get("name", ""),
                                "description": t.get("description", ""),
                                "parameters": t.get("input_schema", t.get("parameters", {})),
                            },
                        }
                    )
                else:
                    # skip invalid tool without name
                    _debug(f"  [convert] SKIP tool without name type={t_type!r}")
                    continue
        if oai_tools:
            oai["tools"] = oai_tools
        tc = body.get("tool_choice", "auto")
        if isinstance(tc, dict):
            tc_type = tc.get("type", "auto")
            if tc_type == "tool":
                oai["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}
            elif tc_type == "any":
                oai["tool_choice"] = "required"
            else:
                oai["tool_choice"] = "auto"
        else:
            oai["tool_choice"] = tc

    # Convert Anthropic thinking/effort → OpenAI reasoning parameters
    # Claude Code sends: thinking: {type: "adaptive"} OR effort: "low"/"medium"/"high"/"xhigh"/"max"
    effort_level = body.get("effort")
    thinking_param = body.get("thinking") if isinstance(body.get("thinking"), dict) else {}
    ttype = thinking_param.get("type", "")
    budget = thinking_param.get("budget_tokens", 0)

    if effort_level and effort_level != "none":
        wants_thinking = True
    elif ttype in ("enabled", "adaptive") or budget > 0:
        wants_thinking = True
        if budget >= 16000 or budget == 0:
            effort_level = "xhigh"
        elif budget >= 10000:
            effort_level = "high"
        elif budget >= 4000:
            effort_level = "medium"
        elif ttype == "adaptive":
            effort_level = "medium"
        else:
            effort_level = "low"
    else:
        wants_thinking = False

    if wants_thinking:
        oai["reasoning_effort"] = _effort_to_reasoning(effort_level, model)
        _debug(
            f"  [thinking] {model}: reasoning_effort={oai['reasoning_effort']} (effort={effort_level})"
        )

    # Restructure system prompt for models without semantic caching
    oai = _restructure_for_cache(oai, model)
    return oai


_orig_anthropic_to_openai = anthropic_to_openai
# [P4] LRU borné : OrderedDict move-to-end + drop-oldest (l'ancien dict
# gelerait le contenu à 512 entrées — plus aucun nouveau body mis en cache).
_anthropic_cache: OrderedDict = OrderedDict()
_anthropic_cache_max = 512


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


def anthropic_to_openai(body: dict, model: str, raw: bytes | None = None) -> dict:
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
            # [C1] shallow copy TOP-LEVEL uniquement : audit plan §3 — les
            # mutations post-conversion des callers touchent des clés racine
            # (model / stream_options / min_tokens), jamais les structures
            # imbriquées partagées. Fini les deepcopy hit ET miss.
            return dict(hit)
        res = _orig_anthropic_to_openai(body, model)
        # Objet stocké JAMAIS exposé tel quel (le caller reçoit une copie
        # racine) → le cache reste pristine sans deepcopy.
        _anthropic_cache[key] = res
        while len(_anthropic_cache) > _anthropic_cache_max:
            _anthropic_cache.popitem(last=False)  # drop-oldest
        return dict(res)
    except Exception:
        return _orig_anthropic_to_openai(body, model)


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
    return base64.b64encode(
        hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()
    ).decode()


def _is_local_signature(text: str, signature: str) -> bool:
    """[PLAN-raisonnement Phase D] Détecte une signature FORGÉE par le proxy.

    Provenance stateless : on recalcule le HMAC local du texte et on compare.
    Une signature authentique (Anthropic) ne peut pas correspondre — elle
    n'est pas produite par notre clé. Un bloc thinking re-émis par le client
    avec NOTRE signature est donc identifiable sans table d'état."""
    if not isinstance(signature, str) or not signature:
        return False
    if not isinstance(text, str) or not text:
        return False
    try:
        import hmac as _hmac

        return _hmac.compare_digest(_local_signature(text), signature)
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
    return stripped


def openai_to_anthropic(resp: dict, model: str) -> dict:
    choice = resp.get("choices", [{}])[0]
    msg = choice.get("message", {})
    usage = resp.get("usage", {})

    blocks = []
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
        try:
            inp = _json_loads(fn.get("arguments", "{}"))
        except Exception:
            inp = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
                "name": fn.get("name", ""),
                "input": inp,
            }
        )

    if not blocks:
        blocks.append({"type": "text", "text": ""})

    stop = "tool_use" if msg.get("tool_calls") else "end_turn"
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
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": usage.get("prompt_tokens_details", {}).get(
                "cached_tokens", 0
            ),
        },
    }


def openai_to_anthropic_request(oai_body: dict) -> dict:
    """Convert OpenAI Chat Completions request → Anthropic Messages format."""
    system_text = ""
    pending_tool_results = []
    anthro_messages = []

    for msg in oai_body.get("messages", []):
        role = msg.get("role", "")

        if role == "system":
            system_text = _extract_text(msg.get("content", ""))
            continue

        if role == "tool":
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": _extract_text(msg.get("content", "")),
                }
            )
            continue

        if role not in ("user", "assistant"):
            continue

        blocks = []

        # Prepend pending tool_results to the next user message
        if role == "user" and pending_tool_results:
            blocks.extend(pending_tool_results)
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

        # Convert tool_calls (assistant only)
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                inp = _json_loads(fn.get("arguments", "{}"))
            except Exception:
                inp = {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
                    "name": fn.get("name", ""),
                    "input": inp,
                }
            )

        # [PLAN-raisonnement Phase D.3] PAS de conversion reasoning_content →
        # thinking ici : ce corps part vers un UPSTREAM Anthropic, qui valide
        # cryptographiquement les signatures. Forger une signature locale
        # produirait un 400 au tour suivant ; le raisonnement est simplement
        # omis de l'historique (jamais de fausse signature vers Anthropic).
        if isinstance(msg.get("reasoning_content") or msg.get("reasoning"), str) and (
            msg.get("reasoning_content") or msg.get("reasoning")
        ).strip():
            _debug(
                "  [convert] DROP reasoning_content historique → upstream Anthropic "
                "(pas de signature forgée)"
            )

        # Ensure at least one block
        if not blocks:
            blocks.append({"type": "text", "text": ""})

        anthro_messages.append({"role": role, "content": blocks})

    # Trailing tool_results (edge case)
    if pending_tool_results:
        anthro_messages.append({"role": "user", "content": pending_tool_results})

    result = {
        "model": oai_body.get("model", ""),
        "messages": anthro_messages,
        "max_tokens": oai_body.get("max_tokens", 16384),
        "stream": oai_body.get("stream", False),
    }

    if system_text:
        result["system"] = system_text

    # Map simple params
    for key, anthro_key in [
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stop", "stop_sequences"),
    ]:
        if key in oai_body:
            result[anthro_key] = oai_body[key]

    # Convert tools - v3.3: preserve server tools B4
    if "tools" in oai_body:
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
                anthro_tools.append({"name": name, "description": t.get("description", ""), "input_schema": t.get("input_schema", t.get("parameters", {}))})
                continue
            if t.get("type") == "function":
                fn = t.get("function", {})
                if not fn.get("name"):
                    _debug("  [convert] SKIP function tool without name")
                    continue
                anthro_tools.append(
                    {
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "input_schema": fn.get("parameters", {}),
                    }
                )
            elif "function" in t:
                fn = t["function"]
                if not fn.get("name"):
                    continue
                anthro_tools.append(
                    {
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "input_schema": fn.get("parameters", {}),
                    }
                )
        if anthro_tools:
            result["tools"] = anthro_tools

        # Convert tool_choice
        tc = oai_body.get("tool_choice", "auto")
        if isinstance(tc, dict):
            tc_type = tc.get("type", "auto")
            if tc_type == "function":
                result["tool_choice"] = {
                    "type": "tool",
                    "name": tc.get("function", {}).get("name", ""),
                }
            elif tc_type == "any":
                result["tool_choice"] = {"type": "any"}
            else:
                result["tool_choice"] = tc_type
        else:
            result["tool_choice"] = tc

    return result


def anthropic_to_openai_response(anthro: dict, model: str) -> dict:
    """Convert Anthropic Messages response → OpenAI Chat Completions format."""
    content_blocks = anthro.get("content", [])
    text_parts = []
    reasoning_text = ""
    tool_calls = []

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
                    "id": block.get("id", f"call_{uuid.uuid4().hex[:12]}"),
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
    else:
        finish = "stop"

    # Usage mapping
    usage = anthro.get("usage", {})
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)
    oai_usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    cache_read = usage.get("cache_read_input_tokens", 0)
    if cache_read:
        oai_usage["prompt_tokens_details"] = {"cached_tokens": cache_read}

    message = {"role": "assistant"}
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


def openai_responses_to_anthropic(body: dict) -> dict:
    """Convert OpenAI Responses API request → Anthropic Messages format."""
    system_text = ""
    pending_tool_results = []
    anthro_messages = []

    for item in body.get("input", []):
        if not isinstance(item, dict):
            continue
        role = item.get("role", item.get("type", "user"))

        if role in ("system", "developer"):
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "input_text":
                    system_text += block.get("text", "")
            continue

        if item.get("type") == "function_call_output":
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": item.get("call_id", item.get("id", "")),
                    "content": item.get("output", ""),
                }
            )
            continue

        # Convert previous-turn function_call items to assistant tool_use blocks
        if item.get("type") == "function_call":
            try:
                inp = _json_loads(item.get("arguments", "{}"))
            except Exception:
                inp = {}
            anthro_messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": item.get("call_id")
                            or item.get("id")
                            or f"toolu_{uuid.uuid4().hex[:12]}",
                            "name": item.get("name", ""),
                            "input": inp,
                        }
                    ],
                }
            )
            continue

        if role not in ("user", "assistant"):
            continue

        blocks = []
        if role == "user" and pending_tool_results:
            blocks.extend(pending_tool_results)
            pending_tool_results = []

        for block in item.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype in ("input_text", "text"):
                blocks.append({"type": "text", "text": block.get("text", "")})
            elif btype == "reasoning":
                # [PLAN-raisonnement Phase D.3] pas de thinking forgé vers
                # l'upstream Anthropic (signature cryptographique exigée) —
                # le summary est omis de l'historique.
                _summary = block.get("summary") or []
                _has_text = any(
                    isinstance(s, dict) and s.get("text") for s in _summary
                )
                if _has_text:
                    _debug(
                        "  [convert] DROP reasoning summary historique → upstream Anthropic "
                        "(pas de signature forgée)"
                    )

        if not blocks:
            blocks.append({"type": "text", "text": ""})
        anthro_messages.append({"role": role, "content": blocks})

    if pending_tool_results:
        anthro_messages.append({"role": "user", "content": pending_tool_results})

    result = {
        "model": body.get("model", ""),
        "messages": anthro_messages,
        "max_tokens": body.get("max_output_tokens", 16384),
        "stream": body.get("stream", False),
    }
    if system_text:
        result["system"] = system_text
    if "temperature" in body:
        result["temperature"] = body["temperature"]
    if "top_p" in body:
        result["top_p"] = body["top_p"]

    # Convert tools (strip "type": "function" wrapper)
    if "tools" in body:
        result["tools"] = []
        for t in body["tools"]:
            if isinstance(t, dict) and t.get("type") == "function":
                tool = {"name": t["name"], "description": t.get("description", "")}
                tool["input_schema"] = t.get("input_schema") or t.get("parameters") or {}
                result["tools"].append(tool)
        tc = body.get("tool_choice", "auto")
        if isinstance(tc, dict):
            tc_type = tc.get("type", "auto")
            if tc_type == "function":
                result["tool_choice"] = {"type": "tool", "name": tc.get("name", "")}
            else:
                result["tool_choice"] = tc_type
        else:
            result["tool_choice"] = tc

    # Convert Anthropic thinking/effort -> model-specific reasoning parameter
    # Claude Code sends: thinking: {type: "adaptive"} OR effort: "low"/"medium"/"high"/"xhigh"/"max"
    effort_level = body.get("effort")
    thinking = body.get("thinking") if isinstance(body.get("thinking"), dict) else {}
    ttype = thinking.get("type", "")
    budget = thinking.get("budget_tokens", 0)

    # Determine desired effort from effort param or thinking param
    if effort_level and effort_level != "none":
        wants_thinking = True
    elif ttype in ("enabled", "adaptive") or budget > 0:
        wants_thinking = True
        # Map deprecated budget_tokens -> effort level
        if budget >= 16000 or budget == 0:
            effort_level = "xhigh"
        elif budget >= 10000:
            effort_level = "high"
        elif budget >= 4000:
            effort_level = "medium"
        elif ttype == "adaptive":
            effort_level = "medium"
        else:
            effort_level = "low"
    else:
        wants_thinking = False

    if wants_thinking:
        _model = result.get("model", "")
        result["reasoning_effort"] = _effort_to_reasoning(effort_level, _model)
        _debug(
            f"  [thinking] {_model}: reasoning_effort={result['reasoning_effort']} (effort={effort_level})"
        )

    return result


def anthropic_to_openai_responses(anthro: dict, model: str) -> dict:
    """Convert Anthropic Messages response → OpenAI Responses API format."""
    content_blocks = anthro.get("content", [])
    output_items = []
    text_content = []
    function_calls = []

    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "text":
            text_content.append({"type": "output_text", "text": block.get("text", "")})
        elif btype == "thinking":
            output_items.insert(
                0,
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": block.get("thinking", "")}],
                },
            )
        elif btype == "tool_use":
            function_calls.append(
                {
                    "type": "function_call",
                    "call_id": block.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                    "name": block.get("name", ""),
                    "arguments": _json_dumps_str(block.get("input", {}), ensure_ascii=False),
                    "status": "completed",
                }
            )

    if text_content:
        output_items.append({"type": "message", "role": "assistant", "content": text_content})
    output_items.extend(function_calls)

    # Status mapping
    sr = anthro.get("stop_reason", "")
    if sr == "max_tokens":
        status = "incomplete"
    else:
        status = "completed"

    # Usage mapping
    usage = anthro.get("usage", {})
    in_t = usage.get("input_tokens", 0)
    out_t = usage.get("output_tokens", 0)
    oai_usage = {
        "input_tokens": in_t,
        "output_tokens": out_t,
        "total_tokens": in_t + out_t,
        "output_tokens_details": {
            "reasoning_tokens": 0,
            "cached_tokens": usage.get("cache_read_input_tokens", 0),
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


def openai_chat_to_responses(chat_resp: dict, model: str) -> dict:
    """Convert OpenAI Chat Completions response directly to OpenAI Responses API format.

    Bypasses the intermediate Anthropic format to avoid data loss and unnecessary conversion.
    """
    choice = chat_resp.get("choices", [{}])[0]
    msg = choice.get("message", {})
    usage = chat_resp.get("usage", {})

    output_items = []

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
    if content:
        output_items.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content}],
            }
        )

    # Tool calls -> function_call items
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        output_items.append(
            {
                "type": "function_call",
                "call_id": tc.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                "name": fn.get("name", ""),
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
        "output_tokens_details": {
            "reasoning_tokens": 0,
            "cached_tokens": cached,
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


def _chat_to_responses_request(chat: dict) -> dict:
    if "input" in chat and "messages" not in chat:
        return dict(chat)
    inp = []
    _has_reasoning_items = False
    for m in chat.get("messages", []) or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        # Preserve cache_control from the chat message for prefix caching
        cache_ctrl = m.get("cache_control")
        # Tool results must be function_call_output only — never a "role": "tool" input_text (invalid for Responses)
        if role == "tool":
            cid = m.get("tool_call_id", "")
            if not cid:
                continue
            inp.append(
                {
                    "type": "function_call_output",
                    "call_id": cid,
                    "output": content if isinstance(content, str) else str(content),
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
                    item["cache_control"] = cache_ctrl
                inp.append(item)
        elif isinstance(content, list):
            txt = "\n".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if txt:
                ctype = "output_text" if role == "assistant" else "input_text"
                item = {"role": role, "content": [{"type": ctype, "text": txt}]}
                if cache_ctrl:
                    item["cache_control"] = cache_ctrl
                inp.append(item)
        for tc in m.get("tool_calls", []) or []:
            fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
            _fn_name = fn.get("name", "")
            if not isinstance(_fn_name, str) or not _fn_name.strip():
                _debug(
                    f"  [convert] SKIP function_call with empty name call_id={tc.get('call_id') or tc.get('id', '?')}"
                )
                continue
            _cid = tc.get("call_id") or tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
            inp.append(
                {
                    "type": "function_call",
                    "call_id": _cid,
                    "name": _fn_name.strip(),
                    "arguments": fn.get("arguments", "{}"),
                }
            )
    # Orphan filter for Responses input
    inp = _drop_orphan_responses_input(inp)

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
    if _has_reasoning_items:
        req["_has_synthetic_reasoning_items"] = True
    if "max_tokens" in chat:
        req["max_output_tokens"] = chat["max_tokens"]
    if "max_output_tokens" in chat:
        req["max_output_tokens"] = chat["max_output_tokens"]
    for k in ("temperature", "top_p"):
        if k in chat:
            req[k] = chat[k]
    # Forward reasoning parameters to Responses API format
    if "reasoning_effort" in chat:
        effort = chat["reasoning_effort"]
        # summary:auto is required to get visible reasoning summary; without it
        # upstream returns only encrypted_content and proxy emits placeholder.
        req["reasoning"] = {"summary": "auto", "effort": effort}
    elif "reasoning" in chat:
        req["reasoning"] = chat["reasoning"]
    if "tools" in chat:
        tools = []
        for t in chat["tools"]:
            if isinstance(t, dict) and "function" in t:
                fn = t["function"]
                tools.append(
                    {
                        "type": "function",
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                    }
                )
        if tools:
            req["tools"] = tools
        tc = chat.get("tool_choice")
        if tc is not None:
            if isinstance(tc, dict) and tc.get("type") == "function":
                req["tool_choice"] = {"type": "function", "name": tc["function"].get("name", "")}
            else:
                req["tool_choice"] = tc
    return req


def _anthropic_to_responses_request(anthro: dict) -> dict:
    if "input" in anthro and "messages" not in anthro:
        return dict(anthro)
    chat = anthropic_to_openai(anthro, anthro.get("model", ""))
    return _chat_to_responses_request(chat)


def _responses_to_chat_response(resp: dict, model: str) -> dict:
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
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                }
            )
    # vrai seulement : pas de placeholder si pas de summary visible
    msg = {"role": "assistant", "content": "\n".join(texts)}
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = tool_calls
    usage = resp.get("usage", {}) if isinstance(resp.get("usage"), dict) else {}
    # Cache tokens come from input_tokens_details, NOT output_tokens_details
    _inp_details = (
        usage.get("input_tokens_details")
        if isinstance(usage.get("input_tokens_details"), dict)
        else {}
    )
    _cached = _inp_details.get("cached_tokens", 0)
    chat_usage = {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get(
            "total_tokens", usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        ),
    }
    if _cached:
        chat_usage["prompt_tokens_details"] = {"cached_tokens": _cached}
    status = resp.get("status", "completed")
    finish = "stop" if status == "completed" else "length"
    return {
        "id": resp.get("id", f"chatcmpl-{uuid.uuid4().hex[:8]}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": chat_usage,
    }


def _responses_to_anthropic_response(resp: dict, model: str) -> dict:
    blocks = []
    for item in resp.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "reasoning":
            for s in item.get("summary", []) or []:
                if isinstance(s, dict) and s.get("text"):
                    blocks.append(
            {
                "type": "thinking",
                "thinking": s.get("text", ""),
                "signature": _local_signature(s.get("text", "")),
            }
        )
        elif item.get("type") == "message":
            for blk in item.get("content", []) or []:
                if isinstance(blk, dict) and blk.get("type") == "output_text" and blk.get("text"):
                    blocks.append({"type": "text", "text": blk.get("text", "")})
        elif item.get("type") == "function_call":
            try:
                inp = _json_loads(item.get("arguments", "{}"))
            except Exception:
                inp = {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": item.get("call_id", item.get("id", "")),
                    "name": item.get("name", ""),
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
    # Cache tokens come from input_tokens_details, NOT output_tokens_details
    _inp_details = (
        usage.get("input_tokens_details")
        if isinstance(usage.get("input_tokens_details"), dict)
        else {}
    )
    _cache_read = _inp_details.get("cached_tokens", 0)
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": blocks,
        "model": model,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_input_tokens": _cache_read,
        },
    }


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


class ResponsesSseState:
    """État de conversion SSE Responses-API pour UN stream."""

    __slots__ = ("tool_cache", "tool_index_map", "reasoning_seen")

    def __init__(self) -> None:
        self.tool_cache: dict = {}
        self.tool_index_map: dict = {}
        self.reasoning_seen: set = set()

    def reset(self) -> None:
        self.tool_cache.clear()
        self.tool_index_map.clear()
        self.reasoning_seen.clear()


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
    else:
        tool_cache = _responses_tool_cache
        tool_index_map = _responses_tool_index_map
        reasoning_seen = _reasoning_seen_ids

    def _clear_state() -> None:
        if state is not None:
            state.reset()
        else:
            _responses_tool_cache.clear()
            _responses_tool_index_map.clear()
            _reasoning_seen_ids.clear()

    etype = chunk.get("type", "")
    if _cfg_settings.DEBUG:
        # [B4 perf] f-string par delta — construite seulement quand DEBUG on
        _debug(f"  [responses-sse] event type={etype!r} keys={list(chunk.keys())[:8]}")

    # response.output_text.delta — direct text delta (Responses API streaming format)
    if etype == "response.output_text.delta":
        text = chunk.get("delta", "")
        if not text:
            return None
        return {"choices": [{"delta": {"content": text}, "finish_reason": None}]}

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
            call_id = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:12]}"
            name = item.get("name", "")
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
                    "delta": {
                        "tool_calls": [{"index": tool_idx, "function": {"arguments": delta}}]
                    },
                    "finish_reason": None,
                }
            ]
        }

    # response.function_call_arguments.done — final tool arguments (optional, ensure completeness)
    if etype == "response.function_call_arguments.done":
        # This contains the full arguments but deltas already streamed; we can skip or emit final check
        # Don't emit extra delta if already streamed via .delta events; just ensure cache is updated
        iid = chunk.get("item_id", "")
        chunk.get("arguments", "")
        name = chunk.get("name", "")
        if iid and iid in tool_cache and name:
            tool_cache[iid]["name"] = name
        return None

    # response.output_item.done — reasoning item final with summary array (fallback + 100% guarantee)
    if etype == "response.output_item.done":
        item = chunk.get("item", {}) if isinstance(chunk.get("item"), dict) else {}
        if isinstance(item, dict) and item.get("type") == "reasoning":
            iid = item.get("id", "") or f"rs_{chunk.get('output_index', 0)}"
            # dedupe: if any summary part for this item already emitted, skip (check prefix)
            if iid and any(k == iid or k.startswith(f"{iid}:") for k in reasoning_seen):
                return None
            summary = item.get("summary", [])
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
                _debug(f"  [responses-sse] output_item.done no visible summary, skip (vrai seulement) iid={iid!r} encrypted={bool(item.get('encrypted_content'))}")
                return None
            if reasoning:
                if iid:
                    reasoning_seen.add(iid)
                    # also mark per-index to prevent double emit from summary_text.done
                    reasoning_seen.add(f"{iid}:0")
                _debug(f"  [responses-sse] output_item.done reasoning fallback len={len(reasoning)} iid={iid!r}")
                return {"choices": [{"delta": {"reasoning_content": reasoning}, "finish_reason": None}]}
        return None

    # response.completed — final event with usage
    if etype == "response.completed":
        # Clear tool cache + reasoning dedupe for next request
        _clear_state()
        resp = chunk.get("response", {})
        usage = resp.get("usage", {})
        # Cache tokens come from input_tokens_details, NOT output_tokens_details
        _inp_details = (
            usage.get("input_tokens_details")
            if isinstance(usage.get("input_tokens_details"), dict)
            else {}
        )
        _cached = _inp_details.get("cached_tokens", 0)
        chat_usage = {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get(
                "total_tokens", usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            ),
        }
        if _cached:
            chat_usage["prompt_tokens_details"] = {"cached_tokens": _cached}
        return {"choices": [], "usage": chat_usage}

    # response.incomplete — model didn't generate output, treat as stream end
    if etype == "response.incomplete":
        _clear_state()
        _debug("  [responses-sse] response.incomplete received — model produced no output")
        resp = chunk.get("response", {})
        usage = resp.get("usage", {}) if isinstance(resp.get("usage"), dict) else {}
        chat_usage = {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get(
                "total_tokens", usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            ),
        }
        return {"choices": [], "usage": chat_usage}

    # All other event types (response.created, response.in_progress,
    # response.output_item.done, response.content_part.added/done, etc.) — skip
    return None
