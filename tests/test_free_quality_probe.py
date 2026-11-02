"""Quality probe: zero-quota GET with the official Bun/TLS replay."""

from __future__ import annotations

import pytest

import opencode as oc
from free.probe import probe_station


class _Station:
    _station = 3
    current_ip = "187.15.130.159"
    socks5_url = "socks5://127.0.0.1:1082"


class _Resp:
    status_code = 204


@pytest.mark.asyncio
async def test_quality_probe_uses_official_fingerprint(monkeypatch):
    calls = []

    def _fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp()

    monkeypatch.setattr("curl_cffi.requests.get", _fake_get)
    monkeypatch.setattr(
        oc,
        "_get_free_fp_kwargs",
        lambda version="": (
            "chrome131",
            {
                "ja3": "official-ja3",
                "extra_fp": {"tls_grease": False},
                "http_version": "v1",
            },
        ),
    )

    result = await probe_station(_Station(), url="https://opencode.ai/robots.txt")

    assert result["ok"] is True
    assert result["error"] is None
    url, kwargs = calls[0]
    assert url == "https://opencode.ai/robots.txt"
    assert kwargs["proxies"]["https"].startswith("socks5h://127.0.0.1:1082")
    assert kwargs["impersonate"] == "chrome131"
    assert kwargs["ja3"] == "official-ja3"
    assert kwargs["extra_fp"] == {"tls_grease": False}
    assert kwargs["http_version"] == "v1"


@pytest.mark.asyncio
async def test_quality_probe_reports_http_failure(monkeypatch):
    class _Bad:
        status_code = 403

    monkeypatch.setattr("curl_cffi.requests.get", lambda *a, **k: _Bad())

    result = await probe_station(_Station(), url="https://opencode.ai/robots.txt")

    assert result["ok"] is False
    assert result["error"] == "http_403"
