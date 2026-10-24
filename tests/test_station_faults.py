"""V6.3.2 — imputer une panne à une station, et seulement quand c'est elle.

**Le defaut que ces tests protegent.** Un `403` free confond deux causes
opposees : la sortie refusee (IP cramee — ecarter *cette* station est utile) et
la requete malformee (les cinq stations echoueront — ecarter est du gaspillage).
La session a deja mesure cette confusion une fois : un `403` de forme declenchait
la meme rotation qu'un quota epuise.

**La regle centrale, et sa subtilite.** `egress` seul accuse la station.
`egress` **plus** `body`/`identity` n'accuse personne : on ne peut pas prouver
que l'IP est fautive quand la requete l'est aussi. C'est cette nuance qui
empeche le disjoncteur de punir une station pour une faute du client.
"""

import pytest

from upstream.station_faults import (
    STATION_FAULTS,
    attributable_fault,
    cooldown_seconds,
    note_station_fault,
    reset_station_fault_stats,
    station_fault_stats,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_station_fault_stats()
    yield
    reset_station_fault_stats()


class TestAttribution:
    def test_egress_alone_accuses_the_station(self):
        assert attributable_fault(["egress"]) == "egress"

    def test_egress_with_a_malformed_body_accuses_nobody(self):
        """La requete est fautive aussi : l'accusation n'est pas demontrable."""
        assert attributable_fault(["egress", "body"]) is None
        assert attributable_fault(["egress", "identity"]) is None
        assert attributable_fault(["egress", "body", "identity"]) is None

    def test_shape_refusals_never_accuse_the_station(self):
        assert attributable_fault(["body"]) is None
        assert attributable_fault(["identity"]) is None
        assert attributable_fault(["body", "identity"]) is None

    def test_transport_faults_are_attributable(self):
        assert attributable_fault([], "timeout") == "timeout"
        assert attributable_fault([], "connect") == "connect"
        assert attributable_fault([], "transit") == "transit"

    def test_unknown_faults_are_not_attributed(self):
        assert attributable_fault([], "quelque-chose") is None
        assert attributable_fault([], None) is None
        assert attributable_fault([]) is None

    def test_fault_names_are_normalised(self):
        assert attributable_fault([], "  TIMEOUT ") == "timeout"

    def test_no_input_never_crashes(self):
        assert attributable_fault(None, None) is None
        assert attributable_fault(object(), None) is None


class TestCooldown:
    def test_every_fault_has_a_positive_cooldown(self):
        for fault in STATION_FAULTS:
            assert cooldown_seconds(fault) > 0

    def test_egress_is_benched_longer_than_transit(self):
        """Une IP refusee reste refusee ; une surcharge passe."""
        assert cooldown_seconds("egress") > cooldown_seconds("transit")

    def test_unknown_fault_means_no_cooldown(self):
        assert cooldown_seconds("inconnu") == 0.0
        assert cooldown_seconds(None) == 0.0

    def test_cooldowns_stay_bounded(self):
        for fault in STATION_FAULTS:
            assert 5.0 <= cooldown_seconds(fault) <= 600.0


class TestCounter:
    def test_counts_per_station_and_cause(self):
        note_station_fault(3, "egress")
        note_station_fault(3, "egress")
        note_station_fault(2, "timeout")
        assert station_fault_stats() == {"3:egress": 2, "2:timeout": 1}

    def test_sorted_by_frequency(self):
        note_station_fault(1, "transit")
        for _ in range(3):
            note_station_fault(2, "egress")
        assert list(station_fault_stats()) == ["2:egress", "1:transit"]

    def test_missing_station_id_is_tolerated(self):
        note_station_fault(None, "timeout")
        assert station_fault_stats() == {"?:timeout": 1}

    def test_counter_never_raises(self):
        note_station_fault(1, None)  # type: ignore[arg-type]
        assert sum(station_fault_stats().values()) == 1


class TestWiring:
    def test_proxy_exposes_the_policy(self):
        import opencode as oc

        assert oc._station_attributable_fault(["egress"]) == "egress"
        assert oc._station_cooldown_seconds("egress") > 0
        assert callable(oc._note_station_fault)

    def test_health_exposes_station_faults(self):
        import inspect

        import opencode as oc

        assert "station_faults" in inspect.getsource(oc.health)

    def test_the_gate_branch_benches_only_on_attributable_faults(self):
        """Garde-fou structurel : la mise a l'ecart passe par la politique."""
        import inspect

        import opencode as oc

        source = inspect.getsource(oc)
        assert "_station_attributable_fault(" in source
        assert "_set_free_cooldown(free_model, _bench" in source, (
            "la mise à l'écart doit passer par la pool (source de vérité unique)"
        )
