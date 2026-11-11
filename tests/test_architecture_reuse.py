"""Architecture reuse guards (T110): no fork of lifecycle/reconciler/manager."""

from __future__ import annotations

import asyncio

from ops.providers.nordvpn import NordVpnProvider
from ops.reconciler import reconcile
from ops.supervisor import StationSupervisor


class FakeManager:
    _station = 1
    _docker_container = "opencode-vpn-1"

    def __init__(self):
        self.calls = []

    async def start(self):
        self.calls.append("start")

    async def stop(self):
        self.calls.append("stop")

    async def restart(self):
        self.calls.append("restart")

    def get_status(self):
        return {
            "station": 1,
            "status": "connected",
            "assigned_stack": "wireguard",
            "current_host": "se1.nordvpn.com",
            "current_country": "Sweden",
            "proxy_port": 8888,
            "ip": "1.2.3.4",
        }


def test_supervisor_delegates_to_vpn_manager():
    manager = FakeManager()
    supervisor = StationSupervisor(station=1, manager=manager)
    asyncio.run(supervisor.start())
    asyncio.run(supervisor.stop())
    asyncio.run(supervisor.restart("test"))
    assert manager.calls == ["start", "stop", "restart"]


def test_nordvpn_provider_reuses_manager_status():
    manager = FakeManager()
    specs = NordVpnProvider([manager]).specs()
    assert len(specs) == 1
    assert specs[0].station == 1
    assert specs[0].host == "se1.nordvpn.com"
    assert specs[0].container == "opencode-vpn-1"
    assert specs[0].transport == "wireguard"


def test_reconciler_is_the_shared_entrypoint():
    assert reconcile([], []) == []
