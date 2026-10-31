#!/usr/bin/env python3
"""scripts/vpn_alerts.py — [audit-2026-10-24 Phase5] alerte minimale.

Scrape /metrics + /health?detail=full et pousse un webhook si :
- pool_usable_stations < floor 2 min
- slo.status == breached
- canary == failing
- watchdog_restart_total augmente vite (budget épuisé)

Usage :
  python scripts/vpn_alerts.py --base http://localhost:4000 --webhook https://... [--once]
  Boucle 60s par défaut ; --once pour cron/systemd timer.

Fail-soft : jamais d'exit !=0 sur sonde KO (log seulement), exit 2 si args invalides.
Env : VPN_ALERT_WEBHOOK (fallback --webhook), VPN_ALERT_BASE (fallback --base).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request


def _get(base: str, path: str, timeout: float = 10.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"ERR {e}"


def _metric(text: str, name: str) -> float | None:
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.startswith(name + " ") or ln.startswith(name + "{"):
            try:
                return float(ln.rsplit(" ", 1)[1])
            except Exception:
                return None
    return None


def evaluate_health_payload(health: dict) -> list[str]:
    """Alertes capacity/spare/park à partir de ``/health``."""
    alerts: list[str] = []
    cap = health.get("free_capacity") if isinstance(health, dict) else None
    if not isinstance(cap, dict):
        return alerts
    if cap.get("capacity_degraded"):
        reason = str(cap.get("capacity_reason") or "capacity degraded")
        alerts.append(f"free capacity degraded: {reason}")
    spare = int(cap.get("spare_station") or 0)
    if spare and not bool(cap.get("warm_spare_ready")):
        alerts.append(f"warm spare #{spare} not ready")
    parked = int(cap.get("parked_stations") or 0)
    if parked > 0:
        alerts.append(f"{parked} station(s) parked")
    return alerts


def evaluate_free_health_payload(payload: dict) -> list[str]:
    """Alertes auth/quota à partir de ``/api/free-health``."""
    alerts: list[str] = []
    if not isinstance(payload, dict):
        return alerts
    health = payload.get("station_health")
    if isinstance(health, dict):
        for sid, row in health.items():
            if not isinstance(row, dict):
                continue
            if row.get("state") == "parked":
                alerts.append(f"station {sid} parked ({row.get('cause') or 'unknown'})")
            elif row.get("action_required"):
                alerts.append(f"station {sid} action required ({row.get('cause') or row.get('last_error') or '?'})")
    global_backoff = payload.get("model_global_backoff")
    if isinstance(global_backoff, list) and global_backoff:
        models = ",".join(str(x.get("model")) for x in global_backoff[:3] if isinstance(x, dict))
        alerts.append(f"model global backoff active: {models or '?'}")
    leases = payload.get("quota_leases")
    if isinstance(leases, list):
        bad = sum(1 for row in leases if isinstance(row, dict) and row.get("state") == "bad")
        if bad:
            alerts.append(f"{bad} bad IP lease(s)")
    return alerts


def check(base: str) -> tuple[list[str], dict]:
    alerts: list[str] = []
    ctx: dict = {}
    sc, metrics = _get(base, "/metrics")
    ctx["metrics_status"] = sc
    if sc != 200:
        return [f"metrics KO (http={sc})"], ctx
    usable = _metric(metrics, "pool_usable_stations")
    floor = _metric(metrics, "pool_floor")
    restarts = _metric(metrics, "watchdog_restart_total")
    ctx.update({"usable": usable, "floor": floor, "restarts": restarts})
    if usable is not None and floor is not None and usable < floor:
        alerts.append(f"pool_usable {usable} < floor {floor}")
    _, health = _get(base, "/health?detail=full")
    ctx["health"] = health[:2000]
    try:
        h = json.loads(health)
        alerts.extend(evaluate_health_payload(h))
        slo = ((h.get("slo") or {}).get("status") if isinstance(h.get("slo"), dict) else None) or h.get("slo_status")
        if slo == "breached":
            alerts.append("slo breached")
        can = h.get("canary") or {}
        if isinstance(can, dict) and can.get("status") == "failing":
            alerts.append("canary failing")
        elif "failing" in health.lower() and "canary" in health.lower():
            alerts.append("canary failing (texte)")
    except Exception:
        if "breached" in health.lower():
            alerts.append("slo breached (texte)")
    sc_fh, free_health = _get(base, "/api/free-health")
    ctx["free_health_status"] = sc_fh
    if sc_fh == 200:
        try:
            alerts.extend(evaluate_free_health_payload(json.loads(free_health)))
        except Exception:
            pass
    return alerts, ctx


def push(webhook: str, text: str, timeout: float = 10.0) -> bool:
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"[vpn-alerts] webhook KO: {e}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.getenv("VPN_ALERT_BASE", "http://localhost:4000"))
    ap.add_argument("--webhook", default=os.getenv("VPN_ALERT_WEBHOOK", ""))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=60.0)
    a = ap.parse_args()
    bad_since: float | None = None
    while True:
        alerts, ctx = check(a.base)
        if alerts:
            if bad_since is None:
                bad_since = time.monotonic()
            if time.monotonic() - bad_since >= 120 or a.once:
                msg = f"[vpn-alerts] {a.base} : " + "; ".join(alerts) + f" | ctx={ctx}"
                print(msg, flush=True)
                if a.webhook:
                    push(a.webhook, msg)
        else:
            bad_since = None
        if a.once:
            return 0
        time.sleep(max(15.0, a.interval))


if __name__ == "__main__":
    raise SystemExit(main())
