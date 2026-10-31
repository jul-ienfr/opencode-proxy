"""Exposition des modeles free VERIFIES : un appel reel, pas une lecture de catalogue.

Contexte (audit 2026-10-20, ecart V6.4). Le catalogue `models.opencode.ai`
declare 128 modeles sur la jambe `opencode` ; `config.yaml` en expose 46. On
pouvait lire l'ecart comme « 82 modeles a conquerir ». La mesure dit le
contraire.

**Ce qui a ete mesure.** Un POST reel par id absent de la config (81 ids),
corps conforme a la porte (stream + outil `read` realiste), via tunnel SOCKS,
`max_tokens: 8` — sur le chemin de la famille declaree (chat, messages ou
responses) :

| Reponse | Nombre | Sens |
|---|---|---|
| **200** | **1** (`big-pickle`) | servi anonymement |
| 401 `Missing API key` | 32 | reserve aux comptes authentifies |
| 400 `Model is unavailable` | 21 | annonce mais non servi |
| timeout / erreur tunnel (1er passage) | 27 | **non conclus** — re-testes : 2 s, 401 ou 400 |

Les 27 « non conclus » ont ete re-testes avec un delai plus long : ils
repondent en ~2 s. Les timeouts du premier passage etaient un artefact de
charge sur les tunnels, pas une indisponibilite. **Aucun n'a rendu 200.**

**Conclusion, et c'est une conclusion negative utile :** les 81 ids free non
exposes ne sont pas une reserve de modeles gratuits. Ils sont dans le
catalogue parce que l'editeur les *propose*, pas parce que la porte anonyme les
*sert*. Exposer `big-pickle` et documenter le refus des autres vaut mieux que
d'annoncer 23 modeles gagnes et de livrer 23 erreurs.

**Regle d'exposition.** Seul un id ayant rendu **200 a un appel reel** est
expose, et uniquement en famille `chat` : pour `messages`, ni la base ni les
en-tetes attendus n'ont ete valides sur la jambe free. Un modele expose sans
preuve serait une promesse que le proxy ne peut pas tenir.
"""

import json

import pytest

from config import settings as cs

SPEC = cs.FREE_MODELS_EXPOSED_FILE


def test_exposure_file_exists_and_is_readable():
    assert SPEC.exists(), f"{SPEC} absent : relancer _research/probe_free_models.py"
    data = json.loads(SPEC.read_text(encoding="utf-8"))
    assert "models" in data and "unavailable" in data


def test_only_verified_200_models_are_exposed():
    """Aucun modele n'est expose sans un 200 mesure."""
    data = json.loads(SPEC.read_text(encoding="utf-8"))
    for model_id, spec in data["models"].items():
        assert spec.get("verified_status") == 200, f"{model_id} expose sans preuve"
        assert model_id not in data["unavailable"]["model_unavailable"]
        assert model_id not in data["unavailable"]["missing_api_key"]


def test_big_pickle_is_exposed_and_routed_to_the_free_leg():
    entry = cs.MODELS.get("big-pickle")
    assert entry is not None, "big-pickle a rendu 200 : il doit etre servi"
    assert entry["source"] == "free_verified"
    assert entry["endpoint"] == cs.API_BASE_FREE
    assert entry["protocol"] == "openai"


def test_config_yaml_stays_sovereign(monkeypatch):
    """Un id present dans `config.yaml` n'est jamais ecrase par l'exposition."""
    monkeypatch.setattr(cs, "MODELS", dict(cs.MODELS))
    original = dict(cs.MODELS["big-pickle"])
    assert cs.MODELS["big-pickle"]["endpoint"] == original["endpoint"]


def test_only_chat_family_is_exposed():
    """`messages` non valide sur la jambe free : on n'expose pas a l'aveugle."""
    data = json.loads(SPEC.read_text(encoding="utf-8"))
    for model_id, spec in data["models"].items():
        assert spec.get("family") == "chat", model_id
        assert spec.get("protocol") == "openai", model_id


def test_the_unavailable_set_is_documented_not_guessed():
    """Le refus est date et compte : la prochaine session ne refera pas le travail."""
    data = json.loads(SPEC.read_text(encoding="utf-8"))
    unav = data["unavailable"]
    assert data.get("verified_at"), "la mesure doit etre datee"
    assert len(unav["missing_api_key"]) + len(unav["model_unavailable"]) >= 50, (
        "le fichier doit garder la trace des ids refuses : c'est la preuve que l'exposition n'a pas ete faite a moitie"
    )
    assert "big-pickle" not in (
        unav["missing_api_key"] + unav["model_unavailable"] + unav["inconclusive_at_first_pass"]
    )


@pytest.mark.parametrize("model_id", ["claude-opus-4-5", "gpt-5.2", "gemini-3-pro", "glm-4.7-free"])
def test_known_unavailable_models_are_not_exposed(model_id):
    """Temoins refuses : s'ils apparaissaient un jour, ce serait une regression."""
    data = json.loads(SPEC.read_text(encoding="utf-8"))
    refuses = set(
        data["unavailable"]["missing_api_key"]
        + data["unavailable"]["model_unavailable"]
        + data["unavailable"]["inconclusive_at_first_pass"]
    )
    if model_id in refuses:
        assert model_id not in cs.MODELS or cs.MODELS[model_id].get("source") != "free_verified"


# ── Classification « free » du pipeline (défaut mesuré le 2026-10-20) ────────
#
# Exposer un modèle dans `MODELS` avec la base free ne suffit pas : le pipeline
# décide de la voie avec `_is_free_model_route`, qui ne reconnaissait que les
# ids finissant par `-free` ou présents dans `FREE_MODEL_MAP`. `big-pickle`
# n'était ni l'un ni l'autre : il partait donc sur la voie **payante**, et
# comme aucune clé n'est valide, rendait « All API keys exhausted » — mesuré en
# direct via le proxy redémarré, sur un modèle dont l'endpoint était pourtant
# l'inférence anonyme. Ces deux tests verrouillent le maillon manquant.


def test_verified_model_is_classified_as_free_by_the_pipeline():
    assert "big-pickle" in cs.FREE_MODELS, (
        "un modèle vérifié en direct doit rejoindre FREE_MODELS, sinon le pipeline le traite en payant"
    )


def test_verified_model_route_resolves_to_the_free_leg():
    import opencode as oc

    assert oc._is_free_model_route("big-pickle") is True


def test_non_verified_models_are_never_classified_free():
    """Temoin negatif : la classification ne doit pas s'elargir par accident."""
    import opencode as oc

    for model_id in ("claude-opus-4-5", "gpt-5.2", "glm-5.2"):
        assert oc._is_free_model_route(model_id) is False, model_id


def test_discovery_pass_does_not_erase_verified_ids():
    """La decouverte rebatit FREE_MODELS : elle ne doit pas effacer les verifies.

    `apply_discovered_free_models` fait `free_models.clear()` puis
    `update(free_ids)`. Un modele verifie en direct ne finit pas par `-free` et
    n'est pas une cible decouverte : sans reajout explicite, la passe suivante
    l'effacait et le pipeline le renvoyait sur la voie payante (mesure :
    « All API keys exhausted » sur `big-pickle`).
    """
    saved = set(cs.FREE_MODELS)
    try:
        cs.FREE_MODELS.discard("big-pickle")
        assert "big-pickle" not in cs.FREE_MODELS
        cs._apply_discovered_free_models(set(), source="test")
        assert "big-pickle" in cs.FREE_MODELS
    finally:
        cs.FREE_MODELS.clear()
        cs.FREE_MODELS.update(saved)
