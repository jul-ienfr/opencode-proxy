#!/usr/bin/env python3
"""Échantillonne /api/vpn-status (+ /metrics) et écrit un JSONL SLO.

Chaque ligne contient l'état flotte, la qualité par station (TTFB/probe,
succès, 429) et les compteurs Prometheus utiles (rotations, fallback payant,
statuts free). Le rapport `station_slo_report.py` agrège le tout.

Usage :
    python scripts/station_slo_watch.py --duration 3600
    python scripts/station_slo_watch.py --duration 0   # infini
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_QUALITY_KEYS = (
    "score",
    "ewma_ms",
    "p95_ms",
    "probe_ms",
    "probe_ok",
    "probe_age_s",
    "recent_429",
    "success",
    "errors",
    "count",
)


def _get_text(url: str, timeout: float = 10.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _get_json(url: str, timeout: float = 10.0) -> dict:
    return json.loads(_get_text(url, timeout=timeout))


def _metric_value(line: str) -> float:
    try:
        return float(line.rsplit(" ", 1)[1])
    except Exception:
        return 0.0


def _parse_metrics(text: str) -> dict:
    """Extrait les compteurs utiles, sans dépendance Prometheus."""
    out: dict = {
        "rotations": {},
        "fallback": {},
        "free_status": {},
        "flap": {},
    }
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        try:
            if line.startswith("vpn_rotations_total"):
                m = re.search(r'kind="([^"]+)"', line)
                if m:
                    out["rotations"][m.group(1)] = _metric_value(line)
            elif line.startswith("proxy_fallback_total"):
                leg = re.search(r'leg="([^"]+)"', line)
                cause = re.search(r'cause="([^"]+)"', line)
                if leg and cause:
                    out["fallback"][f"{leg.group(1)}:{cause.group(1)}"] = _metric_value(line)
            elif line.startswith("proxy_misc_total"):
                name = re.search(r'name="([^"]+)"', line)
                if name and name.group(1).startswith("leg_status:"):
                    out["free_status"][name.group(1)] = _metric_value(line)
            elif line.startswith("vpn_station_flap_1h"):
                sid = re.search(r'station="([^"]+)"', line)
                if sid:
                    out["flap"][sid.group(1)] = _metric_value(line)
        except Exception:
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:4000")
    ap.add_argument("--duration", type=float, default=86400.0, help="0 = infini")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--out", default="logs/station_slo.jsonl")
    ap.add_argument("--no-metrics", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    base = args.api.rstrip("/")
    started = time.monotonic()
    with out.open("a", encoding="utf-8") as f:
        while True:
            _t0 = time.monotonic()
            try:
                s = _get_json(f"{base}/api/vpn-status")
                _ms = round((time.monotonic() - _t0) * 1000.0, 1)
                q = s.get("quality") or {}
                quality = {
                    "enabled": bool(q.get("enabled")),
                    "stations": {
                        str(sid): {k: v.get(k) for k in _QUALITY_KEYS}
                        for sid, v in (q.get("stations") or {}).items()
                        if isinstance(v, dict)
                    },
                }
                metrics = None
                if not args.no_metrics:
                    try:
                        metrics = _parse_metrics(_get_text(f"{base}/metrics", timeout=5.0))
                    except Exception as e:
                        metrics = {"error": f"{type(e).__name__}: {e}"}
                row = {
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "ms": _ms,
                    "status": s.get("status"),
                    "healthy": s.get("healthy"),
                    "total": s.get("total"),
                    "routable": s.get("routable"),
                    "boot_error": s.get("boot_error"),
                    "stale": bool(s.get("stale")),
                    "status_age_s": s.get("status_age_s"),
                    "refresh_error": s.get("refresh_error"),
                    "quality_enabled": bool(q.get("enabled")),
                    "quality": quality,
                    "metrics": metrics,
                    "stations": [
                        {
                            "station": st.get("station"),
                            "status": st.get("vpn_status"),
                            "ip": st.get("current_ip"),
                            "flap_1h": (st.get("vpn") or {}).get("flap_count_1h"),
                            "secondary": (st.get("vpn") or {}).get("secondary_path_issue"),
                            "auth_failed": (st.get("vpn") or {}).get("auth_failed"),
                            "total_switches": (st.get("vpn") or {}).get("total_switches"),
                            "reason": st.get("non_routable_reason"),
                        }
                        for st in (s.get("stations") or [])
                    ],
                }
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
            except Exception as e:
                f.write(
                    json.dumps(
                        {
                            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "ms": round((time.monotonic() - _t0) * 1000.0, 1),
                            "error": f"{type(e).__name__}: {e}",
                        }
                    )
                    + "\n"
                )
                f.flush()
            if args.duration and (time.monotonic() - started) >= args.duration:
                return 0
            time.sleep(max(5.0, float(args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())
