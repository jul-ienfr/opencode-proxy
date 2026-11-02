"""ComposeHostnameBinder tests (no Docker)."""

from __future__ import annotations

import subprocess

from free.ip_registry.adapters.compose_binder import ComposeHostnameBinder


class FakeRunner:
    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, command, cwd):
        self.calls.append((list(command), cwd))
        return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)


async def test_assign_writes_override_and_recreates(tmp_path):
    runner = FakeRunner()

    async def egress(station: int) -> str:
        return "186.247.44.61"

    binder = ComposeHostnameBinder(
        tmp_path,
        settle_s=0,
        runner=runner,
        egress=egress,
    )

    result = await binder.assign(2, "al110.nordvpn.com")

    assert result.ok
    assert result.egress_ip == "186.247.44.61"
    assert result.hostname == "al110.nordvpn.com"
    override = binder.override_path_for(2).read_text(encoding="utf-8")
    assert "vpn-gluetun-2" in override
    assert "SERVER_HOSTNAMES=al110.nordvpn.com" in override
    command, cwd = runner.calls[0]
    assert "vpn-gluetun-2" in command
    assert "--force-recreate" in command
    assert cwd == str(tmp_path)


async def test_assign_writes_a_country_override(tmp_path):
    runner = FakeRunner()

    async def egress(station: int) -> str:
        return "146.70.238.51"

    binder = ComposeHostnameBinder(
        tmp_path,
        settle_s=0,
        runner=runner,
        egress=egress,
    )

    result = await binder.assign(5, "ae73.nordvpn.com", country="United Arab Emirates")

    assert result.ok
    override = binder.override_path_for(5).read_text(encoding="utf-8")
    assert "SERVER_HOSTNAMES=ae73.nordvpn.com" in override
    assert "SERVER_COUNTRIES=United Arab Emirates" in override


async def test_assign_writes_an_openvpn_override(tmp_path):
    runner = FakeRunner()

    async def egress(station: int) -> str:
        return "187.15.106.56"

    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=runner, egress=egress)

    result = await binder.assign(
        5,
        "bg95.nordvpn.com",
        country="Bulgaria",
        vpn_type="openvpn",
        openvpn_protocol="tcp",
    )

    assert result.ok
    override = binder.override_path_for(5).read_text(encoding="utf-8")
    assert "VPN_TYPE=openvpn" in override
    assert "OPENVPN_PROTOCOL=tcp" in override


async def test_assign_reports_recreate_failure(tmp_path):
    runner = FakeRunner(returncode=1, stderr="boom")
    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=runner)

    result = await binder.assign(1, "al110.nordvpn.com")

    assert not result.ok
    assert result.error is not None and "recreate" in result.error


async def test_assign_rejects_empty_hostname(tmp_path):
    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=FakeRunner())
    result = await binder.assign(1, "")
    assert not result.ok
    assert result.error == "empty-hostname"


async def test_assign_reports_no_egress(tmp_path):
    async def egress(station: int) -> str:
        return ""

    binder = ComposeHostnameBinder(
        tmp_path, settle_s=0, runner=FakeRunner(), egress=egress
    )
    result = await binder.assign(1, "al110.nordvpn.com")
    assert not result.ok
    assert result.error == "no-egress"


async def test_assign_retries_egress_until_the_tunnel_answers(tmp_path):
    calls = []

    async def egress(station: int) -> str:
        calls.append(station)
        return "" if len(calls) < 3 else "186.247.44.77"

    binder = ComposeHostnameBinder(
        tmp_path,
        settle_s=0,
        egress_wait_s=0.25,
        egress_poll_s=0.1,
        runner=FakeRunner(),
        egress=egress,
    )
    result = await binder.assign(1, "al110.nordvpn.com")
    assert result.ok
    assert result.egress_ip == "186.247.44.77"
    assert calls == [1, 1, 1]


async def test_assign_gives_up_after_the_egress_deadline(tmp_path):
    calls = []

    async def egress(station: int) -> str:
        calls.append(station)
        return ""

    binder = ComposeHostnameBinder(
        tmp_path,
        settle_s=0,
        egress_wait_s=0.25,
        egress_poll_s=0.1,
        runner=FakeRunner(),
        egress=egress,
    )
    result = await binder.assign(1, "al110.nordvpn.com")
    assert not result.ok
    assert result.error == "no-egress"
    assert len(calls) >= 2 and set(calls) == {1}


def test_override_path_is_per_station(tmp_path):
    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=FakeRunner())
    p1 = binder.override_path_for(1)
    p2 = binder.override_path_for(2)
    assert p1 != p2
    assert p1.name.endswith("-1.yml")
    assert p2.name.endswith("-2.yml")


def test_override_path_already_station_suffixed_is_kept(tmp_path):
    binder = ComposeHostnameBinder(
        tmp_path,
        override_path="logs/compose-hostname-3.yml",
        settle_s=0,
        runner=FakeRunner(),
    )
    assert binder.override_path_for(3) == tmp_path / "logs" / "compose-hostname-3.yml"
