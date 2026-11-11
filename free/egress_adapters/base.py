"""Provider adapters for the egress catalog.

Each adapter owns one provider's discovery + validation + persistence.  The
scheduler, store and rotation never import provider internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from free.egress_catalog import ProviderPolicy
from free.egress_store import EgressStore


@dataclass
class RefreshResult:
    provider: str
    candidates: int = 0
    promoted: int = 0
    quarantined: int = 0
    dead: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    observe_only: bool = False


class EgressAdapter(Protocol):
    provider: str

    async def refresh(
        self,
        store: EgressStore,
        policy: ProviderPolicy,
        *,
        run_id: str = "",
        max_candidates: int | None = None,
        observe_only: bool = False,
    ) -> RefreshResult: ...


__all__ = ["EgressAdapter", "RefreshResult"]
