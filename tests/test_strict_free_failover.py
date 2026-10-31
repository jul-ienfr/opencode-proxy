"""strict_free failover : un bucket trial-mort ne doit plus refuser quand un frere est vivant."""

import pytest

import opencode as oc


def _clear():
    oc._free_model_rr.clear()
    oc._free_model_cooldowns.clear()
    try:
        oc._free_cooldown_scope.clear()
    except Exception:
        pass


def test_candidate_chain_endpoint_aware(monkeypatch):
    _clear()
    monkeypatch.setitem(oc.IP_ROTATION, "free_substitution_policy", "family")
    chain = oc._free_candidate_chain("muse-spark-1.3-contributor", "muse-spark-1.3-contributor-free")
    assert chain[0] == "muse-spark-1.3-contributor-free"
    assert "jev-1.13-free" not in chain
    chain_chat = oc._free_candidate_chain("mimo-v2.5", "mimo-v2.6-flash-free")
    assert chain_chat[0] == "mimo-v2.6-flash-free"
    assert "jev-1.13-free" not in chain_chat
    assert "muse-spark-1.3-contributor-free" not in chain_chat
    # Modèles morts amont (vérifié live) : jamais proposés en repli.
    for _dead in ("mimo-v2.5-free", "deepseek-v4-flash-free", "space-bunny-free"):
        assert _dead not in chain_chat, f"{_dead} est mort amont (410/400)"
    chain_sys = oc._free_candidate_chain("jev-1.13", "jev-1.13-free")
    assert chain_sys == ["jev-1.13-free"]


def test_free_ids_are_never_remapped():
    """Un id déjà free est servi tel quel — jamais remplacé par un autre free."""
    _clear()
    assert oc._resolve_free_model("mimo-v2.5-free") == "mimo-v2.5-free"
    assert oc._resolve_free_model("minimax-m3.1-flash-free") == "minimax-m3.1-flash-free"


def test_resolve_failover_to_alive(monkeypatch):
    _clear()
    monkeypatch.setitem(oc.IP_ROTATION, "free_model_spread", False)
    monkeypatch.setitem(oc.IP_ROTATION, "free_substitution_policy", "family")
    monkeypatch.setitem(
        oc.FREE_MODEL_MAP,
        "mimo-v2.5",
        "mimo-v2.6-flash-free",
    )
    monkeypatch.setitem(
        oc.IP_ROTATION,
        "free_model_candidates",
        {"mimo-v2.5": ["mimo-v2.6-flash-free", "mimo-v2.6-pro-free"]},
    )

    class _P:
        _stations = [object(), object()]
        enabled = True

    monkeypatch.setattr(oc, "_free_ip_pool", _P())

    def _exhausted(m):
        return m == "mimo-v2.6-flash-free"

    monkeypatch.setattr(oc, "_free_stations_exhausted", _exhausted)
    got = oc._resolve_free_model("mimo-v2.5")
    assert got == "mimo-v2.6-pro-free"


def test_resolve_no_cross_family_failover(monkeypatch):
    """Même si un frère d'une AUTRE famille est vivant, on ne le sert jamais."""
    _clear()
    monkeypatch.setitem(oc.IP_ROTATION, "free_model_spread", False)
    monkeypatch.setitem(oc.IP_ROTATION, "free_substitution_policy", "family")
    monkeypatch.setitem(oc.FREE_MODEL_MAP, "mimo-v2.5", "mimo-v2.6-flash-free")
    monkeypatch.setitem(
        oc.IP_ROTATION,
        "free_model_candidates",
        {"mimo-v2.5": ["mimo-v2.6-flash-free", "deepseek-v4.1-flash-free"]},
    )

    class _P:
        _stations = [object()]
        enabled = True

    monkeypatch.setattr(oc, "_free_ip_pool", _P())
    monkeypatch.setattr(oc, "_free_stations_exhausted", lambda m: True)
    assert oc._resolve_free_model("mimo-v2.5") == "mimo-v2.6-flash-free"


def test_resolve_no_failover_when_pool_boot(monkeypatch):
    _clear()
    monkeypatch.setitem(oc.IP_ROTATION, "free_model_spread", False)
    monkeypatch.setitem(oc.IP_ROTATION, "free_substitution_policy", "family")
    monkeypatch.setitem(oc.FREE_MODEL_MAP, "mimo-v2.5", "mimo-v2.6-flash-free")
    monkeypatch.setattr(oc, "_free_ip_pool", None)
    assert oc._resolve_free_model("mimo-v2.5") == "mimo-v2.6-flash-free"


@pytest.mark.asyncio
async def test_all_dead_waits_then_refuses_honestly(monkeypatch):
    """Tout mort + rien ne bouge pendant l'attente → refus 429 honnête, sans appel amont."""
    _clear()
    monkeypatch.setitem(oc.IP_ROTATION, "strict_free", True)
    monkeypatch.setitem(oc.IP_ROTATION, "free_model_spread", False)
    monkeypatch.setitem(oc.FREE_MODEL_MAP, "paid-x", "paid-x-free")
    monkeypatch.setitem(
        oc.IP_ROTATION,
        "free_model_candidates",
        {"paid-x": ["paid-x-free"]},
    )

    class _P:
        _stations = [object()]
        enabled = True

    monkeypatch.setattr(oc, "_free_ip_pool", _P())
    monkeypatch.setattr(oc, "_free_stations_exhausted", lambda m: True)
    monkeypatch.setattr(oc, "_free_min_retry_after", lambda models: "123")
    monkeypatch.setattr(oc, "_free_proxy_mode", lambda: "vpn")
    monkeypatch.setattr(oc, "_fallback_ctx_push", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)

    async def _wait_false(models, timeout_s=45.0, poll_s=3.0, sleep_fn=None):
        return False

    monkeypatch.setattr(oc, "_wait_for_fresh_free", _wait_false)

    async def _never(*a, **k):
        raise AssertionError("aucun appel amont quand tout est mort")

    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", _never)

    with pytest.raises(oc.FreeQuotaExhausted):
        await oc._try_free_model_first(
            {"model": "paid-x", "messages": [{"role": "user", "content": "hi"}]},
            {},
            "openai",
            "paid-x",
            req_id="test-no-block-refuse",
        )


@pytest.mark.asyncio
async def test_all_dead_retries_when_fresh_ip_lands(monkeypatch):
    """Tout mort + IP fraîche pendant l'attente → on retente l'amont (pas de blocage)."""
    _clear()
    monkeypatch.setitem(oc.IP_ROTATION, "strict_free", True)
    monkeypatch.setitem(oc.IP_ROTATION, "free_model_spread", False)
    monkeypatch.setitem(oc.FREE_MODEL_MAP, "paid-x", "paid-x-free")
    monkeypatch.setitem(
        oc.IP_ROTATION,
        "free_model_candidates",
        {"paid-x": ["paid-x-free"]},
    )

    from test_free_multi_attempt import _Station as _RealisticStation

    station = _RealisticStation(1, "9.9.9.9", "socks5://127.0.0.1:1080")

    class _P:
        _stations = [station]
        enabled = True
        socks5_mode = False

        def _best_station(self, forced_pool=None):
            return station

        async def on_request(self, forced_pool=None):
            return station.socks5_url, station

    monkeypatch.setattr(oc, "_free_ip_pool", _P())
    monkeypatch.setattr(oc, "_free_stations_exhausted", lambda m: True)
    monkeypatch.setattr(oc, "_free_proxy_mode", lambda: "vpn")
    monkeypatch.setattr(oc, "_fallback_ctx_push", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log_free_model_usage", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_get_cached_public_ip", lambda: "127.0.0.1")

    class _StubVpn:
        proxy_mode = "vpn"
        current_ip = "10.0.0.1"

    monkeypatch.setattr(oc, "_vpn_manager", _StubVpn())

    async def _wait_true(models, timeout_s=45.0, poll_s=3.0, sleep_fn=None):
        return True

    monkeypatch.setattr(oc, "_wait_for_fresh_free", _wait_true)

    calls = []

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = '{"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}}'

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}}

    async def _fake_curl(body, headers, proxy_url=None, station=None, endpoint=None, **kw):
        calls.append((proxy_url, station))
        return _FakeResp()

    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", _fake_curl)

    out = await oc._try_free_model_first(
        {"model": "paid-x", "messages": [{"role": "user", "content": "hi"}]},
        {},
        "openai",
        "paid-x",
        req_id="test-no-block-retry",
    )
    assert out is not None, "IP fraîche → la requête repart au lieu d'être bloquée"
    assert calls, "l'amont a bien été retenté après l'attente"


@pytest.mark.asyncio
async def test_wait_returns_true_on_fresh_key(monkeypatch):
    """_wait_for_fresh_free : True dès qu'une clé redevient servable."""
    _clear()

    class _P:
        _stations = [object(), object()]
        enabled = True

        def _launch_rotation(self, st):
            pass

    monkeypatch.setattr(oc, "_free_ip_pool", _P())
    states = {"dead": True}

    async def _fake_sleep(s):
        states["dead"] = False

    monkeypatch.setattr(oc, "_free_stations_exhausted", lambda m: states["dead"])
    assert await oc._wait_for_fresh_free(["m"], timeout_s=10.0, poll_s=1.0, sleep_fn=_fake_sleep) is True


@pytest.mark.asyncio
async def test_wait_returns_false_when_nothing_moves(monkeypatch):
    """_wait_for_fresh_free : False après le délai si rien ne bouge."""
    _clear()

    class _P:
        _stations = [object()]
        enabled = True

        def _launch_rotation(self, st):
            pass

    monkeypatch.setattr(oc, "_free_ip_pool", _P())
    monkeypatch.setattr(oc, "_free_stations_exhausted", lambda m: True)

    async def _fake_sleep(s):
        return None

    assert await oc._wait_for_fresh_free(["m"], timeout_s=0.05, poll_s=0.01, sleep_fn=_fake_sleep) is False
