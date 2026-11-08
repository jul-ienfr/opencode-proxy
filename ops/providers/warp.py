"""Adapter WARP (docker run/registration/64/options) du noyau 011.

Le mécanisme reste ici ; la politique commune vient de
``ops.lifecycle_policy``.
"""

from __future__ import annotations

from typing import Any

from free.egress_probe import resolve_egress
from free.egress_reconcile import bucket_for_endpoint
from free.warp_provisioner import WarpEndpointSpec, WarpProvisioner
from ops.desired_state import DesiredEndpointSpec
from ops.lifecycle import SlotObservation


class WarpProvider:
    provider = "warp"

    def __init__(
        self,
        endpoints: list[Any] | None = None,
        *,
        rotator: Any = None,
        provisioner: WarpProvisioner | None = None,
        pool: Any = None,
        **kwargs: Any,
    ) -> None:
        self.endpoints = [ep for ep in (endpoints or []) if ep is not None]
        self.rotator = rotator
        self.provisioner = provisioner or WarpProvisioner()
        self.pool = pool
        self.options = dict(kwargs)

    def _endpoint(self, spec: DesiredEndpointSpec) -> Any:
        for endpoint in self.endpoints:
            if str(getattr(endpoint, "label", "")) == spec.label:
                return endpoint
        return None

    def specs(self) -> list[DesiredEndpointSpec]:
        specs: list[DesiredEndpointSpec] = []
        for endpoint in self.endpoints:
            label = str(getattr(endpoint, "label", "") or "")
            if not label:
                continue
            specs.append(
                DesiredEndpointSpec(
                    provider="warp",
                    label=label,
                    container=str(getattr(endpoint, "container", "") or label),
                    port=int(getattr(endpoint, "port", 0) or 0),
                    enabled=bool(getattr(endpoint, "enabled", True)),
                    host=str(getattr(endpoint, "host", "127.0.0.1") or "127.0.0.1"),
                    ipv6=bool(getattr(endpoint, "ipv6", True)),
                    managed=bool(getattr(endpoint, "managed", True)),
                    image=str(
                        getattr(endpoint, "image", "")
                        or "caomingjun/warp:latest"
                    ),
                    block=str(getattr(endpoint, "block", "") or "") or None,
                )
            )
        return specs

    def observe(self, spec: DesiredEndpointSpec) -> SlotObservation:
        endpoint = self._endpoint(spec)
        if endpoint is None:
            return SlotObservation(reason="missing")
        egress = str(getattr(endpoint, "egress_ip", "") or "").strip() or None
        health = None
        if self.pool is not None and hasattr(self.pool, "_station_health"):
            try:
                health = self.pool._station_health.get(
                    int(getattr(endpoint, "_station", 0))
                )
            except Exception:
                health = None
        usable = bool(
            getattr(endpoint, "enabled", True)
            and egress
            and (health is None or str(getattr(health, "state", "")) in ("usable", "probing"))
        )
        return SlotObservation(
            container_state="running" if egress else "unknown",
            health="healthy" if usable else "unhealthy",
            tunnel_up=bool(egress),
            dns_ok=bool(egress),
            egress_ip=egress,
            block=bucket_for_endpoint(endpoint),
            quota_state="ok" if usable else "unknown",
            client_usable=usable,
            reason=None if usable else "egress_or_health",
        )

    def create(self, spec: DesiredEndpointSpec) -> None:
        endpoint_spec = self._provision_spec(spec)
        result = self.provisioner.apply([endpoint_spec], actual={})
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "create failed"))

    def recreate(self, spec: DesiredEndpointSpec, reason: str | None = None) -> None:
        endpoint_spec = self._provision_spec(spec)
        result = self.provisioner.apply(
            [endpoint_spec],
            actual={
                endpoint_spec.container: {
                    "image": endpoint_spec.image,
                    "ports": "0.0.0.0:0->1080/tcp",
                }
            },
        )
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "recreate failed"))

    def delete(self, spec: DesiredEndpointSpec, reason: str | None = None) -> None:
        result = self.provisioner.delete(spec.container, confirm=True)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "delete failed"))

    def rotate(self, spec: DesiredEndpointSpec, trigger: str) -> dict[str, Any]:
        if self.rotator is None:
            return {"ok": False, "error": "no_rotator"}
        endpoint = self._endpoint(spec)
        if endpoint is None:
            return {"ok": False, "error": "missing"}
        try:
            result = self.rotator.rotate(endpoint)
            return {"ok": bool(result.ok), "trigger": trigger, **result.as_dict()}
        except Exception as exc:
            return {"ok": False, "trigger": trigger, "error": str(exc)[:200]}

    def probe(self, spec: DesiredEndpointSpec) -> SlotObservation:
        endpoint = self._endpoint(spec)
        if endpoint is not None:
            try:
                ip = resolve_egress(
                    proxy_url=f"socks5h://{getattr(endpoint, 'host', '127.0.0.1')}:"
                    f"{int(getattr(endpoint, 'port', 0) or 0)}",
                    ipv6=bool(getattr(endpoint, "ipv6", True)),
                    timeout_s=8.0,
                )
                if ip:
                    return SlotObservation(
                        egress_ip=ip,
                        client_usable=True,
                        health="healthy",
                    )
            except Exception:
                pass
        return self.observe(spec)

    def persist(self, spec: DesiredEndpointSpec, observation: SlotObservation) -> None:
        return None

    def _provision_spec(self, spec: DesiredEndpointSpec) -> WarpEndpointSpec:
        return WarpEndpointSpec(
            label=spec.label,
            container=spec.container,
            port=spec.port,
            image=spec.image,
            ipv6=spec.ipv6,
            managed=spec.managed,
            provider="warp",
            block=spec.block,
            enabled=spec.enabled,
        )
