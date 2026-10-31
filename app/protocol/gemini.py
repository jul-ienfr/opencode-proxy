"""Google Gemini native dialect ↔ OpenAI Chat Completions (pure helpers).

[audit 2026-10-30] The catalogue exposes a native ``google/v1beta`` family
(``generateContent`` / ``streamGenerateContent``). The proxy routes those
models through a Chat-shaped intermediate: callers convert their protocol to
Chat, use :func:`chat_to_gemini`, then convert the Gemini reply back to Chat.
No network here; functions never raise (invalid input → empty/safe shape).
"""

from __future__ import annotations

import json as _json
import time
import uuid

_MAX_TOOL_SCHEMA_KEYS = {
    "type",
    "properties",
    "required",
    "items",
    "enum",
    "description",
    "format",
    "nullable",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "pattern",
    "anyOf",
    "oneOf",
}


def _text_of_content(content) -> str:
    try:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return "" if content is None else str(content)
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "") in ("text", "input_text", "output_text"):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    except Exception:
        return ""


def _clean_schema(schema):
    """Keep the JSON-Schema subset Gemini's functionDeclarations accepts."""
    try:
        if isinstance(schema, list):
            return [_clean_schema(x) for x in schema]
        if not isinstance(schema, dict):
            return schema
        out = {}
        for key, value in schema.items():
            if key not in _MAX_TOOL_SCHEMA_KEYS:
                continue
            if key == "properties" and isinstance(value, dict):
                out[key] = {k: _clean_schema(v) for k, v in value.items()}
            elif key in ("items", "anyOf", "oneOf"):
                out[key] = _clean_schema(value)
            else:
                out[key] = value
        return out
    except Exception:
        return {"type": "object", "properties": {}}


def _chat_tools_to_gemini(tools):
    try:
        out = []
        for tool in tools if isinstance(tools, list) else []:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            name = str(fn.get("name") or "").strip()
            if not name:
                continue
            decl = {"name": name}
            if isinstance(fn.get("description"), str) and fn["description"]:
                decl["description"] = fn["description"]
            params = fn.get("parameters")
            if isinstance(params, dict):
                decl["parameters"] = _clean_schema(params)
            out.append(decl)
        return out
    except Exception:
        return []


def _user_parts(msg):
    content = msg.get("content")
    if isinstance(content, str):
        return [{"text": content}] if content else []
    parts = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype in ("text", "input_text"):
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append({"text": text})
        elif btype in ("image_url", "input_image"):
            url = block.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                header, data = url.split(";base64,", 1)
                mime = header[5:] or "image/png"
                parts.append({"inlineData": {"mimeType": mime, "data": data}})
            elif isinstance(url, str) and url:
                parts.append({"text": f"[image: {url[:200]}]"})
        elif btype in ("input_audio", "audio"):
            parts.append({"text": "[audio omitted]"})
    return parts


def _assistant_parts(msg):
    parts = []
    text = _text_of_content(msg.get("content"))
    if text:
        parts.append({"text": text})
    for call in msg.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = _json.loads(args) if args.strip() else {}
            except Exception:
                args = {"raw": args}
        if not isinstance(args, dict):
            args = {}
        parts.append({"functionCall": {"name": name, "args": args}})
    return parts


def _tool_response_part(msg):
    content = msg.get("content")
    text = content if isinstance(content, str) else _text_of_content(content)
    name = str(msg.get("name") or msg.get("tool_call_id") or "tool")
    return {"functionResponse": {"name": name, "response": {"result": text}}}


def _tool_config(body):
    choice = body.get("tool_choice")
    if choice is None:
        return None
    if isinstance(choice, dict):
        fn = choice.get("function") if isinstance(choice.get("function"), dict) else {}
        name = str(fn.get("name") or "").strip()
        if name:
            return {
                "functionCallingConfig": {
                    "mode": "ANY",
                    "allowedFunctionNames": [name],
                }
            }
        return {"functionCallingConfig": {"mode": "AUTO"}}
    low = str(choice).strip().lower()
    if low in ("none",):
        return {"functionCallingConfig": {"mode": "NONE"}}
    if low in ("required", "any"):
        return {"functionCallingConfig": {"mode": "ANY"}}
    return {"functionCallingConfig": {"mode": "AUTO"}}


def chat_to_gemini(chat_body: dict, model_id: str = "") -> dict:
    """Convert an OpenAI Chat body to a Gemini ``generateContent`` body."""
    try:
        body = chat_body if isinstance(chat_body, dict) else {}
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        system_parts = []
        contents = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            if role in ("system", "developer"):
                text = _text_of_content(msg.get("content"))
                if text:
                    system_parts.append(text)
                continue
            if role == "assistant":
                parts = _assistant_parts(msg)
                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue
            if role in ("tool", "function"):
                contents.append({"role": "user", "parts": [_tool_response_part(msg)]})
                continue
            parts = _user_parts(msg)
            if parts:
                contents.append({"role": "user", "parts": parts})
        out: dict = {"contents": contents}
        if system_parts:
            out["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}
        declarations = _chat_tools_to_gemini(body.get("tools"))
        if declarations:
            out["tools"] = [{"functionDeclarations": declarations}]
        gen: dict = {}
        max_tokens = body.get("max_completion_tokens") or body.get("max_tokens") or body.get("max_output_tokens")
        try:
            if max_tokens:
                gen["maxOutputTokens"] = int(max_tokens)
        except (TypeError, ValueError):
            pass
        for src, dst in (("temperature", "temperature"), ("top_p", "topP")):
            value = body.get(src)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                gen[dst] = value
        stop = body.get("stop")
        if isinstance(stop, str) and stop:
            gen["stopSequences"] = [stop]
        elif isinstance(stop, list):
            seqs = [str(x) for x in stop if isinstance(x, str) and x]
            if seqs:
                gen["stopSequences"] = seqs
        if gen:
            out["generationConfig"] = gen
        tc = _tool_config(body)
        if tc and declarations:
            out["toolConfig"] = tc
        return out
    except Exception:
        return {"contents": []}


def _finish_reason(reason, has_tools: bool = False) -> str:
    if has_tools:
        return "tool_calls"
    low = str(reason or "").upper()
    if low == "MAX_TOKENS":
        return "length"
    if low in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"):
        return "content_filter"
    return "stop"


def _usage_from_gemini(meta: dict) -> dict:
    try:
        prompt = int(meta.get("promptTokenCount") or 0)
        completion = int(meta.get("candidatesTokenCount") or 0)
        thoughts = int(meta.get("thoughtsTokenCount") or 0)
        cached = int(meta.get("cachedContentTokenCount") or 0)
        total = int(meta.get("totalTokenCount") or (prompt + completion + thoughts))
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion + thoughts,
            "total_tokens": total or (prompt + completion + thoughts),
            "prompt_tokens_details": {"cached_tokens": cached},
            "completion_tokens_details": {"reasoning_tokens": thoughts},
        }
    except Exception:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _tool_call_id(name: str) -> str:
    return f"call_{uuid.uuid4().hex[:16]}"


def gemini_response_to_chat(data: dict, model_id: str = "") -> dict:
    """Convert a Gemini ``generateContent`` reply to an OpenAI Chat reply."""
    try:
        payload = data if isinstance(data, dict) else {}
        candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        cand = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
        content = cand.get("content") if isinstance(cand.get("content"), dict) else {}
        parts = content.get("parts") if isinstance(content.get("parts"), list) else []
        texts = []
        tool_calls = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
            fc = part.get("functionCall")
            if isinstance(fc, dict):
                name = str(fc.get("name") or "").strip()
                if not name:
                    continue
                args = fc.get("args")
                if not isinstance(args, dict):
                    args = {}
                tool_calls.append(
                    {
                        "id": _tool_call_id(name),
                        "type": "function",
                        "function": {"name": name, "arguments": _json.dumps(args, ensure_ascii=False)},
                    }
                )
        message: dict = {"role": "assistant", "content": "".join(texts)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        finish = _finish_reason(cand.get("finishReason"), bool(tool_calls))
        return {
            "id": f"chatcmpl_{uuid.uuid4().hex[:20]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_id,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": _usage_from_gemini(payload.get("usageMetadata") or {}),
        }
    except Exception:
        return {
            "id": f"chatcmpl_{uuid.uuid4().hex[:20]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_id,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


def gemini_sse_to_chat_chunk(data: dict, model_id: str = "", chunk_id: str = "") -> dict | None:
    """Convert one Gemini stream chunk to a Chat ``chat.completion.chunk``."""
    try:
        payload = data if isinstance(data, dict) else {}
        candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        usage = payload.get("usageMetadata")
        base = {
            "id": chunk_id or f"chatcmpl_{uuid.uuid4().hex[:20]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_id,
        }
        if not candidates:
            if isinstance(usage, dict):
                return {**base, "choices": [], "usage": _usage_from_gemini(usage)}
            return None
        cand = candidates[0] if isinstance(candidates[0], dict) else {}
        content = cand.get("content") if isinstance(cand.get("content"), dict) else {}
        parts = content.get("parts") if isinstance(content.get("parts"), list) else []
        text_parts = []
        tool_calls = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
            fc = part.get("functionCall")
            if isinstance(fc, dict):
                name = str(fc.get("name") or "").strip()
                if name:
                    args = fc.get("args") if isinstance(fc.get("args"), dict) else {}
                    tool_calls.append(
                        {
                            "index": len(tool_calls),
                            "id": _tool_call_id(name),
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": _json.dumps(args, ensure_ascii=False),
                            },
                        }
                    )
        delta: dict = {}
        if text_parts:
            delta["content"] = "".join(text_parts)
        if tool_calls:
            delta["tool_calls"] = tool_calls
        chunk = {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": _finish_reason(cand.get("finishReason"), bool(tool_calls))
                    if cand.get("finishReason") or tool_calls
                    else None,
                }
            ],
        }
        if isinstance(usage, dict):
            chunk["usage"] = _usage_from_gemini(usage)
        return chunk
    except Exception:
        return None


def gemini_endpoint(base: str, model_id: str, stream: bool = False) -> str:
    """Build the native Gemini method URL for a configured base."""
    try:
        root = str(base or "").strip().rstrip("/")
        model = str(model_id or "").strip()
        if not root or not model:
            return ""
        if root.endswith("/models"):
            root = root[: -len("/models")]
        method = "streamGenerateContent" if stream else "generateContent"
        url = f"{root}/models/{model}:{method}"
        if stream:
            url += "?alt=sse"
        return url
    except Exception:
        return ""


__all__ = [
    "chat_to_gemini",
    "gemini_endpoint",
    "gemini_response_to_chat",
    "gemini_sse_to_chat_chunk",
]
