"""Canary de la couche fiabilité (P6) — routage déterministe et shadow.

Le canary ne change pas le modèle : il choisit seulement quel sélecteur de
station sert la requête. Le bucket est déterministe par clé de requête, afin
qu'un même couple (modèle, tentative) retombe sur la même branche.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    return bool(value) if isinstance(value, bool) else default


@dataclass
class CanaryConfig:
    enabled: bool = False
    percent: int = 0
    shadow: bool = True
    salt: str = "reliability"

    @classmethod
    def from_config(cls, cfg: dict | None) -> CanaryConfig:
        raw = cfg if isinstance(cfg, dict) else {}
        nested = raw.get("reliability_canary")
        data = nested if isinstance(nested, dict) else {}
        return cls(
            enabled=_as_bool(data.get("enabled"), False),
            percent=max(0, min(100, _as_int(data.get("percent", 0), 0))),
            shadow=_as_bool(data.get("shadow"), True),
            salt=str(data.get("salt") or "reliability"),
        )


@dataclass
class CanaryRouter:
    cfg: CanaryConfig = field(default_factory=CanaryConfig)
    new_count: int = 0
    legacy_count: int = 0
    shadow_divergences: int = 0
    last_bucket: int | None = None
    last_branch: str | None = None

    def bucket(self, request_key: str | int) -> int:
        payload = f"{self.cfg.salt}:{request_key}".encode()
        digest = hashlib.sha256(payload).digest()
        return int.from_bytes(digest[:4], "big") % 100

    def use_new(self, request_key: str | int) -> bool:
        bucket = self.bucket(request_key)
        self.last_bucket = bucket
        if not self.cfg.enabled or self.cfg.percent <= 0:
            self.last_branch = "legacy"
            self.legacy_count += 1
            return False
        use = bucket < self.cfg.percent
        self.last_branch = "new" if use else "legacy"
        if use:
            self.new_count += 1
        else:
            self.legacy_count += 1
        return use

    def note_choice(self, *, new_choice: Any, legacy_choice: Any) -> None:
        if new_choice is not None and legacy_choice is not None and new_choice is not legacy_choice:
            self.shadow_divergences += 1

    def snapshot(self) -> dict[str, Any]:
        total = self.new_count + self.legacy_count
        return {
            "enabled": self.cfg.enabled,
            "percent": self.cfg.percent,
            "shadow": self.cfg.shadow,
            "new_count": self.new_count,
            "legacy_count": self.legacy_count,
            "total": total,
            "shadow_divergences": self.shadow_divergences,
            "last_bucket": self.last_bucket,
            "last_branch": self.last_branch,
        }


__all__ = ["CanaryConfig", "CanaryRouter"]
