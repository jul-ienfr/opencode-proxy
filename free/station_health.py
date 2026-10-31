"""Santé d'une station free : machine à états et contrats d'éligibilité.

Ce module est volontairement pur : aucun Docker, aucun réseau, aucune horloge
implicite. Le pool et le watchdog lui passent des observations datées.

Contrats :

- ``observe_probe`` : 1 mauvais probe -> ``suspect``, 2 -> ``repairing`` ;
  2 bons probes -> ``usable``. Un seul probe ne retire jamais une station.
- ``repair_succeeded`` : retour en ``probing``, jamais directement ``usable``.
- ``park`` : station exclue jusqu'à ``parked_until`` ; le retry repasse par
  ``repairing``.
- ``station_contract`` : le contrat ``station_usable`` du plan (auth, egress,
  fraîcheur, pas de réparation/park).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

USABLE = "usable"
SUSPECT = "suspect"
REPAIRING = "repairing"
PROBING = "probing"
PARKED = "parked"
DISABLED = "disabled"

_ALL_STATES = {USABLE, SUSPECT, REPAIRING, PROBING, PARKED, DISABLED}


def _now(now: float | None) -> float:
    return float(now if now is not None else time.monotonic())


@dataclass
class StationHealth:
    """État borné d'une station (par numéro de station)."""

    station: int
    state: str = USABLE
    good_probes: int = 0
    bad_probes: int = 0
    repair_attempts: int = 0
    repair_successes: int = 0
    last_probe_at: float = 0.0
    last_ok_at: float = 0.0
    last_error_at: float = 0.0
    last_error: str | None = None
    cause: str | None = None
    repairing_since: float = 0.0
    parked_until: float = 0.0
    next_retry_at: float = 0.0
    action_required: bool = False
    transitions: int = 0
    _last_state: str = field(default=USABLE, repr=False)

    def __post_init__(self) -> None:
        if self.state not in _ALL_STATES:
            self.state = DISABLED
        self._last_state = self.state

    def _transition(self, state: str) -> None:
        if state not in _ALL_STATES:
            return
        if state != self.state:
            self.state = state
            self.transitions += 1
        self._last_state = state

    @property
    def usable(self) -> bool:
        return self.state == USABLE

    @property
    def selectable(self) -> bool:
        return self.state in (USABLE, PROBING)

    def observe_probe(
        self,
        ok: bool,
        now: float | None = None,
        *,
        good_needed: int = 2,
        bad_needed: int = 2,
    ) -> str:
        """Enregistre un probe ; retourne le nouvel état.

        ``probing`` exige ``good_needed`` bons probes avant de repasser
        ``usable``. ``repairing`` n'est jamais guéri par un probe seul : il
        faut appeler ``repair_succeeded`` puis deux bons probes.
        """
        ts = _now(now)
        self.last_probe_at = ts
        good_needed = max(1, int(good_needed))
        bad_needed = max(1, int(bad_needed))
        if self.state == DISABLED:
            return self.state
        if self.state == PARKED:
            if ts < self.parked_until:
                return self.state
            # Le park est expiré : on repasse par une réparation planifiée.
            self._transition(REPAIRING)
            self.repairing_since = ts
            return self.state
        if self.state == REPAIRING:
            # Une réparation peut être externe (watchdog, stack, redémarrage
            # Docker) : deux probes réellement bons doivent pouvoir rouvrir la
            # station. Sinon un état `repairing` sans worker resterait collé à
            # vie et exclurait une station pourtant guérie.
            if ok:
                self.good_probes += 1
                self.bad_probes = 0
                self.last_ok_at = ts
                self.last_error = None
                if self.good_probes >= good_needed:
                    self.action_required = False
                    self.cause = None
                    self._transition(USABLE)
                elif self.good_probes >= 1:
                    self._transition(PROBING)
            else:
                self.bad_probes += 1
            return self.state
        if ok:
            self.good_probes += 1
            self.bad_probes = 0
            self.last_ok_at = ts
            self.last_error = None
            if self.state == PROBING and self.good_probes >= good_needed:
                self._transition(USABLE)
            elif self.state == SUSPECT and self.good_probes >= good_needed:
                self._transition(USABLE)
            elif self.state == USABLE:
                self.good_probes = max(self.good_probes, good_needed)
            return self.state
        self.good_probes = 0
        self.bad_probes += 1
        self.last_error_at = ts
        if self.state == PROBING:
            self._transition(REPAIRING)
            self.repairing_since = ts
        elif self.state == USABLE:
            self._transition(SUSPECT)
        elif self.state == SUSPECT and self.bad_probes >= bad_needed:
            self._transition(REPAIRING)
            self.repairing_since = ts
        return self.state

    def begin_repair(self, now: float | None = None, *, cause: str | None = None) -> None:
        ts = _now(now)
        self.repair_attempts += 1
        self.repairing_since = ts
        self.cause = cause or self.cause
        self.action_required = True
        self._transition(REPAIRING)

    def repair_succeeded(self, now: float | None = None) -> None:
        ts = _now(now)
        self.repair_successes += 1
        self.good_probes = 0
        self.bad_probes = 0
        self.last_ok_at = ts
        self.last_error = None
        self.cause = None
        self.parked_until = 0.0
        self.next_retry_at = 0.0
        self.action_required = False
        self._transition(PROBING)

    def repair_failed(
        self,
        now: float | None = None,
        *,
        cause: str | None = None,
        next_retry_at: float = 0.0,
    ) -> None:
        ts = _now(now)
        self.cause = cause or self.cause
        self.last_error_at = ts
        self.last_error = cause or "repair-failed"
        self.next_retry_at = max(0.0, float(next_retry_at or 0.0))
        self.action_required = True
        self._transition(REPAIRING)

    def park(self, now: float | None = None, *, seconds: float = 1800.0, cause: str | None = None) -> None:
        ts = _now(now)
        self.parked_until = ts + max(0.0, float(seconds))
        self.next_retry_at = self.parked_until
        self.cause = cause or self.cause
        self.last_error = cause or self.last_error or "parked"
        self.last_error_at = ts
        self.action_required = True
        self._transition(PARKED)

    def resume_if_due(self, now: float | None = None) -> bool:
        ts = _now(now)
        if self.state == PARKED and ts >= self.parked_until:
            self._transition(REPAIRING)
            self.repairing_since = ts
            self.next_retry_at = 0.0
            return True
        return False

    def disable(self) -> None:
        self.action_required = False
        self._transition(DISABLED)

    def enable(self) -> None:
        if self.state == DISABLED:
            self._transition(PROBING)
            self.good_probes = 0
            self.bad_probes = 0

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        ts = _now(now)
        return {
            "station": self.station,
            "state": self.state,
            "usable": self.usable,
            "good_probes": self.good_probes,
            "bad_probes": self.bad_probes,
            "repair_attempts": self.repair_attempts,
            "repair_successes": self.repair_successes,
            "last_probe_age_s": round(max(0.0, ts - self.last_probe_at), 1) if self.last_probe_at else None,
            "last_ok_age_s": round(max(0.0, ts - self.last_ok_at), 1) if self.last_ok_at else None,
            "cause": self.cause,
            "last_error": self.last_error,
            "repairing_s": round(max(0.0, ts - self.repairing_since), 1) if self.repairing_since else 0.0,
            "parked_remaining_s": max(0.0, self.parked_until - ts),
            "next_retry_in_s": max(0.0, self.next_retry_at - ts) if self.next_retry_at else 0.0,
            "action_required": bool(self.action_required),
            "transitions": self.transitions,
        }


class StationHealthRegistry:
    """Registre borné des santé par station."""

    def __init__(self, *, max_entries: int = 64) -> None:
        self._items: dict[int, StationHealth] = {}
        self._max_entries = max(1, int(max_entries))

    def get(self, station_id: int, *, create: bool = True) -> StationHealth | None:
        sid = int(station_id)
        item = self._items.get(sid)
        if item is None and create:
            item = StationHealth(station=sid)
            self._items[sid] = item
            while len(self._items) > self._max_entries:
                self._items.pop(next(iter(self._items)))
        return item

    def observe(self, station_id: int, ok: bool, now: float | None = None, **kw) -> str:
        item = self.get(station_id)
        assert item is not None
        return item.observe_probe(ok, now, **kw)

    def remove(self, station_id: int) -> None:
        self._items.pop(int(station_id), None)

    def prune(self, keep_sids) -> None:
        keep = {int(s) for s in keep_sids}
        for sid in list(self._items):
            if sid not in keep:
                self._items.pop(sid, None)

    def snapshot(self, now: float | None = None) -> dict[int, dict[str, Any]]:
        ts = _now(now)
        return {sid: item.snapshot(ts) for sid, item in sorted(self._items.items())}

    def export_state(self) -> dict[str, dict[str, Any]]:
        """État minimal persistable (sans les âges relatifs)."""
        out: dict[str, dict[str, Any]] = {}
        for sid, item in self._items.items():
            out[str(sid)] = {
                "state": item.state,
                "good_probes": item.good_probes,
                "bad_probes": item.bad_probes,
                "repair_attempts": item.repair_attempts,
                "repair_successes": item.repair_successes,
                "last_ok_at": item.last_ok_at,
                "last_error": item.last_error,
                "cause": item.cause,
                "parked_until": item.parked_until,
                "next_retry_at": item.next_retry_at,
                "action_required": item.action_required,
            }
        return out

    def restore_state(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        for sid_s, raw in data.items():
            try:
                sid = int(sid_s)
                if not isinstance(raw, dict):
                    continue
                item = self.get(sid)
                if item is None:
                    continue
                state = str(raw.get("state") or USABLE)
                if state in _ALL_STATES:
                    item.state = state
                item.good_probes = max(0, int(raw.get("good_probes", 0) or 0))
                item.bad_probes = max(0, int(raw.get("bad_probes", 0) or 0))
                item.repair_attempts = max(0, int(raw.get("repair_attempts", 0) or 0))
                item.repair_successes = max(0, int(raw.get("repair_successes", 0) or 0))
                item.last_ok_at = max(0.0, float(raw.get("last_ok_at", 0.0) or 0.0))
                item.last_error = raw.get("last_error")
                item.cause = raw.get("cause")
                item.parked_until = max(0.0, float(raw.get("parked_until", 0.0) or 0.0))
                item.next_retry_at = max(0.0, float(raw.get("next_retry_at", 0.0) or 0.0))
                item.action_required = bool(raw.get("action_required", False))
            except (TypeError, ValueError):
                continue


def station_contract(
    station: Any,
    *,
    health: StationHealth | None = None,
    now: float | None = None,
    max_effective_age_s: float = 60.0,
) -> tuple[bool, str | None]:
    """Contrat ``station_usable`` strict.

    Retourne ``(True, None)`` ou ``(False, raison)``. Les attributs absents
    sont tolérés (doubles de test, endpoints SOCKS5) : le contrat ne doit pas
    inventer une panne à partir d'une absence de donnée.
    """
    ts = _now(now)
    if bool(getattr(station, "_disabled", False)):
        return False, "disabled"
    if health is not None and not health.usable:
        return False, f"health={health.state}"
    try:
        status = str(getattr(station, "status", "") or "")
    except Exception:
        status = ""
    if status and status not in ("connected", "degraded"):
        return False, f"status={status}"
    if bool(getattr(station, "_auth_failed", False)):
        return False, "auth-failed"
    if float(getattr(station, "_auth_park_until", 0.0) or 0.0) > ts:
        return False, "auth-parked"
    if bool(getattr(station, "_repairing", False)):
        return False, "repairing"
    if float(getattr(station, "_parked_until", 0.0) or 0.0) > ts:
        return False, "parked"
    egress = str(getattr(station, "_egress_state", "") or "")
    if egress == "tunnel_down":
        return False, "egress=tunnel_down"
    last_ok = float(getattr(station, "_last_effective_ok_at", 0.0) or 0.0)
    if last_ok > 0.0 and max_effective_age_s > 0:
        age = ts - last_ok
        if age > max_effective_age_s:
            return False, "egress-stale"
    return True, None


__all__ = [
    "DISABLED",
    "PARKED",
    "PROBING",
    "REPAIRING",
    "SUSPECT",
    "USABLE",
    "StationHealth",
    "StationHealthRegistry",
    "station_contract",
]
