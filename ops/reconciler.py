"""Reconcile desired/actual sans effet de bord (feature 011).

Le module décide, il n'exécute pas : les adapters providers appliquent. Règle
centrale : un conteneur conforme n'est jamais supprimé ; une divergence de
génération/stack produit **un** recreate planifié.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ops.desired_state import DesiredEndpointSpec, DesiredStationSpec


@dataclass(frozen=True, slots=True)
class ActualSlot:
    provider: str
    slot_id: int
    container: str = ""
    generation: int = 0
    stack: str = ""
    transport: str = ""
    running: bool = False
    health: str = "unknown"
    image: str = ""
    port: int = 0
    block: str | None = None


@dataclass(frozen=True, slots=True)
class ReconcileDecision:
    provider: str
    slot_id: int
    action: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "slot_id": self.slot_id,
            "action": self.action,
            "reason": self.reason,
        }


def decide(
    desired: DesiredStationSpec | DesiredEndpointSpec,
    actual: ActualSlot | None,
) -> ReconcileDecision:
    provider = str(desired.provider)
    if isinstance(desired, DesiredStationSpec):
        slot_id = int(desired.station)
    else:
        slot_id = 0
    if actual is None:
        return ReconcileDecision(provider, slot_id, "create", "missing")
    if not actual.running:
        return ReconcileDecision(provider, slot_id, "start", "not_running")
    if isinstance(desired, DesiredStationSpec):
        desired_stack = desired.stack
        desired_transport = desired.transport
    else:
        desired_stack = "warp" if provider == "warp" else "socks5"
        desired_transport = "socks5"
    if actual.generation and int(actual.generation) != int(desired.generation):
        return ReconcileDecision(provider, slot_id, "recreate", "generation")
    if actual.stack and actual.stack != str(desired_stack):
        return ReconcileDecision(provider, slot_id, "recreate", "stack")
    if actual.transport and actual.transport != str(desired_transport):
        return ReconcileDecision(provider, slot_id, "recreate", "transport")
    if actual.block and getattr(desired, "block", None) and actual.block != desired.block:
        return ReconcileDecision(provider, slot_id, "recreate", "block")
    if actual.health in ("unhealthy", "failed"):
        return ReconcileDecision(provider, slot_id, "repair", "health")
    return ReconcileDecision(provider, slot_id, "noop", "conforme")


def reconcile(
    desired: list[DesiredStationSpec | DesiredEndpointSpec],
    actual: list[ActualSlot],
) -> list[ReconcileDecision]:
    by_key = {
        (item.provider, int(item.slot_id)): item
        for item in actual
    }
    decisions: list[ReconcileDecision] = []
    for spec in desired:
        key = (
            spec.provider,
            int(getattr(spec, "station", 0) or 0)
            if isinstance(spec, DesiredStationSpec)
            else 0,
        )
        if isinstance(spec, DesiredEndpointSpec):
            # Les endpoints n'ont pas de slot_id numérique stable : on apparie
            # par container, qui est unique.
            current = next(
                (
                    item
                    for item in actual
                    if item.provider == spec.provider and item.container == spec.container
                ),
                None,
            )
        else:
            current = by_key.get(key)
        decisions.append(decide(spec, current))
    return decisions
