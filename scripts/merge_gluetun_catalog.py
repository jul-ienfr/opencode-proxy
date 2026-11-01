#!/usr/bin/env python3
"""Merge gluetun's embedded NordVPN catalog with the live API and manifest.

The public NordVPN API omits older servers such as ``al36``–``al75`` (the
87.120.102.x Albanian pool).  Gluetun still embeds them in its binary, but its
on-disk manifest (downloaded from the API) is newer and wins, so
``SERVER_HOSTNAMES=al41.nordvpn.com`` is rejected as "not a valid choice".

The opposite is also true: the embedded catalog predates servers created after
the gluetun build, so replacing the manifest with the embedded copy loses new
servers.  This script builds the union of three sources:

1. gluetun's embedded catalog (old servers, including al36..al75),
2. the container's current manifest (whatever it has today),
3. the live NordVPN API (newest servers and WireGuard public keys).

Later sources win on ``(hostname, vpn)`` duplicates.  The union is written to
``/gluetun/servers/nordvpn.json`` and exported locally for ``NordVpnCatalog``.

Usage:
    python scripts/merge_gluetun_catalog.py --dry-run
    python scripts/merge_gluetun_catalog.py
    python scripts/merge_gluetun_catalog.py --container opencode-vpn
    python scripts/merge_gluetun_catalog.py --no-api
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_API_URL = "https://api.nordvpn.com/v1/servers?limit=0"


def _docker_cp(src: str, dst: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "cp", src, dst],
        capture_output=True,
        text=True,
        timeout=180,
    )


def _read_container_json(container: str, path: str, tmp: str, name: str) -> dict | None:
    dest = str(Path(tmp) / name)
    proc = _docker_cp(f"{container}:{path}", dest)
    if proc.returncode != 0 or not Path(dest).exists():
        return None
    try:
        with open(dest, encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _extract_nordvpn(data: bytes) -> dict:
    markers = (b"al41.nordvpn.com", b"al36.nordvpn.com", b"al109.nordvpn.com")
    for marker in markers:
        idx = data.find(marker)
        if idx < 0:
            continue
        start = data.rfind(b'{\n  "version"', 0, idx)
        while start >= 0:
            text = data[start:].decode("utf-8", "ignore")
            try:
                obj, _end = json.JSONDecoder().raw_decode(text)
            except Exception:
                start = data.rfind(b'{\n  "version"', 0, start)
                continue
            servers = obj.get("servers") if isinstance(obj, dict) else None
            if isinstance(servers, list) and any(
                isinstance(s, dict) and s.get("hostname") == marker.decode()
                for s in servers
            ):
                return obj
            start = data.rfind(b'{\n  "version"', 0, start)
    raise SystemExit("NordVPN catalog containing al36/al41/al109 not found")


def _fetch_api(url: str) -> list[dict]:
    import requests

    response = requests.get(url, timeout=180)
    response.raise_for_status()
    raw = response.json()
    if not isinstance(raw, list):
        raise ValueError("NordVPN API did not return a server list")
    return [item for item in raw if isinstance(item, dict)]


def _api_entry(item: dict, *, vpn: str, pubkey: str | None = None) -> dict | None:
    hostname = str(item.get("hostname") or "").strip()
    station = str(item.get("station") or "").strip()
    locations = item.get("locations") or []
    if not hostname or not station or not isinstance(locations, list) or not locations:
        return None
    loc = locations[0] if isinstance(locations[0], dict) else {}
    country_obj = loc.get("country") or {}
    country = str((country_obj or {}).get("name") or "").strip()
    city_obj = (country_obj or {}).get("city") or {}
    city = str((city_obj or {}).get("name") or "").strip()
    if not country:
        return None
    match = re.search(r"(\d+)$", hostname.split(".", 1)[0])
    number = int(match.group(1)) if match else 0
    base = {
        "vpn": vpn,
        "country": country,
        "city": city,
        "categories": ["Standard VPN servers"],
        "number": number,
        "hostname": hostname,
    }
    if vpn == "wireguard":
        if not pubkey:
            return None
        base["wgpubkey"] = pubkey
        base["ips"] = [station]
        return base
    technologies = {
        str((tech or {}).get("identifier") or "").strip()
        for tech in (item.get("technologies") or [])
        if isinstance(tech, dict)
    }
    base["tcp"] = "openvpn_tcp" in technologies
    base["udp"] = "openvpn_udp" in technologies
    base["ips"] = [station]
    return base


def _api_catalog(raw: list[dict]) -> dict:
    servers: list[dict] = []
    for item in raw:
        if str(item.get("status") or "") != "online":
            continue
        wireguard = next(
            (
                tech
                for tech in (item.get("technologies") or [])
                if isinstance(tech, dict)
                and str(tech.get("identifier") or "") == "wireguard_udp"
            ),
            None,
        )
        if wireguard is not None:
            pubkey = next(
                (
                    str((meta or {}).get("value") or "").strip()
                    for meta in (wireguard.get("metadata") or [])
                    if isinstance(meta, dict)
                    and str(meta.get("name") or "") == "public_key"
                ),
                "",
            )
            entry = _api_entry(item, vpn="wireguard", pubkey=pubkey)
            if entry:
                servers.append(entry)
        technologies = {
            str((tech or {}).get("identifier") or "").strip()
            for tech in (item.get("technologies") or [])
            if isinstance(tech, dict)
        }
        if "openvpn_tcp" in technologies or "openvpn_udp" in technologies:
            entry = _api_entry(item, vpn="openvpn")
            if entry:
                servers.append(entry)
    return {"version": 5, "timestamp": int(time.time()), "servers": servers}


def _merge(catalogs: list[dict]) -> dict:
    merged: dict[tuple[str, str], dict] = {}
    version = 0
    for catalog in catalogs:
        if not isinstance(catalog, dict):
            continue
        try:
            version = max(version, int(catalog.get("version") or 0))
        except (TypeError, ValueError):
            pass
        for server in catalog.get("servers") or []:
            if not isinstance(server, dict):
                continue
            hostname = str(server.get("hostname") or "").strip()
            vpn = str(server.get("vpn") or "").strip()
            if hostname and vpn:
                merged[(hostname, vpn)] = server
    return {
        "version": version or 5,
        "timestamp": int(time.time()),
        "servers": list(merged.values()),
    }


def _counts(catalog: dict) -> str:
    servers = catalog.get("servers") or []
    wireguard = sum(1 for server in servers if server.get("vpn") == "wireguard")
    return f"{len(servers)} servers ({wireguard} wireguard)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="opencode-vpn")
    parser.add_argument("--manifest", default="/gluetun/servers/nordvpn.json")
    parser.add_argument("--backup", default=str(ROOT / "logs" / "nordvpn_manifest_backup.json"))
    parser.add_argument(
        "--export",
        default=str(ROOT / "logs" / "gluetun_nordvpn_embedded.json"),
        help="local copy of the merged catalog for NordVpnCatalog ('' to skip)",
    )
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--no-api", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        binary = str(Path(tmp) / "gluetun-entrypoint")
        proc = _docker_cp(f"{args.container}:/gluetun-entrypoint", binary)
        if proc.returncode != 0:
            raise SystemExit(f"docker cp entrypoint failed: {proc.stderr[:300]}")
        embedded = _extract_nordvpn(Path(binary).read_bytes())
        print(f"embedded: {_counts(embedded)}, version={embedded.get('version')}")

        current = _read_container_json(args.container, args.manifest, tmp, "current.json")
        if current is not None:
            print(f"current:  {_counts(current)}")
        else:
            print("current:  none")

        api = None
        if not args.no_api:
            try:
                api = _api_catalog(_fetch_api(args.api_url))
                print(f"api:      {_counts(api)}")
            except Exception as exc:
                print(f"api:      unavailable ({type(exc).__name__}: {exc})")

        merged = _merge([catalog for catalog in (embedded, current, api) if catalog])
        print(f"merged:   {_counts(merged)}")
        for name in ("al36.nordvpn.com", "al41.nordvpn.com", "al75.nordvpn.com", "ch524.nordvpn.com"):
            hit = any(
                server.get("hostname") == name
                for server in (merged.get("servers") or [])
            )
            print(f"  {name}: {'yes' if hit else 'no'}")

        if args.export:
            export_path = Path(args.export)
            export_path.parent.mkdir(parents=True, exist_ok=True)
            with open(export_path, "w", encoding="utf-8") as handle:
                json.dump(merged, handle, separators=(",", ":"))
            print(f"catalog exported: {export_path}")
        if args.dry_run:
            return 0

        merged_path = str(Path(tmp) / "nordvpn_merged.json")
        with open(merged_path, "w", encoding="utf-8") as handle:
            json.dump(merged, handle)
        Path(args.backup).parent.mkdir(parents=True, exist_ok=True)
        backup = _docker_cp(f"{args.container}:{args.manifest}", args.backup)
        if backup.returncode == 0:
            print(f"backup: {args.backup}")
        updated = _docker_cp(merged_path, f"{args.container}:{args.manifest}")
        if updated.returncode != 0:
            raise SystemExit(f"docker cp manifest failed: {updated.stderr[:300]}")
        print(f"manifest updated: {args.container}:{args.manifest}")
        print("recreate the station with SERVER_HOSTNAMES=<hostname> to use it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
