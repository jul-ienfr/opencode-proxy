"""Session gateway Zen (`x-opencode-session`) — correctif incident 400
MissingSessionID (2026-09-07).

La gateway substitue ce header dans les requêtes provider (`$session`) ;
absent -> 400 sur TOUS les modèles free (vérifié : anonymous, cookie
workspace, clé paid -> 400 ; uuid quelconque -> 200). UUID stable par
instance (persisté), pas un secret d'account.

Hermétique : aucune I/O réseau (le header est local), fichier de session
redirigé vers tmp_path.
"""

import re

import pytest

import opencode as oc


@pytest.fixture
def _iso_session(monkeypatch, tmp_path):
    monkeypatch.setattr(oc, "_PROXY_SESSION_FILE", str(tmp_path / "_proxy_session_id"))
    monkeypatch.setattr(oc, "_proxy_session_id_cache", None)
    return oc


def _profile():
    return {"impersonate": "chrome131", "user_agent": None, "extra_headers": {}}


def test_session_id_stable_et_persiste(_iso_session):
    m = _iso_session
    a = m._proxy_session_id()
    b = m._proxy_session_id()
    assert a == b, "stable en mémoire"
    assert re.fullmatch(r"[0-9a-fA-F-]{36}", a), "format UUID"
    # relecture disque après reset du cache (= reboot proxy)
    m._proxy_session_id_cache = None
    assert m._proxy_session_id() == a, "stable après reboot (fichier)"


def test_session_id_regenere_si_corrompu(_iso_session, tmp_path):
    m = _iso_session
    (tmp_path / "_proxy_session_id").write_text("pas-un-uuid!!!", encoding="utf-8")
    m._proxy_session_id_cache = None
    fresh = m._proxy_session_id()
    assert re.fullmatch(r"[0-9a-fA-F-]{36}", fresh)


def test_apply_identity_injecte_session(_iso_session):
    m = _iso_session
    out = m._apply_identity({"Content-Type": "application/json"}, _profile())
    assert re.fullmatch(r"[0-9a-fA-F-]{36}", out.get("x-opencode-session", ""))


def test_apply_identity_necrase_pas_session_existante(_iso_session):
    m = _iso_session
    out = m._apply_identity(
        {"x-opencode-session": "sess-existante", "Content-Type": "application/json"},
        _profile(),
    )
    assert out["x-opencode-session"] == "sess-existante"


def test_apply_identity_casse_insensible(_iso_session):
    m = _iso_session
    out = m._apply_identity(
        {"X-Opencode-Session": "s", "Content-Type": "application/json"},
        _profile(),
    )
    assert out["X-Opencode-Session"] == "s"
    assert "x-opencode-session" not in out, "pas de doublon de casse"
