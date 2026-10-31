"""Alertes opérateur P5 : capacité, spare froid, station parkée, quota bad."""

from __future__ import annotations

from scripts.vpn_alerts import evaluate_free_health_payload, evaluate_health_payload


def test_health_capacity_alerts():
    h = {
        "free_capacity": {
            "capacity_degraded": True,
            "capacity_reason": "5/6 stations usable",
            "spare_station": 7,
            "warm_spare_ready": False,
            "parked_stations": 1,
        }
    }
    alerts = evaluate_health_payload(h)
    assert any("capacity degraded" in a for a in alerts)
    assert any("warm spare #7" in a for a in alerts)
    assert any("parked" in a for a in alerts)


def test_health_capacity_is_quiet_when_healthy():
    h = {
        "free_capacity": {
            "capacity_degraded": False,
            "spare_station": 7,
            "warm_spare_ready": True,
            "parked_stations": 0,
        }
    }
    assert evaluate_health_payload(h) == []


def test_free_health_alerts_for_parked_and_bad_lease():
    payload = {
        "station_health": {
            "4": {"state": "parked", "cause": "auth_throttle"},
            "5": {"state": "repairing", "action_required": True, "last_error": "egress"},
        },
        "model_global_backoff": [{"model": "deepseek-v4.1-flash-free"}],
        "quota_leases": [{"state": "bad"}, {"state": "active"}],
    }
    alerts = evaluate_free_health_payload(payload)
    assert any("station 4 parked" in a for a in alerts)
    assert any("station 5 action required" in a for a in alerts)
    assert any("model global backoff" in a for a in alerts)
    assert any("1 bad IP" in a for a in alerts)
