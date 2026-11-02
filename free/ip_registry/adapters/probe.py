"""Real station probe adapter (official free body, SOCKS5, curl_cffi).

The probe only reads the status line/headers and closes the stream.  It never
consumes the full SSE body, so a slow model cannot block the allocator.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..domain import ProbeResult

ENDPOINT = "https://opencode.ai/inference/openai/v1/chat/completions"
PAYLOAD_PATH = Path(__file__).resolve().parent.parent.parent / "official_payload.json"

# Official client versions with their measured TLS/Bun profiles. The probe
# picks one deterministically per egress IP so it does not look like a single
# frozen installation, while staying a profile a real client can emit.
PROBE_VERSIONS = ("1.19.5", "1.19.3", "1.18.31", "1.19.0")


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
        self._body, self._body_raw = self._load_payload()

    async def probe(self, station: int, model: str) -> ProbeResult:
        tested_at = self._clock()
        egress_ip = ""
        if self._egress is not None:
            try:
                egress_ip = str(await self._egress(station) or "").strip()
            except Exception:
                egress_ip = ""
        content = self._wire_bytes(model)
        version = self._version_for(egress_ip)
        headers = self._headers(egress_ip, version)
        last_error = "no-attempt"
        for scheme in ("socks5h", "socks5"):
            proxy = f"{scheme}://127.0.0.1:{1079 + int(station)}"
            started = time.monotonic()
            try:
                resp = await asyncio.to_thread(
                    self._post,
                    ENDPOINT,
                    headers=headers,
                    content=content,
                    proxies={"http": proxy, "https": proxy},
                    timeout=self._timeout_s,
                    stream=True,
                    fp_version=version,
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
    def _load_payload() -> tuple[dict, str]:
        with open(PAYLOAD_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        body = data.get("body") if isinstance(data, dict) else data
        body = dict(body or {})
        raw = ""
        if isinstance(data, dict):
            candidate = data.get("_body_raw")
            expected = str(data.get("_body_sha256") or "")
            if isinstance(candidate, str) and candidate:
                actual = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
                if not expected or actual == expected:
                    raw = candidate
        return body, raw

    def _wire_bytes(self, model: str) -> bytes:
        """Exact official bytes; only the model string is substituted.

        The raw body is the byte-exact client capture (compact UTF-8 JSON).
        Re-serializing it - even with orjson - is unnecessary and risks
        whitespace escaping differences. Fall back to orjson only when the
        capture has no raw body.
        """
        old = str(self._body.get("model") or "")
        if self._body_raw and model != old:
            needle = f'"model":{json.dumps(old, ensure_ascii=False)}'
            replacement = f'"model":{json.dumps(model, ensure_ascii=False)}'
            if needle in self._body_raw:
                return self._body_raw.replace(needle, replacement, 1).encode("utf-8")
        if self._body_raw:
            return self._body_raw.encode("utf-8")
        import opencode as oc

        body = dict(self._body)
        body["model"] = model
        return oc._serialize_json_body(body)

    @staticmethod
    def _version_for(egress_ip: str) -> str:
        """Deterministic official profile per egress IP.

        The egress IP is the stable key: the same station keeps its client
        version across probes, while different stations can legitimately look
        like different official installations.
        """
        if not egress_ip:
            return "1.19.5"
        from free.identities import VERSION_SPECS, version_for

        # Prefer the identity the general free path would actually use for
        # this egress (persisted version / forced env), so test and production
        # never pick different official profiles for the same IP.
        try:
            import opencode as oc

            vault_factory = getattr(oc, "_ip_vault", None)
            if callable(vault_factory):
                ident = vault_factory().get(str(egress_ip))
                version = str(getattr(ident, "version", "") or "")
                if version in VERSION_SPECS:
                    return version
        except Exception:
            pass
        return version_for(str(egress_ip))

    @staticmethod
    def _headers(egress_ip: str, version: str = "1.19.5") -> dict:
        # Lazy imports: the probe adapter must not drag the whole app at import.
        from free.identity_generator import generate_profile

        # Mirror the general free path's official project derivation: the
        # client sends ``global`` outside a git repo and the 40-hex project ID
        # inside one. The gate does not read this header, but a faithful copy
        # must not invent a third value.
        try:
            import opencode as oc

            base_project = str(getattr(oc, "_OPENCODE_PROJECT", "") or "global")
        except Exception:
            base_project = "global"
        profile = generate_profile(
            version=version,
            client="desktop",
            ip=egress_ip or "probe",
            project_base=base_project,
            generation=0,
            endpoint=ENDPOINT,
        )
        return dict(profile["headers"])

    @staticmethod
    def _curl_post(url: str, **kwargs: Any) -> Any:
        from curl_cffi import requests as curl_requests

        import opencode as oc

        # curl_cffi's ``json=`` serializes with indent=2 (request.py), while
        # the official Bun client emits compact UTF-8 JSON. The measured
        # capture is 46,010 bytes; send the same orjson bytes as the real
        # free leg (``_serialize_json_body``), never a re-indented body.
        body = kwargs.pop("json", None)
        if body is not None and "content" not in kwargs:
            kwargs["content"] = oc._serialize_json_body(body)
        version = str(kwargs.pop("fp_version", "1.19.5") or "1.19.5")
        impersonate, fp = oc._get_free_fp_kwargs(version)
        if impersonate:
            kwargs["impersonate"] = impersonate
        if fp:
            kwargs.update(fp)
        return curl_requests.post(url, **kwargs)
