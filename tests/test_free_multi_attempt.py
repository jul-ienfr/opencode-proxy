"""test_free_multi_attempt.py — [plan 19/08 §1/§2] multi-station free retry.

Verifies the two GUI-configurable free-recovery options:

  max_free_attempts        (1-3, default 2)  — free strikes per request,
                              each on a DIFFERENT station/IP (fresh bucket)
  free_exception_fallback  (station-first/direct, default station-first)
                              — dead tunnel → retry another station BEFORE
                              the residential-IP direct fallback

Coverage by plan verification item:

  (a) non-stream 429 sur A → retry B → OK            test_non_stream_429_retries_fresh_station
      + never re-strikes A, rotation only for A, on_request counted once
  (a') non-stream budget épuisé (429 A, 429 B) → None (payant), pas de
      direct fallback sur 429 final                   test_non_stream_budget_exhausted_returns_none
  (c) non-stream: exception tunnel sur A, station-first → retry B → OK
                                                      test_non_stream_tunnel_failure_retries_station
  (d) non-stream: exception tunnel sur A, direct mode → direct fallback
      immédiat (legacy), jamais B                     test_non_stream_direct_mode_legacy_fallback
  (b) stream 429: cooldown (model, IP) + rotation + refuse flag, strict_free
      variant; la boucle stream elle-même (closure du handler) n'est pas
      invocable hors handler — sa décision « continue sans réverter
      _using_free » est couverte par _on_free_429_stream + _free_max_attempts
      + la revue du code                         test_stream_429_cooldown_rotation_refuse
  (c'/d') stream CM: direct_fallback=False → _FreeTunnelFailure re-raisé
      (la boucle caller retente une station fraîche) ; direct_fallback=True
      → fallback httpx direct legacy, station préservée dans le ContextVar
                                                      test_stream_tunnel_failure_raises_free_tunnel_failure
                                                      test_stream_direct_mode_legacy_direct_fallback
  (e) exclusion cumulée (real FreeIPPool._best_station_excluding_many):
      jamais re-striker une IP déjà tentée            test_excluding_many_never_restrikes
  (f) hot-reload: mutation EN PLACE d'IP_ROTATION (ce que fait
      POST /api/vpn-config → _persist_vpn_config) change la stratégie sans
      restart — clamps, enum, valeurs dégradées       test_hot_reload_in_place_mutation

Invariant A.0 (jamais d'artefact payant sur API_BASE_FREE) est vérifié sur
CHAQUE essai free des tests non-stream (headers + body capturés par le
fake curl).

Never touches the live system: no real curl/httpx (both faked), no VPN,
no DB write (free-usage logging no-op'd).
"""

import json
from contextlib import asynccontextmanager

import pytest

import opencode as oc
from free_ip_pool import FreeIPPool

PAID_KEY_MARKER = "sk-ant-test-paid-key-1234567890"
PAID_HEADERS = {
    "Authorization": f"Bearer {PAID_KEY_MARKER}",
    "x-api-key": PAID_KEY_MARKER,
    "User-Agent": "claude-cli/1.0.3 (Claude Code) custom-agent/0.1",
    "Cookie": "session=abc123; ubid=xyz",
    "x-request-id": "req_test_123",
    "x-stainless-arch": "x64",
    "x-stainless-lang": "python",
    "anthropic-version": "2023-06-01",
    "Content-Type": "application/json",
}
FREE_MODEL = "free-test-model"
PAID_MODEL = "paid-test-model"


def assert_no_paid_artifacts(label, headers, body=""):
    """Invariant A.0 (voir test_invariant_a0.py) — aucun artefact payant."""
    for name in ("authorization", "x-api-key", "cookie", "x-request-id"):
        assert name not in headers, f"{label}: forbidden header {name!r} reached API_BASE_FREE"
    for name, value in headers.items():
        assert not name.startswith("x-stainless-"), (
            f"{label}: SDK identifier {name!r} reached API_BASE_FREE"
        )
        assert PAID_KEY_MARKER not in value, (
            f"{label}: paid key leaked in header {name!r} (value={value!r})"
        )
    ua = headers.get("user-agent", "")
    assert "claude-cli" not in ua and "python-httpx" not in ua, (
        f"{label}: client UA leaked to the free endpoint: {ua!r}"
    )
    if body:
        assert PAID_KEY_MARKER not in body, f"{label}: paid key leaked in request body"


class _Station:
    """Duck-typed VPNManager: exactly the attributes the free path reads."""

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
    """_vpn_manager double: proxy_mode=vpn forces the VPN branch."""

    proxy_mode = "vpn"
    current_ip = "10.0.0.1"


class _FakeResp:
    """Minimal response double for the non-stream path: status + headers
    (lowercased, like httpx) + json()/text."""

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
    """_do_free_request_curl_cffi double: scripted per-call responses,
    records (proxy_url, station) per attempt."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []  # [(proxy_url, station), ...]
        self.bodies = []  # body per attempt (model swap check)
        self.headers = []  # headers per attempt (invariant A.0)

    async def __call__(self, body, headers, proxy_url=None, station=None, endpoint=None, **kwargs):
        self.calls.append((proxy_url, station))
        self.bodies.append(body)
        self.headers.append(headers)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _PoolMulti:
    """FreeIPPool double for the non-stream loop: real exclusion logic via
    _best_station_excluding_many, records rotation + on_request calls."""

    enabled = True
    active_station = None

    def __init__(self, stations):
        self._stations = stations
        self.rotated = []  # on_quota_exhausted(station) calls
        self.requests = 0  # on_request() calls (quota counter)

    async def on_request(self):
        self.requests += 1
        best = self._best_station()
        return best.socks5_url if best else None, best

    def _best_station(self):
        for st in self._stations:
            if self._station_usable(st, exclude_approaching=False):
                return st
        return None

    def _best_station_excluding_many(self, excluded, forced_pool=None):
        for exclude_approaching in (True, False):
            for st in self._stations:
                if st in excluded:
                    continue
                if self._station_usable(st, exclude_approaching=exclude_approaching):
                    return st
        return None

    def _station_usable(self, st, *, exclude_approaching=False):
        return st.status == "connected"

    def on_quota_exhausted(self, station):
        self.rotated.append(station)


@pytest.fixture
def free_vpn_env(monkeypatch):
    """Point the free machinery at the fakes; neutralise live side effects."""
    monkeypatch.setattr(oc, "_vpn_manager", _StubVpn())
    monkeypatch.setattr(oc, "_get_cached_public_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(oc, "_log_free_model_usage", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "FREE_MODEL_MAP", {PAID_MODEL: FREE_MODEL})
    oc._free_model_cooldowns.clear()
    return oc


@pytest.fixture
def free_cfg():
    """Save/restore the hot-reload keys on the LIVE IP_ROTATION dict
    (the same dict POST /api/vpn-config mutates in place)."""
    saved = {
        k: oc.IP_ROTATION.get(k)
        for k in (
            "max_free_attempts",
            "free_exception_fallback",
            "strict_free",
            "auto_max_free_attempts",
            "station_count",
            "free_parallel",
        )
    }
    # deep copy for dict values
    if isinstance(saved.get("free_parallel"), dict):
        saved["free_parallel"] = dict(saved["free_parallel"])
    # ensure known clean state for this test (station_count 2, free_parallel OFF)
    # otherwise a previous test file that changed station_count to 5 would leak
    oc.IP_ROTATION["station_count"] = 2
    # [v10 §15.1.3 hermeticity] force strict_free OFF: the live IP_ROTATION dict
    # is seeded from the machine's config.yaml (strict_free: true) and leaked
    # into the "budget exhausted → paid fallback" tests. Tests wanting strict
    # mode set it explicitly.
    oc.IP_ROTATION["strict_free"] = False
    oc.IP_ROTATION["free_parallel"] = {
        "enabled": False,
        "routing": "round-robin",
        "mode": "load-balance",
        "hedge_delay_ms": 300,
        "hedge_max_attempts": 1,
    }
    try:
        import config.settings as _st

        _st.FREE_PARALLEL.clear()
        _st.FREE_PARALLEL.update(_st._normalize_free_parallel(oc.IP_ROTATION["free_parallel"]))
        if hasattr(oc, "_free_ip_pool") and oc._free_ip_pool and hasattr(oc._free_ip_pool, "update_config"):
            oc._free_ip_pool.update_config(oc.IP_ROTATION)
    except Exception:
        pass
    yield
    for k, v in saved.items():
        if v is None:
            oc.IP_ROTATION.pop(k, None)
        else:
            # restore dict copy to avoid aliasing
            oc.IP_ROTATION[k] = dict(v) if isinstance(v, dict) else v
    # also sync settings mirror and pool if free_parallel changed
    try:
        import config.settings as _st

        _st.FREE_PARALLEL.clear()
        _st.FREE_PARALLEL.update(_st._normalize_free_parallel(oc.IP_ROTATION.get("free_parallel", {})))
    except Exception:
        pass
    try:
        if oc._free_ip_pool and hasattr(oc._free_ip_pool, "update_config"):
            oc._free_ip_pool.update_config(oc.IP_ROTATION)
    except Exception:
        pass


def _free_body():
    return {"model": PAID_MODEL, "messages": [{"role": "user", "content": "hello"}]}


# ── (a) non-stream 429 sur A → retry B → OK ────────────────────────────────
@pytest.mark.asyncio
async def test_non_stream_429_retries_fresh_station(free_vpn_env, free_cfg, monkeypatch):
    """429 on station A consumes one attempt; the loop retries station B
    (fresh IP = fresh quota) and succeeds. A is never re-struck, the
    rotation fires ONLY for the 429'd station, and on_request() (the quota
    counter) runs exactly once for the whole request."""
    oc.IP_ROTATION["max_free_attempts"] = 2
    a = _Station(1, "1.1.1.1", "socks5://127.0.0.1:1080")
    b = _Station(2, "2.2.2.2", "socks5://127.0.0.1:1081")
    pool = _PoolMulti([a, b])
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    fake = _FakeFreeCurl(
        [
            _FakeResp(
                429,
                {"retry-after": "30", "content-type": "application/json"},
                text='{"error": "rate limited"}',
            ),
            _FakeResp(
                200,
                {"content-type": "application/json"},
                usage={"prompt_tokens": 3, "completion_tokens": 5},
            ),
        ]
    )
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", fake)

    result = await oc._try_free_model_first(_free_body(), dict(PAID_HEADERS), "openai", PAID_MODEL)
    assert result is not None, "free attempt 2 must succeed on station B"
    resp, _resp_headers, free_model, free_ip = result
    assert resp.status_code == 200
    assert free_model == FREE_MODEL
    assert free_ip == "2.2.2.2", "logged IP must be the SUCCESSFUL station's IP"

    # Two strikes, exactly one per station, A then B — never re-struck
    assert [p for p, _s in fake.calls] == [a.socks5_url, b.socks5_url]
    assert [s for _p, s in fake.calls] == [a, b]
    # Rotation only for the exhausted station A
    assert pool.rotated == [a]
    # Quota counter advances exactly once for the whole request
    assert pool.requests == 1
    # Invariant A.0 on EVERY free attempt (each station sees clean headers)
    for i, (headers, body) in enumerate(zip(fake.headers, fake.bodies, strict=False)):
        assert_no_paid_artifacts(f"non-stream attempt {i + 1}", headers, json.dumps(body))
        assert json.loads(json.dumps(body))["model"] == FREE_MODEL


# ── (a'') socks5 mode: round-robin `_socks5_next`, jamais la même proxy ────
class _Socks5Ep:
    """Minimal Socks5Endpoint double: negative _station, host:port pid."""

    def __init__(self, idx, *, enabled=True, usable=True):
        self._station = -idx
        self.enabled = enabled
        self._usable = usable
        self.current_ip = None  # socks5 has no docker IP
        self.pid = f"1.2.3.4:10{idx}80"
        self.socks5_url = f"socks5://1.2.3.4:10{idx}80"
        self._quota_per_ip = 15

    def __eq__(self, other):
        return isinstance(other, _Socks5Ep) and other._station == self._station

    def __hash__(self):
        return hash(self._station)

    def __repr__(self):
        return f"<Socks5Ep {self.pid}>"


class _Socks5PoolMulti:
    """FreeIPPool double in socks5 mode: round-robin ``_socks5_next`` with
    exclusion (docker stations inert — ``_stations`` stays empty)."""

    enabled = True
    socks5_mode = True
    active_station = None

    def __init__(self, eps):
        self._eps = eps
        self._rr = -1
        self.rotated = []  # on_quota_exhausted(ep) calls
        self.requests = 0  # on_request() calls (quota counter)

    async def on_request(self):
        self.requests += 1
        ep = self._socks5_next()
        self.active_station = ep
        return ep.socks5_url if ep else None, ep

    def _best_station(self):
        return None  # no docker stations in socks5 mode

    def _socks5_enabled_eps(self):
        return [ep for ep in self._eps if ep.enabled]

    def _socks5_usable(self, ep, *, exclude_approaching=False):
        """Same surface the pre-flight gate (_free_stations_exhausted) reads."""
        return ep.enabled and ep._usable

    def _socks5_next(self, excluded=None):
        eps = self._socks5_enabled_eps()
        excluded = excluded or set()
        start = (self._rr + 1) % len(eps)
        for i in range(len(eps)):
            ep = eps[(start + i) % len(eps)]
            if ep in excluded:
                continue
            if ep._usable:
                self._rr = (start + i) % len(eps)
                return ep
        return None

    def on_quota_exhausted(self, station):
        self.rotated.append(station)


@pytest.mark.asyncio
async def test_socks5_mode_429_retries_next_proxy(free_vpn_env, free_cfg, monkeypatch):
    """[Axe 3.1] socks5 attempt loop: 429 on proxy A → the NEXT round-robin
    proxy (exclusion never re-strikes A), usage logged with the proxy's
    host:port, the per-proxy pid cooldown set for A only, and the docker
    rotation machinery is NOT invoked (on_quota_exhausted records the ep)."""
    oc.IP_ROTATION["max_free_attempts"] = 2
    a = _Socks5Ep(1)
    b = _Socks5Ep(2)
    pool = _Socks5PoolMulti([a, b])
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    fake = _FakeFreeCurl(
        [
            _FakeResp(429, {"retry-after": "30"}, text="{}"),
            _FakeResp(
                200,
                {"content-type": "application/json"},
                usage={"prompt_tokens": 3, "completion_tokens": 5},
            ),
        ]
    )
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", fake)

    result = await oc._try_free_model_first(_free_body(), dict(PAID_HEADERS), "openai", PAID_MODEL)
    assert result is not None and result[0].status_code == 200
    resp, _resp_headers, free_model, free_ip = result
    assert free_model == FREE_MODEL
    assert free_ip == b.pid, "usage row carries the SUCCESSFUL proxy's host:port"
    assert [s for _p, s in fake.calls] == [a, b], "proxy A then proxy B — never A again"
    assert pool.rotated == [a], "the 429'd proxy quota-exhausted exactly once"
    assert oc._free_cooldown_active(FREE_MODEL, a), "proxy A's pid cooldown set"
    assert not oc._free_cooldown_active(FREE_MODEL, b), "proxy B keeps a FRESH key"
    assert pool.requests == 1, "quota counter advances once for the whole request"


@pytest.mark.asyncio
async def test_socks5_mode_single_proxy_429_budget_exhausted(free_vpn_env, free_cfg, monkeypatch):
    """[Axe 3.1] ONE static proxy, 429 with max_free_attempts=2: the second
    pass of _socks5_next admits NO other proxy (exclusion never re-strikes
    the cooldowned one) → the loop breaks and — identically to a single vpn
    station — the last-resort residential direct fallback runs; its 429
    answer is swallowed and the caller pays (None). No infinite loop."""
    oc.IP_ROTATION["auto_max_free_attempts"] = False
    oc.IP_ROTATION["max_free_attempts"] = 2
    a = _Socks5Ep(1)
    pool = _Socks5PoolMulti([a])
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    fake = _FakeFreeCurl([_FakeResp(429, {"retry-after": "30"}, text="{}")])
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", fake)
    direct_calls = []

    async def _fake_direct(url, body, headers, protocol, retry_on_429=False):
        direct_calls.append(url)
        return _FakeResp(429, {"retry-after": "90"}, text="{}"), {}

    monkeypatch.setattr(oc, "_do_request_with_retry", _fake_direct)

    result = await oc._try_free_model_first(_free_body(), dict(PAID_HEADERS), "openai", PAID_MODEL)
    assert result is None, "no second proxy → paid fallback (None)"
    assert [s for _p, s in fake.calls] == [a], "the sole proxy struck exactly once"
    assert len(direct_calls) == 1, "last-resort residential fallback attempted"
    assert direct_calls[0] == oc.API_BASE_FREE


# ── (a') non-stream budget épuisé → None (payant), pas de direct 429 ───────
@pytest.mark.asyncio
async def test_non_stream_budget_exhausted_returns_none(free_vpn_env, free_cfg, monkeypatch):
    """429 on A then 429 on B with max_free_attempts=2 → the final 429 is
    returned as-is (no direct residential fallback on a quota answer) and
    the caller falls back to paid (None)."""
    oc.IP_ROTATION["max_free_attempts"] = 2
    a = _Station(1, "1.1.1.1", "socks5://127.0.0.1:1080")
    b = _Station(2, "2.2.2.2", "socks5://127.0.0.1:1081")
    pool = _PoolMulti([a, b])
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    fake = _FakeFreeCurl(
        [
            _FakeResp(429, {"retry-after": "30"}, text="{}"),
            _FakeResp(429, {"retry-after": "60"}, text="{}"),
        ]
    )
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", fake)
    direct_calls = []

    async def _fake_direct(url, body, headers, protocol, retry_on_429=False):
        direct_calls.append(url)
        return _FakeResp(429, {"retry-after": "90"}, text="{}"), {}

    monkeypatch.setattr(oc, "_do_request_with_retry", _fake_direct)

    result = await oc._try_free_model_first(_free_body(), dict(PAID_HEADERS), "openai", PAID_MODEL)
    assert result is None, "budget exhausted → paid fallback (None)"
    assert [s for _p, s in fake.calls] == [a, b], "both stations struck once"
    assert pool.rotated == [a, b], "both exhausted stations rotated"
    assert direct_calls == [], "no direct fallback on a final 429 (quota answer)"


# ── (c) non-stream: exception tunnel, station-first → retry B ──────────────
@pytest.mark.asyncio
async def test_non_stream_tunnel_failure_retries_station(free_vpn_env, free_cfg, monkeypatch):
    """Dead tunnel on A (exception) in station-first mode → retry station B
    BEFORE the direct residential fallback; B succeeds."""
    oc.IP_ROTATION["max_free_attempts"] = 2
    oc.IP_ROTATION["free_exception_fallback"] = "station-first"
    a = _Station(1, "1.1.1.1", "socks5://127.0.0.1:1080")
    b = _Station(2, "2.2.2.2", "socks5://127.0.0.1:1081")
    pool = _PoolMulti([a, b])
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    fake = _FakeFreeCurl(
        [
            RuntimeError("SOCKS5 tunnel dead"),
            _FakeResp(
                200,
                {"content-type": "application/json"},
                usage={"prompt_tokens": 3, "completion_tokens": 5},
            ),
        ]
    )
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", fake)
    direct_calls = []

    async def _fake_direct(url, body, headers, protocol, retry_on_429=False):
        direct_calls.append(url)
        return _FakeResp(200, {"content-type": "application/json"}), {}

    monkeypatch.setattr(oc, "_do_request_with_retry", _fake_direct)

    result = await oc._try_free_model_first(_free_body(), dict(PAID_HEADERS), "openai", PAID_MODEL)
    assert result is not None
    assert result[0].status_code == 200
    assert [s for _p, s in fake.calls] == [a, b], (
        "station-first: dead tunnel on A must retry B, not go direct"
    )
    assert direct_calls == [], "direct residential fallback never reached"


# ── (d) non-stream: exception tunnel, direct mode → legacy direct ──────────
@pytest.mark.asyncio
async def test_non_stream_direct_mode_legacy_fallback(free_vpn_env, free_cfg, monkeypatch):
    """direct mode (legacy): a dead tunnel on A falls back to the direct
    residential path IMMEDIATELY — B is never struck."""
    oc.IP_ROTATION["max_free_attempts"] = 2
    oc.IP_ROTATION["free_exception_fallback"] = "direct"
    a = _Station(1, "1.1.1.1", "socks5://127.0.0.1:1080")
    b = _Station(2, "2.2.2.2", "socks5://127.0.0.1:1081")
    pool = _PoolMulti([a, b])
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    fake = _FakeFreeCurl([RuntimeError("SOCKS5 tunnel dead")])
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", fake)
    direct_calls = []

    async def _fake_direct(url, body, headers, protocol, retry_on_429=False):
        direct_calls.append(url)
        return _FakeResp(200, {"content-type": "application/json"}), {}

    monkeypatch.setattr(oc, "_do_request_with_retry", _fake_direct)

    result = await oc._try_free_model_first(_free_body(), dict(PAID_HEADERS), "openai", PAID_MODEL)
    assert result is not None and result[0].status_code == 200
    assert [s for _p, s in fake.calls] == [a], (
        "direct mode: exactly ONE tunnel strike, then direct fallback"
    )
    assert len(direct_calls) == 1, "legacy direct fallback ran"
    assert direct_calls[0] == oc.API_BASE_FREE


# ── (b) stream 429: cooldown (model, IP) + rotation + refuse flag ─────────
class _PoolStream429(_PoolMulti):
    """Adds the stream-side protocol: notify_connection_failure (recorded)."""

    def notify_connection_failure(self, station):
        self.rotated.append(("signal", station))


@pytest.mark.asyncio
async def test_stream_429_cooldown_rotation_refuse(free_vpn_env, free_cfg, monkeypatch):
    """_on_free_429_stream (the stream-loop 429 handler): cooldowns the
    (model, IP) key of the ATTEMPTED station only — a different station
    keeps a FRESH key — rotates that station in the background, and
    returns the strict_free refuse flag (False here, True when every
    station is exhausted under strict_free)."""
    oc.IP_ROTATION["strict_free"] = False
    a = _Station(1, "1.1.1.1", "socks5://127.0.0.1:1080")
    b = _Station(2, "2.2.2.2", "socks5://127.0.0.1:1081")
    pool = _PoolStream429([a, b])
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    oc._current_free_attempt.set({"station": a, "proxy_url": a.socks5_url})

    refuse = oc._on_free_429_stream(FREE_MODEL, "30")
    assert refuse is False, "strict_free off → paid fallback (False)"
    # (model, IP) cooldown on the attempted station's IP…
    assert oc._free_cooldown_active(FREE_MODEL, a), "cooldown key must be set for the 429'd station"
    # …but station B carries a different IP → its key stays FRESH
    assert not oc._free_cooldown_active(FREE_MODEL, b), (
        "fresh station must NOT inherit the exhausted station's cooldown"
    )
    assert pool.rotated == [a], "background rotation fired for the 429'd station"

    # strict_free: every station exhausted → refuse instead of paying
    oc.IP_ROTATION["strict_free"] = True
    oc._set_free_cooldown(FREE_MODEL, 9999, a)
    oc._set_free_cooldown(FREE_MODEL, 9999, b)
    refuse = oc._on_free_429_stream(FREE_MODEL, "60")
    assert refuse is True, "strict_free + all stations exhausted → refuse"


# ── (c') stream CM: direct_fallback=False → _FreeTunnelFailure re-raisé ────
class _FakeCurlSessionRaise:
    """curl_cffi AsyncSession double whose post() raises (dead tunnel)."""

    created = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).created.append(kwargs)

    async def post(self, *args, **kwargs):
        raise RuntimeError("SOCKS5 tunnel dead")

    async def close(self):
        pass


class _FakeStreamResp:
    status_code = 200
    headers = {}

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_stream_tunnel_failure_raises_free_tunnel_failure(
    free_vpn_env, free_cfg, monkeypatch
):
    """station-first stream: a tunnel exception with direct_fallback=False
    RE-RAISES _FreeTunnelFailure (carrying the dead station) instead of
    falling through to the direct httpx path — the caller's loop then
    retries a FRESH station. The ContextVar still holds the dead station
    so the next attempt excludes it. A non-connect exception must NOT
    signal the pool (the signal gate is connect-error-only)."""
    pytest.importorskip("curl_cffi")
    _FakeCurlSessionRaise.created.clear()
    a = _Station(1, "1.1.1.1", "socks5://127.0.0.1:1080")
    pool = _PoolStream429([a])
    pool.active_station = a
    pool.proxy_url = a.socks5_url
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _FakeCurlSessionRaise)

    body = {"model": FREE_MODEL, "messages": [{"role": "user", "content": "hello"}]}
    with pytest.raises(oc._FreeTunnelFailure) as excinfo:
        async with oc._open_free_stream(
            oc.API_BASE_FREE, body, dict(PAID_HEADERS), use_free=True, direct_fallback=False
        ):
            raise AssertionError("the CM must not yield on a dead tunnel in station-first mode")
    assert excinfo.value.station is a, (
        "the re-raised failure must carry the DEAD station (next attempt excludes it)"
    )
    assert "tunnel dead" in str(excinfo.value.cause)
    # Tunnel branch really ran with the station's tunnel
    assert len(_FakeCurlSessionRaise.created) == 1
    assert _FakeCurlSessionRaise.created[0]["proxy"] == "socks5h://127.0.0.1:1080"
    # No direct httpx fallback, no pool signal for a non-connect error
    assert pool.rotated == [], "RuntimeError is not a connect error → no signal"
    # The dead station is preserved for the next attempt's exclusion
    attempt = oc._current_free_attempt.get() or {}
    assert attempt.get("station") is a


# ── (d') stream CM: direct_fallback=True → legacy direct httpx ─────────────
class _FakeHttpxClient:
    """httpx.AsyncClient double: records stream() calls, yields offline."""

    def __init__(self):
        self.calls = []

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        yield _FakeStreamResp()

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_stream_direct_mode_legacy_direct_fallback(free_vpn_env, free_cfg, monkeypatch):
    """direct mode (legacy stream): a tunnel exception falls through to the
    direct httpx fallback — NO _FreeTunnelFailure. Invariant A.0 holds on
    the direct request, and the failed station is preserved in the
    ContextVar (a later disconnect retry can still switch away from it)."""
    a = _Station(1, "1.1.1.1", "socks5://127.0.0.1:1080")
    pool = _PoolStream429([a])
    pool.active_station = a
    pool.proxy_url = a.socks5_url
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _FakeCurlSessionRaise)
    fake_client = _FakeHttpxClient()
    monkeypatch.setattr(oc, "_client", fake_client)

    body = {"model": FREE_MODEL, "messages": [{"role": "user", "content": "hello"}]}
    async with oc._open_free_stream(
        oc.API_BASE_FREE, body, dict(PAID_HEADERS), use_free=True, direct_fallback=True
    ) as resp:
        assert resp.status_code == 200

    assert len(fake_client.calls) == 1, "legacy direct httpx fallback ran"
    method, url, kwargs = fake_client.calls[0]
    assert method == "POST" and url == oc.API_BASE_FREE
    assert_no_paid_artifacts("stream direct fallback", kwargs["headers"])
    attempt = oc._current_free_attempt.get() or {}
    assert attempt.get("station") is a, "failed station preserved for exclusion"


# ── (e) exclusion cumulée (real FreeIPPool._best_station_excluding_many) ──
def _pool_with(stations):
    pool = FreeIPPool.__new__(FreeIPPool)  # skip __init__ (docker-coupled)
    pool._stations = stations
    pool._per = {}
    pool._rotation_stagger = 10
    pool._active_station = None
    return pool


def test_excluding_many_never_restrikes():
    """Cumulative exclusion: each retry lands on a station NOT yet tried;
    never re-strikes the same IP (the same bucket)."""
    a = _Station(1, "1.1.1.1", "socks5://127.0.0.1:1080")
    b = _Station(2, "2.2.2.2", "socks5://127.0.0.1:1081")
    c = _Station(3, "3.3.3.3", "socks5://127.0.0.1:1082")
    pool = _pool_with([a, b, c])

    assert pool._best_station_excluding_many(set()) == a
    assert pool._best_station_excluding_many({a}) == b
    assert pool._best_station_excluding_many({a, b}) == c
    assert pool._best_station_excluding_many({a, b, c}) is None


def test_excluding_many_skips_bad_station_even_when_excluded_empty():
    """The excluded set is cumulative, but _station_usable still applies:
    a station marked bad / tunnel down is never picked."""
    a = _Station(1, "1.1.1.1", "socks5://127.0.0.1:1080")
    b = _Station(2, "2.2.2.2", "socks5://127.0.0.1:1081")
    b.status = "disconnected"  # tunnel down
    pool = _pool_with([a, b])
    assert pool._best_station_excluding_many(set()) is a
    assert pool._best_station_excluding_many({a}) is None, (
        "no usable station left → None (caller goes direct/paid)"
    )


# ── (f) hot-reload: mutation EN PLACE d'IP_ROTATION = POST /api/vpn-config ─
def test_hot_reload_in_place_mutation(free_cfg):
    """The GUI POST mutates IP_ROTATION in place (dashboard/api.py
    _persist_vpn_config) — the next request reads the NEW strategy with no
    restart. Clamps, enum validation, degraded values all covered."""
    oc.IP_ROTATION["auto_max_free_attempts"] = False
    # 1 → legacy: no extra free strikes, active() False in direct mode
    oc.IP_ROTATION["max_free_attempts"] = 1
    oc.IP_ROTATION["free_exception_fallback"] = "direct"
    assert oc._free_max_attempts() == 1
    assert oc._free_exception_fallback_mode() == "direct"
    assert oc._free_attempts_active() is False
    assert max(0, oc._free_max_attempts() - 1) == 0, "bound unchanged (legacy)"

    # 3 + station-first → full budget
    oc.IP_ROTATION["max_free_attempts"] = 3
    oc.IP_ROTATION["free_exception_fallback"] = "station-first"
    assert oc._free_max_attempts() == 3
    assert oc._free_attempts_active() is True
    assert max(0, oc._free_max_attempts() - 1) == 2, "2 extra free strikes"

    # Clamps: GUI validates, but the code must never trust the mirror (P1 melodic-pearl 5)
    oc.IP_ROTATION["max_free_attempts"] = 99
    assert oc._free_max_attempts() == 5
    oc.IP_ROTATION["max_free_attempts"] = 0
    assert oc._free_max_attempts() == 2, "0 is treated as unset → default 2"
    oc.IP_ROTATION["max_free_attempts"] = "abc"
    assert oc._free_max_attempts() == 2, "degraded value → default 2"

    # Enum validation
    oc.IP_ROTATION["free_exception_fallback"] = "banana"
    assert oc._free_exception_fallback_mode() == "station-first"
    oc.IP_ROTATION["free_exception_fallback"] = None
    assert oc._free_exception_fallback_mode() == "station-first"

    # Keys missing → defaults (fresh install / removed from config)
    oc.IP_ROTATION.pop("max_free_attempts", None)
    oc.IP_ROTATION.pop("free_exception_fallback", None)
    assert oc._free_max_attempts() == 2
    assert oc._free_exception_fallback_mode() == "station-first"
