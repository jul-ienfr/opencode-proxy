"""Dashboard proxy probes must use the measured OpenCode TLS replay."""

from __future__ import annotations

import pytest

import dashboard.api as api
import opencode as oc


class _Resp:
    status_code = 200
    text = "203.0.113.7"


@pytest.mark.asyncio
async def test_socks5_probe_uses_official_fingerprint(monkeypatch):
    calls = []

    def _fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp()

    monkeypatch.setattr("curl_cffi.requests.get", _fake_get)
    monkeypatch.setattr(
        oc,
        "_get_free_fp_kwargs",
        lambda version="": ("chrome131", {"ja3": "official-ja3"}),
    )

    result = await api._socks5_probe(
        "socks-nl1.nordvpn.com", 1080, "user", "pass"
    )

    assert result["ok"] is True
    assert result["ip"] == "203.0.113.7"
    url, kwargs = calls[0]
    assert url == "https://api.ipify.org/"
    assert kwargs["impersonate"] == "chrome131"
    assert kwargs["ja3"] == "official-ja3"
    assert kwargs["proxies"]["https"].startswith(
        "socks5h://user:pass@socks-nl1.nordvpn.com:1080"
    )


@pytest.mark.asyncio
async def test_https_probe_uses_official_fingerprint(monkeypatch):
    calls = []

    def _fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp()

    monkeypatch.setattr("curl_cffi.requests.get", _fake_get)
    monkeypatch.setattr(
        oc,
        "_get_free_fp_kwargs",
        lambda version="": ("chrome131", {"ja3": "official-ja3"}),
    )

    result = await api._https_probe("uk765.nordvpn.com", 89, "user", "pass")

    assert result["ok"] is True
    _url, kwargs = calls[0]
    assert kwargs["impersonate"] == "chrome131"
    assert kwargs["ja3"] == "official-ja3"
    assert kwargs["curl_options"]
    assert kwargs["proxies"]["https"].startswith(
        "https://user:pass@uk765.nordvpn.com:89"
    )
