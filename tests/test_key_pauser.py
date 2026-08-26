"""[plan v10 §4 Lot 0 / §9.3.14] Filet mock pur pour `_KeyPauser`.

Classe critique (pause des clés API sur 429/401/403, persistance
logs/paused_keys.yaml) jusqu'ici sans AUCUN test dédié. Horloge contrôlée
(monkeypatch time.monotonic/time.time du module opencode) → déterministe,
zéro sleep. Fichier de persistance redirigé vers tmp_path.
"""


import pytest

import opencode as oc


class FakeClock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = 1_000_000.0

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall

    def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds


@pytest.fixture
def clock(monkeypatch, tmp_path):
    clk = FakeClock()
    monkeypatch.setattr(oc.time, "monotonic", clk.monotonic)
    monkeypatch.setattr(oc.time, "time", clk.time)
    kp = oc._KeyPauser(max_pause=600)
    kp._PAUSED_FILE = str(tmp_path / "paused_keys.yaml")
    return clk, kp


def test_pause_then_expire_purges_entry(clock):
    clk, kp = clock
    key = "sk-ant-api03-aaaaaaaaaaaa1234"
    kp.pause_key(key, 60, "429")
    assert kp.is_paused(key) is True
    assert kp.remaining(key) == pytest.approx(60, abs=0.5)
    clk.advance(61)
    assert kp.is_paused(key) is False
    assert kp.remaining(key) == 0.0
    assert kp.get_all_status() == {}, "entrée expirée purgée au statut"


def test_quota_based_capped_explicit_not_capped(clock):
    clk, kp = clock
    long_key = "sk-ant-api03-capcapcapcap9999"
    kp.pause_key(long_key, 99_999, "quota estimate wrong", quota_based=True)
    assert kp.remaining(long_key) <= kp._max_pause, "pause quota plafonnée à max_pause"
    revoked = "sk-ant-api03-revrevrevrev0001"
    kp.pause_key(revoked, 86_400, "401 revoked", quota_based=False)
    assert kp.remaining(revoked) == pytest.approx(86_400, abs=0.5), (
        "durée explicite 401 honorée en entier (clé révoquée ne récupère pas)"
    )


def test_only_extend_never_shorten(clock):
    clk, kp = clock
    key = "sk-ant-api03-extextextext77"
    kp.pause_key(key, 100, "first")
    kp.pause_key(key, 10, "shorter retry-after")
    assert kp.remaining(key) == pytest.approx(100, abs=0.5)


def test_full_key_identity_no_provider_prefix_collision(clock):
    """[bug réel trouvé par le filet] l'ancien préfixe api_key[:12] fusionnait
    toutes les clés « sk-ant-api03-… » (12 premiers chars = préfixe fournisseur)
    → une pause s'appliquait à TOUTES les clés Anthropic. Désormais : slot
    unique par clé complète, même avec un long préfixe commun."""
    clk, kp = clock
    a = "sk-ant-api03-SHAREDSUFFIX-x1"
    b = "sk-ant-api03-SHAREDSUFFIX-x2"
    kp.pause_key(a, 60, "429")
    assert kp.is_paused(b) is False, "clé distincte = slot distinct"
    assert kp.is_paused(a) is True
    assert kp.is_paused("") is False, "clé vide → slot dédié, jamais collée aux autres"


def test_best_available_none_when_any_free(clock):
    clk, kp = clock
    paused = {"api_key": "sk-ant-api03-pausedpaus11"}
    free = {"api_key": "sk-ant-api03-freefreef22"}
    kp.pause_key(paused["api_key"], 60, "429")
    assert kp.best_available([paused, free]) is None, "une clé dispo → sélection normale"


def test_best_available_shortest_pause_when_all_paused(clock):
    clk, kp = clock
    short = {"api_key": "sk-ant-api03-shortshort1"}
    long = {"api_key": "sk-ant-api03-longelonge2"}
    kp.pause_key(short["api_key"], 30, "429")
    kp.pause_key(long["api_key"], 600, "401")
    best = kp.best_available([long, short])
    assert best is not None and best["api_key"] == short["api_key"]


def test_persist_roundtrip_survives_new_instance(clock, tmp_path):
    clk, kp = clock
    key = "sk-ant-api03-survisurvis33"
    kp.pause_key(key, 120, "429 persisted")
    # _save est synchrone hors event loop (RuntimeError → fallback direct)
    assert (tmp_path / "paused_keys.yaml").exists()

    kp2 = oc._KeyPauser(max_pause=600)
    kp2._PAUSED_FILE = kp._PAUSED_FILE
    kp2.load([])
    assert kp2.is_paused(key) is True
    assert kp2.remaining(key) == pytest.approx(120, abs=0.5)

    # entrée déjà expirée sur disque → silencieusement ignorée au load
    clk.advance(500)
    kp.pause_key("sk-ant-api03-freshfresh44", 60, "r")  # re-save avec la fraîche
    kp3 = oc._KeyPauser(max_pause=600)
    kp3._PAUSED_FILE = kp._PAUSED_FILE
    kp3.load([])
    assert kp3.is_paused(key) is False, "expirée sur disque → drop"


def test_load_missing_file_is_noop(clock, tmp_path):
    clk, kp = clock
    kp._PAUSED_FILE = str(tmp_path / "inexistant.yaml")
    kp.load([])  # ne doit pas lever
    assert kp.get_all_status() == {}


def test_unpause_if_paused(clock):
    clk, kp = clock
    key = "sk-ant-api03-unpauunpau55"
    kp.pause_key(key, 60, "429")
    assert kp.unpause_if_paused(key) is True
    assert kp.is_paused(key) is False
    assert kp.unpause_if_paused(key) is False, "double unpause → False"


def test_cleanup_expired_keeps_active(clock):
    clk, kp = clock
    k1 = "sk-ant-api03-cleanaclean66"
    k2 = "sk-ant-api03-cleanclean77"
    kp.pause_key(k1, 10, "court")
    kp.pause_key(k2, 300, "long")
    clk.advance(11)
    kp.cleanup_expired()
    assert kp.is_paused(k1) is False
    assert kp.is_paused(k2) is True


def test_module_singleton_exists():
    assert isinstance(oc._key_pauser, oc._KeyPauser), (
        "le singleton global utilisé par les endpoints /api/key-pauses* doit rester une instance"
    )
