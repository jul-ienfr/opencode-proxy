"""
app.streaming — SSE handlers + StreamingResponse

Extraction de opencode.py: _sse_keepalive, _do_request_with_retry (retry 2, orphan guard),
StreamingResponse wrappers. Pure ASGI, pas de lock.
"""

# Re-export pour compat — l'impl vit encore dans opencode.py
try:
    from opencode import _sse_keepalive as _sse_keepalive
except ImportError:

    async def _sse_keepalive(stream_gen, interval: float = 15.0):
        """Repli sans opencode : pass-through (pas d'injection keepalive).

        Même signature que l'original (mypy variants) ; le `yield from`
        synchrone précédent était de toute façon cassé sur un générateur
        asynchrone (TypeError au premier chunk).
        """
        async for chunk in stream_gen:
            yield chunk


__all__ = ["_sse_keepalive"]
