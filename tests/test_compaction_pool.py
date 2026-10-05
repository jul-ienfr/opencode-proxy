"""test_compaction_pool.py — P1-6 : jambe pool du résumeur (DI pure).

``app.compaction.pool`` extrait de ``opencode.py`` : mêmes contrats, zérodépendance projet — les I/O et erreurs sont injectés.
"""

import pytest

from app.compaction.pool import free_pool_request, summarizer_dispatch


class _UpstreamError(Exception):
    def __init__(self, message="", status_code=502, original=None):
        super().__init__(message)
        self.status_code = status_code
        self.original = original


class _Refusal(Exception):
    pass


def _deps(**kw):
    base = {
        "try_free_fn": None,
        "refusal_types": (_Refusal, _UpstreamError),
        "upstream_error_cls": _UpstreamError,
    }
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_no_model_key_raises_502():
    with pytest.raises(_UpstreamError) as ei:
        await free_pool_request({}, {}, "openai", "", **_deps(try_free_fn=_boom_free))
    assert ei.value.status_code == 502


async def _boom_free(*a, **k):  # pragma: no cover
    raise AssertionError("must not be called without model key")


@pytest.mark.asyncio
async def test_model_key_from_body():
    seen = {}

    async def _ok(body, headers, protocol, key, *, forced_pool=None, req_id=None):
        seen.update(body=body, key=key)
        return ("resp", {"h": "1"}, "m", "ip")

    out = await free_pool_request({"model": "mimo-free"}, {}, "openai", "", **_deps(try_free_fn=_ok))
    assert out == ("resp", {"h": "1"})
    assert seen["key"] == "mimo-free"


@pytest.mark.asyncio
async def test_refusal_wrapped_502():
    async def _refuse(*a, **k):
        raise _Refusal("quota")

    with pytest.raises(_UpstreamError) as ei:
        await free_pool_request({"model": "m"}, {}, "openai", "m", **_deps(try_free_fn=_refuse))
    assert ei.value.status_code == 502


@pytest.mark.asyncio
async def test_none_result_exhausted():
    async def _none(*a, **k):
        return None

    with pytest.raises(_UpstreamError):
        await free_pool_request({"model": "m"}, {}, "openai", "m", **_deps(try_free_fn=_none))


@pytest.mark.asyncio
async def test_unexpected_error_wrapped_not_leaked():
    async def _weird(*a, **k):
        raise ValueError("x")

    with pytest.raises(_UpstreamError):
        await free_pool_request({"model": "m"}, {}, "openai", "m", **_deps(try_free_fn=_weird))


@pytest.mark.asyncio
async def test_dispatch_free_uses_pool_not_paid():
    calls = []

    async def _pool(wire_body, wire_headers, protocol, seed, *, forced_pool=None, req_id=None):
        calls.append("pool")
        return ("r", {})

    async def _paid(*a, **k):  # pragma: no cover
        calls.append("paid")
        raise AssertionError("paid must not be called on free path")

    async def _transport(is_free, *a, **k):
        assert is_free is True
        return await k["do_free_fn"]("ep", {"model": "m"}, {})

    out = await summarizer_dispatch(
        True, "ep", {"model": "m"}, {}, "openai",
        seed="m", free_pool_fn=_pool, do_paid_fn=_paid,
        do_free_direct_fn=_paid, transport_request_fn=_transport,
        chat_to_responses_fn=None, normalize_response_fn=None,
    )
    assert out == ("r", {})
    assert calls == ["pool"]


@pytest.mark.asyncio
async def test_dispatch_paid_uses_paid_not_pool():
    calls = []

    async def _pool(*a, **k):  # pragma: no cover
        raise AssertionError("pool must not be called on paid path")

    async def _paid(*a, **k):
        calls.append("paid")
        return ("r", {})

    async def _transport(is_free, *a, **k):
        assert is_free is False
        return await k["do_paid_fn"]()

    out = await summarizer_dispatch(
        False, "ep", {"model": "m"}, {}, "openai",
        free_pool_fn=_pool, do_paid_fn=_paid,
        do_free_direct_fn=_paid, transport_request_fn=_transport,
        chat_to_responses_fn=None, normalize_response_fn=None,
    )
    assert out == ("r", {})
    assert calls == ["paid"]
