"""Read-only status for the IP registry (dashboard / monitoring).

The endpoint must answer even when the registry is disabled or its DB does not
exist yet: that is precisely the state the operator needs to see.  It never
fetches a catalog and never writes.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import ROOT, load_registry_config
from .sources.registry import build_default_registry


def _resolve(path_value: str | None, default: str) -> Path:
    path = Path(path_value or default)
    return path if path.is_absolute() else ROOT / path


def _db_summary(db_path: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "exists": db_path.exists(),
        "schema_version": None,
        "servers": {},
        "sources": {},
        "stale": None,
        "snapshots": [],
        "ip_state": {},
    }
    if not db_path.exists():
        return summary
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return summary
    try:
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        target_schema = "egress_ips" in tables and "leases" in tables
        if target_schema:
            row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            summary["schema_version"] = int(row["v"]) if row and row["v"] else None
            total = conn.execute("SELECT COUNT(*) AS n FROM servers").fetchone()["n"]
            stale = conn.execute(
                "SELECT COUNT(*) AS n FROM servers WHERE stale=1"
            ).fetchone()["n"]
            summary["servers"] = {
                "total": total,
                "active": total - stale,
                "stale": stale,
            }
            summary["sources"] = {
                str(r["source_id"]): int(r["n"])
                for r in conn.execute(
                    "SELECT source_id, COUNT(*) AS n FROM servers "
                    "GROUP BY source_id ORDER BY source_id"
                )
            }
            summary["snapshots"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT source_id, fetched_at, stale, server_count, error "
                    "FROM catalog_sources ORDER BY fetched_at DESC LIMIT 5"
                )
            ]
            summary["stale"] = (
                bool(summary["snapshots"][0]["stale"]) if summary["snapshots"] else None
            )
            summary["ip_state"] = {
                str(r["state"]): int(r["n"])
                for r in conn.execute(
                    "SELECT state, COUNT(*) AS n FROM egress_ips "
                    "GROUP BY state ORDER BY state"
                )
            }
            summary["observations"] = conn.execute(
                "SELECT COUNT(*) AS n FROM observations"
            ).fetchone()["n"]
            summary["open_assignments"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT l.station, l.ip AS egress_ip, l.hostname, "
                    "l.started_at AS assigned_at "
                    "FROM leases l WHERE l.status='active' ORDER BY l.station"
                )
            ]
            summary["assignments_open"] = len(summary["open_assignments"])
            return summary
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        summary["schema_version"] = int(row["value"]) if row else None
        summary["servers"] = {
            "total": conn.execute("SELECT COUNT(*) AS n FROM servers").fetchone()["n"],
            "active": conn.execute(
                "SELECT COUNT(*) AS n FROM servers WHERE active=1"
            ).fetchone()["n"],
            "stale": conn.execute(
                "SELECT COUNT(*) AS n FROM servers WHERE stale=1"
            ).fetchone()["n"],
        }
        summary["sources"] = {
            str(r["source_id"]): int(r["n"])
            for r in conn.execute(
                "SELECT source_id, COUNT(*) AS n FROM servers "
                "WHERE active=1 GROUP BY source_id ORDER BY source_id"
            )
        }
        summary["snapshots"] = [
            dict(r)
            for r in conn.execute(
                "SELECT id, source_id, fetched_at, etag, stale, server_count, error "
                "FROM catalog_snapshots ORDER BY id DESC LIMIT 5"
            )
        ]
        summary["stale"] = (
            bool(summary["snapshots"][0]["stale"]) if summary["snapshots"] else None
        )
        summary["ip_state"] = {
            str(r["state"]): int(r["n"])
            for r in conn.execute(
                "SELECT state, COUNT(*) AS n FROM ip_state GROUP BY state ORDER BY state"
            )
        }
        summary["observations"] = conn.execute(
            "SELECT COUNT(*) AS n FROM observations"
        ).fetchone()["n"]
        summary["open_assignments"] = [
            dict(r)
            for r in conn.execute(
                "SELECT station, egress_ip, hostname, assigned_at "
                "FROM assignments WHERE released_at IS NULL ORDER BY station"
            )
        ]
        summary["assignments_open"] = len(summary["open_assignments"])
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return summary


def _quality_summary() -> dict[str, Any] | None:
    try:
        from free.quality import get_quality_registry

        registry = get_quality_registry()
        items = list(registry._items.values())
        hosts = {item.server_host for item in items if item.server_host}
        return {
            "entries": len(items),
            "hosts": len(hosts),
            "tps_samples": sum(int(item.tps_n) for item in items),
            "with_host": sum(1 for item in items if item.server_host),
        }
    except Exception:
        return None


def _host_attribution() -> dict[str, Any] | None:
    try:
        from free.host_attribution import snapshot

        return snapshot()
    except Exception:
        return None


def _claims_summary() -> dict[str, Any]:
    path = ROOT / "logs" / "registry.db"
    if not path.exists():
        return {"exists": False}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        return {"exists": True, "error": f"{type(exc).__name__}: {exc}"}
    try:
        catalog = conn.execute("SELECT COUNT(*) AS n FROM catalog").fetchone()["n"]
        states = {
            str(r["status"]): int(r["count"])
            for r in conn.execute(
                "SELECT status, COUNT(*) AS count FROM claims GROUP BY status"
            )
        }
        next_reset = None
        row = conn.execute(
            "SELECT MIN(reset_at) AS ts FROM claims "
            "WHERE status='429' AND reset_at > ?",
            (time.time(),),
        ).fetchone()
        if row is not None and row["ts"] is not None:
            next_reset = float(row["ts"])
        return {
            "exists": True,
            "catalog": catalog,
            "claims": states,
            "next_reset_at": next_reset,
            "next_reset_in_s": (
                round(max(0.0, next_reset - time.time()), 1) if next_reset else None
            ),
        }
    except Exception as exc:
        return {"exists": True, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


def _allocator_summary(cfg: dict[str, Any]) -> dict[str, Any]:
    """Chain/winners/claims view: who is repairing what, and with which IP."""

    chains: list[dict[str, Any]] = []
    now = time.time()
    try:
        from config.settings import yaml_get

        station_count = max(
            1, int(yaml_get("ip_rotation", "station_count", 6) or 6)
        )
    except Exception:
        station_count = 6
    for station in range(1, station_count + 1):
        locks = sorted((ROOT / "logs").glob(f"fresh_chain_{station}*.lock"))
        lock = next(
            (item for item in locks if item.stem.endswith("_auto")),
            locks[0] if locks else None,
        )
        pid = 0
        if lock is not None:
            try:
                data = json.loads(lock.read_text(encoding="utf-8"))
                pid = int(data.get("pid") or 0)
            except Exception:
                pid = 0
        state: dict[str, Any] = {}
        transport = "auto"
        for suffix, label in (
            ("_auto", "auto"),
            ("", "wireguard"),
            ("_openvpn-udp", "openvpn-udp"),
            ("_openvpn-tcp", "openvpn-tcp"),
        ):
            try:
                from free.chain_state import load_state as _load_chain_state

                candidate = _load_chain_state(
                    ROOT / "logs" / f"fresh_chain_state_{station}{suffix}.json"
                )
            except Exception:
                candidate = {}
            if candidate.get("results") or candidate.get("winner"):
                state = candidate
                transport = label
                break
        results = state.get("results") or []
        last = results[-1] if results else {}
        chains.append(
            {
                "station": station,
                "transport": transport,
                "pid": pid or None,
                "running": bool(
                    pid and lock is not None and now - lock.stat().st_mtime < 3600.0
                ),
                "age_s": (
                    round(now - lock.stat().st_mtime, 1) if lock is not None else None
                ),
                "tested": len(results),
                "last_status": last.get("status"),
                "last_host": last.get("host"),
                "reason": last.get("error") or last.get("status"),
                "winner": state.get("winner"),
            }
        )
    alloc = cfg.get("allocator") if isinstance(cfg.get("allocator"), dict) else {}
    pause: dict[str, Any] = {}
    try:
        from free.chain_state import load_json_state

        pause = load_json_state(
            ROOT / "logs" / "allocator_state.json", default={}
        ) or {}
    except Exception:
        pause = {}
    return {
        "mode": str(alloc.get("mode") or "off"),
        "max_parallel_repairs": int(alloc.get("max_parallel_repairs", 0) or 0),
        "rebind_limit_per_min": int(alloc.get("rebind_limit_per_min", 0) or 0),
        "pause": pause,
        "chains": chains,
        "claims": _claims_summary(),
    }


def registry_status(
    config: dict[str, Any] | None = None,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the full observe-mode picture for the IP registry."""

    cfg = config if isinstance(config, dict) else load_registry_config()
    sources_cfg = cfg.get("sources") if isinstance(cfg.get("sources"), dict) else {}
    path = Path(db_path) if db_path is not None else _resolve(
        cfg.get("registry_db_path") or cfg.get("db_path"), "logs/registry.db"
    )

    registry = None
    hierarchy_ids: set[str] = set()
    try:
        registry = build_default_registry(
            manifest_path=_resolve(
                sources_cfg.get("manifest"), "docs/nordvpn_sources.yaml"
            ),
            extra_path=_resolve(
                sources_cfg.get("embedded_path"),
                "logs/gluetun_nordvpn_embedded.json",
            ),
            cache_path=_resolve(
                sources_cfg.get("cache_path"), "logs/nordvpn_api.json"
            ),
            gluetun_manifest_path=_resolve(
                sources_cfg.get("gluetun_manifest_path"),
                "logs/gluetun_manifest.json",
            ),
        )
        capabilities = registry.capabilities()
        manifest = registry.manifest
        hierarchy_ids = set(registry.catalog_order()) | set(
            registry.completeness_order()
        )
    except Exception:
        capabilities = {}
        manifest = None

    sources = []
    for source_id, adapter_ok in sorted(capabilities.items()):
        definition = manifest.definition(source_id) if manifest is not None else None
        # Le statut répond à « d'où vient le catalogue ? » : ne pas noyer
        # l'opérateur sous les sources réseau/outils sans adaptateur.
        if definition is not None and definition.kind not in {
            "catalog",
            "catalog_mirror",
            "catalog_frontend",
            "config_archive",
        }:
            continue
        if hierarchy_ids and source_id not in hierarchy_ids:
            continue
        sources.append(
            {
                "id": source_id,
                "adapter": bool(adapter_ok),
                "category": definition.category if definition else None,
                "kind": definition.kind if definition else None,
                "status": definition.status if definition else None,
            }
        )

    incidents: list[dict[str, Any]] = []
    try:
        from observability.incidents import open_incidents

        incidents = open_incidents(path=path, limit=50)
    except Exception:
        incidents = []
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "enabled": bool(cfg.get("enabled", False)),
        "allocator_mode": str((cfg.get("allocator") or {}).get("mode") or "off"),
        "binder_mode": str((cfg.get("binder") or {}).get("mode") or ""),
        "db_path": str(path),
        "db": _db_summary(path),
        "sources": sources,
        "no_adapter": [item["id"] for item in sources if not item["adapter"]],
        "allocator": _allocator_summary(cfg),
        "incidents": incidents,
        "host_attribution": _host_attribution(),
        "quality": _quality_summary(),
    }


__all__ = ["registry_status"]
