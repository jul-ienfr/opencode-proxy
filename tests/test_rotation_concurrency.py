"""test_rotation_concurrency.py — Axe 1 (plan 18/08, revue 19/08).

Rotation IP robustness, all OFFLINE (same _Station double pattern as
test_pool_station_set.py — no docker, no real loop tasks beyond the pool's
own rotation workers, which are cancelled at the end of each test):

  1.1 Bounded rotation concurrency:
      * two stations queued → both rotations run IN PARALLEL
        (_ROTATION_CONCURRENCY = 2 workers), a blocked one no longer
        freezes the fleet
      * rotation_concurrency = 1 → serialized again (back-compat)
      * per-station single-flight dedup kept (queued twice → one rotation)
      * update_config/get_status expose rotation_concurrency (clamped >= 1)
  1.2 Post-commit probe:
      * fresh IP probed immediately after commit (direct call, uncached)
      * dead probe → bad-mark (C1-guarded) + watchdog armed + RE-QUEUE
        (dedup-safe: re-launch happens after the in-flight pop)
      * alive probe → counter reset, no re-queue
      * cap _POST_COMMIT_RETRY_MAX → no hot-loop, watchdog owns recovery
      * proxy_mode != "vpn" → no probe at all (static socks5 pool later)
  1.3 Adaptive probe timeout:
      * _classify_probe_exc structural classification (timeout vs refused
        vs error; cause-chain walk; httpx-less fake-safe)
      * two-phase _probe_tunnel_light: timeout-only → grace attempt with
        remaining budget; refused/error → definitive (no grace)
  1.4 Exhaustion under rotation lock:
      * pool.any_rotation_in_flight() (no lock needed — single-threaded)
      * _free_stations_exhausted returns False while a rotation is in
        flight (a fresh (model, IP) key is imminent)
"""
import asyncio
import time

import pytest

import opencode as oc
from free_ip_pool import FreeIPPool
import vpn_manager as vm


class _Station:
    """Minimal station double (same shape as test_pool_station_set), plus
    the Axe 1.2 probe surface: ``_probe_tunnel_light`` returns
    ``probe_result`` and counts its calls."""

    def __init__(self, sid, *, proxy_mode="vpn", status="connected",
                 current_ip=None, quota=15, probe=True):
        self._station = sid
        self.enabled = True                  # FreeIPPool.get_status reads it
        self.proxy_mode = proxy_mode
        self.status = status
        self.current_ip = current_ip
        self.current_server = None
        self._quota_per_ip = quota
        self.socks5_url = f"socks5://127.0.0.1:1{sid}080"
        self.armed = []
        self.probe_result = probe
        self.probe_calls = 0

    def get_status(self):
        return {"station": self._station, "status": self.status}

    def arm_egress_watchdog(self):
        self.armed.append(self._station)

    async def _probe_tunnel_light(self):
        self.probe_calls += 1
        return self.probe_result


def _pool(st1, st2=None, *, bad_ttl=60):
    """Pool with the REAL rotation machinery (no _launch_rotation stub —
    these tests exercise the worker/queue path)."""
    pool = FreeIPPool(st1, st2)
    pool._bad_ttl = float(bad_ttl)
    pool.switched = []                         # fake switch_ip's record log
    pool.switch_ip = _fake_switch(pool)   # no docker — instance attr shadows
    return pool


def _fake_switch(pool):
    """switch_ip that just records the station, increments the IP and sets
    status connected — the "rotation landed" contract _rotate_station
    expects."""
    async def switch(station):
        pool.switched.append(station._station)
        station.current_ip = f"10.{station._station}.0.{len(pool.switched)}"
        station.status = "connected"
    return switch


async def _shutdown(pool):
    """Cancel the pool's rotation workers (while-True queue drains) so
    asyncio.run exits cleanly."""
    for t in list(pool._worker_tasks):
        t.cancel()
    if pool._worker_tasks:
        await asyncio.gather(*pool._worker_tasks, return_exceptions=True)


# ── 1.1 Bounded rotation concurrency ──────────────────────────────

def test_two_rotations_run_in_parallel():
    """The core 1.1 fix: with _ROTATION_CONCURRENCY = 2, two queued
    stations rotate CONCURRENTLY — the old single worker would serialize
    them (the second enters only after the first finishes)."""
    async def _go():
        s1, s2 = _Station(1), _Station(2)
        p = _pool(s1, s2)
        both_inside = asyncio.Event()
        release = asyncio.Event()

        async def blocking_switch(station):
            p.switched.append(station._station)
            if len(p.switched) == 2:
                both_inside.set()          # both workers inside a rotation
            await release.wait()           # hold both until released

        p.switch_ip = blocking_switch
        p._launch_rotation(s1)
        p._launch_rotation(s2)
        # If rotations were serialized, only ONE would be inside now and
        # this wait would time out.
        await asyncio.wait_for(both_inside.wait(), 1.0)
        release.set()
        await asyncio.sleep(0.05)
        await _shutdown(p)
        assert sorted(p.switched) == [1, 2]

    asyncio.run(_go())


def test_rotation_concurrency_one_serializes():
    """Back-compat: rotation_concurrency = 1 must reproduce the old
    serialized behavior — the second rotation starts only after the first
    finished.

    [fix 20/08][Axe 1.1] Non-vacuous redesign: the old in-fake assert fired
    inside switch_ip, which _rotate_station's broad `except Exception`
    swallows (free_ip_pool worker guard) — the test could never fail on a
    serialization regression. Now the assertion is OBSERVABLE from outside:
    station 1 blocks on a gate, and while it is blocked the second station
    must NOT enter (with 2 workers — the regression this guards — station 2
    would enter during the observation window and trip the assert)."""
    async def _go():
        s1, s2 = _Station(1), _Station(2)
        p = _pool(s1, s2)
        p._ROTATION_CONCURRENCY = 1
        order = []
        s1_entered = asyncio.Event()
        s2_entered = asyncio.Event()
        gate = asyncio.Event()

        async def serial_switch(station):
            order.append(station._station)
            if station._station == 1:
                s1_entered.set()
                await gate.wait()          # hold rotation 1 INSIDE switch_ip
            else:
                s2_entered.set()

        p.switch_ip = serial_switch
        p._launch_rotation(s1)
        await asyncio.wait_for(s1_entered.wait(), 1.0)  # rotation 1 in flight
        p._launch_rotation(s2)             # queued behind the single worker
        await asyncio.sleep(0.1)           # observation window
        assert not s2_entered.is_set(), \
            "with 1 worker, station 2 must NOT enter while station 1 blocks"
        gate.set()                         # release rotation 1
        await asyncio.wait_for(s2_entered.wait(), 1.0)  # then 2 runs second
        await asyncio.sleep(0.05)
        await _shutdown(p)
        assert order == [1, 2]

    asyncio.run(_go())


def test_launch_rotation_dedups_same_station():
    """Per-station single-flight kept under concurrency: two launch calls
    for the same station → ONE physical rotation."""
    async def _go():
        s1 = _Station(1)
        p = _pool(s1)
        p._launch_rotation(s1)
        p._launch_rotation(s1)             # deduped (already queued)
        await asyncio.sleep(0.05)
        await _shutdown(p)
        assert p.switched == [1], "one rotation per queued station, never two"

    asyncio.run(_go())


def test_update_config_sets_rotation_concurrency():
    p = _pool(_Station(1))
    p.update_config({"rotation_concurrency": 4})
    assert p._ROTATION_CONCURRENCY == 4
    p.update_config({"rotation_concurrency": 0})   # clamped to the minimum
    assert p._ROTATION_CONCURRENCY == 1
    assert p.get_status()["rotation_concurrency"] == 1


# ── 1.2 Post-commit probe ─────────────────────────────────────────

def test_dead_post_commit_probe_requeues_and_bad_marks():
    """A rotation that lands a DEAD fresh tunnel must re-rotate immediately
    (not wait for the watchdog tick): bad-mark (other station usable), arm
    the watchdog, and re-queue — bounded by _POST_COMMIT_RETRY_MAX so a
    persistently flapping tunnel does not hot-loop the docker stack."""
    async def _go():
        s1, s2 = _Station(1, probe=False), _Station(2, probe=True)
        p = _pool(s1, s2)
        release = asyncio.Event()

        async def gated_switch(station):
            # Rotation 2 enters here while rotation 1's aftermath (bad-mark,
            # counter, arm) is still observable.
            p.switched.append(station._station)
            if len(p.switched) == 2:
                await release.wait()

        p.switch_ip = gated_switch
        p._launch_rotation(s1)
        await asyncio.sleep(0.05)          # rotation 1: commit + dead probe
        per = p._per_station(s1)
        assert p.switched == [1, 1], "re-queued: rotation 2 already in flight"
        assert per["post_commit_retry_count"] == 1, \
            "rotation 2 not yet committed — counter still at 1"
        assert per["bad_until"] is not None, \
            "dead fresh IP bad-marked (station 2 usable — no C1)"
        assert s1.armed == [1], "watchdog armed on the dead probe"

        release.set()
        await asyncio.sleep(0.05)          # rotation 2: hits the cap
        assert p.switched == [1, 1]
        assert per["post_commit_retry_count"] == 2
        assert s1.armed == [1, 1], "watchdog armed on EVERY dead probe"

        await asyncio.sleep(0.05)          # nothing more happens
        assert p.switched == [1, 1], "no third rotation after the cap"
        await _shutdown(p)

    asyncio.run(_go())


def test_alive_post_commit_probe_resets_counter():
    """A healthy fresh IP → probe alive → counter reset, no bad-mark, no
    re-queue (the rotation is complete)."""
    async def _go():
        s1, s2 = _Station(1, probe=True), _Station(2, probe=True)
        p = _pool(s1, s2)
        p._per_station(s1)["post_commit_retry_count"] = 1  # stale counter
        p._launch_rotation(s1)
        await asyncio.sleep(0.05)
        per = p._per_station(s1)
        assert p.switched == [1]
        assert s1.probe_calls == 1, "one fresh probe right after the commit"
        assert per["post_commit_retry_count"] == 0, "counter reset on alive"
        assert per["bad_until"] is None
        assert s1.armed == [], "no watchdog kick on a healthy tunnel"
        assert p._pending == set()
        await _shutdown(p)

    asyncio.run(_go())


def test_dead_post_commit_probe_c1_alone_no_badmark_but_requeues():
    """C1: the failed station is the ONLY usable one → no bad-mark (that
    would drive free traffic to paid), but the immediate re-rotation and
    the watchdog arm still happen."""
    async def _go():
        s1, s2 = _Station(1, probe=False), _Station(2, status="disconnected")
        p = _pool(s1, s2)
        release = asyncio.Event()

        async def gated_switch(station):
            p.switched.append(station._station)
            if len(p.switched) == 2:
                await release.wait()

        p.switch_ip = gated_switch
        p._launch_rotation(s1)
        await asyncio.sleep(0.05)
        per = p._per_station(s1)
        assert p.switched == [1, 1], "re-queued even under C1"
        assert per["post_commit_retry_count"] == 1
        assert per["bad_until"] is None, "C1: last standing never bad-marked"
        assert s1.armed == [1]

        release.set()
        await asyncio.sleep(0.05)          # rotation 2: cap reached
        assert per["post_commit_retry_count"] == 2
        assert per["bad_until"] is None, "still C1 — never bad-marked"
        assert s1.armed == [1, 1]

        await asyncio.sleep(0.05)
        assert p.switched == [1, 1], "no third rotation after the cap"
        await _shutdown(p)

    asyncio.run(_go())


def test_no_post_commit_probe_outside_vpn_mode():
    """proxy_mode != vpn (e.g. the future static socks5 pool, or direct):
    no docker-backed probe, no re-queue, rotation just completes."""
    async def _go():
        s1 = _Station(1, proxy_mode="direct")
        p = _pool(s1)
        p._launch_rotation(s1)
        await asyncio.sleep(0.05)
        assert p.switched == [1]
        assert s1.probe_calls == 0, "no probe outside vpn mode"
        assert p._pending == set()
        await _shutdown(p)

    asyncio.run(_go())


# ── 1.4 Exhaustion under rotation lock ────────────────────────────

def test_any_rotation_in_flight():
    async def _go():
        s1 = _Station(1)
        p = _pool(s1)
        assert p.any_rotation_in_flight() is False, "empty registry → False"
        t = asyncio.create_task(asyncio.sleep(1))
        p._rotation_tasks[1] = t
        assert p.any_rotation_in_flight() is True
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert p.any_rotation_in_flight() is False, \
            "a finished rotation no longer counts as in flight"
        await _shutdown(p)

    asyncio.run(_go())


class _FakePool:
    """Pool double for _free_stations_exhausted (opencode side)."""

    def __init__(self, inflight: bool):
        self._stations = [object()]
        self._inflight = inflight

    def any_rotation_in_flight(self) -> bool:
        return self._inflight

    def _station_usable(self, st, *, exclude_approaching: bool):
        return False


def test_free_stations_exhausted_false_while_rotation_in_flight(monkeypatch):
    """[Axe 1.4] A rotation in flight is about to land a fresh (model, IP)
    key — the pool is NOT exhausted: strict_free must refuse nothing."""
    monkeypatch.setattr(oc, "_free_ip_pool", _FakePool(inflight=True))
    monkeypatch.setattr(oc, "_free_cooldown_active", lambda *a: True)
    assert oc._free_stations_exhausted("some-model") is False


def test_free_stations_exhausted_true_without_rotation_in_flight(monkeypatch):
    """No rotation in flight + no usable station → truly exhausted."""
    monkeypatch.setattr(oc, "_free_ip_pool", _FakePool(inflight=False))
    monkeypatch.setattr(oc, "_free_cooldown_active", lambda *a: True)
    assert oc._free_stations_exhausted("some-model") is True


def test_free_stations_exhausted_true_without_pool(monkeypatch):
    monkeypatch.setattr(oc, "_free_ip_pool", None)
    assert oc._free_stations_exhausted("some-model") is True


class _Ep:
    """Minimal Socks5Endpoint double (host:port identity)."""

    def __init__(self, name, *, enabled=True, usable=True):
        self.name = name
        self.enabled = enabled
        self.usable = usable
        self.pid = f"1.2.3.4:10{name}00"


class _Socks5FakePool:
    """Pool double in socks5 mode for _free_stations_exhausted: the docs
    contrast with _FakePool — socks5 mode has NO rotation-in-flight
    exemption (no docker), the usable set is the enabled static proxies."""

    socks5_mode = True
    _stations = []          # docker stations inert in socks5 mode

    def __init__(self, eps):
        self._eps = eps

    def _socks5_enabled_eps(self):
        return [ep for ep in self._eps if ep.enabled]

    def _socks5_usable(self, ep, *, exclude_approaching=False):
        return ep.usable


def test_socks5_exhausted_false_when_usable_proxy_no_cooldown(monkeypatch):
    """[Axe 3.1] In socks5 mode a usable proxy with a clear cooldown key
    means the pool is NOT exhausted (the free shot can land on it)."""
    ep = _Ep(1, usable=True)
    monkeypatch.setattr(oc, "_free_ip_pool", _Socks5FakePool([ep]))
    monkeypatch.setattr(oc, "_free_cooldown_active", lambda *a: False)
    assert oc._free_stations_exhausted("some-model") is False


def test_socks5_exhausted_true_when_all_cooldowned(monkeypatch):
    """Every proxy is (still) on cooldown, though otherwise usable → the
    free model refuses until a cooldown expires — no docker rotation
    exists to land a fresh key."""
    ep = _Ep(1, usable=True)
    monkeypatch.setattr(oc, "_free_ip_pool", _Socks5FakePool([ep]))
    monkeypatch.setattr(oc, "_free_cooldown_active", lambda *a: True)
    assert oc._free_stations_exhausted("some-model") is True


def test_socks5_exhausted_true_when_no_usable_proxy(monkeypatch):
    """All proxies unusable (dead/disabled): no usable set → exhausted."""
    monkeypatch.setattr(oc, "_free_ip_pool",
                        _Socks5FakePool([_Ep(1, usable=False),
                                         _Ep(2, enabled=False, usable=True)]))
    assert oc._free_stations_exhausted("some-model") is True


def test_free_stations_exhausted_getattr_duck_safe(monkeypatch):
    """Pool doubles WITHOUT socks5_mode must hit the vpn path with no
    AttributeError — the minimal _FakePool from a prior axis is such a
    double, and a regression here would crash the free path at runtime."""
    monkeypatch.setattr(oc, "_free_ip_pool", _FakePool(inflight=False))
    monkeypatch.setattr(oc, "_free_cooldown_active", lambda *a: True)
    assert oc._free_stations_exhausted("some-model") is True


# ── 1.3 Adaptive probe timeout ────────────────────────────────────

def test_classify_probe_exc_timeout():
    assert vm._classify_probe_exc(asyncio.TimeoutError()) == "timeout"
    assert vm._classify_probe_exc(TimeoutError()) == "timeout"

    class _Named(Exception):
        pass
    _Named.__name__ = "ConnectTimeout"     # httpx's real type name
    assert vm._classify_probe_exc(_Named("slow tunnel")) == "timeout"

    assert vm._classify_probe_exc(RuntimeError("timed out waiting")) == "timeout"


def test_classify_probe_exc_refused():
    assert vm._classify_probe_exc(ConnectionRefusedError()) == "refused"
    assert vm._classify_probe_exc(RuntimeError("connection refused")) == "refused"
    assert vm._classify_probe_exc(RuntimeError("WinError 10061")) == "refused"
    # cause-chain walk: httpx/httpcore wrap the real socket error
    inner = ConnectionRefusedError()
    outer = RuntimeError("all connection attempts failed")
    outer.__cause__ = inner
    assert vm._classify_probe_exc(outer) == "refused"


def test_classify_probe_exc_error():
    """Bare RuntimeError (the F2 tests' fake httpx behavior) → "error":
    treated as dead, NO grace phase — the fake-module records stay exact."""
    assert vm._classify_probe_exc(RuntimeError("network dead")) == "error"


class _ProbeMgr:
    """Manager double for _probe_tunnel_light: records (url, per_attempt)
    calls, serves verdicts from a queue."""

    def __init__(self, verdicts, *, budget=8.0, urls=None):
        self._ip_check_url = "http://a"
        # None → single-endpoint path; the two-endpoint sweep is the real
        # multi-url config, so it is the test default here.
        self._ip_check_urls = urls if urls is not None else ["http://a",
                                                             "http://b"]
        self._ip_probe_budget = budget
        self._ip_check_idx = 0
        self.calls = []
        self._verdicts = list(verdicts)

    async def _probe_connect(self, url, *, per_attempt):
        self.calls.append((url, per_attempt))
        return self._verdicts.pop(0) if self._verdicts else "error"


def _light(mgr):
    """Call the real _probe_tunnel_light on a double (unbound method)."""
    return vm.VPNManager._probe_tunnel_light(mgr)


def test_probe_timeout_then_ok_advances_sticky():
    mgr = _ProbeMgr(["timeout", "ok"])
    assert asyncio.run(_light(mgr)) is True
    assert mgr.calls == [("http://a", 2.0), ("http://b", 2.0)]
    assert mgr._ip_check_idx == 1, "sticky advance on a later endpoint"


def test_probe_timeout_only_gets_grace_attempt():
    """[Axe 1.3] timeout-only failure → ONE grace attempt with the REMAINING
    budget on the sticky-first endpoint. A slow-but-alive tunnel that
    answers here was never dead."""
    mgr = _ProbeMgr(["timeout", "timeout", "ok"])
    assert asyncio.run(_light(mgr)) is True
    assert mgr.calls == [("http://a", 2.0), ("http://b", 2.0),
                         ("http://a", 4.0)], "grace uses budget - sweep cost"
    assert mgr._ip_check_idx == 0, "grace does not advance the sticky index"


def test_probe_grace_fails_then_dead():
    mgr = _ProbeMgr(["timeout", "timeout", "timeout"])
    assert asyncio.run(_light(mgr)) is False
    assert mgr.calls == [("http://a", 2.0), ("http://b", 2.0),
                         ("http://a", 4.0)]


def test_probe_refused_is_definitive_no_grace():
    """ECONNREFUSED is definitive dead — refused verdicts never trigger the
    grace phase (only timeouts mean "might be slow")."""
    mgr = _ProbeMgr(["refused", "refused"])
    assert asyncio.run(_light(mgr)) is False
    assert mgr.calls == [("http://a", 2.0), ("http://b", 2.0)], \
        "no third (grace) attempt after refused verdicts"


def test_probe_error_is_definitive_no_grace():
    """Unknown errors (the fake httpx RuntimeError class) → dead, no grace:
    keeps the F2 offline tests' exact record assertions."""
    mgr = _ProbeMgr(["error", "error"])
    assert asyncio.run(_light(mgr)) is False
    assert mgr.calls == [("http://a", 2.0), ("http://b", 2.0)]


def test_probe_refused_then_alive_falls_through():
    mgr = _ProbeMgr(["refused", "ok"])
    assert asyncio.run(_light(mgr)) is True
    assert mgr.calls == [("http://a", 2.0), ("http://b", 2.0)]


def test_probe_grace_skipped_when_budget_exhausted():
    """budget 3.0: the sweep (2 × 2.0) already burns more than the budget —
    no leftover > 0.5 s → no grace attempt."""
    mgr = _ProbeMgr(["timeout", "timeout"], budget=3.0)
    assert asyncio.run(_light(mgr)) is False
    assert mgr.calls == [("http://a", 2.0), ("http://b", 2.0)]


def test_probe_single_endpoint_grace_uses_full_remaining():
    """One-endpoint config: sweep cost 2.0, grace gets the rest (6.0)."""
    mgr = _ProbeMgr(["timeout", "ok"], urls=["http://a"])
    assert asyncio.run(_light(mgr)) is True
    assert mgr.calls == [("http://a", 2.0), ("http://a", 6.0)]
