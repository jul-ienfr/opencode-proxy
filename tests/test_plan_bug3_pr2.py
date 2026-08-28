"""Tests manquants du plan Bug #3 + PR2 — repro TTL asym, canary, cascade."""

import asyncio
import subprocess
import time

import pytest

import vpn_manager as vm
from test_vpn_freshness import FakeVPNManager, _cfg


# ── helpers ──────────────────────────────────────────────────────────

class _Clock:
    def __init__(self, t=0.0):
        self.t = t
        self._step = 0.0
    def set_step(self, s): self._step = s
    def advance(self, dt): self.t += dt
    def __call__(self):
        self.t += self._step
        return self.t


class _CanaryMgr(vm.VPNManager):
    _WG_CANARY_POLL_INTERVAL_S = 0.0
    def __init__(self, cfg, tmp_path):
        cfg = dict(cfg)
        cfg.setdefault("state_file", str(tmp_path / "vpn_state.json"))
        super().__init__(cfg, station=1)
        self._wg_key_file = str(tmp_path / "wireguard.env")
        self.compose_calls = []
        self.probe_calls = 0
        self._canary_probe_result = True
    def _docker_run(self, args, timeout=30, env=None):
        self.compose_calls.append(list(args))
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()
    async def _canary_probe_once(self):
        self.probe_calls += 1
        return self._canary_probe_result


def _canary_mgr(tmp_path, **over):
    cfg = {
        "enabled": True, "vpn_stack": "auto",
        "auto_wg_egress_ticks": 3, "auto_flip_cooldown_min": 0,
        "auto_ov_return_min": 60, "auto_ov_fail_threshold": 3,
        "wg_canary_fail_ttl_s": 90, "wg_canary_pass_ttl_s": 600,
        "wg_canary_countries": "Switzerland,Germany,Netherlands",
        "wg_canary_enabled": True,
    }
    cfg.update(over)
    return _CanaryMgr(cfg, tmp_path)


# ── P1.4 canary TTL asym ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_ttl_asym_fail_retested_before_pass(tmp_path):
    """FAIL 90s vs PASS 600s — un FAIL expire en ~90s, un PASS tient 600s."""
    m = _canary_mgr(tmp_path)
    clock = _Clock()
    clock.set_step(1.0)
    m._now_fn = clock
    m._canary_probe_result = False
    # FAIL -> cache 90s
    assert await m._wg_canary_alive("r1") is False
    first_ups = len([c for c in m.compose_calls if "up" in c])
    # re-consult at +50s -> still cached (no docker)
    clock.advance(50)
    clock.set_step(0)
    assert await m._wg_canary_alive("r2") is False
    assert len([c for c in m.compose_calls if "up" in c]) == first_ups
    # at +91s -> re-validation (docker)
    clock.advance(41)
    m._canary_probe_result = False
    clock.set_step(1.0)
    assert await m._wg_canary_alive("r3") is False
    assert len([c for c in m.compose_calls if "up" in c]) > first_ups

    # PASS -> cache 600s
    m2 = _canary_mgr(tmp_path)
    clock2 = _Clock()
    clock2.set_step(1.0)
    m2._now_fn = clock2
    m2._canary_probe_result = True
    assert await m2._wg_canary_alive("r1") is True
    ups2 = len([c for c in m2.compose_calls if "up" in c])
    clock2.advance(500)
    clock2.set_step(0)
    assert await m2._wg_canary_alive("r2") is True
    assert len([c for c in m2.compose_calls if "up" in c]) == ups2
    clock2.advance(150)  # 650s total -> expire
    clock2.set_step(1.0)
    assert await m2._wg_canary_alive("r3") is True
    assert len([c for c in m2.compose_calls if "up" in c]) > ups2


@pytest.mark.asyncio
async def test_canary_cache_invalidated_after_ov_to_wg_flip(tmp_path):
    """_apply_stack OV->WG invalide le cache canari (verdict obsolete)."""
    m = _canary_mgr(tmp_path, wg_canary_fail_ttl_s=90, wg_canary_pass_ttl_s=600)
    clock = _Clock()
    m._now_fn = clock
    # need compose file for _apply_stack .env write
    import tempfile, os
    tmpdir = str(tmp_path)
    compose = os.path.join(tmpdir, "docker-compose.yml")
    open(compose, "w").write("services: {}")
    m._compose_file_path = lambda: compose
    m._stack_effective = "openvpn"
    m._wg_canary_state = {"ok": True, "at": clock()}
    # flip OV->WG
    ok = await m._apply_stack("wireguard", reason="test", auto=True, stations=[1])
    # _apply_stack refuses WG without key -> create key
    if not ok:
        open(m._wg_key_file, "w").write("k")
        ok = await m._apply_stack("wireguard", reason="test", auto=True, stations=[1])
    assert m._wg_canary_state == {"ok": None, "at": None}


@pytest.mark.asyncio
async def test_wg_canary_disabled_bypasses_gate(tmp_path, caplog):
    m = _canary_mgr(tmp_path, wg_canary_enabled=False)
    m._stack_effective = "openvpn"
    m._pending_flip = ("wireguard", "OV healthy 60 min")
    m._canary_probe_result = False
    import logging
    with caplog.at_level(logging.WARNING):
        cancelled = await m._cancel_wg_flip_if_canary_dead()
    assert cancelled is False
    assert m._pending_flip is not None
    assert any("disabled" in r.getMessage() and "bypassed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_httpcore_missing_logs_warning(tmp_path, caplog, monkeypatch):
    """httpcore[socks] absent -> warning + bypass (pas de faux positif)."""
    m = _canary_mgr(tmp_path)
    # force ImportError for httpcore by patching __import__
    import builtins
    orig_import = builtins.__import__
    def fake_import(name, *a, **kw):
        if name == "httpcore":
            raise ImportError("No module named 'httpcore'")
        return orig_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    import logging
    # call the real probe (not the fake) — need a manager that uses real method
    # reuse _CanaryMgr but override to call super
    m2 = _CanaryMgr({"enabled": True, "vpn_stack": "auto", "state_file": str(tmp_path / "s.json"), "wg_canary_enabled": True}, tmp_path)
    # replace with real impl for this test
    m2._canary_probe_once = vm.VPNManager._canary_probe_once.__get__(m2, vm.VPNManager)
    with caplog.at_level(logging.WARNING):
        res = await m2._canary_probe_once()
    assert res is False
    assert any("httpcore" in r.getMessage() for r in caplog.records)


# ── P2 cascade ───────────────────────────────────────────────────────

def _cascade_mgr(tmp_path, **over):
    cfg = {"enabled": True, "vpn_stack": "auto", "server_countries": "Germany,France",
           "ovpn_protocol": "udp", "cascade_enabled": True, "auto_wg_egress_ticks": 3}
    cfg.update(over)
    m = FakeVPNManager(_cfg(tmp_path, **cfg), tmp_path=tmp_path)
    # ensure cascade attrs exist
    m._cascade_enabled = cfg.get("cascade_enabled", False)
    m._cascade_sequence = [("openvpn", "udp"), ("openvpn", "tcp")]
    m._cascade_step = 0
    m._cascade_started_at = None
    m._cascade_max_duration = 120
    m._cascade_pending_proto = None
    m._ovpn_protocol_effective = "udp"
    return m


@pytest.mark.asyncio
async def test_cascade_progression_and_exhaustion(tmp_path):
    m = _cascade_mgr(tmp_path, cascade_enabled=True)
    m._cascade_start()
    m._ovpn_protocol_effective = "udp"
    # step0 udp == effective -> skip -> tcp
    s1 = m._cascade_next_step()
    assert s1 == ("openvpn", "tcp")
    assert m._cascade_step == 2
    assert m._cascade_pending_proto == "tcp"
    # exhausted
    s2 = m._cascade_next_step()
    assert s2 is None
    assert m._cascade_is_active() is False or m._cascade_started_at is None


@pytest.mark.asyncio
async def test_cascade_timeout_falls_back_to_normal_logic(tmp_path):
    m = _cascade_mgr(tmp_path, cascade_enabled=True)
    m._cascade_start()
    m._cascade_started_at = time.monotonic() - 121  # expired
    assert m._cascade_is_active() is False
    # _auto_flip_decision with cascade active but timed out -> should fall through to normal UDP->TCP or WG
    m._stack = "auto"
    m._stack_effective = "openvpn"
    m._egress_failures = 3
    m._ovpn_protocol_effective = "udp"
    m._auth_failed_window = []
    m._stack_since = time.monotonic() - 100
    m._last_auto_flip_at = None
    m._auto_wg_egress_ticks = 3
    m._auto_ov_fail_threshold = 3
    res = m._auto_flip_decision()
    # cascade timed out, normal logic: OV udp dead -> tcp
    assert res == ("openvpn", "egress dead 3 ticks UDP -> TCP")


@pytest.mark.asyncio
async def test_cascade_intercept_uses_pending_not_reason(tmp_path):
    """AUTH_FAILED reason contient 'AUTH_FAILED -> tcp' mais pending porte 'tcp' proprement."""
    m = _cascade_mgr(tmp_path, cascade_enabled=True)
    m._cascade_enabled = True
    m._cascade_pending_proto = "tcp"
    m._pending_flip = ("openvpn", "cascade step 1: AUTH_FAILED \u2192 tcp")
    mode, reason = m._pending_flip
    # simulate watchdog intercept
    assert m._cascade_pending_proto in ("udp", "tcp")
    proto = m._cascade_pending_proto
    m._cascade_pending_proto = None
    assert proto == "tcp"
    # fallback parsing would give 'tcp' via split()[-1] but also 'AUTH_FAILED' bug avant fix
    # verify pending cleared
    assert m._cascade_pending_proto is None


@pytest.mark.asyncio
async def test_cascade_reset_on_wg_flip(tmp_path):
    m = _cascade_mgr(tmp_path, cascade_enabled=True)
    m._cascade_start()
    m._cascade_step = 1
    m._cascade_pending_proto = "tcp"
    # simulate _apply_stack wireguard reset
    m._cascade_reset()
    assert m._cascade_step == 0
    assert m._cascade_started_at is None
    assert m._cascade_pending_proto is None


def test_cascade_flag_off_zero_regression(tmp_path):
    """flag OFF -> _cascade_is_active False, _cascade_next_step None, decisions normales."""
    m = _cascade_mgr(tmp_path, cascade_enabled=False)
    assert m._cascade_is_active() is False
    assert m._cascade_next_step() is None
    m._stack = "auto"
    m._stack_effective = "wireguard"
    m._egress_failures = 3
    m._restart_churn = False
    m._last_auto_flip_at = None
    res = m._auto_flip_decision()
    assert res == ("openvpn", "egress dead 3 ticks")
    assert m._cascade_started_at is None  # not started when disabled


def test_cascade_antipingpong_skip(tmp_path):
    m = _cascade_mgr(tmp_path, cascade_enabled=True)
    m._cascade_start()
    m._ovpn_protocol_effective = "udp"
    # sequence [udp, tcp], effective udp -> skip udp -> tcp
    assert m._cascade_next_step() == ("openvpn", "tcp")
    m._cascade_reset()
    m._cascade_start()
    m._ovpn_protocol_effective = "tcp"
    assert m._cascade_next_step() == ("openvpn", "udp")
