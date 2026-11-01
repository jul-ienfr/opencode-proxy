"""Adapters for the IP registry (Compose binder, future probe/catalog wiring)."""

from __future__ import annotations

from .compose_binder import ComposeHostnameBinder

__all__ = ["ComposeHostnameBinder"]
