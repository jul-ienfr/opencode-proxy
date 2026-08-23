"""In-process smoke test for Vague 2: [2] pause_key cap, [3][4] cooldown
per (model, IP) + ROTATION on 429 ([0]/[42] restored per user demand),
[6] timeout + todo-8: [17] CancelledError, [19] count_request, [20] atomic
state, [21] apply_update TOCTOU, [27] quota parser, [46] loggers,
CRITIC(5)(6) rotation failure honesty, CRITIC(11) quota hot-reload,
[25] CB gate. No live server touched.

Run: python scripts/smoke_todo10.py
"""

import sys, time

sys.path.insert(0, ".")


class _StubVPN:
    """Minimal VPNManager stand-in for FreeIPPool smoke tests."""

    enabled = True
    proxy_mode = "vpn"
    status = "connected"
    current_server = None
    _quota_per_ip = 300
    _ip_history = []
    socks5_url = "socks5://127.0.0.1:1080"

    def __init__(self, ip="1.2.3.4"):
        self.current_ip = ip

    def note_free_request(self):
        pass

    async def connect_next(self):
        return "5.6.7.8"


def main():
    import opencode as oc

    # ---- [4] _free_429_cooldown_seconds ----
    assert oc._free_429_cooldown_seconds("") == 3600.0, "absent -> 3600"
    assert oc._free_429_cooldown_seconds(None) == 3600.0, "None -> 3600"
    assert oc._free_429_cooldown_seconds("5") == 5.0, "short retry-after honored"
    assert oc._free_429_cooldown_seconds("47422") == 47422.0, "13h honored"
    assert oc._free_429_cooldown_seconds("100000") == 3600.0, ">24h -> default"
    assert oc._free_429_cooldown_seconds("0") == 3600.0, "0 -> default"
    assert oc._free_429_cooldown_seconds("garbage") == 3600.0, "garbage -> default"
    print("PASS _free_429_cooldown_seconds")

    # ---- [4] per-(model, IP) cooldown ----
    oc._free_model_cooldowns.clear()
    oc._vpn_manager = None  # no VPN in smoke -> key suffix "direct"
    oc._set_free_cooldown("mimo-v2.5-free", 60)
    assert oc._free_cooldown_active("mimo-v2.5-free"), "own cooldown active"
    assert not oc._free_cooldown_active("deepseek-v4-flash-free"), "other model NOT blocked"
    # expiry pass
    oc._free_model_cooldowns["mimo-v2.5-free|direct"] = time.monotonic() - 1
    assert not oc._free_cooldown_active("mimo-v2.5-free"), "expired -> inactive"
    # same model on a DIFFERENT IP -> fresh key -> not blocked ([4])
    oc._free_model_cooldowns.clear()
    oc._set_free_cooldown("mimo-v2.5-free", 600)
    assert oc._free_cooldown_active("mimo-v2.5-free"), "cooldown active for (model, IP-1)"
    oc._vpn_manager = _StubVPN("9.9.9.9")
    assert not oc._free_cooldown_active("mimo-v2.5-free"), "rotation -> fresh IP -> fresh key"
    oc._vpn_manager = None
    oc._free_model_cooldowns.clear()
    print("PASS per-(model, IP) cooldown isolation")

    # ---- [0]/[42] rotation scheduled on 429 (restored per user demand) ----
    import asyncio
    import free_ip_pool

    async def _check_429_rotation():
        pool = free_ip_pool.FreeIPPool(_StubVPN("1.2.3.4"))
        oc._free_ip_pool = pool
        oc._free_model_cooldowns.clear()
        oc._on_free_429_stream("mimo-v2.5-free", "")
        assert oc._free_cooldown_active("mimo-v2.5-free"), "cooldown set by stream 429"
        assert pool._rotation_task is not None, "stream 429 must schedule a rotation"
        oc._on_free_429_stream("deepseek-v4-flash-free", "90")
        assert pool._rotation_task is not None, "second 429 reuses the in-flight rotation"
        await pool._rotation_task
        assert pool._rotation_task is None, "rotation task cleared after completion"
        # direct mode -> no rotation, but cooldown still set
        vpn_direct = _StubVPN("1.2.3.4")
        vpn_direct.proxy_mode = "direct"
        pool2 = free_ip_pool.FreeIPPool(vpn_direct)
        oc._free_ip_pool = pool2
        oc._free_model_cooldowns.clear()
        oc._on_free_429_stream("mimo-v2.5-free", "")
        assert oc._free_cooldown_active("mimo-v2.5-free"), "cooldown set in direct mode"
        assert pool2._rotation_task is None, "no rotation in direct mode"
        oc._free_ip_pool = None

    asyncio.run(_check_429_rotation())
    print("PASS 429 -> cooldown + single-flight rotation ([0]/[42], [4])")

    # ---- [2] pause_key cap semantics ----
    import threading

    p = oc._KeyPauser.__new__(oc._KeyPauser)  # bare instance, no deps needed for cap logic
    p._max_pause = 600
    # explicit 401 (quota_based=False) honored in full
    p._paused, p._reasons, p._lock = {}, {}, threading.Lock()
    p._save = lambda: None
    p.pause_key("sk-test-aaaaaaaa", 86400, "401 Unauthorized (key likely revoked)")
    assert abs(p._paused[p._prefix("sk-test-aaaaaaaa")] - (time.monotonic() + 86400)) < 2, (
        "401 pause must NOT be capped at max_pause"
    )
    # quota-based capped
    p.pause_key("sk-test-bbbbbbbb", 99999, "quota reset", quota_based=True)
    assert abs(p._paused[p._prefix("sk-test-bbbbbbbb")] - (time.monotonic() + 600)) < 2, (
        "quota pause MUST be capped at max_pause"
    )
    print("PASS pause_key cap semantics ([2])")

    # ---- [6] timeout present on Path A ----
    import inspect

    src = inspect.getsource(oc._do_free_request_curl_cffi)
    assert "timeout=(30, 600)" in src, "Path A timeout must be (30, 600)"
    print("PASS Path A timeout (30, 600) ([6])")

    # ============ TODO-8: [17][19][20][21][27][46] + CRITIC(5)(6)(11) + [25] ============
    import vpn_manager, tempfile, json as json_mod, os

    def _bare_vpn():
        """VPNManager without __init__ — never touches live state/state file."""
        v = vpn_manager.VPNManager.__new__(vpn_manager.VPNManager)
        v._enabled = True
        v._status = vpn_manager.VPNState.DISCONNECTED
        v._error = None
        v._lock = asyncio.Lock()
        v._rotation_task = None
        v._last_rotation_failed_at = None
        v._ROTATION_FAIL_COOLDOWN = 300
        v._docker_container = "smoke-test"
        v._identity_rotation_enabled = False
        v._identity_profiles = [{"impersonate": "chrome131"}]
        v._identity_index = 0
        v._proxy_mode = "vpn"
        v._auth_failed = False
        v._current_ip = None
        v._current_server = None
        v._connected_at = None
        v._total_switches = 0
        v._ip_history = []
        v._server_countries = "smoke"
        v._switch_delay = 0
        v._circuit_breaker = vpn_manager.CircuitBreaker()
        v._backoff = vpn_manager.BackoffTimer(base_delay=0, max_delay=0)
        v.save_state = lambda: None  # shadow: never write the real state file
        return v

    async def _check_rotation_failure():
        # CRITIC(5): total rotation failure raises RotationFailed (never None)
        v = _bare_vpn()

        async def _docker_down():
            raise RuntimeError("docker daemon unreachable")

        v._ensure_container = _docker_down
        raised = False
        try:
            await v.connect_next()
        except vpn_manager.RotationFailed as e:
            raised = True
            assert "3 attempts" in str(e), f"unexpected message: {e}"
        assert raised, "CRITIC(5): connect_next must RAISE on total failure"
        assert v._status == vpn_manager.VPNState.ERROR, "status must be ERROR"
        assert v._last_rotation_failed_at is not None, (
            "CRITIC(6): fail-fast cooldown must be armed after a failure"
        )
        # CRITIC(6): second call within 300 s is refused immediately, no task
        raised = False
        try:
            await v.connect_next()
        except vpn_manager.RotationFailed as e:
            raised = True
            assert "cooldown" in str(e), f"unexpected message: {e}"
        assert raised, "CRITIC(6): rotation refused during fail-fast cooldown"
        assert v._rotation_task is None, "no rotation task during cooldown"
        # [25]: circuit breaker open gates the rotation
        v2 = _bare_vpn()
        v2._circuit_breaker.record_failure("smoke-test")
        v2._circuit_breaker.record_failure("smoke-test")
        v2._circuit_breaker.record_failure("smoke-test")  # opens
        assert not v2._circuit_breaker.is_available("smoke-test"), "CB must be open"
        raised = False
        try:
            await v2.connect_next()
        except vpn_manager.RotationFailed as e:
            raised = True
            assert "circuit breaker" in str(e), f"unexpected message: {e}"
        assert raised, "[25]: open CB must gate rotation with RotationFailed"
        print("PASS CRITIC(5)(6) + [25] rotation failure honesty + gates")

    asyncio.run(_check_rotation_failure())

    async def _check_cancel():
        # [17]: CancelledError mid-rotation / mid-connect -> ERROR, not CONNECTING
        v3 = _bare_vpn()

        async def _boom():
            raise asyncio.CancelledError()

        v3._ensure_container = _boom
        raised = False
        try:
            await v3._connect_next_impl()
        except asyncio.CancelledError:
            raised = True
        assert raised, "CancelledError must propagate"
        assert v3._status == vpn_manager.VPNState.ERROR, (
            "[17]: cancelled rotation must not stay CONNECTING"
        )
        v7 = _bare_vpn()

        async def _boom2():
            raise asyncio.CancelledError()

        v7._compose_up = _boom2
        raised = False
        try:
            await v7.connect()
        except asyncio.CancelledError:
            raised = True
        assert raised, "CancelledError must propagate from connect()"
        assert v7._status == vpn_manager.VPNState.ERROR, (
            "[17]: cancelled connect() must not stay CONNECTING"
        )
        print("PASS [17] CancelledError -> ERROR status")

    asyncio.run(_check_cancel())

    # [20]: atomic save (tmp+rename) + load restores current_ip/identity/CB
    with tempfile.TemporaryDirectory() as td:
        state_path = os.path.join(td, "vpn_state.json")
        v4 = _bare_vpn()
        del v4.save_state  # restore the REAL save_state for the atomicity test
        v4._get_state_path = lambda: state_path
        v4._current_ip = "10.0.0.77"
        v4._total_switches = 5
        v4._identity_index = 2
        v4._identity_profiles = [{"impersonate": "a"}, {"impersonate": "b"}, {"impersonate": "c"}]
        v4._ip_history = [{"ip": "10.0.0.77", "server": "smoke-test", "time": "t"}]
        v4._circuit_breaker.record_failure("smoke-test")  # 1 failure, still closed
        v4.save_state()
        assert not os.path.exists(state_path + ".tmp"), "[20]: no .tmp left after atomic save"
        v5 = _bare_vpn()
        v5._get_state_path = lambda: state_path
        v5._identity_profiles = v4._identity_profiles
        v5.load_state()
        assert v5._current_ip == "10.0.0.77", "[20]: current_ip restored"
        assert v5._identity_index == 2, "[20]: identity_index restored"
        assert v5._total_switches == 5, "[20]: total_switches restored"
        assert v5._circuit_breaker._servers["smoke-test"]["failures"] == 1, (
            "[20]: CB failure count restored"
        )
        # clamp: config shrank between restarts
        v5._identity_profiles = [{"impersonate": "a"}]
        v5.load_state()
        assert v5._identity_index == 0, "[20]: identity_index clamped % len(profiles)"
    print("PASS [20] atomic save_state + load_state restore")

    async def _check_apply_update():
        # [21]: opportune checks run INSIDE the lock; skips when traffic
        v6 = _bare_vpn()
        v6._update_available = False
        r = await v6.apply_update(check_opportune=True)
        assert r["error"] == "no update available", "no-update path unaffected"
        v6._update_available = True
        v6._active_free_streams = 0
        v6._update_opportune = lambda: False
        r = await v6.apply_update(check_opportune=True)
        assert r["error"] == "not opportune (traffic active)", (
            "[21]: not-opportune must skip inside the lock"
        )
        v6._update_opportune = lambda: True
        v6._active_free_streams = 2
        r = await v6.apply_update(check_opportune=True)
        assert "streams" in r["error"], "[21]: live free streams must defer"
        # check_opportune=False proceeds past both skips (proven by reaching
        # the next gate) and must NOT consult _update_opportune
        v6._active_free_streams = 0
        called = {"n": 0}

        def _opp():
            called["n"] += 1
            return True

        v6._update_opportune = _opp
        v6._acquire_update_lock = lambda: False  # stop before docker
        r = await v6.apply_update(check_opportune=False)
        assert r["error"] == "another instance is applying an update", (
            "[21]: check_opportune=False proceeds past the skips"
        )
        assert called["n"] == 0, "[21]: _update_opportune must not be called"
        print("PASS [21] apply_update TOCTOU (check_opportune inside lock)")

    asyncio.run(_check_apply_update())

    # [19]: 4 stream call sites count each request exactly once (attempt 0)
    src19 = open("opencode.py", encoding="utf-8").read()
    assert src19.count("count_request=(_attempt == 0)") == 4, (
        "[19]: expected exactly 4 count_request=(_attempt == 0) call sites"
    )
    print("PASS [19] count_request once per request (4 call sites)")

    # [46]: VPN loggers routed into the rich log panel
    assert '"vpn_manager", "free_ip_pool"' in src19, (
        "[46]: handler must be attached to vpn_manager/free_ip_pool loggers"
    )
    print("PASS [46] VPN loggers attached to the app log panel")

    # CRITIC(11): hot-reload of quota_per_ip resets the request counter
    async def _check_quota_hot_reload():
        vpn = _StubVPN("1.2.3.4")
        pool = free_ip_pool.FreeIPPool(vpn)
        assert await pool.on_request() is not None, "proxy URL expected"
        assert pool._request_count == 1, "first request counts"
        vpn._quota_per_ip = 500  # hot-reload change
        assert await pool.on_request() is not None
        assert pool._request_count == 1, "CRITIC(11): counter must reset when quota_per_ip changes"
        assert pool._last_quota_per_ip == 500
        assert await pool.on_request() is not None
        assert pool._request_count == 2, "counting resumes under new quota"

    asyncio.run(_check_quota_hot_reload())
    print("PASS CRITIC(11) quota_per_ip hot-reload resets counter")

    # [27]: JS->JSON quota parser handles backslashes/quotes correctly
    from dashboard.quota import _normalize_js_object

    samples = [
        ("{label: 'it\\'s ok', pct: 42}", {"label": "it's ok", "pct": 42}),
        ("{path: 'C:\\Users\\x', pct: 1}", {"path": "C:\\Users\\x", "pct": 1}),
        ("{q: 'say \"hi\"', pct: 2}", {"q": 'say "hi"', "pct": 2}),
        ("{q: 'a\\\\b', pct: 3}", {"q": "a\\b", "pct": 3}),
    ]
    for src, expected in samples:
        got = json_mod.loads(_normalize_js_object(src))
        assert got == expected, f"[27]: {src!r} parsed to {got!r}, want {expected!r}"
    print("PASS [27] JS->JSON quota parser edge cases")

    print("\nALL TODO-10 SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
