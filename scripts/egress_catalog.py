#!/usr/bin/env python3
"""CLI du catalogue d'egress.

Exemples :
    python scripts/egress_catalog.py refresh --provider public --max-candidates 50
    python scripts/egress_catalog.py refresh --provider warp --observe-only
    python scripts/egress_catalog.py refresh --provider nordvpn
    python scripts/egress_catalog.py verify
    python scripts/egress_catalog.py export
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from free.egress_adapters.nordvpn import NordVpnEgressAdapter  # noqa: E402
from free.egress_adapters.public import PublicEgressAdapter  # noqa: E402
from free.egress_adapters.warp import WarpEgressAdapter  # noqa: E402
from free.egress_catalog import Provider, ProviderPolicy  # noqa: E402
from free.egress_sources import SourceDefinition, load_source_file, parse_source_definitions  # noqa: E402
from free.egress_store import EgressStore  # noqa: E402
from free.threat_intel import ThreatIntel, parse_feed_config  # noqa: E402


def _load_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    return value if isinstance(value, dict) else {}


def _provider_cfg(cfg: dict[str, Any], provider: str) -> dict[str, Any]:
    section = cfg.get("egress_catalog") if isinstance(cfg.get("egress_catalog"), dict) else {}
    providers = section.get("providers") if isinstance(section.get("providers"), dict) else {}
    raw = providers.get(provider) if isinstance(providers.get(provider), dict) else {}
    return dict(raw)


def _public_sources(cfg: dict[str, Any]) -> list[SourceDefinition]:
    provider = _provider_cfg(cfg, Provider.PUBLIC)
    interval = int(provider.get("discovery_interval_s") or 21_600)
    path = str(provider.get("sources_file") or "").strip()
    if path:
        rows = load_source_file(path, default_interval=interval)
        if rows:
            return rows
    rows = provider.get("sources")
    return parse_source_definitions(
        rows if isinstance(rows, list) else [],
        default_interval=interval,
    )


def _warp_containers(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    section = cfg.get("ip_rotation") if isinstance(cfg.get("ip_rotation"), dict) else {}
    rows = section.get("socks5_proxies")
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


def _fetch_json(url: str, timeout: float = 10.0) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _nordvpn_stations(api_base: str) -> list[dict[str, Any]]:
    payload = _fetch_json(f"{api_base.rstrip('/')}/api/vpn/fleet")
    rows = payload.get("stations") if isinstance(payload, dict) else None
    stations: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        stations.append(
            {
                "station": f"station-{row.get('station')}",
                "server": f"station-{row.get('station')}",
                "egress_ip": str(row.get("ip") or "").strip(),
                "status": str(row.get("status") or "").strip().lower(),
                "auth_failed": str(row.get("status") or "").strip().lower()
                in ("auth_failed", "unauthorized"),
            }
        )
    return stations


def _build_store(args) -> EgressStore:
    return EgressStore(args.db, retention_days=float(args.retention_days))


def _threat_intel(store: EgressStore, cfg: dict[str, Any]) -> ThreatIntel | None:
    section = cfg.get("egress_catalog") if isinstance(cfg.get("egress_catalog"), dict) else {}
    raw = section.get("threat_feeds") if isinstance(section.get("threat_feeds"), dict) else {}
    if not bool(raw.get("enabled", False)):
        return None
    return ThreatIntel(
        store,
        parse_feed_config(raw.get("urls") or []),
        refresh_interval_s=float(raw.get("refresh_interval_s") or 21_600),
    )


def _check_sources(cfg: dict[str, Any], *, timeout: float = 20.0) -> list[dict[str, Any]]:
    from free.egress_sources import parse_payload, read_source_text

    rows: list[dict[str, Any]] = []
    for source in _public_sources(cfg):
        if not source.enabled:
            rows.append({"id": source.id, "enabled": False, "count": 0})
            continue
        try:
            text = read_source_text(source, timeout=timeout)
            parsed = parse_payload(source, text)
            rows.append(
                {
                    "id": source.id,
                    "enabled": True,
                    "type": source.type,
                    "scheme": source.scheme,
                    "count": len(parsed),
                    "ok": True,
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "id": source.id,
                    "enabled": True,
                    "type": source.type,
                    "scheme": source.scheme,
                    "count": 0,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                }
            )
    return rows


def _seed_sources(store: EgressStore, cfg: dict[str, Any]) -> None:
    for source in _public_sources(cfg):
        try:
            store.upsert_source(
                {
                    "id": source.id,
                    "type": source.type,
                    "url": source.url,
                    "scheme": source.scheme,
                    "enabled": source.enabled,
                    "interval_s": source.interval_s,
                    "config": source.config,
                }
            )
        except Exception:
            pass


def _adapter(args, cfg: dict[str, Any], *, threat: ThreatIntel | None = None):
    provider = str(args.provider or "").lower()
    if provider == Provider.PUBLIC:
        policy = ProviderPolicy.from_config(Provider.PUBLIC, _provider_cfg(cfg, Provider.PUBLIC))
        adapter = PublicEgressAdapter(
            sources=_public_sources(cfg),
            concurrency=policy.concurrency,
            denylist=list((cfg.get("egress_catalog") or {}).get("denylist") or []),
            threat_intel=threat,
        )
        return adapter, policy
    if provider == Provider.WARP:
        policy = ProviderPolicy.from_config(Provider.WARP, _provider_cfg(cfg, Provider.WARP))
        return WarpEgressAdapter(_warp_containers(cfg)), policy
    if provider == Provider.NORDVPN:
        policy = ProviderPolicy.from_config(Provider.NORDVPN, _provider_cfg(cfg, Provider.NORDVPN))
        stations = _nordvpn_stations(args.api_base)
        return (
            NordVpnEgressAdapter(
                stations,
                max_active_stations=int(policy.max_active or 6),
            ),
            policy,
        )
    raise SystemExit(f"provider inconnu: {provider!r}")


async def _refresh(args, cfg: dict[str, Any]) -> dict[str, Any]:
    store = _build_store(args)
    try:
        _seed_sources(store, cfg)
        threat = None
        if str(args.provider or "").lower() == Provider.PUBLIC:
            threat = _threat_intel(store, cfg)
            if threat is not None:
                await asyncio.to_thread(threat.refresh_once)
        adapter, policy = _adapter(args, cfg, threat=threat)
        run_id = store.start_run(adapter.provider)
        try:
            result = await adapter.refresh(
                store,
                policy,
                run_id=run_id,
                max_candidates=args.max_candidates,
                observe_only=bool(args.observe_only),
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:300]
            store.finish_run(run_id, status="failed", errors=[message])
            return {"ok": False, "provider": adapter.provider, "error": message}
        store.finish_run(
            run_id,
            status="partial" if result.errors else "ok",
            candidates=result.candidates,
            promoted=result.promoted,
            quarantined=result.quarantined,
            dead=result.dead,
            errors=result.errors,
        )
        return {
            "ok": True,
            "provider": result.provider,
            "candidates": result.candidates,
            "promoted": result.promoted,
            "quarantined": result.quarantined,
            "dead": result.dead,
            "skipped": result.skipped,
            "errors": result.errors,
            "run_id": run_id,
        }
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("refresh", "export", "verify", "sources", "sources-check", "threat-feeds"))
    parser.add_argument("--provider", default=Provider.PUBLIC, choices=[p.value for p in Provider])
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--db", default=str(ROOT / "logs" / "registry.db"))
    parser.add_argument("--output", default=str(ROOT / "logs" / "egress_catalog.json"))
    parser.add_argument("--api-base", default="http://127.0.0.1:4000")
    parser.add_argument("--max-candidates", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retention-days", type=float, default=7.0)
    parser.add_argument("--observe-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    cfg = _load_config(args.config)

    if args.command == "sources-check":
        rows = _check_sources(cfg, timeout=float(args.timeout))
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for row in rows:
                status = "OK" if row.get("ok", True) else "ERR"
                print(f"{status:3} {row['id']:22} {row.get('scheme') or '-':7} {row.get('count', 0):>7} {row.get('error', '')}")
        return 0 if all(row.get("ok", True) for row in rows) else 1

    if args.command == "refresh":
        payload = asyncio.run(_refresh(args, cfg))
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("ok") else 1

    store = _build_store(args)
    try:
        _seed_sources(store, cfg)
        if args.command == "threat-feeds":
            threat = _threat_intel(store, cfg)
            if threat is None:
                print(json.dumps({"ok": False, "error": "threat_feeds désactivé"}, ensure_ascii=False))
                return 1
            result = threat.refresh_once()
            print(json.dumps({"ok": not result["errors"], **result}, ensure_ascii=False, indent=2))
            return 0 if not result["errors"] else 1
        if args.command == "export":
            target = store.export_json(args.output)
            print(json.dumps({"ok": True, "path": str(target)}, ensure_ascii=False))
            return 0
        if args.command == "sources":
            rows = store.list_sources()
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        threat = _threat_intel(store, cfg)
        payload = {
            "providers": store.summary(),
            "runs": store.list_runs(limit=20),
            "sources": store.list_sources(),
            "threat_intel": threat.stats() if threat else None,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
