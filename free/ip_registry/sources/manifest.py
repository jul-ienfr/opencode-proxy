"""Validation and loading of ``docs/nordvpn_sources.yaml``.

The YAML is the operator-facing registry: it lists every known source, what
it provides and the catalog hierarchy.  This module turns it into typed
objects and refuses duplicate ids or dangling hierarchy references, so a typo
cannot silently change which source feeds the allocator.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST_PATH = ROOT / "docs" / "nordvpn_sources.yaml"

_REQUIRED = ("id", "name", "url", "category", "kind", "auth", "refresh", "status")


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    id: str
    name: str
    url: str
    category: str
    kind: str
    auth: str
    refresh: str
    status: str
    notes: str = ""
    used_by: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceManifest:
    path: Path
    version: int
    sources: dict[str, SourceDefinition]
    hierarchy: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path | None = None) -> SourceManifest:
        target = Path(path) if path is not None else DEFAULT_MANIFEST_PATH
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"source manifest is not a mapping: {target}")
        raw_sources = data.get("sources") or []
        if not isinstance(raw_sources, list):
            raise ValueError("source manifest 'sources' must be a list")

        sources: dict[str, SourceDefinition] = {}
        for item in raw_sources:
            if not isinstance(item, dict):
                raise ValueError("source manifest entries must be mappings")
            missing = [field for field in _REQUIRED if not item.get(field)]
            if missing:
                raise ValueError(f"source {item.get('id')!r} misses {missing}")
            source_id = str(item["id"])
            if source_id in sources:
                raise ValueError(f"duplicate source id: {source_id}")
            used_by = item.get("used_by") or []
            sources[source_id] = SourceDefinition(
                id=source_id,
                name=str(item["name"]),
                url=str(item["url"]),
                category=str(item["category"]),
                kind=str(item["kind"]),
                auth=str(item["auth"]),
                refresh=str(item["refresh"]),
                status=str(item["status"]),
                notes=str(item.get("notes") or ""),
                used_by=tuple(str(entry) for entry in used_by if entry),
            )

        hierarchy = data.get("hierarchy") or {}
        if not isinstance(hierarchy, dict):
            raise ValueError("source manifest 'hierarchy' must be a mapping")
        for key, value in hierarchy.items():
            values = [value] if isinstance(value, str) else value
            if not isinstance(values, list):
                continue
            if key == "egress_truth":
                continue
            for ref in values:
                if str(ref) not in sources:
                    raise ValueError(f"hierarchy {key!r} references unknown source {ref!r}")

        return cls(
            path=target,
            version=int(data.get("version") or 0),
            sources=sources,
            hierarchy=hierarchy,
        )

    def catalog_order(self) -> tuple[str, ...]:
        """Primary then fallbacks, from the manifest hierarchy."""

        order: list[str] = []
        primary = self.hierarchy.get("catalog_primary")
        if isinstance(primary, str) and primary in self.sources:
            order.append(primary)
        for source_id in self.hierarchy.get("catalog_fallbacks") or []:
            if isinstance(source_id, str) and source_id in self.sources and source_id not in order:
                order.append(source_id)
        return tuple(order)

    def completeness_order(self) -> tuple[str, ...]:
        """Sources merged *under* the winner (they never replace it)."""

        order: list[str] = []
        for source_id in self.hierarchy.get("hostname_completeness") or []:
            if isinstance(source_id, str) and source_id in self.sources and source_id not in order:
                order.append(source_id)
        return tuple(order)

    def definition(self, source_id: str) -> SourceDefinition | None:
        return self.sources.get(source_id)
