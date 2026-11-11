"""Politique de réparation des stations — classification et escalade bornée.

Ce module est pur : il décide *quoi faire* et *quand*, jamais *comment*
exécuter Docker. Le manager applique les actions (restart, stop réel, pin,
recreate, stack, park) et pousse les observations.

Séquence auth du plan :

``restart immédiat -> stop 5 min -> stop 15 min -> pin dernier bon serveur
-> recreate -> stack -> park 30 min``.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

AUTH_THROTTLE = "auth_throttle"
AUTH_GLOBAL = "auth_global"
CREDENTIALS = "credentials"
SERVER = "server"
EGRESS = "egress"
DOCKER = "docker"
CAPACITY = "capacity"
UNKNOWN = "unknown"

RESTART = "restart"
STOP = "stop"
PIN_LAST_GOOD = "pin_last_good"
RECREATE = "recreate"
STACK = "stack"
PARK = "park"


def _now(now: float | None) -> float:
    return float(now if now is not None else time.monotonic())


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def classify_repair_cause(
    *,
    auth_failed: bool = False,
    auth_global: bool = False,
    credentials_suspect: bool = False,
    server_issue: bool = False,
    egress_dead: bool = False,
    docker_down: bool = False,
) -> str:
    """Classe une panne, dans l'ordre de gravité du plan."""
    if credentials_suspect:
        return CREDENTIALS
    if docker_down:
        return DOCKER
    if auth_global:
        return AUTH_GLOBAL
    if auth_failed:
        return AUTH_THROTTLE
    if server_issue:
        return SERVER
    if egress_dead:
        return EGRESS
    return UNKNOWN


@dataclass
class RepairPolicy:
    enabled: bool = False
    detect_after_s: float = 20.0
    good_probes: int = 2
    bad_probes: int = 2
    auth_backoff_s: tuple[int, ...] = (300, 900, 1800)
    park_after_failures: int = 3
    park_s: float = 1800.0
    max_restarts_per_hour: int = 3
    max_recreates_per_hour: int = 1
    auth_global_window_s: float = 60.0
    auth_global_min_stations: int = 2
    auth_global_pause_s: float = 300.0
    verify_egress_before_usable: bool = True
    last_good_first: bool = True
    egress_recovery_deadline_s: float = 240.0
    egress_restart_budget_per_hour: int = 6
    egress_flip_after_s: float = 60.0
    egress_recreate_after_s: float = 120.0
    egress_auth_cool_max_wait_s: float = 30.0
    connect_hard_deadline_s: float = 180.0

    @classmethod
    def from_config(cls, cfg: dict | None) -> RepairPolicy:
        raw = cfg if isinstance(cfg, dict) else {}
        nested = raw.get("station_repair")
        data = nested if isinstance(nested, dict) else {}
        backoff_raw = data.get("auth_backoff_s", (300, 900, 1800))
        backoff: list[int] = []
        if isinstance(backoff_raw, (list, tuple)):
            for item in backoff_raw:
                value = _as_int(item, 0)
                if value > 0:
                    backoff.append(value)
        if not backoff:
            backoff = [300, 900, 1800]
        return cls(
            enabled=bool(data.get("enabled", False)),
            detect_after_s=max(0.0, _as_float(data.get("detect_after_s", 20), 20.0)),
            good_probes=max(1, _as_int(data.get("good_probes", 2), 2)),
            bad_probes=max(1, _as_int(data.get("bad_probes", 2), 2)),
            auth_backoff_s=tuple(backoff),
            park_after_failures=max(1, _as_int(data.get("park_after_failures", 3), 3)),
            park_s=max(30.0, _as_float(data.get("park_s", 1800), 1800.0)),
            max_restarts_per_hour=max(1, _as_int(data.get("max_restarts_per_hour", 3), 3)),
            max_recreates_per_hour=max(1, _as_int(data.get("max_recreates_per_hour", 1), 1)),
            auth_global_window_s=max(5.0, _as_float(data.get("auth_global_window_s", 60), 60.0)),
            auth_global_min_stations=max(2, _as_int(data.get("auth_global_min_stations", 2), 2)),
            auth_global_pause_s=max(30.0, _as_float(data.get("auth_global_pause_s", 300), 300.0)),
            verify_egress_before_usable=bool(data.get("verify_egress_before_usable", True)),
            last_good_first=bool(data.get("last_good_first", True)),
            egress_recovery_deadline_s=max(
                30.0, _as_float(data.get("egress_recovery_deadline_s", 240), 240.0)
            ),
            egress_restart_budget_per_hour=max(
                1, _as_int(data.get("egress_restart_budget_per_hour", 6), 6)
            ),
            egress_flip_after_s=max(15.0, _as_float(data.get("egress_flip_after_s", 60), 60.0)),
            egress_recreate_after_s=max(
                30.0, _as_float(data.get("egress_recreate_after_s", 120), 120.0)
            ),
            egress_auth_cool_max_wait_s=max(
                0.0, _as_float(data.get("egress_auth_cool_max_wait_s", 30), 30.0)
            ),
            connect_hard_deadline_s=max(
                60.0, _as_float(data.get("connect_hard_deadline_s", 180), 180.0)
            ),
        )

    def stop_delay_for(self, attempt: int) -> float:
        if not self.auth_backoff_s:
            return 300.0
        idx = min(max(0, int(attempt) - 1), len(self.auth_backoff_s) - 1)
        return float(self.auth_backoff_s[idx])

    def steps(self) -> list[str]:
        return [RESTART, STOP, STOP, PIN_LAST_GOOD, RECREATE, STACK, PARK]

    def step_for(self, attempt: int) -> str:
        steps = self.steps()
        idx = max(0, min(int(attempt), len(steps) - 1))
        return steps[idx]


@dataclass
class RepairAction:
    action: str
    attempt: int
    delay_s: float = 0.0
    reason: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "attempt": self.attempt,
            "delay_s": self.delay_s,
            "reason": self.reason,
        }


@dataclass
class RepairState:
    attempt: int = 0
    parked_until: float = 0.0
    next_retry_at: float = 0.0
    last_action: str | None = None
    last_error: str | None = None
    last_error_at: float = 0.0
    last_repair_at: float = 0.0
    repair_successes: int = 0
    repair_failures: int = 0
    action_required: bool = False
    last_good_country: str | None = None
    last_good_host: str | None = None
    last_good_ip: str | None = None
    last_good_at: float = 0.0

    def due(self, now: float | None = None) -> bool:
        ts = _now(now)
        if self.parked_until and ts < self.parked_until:
            return False
        return ts >= self.next_retry_at

    def plan(self, policy: RepairPolicy, now: float | None = None) -> RepairAction:
        ts = _now(now)
        if self.parked_until and ts < self.parked_until:
            return RepairAction(PARK, self.attempt, self.parked_until - ts, "parked")
        action = policy.step_for(self.attempt)
        if action == RESTART:
            delay = 0.0
        elif action == STOP:
            delay = policy.stop_delay_for(self.attempt)
        else:
            delay = 0.0
        return RepairAction(action, self.attempt, delay, self.last_error or "")

    def note_attempt(self, action: RepairAction, now: float | None = None) -> None:
        ts = _now(now)
        self.attempt = max(self.attempt + 1, action.attempt + 1)
        self.last_action = action.action
        self.last_repair_at = ts
        self.action_required = True
        if action.delay_s:
            self.next_retry_at = ts + action.delay_s
        else:
            self.next_retry_at = 0.0

    def note_success(self, now: float | None = None) -> None:
        ts = _now(now)
        self.repair_successes += 1
        self.attempt = 0
        self.parked_until = 0.0
        self.next_retry_at = 0.0
        self.last_error = None
        self.action_required = False
        self.last_repair_at = ts

    def note_failure(self, error: str, now: float | None = None) -> None:
        ts = _now(now)
        self.repair_failures += 1
        self.last_error = str(error or "repair-failed")[:300]
        self.last_error_at = ts
        self.action_required = True

    def park(self, policy: RepairPolicy, now: float | None = None, *, cause: str | None = None) -> None:
        ts = _now(now)
        self.parked_until = ts + max(30.0, float(policy.park_s))
        self.next_retry_at = self.parked_until
        self.last_error = cause or self.last_error or "parked"
        self.last_error_at = ts
        self.action_required = True

    def mark_last_good(
        self,
        *,
        country: str | None = None,
        host: str | None = None,
        ip: str | None = None,
        now: float | None = None,
    ) -> None:
        ts = _now(now)
        if country:
            self.last_good_country = str(country)
        if host:
            self.last_good_host = str(host)
        if ip:
            self.last_good_ip = str(ip)
        if country or host or ip:
            self.last_good_at = ts

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        ts = _now(now)
        return {
            "attempt": self.attempt,
            "parked_remaining_s": max(0.0, self.parked_until - ts),
            "next_retry_in_s": max(0.0, self.next_retry_at - ts) if self.next_retry_at else 0.0,
            "last_action": self.last_action,
            "last_error": self.last_error,
            "last_repair_age_s": round(max(0.0, ts - self.last_repair_at), 1) if self.last_repair_at else None,
            "repair_successes": self.repair_successes,
            "repair_failures": self.repair_failures,
            "action_required": self.action_required,
            "last_good_country": self.last_good_country,
            "last_good_host": self.last_good_host,
            "last_good_ip": self.last_good_ip,
            "last_good_age_s": round(max(0.0, ts - self.last_good_at), 1) if self.last_good_at else None,
        }


@dataclass
class _AuthScope:
    """One independent AUTH pause bucket (global or per transport)."""

    events: deque[tuple[float, str]] = field(
        default_factory=lambda: deque(maxlen=128)
    )
    pause_until: float = 0.0
    level: int = 0
    probe_owner: str | None = None
    last_trigger_at: float = 0.0


@dataclass
class GlobalAuthGuard:
    """Garde auth : 2+ stations en échec -> une seule probe.

    [P0 2026-11-11] The guard is transport scoped. An OpenVPN AUTH_FAILED
    must not freeze WireGuard repairs: the two transports fail for different
    reasons and NordVPN rate-limits them independently. Calls without
    ``transport`` keep the historical global behaviour (dashboard, legacy
    callers); calls with a transport only pause that transport.

    ``decay_s`` forgets the escalation level after a quiet period so a single
    old storm can never pin the fleet at the maximum pause forever. Any real
    egress proof calls :meth:`clear` and drops the level immediately.
    """

    window_s: float = 60.0
    min_stations: int = 2
    pause_s: float = 300.0
    max_pause_s: float = 900.0
    decay_s: float = 300.0
    global_scope: _AuthScope = field(default_factory=_AuthScope)
    transports: dict[str, _AuthScope] = field(default_factory=dict)

    # Read-only compatibility views of the historical flat fields.
    @property
    def events(self) -> deque[tuple[float, str]]:
        return self.global_scope.events

    @property
    def pause_until(self) -> float:
        return self.global_scope.pause_until

    @property
    def level(self) -> int:
        return self.global_scope.level

    @property
    def probe_owner(self) -> str | None:
        return self.global_scope.probe_owner

    @property
    def last_trigger_at(self) -> float:
        return self.global_scope.last_trigger_at

    def _scope(self, transport: str | None) -> _AuthScope:
        if transport is None or not str(transport).strip():
            return self.global_scope
        key = str(transport).strip().lower()
        return self.transports.setdefault(key, _AuthScope())

    def note_failure(
        self,
        station: str | int,
        now: float | None = None,
        *,
        transport: str | None = None,
    ) -> bool:
        ts = _now(now)
        scope = self._scope(transport)
        if (
            scope.level
            and self.decay_s > 0
            and (ts - scope.last_trigger_at) >= float(self.decay_s)
        ):
            scope.level = 0
        scope.events.append((ts, str(station)))
        window = max(1.0, self.window_s)
        while scope.events and ts - scope.events[0][0] > window:
            scope.events.popleft()
        distinct = {sid for t, sid in scope.events if ts - t <= window}
        if len(distinct) < max(2, int(self.min_stations)):
            return False
        if ts < scope.pause_until:
            return False
        scope.level = min(2, int(scope.level) + 1)
        pause = (
            float(self.pause_s)
            if scope.level <= 1
            else max(float(self.pause_s), float(self.max_pause_s))
        )
        scope.pause_until = ts + pause
        scope.last_trigger_at = ts
        scope.probe_owner = None
        return True

    def remaining(
        self, now: float | None = None, *, transport: str | None = None
    ) -> float:
        ts = _now(now)
        if transport is None or not str(transport).strip():
            # No transport: report the worst active pause so the dashboard and
            # the supervisor still see a storm, whatever transport caused it.
            values = [self.global_scope.pause_until]
            values.extend(scope.pause_until for scope in self.transports.values())
            return max(0.0, max(values) - ts)
        scope = self._scope(transport)
        return max(0.0, scope.pause_until - ts)

    def allow_probe(
        self,
        station: str | int,
        now: float | None = None,
        *,
        transport: str | None = None,
    ) -> bool:
        ts = _now(now)
        scope = self._scope(transport)
        if ts >= scope.pause_until:
            return True
        sid = str(station)
        if scope.probe_owner is None:
            scope.probe_owner = sid
            return True
        return scope.probe_owner == sid

    def clear(self, *, transport: str | None = None) -> None:
        if transport is None or not str(transport).strip():
            self.global_scope = _AuthScope()
            self.transports.clear()
            return
        self.transports.pop(str(transport).strip().lower(), None)

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        ts = _now(now)
        return {
            "pause_remaining_s": round(self.remaining(ts), 1),
            "level": self.global_scope.level,
            "probe_owner": self.global_scope.probe_owner,
            "events": len(self.global_scope.events),
            "transports": {
                key: {
                    "pause_remaining_s": round(max(0.0, scope.pause_until - ts), 1),
                    "level": scope.level,
                    "probe_owner": scope.probe_owner,
                }
                for key, scope in self.transports.items()
            },
        }


__all__ = [
    "AUTH_GLOBAL",
    "AUTH_THROTTLE",
    "CAPACITY",
    "CREDENTIALS",
    "DOCKER",
    "EGRESS",
    "PARK",
    "PIN_LAST_GOOD",
    "RECREATE",
    "RESTART",
    "SERVER",
    "STACK",
    "STOP",
    "UNKNOWN",
    "GlobalAuthGuard",
    "RepairAction",
    "RepairPolicy",
    "RepairState",
    "classify_repair_cause",
]
