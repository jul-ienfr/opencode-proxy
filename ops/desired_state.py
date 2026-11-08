"""Desired state des stations et endpoints (feature 011).

Domaine pur : aucune I/O, aucun Docker. Une génération monotone permet au
reconcile de distinguer un conteneur conforme d'un conteneur obsolète.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

PROVIDERS = ("nordvpn", "warp", "proxies", "direct")
STACKS = ("openvpn", "wireguard", "warp", "socks5")
TRANSPORTS = ("openvpn-udp", "openvpn-tcp", "wireguard", "socks5")


def _hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class DesiredStationSpec:
    provider: str
    station: int
    generation: int = 1
    enabled: bool = True
    stack: str = "openvpn"
    transport: str = "openvpn-udp"
    host: str | None = None
    country: str | None = None
    port: int = 0
    container: str = ""
    image: str = ""
    ipv6: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise ValueError(f"provider invalide: {self.provider}")
        if not (1 <= int(self.station) <= 10):
            raise ValueError("station doit être entre 1 et 10")
        if int(self.generation) < 1:
            raise ValueError("generation doit être >= 1")
        if self.stack not in STACKS:
            raise ValueError(f"stack invalide: {self.stack}")
        if self.transport not in TRANSPORTS:
            raise ValueError(f"transport invalide: {self.transport}")
        if self.provider == "nordvpn":
            if self.stack not in ("openvpn", "wireguard"):
                raise ValueError("nordvpn exige stack openvpn|wireguard")
            if self.transport not in ("openvpn-udp", "openvpn-tcp", "wireguard"):
                raise ValueError("nordvpn exige un transport VPN")
        elif self.provider == "warp":
            if self.stack != "warp" or self.transport != "socks5":
                raise ValueError("warp exige stack=warp et transport=socks5")
        if self.port and not (1 <= int(self.port) <= 65535):
            raise ValueError("port doit être entre 1 et 65535")

    @property
    def spec_hash(self) -> str:
        payload = asdict(self)
        payload.pop("generation", None)
        return _hash(payload)

    def bump(self, **changes: Any) -> DesiredStationSpec:
        data = asdict(self)
        data.update(changes)
        data["generation"] = int(self.generation) + 1
        return DesiredStationSpec(**data)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "spec_hash": self.spec_hash}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DesiredStationSpec:
        data = dict(raw or {})
        data.pop("spec_hash", None)
        return cls(**data)


@dataclass(frozen=True, slots=True)
class DesiredEndpointSpec:
    provider: str
    label: str
    container: str
    port: int
    generation: int = 1
    enabled: bool = True
    host: str = "127.0.0.1"
    ipv6: bool = True
    managed: bool = True
    image: str = "caomingjun/warp:latest"
    block: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.provider not in ("warp", "proxies"):
            raise ValueError(f"provider endpoint invalide: {self.provider}")
        if not self.label or not self.container:
            raise ValueError("label et container sont requis")
        if not (1 <= int(self.port) <= 65535):
            raise ValueError("port doit être entre 1 et 65535")
        if int(self.generation) < 1:
            raise ValueError("generation doit être >= 1")
        if self.provider == "warp" and self.host != "127.0.0.1":
            raise ValueError("un endpoint WARP doit être bindé sur 127.0.0.1")

    @property
    def spec_hash(self) -> str:
        payload = asdict(self)
        payload.pop("generation", None)
        return _hash(payload)

    def bump(self, **changes: Any) -> DesiredEndpointSpec:
        data = asdict(self)
        data.update(changes)
        data["generation"] = int(self.generation) + 1
        return DesiredEndpointSpec(**data)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "spec_hash": self.spec_hash}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DesiredEndpointSpec:
        data = dict(raw or {})
        data.pop("spec_hash", None)
        return cls(**data)
