"""P7 : rapport capacité — décision keep/scale déterministe."""

from __future__ import annotations

from scripts.capacity_report import build_capacity_report


def test_capacity_report_keeps_six_when_utilization_low():
    report = build_capacity_report(
        active_stations=6,
        spare_station=7,
        warm_spare_ready=True,
        quota_per_ip=150,
        requests=10,
        window_s=60,
        headroom=800,
        model="deepseek-v4.1-flash-free",
    )
    assert report["capacity_hour"] == 900
    assert report["recommendation"] == "keep"
    assert report["utilization_pct"] < 70
    assert report["warm_spare_ready"] is True


def test_capacity_report_scales_above_seventy_percent():
    report = build_capacity_report(
        active_stations=6,
        spare_station=7,
        warm_spare_ready=True,
        quota_per_ip=150,
        requests=200,
        window_s=60,
        headroom=100,
        model="m",
    )
    # 200 req/min = 12 000 req/h sur 900 de capacité => > 70 %.
    assert report["recommendation"] == "scale"
    assert report["utilization_pct"] > 70
    assert "8–9" in report["target"]


def test_capacity_report_handles_zero_rate():
    report = build_capacity_report(
        active_stations=6,
        spare_station=0,
        warm_spare_ready=False,
        quota_per_ip=150,
        requests=0,
        window_s=10,
        headroom=900,
        model="m",
    )
    assert report["requests_per_hour"] == 0
    assert report["recommendation"] == "keep"
