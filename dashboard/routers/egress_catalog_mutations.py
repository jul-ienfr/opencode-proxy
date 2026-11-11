"""Egress catalog mutation routes (refresh, threat feeds, block, sources).

Extracted from ``dashboard/api.py`` (slice S10).  The read routes live in
``egress_catalog_routes.py``; this module keeps the mutations.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_egress_catalog_mutations(
    app: FastAPI,
    *,
    catalog_getter: Callable[[], Any],
    token_checker: Callable[[Request], Any],
    denylist_getter: Callable[[], list[str]],
    denylist_setter: Callable[[list[str]], Any],
) -> None:
    @app.post("/api/egress/catalog/refresh")
    async def refresh_egress_catalog(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        provider = str(body.get("provider") or "public").strip().lower()
        runtime = catalog_getter()
        if runtime is None:
            return JSONResponse({"error": "catalogue egress désactivé"}, status_code=503)
        if provider not in runtime.policies:
            return JSONResponse({"error": f"provider inactif: {provider}"}, status_code=400)
        result = await runtime.refresh_now(provider)
        if result is None:
            return JSONResponse({"error": "cycle indisponible"}, status_code=503)
        return JSONResponse(
            {
                "ok": not result.errors,
                "provider": provider,
                "candidates": result.candidates,
                "promoted": result.promoted,
                "quarantined": result.quarantined,
                "dead": result.dead,
                "skipped": result.skipped,
                "errors": result.errors,
            },
            status_code=202,
        )

    @app.post("/api/egress/catalog/threat-feeds/refresh")
    async def refresh_egress_threat_feeds(request: Request):
        err = token_checker(request)
        if err:
            return err
        runtime = catalog_getter()
        threat = getattr(runtime, "threat_intel", None) if runtime is not None else None
        if threat is None:
            return JSONResponse({"error": "threat feeds désactivés"}, status_code=503)
        result = await asyncio.to_thread(threat.refresh_once)
        return {"ok": not result["errors"], **result}

    @app.post("/api/egress/catalog/block")
    async def block_egress_indicator(request: Request):
        try:
            err = token_checker(request)
            if err:
                return err
            runtime = catalog_getter()
            if runtime is None:
                return JSONResponse({"error": "catalogue egress désactivé"}, status_code=503)
            body = await request.json()
            result = await asyncio.to_thread(
                runtime.block_indicator,
                str(body.get("ip") or ""),
            )
            try:
                current = list(denylist_getter() or [])
                if result["ip"] not in current:
                    current.append(result["ip"])
                    denylist_setter(current)
            except Exception as exc:
                return JSONResponse(
                    {"error": f"blocage appliqué mais config non persistée: {exc}", **result},
                    status_code=500,
                )
            try:
                asyncio.create_task(asyncio.to_thread(runtime.sync_public_pool, None))
            except Exception:
                pass
            return {"ok": True, **result}
        except ValueError as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    @app.post("/api/egress/catalog/sources")
    async def upsert_egress_catalog_source(request: Request):
        err = token_checker(request)
        if err:
            return err
        runtime = catalog_getter()
        if runtime is None:
            return JSONResponse({"error": "catalogue egress désactivé"}, status_code=503)
        body = await request.json()
        try:
            from free.egress_sources import SourceDefinition

            SourceDefinition.from_config(body)
            source = runtime.store.upsert_source(body)
        except Exception as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=400)
        return {"ok": True, "source": source}


__all__ = ["register_egress_catalog_mutations"]
