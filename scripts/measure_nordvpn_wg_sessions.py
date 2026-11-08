"""Mesure la limite de sessions NordVPN en WireGuard (011, free-only).

Crée N conteneurs Gluetun temporaires avec la même clé WireGuard et le même
host, puis teste l'egress de chacun. Aucun secret n'est affiché.

Usage :
    python scripts/measure_nordvpn_wg_sessions.py --dry-run
    python scripts/measure_nordvpn_wg_sessions.py --apply --isolate

`--isolate` arrête les conteneurs `opencode-vpn*` avant la mesure. Le proxy et
le superviseur doivent être arrêtés pour que la mesure soit propre.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = (
    "qmcgaw/gluetun@sha256:"
    "4af0c804003481e100180e1f8d4d5e9c48163fedc0f88d061881b615aef9d183"
)
WG_ENV = ROOT / "vpn_configs" / "wireguard.env"
BASE_PORT = 12001
DEFAULT_HOST = "mx162.nordvpn.com"


def _docker(args: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _probe(port: int) -> dict:
    proc = subprocess.run(
        [
            "curl",
            "-s",
            "--max-time",
            "12",
            "--socks5-hostname",
            f"127.0.0.1:{port}",
            "https://api.ipify.org",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    ip = (proc.stdout or "").strip()
    return {"port": port, "ok": bool(ip), "ip": ip or None}


def _run_args(name: str, host_port: int, host: str) -> list[str]:
    return [
        "run",
        "-d",
        "--name",
        name,
        "--cap-add",
        "NET_ADMIN",
        "--device",
        "/dev/net/tun",
        "-p",
        f"127.0.0.1:{host_port}:1080",
        "--env-file",
        str(WG_ENV),
        "-e",
        "VPN_SERVICE_PROVIDER=nordvpn",
        "-e",
        "VPN_TYPE=wireguard",
        "-e",
        f"SERVER_HOSTNAMES={host}",
        "-e",
        "HTTPPROXY=on",
        "-e",
        "SOCKS5_ENABLED=on",
        "-e",
        "SOCKS5_LISTENING_ADDRESS=:1080",
        "-e",
        "FIREWALL=on",
        "-e",
        "DNS_UPSTREAM_RESOLVER_TYPE=plain",
        "-e",
        "DNS_UPSTREAM_PLAIN_ADDRESSES=1.1.1.1:53,8.8.8.8:53",
        IMAGE,
    ]


DEFAULT_HOSTS = [
    "al41.nordvpn.com",
    "al74.nordvpn.com",
    "al46.nordvpn.com",
    "al75.nordvpn.com",
    "mx162.nordvpn.com",
    "dz6.nordvpn.com",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--isolate", action="store_true")
    parser.add_argument("--count", type=int, default=6)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument(
        "--hosts",
        default=",".join(DEFAULT_HOSTS),
        help="liste de hosts distincts, séparés par des virgules",
    )
    parser.add_argument("--settle", type=float, default=45.0)
    args = parser.parse_args(argv)
    hosts = [item.strip() for item in str(args.hosts).split(",") if item.strip()]
    if not hosts:
        hosts = [args.host]
    if not args.apply or args.dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "count": args.count,
                    "host": args.host,
                    "image": IMAGE,
                }
            )
        )
        return 0
    if not WG_ENV.is_file():
        print(json.dumps({"ok": False, "error": "wireguard.env absent"}))
        return 2

    names = [f"opencode-sess-test-{i}" for i in range(1, args.count + 1)]
    for name in names:
        _docker(["rm", "-f", name], timeout=60)
    if args.isolate:
        _docker(["stop", *[f"opencode-vpn-{i}" for i in range(2, 7)]], timeout=120)
        _docker(["stop", "opencode-vpn"], timeout=60)
        time.sleep(60)

    started: list[str] = []
    results: list[dict] = []
    try:
        for index, name in enumerate(names, start=1):
            port = BASE_PORT + index - 1
            host = hosts[(index - 1) % len(hosts)]
            start = _docker(_run_args(name, port, host))
            if start.returncode != 0:
                results.append(
                    {
                        "count": index,
                        "container": name,
                        "host": host,
                        "started": False,
                        "error": (start.stderr or "").strip()[:200],
                    }
                )
                break
            started.append(name)
            time.sleep(max(0.0, args.settle))
            probes = [_probe(BASE_PORT + j) for j in range(index)]
            result = {
                "count": index,
                "container": name,
                "host": host,
                "started": True,
                "ok": sum(1 for item in probes if item["ok"]),
                "probes": probes,
            }
            results.append(result)
            if result["ok"] < index:
                break
    finally:
        for name in started:
            _docker(["rm", "-f", name], timeout=60)

    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
