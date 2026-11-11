"""Canary checks for the six-station free qualification release.

The evaluator is pure: the script fetches API payloads, this module applies the
release contract.  A failing check names the missing evidence instead of
guessing from a single aggregate.
"""

from __future__ import annotations

from typing import Any


def _check(checks: list[dict[str, Any]], name: str, ok: bool, detail: Any = "") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def evaluate_canary(
    payloads: dict[str, Any],
    *,
    target: int = 6,
    fetch_errors: list[str] | None = None,
) -> dict[str, Any]:
    target = max(1, int(target))
    checks: list[dict[str, Any]] = []
    for error in fetch_errors or []:
        _check(checks, f"fetch:{error}", False, error)

    summary = payloads.get("summary") or {}
    stations = list((payloads.get("stations") or {}).get("items") or [])
    slo = payloads.get("slo") or {}
    watchdog = payloads.get("watchdog") or {}
    discovery_items = list(
        (payloads.get("discovery") or {}).get("items") or []
    )
    transports = list(
        (payloads.get("transports") or {}).get("items") or []
    )
    rotations = list((payloads.get("rotations") or {}).get("items") or [])

    usable = [item for item in stations if item.get("client_usable")]
    _check(
        checks,
        "summary-enabled",
        bool(summary.get("enabled")) and int(summary.get("total") or 0) > 0,
        {
            "enabled": bool(summary.get("enabled")),
            "total": int(summary.get("total") or 0),
        },
    )
    _check(
        checks,
        "six-stations-present",
        len(stations) >= target,
        {"stations": len(stations), "target": target},
    )
    _check(
        checks,
        f"{target}-stations-usable",
        len(usable) == target,
        {
            "usable": len(usable),
            "stations": [
                {
                    "station": item.get("station"),
                    "state": item.get("state"),
                    "reason": item.get("reason"),
                }
                for item in stations
                if not item.get("client_usable")
            ],
        },
    )
    _check(
        checks,
        "slo-six-of-six",
        bool(slo.get("six_of_six")),
        {
            "stations_usable": slo.get("stations_usable"),
            "stations_total": slo.get("stations_total"),
            "fleet_ratio_pct": slo.get("fleet_ratio_pct"),
        },
    )
    target_pct = float(slo.get("target_pct") or 99.0)
    _check(
        checks,
        "slo-availability",
        float(slo.get("availability_1h_pct") or 0.0) >= target_pct,
        {
            "availability_1h_pct": slo.get("availability_1h_pct"),
            "target_pct": target_pct,
        },
    )

    incidents = list(watchdog.get("incidents") or [])
    heartbeat = watchdog.get("heartbeat") or {}
    heartbeat_status = str(heartbeat.get("status") or "")
    _check(
        checks,
        "watchdog-heartbeat",
        bool(heartbeat) and heartbeat_status in ("ok", "running"),
        {"status": heartbeat_status or "missing"},
    )
    _check(
        checks,
        "watchdog-no-open-incidents",
        not incidents,
        {"open_incidents": len(incidents)},
    )

    latest_discovery = discovery_items[0] if discovery_items else {}
    _check(
        checks,
        "discovery-fresh",
        bool(latest_discovery)
        and not bool(latest_discovery.get("stale"))
        and str(latest_discovery.get("status") or "") != "stale"
        and int(latest_discovery.get("server_count") or 0) > 0,
        {
            "status": latest_discovery.get("status"),
            "stale": latest_discovery.get("stale"),
            "server_count": latest_discovery.get("server_count"),
        },
    )

    by_transport = {str(item.get("transport")): item for item in transports}
    required_transports = ("wireguard", "openvpn-udp", "openvpn-tcp")
    missing_transports = [name for name in required_transports if name not in by_transport]
    unhealthy = [
        name
        for name in required_transports
        if name in by_transport
        and (
            bool(by_transport[name].get("degraded"))
            or int(by_transport[name].get("attempts") or 0) <= 0
            or int(by_transport[name].get("successes") or 0) <= 0
        )
    ]
    _check(
        checks,
        "transports-exercised",
        not missing_transports and not unhealthy,
        {
            "missing": missing_transports,
            "unhealthy": unhealthy,
            "transports": {
                name: {
                    "attempts": item.get("attempts"),
                    "successes": item.get("successes"),
                    "degraded": item.get("degraded"),
                    "health": item.get("health"),
                }
                for name, item in by_transport.items()
            },
        },
    )

    committed = [
        item for item in rotations if str(item.get("result") or "") == "committed"
    ]
    _check(
        checks,
        "cutover-committed",
        bool(committed),
        {"committed": len(committed), "rotations": len(rotations)},
    )

    passed = all(item["ok"] for item in checks)
    return {
        "passed": passed,
        "target": target,
        "checks": checks,
        "evidence": {
            "stations_total": len(stations),
            "stations_usable": len(usable),
            "open_incidents": len(incidents),
            "transports": len(transports),
            "rotations": len(rotations),
            "committed_cutovers": len(committed),
        },
    }


__all__ = ["evaluate_canary"]
