"""
Claude Code Proxy → opencode.ai
Convert Anthropic /v1/messages ↔ OpenAI chat/completions
"""

import json
import uuid
import time
import logging
import os
import sqlite3
import threading
import traceback
import asyncio
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse

from config import API_KEY, PROXY, MODELS, ROUTES, get_model_config, HOST, PORT, WEB_PORT, DISABLE_MAPPING, API_KEYS, API_KEY_ROUTING, CUSTOM_ROUTES

import itertools

# ── API key routing ──
_key_cycle = None
_key_cycle_keys = []
_key_failover_index = 0

def _get_enabled_keys() -> list[dict]:
    return [k for k in API_KEYS if k.get("enabled", True)]

def get_next_api_key() -> dict:
    global _key_cycle, _key_cycle_keys, _key_failover_index
    if not API_KEYS:
        return {"api_key": API_KEY}
    enabled = _get_enabled_keys()
    if not enabled:
        return {"api_key": API_KEY}
    if len(enabled) == 1:
        return enabled[0]
    if API_KEY_ROUTING == "failover":
        for i in range(len(API_KEYS)):
            idx = (_key_failover_index + i) % len(API_KEYS)
            if API_KEYS[idx].get("enabled", True):
                return API_KEYS[idx]
        return {"api_key": API_KEY}
    # rebuild cycle if enabled keys changed (e.g. toggled via dashboard)
    current_ids = [k.get("api_key") for k in enabled]
    if _key_cycle is None or _key_cycle_keys != current_ids:
        _key_cycle = itertools.cycle(enabled)
        _key_cycle_keys = current_ids
    return next(_key_cycle)

def _find_alternative_key(failed_key: str) -> dict | None:
    """Return the first enabled key different from failed_key, or None."""
    for k in API_KEYS:
        if k.get("api_key") != failed_key and k.get("enabled", True):
            return k
    return None

def advance_failover():
    global _key_failover_index
    if API_KEYS and API_KEY_ROUTING == "failover":
        _key_failover_index = (_key_failover_index + 1) % len(API_KEYS)

def _alias_for_key(api_key: str) -> str:
    """Look up the alias for a given API key. Returns empty string if not found."""
    for k in API_KEYS:
        if k.get("api_key") == api_key:
            return k.get("alias", "") or ""
    return ""

def _key_from_headers(headers: dict, protocol: str) -> str:
    """Extract the API key from request headers."""
    if protocol == "anthropic":
        return headers.get("x-api-key", "")
    return headers.get("Authorization", "").replace("Bearer ", "")

def _get_auth_headers(protocol: str, entry: dict | None = None) -> dict:
    if entry is None:
        entry = get_next_api_key()
    ak = entry.get("api_key", API_KEY)
    if protocol == "anthropic":
        return {"x-api-key": ak, "Content-Type": "application/json",
                "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {ak}", "Content-Type": "application/json"}


try:
    import tiktoken
    _encoding = tiktoken.get_encoding("cl100k_base")
except Exception:
    _encoding = None

from dashboard import register_dashboard
from dashboard import start_quota_fetcher
from dashboard.display import log as _log, RichLogHandler, run_terminal_loop
from dashboard.events import get_event_manager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# SQLite setup
_db_path = os.path.join(LOG_DIR, "requests.db")
_conn = sqlite3.connect(_db_path, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA busy_timeout=5000")
_db_lock = asyncio.Lock()
_conn.execute("""
    CREATE TABLE IF NOT EXISTS requests (
        id TEXT PRIMARY KEY,
        timestamp TEXT NOT NULL,
        model TEXT NOT NULL,
        original_model TEXT,
        duration_ms INTEGER,
        tokens_input INTEGER,
        tokens_output INTEGER,
        tokens_cache INTEGER,
        success INTEGER,
        error TEXT,
        protocol TEXT,
        is_stream INTEGER,
        thinking TEXT,
        effort TEXT
    )
""")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON requests(timestamp)")
for col, default in [("protocol", "NULL"), ("is_stream", "0"), ("thinking", "NULL"), ("effort", "NULL"), ("client_ip", "NULL"), ("account_alias", "NULL"), ("tools", "NULL")]:
    try:
        _conn.execute(f"ALTER TABLE requests ADD COLUMN {col} TEXT DEFAULT {default}")
    except Exception:
        pass
_conn.commit()


async def _save_request(req_id, model, original_model, duration_ms,
	                  tokens_input, tokens_output, tokens_cache, success=True, error=None,
	                  protocol=None, is_stream=False, thinking=None, effort=None,
	                  client_ip=None, account_alias=None, tools=None):
    tools_json = json.dumps(tools) if tools else "[]"
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    async with _db_lock:
        _conn.execute("""
            INSERT OR REPLACE INTO requests (id, timestamp, model, original_model, duration_ms,
                tokens_input, tokens_output, tokens_cache, success, error,
                protocol, is_stream, thinking, effort, client_ip, account_alias, tools)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (req_id, timestamp, model, original_model, duration_ms,
              tokens_input, tokens_output, tokens_cache, 1 if success else 0, error,
              protocol, 1 if is_stream else 0, thinking, effort,
              client_ip, account_alias, tools_json))
        _conn.commit()

    # Notify dashboard SSE clients about the update
    try:
        get_event_manager().publish("stats_updated", {"time": timestamp})
    except Exception:
        pass


# Token usage tracking (in-memory, lost on restart)
_token_usage = {model: {"input": 0, "output": 0, "cache": 0} for model in MODELS}
_token_lock = threading.Lock()

# Shared HTTP client (reused across requests)
_transport = httpx.AsyncHTTPTransport(proxy=PROXY) if PROXY else None
_client = httpx.AsyncClient(transport=_transport, timeout=300)


@asynccontextmanager
async def lifespan(app):
    # Start background quota fetcher (no-op if env vars not set)
    await start_quota_fetcher(app)
    yield
    # Cancel quota fetcher
    task = getattr(app.state, '_quota_task', None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await _client.aclose()

app = FastAPI(lifespan=lifespan)

@app.exception_handler(Exception)
async def debug_exception(request: Request, exc: Exception):
    tb = traceback.format_exc()
    _log(f"ERROR: {exc}\n{tb}")
    return JSONResponse(status_code=500, content={"error": str(exc), "traceback": tb})

# Server manager set later in __main__ for GUI mode; None means always-running
_server_manager = None

register_dashboard(app, STATIC_DIR, _conn, _db_lock, server_manager_getter=lambda: _server_manager)


def _sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _route_for(model_name: str, tool_names: list = None) -> dict:
    if DISABLE_MAPPING:
        return {"match": [], "model": model_name}
    name = model_name.lower()
    tool_names_lower = [t.lower() for t in (tool_names or [])]
    for r in ROUTES.values():
        if r.get("enabled") is False:
            continue
        if any(m in name for m in r.get("match", [])):
            return r
        # Match on tool names too (optional, additive)
        if tool_names_lower and any(m in t for m in r.get("match", []) for t in tool_names_lower):
            return r
    # Wildcard catch-all: if a custom route "*" (or legacy "") exists, use it
    wildcard = CUSTOM_ROUTES.get("*") or CUSTOM_ROUTES.get("")
    if wildcard and isinstance(wildcard, dict) and wildcard.get("model") and wildcard.get("enabled") is not False:
        return wildcard
    return ROUTES["sonnet"]


def _extract_tool_names(body: dict) -> list:
    """Extract tool names from request body (Anthropic or OpenAI format)."""
    tools = body.get("tools", [])
    if not isinstance(tools, list):
        return []
    names = []
    for t in tools:
        if isinstance(t, dict):
            if "name" in t and isinstance(t["name"], str):
                names.append(t["name"])
            elif "function" in t and isinstance(t["function"], dict):
                fn = t["function"]
                if "name" in fn and isinstance(fn["name"], str):
                    names.append(fn["name"])
    return names


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


def anthropic_to_openai(body: dict, model: str) -> dict:
    thinking = isinstance(body.get("thinking"), dict) and body["thinking"].get("type") in ("enabled", "adaptive")

    messages = []

    # System prompt
    if system_text := _extract_text(body.get("system", "")):
        messages.append({"role": "system", "content": system_text})

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

        for block in content:
            if isinstance(block, str):
                text_parts.append(block)
                continue
            if not isinstance(block, dict):
                continue

            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "thinking":
                thinking_parts.append(block.get("thinking", ""))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })
            elif btype == "tool_result":
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": _extract_text(block.get("content", "")),
                })

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
            messages.append(out)
        elif text_parts or thinking_parts or (thinking and is_asst):
            out = {"role": role, "content": "\n".join(text_parts) if text_parts else ""}
            if joined_thinking:
                out["reasoning_content"] = joined_thinking
            elif thinking and is_asst:
                out["reasoning_content"] = " "
            messages.append(out)

    # Build request
    oai = {"model": model, "messages": messages,
           "max_tokens": body.get("max_tokens", 16384),
           "stream": body.get("stream", False)}

    for key, oai_key in [("temperature", "temperature"), ("top_p", "top_p"), ("stop_sequences", "stop")]:
        if key in body:
            oai[oai_key] = body[key]

    if "tools" in body:
        oai["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t.get("input_schema", {}),
        }} for t in body["tools"]]
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

    return oai


def openai_to_anthropic(resp: dict, model: str) -> dict:
    choice = resp.get("choices", [{}])[0]
    msg = choice.get("message", {})
    usage = resp.get("usage", {})

    blocks = []
    if reasoning := msg.get("reasoning_content"):
        blocks.append({"type": "thinking", "thinking": reasoning})
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function", {})
        try:
            inp = json.loads(fn.get("arguments", "{}"))
        except Exception:
            inp = {}
        blocks.append({"type": "tool_use", "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
                        "name": fn.get("name", ""), "input": inp})

    if not blocks:
        blocks.append({"type": "text", "text": ""})

    stop = "tool_use" if msg.get("tool_calls") else "end_turn"
    if choice.get("finish_reason") == "length":
        stop = "max_tokens"

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant",
        "content": blocks, "model": model, "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)},
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
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": _extract_text(msg.get("content", "")),
            })
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
                inp = json.loads(fn.get("arguments", "{}"))
            except Exception:
                inp = {}
            blocks.append({
                "type": "tool_use",
                "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
                "name": fn.get("name", ""),
                "input": inp,
            })

        # Convert reasoning_content → thinking block (assistant only)
        if role == "assistant":
            reasoning = msg.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning.strip():
                blocks.insert(0, {"type": "thinking", "thinking": reasoning})

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
    for key, anthro_key in [("temperature", "temperature"), ("top_p", "top_p"),
                             ("stop", "stop_sequences")]:
        if key in oai_body:
            result[anthro_key] = oai_body[key]

    # Convert tools
    if "tools" in oai_body:
        result["tools"] = [{
            "name": t["function"]["name"],
            "description": t["function"].get("description", ""),
            "input_schema": t["function"].get("parameters", {}),
        } for t in oai_body["tools"] if t.get("type") == "function"]

        # Convert tool_choice
        tc = oai_body.get("tool_choice", "auto")
        if isinstance(tc, dict):
            tc_type = tc.get("type", "auto")
            if tc_type == "function":
                result["tool_choice"] = {"type": "tool", "name": tc.get("function", {}).get("name", "")}
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
            tool_calls.append({
                "id": block.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            })

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
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish,
        }],
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
            for block in (item.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "input_text":
                    system_text += block.get("text", "")
            continue

        if item.get("type") == "function_call_output":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": item.get("call_id", item.get("id", "")),
                "content": item.get("output", ""),
            })
            continue

        if role not in ("user", "assistant"):
            continue

        blocks = []
        if role == "user" and pending_tool_results:
            blocks.extend(pending_tool_results)
            pending_tool_results = []

        for block in (item.get("content") or []):
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype in ("input_text", "text"):
                blocks.append({"type": "text", "text": block.get("text", "")})
            elif btype == "reasoning":
                summary = block.get("summary") or []
                text = "".join(s.get("text", "") for s in summary if isinstance(s, dict))
                if text:
                    blocks.insert(0, {"type": "thinking", "thinking": text})

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

    # Pass through thinking control if explicitly set
    if "thinking" in body:
        result["thinking"] = body["thinking"]

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
            output_items.insert(0, {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": block.get("thinking", "")}],
            })
        elif btype == "tool_use":
            function_calls.append({
                "type": "function_call",
                "id": block.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                "name": block.get("name", ""),
                "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                "status": "completed",
            })

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
    }
    if usage.get("cache_read_input_tokens"):
        oai_usage["output_tokens_details"] = {"cached_tokens": usage["cache_read_input_tokens"]}

    return {
        "id": f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "status": status,
        "model": model,
        "output": output_items,
        "usage": oai_usage,
    }


# ── Thinking models token guard ────────────────────────────
THINKING_MODELS = {
    "deepseek-v4-flash": 2048,
    "deepseek-v4-pro": 4096,
}

def ensure_min_tokens(body: dict, default: int = 256) -> dict:
    """Ajuste max_output_tokens pour les modèles thinking afin qu'il
    reste des tokens pour la réponse après le reasoning."""
    model = body.get("model", "")
    min_tokens = default
    for prefix, tokens in THINKING_MODELS.items():
        if model.startswith(prefix) or model == prefix:
            min_tokens = max(min_tokens, tokens)
            break
    current = body.get("max_output_tokens") or body.get("max_tokens")
    if current is not None and current < min_tokens:
        body["max_output_tokens"] = min_tokens
        _log(f"  ⚠️ {model}: max_tokens ajusté {current} → {min_tokens}")
    return body


def _estimate_tokens(text: str) -> int:
    if _encoding:
        return len(_encoding.encode(text))
    return max(1, len(text) // 3)


def _estimate_input_tokens(body: dict) -> int:
    """Estimate input tokens from message content, tools, and tool_results."""
    chunks = []

    # System prompt
    system = body.get("system", "")
    if isinstance(system, str):
        chunks.append(system)
    elif isinstance(system, list):
        for s in system:
            if isinstance(s, str):
                chunks.append(s)
            elif isinstance(s, dict):
                chunks.append(s.get("text", ""))

    # Tools definitions
    for tool in body.get("tools", []):
        chunks.append(tool.get("name", ""))
        chunks.append(tool.get("description", ""))
        chunks.append(str(tool.get("input_schema", {})))

    # Messages
    for msg in body.get("messages", []):
        content = msg.get("content", "")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    chunks.append(block)
                elif isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "tool_result":
                        chunks.append(_extract_text(block.get("content", "")))
                    elif btype == "thinking":
                        chunks.append(block.get("thinking", ""))
                    else:
                        chunks.append(block.get("text", ""))
                        chunks.append(str(block.get("input", "")))

    combined = "\n".join(chunks)
    if _encoding:
        return len(_encoding.encode(combined))
    return max(1, len(combined) // 3)


def _extract_cache_tokens(usage: dict) -> int:
    details = usage.get("prompt_tokens_details") or {}
    if "cached_tokens" in details:
        return details["cached_tokens"]
    if "cached_tokens" in usage:
        return usage["cached_tokens"]
    if "cache_read_input_tokens" in usage:
        return usage["cache_read_input_tokens"]
    return 0


def _elapsed_ms(start_time: float) -> int:
    return int((time.time() - start_time) * 1000)


@app.api_route("/v1/messages", methods=["POST"])
@app.api_route("/anthropic/v1/messages", methods=["POST"])
async def messages(request: Request):
    req_id = f"msg_{uuid.uuid4().hex[:24]}"
    start_time = time.time()
    client_ip = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()

    try:
        body = json.loads(await request.body())
    except Exception:
        return Response(content='{"error":"invalid json"}', status_code=400)

    original_model = body.get("model", "")
    tool_names = _extract_tool_names(body)
    route = _route_for(original_model, tool_names)
    model_id = route["model"]
    cfg = get_model_config(model_id)
    endpoint = cfg["endpoint"]
    protocol = cfg["protocol"]

    body = dict(body)
    body["model"] = model_id

    # Apply custom route overrides for thinking/effort
    thinking_override = route.get("thinking")
    if thinking_override and thinking_override != "auto":
        if not isinstance(body.get("thinking"), dict):
            body["thinking"] = {}
        body["thinking"]["type"] = thinking_override
    effort_override = route.get("effort")
    if effort_override and effort_override != "auto":
        body["effort"] = effort_override

    # Extract thinking for logging
    thinking = body.get("thinking", {})
    thinking_type = thinking.get("type", "none") if isinstance(thinking, dict) else "none"
    effort = (body.get("effort")
              or (thinking.get("effort") if isinstance(thinking, dict) else None)
              or (body.get("output_config", {}).get("effort") if isinstance(body.get("output_config"), dict) else None)
              or "none")

    _log(f"→ {original_model!r} → {model_id} | {protocol} | stream={body.get('stream', False)} | thinking={thinking_type} | effort={effort} | ip={client_ip}")

    # ── Anthropic pass-through ──────────────────────────────────
    if protocol == "anthropic":
        a_headers = _get_auth_headers("anthropic")
        is_stream = body.get("stream", False)

        if not is_stream:
            resp = await _client.post(endpoint, json=body, headers=a_headers)
            if resp.status_code == 429 and len(API_KEYS) > 1:
                failed_key = a_headers.get("x-api-key", "")
                alt = _find_alternative_key(failed_key)
                if alt:
                    _log(f"  429 on key, retrying with alternative key")
                    a_headers = _get_auth_headers("anthropic", entry=alt)
                    resp = await _client.post(endpoint, json=body, headers=a_headers)
            account_alias = _alias_for_key(a_headers.get("x-api-key", ""))
            if resp.status_code != 200:
                _log(f"  ERROR {resp.status_code}: {resp.text[:300]}")
                await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                             0, 0, 0, success=False, error=f"HTTP {resp.status_code}",
                             protocol=protocol, is_stream=is_stream, thinking=thinking_type, effort=effort,
                             client_ip=client_ip, account_alias=account_alias, tools=tool_names)
                return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            usage = data.get("usage", {})
            req_in = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
            req_out = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
            req_cache = usage.get("cache_read_input_tokens", 0)
            with _token_lock:
                _token_usage[model_id]["input"] += req_in
                _token_usage[model_id]["output"] += req_out
                _token_usage[model_id]["cache"] += req_cache
            alias_tag = f" | account={account_alias}" if account_alias else ""
            _log(f"  ← {model_id} | +{req_in} in | +{req_out} out | +{req_cache} cache{alias_tag}")
            await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                         req_in, req_out, req_cache, success=True,
                         protocol=protocol, is_stream=is_stream, thinking=thinking_type, effort=effort,
                         client_ip=client_ip, account_alias=account_alias, tools=tool_names)
            return Response(content=resp.content, media_type="application/json")


        # Estimate input tokens for Anthropic streaming
        est_input = _estimate_input_tokens(body)
        with _token_lock:
            _token_usage[model_id]["input"] += est_input

        async def anthropic_stream(headers):
            stream_in = None
            stream_out = stream_cache = 0
            _line_buf = ""
            for _attempt in range(2):  # retry once on 429
                try:
                    async with _client.stream("POST", endpoint, json=body, headers=headers) as resp:
                        if resp.status_code != 200:
                            if resp.status_code == 429 and _attempt == 0 and len(API_KEYS) > 1:
                                failed_key = headers.get("x-api-key", "")
                                alt = _find_alternative_key(failed_key)
                                if alt:
                                    _log(f"  429 on key, retrying with alternative key")
                                    headers = _get_auth_headers("anthropic", entry=alt)
                                    continue  # retry with next attempt
                            err = await resp.aread()
                            _log(f"  ERROR {resp.status_code}: {err[:300]}")
                            ak = _alias_for_key(headers.get("x-api-key", ""))
                            await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                                         0, 0, 0, success=False, error=f"HTTP {resp.status_code}",
                                         protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                                         client_ip=client_ip, account_alias=ak, tools=tool_names)
                            error_payload = {"type": "error", "error": {"type": "api_error",
                                           "message": f"HTTP {resp.status_code}: {err.decode('utf-8', errors='replace')[:200]}"}}
                            yield _sse("error", error_payload)
                            return
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                            _line_buf += chunk.decode("utf-8", errors="replace")
                            while "\n" in _line_buf:
                                line, _line_buf = _line_buf.split("\n", 1)
                                line = line.strip()
                                if not line.startswith("data:"):
                                    continue
                                data_str = line[5:].strip()
                                if data_str == "[DONE]":
                                    continue
                                try:
                                    event = json.loads(data_str)
                                except Exception:
                                    continue
                                etype = event.get("type", "")
                                if etype == "message_start":
                                    usage = event.get("message", {}).get("usage", {})
                                    stream_in = usage.get("input_tokens")
                                    if stream_in is not None:
                                        with _token_lock:
                                            _token_usage[model_id]["input"] -= est_input
                                            _token_usage[model_id]["input"] += stream_in
                                    stream_cache = usage.get("cache_read_input_tokens", 0)
                                    if stream_cache:
                                        with _token_lock:
                                            _token_usage[model_id]["cache"] += stream_cache
                                elif etype == "message_delta":
                                    usage = event.get("usage", {})
                                    stream_out = usage.get("output_tokens", 0)
                        # After stream ends, apply final output token count
                        if stream_out:
                            with _token_lock:
                                _token_usage[model_id]["output"] += stream_out
                except Exception as e:
                    ak = _alias_for_key(headers.get("x-api-key", "")) if headers else ""
                    _log(f"  ERROR stream: {e}")
                    if stream_in is None:
                        with _token_lock:
                            _token_usage[model_id]["input"] -= est_input
                    if stream_out:
                        with _token_lock:
                            _token_usage[model_id]["output"] += stream_out
                    await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                                 stream_in if stream_in is not None else est_input, stream_out, stream_cache, success=False, error=str(e),
                                 protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                                 client_ip=client_ip, account_alias=ak, tools=tool_names)
                    return
                else:
                    # Only reached if no exception and no break (successful stream)
                    break
            else:
                # Exhausted retries without success → error already yielded
                return
            logged_in = stream_in if stream_in is not None else est_input
            if stream_in is not None or stream_out:
                ak = _alias_for_key(headers.get("x-api-key", ""))
                alias_tag = f" | account={ak}" if ak else ""
                _log(f"  ← {model_id} | +{logged_in} in | +{stream_out} out | +{stream_cache} cache{alias_tag}")
                await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                             logged_in, stream_out, stream_cache, success=True,
                             protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                             client_ip=client_ip, account_alias=ak, tools=tool_names)

        return StreamingResponse(anthropic_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

    # ── OpenAI-protocol ─────────────────────────────────────────
    oai_body = anthropic_to_openai(body, model_id)
    headers = _get_auth_headers("openai")
    is_stream = oai_body["stream"]

    if not is_stream:
        resp = await _client.post(endpoint, json=oai_body, headers=headers)
        if resp.status_code == 429 and len(API_KEYS) > 1:
            failed_key = headers.get("Authorization", "").replace("Bearer ", "")
            alt = _find_alternative_key(failed_key)
            if alt:
                _log(f"  429 on key, retrying with alternative key")
                headers = _get_auth_headers("openai", entry=alt)
                resp = await _client.post(endpoint, json=oai_body, headers=headers)
        account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
        if resp.status_code != 200:
            _log(f"  ERROR {resp.status_code}: {resp.text[:300]}")
            await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                         0, 0, 0, success=False, error=f"HTTP {resp.status_code}",
                         protocol=protocol, is_stream=is_stream, thinking=thinking_type, effort=effort,
                         client_ip=client_ip, account_alias=account_alias, tools=tool_names)
            try:
                err_data = resp.json()
                err_msg = err_data.get("error", {})
                if isinstance(err_msg, dict):
                    err_msg = err_msg.get("message", resp.text[:200])
            except Exception:
                err_msg = resp.text[:200]
            anthro_err = json.dumps({"type": "error", "error": {"type": "api_error", "message": f"HTTP {resp.status_code}: {err_msg}"}},
                                    ensure_ascii=False)
            return Response(content=anthro_err, status_code=resp.status_code, media_type="application/json")
        data = resp.json()
        usage = data.get("usage", {})
        req_in = usage.get("prompt_tokens", 0)
        req_out = usage.get("completion_tokens", 0)
        cache = _extract_cache_tokens(usage)
        with _token_lock:
            _token_usage[model_id]["input"] += req_in
            _token_usage[model_id]["output"] += req_out
            _token_usage[model_id]["cache"] += cache
        alias_tag = f" | account={account_alias}" if account_alias else ""
        _log(f"  ← {model_id} | +{req_in} in | +{req_out} out | +{cache} cache{alias_tag}")
        await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                     req_in, req_out, cache, success=True,
                     protocol=protocol, is_stream=False, thinking=thinking_type, effort=effort,
                     client_ip=client_ip, account_alias=account_alias, tools=tool_names)
        return Response(content=json.dumps(openai_to_anthropic(data, original_model), ensure_ascii=False),
                        media_type="application/json")

    # Streaming
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    oai_body["stream_options"] = {"include_usage": True}

    stream_in_est = _estimate_input_tokens(body)
    with _token_lock:
        _token_usage[model_id]["input"] += stream_in_est

    async def stream_gen(hdrs):
        started = False
        open_blocks = []
        text_block_idx = None
        reasoning_block_idx = None
        tool_block_idx = {}
        next_block_idx = 0
        stream_out_tokens = 0
        actual_usage = None

        for _attempt in range(2):
            try:
                async with _client.stream("POST", endpoint, json=oai_body, headers=hdrs) as resp:
                    if resp.status_code != 200:
                        if resp.status_code == 429 and _attempt == 0 and len(API_KEYS) > 1:
                            failed_key = hdrs.get("Authorization", "").replace("Bearer ", "")
                            alt = _find_alternative_key(failed_key)
                            if alt:
                                _log(f"  429 on key, retrying with alternative key")
                                hdrs = _get_auth_headers("openai", entry=alt)
                                continue
                        err = await resp.aread()
                        _log(f"  ERROR {resp.status_code}: {err[:300]}")
                        ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                        await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                                     0, 0, 0, success=False, error=f"HTTP {resp.status_code}",
                                     protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                                     client_ip=client_ip, account_alias=ak_h, tools=tool_names)
                        error_payload = {"type": "error", "error": {"type": "api_error",
                                       "message": f"HTTP {resp.status_code}: {err.decode('utf-8', errors='replace')[:200]}"}}
                        yield _sse("error", error_payload)
                        return

                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()

                        if data == "[DONE]":
                            final_in = stream_in_est
                            final_out = stream_out_tokens
                            final_cache = 0
                            with _token_lock:
                                if actual_usage:
                                    final_in = actual_usage.get("prompt_tokens")
                                    if final_in is None:
                                        final_in = stream_in_est
                                    final_out = actual_usage.get("completion_tokens")
                                    if final_out is None:
                                        total = actual_usage.get("total_tokens")
                                        prompt = actual_usage.get("prompt_tokens")
                                        if total is not None and prompt is not None:
                                            final_out = total - prompt
                                    if final_out is None:
                                        final_out = stream_out_tokens
                                    final_cache = _extract_cache_tokens(actual_usage)
                                    _token_usage[model_id]["input"] -= stream_in_est
                                    _token_usage[model_id]["input"] += final_in
                                    _token_usage[model_id]["output"] += final_out
                                    if final_cache:
                                        _token_usage[model_id]["cache"] += final_cache
                                else:
                                    _token_usage[model_id]["output"] += stream_out_tokens
                            if not started:
                                started = True
                                yield _sse("message_start", {"type": "message_start", "message": {
                                    "id": msg_id, "type": "message", "role": "assistant", "content": [],
                                    "model": original_model, "stop_reason": None, "stop_sequence": None,
                                    "usage": {"input_tokens": final_in, "output_tokens": 0}}})
                            for idx in open_blocks:
                                yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
                            has_tools = bool(tool_block_idx)
                            yield _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use" if has_tools else "end_turn"}, "usage": {"output_tokens": final_out}})
                            yield _sse("message_stop", {"type": "message_stop"})
                            log_tag = "" if actual_usage else " (est)"
                            ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                            alias_tag = f" | account={ak_h}" if ak_h else ""
                            _log(f"  ← {model_id} | +{final_in} in{log_tag} | +{final_out} out{log_tag} | +{final_cache} cache{alias_tag}")
                            await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                                         final_in, final_out, final_cache, success=True,
                                         protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                                         client_ip=client_ip, account_alias=ak_h, tools=tool_names)
                            break

                        try:
                            chunk = json.loads(data)
                        except Exception:
                            continue

                        chunk_usage = chunk.get("usage")
                        if chunk_usage and isinstance(chunk_usage, dict):
                            actual_usage = chunk_usage

                        choices = chunk.get("choices", [])
                        if not choices or not isinstance(choices, list):
                            continue
                        first_choice = choices[0] if choices else {}
                        delta = first_choice.get("delta", {}) if isinstance(first_choice, dict) else {}
                        if not delta or not isinstance(delta, dict):
                            delta = {}

                        if not started:
                            started = True
                            yield _sse("message_start", {"type": "message_start", "message": {
                                "id": msg_id, "type": "message", "role": "assistant", "content": [],
                                "model": original_model, "stop_reason": None, "stop_sequence": None,
                                "usage": {"input_tokens": stream_in_est, "output_tokens": 0}}})

                        # Text
                        text = ""
                        c = delta.get("content")
                        if isinstance(c, str):
                            text = c
                        elif isinstance(c, list):
                            text = "".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")

                        if text:
                            if text_block_idx is None:
                                text_block_idx = next_block_idx
                                next_block_idx += 1
                                yield _sse("content_block_start", {"type": "content_block_start", "index": text_block_idx,
                                           "content_block": {"type": "text", "text": ""}})
                                open_blocks.append(text_block_idx)
                            stream_out_tokens += _estimate_tokens(text)
                            yield _sse("content_block_delta", {"type": "content_block_delta", "index": text_block_idx,
                                       "delta": {"type": "text_delta", "text": text}})

                        # Reasoning content
                        reasoning = delta.get("reasoning_content")
                        if isinstance(reasoning, str) and reasoning:
                            if reasoning_block_idx is None:
                                reasoning_block_idx = next_block_idx
                                next_block_idx += 1
                                yield _sse("content_block_start", {"type": "content_block_start", "index": reasoning_block_idx,
                                           "content_block": {"type": "thinking", "thinking": ""}})
                                open_blocks.append(reasoning_block_idx)
                            stream_out_tokens += _estimate_tokens(reasoning)
                            yield _sse("content_block_delta", {"type": "content_block_delta", "index": reasoning_block_idx,
                                       "delta": {"type": "thinking_delta", "thinking": reasoning}})

                        # Tool calls
                        for tc in (delta.get("tool_calls") or []):
                            api_idx = tc.get("index", 0)
                            if api_idx not in tool_block_idx:
                                block_idx = next_block_idx
                                next_block_idx += 1
                                tool_block_idx[api_idx] = block_idx
                                tc_id = tc.get("id", f"toolu_{uuid.uuid4().hex[:8]}")
                                yield _sse("content_block_start", {"type": "content_block_start", "index": block_idx,
                                           "content_block": {"type": "tool_use", "id": tc_id,
                                           "name": tc.get("function", {}).get("name", ""), "input": {}}})
                                open_blocks.append(block_idx)
                            if args := tc.get("function", {}).get("arguments", ""):
                                stream_out_tokens += _estimate_tokens(args)
                                yield _sse("content_block_delta", {"type": "content_block_delta", "index": tool_block_idx[api_idx],
                                           "delta": {"type": "input_json_delta", "partial_json": args}})
            except Exception as e:
                _log(f"  ERROR stream: {e}")
                with _token_lock:
                    _token_usage[model_id]["input"] -= stream_in_est
                ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                             stream_in_est, stream_out_tokens, 0, success=False, error=str(e),
                             protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                             client_ip=client_ip, account_alias=ak_h, tools=tool_names)
                if started:
                    for idx in open_blocks:
                        yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
                    yield _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "error"}, "usage": {"output_tokens": stream_out_tokens}})
                    yield _sse("message_stop", {"type": "message_stop"})
                return
            else:
                break
        else:
            return

    return StreamingResponse(stream_gen(headers), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


async def health():
    async with _db_lock:
        _conn.execute("SELECT 1")
    with _token_lock:
        usage = {model: {"input": d["input"], "output": d["output"], "cache": d["cache"]}
                 for model, d in _token_usage.items()}
    return {"status": "ok", "usage": usage}


@app.get("/v1/models")
async def list_models():
    now = int(time.time())
    data = [{"id": model_id, "object": "model", "created": now, "owned_by": "opencode"} for model_id in MODELS]
    return {"object": "list", "data": data}


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    try:
        body = json.loads(await request.body())
    except Exception:
        return Response(content='{"error":"invalid json"}', status_code=400)
    tokens = _estimate_input_tokens(body)
    return {"input_tokens": tokens}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    req_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    start_time = time.time()
    client_ip = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()

    try:
        body = json.loads(await request.body())
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid json"})

    original_model = body.get("model", "")
    tool_names = _extract_tool_names(body)
    route = _route_for(original_model, tool_names)
    model_id = route["model"]
    cfg = get_model_config(model_id)
    endpoint = cfg["endpoint"]
    protocol = cfg["protocol"]

    body = dict(body)
    body["model"] = model_id
    is_stream = body.get("stream", False)

    thinking_type = "none"
    effort = body.get("effort", "none")

    _log(f"→ {original_model!r} → {model_id} | {protocol} | chat/completions | stream={is_stream} | ip={client_ip}")

    # ── OpenAI passthrough ─────────────────────────────────────
    if protocol == "openai":
        headers = _get_auth_headers("openai")

        if not is_stream:
            resp = await _client.post(endpoint, json=body, headers=headers)
            if resp.status_code == 429 and len(API_KEYS) > 1:
                failed_key = headers.get("Authorization", "").replace("Bearer ", "")
                alt = _find_alternative_key(failed_key)
                if alt:
                    _log(f"  429 on key, retrying with alternative key")
                    headers = _get_auth_headers("openai", entry=alt)
                    resp = await _client.post(endpoint, json=body, headers=headers)
            account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
            if resp.status_code != 200:
                _log(f"  ERROR {resp.status_code}: {resp.text[:300]}")
                await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                             0, 0, 0, success=False, error=f"HTTP {resp.status_code}",
                             protocol=protocol, is_stream=is_stream, thinking=thinking_type, effort=effort,
                             client_ip=client_ip, account_alias=account_alias, tools=tool_names)
                try:
                    err_data = resp.json()
                except Exception:
                    return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
                return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
            data = resp.json()
            usage = data.get("usage", {})
            req_in = usage.get("prompt_tokens", 0)
            req_out = usage.get("completion_tokens", 0)
            cache = _extract_cache_tokens(usage)
            with _token_lock:
                _token_usage[model_id]["input"] += req_in
                _token_usage[model_id]["output"] += req_out
                _token_usage[model_id]["cache"] += cache
            alias_tag = f" | account={account_alias}" if account_alias else ""
            _log(f"  ← {model_id} | +{req_in} in | +{req_out} out | +{cache} cache{alias_tag}")
            await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                         req_in, req_out, cache, success=True,
                         protocol=protocol, is_stream=is_stream, thinking=thinking_type, effort=effort,
                         client_ip=client_ip, account_alias=account_alias, tools=tool_names)
            return Response(content=json.dumps(data, ensure_ascii=False), media_type="application/json")

        # ── OpenAI streaming passthrough ──
        oai_body = dict(body)
        oai_body["stream_options"] = {"include_usage": True}

        est_input = sum(_estimate_tokens(m.get("content", "")) if isinstance(m.get("content"), str) else 0
                        for m in oai_body.get("messages", []))
        with _token_lock:
            _token_usage[model_id]["input"] += est_input

        async def openai_stream(hdrs):
            stream_out = 0
            actual_usage = None
            for _attempt in range(2):
                try:
                    async with _client.stream("POST", endpoint, json=oai_body, headers=hdrs) as resp:
                        if resp.status_code != 200:
                            if resp.status_code == 429 and _attempt == 0 and len(API_KEYS) > 1:
                                failed_key = hdrs.get("Authorization", "").replace("Bearer ", "")
                                alt = _find_alternative_key(failed_key)
                                if alt:
                                    _log(f"  429 on key, retrying with alternative key")
                                    hdrs = _get_auth_headers("openai", entry=alt)
                                    continue
                            err = await resp.aread()
                            _log(f"  ERROR {resp.status_code}: {err[:300]}")
                            ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                            await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                                         0, 0, 0, success=False, error=f"HTTP {resp.status_code}",
                                         protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                                         client_ip=client_ip, account_alias=ak_h, tools=tool_names)
                            yield b"data: " + json.dumps({"error": {"message": f"HTTP {resp.status_code}"}}, ensure_ascii=False).encode() + b"\n\ndata: [DONE]\n\n"
                            return

                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                yield line.encode() + b"\n\n"
                                continue
                            try:
                                chunk = json.loads(data_str)
                            except Exception:
                                yield line.encode() + b"\n\n"
                                continue
                            chunk_usage = chunk.get("usage")
                            if isinstance(chunk_usage, dict):
                                actual_usage = chunk_usage
                            choices = chunk.get("choices", [])
                            if choices and isinstance(choices, list) and len(choices) > 0:
                                delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else {}
                                if isinstance(delta, dict):
                                    c = delta.get("content")
                                    if isinstance(c, str):
                                        stream_out += _estimate_tokens(c)
                                    rc = delta.get("reasoning_content")
                                    if isinstance(rc, str):
                                        stream_out += _estimate_tokens(rc)
                            yield line.encode() + b"\n\n"

                        # Stream ended — finalize tracking
                        final_in = est_input
                        final_out = stream_out
                        final_cache = 0
                        with _token_lock:
                            if actual_usage:
                                final_in = actual_usage.get("prompt_tokens", est_input)
                                final_out = actual_usage.get("completion_tokens", stream_out)
                                final_cache = _extract_cache_tokens(actual_usage)
                                _token_usage[model_id]["input"] -= est_input
                                _token_usage[model_id]["input"] += final_in
                                _token_usage[model_id]["output"] += final_out
                                if final_cache:
                                    _token_usage[model_id]["cache"] += final_cache
                            else:
                                _token_usage[model_id]["output"] += stream_out

                        log_tag = "" if actual_usage else " (est)"
                        ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                        alias_tag = f" | account={ak_h}" if ak_h else ""
                        _log(f"  ← {model_id} | +{final_in} in{log_tag} | +{final_out} out{log_tag} | +{final_cache} cache{alias_tag}")
                        await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                                     final_in, final_out, final_cache, success=True,
                                     protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                                     client_ip=client_ip, account_alias=ak_h, tools=tool_names)
                except Exception as e:
                    _log(f"  ERROR stream: {e}")
                    with _token_lock:
                        _token_usage[model_id]["input"] -= est_input
                    ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                    await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                                 est_input, stream_out, 0, success=False, error=str(e),
                                 protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                                 client_ip=client_ip, account_alias=ak_h, tools=tool_names)
                    return
                else:
                    break
            else:
                return

        return StreamingResponse(openai_stream(headers), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

    # ── Anthropic protocol (double conversion) ──────────────────
    anthro_body = openai_to_anthropic_request(body)

    # Apply thinking/effort overrides from route
    thinking_override = route.get("thinking")
    if thinking_override and thinking_override != "auto":
        if not isinstance(anthro_body.get("thinking"), dict):
            anthro_body["thinking"] = {}
        anthro_body["thinking"]["type"] = thinking_override
    effort_override = route.get("effort")
    if effort_override and effort_override != "auto":
        anthro_body["effort"] = effort_override

    thinking = anthro_body.get("thinking", {})
    thinking_type = thinking.get("type", "none") if isinstance(thinking, dict) else "none"
    effort = (anthro_body.get("effort")
              or (thinking.get("effort") if isinstance(thinking, dict) else None)
              or "none")
    a_headers = _get_auth_headers("anthropic")

    if not is_stream:
        resp = await _client.post(endpoint, json=anthro_body, headers=a_headers)
        if resp.status_code == 429 and len(API_KEYS) > 1:
            failed_key = a_headers.get("x-api-key", "")
            alt = _find_alternative_key(failed_key)
            if alt:
                _log(f"  429 on key, retrying with alternative key")
                a_headers = _get_auth_headers("anthropic", entry=alt)
                resp = await _client.post(endpoint, json=anthro_body, headers=a_headers)
        account_alias = _alias_for_key(a_headers.get("x-api-key", ""))
        if resp.status_code != 200:
            _log(f"  ERROR {resp.status_code}: {resp.text[:300]}")
            await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                         0, 0, 0, success=False, error=f"HTTP {resp.status_code}",
                         protocol=protocol, is_stream=is_stream, thinking=thinking_type, effort=effort,
                         client_ip=client_ip, account_alias=account_alias, tools=tool_names)
            try:
                err_data = resp.json()
                err_msg = err_data.get("error", {}).get("message", resp.text[:200])
            except Exception:
                err_msg = resp.text[:200]
            oai_err = json.dumps({"error": {"message": err_msg, "type": "api_error"}}, ensure_ascii=False)
            return Response(content=oai_err, status_code=resp.status_code, media_type="application/json")

        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        usage = data.get("usage", {})
        req_in = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
        req_out = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
        req_cache = usage.get("cache_read_input_tokens", 0)
        with _token_lock:
            _token_usage[model_id]["input"] += req_in
            _token_usage[model_id]["output"] += req_out
            _token_usage[model_id]["cache"] += req_cache
        alias_tag = f" | account={account_alias}" if account_alias else ""
        _log(f"  ← {model_id} | +{req_in} in | +{req_out} out | +{req_cache} cache{alias_tag}")
        await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                     req_in, req_out, req_cache, success=True,
                     protocol=protocol, is_stream=is_stream, thinking=thinking_type, effort=effort,
                     client_ip=client_ip, account_alias=account_alias, tools=tool_names)

        oai_response = anthropic_to_openai_response(data, original_model)
        return Response(content=json.dumps(oai_response, ensure_ascii=False), media_type="application/json")

    # ── Streaming with Anthropic backend (true streaming) ──
    async def _anthro_to_oai_stream(hdrs):
        _id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        _created = int(time.time())
        started = False
        content_types = {}
        text_data = {}
        thinking_data = {}
        tool_data = {}
        open_blocks = set()
        stream_out = 0
        actual_usage = None
        total_input = 0
        _line_buf = ""

        def _chunk(delta_override, finish):
            c = {
                "id": _id, "object": "chat.completion.chunk", "created": _created,
                "model": original_model,
                "choices": [{"index": 0, "delta": delta_override, "finish_reason": finish}],
            }
            return b"data: " + json.dumps(c, ensure_ascii=False).encode() + b"\n\n"

        for _attempt in range(2):
            try:
                async with _client.stream("POST", endpoint, json=anthro_body, headers=hdrs) as resp:
                    if resp.status_code != 200:
                        if resp.status_code == 429 and _attempt == 0 and len(API_KEYS) > 1:
                            failed_key = hdrs.get("x-api-key", "")
                            alt = _find_alternative_key(failed_key)
                            if alt:
                                _log(f"  429 on key, retrying with alternative key")
                                hdrs = _get_auth_headers("anthropic", entry=alt)
                                continue
                        ak = _alias_for_key(hdrs.get("x-api-key", ""))
                        _log(f"  ERROR {resp.status_code}")
                        await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                                     0, 0, 0, success=False, error=f"HTTP {resp.status_code}",
                                     protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                                     client_ip=client_ip, account_alias=ak, tools=tool_names)
                        yield b"data: " + json.dumps({"error": {"message": f"HTTP {resp.status_code}"}}, ensure_ascii=False).encode() + b"\n\ndata: [DONE]\n\n"
                        return

                    async for raw in resp.aiter_bytes():
                        _line_buf += raw.decode("utf-8", errors="replace")
                        while "\n" in _line_buf:
                            line, _line_buf = _line_buf.split("\n", 1)
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            try:
                                ev = json.loads(data_str)
                            except Exception:
                                continue
                            etype = ev.get("type", "")

                            if etype == "message_start":
                                msg = ev.get("message", {})
                                usage = msg.get("usage", {})
                                total_input = usage.get("input_tokens", 0)
                                started = True
                                yield _chunk({"role": "assistant", "content": ""}, None)

                            elif etype == "content_block_start":
                                idx = ev.get("index")
                                block = ev.get("content_block", {})
                                btype = block.get("type")
                                content_types[idx] = btype
                                open_blocks.add(idx)
                                if btype == "tool_use":
                                    tool_data[idx] = {
                                        "id": block.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
                                        "name": block.get("name", ""),
                                        "args": "",
                                    }
                                    yield _chunk({
                                        "tool_calls": [{
                                            "index": idx, "id": tool_data[idx]["id"],
                                            "type": "function",
                                            "function": {"name": tool_data[idx]["name"], "arguments": ""},
                                        }]
                                    }, None)

                            elif etype == "content_block_delta":
                                idx = ev.get("index")
                                delta = ev.get("delta", {})
                                dtype = delta.get("type")
                                if dtype == "text_delta":
                                    txt = delta.get("text", "")
                                    text_data[idx] = text_data.get(idx, "") + txt
                                    stream_out += _estimate_tokens(txt)
                                    yield _chunk({"content": txt}, None)
                                elif dtype == "thinking_delta":
                                    th = delta.get("thinking", "")
                                    thinking_data[idx] = thinking_data.get(idx, "") + th
                                    stream_out += _estimate_tokens(th)
                                    yield _chunk({"reasoning_content": th}, None)
                                elif dtype == "input_json_delta":
                                    pj = delta.get("partial_json", "")
                                    if idx in tool_data:
                                        tool_data[idx]["args"] += pj
                                        stream_out += _estimate_tokens(pj)
                                        yield _chunk({
                                            "tool_calls": [{
                                                "index": idx, "id": tool_data[idx]["id"],
                                                "type": "function",
                                                "function": {"name": tool_data[idx]["name"], "arguments": pj},
                                            }]
                                        }, None)

                            elif etype == "content_block_stop":
                                idx = ev.get("index")
                                open_blocks.discard(idx)

                            elif etype == "message_delta":
                                d = ev.get("delta", {})
                                u = ev.get("usage", {})
                                actual_usage = u
                                if u.get("output_tokens"):
                                    stream_out = u["output_tokens"]
                                sr = d.get("stop_reason", "")
                                if sr == "end_turn":
                                    finish = "stop"
                                elif sr == "max_tokens":
                                    finish = "length"
                                elif sr == "tool_use":
                                    finish = "tool_calls"
                                else:
                                    finish = "stop"
                                yield _chunk({}, finish)

                            elif etype == "message_stop":
                                with _token_lock:
                                    _token_usage[model_id]["input"] += total_input
                                    _token_usage[model_id]["output"] += stream_out
                                    if actual_usage:
                                        cache = _extract_cache_tokens(actual_usage)
                                        if cache:
                                            _token_usage[model_id]["cache"] += cache
                                ak = _alias_for_key(hdrs.get("x-api-key", ""))
                                alias_tag = f" | account={ak}" if ak else ""
                                _log(f"  ← {model_id} | +{total_input} in | +{stream_out} out{alias_tag}")
                                await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                                             total_input, stream_out, _extract_cache_tokens(actual_usage or {}), success=True,
                                             protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                                             client_ip=client_ip, account_alias=ak, tools=tool_names)
                                yield b"data: [DONE]\n\n"
                                return
            except Exception as e:
                _log(f"  ERROR stream: {e}")
                ak = _alias_for_key(hdrs.get("x-api-key", ""))
                await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                             total_input or 0, stream_out, 0, success=False, error=str(e),
                             protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                             client_ip=client_ip, account_alias=ak, tools=tool_names)
                if started:
                    yield _chunk({}, "stop")
                    yield b"data: [DONE]\n\n"
                return
            else:
                break
        else:
            return

    return StreamingResponse(_anthro_to_oai_stream(a_headers), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


@app.post("/v1/responses")
async def responses(request: Request):
    req_id = f"resp_{uuid.uuid4().hex[:24]}"
    start_time = time.time()
    client_ip = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()

    try:
        body = json.loads(await request.body())
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid json"})

    body = ensure_min_tokens(body)

    # Disable thinking for deepseek models (they consume all tokens on reasoning)
    if "deepseek-v4" in (body.get("model", "")):
        body["thinking"] = {"type": "disabled"}

    original_model = body.get("model", "")
    tool_names = _extract_tool_names(body)
    route = _route_for(original_model, tool_names)
    model_id = route["model"]
    cfg = get_model_config(model_id)
    endpoint = cfg["endpoint"]
    protocol = cfg["protocol"]
    is_stream = body.get("stream", False)

    _log(f"→ {original_model!r} → {model_id} | {protocol} | responses | stream={is_stream} | ip={client_ip}")

    # Convert Responses API → Anthropic format
    anthro_body = openai_responses_to_anthropic(body)
    anthro_body["model"] = model_id

    # Apply route overrides
    thinking_override = route.get("thinking")
    if thinking_override and thinking_override != "auto":
        if not isinstance(anthro_body.get("thinking"), dict):
            anthro_body["thinking"] = {}
        anthro_body["thinking"]["type"] = thinking_override
    effort_override = route.get("effort")
    if effort_override and effort_override != "auto":
        anthro_body["effort"] = effort_override

    thinking = anthro_body.get("thinking", {})
    thinking_type = thinking.get("type", "none") if isinstance(thinking, dict) else "none"
    effort = (anthro_body.get("effort")
              or (thinking.get("effort") if isinstance(thinking, dict) else None)
              or "none")

    # Disable thinking for deepseek models (they consume all tokens on reasoning)
    if "deepseek-v4" in model_id:
        anthro_body["thinking"] = {"type": "disabled"}

    # ── Anthropic backend (passthrough) ─────────────────────
    if protocol == "anthropic":
        a_headers = _get_auth_headers("anthropic")
        if not is_stream:
            resp = await _client.post(endpoint, json=anthro_body, headers=a_headers)
            if resp.status_code == 429 and len(API_KEYS) > 1:
                failed_key = a_headers.get("x-api-key", "")
                alt = _find_alternative_key(failed_key)
                if alt:
                    a_headers = _get_auth_headers("anthropic", entry=alt)
                    resp = await _client.post(endpoint, json=anthro_body, headers=a_headers)
            account_alias = _alias_for_key(a_headers.get("x-api-key", ""))
            if resp.status_code != 200:
                _log(f"  ERROR {resp.status_code}: {resp.text[:300]}")
                await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                             0, 0, 0, success=False, error=f"HTTP {resp.status_code}",
                             protocol=protocol, is_stream=is_stream, thinking=thinking_type, effort=effort,
                             client_ip=client_ip, account_alias=account_alias, tools=tool_names)
                try:
                    err_data = resp.json()
                    err_msg = err_data.get("error", {}).get("message", resp.text[:200])
                except Exception:
                    err_msg = resp.text[:200]
                return Response(content=json.dumps({"error": {"message": err_msg}}), status_code=resp.status_code, media_type="application/json")
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            usage = data.get("usage", {})
            req_in = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
            req_out = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
            req_cache = usage.get("cache_read_input_tokens", 0)
            with _token_lock:
                _token_usage[model_id]["input"] += req_in
                _token_usage[model_id]["output"] += req_out
                _token_usage[model_id]["cache"] += req_cache
            alias_tag = f" | account={account_alias}" if account_alias else ""
            _log(f"  ← {model_id} | +{req_in} in | +{req_out} out | +{req_cache} cache{alias_tag}")
            await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                         req_in, req_out, req_cache, success=True,
                         protocol=protocol, is_stream=is_stream, thinking=thinking_type, effort=effort,
                         client_ip=client_ip, account_alias=account_alias, tools=tool_names)
            oai_resp = anthropic_to_openai_responses(data, original_model)
            return Response(content=json.dumps(oai_resp, ensure_ascii=False), media_type="application/json")
        # Anthropic streaming → Responses SSE
        anthro_body["stream"] = True
        async def _responses_anthro_stream(hdrs):
            _id = f"resp_{uuid.uuid4().hex[:24]}"
            item_id = f"msg_{uuid.uuid4().hex[:12]}"
            content_types = {}
            text_buf = []
            stream_out = 0
            total_input = 0
            output_index = 0
            content_index = 0
            _line_buf = ""
            actual_usage = None
            for _attempt in range(2):
                try:
                    async with _client.stream("POST", endpoint, json=anthro_body, headers=hdrs) as resp:
                        if resp.status_code != 200:
                            if resp.status_code == 429 and _attempt == 0 and len(API_KEYS) > 1:
                                failed_key = hdrs.get("x-api-key", "")
                                alt = _find_alternative_key(failed_key)
                                if alt:
                                    hdrs = _get_auth_headers("anthropic", entry=alt)
                                    continue
                            ak = _alias_for_key(hdrs.get("x-api-key", ""))
                            await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                                         0, 0, 0, success=False, error=f"HTTP {resp.status_code}",
                                         protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                                         client_ip=client_ip, account_alias=ak, tools=tool_names)
                            yield b"data: " + json.dumps({"error": {"message": f"HTTP {resp.status_code}"}}, ensure_ascii=False).encode() + b"\n\n"
                            return
                        async for raw in resp.aiter_bytes():
                            _line_buf += raw.decode("utf-8", errors="replace")
                            while "\n" in _line_buf:
                                line, _line_buf = _line_buf.split("\n", 1)
                                line = line.strip()
                                if not line.startswith("data:"):
                                    continue
                                data_str = line[5:].strip()
                                try:
                                    ev = json.loads(data_str)
                                except Exception:
                                    continue
                                etype = ev.get("type", "")
                                if etype == "message_start":
                                    usage = ev.get("message", {}).get("usage", {})
                                    total_input = usage.get("input_tokens", 0)
                                elif etype == "content_block_start":
                                    idx = ev.get("index")
                                    block = ev.get("content_block", {})
                                    content_types[idx] = block.get("type")
                                    if content_types[idx] == "text":
                                        content_index = len(text_buf)
                                elif etype == "content_block_delta":
                                    idx = ev.get("index")
                                    delta = ev.get("delta", {})
                                    dtype = delta.get("type")
                                    if dtype == "text_delta":
                                        txt = delta.get("text", "")
                                        text_buf.append(txt)
                                        stream_out += _estimate_tokens(txt)
                                        yield _sse("response.output_text.delta", {
                                            "delta": txt,
                                            "item_id": item_id,
                                            "output_index": output_index,
                                            "content_index": content_index,
                                        })
                                    elif dtype == "thinking_delta":
                                        txt = delta.get("thinking", "")
                                        stream_out += _estimate_tokens(txt)
                                        if txt:
                                            yield _sse("response.reasoning.delta", {
                                                "delta": txt,
                                                "item_id": item_id,
                                                "output_index": output_index,
                                            })
                                elif etype == "content_block_stop":
                                    idx = ev.get("index")
                                    if content_types.get(idx) == "text":
                                        yield _sse("response.output_text.done", {
                                            "item_id": item_id,
                                            "output_index": output_index,
                                            "content_index": content_index,
                                        })
                                elif etype == "message_delta":
                                    actual_usage = ev.get("usage", {})
                                    if actual_usage.get("output_tokens"):
                                        stream_out = actual_usage["output_tokens"]
                                elif etype == "message_stop":
                                    full_text = "".join(text_buf)
                                    msg_item = {"type": "message", "role": "assistant",
                                                "content": [{"type": "output_text", "text": full_text}]}
                                    yield _sse("response.output_item.done", {
                                        "id": item_id, **msg_item,
                                    })
                                    status = "completed"
                                    yield _sse("response.completed", {
                                        "id": _id, "status": status,
                                        "output": [msg_item],
                                        "usage": {"input_tokens": total_input, "output_tokens": stream_out},
                                    })
                                    yield b"data: [DONE]\n\n"
                                    with _token_lock:
                                        _token_usage[model_id]["input"] += total_input
                                        _token_usage[model_id]["output"] += stream_out
                                    ak = _alias_for_key(hdrs.get("x-api-key", ""))
                                    _log(f"  ← {model_id} | +{total_input} in | +{stream_out} out")
                                    await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                                                 total_input, stream_out, 0, success=True,
                                                 protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                                                 client_ip=client_ip, account_alias=ak, tools=tool_names)
                                    return
                except Exception as e:
                    _log(f"  ERROR stream: {e}")
                    return
                else:
                    break
            else:
                return
        return StreamingResponse(_responses_anthro_stream(a_headers), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

    # ── OpenAI backend (double conversion) ──────────────────
    # Convert Anthropic → Chat Completions for the backend
    oai_body = anthropic_to_openai(anthro_body, model_id)

    # Reduce reasoning effort for deepseek models
    if "deepseek-v4" in model_id:
        oai_body["reasoning_effort"] = "low"

    headers = _get_auth_headers("openai")
    is_stream = oai_body["stream"]

    if not is_stream:
        resp = await _client.post(endpoint, json=oai_body, headers=headers)
        if resp.status_code == 429 and len(API_KEYS) > 1:
            failed_key = headers.get("Authorization", "").replace("Bearer ", "")
            alt = _find_alternative_key(failed_key)
            if alt:
                headers = _get_auth_headers("openai", entry=alt)
                resp = await _client.post(endpoint, json=oai_body, headers=headers)
        account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
        if resp.status_code != 200:
            _log(f"  ERROR {resp.status_code}: {resp.text[:300]}")
            await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                         0, 0, 0, success=False, error=f"HTTP {resp.status_code}",
                         protocol=protocol, is_stream=is_stream, thinking=thinking_type, effort=effort,
                         client_ip=client_ip, account_alias=account_alias, tools=tool_names)
            try:
                err_data = resp.json()
                err_msg = err_data.get("error", {}).get("message", resp.text[:200])
            except Exception:
                err_msg = resp.text[:200]
            return Response(content=json.dumps({"error": {"message": err_msg}}), status_code=resp.status_code, media_type="application/json")
        data = resp.json()
        usage = data.get("usage", {})
        req_in = usage.get("prompt_tokens", 0)
        req_out = usage.get("completion_tokens", 0)
        cache = _extract_cache_tokens(usage)
        with _token_lock:
            _token_usage[model_id]["input"] += req_in
            _token_usage[model_id]["output"] += req_out
            _token_usage[model_id]["cache"] += cache
        alias_tag = f" | account={account_alias}" if account_alias else ""
        _log(f"  ← {model_id} | +{req_in} in | +{req_out} out | +{cache} cache{alias_tag}")
        await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                     req_in, req_out, cache, success=True,
                     protocol=protocol, is_stream=is_stream, thinking=thinking_type, effort=effort,
                     client_ip=client_ip, account_alias=account_alias, tools=tool_names)
        # Convert Chat Completions → Anthropic → Responses
        anthro_data = openai_to_anthropic(data, original_model)
        oai_resp = anthropic_to_openai_responses(anthro_data, original_model)
        return Response(content=json.dumps(oai_resp, ensure_ascii=False), media_type="application/json")

    # ── Streaming (OpenAI backend) ──────────────────────────
    oai_body["stream_options"] = {"include_usage": True}
    est_input = sum(_estimate_tokens(m.get("content", "")) if isinstance(m.get("content"), str) else 0
                    for m in oai_body.get("messages", []))
    with _token_lock:
        _token_usage[model_id]["input"] += est_input

    async def _responses_openai_stream(hdrs):
        _id = f"resp_{uuid.uuid4().hex[:24]}"
        item_id = f"msg_{uuid.uuid4().hex[:12]}"
        stream_out = 0
        actual_usage = None
        full_content = ""
        content_index = 0
        output_index = 0
        for _attempt in range(2):
            try:
                async with _client.stream("POST", endpoint, json=oai_body, headers=hdrs) as resp:
                    if resp.status_code != 200:
                        if resp.status_code == 429 and _attempt == 0 and len(API_KEYS) > 1:
                            failed_key = hdrs.get("Authorization", "").replace("Bearer ", "")
                            alt = _find_alternative_key(failed_key)
                            if alt:
                                hdrs = _get_auth_headers("openai", entry=alt)
                                continue
                        err = await resp.aread()
                        ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                        await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                                     0, 0, 0, success=False, error=f"HTTP {resp.status_code}",
                                     protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                                     client_ip=client_ip, account_alias=ak_h, tools=tool_names)
                        yield b"data: " + json.dumps({"error": {"message": f"HTTP {resp.status_code}"}}, ensure_ascii=False).encode() + b"\n\ndata: [DONE]\n\n"
                        return
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(data_str)
                        except Exception:
                            continue
                        chunk_usage = chunk.get("usage")
                        if isinstance(chunk_usage, dict):
                            actual_usage = chunk_usage
                        choices = chunk.get("choices", [])
                        if choices and isinstance(choices, list) and len(choices) > 0:
                            delta = choices[0].get("delta", {})
                            if isinstance(delta, dict):
                                c = delta.get("content")
                                if isinstance(c, str) and c:
                                    full_content += c
                                    stream_out += _estimate_tokens(c)
                                    yield _sse("response.output_text.delta", {
                                        "delta": c,
                                        "item_id": item_id,
                                        "output_index": output_index,
                                        "content_index": content_index,
                                    })
                                rc = delta.get("reasoning_content")
                                if isinstance(rc, str) and rc:
                                    stream_out += _estimate_tokens(rc)
                                    yield _sse("response.reasoning.delta", {
                                        "delta": rc,
                                        "item_id": item_id,
                                        "output_index": output_index,
                                    })
                    # Stream ended
                    yield _sse("response.output_text.done", {
                        "item_id": item_id,
                        "output_index": output_index,
                        "content_index": content_index,
                    })
                    final_in = est_input
                    final_out = stream_out
                    final_cache = 0
                    with _token_lock:
                        if actual_usage:
                            final_in = actual_usage.get("prompt_tokens", est_input)
                            final_out = actual_usage.get("completion_tokens", stream_out)
                            final_cache = _extract_cache_tokens(actual_usage)
                            _token_usage[model_id]["input"] -= est_input
                            _token_usage[model_id]["input"] += final_in
                            _token_usage[model_id]["output"] += final_out
                            if final_cache:
                                _token_usage[model_id]["cache"] += final_cache
                        else:
                            _token_usage[model_id]["output"] += stream_out

                    msg_item = {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": full_content}]}
                    yield _sse("response.output_item.done", {"id": item_id, **msg_item})
                    yield _sse("response.completed", {
                        "id": _id, "status": "completed",
                        "output": [msg_item],
                        "usage": {"input_tokens": final_in, "output_tokens": final_out},
                    })
                    yield b"data: [DONE]\n\n"

                    log_tag = "" if actual_usage else " (est)"
                    ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                    _log(f"  ← {model_id} | +{final_in} in{log_tag} | +{final_out} out")
                    await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                                 final_in, final_out, final_cache, success=True,
                                 protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                                 client_ip=client_ip, account_alias=ak_h, tools=tool_names)
            except Exception as e:
                _log(f"  ERROR stream: {e}")
                with _token_lock:
                    _token_usage[model_id]["input"] -= est_input
                ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                             est_input, stream_out, 0, success=False, error=str(e),
                             protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                             client_ip=client_ip, account_alias=ak_h, tools=tool_names)
                return
            else:
                break
        else:
            return

    return StreamingResponse(_responses_openai_stream(headers), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


class ServerManager:
    """Manages uvicorn server lifecycle for start/stop/restart."""

    def __init__(self, app, host, port, web_port):
        self.app = app
        self.host = host
        self.port = port
        self.web_port = web_port
        self._server = None
        self._thread = None
        self._lock = threading.Lock()
        self.is_running = False

    def start(self):
        from uvicorn import Config, Server
        with self._lock:
            if self.is_running:
                return
            h = RichLogHandler()
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
                lg = logging.getLogger(name)
                lg.handlers = [h]
                lg.propagate = False

            config = Config(self.app, host=self.host, port=self.port, log_level="info", log_config=None)
            self._server = Server(config)
            self._thread = threading.Thread(target=self._server.run, daemon=True)
            self._thread.start()

            time.sleep(0.5)
            self.is_running = True

    def stop(self):
        with self._lock:
            if not self.is_running:
                return
            if self._server:
                self._server.should_exit = True
            self.is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def restart(self, port=None, web_port=None, host=None):
        """Hot-restart: stop + update host/ports + start."""
        self.stop()
        if host is not None:
            self.host = host
        if port is not None:
            self.port = int(port)
        if web_port is not None:
            self.web_port = int(web_port)
        time.sleep(0.3)
        self.start()


if __name__ == "__main__":
    import sys

    use_gui = "--gui" in sys.argv

    mgr = ServerManager(app, HOST, PORT, WEB_PORT)
    _server_manager = mgr
    mgr.start()

    _log(f"API: http://localhost:{PORT}")

    if use_gui:
        try:
            from gui import run_gui
        except ImportError:
            print("GUI dependencies not installed. Run: pip install pystray Pillow pywebview")
            sys.exit(1)
        run_gui(mgr, HOST, PORT, WEB_PORT)
    else:
        run_terminal_loop(ROUTES, _token_usage, _token_lock)
