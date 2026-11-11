"""Free-gate probe for managed WARP/NordVPN tunnels (no public worker)."""

from __future__ import annotations

import pytest

from free.egress_probes import probe_proxy_free_gate


class _Response:
    def __init__(self, status: int, *, headers=None, text: str = ""):
        self.status_code = status
        self.headers = headers or {}
        self.text = text

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_200_is_a_usable_outcome():
    async def requester(body, headers, **kwargs):
        assert body["messages"][0]["content"] == "ping"
        assert kwargs["proxy_url"] == "socks5://127.0.0.1:1080"
        return _Response(200)

    outcome = await probe_proxy_free_gate(
        "socks5://127.0.0.1:1080",
        provider="warp",
        requester=requester,
    )
    assert outcome.status == 200
    assert outcome.error == ""


@pytest.mark.asyncio
async def test_429_keeps_the_reset_header():
    async def requester(body, headers, **kwargs):
        return _Response(429, headers={"Retry-After": "120"})

    outcome = await probe_proxy_free_gate(
        "socks5://127.0.0.1:1080",
        provider="nordvpn",
        requester=requester,
    )
    assert outcome.status == 429
    assert outcome.headers["Retry-After"] == "120"


@pytest.mark.asyncio
async def test_exception_is_a_dead_outcome_not_a_crash():
    async def requester(body, headers, **kwargs):
        raise TimeoutError("tunnel down")

    outcome = await probe_proxy_free_gate(
        "socks5://127.0.0.1:1080",
        provider="warp",
        requester=requester,
    )
    assert outcome.status == 0
    assert "TimeoutError" in outcome.error
