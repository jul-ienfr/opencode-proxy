"""test_graceful_aurora.py — LOTS A/B/C/D/E/F/H/J (plan 07/09 graceful-aurora).

Offline : aucun docker, aucun réseau. Deux fakes :
  * GraceFake (ici) : VPNManager réel dont SEULES les frontières
    subprocess/réseau sont fakingées — toute la logique LOT A/B/C-scan/E/F
    tourne inchangée (refresh_status, _check_*, _wait_healthy, pins).
  * FakeVPNManager (test_vpn_freshness) : fakes historiques à marqueurs —
    utilisés pour LOT C-gate/D/H (comportement watchdog/superviseur), avec
    les kill-switches LOT D qui pinent l'historique (grâce 0).

Rollback de chaque LOT via sa clé config (cf. config.yaml) — les tests
pinent aussi le comportement historique quand pertinent.
"""

import asyncio
import subprocess

import pytest
from test_vpn_freshness import FakeVPNManager
from test_vpn_freshness import _cfg as _base_cfg

import vpn.manager as cvm
from free_ip_pool import FreeIPPool
from ops.supervisor import StationSupervisor
from vpn.manager import AuthCoolingDownError, VPNManager, VPNState

# ── GraceFake ──────────────────────────────────────────────────────


def _gcfg(tmp_path, **over):
    cfg = {
        "enabled": True,
        "proxy_mode": "vpn",
        "switch_delay": 0,
        "identity_rotation": True,
        "identity_diversity": False,
        "identity_profiles": [
            {"impersonate": "chrome131", "user_agent": None, "extra_headers": {}}
        ],
        "watchdog_backoff_base": 15.0,
        "watchdog_backoff_max": 60.0,
        "auto_wg_egress_ticks": 3,
        "egress_failure_tick_interval": 2.0,
        "ip_probe_budget": 8.0,
        "control_pin_catchup": 0.0,
        "server_countries": "Germany,Netherlands,France,Sweden,Switzerland",
        "least_loaded_enabled": False,
    }
    cfg.update(over)
    return cfg


class GraceFake(cvm.VPNManager):
    """VPNManager réel, frontières fakingées (subprocess + egress)."""

    def __init__(self, cfg, tmp_path, station=1):
        cfg = dict(cfg)
        key = f"state_file_{station}" if station > 1 else "state_file"
        cfg.setdefault(key, str(tmp_path / f"grace_state{station}.json"))
        super().__init__(cfg, station=station, shared=None)
        self._wg_key_file = str(tmp_path / "wireguard.env")
        self.log_text = ""
        self.inspect_running = True
        self.ctl_status = True
        self.ctl_ip = "9.9.9.9"
        self.socks_ok = True
        self.socks_ip = "9.9.9.9"

    def _docker_run(self, args, timeout=30, env=None):
        script = str(args[-1]) if args else ""
        if "/v1/vpn/settings" in script:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout=self.log_text, stderr="")

    async def _docker_inspect(self):
        if not self.inspect_running:
            return {"running": False, "started_at": "", "mounts": []}
        return {
            "running": True,
            "started_at": "2026-01-01T00:00:00Z",
            "mounts": [],
        }

    async def _control_status(self, retries=2):
        if not self._control_enabled:
            return None
        return self.ctl_status

    async def _control_public_ip(self):
        if not self._control_enabled:
            return None
        return self.ctl_ip

    async def get_public_ip(self):
        return self.socks_ip if self.socks_ok else None

    async def _socks_egress_ok(self):
        return bool(self.socks_ok)


def _cancel(task):
    try:
        if task is not None and not task.done():
            task.cancel()
    except Exception:
        pass


# ── LOT A : server_issue grace ─────────────────────────────────────


class TestServerIssueGrace:
    async def test_reset_then_init_is_healed_connected(self, tmp_path):
        """reset suivi d'INIT +11 s → pas error (guérison prouvée)."""
        mgr = GraceFake(_gcfg(tmp_path), tmp_path)
        mgr.log_text = (
            "2026-09-07T21:00:00Z openvpn: Connection reset, restarting\n"
            "2026-09-07T21:00:11Z openvpn: Initialization Sequence Completed\n"
        )
        assert await mgr._check_server_issue("") is False
        st = await mgr.refresh_status(force=True)
        assert st["status"] == VPNState.CONNECTED
        assert st["server_issue_pending"] is False
        _cancel(mgr._server_issue_recheck_task)

    async def test_fresh_reset_is_degraded_not_error(self, tmp_path):
        """reset frais sans INIT → degraded routable, IP gardée, pending."""
        mgr = GraceFake(_gcfg(tmp_path), tmp_path)
        mgr._current_ip = "9.9.9.9"  # IP servie avant la micro-coupure
        mgr.log_text = "openvpn: Connection reset, restarting\n"
        assert await mgr._check_server_issue("") is True
        st = await mgr.refresh_status(force=True)
        assert st["status"] == VPNState.DEGRADED
        assert st["server_issue_pending"] is True
        assert st["ip"] == "9.9.9.9"  # jamais purgée sur la voie degraded
        assert mgr._server_issue_recheck_task is not None
        _cancel(mgr._server_issue_recheck_task)

    async def test_old_reset_is_error(self, tmp_path):
        """reset sans INIT vieux de > grace + egress KO → error historique.

        [audit 2026-09-09] Avec egress OK, c'est désormais la voie
        pin-loop (degraded routable) — cf. TestPinLoopEgressOk.
        """
        mgr = GraceFake(_gcfg(tmp_path), tmp_path)
        mgr.socks_ok = False  # egress réellement morte → vrai error
        mgr.ctl_status = False  # control arrêté aussi (pas de fallthrough)
        mgr.log_text = (
            "2026-01-01T00:00:00Z openvpn: Connection reset, restarting\n"
        )
        st = await mgr.refresh_status(force=True)
        assert st["status"] == VPNState.ERROR
        assert st["ip"] is None
        _cancel(mgr._server_issue_recheck_task)

    async def test_double_reset_single_recheck_task(self, tmp_path):
        """double reset → une seule recheck-task (single-flight)."""
        mgr = GraceFake(_gcfg(tmp_path), tmp_path)
        mgr.log_text = "openvpn: Connection reset, restarting\n"
        await mgr.refresh_status(force=True)
        t1 = mgr._server_issue_recheck_task
        assert t1 is not None and not t1.done()
        await mgr.refresh_status(force=True)
        t2 = mgr._server_issue_recheck_task
        assert t2 is not None and not t2.done()
        assert t2 is not t1
        await asyncio.sleep(0)  # laisse la cancel() se matérialiser
        assert t1.done()  # annulée par la seconde
        _cancel(t2)

    async def test_healed_after_init_returns_connected(self, tmp_path):
        """degraded puis INIT → connected + pending effacé."""
        mgr = GraceFake(_gcfg(tmp_path), tmp_path)
        mgr.log_text = "openvpn: Connection reset, restarting\n"
        st = await mgr.refresh_status(force=True)
        assert st["status"] == VPNState.DEGRADED
        _cancel(mgr._server_issue_recheck_task)
        mgr.log_text += "openvpn: Initialization Sequence Completed\n"
        st = await mgr.refresh_status(force=True)
        assert st["status"] == VPNState.CONNECTED
        assert st["server_issue_pending"] is False
        _cancel(mgr._server_issue_recheck_task)

    async def test_recheck_disabled_is_historic_error(self, tmp_path):
        """Rollback : server_issue_recheck=false + egress KO → error."""
        mgr = GraceFake(_gcfg(tmp_path, server_issue_recheck=False), tmp_path)
        mgr.socks_ok = False  # egress réellement morte → vrai error
        mgr.ctl_status = False  # control arrêté aussi (pas de fallthrough)
        mgr.log_text = "openvpn: Connection reset, restarting\n"
        st = await mgr.refresh_status(force=True)
        assert st["status"] == VPNState.ERROR


# ── LOT B : _wait_healthy tolérant ─────────────────────────────────


class TestWaitHealthyTolerant:
    async def test_tunnel_up_socks_down_succeeds_with_flag(self, tmp_path):
        """tunnel UP + SOCKS KO → succès + egress_socks_pending + catch-up."""
        mgr = GraceFake(
            _gcfg(tmp_path, control_api_key="k", country_rotation=False), tmp_path
        )
        mgr.socks_ok = False
        started = await mgr._wait_healthy(timeout=5)
        assert started == "2026-01-01T00:00:00Z"
        assert mgr._egress_socks_pending is True
        assert mgr._socks_catchup_task is not None
        _cancel(mgr._socks_catchup_task)

    async def test_tunnel_up_socks_up_clears_flag(self, tmp_path):
        mgr = GraceFake(
            _gcfg(tmp_path, control_api_key="k", country_rotation=False), tmp_path
        )
        started = await mgr._wait_healthy(timeout=5)
        assert started == "2026-01-01T00:00:00Z"
        assert mgr._egress_socks_pending is False
        _cancel(mgr._socks_catchup_task)

    async def test_late_tunnel_up_recovers_no_runtime_error(self, tmp_path, caplog):
        """tunnel qui répond après le timeout → late_healthy_recovered."""
        mgr = GraceFake(
            _gcfg(
                tmp_path,
                control_api_key="k",
                country_rotation=False,
                wait_healthy_late_retry_s=5,
            ),
            tmp_path,
        )
        mgr.socks_ok = False
        calls = {"n": 0}

        async def _flip(retries=2):
            calls["n"] += 1
            return True if calls["n"] >= 3 else False

        mgr._control_status = _flip  # type: ignore[method-assign]
        import logging

        with caplog.at_level(logging.WARNING, logger="vpn_manager"):
            started = await mgr._wait_healthy(timeout=0.05)
        assert started == "2026-01-01T00:00:00Z"
        assert mgr._last_rotation_error is None
        assert "late_healthy_recovered" in caplog.text
        _cancel(mgr._socks_catchup_task)

    async def test_tunnel_down_returns_none(self, tmp_path):
        """tunnel DOWN passé le late retry → None (RuntimeError conservé)."""
        mgr = GraceFake(
            _gcfg(
                tmp_path,
                control_api_key="k",
                country_rotation=False,
                wait_healthy_late_retry_s=0.1,
            ),
            tmp_path,
        )
        mgr.ctl_status = False
        mgr.socks_ok = False
        assert await mgr._wait_healthy(timeout=0.1) is None
        _cancel(mgr._socks_catchup_task)

    async def test_require_socks_rollback(self, tmp_path):
        """Rollback : wait_healthy_require_socks=true → SOCKS exigé."""
        mgr = GraceFake(
            _gcfg(
                tmp_path,
                control_api_key="k",
                country_rotation=False,
                wait_healthy_require_socks=True,
                wait_healthy_late_retry_s=0,
            ),
            tmp_path,
        )
        mgr.socks_ok = False
        assert await mgr._wait_healthy(timeout=0.5) is None


# ── LOT C : cooldown AUTH local ────────────────────────────────────

_AUTH_TXT = (
    "[de1234.nordvpn.com] Peer Connection Initiated with [AF_INET]1.2.3.4:1194\n"
    "AUTH: Received control message: AUTH_FAILED, restarting\n"
)


class TestOvAuthCooldown:
    async def test_burst_arms_cooling_and_gates_connect(self, tmp_path):
        """3 AUTH / fenêtre (seuil 3) → cooling + connect_next refusé."""
        mgr = GraceFake(_gcfg(tmp_path, ov_auth_station_threshold=3), tmp_path)
        for _ in range(3):
            assert await mgr._check_auth_failed("", text=_AUTH_TXT) is True
        assert mgr._auth_cooling() is True
        assert mgr._auth_cool_remaining_s() > 0
        with pytest.raises(AuthCoolingDownError):
            await mgr.connect_next()
        # AuthCoolingDownError ⊂ RotationFailed (compat handlers).
        assert issubclass(AuthCoolingDownError, cvm.RotationFailed)

    async def test_three_consec_same_host_blacklists_and_flags_repin(self, tmp_path):
        mgr = GraceFake(_gcfg(tmp_path, ov_auth_station_threshold=999), tmp_path)
        for _ in range(3):
            await mgr._check_auth_failed("", text=_AUTH_TXT)
        assert mgr._host_blacklisted("de1234.nordvpn.com") is True
        assert mgr._auth_repin_needed is True

    async def test_burst_sets_backoff_and_gate_respects_global_throttle(
        self, tmp_path, monkeypatch
    ):
        """Backoff piloté posé en rafale ; connect() passe TOUJOURS par
        le throttle global (jamais de contournement)."""
        mgr = GraceFake(_gcfg(tmp_path, ov_auth_station_threshold=999), tmp_path)
        for _ in range(3):
            await mgr._check_auth_failed("", text=_AUTH_TXT)
        assert mgr._auth_backoff_delay >= 90.0
        calls = {"n": 0}

        async def _spy(count_inflight=True):
            calls["n"] += 1

        monkeypatch.setattr(cvm, "_auth_gate", _spy)
        await mgr.connect()
        assert calls["n"] >= 1  # gate consulté (compose + legacy branch)
        assert mgr._status == VPNState.CONNECTED

    async def test_cooling_threshold_999_disables(self, tmp_path):
        """Rollback : ov_auth_station_threshold=999 → jamais de cooling."""
        mgr = GraceFake(_gcfg(tmp_path, ov_auth_station_threshold=999), tmp_path)
        for _ in range(10):
            await mgr._check_auth_failed("", text=_AUTH_TXT)
        assert mgr._auth_cooling() is False


# ── LOT D : watchdog en grâce ──────────────────────────────────────


def _d_cfg(tmp_path, **over):
    over.setdefault("watchdog_auth_grace_s", 240)
    over.setdefault("watchdog_egress_grace_ticks", 3)
    over.setdefault("watchdog_max_restarts_per_hour", 3)
    return _base_cfg(tmp_path, **over)


class TestWatchdogGrace:
    async def test_first_auth_no_restart_grace_set(self, tmp_path):
        mgr = FakeVPNManager(_d_cfg(tmp_path), tmp_path=tmp_path)
        mgr.log_text = "AUTH_FAILED - credentials rejected"
        mgr.ips = ["5.5.5.5"]
        await mgr._watchdog_tick()
        assert mgr.calls["restart"] == 0
        assert mgr._auth_grace_until > mgr._now_fn()
        assert mgr._watchdog_last_action == "grace-first"

    async def test_healed_auth_no_restart(self, tmp_path):
        """Log sans AUTH (INIT/guéri) → tick sain, grâce purgée, 0 restart."""
        mgr = FakeVPNManager(_d_cfg(tmp_path), tmp_path=tmp_path)
        mgr._auth_grace_until = mgr._now_fn() + 240.0
        mgr.log_text = "openvpn: Initialization Sequence Completed"
        mgr.ips = ["1.2.3.4"]
        await mgr._watchdog_tick()
        assert mgr.calls["restart"] == 0
        assert mgr._auth_grace_until == 0.0

    async def test_persistent_auth_after_grace_restarts_once(self, tmp_path):
        mgr = FakeVPNManager(_d_cfg(tmp_path), tmp_path=tmp_path)
        mgr._auth_grace_until = mgr._now_fn() - 1.0  # grâce expirée
        mgr.log_text = "AUTH_FAILED"
        mgr.ips = ["5.5.5.5", "5.5.5.5", "5.5.5.5"]
        await mgr._watchdog_tick()
        assert mgr.calls["restart"] >= 1

    async def test_budget_exhausted_refuses_and_escalates(self, tmp_path):
        mgr = FakeVPNManager(_d_cfg(tmp_path), tmp_path=tmp_path)
        now = mgr._now_fn()
        mgr._watchdog_restarts_1h = [now - 10.0, now - 20.0, now - 30.0]
        mgr._auth_grace_until = now - 1.0
        mgr.log_text = "AUTH_FAILED"
        await mgr._watchdog_tick()
        assert mgr.calls["restart"] == 0
        assert mgr._watchdog_last_action == "budget-exhausted"
        assert mgr._auth_cooling() is True

    async def test_grace_zero_is_historic_immediate_restart(self, tmp_path):
        """Rollback : watchdog_auth_grace_s=0 → restart immédiat."""
        mgr = FakeVPNManager(_base_cfg(tmp_path), tmp_path=tmp_path)
        mgr.log_text = "AUTH_FAILED - credentials rejected"
        mgr.ips = ["4.4.4.4", "5.5.5.5", "5.5.5.5", "5.5.5.5"]
        await mgr._watchdog_tick()
        assert mgr.calls["restart"] >= 1


# ── LOT F : egress distingué ───────────────────────────────────────


class TestEgressStates:
    async def test_tunnel_up_full_is_connected(self, tmp_path):
        mgr = GraceFake(
            _gcfg(tmp_path, control_api_key="k", country_rotation=False), tmp_path
        )
        st = await mgr.refresh_status(force=True)
        assert st["status"] == VPNState.CONNECTED
        assert st["egress_state"] == "tunnel_up_full"

    async def test_tunnel_up_socks_down_is_degraded_routable(self, tmp_path):
        """Cas S6 : control running + IP, SOCKS KO → degraded, IP gardée."""
        mgr = GraceFake(
            _gcfg(tmp_path, control_api_key="k", country_rotation=False), tmp_path
        )
        mgr.socks_ok = False
        mgr._egress_socks_pending = True  # doute LOT B → classification LOT F
        st = await mgr.refresh_status(force=True)
        assert st["status"] == VPNState.DEGRADED
        assert st["egress_state"] == "tunnel_up_socks_down"
        assert st["ip"] == "9.9.9.9"
        assert st["degraded_reason"] == "socks-down"
        _cancel(mgr._server_issue_recheck_task)

    async def test_tunnel_down_is_error(self, tmp_path):
        mgr = GraceFake(
            _gcfg(tmp_path, control_api_key="k", country_rotation=False), tmp_path
        )
        mgr.ctl_status = False
        st = await mgr.refresh_status(force=True)
        assert st["status"] == VPNState.ERROR
        assert st["egress_state"] == "tunnel_down"

    async def test_pool_prefers_full_over_socks_down(self, tmp_path):
        """Le pool sert full d'abord, socks_down en second choix."""
        m1 = GraceFake(_gcfg(tmp_path), tmp_path, station=1)
        m2 = GraceFake(_gcfg(tmp_path), tmp_path, station=2)
        pool = FreeIPPool(m1, m2)
        m1._status = VPNState.DEGRADED
        m1._current_ip = "1.1.1.1"
        m1._egress_state = "tunnel_up_socks_down"
        m2._status = VPNState.CONNECTED
        m2._current_ip = "2.2.2.2"
        m2._egress_state = "tunnel_up_full"
        assert pool._station_usable(m1, exclude_approaching=False) is True
        best = pool._best_station()
        assert best is m2
        # socks_down seul → quand même servi (jamais zéro candidat).
        m2._status = VPNState.ERROR
        assert pool._best_station() is m1

    async def test_socks_down_routable_false_is_historic(self, tmp_path):
        """Rollback : socks_down_routable=false → CONNECTED direct."""
        mgr = GraceFake(
            _gcfg(tmp_path, control_api_key="k", socks_down_routable=False),
            tmp_path,
        )
        mgr.socks_ok = False
        mgr._egress_socks_pending = True
        st = await mgr.refresh_status(force=True)
        assert st["status"] == VPNState.CONNECTED


# ── LOT E : pin réversible ─────────────────────────────────────────


class TestPinWiden:
    def _pinned(self, tmp_path, **over):
        over.setdefault("control_api_key", "k")
        over.setdefault("country_rotation", True)
        mgr = GraceFake(_gcfg(tmp_path, **over), tmp_path)
        mgr._current_country = "Netherlands"
        VPNManager._pin_widen_in_flight = {}
        return mgr

    async def test_below_thresholds_never_widens(self, tmp_path):
        mgr = self._pinned(tmp_path)
        await mgr._maybe_pin_widen()
        assert mgr._current_country == "Netherlands"
        assert mgr._pin_widened_until == 0.0

    async def test_unstable_pinned_widens_then_repins(self, tmp_path):
        mgr = self._pinned(tmp_path)
        now = mgr._now_fn()
        mgr._pin_resets_1h = [now - 60.0 * i for i in range(7)]
        await mgr._maybe_pin_widen()
        assert mgr._current_country is None  # élargi : liste complète
        assert mgr._pin_widened_until > mgr._now_fn()
        assert mgr._pin_widen_original == "Netherlands"
        # Fin d'élargissement, compteurs retombés → re-pin origine.
        mgr.log_text = ""
        mgr._pin_resets_1h = []
        mgr._pin_auths_1h = []
        mgr._pin_widened_until = mgr._now_fn() - 1.0
        await mgr._maybe_pin_restore()
        assert mgr._current_country == "Netherlands"
        assert mgr._pin_widened_until == 0.0

    async def test_widen_extends_once_then_escalates(self, tmp_path):
        mgr = self._pinned(tmp_path)
        now = mgr._now_fn()
        mgr._pin_resets_1h = [now - 60.0 * i for i in range(7)]
        mgr._pin_widen_original = "Netherlands"
        mgr._pin_widened_until = now - 1.0
        mgr._pin_widen_extended = False
        await mgr._maybe_pin_restore()
        assert mgr._pin_widen_extended is True  # prolongé une fois
        assert mgr._pin_widened_until > now
        mgr._pin_widened_until = mgr._now_fn() - 1.0
        await mgr._maybe_pin_restore()
        assert mgr._auth_cooling() is True  # escalade auth_cooling

    async def test_concurrent_widen_staggers(self, tmp_path):
        mgr = self._pinned(tmp_path)
        now = mgr._now_fn()
        mgr._pin_resets_1h = [now - 60.0 * i for i in range(7)]
        VPNManager._pin_widen_in_flight = {99: now}
        try:
            await mgr._maybe_pin_widen()
            assert mgr._current_country == "Netherlands"  # reporté
            assert mgr._pin_widened_until == 0.0
        finally:
            VPNManager._pin_widen_in_flight = {}


_TLS_NO_INIT_TXT = (
    "2026-09-09T07:00:00Z [de1234.nordvpn.com] TLS Error: TLS key negotiation failed to occur within 60 seconds\n"
    "2026-09-09T07:00:05Z openvpn: Connection reset, restarting\n"
)


# ── LOT K : audit 2026-09-09 — resets voie secondaire, egress OK ──────
#
# Cas prod s4 : stack effectif WG avec egress OK, mais 100+ resets/h de la
# voie secondaire OV (cascade/pin) dans les logs → refresh_status collait
# error + purgeait l'IP (faux status=error). P0-1 : egress OK = DEGRADED
# routable "pin-loop", IP gardée. P0-2 : la voie error court-circuitait
# _maybe_pin_widen() (seuils jamais actionnés) → widen appelé sur la voie
# degraded pour casser la boucle. P0-3 : message TLS qualifié selon le
# stack effectif (voie secondaire OV vs tunnel effectif down).


class TestPinLoopEgressOk:
    def _pin_loop_mgr(self, tmp_path, **over):
        over.setdefault("control_api_key", "k")
        over.setdefault("country_rotation", True)
        mgr = GraceFake(_gcfg(tmp_path, **over), tmp_path, station=4)
        mgr._current_country = "Switzerland"
        mgr._stack_effective = "wireguard"
        mgr._current_ip = "9.9.9.9"  # IP WG servie avant les resets OV
        mgr.log_text = _TLS_NO_INIT_TXT
        VPNManager._pin_widen_in_flight = {}
        return mgr

    async def test_pin_loop_egress_ok_is_degraded_not_error(self, tmp_path):
        """P0-1 : resets vieux (> grace) + egress OK → degraded pin-loop."""
        mgr = self._pin_loop_mgr(tmp_path)
        st = await mgr.refresh_status(force=True)
        assert st["status"] == VPNState.DEGRADED
        assert st["degraded_reason"] == "pin-loop"
        assert st["egress_state"] == "tunnel_up_full"
        assert st["ip"] == "9.9.9.9"  # jamais purgée sur la voie degraded
        _cancel(mgr._server_issue_recheck_task)

    async def test_pin_loop_triggers_widen(self, tmp_path):
        """P0-2 : pin-loop + compteurs hauts → widen déclenché (boucle)."""
        mgr = self._pin_loop_mgr(tmp_path)
        now = mgr._now_fn()
        mgr._pin_resets_1h = [now - 60.0 * i for i in range(7)]
        st = await mgr.refresh_status(force=True)
        assert st["status"] == VPNState.DEGRADED
        assert mgr._current_country is None  # élargi : liste complète
        assert mgr._pin_widened_until > mgr._now_fn()
        assert mgr._pin_widen_original == "Switzerland"
        _cancel(mgr._server_issue_recheck_task)

    async def test_pin_loop_egress_ko_is_qualified_error(self, tmp_path):
        """P0-3 : egress KO → error, message qualifié voie secondaire."""
        mgr = self._pin_loop_mgr(tmp_path)
        mgr.socks_ok = False  # egress réellement morte
        mgr.ctl_status = False  # control arrêté aussi (pas de fallthrough)
        st = await mgr.refresh_status(force=True)
        assert st["status"] == VPNState.ERROR
        assert st["ip"] is None
        # Le stack effectif WG est nommé : ce n'est pas le tunnel WG
        # qui a échoué la négociation TLS, mais la voie OV.
        assert "wireguard" in (st["error"] or "")
        _cancel(mgr._server_issue_recheck_task)

    async def test_auth_failed_keeps_strict_error(self, tmp_path):
        """AUTH live + egress OK → error strict (P0-1 ne s'applique pas)."""
        mgr = self._pin_loop_mgr(tmp_path)
        mgr.log_text = _AUTH_TXT  # AUTH live, pas de INIT postérieur
        st = await mgr.refresh_status(force=True)
        assert st["status"] == VPNState.ERROR
        assert st["ip"] is None
        assert "AUTH_FAILED" in (st["error"] or "")
        _cancel(mgr._server_issue_recheck_task)

    async def test_old_reset_ov_stack_stays_plain_error(self, tmp_path):
        """Pas de régression LOT A : stack OV + egress KO → error simple."""
        mgr = GraceFake(
            _gcfg(tmp_path, control_api_key="k", country_rotation=False), tmp_path
        )
        mgr._stack_effective = "openvpn"
        mgr.socks_ok = False
        mgr.ctl_status = False
        mgr.log_text = _TLS_NO_INIT_TXT
        st = await mgr.refresh_status(force=True)
        assert st["status"] == VPNState.ERROR
        assert "voie secondaire" not in (st["error"] or "")
        _cancel(mgr._server_issue_recheck_task)


# ── LOT H : lock restart par station ───────────────────────────────


class _SlowMgr:
    def __init__(self):
        self.n = 0

    async def restart(self):
        self.n += 1
        await asyncio.sleep(0.05)

    def get_status(self):
        return {}


class TestSupervisorRestartLock:
    async def test_concurrent_restarts_same_station_single_effect(self):
        sup = StationSupervisor(station=1, manager=_SlowMgr())
        await asyncio.gather(*[sup.restart(reason="race") for _ in range(10)])
        assert sup.manager.n == 1

    async def test_parallel_restarts_two_stations_both_effective(self):
        s1 = StationSupervisor(station=1, manager=_SlowMgr())
        s2 = StationSupervisor(station=2, manager=_SlowMgr())
        await asyncio.gather(
            *[s1.restart(reason="a") for _ in range(5)],
            *[s2.restart(reason="b") for _ in range(5)],
        )
        assert s1.manager.n == 1
        assert s2.manager.n == 1

    async def test_manager_restart_serializes_direct_calls(self, tmp_path):
        """Garde réentrance côté VPNManager (dashboard/watchdog directs)."""
        mgr = GraceFake(_gcfg(tmp_path), tmp_path)
        calls = {"n": 0}
        real = mgr._docker_restart

        async def _slow():
            calls["n"] += 1
            await asyncio.sleep(0.05)

        mgr._docker_restart = _slow  # type: ignore[method-assign]
        await asyncio.gather(*[mgr.restart() for _ in range(6)])
        assert calls["n"] == 1
        mgr._docker_restart = real  # type: ignore[method-assign]


# ── LOT J : contrat observabilité ──────────────────────────────────


class TestStatusContract:
    async def test_manager_status_has_graceful_keys(self, tmp_path):
        mgr = GraceFake(_gcfg(tmp_path, control_api_key="k"), tmp_path)
        st = mgr.get_status()
        for key in (
            "server_issue_pending",
            "last_reset_age_s",
            "last_init_ok_at",
            "egress_state",
            "degraded_reason",
            "egress_socks_pending",
            "auth_fail_30min",
            "auth_cool_remaining_s",
            "pinned_country",
            "pin_widened_until",
            "pin_resets_1h",
            "pin_auths_1h",
        ):
            assert key in st, f"clé manquante: {key}"
        wd = st["watchdog"]
        for key in (
            "watchdog_last_action",
            "watchdog_grace_remaining_s",
            "watchdog_restarts_1h",
        ):
            assert key in wd, f"clé watchdog manquante: {key}"
        assert isinstance(st["auth_fail_30min"], int)
        assert isinstance(st["auth_cool_remaining_s"], int)

    async def test_pool_status_has_degraded_routable(self, tmp_path):
        m1 = GraceFake(_gcfg(tmp_path), tmp_path, station=1)
        m2 = GraceFake(_gcfg(tmp_path), tmp_path, station=2)
        pool = FreeIPPool(m1, m2)
        m1._status = VPNState.DEGRADED
        m1._current_ip = "1.1.1.1"
        data = pool.get_status()
        assert isinstance(data["degraded_routable"], int)
        assert data["degraded_routable"] == 1
        assert data["healthy"] == 1
        assert data["healthy_routable"] == 1
        assert data["stations"][0]["egress_state"] in (
            "unknown",
            "tunnel_up_full",
            "tunnel_up_socks_down",
            "tunnel_down",
        )

    def test_metrics_render_has_graceful_families(self):
        from observability.metrics import render_vpn_section

        snap = {
            "stations": [('station="1"', 1)],
            "degraded": [('station="1",reason="socks-down"', 0)],
            "cooldowns": [('station="1",kind="auth_cooling"', 0)],
            "restarts": [('station="1"', 2)],
            "has_engine": False,
        }
        text = "\n".join(render_vpn_section(snap))
        assert "vpn_station_degraded" in text
        assert "vpn_station_cooldown_active" in text
        assert 'watchdog_restart_total{station="1"} 2' in text

    def test_utc_formatter_is_gmtime(self):
        import time as _time

        from dashboard.display import _utc_formatter

        fmt = _utc_formatter("[%(asctime)sZ] x", datefmt="%Y-%m-%d %H:%M:%S")
        assert fmt.converter == _time.gmtime


# ── Audit 2026-09-09 : fast-path boot (P0 cause racine restart) ──────
#
# Cause racine : _status n'est jamais persisté (DISCONNECTED à l'init) donc
# start() lançait _startup_connect() à chaque restart du proxy — re-pin PUT
# (= AUTH côté NordVPN) + rotation forcée sur les 6 stations alors que leurs
# conteneurs/tunnels avaient survécu → rafale de 6 AUTH → throttle compte.
# Le fast-path : si refresh_status() a adopté un conteneur sain (CONNECTED ou
# DEGRADED routable + IP), start() ne touche à rien.
#
# Les 3 verrous prouvent qu'aucun AUTH n'est émis au boot sain (aucune tâche
# de fond + aucun PUT de pin + aucun tour finalize), et le 4e que l'IP
# adoptée est journalisée (registre anti-réutilisation + historique) SANS
# avancer l'identité (on constate le tunnel, on ne le fait pas tourner).


async def _boot_healthy_mgr(tmp_path):
    """GraceFake sain : conteneur up, control running, IP control 9.9.9.9.

    control_api_key explicite : sans lui _control_enabled=False et refresh
    prendrait le fallback SOCKS (sans journalisation) au lieu de la voie
    control-first + _adopt_boot_ip que le fast-path doit exercer.
    """
    mgr = GraceFake(_gcfg(tmp_path, control_api_key="k"), tmp_path)
    assert mgr._control_enabled is True
    await mgr.start()
    return mgr


def _cancel_boot_tasks(mgr):
    _cancel(mgr._startup_connect_task)
    _cancel(mgr._watchdog_task)
    _cancel(mgr._update_task)
    _cancel(mgr._server_issue_recheck_task)


class TestFastPathBoot:
    async def test_boot_healthy_spawns_no_startup_connect(self, tmp_path):
        """Conteneur sain au boot → pas de _startup_connect_task (0 AUTH)."""
        mgr = await _boot_healthy_mgr(tmp_path)
        try:
            assert mgr._startup_connect_task is None
            assert mgr._status in (VPNState.CONNECTED, VPNState.DEGRADED)
            assert mgr._current_ip == "9.9.9.9"
        finally:
            _cancel_boot_tasks(mgr)

    async def test_boot_healthy_emits_no_pin_put(self, tmp_path, monkeypatch):
        """Boot sain → aucun PUT /v1/vpn/settings (le PUT = reconnect AUTH)."""
        puts = []
        orig = cvm.VPNManager._docker_run

        def _spy(self, args, timeout=30, env=None):
            if "/v1/vpn/settings" in str(args[-1] if args else ""):
                puts.append(args)
            return orig(self, args, timeout=timeout, env=env)

        monkeypatch.setattr(cvm.VPNManager, "_docker_run", _spy)
        mgr = await _boot_healthy_mgr(tmp_path)
        try:
            await asyncio.sleep(0)
            assert puts == [], f"PUT pin émis au boot sain: {puts}"
        finally:
            _cancel_boot_tasks(mgr)

    async def test_boot_healthy_runs_no_finalize(self, tmp_path, monkeypatch):
        """Boot sain → _finalize_ip jamais appelé (pas de rotation forcée)."""
        calls = {"n": 0}
        orig = cvm.VPNManager._finalize_ip

        async def _spy(self, allow_stale=False):
            calls["n"] += 1
            return await orig(self, allow_stale=allow_stale)

        monkeypatch.setattr(cvm.VPNManager, "_finalize_ip", _spy)
        mgr = await _boot_healthy_mgr(tmp_path)
        try:
            await asyncio.sleep(0)
            assert calls["n"] == 0
        finally:
            _cancel_boot_tasks(mgr)

    async def test_boot_healthy_down_still_connects(self, tmp_path):
        """Conteneur absent au boot → _startup_connect_task TOUJOURS lancée."""
        mgr = GraceFake(_gcfg(tmp_path), tmp_path)
        mgr.inspect_running = False
        mgr.ctl_status = False
        mgr.ctl_ip = None
        mgr.socks_ok = False
        mgr.socks_ip = None
        await mgr.start()
        try:
            assert mgr._startup_connect_task is not None
        finally:
            _cancel(mgr._startup_connect_task)
            _cancel(mgr._watchdog_task)
            _cancel(mgr._update_task)
            _cancel(mgr._server_issue_recheck_task)

    async def test_boot_adopted_ip_is_journaled_without_identity_advance(
        self, tmp_path
    ):
        """L'IP du tunnel existant est enregistrée (registre + historique)
        SANS avancer l'identité — pas de rotation au boot."""
        mgr = GraceFake(_gcfg(tmp_path, control_api_key="k"), tmp_path)
        assert mgr._control_enabled is True
        assert mgr._current_ip is None
        idx_before = mgr._identity_index
        st = await mgr.refresh_status(force=True)
        assert st["status"] in (VPNState.CONNECTED, VPNState.DEGRADED)
        assert mgr._current_ip == "9.9.9.9"
        assert mgr._identity_index == idx_before  # pas de rotation au boot
        assert mgr._ip_history, "IP adoptée non historisée"
        entry = mgr._ip_history[-1]
        assert entry["ip"] == "9.9.9.9"
        assert entry["identity_index"] == idx_before  # visage LIVE, pas NEW
        _cancel(mgr._server_issue_recheck_task)


# ── Phase 1 : interrupteur choix des serveurs ───────────────────────


def _pick_mgr(tmp_path, **over):
    from test_gluetun_control_api import ControlFakeVPNManager, _fast_cfg

    over.setdefault("server_countries", "Germany,France,Spain,Poland")
    mgr = ControlFakeVPNManager(_fast_cfg(tmp_path, **over), tmp_path=tmp_path)
    mgr.stdout_by_fragment = {
        "/v1/vpn/settings": "",
        "/v1/vpn/status": '{"status":"running"}',
    }
    mgr.log_text = ""
    return mgr


class TestServerPickMode:
    async def test_proxy_mode_pins_hostnames(self, tmp_path, monkeypatch):
        """Mode proxy (nous, défaut) : le PUT impose pays + hostnames."""
        mgr = _pick_mgr(tmp_path)
        assert mgr._server_pick_mode == "proxy"
        fetched = {"n": 0}

        async def _fake_fetch():
            fetched["n"] += 1
            return {}

        monkeypatch.setattr(mgr, "_fetch_nord_loads", _fake_fetch)
        monkeypatch.setattr(
            mgr, "_least_loaded_hostnames", lambda country: ["de9999.nordvpn.com"]
        )
        assert await mgr._control_pin_country("Poland", timeout=2) is True
        script = mgr.settings_script()
        assert '"countries":["Poland"]' in script
        assert "de9999.nordvpn.com" in script
        assert fetched["n"] >= 1

    async def test_gluetun_mode_pins_country_only(self, tmp_path, monkeypatch):
        """Mode gluetun (délégué) : pays seul, pas de fetch loads."""
        mgr = _pick_mgr(tmp_path, server_pick_mode="gluetun")
        assert mgr._server_pick_mode == "gluetun"

        async def _boom_fetch():
            raise AssertionError("pas de fetch loads en mode délégué")

        monkeypatch.setattr(mgr, "_fetch_nord_loads", _boom_fetch)
        monkeypatch.setattr(
            mgr, "_least_loaded_hostnames", lambda country: ["de9999.nordvpn.com"]
        )
        assert await mgr._control_pin_country("Poland", timeout=2) is True
        script = mgr.settings_script()
        assert '"countries":["Poland"]' in script
        assert "hostnames" not in script
        assert "de9999" not in script

    async def test_invalid_mode_falls_back_to_proxy(self, tmp_path):
        mgr = _pick_mgr(tmp_path, server_pick_mode="n'importe quoi")
        assert mgr._server_pick_mode == "proxy"

    async def test_hot_reload_switches_mode(self, tmp_path):
        mgr = _pick_mgr(tmp_path)
        await mgr.update_config({"server_pick_mode": "gluetun"})
        assert mgr._server_pick_mode == "gluetun"
        await mgr.update_config({"server_pick_mode": "bogus"})
        assert mgr._server_pick_mode == "gluetun"  # invalide → inchangé

    async def test_status_and_config_expose_mode(self, tmp_path):
        mgr = _pick_mgr(tmp_path, server_pick_mode="gluetun")
        assert mgr.get_status()["server_pick_mode"] == "gluetun"
        # get_config lit le miroir persisté (config.yaml édité : proxy).
        assert mgr.get_config()["server_pick_mode"] == "proxy"
