"""test_audit_routabilite_pc.py — régressions du plan d'audit 2026-09-08.

Couvre les lots implémentés (PC-x) — offline : aucun docker, aucun réseau
(fakes FakeVPNManager / GraceFake, cf. test_vpn_freshness.py).

  * PC-1 : budget de restart initialisé + message exact
    ``watchdog_restart_budget_exhausted … escalade auth_cooling``.
  * PC-2 : TOUS les restarts comptent — voie light (déjà notée) + voie
    compose ``_ensure_container(force_recreate)`` ; la création initiale
    (conteneur absent) ne compte PAS.
  * PC-3 : décision watchdog UNIQUE (F2) — ``_decide_watchdog_action`` :
    egress dead + auth_cooling ⇒ 1 seule ligne de décision, 0 appel docker,
    jamais de « restarting X » contradictoire.
  * PC-4 : ``boot_stagger_s`` — une seule source, effet observable.
  * PC-5 : ``free_model_spread`` — round-robin par requête (F5) + compteur
    ``free_429_by_model``.
  * PC-8 : refresh périodique de la liste de serveurs (H2).
  * PC-15 : heartbeat watchdog toutes les 5 min (F10).
"""

import logging
import re
import subprocess
import time
from pathlib import Path

from test_pool_connection_failure import _Station
from test_vpn_freshness import FakeVPNManager, _cfg

import vpn.manager as cvm
from free_ip_pool import FreeIPPool

# ── GraceFake vendored (copie locale — le fichier d'origine
# tests/test_graceful_aurora.py appartient à une autre session non
# committée ; ce fichier doit rester autonome pour que le commit
# soit vert sans dépendance externe). ────────────────────────────


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


def _pc_cfg(tmp_path, **over):
    base = _cfg(tmp_path)
    base.update(over)
    return base


# ── PC-1 : budget initialisé + message ────────────────────────────


async def test_pc1_budget_initialized_and_message_exact(tmp_path, caplog):
    mgr = FakeVPNManager(
        _pc_cfg(
            tmp_path,
            watchdog_auth_grace_s=0,
            watchdog_max_restarts_per_hour=3,
        ),
        tmp_path=tmp_path,
    )
    # Le budget est une vraie liste (F1 : getattr None → [] permanent).
    assert isinstance(mgr._watchdog_restarts_1h, list)
    now = mgr._now_fn()
    mgr._watchdog_restarts_1h = [now - 10.0, now - 20.0, now - 30.0]
    mgr._auth_grace_until = now - 1.0
    mgr.log_text = "AUTH_FAILED"
    with caplog.at_level(logging.WARNING, logger="vpn_manager"):
        await mgr._watchdog_tick()
    assert mgr.calls["restart"] == 0
    assert mgr._watchdog_last_action == "budget-exhausted"
    assert "watchdog_restart_budget_exhausted" in caplog.text
    assert "escalade auth_cooling" in caplog.text


# ── PC-2 : toutes les voies comptent ──────────────────────────────


async def test_pc2_compose_recreate_counts_budget(tmp_path):
    mgr = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path)
    assert mgr._watchdog_restarts_1h == []
    mgr._auth_failed = True  # force_recreate None → True
    await mgr._ensure_container()
    assert len(mgr._watchdog_restarts_1h) == 1
    assert mgr.calls["compose_up"] == 1


async def test_pc2_plain_create_does_not_count(tmp_path):
    mgr = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path)
    mgr.container_present = False  # conteneur absent → création, pas restart
    await mgr._ensure_container()
    assert mgr._watchdog_restarts_1h == []
    assert mgr.calls["compose_up"] == 1


async def test_pc2_public_restart_counts_budget(tmp_path):
    """Voie superviseur/dashboard : restart() public = 1 au budget."""
    mgr = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path)
    assert mgr._watchdog_restarts_1h == []
    await mgr.restart()
    assert len(mgr._watchdog_restarts_1h) == 1
    assert mgr.calls["restart"] == 1


# ── PC-3 : décision unique ────────────────────────────────────────


def test_pc3_decide_action_matrix(tmp_path):
    mgr = FakeVPNManager(
        _pc_cfg(tmp_path, watchdog_auth_grace_s=240), tmp_path=tmp_path
    )
    # Voie non-AUTH sans budget consommé → restart.
    mgr._auth_failed = False
    assert mgr._decide_watchdog_action("TLS negotiation timeout")[0] == "restart"
    # Voie AUTH, grâce jamais armée → grace-first.
    mgr._auth_failed = True
    assert mgr._decide_watchdog_action("egress dead")[0] == "grace-first"
    # Grâce active → grace.
    mgr._auth_grace_until = mgr._now_fn() + 100.0
    assert mgr._decide_watchdog_action("AUTH_FAILED")[0] == "grace"
    # Grâce expirée → recheck (vérif async encore requise).
    mgr._auth_grace_until = mgr._now_fn() - 1.0
    assert mgr._decide_watchdog_action("AUTH_FAILED")[0] == "recheck"
    # Cooling actif → auth-cooling (prioritaire sur la grâce).
    mgr._auth_cool_until = mgr._now_fn() + 50.0
    assert mgr._decide_watchdog_action("AUTH_FAILED")[0] == "auth-cooling"
    # Budget épuisé (voie non-AUTH) → budget-exhausted.
    mgr2 = FakeVPNManager(
        _pc_cfg(tmp_path, watchdog_max_restarts_per_hour=3), tmp_path=tmp_path
    )
    now = mgr2._now_fn()
    mgr2._watchdog_restarts_1h = [now - 10.0, now - 20.0, now - 30.0]
    assert mgr2._decide_watchdog_action("egress dead")[0] == "budget-exhausted"


async def test_pc3_single_decision_cooling_no_restart(tmp_path, caplog):
    """F2 : egress dead + auth_cooling ⇒ 1 ligne de décision, 0 docker."""
    mgr = FakeVPNManager(
        _pc_cfg(
            tmp_path,
            watchdog_auth_grace_s=240,
            watchdog_max_restarts_per_hour=3,
        ),
        tmp_path=tmp_path,
    )
    mgr.log_text = "AUTH_FAILED - credentials rejected"
    mgr._auth_cool_until = mgr._now_fn() + 300.0
    with caplog.at_level(logging.WARNING, logger="vpn_manager"):
        await mgr._watchdog_tick()
    assert mgr.calls["restart"] == 0
    assert mgr.calls["compose_up"] == 0
    assert mgr._watchdog_last_action == "auth-cooling"
    wd = [
        r
        for r in caplog.records
        if r.name == "vpn_manager" and "[vpn-watchdog]" in r.getMessage()
    ]
    assert len(wd) == 1, [r.getMessage() for r in wd]
    assert "restarting" not in caplog.text


# ── PC-4 : boot stagger (O1 : la clé change le comportement) ───────


def test_pc4_boot_stagger_key_has_effect(monkeypatch):
    import opencode as oc

    monkeypatch.setitem(oc.IP_ROTATION, "boot_stagger_s", 30)
    assert oc._boot_stagger_s() == 30.0
    monkeypatch.setitem(oc.IP_ROTATION, "boot_stagger_s", 0)
    assert oc._boot_stagger_s() == 0.0
    monkeypatch.setitem(oc.IP_ROTATION, "boot_stagger_s", "nawak")
    assert oc._boot_stagger_s() == 5.0  # invalide → défaut historique
    monkeypatch.setitem(oc.IP_ROTATION, "boot_stagger_s", 9999)
    assert oc._boot_stagger_s() == 120.0  # clamp haut


# ── PC-5 : spread modèle free (F5) ────────────────────────────────


def test_pc5_spread_round_robin(monkeypatch):
    import opencode as oc

    oc._free_model_rr.clear()
    try:
        monkeypatch.setitem(oc.IP_ROTATION, "free_model_spread", True)
        monkeypatch.setitem(
            oc.IP_ROTATION,
            "free_model_candidates",
            {
                "muse-spark-1.3-contributor": [
                    "muse-spark-1.3-contributor-free",
                    "mimo-v2.5-free",
                ]
            },
        )
        monkeypatch.setitem(
            oc.FREE_MODEL_MAP,
            "muse-spark-1.3-contributor",
            "muse-spark-1.3-contributor-free",
        )
        r1 = oc._resolve_free_model("muse-spark-1.3-contributor")
        r2 = oc._resolve_free_model("muse-spark-1.3-contributor")
        assert r1 != r2  # 2 requêtes consécutives ≠ même modèle
        assert {r1, r2} == {
            "muse-spark-1.3-contributor-free",
            "mimo-v2.5-free",
        }
    finally:
        oc._free_model_rr.clear()


def test_pc5_spread_off_is_historic(monkeypatch):
    import opencode as oc

    oc._free_model_rr.clear()
    try:
        monkeypatch.setitem(oc.IP_ROTATION, "free_model_spread", False)
        monkeypatch.setitem(
            oc.FREE_MODEL_MAP,
            "muse-spark-1.3-contributor",
            "muse-spark-1.3-contributor-free",
        )
        assert (
            oc._resolve_free_model("muse-spark-1.3-contributor")
            == "muse-spark-1.3-contributor-free"
        )
        assert oc._resolve_free_model("no-such-model") is None
    finally:
        oc._free_model_rr.clear()


def test_pc5_429_counter_by_model():
    import opencode as oc

    oc._free_429_by_model.clear()
    try:
        oc._note_free_429("muse-spark-1.3-contributor-free")
        oc._note_free_429("muse-spark-1.3-contributor-free")
        assert oc._free_429_by_model["muse-spark-1.3-contributor-free"] == 2
    finally:
        oc._free_429_by_model.clear()


# ── PC-8 : refresh périodique liste serveurs ───────────────────────


async def test_pc8_periodic_server_list_refresh(tmp_path):
    mgr = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path)
    # La clé est lue au boot ET au hot-reload (O1 : effet prouvé).
    assert mgr._server_list_refresh_interval_s == 21600.0
    await mgr.update_config({"server_list_refresh_interval_s": 60})
    assert mgr._server_list_refresh_interval_s == 60.0
    fired = []
    _orig = mgr._refresh_server_list

    async def _spy():
        fired.append(1)
        await _orig()

    mgr._refresh_server_list = _spy  # type: ignore[method-assign]
    mgr._server_list_refresh_interval_s = 60.0
    mgr._last_server_list_refresh_at = mgr._now_fn() - 120.0
    await mgr._watchdog_tick()  # fake sain
    assert fired == [1]
    await mgr._watchdog_tick()  # tick immédiat → pas de refresh
    assert fired == [1]


async def test_pc8_no_periodic_refresh_during_incident(tmp_path):
    """Pendant un incident, seul le refresh piloté par la recovery tourne."""
    mgr = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path)
    fired = []
    _orig = mgr._refresh_server_list

    async def _spy():
        fired.append(1)
        await _orig()

    mgr._refresh_server_list = _spy  # type: ignore[method-assign]
    mgr._server_list_refresh_interval_s = 60.0
    mgr._last_server_list_refresh_at = mgr._now_fn() - 3600.0
    mgr._auth_failed = True  # incident AUTH en cours
    await mgr._watchdog_tick()
    # Le tick peut refresher via la voie incident (recovery) mais JAMAIS via
    # la voie périodique : le timer périodique n'a pas été réarmé par elle.
    assert mgr._last_server_list_refresh_at <= mgr._now_fn() - 3600.0


# ── PC-15 : heartbeat ─────────────────────────────────────────────


async def test_pc15_heartbeat_every_5min(tmp_path, caplog):
    mgr = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path)
    mgr._now_fn = lambda: 1000.0  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="vpn_manager"):
        await mgr._watchdog_tick()
    hb = [r for r in caplog.records if "[watchdog] hb" in r.getMessage()]
    assert len(hb) == 1, [r.getMessage() for r in caplog.records]
    assert "s1" in hb[0].getMessage()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="vpn_manager"):
        await mgr._watchdog_tick()
    assert not [r for r in caplog.records if "[watchdog] hb" in r.getMessage()]
    mgr._now_fn = lambda: 1400.0  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="vpn_manager"):
        await mgr._watchdog_tick()
    assert (
        len([r for r in caplog.records if "[watchdog] hb" in r.getMessage()]) == 1
    )


# ── PC-7 : canari indéterminé (F3/H7) ───────────────────────────


async def test_pc7_bringup_failure_is_indeterminate(tmp_path, caplog):
    """Bring-up compose échoué ⇒ INDÉTERMINÉ : flip maintenu, pas de cooldown."""
    mgr = GraceFake(_gcfg(tmp_path), tmp_path)

    def _raise(args, timeout=30, env=None):
        if "up" in list(args):
            raise RuntimeError("boom compose")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    mgr._docker_run = _raise  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="vpn_manager"):
        alive = await mgr._wg_canary_alive("test")
    assert alive is False
    assert mgr._wg_canary_state["ok"] is None  # indéterminé, pas FAIL
    assert "INDÉTERMINÉ" in caplog.text
    mgr._pending_flip = ("wireguard", "test")
    cancelled = await mgr._cancel_wg_flip_if_canary_dead()
    assert cancelled is False
    assert mgr._pending_flip == ("wireguard", "test")
    assert mgr._flip_annule_cooldown_until == 0.0


async def test_pc7_measured_negative_cancels_with_cooldown(tmp_path):
    """Egress négative MESURÉE ⇒ FAIL : flip annulé + cooldown 30 min."""
    mgr = GraceFake(_gcfg(tmp_path), tmp_path)
    mgr._WG_CANARY_BOOT_TIMEOUT_S = 0.05
    mgr._WG_CANARY_POLL_INTERVAL_S = 0.01

    async def _no_probe():
        return False

    mgr._canary_probe_once = _no_probe  # type: ignore[method-assign]
    alive = await mgr._wg_canary_alive("test")
    assert alive is False
    assert mgr._wg_canary_state["ok"] is False  # FAIL mesuré (caché 90 s)
    mgr._pending_flip = ("wireguard", "test")
    cancelled = await mgr._cancel_wg_flip_if_canary_dead()
    assert cancelled is True
    assert mgr._pending_flip is None
    assert mgr._flip_annule_cooldown_until > mgr._now_fn()


# ── PC-11 : périmètre pays (F4/H8) ─────────────────────────────


def test_pc11_assigned_country_deterministic(tmp_path):
    """N=6, 5 pays : partage explicite et déterministe (s6 avec s1)."""
    mk = lambda n: GraceFake(  # noqa: E731
        _gcfg(tmp_path, server_countries="Germany,Netherlands,France,Sweden,Switzerland"),
        tmp_path,
        station=n,
    )
    assert mk(1)._assigned_country() == "Germany"
    assert mk(2)._assigned_country() == "Netherlands"
    assert mk(6)._assigned_country() == "Germany"
    assert mk(6)._assigned_country() == mk(1)._assigned_country()


def test_pc11_perimeter_helper(tmp_path):
    mgr = GraceFake(
        _gcfg(tmp_path, server_countries="Germany,Netherlands"), tmp_path
    )
    assert mgr._country_in_perimeter("Germany") is True
    assert mgr._country_in_perimeter("Brazil") is False
    assert mgr._country_in_perimeter("") is False


async def test_pc11_out_of_perimeter_refused(tmp_path, caplog):
    """Pin hors périmètre ⇒ refusé + loggé, aucun PUT ne part."""
    mgr = GraceFake(
        _gcfg(tmp_path, control_api_key="k", server_countries="Germany,Netherlands"),
        tmp_path,
    )
    assert mgr._control_enabled is True
    calls = []

    async def _spy(method, path, body=None, timeout=10.0):
        calls.append((method, path))
        return []

    mgr._control_exec = _spy  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="vpn_manager"):
        ok = await mgr._control_pin_country("Brazil", timeout=1)
    assert ok is False
    assert calls == []
    assert "hors périmètre" in caplog.text


async def test_pc11_opt_out_allows_any_country(tmp_path):
    """egress_allow_any_country: true ⇒ le gate laisse passer (PUT émis)."""
    mgr = GraceFake(
        _gcfg(
            tmp_path,
            control_api_key="k",
            server_countries="Germany,Netherlands",
            egress_allow_any_country=True,
        ),
        tmp_path,
    )
    assert mgr._egress_allow_any_country is True
    calls = []

    async def _spy(method, path, body=None, timeout=10.0):
        calls.append((method, path))
        return ["running"]

    mgr._control_exec = _spy  # type: ignore[method-assign]
    ok = await mgr._control_pin_country("Brazil", timeout=5, catchup=0)
    assert calls, "le PUT aurait dû partir (opt-out)"
    assert ok is True


# ── PC-14 : couverture configuration (O1) ────────────────────────


def test_pc14_analyze_statuses(tmp_path, monkeypatch):

    monkeypatch.syspath_prepend("scripts")
    import config_coverage as cc

    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "code.py").write_text('X = cfg.get("live_key")\n', encoding="utf-8")
    (root / "pkg" / "code2.py").write_text('Y = cfg.get("partial_key")\n', encoding="utf-8")
    (root / "tests" / "test_a.py").write_text(
        'def test_x():\n    assert "live_key"\n', encoding="utf-8"
    )
    cfg = root / "config.yaml"
    cfg.write_text("live_key: 1\npartial_key: 2\ndead_key: 3\n", encoding="utf-8")
    rows, _ = cc.analyze(cfg, root)
    by_key = {d: s for d, s, _, _ in rows}
    assert by_key["live_key"] == "CONSOMMÉE"
    assert by_key["partial_key"] == "PARTIELLE"
    assert by_key["dead_key"] == "MORTE"
    assert cc.main(["--config", str(cfg), "--root", str(root)]) == 0
    assert cc.main(["--config", str(cfg), "--root", str(root), "--strict"]) == 1


def test_pc14_display_knobs_take_effect(monkeypatch):
    from dashboard import display as disp

    saved = (disp.LOG_VISIBLE, disp._DEBUG_FLUSH_INTERVAL, disp._DEBUG_MAX_SIZE, disp.log_lines)
    try:
        monkeypatch.setattr(
            disp._cfg_settings,
            "yaml_get",
            lambda s, k=None, d=None: {
                "dashboard": {"display_lines": 50},
                "debug": {"log_lines_max": 300, "flush_interval": 7, "max_size": 12345678},
            }.get(s, {}).get(k, d),
        )
        out = disp.refresh_display_config()
        assert out == {
            "display_lines": 50,
            "log_lines_max": 300,
            "flush_interval": 7,
            "max_size": 12345678,
        }
        assert disp.LOG_VISIBLE == 50 and disp.log_lines.maxlen == 300
    finally:
        disp.LOG_VISIBLE, disp._DEBUG_FLUSH_INTERVAL, disp._DEBUG_MAX_SIZE, disp.log_lines = saved


def test_pc14_quota_interval_knob(monkeypatch):
    import config.settings as cs
    from dashboard import quota as q

    monkeypatch.setattr(cs, "yaml_get", lambda s, k=None, d=None: 90)
    assert q._quota_fetch_interval() == 90.0
    monkeypatch.setattr(cs, "yaml_get", lambda s, k=None, d=None: 5)
    assert q._quota_fetch_interval() == 60.0  # clamp bas
    monkeypatch.setattr(cs, "yaml_get", lambda s, k=None, d=None: 99999)
    assert q._quota_fetch_interval() == 3600.0  # clamp haut


# ── PC-13 : logging sans doublons (F8) + timestamps UTC (F11) ───


def test_pc13_no_duplicate_log_lines(tmp_path):
    """Ratio brut/unique = 1.0 : 3 émissions ⇒ 3 lignes fichier, format Z."""
    from dashboard import display as disp

    logfile = tmp_path / "debug.log"
    logger = logging.getLogger("vpn_manager")
    for h in list(logger.handlers):
        logger.removeHandler(h)
    old_level = logger.level
    logger.setLevel(logging.NOTSET)
    disp.set_debug_log_file(str(logfile))
    try:
        disp.attach_module_logger("vpn_manager")
        for i in range(3):
            logger.warning(f"[vpn-watchdog] pc13-nodup line {i}")
        lines = [
            l
            for l in logfile.read_text(encoding="utf-8").splitlines()
            if "pc13-nodup" in l
        ]
        assert len(lines) == 3
        for l in lines:
            assert re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}Z\]", l), l
    finally:
        for h in list(logger.handlers):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        logger.setLevel(old_level)
        if disp._debug_file is not None:
            try:
                disp._debug_file.close()
            except Exception:
                pass
        disp._debug_file = None
        disp._debug_file_path = None
        disp._extra_handlers = []


def test_pc13_attach_is_idempotent(tmp_path):
    """Double attach ⇒ 1 seul handler, 1 ligne par émission (F8 en prod)."""
    from dashboard import display as disp

    logfile = tmp_path / "debug.log"
    logger = logging.getLogger("vpn_manager")
    for h in list(logger.handlers):
        logger.removeHandler(h)
    old_level = logger.level
    logger.setLevel(logging.NOTSET)
    disp.set_debug_log_file(str(logfile))
    try:
        fh1 = disp.attach_module_logger("vpn_manager")
        fh2 = disp.attach_module_logger("vpn_manager")
        assert fh2 is fh1
        logger.warning("pc13-idem line")
        lines = [
            l
            for l in logfile.read_text(encoding="utf-8").splitlines()
            if "pc13-idem" in l
        ]
        assert len(lines) == 1
    finally:
        for h in list(logger.handlers):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        logger.setLevel(old_level)
        if disp._debug_file is not None:
            try:
                disp._debug_file.close()
            except Exception:
                pass
        disp._debug_file = None
        disp._debug_file_path = None
        disp._extra_handlers = []


# ── PC-16 : free-usage désambiguïsé (F9) + enforce_vpn_only ───────

def test_pc16_usage_line_has_both_ips(monkeypatch):
    import opencode as oc

    msgs = []
    monkeypatch.setattr(oc, "_debug", msgs.append)
    oc._log_free_model_usage(
        "paid-m", "free-m", "k" * 16, "w" * 12, 200, 11, 22, 33,
        ip="5.6.7.8", client_ip="192.168.1.2",
    )
    line = next(m for m in msgs if "[free-usage]" in m)
    assert "client_ip=192.168.1.2" in line
    assert "egress_ip=5.6.7.8" in line


def test_pc16_client_ip_contextvar(monkeypatch):
    import opencode as oc

    msgs = []
    monkeypatch.setattr(oc, "_debug", msgs.append)
    tok = oc._current_client_ip.set("10.0.0.9")
    try:
        oc._log_free_model_usage("paid-m", "free-m", "k" * 16, "w" * 12, 200, ip="5.6.7.8")
    finally:
        oc._current_client_ip.reset(tok)
    line = next(m for m in msgs if "[free-usage]" in m)
    assert "client_ip=10.0.0.9" in line
    assert "egress_ip=5.6.7.8" in line


def test_pc16_enforce_blocks_direct(monkeypatch):
    import opencode as oc

    monkeypatch.setitem(oc.IP_ROTATION, "enforce_vpn_only", True)
    assert oc._direct_fallback_allowed() is False
    monkeypatch.setitem(oc.IP_ROTATION, "enforce_vpn_only", False)
    assert oc._direct_fallback_allowed() is True


# ── Observabilité SLO (O4) + free_429 (PC-5) ────────────────────


def test_metrics_pool_section():
    from observability.metrics import render_pool_section

    assert render_pool_section(None) == []
    assert render_pool_section({}) == []
    lines = render_pool_section(
        {
            "usable": [('station="1"', 1), ('station="2"', 0)],
            "usable_count": 1,
            "total": 2,
            "floor": 2,
        }
    )
    text = "\n".join(lines)
    assert 'pool_station_usable{station="1"} 1' in text
    assert 'pool_station_usable{station="2"} 0' in text
    assert 'pool_usable_stations{total="2"} 1' in text
    assert 'pool_usable_floor{total="2"} 2' in text


def test_metrics_free_429_section():
    from observability.metrics import render_free_429_section

    assert render_free_429_section({}) == []
    assert render_free_429_section(None) == []
    text = "\n".join(render_free_429_section({"m1": 3, "m2": 1}))
    assert 'free_429_by_model{model="m1"} 3' in text
    assert 'free_429_by_model{model="m2"} 1' in text


def test_metrics_host_wires_pool_and_429(monkeypatch):
    import opencode as oc
    import shared_state as ss

    sts = [_Station(i + 1) for i in range(3)]
    pool = _pc_pool(*sts)
    pool._apply_bad_mark(sts[0], "rate_limit")  # 2/3 éligibles, plancher 2
    monkeypatch.setattr(ss, "free_ip_pool", pool, raising=False)
    oc._free_429_by_model.clear()
    try:
        oc._note_free_429("muse-spark-1.3-contributor-free")
        text = oc._build_metrics_text()
    finally:
        oc._free_429_by_model.clear()
    assert 'pool_usable_stations{total="3"} 2' in text
    assert 'pool_usable_floor{total="3"} 2' in text
    assert 'free_429_by_model{model="muse-spark-1.3-contributor-free"} 1' in text


# ── O3-mixed : flotte hétérogène (toutes technos) ───────────────


def test_mixed_slot_mapping(tmp_path):
    mgr = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path)
    assert mgr._mixed_active() is True
    got = []
    for n in range(1, 7):
        mgr._station = n
        got.append(mgr._mixed_slot())
    assert got == ["wireguard", "openvpn-tcp", "openvpn-udp"] * 2


def test_mixed_gate_blocks_ov_slot_wg_return(tmp_path):
    """Slot OV + rafale AUTH + clé WG présente ⇒ PAS de flip WG (stable)."""
    mgr = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path)
    mgr._station = 2  # slot openvpn-tcp
    Path(mgr._wg_key_file).touch()
    now = mgr._now_fn()
    mgr._auth_failed_window = [now - 100.0, now - 200.0, now - 300.0]
    assert mgr._decide_watchdog_action("egress dead")[0] in ("restart", "budget-exhausted")
    assert mgr._auto_flip_decision() is None


def test_mixed_wg_slot_returns_when_proven(tmp_path):
    """Slot WG + même rafale ⇒ flip WG proposé (canari tranche ensuite)."""
    mgr = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path)
    mgr._station = 1  # slot wireguard
    Path(mgr._wg_key_file).touch()
    now = mgr._now_fn()
    mgr._auth_failed_window = [now - 100.0, now - 200.0, now - 300.0]
    mode, reason = mgr._auto_flip_decision()
    assert mode == "wireguard"
    assert "AUTH_FAILED" in reason


def test_mixed_off_restores_legacy_auto(tmp_path):
    """Rollback auto_mixed_stacks=false : slot OV reflippe comme avant."""
    mgr = FakeVPNManager(
        _pc_cfg(tmp_path, auto_mixed_stacks=False), tmp_path=tmp_path
    )
    assert mgr._mixed_active() is False
    mgr._station = 2
    Path(mgr._wg_key_file).touch()
    now = mgr._now_fn()
    mgr._auth_failed_window = [now - 100.0, now - 200.0, now - 300.0]
    mode, _reason = mgr._auto_flip_decision()
    assert mode == "wireguard"


def test_apply_mixed_slot_sets_state(tmp_path):
    m1 = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path, station=1)
    m2 = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path, station=2)
    m3 = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path, station=3)
    assert m1.apply_mixed_slot() == "wireguard"
    assert m1._stack_effective == "wireguard"
    assert m1._stack == "auto"  # le mode choisi ne change pas
    assert m2.apply_mixed_slot() == "openvpn-tcp"
    assert m2._stack_effective == "openvpn"
    assert m2._ovpn_protocol_effective == "tcp"
    assert m2._ovpn_endpoint_port_effective == "443"
    assert m3.apply_mixed_slot() == "openvpn-udp"
    assert m3._ovpn_protocol_effective == "udp"
    assert m3._ovpn_endpoint_port_effective == "1194"
    m4 = FakeVPNManager(
        _pc_cfg(tmp_path, auto_mixed_stacks=False), tmp_path=tmp_path, station=2
    )
    before = m4._stack_effective
    assert m4.apply_mixed_slot() == "openvpn-tcp"
    assert m4._stack_effective == before  # rollback : état inchangé


def test_mixed_inactive_on_uniform_modes(tmp_path):
    mgr = FakeVPNManager(
        _pc_cfg(tmp_path, vpn_stack="wireguard"), tmp_path=tmp_path
    )
    assert mgr._mixed_active() is False


# ── Sonde SOCKS double-passe (faux "socks down") ────────────────


async def test_socks_probe_second_pass_on_timeouts(tmp_path, monkeypatch):
    """Timeouts 3 s partout puis OK à 8 s ⇒ True (tunnel lent, pas mort)."""
    mgr = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path)
    mgr._ip_check_urls = ["http://a.invalid", "http://b.invalid"]
    calls = {"n": 0, "budgets": []}

    async def _fake_probe(url, *, per_attempt):
        calls["n"] += 1
        calls["budgets"].append(per_attempt)
        return "ok" if per_attempt >= 8.0 else "timeout"

    monkeypatch.setattr(mgr, "_probe_connect", _fake_probe)
    assert await mgr._socks_egress_ok() is True
    assert calls["budgets"][0] <= 3.0
    assert max(calls["budgets"]) >= 8.0


async def test_socks_probe_refused_no_second_pass(tmp_path, monkeypatch):
    """Refus franc ⇒ False immédiat, pas de repasse (mort franche)."""
    mgr = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path)
    mgr._ip_check_urls = ["http://a.invalid"]
    calls = {"n": 0}

    async def _fake_probe(url, *, per_attempt):
        calls["n"] += 1
        return "refused"

    monkeypatch.setattr(mgr, "_probe_connect", _fake_probe)
    assert await mgr._socks_egress_ok() is False
    assert calls["n"] == 1


# ── Détecteur recreate externe (audit 2026-09-08) ────────────────


def test_mutating_verb_classifier():
    from vpn.manager import _is_mutating_docker_op as _cls

    assert _cls(["restart", "x"]) is True
    assert _cls(["compose", "-f", "f", "up", "-d", "svc"]) is True
    assert _cls(["compose", "-f", "f", "rm", "-sf", "svc"]) is True
    assert _cls(["compose", "-f", "f", "stop", "svc"]) is True
    assert _cls(["inspect", "-f", "{{.State}}", "x"]) is False
    assert _cls(["logs", "--tail", "5", "x"]) is False
    assert _cls(["exec", "x", "wget", "http://y"]) is False
    assert _cls(["ps", "-a"]) is False
    assert _cls([]) is False
    assert _cls(None) is False


def test_external_recreate_detected(tmp_path, caplog):
    import logging

    mgr = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path)
    with caplog.at_level(logging.WARNING, logger="vpn_manager"):
        mgr._detect_external_container_change({"started_at": "2026-09-08T10:00:00Z"})
    assert "HORS proxy" not in caplog.text  # premier constat : mémorise
    mgr._last_own_docker_op_at = mgr._now_fn() - 500.0
    with caplog.at_level(logging.WARNING, logger="vpn_manager"):
        mgr._detect_external_container_change({"started_at": "2026-09-08T11:00:00Z"})
    assert "HORS proxy" in caplog.text
    assert "s1" in caplog.text


def test_own_recreate_stays_silent(tmp_path, caplog):
    import logging

    mgr = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path)
    mgr._detect_external_container_change({"started_at": "2026-09-08T10:00:00Z"})
    mgr._last_own_docker_op_at = mgr._now_fn()  # notre op à l'instant
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="vpn_manager"):
        mgr._detect_external_container_change({"started_at": "2026-09-08T12:00:00Z"})
    assert "HORS proxy" not in caplog.text


# ── Helpers pool ────────────────────────────────────────────────


def _pc_pool(*stations):
    pool = FreeIPPool(stations[0], stations[1] if len(stations) > 1 else None)
    if len(stations) > 2:
        pool.set_stations(list(stations))
    pool._bad_ttl = 60.0
    pool._rotation_wait_timeout = 5.0
    return pool


# ── PC-10 : bad-mark par cause + garde N-2 ───────────────────────


def test_pc10_cause_ttl():
    sts = [_Station(i + 1) for i in range(3)]
    pool = _pc_pool(*sts)
    pool.update_config(
        {"station_bad_ttl_s": 60, "bad_ttl_by_cause": {"timeout": 30, "rate_limit": None}}
    )
    pool._apply_bad_mark(sts[0], "timeout")
    rem = pool._per_station(sts[0])["bad_until"] - time.monotonic()
    assert 25.0 < rem <= 30.0
    assert pool._per_station(sts[0])["bad_cause"] == "timeout"
    pool._apply_bad_mark(sts[1], "rate_limit")  # null → legacy 60 s
    rem = pool._per_station(sts[1])["bad_until"] - time.monotonic()
    assert 55.0 < rem <= 60.0
    pool._apply_bad_mark(sts[2], "unknown-cause")  # inconnue → legacy
    rem = pool._per_station(sts[2])["bad_until"] - time.monotonic()
    assert 55.0 < rem <= 60.0


def test_pc10_guard_n2_keeps_serving(caplog):
    """6 stations, 3 marquées : la 3ᵉ reste servie (dégradée), les saines d'abord."""
    sts = [_Station(i + 1) for i in range(6)]
    pool = _pc_pool(*sts)
    assert pool._pool_floor() == 4
    pool._apply_bad_mark(sts[0], "rate_limit")
    pool._apply_bad_mark(sts[1], "rate_limit")
    assert pool._per_station(sts[0])["degraded_override"] is False
    assert pool._per_station(sts[1])["degraded_override"] is False
    with caplog.at_level(logging.WARNING, logger="free.pool"):
        pool._apply_bad_mark(sts[2], "rate_limit")
    assert pool._per_station(sts[2])["degraded_override"] is True
    assert "invariant_n2" in caplog.text
    # Marquée mais servie : raison None, usable True, dernier recours au tri.
    assert pool._station_usable(sts[2], exclude_approaching=False) is True
    assert pool._non_routable_reason(sts[2]) is None
    assert pool._best_station() is sts[3]
    # Les 2 premières restent exclues (pas d'override).
    assert pool._station_usable(sts[0], exclude_approaching=False) is False
    assert pool._non_routable_reason(sts[0]).startswith("bad_until")


def test_pc10_small_pool_failover_preserved():
    """2 stations : la marquée reste dernier recours, la saine est servie."""
    st1, st2 = _Station(1), _Station(2)
    pool = _pc_pool(st1, st2)
    pool._apply_bad_mark(st1, "timeout")
    assert pool._per_station(st1)["bad_until"] is not None
    assert pool._best_station() is st2
    st2.status = "error"
    assert pool._best_station() is st1  # jamais 0 candidat


def test_pc10_rotation_clears_override():
    sts = [_Station(i + 1) for i in range(6)]
    pool = _pc_pool(*sts)
    pool._apply_bad_mark(sts[0], "rate_limit")
    pool._apply_bad_mark(sts[1], "rate_limit")
    pool._apply_bad_mark(sts[2], "rate_limit")
    assert pool._per_station(sts[2])["degraded_override"] is True
    per = pool._per_station(sts[2])
    per["bad_until"] = None  # fin de rotation (cf. _rotate_station)
    per["bad_cause"] = None
    per["degraded_override"] = False
    assert pool._station_usable(sts[2], exclude_approaching=False) is True


# ── PC-9 : rotation sans coupure ────────────────────────────────


def test_pc9_connecting_grace_serves_old_ip():
    st = _Station(1, status="connecting", current_ip="9.9.9.9")
    pool = _pc_pool(st)
    per = pool._per_station(st)
    per["last_confirmed_ip"] = "9.9.9.9"
    per["rotation_started_at"] = time.monotonic()
    assert pool._station_usable(st, exclude_approaching=False) is True
    assert pool._non_routable_reason(st) is None


def test_pc9_grace_expires_or_no_ip():
    st = _Station(1, status="connecting", current_ip="9.9.9.9")
    pool = _pc_pool(st)
    per = pool._per_station(st)
    per["last_confirmed_ip"] = "9.9.9.9"
    per["rotation_started_at"] = time.monotonic() - 100.0  # grâce 5 s dépassée
    assert pool._station_usable(st, exclude_approaching=False) is False
    per["rotation_started_at"] = time.monotonic()
    per["last_confirmed_ip"] = None
    st.current_ip = None
    assert pool._station_usable(st, exclude_approaching=False) is False


def test_pc9_bad_mark_wins_over_grace():
    sts = [_Station(i + 1) for i in range(3)]
    pool = _pc_pool(*sts)
    pool._apply_bad_mark(sts[0], "rate_limit")  # pas d'override (2/3 ≥ plancher 2)
    assert pool._per_station(sts[0])["degraded_override"] is False
    sts[0].status = "connecting"
    per = pool._per_station(sts[0])
    per["last_confirmed_ip"] = "9.9.9.9"
    per["rotation_started_at"] = time.monotonic()
    assert pool._station_usable(sts[0], exclude_approaching=False) is False
    assert pool._non_routable_reason(sts[0]).startswith("bad_until")
