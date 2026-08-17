"""
Tests for the Wireshark-like traffic capture (traffic_capture.py).

Covers the frame lifecycle: capture on request start, raw header/body
teeing, truncation under body_cap, ring eviction under max_frames and
max_bytes, toggle enable/disable, skip-prefix filtering, stats
aggregation, and the two RST classification paths (http.disconnect
before the response, and a send() failure while streaming).

Happy paths run through a real Starlette app via TestClient; the abort
paths need raw ASGI control, so those feed the middleware a fake scope
directly.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from traffic_capture import (
    TrafficCapture,
    TrafficCaptureMiddleware,
    hex_dump,
)


# ── helpers ───────────────────────────────────────────────────────

async def _echo_route(request):
    """Echo back the request body + metadata so tests can verify bytes."""
    body = await request.body()
    resp = JSONResponse({
        "method": request.method,
        "path": request.url.path,
        "query": request.url.query,
        "body_len": len(body),
    })
    resp.headers["X-Echo"] = "yes"
    return resp


def _make_app(cap):
    app = Starlette(routes=[Route("/v1/messages", _echo_route, methods=["POST"]),
                            Route("/v1/messages", _echo_route, methods=["GET"]),
                            Route("/health", _echo_route)])
    return TrafficCaptureMiddleware(app, cap)


def _new_cap(**kwargs):
    cap = TrafficCapture()
    cap.configure(**kwargs)
    return cap


def _post(text: str, **kwargs):
    """POST JSON to the echo route through the capture middleware."""
    cap = kwargs.pop("cap", None)
    if cap is None:
        cap = _new_cap()
    app = _make_app(cap)
    with TestClient(app) as client:
        r = client.post("/v1/messages", content=text.encode(),
                        headers={"Content-Type": "application/json",
                                 "X-Custom": "token123"})
    return cap, r.text


# ── happy path ────────────────────────────────────────────────────

class TestCaptureHappyPath:
    def test_frame_captured_with_raw_meta(self):
        cap = _new_cap()
        app = _make_app(cap)
        payload = b'{"model":"mimo-v2.5","messages":[{"role":"user","content":"hi"}]}'
        with TestClient(app) as client:
            r = client.post("/v1/messages?foo=bar&baz=1", content=payload,
                            headers={"Content-Type": "application/json",
                                     "X-Custom": "token123"})
        assert r.status_code == 200
        assert cap.status()["frames"] == 1
        f = cap.frames()[0]
        assert f["method"] == "POST"
        assert f["path"] == "/v1/messages"
        assert f["query"] == "foo=bar&baz=1"
        assert f["request_line"].startswith("POST /v1/messages?foo=bar&baz=1 HTTP/")
        assert f["status"] == 200
        assert f["aborted"] is False
        assert f["body_len"] == len(payload)
        assert f["truncated"] is False
        assert f["duration_ms"] >= 0
        assert f["delta_ms"] >= 0
        assert f["client_ip"] == "testclient"

    def test_detail_has_headers_and_hex_bytes(self):
        cap = _new_cap()
        payload = b'{"a":"b"}'
        cap, _ = _post(payload.decode(), cap=cap)
        detail = cap.frame_detail(1)
        assert detail is not None
        headers = {h["name"]: h["value"] for h in detail["headers"]}
        assert headers["content-type"] == "application/json"
        assert headers["x-custom"] == "token123"
        # Body hex: first row must contain the raw bytes of the payload.
        assert detail["body_hex"]
        joined = "".join(r["hex"].replace(" ", "")
                         for r in detail["body_hex"])
        assert payload.hex() in joined
        assert "".join(r["ascii"] for r in detail["body_hex"]).startswith('{"a":"b"}')

    def test_get_request_captured(self):
        cap = _new_cap()
        app = _make_app(cap)
        with TestClient(app) as client:
            client.get("/v1/messages")
        f = cap.frames()[0]
        assert f["method"] == "GET"
        assert f["body_len"] == 0
        assert f["status"] == 200

    def test_delta_ms_advances_between_requests(self):
        cap = _new_cap()
        app = _make_app(cap)
        with TestClient(app) as client:
            for _ in range(3):
                client.get("/v1/messages")
        frames = cap.frames(limit=10)
        assert len(frames) == 3
        # Newest-first: frame 3 first, frame 1 last.
        assert [f["id"] for f in frames] == [3, 2, 1]
        # Inter-arrival tempo is a non-negative float for every frame.
        assert all(f["delta_ms"] >= 0 for f in frames)

    def test_timestamp_fields_present(self):
        cap, _ = _post("x", cap=_new_cap())
        f = cap.frames()[0]
        assert isinstance(f["time"], str) and f["time"]
        assert 0 <= f["time_ms"] <= 999


# ── truncation ────────────────────────────────────────────────────

class TestTruncation:
    def test_body_over_cap_is_truncated_but_counted(self):
        cap = _new_cap(body_cap=1024)
        payload = b"y" * 5000
        cap, _ = _post(payload.decode(), cap=cap)
        f = cap.frames()[0]
        assert f["body_len"] == 5000          # full wire count preserved
        assert f["truncated"] is True
        detail = cap.frame_detail(f["id"])
        # Stored dump is capped to body_cap bytes.
        stored = b"".join(bytes.fromhex(r["hex"].replace(" ", ""))
                          for r in detail["body_hex"])
        assert len(stored) == 1024
        assert stored == b"y" * 1024

    def test_body_under_cap_untouched(self):
        cap = _new_cap(body_cap=4096)
        payload = b"small body"
        cap, _ = _post(payload.decode(), cap=cap)
        f = cap.frames()[0]
        assert f["truncated"] is False
        assert f["body_len"] == len(payload)
        detail = cap.frame_detail(f["id"])
        assert len(detail["body_hex"]) == 1  # one row, 10 bytes


# ── ring eviction ─────────────────────────────────────────────────

class TestEviction:
    def test_max_frames_ring(self):
        cap = _new_cap(max_frames=10)
        app = _make_app(cap)
        with TestClient(app) as client:
            for i in range(15):
                client.post("/v1/messages", content=f"req-{i}".encode())
        status = cap.status()
        assert status["frames"] == 10
        assert status["counter"] == 15
        frames = cap.frames(limit=100)
        # Ring keeps the 10 newest; ids 6..15.
        assert [f["id"] for f in frames] == list(range(15, 5, -1))
        # Evicted ids are gone from the detail map.
        assert cap.frame_detail(1) is None
        assert cap.frame_detail(15) is not None

    def test_max_bytes_evicts_oldest(self):
        cap = _new_cap(body_cap=1024, max_bytes=4096)
        app = _make_app(cap)
        with TestClient(app) as client:
            for i in range(6):
                client.post("/v1/messages", content=(b"x" * 1024))
        status = cap.status()
        # 6x1KB = 6144 over budget → oldest evicted until <= 4096 (4 frames).
        assert status["frames"] == 4
        assert status["bytes_stored"] <= 4096
        frames = cap.frames(limit=100)
        assert [f["id"] for f in frames] == [6, 5, 4, 3]
        assert cap.frame_detail(1) is None
        # Bytes accounting is exact after eviction.
        assert status["bytes_stored"] == 4 * 1024

    def test_clear_empties_ring(self):
        cap = _new_cap()
        app = _make_app(cap)
        with TestClient(app) as client:
            client.get("/v1/messages")
        assert cap.clear() == 1
        status = cap.status()
        assert status["frames"] == 0
        assert status["bytes_stored"] == 0
        assert cap.frames() == []


# ── toggle & skip ─────────────────────────────────────────────────

class TestToggleAndSkip:
    def test_configure_enabled_false_passes_through(self):
        cap = _new_cap(enabled=False)
        app = _make_app(cap)
        with TestClient(app) as client:
            r = client.post("/v1/messages", content=b'{"x":1}')
        assert r.status_code == 200
        assert cap.frames() == []

        # Re-enable → capture resumes.
        cap.configure(enabled=True)
        with TestClient(app) as client:
            client.get("/v1/messages")
        assert len(cap.frames()) == 1

    def test_health_and_traffic_paths_skipped(self):
        cap = _new_cap()
        app = _make_app(cap)
        starlette = Starlette(routes=[Route("/health", _echo_route),
                                      Route("/api/traffic/status", _echo_route),
                                      Route("/static/app.js", _echo_route),
                                      Route("/v1/messages", _echo_route)])
        wrapped = TrafficCaptureMiddleware(starlette, cap)
        with TestClient(wrapped) as client:
            client.get("/health")
            client.get("/api/traffic/status")
            client.get("/static/app.js")
            client.get("/v1/messages")
        frames = cap.frames()
        assert len(frames) == 1
        assert frames[0]["path"] == "/v1/messages"


# ── stats ─────────────────────────────────────────────────────────

class TestStats:
    def test_aggregates_methods_paths_statuses(self):
        cap = _new_cap()
        app = _make_app(cap)
        with TestClient(app) as client:
            for _ in range(2):
                client.post("/v1/messages", content=b'{"a":1}')
            client.get("/v1/messages")
        s = cap.stats(window=60.0)
        assert s["frames"] == 3
        assert s["methods"] == {"POST": 2, "GET": 1}
        assert s["statuses"] == {"200": 3}
        assert s["paths_top"] == [{"path": "/v1/messages", "count": 3}]
        assert s["aborted"] == 0
        assert s["in_flight"] == 0
        assert s["tempo_delta_ms"]["n"] == 3
        assert s["duration_ms"]["n"] == 3

    def test_empty_stats(self):
        cap = _new_cap()
        s = cap.stats(window=60.0)
        assert s["frames"] == 0
        assert s["rps"] == 0.0
        assert s["tempo_delta_ms"] is None


# ── abort / RST classification (raw ASGI) ─────────────────────────

def _scope(**kw):
    scope = {
        "type": "http",
        "http_version": "1.1",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "scheme": "http",
        "path": "/v1/messages",
        "raw_path": b"/v1/messages",
        "query_string": b"x=1",
        "headers": [(b"content-type", b"application/json"),
                    (b"x-token", b"abc")],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 4000),
    }
    scope.update(kw)
    return scope


def _request_stream(body: bytes = b""):
    """A receive() that delivers one http.request then a disconnect."""
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


class TestAbortClassification:
    @pytest.mark.asyncio
    async def test_http_disconnect_before_response(self):
        """Client closes the request stream before any response → RST before response."""
        cap = TrafficCapture()

        async def app(scope, receive, send):
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return

        mw = TrafficCaptureMiddleware(app, cap)
        await mw(_scope(), _request_stream(b"partial"), lambda m: None)

        f = cap.frames()[0]
        assert f["status"] is None
        assert f["aborted"] is True
        assert f["abort_reason"] == "client closed mid-request (RST before response)"
        # The partial body is still teed into the frame.
        assert f["body_len"] == 7

    @pytest.mark.asyncio
    async def test_disconnect_after_499_status(self):
        """App answers 499 then the client stream dies → RST → 499 classification."""
        cap = TrafficCapture()
        sent = []

        async def send(message):
            sent.append(message)

        async def app(scope, receive, send):
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    break
            await send({"type": "http.response.start", "status": 499, "headers": []})
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        mw = TrafficCaptureMiddleware(app, cap)
        await mw(_scope(), _request_stream(b""), send)

        f = cap.frames()[0]
        assert f["status"] == 499
        assert f["aborted"] is True
        assert f["abort_reason"] == "client disconnected (RST → 499)"

    @pytest.mark.asyncio
    async def test_send_failure_while_streaming(self):
        """send() raises after the response started → RST while streaming."""
        cap = TrafficCapture()
        started = []

        class _PipeBroken(Exception):
            pass

        async def send(message):
            if message["type"] == "http.response.start":
                started.append(message)
            raise _PipeBroken("connection reset by peer")

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        mw = TrafficCaptureMiddleware(app, cap)
        with pytest.raises(_PipeBroken):
            await mw(_scope(), _request_stream(b""), send)

        f = cap.frames()[0]
        assert f["status"] == 200
        assert f["aborted"] is True
        assert f["abort_reason"] == "client reset while response streaming (RST)"
        assert f["ttfb_ms"] is not None

    @pytest.mark.asyncio
    async def test_normal_request_not_aborted(self):
        """Happy-path raw ASGI run: no abort flags, status recorded."""
        cap = TrafficCapture()
        sent = []

        async def send(message):
            sent.append(message)

        async def app(scope, receive, send):
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}", "more_body": False})

        mw = TrafficCaptureMiddleware(app, cap)
        await mw(_scope(), _request_stream(b'{"m":1}'), send)

        f = cap.frames()[0]
        assert f["status"] == 200
        assert f["aborted"] is False
        assert f["abort_reason"] is None
        assert f["body_len"] == 7


# ── hex_dump ──────────────────────────────────────────────────────

class TestHexDump:
    def test_empty(self):
        assert hex_dump(b"") == []

    def test_single_row_padding(self):
        rows = hex_dump(b"AB\x00\xff")
        assert len(rows) == 1
        assert rows[0]["offset"] == 0
        assert rows[0]["len"] == 4
        # Groups are ljust-padded to full 16-byte width for alignment.
        assert rows[0]["hex"].rstrip() == "4142 00ff"
        assert len(rows[0]["hex"]) == 16 * 3 - 1
        assert rows[0]["ascii"] == "AB.."

    def test_multi_row_offsets(self):
        rows = hex_dump(b"0123456789abcdef" + b"wxyz")
        assert len(rows) == 2
        assert rows[0]["offset"] == 0
        assert rows[0]["len"] == 16
        assert rows[1]["offset"] == 16
        assert rows[1]["len"] == 4
        assert rows[0]["ascii"] == "0123456789abcdef"
        assert rows[1]["ascii"] == "wxyz"

    def test_hex_groups_16_bytes(self):
        rows = hex_dump(b"0123456789abcdef")
        groups = rows[0]["hex"].split()
        assert groups == ["3031", "3233", "3435", "3637", "3839", "6162", "6364", "6566"]
        assert rows[0]["ascii"] == "0123456789abcdef"