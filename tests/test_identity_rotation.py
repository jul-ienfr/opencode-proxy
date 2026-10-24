"""test_identity_rotation.py — changer d'impersonnalisation sans changer d'IP.

Quota épuisé → nouvelle installation (version/UA/ses_/project) sur la MÊME
IP, zéro AUTH NordVPN. Si le bucket amont inclut la face client, la
prochaine tentative passe ; sinon la rotation d'IP (bras armé en parallèle)
prend le relais.
"""

import pytest

from free.identities import IdentityVault, IpIdentity


def _vault(tmp_path):
    import free.identities as _fi

    _fi._VAULT = None
    import os

    os.environ["OPENCODE_IDENTITIES_DIR"] = str(tmp_path / "ids")
    v = IdentityVault(base_project="global")
    yield v
    _fi._VAULT = None
    os.environ.pop("OPENCODE_IDENTITIES_DIR", None)


import contextlib


@contextlib.contextmanager
def _vault_ctx(tmp_path):
    import free.identities as _fi
    import os

    _fi._VAULT = None
    os.environ["OPENCODE_IDENTITIES_DIR"] = str(tmp_path / "ids")
    try:
        yield IdentityVault(base_project="global")
    finally:
        _fi._VAULT = None
        os.environ.pop("OPENCODE_IDENTITIES_DIR", None)


def test_rotate_changes_face_same_ip(tmp_path):
    with _vault_ctx(tmp_path) as v:
        before = v.get("9.9.9.9").snapshot()
        after = v.rotate_installation("9.9.9.9")
        assert after["ip"] == "9.9.9.9"
        assert after["generation"] == 1
        assert after["ses"] != before["ses"], "nouveau ses_"
        # Pool mesuré à 2 versions / 2 clients : gen+1 bascule forcément.
        assert (after["version"], after["client"]) != (before["version"], before["client"])
        again = v.rotate_installation("9.9.9.9")
        assert again["generation"] == 2
        assert again["ses"] != after["ses"]


def test_rotate_persists_and_reloads(tmp_path):
    with _vault_ctx(tmp_path) as v:
        v.rotate_installation("9.9.9.9")
        snap = v.get("9.9.9.9").snapshot()
    # Nouveau vault (même dossier) : la face tournée est stable.
    with _vault_ctx(tmp_path) as v2:
        assert v2.get("9.9.9.9").snapshot()["ses"] == snap["ses"]


def test_rotate_forced_version_respected_but_ses_fresh(tmp_path):
    with _vault_ctx(tmp_path) as v:
        import free.identities as _fi

        _fi._VAULT = None
        v = IdentityVault(base_project="global", forced_version="1.19.3")
        before = v.get("9.9.9.9").snapshot()
        after = v.rotate_installation("9.9.9.9")
        assert after["version"] == "1.19.3"
        assert after["ses"] != before["ses"]


def test_rotate_never_raises_on_garbage(tmp_path):
    with _vault_ctx(tmp_path) as v:
        assert v.rotate_installation("")["ip"] == "direct"
        assert v.rotate_installation(None)["ip"] == "direct"


def test_hook_rotates_vault_on_quota_not_on_transient(monkeypatch):
    """_mark_free_stations_429 : quota → nouvelle installation ; transitoire → rien."""
    import opencode as oc
    from test_free_multi_attempt import _Station

    oc._free_model_cooldowns.clear()
    try:
        oc._free_cooldown_scope.clear()
    except Exception:
        pass

    st = _Station(1, "9.9.9.9", "socks5://127.0.0.1:1080")

    class _Pool:
        enabled = True
        _stations = [st]

        def __init__(self):
            self.quota_calls = []

        def on_quota_exhausted(self, station, forced_pool=None, quota=True):
            self.quota_calls.append((station, quota))

    pool = _Pool()
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    monkeypatch.setattr(oc, "_record_global_429", lambda: None)
    monkeypatch.setattr(oc, "_note_free_429", lambda m: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)

    import free.identities as _fi

    before = oc._ip_vault().get("9.9.9.9").snapshot()

    quota_body = '{"error":{"message":"usage_not_included: plan does not include this usage"}}'
    oc._mark_free_stations_429("m", "", stations=[st], body=quota_body)

    after = oc._ip_vault().get("9.9.9.9").snapshot()
    assert after["ses"] != before["ses"], "quota → nouvelle installation, même IP"
    assert after["generation"] == 1
    assert pool.quota_calls and pool.quota_calls[0][1] is True, "rotation d'IP toujours armée en parallèle"

    transient_body = '{"error":{"type":"server_error","message":"Endpoint is unavailable. Please try again later."}}'
    oc._mark_free_stations_429("m", "", stations=[st], body=transient_body)
    assert oc._ip_vault().get("9.9.9.9").snapshot()["ses"] == after["ses"], "transitoire → on ne touche pas à l'identité"


@pytest.mark.asyncio
async def test_switch_ip_mints_virgin_client(tmp_path):
    """Rotation réussie → nouvelle installation vierge sur la nouvelle IP."""
    import asyncio
    import os

    import free.identities as _fi
    import free.pool as _pool_mod

    _fi._VAULT = None
    os.environ["OPENCODE_IDENTITIES_DIR"] = str(tmp_path / "ids3")
    try:
        vault = _fi.IdentityVault(base_project="global")
        before = vault.get("9.9.9.99").snapshot()

        pool = _pool_mod.FreeIPPool.__new__(_pool_mod.FreeIPPool)
        pool._per = {}
        pool._stations = []
        pool._active_station = None
        pool._vpn = None
        pool._rotation_max_duration = 240.0
        pool._switch_delay = 0
        pool._post_rotation_flush_cb = None
        pool._lock = asyncio.Lock()
        pool.latency_engine = None
        pool.save_pool_state = lambda *a, **k: None

        class _Station:
            _station = 1
            current_ip = "1.1.1.1"
            current_server = {"name": "opencode-vpn"}
            status = "connected"

            async def connect_next(self):
                self.current_ip = "9.9.9.99"
                return "9.9.9.99"

        # Le hook utilise le singleton free.identities : on l'y installe.
        _fi._VAULT = vault
        st = _Station()
        new_ip = await pool.switch_ip(st)
        assert new_ip == "9.9.9.99"
        after = vault.get("9.9.9.99").snapshot()
        assert after["generation"] >= 1, "rotation → nouvelle installation"
        assert after["ses"] != before["ses"], "nouveau ses_ vierge"
    finally:
        _fi._VAULT = None
        os.environ.pop("OPENCODE_IDENTITIES_DIR", None)
