"""test_sse_keepalive.py — regression tests for _sse_keepalive (opencode.py).

The keepalive wrapper must inject `: ping` comments during idle periods
WITHOUT killing the upstream read. The old implementation used
`asyncio.wait_for(anext(stream_gen), timeout=interval)`: on a silence
longer than the interval, wait_for CANCELS the pending anext, and the
CancelledError thrown into the inner generator kills the upstream stream —
the stream then ended with EOF and no terminal SSE event (a stall >15s
looked like a dropped connection, and the abrupt teardown is a classic RST
source). The fix races the read task against a ping task and never cancels
the read on timeout.

Test 1 (test_stall_is_bridged_not_killed) is the regression: with the old
code the data chunk after the stall never arrives.
"""
import asyncio

import pytest

import opencode as oc


async def _consume(gen):
    """Collect all chunks from the wrapped generator (with a watchdog)."""
    out = []
    async for chunk in gen:
        out.append(chunk)
    return out


async def _gen_that_stalls(stall, then):
    """Yield b"first", sleep `stall`, yield `then`, then stop."""
    yield b"first"
    await asyncio.sleep(stall)
    yield then


class TestKeepalive:
    @pytest.mark.asyncio
    async def test_normal_chunks_pass_through(self):
        async def gen():
            yield b"a"
            yield b"b"

        out = await _consume(oc._sse_keepalive(gen(), interval=60.0))
        assert out == [b"a", b"b"]

    @pytest.mark.asyncio
    async def test_ping_injected_between_chunks(self):
        """A ping bridges the idle gap between two chunks: pings appear
        strictly between 'a' and 'b', never before or after.

        Semantic form, not an exact list — gap 0.15 (>=9 ticks) vs interval
        0.05 (>=3 ticks) leaves a 0.10 margin between the first ping deadline
        and the chunk wakeup, so load can't flip the order; under load a
        second ping may fire, and the invariant that matters is the bridge.
        """
        async def gen():
            yield b"a"
            await asyncio.sleep(0.15)
            yield b"b"

        out = await _consume(oc._sse_keepalive(gen(), interval=0.05))
        assert out[0] == b"a" and out[-1] == b"b"
        assert b": ping\n\n" in out
        assert all(c == b": ping\n\n" for c in out[1:-1])

    @pytest.mark.asyncio
    async def test_stall_is_bridged_not_killed(self):
        """REG.RESSION: a silence longer than `interval` must NOT kill the stream.

        Old behaviour: the pending anext was cancelled at 0.05s → the inner
        generator died → the stream ended after the ping, `b"late"` never
        arrived. New behaviour: the ping fires, the read keeps waiting, and
        the chunk arrives once the upstream wakes up.
        """
        out = await _consume(oc._sse_keepalive(
            _gen_that_stalls(0.10, b"late"), interval=0.05))
        assert out == [b"first", b": ping\n\n", b"late"]

    @pytest.mark.asyncio
    async def test_multiple_pings_then_data(self):
        """A long stall (10x interval) yields repeated pings, then the data.

        Margins are wide on purpose: Windows event-loop timers quantize to
        ~16 ms ticks, so interval-vs-stall gaps under one tick are ties that
        flip run-to-run. interval 0.02 (>=1 tick) vs stall 0.20 (>=12 ticks)
        leaves the ping count far from the assertion boundary.
        """
        out = await _consume(oc._sse_keepalive(
            _gen_that_stalls(0.20, b"late"), interval=0.02))
        pings = [c for c in out if c == b": ping\n\n"]
        assert len(pings) >= 3
        assert out[-1] == b"late"

    @pytest.mark.asyncio
    async def test_chunk_after_ping_resets_timer(self):
        """After a ping, a fresh silence window starts (no double pings).

        Gap 0.09 (>=5 ticks) vs interval 0.05 (>=3 ticks) — the ping always
        fires before the chunk; gap 0.02 (1 tick) is always under the
        interval, so it can never produce a second ping.
        """
        async def gen():
            yield b"a"
            await asyncio.sleep(0.09)   # one ping
            yield b"b"
            await asyncio.sleep(0.02)   # under interval — no ping
            yield b"c"

        out = await _consume(oc._sse_keepalive(gen(), interval=0.05))
        assert out == [b"a", b": ping\n\n", b"b", b"c"]

    @pytest.mark.asyncio
    async def test_stop_async_iteration_ends_stream(self):
        async def gen():
            yield b"only"

        out = await _consume(oc._sse_keepalive(gen(), interval=0.01))
        assert out == [b"only"]

    @pytest.mark.asyncio
    async def test_upstream_error_ends_gracefully(self):
        """An upstream exception is logged and the stream ends (no raise)."""
        async def gen():
            yield b"a"
            raise RuntimeError("upstream exploded")

        out = await _consume(oc._sse_keepalive(gen(), interval=60.0))
        assert out == [b"a"]

    @pytest.mark.asyncio
    async def test_cancel_closes_pending_read(self):
        """Generator teardown (client gone) cancels the pending read task."""
        got_cancelled = asyncio.Event()

        async def gen():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                got_cancelled.set()
                raise
            if False:
                yield  # make this an async GENERATOR (not a coroutine)

        wrapped = oc._sse_keepalive(gen(), interval=60.0)
        it = wrapped.__aiter__()
        started = asyncio.create_task(it.__anext__())  # pulls the read into read_task
        await asyncio.sleep(0)  # wrapper is now inside wait(), read pending
        # Simulate the client disconnecting: cancel the streaming task. The
        # CancelledError hits the wrapper at its wait → finally cancels the
        # pending read task → delivered next iteration → inner gen gets
        # CancelledError. (aclose() from outside is illegal here: the wrapper
        # is "already running" inside `started`.)
        started.cancel()
        await asyncio.wait_for(got_cancelled.wait(), 1.0)
        assert got_cancelled.is_set()
        with pytest.raises(asyncio.CancelledError):
            await started
