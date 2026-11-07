"""Politique de repli d'egress — configuration validée (feature 007).

Ce module est volontairement pur : il ne lit pas `config.yaml` lui-même et ne
connaît ni le pool ni les providers. `config/settings.py` fournit le mapping
YAML ; les décisions runtime viendront dans un second temps.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
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


class FallbackTrigger(StrEnum):
    QUOTA = "quota"
    WARP_DOWN = "warp_down"
    REPEATED_BLOCK = "repeated_block"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class FallbackDecision:
    """Décision pure : le pool applique, la politique décide."""

    action: str  # stay | fallback | return | degraded | noop
    station: str = ""
    provider: str | None = None
    reason: str = ""
    active: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "station": self.station,
            "provider": self.provider,
            "reason": self.reason,
            "active": self.active,
        }


class FallbackPolicy:
    """Politique de bascule gratuite, en mémoire et sans I/O.

    Le pool fournit ``available`` (providers réellement servables) et ``now`` ;
    la politique ne touche ni au réseau ni à la config. Elle garantit :
    - WARP n'est jamais choisi comme fallback de lui-même ;
    - au plus ``max_fallback_stations`` stations en fallback ;
    - retour WARP après ``recovery_successes`` probes 200 + cooldown ;
    - un 403/auth n'est jamais un déclencheur.
    """

    def __init__(self, config: FallbackConfig | None = None) -> None:
        self.config = config or FallbackConfig()
        self._active: dict[str, str] = {}
        self._successes: dict[str, int] = {}
        self._last_switch_at: float = 0.0
        self._lock = RLock()

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def active_provider(self, station: str) -> str | None:
        with self._lock:
            return self._active.get(str(station))

    def select_provider(self, available: set[str] | frozenset[str]) -> str | None:
        """Premier provider de l'ordre qui n'est ni WARP ni déjà saturé."""
        for name in self.config.order:
            if name in ("warp", "static"):
                continue
            if name in available:
                return name
        return None

    def evaluate(
        self,
        station: str,
        trigger: FallbackTrigger,
        available: set[str] | frozenset[str],
        *,
        now: float,
    ) -> FallbackDecision:
        label = str(station or "")
        with self._lock:
            if not label:
                return FallbackDecision("noop", reason="empty_station")
            if label in self._active:
                return FallbackDecision(
                    "stay",
                    station=label,
                    provider=self._active[label],
                    reason="already_on_fallback",
                    active=len(self._active),
                )
            if len(self._active) >= self.config.max_fallback_stations:
                return FallbackDecision(
                    "degraded",
                    station=label,
                    reason="max_fallback_stations",
                    active=len(self._active),
                )
            provider = self.select_provider(available)
            if provider is None:
                return FallbackDecision(
                    "degraded",
                    station=label,
                    reason="no_fallback_provider",
                    active=len(self._active),
                )
            self._active[label] = provider
            self._successes[label] = 0
            self._last_switch_at = float(now)
            return FallbackDecision(
                "fallback",
                station=label,
                provider=provider,
                reason=trigger.value,
                active=len(self._active),
            )

    def evaluate_error(
        self,
        station: str,
        error_class: str,
        available: set[str] | frozenset[str],
        *,
        now: float,
    ) -> FallbackDecision:
        """Mapping taxonomie → fallback. 403/AUTH/rate-limit/5xx = noop."""
        cls = str(error_class or "").upper()
        if cls in ("GATE_403", "AUTH", "RATE_LIMIT_429", "TRANSIENT_5XX"):
            return FallbackDecision("noop", station=str(station or ""), reason="not_a_quota")
        if cls in ("QUOTA_429", "ACCOUNT_OR_GLOBAL"):
            return self.evaluate(station, FallbackTrigger.QUOTA, available, now=now)
        return FallbackDecision("noop", station=str(station or ""), reason="unknown_class")

    def note_probe(
        self,
        station: str,
        provider: str,
        ok: bool,
        *,
        warp_healthy: bool,
        now: float,
    ) -> FallbackDecision:
        """Enregistre un probe du provider fallback et décide le retour WARP."""
        label = str(station or "")
        with self._lock:
            active_provider = self._active.get(label)
            if active_provider is None:
                return FallbackDecision("noop", station=label, reason="not_on_fallback")
            if str(provider or "") != active_provider:
                return FallbackDecision(
                    "noop",
                    station=label,
                    provider=active_provider,
                    reason="provider_mismatch",
                    active=len(self._active),
                )
            if not ok:
                self._successes[label] = 0
                return FallbackDecision(
                    "stay",
                    station=label,
                    provider=active_provider,
                    reason="probe_failed",
                    active=len(self._active),
                )
            self._successes[label] = int(self._successes.get(label, 0)) + 1
            if (
                warp_healthy
                and self._successes[label] >= self.config.recovery_successes
                and float(now) - self._last_switch_at >= self.config.cooldown_s
            ):
                self._active.pop(label, None)
                self._successes.pop(label, None)
                self._last_switch_at = float(now)
                return FallbackDecision(
                    "return",
                    station=label,
                    provider="warp",
                    reason="warp_recovered",
                    active=len(self._active),
                )
            return FallbackDecision(
                "stay",
                station=label,
                provider=active_provider,
                reason="probe_ok",
                active=len(self._active),
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": dict(self._active),
                "successes": dict(self._successes),
                "active_count": len(self._active),
                "max_fallback_stations": self.config.max_fallback_stations,
                "last_switch_at": self._last_switch_at,
                "fail_closed": self.config.fail_closed,
            }


__all__ = [
    "DEFAULT_ORDER",
    "FallbackConfig",
    "FallbackDecision",
    "FallbackPolicy",
    "FallbackTrigger",
    "KNOWN_PROVIDERS",
    "PAID_PROVIDERS",
]
