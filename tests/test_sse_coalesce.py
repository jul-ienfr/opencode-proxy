"""[B3 audit vitesse] _sse_coalesce — micro-batch SSE non-bloquant.

Contrats :
  - les chunks disponibles dans le MÊME tick scheduler sont émis groupés
    (un seul send ASGI pour un burst upstream) ;
  - AUCUNE attente ajoutée : un chunk qui n'arrive qu'après un vrai wait
    est émis séparément (zéro latence, pas de bufferisation) ;
  - frames SSE complètes → concaténation transparente ;
  - fin de stream (StopAsyncIteration) et erreurs upstream terminent
    proprement après flush du groupe.
"""

import asyncio

import pytest

import opencode as oc


async def _consume(gen):
    out = []
    async for chunk in gen:
        out.append(chunk)
    return out


class TestCoalesce:
    @pytest.mark.asyncio
    async def test_burst_in_one_tick_is_merged(self):
        """Deux yields synchrones (même tick) → UN seul bytes groupé."""

        async def burst():
            yield b"data: a\n\n"
            yield b"data: b\n\n"

        out = await _consume(oc._sse_coalesce(burst()))
        assert out == [b"data: a\n\ndata: b\n\n"]

    @pytest.mark.asyncio
    async def test_real_gap_never_merges(self):
        """Un chunk séparé par un vrai wait réseau reste isolé : zéro
        latence ajoutée (pas de bufferisation au-delà du tick drain)."""

        async def spaced():
            yield b"one"
            await asyncio.sleep(0.05)
            yield b"two"
            await asyncio.sleep(0.05)
            yield b"three"

        out = await _consume(oc._sse_coalesce(spaced()))
        assert out == [b"one", b"two", b"three"]

    @pytest.mark.asyncio
    async def test_passthrough_non_bytes(self):
        async def mixed():
            yield "str-chunk"
            yield b"bytes"

        out = await _consume(oc._sse_coalesce(mixed()))
        assert out == ["str-chunk", b"bytes"]

    @pytest.mark.asyncio
    async def test_stop_async_iteration_flushes_then_ends(self):
        async def gen():
            yield b"x"
            yield b"y"

        out = await _consume(oc._sse_coalesce(gen()))
        assert out == [b"xy"]

    @pytest.mark.asyncio
    async def test_upstream_error_ends_after_flush(self):
        async def gen():
            yield b"ok-part"
            raise RuntimeError("boom")

        out = await _consume(oc._sse_coalesce(gen()))
        assert out == [b"ok-part"]

    @pytest.mark.asyncio
    async def test_composes_with_keepalive(self):
        """Pipeline production : keepalive(coalesce(gen)) — la fusion opère
        À LA SOURCE (bursts du générateur de conversion), les pings restent
        injectés par le wrapper externe, ordre stable."""

        async def gen():
            yield b"a"
            yield b"b"
            await asyncio.sleep(0.12)
            yield b"c"

        out = await _consume(oc._sse_keepalive(oc._sse_coalesce(gen()), interval=0.04))
        assert out[0] == b"ab", f"burst groupé attendu, got {out!r}"
        assert out[-1] == b"c"
        assert b": ping\n\n" in out
