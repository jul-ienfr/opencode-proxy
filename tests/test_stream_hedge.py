"""Hedge de flux : 2 stations en course, la première qui émet un token gagne.

Mesure 2026-10-25 : le hedge existant était réservé au non-stream. En
streaming, le client attend ; si la station primaire est lente, une seconde
peut produire le premier token avant elle. Le perdant est annulé et fermé,
la comptabilité n'est incrémentée que pour le gagnant.
"""

from contextlib import asynccontextmanager

import pytest

import opencode as oc


class _Resp:
    def __init__(self, name, lines, status=200, delay=0.0):
        self.name = name
        self._lines = list(lines)
        self.status_code = status
        self._delay = delay
        self.closed = False

    async def aiter_lines(self):
        if self._delay:
            import asyncio

            await asyncio.sleep(self._delay)
        for line in self._lines:
            yield line

    async def aclose(self):
        self.closed = True


class _Station:
    def __init__(self, n):
        self._station = n
        self.current_ip = f"10.0.0.{n}"
        self.socks5_url = f"socks5://127.0.0.1:{1080 + n}"


class _Pool:
    def __init__(self, stations):
        self._stations = stations
        self.enabled = True
        self.winners = []

    def pick_candidates(self, forced_pool=None):
        return list(self._stations)

    def note_hedge_winner(self, winner, primary=None):
        self.winners.append((winner, primary))


@pytest.fixture()
def hedge_env(monkeypatch):
    monkeypatch.setitem(
        oc.IP_ROTATION,
        "free_parallel",
        {"enabled": True, "mode": "hedge", "hedge_delay_ms": 50, "hedge_max_attempts": 1},
    )


@pytest.mark.asyncio
async def test_le_plus_rapide_gagne_et_le_perdant_est_ferme(monkeypatch, hedge_env):
    s1, s2 = _Station(1), _Station(2)
    pool = _Pool([s1, s2])
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    resps = {}

    @asynccontextmanager
    async def _fake_open(endpoint, body, headers, use_free, count_request=True,
                         fresh_station=False, direct_fallback=True,
                         forced_pool=None, forced_station=None):
        if forced_station is s1:
            r = _Resp("slow", ['data: {"choices":[{"delta":{"content":"lent"}}]}'], delay=0.4)
        else:
            r = _Resp("fast", ['data: {"choices":[{"delta":{"content":"vite"}}]}'], delay=0.0)
        resps[getattr(forced_station, "_station", 0)] = r
        try:
            yield r
        finally:
            pass

    monkeypatch.setattr(oc, "_open_free_stream", _fake_open)

    async with oc._open_free_stream_hedged(
        "https://h", {"stream": True}, {}, True, True, False, True, None
    ) as resp:
        lines = [ln async for ln in resp.aiter_lines()]

    assert "vite" in lines[0]
    assert resps[2].closed is True          # perdant fermé
    assert pool.winners and pool.winners[0][0] is s2


@pytest.mark.asyncio
async def test_pas_de_seconde_requete_si_la_primaire_repond_vite(monkeypatch, hedge_env):
    s1, s2 = _Station(1), _Station(2)
    pool = _Pool([s1, s2])
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    opened = []

    @asynccontextmanager
    async def _fake_open(endpoint, body, headers, use_free, count_request=True,
                         fresh_station=False, direct_fallback=True,
                         forced_pool=None, forced_station=None):
        opened.append(getattr(forced_station, "_station", 0))
        r = _Resp("fast", ['data: {"choices":[{"delta":{"content":"ok"}}]}'])
        try:
            yield r
        finally:
            pass

    monkeypatch.setattr(oc, "_open_free_stream", _fake_open)

    async with oc._open_free_stream_hedged(
        "https://h", {"stream": True}, {}, True, True, False, True, None
    ) as resp:
        _ = [ln async for ln in resp.aiter_lines()]

    assert opened == [1]                    # aucune 2e requête
    assert pool.winners[0][0] is s1


@pytest.mark.asyncio
async def test_429_sur_la_primaire_laisse_gagner_la_seconde(monkeypatch, hedge_env):
    s1, s2 = _Station(1), _Station(2)
    pool = _Pool([s1, s2])
    monkeypatch.setattr(oc, "_free_ip_pool", pool)

    @asynccontextmanager
    async def _fake_open(endpoint, body, headers, use_free, count_request=True,
                         fresh_station=False, direct_fallback=True,
                         forced_pool=None, forced_station=None):
        if forced_station is s1:
            r = _Resp("429", ['{"error":"quota"}'], status=429)
        else:
            r = _Resp("ok", ['data: {"choices":[{"delta":{"content":"ok"}}]}'], delay=0.05)
        try:
            yield r
        finally:
            pass

    monkeypatch.setattr(oc, "_open_free_stream", _fake_open)

    async with oc._open_free_stream_hedged(
        "https://h", {"stream": True}, {}, True, True, False, True, None
    ) as resp:
        assert resp.status_code == 200
        lines = [ln async for ln in resp.aiter_lines()]

    assert "ok" in lines[0]
    assert pool.winners[0][0] is s2


@pytest.mark.asyncio
async def test_replay_rend_la_premiere_ligne_puis_le_reste():
    r = _Resp("x", ["a", "b", "c"])
    replay = oc._ReplayStreamResponse(r, "zero")
    assert [ln async for ln in replay.aiter_lines()] == ["zero", "a", "b", "c"]
    assert [ln async for ln in replay.aiter_lines()] == ["a", "b", "c"]


def test_hedge_desactive_par_defaut_hors_mode_hedge(monkeypatch):
    monkeypatch.setitem(
        oc.IP_ROTATION, "free_parallel", {"enabled": True, "mode": "load-balance"}
    )
    assert oc._stream_hedge_enabled() is False
    monkeypatch.setitem(
        oc.IP_ROTATION, "free_parallel", {"enabled": True, "mode": "hedge"}
    )
    assert oc._stream_hedge_enabled() is True
    monkeypatch.setitem(
        oc.IP_ROTATION, "free_parallel", {"enabled": False, "mode": "hedge"}
    )
    assert oc._stream_hedge_enabled() is False
