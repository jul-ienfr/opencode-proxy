"""Real station probe adapter (official free body, SOCKS5, curl_cffi).

The probe only reads the status line/headers and closes the stream.  It never
consumes the full SSE body, so a slow model cannot block the allocator.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..domain import ProbeResult

ENDPOINT = "https://opencode.ai/inference/openai/v1/chat/completions"
PAYLOAD_PATH = Path(__file__).resolve().parent.parent.parent / "official_payload.json"


class StationProbe:
    def __init__(
        self,
        *,
        post: Callable[..., Any] | None = None,
        egress: Callable[[int], Awaitable[str]] | None = None,
        timeout_s: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._post = post or self._curl_post
        self._egress = egress
        self._timeout_s = max(1.0, float(timeout_s))
        self._clock = clock
        self._body = self._load_body()

    async def probe(self, station: int, model: str) -> ProbeResult:
        tested_at = self._clock()
        egress_ip = ""
        if self._egress is not None:
            try:
                egress_ip = str(await self._egress(station) or "").strip()
            except Exception:
                egress_ip = ""
        body = dict(self._body)
        body["model"] = model
        headers = self._headers(egress_ip)
        last_error = "no-attempt"
        for scheme in ("socks5h", "socks5"):
            proxy = f"{scheme}://127.0.0.1:{1079 + int(station)}"
            started = time.monotonic()
            try:
                resp = await asyncio.to_thread(
                    self._post,
                    ENDPOINT,
                    headers=headers,
                    json=body,
                    proxies={"http": proxy, "https": proxy},
                    timeout=self._timeout_s,
                    stream=True,
                )
                status = int(getattr(resp, "status_code", 0) or 0)
                try:
                    retry_after = float(
                        (getattr(resp, "headers", {}) or {}).get("retry-after") or 0
                    )
                except (TypeError, ValueError):
                    retry_after = 0.0
                try:
                    resp.close()
                except Exception:
                    pass
                return ProbeResult(
                    status=status,
                    tested_at=tested_at,
                    retry_after_s=max(0.0, retry_after),
                    latency_ms=(time.monotonic() - started) * 1000.0,
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        return ProbeResult(status=0, tested_at=tested_at, error=last_error[:200])

    @staticmethod
    def _load_body() -> dict:
        with open(PAYLOAD_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        body = data.get("body") if isinstance(data, dict) else data
        return dict(body or {})

    @staticmethod
    def _headers(egress_ip: str) -> dict:
        # Lazy imports: the probe adapter must not drag the whole app at import.
        from free.identities import IpIdentity

        identity = IpIdentity(
            f"probe-{egress_ip or 'unknown'}",
            base_project="global",
            forced_client="desktop",
            forced_version="1.19.5",
            generation=1,
            persist=False,
        )
        headers = identity.official_provider_headers(ENDPOINT, None)
        headers.update(
            {
                "Connection": "keep-alive",
                "Accept": "*/*",
                "Host": "opencode.ai",
                "Accept-Encoding": "gzip, deflate, br, zstd",
            }
        )
        return headers

    @staticmethod
    def _curl_post(url: str, **kwargs: Any) -> Any:
        from curl_cffi import requests as curl_requests

        import opencode as oc

        impersonate, fp = oc._get_free_fp_kwargs("1.19.5")
        if impersonate:
            kwargs["impersonate"] = impersonate
        if fp:
            kwargs.update(fp)
        return curl_requests.post(url, **kwargs)
