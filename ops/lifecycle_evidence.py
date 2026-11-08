"""Preuves, incidents et logs communs aux providers (011).

Le pipeline unique doit écrire ``provider``, ``slot``, ``cause``, ``action``,
``evidence`` et ``result``. Les adapters fournissent les valeurs ; ce module ne
connaît ni Docker ni le réseau.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("lifecycle")


@dataclass(frozen=True, slots=True)
class Evidence:
    provider: str
    slot: int
    cause: str | None = None
    action: str | None = None
    result: str | None = None
    evidence: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "slot": int(self.slot),
            "cause": self.cause,
            "action": self.action,
            "result": self.result,
            "evidence": self.evidence,
            **dict(self.extra),
        }


def log_evidence(item: Evidence, *, level: int = logging.INFO) -> None:
    logger.log(level, "[lifecycle] %s", item.as_dict())
