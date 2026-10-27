"""Tests du middleware de retry transparent (retry until success)."""

import asyncio
import json

from streaming.retry_until_success import RetryUntilSuccessMiddleware

SSE = [(b"content-type", b"text/event-stream")]


def _start(status=200, headers=None):
    return {"type": "http.response.start", "status": status, "headers": headers or []}


def _body(payload, more=False):
    return {"type": "http.response.body", "body": payload, "more_body": more}


def _json_error(status, message="rate limited", etype="rate_limit_error"):
    body = json.dumps({"error": {"message": message, "type": etype}}).encode()
    return [
        _start(status, [(b"content-type", b"application/json")]),
        _body(body),
    ]


def _sse(payload, more=True):
    return [_start(200, SSE), _body(payload, more=more)]


def _run(cfg, app, path="/v1/chat/completions", body=b'{"stream":true}', headers=None):
    sent = []
    consumed = {"body": False}
    never = asyncio.Event()

    async def send(message):
        sent.append(message)

    async def receive():
        if not consumed["body"]:
            consumed["body"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await never.wait()
        return {"type": "http.disconnect"}

    middleware = RetryUntilSuccessMiddleware(app, config_getter=lambda: cfg)
    asyncio.run(
        middleware(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": headers or [(b"accept", b"text/event-stream")],
            },
            receive,
            send,
        )
    )
    return sent


def _seq(responses, *, delay=0.0, read_body=False):
    calls = []

    async def app(scope, receive, send):
        index = len(calls)
        calls.append(None)
        if read_body:
            chunks = []
            while True:
                message = await receive()
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    break
            calls[index] = b"".join(chunks)
        if delay:
            await asyncio.sleep(delay)
        response = responses[min(index, len(responses) - 1)]
        for message in response:
            await send(message)

    app.calls = calls
    return app


def _cfg(**overrides):
    cfg = {
        "enabled": True,
        "max_wait_s": 2.0,
        "base_delay_s": 0.01,
        "max_delay_s": 0.01,
        "keepalive_interval_s": 0.01,
        "jitter": 0.0,
    }
    cfg.update(overrides)
    return cfg


def _starts(sent):
    return [m for m in sent if m.get("type") == "http.response.start"]


def _bodies(sent):
    return b"".join(
        m.get("body") or b""
        for m in sent
        if m.get("type") == "http.response.body"
    )


def test_429_then_stream_success_is_transparent():
    app = _seq([_json_error(429), _sse(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n', more=False)])
    sent = _run(_cfg(), app)
    starts = _starts(sent)
    assert starts and starts[0]["status"] == 200
    assert b"hello" in _bodies(sent)
    assert not any(m.get("status") == 429 for m in starts)
    assert len(app.calls) == 2


def test_empty_200_then_stream_success_is_retried():
    app = _seq([[_start(200, SSE), _body(b"")], _sse(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n', more=False)])
    sent = _run(_cfg(), app)
    assert _starts(sent)[0]["status"] == 200
    assert b"ok" in _bodies(sent)
    assert len(app.calls) == 2


def test_first_error_frame_retryable_is_retried():
    frame = b'data: {"error":{"message":"upstream produced no output","type":"api_error"}}\n\n'
    app = _seq([_sse(frame, more=False), _sse(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n', more=False)])
    sent = _run(_cfg(), app)
    assert b"ok" in _bodies(sent)
    assert len(app.calls) == 2


def test_first_error_frame_non_retryable_passes_through():
    frame = b'data: {"error":{"message":"context too long","type":"invalid_request_error"}}\n\n'
    app = _seq([_sse(frame, more=False)])
    sent = _run(_cfg(), app)
    assert _starts(sent)[0]["status"] == 200
    assert frame in _bodies(sent)
    assert len(app.calls) == 1


def test_400_is_not_retried():
    app = _seq([_json_error(400, "bad request", "invalid_request_error")])
    sent = _run(_cfg(), app)
    assert _starts(sent)[0]["status"] == 400
    assert len(app.calls) == 1


def test_local_rate_limit_is_not_retried():
    body = {
        "error": {
            "message": "Rate limit exceeded. Please retry shortly.",
            "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
        }
    }
    raw = json.dumps(body).encode()
    app = _seq([[_start(429, [(b"content-type", b"application/json")]), _body(raw)]])
    sent = _run(_cfg(), app)
    assert _starts(sent)[0]["status"] == 429
    assert len(app.calls) == 1


def test_non_stream_429_then_200_json():
    app = _seq(
        [
            _json_error(429),
            [_start(200, [(b"content-type", b"application/json")]), _body(b'{"ok":true}')],
        ]
    )
    sent = _run(_cfg(), app, body=b'{"stream":false}')
    assert _starts(sent)[0]["status"] == 200
    assert b'{"ok":true}' in _bodies(sent)
    assert len(app.calls) == 2


def test_request_body_is_replayed():
    app = _seq([_json_error(429), _sse(b'data: {"ok":1}\n\n', more=False)], read_body=True)
    sent = _run(_cfg(), app, body=b'{"stream":true,"messages":[{"role":"user","content":"x"}]}')
    assert _starts(sent)[0]["status"] == 200
    assert len(app.calls) == 2
    assert all(call == b'{"stream":true,"messages":[{"role":"user","content":"x"}]}' for call in app.calls)


def test_keepalive_is_sent_while_retrying():
    app = _seq([_json_error(429), _sse(b'data: {"ok":1}\n\n', more=False)])
    sent = _run(_cfg(), app)
    assert b": keepalive" in _bodies(sent)


def test_max_wait_ends_the_stream_with_an_error_event():
    app = _seq([_json_error(429)])
    sent = _run(_cfg(max_wait_s=0.12, base_delay_s=0.05, max_delay_s=0.05), app)
    body = _bodies(sent)
    assert _starts(sent)[0]["status"] == 200
    assert b"error" in body.lower()
    assert len(app.calls) >= 1


def test_disabled_is_a_pure_passthrough():
    app = _seq([_json_error(429)])
    sent = _run(_cfg(enabled=False), app)
    assert _starts(sent)[0]["status"] == 429
    assert len(app.calls) == 1


def test_middleware_is_registered_between_empty_guard_and_deadline():
    """Ordre ASGI : Retry voit le 502 d'EmptyGuard, RequestDeadline borne la boucle."""
    import opencode as oc
    from streaming.deadline import RequestDeadlineMiddleware
    from streaming.empty_guard import EmptyStreamGuardMiddleware
    from streaming.retry_until_success import RetryUntilSuccessMiddleware

    classes = [m.cls for m in oc.app.user_middleware]
    assert RetryUntilSuccessMiddleware in classes
    # `user_middleware` est trié du plus EXTERNE au plus interne :
    # RequestDeadline (0) → Retry (1) → EmptyGuard (2).
    assert classes.index(RetryUntilSuccessMiddleware) < classes.index(EmptyStreamGuardMiddleware)
    assert classes.index(RetryUntilSuccessMiddleware) > classes.index(RequestDeadlineMiddleware)


def test_client_disconnect_cancels_retry():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def app(scope, receive, send):
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    calls = {"n": 0}

    async def receive():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"type": "http.request", "body": b'{"stream":true}', "more_body": False}
        return {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    middleware = RetryUntilSuccessMiddleware(app, config_getter=lambda: _cfg())
    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []},
            receive,
            send,
        )
    )
    assert started.is_set()
    assert cancelled.is_set()
    assert not sent
