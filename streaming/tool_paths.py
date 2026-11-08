"""Healing pur des arguments de tool_calls (alias de chemin).

Le prompt free est templaté pour cacher le vrai workspace, mais certains
modèles écrivent ``filePath``/``file_path`` au lieu du ``path`` exigé par les
schémas OpenCode. Le client rejette alors l'appel avec « path: Missing key ».
Ces fonctions sont pures et partagées entre le handler et le middleware SSE.
"""

from __future__ import annotations

import contextvars
import json
import re

PATH_TOOL_NAMES = frozenset(
    {"edit", "read", "write", "patch", "multiedit", "create", "delete", "move", "copy"}
)
PATH_ARG_ALIASES = ("filePath", "file_path", "filepath", "filename", "file")

#: Noms officiels du gate free -> outil réellement déclaré par le client.
TOOL_NAME_ALIASES = {
    "bash": "shell",
    "task": "subagent",
    "todowrite": "todo_write",
    "todo_write": "todowrite",
}

#: Outils client réellement exécutables, posés par `_free_wire_body`.
CLIENT_TOOL_NAMES: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar(
    "CLIENT_TOOL_NAMES", default=None
)


def set_client_tool_names(names) -> None:
    cleaned = frozenset(
        str(name).strip() for name in (names or ()) if str(name).strip()
    )
    CLIENT_TOOL_NAMES.set(cleaned or None)


def heal_tool_name(name: str) -> str:
    """Remplace un placeholder officiel par l'outil client équivalent.

    Le gate free exige ``bash``/``task``/``todowrite`` dans la grille, mais le
    client OpenCode 2.2.5 expose ``shell``/``subagent``. Sans ce renommage, le
    modèle appelle ``bash`` et le client répond « invalid tool call ».
    """
    raw = str(name or "")
    if not raw:
        return raw
    available = CLIENT_TOOL_NAMES.get()
    if not available:
        return raw
    if raw in available:
        return raw
    target = TOOL_NAME_ALIASES.get(raw)
    if target and target in available:
        return target
    return raw


def heal_tool_arguments(
    name: str,
    arguments: str,
    path_map: dict[str, str] | None = None,
) -> str:
    """Remappe les racines factices et renomme les alias de chemin en ``path``."""
    text = str(arguments or "")
    if path_map:
        try:
            from free.station_identity import translate_paths

            text = translate_paths(text, path_map, json_escape=True)
        except Exception:
            pass
    if name and name not in PATH_TOOL_NAMES:
        return text
    if re.search(r'[{,]\s*"path"\s*:', text):
        return text
    for alias in PATH_ARG_ALIASES:
        healed = re.sub(
            rf'([{{,]\s*)"{re.escape(alias)}"\s*:',
            r'\1"path":',
            text,
            count=1,
        )
        if healed != text:
            return healed
    return text


def heal_openai_payload(payload) -> bool:
    """Heal ``choices[].delta/message.tool_calls[].function.arguments``."""
    changed = False
    if not isinstance(payload, dict):
        return False
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        for bucket in ("delta", "message"):
            holder = choice.get(bucket)
            if not isinstance(holder, dict):
                continue
            calls = holder.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function")
                if not isinstance(fn, dict):
                    continue
                old_name = str(fn.get("name") or "")
                new_name = heal_tool_name(old_name)
                if new_name != old_name:
                    fn["name"] = new_name
                    changed = True
                args = fn.get("arguments")
                if not isinstance(args, str):
                    continue
                new_args = heal_tool_arguments(str(fn.get("name") or ""), args)
                if new_args != args:
                    fn["arguments"] = new_args
                    changed = True
    return changed


def heal_anthropic_payload(payload) -> bool:
    """Heal ``tool_use.input`` et ``input_json_delta.partial_json``."""
    if not isinstance(payload, dict):
        return False
    changed = False
    ptype = str(payload.get("type") or "")
    if ptype == "content_block_start":
        block = payload.get("content_block")
        if isinstance(block, dict) and block.get("type") == "tool_use":
            old_name = str(block.get("name") or "")
            new_name = heal_tool_name(old_name)
            if new_name != old_name:
                block["name"] = new_name
                changed = True
            value = block.get("input")
            if isinstance(value, dict) and "path" not in value:
                for alias in PATH_ARG_ALIASES:
                    if alias in value:
                        value["path"] = value.pop(alias)
                        changed = True
                        break
    elif ptype == "content_block_delta":
        delta = payload.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "input_json_delta":
            partial = delta.get("partial_json")
            if isinstance(partial, str):
                new_partial = heal_tool_arguments("", partial)
                if new_partial != partial:
                    delta["partial_json"] = new_partial
                    changed = True
    return changed


def heal_sse_data_line(line: bytes) -> bytes:
    """Réécrit une ligne SSE ``data: {...}`` si un alias de chemin est présent."""
    if not line.startswith(b"data:"):
        return line
    raw = line[5:].strip()
    if not raw or raw == b"[DONE]":
        return line
    try:
        payload = json.loads(raw)
    except Exception:
        return line
    changed = heal_openai_payload(payload) or heal_anthropic_payload(payload)
    if not changed:
        return line
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode()


__all__ = [
    "CLIENT_TOOL_NAMES",
    "PATH_ARG_ALIASES",
    "PATH_TOOL_NAMES",
    "TOOL_NAME_ALIASES",
    "heal_anthropic_payload",
    "heal_openai_payload",
    "heal_sse_data_line",
    "heal_tool_arguments",
    "heal_tool_name",
    "set_client_tool_names",
]
