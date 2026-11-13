"""Adapters for the IP registry (Compose binder, future probe/catalog wiring)."""

from __future__ import annotations

from .compose_binder import ComposeHostnameBinder
from .egress import StationEgress, host_public_ip
from .gluetun_control import GluetunControlBinder
from .gluetun_control_v2 import GluetunControlV2Binder, RotationOutcome
from .hybrid_binder import HybridBinder
from .probe import StationProbe

__all__ = [
    "ComposeHostnameBinder",
    "GluetunControlBinder",
    "GluetunControlV2Binder",
    "HybridBinder",
    "RotationOutcome",
    "StationEgress",
    "StationProbe",
    "host_public_ip",
]
