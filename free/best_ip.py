"""Sélecteur de "meilleure IP" — filtres durs puis score lexicographique.

Le quota restant pèse avant la latence : une IP rapide mais presque épuisée
ne doit pas gagner contre une IP un peu plus lente qui peut encore servir.
Le sélecteur ne fait aucune I/O et ne connaît ni Docker ni HTTP ; le pool lui
fournit des stations déjà lues et des callbacks pour la charge/latence.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from free.ip_quota import DRAINING, IpQuotaLease, IpQuotaRegistry
from free.station_health import StationHealthRegistry, station_contract


@dataclass
class Selection:
    station: Any
    model: str
    lease: IpQuotaLease
    score: tuple
    reason: str

    def snapshot(self) -> dict[str, Any]:
        return {
            "station": int(getattr(self.station, "_station", -1)),
            "model": self.model,
            "ip": self.lease.ip,
            "score": list(self.score),
            "headroom": self.lease.headroom,
            "quota_estimate": self.lease.quota_estimate,
            "success_rate": round(self.lease.success_rate, 4),
            "state": self.lease.state,
            "latency_ms": self.lease.latency_ms,
            "inflight": self.lease.inflight,
            "reason": self.reason,
        }


@dataclass
class BestIpSelector:
    quota: IpQuotaRegistry
    health: StationHealthRegistry | None = None
    inflight_provider: Callable[[Any], int] | None = None
    latency_provider: Callable[[Any], float | None] | None = None
    max_effective_age_s: float = 60.0
    last_choice: Selection | None = None
    last_rejections: list[dict[str, Any]] = field(default_factory=list)

    def _inflight(self, station: Any) -> int:
        if self.inflight_provider is not None:
            try:
                return max(0, int(self.inflight_provider(station)))
            except Exception:
                return 0
        try:
            return max(0, int(getattr(station, "_inflight", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _latency(self, station: Any) -> float | None:
        if self.latency_provider is not None:
            try:
                value = self.latency_provider(station)
                return float(value) if value is not None else None
            except Exception:
                return None
        try:
            value = getattr(station, "_latency_ms", None)
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def evaluate(self, station: Any, model: str, *, now: float | None = None) -> Selection | None:
        ts = float(now if now is not None else time.monotonic())
        health = self.health.get(int(getattr(station, "_station", -1))) if self.health is not None else None
        ok, reason = station_contract(
            station,
            health=health,
            now=ts,
            max_effective_age_s=self.max_effective_age_s,
        )
        if not ok:
            self.last_rejections.append(
                {"station": int(getattr(station, "_station", -1)), "reason": reason, "stage": "station"}
            )
            return None
        lease = self.quota.lease_for(model, station, now=ts)
        if lease is None:
            self.last_rejections.append(
                {"station": int(getattr(station, "_station", -1)), "reason": "no-lease", "stage": "quota"}
            )
            return None
        lease.inflight = self._inflight(station)
        latency = self._latency(station)
        if latency is not None:
            lease.latency_ms = latency
        usable, quota_reason = self.quota.model_usable(model, station, now=ts)
        if not usable:
            self.last_rejections.append(
                {
                    "station": int(getattr(station, "_station", -1)),
                    "reason": quota_reason,
                    "stage": "quota",
                    "state": lease.state,
                    "headroom": lease.headroom,
                }
            )
            return None
        return Selection(
            station=station,
            model=model,
            lease=lease,
            score=lease.score_key(self.quota.profile),
            reason="selected",
        )

    def rank(self, stations: list[Any], model: str, *, now: float | None = None) -> list[Selection]:
        self.last_rejections = []
        ts = float(now if now is not None else time.monotonic())
        rows: list[Selection] = []
        for station in stations:
            try:
                row = self.evaluate(station, model, now=ts)
            except Exception as exc:
                self.last_rejections.append(
                    {
                        "station": int(getattr(station, "_station", -1)),
                        "reason": f"selector-error:{type(exc).__name__}",
                        "stage": "selector",
                    }
                )
                row = None
            if row is not None:
                rows.append(row)
        rows.sort(key=lambda item: item.score, reverse=True)
        # Une même IP ne doit servir qu'une seule station : le quota est une
        # propriété de l'IP. On garde la meilleure station pour chaque IP et on
        # expose les autres comme rejetées (elles seront drainées/rotées).
        unique: list[Selection] = []
        seen: set[str] = set()
        for row in rows:
            ip = str(row.lease.ip or "")
            if ip and ip != "unknown":
                if ip in seen:
                    self.last_rejections.append(
                        {
                            "station": int(getattr(row.station, "_station", -1)),
                            "reason": "duplicate-ip",
                            "stage": "ip",
                            "ip": ip,
                        }
                    )
                    continue
                seen.add(ip)
            unique.append(row)
        return unique

    def choose(self, stations: list[Any], model: str, *, now: float | None = None) -> Any | None:
        ranked = self.rank(stations, model, now=now)
        if not ranked:
            self.last_choice = None
            return None
        self.last_choice = ranked[0]
        return ranked[0].station

    def should_drain(self, station: Any, model: str, *, now: float | None = None) -> bool:
        lease = self.quota.lease_for(model, station, now=now)
        if lease is None:
            return False
        lease.refresh(self.quota.profile, now)
        return lease.state == DRAINING or lease.headroom <= self.quota.profile.reserve_for(model)

    def snapshot(self) -> dict[str, Any]:
        return {
            "last_choice": self.last_choice.snapshot() if self.last_choice is not None else None,
            "last_rejections": list(self.last_rejections[-20:]),
        }


__all__ = ["BestIpSelector", "Selection"]
