"""Adapters for the IP registry (Compose binder, future probe/catalog wiring)."""

from __future__ import annotations

from .compose_binder import ComposeHostnameBinder
from .egress import StationEgress
from .probe import StationProbe

__all__ = ["ComposeHostnameBinder", "StationEgress", "StationProbe"]
