"""StationProbe tests (no network)."""

from __future__ import annotations

from free.ip_registry.adapters.probe import StationProbe


class FakeResponse:
    def __init__(self, status: int, headers: dict | None = None):
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakePost:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def test_probe_200_uses_official_headers():
    response = FakeResponse(200)
    post = FakePost([response])
    probe = StationProbe(post=post, egress=lambda station: _async("87.120.102.53"))

    result = await probe.probe(6, "deepseek-v4.1-flash-free")

    assert result.status == 200
    assert result.ok
    assert result.latency_ms is not None
    assert response.closed
    kwargs = post.calls[0]
    assert kwargs["headers"]["Authorization"] == "Bearer public"
    assert kwargs["json"]["model"] == "deepseek-v4.1-flash-free"
    assert kwargs["stream"] is True
    assert "socks5h://127.0.0.1:1085" in kwargs["proxies"]["https"]


async def test_probe_429_reads_retry_after():
    post = FakePost([FakeResponse(429, {"retry-after": "3600"})])
    probe = StationProbe(post=post)

    result = await probe.probe(6, "m")

    assert result.status == 429
    assert result.retry_after_s == 3600.0
    assert result.is_quota_429


async def test_probe_falls_back_to_local_dns():
    post = FakePost([RuntimeError("remote dns failed"), FakeResponse(200)])
    probe = StationProbe(post=post)

    result = await probe.probe(2, "m")

    assert result.status == 200
    assert len(post.calls) == 2
    assert "socks5h://" in post.calls[0]["proxies"]["https"]
    assert "socks5://" in post.calls[1]["proxies"]["https"]


async def test_probe_returns_error_when_both_fail():
    post = FakePost([RuntimeError("a"), RuntimeError("b")])
    probe = StationProbe(post=post)

    result = await probe.probe(1, "m")

    assert result.status == 0
    assert result.error is not None
    assert "RuntimeError" in result.error


async def _async(value: str) -> str:
    return value
