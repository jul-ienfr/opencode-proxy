"""Sonde active légère par station — alimente `free.quality`.

Contrat : un GET statique via le SOCKS5 de la station, jamais l'endpoint
d'inférence, donc zéro quota. Le GET utilise le replay TLS/headers officiel
(1.19.5) pour ne pas être refusé sur la seule signature réseau. La sonde ne
décide pas de la santé du tunnel : elle fournit une mesure de latence et un
signal de vivacité.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from free.quality import QualityConfig, get_quality_registry


def _probe_url() -> str:
    try:
        from config.settings import API_BASE_FREE, yaml_get

        configured = str(yaml_get("free_prewarm", "url", "") or "").strip()
        if configured:
            return configured
        from urllib.parse import urlsplit

        parts = urlsplit(str(API_BASE_FREE or ""))
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}/robots.txt"
    except Exception:
        pass
    return "https://opencode.ai/robots.txt"


async def probe_station(station: Any, *, url: str | None = None, timeout: float = 5.0) -> dict:
    """Une sonde bornée. Retourne {ok, ms, station, ip, error}."""
    sid = int(getattr(station, "_station", -1))
    ip = str(getattr(station, "current_ip", "") or "unknown")
    proxy = getattr(station, "socks5_url", None)
    target = url or _probe_url()
    if not proxy:
        return {"ok": False, "ms": None, "station": sid, "ip": ip, "error": "no_proxy"}
    started = time.perf_counter()

    def _get() -> Any:
        from curl_cffi import requests as curl_requests

        proxy_url = str(proxy)
        if proxy_url.startswith("socks5://"):
            proxy_url = "socks5h://" + proxy_url[len("socks5://") :]
        kwargs: dict[str, Any] = {
            "proxies": {"http": proxy_url, "https": proxy_url},
            "timeout": max(1.0, float(timeout)),
        }
        # Same Bun/TLS replay as the inference leg: a plain httpx GET is
        # fingerprinted and can fail for the wrong reason. The static
        # robots.txt target keeps this probe at zero inference quota.
        try:
            import opencode as oc

            impersonate, fp = oc._get_free_fp_kwargs("1.19.5")
            if impersonate:
                kwargs["impersonate"] = impersonate
            if fp:
                kwargs.update(fp)
        except Exception:
            pass
        return curl_requests.get(target, **kwargs)

    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(_get), timeout=max(1.0, float(timeout))
        )
        ms = (time.perf_counter() - started) * 1000.0
        status = int(getattr(resp, "status_code", 0) or 0)
        ok = 200 <= status < 400
        return {
            "ok": ok,
            "ms": round(ms, 1),
            "station": sid,
            "ip": ip,
            "error": None if ok else f"http_{status or '?'}",
        }
    except Exception as e:
        return {
            "ok": False,
            "ms": round((time.perf_counter() - started) * 1000.0, 1),
            "station": sid,
            "ip": ip,
            "error": type(e).__name__,
        }


async def probe_once(
    managers: list[Any],
    *,
    cfg: QualityConfig | None = None,
    concurrency: int = 2,
    url: str | None = None,
) -> list[dict]:
    cfg = cfg or QualityConfig.from_yaml()
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    registry = get_quality_registry()
    target = url or _probe_url()

    async def _one(m):
        async with sem:
            res = await probe_station(m, url=target, timeout=cfg.probe_timeout_s)
            try:
                registry.observe_probe(
                    int(res["station"]),
                    str(res["ip"]),
                    float(res["ms"] or cfg.probe_timeout_s * 1000.0),
                    bool(res["ok"]),
                )
            except Exception:
                pass
            return res

    return list(await asyncio.gather(*(_one(m) for m in managers if m is not None), return_exceptions=False))


__all__ = ["probe_once", "probe_station"]
