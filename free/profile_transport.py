"""Transport curl_cffi pour la vérification de profils.

Le transport est volontairement mince : il ne décide pas si un profil est
valide, il envoie la requête et renvoie `VerificationResponse`. Le
`IdentityProfileSender` et `ProfileVerifier` gardent la classification et les
règles P0.5.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from free.profile_live import VerificationRequest, VerificationResponse


@dataclass
class CurlCffiTransport:
    proxies: dict[str, str | None]
    impersonate: str = "chrome131"
    ja3: str = ""
    sigalgs: list[str] = field(default_factory=list)
    header_order: str | list[str] = ""
    http_version: str = "v1"
    timeout_s: float = 60.0
    ipv6: bool = False

    def __call__(self, request: VerificationRequest, block: str) -> VerificationResponse:
        if block not in self.proxies:
            raise KeyError(f"bloc sans proxy: {block}")
        from curl_cffi import CurlOpt
        from curl_cffi import requests as curl_requests

        extra_fp: dict[str, Any] = {
            "tls_signature_algorithms": list(self.sigalgs),
            "tls_grease": False,
        }
        if self.header_order:
            extra_fp["header_order"] = (
                self.header_order
                if isinstance(self.header_order, str)
                else ",".join(self.header_order)
            )
        kwargs: dict[str, Any] = {
            "proxy": self.proxies[block],
            "impersonate": self.impersonate,
            "ja3": self.ja3,
            "extra_fp": extra_fp,
            "http_version": self.http_version,
            "default_headers": False,
            "timeout": self.timeout_s,
        }
        if self.ipv6:
            kwargs["curl_options"] = {CurlOpt.IPRESOLVE: 2}
        started = time.monotonic()
        with curl_requests.Session(**kwargs) as session:
            response = session.post(
                request.endpoint,
                content=json.dumps(request.body, ensure_ascii=False).encode(),
                headers=request.headers,
                timeout=self.timeout_s,
            )
        elapsed_ms = int(max(0.0, (time.monotonic() - started) * 1000.0))
        text = str(getattr(response, "text", "") or "")
        return VerificationResponse(
            status=int(getattr(response, "status_code", 0) or 0),
            content=text,
            error_frame="event: error" in text,
            latency_ms=elapsed_ms,
        )


__all__ = ["CurlCffiTransport"]
