"""Regression: a dead SERVER_HOSTNAMES_STATIONn pin must be escapable.

2026-11-01: station 1 was pinned to ``al41.nordvpn.com`` with
``country_rotation: false``. The server died; every restart/compose
reconnected the same hostname and the gluetun healthcheck restarted the
container forever. The watchdog now drops the pin after a failed recovery
so gluetun re-picks a live server inside SERVER_COUNTRIES.
"""

import os

from test_vpn_freshness import FakeVPNManager, _cfg


class _PinFake(FakeVPNManager):
    async def _compose_up(self, force_recreate=False):
        self.calls["compose_up"] += 1
        if force_recreate:
            self.calls["force_recreate"] += 1


async def test_escape_clears_pin_file_process_env_and_recreates(tmp_path, monkeypatch):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setenv("VPN_DOCKER_COMPOSE_FILE", str(compose))
    env_file = tmp_path / ".env"
    env_file.write_text(
        "UNRELATED=keep\nSERVER_HOSTNAMES_STATION2=al75.nordvpn.com\nOTHER=keep-too\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SERVER_HOSTNAMES_STATION2", "al75.nordvpn.com")

    mgr = _PinFake(_cfg(tmp_path), station=2, tmp_path=tmp_path)
    mgr._pinned_hostname = "al75.nordvpn.com"

    assert await mgr._escape_dead_compose_pin("egress dead test") is True

    text = env_file.read_text(encoding="utf-8")
    assert "SERVER_HOSTNAMES_STATION2=\n" in text
    assert "UNRELATED=keep" in text and "OTHER=keep-too" in text  # untouched
    assert os.environ["SERVER_HOSTNAMES_STATION2"] == ""
    assert mgr._pinned_hostname is None
    assert mgr.calls["force_recreate"] == 1
    assert mgr._watchdog_last_action == "pin-escape"


async def test_escape_is_noop_without_pin(tmp_path, monkeypatch):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setenv("VPN_DOCKER_COMPOSE_FILE", str(compose))
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")

    mgr = _PinFake(_cfg(tmp_path), station=2, tmp_path=tmp_path)
    mgr._pinned_hostname = None

    assert await mgr._escape_dead_compose_pin("test") is False
    assert mgr.calls["compose_up"] == 0


async def test_escape_is_rate_limited(tmp_path, monkeypatch):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setenv("VPN_DOCKER_COMPOSE_FILE", str(compose))
    (tmp_path / ".env").write_text("SERVER_HOSTNAMES_STATION2=al75.nordvpn.com\n", encoding="utf-8")
    monkeypatch.setenv("SERVER_HOSTNAMES_STATION2", "al75.nordvpn.com")

    mgr = _PinFake(_cfg(tmp_path), station=2, tmp_path=tmp_path)
    mgr._pinned_hostname = "al75.nordvpn.com"

    assert await mgr._escape_dead_compose_pin("first") is True
    # Simulate a re-pin racing the cooldown window.
    mgr._pinned_hostname = "al75.nordvpn.com"
    assert await mgr._escape_dead_compose_pin("second") is False
    assert mgr.calls["force_recreate"] == 1
