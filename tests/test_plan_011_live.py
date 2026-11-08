"""Plan 011 — preuves live (Docker requis, exclues du gate par défaut).

Lancer avec :

    python -m pytest tests/test_plan_011_live.py -m docker -q

Ces tests sont volontairement ``xfail(strict=False)`` : l'état live peut
changer entre deux exécutions (Docker arrêté, station en cours de rotation).
Ils documentent les preuves à obtenir après la Phase 0/2. Ils ne remplacent
pas le soak 24 h ni ``station_slo_watch``.
"""

from __future__ import annotations

import json
import socket
import subprocess
import urllib.request

import pytest


def _api_json(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:4000{path}", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _docker_ps() -> list[dict]:
    proc = subprocess.run(
        ["docker", "ps", "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _socks_egress(port: int, url: str = "https://api6.ipify.org") -> str | None:
    proc = subprocess.run(
        [
            "curl",
            "-s",
            "--max-time",
            "12",
            "--socks5-hostname",
            f"127.0.0.1:{port}",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    value = (proc.stdout or "").strip()
    return value or None


@pytest.mark.docker
def test_live_six_nordvpn_containers_exist() -> None:
    names = {row["Names"] for row in _docker_ps()}
    expected = {"opencode-vpn"} | {f"opencode-vpn-{i}" for i in range(2, 7)}
    assert expected <= names


@pytest.mark.docker
def test_live_warp_endpoints_have_six_unique_blocks() -> None:
    ports = [11080, 11085, 11086, 11087, 11103, 11105]
    blocks = set()
    for port in ports:
        ip = _socks_egress(port)
        assert ip, f"pas d'egress sur {port}"
        parts = ip.split(":")
        blocks.add(":".join(parts[:4]) + "::/64")
    assert len(blocks) == 6


@pytest.mark.docker
def test_live_warp_ports_are_bound_to_loopback() -> None:
    rows = [row for row in _docker_ps() if row["Names"].startswith("warp-")]
    assert rows
    for row in rows:
        assert row["Ports"].startswith("127.0.0.1:"), row["Ports"]


@pytest.mark.docker
def test_live_proxy_listens_on_4000() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        assert sock.connect_ex(("127.0.0.1", 4000)) == 0


@pytest.mark.docker
def test_live_usable_stations_matches_station_health() -> None:
    data = _api_json("/api/vpn-status")
    providers = {str(item).lower() for item in (data.get("egress_providers") or [])}
    if "warp" in providers:
        keys = [key for key in (data.get("station_health") or {}) if str(key).startswith("-")]
    else:
        keys = [key for key in (data.get("station_health") or {}) if str(key).isdigit() and int(key) > 0]
    health = data.get("station_health") or {}
    healthy = sum(
        1
        for key in keys
        if str((health.get(key) or {}).get("state")) in ("usable", "probing")
    )
    assert data.get("usable_stations") == healthy


@pytest.mark.docker
def test_live_capacity_is_not_ok_when_boot_error_is_zero() -> None:
    data = _api_json("/api/vpn-status")
    boot_error = str(data.get("boot_error") or "")
    assert not (data.get("usable_stations", 0) >= 6 and "boot 0/6" in boot_error)


@pytest.mark.docker
@pytest.mark.xfail(strict=False, reason="011 SC-001 : 6/6 NordVPN client-utilisables")
def test_live_nordvpn_socks_egress_responds() -> None:
    ports = [1080, 1081, 1082, 1083, 1084, 1085]
    ok = 0
    for port in ports:
        if _socks_egress(port, "https://api.ipify.org"):
            ok += 1
    assert ok == 6
