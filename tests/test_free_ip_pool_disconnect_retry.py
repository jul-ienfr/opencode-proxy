"""test_free_ip_pool_disconnect_retry.py — FreeIPPool.on_disconnect_retry.

Regression test for the 2026-08-17 21:44:40 incident: dashboard history
showed a ✘ row
  mimo-v2.5 → deepseek-v4-flash, error "Server disconnected without sending
  a response."
The stream retry loop re-struck the SAME station/proxy that just died
(``count_request=False`` branch in ``_open_free_stream``). Under the
per-IP quota model a dead IP is a guaranteed failure: the same (model, IP)
cooldown key is still cooling down, so the retry had no chance — a ✘.

Fix: ``on_disconnect_retry(failed)`` picks a DIFFERENT station (fresh IP =
fresh (model, IP) cooldown key) WITHOUT advancing the quota counter — a
retry is not a new request, the failed attempt already consumed its per-IP
slot.

Covered here (offline, fake stations — no docker, no sockets, no loop
tasks: ``_launch_rotation`` is stubbed per instance):
  * disabled / not-vpn → (None, None)
  * dual-station: retry lands on the OTHER station; failed one bad-marked
    and rotated in the background; quota counters untouched
  * C1 guard: never bad-mark the last standing station
  * failed=None (no station known, e.g. a direct fallback) → best station
    across the pool
  * single-station mode: no other station → (None, None), the failed
    station is still rotated in the background
"""
import time

import pytest

from free_ip_pool import FreeIPPool


class _StubStation:
    """Minimal VPNManager double exposing the attrs the pool reads."""

    def __init__(self, sid, *, enabled=True, proxy_mode="vpn",
                 status="connected", quota_per_ip=15, ip="1.1.1.1",
                 server_name="se-1"):
        self._station = sid
        self.enabled = enabled
        self.proxy_mode = proxy_mode
        self.status = status
        self._quota_per_ip = quota_per_ip
        self.current_ip = ip
        self.current_server = {"name": server_name}
        self.socks5_url = f"socks5://127.0.0.1:1{sid}080"
        self._last_rotation_error = None
        self.armed = []                     # [plan 18/08 §1c] egress-watchdog arms

    def note_free_request(self):
        pass

    def arm_egress_watchdog(self):
        """[plan 18/08 §1b] the pool signals real connection failures to the
        manager — the recorder must expose the arm (never the real loop)."""
        self.armed.append(self._station)

    def get_status(self):
        return {"station": self._station, "status": self.status}


def _pool(st1, st2=None, *, bad_ttl=60):
    pool = FreeIPPool(st1, st2)
    pool._bad_ttl = float(bad_ttl)
    # Stub the background-rotation launcher: keep tests free of loop tasks.
    pool.rotated = []
    pool._launch_rotation = lambda station, forced_pool=None, **kw: pool.rotated.append(station)
    return pool


def _fresh_pool(*, with_second=True):
    st1 = _StubStation(1)
    st2 = _StubStation(2) if with_second else None
    return _pool(st1, st2)


class TestDisabledOrDirect:
    @pytest.mark.asyncio
    async def test_disabled_returns_none(self):
        st1 = _StubStation(1, enabled=False)
        st2 = _StubStation(2)
        p = _pool(st1, st2)
        proxy, station = await p.on_disconnect_retry(st1)
        assert proxy is None and station is None

    @pytest.mark.asyncio
    async def test_direct_mode_returns_none(self):
        st1 = _StubStation(1, proxy_mode="direct")
        st2 = _StubStation(2)
        p = _pool(st1, st2)
        proxy, station = await p.on_disconnect_retry(st1)
        assert proxy is None and station is None


class TestDualStationSwitch:
    @pytest.mark.asyncio
    async def test_retry_lands_on_other_station(self):
        """A disconnect retry must switch to the OTHER station — the failed
        station's IP is dead, re-striking it burns the retry for nothing."""
        st1 = _StubStation(1)
        st2 = _StubStation(2)
        p = _pool(st1, st2)

        proxy, station = await p.on_disconnect_retry(st1)

        assert station is st2, "retry must land on the OTHER station"
        assert proxy == st2.socks5_url
        assert p._active_station is st2

    @pytest.mark.asyncio
    async def test_failed_station_bad_marked_and_rotated(self):
        """The failed station is bad-marked (double embrayage) and a
        background rotation is launched — fresh IP for the next request."""
        st1 = _StubStation(1)
        st2 = _StubStation(2)
        p = _pool(st1, st2)

        await p.on_disconnect_retry(st1)

        assert p._per_station(st1)["bad_until"] is not None, \
            "failed station must be bad-marked so subsequent requests skip it"
        assert p.rotated == [st1], "background rotation launched for the failed station"
        # Bad-mark is time-bounded (bad_ttl)
        assert p._per_station(st1)["bad_until"] > time.monotonic()

    @pytest.mark.asyncio
    async def test_no_counter_advance(self):
        """A retry is not a new request: the failed attempt already consumed
        its per-IP slot — neither station's counter must move."""
        st1 = _StubStation(1)
        st2 = _StubStation(2)
        p = _pool(st1, st2)
        # Give both stations a nonzero counter first (precondition)
        p._per_station(st1)["request_count"] = 7
        p._per_station(st2)["request_count"] = 3

        await p.on_disconnect_retry(st1)

        assert p._per_station(st1)["request_count"] == 7
        assert p._per_station(st2)["request_count"] == 3

    @pytest.mark.asyncio
    async def test_c1_never_bad_mark_last_standing(self):
        """C1: when the failed station is the ONLY usable one (other down),
        it must NOT be bad-marked — that would remove the last usable
        station and drive free traffic direct. No other station is usable
        either, so the retry returns (None, None) and the rotation still runs."""
        st1 = _StubStation(1)
        st2 = _StubStation(2, status="disconnected")  # the other tunnel is down
        p = _pool(st1, st2)

        proxy, station = await p.on_disconnect_retry(st1)

        assert proxy is None and station is None, \
            "none usable → caller falls back to direct/paid instead of a dead IP"
        assert p._per_station(st1)["bad_until"] is None, \
            "C1: last standing station must NOT be bad-marked"
        # The failed station still rotates in the background (plus the down
        # station's reconnect from ensure_connected — a reconnect IS a rotation).
        assert st1 in p.rotated, "the failed station still rotates in the background"

    @pytest.mark.asyncio
    async def test_failed_none_returns_best_station(self):
        """failed=None (no station known, e.g. a direct fallback) → best
        station across the pool, with no bad-marking."""
        st1 = _StubStation(1)
        st2 = _StubStation(2)
        p = _pool(st1, st2)

        proxy, station = await p.on_disconnect_retry()

        # Preference A: station 1 wins a clean tie.
        assert station is st1
        assert proxy == st1.socks5_url
        assert p._per_station(st1)["bad_until"] is None
        assert p.rotated == [], "failed=None must not rotate anything"

    @pytest.mark.asyncio
    async def test_other_station_rotated_in_background(self):
        """The failed station rotates in the background even when the retry
        does land elsewhere — it comes back healthy on a fresh IP."""
        st1 = _StubStation(1)
        st2 = _StubStation(2, status="disconnected")
        p = _pool(st1, st2)

        # Make the DOWN station usable so the retry can land on it, and the
        # failed one keeps rotating in the background.
        st2.status = "connected"
        await p.on_disconnect_retry(st1)

        assert p.rotated == [st1]


class TestSingleStation:
    @pytest.mark.asyncio
    async def test_single_station_returns_none_but_rotates(self):
        """Single-station mode: there is no OTHER station to switch to —
        (None, None) (caller falls back to direct/paid), but the dead IP
        still rotates in the background for the next request."""
        st1 = _StubStation(1)
        p = _pool(st1)

        proxy, station = await p.on_disconnect_retry(st1)

        assert proxy is None and station is None
        assert p._per_station(st1)["bad_until"] is None, \
            "single station = last standing → must not be bad-marked"
        assert p.rotated == [st1]

    @pytest.mark.asyncio
    async def test_single_station_failed_none_returns_best(self):
        """Single-station mode, failed=None → the only station, no marks."""
        st1 = _StubStation(1)
        p = _pool(st1)

        proxy, station = await p.on_disconnect_retry()

        assert station is st1
        assert proxy == st1.socks5_url
        assert p._per_station(st1)["bad_until"] is None
        assert p.rotated == []