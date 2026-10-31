"""Lease IP/quota par modèle — source de vérité pour la sélection free.

Le quota est une propriété de ``(modèle, IP)``, pas de la station : une IP
excellente pour DeepSeek peut être épuisée pour GLM. Ce module maintient un
lease borné par couple et expose un contrat simple au pool :

- ``headroom`` = requêtes restantes estimées ;
- ``state`` ∈ ``warming | qualified | active | draining | exhausted | bad`` ;
- ``model_usable`` = headroom > réserve et pas de quarantaine active.

Aucun réseau, aucun Docker : les résultats réels sont poussés par le pool.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

WARMING = "warming"
QUALIFIED = "qualified"
ACTIVE = "active"
DRAINING = "draining"
EXHAUSTED = "exhausted"
BAD = "bad"

_SELECTABLE = {QUALIFIED, ACTIVE}
_NON_SELECTABLE = {WARMING, DRAINING, EXHAUSTED, BAD}


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


@dataclass
class QuotaProfile:
    """Profils de quota par modèle + marges de sécurité."""

    default_quota: int = 500
    by_model: dict[str, int] = field(default_factory=dict)
    reserve_pct: float = 0.10
    rotation_pct: float = 0.20
    bad_ip_ttl_s: float = 1800.0
    quota_ip_block_s: float = 120.0
    demand: int = 10

    @classmethod
    def from_config(cls, cfg: dict | None) -> QuotaProfile:
        raw = cfg if isinstance(cfg, dict) else {}
        nested = raw.get("ip_quota")
        quota_cfg = nested if isinstance(nested, dict) else raw
        by_model_raw = raw.get("quota_per_ip_by_model")
        by_model: dict[str, int] = {}
        if isinstance(by_model_raw, dict):
            for key, value in by_model_raw.items():
                q = _as_int(value, 0)
                if q > 0:
                    by_model[str(key).strip().lower()] = q
        reserve_pct = max(0.0, min(0.5, _as_float(quota_cfg.get("quota_reserve_pct", 10), 10.0) / 100.0))
        rotation_pct = max(
            reserve_pct,
            min(0.8, _as_float(quota_cfg.get("rotation_threshold_pct", 20), 20.0) / 100.0),
        )
        return cls(
            default_quota=max(1, _as_int(raw.get("quota_per_ip", 500), 500)),
            by_model=by_model,
            reserve_pct=reserve_pct,
            rotation_pct=rotation_pct,
            bad_ip_ttl_s=max(1.0, _as_float(raw.get("bad_ip_ttl_s", 1800), 1800.0)),
            quota_ip_block_s=max(1.0, _as_float(raw.get("quota_ip_block_s", 120), 120.0)),
            demand=max(1, _as_int(quota_cfg.get("selection_demand", 10), 10)),
        )

    def profile_for(self, model: str | None) -> int:
        key = str(model or "").strip().lower()
        if key and key in self.by_model:
            return self.by_model[key]
        # Un modèle suffixé (``-free``, variante) hérite du profil de sa
        # famille la plus longue : ``deepseek-v4.1-flash-free`` ->
        # ``deepseek-v4.1-flash``.
        best = 0
        best_len = 0
        for name, quota in self.by_model.items():
            if key.startswith(name) and len(name) > best_len:
                best = quota
                best_len = len(name)
        return best or self.default_quota

    def reserve_for(self, model: str | None) -> int:
        profile = self.profile_for(model)
        if profile <= 1:
            return 0
        return min(profile - 1, max(1, int(round(profile * self.reserve_pct))))

    def rotation_headroom_for(self, model: str | None) -> int:
        profile = self.profile_for(model)
        if profile <= 1:
            return 0
        reserve = self.reserve_for(model)
        return min(profile - 1, max(reserve, int(round(profile * self.rotation_pct))))

    def rotation_at_used(self, model: str | None) -> int:
        profile = self.profile_for(model)
        return max(1, profile - self.rotation_headroom_for(model))

    def reserve_pct_for(self, model: str | None) -> float:
        profile = self.profile_for(model)
        return self.reserve_for(model) / float(profile) if profile else 0.0


@dataclass
class IpQuotaLease:
    """État d'une IP pour un modèle donné."""

    station: int
    model: str
    ip: str
    quota_estimate: int
    used: int = 0
    success_count: int = 0
    error_count: int = 0
    recent_429: int = 0
    last_429_at: float = 0.0
    last_ok_at: float = 0.0
    last_request_at: float = 0.0
    blocked_until: float = 0.0
    state: str = QUALIFIED
    latency_ms: float | None = None
    server_health: float = 1.0
    inflight: int = 0
    rotation_count: int = 0

    @property
    def headroom(self) -> int:
        return max(0, int(self.quota_estimate) - int(self.used))

    @property
    def success_rate(self) -> float:
        # Lissage de Laplace : une IP fraîche n'est pas jugée sur 0 échec.
        return (self.success_count + 1.0) / (self.success_count + self.error_count + 2.0)

    @property
    def selectable(self) -> bool:
        return self.state in _SELECTABLE

    @property
    def blocked(self) -> bool:
        return self.blocked_until > _now(None)

    def refresh(self, profile: QuotaProfile, now: float | None = None) -> None:
        ts = _now(now)
        if self.state == BAD and self.blocked_until and ts >= self.blocked_until:
            # La quarantaine bad IP est expirée : l'IP redevient candidate,
            # mais jamais ``active`` avant une nouvelle preuve.
            self.state = QUALIFIED
            self.blocked_until = 0.0
        if self.headroom <= 0:
            self.state = EXHAUSTED
        elif self.headroom <= profile.rotation_headroom_for(self.model):
            if self.state not in (BAD, EXHAUSTED, WARMING):
                self.state = DRAINING
        elif self.state in (DRAINING, EXHAUSTED):
            self.state = ACTIVE if self.success_count else QUALIFIED

    def note_request(self, now: float | None = None) -> None:
        ts = _now(now)
        if self.state in _NON_SELECTABLE:
            return
        self.used += 1
        self.last_request_at = ts
        if self.state == QUALIFIED:
            self.state = ACTIVE

    def note_success(self, now: float | None = None, *, profile: QuotaProfile | None = None) -> None:
        ts = _now(now)
        self.success_count += 1
        self.last_ok_at = ts
        self.last_429_at = 0.0
        self.recent_429 = 0
        self.error_count = max(0, self.error_count)
        if self.state == BAD:
            self.state = ACTIVE
        if profile is not None:
            self.refresh(profile, ts)
        elif self.headroom > 0 and self.state in (WARMING, QUALIFIED, DRAINING):
            self.state = ACTIVE

    def note_429(
        self,
        now: float | None = None,
        *,
        profile: QuotaProfile,
        status: int | None = 429,
    ) -> str:
        ts = _now(now)
        self.error_count += 1
        self.recent_429 += 1
        self.last_429_at = ts
        if int(status or 0) != 429:
            return self.state
        if self.success_count <= 0:
            # 429 sans aucun succès : ce n'est pas un quota consommé, c'est
            # une mauvaise IP. Quarantaine longue.
            self.state = BAD
            self.blocked_until = max(self.blocked_until, ts + profile.bad_ip_ttl_s)
            return self.state
        self.blocked_until = max(self.blocked_until, ts + profile.quota_ip_block_s)
        if self.headroom <= 0:
            self.state = EXHAUSTED
        elif self.headroom <= profile.rotation_headroom_for(self.model):
            self.state = DRAINING
        else:
            self.state = ACTIVE
        return self.state

    def score_key(self, profile: QuotaProfile, *, demand: int | None = None) -> tuple:
        """Clé lexicographique : succès restants, puis latence, puis charge.

        ``success_remaining`` estime les requêtes utiles restantes avant le
        mur de quota : c'est ce qui doit dominer, pas la latence brute.
        """
        cap = max(1, int(demand if demand is not None else profile.demand))
        useful = min(self.headroom, cap) * self.success_rate
        latency = float(self.latency_ms) if self.latency_ms is not None else 5000.0
        return (
            round(useful, 6),
            -max(0.0, latency),
            -max(0, int(self.inflight)),
            -int(self.station),
        )

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        ts = _now(now)
        return {
            "station": self.station,
            "model": self.model,
            "ip": self.ip,
            "quota_estimate": self.quota_estimate,
            "used": self.used,
            "headroom": self.headroom,
            "success_count": self.success_count,
            "error_count": self.error_count,
            "success_rate": round(self.success_rate, 4),
            "recent_429": self.recent_429,
            "blocked_remaining_s": max(0.0, self.blocked_until - ts),
            "state": self.state,
            "selectable": self.selectable,
            "latency_ms": self.latency_ms,
            "server_health": round(float(self.server_health), 4),
            "inflight": self.inflight,
            "rotation_count": self.rotation_count,
            "last_ok_age_s": round(max(0.0, ts - self.last_ok_at), 1) if self.last_ok_at else None,
            "last_429_age_s": round(max(0.0, ts - self.last_429_at), 1) if self.last_429_at else None,
        }


class IpQuotaRegistry:
    """Registre borné des leases ``(modèle, station, IP)``."""

    def __init__(self, profile: QuotaProfile | None = None, *, max_entries: int = 4096) -> None:
        self.profile = profile or QuotaProfile()
        self._items: dict[tuple[str, str], IpQuotaLease] = {}
        self._max_entries = max(1, int(max_entries))

    @staticmethod
    def _key(model: str | None, station: Any, ip: str | None) -> tuple[str, str]:
        mid = str(model or "").strip().lower()
        logical_ip = str(ip or getattr(station, "current_ip", "") or getattr(station, "pid", "") or "unknown")
        # Le quota est une propriété de (modèle, IP), pas de la station : deux
        # stations qui partagent la même IP doivent partager le même lease.
        return mid, logical_ip

    def lease_for(
        self,
        model: str | None,
        station: Any,
        *,
        ip: str | None = None,
        now: float | None = None,
        create: bool = True,
    ) -> IpQuotaLease | None:
        key = self._key(model, station, ip)
        item = self._items.get(key)
        if item is None and create:
            try:
                sid = int(getattr(station, "_station", 0) or 0)
            except (TypeError, ValueError):
                sid = 0
            item = IpQuotaLease(
                station=sid,
                model=key[0],
                ip=key[1],
                quota_estimate=self.profile.profile_for(key[0]),
            )
            self._items[key] = item
            while len(self._items) > self._max_entries:
                self._items.pop(next(iter(self._items)))
        if item is not None:
            item.refresh(self.profile, now)
        return item

    def note_request(self, model: str | None, station: Any, *, ip: str | None = None, now: float | None = None) -> None:
        item = self.lease_for(model, station, ip=ip, now=now)
        if item is not None:
            item.note_request(now)

    def note_success(self, model: str | None, station: Any, *, ip: str | None = None, now: float | None = None) -> None:
        item = self.lease_for(model, station, ip=ip, now=now)
        if item is not None:
            item.note_success(now, profile=self.profile)

    def note_429(
        self,
        model: str | None,
        station: Any,
        *,
        ip: str | None = None,
        now: float | None = None,
        status: int | None = 429,
    ) -> str:
        item = self.lease_for(model, station, ip=ip, now=now)
        if item is None:
            return BAD
        return item.note_429(now, profile=self.profile, status=status)

    def note_result(
        self,
        model: str | None,
        station: Any,
        status: int | None,
        *,
        ip: str | None = None,
        now: float | None = None,
    ) -> None:
        code = int(status or 0)
        if 200 <= code < 300:
            self.note_success(model, station, ip=ip, now=now)
        elif code == 429:
            self.note_429(model, station, ip=ip, now=now, status=code)

    def headroom(self, model: str | None, station: Any, *, ip: str | None = None, now: float | None = None) -> int:
        item = self.lease_for(model, station, ip=ip, now=now)
        return item.headroom if item is not None else 0

    def state(self, model: str | None, station: Any, *, ip: str | None = None, now: float | None = None) -> str:
        item = self.lease_for(model, station, ip=ip, now=now)
        return item.state if item is not None else WARMING

    def model_usable(
        self,
        model: str | None,
        station: Any,
        *,
        ip: str | None = None,
        now: float | None = None,
        allow_draining: bool = False,
    ) -> tuple[bool, str | None]:
        item = self.lease_for(model, station, ip=ip, now=now)
        if item is None:
            return False, "no-lease"
        ts = _now(now)
        item.refresh(self.profile, ts)
        if item.state == BAD:
            return False, "bad-ip"
        if item.blocked_until > ts:
            return False, "quota-blocked"
        if item.state == WARMING:
            return False, "warming"
        if item.state == EXHAUSTED:
            return False, "exhausted"
        if item.state == DRAINING and not allow_draining:
            return False, "draining"
        if item.headroom <= self.profile.reserve_for(item.model):
            return False, "reserve"
        if not item.selectable and not (allow_draining and item.state == DRAINING):
            return False, f"state={item.state}"
        return True, None

    def ranking_key(self, lease: IpQuotaLease, *, demand: int | None = None) -> tuple:
        return lease.score_key(self.profile, demand=demand)

    def snapshot(self, limit: int = 80, now: float | None = None) -> list[dict[str, Any]]:
        ts = _now(now)
        items = list(self._items.values())[-max(1, int(limit)) :]
        return [item.snapshot(ts) for item in items]

    def export_state(self) -> list[dict[str, Any]]:
        return [
            {
                "station": item.station,
                "model": item.model,
                "ip": item.ip,
                "quota_estimate": item.quota_estimate,
                "used": item.used,
                "success_count": item.success_count,
                "error_count": item.error_count,
                "recent_429": item.recent_429,
                "last_429_at": item.last_429_at,
                "last_ok_at": item.last_ok_at,
                "blocked_until": item.blocked_until,
                "state": item.state,
                "latency_ms": item.latency_ms,
                "server_health": item.server_health,
            }
            for item in self._items.values()
        ]

    def restore_state(self, data: Any) -> None:
        if not isinstance(data, list):
            return
        for raw in data:
            if not isinstance(raw, dict):
                continue
            try:
                model = str(raw.get("model") or "").strip().lower()
                sid = int(raw.get("station", 0) or 0)
                ip = str(raw.get("ip") or "unknown")
                key = (model, ip)
                item = IpQuotaLease(
                    station=sid,
                    model=model,
                    ip=ip,
                    quota_estimate=max(1, int(raw.get("quota_estimate", self.profile.profile_for(model)))),
                )
                item.used = max(0, int(raw.get("used", 0) or 0))
                item.success_count = max(0, int(raw.get("success_count", 0) or 0))
                item.error_count = max(0, int(raw.get("error_count", 0) or 0))
                item.recent_429 = max(0, int(raw.get("recent_429", 0) or 0))
                item.last_429_at = max(0.0, float(raw.get("last_429_at", 0.0) or 0.0))
                item.last_ok_at = max(0.0, float(raw.get("last_ok_at", 0.0) or 0.0))
                item.blocked_until = max(0.0, float(raw.get("blocked_until", 0.0) or 0.0))
                state = str(raw.get("state") or QUALIFIED)
                item.state = state if state in _SELECTABLE | _NON_SELECTABLE else QUALIFIED
                latency = raw.get("latency_ms")
                item.latency_ms = float(latency) if latency is not None else None
                item.server_health = max(0.0, min(1.0, float(raw.get("server_health", 1.0) or 1.0)))
                self._items[key] = item
            except (TypeError, ValueError):
                continue
            while len(self._items) > self._max_entries:
                self._items.pop(next(iter(self._items)))

    def leases_for_model(self, model: str | None) -> list[IpQuotaLease]:
        mid = str(model or "").strip().lower()
        return [item for item in self._items.values() if item.model == mid]

    def prune(self, keep: set[tuple[str, str]] | None = None) -> None:
        if keep is None:
            return
        for key in list(self._items):
            if key not in keep:
                self._items.pop(key, None)

    def prune_station(self, station_id: int) -> None:
        sid = int(station_id)
        for key, item in list(self._items.items()):
            if item.station == sid:
                self._items.pop(key, None)


__all__ = [
    "ACTIVE",
    "BAD",
    "DRAINING",
    "EXHAUSTED",
    "QUALIFIED",
    "WARMING",
    "IpQuotaLease",
    "IpQuotaRegistry",
    "QuotaProfile",
]
