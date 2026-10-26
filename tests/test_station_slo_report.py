"""Tests du rapport SLO (scripts/station_slo_report.py)."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _row(
    at,
    *,
    healthy,
    routable,
    ms,
    age,
    stale=False,
    boot=None,
    refresh=None,
    stations=None,
    quality=None,
    metrics=None,
):
    return {
        "at": at,
        "ms": ms,
        "status": "connected",
        "healthy": healthy,
        "total": 6,
        "routable": routable,
        "boot_error": boot,
        "stale": stale,
        "status_age_s": age,
        "refresh_error": refresh,
        "quality_enabled": True,
        "quality": quality or {},
        "metrics": metrics,
        "stations": stations or [],
    }


def _st(sid, status="connected", ip="1.2.3.4", flap=1, switches=10, secondary=False):
    return {
        "station": sid,
        "status": status,
        "ip": ip,
        "flap_1h": flap,
        "secondary": secondary,
        "total_switches": switches,
        "reason": None,
    }


def _quality(sid, *, probe, ewma, p95, success, errors, r429=0, score=0.5):
    return {
        "enabled": True,
        "stations": {
            str(sid): {
                "probe_ms": probe,
                "ewma_ms": ewma,
                "p95_ms": p95,
                "success": success,
                "errors": errors,
                "recent_429": r429,
                "score": score,
            }
        },
    }


def _write(tmp_path, rows):
    p = tmp_path / "slo.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return p


def test_build_report_metrics_and_filters(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    sys.modules.pop("station_slo_report", None)
    from station_slo_report import build_report

    base = [_st(i) for i in range(1, 7)]
    rows = [
        _row(
            "2026-10-26T12:00:00Z",
            healthy=6,
            routable=6,
            ms=100,
            age=10,
            stations=base,
            quality=_quality(1, probe=100, ewma=200, p95=300, success=10, errors=0, score=0.9),
            metrics={
                "rotations": {"soft": 1},
                "fallback": {"free_to_paid:quota_429": 1},
                "free_status": {"leg_status:free:200": 10},
            },
        ),
        _row(
            "2026-10-26T12:01:00Z",
            healthy=5,
            routable=5,
            ms=300,
            age=130,
            stale=True,
            refresh="timeout",
            stations=[*base[:5], _st(6, status="degraded", flap=3, secondary=True)],
            quality=_quality(2, probe=500, ewma=600, p95=700, success=5, errors=5, r429=2, score=0.5),
            metrics={
                "rotations": {"soft": 3},
                "fallback": {"free_to_paid:quota_429": 3},
                "free_status": {"leg_status:free:200": 15},
            },
        ),
        _row(
            "2026-10-26T12:02:00Z",
            healthy=6,
            routable=6,
            ms=50,
            age=20,
            boot="boot 5/6",
            stations=[_st(1, switches=12), *base[1:5], _st(6, switches=11)],
        ),
        {"at": "2026-10-26T12:03:00Z", "error": "boom"},
    ]
    p = _write(tmp_path, rows)

    out = build_report(p)
    assert "Échantillons valides : 3 / erreurs : 1" in out
    assert "Flotte complète (healthy+routable) : 2/3 = 66.7 %" in out
    assert "Routable 6/6 : 2/3 = 66.7 %" in out
    assert "`boot_error` : 1/3 = 33.3 %" in out
    assert "`stale` : 1/3 = 33.3 %" in out
    assert "`refresh_error` : 1/3 = 33.3 %" in out
    assert "p95 300 ms" in out
    assert "| 6 | 100.0 % | 100.0 % | 33.3 % | 0.0 % | 33.3 % | 3 |" in out
    assert "| 1 | 100.0 % | 100.0 % | 0.0 % | 0.0 % | 0.0 % | 1 | 2 |" in out

    # Qualité / upstream
    assert "Probe TTFB (station-échantillon) : p50 100 ms · p95 500 ms" in out
    assert "Taux de succès (snapshots) : moyenne 75.0 % · min 50.0 %" in out
    assert "Rotations (fenêtre) : soft=2" in out
    assert "Fallback payant (fenêtre) : total 2" in out
    assert "leg_status:free:200=+5" in out
    assert "| 1 | 100 ms | 100 ms | 200 ms | 200 ms | 100.0 % | 0 | 0.900 |" in out
    assert "| 2 | 500 ms | 500 ms | 600 ms | 600 ms | 50.0 % | 2 | 0.500 |" in out

    window = build_report(p, since="2026-10-26T12:01:00Z", until="2026-10-26T12:02:00Z")
    assert "Fenêtre : `2026-10-26T12:01:00Z` → `2026-10-26T12:02:00Z`" in window
    assert "Flotte complète (healthy+routable) : 1/2 = 50.0 %" in window
