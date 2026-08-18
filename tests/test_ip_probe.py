"""test_ip_probe.py — [plan 18/08 §A] parallel bounded public-IP probe
(vpn_manager.py).

The old get_public_ip swept endpoints SEQUENTIALLY: n × per-request
timeout stacked on repeated callers (the 445 s stall class) and no
sweep-level cap. The refonte probes ALL endpoints in PARALLEL under
ONE bounded sweep (ip_probe_budget); the first success in endpoint
order from _ip_check_idx is sticky; a total failure OR budget overrun
resets the index and closes every client (cancelled __aexit__ — no
orphan AsyncClient, no unbounded stall).

Covered here (offline — httpx module stubbed into sys.modules, piège 4:
the probe imports httpx inside the function, never setattr on
vpn_manager):
  * parallel: every endpoint is queried even when the FIRST succeeds
    (the old sequential code short-circuited after the first hit)
  * sticky: first success in rotated order advances _ip_check_idx; all
    alive keeps it
  * total failure → None + index reset to 0
  * budget: a hung sweep (tunnel accepts but never answers) is
    cancelled at ip_probe_budget → None, index reset, clients closed
  * am.14: each _finalize_ip recovery round is bounded by
    _rotation_recovery_timeout (per-round, not the old flat 120 s that
    summed to the measured 445 s stall over 3 rounds)
"""
import asyncio
import sys
import time

import pytest

import vpn_manager as vm

from test_vpn_freshness import FakeVPNManager, _cfg


class _FakeResp:
    """httpx.Response stand-in: the probe only reads .text."""

    def __init__(self, text):
        self._text = text

    @property
    def text(self):
        return self._text


class _FakeClient:
    """httpx.AsyncClient stand-in for the get() shape of _probe_url.
    Records every URL, raises on dead endpoints, hangs forever when
    hang=True (budget test). __aenter__/__aexit__ mirror the real async
    context manager: a cancelled sweep runs __aexit__ → closed=True."""

    def __init__(self, behavior, record, hang):
        self._behavior, self._record, self._hang = behavior, record, hang
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def get(self, url):
        self._record.append(url)
        if self._hang:
            await asyncio.sleep(3600)       # cancelled by the sweep budget
        if url not in self._behavior:
            raise RuntimeError(f"connect failed: {url}")
        return _FakeResp(self._behavior[url])


class _FakeHttpx:
    """Fake httpx MODULE stubbed into sys.modules (piège 4)."""

    def __init__(self, behavior, record, hang=False):
        self._behavior, self._record, self._hang = behavior, record, hang
        self.clients = []

    def AsyncClient(self, **kw):
        client = _FakeClient(self._behavior, self._record, self._hang)
        self.clients.append(client)
        return client

    def Timeout(self, value):
        return value


class TestParallelBoundedProbe:
    """[plan 18/08 §A] the REAL get_public_ip under test: parallel sweep,
    sticky index, budget cap."""

    URLS = ["http://ip-a", "http://ip-b", "http://ip-c"]

    def _mgr(self, tmp_path, budget=8.0):
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        # Bind the REAL method — the fake class overrides get_public_ip
        # with a FIFO queue (same pattern as TestProbeTunnelLightEndpoints).
        mgr.get_public_ip = vm.VPNManager.get_public_ip.__get__(mgr)
        mgr._ip_check_urls = list(self.URLS)
        mgr._ip_check_idx = 0
        mgr._ip_probe_budget = budget
        return mgr

    def _patch(self, monkeypatch, behavior, hang=False):
        record = []
        fake = _FakeHttpx(behavior, record, hang=hang)
        monkeypatch.setitem(sys.modules, "httpx", fake)
        return fake, record

    @pytest.mark.asyncio
    async def test_parallel_sweep_queries_every_endpoint(self, tmp_path, monkeypatch):
        """The FIRST endpoint succeeds, yet ALL endpoints are queried —
        the parallel signature: the old sequential code stopped after
        the first hit. First success in endpoint order wins."""
        mgr = self._mgr(tmp_path)
        fake, record = self._patch(monkeypatch, {
            "http://ip-a": "1.1.1.1",
            "http://ip-b": "2.2.2.2",
            "http://ip-c": "3.3.3.3",
        })

        ip = await mgr.get_public_ip()

        assert ip == "1.1.1.1"
        assert set(record) == set(self.URLS), \
            "every endpoint is probed concurrently — no short-circuit"
        assert mgr._ip_check_idx == 0          # first success = index 0, kept
        assert all(c.closed for c in fake.clients)

    @pytest.mark.asyncio
    async def test_sticky_first_success_in_rotated_order(self, tmp_path, monkeypatch):
        """Index 1 → the sweep walks b, c, a: b dead, c answers → the
        FIRST success in that order wins and the index advances to c."""
        mgr = self._mgr(tmp_path)
        mgr._ip_check_idx = 1
        fake, record = self._patch(monkeypatch, {
            "http://ip-a": "1.1.1.1",
            "http://ip-c": "2.2.2.2",         # b absent = dead
        })

        ip = await mgr.get_public_ip()

        assert ip == "2.2.2.2"
        assert mgr._ip_check_idx == 2
        assert all(c.closed for c in fake.clients)

    @pytest.mark.asyncio
    async def test_sticky_kept_when_first_success_is_sticky(self, tmp_path, monkeypatch):
        """Index 1, all alive → b answers (i == 0) — the index stays 1:
        no gratuitous endpoint churn on a healthy chain."""
        mgr = self._mgr(tmp_path)
        mgr._ip_check_idx = 1
        fake, record = self._patch(monkeypatch, {
            "http://ip-a": "1.1.1.1",
            "http://ip-b": "2.2.2.2",
            "http://ip-c": "3.3.3.3",
        })

        ip = await mgr.get_public_ip()

        assert ip == "2.2.2.2"
        assert mgr._ip_check_idx == 1

    @pytest.mark.asyncio
    async def test_total_failure_resets_index(self, tmp_path, monkeypatch):
        """Every endpoint dead → None, index back to 0 (the next call
        restarts at the top of the chain)."""
        mgr = self._mgr(tmp_path)
        mgr._ip_check_idx = 2
        fake, record = self._patch(monkeypatch, {})

        ip = await mgr.get_public_ip()

        assert ip is None
        assert mgr._ip_check_idx == 0

    @pytest.mark.asyncio
    async def test_budget_cancels_hung_sweep(self, tmp_path, monkeypatch):
        """A tunnel that accepts the connection but never answers (the
        445 s stall class) is cancelled at ip_probe_budget: None, index
        reset, every client closed by the cancellation __aexit__ — no
        orphan AsyncClient, no unbounded stall."""
        mgr = self._mgr(tmp_path, budget=0.2)
        fake, record = self._patch(monkeypatch, {}, hang=True)

        t0 = time.monotonic()
        ip = await mgr.get_public_ip()
        elapsed = time.monotonic() - t0

        assert ip is None
        assert mgr._ip_check_idx == 0
        assert elapsed < 2.0, \
            "the sweep must be cancelled at the budget, not hang"
        assert len(record) == 3                # every endpoint was entered
        assert all(c.closed for c in fake.clients), \
            "cancelled clients must be closed (__aexit__ on CancelledError)"


class TestFinalizePerRoundBound:
    """[plan 18/08 §A/am.14] each _finalize_ip recovery round is bounded
    by _rotation_recovery_timeout (45 s default), not the old flat 120 s:
    the 445 s incident stall accumulated over 3 rounds × 120 s + docker
    legs. A tunnel that never comes back aborts the round at the bound
    and the next round restarts it — per-round, not a global cap."""

    @pytest.mark.asyncio
    async def test_recovery_round_honors_the_bound(self, tmp_path):
        """_wait_healthy that takes its timeout literally (a real tunnel
        that never answers would poll until the deadline): with the
        0.05 s bound the whole finalize aborts near 2 × bound instead
        of the old 2 × 120 s."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        mgr._rotation_recovery_timeout = 0.05
        healthy_calls = []

        async def slow_healthy(self, timeout=120.0):
            healthy_calls.append(timeout)
            await asyncio.sleep(timeout)     # deadline loop modeled literally
            return None

        mgr._wait_healthy = slow_healthy.__get__(mgr, type(mgr))

        t0 = time.monotonic()
        ok = await mgr._finalize_ip(allow_stale=False)
        elapsed = time.monotonic() - t0

        assert ok is False
        assert healthy_calls == [0.05, 0.05], \
            "both recovery rounds run, each bounded at the config timeout"
        assert elapsed < 2.0, \
            "the old flat 120 s bound would take ~240 s here"
