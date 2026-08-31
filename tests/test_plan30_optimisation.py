"""[plan 30/08] Non-régression des lots A/B/C : breaker warm-up grace (A1),
bad_ttl effectif fast-pin progressif (A2), storm gate rotations (A4), pool
station_bad_ttl_s / station_connect_retry_interval_s (Lot A « constantes →
config »), /health allégé (B3), debug.log sans doublon (B2), purge hebdo
app.db (B1), supervisor.warmup_excluded_requests (transverse).

Les objets VPNManager/FreeIPPool sont construits via ``object.__new__``
+ injection minimale des attrs visés (zéro docker, zéro réseau) — cohérent
avec la pratique des suites existantes (shells tirés de test_vpn_freshness).
"""

import logging
import time

import pytest

import station_supervisor
import vpn_manager as vm
from app import db as _app_db

# ── A1 : warm-up grace breaker ──────────────────────────────────────


def _mgr_for_breaker():
    """Shell VPNManager minimal pour exercer _breaker_charge_* sans __init__."""
    mgr = object.__new__(vm.VPNManager)
    mgr._docker_container = "test-container"
    mgr._circuit_breaker = vm.CircuitBreaker(failure_threshold=3, recovery_time=300.0)
    mgr._warmup_until = 0.0
    mgr._breaker_warmup_grace_s = 60.0
    return mgr


class TestBreakerWarmup:
    def test_failure_dans_grace_ne_compte_pas(self, caplog):
        mgr = _mgr_for_breaker()
        mgr._warmup_until = time.monotonic() + 30.0  # fenêtre active
        with caplog.at_level(logging.INFO, logger="vpn_manager"):
            mgr._breaker_charge_failure("probe ip")
        info = mgr._circuit_breaker._servers.get("test-container", {})
        assert info.get("failures", 0) == 0
        assert info.get("state") in (None, "closed")
        assert any("warm-up" in rec.message for rec in caplog.records)

    def test_failure_apres_grace_compte(self):
        mgr = _mgr_for_breaker()
        mgr._warmup_until = time.monotonic() - 1.0  # fenêtre expirée
        for _ in range(3):
            mgr._breaker_charge_failure("probe ip")
        mgr._breaker_charge_failure("probe ip")
        info = mgr._circuit_breaker._servers["test-container"]
        assert info["failures"] >= 3
        assert info["state"] == "open"

    def test_success_dans_grace_ouvre_sauf_si_ferme_et_ferme_fenetre(self):
        mgr = _mgr_for_breaker()
        mgr._warmup_until = time.monotonic() + 30.0
        mgr._breaker_charge_success()
        assert mgr._warmup_until == 0.0

    def test_grace_clampee_entre_10_et_600(self):
        # borne basse : valeur 0.0 → 10.0 ; borne haute : 5000 → 600
        for raw, expected in ((0.0, 10.0), (5000.0, 600.0)):
            mgr = _mgr_for_breaker()
            mgr._breaker_warmup_grace_s = vm._clamp_cfg_number(
                {"breaker_warmup_grace_s": raw}, "breaker_warmup_grace_s", 60.0, 10.0, 600.0
            )
            assert mgr._breaker_warmup_grace_s == expected

    def test_grace_invalide_lexNone_defaut_60(self, caplog):
        mgr = _mgr_for_breaker()
        mgr._breaker_warmup_grace_s = vm._clamp_cfg_number(
            {"breaker_warmup_grace_s": "abc"}, "breaker_warmup_grace_s", 60.0, 10.0, 600.0
        )
        assert mgr._breaker_warmup_grace_s == 60.0
        assert any("breaker_warmup_grace_s" in rec.message for rec in caplog.records)


# ── A2 : TTL fast-pin progressif ────────────────────────────────────


def _host_ttl_mgr(cfg):
    mgr = object.__new__(vm.VPNManager)
    mgr._config = cfg
    return mgr


class TestHostTTL:
    def test_base_bad_ttl_minutes_effectif(self):
        mgr = _host_ttl_mgr({"bad_ttl": 10})
        # première défaillance → bad_ttl * 60 (10 min → 600 s)
        ttl = manager_host_ttl(mgr, {"failures": 0})
        assert ttl == 600.0

    def test_progression_col_2(self):
        mgr = _host_ttl_mgr({"bad_ttl": 10, "bad_ttl_factor": 2})
        # 1ʳᵉ défaillance → base ; puis ×factor à chaque re-échec
        assert manager_host_ttl(mgr, {"failures": 1}) == 10 * 60
        assert manager_host_ttl(mgr, {"failures": 2}) == 10 * 60 * 2
        assert manager_host_ttl(mgr, {"failures": 3}) == 10 * 60 * (2**2)

    def test_plafond_bad_ttl_max(self):
        mgr = _host_ttl_mgr({"bad_ttl": 10, "bad_ttl_factor": 4, "bad_ttl_max": 30})
        ttl = manager_host_ttl(mgr, {"failures": 10})
        assert ttl == 30 * 60  # clamp au plafond

    def test_fallback_defaut_24h(self):
        mgr = _host_ttl_mgr({})  # pas de bad_ttl → historique 24 h
        ttl = manager_host_ttl(mgr, {"failures": 1})
        assert ttl == 24 * 3600

    def test_bad_ttl_invalide_lexNone(self):
        mgr = _host_ttl_mgr({"bad_ttl": -1})
        ttl = manager_host_ttl(mgr, {"failures": 1})
        assert ttl == 60.0  # clampé à la borne min 1 min, avec warning


def manager_host_ttl(mgr, entry):
    """Wrappe la fonction module _host_ttl_seconds(failures, cfg)."""
    return vm._host_ttl_seconds(int(entry.get("failures", 0)), mgr._config)


# ── A4 : storm gate rotations ───────────────────────────────────────


class TestRotationStorm:
    def _mgr(self):
        mgr = object.__new__(vm.VPNManager)
        mgr._rotation_history = []
        mgr._max_rotations_per_hour = 10
        mgr._rotation_storm_cooldown_s = 600.0
        mgr._storm_cooldown_until = 0.0
        mgr._station = 3
        return mgr

    def test_sous_le_seuil_le_gate_ne_bloque_pas(self):
        mgr = self._mgr()
        for _ in range(10):
            mgr._check_rotation_storm()
        assert mgr._storm_cooldown_until == 0.0
        assert len(mgr._rotation_history) == 10

    def test_onzieme_declenche_cooldown(self):
        mgr = self._mgr()
        for _ in range(10):
            mgr._check_rotation_storm()
        with pytest.raises(vm.RotationFailed, match="rotation storm"):
            mgr._check_rotation_storm()
        assert mgr._storm_cooldown_until > time.monotonic()

    def test_refus_explicite_pendant_cooldown(self):
        mgr = self._mgr()
        mgr._storm_cooldown_until = time.monotonic() + 120
        with pytest.raises(vm.RotationFailed, match="cooldown"):
            mgr._check_rotation_storm()

    def test_fenetre_coulee_purge_les_vieux(self):
        mgr = self._mgr()
        mgr._rotation_history = [time.monotonic() - 3700.0] * 11
        mgr._check_rotation_storm()  # purge + 1 → sous seuil
        assert len(mgr._rotation_history) == 1
        assert mgr._storm_cooldown_until == 0.0

    def test_seuil_clamp_1_720(self):
        # même clamp utilisé en __init__/update_config
        for raw, expected in ((0, 1), (1000, 720)):
            v = int(vm._clamp_cfg_number(
                {"station_max_rotations_per_hour": raw},
                "station_max_rotations_per_hour", 10.0, 1.0, 720.0
            ))
            assert v == expected

    def test_cooldown_clamp_30_7200(self):
        for raw, expected in ((10, 30.0), (99999.0, 7200.0)):
            v = vm._clamp_cfg_number(
                {"rotation_storm_cooldown_s": raw},
                "rotation_storm_cooldown_s", 600.0, 30.0, 7200.0
            )
            assert v == expected


# ── Lot « constantes → config » : free_ip_pool (B-transverse) ───────


class _DictCfg:
    """vpn manager minimal pour amorcer FreeIPPool sans docker."""

    def __init__(self, cfg):
        self._config = cfg
        self._station = 1
        self.station_id = 1


class TestPoolStationKeys:
    def test_station_bad_ttl_s_prenant_sur_bad_ttl(self, tmp_path):
        from free_ip_pool import FreeIPPool

        pool = FreeIPPool.__new__(FreeIPPool)
        # les class defaults ; le handler sur dict fait ici office d'init minimum
        pool._connect_retry_interval = float(FreeIPPool._CONNECT_RETRY_INTERVAL)
        pool._bad_ttl = float(FreeIPPool._BAD_TTL)
        pool.update_config({"station_bad_ttl_s": 120, "bad_ttl": 10})
        assert pool._bad_ttl == 120.0  # clé dédiée gagne malgré bad_ttl legacy
        pool.update_config({"bad_ttl": 90})
        assert pool._bad_ttl == 90.0  # sans clé dédiée, legacy périmètre

    def test_station_connect_retry_interval_clampe(self, tmp_path):
        from free_ip_pool import FreeIPPool

        pool = FreeIPPool.__new__(FreeIPPool)
        pool._connect_retry_interval = 300.0
        pool._bad_ttl = 60.0
        pool.update_config({"station_connect_retry_interval_s": 99999})
        assert pool._connect_retry_interval == 3600.0  # clamp
        pool.update_config({"station_connect_retry_interval_s": "nan"})
        assert pool._connect_retry_interval == 3600.0  # inchangé

    def test_seed_depuis_manager_config(self):
        from free_ip_pool import FreeIPPool

        pool = FreeIPPool.__new__(FreeIPPool)
        pool._connect_retry_interval = 5.0
        pool._bad_ttl = 5.0
        from free_ip_pool import _clamp_seconds

        cfg = {"station_bad_ttl_s": 200, "station_connect_retry_interval_s": 400}
        v = _clamp_seconds(cfg, "station_bad_ttl_s", 1.0, 3600.0)
        assert v == 200.0
        v = _clamp_seconds(cfg, "station_connect_retry_interval_s", 5.0, 3600.0)
        assert v == 400.0


# ── B3 : /health allégé — usage caché sans detail=full ───────────────


class TestHealthDetail:
    def test_default_ne_serialise_pas_usage(self):
        import opencode as oc
        from fastapi.testclient import TestClient

        oc._token_usage["test-health-model"] = {"input": 1, "output": 2, "cache": 0}
        client = TestClient(oc.app)
        try:
            r = client.get("/health")
            assert r.status_code == 200
            assert r.json().get("usage") == {}
            r2 = client.get("/health?detail=full")
            assert r2.json().get("usage", {}).get("test-health-model") == {
                "input": 1, "output": 2, "cache": 0
            }
        finally:
            oc._token_usage.pop("test-health-model", None)

    def test_detail_case_insensitive(self):
        from opencode import app
        from fastapi.testclient import TestClient

        import opencode as oc

        oc._token_usage["test-health-case"] = {"input": 3, "output": 4, "cache": 5}
        client = TestClient(app)
        try:
            r = client.get("/health?detail=FULL")
            assert r.status_code == 200
            assert r.json().get("usage", {}).get("test-health-case") is not None
        finally:
            oc._token_usage.pop("test-health-case", None)


# ── B2 : debug.log — un seul écrivain fichier par logger ────────────


class TestRichLogDedupe:
    def test_pas_de_doublon_quand_logger_a_deja_filehandler(self, tmp_path):
        from dashboard import display

        logfile = tmp_path / "debug_test.log"
        display.set_debug_log_file(str(logfile))
        try:
            lg = logging.getLogger("test_dedupe_logger")
            for h in list(lg.handlers):
                lg.removeHandler(h)
            # attach_module_logger : file handler sur debug.log
            display.attach_module_logger("test_dedupe_logger")
            # attach_panel_logger : au moins un RichLogHandler côté panel
            rh = display.RichLogHandler()
            display.attach_panel_logger("test_dedupe_logger", rh)

            nb = display._logger_writes_debug_file("test_dedupe_logger")
            assert nb is True, "la sonde doit voir le FileHandler attaché"
        finally:
            lg.handlers.clear()

    def test_panel_seul_si_pas_de_filehandler(self, tmp_path):
        from dashboard import display

        logfile = tmp_path / "debug2.log"
        display.set_debug_log_file(str(logfile))
        try:
            assert display._logger_writes_debug_file("unattached_logger_xyz") is False
        finally:
            pass


# ── B1 : purge hebdo app.db (> N jours) ─────────────────────────────


class TestWeeklyPurge:
    def _db(self, tmp_path, rows):
        import sqlite3

        db_path = tmp_path / "w.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE requests (id TEXT PRIMARY KEY, timestamp TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE free_model_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL)"
        )
        for i, ts in rows:
            conn.execute("INSERT INTO requests(id, timestamp) VALUES (?, ?)", (f"r{i}", ts))
            conn.execute("INSERT INTO free_model_usage(timestamp) VALUES (?)", (ts,))
        conn.commit()
        return conn

    def test_purge_90_jours_masque_ancien_conserve_recent(self, tmp_path):
        import datetime as _dt

        n = _dt.datetime.now(_dt.timezone.utc)
        recent = (n - _dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old = (n - _dt.timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = self._db(tmp_path, [(1, recent), (2, old), (3, recent)])

        purged = _app_db._purge_old_rows_locked(conn, days=90)
        assert purged == 2, f"2 lignes (1 par table) attendues hors de 90 jours, eu {purged}"
        rem = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        assert rem == 2

    def test_purge_0_desactivee(self, tmp_path):
        import datetime as _dt

        ts = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=400)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn = self._db(tmp_path, [(1, ts)])
        purged = _app_db._purge_old_rows_locked(conn, days=0)
        assert purged == 0
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1

    def test_tolerance_db_sans_free_model_usage(self, tmp_path):
        import datetime as _dt
        import sqlite3

        old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=365)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE requests (id TEXT, timestamp TEXT)")
        conn.execute("INSERT INTO requests VALUES ('x', ?)", (old,))
        conn.commit()
        # pas de table free_model_usage → ne doit pas lever
        purged = _app_db._purge_old_rows_locked(conn, days=90)
        assert purged == 1
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


# ── supervisor.warmup_excluded_requests (transverse) ────────────────


class TestSupervisorWarmupKey:
    def test_defaut_1(self):
        assert station_supervisor.WARMUP_EXCLUDED_REQUESTS == 1

    def test_fonction_replique_config_mirror(self, tmp_path, monkeypatch):
        # Pas de fichier config.yaml en test : le helper doit rester stable
        # sur le défaut 1 (comportement historique).
        val = station_supervisor.warmup_excluded_requests()
        assert isinstance(val, int)
        # hors bornes impossible en l'absence de config raw → plage 0-1000 aussi
        assert 0 <= val <= 1000