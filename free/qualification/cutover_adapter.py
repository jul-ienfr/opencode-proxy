"""Production cutover ports over the registry station adapter.

``CutoverService`` owns the make-before-break state machine; this adapter
performs the four real actions on a station: prepare, switch, probe and
rollback.  It is deliberately thin so tests can inject a fake binder/prober.
"""

from __future__ import annotations

from .cutover import CutoverTarget
from .station_adapter import RegistryStationAdapter, transport_parts


class AdapterCutoverPorts:
    def __init__(
        self,
        adapter: RegistryStationAdapter,
        *,
        station: int,
        model: str = "",
    ) -> None:
        self.adapter = adapter
        self.station = int(station)
        self.model = str(model or adapter.model or "")

    async def prepare(self, target: CutoverTarget) -> bool:
        return bool(str(target.to_option_key or "").strip())

    async def _assign(self, hostname: str, transport: str) -> bool:
        hostname = str(hostname or "").strip()
        if not hostname:
            return False
        vpn_type, openvpn_protocol = transport_parts(transport)
        bind = await self.adapter.binder.assign(
            self.station,
            hostname,
            None,
            vpn_type=vpn_type,
            openvpn_protocol=openvpn_protocol,
        )
        return bool(getattr(bind, "ok", False))

    async def switch(self, target: CutoverTarget) -> bool:
        return await self._assign(target.to_option_key, target.to_transport)

    async def probe(self, target: CutoverTarget) -> bool:
        result = await self.adapter.prober.probe(self.station, self.model)
        return int(getattr(result, "status", 0) or 0) == 200 and bool(
            getattr(result, "valid", True)
        )

    async def rollback(self, target: CutoverTarget) -> bool:
        if not str(target.from_option_key or "").strip():
            return True
        return await self._assign(target.from_option_key, target.from_transport)


__all__ = ["AdapterCutoverPorts"]
