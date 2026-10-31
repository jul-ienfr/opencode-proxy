"""Watchdog premier octet du flux free (P0.2)."""

from __future__ import annotations

import asyncio

import pytest

import opencode as oc


class _SlowUpstream:
    status_code = 200
    headers = {}

    def __init__(self, delay: float = 0.2) -> None:
        self.delay = delay
        self.closed = False

    async def aiter_content(self):
        await asyncio.sleep(self.delay)
        yield b"data: {}\n\n"

    async def aclose(self) -> None:
        self.closed = True


class _Station:
    _station = 1
    current_ip = "203.0.113.10"


def test_no_first_byte_raises_free_ttfb_timeout():
    async def run() -> None:
        wrapper = oc._CurlCffiStreamResponse(_SlowUpstream())
        wrapper._first_byte_timeout_s = 0.05
        wrapper._ttfb_ctx = (_Station(), 0.0, "deepseek-test")
        with pytest.raises(oc._FreeTtfbTimeout):
            async for _ in wrapper.aiter_bytes():
                pass

    asyncio.run(run())


def test_first_chunk_disables_watchdog_for_rest_of_stream():
    class _StallAfterFirst(_SlowUpstream):
        async def aiter_content(self):
            yield b"data: one\n\n"
            await asyncio.sleep(0.2)
            yield b"data: two\n\n"

    async def run() -> None:
        wrapper = oc._CurlCffiStreamResponse(_StallAfterFirst())
        wrapper._first_byte_timeout_s = 0.05
        wrapper._ttfb_ctx = (_Station(), 0.0, "deepseek-test")
        chunks = [chunk async for chunk in wrapper.aiter_bytes()]
        assert chunks == [b"data: one\n\n", b"data: two\n\n"]

    asyncio.run(run())
