"""test_faux_429_veridique.py — [PLAN_CORRECTION_FAUX_429 Lot E] refus véridique.

E1: free 503 strict_free → 503 api_error + vrai message (jamais 429)
E2: vrais 429 toutes stations → 429 quota + Retry-After réel du dernier 429
E3: échecs tunnel toutes stations → 503 « no usable VPN station/tunnel »
E4: collect-stream (/v1/messages stream=true) + free 503 → 503 api_error, pas « quota »
E5: free 503 station1, 200 station2 → succès 200 (B1), usage loggé 2 essais
E6: 503 SANS Retry-After → réponse SANS header fabriqué (+ 429 sans RA → sans header)
"""

import pytest

import opencode as oc

PAID_MODEL = "paid-test-model"
FREE_MODEL = "free-test-model"
PAID_HEADERS = {"Content-Type": "application/json"}


class _Station:
    def __init__(self, n, ip, socks5_url):
        self._station = n
        self.status = "connected"
        self.current_ip = ip
        self.socks5_url = socks5_url
        self._quota_per_ip = 15
        self.current_identity = {
            "impersonate": "chrome131",
            "user_agent": None,
            "extra_headers": {},
        }


class _StubVpn:
    proxy_mode = "vpn"
    current_ip = "10.0.0.1"


class _FakeResp:
    def __init__(self, status_code, headers=None, usage=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._usage = usage or {}
        self.text = text

    def json(self):
        return {
            "usage": self._usage,
            "choices": [{"message": {"role": "assistant", "content": "echo"}}],
        }


class _FakeFreeCurl:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, body, headers, proxy_url=None, station=None, endpoint=None, **kwargs):
        self.calls.append((proxy_url, station))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _PoolMulti:
    enabled = True
    active_station = None

    def __init__(self, stations):
        self._stations = stations
        self.rotated = []
        self.requests = 0

    async def on_request(self):
        self.requests += 1
        best = self._best_station()
        return best.socks5_url if best else None, best

    def _best_station(self):
        for st in self._stations:
            if st.status == "connected":
                return st
        return None

    def _best_station_excluding_many(self, excluded, forced_pool=None):
        for st in self._stations:
            if st in excluded:
                continue
            if st.status == "connected":
                return st
        return None

    def _station_usable(self, st, exclude_approaching=False, forced_pool=None):
        return st.status == "connected"

    def pick_candidates(self, forced_pool=None):
        return [st for st in self._stations if st.status == "connected"]

    def on_quota_exhausted(self, station):
        self.rotated.append(station)

    def note_hedge_winner(self, winner, primary=None):
        pass


@pytest.fixture
def free_vpn_env(monkeypatch):
    monkeypatch.setattr(oc, "_vpn_manager", _StubVpn())
    monkeypatch.setattr(oc, "_get_cached_public_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "FREE_MODEL_MAP", {PAID_MODEL: FREE_MODEL})
    oc._free_model_cooldowns.clear()
    return oc


@pytest.fixture
def free_cfg():
    saved = {k: oc.IP_ROTATION.get(k) for k in (
        "max_free_attempts", "free_exception_fallback", "strict_free",
        "auto_max_free_attempts", "station_count", "free_parallel", "on_429_action")}
    if isinstance(saved.get("free_parallel"), dict):
        saved["free_parallel"] = dict(saved["free_parallel"])
    oc.IP_ROTATION["station_count"] = 2
    oc.IP_ROTATION["strict_free"] = False
    oc.IP_ROTATION["on_429_action"] = "both"
    oc.IP_ROTATION["auto_max_free_attempts"] = False
    oc.IP_ROTATION["max_free_attempts"] = 2
    oc.IP_ROTATION["free_exception_fallback"] = "station-first"
    oc.IP_ROTATION["free_parallel"] = {
        "enabled": False, "routing": "round-robin", "mode": "load-balance",
        "hedge_delay_ms": 300, "hedge_max_attempts": 1}
    try:
        import config.settings as _st
        _st.FREE_PARALLEL.clear()
        _st.FREE_PARALLEL.update(_st._normalize_free_parallel(oc.IP_ROTATION["free_parallel"]))
    except Exception:
        pass
    yield
    for k, v in saved.items():
        if v is None:
            oc.IP_ROTATION.pop(k, None)
        else:
            oc.IP_ROTATION[k] = dict(v) if isinstance(v, dict) else v


def _free_body():
    return {"model": PAID_MODEL, "messages": [{"role": "user", "content": "hello"}]}


def _resp_json(resp):
    import json as _j
    return _j.loads(bytes(resp.body).decode())


# ── E1: 503 strict_free → 503 api_error, jamais 429 ──
@pytest.mark.asyncio
async def test_E1_free_503_strict_free_refuse_503(free_vpn_env, free_cfg, monkeypatch):
    oc.IP_ROTATION["strict_free"] = True
    oc.IP_ROTATION["max_free_attempts"] = 1
    a = _Station(1, "1.1.1.1", "socks5://127.0.0.1:1080")
    monkeypatch.setattr(oc, "_free_ip_pool", _PoolMulti([a]))
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi",
                        _FakeFreeCurl([_FakeResp(503, {}, text="Service Unavailable upstream")]))
    monkeypatch.setattr(oc, "_log_free_model_usage", lambda *a_, **k: None)
    with pytest.raises(oc.FreeRefusal) as ei:
        await oc._try_free_model_first(_free_body(), dict(PAID_HEADERS), "openai", PAID_MODEL)
    exc = ei.value
    assert exc.status == 503, f"un 503 upstream doit lever 503, pas {exc.status}"
    assert "Service Unavailable" in exc.body
    assert exc.retry_after == "", "pas de Retry-After inventé"
    # Réponse HTTP véridique
    for proto in ("openai", "anthropic"):
        r = oc._free_refusal_response(exc, proto)
        assert r.status_code == 503, f"{proto}: attendu 503, reçu {r.status_code}"
        assert "Retry-After" not in r.headers, f"{proto}: header fabriqué interdit"
        payload = _resp_json(r)
        txt = str(payload)
        assert "503" in txt and "Service Unavailable" in txt
        assert "quota" not in txt.lower(), "le mot quota ne doit jamais apparaître sur un 503"


# ── E2: vrais 429 → 429 + Retry-After réel ──
@pytest.mark.asyncio
async def test_E2_true_429_keeps_real_retry_after(free_vpn_env, free_cfg, monkeypatch):
    oc.IP_ROTATION["strict_free"] = True
    oc.IP_ROTATION["max_free_attempts"] = 1
    a = _Station(1, "1.1.1.1", "socks5://127.0.0.1:1080")
    monkeypatch.setattr(oc, "_free_ip_pool", _PoolMulti([a]))
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi",
                        _FakeFreeCurl([_FakeResp(429, {"retry-after": "45"}, text="rate limited")]))
    monkeypatch.setattr(oc, "_log_free_model_usage", lambda *a_, **k: None)
    with pytest.raises(oc.FreeRefusal) as ei:
        await oc._try_free_model_first(_free_body(), dict(PAID_HEADERS), "openai", PAID_MODEL)
    exc = ei.value
    assert exc.status == 429
    assert exc.retry_after == "45", f"Retry-After réel attendu '45', reçu {exc.retry_after!r}"
    r = oc._free_refusal_response(exc, "openai")
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "45"
    assert "quota" in _resp_json(r).__str__().lower()


@pytest.mark.asyncio
async def test_E2b_two_429_propagates_last_retry_after(free_vpn_env, free_cfg, monkeypatch):
    """2 stations, 429(30) puis 429(60) final → le refus porte le dernier RA (60)."""
    oc.IP_ROTATION["strict_free"] = True
    oc.IP_ROTATION["max_free_attempts"] = 2
    a = _Station(1, "1.1.1.1", "socks5://127.0.0.1:1080")
    b = _Station(2, "2.2.2.2", "socks5://127.0.0.1:1081")
    monkeypatch.setattr(oc, "_free_ip_pool", _PoolMulti([a, b]))
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", _FakeFreeCurl([
        _FakeResp(429, {"retry-after": "30"}, text="rl1"),
        _FakeResp(429, {"retry-after": "60"}, text="rl2")]))
    monkeypatch.setattr(oc, "_log_free_model_usage", lambda *a_, **k: None)
    with pytest.raises(oc.FreeRefusal) as ei:
        await oc._try_free_model_first(_free_body(), dict(PAID_HEADERS), "openai", PAID_MODEL)
    assert ei.value.status == 429
    assert ei.value.retry_after == "60"


# ── E3: tunnels morts → 503 tunnel ──
@pytest.mark.asyncio
async def test_E3_tunnel_failures_refuse_503(free_vpn_env, free_cfg, monkeypatch):
    oc.IP_ROTATION["strict_free"] = True
    oc.IP_ROTATION["max_free_attempts"] = 2
    a = _Station(1, "1.1.1.1", "socks5://127.0.0.1:1080")
    b = _Station(2, "2.2.2.2", "socks5://127.0.0.1:1081")
    monkeypatch.setattr(oc, "_free_ip_pool", _PoolMulti([a, b]))
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi",
                        _FakeFreeCurl([RuntimeError("SOCKS5 dead A"), RuntimeError("SOCKS5 dead B")]))
    monkeypatch.setattr(oc, "_log_free_model_usage", lambda *a_, **k: None)
    with pytest.raises(oc.FreeRefusal) as ei:
        await oc._try_free_model_first(_free_body(), dict(PAID_HEADERS), "openai", PAID_MODEL)
    exc = ei.value
    assert exc.status == 503, f"tunnel mort → 503, reçu {exc.status}"
    assert "no usable VPN station/tunnel" in exc.body
    assert "quota" not in exc.body.lower()
    r = oc._free_refusal_response(exc, "anthropic")
    assert r.status_code == 503
    assert "quota" not in str(_resp_json(r)).lower()
    assert "Retry-After" not in r.headers


# ── E4: collect-stream mindset — même helper, protocole anthropic ──
def test_E4_collect_stream_503_anthropique_veridique():
    """Le chemin collect-stream /v1/messages utilise _free_refusal_response :
    un 503 free doit donner un refus 503 api_error côté anthropic."""
    exc = oc.FreeRefusal(status=503, body="upstream overloaded", retry_after="")
    r = oc._free_refusal_response(exc, "anthropic")
    assert r.status_code == 503
    body = _resp_json(r)
    err = body.get("error", {})
    assert err.get("type") == "api_error"
    assert "503" in err.get("message", "") and "overloaded" in err.get("message", "")
    assert "quota" not in err.get("message", "").lower()
    # décision C1 documentée en code (HTTP JSON véridique, pas SSE)
    import inspect
    src = inspect.getsource(oc)
    assert "Lot C1" in src and "_free_refusal_response" in src


# ── E5: 503 puis 200 → succès (B1) ──
@pytest.mark.asyncio
async def test_E5_503_then_200_succeeds(free_vpn_env, free_cfg, monkeypatch):
    oc.IP_ROTATION["strict_free"] = True
    oc.IP_ROTATION["max_free_attempts"] = 2
    a = _Station(1, "1.1.1.1", "socks5://127.0.0.1:1080")
    b = _Station(2, "2.2.2.2", "socks5://127.0.0.1:1081")
    monkeypatch.setattr(oc, "_free_ip_pool", _PoolMulti([a, b]))
    fake = _FakeFreeCurl([
        _FakeResp(503, {}, text="flap"),
        _FakeResp(200, {"content-type": "application/json"},
                  usage={"prompt_tokens": 3, "completion_tokens": 5}),
    ])
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", fake)
    usage_calls = []
    monkeypatch.setattr(oc, "_log_free_model_usage",
                        lambda *a_, **k: usage_calls.append(a_[4] if len(a_) > 4 else k.get("status")))
    result = await oc._try_free_model_first(_free_body(), dict(PAID_HEADERS), "openai", PAID_MODEL)
    assert result is not None and result[0].status_code == 200
    assert [s for _, s in fake.calls] == [a, b], "station 1 (503) puis station 2 (200)"
    assert usage_calls == [503, 200], f"usage loggé pour les 2 essais, reçu {usage_calls}"


# ── E6: pas de header fabriqué ──
def test_E6_no_fabricated_retry_after():
    e503 = oc.FreeRefusal(status=503, body="x", retry_after="")
    for proto in ("openai", "anthropic"):
        r = oc._free_refusal_response(e503, proto)
        assert "Retry-After" not in r.headers, f"{proto}: 503 sans RA upstream → pas de header"
    e429_nora = oc.FreeRefusal(status=429, body="rl", retry_after="")
    r = oc._free_refusal_response(e429_nora, "openai")
    assert r.status_code == 429
    assert "Retry-After" not in r.headers, "429 sans RA upstream → pas de header inventé"
    assert "quota" in str(_resp_json(r)).lower()
    # legacy wrapper délégué (compat)
    r2 = oc._free_quota_exhausted_response(e429_nora, "openai")
    assert r2.status_code == 429 and "Retry-After" not in r2.headers


def test_legacy_FreeQuotaExhausted_still_compat():
    e = oc.FreeQuotaExhausted("45")
    assert e.status == 429 and e.retry_after == "45"
    assert isinstance(e, oc.FreeRefusal)
    r = oc._free_refusal_response(e, "openai")
    assert r.status_code == 429 and r.headers.get("Retry-After") == "45"
