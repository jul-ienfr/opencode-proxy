"""V6.3.4 — le SLO transforme une mesure en verdict, pour ne plus en debattre.

**La situation qui a produit ce module.** Une impression de lenteur a coute
plusieurs heures : il a fallu interroger la base, faire des appels, comparer des
distributions. La mesure existait deja ; personne ne la comparait a une cible.

**Ce que ces tests verrouillent, et pourquoi chaque regle existe :**
  * `p50` au-dela -> `breached` : un regime mauvais n'est pas une queue ;
  * `p95` seul au-dela -> `degraded` : le regime tient, la queue casse ;
  * moins de 5 mesures -> `unknown` : une mediane sur deux points vaut l'un des
    deux points, ce n'est pas une mesure ;
  * une serie `unknown` ne masque jamais une serie cassee.
"""

import pytest

from observability.slo import DEFAULT_TARGETS, MIN_SAMPLES, evaluate, series_from_health


def series(n=10, p50=1000.0, p95=2000.0):
    return {"leg_ms:free:m": {"n": n, "p50_ms": p50, "p95_ms": p95}}


class TestVerdicts:
    def test_fast_series_is_ok(self):
        v = evaluate(series(p50=1200, p95=3000))
        assert v["status"] == "ok"
        assert v["offenders"] == []

    def test_slow_median_is_breached(self):
        """Le cas d'aujourd'hui : deepseek free a 24 s de mediane."""
        v = evaluate(series(p50=24000, p95=50000))
        assert v["status"] == "breached"
        assert v["series"][0]["verdict"] == "breached"

    def test_only_the_tail_is_degraded(self):
        v = evaluate(series(p50=3000, p95=20000))
        assert v["status"] == "degraded"
        assert v["series"][0]["verdict"] == "degraded"

    def test_targets_come_from_the_plan(self):
        assert DEFAULT_TARGETS["p50_ms"] == 4000.0
        assert DEFAULT_TARGETS["p95_ms"] == 12000.0

    def test_custom_targets_are_honoured(self):
        v = evaluate(series(p50=5000), targets={"p50_ms": 10000, "p95_ms": 99999})
        assert v["status"] == "ok"


class TestInsufficientData:
    def test_too_few_samples_is_unknown(self):
        v = evaluate(series(n=MIN_SAMPLES - 1, p50=99000, p95=99000))
        assert v["status"] == "unknown"
        assert v["series"][0]["verdict"] == "unknown"

    def test_enough_samples_gives_a_verdict(self):
        v = evaluate(series(n=MIN_SAMPLES, p50=99000))
        assert v["status"] == "breached"

    def test_unknown_never_masks_a_breach(self):
        payload = {
            "leg_ms:free:neuf": {"n": 1, "p50_ms": 1.0, "p95_ms": 1.0},
            "leg_ms:free:casse": {"n": 40, "p50_ms": 30000.0, "p95_ms": 60000.0},
        }
        v = evaluate(payload)
        assert v["status"] == "breached"
        assert v["offenders"] == ["leg_ms:free:casse"]

    def test_empty_input_is_unknown(self):
        assert evaluate({})["status"] == "unknown"
        assert evaluate(None)["status"] == "unknown"


class TestRobustness:
    def test_tuple_form_is_accepted(self):
        v = evaluate({"leg_ms:free:m": (20, 30000.0, 60000.0, 90000.0)})
        assert v["status"] == "breached"

    def test_garbage_entries_are_skipped(self):
        v = evaluate({"leg_ms:free:m": {"n": "beaucoup", "p50_ms": "?"}})
        assert v["status"] == "unknown"

    def test_offenders_are_sorted_worst_first(self):
        payload = {
            "leg_ms:free:lent": {"n": 20, "p50_ms": 30000.0, "p95_ms": 40000.0},
            "leg_ms:free:queue": {"n": 20, "p50_ms": 1000.0, "p95_ms": 30000.0},
        }
        v = evaluate(payload)
        assert v["offenders"][0] == "leg_ms:free:lent"

    def test_evaluate_never_raises(self):
        assert evaluate("n'importe quoi")["status"] == "unknown"
        assert evaluate({"x": None})["status"] == "unknown"


class TestHealthExtraction:
    def test_only_leg_series_are_kept(self):
        payload = {
            "latency_by_leg": {"leg_ms:free:m": {"n": 5, "p50_ms": 1, "p95_ms": 2}},
            "other": 1,
        }
        assert list(series_from_health(payload)) == ["leg_ms:free:m"]

    def test_missing_payload_is_tolerated(self):
        assert series_from_health({}) == {}
        assert series_from_health(None) == {}

    def test_health_exposes_the_slo(self):
        import inspect

        import opencode as oc

        assert "slo" in inspect.getsource(oc.health)
