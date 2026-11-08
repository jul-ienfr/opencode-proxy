"""Politiques partagées de santé, réparation, rotation et démarrage (011)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

CAUSE_ACTIONS: dict[str, str] = {
    "auth": "repair_auth",
    "auth_failed": "repair_auth",
    "server_dead": "repin",
    "dns": "recreate",
    "transport": "flip_transport",
    "quota_429": "park",
    "quota": "park",
    "healthcheck": "recreate",
    "proxy_down": "start_proxy",
    "duplicate_block": "rotate",
    "generation": "recreate",
    "port_conflict": "recreate",
    "unknown": "inspect",
}


@dataclass
class RepairPolicy:
    enabled: bool = True
    max_restarts_per_hour: int = 3
    max_recreates_per_hour: int = 1
    park_after_failures: int = 3
    park_s: float = 1800.0
    verify_egress_before_usable: bool = True
    last_good_first: bool = True

    @classmethod
    def from_config(cls, raw: Any = None) -> RepairPolicy:
        data = raw if isinstance(raw, dict) else {}
        return cls(
            enabled=bool(data.get("enabled", True)),
            max_restarts_per_hour=int(data.get("max_restarts_per_hour", 3) or 3),
            max_recreates_per_hour=int(data.get("max_recreates_per_hour", 1) or 1),
            park_after_failures=int(data.get("park_after_failures", 3) or 3),
            park_s=float(data.get("park_s", 1800.0) or 1800.0),
            verify_egress_before_usable=bool(
                data.get("verify_egress_before_usable", True)
            ),
            last_good_first=bool(data.get("last_good_first", True)),
        )

    def action_for(self, cause: str) -> str:
        return CAUSE_ACTIONS.get(str(cause or "").strip().lower(), "inspect")


@dataclass
class StartupPolicy:
    """Démarrage/maintenance indépendants de la sélection trafic."""

    repair_owner: str = "supervisor"
    providers: list[str] = field(default_factory=list)
    station_count: int = 6
    disabled: set[int] = field(default_factory=set)

    def should_connect(self, provider: str) -> bool:
        name = str(provider or "").strip().lower()
        if not name:
            return False
        if self.station_count <= 0:
            return False
        if len(self.disabled) >= self.station_count:
            return False
        # La sélection trafic ne décide pas qui est démarré/maintenu.
        return True


@dataclass
class RotationBudget:
    max_per_hour: int = 10
    store: dict[str, list[float]] = field(default_factory=dict)
    clock: Any = time.time

    def _key(self, provider: str, slot_id: int) -> str:
        return f"{provider}:{int(slot_id)}"

    def _recent(self, key: str) -> list[float]:
        now = float(self.clock())
        recent = [
            float(ts)
            for ts in self.store.get(key, [])
            if 0 <= now - float(ts) < 3600.0
        ]
        self.store[key] = recent
        return recent

    def allow(self, provider: str, slot_id: int) -> bool:
        if self.max_per_hour <= 0:
            return True
        return len(self._recent(self._key(provider, slot_id))) < self.max_per_hour

    def record(self, provider: str, slot_id: int) -> None:
        key = self._key(provider, slot_id)
        recent = self._recent(key)
        recent.append(float(self.clock()))
        self.store[key] = recent
