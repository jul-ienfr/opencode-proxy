"""V8.3 — latence par jambe et par modele, sans espace de cles non borne.

**Le besoin.** Savoir que la jambe free a un p50 de 4,5 s ne dit pas *quel
modele* est lent, ni si un modele precis concentre les 403. Ces series-la se
construisent par (jambe, modele, statut).

**Le risque que ces tests verrouillent.** Un nom de modele vient du client. Une
serie par modele sans borne, c'est une fuite memoire pilotee de l'exterieur. D'ou
le plafond `max_keys` : au-dela, les modeles excedentaires partagent une serie
`other`. On perd du detail sur les modeles rares, on garde le service.

**Le choix de branchement.** L'observation est posee dans `_log_free_model_usage`,
seul point de passage qui recoit a la fois le modele, le statut et la duree.
L'instrumenter chez les appelants aurait duplique la logique quatre fois.
"""

import threading

import pytest

from observability.metrics import (
    LEG_LAT_MAX_KEYS,
    latency_snapshot,
    leg_latency_key,
    observe_leg_call,
)


@pytest.fixture
def rings():
    return {}


@pytest.fixture
def lock():
    return threading.Lock()


class TestKey:
    def test_shape(self):
        assert leg_latency_key("free", "big-pickle") == "leg_ms:free:big-pickle"

    def test_leg_is_lowercased(self):
        assert leg_latency_key("FREE", "m") == "leg_ms:free:m"

    def test_missing_parts_are_tolerated(self):
        assert leg_latency_key(None, None) == "leg_ms:?:?"
        assert leg_latency_key("", "") == "leg_ms:?:?"

    def test_long_model_names_are_truncated(self):
        key = leg_latency_key("free", "m" * 500)
        assert len(key) <= len("leg_ms:free:") + 64


class TestObservation:
    def test_a_call_creates_its_series(self, rings, lock):
        key = observe_leg_call(rings, lock, "free", "big-pickle", 1234.0, 200)
        assert key == "leg_ms:free:big-pickle"
        assert len(rings[key]) == 1

    def test_repeated_calls_accumulate(self, rings, lock):
        for value in (100.0, 200.0, 300.0):
            observe_leg_call(rings, lock, "free", "big-pickle", value, 200)
        snapshot = latency_snapshot(rings, lock)
        n, p50, p95, _p99 = snapshot["leg_ms:free:big-pickle"]
        assert n == 3
        assert 100.0 <= p50 <= 300.0
        assert p95 >= p50

    def test_legs_and_models_stay_separate(self, rings, lock):
        observe_leg_call(rings, lock, "free", "big-pickle", 10.0, 200)
        observe_leg_call(rings, lock, "paid", "big-pickle", 20.0, 200)
        assert set(rings) == {"leg_ms:free:big-pickle", "leg_ms:paid:big-pickle"}

    def test_status_counter_is_bumped(self, rings, lock):
        counters = {}
        observe_leg_call(rings, lock, "free", "m", 5.0, 429, counters=counters)
        assert counters == {"leg_status:free:429": 1}


class TestBounded:
    def test_overflow_models_share_the_other_series(self, rings, lock):
        observe_leg_call(rings, lock, "free", "a", 1.0, 200, max_keys=2)
        observe_leg_call(rings, lock, "free", "b", 2.0, 200, max_keys=2)
        key = observe_leg_call(rings, lock, "free", "c", 3.0, 200, max_keys=2)
        assert key == "leg_ms:free:other", "un modele excedentaire doit etre agrege"
        assert "leg_ms:free:c" not in rings
        assert set(rings) == {"leg_ms:free:a", "leg_ms:free:b", "leg_ms:free:other"}

    def test_an_existing_series_keeps_growing_after_the_cap(self, rings, lock):
        observe_leg_call(rings, lock, "free", "a", 1.0, 200, max_keys=1)
        observe_leg_call(rings, lock, "free", "b", 2.0, 200, max_keys=1)
        observe_leg_call(rings, lock, "free", "a", 3.0, 200, max_keys=1)
        assert len(rings["leg_ms:free:a"]) == 2

    def test_the_default_cap_is_reasonable(self):
        assert 10 <= LEG_LAT_MAX_KEYS <= 10_000


class TestFailSoft:
    def test_no_counters_is_fine(self, rings, lock):
        assert observe_leg_call(rings, lock, "free", "m", 1.0, 200) is not None

    def test_bad_inputs_never_raise(self, rings, lock):
        assert observe_leg_call(rings, lock, "free", "m", "pas-un-nombre", 200) is None or rings
        observe_leg_call(rings, lock, None, None, 1.0, None)
        observe_leg_call({}, lock, "free", "m", 1.0, 200, max_keys=0)


class TestExposure:
    def test_health_exposes_latency_by_leg(self):
        import inspect

        import opencode as oc

        source = inspect.getsource(oc.health)
        assert "latency_by_leg" in source

    def test_the_series_prefix_is_the_one_health_filters_on(self):
        """Le filtre de /health doit correspondre au prefixe reellement ecrit."""
        import inspect

        import opencode as oc

        assert "leg_ms:" in inspect.getsource(oc.health)
        assert leg_latency_key("free", "x").startswith("leg_ms:")
