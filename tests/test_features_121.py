"""[plan v10 §12.1 + §9.1.2] Features Lot 7 — prédiction, alerte, validate, pin.

Couvre : tendance EWMA prédictive (12.1.1), action `warn` approaching_slow
(12.1.3), endpoint dry-run /api/vpn-config/validate (12.1.4), wrappers
pin/unpin country (12.1.5), persistance ATOMIQUE des clés pausées (§9.1.2).
"""

import pytest
import yaml

import opencode as oc
from ip_latency import IpLatencyTracker
from latency_rotation import COOLDOWN_SOFT, LatencyRotationEngine
from vpn_manager import VPNManager


class FakeClock:
    def __init__(self):
        self.t = 50_000.0

    def monotonic(self):
        return self.t

    def time(self):
        return self.t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def kp_clock(monkeypatch, tmp_path):
    """Horloge contrôlée + _KeyPauser fichier redirigé (cf. test_key_pauser)."""
    clk = FakeClock()
    monkeypatch.setattr(oc.time, "monotonic", clk.monotonic)
    monkeypatch.setattr(oc.time, "time", clk.time)
    kp = oc._KeyPauser(max_pause=600)
    kp._PAUSED_FILE = str(tmp_path / "paused_keys.yaml")
    return clk, kp


@pytest.fixture
def engine(monkeypatch):
    eng = LatencyRotationEngine()
    clock = FakeClock()
    monkeypatch.setattr(eng, "_now", clock)
    return eng


# ── 12.1.1 : rotation prédictive ─────────────────────────────────────────


def test_tracker_trend_pct_math():
    tr = IpLatencyTracker(station=1, ip="1.1.1.1")
    assert tr.trend_pct(5) is None  # pas d'historique
    for d in (1000, 1000, 1000, 1000, 1000, 2000, 3000):
        tr.record(d, slow_threshold_ms=99999)
    t = tr.trend_pct(5)
    assert t is not None and t > 0, "ewma monte -> pente positive"


def test_engine_predictive_soft_before_thresholds(engine):
    engine.cfg.predictive_trend_pct = 30.0
    sid, ip = 1, "10.0.0.1"
    dec = None
    # pente montante rapide mais sous les seuils absolus (floor 3000)
    for d in (1200, 1300, 1500, 1900, 2400, 3100):
        dec = engine.record_request(sid, ip, d, "glm", 200)
        if dec.get("action") == "soft":
            break
    assert dec["action"] == "soft", "pente >30% anticipée avant les seuils"
    assert dec["reason"] == "predictive_trend"
    assert engine.cooldown_kind(sid, ip) == COOLDOWN_SOFT


def test_engine_predictive_disabled_when_zero(engine):
    engine.cfg.predictive_trend_pct = 0.0
    sid, ip = 1, "10.0.0.2"
    last = None
    for d in (1200, 1300, 1500, 1900, 2400, 3100):
        last = engine.record_request(sid, ip, d, "glm", 200)
    assert last["action"] == "none", "prédiction désactivée -> aucune action"


def test_predictive_never_fires_on_decreasing(engine):
    sid, ip = 1, "10.0.0.3"
    last = None
    for d in (3000, 2500, 2200, 2000, 1800, 1600):
        last = engine.record_request(sid, ip, d, "glm", 200)
    assert last["action"] == "none"


# ── 12.1.3 : alerte proactive warn ───────────────────────────────────────


def test_engine_warn_one_slow_before_limit(engine):
    sid, ip = 2, "20.0.0.1"
    decs = [engine.record_request(sid, ip, 15000, "glm", 200) for _ in range(3)]
    # warmup exclut la 1ʳᵉ ; consecutive_slow atteint limit-1 (=2) à la 3ᵉ
    assert decs[0]["action"] == "none" and decs[0]["reason"] == "warmup_excluded"
    assert any(d["action"] == "warn" and d["reason"] == "approaching_slow" for d in decs)


# ── 12.1.4 : validate endpoint (helper) ──────────────────────────────────


def test_validate_payload_ok_and_errors():
    from dashboard.api import _validate_vpn_config_payload as v

    assert v({"station_count": 3}) == []
    assert v({"latency_rotation": {"soft_cooldown_sec": 600, "stream_metric": "ttfb"}}) == []
    errs = v({"station_count": 99})
    assert errs and "station_count" in errs[0]
    errs = v({"latency_rotation": {"slow_cooldown_sec": "abc"}}) or v(
        {"latency_rotation": {"floor_ms": -5}}
    )
    assert errs, "valeur hors bornes détectée"
    errs = v({"per_station": {"11": {"quota_per_ip": 1}}})
    assert errs and "station invalide" in errs[0]
    errs = v({"per_station": {"2": {"unknown_key": 1}}})
    assert errs and "non supportées" in errs[0]
    assert v({}) == []


def test_validate_endpoint_dry_run_does_not_apply():
    """Le validate est un DRY-RUN : la logique d'application n'est JAMAIS
    exécutée par ce chemin (garde structurel sur la source du handler,
    nested dans register_dashboard)."""
    import inspect
    import re

    import dashboard.api as api

    reg_src = inspect.getsource(api.register_dashboard)
    m = re.search(
        r"async def validate_vpn_config.*?(?=\n    @|\Z)", reg_src, re.S
    )
    assert m, "handler validate introuvable"
    block = m.group(0)
    assert "update_config" not in block
    assert "save_yaml_config" not in block
    assert "_apply_import_payload" not in block
    # et le helper de validation reste pur
    assert api._validate_vpn_config_payload({"station_count": 4}) == []


# ── 12.1.5 : pin/unpin country ───────────────────────────────────────────


def _mgr_with_control():
    from vpn_manager import VPNManager

    m = VPNManager.__new__(VPNManager)
    m._control_enabled = True
    m._station = 2
    m._current_country = None
    m._country_pinned_at = None
    m._countries_list = lambda: ["Germany", "France"]
    pinned = {}

    async def fake_pin(country, timeout=30.0):
        pinned["country"] = country
        return country != "Nowhere"

    async def fake_exec(method, path, body=None, timeout=10.0):
        return ["running"]

    m._control_pin_country = fake_pin
    m._control_exec = fake_exec
    return m, pinned


@pytest.mark.asyncio
async def test_pin_country_wrapper():
    m, pinned = _mgr_with_control()
    ok = await VPNManager.pin_country(m, "Japan")
    assert ok is True and pinned["country"] == "Japan"
    ok = await VPNManager.pin_country(m, "Nowhere")
    assert ok is False, "serveur rejetant -> False"


@pytest.mark.asyncio
async def test_unpin_country_restores_full_list():
    m, pinned = _mgr_with_control()
    m._current_country = "Japan"
    ok = await VPNManager.unpin_country(m)
    assert ok is True
    assert m._current_country is None, "état de pin nettoyé"


@pytest.mark.asyncio
async def test_unpin_disabled_control_returns_false():
    m = VPNManager.__new__(VPNManager)
    m._control_enabled = False
    assert await VPNManager.unpin_country(m) is False


# ── §9.1.2 : KeyPauser save atomique ─────────────────────────────────────


def test_key_pauser_save_writes_valid_yaml_no_tmp_left(kp_clock, tmp_path):
    clk, kp = kp_clock
    key = "sk-ant-api03-atomicatomic01"
    kp.pause_key(key, 60, "r")
    path = tmp_path / "paused_keys.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert list(data.get("paused_keys", {})), "fichier YAML valide avec entrée"
    assert not list(tmp_path.glob("*.tmp")), "pas de .tmp résiduel après replace"
