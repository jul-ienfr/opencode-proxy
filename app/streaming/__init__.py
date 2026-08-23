"""
app.streaming — SSE handlers + StreamingResponse

Extraction de opencode.py: _sse_keepalive, _do_request_with_retry (retry 2, orphan guard),
StreamingResponse wrappers. Pure ASGI, pas de lock.
"""

# Re-export pour compat — l'impl vit encore dans opencode.py
try:
    from opencode import _sse_keepalive as _sse_keepalive
except ImportError:

    def _sse_keepalive(gen):
        yield from gen


__all__ = ["_sse_keepalive"]
