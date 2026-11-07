"""Politique de repli d'egress — configuration validée (feature 007).

Ce module est volontairement pur : il ne lit pas `config.yaml` lui-même et ne
connaît ni le pool ni les providers. `config/settings.py` fournit le mapping
YAML ; les décisions runtime viendront dans un second temps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_ORDER: tuple[str, ...] = (
    "warp",
    "warp_alt",
    "isp_ipv6",
    "oracle",
    "proton",
    "windscribe",
    "vps_ipv6",
)

KNOWN_PROVIDERS = frozenset(
    {
        "warp",
        "warp_alt",
        "isp_ipv6",
        "oracle",
        "proton",
        "windscribe",
        "vps_ipv6",
        "static",
    }
)

# Les providers payants restent documentés mais ne sont jamais activés par
# défaut ; `from_mapping` ne les retire pas si l'opérateur les a explicitement
# listés.
PAID_PROVIDERS = frozenset({"vps_ipv6"})


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    return default


@dataclass(frozen=True, slots=True)
class FallbackConfig:
    order: tuple[str, ...] = DEFAULT_ORDER
    max_fallback_stations: int = 2
    probe_interval_s: int = 300
    recovery_successes: int = 2
    cooldown_s: int = 900
    max_block_s: int = 0
    park_window_s: int = 1800
    fail_closed: bool = True

    @classmethod
    def from_mapping(cls, raw: Any) -> FallbackConfig:
        data = raw if isinstance(raw, dict) else {}
        order_raw = data.get("order")
        if isinstance(order_raw, (list, tuple)):
            seen: list[str] = []
            for item in order_raw:
                name = str(item or "").strip().lower()
                if name in KNOWN_PROVIDERS and name not in seen:
                    seen.append(name)
            order = tuple(seen) or DEFAULT_ORDER
        else:
            order = DEFAULT_ORDER
        return cls(
            order=order,
            max_fallback_stations=max(
                1, min(2, _as_int(data.get("max_fallback_stations", 2), 2))
            ),
            probe_interval_s=max(
                60, min(3600, _as_int(data.get("probe_interval_s", 300), 300))
            ),
            recovery_successes=max(
                1, min(5, _as_int(data.get("recovery_successes", 2), 2))
            ),
            cooldown_s=max(
                300, min(86400, _as_int(data.get("cooldown_s", 900), 900))
            ),
            max_block_s=max(
                0, min(86400, _as_int(data.get("max_block_s", 0), 0))
            ),
            park_window_s=max(
                300, min(7200, _as_int(data.get("park_window_s", 1800), 1800))
            ),
            fail_closed=_as_bool(data.get("fail_closed", True), True),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "order": list(self.order),
            "max_fallback_stations": self.max_fallback_stations,
            "probe_interval_s": self.probe_interval_s,
            "recovery_successes": self.recovery_successes,
            "cooldown_s": self.cooldown_s,
            "max_block_s": self.max_block_s,
            "park_window_s": self.park_window_s,
            "fail_closed": self.fail_closed,
        }


__all__ = [
    "DEFAULT_ORDER",
    "FallbackConfig",
    "KNOWN_PROVIDERS",
    "PAID_PROVIDERS",
]
