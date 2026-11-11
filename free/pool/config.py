"""Config helpers for the free IP pool.

Extracted from the legacy pool so the ratchet sees a smaller monolith and the
clamping rule is testable without constructing a pool.
"""

from __future__ import annotations

import logging
import math
from typing import Any

_LOGGER = logging.getLogger("free.pool")


def clamp_seconds(
    cfg: dict[str, Any],
    key: str,
    lo: float,
    hi: float,
    *,
    logger: logging.Logger | None = None,
) -> float | None:
    """Read a duration key with bounds.

    Missing or invalid → ``None`` (the caller keeps its current value);
    outside bounds → clamped with a warning. Never silently overwrite an
    existing setting with a default.
    """

    log = logger or _LOGGER
    if key not in cfg:
        return None
    raw = cfg[key]
    try:
        val = float(raw)
    except (TypeError, ValueError):
        log.warning(
            "[pool-config] %s=%r invalide — valeur courante conservée",
            key,
            raw,
        )
        return None
    if math.isnan(val):
        log.warning(
            "[pool-config] %s=%r invalide (NaN) — valeur courante conservée",
            key,
            raw,
        )
        return None
    if val < lo or val > hi:
        clamped = max(lo, min(hi, val))
        log.warning(
            "[pool-config] %s=%s hors bornes [%s, %s] — clamp à %s",
            key,
            raw,
            lo,
            hi,
            clamped,
        )
        return clamped
    return val


__all__ = ["clamp_seconds"]
