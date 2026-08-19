"""test_vpn_freshness.py — IP freshness, identity-per-IP, watchdog recovery
(vpn_manager.py).

The plan's anti-reuse + "l'identité change quand l'IP change (par station)"
requirements, offline: every IP the tunnel lands on must be fresh (never
recent on EITHER station), must be journalized in _ip_history, and the
history entry must carry the NEW identity (advance happens BEFORE the
append — the old order logged the pre-advance face). The watchdog must
recover from AUTH_FAILED by landing on a fresh IP with a new face — never
back on the failed server — and escalate only after persistent failures.

Covered here (plan "Vérification" section 1, offline):
  * _finalize_ip: fresh accepted; recent rejected round 0/1 then accepted;
    allow_stale accepts on the last attempt; a dead probe never escapes
  * connect() with containers still up + recent boot IP → forced rotation
    (first IP journalized with a NEW identity — the old code never logged
    the boot IP)
  * _connect_next_impl: recent-IP rejection, advance-before-journalize
    ordering, RotationFailed after 3 attempts + fail-fast cooldown
  * _watchdog_tick: AUTH_FAILED → restart → FRESH IP → recovered (backoff
    reset, CONNECTED); persistent failure → escalate after 2; healthy tick
    resets the backoff
  * _watchdog_loop cadence: healthy ticks pace at watchdog_interval, a
    failure switches to the backoff delay (base→max), recovery returns to
    the interval
  * health_check: the tunnel re-picked an IP outside a rotation → registry +
    history carry the NEW face; same IP → no double-advance (finding j)
  * apply_update: post-recreate _finalize_ip(allow_stale=False); AUTH_FAILED
    after update / finalize failure → rollback, update not marked applied
  * retrocompat: shared=None → local history tail + local (idx+1)%n advance
  * cross-station: one real SharedRotationState on a tmp file → station 2
    rejects station 1's IP; live identity indexes never collide

Never touches the live system: fake docker (inspect/restart/compose-up),
FIFO get_public_ip, flag-driven log scans, escalation chain stubbed to
record-only, per-station state files in tmp_path.
"""
import asyncio
import contextlib
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import shared_rotation
import shared_state
import vpn_manager as vm

# Explicit 3-profile pool (diversity off — the plan's strict retrocompat
# path). Index order: 0 chrome131, 1 firefox144, 2 edge101.
PROFILES = [
    {"impersonate": "chrome131", "user_agent": None, "extra_headers": {}},
    {"impersonate": "firefox144", "user_agent": None, "extra_headers": {}},
    {"impersonate": "edge101", "user_agent": None, "extra_headers": {}},
]


def _cfg(tmp_path, **over):
    """Minimal ip_rotation config for the fake manager.

    switch_delay: 0 kills every sleep in the rotation path; state file lives
    in tmp_path so save_state()/load_state() never touch logs/.
    """
    cfg = {
        "enabled": True,
        "proxy_mode": "vpn",
        "switch_delay": 0,
        "identity_rotation": True,
        "identity_diversity": False,          # explicit profiles, no expansion
        "identity_profiles": [dict(p) for p in PROFILES],
        "watchdog_backoff_base": 15.0,        # config.yaml ships 15/60
        "watchdog_backoff_max": 60.0,
        # [plan 18/08 §1d] armed tick cadence + probe budget + threshold.
        # Symbolic values — tests read mgr._auto_wg_egress_ticks, never a
        # literal 3/5. control_pin_catchup: 0.0 keeps the pin tests at the
        # old 60 s behavior (am.5 — only the fast-recover path threads it).
        "auto_wg_egress_ticks": 3,
        "egress_failure_tick_interval": 2.0,
        "ip_probe_budget": 8.0,
        "control_pin_catchup": 0.0,
    }
    cfg.update(over)
    return cfg


class FakeVPNManager(vm.VPNManager):
    """VPNManager with docker/network side effects replaced by in-memory fakes.

    The REAL logic under test — connect / _connect_next_impl / _finalize_ip
    / _commit_ip / _watchdog_tick, identity advance, journalize ordering,
    state persistence, backoff cadence — runs unchanged. Only the subprocess
    and network boundaries are faked:

      * _docker_inspect  → static running state (container_present flag)
      * _docker_restart  → counter; clears the AUTH_FAILED log marker by
                          default (a restart really does rotate the logs)
      * _compose_up      → counter
      * _check_auth/_tls → driven by the log_text marker string
      * _wait_healthy    → instant, always healthy
      * get_public_ip    → FIFO queue; None when exhausted (dead tunnel)
      * _watchdog_escalate → record-only (a restart cycle would need docker)
    """

    def __init__(self, cfg, station=1, shared=None, tmp_path=None, **kw):
        cfg = dict(cfg)
        # [plan 18/08 §4] N-station: any station > 1 gets its own explicit key
        # (state_file_2 → state_file_{N}); station 1 stays the legacy default.
        key = f"state_file_{station}" if station > 1 else "state_file"
        cfg.setdefault(key, str(tmp_path / f"vpn_state{station}.json"))
        super().__init__(cfg, station=station, shared=shared)
        # [plan 18/08 §2d] Pin the WG key file into tmp_path: the repo's
        # vpn_configs/ may or may not hold the NordLynx key on this machine,
        # and the "auto" stack resolution must be deterministic in tests.
        # Re-derive the effective stack exactly like the real __init__.
        self._wg_key_file = str(tmp_path / "wireguard.env")
        if self._stack == "wireguard":
            self._stack_effective = "wireguard"
        elif self._stack == "openvpn":
            self._stack_effective = "openvpn"
        else:
            self._stack_effective = "wireguard" if os.path.exists(self._wg_key_file) else "openvpn"
        self.ips = []                       # FIFO answers for get_public_ip()
        self.probe_delay = 0.0              # sleep before answering (wall test)
        self.container_present = True
        self.log_text = ""                  # container logs: AUTH_FAILED marker
        self.clear_logs_on_restart = True
        self.calls = {"restart": 0, "compose_up": 0, "inspect": 0,
                      "force_recreate": 0}
        self.escalations = 0
        self.finalize_calls = 0

    # ── docker fakes ─────────────────────────────────────────────
    async def _docker_inspect(self):
        self.calls["inspect"] += 1
        if not self.container_present:
            return {}
        return {"running": True, "healthy": True, "restarting": False,
                "started_at": "2026-01-01T00:00:00Z", "mounts": []}

    async def _docker_restart(self):
        self.calls["restart"] += 1
        if self.clear_logs_on_restart:
            self.log_text = ""              # fresh logs after restart

    async def _compose_up(self, force_recreate=False):
        self.calls["compose_up"] += 1
        if force_recreate:
            self.calls["force_recreate"] += 1
        # A recreated container is brand-new: fresh logs. Same knob as
        # _docker_restart — clear_logs_on_restart=False models the pool
        # returning ANOTHER failing server (the 17/08 case: 3 bad German
        # servers), i.e. the recreated container comes back dirty.
        if self.clear_logs_on_restart:
            self.log_text = ""

    def _docker_run(self, args, timeout=30):
        """subprocess fake (apply_update / rollback): record, always succeed."""
        self.calls["docker_run"] = self.calls.get("docker_run", 0) + 1
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    async def _check_auth_failed(self, started_at=""):
        return "AUTH_FAILED" in self.log_text

    async def _check_server_issue(self, started_at=""):
        return False

    async def _wait_healthy(self, timeout=120.0):
        return "2026-01-01T00:00:00Z"

    async def get_public_ip(self):
        if self.probe_delay:
            await asyncio.sleep(self.probe_delay)
        if self.ips:
            return self.ips.pop(0)
        return None

    # [plan 18/08 §1d] Light-egress probe stub: the REAL tick calls
    # _probe_tunnel_light() on both stacks (WG and OV — the shared counter
    # is the authority). probe_alive=False models a dead tunnel; the reset
    # path is tested by flipping it back to True (blip absorbed, no
    # recovery at all). NOT bool(self.ips): the refresh pops the only IP
    # before the probe runs, which would fake a dead tunnel on every tick.
    probe_alive = True

    async def _probe_tunnel_light(self):
        return self.probe_alive

    # ── escalation stub (record-only) ────────────────────────────
    async def _watchdog_escalate(self):
        self.escalations += 1

    async def _finalize_ip(self, allow_stale=False):   # spy over the real one
        self.finalize_calls += 1
        return await super()._finalize_ip(allow_stale=allow_stale)


def _shared(tmp_path):
    """One real SharedRotationState on a tmp file (cross-station tests)."""
    return shared_rotation.SharedRotationState({
        "shared_rotation_file": str(tmp_path / "shared_rotation.json"),
        "recent_ip_window": 20,
        "recent_ip_max_age": 1800,
    })


# ── _finalize_ip ─────────────────────────────────────────────────

class TestFinalizeIp:
    @pytest.mark.asyncio
    async def test_fresh_ip_committed_all_sides(self, tmp_path):
        """A fresh IP is committed: current IP, shared registry, NEW identity."""
        shared = _shared(tmp_path)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr.ips = ["9.9.9.9"]
        assert await mgr._finalize_ip(allow_stale=False) is True
        assert mgr._current_ip == "9.9.9.9"
        assert shared.is_recent("9.9.9.9")             # registry records it
        assert mgr._auth_failed is False
        # Advance BEFORE journalize — the entry carries the NEW face (index 1)
        assert mgr._identity_index == 1
        assert mgr._ip_history[-1]["ip"] == "9.9.9.9"
        assert mgr._ip_history[-1]["identity"] == "firefox144"
        assert mgr._ip_history[-1]["identity_index"] == 1
        assert mgr._ip_history[-1]["identity"] == \
            mgr._live_identity.get("impersonate") or ""

    @pytest.mark.asyncio
    async def test_recent_ip_rejected_then_fresh(self, tmp_path):
        """A recent IP is rejected on round 0 → container restart → fresh
        IP accepted on round 1."""
        shared = _shared(tmp_path)
        shared.record_ip("1.1.1.1", 1)                 # used recently
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr.ips = ["1.1.1.1", "2.2.2.2"]
        assert await mgr._finalize_ip(allow_stale=False) is True
        assert mgr._current_ip == "2.2.2.2"
        assert mgr.calls["restart"] >= 1               # re-picked a server
        assert mgr._ip_history[-1]["ip"] == "2.2.2.2"
        assert mgr._ip_history[-1]["identity"] == "firefox144"

    @pytest.mark.asyncio
    async def test_all_recent_no_stale_fails(self, tmp_path):
        """Strict freshness (allow_stale=False): 3 recent probes → failure,
        identity NOT advanced, no exception."""
        shared = _shared(tmp_path)
        shared.record_ip("1.1.1.1", 1)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr.ips = ["1.1.1.1", "1.1.1.1", "1.1.1.1"]
        assert await mgr._finalize_ip(allow_stale=False) is False
        assert mgr._current_ip is None
        assert mgr._identity_index == 0                # nothing committed
        assert mgr.calls["restart"] == 2               # rounds 0,1 only

    @pytest.mark.asyncio
    async def test_allow_stale_accepts_on_last_attempt(self, tmp_path):
        """allow_stale=True accepts a recent IP on the last round (a
        constrained boot cannot loop forever)."""
        shared = _shared(tmp_path)
        shared.record_ip("1.1.1.1", 1)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr.ips = ["1.1.1.1", "1.1.1.1", "1.1.1.1"]
        assert await mgr._finalize_ip(allow_stale=True) is True
        assert mgr._current_ip == "1.1.1.1"
        assert mgr._identity_index == 1                # advanced, face committed

    @pytest.mark.asyncio
    async def test_dead_probe_never_escapes(self, tmp_path):
        """A probe exception (network dead) is a failed attempt, never an
        exception escaping to _watchdog_tick / connect."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        async def boom():
            raise RuntimeError("network dead")
        mgr.get_public_ip = boom
        assert await mgr._finalize_ip(allow_stale=False) is False
        assert mgr._current_ip is None

    @pytest.mark.asyncio
    async def test_identity_rotation_off_pins_index(self, tmp_path):
        """identity_rotation: false → no advance, chrome131 forever
        (retrocompat gate)."""
        mgr = FakeVPNManager(_cfg(tmp_path, identity_rotation=False),
                             tmp_path=tmp_path)
        mgr.ips = ["6.6.6.6"]
        assert await mgr._finalize_ip(allow_stale=False) is True
        assert mgr._identity_index == 0
        assert mgr._ip_history[-1]["identity"] == "chrome131"
        assert mgr._ip_history[-1]["identity_index"] == 0

    @pytest.mark.asyncio
    async def test_commit_is_atomic_and_persisted(self, tmp_path):
        """history entry + state file agree after one commit."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        mgr.ips = ["6.6.6.7"]
        assert await mgr._finalize_ip(allow_stale=False) is True
        state_file = tmp_path / "vpn_state1.json"
        assert state_file.exists()
        raw = state_file.read_text(encoding="utf-8")
        assert '"6.6.6.7"' in raw and '"firefox144"' in raw


# ── connect(): cold boot with containers up ─────────────────────

class TestConnect:
    @pytest.mark.asyncio
    async def test_cold_boot_with_recent_ip_forces_rotation(self, tmp_path):
        """Proxy restart with containers still up: the boot IP is 'recent'
        in the shared registry → connect() forces a rotation and commits a
        FRESH IP journalized with a NEW identity (the old code never logged
        the boot IP at all)."""
        shared = _shared(tmp_path)
        shared.record_ip("1.1.1.1", 1)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr.ips = ["1.1.1.1", "5.6.7.8"]
        await mgr.connect()
        assert mgr._status == vm.VPNState.CONNECTED
        assert mgr._current_ip == "5.6.7.8"
        assert mgr.calls["compose_up"] == 1
        assert mgr.calls["restart"] >= 1                # rotation inside connect
        assert mgr._total_switches == 0                 # boot is not a rotation
        assert mgr._ip_history[-1]["ip"] == "5.6.7.8"
        assert mgr._ip_history[-1]["identity"] == "firefox144"
        assert mgr._ip_history[-1]["identity_index"] == 1
        assert mgr._backoff.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_cold_boot_all_recent_safety_valve_connects(self, tmp_path):
        """A fully-constrained boot never hangs: connect() runs
        _finalize_ip(allow_stale=True), so after two full rotations a still-
        recent IP is ACCEPTED (safety valve) rather than failing boot — the
        watchdog/update paths stay strict (allow_stale=False), boot alone may
        relax. The identity still advances to a NEW face on the accepted IP."""
        shared = _shared(tmp_path)
        shared.record_ip("1.1.1.1", 1)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr.ips = ["1.1.1.1", "1.1.1.1", "1.1.1.1"]
        await mgr.connect()
        assert mgr._status == vm.VPNState.CONNECTED
        assert mgr._current_ip == "1.1.1.1"          # accepted on the last attempt
        assert mgr.calls["restart"] == 2             # two rotations were attempted
        assert mgr._identity_index == 1              # NEW face, still advanced
        assert mgr._ip_history[-1]["identity_index"] == 1
        assert mgr._watchdog_backoff.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_connect_when_already_connected_noop(self, tmp_path):
        shared = _shared(tmp_path)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr.ips = ["1.2.3.4"]
        await mgr.connect()
        compose_calls = mgr.calls["compose_up"]
        await mgr.connect()                            # already connected
        assert mgr.calls["compose_up"] == compose_calls


# ── _connect_next_impl: rotation ────────────────────────────────

class TestConnectNext:
    @pytest.mark.asyncio
    async def test_rejects_recent_then_accepts(self, tmp_path):
        shared = _shared(tmp_path)
        shared.record_ip("1.1.1.1", 1)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr.ips = ["1.1.1.1", "2.2.2.2"]
        new_ip = await mgr.connect_next()
        assert new_ip == "2.2.2.2"
        assert mgr._status == vm.VPNState.CONNECTED
        assert mgr._total_switches == 1
        assert shared.is_recent("2.2.2.2")
        # advance-then-journalize: history carries the NEW face
        assert mgr._ip_history[-1]["identity_index"] == 1
        assert mgr._ip_history[-1]["identity"] == "firefox144"
        assert mgr._identity_index == 1
        assert mgr._last_rotation_error is None

    @pytest.mark.asyncio
    async def test_two_rotations_two_faces(self, tmp_path):
        """Each rotation lands on a DIFFERENT profile (index 1 then 2)."""
        shared = _shared(tmp_path)
        shared.record_ip("1.1.1.1", 1)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr.ips = ["1.1.1.1", "2.2.2.2", "2.2.2.2", "3.3.3.3"]
        await mgr.connect_next()                       # → 2.2.2.2
        assert mgr._ip_history[-1]["identity_index"] == 1
        await mgr.connect_next()                       # → 3.3.3.3
        assert mgr._total_switches == 2
        assert mgr._ip_history[-1]["ip"] == "3.3.3.3"
        assert mgr._ip_history[-1]["identity_index"] == 2
        assert mgr._ip_history[-1]["identity"] == "edge101"
        assert {e["identity_index"] for e in mgr._ip_history} == {1, 2}
        assert shared.get_status()["cursor"] == 2      # monotone, never rewound

    @pytest.mark.asyncio
    async def test_all_recent_raises_and_cooldown(self, tmp_path):
        """Recent IPs rejected on attempts 0-1; attempt 2 (the legacy
        last-attempt escape hatch) accepts a RECENT IP, so a dead probe
        (None) is what forces the rotation to fail → RotationFailed +
        fail-fast cooldown armed."""
        shared = _shared(tmp_path)
        shared.record_ip("1.1.1.1", 1)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr.ips = ["1.1.1.1", "1.1.1.1", None]           # recent, recent, dead
        with pytest.raises(vm.RotationFailed, match="public IP"):
            await mgr.connect_next()
        assert mgr._status == vm.VPNState.ERROR
        assert mgr._last_rotation_failed_at is not None   # CRITIC(6) armed
        # Fail-fast cooldown: the next rotation is refused immediately
        with pytest.raises(vm.RotationFailed, match="cooldown"):
            await mgr.connect_next()

    # ── [plan 18/08 §C] rotation died on a DEAD tunnel → egress arm ──
    @pytest.mark.asyncio
    async def test_rotation_dead_probe_arms_egress_watchdog(self, tmp_path):
        """A rotation that dies on a REAL probe failure ("could not
        determine public IP") hands itself to the egress watchdog:
        counter armed at the threshold + wake — the next tick repairs
        (~1 s), not after N idle ticks (the incident's 11 min 45 s
        stall: the dead rotation then went silent)."""
        shared = _shared(tmp_path)
        shared.record_ip("1.1.1.1", 1)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr.ips = ["1.1.1.1", "1.1.1.1", None]           # recent, recent, dead
        mgr._watchdog_event = asyncio.Event()            # fake: start() never runs
        with pytest.raises(vm.RotationFailed, match="public IP"):
            await mgr.connect_next()
        assert mgr._rotation_probe_dead is True
        assert mgr._egress_failures == mgr._auto_wg_egress_ticks
        assert mgr._watchdog_event.is_set()              # wake → live tick

    @pytest.mark.asyncio
    async def test_rotation_unchanged_ip_never_arms(self, tmp_path):
        """"IP unchanged" proves the tunnel ANSWERS — the rotation lost
        the lottery but the tunnel is alive. No arm: arming on this class
        would send the watchdog repairing a perfectly healthy tunnel."""
        shared = _shared(tmp_path)
        shared.record_ip("1.1.1.1", 1)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr._current_ip = "1.1.1.1"                      # old_ip non-None
        mgr.ips = ["1.1.1.1", "1.1.1.1", "1.1.1.1"]      # unchanged ×3
        mgr._watchdog_event = asyncio.Event()
        with pytest.raises(vm.RotationFailed, match="unchanged"):
            await mgr.connect_next()
        assert mgr._rotation_probe_dead is False
        assert mgr._egress_failures == 0
        assert not mgr._watchdog_event.is_set()

    @pytest.mark.asyncio
    async def test_rotation_arm_is_monotone(self, tmp_path):
        """max() keeps the counter monotone: an already-armed watchdog
        (higher counter) stays where it is — no reset, no double logic."""
        shared = _shared(tmp_path)
        shared.record_ip("1.1.1.1", 1)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr.ips = ["1.1.1.1", "1.1.1.1", None]
        mgr._egress_failures = 5                         # pre-armed higher
        with pytest.raises(vm.RotationFailed, match="public IP"):
            await mgr.connect_next()
        assert mgr._egress_failures == 5

    @pytest.mark.asyncio
    async def test_rotation_live_probe_after_dead_one_never_arms(self, tmp_path):
        """Stale-latch guard (audit 6.2): attempt 0 dies on the probe →
        attempt 1 the probe ANSWERS (tunnel lives) then "unchanged" →
        the latch must be cleared by the successful probe — arming would
        send the watchdog repairing a tunnel whose probe just answered,
        and the WARN "real IP probe never answered" would lie."""
        shared = _shared(tmp_path)
        shared.record_ip("1.1.1.1", 1)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr._current_ip = "1.1.1.1"
        mgr.ips = [None, "1.1.1.1", "1.1.1.1"]             # dead, live→unchanged ×2
        mgr._watchdog_event = asyncio.Event()
        with pytest.raises(vm.RotationFailed, match="unchanged"):
            await mgr.connect_next()
        assert mgr._rotation_probe_dead is False           # probe answered
        assert mgr._egress_failures == 0
        assert not mgr._watchdog_event.is_set()

    # ── [plan 18/08 §D] rotation wall — per-attempt deadline ────
    @pytest.mark.asyncio
    async def test_rotation_hits_wall_raises_and_cleans_up(self, tmp_path):
        """The global wall (rotation_max_duration) caps the whole rotation:
        a slow probe that would take 1.5 s runs under wait_for(max(1.0,
        remaining)) → TimeoutError → drain (no op in flight) → loop-top sees
        the deadline gone → RotationFailed naming the wall. The rotation-
        scoped op accounting is torn down (no leak into the next rotation)."""
        shared = _shared(tmp_path)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr._rotation_max_duration = 0.3                   # floor is 5 s at init,
        # so poke the attr directly — the wall semantics are what is tested.
        mgr.ips = ["1.1.1.1"]                              # the probe never answers
        mgr.probe_delay = 1.5                              # slower than the budget
        with pytest.raises(vm.RotationFailed, match="wall"):
            await mgr.connect_next()
        assert mgr._status == vm.VPNState.ERROR
        assert mgr.ips == ["1.1.1.1"]                      # probe 1 started, 0 answered
        assert mgr._rotation_probe_dead is False           # no probe evidence
        assert mgr._egress_failures == 0                   # wall ≠ dead tunnel
        # cleanup: a new rotation must not inherit the old accounting
        assert mgr._rotation_op_count == 0
        assert mgr._rotation_op_event is None
        assert mgr._rotation_loop is None

    @pytest.mark.asyncio
    async def test_rotation_wall_threshold_but_crosses_it(self, tmp_path):
        """A generous budget lets the rotation CROSS an early failure — the
        wait_for wrapper is per-ATTEMPT, not a one-shot: attempt 0 dies on a
        dead probe, backoff pause is capped to the remaining wall, attempt 1
        succeeds and returns the fresh IP (extraction regression)."""
        shared = _shared(tmp_path)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr._current_ip = "1.1.1.1"
        mgr.ips = [None, "2.2.2.2"]                        # dead probe, then fresh
        await mgr.connect_next()
        assert mgr._status == vm.VPNState.CONNECTED
        assert mgr._current_ip == "2.2.2.2"
        assert mgr._rotation_probe_dead is False           # the answer cleared it
        assert mgr._ip_history[-1]["ip"] == "2.2.2.2"
        assert mgr._rotation_op_count == 0                 # torn down on success
        assert mgr._rotation_op_event is None
        assert mgr._rotation_loop is None

    @pytest.mark.asyncio
    async def test_drain_waits_for_real_thread_end(self, tmp_path):
        """am.8 unit: when a rotation deadline expires while a docker op is
        still running (asyncio.to_thread is NOT cancellable), the drain waits
        for its REAL end — including the level-triggered hazard where op_ev
        is already set (clear lands a whole cycle, the wait then blocks until
        the worker signals again). count == 0 returns instantly."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        loop = asyncio.get_running_loop()

        # (a) stale-set hazard: event already set but the op is still alive
        mgr._rotation_loop = loop
        mgr._rotation_op_event = asyncio.Event()
        mgr._rotation_op_event.set()                       # stale set (level-triggered)
        mgr._rotation_op_count = 1

        def _slow_worker():
            time.sleep(0.05)
            mgr._rotation_op_count = max(0, mgr._rotation_op_count - 1)
            loop.call_soon_threadsafe(mgr._rotation_op_event.set)

        task = asyncio.create_task(asyncio.to_thread(_slow_worker))
        t0 = time.monotonic()
        await mgr._await_rotation_ops_drained()
        elapsed = time.monotonic() - t0
        await task
        assert elapsed >= 0.04                    # blocked on the REAL end
        assert mgr._rotation_op_count == 0

        # (b) nothing in flight → returns immediately
        mgr._rotation_op_count = 0
        t0 = time.monotonic()
        await mgr._await_rotation_ops_drained()
        assert time.monotonic() - t0 < 0.01


# ── _watchdog_tick: AUTH_FAILED recovery ────────────────────────

class TestWatchdogTick:
    @pytest.mark.asyncio
    async def test_auth_failed_recovery_lands_on_fresh_ip(self, tmp_path):
        """AUTH_FAILED → restart → the marker is gone → _finalize_ip commits
        a FRESH IP with a NEW identity → recovered (backoff reset, CONNECTED).
        The watchdog must never land back on the failed server/IP."""
        shared = _shared(tmp_path)
        shared.record_ip("4.4.4.4", 1)                 # the failed IP is recent
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr.log_text = "AUTH_FAILED - credentials rejected"
        # round 0 → recent failed IP rejected; round 1 → FRESH IP committed;
        # then the post-recovery probe + the second tick's probe
        mgr.ips = ["4.4.4.4", "5.5.5.5", "5.5.5.5", "5.5.5.5"]
        await mgr._watchdog_tick()
        assert mgr._status == vm.VPNState.CONNECTED
        assert mgr._auth_failed is False
        assert mgr._current_ip == "5.5.5.5"              # NEVER back on 4.4.4.4
        assert mgr._ip_history[-1]["identity_index"] == 1  # NEW face
        assert mgr._ip_history[-1]["identity"] == "firefox144"
        assert mgr._watchdog_backoff.consecutive_failures == 0
        assert mgr.escalations == 0
        # [plan 18/08 §E3/am.20] Recovery is RUNG: the LIGHT `docker restart`
        # comes first and heals by clearing the AUTH_FAILED marker on the
        # fresh logs — the tick itself never composes. The single compose_up
        # is finalize's re-pick round only: the stale _auth_failed attribute
        # (a heal check does not clear it) forces the --force-recreate there.
        assert mgr.calls["restart"] == 1
        assert mgr.calls["compose_up"] == 1
        assert mgr.calls["force_recreate"] == 1

        # Second tick: tunnel healthy → backoff stays reset, no restart
        await mgr._watchdog_tick()
        assert mgr.calls["restart"] == 1
        assert mgr._watchdog_backoff.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_auth_failed_still_recent_never_lands(self, tmp_path):
        """The restart rotated the logs but the tunnel comes back with the
        SAME recent IP → not fresh → the tick keeps the failure state (the
        watchdog never commits a stale IP)."""
        shared = _shared(tmp_path)
        shared.record_ip("4.4.4.4", 1)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr.log_text = "AUTH_FAILED"
        mgr.ips = ["4.4.4.4", "4.4.4.4", "4.4.4.4"]     # only the recent IP exists
        await mgr._watchdog_tick()
        assert mgr._status == vm.VPNState.ERROR
        # The flag reflects the last DETECTED failure: the tick's refresh saw
        # AUTH_FAILED and nothing committed since → still True (it is cleared
        # by the next healthy refresh / commit).
        assert mgr._auth_failed is True
        assert mgr._ip_history == []                     # nothing committed
        assert mgr._watchdog_backoff.consecutive_failures == 1
        assert mgr.escalations == 0
        assert mgr.finalize_calls == 1                   # finalize really ran

    @pytest.mark.asyncio
    async def test_persistent_failure_escalates_after_two(self, tmp_path):
        """AUTH_FAILED survives the recreate (the pool keeps returning
        failing servers, logs still dirty, no fresh IP): failure #1 backs
        off at 30s; failure #2 escalates at 60s — never a 7-minute silent
        error state again. Each tick tries the LIGHT restart first, then the
        compose --force-recreate once the marker survived (rung 2 is the
        escalation, not a duplicate restart); _finalize_ip is never reached
        (re-check still dirty)."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        mgr.log_text = "AUTH_FAILED"
        mgr.clear_logs_on_restart = False                # recreate changes nothing
        mgr.ips = []
        await mgr._watchdog_tick()
        assert mgr._watchdog_backoff.consecutive_failures == 1
        assert mgr._watchdog_backoff.delay == 30         # 15 * 2^1
        assert mgr.escalations == 0
        await mgr._watchdog_tick()
        assert mgr._watchdog_backoff.consecutive_failures == 2
        assert mgr._watchdog_backoff.delay == 60         # capped at backoff_max
        assert mgr.escalations == 1                      # escalated, ≥2 failures
        assert mgr.calls["restart"] == 2                 # light restart per tick
        assert mgr.calls["compose_up"] == 2              # one recreate per tick
        assert mgr.calls["force_recreate"] == 2          # marker survived → widen
        assert mgr.finalize_calls == 0                   # never recovered

    @pytest.mark.asyncio
    async def test_healthy_tick_resets_backoff(self, tmp_path):
        """A clean tick resets a previously failing backoff to zero."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        mgr._watchdog_backoff.record_failure()
        mgr._watchdog_backoff.record_failure()
        assert mgr._watchdog_backoff.consecutive_failures == 2
        mgr.ips = ["1.2.3.4"]                            # probe answer
        await mgr._watchdog_tick()
        assert mgr._watchdog_backoff.consecutive_failures == 0
        assert mgr._watchdog_backoff.delay == 15
        assert mgr.calls["restart"] == 0

    # ── [plan 18/08 §2d] WireGuard egress watchdog ──────────────

    @pytest.mark.asyncio
    async def test_wg_egress_dead_arms_recovery_at_threshold(self, tmp_path):
        """WG tunnel without egress: the control API still answers "running"
        and no OpenVPN marker exists — only the SOCKS5 egress probe detects
        it. Ticks 1..(threshold-1) arm the shared counter (backoff untouched,
        no recovery action); the threshold tick arms the recovery: fast-pin
        is OFF without a control key, and the restart is PLAIN (no OpenVPN
        marker → no --force-recreate; the tick's restart + the 2 finalize
        re-pick rounds). Threshold read symbolically — never a literal."""
        mgr = FakeVPNManager(_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path)
        mgr.probe_alive = False              # dead tunnel: probe answers nothing
        seuil = mgr._auto_wg_egress_ticks
        for _ in range(seuil - 1):
            await mgr._watchdog_tick()
        assert mgr._egress_failures == seuil - 1
        assert mgr.calls["restart"] == 0      # waiting — no recovery action
        assert mgr.calls["compose_up"] == 0
        assert mgr._watchdog_backoff.consecutive_failures == 0
        await mgr._watchdog_tick()
        assert mgr._egress_failures == seuil
        assert mgr.calls["restart"] == 3      # tick's restart + 2 finalize rounds
        assert mgr.calls["compose_up"] == 0   # no OpenVPN marker → plain restart
        assert mgr.escalations == 0           # first failure — no escalation yet

    @pytest.mark.asyncio
    async def test_wg_egress_recovery_lands_on_fresh_ip(self, tmp_path):
        """Dead-tunnel recovery lands on a FRESH IP (9.9.9.9), CONNECTED;
        the next healthy tick resets the egress counter — no further
        restart."""
        mgr = FakeVPNManager(_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path)
        mgr.probe_alive = False
        # Tick 1 (unarmed) pops one IP via refresh_status; the recovery tick
        # (armed — the LIGHT probe is stubbed, it never pops): finalize round
        # 0 + post-recovery refresh. [None, "9.9.9.9", "9.9.9.9"] exactly.
        mgr.ips = [None] + ["9.9.9.9", "9.9.9.9"]
        seuil = mgr._auto_wg_egress_ticks
        for _ in range(seuil - 1):
            await mgr._watchdog_tick()
        assert mgr._egress_failures == seuil - 1
        await mgr._watchdog_tick()
        assert mgr._status == vm.VPNState.CONNECTED
        assert mgr._current_ip == "9.9.9.9"
        assert mgr.calls["restart"] == 1      # tick's restart only
        assert mgr._egress_failures == seuil  # reset on the next probe success
        mgr.probe_alive = True
        mgr.ips = ["9.9.9.9"]                 # armed tick skips refresh — no pop
        await mgr._watchdog_tick()
        assert mgr._egress_failures == 0
        assert mgr.calls["restart"] == 1      # healthy — no restart

    @pytest.mark.asyncio
    async def test_wg_egress_success_resets_counter(self, tmp_path):
        """A probe success resets the shared counter MID-arming: a transient
        blip (a real failure signalled by the pool, then a healthy probe) is
        absorbed — ZERO recovery, no restart, no compose."""
        mgr = FakeVPNManager(_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path)
        seuil = mgr._auto_wg_egress_ticks
        mgr._egress_failures = seuil - 1      # nearly armed
        mgr.probe_alive = True                # healthy probe (explicit)
        mgr.ips = ["1.1.1.1"]                 # armed tick skips refresh — no pop
        await mgr._watchdog_tick()
        assert mgr._egress_failures == 0
        assert mgr.calls["restart"] == 0
        assert mgr.calls["compose_up"] == 0
        assert mgr._watchdog_backoff.consecutive_failures == 0

    # ── [plan 18/08 §am.22] egress death → cancel in-flight streams ─

    @pytest.mark.asyncio
    async def test_egress_dead_cancels_inflight_streams_at_threshold(self, tmp_path, monkeypatch):
        """The pool registered the streams on this tunnel; when the threshold
        tick CONFIRMS egress death (probe dead 3/3), the tick calls
        pool.cancel_streams(self) — exactly once, at the threshold, never
        while arming. The pool-side cancel semantics are covered in
        test_pool_connection_failure.py; this proves the WIRING."""
        mgr = FakeVPNManager(_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path)
        mgr.probe_alive = False

        class _RecPool:
            def __init__(self):
                self.cancelled = []

            def cancel_streams(self, station):
                self.cancelled.append(station)

        rec = _RecPool()
        monkeypatch.setattr(shared_state, "free_ip_pool", rec, raising=False)
        seuil = mgr._auto_wg_egress_ticks
        for _ in range(seuil - 1):
            await mgr._watchdog_tick()
        assert rec.cancelled == [], "no cancel while arming (1..threshold-1)"
        await mgr._watchdog_tick()
        assert rec.cancelled == [mgr], "the CONFIRMED-death tick cancels this tunnel's streams"
        # The recovery (restart + finalize rounds) ran normally afterwards.
        assert mgr.calls["restart"] == 3

    @pytest.mark.asyncio
    async def test_egress_blip_never_cancels_streams(self, tmp_path, monkeypatch):
        """A transient blip (probe recovers before the threshold) is
        absorbed: counter reset, ZERO recovery, ZERO cancel — the streams on
        a healthy tunnel keep streaming."""
        mgr = FakeVPNManager(_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path)
        mgr._egress_failures = mgr._auto_wg_egress_ticks - 1   # nearly armed

        class _RecPool:
            def __init__(self):
                self.cancelled = []

            def cancel_streams(self, station):
                self.cancelled.append(station)

        rec = _RecPool()
        monkeypatch.setattr(shared_state, "free_ip_pool", rec, raising=False)
        mgr.probe_alive = True                 # healthy probe — blip absorbed
        mgr.ips = ["1.1.1.1"]                  # armed tick skips refresh — no pop
        await mgr._watchdog_tick()
        assert mgr._egress_failures == 0
        assert rec.cancelled == [], "a healthy tunnel's streams are never cancelled"
        assert mgr.calls["restart"] == 0

    @pytest.mark.asyncio
    async def test_egress_dead_without_pool_noop(self, tmp_path, monkeypatch):
        """No pool (self-heal mode / unit test): the tick survives — the
        None-guard keeps the cancel out of the recovery path."""
        mgr = FakeVPNManager(_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path)
        mgr.probe_alive = False
        monkeypatch.setattr(shared_state, "free_ip_pool", None, raising=False)
        seuil = mgr._auto_wg_egress_ticks
        for _ in range(seuil):
            await mgr._watchdog_tick()
        assert mgr._egress_failures == seuil

    # ── [plan 18/08 §E3/am.20] light restart rung before compose ─

    @pytest.mark.asyncio
    async def test_ov_light_restart_heals_without_compose(self, tmp_path):
        """OpenVPN AUTH_FAILED: the LIGHT `docker restart` rung heals (fresh
        logs, no marker) → fresh IP, CONNECTED, ZERO compose. The 60-120 s
        compose wall collapses to the ~1-2 s restart (am.20: OV 120-180 s →
        30-60 s; the wait_healthy fake is instant)."""
        shared = _shared(tmp_path)
        shared.record_ip("4.4.4.4", 1)
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        mgr.log_text = "AUTH_FAILED - credentials rejected"
        mgr.ips = ["5.5.5.5", "5.5.5.5", "5.5.5.5"]
        await mgr._watchdog_tick()
        assert mgr._status == vm.VPNState.CONNECTED
        assert mgr._current_ip == "5.5.5.5"
        assert mgr.calls["restart"] == 1       # the light rung only
        assert mgr.calls["compose_up"] == 0    # never escalated — marker cleared
        assert mgr.calls["force_recreate"] == 0
        assert mgr._watchdog_backoff.consecutive_failures == 0
        assert mgr.escalations == 0

    @pytest.mark.asyncio
    async def test_ov_marker_survives_light_restart_escalates_to_compose(self, tmp_path):
        """OpenVPN AUTH_FAILED survives the light restart (the pool keeps
        returning failing servers): the compose rung is the escalation —
        --force-recreate is flagged because the marker survived (a plain
        restart could never apply the widened SERVER_COUNTRIES pool)."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        mgr.log_text = "AUTH_FAILED"
        mgr.clear_logs_on_restart = False      # restart + recreate change nothing
        mgr.ips = []
        await mgr._watchdog_tick()
        assert mgr.calls["restart"] == 1       # light rung attempted
        assert mgr.calls["compose_up"] == 1    # …then the compose escalation
        assert mgr.calls["force_recreate"] == 1
        assert mgr.finalize_calls == 0         # re-check still dirty
        assert mgr._watchdog_backoff.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_wg_light_restart_heals_but_no_fresh_skips_compose(self, tmp_path):
        """WG dead tunnel: the light restart heals the container (no marker
        to clear — nothing "widens") but no FRESH IP is reachable (only the
        recently-used 1.1.1.1): back off WITHOUT composing. The compose rung
        exists solely for a surviving marker — healed+stale must not pay the
        compose wall."""
        shared = _shared(tmp_path)
        shared.record_ip("1.1.1.1", 1)
        mgr = FakeVPNManager(_cfg(tmp_path, vpn_stack="wireguard"),
                             shared=shared, tmp_path=tmp_path)
        mgr.probe_alive = False
        mgr.ips = ["1.1.1.1"] * 4              # tick1 refresh + 3 finalize rounds
        seuil = mgr._auto_wg_egress_ticks
        for _ in range(seuil - 1):
            await mgr._watchdog_tick()
        mgr.ips = ["1.1.1.1"] * 3              # recovery tick: 3 finalize rounds
        await mgr._watchdog_tick()
        assert mgr._ip_history == []           # never committed 1.1.1.1
        assert mgr.calls["compose_up"] == 0    # healed → compose rung skipped
        assert mgr.calls["force_recreate"] == 0
        assert mgr.calls["restart"] == 3       # light rung + 2 finalize re-picks
        assert mgr._watchdog_backoff.consecutive_failures == 1
        assert mgr.escalations == 0

    @pytest.mark.asyncio
    async def test_light_restart_error_escapes_via_compose_rung(self, tmp_path):
        """The light restart itself FAILS (docker daemon hiccup): the tick
        does not crash — the compose rung is the escape hatch, --force-recreate
        applies, and recovery completes on a fresh IP."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        mgr.log_text = "AUTH_FAILED"
        mgr.ips = ["6.6.6.6", "6.6.6.6", "6.6.6.6"]

        async def boom():
            mgr.calls["restart"] += 1
            raise RuntimeError("docker daemon unreachable")
        mgr._docker_restart = boom

        await mgr._watchdog_tick()
        assert mgr._status == vm.VPNState.CONNECTED
        assert mgr._current_ip == "6.6.6.6"
        assert mgr.calls["restart"] == 1       # the failed light rung
        assert mgr.calls["compose_up"] == 1    # compose took over
        assert mgr.calls["force_recreate"] == 1
        assert mgr._watchdog_backoff.consecutive_failures == 0

# ── retrocompat: shared=None ────────────────────────────────────

class TestRetroCompat:
    @pytest.mark.asyncio
    async def test_no_shared_uses_local_history_tail(self, tmp_path):
        """Without a shared registry: recent = _ip_history[-10:] (legacy);
        advance = (idx+1) % n (legacy)."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        mgr.ips = ["7.7.7.7"]
        assert await mgr._finalize_ip(allow_stale=False) is True
        assert mgr._identity_index == 1
        mgr.ips = ["7.7.7.7", "8.8.8.8"]
        assert await mgr._finalize_ip(allow_stale=False) is True
        assert mgr._current_ip == "8.8.8.8"              # 7.7.7.7 rejected (local)
        assert mgr._identity_index == 2

    @pytest.mark.asyncio
    async def test_state_reload_restores_index_and_history(self, tmp_path):
        """save_state/load_state round-trip: identity continues where it
        left off (no reset to chrome131) and the history survives."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        mgr.ips = ["1.1.1.1"]
        await mgr._finalize_ip(allow_stale=False)        # index 1
        mgr.ips = ["2.2.2.2"]
        await mgr._finalize_ip(allow_stale=False)        # index 2
        reloaded = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        assert reloaded._identity_index == 2
        assert reloaded._ip_history[-1]["ip"] == "2.2.2.2"
        assert reloaded._ip_history[-1]["identity"] == "edge101"


# ── cross-station: one shared registry, two fakes ───────────────

class TestCrossStation:
    @pytest.mark.asyncio
    async def test_other_stations_ip_is_recent(self, tmp_path):
        """Station 2 must never land on an IP station 1 just used."""
        shared = _shared(tmp_path)
        m1 = FakeVPNManager(_cfg(tmp_path), station=1, shared=shared,
                            tmp_path=tmp_path)
        m2 = FakeVPNManager(_cfg(tmp_path), station=2, shared=shared,
                            tmp_path=tmp_path)
        m1.ips = ["3.3.3.3"]
        assert await m1._finalize_ip(allow_stale=False) is True
        m2.ips = ["3.3.3.3", "4.4.4.4"]
        assert await m2._finalize_ip(allow_stale=False) is True
        assert m2._current_ip == "4.4.4.4"               # 3.3.3.3 rejected
        # Attribution recorded per station
        assert {e["station"] for e in shared._ip_events} == {1, 2}

    @pytest.mark.asyncio
    async def test_live_indexes_never_collide(self, tmp_path):
        """The two stations advance through the SHARED absolute cursor →
        live identity indexes stay distinct."""
        shared = _shared(tmp_path)
        m1 = FakeVPNManager(_cfg(tmp_path), station=1, shared=shared,
                            tmp_path=tmp_path)
        m2 = FakeVPNManager(_cfg(tmp_path), station=2, shared=shared,
                            tmp_path=tmp_path)
        m1.ips = ["1.1.1.1"]
        await m1.connect()
        m2.ips = ["2.2.2.2"]
        await m2.connect()
        assert m1._identity_index != m2._identity_index
        assert m1.current_identity["impersonate"] != m2.current_identity["impersonate"]
        st = shared.get_status()
        assert st["last_index_by_station"] == {1: m1._identity_index,
                                               2: m2._identity_index}


# ── _watchdog_loop: cadence interval ↔ backoff ───────────────────

class TestWatchdogLoop:
    @pytest.mark.asyncio
    async def test_cadence_interval_then_backoff_then_interval(self, tmp_path, monkeypatch):
        """Healthy ticks pace at watchdog_interval; a failure switches the
        sleep to the backoff delay (base 15 → 30 after one failure); a clean
        tick returns to the interval."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        ticks = {"n": 0}

        async def fake_tick():
            ticks["n"] += 1
            if ticks["n"] == 2:
                mgr._watchdog_backoff.record_failure()   # AUTH_FAILED detected
            elif ticks["n"] == 3:
                mgr._watchdog_backoff.record_success()   # recovered

        mgr._watchdog_tick = fake_tick
        recorded = []

        async def fake_sleep(delay):
            recorded.append(delay)
            if len(recorded) >= 4:
                raise asyncio.CancelledError

        monkeypatch.setattr(vm.asyncio, "sleep", fake_sleep)
        with pytest.raises(asyncio.CancelledError):
            await mgr._watchdog_loop()
        assert ticks["n"] == 4
        assert recorded[:3] == [mgr._watchdog_interval, 30.0,
                                mgr._watchdog_interval]
        assert len(recorded) == 4                 # 4th sleep never taken

    @pytest.mark.asyncio
    async def test_cadence_armed_interval_while_egress_failures(self, tmp_path, monkeypatch):
        """[plan 18/08 §E2/review 18/08] ARMED state (egress_failures > 0)
        paces ticks at egress_failure_tick_interval (2 s), not the idle
        watchdog_interval — the 2 s ramp of the refonte 1d. Backoff still
        wins when set; the armed cadence is the default while armed."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        ticks = {"n": 0}

        async def fake_tick():
            ticks["n"] += 1
            mgr._egress_failures = 1          # armed on every tick

        mgr._watchdog_tick = fake_tick
        recorded = []

        async def fake_sleep(delay):
            recorded.append(delay)
            if len(recorded) >= 3:
                raise asyncio.CancelledError

        monkeypatch.setattr(vm.asyncio, "sleep", fake_sleep)
        with pytest.raises(asyncio.CancelledError):
            await mgr._watchdog_loop()
        assert ticks["n"] == 3
        assert recorded[:2] == [mgr._egress_failure_tick_interval] * 2
        assert mgr._egress_failure_tick_interval != mgr._watchdog_interval


# ── [plan 18/08 §1c] pool signal → arm + wake ────────────────

class TestArmEgressWatchdog:
    @pytest.mark.asyncio
    async def test_arm_sets_counter_to_threshold(self, tmp_path):
        """One real failure (pool signal) arms the FULL threshold at once:
        the very next tick probes/recovers — no N-tick wait. The max()
        absorbs a signal that arrives mid-arming."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        seuil = mgr._auto_wg_egress_ticks
        mgr._egress_failures = 1          # already mid-arming (WG ticks)

        mgr.arm_egress_watchdog()

        assert mgr._egress_failures == seuil

    @pytest.mark.asyncio
    async def test_arm_wakes_the_watchdog_event(self, tmp_path):
        """The pool signal sets _watchdog_event: the loop's
        wait(timeout) returns immediately → live tick in ~0-2 s instead
        of the next idle cadence."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        mgr._watchdog_event = asyncio.Event()   # manual (am.6)

        mgr.arm_egress_watchdog()

        assert mgr._watchdog_event.is_set()

    @pytest.mark.asyncio
    async def test_arm_none_guard_before_start(self, tmp_path):
        """_watchdog_event is None before start() (piège 14) — the arm
        must not crash on a not-yet-started manager."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        assert mgr._watchdog_event is None

        mgr.arm_egress_watchdog()         # must not raise

        assert mgr._egress_failures == mgr._auto_wg_egress_ticks

    @pytest.mark.asyncio
    async def test_arm_records_observability(self, tmp_path):
        """am.23: last failure time + signal count — the only way to
        debug a parasitic fast-recover straight from the API."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        assert mgr._last_conn_failure_at is None
        assert mgr._conn_failure_signal_count == 0

        mgr.arm_egress_watchdog()
        mgr.arm_egress_watchdog()

        assert mgr._last_conn_failure_at is not None
        assert mgr._conn_failure_signal_count == 2
        # Public keys (am.23) — the exact surface a user debugs from, in
        # BOTH get_status()["watchdog"] and stack_info(): a parasitic
        # fast-recover is visible as an armed counter that keeps re-arming.
        st = mgr.get_status()["watchdog"]
        assert st["egress_failures"] == mgr._auto_wg_egress_ticks
        assert st["egress_armed"] is True
        assert st["egress_threshold"] == mgr._auto_wg_egress_ticks
        assert st["egress_tick_interval"] == mgr._egress_failure_tick_interval
        assert st["last_conn_failure_at"] is not None
        # [review 18/08] the key is signal_count in BOTH surfaces (was
        # conn_failure_signal_count in get_status — a rename, no API break:
        # dashboard app.js only reads s.egress_failures).
        assert st["signal_count"] == 2
        si = mgr.stack_info()
        assert si["egress_failures"] == mgr._auto_wg_egress_ticks
        assert si["egress_armed"] is True
        assert si["last_conn_failure_at"] is not None
        assert si["signal_count"] == 2

    @pytest.mark.asyncio
    async def test_arm_then_healthy_probe_absorbs_blip(self, tmp_path):
        """"sonde saine annule la réparation armée": a real failure then
        a HEALTHY probe = a transient blip — the armed tick resets the
        counter, ZERO recovery (no restart, no compose, no escalation).
        One arm + one tick, and the tunnel is exactly where it was."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        mgr.arm_egress_watchdog()
        assert mgr._egress_failures == mgr._auto_wg_egress_ticks
        mgr.probe_alive = True            # the blip already passed
        mgr.ips = []                      # armed tick skips refresh — no pop

        await mgr._watchdog_tick()

        assert mgr._egress_failures == 0
        assert mgr.calls["restart"] == 0
        assert mgr.calls["compose_up"] == 0
        assert mgr.escalations == 0

    @pytest.mark.asyncio
    async def test_arm_then_dead_probe_recovers_in_one_tick(self, tmp_path):
        """Armed + dead probe → the threshold tick recovers IMMEDIATELY
        (one tick, not N): the arm collapses the 3-tick ramp into ~0-2 s
        (detection 110-130 s → ~1 s after the first real failure). The
        counter stays ARM-ED after a successful recovery (arm 3 + dead
        probe 1) — the next healthy tick resets it, keeping the fast
        follow-up cadence right after a recovery."""
        mgr = FakeVPNManager(_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path)
        mgr.arm_egress_watchdog()
        mgr.probe_alive = False           # tunnel still dead

        await mgr._watchdog_tick()

        assert mgr._egress_failures == mgr._auto_wg_egress_ticks + 1
        assert mgr.calls["restart"] == 3  # tick's restart + 2 finalize rounds
        assert mgr.calls["compose_up"] == 0

    @pytest.mark.asyncio
    async def test_arm_ov_stack_recovers_in_one_tick(self, tmp_path):
        """The INCIDENT stack: OpenVPN. The shared counter + unified
        light probe cover OV without traffic — before the refonte, the
        OV branch reset the counter every tick (ZERO detection)."""
        mgr = FakeVPNManager(_cfg(tmp_path, vpn_stack="openvpn"), tmp_path=tmp_path)
        mgr.arm_egress_watchdog()
        mgr.probe_alive = False

        await mgr._watchdog_tick()

        assert mgr._egress_failures == mgr._auto_wg_egress_ticks + 1
        assert mgr.calls["restart"] == 3
        assert mgr.escalations == 0

    @pytest.mark.asyncio
    async def test_arm_during_rotation_skips_tick_once(self, tmp_path):
        """Garde 2529: a rotation in flight must keep skipping the tick
        (a restart would race its IP validation) — but the skip is now
        TRACED once per rotation (am.4), so a lost pool wake is visible.
        The rotation ends healthy → the armed tick probes → healthy →
        reset, ZERO recovery."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        mgr.arm_egress_watchdog()
        rotation = asyncio.get_running_loop().create_future()
        mgr._rotation_task = rotation

        await mgr._watchdog_tick()        # skip — rotation in flight

        assert mgr._skipped_rotation_task is rotation, \
            "the skip is traced per rotation task (not a resettable flag)"
        assert mgr.calls["restart"] == 0
        assert mgr.escalations == 0
        assert mgr._egress_failures == mgr._auto_wg_egress_ticks  # unchanged

        mgr._rotation_task = None         # rotation done
        mgr.probe_alive = True            # the blip passed
        await mgr._watchdog_tick()

        assert mgr._egress_failures == 0  # healthy probe absorbs the arm
        assert mgr.calls["restart"] == 0


# ── health_check: tunnel re-picked an IP outside a rotation ──────

@contextlib.contextmanager
def _ip_probe(ip):
    """Local HTTP server answering health_check's IP probe (offline)."""
    server = _IpProbeServer(ip)
    try:
        yield server
    finally:
        server.close()


class _IpProbeServer:
    def __init__(self, ip):
        self._ip = ip
        handler = type("_IpProbeHandler", (BaseHTTPRequestHandler,), {
            # "self" below is the HANDLER instance; bound methods of the
            # outer _IpProbeServer must not be used (no send_response etc.)
            "do_GET": lambda self: _IpProbeServer._serve(self, ip),
            "log_message": staticmethod(lambda *a, **k: None),
        })
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @staticmethod
    def _serve(handler, ip):
        body = ip.encode("ascii")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def close(self):
        self._server.shutdown()
        self._server.server_close()


class TestHealthCheckIpChanged:
    @pytest.mark.asyncio
    async def test_new_ip_commits_advance_and_history(self, tmp_path, monkeypatch):
        """The tunnel re-picked an IP without a rotation → the change is
        journalized with a NEW face (registry + history + state file)."""
        shared = _shared(tmp_path)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr._status = vm.VPNState.CONNECTED
        mgr._current_ip = "198.51.100.10"
        monkeypatch.setattr(type(mgr), "socks5_url", None)  # probe directly
        with _ip_probe("203.0.113.77") as server:
            mgr._ip_check_url = server.url
            result = await mgr.health_check()
        assert result["ok"] is True and result["ip_changed"] is True
        assert mgr._current_ip == "203.0.113.77"
        assert mgr._identity_index == 1           # advanced to a NEW face
        assert mgr._ip_history[-1]["ip"] == "203.0.113.77"
        assert mgr._ip_history[-1]["identity"] == "firefox144"
        assert mgr._ip_history[-1]["identity_index"] == 1
        assert shared.is_recent("203.0.113.77")   # registry in sync
        state = (tmp_path / "vpn_state1.json").read_text()
        assert '"203.0.113.77"' in state          # persisted, not just memory

    @pytest.mark.asyncio
    async def test_same_ip_no_double_advance(self, tmp_path, monkeypatch):
        """Same IP → no change: no advance, no history entry (finding-j
        regression: a concurrent rotation must never be double-counted)."""
        shared = _shared(tmp_path)
        mgr = FakeVPNManager(_cfg(tmp_path), shared=shared, tmp_path=tmp_path)
        mgr._status = vm.VPNState.CONNECTED
        mgr._current_ip = "203.0.113.77"
        monkeypatch.setattr(type(mgr), "socks5_url", None)  # probe directly
        with _ip_probe("203.0.113.77") as server:
            mgr._ip_check_url = server.url
            result = await mgr.health_check()
        assert result["ip_changed"] is False
        assert mgr._identity_index == 0
        assert mgr._ip_history == []
        assert shared.recent_ips() == []


# ── apply_update: post-recreate finalize + rollback ──────────────

class TestApplyUpdate:
    def _mgr(self, tmp_path):
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        mgr._update_available = True
        mgr._update_old_image_id = "sha256:old"
        mgr._UPDATE_LOCK_PATH = str(tmp_path / "vpn_update.lock")
        return mgr

    def _finalize_stub(self, mgr, outcome, log):
        async def _finalize(allow_stale=False):
            log["calls"] += 1
            log["allow_stale"] = allow_stale
            if outcome:
                mgr._current_ip = "9.9.9.9"
            return outcome
        return _finalize

    @pytest.mark.asyncio
    async def test_success_finalizes_fresh_ip(self, tmp_path):
        """Compose recreated the container → _finalize_ip(allow_stale=False)
        commits a FRESH ip; the update is marked applied, nothing rolled back."""
        mgr = self._mgr(tmp_path)
        mgr.ips = ["9.9.9.9"]                     # refresh probe after apply
        log = {"calls": 0}
        mgr._finalize_ip = self._finalize_stub(mgr, True, log)
        result = await mgr.apply_update()
        assert result["ok"] is True and result["ip"] == "9.9.9.9"
        assert log["calls"] == 1
        assert log["allow_stale"] is False        # strict post-update freshness
        assert mgr.calls["docker_run"] == 1       # only the apply compose
        assert mgr._update_available is False
        assert mgr._update_applied_at is not None
        assert mgr._update_old_image_id is None   # rollback state cleared
        assert mgr._update_last_error is None

    @pytest.mark.asyncio
    async def test_auth_failed_after_update_rolls_back(self, tmp_path):
        """The recreated container comes back with AUTH_FAILED → rollback,
        finalize never runs, the update stays pending."""
        mgr = self._mgr(tmp_path)
        mgr.log_text = "AUTH_FAILED"
        log = {"calls": 0}
        mgr._finalize_ip = self._finalize_stub(mgr, True, log)
        result = await mgr.apply_update()
        assert result["ok"] is False
        assert "AUTH_FAILED" in result["error"]
        assert mgr._auth_failed is True
        assert log["calls"] == 0
        assert mgr.calls["docker_run"] == 3       # apply + rollback tag + compose
        assert mgr._update_available is True      # never marked applied

    @pytest.mark.asyncio
    async def test_finalize_failure_rolls_back(self, tmp_path):
        """A fresh IP could not be validated after the recreate → rollback
        (the update must not leave the tunnel on a stale/recent IP)."""
        mgr = self._mgr(tmp_path)
        log = {"calls": 0}
        mgr._finalize_ip = self._finalize_stub(mgr, False, log)
        result = await mgr.apply_update()
        assert result["ok"] is False
        assert "could not finalize a fresh IP" in result["error"]
        assert log["calls"] == 1
        assert log["allow_stale"] is False
        assert mgr.calls["docker_run"] == 3
        assert mgr._update_available is True


# ── Hot-reload: watchdog interval + config echo-back (review fixes) ──

class TestConfigHotReload:
    def test_watchdog_interval_below_30_honored(self, tmp_path):
        """config.yaml ships watchdog_interval: 7 for fast AUTH_FAILED
        detection; the previous max(30, …) clamp silently paced healthy
        watchdog ticks at 30 s — the reviewer-flagged intent/effect
        mismatch. The floor must only catch pathological values."""
        mgr = FakeVPNManager(_cfg(tmp_path, watchdog_interval=7), tmp_path=tmp_path)
        assert mgr._watchdog_interval == 7
        assert mgr._watchdog_backoff._base_delay == 15.0   # backoff untouched

    @pytest.mark.asyncio
    async def test_watchdog_interval_hot_reload_honors_value(self, tmp_path):
        mgr = FakeVPNManager(_cfg(tmp_path, watchdog_interval=30), tmp_path=tmp_path)
        await mgr.update_config({"watchdog_interval": 5})
        assert mgr._watchdog_interval == 5

    @pytest.mark.asyncio
    async def test_get_config_echoes_hot_reloaded_windows(self, tmp_path):
        """get_config() must reflect a hot-reloaded window. Before the fix
        self._config stayed stale after update_config: the dashboard form
        re-showed the OLD value and a re-submit silently reverted the live
        registry back to it."""
        mgr = FakeVPNManager(
            _cfg(tmp_path, recent_ip_window=20, recent_ip_max_age=1800,
                 shared_rotation_file="logs/shared_rotation.json"),
            tmp_path=tmp_path)
        cfg = await mgr.update_config(
            {"recent_ip_window": 60, "recent_ip_max_age": 900,
             "shared_rotation_file": "logs/shared_new.json"})
        assert cfg["recent_ip_window"] == 60
        assert cfg["recent_ip_max_age"] == 900
        assert cfg["shared_rotation_file"] == "logs/shared_new.json"
        # the plain-valued fields update through the same path
        assert cfg["watchdog_backoff_base"] == 15.0

    @pytest.mark.asyncio
    async def test_malformed_armed_config_skips_itself_not_fatal(self, tmp_path):
        """[review 18/08 hot-reload] a malformed form value
        (float("nope")/int(None)) raised out of update_config → 500 → the
        WHOLE fan-out was aborted (piège 8). One bad key must skip itself
        with a warning; the VALID sibling still lands."""
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        await mgr.update_config({
            "egress_failure_tick_interval": "nope",   # float("nope") raises
            "auto_wg_egress_ticks": None,             # int(None) raises
            "ip_probe_budget": 5,                     # valid sibling
        })
        assert mgr._egress_failure_tick_interval == 2.0  # untouched (default)
        assert mgr._auto_wg_egress_ticks == 3             # untouched (default)
        assert mgr._ip_probe_budget == 5.0                # applied — fan-out alive

# ── [review 18/08 F2] real light probe: multi-endpoint fallback ──

class _FakeHttpxResp:
    """Minimal httpx.Response stand-in: the probe only closes it."""

    async def aclose(self):
        pass


class _FakeHttpxClient:
    """Records the CONNECT attempts; raises on URLs marked dead.

    send() mirrors the real call shape (request, stream=...). The tuple
    request comes from the fake httpx.Request below — (method, url).
    """

    def __init__(self, behavior, record, **kw):
        self._behavior, self._record = behavior, record
        self.closed = False

    async def send(self, request, stream=False):
        url = request[1]
        self._record.append(url)
        if not self._behavior.get(url, True):
            raise RuntimeError(f"connect failed: {url}")
        return _FakeHttpxResp()

    async def aclose(self):
        self.closed = True


class _FakeHttpx:
    """Fake httpx MODULE (stubbed into sys.modules — the probe imports
    httpx inside the function, piège 4: never setattr on vpn_manager)."""

    def __init__(self, behavior, record):
        self._behavior, self._record = behavior, record

    def AsyncClient(self, **kw):
        return _FakeHttpxClient(self._behavior, self._record, **kw)

    def Timeout(self, value):
        return value

    def Request(self, method, url):
        return (method, url)


class TestProbeTunnelLightEndpoints:
    """[review 18/08 F2] the REAL _probe_tunnel_light walks the rotated
    ip_check chain per-attempt: a dead sticky endpoint must NOT false-death
    a healthy tunnel (the old single-endpoint probe escalated a full
    recovery for nothing). Walk order is sticky-first (base index), the
    last live endpoint becomes sticky, and a total failure leaves the
    index untouched."""

    def _mgr(self, tmp_path, urls):
        mgr = FakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)
        # Bind the REAL probe onto the instance (the fake class method is
        # only for tick-level tests; here the probe itself is under test).
        mgr._probe_tunnel_light = vm.VPNManager._probe_tunnel_light.__get__(mgr)
        mgr._ip_check_urls = urls
        mgr._ip_check_idx = 0
        return mgr

    def _patch(self, monkeypatch, behavior):
        record = []
        monkeypatch.setitem(sys.modules, "httpx",
                            _FakeHttpx(behavior, record))
        return record

    @pytest.mark.asyncio
    async def test_dead_first_endpoint_falls_through_to_alive(self, tmp_path, monkeypatch):
        """The STICKY endpoint is dead but the next one answers → probe
        True, the sticky index advances to the live one (subsequent probes
        hit the responsive host first)."""
        mgr = self._mgr(tmp_path, ["http://a", "http://b", "http://c"])
        record = self._patch(monkeypatch, {"http://a": False})

        ok = await mgr._probe_tunnel_light()

        assert ok is True
        assert record == ["http://a", "http://b"]
        assert mgr._ip_check_idx == 1

    @pytest.mark.asyncio
    async def test_sticky_first_when_all_alive(self, tmp_path, monkeypatch):
        """Base index 1, all endpoints alive → exactly ONE attempt (the
        sticky one) — no wasted probes on a healthy chain."""
        mgr = self._mgr(tmp_path, ["http://a", "http://b", "http://c"])
        mgr._ip_check_idx = 1
        record = self._patch(monkeypatch, {})

        ok = await mgr._probe_tunnel_light()

        assert ok is True
        assert record == ["http://b"]
        assert mgr._ip_check_idx == 1

    @pytest.mark.asyncio
    async def test_all_dead_false_index_unchanged(self, tmp_path, monkeypatch):
        """Every endpoint dead → False (the tick ramps egress_dead), the
        full chain is swept, and the index stays where it was."""
        mgr = self._mgr(tmp_path, ["http://a", "http://b", "http://c"])
        record = self._patch(monkeypatch,
                             {"http://a": False, "http://b": False,
                              "http://c": False})

        ok = await mgr._probe_tunnel_light()

        assert ok is False
        assert record == ["http://a", "http://b", "http://c"]
        assert mgr._ip_check_idx == 0
