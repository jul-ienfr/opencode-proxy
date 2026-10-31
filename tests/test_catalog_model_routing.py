"""Routage par modele : la famille d'API vient du CATALOGUE, pas d'une table.

Contexte (audit 2026-10-20, ecart P0-3) : le proxy deduisait la famille d'API
du `npm` **de niveau provider**, en croyant que le `npm` etait purement global.
C'est faux : 66 des 128 modeles de la jambe `opencode` portent leur propre
`provider.npm`, et **chaque** modele porte sa propre `provider.api`. Consequence
mesuree : 5 modeles sur le mauvais protocole et 10 modeles gemini sans route.

Ces tests verrouillent la resolution par modele contre l'instantane reel du
catalogue (`_research/official-1.19.3/models-api.json`, releve du 2026-10-20).
Ils sont *skippes* si l'instantane est absent : un checkout sans artefact de
mesure ne doit pas echouer, il doit dire qu'il ne sait pas.
"""

import json
import pathlib
import sys

import pytest

sys.path.insert(0, ".")
from config import models_catalog as mc  # noqa: E402

SNAPSHOT = pathlib.Path("_research/official-1.19.3/models-api.json")

pytestmark = pytest.mark.skipif(
    not SNAPSHOT.exists(),
    reason="instantane du catalogue absent (_research/official-1.19.3/models-api.json)",
)


@pytest.fixture(scope="module")
def catalog():
    with open(SNAPSHOT, encoding="utf-8") as fh:
        return json.load(fh)


def _models(catalog, provider):
    return list((catalog.get(provider) or {}).get("models") or {})


# ── 1. Le catalogue porte bien une URL par modele ───────────────────────────


@pytest.mark.parametrize("provider", ["opencode", "opencode-go"])
def test_every_model_has_its_own_api_url(catalog, provider):
    """`provider.api` par modele est TOUJOURS present (mesure : 128/128, 33/33)."""
    manquants = [m for m in _models(catalog, provider) if not mc.model_api(catalog, provider, m)]
    assert not manquants, f"{provider}: {len(manquants)} modeles sans URL d'API : {manquants[:5]}"


def test_model_api_prefers_the_model_override(catalog):
    """Une surcharge de modele gagne sur l'URL du provider."""
    url = mc.model_api(catalog, "opencode", "qwen3.6-plus-free")
    assert url == "https://opencode.ai/inference/anthropic/v1"
    assert "/anthropic/" in url


def test_model_api_falls_back_to_the_provider(catalog):
    """Sans surcharge, c'est l'URL du provider qui s'applique."""
    entry = (catalog["opencode"]["models"]).get("mimo-v2.6-flash-free") or {}
    if (entry.get("provider") or {}).get("api"):
        pytest.skip("ce modele porte desormais une surcharge : choisir un autre temoin")
    assert mc.model_api(catalog, "opencode", "mimo-v2.6-flash-free") == catalog["opencode"]["api"]


# ── 2. La famille se deduit de la donnee ────────────────────────────────────


def test_family_distribution_matches_the_measurement(catalog):
    """Repartition mesuree le 2026-10-20 — toute derive doit echouer ici."""
    import collections

    free = collections.Counter(mc.api_family_for_model(catalog, "opencode", m) for m in _models(catalog, "opencode"))
    paid = collections.Counter(
        mc.api_family_for_model(catalog, "opencode-go", m) for m in _models(catalog, "opencode-go")
    )
    assert dict(free) == {"chat": 63, "messages": 24, "responses": 32, "gemini": 9}
    assert dict(paid) == {"chat": 21, "messages": 6, "responses": 5, "gemini": 1}


def test_gemini_models_are_detected_not_silently_routed_to_chat(catalog):
    """Les 10 modeles gemini doivent etre IDENTIFIES (le proxy les refusera en 501).

    Avant l'alignement, 9 free + 1 payant partaient sur `/chat/completions`
    ou n'avaient aucune route : dans les deux cas, un echec opaque.
    """
    gemini_free = [
        m for m in _models(catalog, "opencode") if mc.api_family_for_model(catalog, "opencode", m) == "gemini"
    ]
    gemini_paid = [
        m for m in _models(catalog, "opencode-go") if mc.api_family_for_model(catalog, "opencode-go", m) == "gemini"
    ]
    assert len(gemini_free) == 9
    assert len(gemini_paid) == 1
    assert "gemini" in mc.KNOWN_API_FAMILIES


def test_anthropic_family_models_get_the_anthropic_url(catalog):
    """Les modeles `@ai-sdk/anthropic` pointent bien vers `/anthropic/v1`."""
    ids = [m for m in _models(catalog, "opencode") if mc.api_family_for_model(catalog, "opencode", m) == "messages"]
    assert len(ids) == 24
    for m in ids:
        assert "/anthropic/" in (mc.model_api(catalog, "opencode", m) or ""), m
        assert mc.model_npm(catalog, "opencode", m) == "@ai-sdk/anthropic"


def test_the_five_known_mismatches_are_correctly_familied(catalog):
    """Les 5 modeles ou `config.yaml` contredit le catalogue.

    Mesure du 2026-10-20 — le catalogue dit :

      * `minimax-m2.5`, `minimax-m2.7`, `minimax-m3`, `qwen3.8-max` : pas de
        `npm`, `api = .../inference/openai/v1` -> famille **chat**.
        Or `config.yaml` les declare `protocol: anthropic` (donc `/messages`).
      * `qwen3.6-plus` : `npm = @ai-sdk/anthropic`,
        `api = .../inference/anthropic/v1` -> famille **messages**.
        Or `config.yaml` le declare `protocol: openai` (donc `/chat/completions`).

    Ce test verrouille la *verite catalogue*. La correction de `config.yaml`
    (ou mieux : la suppression de la table statique au profit du catalogue)
    est le lot suivant de V2.
    """
    attendus = {
        "minimax-m2.5": "chat",
        "minimax-m2.7": "chat",
        "minimax-m3": "chat",
        "qwen3.8-max": "chat",
        "qwen3.6-plus": "messages",
    }
    for model_id, famille in attendus.items():
        got = mc.api_family_for_model(catalog, "opencode", model_id)
        assert got == famille, f"{model_id}: famille {got!r} != {famille!r} (catalogue)"


def test_free_leg_has_anthropic_hosted_models(catalog):
    """3 modeles free parlent `/messages` : le proxy ne sait pas les router.

    `minimax-m2.5-free`, `minimax-m3-free`, `qwen3.6-plus-free` repondent 404
    sur `/chat/completions` parce que leur amont est `/inference/anthropic/v1`.
    C'est l'une des causes des 23 modeles free non servis.
    """
    famille = {
        m: mc.api_family_for_model(catalog, "opencode", m)
        for m in ("minimax-m2.5-free", "minimax-m3-free", "qwen3.6-plus-free")
    }
    assert all(f == "messages" for f in famille.values()), famille
    for m in famille:
        assert "/anthropic/" in (mc.model_api(catalog, "opencode", m) or "")


# ── 3. Refus explicite plutot que repli silencieux ──────────────────────────


def test_unknown_npm_and_url_yields_none_not_chat():
    """Ni URL signee ni `npm` connu -> None. Jamais un repli `/chat/completions`."""
    cat = {
        "p": {
            "npm": "@ai-sdk/quelque-chose-de-nouveau",
            "api": "https://exemple.test/v9",
            "models": {"m": {"provider": {}}},
        }
    }
    assert mc.api_family_for_model(cat, "p", "m") is None


def test_url_marker_wins_over_a_contradictory_npm():
    """Quand l'URL et le `npm` se contredisent, c'est l'URL qui recoit la requete."""
    cat = {
        "p": {
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://exemple.test/v1",
            "models": {
                "m": {
                    "provider": {
                        "npm": "@ai-sdk/openai",  # dit "responses"
                        "api": "https://exemple.test/anthropic/v1",  # dit "messages"
                    }
                }
            },
        }
    }
    assert mc.api_family_for_model(cat, "p", "m") == "messages"


@pytest.mark.parametrize(
    "url,famille",
    [
        ("https://opencode.ai/inference/anthropic/v1", "messages"),
        ("https://opencode.ai/inference/go/anthropic/v1", "messages"),
        ("https://opencode.ai/inference/google/v1beta", "gemini"),
        ("https://opencode.ai/inference/go/google/v1beta", "gemini"),
        ("https://opencode.ai/inference/openai/v1", None),
        ("https://opencode.ai/inference/go/openai/v1", None),
        (None, None),
        ("", None),
    ],
)
def test_api_family_for_url_markers(url, famille):
    assert mc.api_family_for_url(url) == famille


def test_all_families_are_declared(catalog):
    """Aucune famille retournee ne doit sortir de `KNOWN_API_FAMILIES`."""
    for provider in ("opencode", "opencode-go"):
        for m in _models(catalog, provider):
            fam = mc.api_family_for_model(catalog, provider, m)
            assert fam in mc.KNOWN_API_FAMILIES, f"{provider}/{m}: famille inconnue {fam!r}"
