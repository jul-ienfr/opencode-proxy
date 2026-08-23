"""test_restart_churn.py — [plan 20/08 §3] gluetun healthcheck-restart churn.

Un tunnel WG marginal fait boucler le healthcheck INTERNE de gluetun
(HEALTH_RESTART_VPN=on) : restart du VPN toutes les ~12 s — marqueur
`restarting VPN because it failed to pass the healthcheck` dans les logs du
conteneur — pendant que la sonde SOCKS5 échantillonne les fenêtres vivantes
et ne voit rien. ``_check_restart_churn`` compte les marqueurs frais sur une
fenêtre glissante ; au-dessus du seuil, le refresh arme le watchdog egress et
la chaîne de recovery existante joue (fresh IP/country → restart → flip).

Trois volets :

  * **Scan réel** (``_ChurnScanMgr``, le VRAI ``_check_restart_churn`` tourne
    contre des logs canulars) : comptage seuil, rc docker, fenêtre ``--since
    {window}m`` sans borne, fenêtre bornée ``--since {N}s`` APRÈS une
    recovery (anti stale-marker), re-arm par marqueurs post-recovery.
  * **Wiring refresh_status** : churn → flag + arm (jamais ERROR — le tunnel
    peut répondre à l'instant), logs propres → aucun flag.
  * **Tick watchdog** : churn avec probe VIVANTE (le cas qui échappe à la
    sonde) → la recovery joue immédiatement (kind « healthcheck restart
    loop »), CONNECTED sur IP fraîche, flag déposé par la borne recovery,
    tick suivant sain sans second restart.
"""

import logging
import subprocess
import time

import pytest

import vpn_manager as vm
from test_vpn_freshness import FakeVPNManager, _cfg

MARKER = "restarting VPN because it failed to pass the healthcheck: vpn tunnel down"


class _ChurnScanMgr(FakeVPNManager):
    """FakeVPNManager with the REAL _check_restart_churn: `_docker_run`
    answers the `logs --since` scan with canned container logs (and records
    the argv), everything else faked as usual (the base fake's marker stub
    is un-shadowed back to the real implementation)."""

    _check_restart_churn = vm.VPNManager._check_restart_churn

    def __init__(self, cfg, **kw):
        super().__init__(cfg, **kw)
        self.docker_log_args = []
        self.log_stdout = ""
        self.log_rc = 0

    def _docker_run(self, args, timeout=30, env=None):
        super()._docker_run(args, timeout=timeout, env=env)  # call counter
        self.docker_log_args.append(args)
        return subprocess.CompletedProcess(args, self.log_rc, stdout=self.log_stdout, stderr="")


# ── real scan: _check_restart_churn ──────────────────────────────


@pytest.mark.asyncio
async def test_scan_counts_markers_against_threshold(tmp_path):
    """The REAL scan: < threshold markers → False, ≥ threshold → True. No
    recovery boundary yet → the scan window is `--since {window}m`."""
    mgr = _ChurnScanMgr(_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path)
    mgr.log_stdout = MARKER * 3  # 3 markers < default threshold 4
    assert await mgr._check_restart_churn(mgr._restart_churn_window_min) is False
    mgr.log_stdout = MARKER * 4  # threshold reached
    assert await mgr._check_restart_churn(mgr._restart_churn_window_min) is True
    assert mgr.docker_log_args[-1] == [
        "logs",
        "--since",
        f"{mgr._restart_churn_window_min}m",
        mgr._docker_container,
    ]


@pytest.mark.asyncio
async def test_scan_respects_configured_threshold(tmp_path):
    """restart_churn_threshold from the config (not the hard default)."""
    mgr = _ChurnScanMgr(_cfg(tmp_path, restart_churn_threshold=2), tmp_path=tmp_path)
    mgr.log_stdout = MARKER * 2
    assert await mgr._check_restart_churn(mgr._restart_churn_window_min) is True


@pytest.mark.asyncio
async def test_scan_docker_error_no_flag(tmp_path):
    """`docker logs` failing (rc != 0) reads as NO churn — never a flag on
    an infra error (the recovery chain stays unarmed, like the other scans)."""
    mgr = _ChurnScanMgr(_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path)
    mgr.log_stdout = MARKER * 20
    mgr.log_rc = 1
    assert await mgr._check_restart_churn(mgr._restart_churn_window_min) is False


@pytest.mark.asyncio
async def test_scan_snaps_to_recovery_boundary(tmp_path):
    """Anti stale-marker: after a successful recovery the scan window snaps
    to AFTER the boundary (`--since {N}s`, relative Go duration) — docker
    filters the pre-recovery markers out, so a healed tunnel is NOT
    re-flagged from its own history (no recovery loop at vpn_manager)."""
    mgr = _ChurnScanMgr(_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path)
    mgr._restart_churn_recovered_at = time.time()  # recovered just now
    mgr.log_stdout = ""  # post-boundary window: nothing fresh
    assert await mgr._check_restart_churn(10) is False
    assert mgr.docker_log_args[-1] == ["logs", "--since", "1s", mgr._docker_container]


@pytest.mark.asyncio
async def test_scan_fresh_markers_after_recovery_rearm(tmp_path):
    """The boundary must not blind the scan to NEW churn: markers written
    after it (the loop resumed) re-arm — the bounded window still counts
    them (docker returns only post-boundary lines)."""
    mgr = _ChurnScanMgr(_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path)
    mgr._restart_churn_recovered_at = time.time() - 5
    mgr.log_stdout = MARKER * 4  # fresh markers, post-boundary
    assert await mgr._check_restart_churn(mgr._restart_churn_window_min) is True
    since = mgr.docker_log_args[-1][2]
    assert since.endswith("s") and since != f"{mgr._restart_churn_window_min}m"


# ── refresh_status wiring ────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_status_churn_arms_watchdog(tmp_path):
    """Churn detected → flag + arm_egress_watchdog (probe cadence
    immédiate), SANS passer en ERROR : le tunnel peut répondre à l'instant
    (c'est précisément le cas marginal que la sonde rate)."""
    mgr = FakeVPNManager(_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path)
    mgr.ips = ["1.1.1.1"]
    mgr.log_text = MARKER * 4
    await mgr.refresh_status(force=True)
    assert mgr._restart_churn is True
    assert mgr._egress_failures >= mgr._auto_wg_egress_ticks, "watchdog armé"
    assert mgr._status == vm.VPNState.CONNECTED, "churn ≠ ERROR state"
    assert mgr.scan_since == [mgr._restart_churn_window_min], "window passée"


@pytest.mark.asyncio
async def test_refresh_status_clean_logs_no_churn(tmp_path):
    """Logs sans marqueur → aucun flag, aucun arm (fenêtre auto-résolvante :
    quand les restarts s'arrêtent, le flag retombe au scan suivant)."""
    mgr = FakeVPNManager(_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path)
    mgr.ips = ["1.1.1.1"]
    mgr.log_text = "initialization sequence completed"
    await mgr.refresh_status(force=True)
    assert mgr._restart_churn is False
    assert mgr._egress_failures == 0
    assert mgr._restart_churn_recovered_at is None


# ── watchdog tick: churn avec probe VIVANTE ──────────────────────


@pytest.mark.asyncio
async def test_watchdog_tick_churn_recovers_immediately(tmp_path, caplog):
    """Le cas central du plan : probe SOCKS5 VIVANTE (fenêtre vivante) mais
    churn flaggé → le flag seul route le tick dans la recovery immédiate
    (kind « healthcheck restart loop ») : restart → IP fraîche → CONNECTED.
    La borne recovery dépose le flag au refresh interne ; le tick suivant
    est sain — pas de second restart, pas de boucle de recovery."""
    mgr = FakeVPNManager(_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path)
    # arm-refresh (1) + finalize (1) + refresh interne recovery (1) + tick2
    # refresh (1) — FIFO exact du flux.
    mgr.ips = ["5.5.5.5"] * 4
    mgr.log_text = MARKER * 4
    await mgr.refresh_status(force=True)  # arms via the scan
    assert mgr._restart_churn is True

    with caplog.at_level(logging.WARNING, logger="vpn_manager"):
        await mgr._watchdog_tick()

    assert any("healthcheck restart loop" in r.getMessage() for r in caplog.records), (
        "kind du failing block"
    )
    assert mgr.calls["restart"] == 1
    assert mgr.calls["compose_up"] == 0, "recovery légère suffit"
    assert mgr._status == vm.VPNState.CONNECTED
    assert mgr._current_ip == "5.5.5.5"
    assert mgr._restart_churn_recovered_at is not None, "borne posée"
    assert mgr._restart_churn is False, "flag déposé par le scan borné"

    await mgr._watchdog_tick()  # tick sain

    assert mgr.calls["restart"] == 1, "pas de second restart"
    assert mgr._egress_failures == 0
    assert mgr.scan_since == [10, 10, 10], "3 scans: arm, recovery interne, tick sain"


# ── apply/rollback : borne du scan sur le refresh post-recreate ───


@pytest.mark.asyncio
async def test_apply_update_snaps_churn_scan_after_recreate(tmp_path):
    """[revue 20/08] apply_update() pose la borne anti stale-marker AVANT
    son refresh_status(force=True) interne : le refresh voit `--since 1s`
    (fenêtre bornée APRÈS le recreate), pas la pleine fenêtre de 10 min —
    les marqueurs de churn antérieurs à l'update ne re-arment pas le
    watchdog pour un restart docker inutile après chaque mise à jour."""
    mgr = _ChurnScanMgr(_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path)
    mgr.log_stdout = MARKER * 4  # churn actif avant l'update
    mgr.ips = ["9.9.9.9"]  # refresh interne après recreate
    mgr._update_available = True
    mgr._update_old_image_id = "sha256:old"
    mgr._UPDATE_LOCK_PATH = str(tmp_path / "vpn_update.lock")

    async def _finalize(allow_stale=False):
        mgr._current_ip = "9.9.9.9"
        return True

    mgr._finalize_ip = _finalize

    res = await mgr.apply_update(check_opportune=False)

    assert res.get("ok") is True
    scans = [a for a in mgr.docker_log_args if a[:2] == ["logs", "--since"]]
    assert scans, "le refresh interne a scanné les logs du conteneur"
    assert scans[-1][2] == "1s", "fenêtre bornée APRÈS le recreate (anti stale-marker)"


@pytest.mark.asyncio
async def test_rollback_snaps_churn_scan_after_recreate(tmp_path):
    """Même borne sur le chemin rollback : un apply qui échoue (tunnel non
    healthy) refait un recreate via _rollback_update — son refresh interne
    doit scanner en fenêtre bornée, pas sur la pleine fenêtre pré-échec.
    (Le déclencheur est _wait_healthy → None, PAS AUTH_FAILED : une sortie
    auth_failed retomberait AVANT le scan churn dans refresh_status.)"""
    mgr = _ChurnScanMgr(_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path)
    mgr.log_stdout = MARKER * 4
    mgr.ips = ["9.9.9.9"]
    mgr._update_available = True
    mgr._update_old_image_id = "sha256:old"
    mgr._UPDATE_LOCK_PATH = str(tmp_path / "vpn_update.lock")

    async def _finalize(allow_stale=False):
        mgr._current_ip = "9.9.9.9"
        return True

    mgr._finalize_ip = _finalize

    async def _unhealthy(timeout=120.0):
        return None  # tunnel pas healthy → rollback

    mgr._wait_healthy = _unhealthy

    res = await mgr.apply_update(check_opportune=False)

    assert res.get("ok") is False
    assert "tunnel not healthy" in res.get("error", ""), "rollback déclenché"
    scans = [a for a in mgr.docker_log_args if a[:2] == ["logs", "--since"]]
    assert scans, "le refresh du rollback a scanné les logs"
    assert scans[-1][2] == "1s", "rollback: fenêtre bornée APRÈS le recreate (anti stale-marker)"
