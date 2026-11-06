"""Résolution runtime d'une identité de station et de son profil vérifié.

Ce module ne fait aucune I/O réseau. Il relie un `Socks5Endpoint` (label),
`StationIdentityVault` et `ProfileRegistry`, et construit les en-têtes/corps
templatés. `opencode.py` l'active derrière `free.station_identity`.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from free.profile_registry import ProfileRegistry, VerifiedProfile
from free.station_identity import (
    StationIdentity,
    StationIdentityVault,
    template_official_body,
)

#: Politique opérateur : garder les deux dernières versions et les suivantes.
#: Tout ce qui est antérieur à 1.19.5 est retiré du runtime.
RUNTIME_FLOOR = (1, 19, 5)
ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class StationRuntimeIdentity:
    identity: StationIdentity
    profile: VerifiedProfile | None

    @property
    def version(self) -> str:
        return str(self.profile.version if self.profile is not None else self.identity.version)

    @property
    def client(self) -> str:
        return str(self.profile.client if self.profile is not None else self.identity.client)

    @property
    def profile_id(self) -> str:
        return str(self.profile.profile_id if self.profile is not None else "")


def version_key(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(version or ""))
    return tuple(int(part) for part in parts[:3]) or (0,)


def runtime_profile_versions(registry: ProfileRegistry) -> tuple[str, ...]:
    versions = {
        profile.version
        for profile in registry.verified()
        if version_key(profile.version) >= RUNTIME_FLOOR
    }
    return tuple(sorted(versions, key=version_key, reverse=True)[:2])


def station_label(station) -> str:
    if station is None:
        return ""
    try:
        return str(getattr(station, "label", "") or "").strip()
    except Exception:
        return ""


def resolve_station_identity(
    station,
    vault: StationIdentityVault,
    registry: ProfileRegistry,
    *,
    version: str = "2.2.5",
    client: str = "cli",
) -> StationRuntimeIdentity | None:
    label = station_label(station)
    if not label:
        return None
    identity = vault.identity_for(label, version=version, client=client)
    allowed = runtime_profile_versions(registry)
    profile = registry.select_for_station(
        label,
        vault.secret,
        allowed_versions=allowed,
    )
    if profile is None:
        candidate = registry.canonical()
        if candidate is not None and candidate.version in allowed:
            profile = candidate
    return StationRuntimeIdentity(identity=identity, profile=profile)


def build_headers(
    runtime: StationRuntimeIdentity,
    *,
    conversation_key: str | None = None,
    endpoint: str = "",
) -> dict[str, str]:
    headers = runtime.identity.headers(
        runtime.profile,
        conversation_key=conversation_key,
        endpoint=endpoint,
        include_transport=True,
    )
    try:
        from free.console_auth import apply_console_auth

        headers = apply_console_auth(headers)
    except Exception:
        pass
    return headers


def _capture_body(profile: VerifiedProfile) -> dict | None:
    raw = str(profile.body_capture or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    body = data.get("body") if isinstance(data, dict) else None
    return body if isinstance(body, dict) else None


def _client_messages(client_body: dict) -> list:
    messages = client_body.get("messages") if isinstance(client_body, dict) else None
    if not isinstance(messages, list):
        return []
    return [
        deepcopy(message)
        for message in messages
        if not (isinstance(message, dict) and message.get("role") == "system")
    ]


def _build_capture_body(
    client_body: dict,
    capture: dict,
    identity: StationIdentity,
) -> dict:
    out = deepcopy(capture)
    model = str((client_body or {}).get("model") or out.get("model") or "")
    if model:
        out["model"] = model
    system = None
    for message in capture.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "system":
            system = deepcopy(message)
            break
    messages = ([system] if system is not None else []) + _client_messages(client_body)
    out["messages"] = messages
    if isinstance(capture.get("tools"), list):
        out["tools"] = deepcopy(capture["tools"])
    if "tool_choice" in capture:
        out["tool_choice"] = capture["tool_choice"]
    out["stream"] = True
    out["stream_options"] = deepcopy(capture.get("stream_options") or {"include_usage": True})
    for key in ("max_tokens", "max_completion_tokens"):
        if isinstance(capture.get(key), int) and not isinstance(capture.get(key), bool):
            out[key] = capture[key]
    return template_official_body(out, identity)


def build_body(
    body: dict,
    runtime: StationRuntimeIdentity,
    *,
    client_body: dict | None = None,
) -> dict:
    if not isinstance(body, dict):
        return body
    profile = runtime.profile
    if profile is None or profile.version == "2.2.5":
        return template_official_body(body, runtime.identity)
    capture = _capture_body(profile)
    if capture is None:
        return template_official_body(body, runtime.identity)
    source = client_body if isinstance(client_body, dict) else body
    return _build_capture_body(source, capture, runtime.identity)


__all__ = [
    "RUNTIME_FLOOR",
    "StationRuntimeIdentity",
    "build_body",
    "build_headers",
    "resolve_station_identity",
    "runtime_profile_versions",
    "station_label",
    "version_key",
]
