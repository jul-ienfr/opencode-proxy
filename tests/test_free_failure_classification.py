"""V6.3.1 — classification des echecs de la jambe free.

Le probleme que ces tests adressent : un echec free etait traite comme « un
429 » quelle qu'en soit la cause, donc un 403 « forme de requete invalide »
declenchait la meme rotation de station qu'un quota epuise — cinq tunnels
gaspilles pour un refus qui aurait echoue sur les cinq.

Ce fichier verrouille la classification (pure) et le compteur. Il ne verrouille
PAS encore la politique de reprise : la classifier est un prealable, changer la
politique sans mesure reviendrait a deviner.
"""

import pytest

from upstream.free_failures import (
    FREE_FAILURE_KINDS,
    classify_free_failure,
    free_failure_stats,
    gate_factor_stats,
    note_free_failure,
    note_gate_factors,
    reset_free_failure_stats,
)


@pytest.fixture(autouse=True)
def _clean_stats():
    reset_free_failure_stats()
    yield
    reset_free_failure_stats()


class TestClassifyByStatus:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (429, "quota"),
            (401, "auth"),
            (403, "gate"),
            (500, "transit"),
            (502, "transit"),
            (503, "transit"),
            (504, "transit"),
            (529, "transit"),
            (200, "empty"),
        ],
    )
    def test_status_maps_to_kind(self, status, expected):
        assert classify_free_failure(status) == expected

    def test_400_is_model_when_the_body_says_so(self):
        assert classify_free_failure(400, "Model is unavailable") == "model"
        assert classify_free_failure(400, '{"error":"Model is unavailable"}') == "model"

    def test_400_without_a_model_marker_is_a_shape_refusal(self):
        assert classify_free_failure(400, "invalid request body") == "gate"


class TestClassifyByBody:
    def test_gate_markers(self):
        assert classify_free_failure(403, "free tier can only be used from within OpenCode") == "gate"

    def test_body_is_read_as_bytes_too(self):
        assert classify_free_failure(400, b"Model is unavailable") == "model"

    def test_unknown_status_with_auth_text(self):
        assert classify_free_failure(0, "missing api key") == "auth"

    def test_unknown_status_without_marker(self):
        assert classify_free_failure(418, "je suis une theiere") == "unknown"

    def test_missing_status_and_body(self):
        assert classify_free_failure(None, None) == "unknown"

    def test_none_status_with_text(self):
        assert classify_free_failure(None, "quelque chose") == "empty"


class TestCounter:
    def test_counts_are_per_kind(self):
        note_free_failure("gate")
        note_free_failure("gate")
        note_free_failure("quota")
        stats = free_failure_stats()
        assert stats == {"gate": 2, "quota": 1}

    def test_stats_are_sorted_by_frequency(self):
        note_free_failure("quota")
        for _ in range(3):
            note_free_failure("transit")
        assert list(free_failure_stats()) == ["transit", "quota"]

    def test_unknown_kind_is_coerced_not_dropped(self):
        note_free_failure("pas-une-classe")
        assert free_failure_stats() == {"unknown": 1}

    def test_every_declared_kind_is_accepted(self):
        for kind in FREE_FAILURE_KINDS:
            note_free_failure(kind)
        assert set(free_failure_stats()) == set(FREE_FAILURE_KINDS)

    def test_counter_never_raises(self):
        note_free_failure(None)  # type: ignore[arg-type]
        assert sum(free_failure_stats().values()) == 1


class TestWiring:
    def test_proxy_exposes_the_classifier_and_the_stats(self):
        import opencode as oc

        assert oc._classify_free_failure(403) == "gate"
        assert callable(oc._note_free_failure)
        assert oc.free_failure_stats() == {}

    def test_health_exposes_failure_kinds_in_full_detail_only(self):
        import inspect

        import opencode as oc

        source = inspect.getsource(oc.health)
        assert "free_failure_kinds" in source
        assert "_detail_full" in source


class TestGateFactors:
    """V6.1.2 — `gate` dit qu'un facteur a refuse ; ce compteur dit lequel."""

    def test_factors_are_counted(self):
        note_gate_factors(["body"])
        note_gate_factors(["body", "egress"])
        assert gate_factor_stats() == {"body": 2, "egress": 1}

    def test_stats_are_sorted_by_frequency(self):
        note_gate_factors(["egress"])
        for _ in range(3):
            note_gate_factors(["identity"])
        assert list(gate_factor_stats()) == ["identity", "egress"]

    def test_empty_input_is_harmless(self):
        note_gate_factors([])
        note_gate_factors(None)
        assert gate_factor_stats() == {}

    def test_reset_clears_both_counters(self):
        note_free_failure("gate")
        note_gate_factors(["body"])
        reset_free_failure_stats()
        assert free_failure_stats() == {}
        assert gate_factor_stats() == {}

    def test_proxy_exposes_the_gate_counter(self):
        import opencode as oc

        assert callable(oc._note_gate_factors)
        assert callable(oc.gate_factor_stats)

    def test_health_exposes_gate_factors_in_full_detail(self):
        import inspect

        import opencode as oc

        source = inspect.getsource(oc.health)
        assert "gate_factors" in source
