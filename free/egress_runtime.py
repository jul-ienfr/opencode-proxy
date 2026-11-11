"""Composition root for the egress catalog.

Reads ``egress_catalog`` and ``ip_rotation`` once, builds the store, adapters
and scheduler, and exposes the only pool-facing operation.  Every config key
has a consumer here; nothing is read “for information”.
"""

from __future__ import annotations

import ipaddress
import json
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from free.egress_adapters.nordvpn import NordVpnEgressAdapter
from free.egress_adapters.public import PublicEgressAdapter
from free.egress_adapters.warp import WarpEgressAdapter
from free.egress_catalog import (
    Observation,
    ObservationPhase,
    OptionStatus,
    Provider,
    ProviderPolicy,
)
from free.egress_rotation import rows_from_options, select_many
from free.egress_scheduler import EgressScheduler
from free.egress_sources import SourceDefinition, load_source_file, parse_source_definitions
from free.egress_store import EgressStore
from free.threat_intel import ThreatIntel, parse_feed_config

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class EgressRuntime:
    store: EgressStore
    scheduler: EgressScheduler
    policies: dict[str, ProviderPolicy] = field(default_factory=dict)
    pool_cfg: dict[str, Any] = field(default_factory=dict)
    pool_getter: Any = None
    threat_intel: ThreatIntel | None = None
    denylist: list[str] = field(default_factory=list)
    qualification_scheduler: Any = None
    _threat_task: Any = None
    _maintenance_task: Any = None

    async def start(self) -> None:
        self.scheduler.start()
        if self.qualification_scheduler is not None:
            self.qualification_scheduler.start()
        # Apply the catalog's current usable options immediately instead of
        # waiting for the first public cycle (which may take minutes).
        try:
            import asyncio

            pool = self.pool_getter() if callable(self.pool_getter) else None
            if pool is not None:
                pool._public_rows_provider = self.public_rows  # type: ignore[attr-defined]
            for policy in self.policies.values():
                self.store.recompute_scores(policy)
            self.store.sync_source_usable_counts()
            await asyncio.to_thread(self.sync_public_pool, pool)
        except Exception:
            pass
        if self.threat_intel is not None and self._threat_task is None:
            import asyncio

            self._threat_task = asyncio.create_task(
                self.threat_intel.run_forever(),
                name="egress-threat-feeds",
            )
        if self._maintenance_task is None:
            import asyncio

            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(),
                name="egress-maintenance",
            )

    async def close(self) -> None:
        for task in (self._threat_task, self._maintenance_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        self._threat_task = None
        self._maintenance_task = None
        if self.qualification_scheduler is not None:
            await self.qualification_scheduler.stop()
        await self.scheduler.close()
        self.store.close()

    async def refresh_now(self, provider: str):
        return await self.scheduler.refresh_now(provider)

    def rotation_plan(self) -> dict[str, Any]:
        from free.egress_rotation import rotation_plan

        return rotation_plan(self.store, self.policies.values())

    def _is_denied(self, option) -> bool:
        networks = []
        for raw in self.denylist:
            try:
                networks.append(ipaddress.ip_network(str(raw).strip(), strict=False))
            except ValueError:
                continue
        for value in (option.host, option.egress_ip):
            try:
                address = ipaddress.ip_address(str(value or "").strip())
            except ValueError:
                continue
            if any(address in network for network in networks):
                return True
        if self.threat_intel is not None:
            for value in (option.host, option.egress_ip):
                try:
                    if value and self.threat_intel.matches(str(value)):
                        return True
                except Exception:
                    continue
        return False

    def block_indicator(self, value: str) -> dict[str, Any]:
        """Manually retire an IP/CIDR and add it to the runtime denylist."""

        network = ipaddress.ip_network(str(value or "").strip(), strict=False)
        canonical = str(network)
        if canonical not in self.denylist:
            self.denylist.append(canonical)
        matched = 0
        for option in self.store.list_options(limit=5_000):
            values = (option.host, option.egress_ip)
            if not any(_in_network(item, network) for item in values):
                continue
            policy = self.policies.get(option.provider)
            if policy is None:
                continue
            self.store.apply(
                option,
                Observation(
                    phase=ObservationPhase.EGRESS,
                    status=0,
                    result=str(OptionStatus.DEAD),
                    observed_at=time.time(),
                    message=f"blocage manuel: {canonical}",
                    run_id="manual-block",
                ),
                policy,
                run_id="manual-block",
            )
            matched += 1
        return {"ip": canonical, "matched": matched, "denylist": list(self.denylist)}

    def maintenance(self) -> dict[str, Any]:
        """Purge retention and refresh the read-only JSON export."""

        removed = self.store.purge()
        export = Path(self.store.path).with_name("egress_catalog.json")
        self.store.export_json(export)
        return {**removed, "export": str(export)}

    async def _maintenance_loop(self) -> None:
        import asyncio

        while True:
            await asyncio.sleep(86_400.0)
            try:
                await asyncio.to_thread(self.maintenance)
            except Exception:
                pass

    def public_rows(self) -> list[dict[str, Any]]:
        policy = self.policies.get(Provider.PUBLIC)
        if policy is None:
            return []
        selected = [
            option
            for option in select_many(self.store, policy, count=policy.max_active)
            if not self._is_denied(option)
        ]
        for option in selected:
            try:
                self.store.set_slot(
                    Provider.PUBLIC,
                    option.key,
                    reason="public_pool",
                    expires_at=option.expires_at,
                )
            except Exception:
                pass
        return rows_from_options(selected)

    def sync_public_pool(self, pool, *, clear_if_empty: bool = False) -> int:
        """Feed the active pool from catalog-proven options only.

        A failed/empty catalog never clears the pool: the previous endpoints
        stay active until a new usable option exists.
        """

        policy = self.policies.get(Provider.PUBLIC)
        if pool is None and callable(self.pool_getter):
            try:
                pool = self.pool_getter()
            except Exception:
                pool = None
        if policy is None or pool is None:
            return 0
        rows = self.public_rows()
        if not rows:
            if clear_if_empty and hasattr(pool, "clear_public_proxies"):
                try:
                    pool.clear_public_proxies()
                except Exception:
                    pass
            return 0
        cfg = dict(self.pool_cfg)
        return int(
            pool.set_public_proxies(
                rows,
                max_active=int(cfg.get("max_active") or policy.max_active),
                max_per_24=int(cfg.get("max_per_24") or 2),
                isolation=str(cfg.get("isolation") or "process"),
                container_image=str(cfg.get("container_image") or "python:3.12-slim"),
            )
            or 0
        )


def _in_network(value: str, network: Any) -> bool:
    try:
        return ipaddress.ip_address(str(value or "").strip()) in network
    except ValueError:
        return False


def _section(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    return value if isinstance(value, dict) else {}


def _provider_cfg(config: dict[str, Any], provider: str) -> dict[str, Any]:
    providers = _section(_section(config, "egress_catalog"), "providers")
    value = providers.get(provider)
    return value if isinstance(value, dict) else {}


def _enabled(config: dict[str, Any], provider: str) -> bool:
    return bool(_provider_cfg(config, provider).get("enabled", False))


def _warp_containers(config: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _section(config, "ip_rotation").get("socks5_proxies")
    containers: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if str(row.get("provider") or "").lower() != "warp":
            continue
        if not bool(row.get("managed", False)):
            continue
        containers.append(
            {
                "container": str(row.get("container") or "").strip(),
                "egress_ip": str(row.get("egress_ip") or "").strip(),
                "block": str(row.get("block") or "").strip(),
                "enabled": bool(row.get("enabled", True)),
                "healthy": bool(row.get("enabled", True)),
            }
        )
    return containers


def _nordvpn_stations(api_base: str, timeout: float = 10.0) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        f"{api_base.rstrip('/')}/api/vpn/fleet",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        return []
    rows = payload.get("stations") if isinstance(payload, dict) else None
    stations: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip().lower()
        stations.append(
            {
                "station": f"station-{row.get('station')}",
                "server": f"station-{row.get('station')}",
                "egress_ip": str(row.get("ip") or "").strip(),
                "status": status,
                "auth_failed": status in ("auth_failed", "unauthorized"),
            }
        )
    return stations


def _public_sources(provider_cfg: dict[str, Any]) -> list[SourceDefinition]:
    interval = int(provider_cfg.get("discovery_interval_s") or 21_600)
    path = str(provider_cfg.get("sources_file") or "").strip()
    if path:
        rows = load_source_file(path, default_interval=interval)
        if rows:
            return rows
    return parse_source_definitions(
        provider_cfg.get("sources") or [],
        default_interval=interval,
    )


def build_runtime(
    config: dict[str, Any],
    *,
    db_path: str | Path | None = None,
    api_base: str = "http://127.0.0.1:4000",
    sources: list[SourceDefinition] | None = None,
    nordvpn_stations: list[dict[str, Any]] | None = None,
    public_probe=None,
    warp_probe=None,
    nordvpn_probe=None,
    pool_getter=None,
    qualification_scheduler=None,
) -> EgressRuntime | None:
    catalog = _section(config, "egress_catalog")
    if not bool(catalog.get("enabled", False)):
        return None
    target = Path(
        db_path
        or _section(config, "ip_registry").get("registry_db_path")
        or (ROOT / "logs" / "registry.db")
    )
    store = EgressStore(
        target,
        retention_days=float(catalog.get("retention_days") or 7),
    )
    threat_cfg = catalog.get("threat_feeds") if isinstance(catalog.get("threat_feeds"), dict) else {}
    threat_intel = None
    if bool(threat_cfg.get("enabled", False)):
        threat_intel = ThreatIntel(
            store,
            parse_feed_config(threat_cfg.get("urls") or []),
            refresh_interval_s=float(threat_cfg.get("refresh_interval_s") or 21_600),
        )
    providers: dict[str, tuple[Any, ProviderPolicy, float]] = {}
    policies: dict[str, ProviderPolicy] = {}

    if _enabled(config, Provider.PUBLIC):
        policy = ProviderPolicy.from_config(Provider.PUBLIC, _provider_cfg(config, Provider.PUBLIC))
        policies[Provider.PUBLIC] = policy
        source_rows = sources if sources is not None else _public_sources(
            _provider_cfg(config, Provider.PUBLIC)
        )
        for source in source_rows:
            try:
                store.upsert_source(
                    {
                        "id": source.id,
                        "type": source.type,
                        "url": source.url,
                        "scheme": source.scheme,
                        "enabled": source.enabled,
                        "interval_s": source.interval_s,
                        "config": {
                            **source.config,
                            "priority": source.priority,
                            "notes": source.notes,
                            "tags": list(source.tags),
                        },
                    }
                )
            except Exception:
                pass
        adapter = PublicEgressAdapter(
            sources=source_rows,
            probe=public_probe,
            concurrency=policy.concurrency,
            denylist=list(catalog.get("denylist") or []),
            threat_intel=threat_intel,
            sources_file=str(
                _provider_cfg(config, Provider.PUBLIC).get("sources_file") or ""
            ),
        )
        adapter.discovery_interval_s = policy.discovery_interval_s
        adapter.cycle_budget = max(1, int(catalog.get("cycle_candidate_budget") or 500))
        adapter.cycle_timeout_s = max(30.0, float(catalog.get("cycle_timeout_s") or 300))
        providers[Provider.PUBLIC] = (
            adapter,
            policy,
            max(60.0, float(_provider_cfg(config, Provider.PUBLIC).get("revalidate_interval_s") or 1800)),
        )
    if _enabled(config, Provider.WARP):
        policy = ProviderPolicy.from_config(Provider.WARP, _provider_cfg(config, Provider.WARP))
        policies[Provider.WARP] = policy
        providers[Provider.WARP] = (
            WarpEgressAdapter(_warp_containers(config), probe=warp_probe),
            policy,
            max(60.0, float(policy.discovery_interval_s)),
        )
    if _enabled(config, Provider.NORDVPN):
        policy = ProviderPolicy.from_config(Provider.NORDVPN, _provider_cfg(config, Provider.NORDVPN))
        policies[Provider.NORDVPN] = policy
        stations = nordvpn_stations if nordvpn_stations is not None else _nordvpn_stations(api_base)
        max_stations = int(
            _provider_cfg(config, Provider.NORDVPN).get("max_active_stations")
            or policy.max_active
            or 6
        )
        providers[Provider.NORDVPN] = (
            NordVpnEgressAdapter(
                stations,
                probe=nordvpn_probe,
                max_active_stations=max_stations,
            ),
            policy,
            max(60.0, float(policy.discovery_interval_s)),
        )
    if not providers:
        store.close()
        return None
    scheduler = EgressScheduler(
        store,
        providers,
        jitter_pct=float(catalog.get("update_jitter_pct") or 15),
    )
    runtime = EgressRuntime(
        store=store,
        scheduler=scheduler,
        policies=policies,
        pool_cfg=dict(_section(config, "ip_rotation").get("public_proxies") or {}),
        pool_getter=pool_getter,
        threat_intel=threat_intel,
        denylist=list(catalog.get("denylist") or []),
        qualification_scheduler=qualification_scheduler,
    )

    async def _after_run_async(name: str, result) -> None:
        if name == Provider.PUBLIC:
            import asyncio

            await asyncio.to_thread(
                runtime.sync_public_pool,
                None,
                clear_if_empty=not result.errors,
            )

    scheduler.on_run = _after_run_async
    return runtime


__all__ = ["EgressRuntime", "build_runtime"]
