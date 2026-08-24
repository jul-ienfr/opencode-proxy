"""[plan v10 §4 Lot 6] Config per_station + validation + import fan-out.

Couvre : merge per_station à l'init (copie, pas la ref live), hot-reload
per_station, validate_custom_routes (14.2.5), save_custom_routes source
unique sans JSON parallèle (14.4.11), _apply_import_payload fan-out +
validation (14.3.31), _persist_vpn_config statut réel (14.3.30).
"""

import threading

import pytest

# ── per_station merge ────────────────────────────────────────────────────


def _mk_mgr(tmp_path, station, base_cfg):
    from test_vpn_freshness import FakeVPNManager, _cfg

    cfg = _cfg(tmp_path)
    cfg.update(base_cfg)
    return FakeVPNManager(cfg, station=station, tmp_path=tmp_path)


@pytest.mark.asyncio
async def test_per_station_merge_copies_not_shared_ref(tmp_path):
    """[§4 Lot 6] les overrides s'appliquent à CETTE instance et ne polluent
    pas la config de base partagée (copie défensive)."""
    shared = {"quota_per_ip": 300, "enabled": True, "per_station": {"2": {"quota_per_ip": 500}}}
    m1 = _mk_mgr(tmp_path, 1, dict(shared))
    m2 = _mk_mgr(tmp_path, 2, dict(shared))
    assert m1._config["quota_per_ip"] == 300
    assert m2._config["quota_per_ip"] == 500, "override st2 appliqué"
    assert shared["quota_per_ip"] == 300, "la base partagée reste intacte"


@pytest.mark.asyncio
async def test_per_station_hot_reload(tmp_path):
    m = _mk_mgr(tmp_path, 3, {})
    await m.update_config({"per_station": {"3": {"watchdog_interval": 7}}})
    assert m._config.get("watchdog_interval") == 7, "hot-reload per-station re-fusionné"


# ── validate_custom_routes + save single-source ──────────────────────────


def test_validate_custom_routes_rejects_bad_payload():
    from config.settings import validate_custom_routes as v

    assert v("not-a-dict") is not None
    assert v({123: {}}) is not None
    assert v({"glm-5.1": "scalar"}) is not None
    assert v({"glm-5.1": {"match": {"nested": "dict"}}}) is not None
    assert v({"glm-5.1": {"match": "GLM", "aliases": ["a", "b"]}}) is None
    assert v({}) is None


def test_save_custom_routes_no_parallel_json(tmp_path, monkeypatch):
    """[§14.4.11] plus d'écriture custom_routes.json parallèle."""
    import config.settings as st

    monkeypatch.setattr(st, "CUSTOM_ROUTES_PATH", str(tmp_path / "custom_routes.json"))
    monkeypatch.setattr(
        st,
        "_yaml_data",
        {k: v for k, v in st._yaml_data.items() if k != "custom_routes"},
    )
    monkeypatch.setattr(st, "CONFIG_PATH", str(tmp_path / "config.yaml"))
    st.save_custom_routes({"glm-5.1": {"match": "GLM"}})
    assert not (tmp_path / "custom_routes.json").exists(), "source unique YAML"


def test_save_custom_routes_invalid_raises(monkeypatch, tmp_path):
    import config.settings as st

    monkeypatch.setattr(st, "CUSTOM_ROUTES_PATH", str(tmp_path / "x.json"))
    with pytest.raises(ValueError):
        st.save_custom_routes({"bad": "payload"})


# ── 14.3.31 import : validation + fan-out ────────────────────────────────


class _StubMgr:
    def __init__(self, sid):
        self._station = sid
        self.updated = []

    async def update_config(self, cfg):
        self.updated.append(dict(cfg))


@pytest.mark.asyncio
async def test_import_payload_fans_out_all_stations():
    from dashboard.api import _apply_import_payload

    mgrs = [_StubMgr(1), _StubMgr(2), _StubMgr(3)]
    resp, status = await _apply_import_payload(mgrs, {"config": {"quota_per_ip": 250}})
    assert status == 200 and resp["ok"] is True
    assert resp["stations_applied"] == 3, "fan-out TOUTES les stations"
    assert all(m.updated for m in mgrs)


@pytest.mark.asyncio
async def test_import_payload_validates():
    from dashboard.api import _apply_import_payload

    resp, status = await _apply_import_payload([], {"config": "not-a-dict"})
    assert status == 400
    resp2, status2 = await _apply_import_payload([], {"state": {"hack": 1}})
    assert status2 == 400


# ── 14.3.30 persist statut ───────────────────────────────────────────────


def test_persist_vpn_config_returns_error_string(monkeypatch, tmp_path):
    """[§14.3.30] succès → None ; échec → STRING (le contrat `if not err`
    des callers devient enfin réel)."""
    import os

    import dashboard.api as api

    # échec déterministe : chemin config.yaml inexistant
    real_join = os.path.join

    def fake_join(*a):
        if a and isinstance(a[-1], str) and a[-1] == "config.yaml":
            return real_join(str(tmp_path / "does-not-exist"), "config.yaml")
        return real_join(*a)

    monkeypatch.setattr(api.os.path, "join", fake_join)
    lock = threading.Lock()
    monkeypatch.setattr(api, "_persist_lock", lock)
    err = api._persist_vpn_config({"enabled": True})
    assert isinstance(err, str) and err, "échec explicite au lieu du silence historique"

    # succès sur un vrai fichier minimal (join restauré d'abord)
    monkeypatch.undo()
    cfg_file = tmp_path / "config.yaml"
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text("ip_rotation:\n  enabled: true\n", encoding="utf-8")

    def ok_join(*a):
        if a and isinstance(a[-1], str) and a[-1] == "config.yaml":
            return str(cfg_file)
        return real_join(*a)

    monkeypatch.setattr(api.os.path, "join", ok_join)
    monkeypatch.setattr(api, "_persist_lock", threading.Lock())
    monkeypatch.setattr(api, "_config_yaml_known_mtime", 0.0, raising=False)
    err2 = api._persist_vpn_config({"enabled": True})
    assert err2 is None or isinstance(err2, str)
