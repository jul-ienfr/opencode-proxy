"""Compteur d'incidents « flux amont vide » (V5.1, préalable a la correction).

**Le defaut.** Un amont qui repond 200 puis ne produit AUCUN octet est renvoye
au client en HTTP 200 accompagne d'un evenement SSE `error`
(`upstream produced no output`). Le client croit avoir une reponse.

**Pourquoi il n'est pas encore corrige.** Le corriger demande de deplacer la
decision du 200 apres un peek qui exige au moins une trame porteuse de
contenu — ce qui touche les quatre chemins de streaming. Le proxy fait deja un
peek, mais il attend les **en-tetes** (`_stream_ctx.__aenter__` sous
`_TTFBWatchdogTimeout`), pas le premier octet de corps : un 200 suivi d'un
corps vide passe donc le peek. Sans amont factice, on ne peut pas verifier une
telle modification de bout en bout.

**Ce que ce fichier verrouille.** L'incident doit au moins etre **comptable** :
sans compteur, on ne sait ni sa frequence, ni les modeles touches, ni s'il se
concentre sur une jambe. C'est la donnee qui decidera de la priorite — et qui
evitera de « reparer » a l'aveugle un chemin chaud.

Les tests portent sur un etat **reinitialise explicitement** : un compteur
global qui fuit d'un test a l'autre produirait exactement le genre
d'intermittence qu'on vient de corriger ailleurs dans ce depot.
"""

import pytest

import opencode as oc


@pytest.fixture(autouse=True)
def _clean_counter():
    """Etat propre avant ET apres : le compteur est global."""
    oc._EMPTY_STREAM_INCIDENTS.clear()
    yield
    oc._EMPTY_STREAM_INCIDENTS.clear()


def test_counting_starts_at_one():
    oc._note_empty_stream("glm-5.2", "paid")
    assert oc.empty_stream_stats() == {"paid|glm-5.2": 1}


def test_counts_accumulate_per_key():
    for _ in range(3):
        oc._note_empty_stream("glm-5.2", "paid")
    oc._note_empty_stream("mimo-v2.6-flash-free", "free")
    stats = oc.empty_stream_stats()
    assert stats["paid|glm-5.2"] == 3
    assert stats["free|mimo-v2.6-flash-free"] == 1


def test_leg_and_model_are_distinct_dimensions():
    """Le meme modele sur deux jambes doit rester distinguable."""
    oc._note_empty_stream("glm-5.2", "paid")
    oc._note_empty_stream("glm-5.2", "free")
    assert set(oc.empty_stream_stats()) == {"paid|glm-5.2", "free|glm-5.2"}


def test_stats_are_sorted_by_frequency():
    oc._note_empty_stream("rare", "paid")
    for _ in range(4):
        oc._note_empty_stream("frequent", "paid")
    assert list(oc.empty_stream_stats()) == ["paid|frequent", "paid|rare"]


def test_missing_values_do_not_raise():
    """Fail-soft : un incident ne doit jamais casser une requete."""
    oc._note_empty_stream("", "")
    oc._note_empty_stream(None, None)  # type: ignore[arg-type]
    assert sum(oc.empty_stream_stats().values()) == 2


def test_counter_is_bounded():
    """Borne dure : un compteur d'incidents ne doit pas devenir une fuite."""
    for i in range(oc._EMPTY_STREAM_MAX_KEYS + 50):
        oc._note_empty_stream(f"modele-{i}", "paid")
    assert len(oc._EMPTY_STREAM_INCIDENTS) == oc._EMPTY_STREAM_MAX_KEYS
    # Les cles deja connues continuent de s'incrementer malgre la borne.
    first = sorted(oc._EMPTY_STREAM_INCIDENTS)[0]
    model = first.split("|", 1)[1]
    before = oc._EMPTY_STREAM_INCIDENTS[first]
    oc._note_empty_stream(model, "paid")
    assert oc._EMPTY_STREAM_INCIDENTS[first] == before + 1


def test_health_exposes_the_counter_only_in_full_detail():
    """Le payload leger est appele toutes les 30 s : il ne doit pas grossir."""
    import inspect

    source = inspect.getsource(oc.health)
    assert "empty_stream_incidents" in source
    assert "_detail_full" in source
