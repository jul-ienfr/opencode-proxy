"""test_pool_connection_failure.py — real connection failures (plan 18/08).

[plan 18/08 §1a/§1b/§1c] A real SOCKS5 connection failure seen in the
request path (opencode.py) is signalled to the pool — the missing link of
the 2026-08-17 incident (zero watchdog ticks; the pool kept re-striking a
dead tunnel).

Covered here (offline — no docker, no sockets, no loop tasks):
  * pool:  notify_connection_failure bad-marks immediately (étage 0) when
    another station is usable; C1 never bad-marks the last standing station;
    mono-station arms without marking; am.18 late-signal guard (fresh
    rotation → no mark); stale signal → mark; idempotent; NEVER kicks a
    background rotation (am.7 — the wake IS the repair).
  * classification: oc._is_connect_error — curl codes {5,6,7,18,28,35,56,97}
    → True; HTTP responses (429) and non-network errors → False; message
    fallback ("SOCKS"/"connect"/"proxy").
  * signal: oc._signal_connection_failure forwards to the pool and
    swallows any pool error (never blocks the request path).
"""
import time

import pytest

import opencode as oc
from free_ip_pool import FreeIPPool

from curl_cffi.requests import errors as _err


class _Station:
    """Minimal station double exposing the attrs notify_connection_failure
    reads: proxy_mode/enabled/status (via _station_usable) + the arm
    recorder (never the real loop)."""

    def __init__(self, sid, *, enabled=True, proxy_mode="vpn",
                 status="connected"):
        self._station = sid
        self.enabled = enabled
        self.proxy_mode = proxy_mode
        self.status = status
        self.socks5_url = f"socks5://127.0.0.1:1{sid}080"
        self.armed = []                     # [plan 18/08 §1c] egress-watchdog arms

    def arm_egress_watchdog(self):
        self.armed.append(self._station)


def _pool(st1, st2=None, *, bad_ttl=60):
    pool = FreeIPPool(st1, st2)
    pool._bad_ttl = float(bad_ttl)
    # Stub the background-rotation launcher: notify must NEVER call it.
    pool.rotated = []
    pool._launch_rotation = lambda station: pool.rotated.append(station)
    return pool


def _fresh_pair():
    return _Station(1), _Station(2)


class TestPoolSignalling:
    def test_bad_marks_immediately_when_other_usable(self):
        """Real failure on station 1 (station 2 usable) → bad-mark NOW: the
        next request must switch, never re-strike a known-dead tunnel."""
        st1, st2 = _fresh_pair()
        p = _pool(st1, st2)

        p.notify_connection_failure(st1)

        assert p._per_station(st1)["bad_until"] is not None, \
            "étage 0: bad-mark immediately so _station_usable refuses it"
        assert p._per_station(st1)["bad_until"] > time.monotonic()
        assert p._per_station(st2)["bad_until"] is None, \
            "the healthy station stays untouched"

    def test_arms_egress_watchdog(self):
        """[étage 1] the manager must hear about the real failure — the arm
        is recorded even when a bad-mark happens."""
        st1, st2 = _fresh_pair()
        p = _pool(st1, st2)

        p.notify_connection_failure(st1)

        assert st1.armed == [1], "arm_egress_watchdog called on the failed station"

    def test_c1_never_bad_marks_last_standing_but_arms(self):
        """C1: the failed station is the ONLY usable one → no bad-mark (that
        would drive free traffic direct/paid), but the manager still arms."""
        st1 = _Station(1)
        st2 = _Station(2, status="disconnected")  # other tunnel down
        p = _pool(st1, st2)

        p.notify_connection_failure(st1)

        assert p._per_station(st1)["bad_until"] is None, \
            "C1: last standing station must not be bad-marked"
        assert st1.armed == [1], "arm happens even under C1 (mono-station too)"

    def test_mono_station_arms_without_mark(self):
        """Single-station mode: no bad-mark possible (C1), but the signal
        still arms the manager — the tunnel is dead either way, the armed
        tick probes and repairs."""
        st1 = _Station(1)
        p = _pool(st1)

        p.notify_connection_failure(st1)

        assert p._per_station(st1)["bad_until"] is None
        assert st1.armed == [1]

    def test_never_kicks_rotation(self):
        """am.7: notify must NOT launch a background rotation — the pool kick
        would race the tick's fast-recover (lock orders, doesn't cancel) and
        double-pin. The wake IS the repair."""
        st1, st2 = _fresh_pair()
        p = _pool(st1, st2)

        p.notify_connection_failure(st1)

        assert p.rotated == [], "notify must never call _launch_rotation"

    def test_late_signal_skips_bad_mark(self):
        """am.18: a request launched BEFORE a successful rotation may fail
        after it landed — the late signal must not bad-mark a freshly
        rotated (healthy) station. Arm still fires."""
        st1, st2 = _fresh_pair()
        p = _pool(st1, st2, bad_ttl=60)
        # Fresh rotation landed 10 s ago (< bad_ttl).
        p._per_station(st1)["session_start"] = time.monotonic() - 10

        p.notify_connection_failure(st1)

        assert p._per_station(st1)["bad_until"] is None, \
            "fresh rotation (< bad_ttl) absorbs the late signal"
        assert st1.armed == [1]

    def test_stale_signal_bad_marks(self):
        """The rotation is older than bad_ttl → the signal is genuine, the
        station is bad-marked."""
        st1, st2 = _fresh_pair()
        p = _pool(st1, st2, bad_ttl=60)
        p._per_station(st1)["session_start"] = time.monotonic() - 120

        p.notify_connection_failure(st1)

        assert p._per_station(st1)["bad_until"] is not None

    def test_idempotent_signals(self):
        """Bad-marked stations no longer fail → one arm per episode; each
        signal still records (idempotent, no crash on re-mark)."""
        st1, st2 = _fresh_pair()
        p = _pool(st1, st2)

        p.notify_connection_failure(st1)
        p.notify_connection_failure(st1)

        assert st1.armed == [1, 1]
        assert p._per_station(st1)["bad_until"] is not None


class TestIsConnectError:
    def test_code_7_connection_failed(self):
        assert oc._is_connect_error(_err.RequestsError("connect failed", 7))

    def test_raw_curl_error_not_a_signal(self):
        """The requests layer raises RequestsError subclasses, never a raw
        CurlError (its PARENT in 0.14.0) — the isinstance gate is the
        requests-layer bar."""
        assert not oc._is_connect_error(_err.CurlError("could not connect", 7))

    def test_code_28_timeout(self):
        assert oc._is_connect_error(_err.RequestsError("timed out", 28))

    def test_code_56_recv(self):
        assert oc._is_connect_error(_err.RequestsError("recv failure", 56))

    def test_http_429_not_connect_error(self):
        """A 429 response means the tunnel is ALIVE — never a connect signal."""
        assert not oc._is_connect_error(
            _err.RequestsError("Too many requests for this IP", 429))

    def test_non_network_error_false(self):
        assert not oc._is_connect_error(ValueError("boom"))

    def test_message_fallback_socks(self):
        """code 0 (mapping classes don't self-set the code) + a SOCKS-flavoured
        message → classified by message."""
        e = _err.RequestsError("SOCKS5 connection to 127.0.0.1:1080 failed", 0)
        assert oc._is_connect_error(e)

    def test_http_message_not_connect(self):
        """HTTP messages avoid the words socks/connect/proxy → False (and the
        code isn't in the tunnel-death set)."""
        assert not oc._is_connect_error(
            _err.RequestsError("client error 429 - please retry", 429))


class TestPoolSignal:
    class _Recorder:
        def __init__(self):
            self.calls = []

        def notify_connection_failure(self, station):
            self.calls.append(station)

    def test_signal_forwards_to_pool(self, monkeypatch):
        rec = self._Recorder()
        monkeypatch.setattr(oc, "_free_ip_pool", rec)

        oc._signal_connection_failure("st1")

        assert rec.calls == ["st1"]

    def test_signal_swallows_pool_errors(self, monkeypatch):
        """The request path must NEVER suffer the signal — fire-and-forget."""

        class _Boom:
            def notify_connection_failure(self, station):
                raise RuntimeError("pool broke")

        monkeypatch.setattr(oc, "_free_ip_pool", _Boom())

        # Must not raise.
        oc._signal_connection_failure("st1")

    def test_signal_noop_without_pool(self, monkeypatch):
        """No VPN → pool absent (self-heal mode). The signal is a no-op."""
        monkeypatch.setattr(oc, "_free_ip_pool", None)

        oc._signal_connection_failure("st1")
