#!/usr/bin/env python3
"""Roll out the 016 control plane one station at a time (6 -> 1).

The rollout never flips all six stations at once.  For every station it
verifies the fork's capabilities, the tunnel state and a fresh egress proof
before moving to the next one; the first failure stops the rollout and leaves
the remaining stations on the previous (legacy) path.

``--dry-run`` contacts no station and prints the plan, which is what CI and the
operator checklist run first.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_ORDER = (6, 5, 4, 3, 2, 1)
MAX_STATIONS = 6


def parse_order(value: str) -> tuple[int, ...]:
    """Parse ``6,5,4`` into a 6→1 station order without duplicates."""

    stations: list[int] = []
    for part in str(value or "").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            station = int(part)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid station {part!r}") from exc
        if station < 1 or station > MAX_STATIONS:
            raise argparse.ArgumentTypeError(f"station {station} outside 1..{MAX_STATIONS}")
        if station not in stations:
            stations.append(station)
    if not stations:
        raise argparse.ArgumentTypeError("at least one station is required")
    return tuple(stations)


def plan_lines(stations: Sequence[int]) -> list[str]:
    order = " -> ".join(str(station) for station in stations)
    return [
        f"rollout plan: {order}",
        "for each station: verify capabilities, tunnel state and fresh egress,",
        "then set CONTROL_PLANE_MODE=active and wait for client_usable.",
        "stop at the first failure; keep the remaining stations on legacy.",
        f"stations in this rollout: {len(stations)} (max {MAX_STATIONS})",
        "dry run: no station contacted, no config written.",
    ]


def usable(state: Any, verification: Any, forbidden: Iterable[str] = ()) -> bool:
    """The 015 ``client_usable`` gate, applied before moving to the next station."""

    status = str(getattr(state, "vpn_status", "") or "").strip().lower()
    if status != "running":
        return False
    if not bool(getattr(verification, "verified", False)):
        return False
    ip = str(getattr(verification, "ip", "") or "").strip()
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return False
    if ip in {str(item).strip() for item in forbidden}:
        return False
    return True


def probe_station(
    client: Any,
    station: int,
    *,
    forbidden: Iterable[str] = (),
) -> tuple[bool, str]:
    try:
        client.capabilities()
        state = client.state()
        verification = client.egress(fresh=True)
    except Exception as exc:  # noqa: BLE001 - rollout must report, not crash
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"
    if not usable(state, verification, forbidden):
        ip = str(getattr(verification, "ip", "") or "")
        transport = str(getattr(state, "transport", "") or "")
        return False, f"client_usable=false ip={ip or '-'} transport={transport or '-'}"
    ip = str(getattr(verification, "ip", "") or "")
    transport = str(getattr(state, "transport", "") or "")
    return True, f"ip={ip} transport={transport or '-'}"


def build_client(station: int) -> Any:
    from free.control_plane.client import GluetunControlClient
    from free.control_plane.config import load_control_plane_config

    settings = load_control_plane_config()
    return GluetunControlClient(
        settings.base_url_for(station),
        station=station,
        api_key_env=settings.api_key_env,
        timeout_s=settings.timeout_s,
        switch_timeout_s=settings.switch_timeout_s,
        rotate_timeout_s=settings.rotate_timeout_s,
        verify_timeout_s=settings.verify_timeout_s,
        retry_max_attempts=settings.retry.max_attempts,
        retry_backoff_s=settings.retry.backoff_s,
        retry_max_backoff_s=settings.retry.max_backoff_s,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stations",
        type=parse_order,
        default=DEFAULT_ORDER,
        help="station order, default 6,5,4,3,2,1",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without contacting any station",
    )
    parser.add_argument(
        "--forbid-ip",
        action="append",
        default=[],
        help="host public IP that must never be accepted as a fresh egress",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        for line in plan_lines(args.stations):
            print(line)
        return 0

    failures = 0
    for station in args.stations:
        client = build_client(station)
        ok, detail = probe_station(client, station, forbidden=args.forbid_ip)
        print(f"station {station}: {'PASS' if ok else 'FAIL'} {detail}")
        if not ok:
            failures += 1
            print("rollout stopped: remaining stations stay on the legacy path")
            break
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
