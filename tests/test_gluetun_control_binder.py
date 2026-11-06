"""Gluetun control-API binder tests (no Docker, no network)."""

from __future__ import annotations

import subprocess

from free.ip_registry.adapters.gluetun_control import GluetunControlBinder


class FakeRunner:
    def __init__(
        self,
        *,
        key: str = "secret-key",
        cp_rc: int = 0,
        exec_rc: int = 0,
        exec_stdout: str = "running",
    ) -> None:
        self.key = key
        self.cp_rc = cp_rc
        self.exec_rc = exec_rc
        self.exec_stdout = exec_stdout
        self.calls: list[list[str]] = []

    def __call__(self, command, timeout):
        self.calls.append(list(command))
        if command[:2] == ["docker", "inspect"]:
            body = f"VPN_TYPE=wireguard\nVPN_CONTROL_API_KEY={self.key}\n"
            return subprocess.CompletedProcess(command, 0, body, "")
        if command[:2] == ["docker", "cp"]:
            return subprocess.CompletedProcess(command, self.cp_rc, "", "")
        return subprocess.CompletedProcess(
            command, self.exec_rc, self.exec_stdout, ""
        )


async def test_control_bind_switches_hostname_and_returns_egress(monkeypatch):
    monkeypatch.delenv("VPN_CONTROL_API_KEY", raising=False)
    runner = FakeRunner()

    async def egress(station: int) -> str:
        return "186.247.44.61"

    binder = GluetunControlBinder(
        container="opencode-vpn-2",
        egress=egress,
        settle_s=0,
        egress_wait_s=1,
        egress_poll_s=0.01,
        runner=runner,
    )
    result = await binder.assign(
        2, "uk2639.nordvpn.com", country="United Kingdom"
    )

    assert result.ok
    assert result.egress_ip == "186.247.44.61"
    assert binder._container == "opencode-vpn-2"
    scripts = [
        call[-1]
        for call in runner.calls
        if call[:2] == ["docker", "exec"]
    ]
    assert len(scripts) == 1
    assert "--method=PUT" in scripts[0]
    assert "--body-file=/tmp/gluetun-control-body.json" in scripts[0]
    assert "X-API-Key: secret-key" in scripts[0]
    assert any(
        call[:2] == ["docker", "cp"]
        and call[2].endswith(".json")
        and call[3].startswith("opencode-vpn-2:/tmp/")
        for call in runner.calls
    )


async def test_control_bind_accepts_204_empty_body(monkeypatch):
    monkeypatch.delenv("VPN_CONTROL_API_KEY", raising=False)
    runner = FakeRunner(exec_stdout="")

    async def egress(station: int) -> str:
        return "186.247.44.62"

    binder = GluetunControlBinder(
        container="opencode-vpn-2",
        egress=egress,
        settle_s=0,
        egress_wait_s=1,
        egress_poll_s=0.01,
        runner=runner,
    )
    result = await binder.assign(2, "uk2639.nordvpn.com")
    assert result.ok
    assert result.egress_ip == "186.247.44.62"


async def test_control_bind_reports_no_egress(monkeypatch):
    monkeypatch.delenv("VPN_CONTROL_API_KEY", raising=False)
    runner = FakeRunner()

    async def egress(station: int) -> str:
        return ""

    binder = GluetunControlBinder(
        container="opencode-vpn-2",
        egress=egress,
        settle_s=0,
        egress_wait_s=0.05,
        egress_poll_s=0.01,
        runner=runner,
    )
    result = await binder.assign(2, "uk2639.nordvpn.com")
    assert not result.ok
    assert result.error == "control-no-egress"


async def test_control_bind_falls_back_on_unavailable_control(monkeypatch):
    monkeypatch.delenv("VPN_CONTROL_API_KEY", raising=False)
    runner = FakeRunner(cp_rc=1)

    binder = GluetunControlBinder(
        container="opencode-vpn-2",
        egress=lambda station: None,
        settle_s=0,
        runner=runner,
    )
    result = await binder.assign(2, "uk2639.nordvpn.com")
    assert not result.ok
    assert str(result.error).startswith("control-unavailable:")


async def test_control_bind_without_key_is_explicit(monkeypatch):
    monkeypatch.delenv("VPN_CONTROL_API_KEY", raising=False)
    runner = FakeRunner(key="")

    binder = GluetunControlBinder(
        container="opencode-vpn-2",
        egress=lambda station: None,
        settle_s=0,
        runner=runner,
    )
    result = await binder.assign(2, "uk2639.nordvpn.com")
    assert not result.ok
    assert result.error == "control-unavailable:no-key"
