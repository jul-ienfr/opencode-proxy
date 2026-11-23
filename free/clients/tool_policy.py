"""Politique d'outils en données — 023 (T056/T057).

Autorisés (grille officielle), interdits, alias client → officiel, shims,
extras bornés et mapping retour. Aucune branche par client.
"""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
POLICY_PATH = ROOT / "tool_policy.json"

_LOCK = threading.RLock()
_CACHE: dict[str, Any] | None = None
_CACHE_KEY = ""


def _load(path: Path | None = None) -> dict[str, Any]:
    global _CACHE, _CACHE_KEY
    p = Path(path) if path else POLICY_PATH
    key = str(p)
    with _LOCK:
        if _CACHE is not None and _CACHE_KEY == key:
            return _CACHE
        with p.open(encoding="utf-8") as fh:
            _CACHE = json.load(fh)
        _CACHE_KEY = key
        return _CACHE


def reset_cache_for_tests() -> None:
    global _CACHE, _CACHE_KEY
    with _LOCK:
        _CACHE = None
        _CACHE_KEY = ""


def official_grid() -> list[str]:
    return [str(n) for n in (_load().get("official_grid") or [])]


def forbidden_names() -> set[str]:
    return {str(n).lower() for n in (_load().get("forbidden") or [])}


def alias_map() -> dict[str, str]:
    raw = _load().get("aliases") or {}
    return {str(k): str(v) for k, v in raw.items()}


def _alias_lower() -> dict[str, str]:
    return {k.lower(): v for k, v in alias_map().items()}


def max_client_tools() -> int:
    try:
        return max(0, int(_load().get("client_extras_max") or 0))
    except (TypeError, ValueError):
        return 0


def unknown_tool_strategy() -> str:
    return str(_load().get("unknown_tool") or "text_heal")


def tool_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ""
    fn = tool.get("function")
    if isinstance(fn, dict):
        return str(fn.get("name") or "").strip()
    return str(tool.get("name") or "").strip()


def map_tool_name(name: str) -> str:
    """Nom client → nom officiel (alias), sinon inchangé."""
    if not name:
        return name
    aliases = _alias_lower()
    return aliases.get(str(name).lower(), str(name))


def is_forbidden(name: str) -> bool:
    return str(name or "").lower() in forbidden_names()


def _placeholder(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Compatibility tool declared by the official client; do not call.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _normalize_client_tool(tool: Any) -> dict[str, Any] | None:
    """Accepte le format chat (``function``) et le format Anthropic."""
    if not isinstance(tool, dict):
        return None
    if isinstance(tool.get("function"), dict):
        return copy.deepcopy(tool)
    name = str(tool.get("name") or "").strip()
    if not name:
        return None
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": str(tool.get("description") or ""),
            "parameters": copy.deepcopy(
                tool.get("parameters") or tool.get("input_schema") or {"type": "object", "properties": {}}
            ),
        },
    }


def build_grid(
    client_tools: Iterable[Any] | None,
    *,
    max_extras: int | None = None,
) -> dict[str, Any]:
    """Construit la grille officielle + extras et le mapping aller/retour.

    Retourne ``{"tools", "extras", "mapping", "reverse", "forbidden_removed"}``.
    """
    grid_names = official_grid()
    forbidden = forbidden_names()
    limit = max_client_tools() if max_extras is None else max(0, int(max_extras))
    client = [t for t in (_normalize_client_tool(x) for x in (client_tools or [])) if t]
    by_exact: dict[str, dict[str, Any]] = {}
    extras: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    forbidden_removed: list[str] = []
    official_lower = {n.lower() for n in grid_names}
    for tool in client:
        name = tool_name(tool)
        emitted = map_tool_name(name)
        low = emitted.lower()
        if low in forbidden or name.lower() in forbidden:
            forbidden_removed.append(name)
            continue
        mapping[name] = emitted
        if emitted in grid_names:
            renamed = copy.deepcopy(tool)
            renamed["function"]["name"] = emitted
            by_exact[emitted] = renamed
        elif low not in official_lower:
            renamed = copy.deepcopy(tool)
            renamed["function"]["name"] = emitted
            extras.append(renamed)
    grid: list[dict[str, Any]] = []
    placeholders: list[str] = []
    for name in grid_names:
        if name in by_exact:
            grid.append(copy.deepcopy(by_exact[name]))
        else:
            grid.append(_placeholder(name))
            placeholders.append(name)
    extras = extras[:limit]
    reverse: dict[str, str] = {}
    for client_name, emitted in mapping.items():
        reverse.setdefault(emitted, client_name)
    return {
        "tools": grid + extras,
        "official": list(grid_names),
        "extras": [tool_name(t) for t in extras],
        "placeholders": placeholders,
        "mapping": mapping,
        "reverse": reverse,
        "forbidden_removed": forbidden_removed,
    }


def reverse_name(emitted: str, mapping: dict[str, str] | None) -> str:
    """Nom émis → nom client d'origine (le premier alias gagne)."""
    if not emitted or not mapping:
        return emitted
    for client_name, out in mapping.items():
        if out == emitted:
            return client_name
    return emitted


def heal_unknown_call(name: str, declared: set[str] | None = None) -> str:
    """Décide du sort d'un appel à un outil non déclaré."""
    if not name:
        return "drop"
    if declared is None or name in declared:
        return "keep"
    return "text_heal" if unknown_tool_strategy() == "text_heal" else "drop"
