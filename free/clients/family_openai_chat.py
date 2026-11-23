"""Famille OpenAI Chat Completions — 023 (T049)."""

from __future__ import annotations

import copy
from typing import Any

from free.clients import tool_policy

FAMILY = "openai_chat"
MAX_OUTPUT_TOKENS = 32000

SSE_EVENTS = ("chunk", "finish_reason", "usage", "[DONE]")


def _normalize_message(msg: Any) -> dict[str, Any] | None:
    if not isinstance(msg, dict):
        return None
    role = str(msg.get("role") or "")
    if not role:
        return None
    out = copy.deepcopy(msg)
    content = out.get("content")
    if isinstance(content, list):
        # Chat accepte des parties ; les blocs Anthropic "text" sont convertis.
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append({"type": "text", "text": str(block.get("text") or "")})
            elif isinstance(block, dict) and block.get("type") == "image":
                parts.append({"type": "image_url", "image_url": block.get("source") or {}})
            else:
                parts.append(block)
        out["content"] = parts
    return out


def _normalize_tool(tool: Any) -> dict[str, Any] | None:
    if not isinstance(tool, dict):
        return None
    fn = tool.get("function")
    if isinstance(fn, dict):
        name = str(fn.get("name") or "").strip()
        if not name:
            return None
        out = copy.deepcopy(tool)
        out["function"]["name"] = tool_policy.map_tool_name(name)
        return out
    name = str(tool.get("name") or "").strip()
    if not name:
        return None
    return {
        "type": "function",
        "function": {
            "name": tool_policy.map_tool_name(name),
            "description": str(tool.get("description") or ""),
            "parameters": copy.deepcopy(
                tool.get("parameters") or tool.get("input_schema") or {"type": "object", "properties": {}}
            ),
        },
    }


def to_chat(body: dict[str, Any]) -> dict[str, Any]:
    """Normalise un corps chat : ``system`` racine, outils, plafonds, options."""
    if not isinstance(body, dict):
        return body
    out = copy.deepcopy(body)
    system = out.pop("system", None)
    messages = out.get("messages")
    if not isinstance(messages, list):
        messages = []
    if system:
        text = system if isinstance(system, str) else "\n\n".join(
            str(b.get("text") or "") for b in system if isinstance(b, dict)
        )
        messages = [{"role": "system", "content": text}] + [
            m for m in (_normalize_message(x) for x in messages) if m
        ]
    else:
        messages = [m for m in (_normalize_message(x) for x in messages) if m]
    out["messages"] = messages
    tools = out.get("tools")
    if isinstance(tools, list):
        normalized = [t for t in (_normalize_tool(x) for x in tools) if t]
        out["tools"] = normalized
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        value = out.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > MAX_OUTPUT_TOKENS:
            out[key] = MAX_OUTPUT_TOKENS
    if out.get("stream") is True and "stream_options" not in out:
        out["stream_options"] = {"include_usage": True}
    if out.get("tool_choice") == "none":
        out["tool_choice"] = "auto"
    return out


def sse_contract() -> tuple[str, ...]:
    return SSE_EVENTS


def count_tokens(
    body: dict[str, Any],
    *,
    estimate_fn: Any | None = None,
) -> int:
    """Compte les tokens pour une requête Chat (messages + tools)."""
    if estimate_fn is None:
        from protocol.tokens import estimate_input_tokens as estimate_fn
    return estimate_fn(
        {
            "messages": body.get("messages") or [],
            "tools": body.get("tools") or [],
        }
    )
