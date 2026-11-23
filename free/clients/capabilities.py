"""Registre de clients et négociation de capacités — 023 (T047).

Les clients sont des données (``registry.json``), pas des branches de code.
Un client inconnu résout ``custom`` avec des défauts sûrs.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT / "registry.json"

FAMILIES = (
    "anthropic_messages",
    "openai_chat",
    "openai_responses",
    "passthrough",
)

DEFAULT_CAPABILITIES: dict[str, Any] = {
    "stream": True,
    "tools": True,
    "tool_choice": False,
    "reasoning": False,
    "vision": False,
    "cache_control": False,
    "count_tokens": False,
    "max_context": 64000,
}

_LOCK = threading.RLock()
_CACHE: dict[str, Any] | None = None


@dataclass(frozen=True)
class ClientProfile:
    id: str
    family: str
    endpoints: tuple[str, ...]
    auth: dict[str, bool] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    tool_policy: str = "openai"
    reasoning: str = ""
    strict: bool = False

    def supports(self, capability: str) -> bool:
        return bool(self.capabilities.get(capability))

    def max_context(self) -> int:
        try:
            return int(self.capabilities.get("max_context") or 0)
        except (TypeError, ValueError):
            return 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "family": self.family,
            "endpoints": list(self.endpoints),
            "auth": dict(self.auth),
            "capabilities": dict(self.capabilities),
            "tool_policy": self.tool_policy,
            "reasoning": self.reasoning,
            "strict": self.strict,
        }


def _load(path: Path | None = None) -> dict[str, Any]:
    global _CACHE
    p = Path(path) if path else REGISTRY_PATH
    key = str(p)
    with _LOCK:
        if _CACHE is not None and _CACHE.get("__path__") == key:
            return _CACHE
        with p.open(encoding="utf-8") as fh:
            data = json.load(fh)
        data["__path__"] = key
        _CACHE = data
        return data


def reset_cache_for_tests() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None


def _profile(client_id: str, raw: dict[str, Any], default_family: str) -> ClientProfile:
    caps = dict(DEFAULT_CAPABILITIES)
    caps.update(raw.get("capabilities") or {})
    family = str(raw.get("family") or default_family)
    if family not in FAMILIES:
        family = default_family
    return ClientProfile(
        id=client_id,
        family=family,
        endpoints=tuple(str(e) for e in (raw.get("endpoints") or ())),
        auth={str(k): bool(v) for k, v in (raw.get("auth") or {}).items()},
        capabilities=caps,
        tool_policy=str(raw.get("tool_policy") or "openai"),
        reasoning=str(raw.get("reasoning") or ""),
        strict=bool(raw.get("strict") or False),
    )


def profiles() -> dict[str, ClientProfile]:
    data = _load()
    default_family = str(data.get("default_family") or "openai_chat")
    return {
        cid: _profile(cid, raw, default_family)
        for cid, raw in (data.get("clients") or {}).items()
        if isinstance(raw, dict)
    }


def profile_for(client_id: str | None) -> ClientProfile:
    all_profiles = profiles()
    if client_id and client_id in all_profiles:
        return all_profiles[client_id]
    custom = all_profiles.get("custom")
    if custom is not None:
        return custom
    return _profile("custom", {}, "openai_chat")


def family_for(client_id: str | None) -> str:
    return profile_for(client_id).family


def detect_from_headers(
    path: str = "",
    headers: dict[str, Any] | None = None,
) -> str:
    """Détecte la famille depuis l'endpoint et les en-têtes."""
    p = str(path or "").lower()
    hl = {str(k).lower() for k in (headers or {})}
    if p.startswith("/v1/messages"):
        return "anthropic_messages"
    if p.startswith("/v1/responses"):
        return "openai_responses"
    if p.startswith("/v1/chat/completions"):
        return "openai_chat"
    if "anthropic-version" in hl or "x-api-key" in hl:
        return "anthropic_messages"
    return "openai_chat"


def capability_allows(profile: ClientProfile, capability: str) -> bool:
    return profile.supports(capability)
