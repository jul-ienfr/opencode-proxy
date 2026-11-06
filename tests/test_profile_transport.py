"""Transport curl_cffi — testé avec un faux Session, sans réseau."""

from __future__ import annotations

import pytest

from free.profile_live import VerificationRequest
from free.profile_transport import CurlCffiTransport


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "pong") -> None:
        self.status_code = status_code
        self.text = text


class FakeSession:
    instances: list[FakeSession] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.posts: list[dict] = []
        FakeSession.instances.append(self)

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *args) -> None:
        return None

    def post(self, url: str, **kwargs) -> FakeResponse:
        self.posts.append({"url": url, **kwargs})
        return FakeResponse(200, "pong")


@pytest.fixture(autouse=True)
def _reset_instances():
    FakeSession.instances.clear()
    yield
    FakeSession.instances.clear()


def _request() -> VerificationRequest:
    return VerificationRequest(
        endpoint="https://example.test/v1/chat",
        headers={"Authorization": "Bearer public", "x-opencode-project": "a" * 40},
        body={"model": "deepseek-v4.1-flash-free", "stream": True},
    )


def test_transport_uses_profile_tls_and_proxy(monkeypatch):
    import curl_cffi.requests as curl_requests

    monkeypatch.setattr(curl_requests, "Session", FakeSession)
    transport = CurlCffiTransport(
        proxies={"block-a": "socks5h://127.0.0.1:11080"},
        ja3="771,4865",
        sigalgs=["rsa_pss_rsae_sha256"],
        header_order=["Authorization", "Content-Type", "User-Agent"],
    )
    response = transport(_request(), "block-a")
    assert response.status == 200
    assert response.content == "pong"
    session = FakeSession.instances[0]
    assert session.kwargs["proxy"] == "socks5h://127.0.0.1:11080"
    assert session.kwargs["impersonate"] == "chrome131"
    assert session.kwargs["ja3"] == "771,4865"
    assert session.kwargs["extra_fp"]["header_order"] == (
        "Authorization,Content-Type,User-Agent"
    )
    assert session.kwargs["default_headers"] is False
    assert session.posts[0]["url"] == "https://example.test/v1/chat"
    assert session.posts[0]["content"] == b'{"model": "deepseek-v4.1-flash-free", "stream": true}'


def test_transport_accepts_string_header_order(monkeypatch):
    import curl_cffi.requests as curl_requests

    monkeypatch.setattr(curl_requests, "Session", FakeSession)
    transport = CurlCffiTransport(
        proxies={"block-a": None},
        header_order="Authorization,Content-Type,User-Agent",
    )
    transport(_request(), "block-a")
    assert FakeSession.instances[0].kwargs["extra_fp"]["header_order"] == (
        "Authorization,Content-Type,User-Agent"
    )


def test_transport_adds_ipv6_resolve(monkeypatch):
    import curl_cffi.requests as curl_requests
    from curl_cffi import CurlOpt

    monkeypatch.setattr(curl_requests, "Session", FakeSession)
    transport = CurlCffiTransport(proxies={"block-v6": "socks5h://127.0.0.1:11085"}, ipv6=True)
    transport(_request(), "block-v6")
    assert FakeSession.instances[0].kwargs["curl_options"] == {CurlOpt.IPRESOLVE: 2}


def test_transport_flags_error_frame(monkeypatch):
    import curl_cffi.requests as curl_requests

    class ErrorSession(FakeSession):
        def post(self, url: str, **kwargs) -> FakeResponse:
            return FakeResponse(200, "data: {}\n\nevent: error\n")

    monkeypatch.setattr(curl_requests, "Session", ErrorSession)
    transport = CurlCffiTransport(proxies={"block-a": None})
    response = transport(_request(), "block-a")
    assert response.error_frame is True


def test_transport_rejects_unknown_block(monkeypatch):
    import curl_cffi.requests as curl_requests

    monkeypatch.setattr(curl_requests, "Session", FakeSession)
    transport = CurlCffiTransport(proxies={"block-a": None})
    with pytest.raises(KeyError):
        transport(_request(), "missing")
    assert FakeSession.instances == []
