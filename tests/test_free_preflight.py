"""Préflight de quota : refuser en vrai 429 AVANT d'ouvrir le flux SSE.

Contexte mesuré le 2026-10-25 : les en-têtes SSE étant déjà partis, un refus
de quota arrivait en HTTP 200 + ``data: {"error":…}`` et le client affichait
du JSON brut. Le préflight ne conclut que dans le cas non ambigu : pool chaud,
strict_free, aucune rotation en vol, tous les couples (modèle, station) en
cooldown avec un délai restant significatif.
"""

import pytest

import opencode as oc


class _Station:
    """Station minimale : le cooldown est clefé sur `current_ip`."""

    def __init__(self, ip):
        self.current_ip = ip
        self.pid = None


S1 = _Station("10.0.0.1")
S2 = _Station("10.0.0.2")


class _Pool:
    def __init__(self, stations=(S1, S2), usable=True, rotating=False):
        self.enabled = True
        self._stations = list(stations)
        self._usable = usable
        self._rotating = rotating

    def _station_usable(self, st, exclude_approaching=False):
        return self._usable

    def any_rotation_in_flight(self):
        return self._rotating


MODEL = "deepseek-v4.1-flash-free"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    oc._free_model_cooldowns.clear()
    oc._free_cooldown_scope.clear()
    monkeypatch.setitem(oc.IP_ROTATION, "strict_free", True)
    monkeypatch.setattr(oc, "_free_candidate_chain", lambda *a, **k: [MODEL])
    yield
    oc._free_model_cooldowns.clear()
    oc._free_cooldown_scope.clear()


def _pool(monkeypatch, **kw):
    p = _Pool(**kw)
    monkeypatch.setattr(oc, "_free_ip_pool", p)
    return p


def test_strict_free_desactive_ne_conclut_pas(monkeypatch):
    _pool(monkeypatch)
    monkeypatch.setitem(oc.IP_ROTATION, "strict_free", False)
    oc._set_free_cooldown(MODEL, 60, S1)
    oc._set_free_cooldown(MODEL, 60, S2)
    assert oc._free_preflight_retry_after(MODEL) is None


def test_pool_absent_ne_conclut_pas(monkeypatch):
    monkeypatch.setattr(oc, "_free_ip_pool", None)
    assert oc._free_preflight_retry_after(MODEL) is None


def test_aucune_station_chaude_ne_conclut_pas(monkeypatch):
    """Warmup/erreur : le chemin normal doit répondre 503, pas 429."""
    _pool(monkeypatch, usable=False)
    oc._set_free_cooldown(MODEL, 60, S1)
    assert oc._free_preflight_retry_after(MODEL) is None


def test_rotation_en_vol_ne_conclut_pas(monkeypatch):
    _pool(monkeypatch, rotating=True)
    oc._set_free_cooldown(MODEL, 60, S1)
    oc._set_free_cooldown(MODEL, 60, S2)
    assert oc._free_preflight_retry_after(MODEL) is None


def test_une_station_libre_suffit(monkeypatch):
    _pool(monkeypatch)
    oc._set_free_cooldown(MODEL, 60, S1)
    assert oc._free_preflight_retry_after(MODEL) is None


def test_tout_en_cooldown_donne_un_delai(monkeypatch):
    _pool(monkeypatch)
    oc._set_free_cooldown(MODEL, 60, S1)
    oc._set_free_cooldown(MODEL, 90, S2)
    wait = oc._free_preflight_retry_after(MODEL)
    assert wait is not None
    assert 50 <= wait <= 60


def test_cooldown_qui_expire_bientot_laisse_tenter(monkeypatch):
    _pool(monkeypatch)
    oc._set_free_cooldown(MODEL, 1, S1)
    oc._set_free_cooldown(MODEL, 1, S2)
    assert oc._free_preflight_retry_after(MODEL) is None


def test_modele_paye_non_free_ne_declenche_rien(monkeypatch):
    _pool(monkeypatch)
    monkeypatch.setattr(oc, "_is_free_model_route", lambda m: False)
    monkeypatch.setattr(oc, "FREE_MODEL_MAP", {})
    assert oc._free_preflight_retry_after("claude-sonnet-4-5") is None


def test_reponse_429_avec_retry_after(monkeypatch):
    r = oc._free_preflight_response(42.0, anthropic=False)
    assert r.status_code == 429
    assert r.headers.get("retry-after") == "42"
    a = oc._free_preflight_response(42.0, anthropic=True)
    assert a.status_code == 429


def test_http_stream_refuse_en_vrai_429(monkeypatch):
    """Bout en bout : un client streaming reçoit 429 + Retry-After, pas un
    200 avec un JSON d'erreur dans le flux."""
    from fastapi.testclient import TestClient

    _pool(monkeypatch)
    oc._set_free_cooldown(MODEL, 60, S1)
    oc._set_free_cooldown(MODEL, 60, S2)

    client = TestClient(oc.app)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "stream": True,
            "messages": [{"role": "user", "content": "dis ok"}],
        },
    )
    assert r.status_code == 429, r.text[:200]
    assert "quota" in r.text.lower()
    assert r.headers.get("retry-after")
    assert "text/event-stream" not in (r.headers.get("content-type") or "")
