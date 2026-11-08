"""Adapter NordVPN (Gluetun/Compose/auth/session) du noyau 011.

Le mécanisme reste ici : compose, credentials, AUTH throttle, session et
transport WG/OV. La politique commune vient de ``ops.lifecycle_policy``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ops.desired_state import DesiredStationSpec
from ops.lifecycle import SlotObservation


def _run(coro):
    """Exécute une coroutine depuis un adapter synchrone hors event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("NordVpnProvider sync method called inside an event loop")


class NordVpnProvider:
    provider = "nordvpn"

    def __init__(self, managers: list[Any] | None = None, **kwargs: Any) -> None:
        self.managers = [m for m in (managers or []) if m is not None]
        self.options = dict(kwargs)

    def _manager(self, station: int) -> Any:
        for manager in self.managers:
            if int(getattr(manager, "_station", 0) or 0) == int(station):
                return manager
        return None

    def specs(self) -> list[DesiredStationSpec]:
        specs: list[DesiredStationSpec] = []
        for manager in self.managers:
            sid = int(getattr(manager, "_station", 0) or 0)
            if sid <= 0:
                continue
            try:
                status = manager.get_status()
            except Exception:
                status = {}
            stack = str(status.get("assigned_stack") or status.get("stack") or "openvpn")
            transport = str(
                status.get("ovpn_protocol_effective")
                or ("wireguard" if stack == "wireguard" else "openvpn-udp")
            )
            if transport in ("udp", "tcp"):
                transport = f"openvpn-{transport}"
            specs.append(
                DesiredStationSpec(
                    provider="nordvpn",
                    station=sid,
                    enabled=not bool(getattr(manager, "_disabled", False)),
                    stack=stack,
                    transport=transport,
                    host=status.get("pinned_hostname") or status.get("current_host"),
                    country=status.get("assigned_country") or status.get("current_country"),
                    port=int(status.get("proxy_port") or (1079 + sid)),
                    container=str(
                        status.get("container")
                        or getattr(manager, "_docker_container", "")
                    ),
                    image=str(status.get("image") or ""),
                )
            )
        return specs

    def observe(self, spec: DesiredStationSpec) -> SlotObservation:
        manager = self._manager(int(spec.station))
        if manager is None:
            return SlotObservation(reason="missing")
        try:
            status = manager.get_status()
        except Exception as exc:
            return SlotObservation(reason=f"status:{type(exc).__name__}")
        ip = str(status.get("ip") or status.get("current_ip") or "").strip() or None
        state = str(status.get("status") or "unknown")
        error = str(status.get("error") or "").strip() or None
        usable = bool(
            ip
            and state in ("connected", "degraded")
            and not status.get("auth_failed")
            and str(status.get("egress_state") or "") != "tunnel_down"
        )
        return SlotObservation(
            container_state="running" if state != "error" else "error",
            health="healthy" if usable else "unhealthy",
            tunnel_up=bool(ip),
            dns_ok=bool(ip),
            egress_ip=ip,
            quota_state="ok" if usable else "unknown",
            client_usable=usable,
            reason=error,
        )

    def create(self, spec: DesiredStationSpec) -> None:
        manager = self._manager(int(spec.station))
        if manager is not None:
            _run(manager.start())

    def recreate(self, spec: DesiredStationSpec, reason: str | None = None) -> None:
        manager = self._manager(int(spec.station))
        if manager is not None:
            _run(manager.restart())

    def delete(self, spec: DesiredStationSpec, reason: str | None = None) -> None:
        manager = self._manager(int(spec.station))
        if manager is not None:
            _run(manager.stop_container())

    def rotate(self, spec: DesiredStationSpec, trigger: str) -> dict[str, Any]:
        manager = self._manager(int(spec.station))
        if manager is None:
            return {"ok": False, "error": "missing"}
        try:
            ip = _run(manager.connect_next())
            return {"ok": True, "trigger": trigger, "ip": ip}
        except Exception as exc:
            return {"ok": False, "trigger": trigger, "error": str(exc)[:200]}

    def probe(self, spec: DesiredStationSpec) -> SlotObservation:
        return self.observe(spec)

    def persist(self, spec: DesiredStationSpec, observation: SlotObservation) -> None:
        return None
