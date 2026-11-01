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
    override = (tmp_path / "logs" / "compose-hostname.yml").read_text(encoding="utf-8")
    assert "vpn-gluetun-2" in override
    assert "SERVER_HOSTNAMES=al110.nordvpn.com" in override
    command, cwd = runner.calls[0]
    assert "vpn-gluetun-2" in command
    assert "--force-recreate" in command
    assert cwd == str(tmp_path)


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
