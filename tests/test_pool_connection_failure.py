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
                 status="connected", current_ip=None):
        self._station = sid
        self.enabled = enabled
        self.proxy_mode = proxy_mode
        self.status = status
        # [review 18/08 F1b] repair anchor source: the manager re-pins a
        # FRESH IP on repair (current_ip changes) without touching
        # session_start — notify detects the change via this attr.
        self.current_ip = current_ip
        self.socks5_url = f"socks5://127.0.0.1:1{sid}080"
        self.armed = []                     # [plan 18/08 §1c] egress-watchdog arms

    def arm_egress_watchdog(self):
        self.armed.append(self._station)


def _pool(st1, st2=None, *, bad_ttl=60, grace=20.0):
    pool = FreeIPPool(st1, st2)
    pool._bad_ttl = float(bad_ttl)
    # [review 18/08 F1a] the late-signal absorption window — SHORT, the
    # prod default is set explicitly so tests never silently inherit a
    # future default drift.
    pool._late_signal_grace = float(grace)
    # Stub the background-rotation launcher: notify must NEVER call it.
    pool.rotated = []
    pool._launch_rotation = lambda station, forced_pool=None, **kw: pool.rotated.append(station)
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

    def test_late_signal_beyond_grace_bad_marks(self):
        """[review F1a] the absorption window is SHORT (20 s grace, NOT the
        full 60 s bad_ttl): a signal 30 s after rotation is past the dial
        queue (connect timeout 10 s + handshake) + teardown recv — genuine.
        The dead freshly-rotated tunnel is bad-marked NOW (étage 0) instead
        of being re-struck for a full bad_ttl. Fails on the pre-fix code
        (which absorbed everything < bad_ttl)."""
        st1, st2 = _fresh_pair()
        p = _pool(st1, st2, bad_ttl=60)
        p._per_station(st1)["session_start"] = time.monotonic() - 30

        p.notify_connection_failure(st1)

        assert p._per_station(st1)["bad_until"] is not None

    def test_first_failure_marks_without_anchor(self):
        """[review F1b regression guard] a station never pool-rotated has no
        last_confirmed_ip anchor. Establishing the baseline on first
        observation must NOT refresh session_start — the FIRST genuine
        failure still bad-marks (étage 0 alive from day one, never silently
        absorbed by a baseline-establishing refresh)."""
        st1 = _Station(1, current_ip="1.1.1.1")
        st2 = _Station(2)
        p = _pool(st1, st2)

        p.notify_connection_failure(st1)

        assert p._per_station(st1)["bad_until"] is not None, \
            "first genuine failure must bad-mark even without an anchor"

    def test_repair_refresh_absorbs_late_signal(self):
        """[review F1b] the manager repair path re-pins a FRESH IP
        (current_ip changes) without touching session_start — the anchor
        detects the change and refreshes: a late signal from a pre-repair
        request is absorbed, the repaired healthy tunnel is never
        bad-marked."""
        st1 = _Station(1, current_ip="1.1.1.1")
        st2 = _Station(2)
        p = _pool(st1, st2)
        per = p._per_station(st1)
        per["last_confirmed_ip"] = "1.1.1.1"       # pool rotation recorded it
        per["session_start"] = time.monotonic() - 120
        st1.current_ip = "2.2.2.2"                 # the repair re-pin

        p.notify_connection_failure(st1)

        assert per["last_confirmed_ip"] == "2.2.2.2", \
            "anchor must follow the repair re-pin"
        assert per["bad_until"] is None, \
            "fresh repair IP absorbs the late signal"

    def test_repair_refresh_then_genuine_failure_marks(self):
        """After the repair-anchor refresh, a signal beyond the grace window
        is genuine → bad-mark: the refresh absorbs the repair tail, it does
        NOT disable étage 0."""
        st1 = _Station(1, current_ip="1.1.1.1")
        st2 = _Station(2)
        p = _pool(st1, st2)
        per = p._per_station(st1)
        per["last_confirmed_ip"] = "1.1.1.1"
        per["session_start"] = time.monotonic() - 120
        st1.current_ip = "2.2.2.2"

        p.notify_connection_failure(st1)              # absorbed + anchor refresh
        per["session_start"] = time.monotonic() - 30  # grace elapsed
        p.notify_connection_failure(st1)              # genuine failure now

        assert per["bad_until"] is not None

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

    def test_signal_logs_and_reraises_pool_errors(self, monkeypatch, caplog):
        """[Axe 1.5] A bug in the notification path must be VISIBLE, not
        swallowed: the old `except Exception: pass` silently disabled the
        failover for every following request. The exception is now logged
        with traceback and re-raised — the request-level caller's own
        except-block still falls back safely, but the defect surfaces."""

        class _Boom:
            def notify_connection_failure(self, station):
                raise RuntimeError("pool broke")

        monkeypatch.setattr(oc, "_free_ip_pool", _Boom())

        with pytest.raises(RuntimeError, match="pool broke"):
            oc._signal_connection_failure("st1")

        assert any("notify_connection_failure raised" in r.message
                   for r in caplog.records), \
            "the defect must be logged with context, not silenced"

    def test_signal_noop_without_pool(self, monkeypatch):
        """No VPN → pool absent (self-heal mode). The signal is a no-op."""
        monkeypatch.setattr(oc, "_free_ip_pool", None)

        oc._signal_connection_failure("st1")


class TestRequestPathWiring:
    """[review 18/08] the REAL request helper wires the signal — not just
    the unit function. A fake curl session raises RequestsError(7) through
    _do_free_request_curl_cffi → the pool recorder is notified and the
    error re-raised (the caller owns the fallback); an HTTP response
    (even 429) is never a signal (raise_for_status is False — the tunnel
    is alive)."""

    class _RecorderPool:
        def __init__(self):
            self.notified = []

        enabled = True

        async def on_request(self):
            return "socks5://127.0.0.1:1080", "st1"

        def notify_connection_failure(self, station):
            self.notified.append(station)

    class _FakeSession:
        def __init__(self, *, error=None, response=None):
            self._error, self._response = error, response
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            self.closed = True
            return False

        async def post(self, *args, **kwargs):
            if self._error is not None:
                raise self._error
            return self._response

    class _FakeResponse:
        status_code = 429
        headers = {}
        content = b"{}"

    def _patch(self, monkeypatch, session):
        rec = self._RecorderPool()
        monkeypatch.setattr(oc, "_free_ip_pool", rec)
        monkeypatch.setattr(oc, "_current_free_identity",
                            lambda station=None: {"impersonate": "chrome131",
                                                  "user_agent": None,
                                                  "extra_headers": {}})
        monkeypatch.setattr("curl_cffi.requests.AsyncSession",
                            lambda **kw: session)
        # Clear pooled curl sessions: previous test may have cached a fake that raises RuntimeError
        oc._curl_pool.clear()
        return rec

    @pytest.mark.asyncio
    async def test_real_helper_signals_on_connect_failure(self, monkeypatch):
        """RequestsError code 7 (connection failed — the dead-tunnel class)
        → notified + re-raised: the caller keeps its fallback, the pool
        bad-marks and the manager arms."""
        rec = self._patch(monkeypatch, self._FakeSession(
            error=_err.RequestsError("connect failed", 7)))

        with pytest.raises(_err.RequestsError):
            await oc._do_free_request_curl_cffi(
                {}, {}, "socks5://127.0.0.1:1080", "st1")

        assert rec.notified == ["st1"]

    @pytest.mark.asyncio
    async def test_real_helper_no_signal_on_http_response(self, monkeypatch):
        """A 429 response means the tunnel is ALIVE — no signal, the helper
        returns the wrapped response normally."""
        rec = self._patch(monkeypatch, self._FakeSession(
            response=self._FakeResponse()))

        resp = await oc._do_free_request_curl_cffi(
            {}, {}, "socks5://127.0.0.1:1080", "st1")

        assert isinstance(resp, oc._CurlCffiResponse)
        assert resp.status_code == 429
        assert rec.notified == []


class _Task:
    """task double: cancel() records the call; __eq__ can't collide with the
    id()-based marker (never used)."""

    def __init__(self, name):
        self.name = name
        self.cancelled = 0

    def cancel(self):
        self.cancelled += 1


class TestStreamsCancelled:
    """[plan 18/08 §am.22] the tick's egress_death cancel: registered free
    streams are cancelled + marked (watchdog_cancelled OUTSIDE the live
    registry — unregister happens during CancelledError propagation, before
    the handler inspects); genuine client disconnects (never registered)
    are NOT classified; the marker is a bounded per-burst overwrite."""

    def test_cancel_streams_cancels_and_marks_registered(self):
        st1, st2 = _fresh_pair()
        pool = _pool(st1, st2)
        t1a, t1b, t2a = _Task("a"), _Task("b"), _Task("c")
        pool.register_stream(st1, t1a)
        pool.register_stream(st1, t1b)
        pool.register_stream(st2, t2a)

        pool.cancel_streams(st1)

        assert t1a.cancelled == 1 and t1b.cancelled == 1
        assert t2a.cancelled == 0, "other station's streams left alone until IT is confirmed dead"
        assert pool.is_watchdog_cancelled(st1, t1a)
        assert pool.is_watchdog_cancelled(st1, t1b)
        assert not pool.is_watchdog_cancelled(st2, t2a)
        # Marker SURVIVES unregister: the handler unregisters during
        # CancelledError propagation, BEFORE inspecting it.
        pool.unregister_stream(st1, t1a)
        assert pool.is_watchdog_cancelled(st1, t1a)

    def test_noop_when_nothing_registered(self):
        st1 = _Station(1)
        pool = _pool(st1)
        pool.cancel_streams(st1)               # must not raise
        pool.cancel_streams(_Station(2))       # unknown station → no raise
        assert not pool.is_watchdog_cancelled(st1, _Task("x"))

    def test_genuine_client_cancel_not_classified(self):
        st1 = _Station(1)
        pool = _pool(st1)
        reg = _Task("stream")
        client = _Task("client")               # uvicorn cancels this one directly
        pool.register_stream(st1, reg)

        pool.cancel_streams(st1)

        assert reg.cancelled == 1
        assert client.cancelled == 0, "never registered → never cancelled"
        assert not pool.is_watchdog_cancelled(st1, client)
        assert pool.is_watchdog_cancelled(st1, reg)

    def test_burst_marker_bounded_overwrite(self):
        """The cancelled marker is per burst: the second burst rebuilds it from
        what is CURRENTLY registered, so a stale id of an already-propagated
        stream is never carried forward (it can only misclassify a later task
        that happens to reuse the id slot)."""
        st1 = _Station(1)
        pool = _pool(st1)
        t1 = _Task("first burst")
        pool.register_stream(st1, t1)
        pool.cancel_streams(st1)
        assert pool.is_watchdog_cancelled(st1, t1)
        pool.unregister_stream(st1, t1)       # handler propagated → gone

        t2 = _Task("second burst")             # fresh burst on a NEW stream
        pool.register_stream(st1, t2)
        pool.cancel_streams(st1)

        assert pool.is_watchdog_cancelled(st1, t2)
        assert not pool.is_watchdog_cancelled(st1, t1), \
            "stale burst id must not survive the overwrite"

    def test_re_registered_stream_marked_again(self):
        """A stream still REGISTERED at the next burst is re-marked (still
        in flight — legitimately a cancelled stream)."""
        st1 = _Station(1)
        pool = _pool(st1)
        t = _Task("long stream")
        pool.register_stream(st1, t)
        pool.cancel_streams(st1)
        pool.cancel_streams(st1)              # still registered/in-flight
        assert t.cancelled == 2
        assert pool.is_watchdog_cancelled(st1, t)

    # ── registration wiring: who calls register/unregister ─────────

    def test_register_and_unregister_roundtrip(self):
        st1 = _Station(1)
        pool = _pool(st1)
        t = _Task("s")
        pool.register_stream(st1, t)
        pool.unregister_stream(st1, t)
        pool.cancel_streams(st1)               # registry empty → no cancel
        assert t.cancelled == 0
        assert not pool.is_watchdog_cancelled(st1, t)
