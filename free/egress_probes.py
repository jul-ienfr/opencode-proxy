"""Free-gate probes for already-connected WARP / NordVPN tunnels.

Public candidates use the isolated worker (``egress_adapters.public``).  WARP
and NordVPN are managed local tunnels: probing them directly is trusted and
does not expose secrets.  The wire remains the production free gate.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from free.egress_validation import ProbeOutcome, parse_retry_after


async def probe_proxy_free_gate(
    proxy_url: str,
    *,
    provider: str,
    egress_ip: str = "",
    label: str = "",
    model: str = "deepseek-v4.1-flash-free",
    timeout: float = 20.0,
    requester=None,
) -> ProbeOutcome:
    """Send the real free ``ping`` through one managed tunnel."""

    if not proxy_url:
        return ProbeOutcome(status=0, error="proxy_url vide")
    if requester is None:
        import opencode as oc

        requester = oc._do_free_request_curl_cffi
        endpoint = oc.API_BASE_FREE
    else:
        endpoint = "https://opencode.ai/zen/v1/chat/completions"
    station = SimpleNamespace(
        pid=f"catalog-{provider}",
        socks5_url=proxy_url,
        provider=str(provider),
        managed=True,
        container=f"catalog-{provider}",
        scheme="socks5",
        label=str(label or f"{provider}:catalog"),
        current_ip=str(egress_ip or ""),
        egress_ip=str(egress_ip or ""),
        ipv6=(":" in str(egress_ip or "")),
    )
    started = time.monotonic()
    try:
        response = await requester(
            {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
            },
            {},
            proxy_url=proxy_url,
            station=station,
            endpoint=endpoint,
            timeout=(15.0, 60.0),
        )
    except Exception as exc:
        return ProbeOutcome(
            status=0,
            latency_ms=(time.monotonic() - started) * 1000.0,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )
    status = int(getattr(response, "status_code", 0) or 0)
    headers = dict(getattr(response, "headers", None) or {})
    body = str(getattr(response, "text", "") or "")
    close = getattr(response, "aclose", None)
    if callable(close):
        try:
            await close()
        except Exception:
            pass
    return ProbeOutcome(
        status=status,
        latency_ms=(time.monotonic() - started) * 1000.0,
        body=body[:2_000],
        headers=headers,
        error="" if status else "réponse vide",
    )


def reset_from_outcome(outcome: ProbeOutcome, *, now: float | None = None) -> float:
    return parse_retry_after(
        outcome.headers,
        outcome.body,
        now=now,
        retry_after_s=float(outcome.retry_after_s or 0.0),
    )


__all__ = ["probe_proxy_free_gate", "reset_from_outcome"]
