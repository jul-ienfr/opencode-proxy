"""Pure payload parsers for the supported catalog formats.

They perform no I/O and no policy: the adapter decides *which* bytes to read,
the parser only turns them into :class:`Server` rows.  Keeping them here (and
not inside the SQLite registry) lets every source share the same validation
and keeps the resolver testable without network or disk.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..domain import Server


def parse_official_payload(raw: Any) -> list[Server]:
    """Parse ``GET /v1/servers`` (online servers only)."""

    if not isinstance(raw, list):
        return []
    servers: list[Server] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") != "online":
            continue
        hostname = str(item.get("hostname") or "").strip()
        locations = item.get("locations") or []
        country = ""
        city = None
        if isinstance(locations, list) and locations:
            loc = locations[0] if isinstance(locations[0], dict) else {}
            country_obj = loc.get("country") or {}
            country = str((country_obj or {}).get("name") or "").strip()
            city_obj = loc.get("city") or {}
            city = str((city_obj or {}).get("name") or "").strip() or None
        if not hostname or not country:
            continue
        technologies = tuple(
            str((tech or {}).get("name") or "").strip()
            for tech in (item.get("technologies") or [])
            if isinstance(tech, dict) and str((tech or {}).get("name") or "").strip()
        )
        try:
            load = float(item.get("load") or 0.0)
        except (TypeError, ValueError):
            load = 0.0
        servers.append(
            Server(
                hostname=hostname,
                country=country,
                server_ip=str(item.get("station") or "").strip() or None,
                city=city,
                load=max(0.0, load),
                technologies=technologies,
            )
        )
    return servers


def parse_gluetun_payload(raw: Any) -> list[Server]:
    """Parse gluetun's ``vpn``/``ips``/``wgpubkey`` catalog format."""

    if isinstance(raw, dict):
        raw = raw.get("servers") or []
    if not isinstance(raw, list):
        return []
    servers: list[Server] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("vpn") or "").strip().lower() != "wireguard":
            continue
        hostname = str(item.get("hostname") or "").strip()
        country = str(item.get("country") or "").strip()
        if not hostname or not country:
            continue
        ips = item.get("ips") or []
        server_ip = None
        if isinstance(ips, list) and ips:
            server_ip = str(ips[0] or "").strip() or None
        city = str(item.get("city") or "").strip() or None
        servers.append(
            Server(
                hostname=hostname,
                country=country,
                server_ip=server_ip,
                city=city,
                load=0.0,
                technologies=("WireGuard",),
            )
        )
    return servers


def stamp_servers(
    servers: list[Server] | tuple[Server, ...],
    *,
    source_id: str,
    stale: bool,
    fetched_at: float | None,
) -> tuple[Server, ...]:
    """Attach provenance to every row from one snapshot."""

    return tuple(
        replace(
            server,
            source_id=source_id,
            stale=bool(stale),
            catalog_fetched_at=fetched_at,
        )
        for server in servers
    )
