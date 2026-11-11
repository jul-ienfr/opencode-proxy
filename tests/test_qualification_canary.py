"""Canary evaluator tests (no live stations required)."""

from __future__ import annotations

from free.qualification.canary import evaluate_canary


def _payloads(*, usable=6, stale=False, transports=None, committed=True):
    return {
        "summary": {"enabled": True, "total": 7070},
        "stations": {
            "items": [
                {
                    "station": station,
                    "state": "serving",
                    "client_usable": station <= usable,
                    "reason": "" if station <= usable else "dead",
                }
                for station in range(1, 7)
            ]
        },
        "slo": {
            "six_of_six": usable == 6,
            "stations_usable": usable,
            "stations_total": 6,
            "fleet_ratio_pct": usable / 6 * 100,
            "availability_1h_pct": 99.5,
            "target_pct": 99.0,
        },
        "watchdog": {
            "heartbeat": {"status": "ok"},
            "incidents": [],
        },
        "discovery": {
            "items": [
                {
                    "status": "stale" if stale else "ok",
                    "stale": stale,
                    "server_count": 7070,
                }
            ]
        },
        "transports": {
            "items": transports
            if transports is not None
            else [
                {
                    "transport": name,
                    "attempts": 100,
                    "successes": 95,
                    "degraded": False,
                    "health": "healthy",
                }
                for name in ("wireguard", "openvpn-udp", "openvpn-tcp")
            ]
        },
        "rotations": {
            "items": [
                {"result": "committed" if committed else "failed"}
            ]
        },
    }


def _check(report, name):
    return next(item for item in report["checks"] if item["name"] == name)


def test_canary_passes_with_full_evidence():
    report = evaluate_canary(_payloads())
    assert report["passed"] is True
    assert all(item["ok"] for item in report["checks"])
    assert report["evidence"]["stations_usable"] == 6
    assert report["evidence"]["committed_cutovers"] == 1


def test_canary_fails_with_five_usable_stations():
    report = evaluate_canary(_payloads(usable=5))
    assert report["passed"] is False
    assert _check(report, "6-stations-usable")["ok"] is False
    assert _check(report, "slo-six-of-six")["ok"] is False


def test_canary_fails_on_stale_discovery():
    report = evaluate_canary(_payloads(stale=True))
    assert report["passed"] is False
    assert _check(report, "discovery-fresh")["ok"] is False


def test_canary_fails_on_missing_or_degraded_transport():
    missing = _payloads(
        transports=[
            {
                "transport": "wireguard",
                "attempts": 100,
                "successes": 99,
                "degraded": False,
                "health": "healthy",
            }
        ]
    )
    report = evaluate_canary(missing)
    assert report["passed"] is False
    assert _check(report, "transports-exercised")["ok"] is False

    degraded = _payloads()
    degraded["transports"]["items"][1]["degraded"] = True
    report = evaluate_canary(degraded)
    assert report["passed"] is False
    assert _check(report, "transports-exercised")["ok"] is False


def test_canary_fails_without_committed_cutover():
    report = evaluate_canary(_payloads(committed=False))
    assert report["passed"] is False
    assert _check(report, "cutover-committed")["ok"] is False


def test_canary_fails_on_fetch_errors_and_missing_heartbeat():
    payloads = _payloads()
    payloads["watchdog"]["heartbeat"] = {"status": "disabled"}
    report = evaluate_canary(
        payloads,
        fetch_errors=["stations:URLError:down"],
    )
    assert report["passed"] is False
    assert _check(report, "fetch:stations:URLError:down")["ok"] is False
    assert _check(report, "watchdog-heartbeat")["ok"] is False
