"""Multi-source NordVPN catalog: adapters, manifest and resolution policy."""

from __future__ import annotations

from .adapters import JsonCatalogSource, OfficialApiSource
from .base import CatalogResolution, CatalogSource, SourceSnapshot
from .manifest import SourceDefinition, SourceManifest
from .parsers import parse_gluetun_payload, parse_official_payload, stamp_servers
from .registry import SourceRegistry, build_default_registry

__all__ = [
    "CatalogResolution",
    "CatalogSource",
    "JsonCatalogSource",
    "OfficialApiSource",
    "SourceDefinition",
    "SourceManifest",
    "SourceRegistry",
    "SourceSnapshot",
    "build_default_registry",
    "parse_gluetun_payload",
    "parse_official_payload",
    "stamp_servers",
]
