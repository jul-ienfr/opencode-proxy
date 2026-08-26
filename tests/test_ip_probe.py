"""test_ip_probe.py — [plan 18/08 §A] public-IP probe (vpn_manager.py).

[P3 perf] contrat sticky-first : l'endpoint STICKY (_ip_check_idx) est
interrogé SEUL en séquentiel — le cas nominal coûte exactement UN GET.
Le sweep parallèle borné par ``ip_probe_budget`` ne sert qu'en FALLBACK
(sticky mort). Le client httpx est RÉUTILISÉ entre probes (cache par
socks5_url) : plus de handshake SOCKS5+TLS par GET ni de fermeture à
chaque appel — les asserts ``closed`` de l'ancien sweep sont remplacés
par des asserts de RÉUTILISATION (un seul client instancié).

Covered here (offline — httpx module stubbed into sys.modules, piège 4):
  * sticky hit : une chaîne saine = un seul GET, index inchangé
  * fallback : sticky mort → sweep parallèle des AUTRES, premier succès
    dans l'ordre roté gagne et avance l'index
  * total failure → None + index reset à 0
  * budget : un sweep qui pend est annulé à ip_probe_budget → None,
    index reset (le sticky échoue vite, lui, à son per_attempt)
"""

import asyncio
import sys
import time

import pytest
from test_vpn_freshness import FakeVPNManager, _cfg

import vpn_manager as vm


class _FakeResp:
    """httpx.Response stand-in: the probe only reads .text."""

    def __init__(self, text):
        self._text = text

    @property
    def text(self):
        return self._text


class _FakeClient:
    """httpx.AsyncClient stand-in for the get() shape of _probe_url.

    Records every URL, raises on dead endpoints, hangs forever for URLs
    in ``hang_urls`` (budget test). [P3] le client réel est réutilisé :
    plus de __aenter__/__aexit__ par probe."""

    def __init__(self, behavior, record, hang_urls):
        self._behavior, self._record, self._hang = behavior, record, hang_urls
        self.closed = False

    async def get(self, url):
        self._record.append(url)
        if url in self._hang:
            await asyncio.sleep(3600)  # cancelled by the sweep budget
        if url not in self._behavior:
            raise RuntimeError(f"connect failed: {url}")
        return _FakeResp(self._behavior[url])


class _FakeHttpx:
    """Fake httpx MODULE stubbed into sys.modules (piège 4)."""

    def __init__(self, behavior, record, hang_urls=frozenset()):
        self._behavior, self._record, self._hang = behavior, record, hang_urls
        self.clients = []

    def AsyncClient(self, **kw):
        client = _FakeClient(self._behavior, self._record, self._hang)
        self.clients.append(client)
        return client

    def Timeout(self, value):
        return value


class TestStickyFirstProbe:
    """[P3 perf] le VRAI get_public_ip sous test : sticky-first, fallback
    parallèle borné, réutilisation du client."""

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

    def _patch(self, monkeypatch, behavior, hang_urls=frozenset()):
        record = []
        fake = _FakeHttpx(behavior, record, hang_urls=hang_urls)
        monkeypatch.setitem(sys.modules, "httpx", fake)
        return fake, record

    @pytest.mark.asyncio
    async def test_healthy_chain_costs_exactly_one_probe(self, tmp_path, monkeypatch):
        """Chaîne saine : le sticky répond → UN SEUL GET, pas de sweep."""
        mgr = self._mgr(tmp_path)
        fake, record = self._patch(
            monkeypatch,
            {
                "http://ip-a": "1.1.1.1",
                "http://ip-b": "2.2.2.2",
                "http://ip-c": "3.3.3.3",
            },
        )

        ip = await mgr.get_public_ip()

        assert ip == "1.1.1.1"
        assert record == ["http://ip-a"], (
            "le cas nominal = exactement un GET sur l'endpoint sticky"
        )
        assert mgr._ip_check_idx == 0  # succès sticky → index inchangé
        assert len(fake.clients) == 1, "client réutilisé, pas recréé"

    @pytest.mark.asyncio
    async def test_fallback_sweep_on_sticky_failure(self, tmp_path, monkeypatch):
        """Index 1 → sticky b mort → sweep parallèle de (c, a) : c gagne
        (premier succès dans l'ordre roté) et l'index avance vers c."""
        mgr = self._mgr(tmp_path)
        mgr._ip_check_idx = 1
        fake, record = self._patch(
            monkeypatch,
            {
                "http://ip-a": "1.1.1.1",
                "http://ip-c": "2.2.2.2",  # b absent = dead
            },
        )

        ip = await mgr.get_public_ip()

        assert ip == "2.2.2.2"
        assert mgr._ip_check_idx == 2
        assert record.count("http://ip-b") == 1, "sticky essayé une fois"
        assert set(record) >= {"http://ip-b", "http://ip-c"}, (
            "fallback : les autres endpoints sont sondés"
        )
        assert len(fake.clients) == 1

    @pytest.mark.asyncio
    async def test_sticky_kept_when_all_alive(self, tmp_path, monkeypatch):
        """Index 1, tout vivant → b répond (un seul GET), l'index reste 1 :
        aucun churn d'endpoint sur une chaîne saine."""
        mgr = self._mgr(tmp_path)
        mgr._ip_check_idx = 1
        fake, record = self._patch(
            monkeypatch,
            {
                "http://ip-a": "1.1.1.1",
                "http://ip-b": "2.2.2.2",
                "http://ip-c": "3.3.3.3",
            },
        )

        ip = await mgr.get_public_ip()

        assert ip == "2.2.2.2"
        assert mgr._ip_check_idx == 1
        assert record == ["http://ip-b"]

    @pytest.mark.asyncio
    async def test_total_failure_resets_index(self, tmp_path, monkeypatch):
        """Tous morts → None, index reset à 0 (prochain appel repart du haut),
        les trois endpoints ont été entrés (sticky + sweep)."""
        mgr = self._mgr(tmp_path)
        mgr._ip_check_idx = 2
        fake, record = self._patch(monkeypatch, {})

        ip = await mgr.get_public_ip()

        assert ip is None
        assert mgr._ip_check_idx == 0
        assert set(record) == set(self.URLS)

    @pytest.mark.asyncio
    async def test_budget_cancels_hung_sweep(self, tmp_path, monkeypatch):
        """Un tunnel qui accepte mais ne répond JAMAIS (classe 445 s) :
        le sticky échoue vite (erreur immédiate), puis le sweep pendant est
        annulé au budget ip_probe_budget → None, index reset. Le client
        réutilisé n'est PAS fermé (par design — cache par socks5_url)."""
        mgr = self._mgr(tmp_path, budget=0.2)
        # ip-a absent → erreur immédiate (sticky rapide) ; b/c pendent
        fake, record = self._patch(
            monkeypatch, {}, hang_urls={"http://ip-b", "http://ip-c"}
        )

        t0 = time.monotonic()
        ip = await mgr.get_public_ip()
        elapsed = time.monotonic() - t0

        assert ip is None
        assert mgr._ip_check_idx == 0
        assert elapsed < 2.5, "sticky rapide + sweep annulé au budget"
        assert set(record) == set(self.URLS), "tous les endpoints ont été entrés"


class TestParallelBoundedProbeCompat:
    """Compat historique : avec M endpoints tous vivants et un sticky qui
    échoue, le fallback reste PARALLÈLE (les deux autres sont entrés avant
    que le premier ne réponde) et borné."""


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
            await asyncio.sleep(timeout)  # deadline loop modeled literally
            return None

        mgr._wait_healthy = slow_healthy.__get__(mgr, type(mgr))

        t0 = time.monotonic()
        ok = await mgr._finalize_ip(allow_stale=False)
        elapsed = time.monotonic() - t0

        assert ok is False
        assert healthy_calls == [0.05, 0.05], (
            "both recovery rounds run, each bounded at the config timeout"
        )
        assert elapsed < 2.0, "the old flat 120 s bound would take ~240 s here"
