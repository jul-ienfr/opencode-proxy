"""test_free_vpn_required.py — [proxy_mode] vpn/socks5: NEVER direct.

The two non-stream scenarios of the plan live in test_free_multi_attempt.py
(reusing its fixtures/doubles):

  test_non_stream_exception_fallback_ignored_in_vpn_mode
      proxy_mode=vpn + free_exception_fallback=direct → tunnels only,
      budget exhausted → None (paid), never _do_request_with_retry.
  test_free_direct_mode_uses_direct
      proxy_mode=direct → free served via the residential direct branch,
      curl_cffi/station picks never happen.

This file pins the guard added in opencode._open_free_stream: in vpn/socks5
mode with direct_fallback=False and NO usable station (proxy_url None), the
stream must raise _FreeTunnelFailure (caller retries another station / falls
back to paid) instead of silently opening a direct residential stream.
"""

import pytest

import opencode as oc
from test_free_multi_attempt import (  # noqa: F401  (shared doubles)
    _Station,
    _StubVpn,
)


class _PoolNoStations:
    """FreeIPPool double: enabled but every on_request finds nothing usable."""

    enabled = True
    active_station = None

    async def on_request(self):
        return None, None


@pytest.mark.asyncio
async def test_stream_vpn_mode_no_station_never_opens_direct(monkeypatch):
    """vpn mode, no usable station, direct_fallback=False → _FreeTunnelFailure
    (paid/other-station retry); a direct httpx stream must NOT be opened."""
    monkeypatch.setattr(oc, "_vpn_manager", _StubVpn())
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_free_ip_pool", _PoolNoStations())
    oc._current_free_attempt.set({})

    httpx_calls = []

    class _BoomClient:
        def stream(self, *a, **k):
            httpx_calls.append(k.get("url"))
            raise AssertionError("direct httpx.stream must never be opened in vpn mode")

    monkeypatch.setattr(oc.httpx, "AsyncClient", lambda *a, **k: _BoomClient(), raising=False)

    with pytest.raises(oc._FreeTunnelFailure):
        async with oc._open_free_stream(
            oc.API_BASE_FREE,
            {"model": "free-test-model", "messages": []},
            {"Content-Type": "application/json"},
            True,
            count_request=True,
            direct_fallback=False,
        ):
            pass

    assert httpx_calls == [], "no direct stream opened when no station is usable"
