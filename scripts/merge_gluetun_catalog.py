#!/usr/bin/env python3
"""Merge gluetun's embedded NordVPN catalog into its on-disk manifest.

The public NordVPN API omits older servers such as ``al36``–``al75`` (the
87.120.102.x Albanian pool).  Gluetun still embeds them in its binary, but its
manifest file is newer and wins, so ``SERVER_HOSTNAMES=al41.nordvpn.com`` is
rejected as "not a valid choice".

This script extracts the embedded NordVPN JSON from ``/gluetun-entrypoint``,
backs up the current manifest, and writes the full catalog to
``/gluetun/servers/nordvpn.json``.  The next container recreate can then pin
those servers.

Usage:
    python scripts/merge_gluetun_catalog.py --dry-run
    python scripts/merge_gluetun_catalog.py
    python scripts/merge_gluetun_catalog.py --container opencode-vpn
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _docker_cp(src: str, dst: str) -> None:
    proc = subprocess.run(
        ["docker", "cp", src, dst],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        raise SystemExit(f"docker cp {src} -> {dst} failed: {proc.stderr[:300]}")


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="opencode-vpn")
    parser.add_argument("--manifest", default="/gluetun/servers/nordvpn.json")
    parser.add_argument("--backup", default=str(ROOT / "logs" / "nordvpn_manifest_backup.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        binary = str(Path(tmp) / "gluetun-entrypoint")
        _docker_cp(f"{args.container}:/gluetun-entrypoint", binary)
        data = Path(binary).read_bytes()
        catalog = _extract_nordvpn(data)
        servers = catalog.get("servers") or []
        print(f"embedded catalog: {len(servers)} servers, version={catalog.get('version')}")
        for name in ("al36.nordvpn.com", "al41.nordvpn.com", "al75.nordvpn.com"):
            hit = next((s for s in servers if s.get("hostname") == name), None)
            print(f"  {name}: {'yes' if hit else 'no'}")
        if args.dry_run:
            return 0

        merged = str(Path(tmp) / "nordvpn_full.json")
        with open(merged, "w", encoding="utf-8") as handle:
            json.dump(catalog, handle)
        Path(args.backup).parent.mkdir(parents=True, exist_ok=True)
        _docker_cp(f"{args.container}:{args.manifest}", args.backup)
        _docker_cp(merged, f"{args.container}:{args.manifest}")
        print(f"backup: {args.backup}")
        print(f"manifest updated: {args.container}:{args.manifest}")
        print("recreate the station with SERVER_HOSTNAMES=<alxx> to use it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
