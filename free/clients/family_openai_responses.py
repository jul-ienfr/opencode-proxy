"""Famille OpenAI Responses — 023 (T050)."""

from __future__ import annotations

import copy
from typing import Any

from free.clients import tool_policy

FAMILY = "openai_responses"
MAX_OUTPUT_TOKENS = 32000

SSE_EVENTS = (
    "response.created",
    "response.in_progress",
    "response.output_item.added",
    "response.output_item.done",
    "response.completed",
    "response.failed",
)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in ("input_text", "output_text", "text"):
                    parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return ""


def _convert_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    itype = str(item.get("type") or "")
    if itype in ("message", "input_message", "") and item.get("role"):
        return {
            "role": str(item.get("role") or "user"),
            "content": _content_text(item.get("content")),
        }
    if itype == "function_call_output":
        return {
            "role": "tool",
            "tool_call_id": str(item.get("call_id") or ""),
            "content": _content_text(item.get("output")),
        }
    return None


def _convert_tool(tool: Any) -> dict[str, Any] | None:
    if not isinstance(tool, dict):
        return None
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    name = str(fn.get("name") or "").strip()
    if not name:
        return None
    return {
        "type": "function",
        "function": {
            "name": tool_policy.map_tool_name(name),
            "description": str(fn.get("description") or ""),
            "parameters": copy.deepcopy(
                fn.get("parameters") or {"type": "object", "properties": {}}
            ),
        },
    }


def to_chat(body: dict[str, Any]) -> dict[str, Any]:
    """Convertit une requête Responses en corps chat officiel."""
    if not isinstance(body, dict):
        return body
    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": _content_text(instructions)})
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            converted = _convert_item(item)
            if converted is not None:
                messages.append(converted)
    tools = [t for t in (_convert_tool(x) for x in (body.get("tools") or [])) if t]
    chat: dict[str, Any] = {
        "model": body.get("model"),
        "messages": messages,
        "tools": tools,
        "stream": bool(body.get("stream")),
    }
    max_out = body.get("max_output_tokens")
    if isinstance(max_out, int) and not isinstance(max_out, bool):
        chat["max_completion_tokens"] = min(int(max_out), MAX_OUTPUT_TOKENS)
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        chat["reasoning_effort"] = str(reasoning.get("effort"))
    if body.get("prompt_cache_key"):
        chat["prompt_cache_key"] = str(body.get("prompt_cache_key"))
    if chat["stream"]:
        chat["stream_options"] = {"include_usage": True}
    return chat


def sse_contract() -> tuple[str, ...]:
    return SSE_EVENTS


def count_tokens(
    body: dict[str, Any],
    *,
    estimate_fn: Any | None = None,
) -> int:
    """Compte les tokens pour une requête Responses (instructions + input + tools)."""
    if estimate_fn is None:
        from protocol.tokens import estimate_input_tokens as estimate_fn
    return estimate_fn(
        {
            "instructions": body.get("instructions") or "",
            "input": body.get("input") or [],
            "tools": body.get("tools") or [],
        }
    )
