"""End-to-end wire test: the real curl_cffi sender must emit the 2.2.5 order.

The unit tests build a header dict; libcurl can still reorder or append
headers. This test captures the bytes on the wire through a local HTTP server
and locks the exact 16-header order of the official 2.2.5 capture.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import opencode as oc
from free.identities import IdentityVault

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_ORDER = [
    "Authorization",
    "Content-Type",
    "User-Agent",
    "b3",
    "traceparent",
    "x-opencode-client",
    "x-opencode-project",
    "x-opencode-session",
    "x-opencode-session-id",
    "x-session-affinity",
    "x-session-id",
    "Connection",
    "Accept",
    "Host",
    "Accept-Encoding",
    "Content-Length",
]

_CAPTURED: list[dict] = []


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        _CAPTURED.append(
            {
                "path": self.path,
                "headers": list(self.headers.raw_items()),
                "body": body,
            }
        )
        payload = json.dumps(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        return


class _Station:
    current_ip = "203.0.113.88"


def _official_body() -> dict:
    return json.loads((ROOT / "free" / "official_payload.json").read_text(encoding="utf-8"))["body"]


def test_real_wire_order_is_225(monkeypatch, tmp_path):
    _CAPTURED.clear()
    vault = IdentityVault(
        base_project="global",
        forced_client="cli",
        forced_version="1.19.5",
    )
    monkeypatch.setattr(oc, "_ip_vault", lambda: vault)
    monkeypatch.setattr(oc, "_FREE_WIRE_STYLE", "official")
    monkeypatch.setattr(oc, "_OPENCODE_PROJECT", "global")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
    try:
        body = _official_body()

        async def _run():
            return await oc._do_free_request_curl_cffi(
                body,
                {},
                proxy_url=None,
                station=_Station(),
                endpoint=url,
            )

        response = asyncio.run(_run())
        assert response.status_code == 200
    finally:
        server.shutdown()

    assert len(_CAPTURED) == 1
    captured = _CAPTURED[0]
    assert [name for name, _ in captured["headers"]] == OFFICIAL_ORDER
    lower = {name.lower(): value for name, value in captured["headers"]}
    assert lower["user-agent"] == "opencode/latest/2.2.5/cli"
    assert lower["x-opencode-client"] == "cli"
    assert lower["x-opencode-project"] == "global"
    parsed = json.loads(captured["body"])
    assert list(parsed) == [
        "model",
        "messages",
        "tools",
        "stream",
        "stream_options",
        "max_completion_tokens",
    ]
    assert len(parsed["tools"]) == 13
