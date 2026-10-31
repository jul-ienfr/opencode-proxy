#!/usr/bin/env python3
"""Rapport capacité P7 — faut-il passer à 8–9 stations actives ?

Le rapport estime le débit free observé, la capacité horaire du parc et le
taux d'utilisation. Il ne démarre aucune station et ne modifie rien.

Usage :
  python scripts/capacity_report.py --base http://127.0.0.1:4000 --window 10
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from typing import Any


def _get(base: str, path: str, timeout: float = 10.0) -> dict:
    try:
        with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return {}


def build_capacity_report(
    *,
    active_stations: int,
    spare_station: int,
    warm_spare_ready: bool,
    quota_per_ip: int,
    requests: int,
    window_s: float,
    headroom: int,
    model: str,
) -> dict[str, Any]:
    """Calcule le taux d'utilisation et la recommandation de capacité."""
    active = max(1, int(active_stations))
    quota = max(1, int(quota_per_ip))
    window = max(1.0, float(window_s))
    rate = max(0.0, float(requests)) / window
    hourly = rate * 3600.0
    capacity_hour = active * quota
    utilization = hourly / capacity_hour if capacity_hour else 0.0
    recommended = "scale" if utilization >= 0.70 else "keep"
    return {
        "model": model,
        "active_stations": active,
        "spare_station": int(spare_station or 0),
        "warm_spare_ready": bool(warm_spare_ready),
        "quota_per_ip": quota,
        "capacity_hour": capacity_hour,
        "headroom": max(0, int(headroom)),
        "requests": max(0, int(requests)),
        "window_s": window,
        "requests_per_s": round(rate, 4),
        "requests_per_hour": round(hourly, 1),
        "utilization_pct": round(utilization * 100.0, 2),
        "recommendation": recommended,
        "target": "8–9 actives + 1 spare" if recommended == "scale" else "6 actives + 1 spare",
        "reason": (
            "utilisation >= 70 % : envisager 8–9 stations"
            if recommended == "scale"
            else "utilisation < 70 % : capacité actuelle suffisante"
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.getenv("VPN_ALERT_BASE", "http://localhost:4000"))
    ap.add_argument("--model", default="deepseek-v4.1-flash-free")
    ap.add_argument("--window", type=float, default=10.0)
    a = ap.parse_args()
    first = _get(a.base, "/api/pool-status")
    if not first:
        print(json.dumps({"error": "pool-status indisponible", "base": a.base}))
        return 1
    t0 = time.monotonic()
    r0 = int(first.get("total_free_requests") or 0)
    time.sleep(max(1.0, a.window))
    second = _get(a.base, "/api/pool-status") or first
    window = time.monotonic() - t0
    r1 = int(second.get("total_free_requests") or 0)
    model = str(a.model)
    leases = second.get("quota_leases") or []
    active_rows = [
        row
        for row in (second.get("stations") or [])
        if isinstance(row, dict)
        and not (second.get("spare_station") and row.get("station") == second.get("spare_station"))
    ]
    current = {
        (int(row.get("station") or 0), str(row.get("current_ip") or ""))
        for row in active_rows
    }
    current_leases = [
        row
        for row in leases
        if isinstance(row, dict)
        and str(row.get("model") or "").lower() == model.lower()
        and (int(row.get("station") or 0), str(row.get("ip") or "")) in current
    ]
    headroom = sum(int(row.get("headroom") or 0) for row in current_leases)
    # Profil du modèle : priorité au lease réel, sinon le statut du pool.
    quota_per_ip = next(
        (
            int(row.get("quota_estimate") or 0)
            for row in current_leases
            if row.get("quota_estimate")
        ),
        150,
    )
    report = build_capacity_report(
        active_stations=int(second.get("configured_stations") or 0),
        spare_station=int(second.get("spare_station") or 0),
        warm_spare_ready=bool(second.get("warm_spare_ready")),
        quota_per_ip=quota_per_ip,
        requests=max(0, r1 - r0),
        window_s=window,
        headroom=headroom,
        model=model,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
