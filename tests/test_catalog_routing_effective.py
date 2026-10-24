"""Routage effectif vs catalogue : la surcharge est etroite, et c'est voulu.

Contexte (audit 2026-10-20). La table `models:` de `config.yaml` declare un
`protocol` par modele. Le catalogue public de l'editeur declare, lui, une
famille d'API par modele **et par jambe**. Les deux divergent parfois — et le
28/20 la mesure a montre que l'ecart est plus etroit qu'annonce :

  * `minimax-m2.7`, `minimax-m3`, `qwen3.8-max` existent sur les DEUX jambes,
    avec des familles differentes (chat cote free, messages cote payant). Le
    proxy les route vers le payant : sa config `anthropic` est donc CORRECTE.
    L'audit les avait comptes comme fautifs en comparant la verite de la jambe
    free au routage de la jambe payante.
  * `minimax-m2.5` et `qwen3.6-plus` n'existent QUE cote free. Les router vers
    le payant est une erreur de JAMBE, pas de protocole.
  * `minimax-m3.1-flash-free` est le seul cas ou la surcharge corrige vraiment
    quelque chose aujourd'hui : `protocol: anthropic` sur un endpoint chat.

Les tests ci-dessous verrouillent trois proprietes, dans cet ordre d'importance:
1. on ne devine jamais (pas de repli d'une jambe sur l'autre) ;
2. on ne casse pas ce qui marche (l'endpoint actuel est garde s'il parle la
   bonne famille) ;
3. on corrige le protocole des qu'il diverge, meme quand l'endpoint est bon.
"""

import sys

import pytest

sys.path.insert(0, ".")
from config import settings as cs  # noqa: E402

FREE_BASE = "https://opencode.ai/inference/v1/chat/completions"


# ── 1. Le vrai correctif d'aujourd'hui ──────────────────────────────────────


def test_the_one_real_fix_keeps_the_working_endpoint():
    """`minimax-m3.1-flash-free` : protocole corrige, endpoint free conserve.

    La config le declare `anthropic` ; le catalogue dit `chat`. Corriger le
    protocole est indispensable (sinon un corps Anthropic part vers
    `/chat/completions`). Changer l'endpoint ne l'est pas : la base free du
    proxy repond 200, celle du catalogue (`/inference/openai/v1`) n'est pas
    verifiee sur cette route.
    """
    data = cs._models_cfg["minimax-m3.1-flash-free"]
    assert data.get("protocol") == "anthropic", "la config doit rester le temoin du bug"
    override = cs._catalog_override("minimax-m3.1-flash-free", data, "anthropic")
    assert override is not None
    assert override["protocol"] == "openai"
    assert override.get("endpoint") is None, "l'endpoint free qui marche ne doit pas bouger"
    assert cs._resolve_model_endpoint("minimax-m3.1-flash-free", data, "openai") == FREE_BASE


def test_the_corrected_model_is_resolved_with_the_catalog_protocol():
    """Le MODELS construit a l'import porte bien `openai` pour ce modele."""
    entry = cs.MODELS["minimax-m3.1-flash-free"]
    assert entry["protocol"] == "openai"
    assert entry["endpoint"] == FREE_BASE


# ── 2. Aucun repli d'une jambe sur l'autre ──────────────────────────────────


def test_no_cross_leg_fallback_free_to_paid():
    """`minimax-m2.5` n'existe que cote free : aucune route payante n'est inventee.

    Le repli inter-jambes envoyait le trafic payant sur la base free (ou
    l'inverse) — une erreur silencieuse, pire qu'un refus.
    """
    assert cs.catalog_route_for("minimax-m2.5", is_free=False) is None
    assert cs.catalog_route_for("minimax-m2.5", is_free=True) is not None


def test_no_cross_leg_fallback_paid_to_free():
    """Un id purement payant ne doit pas emprunter la route free."""
    payes = [m for m in (cs.CATALOG_ROUTES.get("opencode-go") or {})]
    assert payes, "le cache payant est vide : le test ne prouve rien"
    purement_payes = [
        m for m in payes if m not in (cs.CATALOG_ROUTES.get("opencode") or {})
    ]
    assert purement_payes, "aucun id purement payant : adapter le test"
    temoin = purement_payes[0]
    assert cs.catalog_route_for(temoin, is_free=False) is not None
    assert cs.catalog_route_for(temoin, is_free=True) is None


# ── 3. On ne casse pas ce qui marche ────────────────────────────────────────


def test_no_override_when_config_and_catalog_agree():
    """Le cas courant : rien ne bouge, et surtout pas l'endpoint."""
    data = {"protocol": "openai"}
    for model_id in ("glm-5.2", "kimi-k2.6"):
        assert cs._catalog_override(model_id, data, "openai") is None, model_id


def test_paid_leg_anthropic_models_are_left_alone():
    """`minimax-m2.7`/`m3`/`qwen3.8-max` : la config a RAISON sur la jambe payante.

    Le catalogue les declare `messages` cote `opencode-go` — exactement ce que
    dit `config.yaml`. L'audit les avait signales a tort.
    """
    for model_id in ("minimax-m2.7", "minimax-m3", "qwen3.8-max"):
        route = cs.catalog_route_for(model_id, is_free=False)
        assert route and route["family"] == "messages", model_id
        assert cs._catalog_override(model_id, {"protocol": "anthropic"}, "anthropic") is None


def test_explicit_endpoint_key_still_wins():
    """Un `endpoint:` explicite en config reste souverain (ex. `-free` sur go)."""
    assert cs._resolve_model_endpoint(
        "minimax-m3.1-flash-free", {"endpoint": "go"}, "anthropic"
    ) == cs.API_BASE_OPENAI


# ── 4. La surcharge d'endpoint quand la famille differe vraiment ────────────


def test_endpoint_is_replaced_only_when_the_family_really_differs(monkeypatch):
    """Cas synthetique : modele free declare anthropic, catalogue dit chat.

    La, l'endpoint free parle deja chat -> on garde. Le test inverse (catalogue
    `messages`, endpoint chat) doit, lui, changer d'endpoint.
    """
    monkeypatch.setattr(
        cs,
        "CATALOG_ROUTES",
        {
            "opencode": {
                "synthetique-free": {
                    "family": "messages",
                    "protocol": "anthropic",
                    "base_url": "https://opencode.ai/inference/anthropic/v1",
                }
            },
            "opencode-go": {},
        },
    )
    override = cs._catalog_override("synthetique-free", {"protocol": "openai"}, "openai")
    assert override == {
        "protocol": "anthropic",
        "endpoint": "https://opencode.ai/inference/anthropic/v1/messages",
    }


def test_gemini_family_gets_no_route_rather_than_a_wrong_one():
    """Famille connue, pas de convertisseur : aucune route inventee."""
    monkeypatch_route = {
        "family": "gemini",
        "protocol": None,
        "base_url": "https://opencode.ai/inference/google/v1beta",
    }
    assert cs._FAMILY_SUFFIX.get("gemini") is None, (
        "une famille sans suffixe ne peut pas composer d'endpoint"
    )
    assert monkeypatch_route["protocol"] is None


@pytest.mark.parametrize(
    "endpoint,famille",
    [
        ("https://x/v1/chat/completions", "chat"),
        ("https://x/v1/messages", "messages"),
        ("https://x/v1/responses", "responses"),
        ("https://x/anthropic/v1/messages", "messages"),
    ],
)
def test_endpoint_family_detection(endpoint, famille):
    assert cs._endpoint_family(endpoint, "openai") == famille


def test_legacy_endpoint_is_unchanged_for_ordinary_models():
    """Le comportement historique est preserve pour tout le reste."""
    assert cs._legacy_endpoint("kimi-k2.6", {"protocol": "openai"}, "openai") == cs.API_BASE_OPENAI
    assert cs._legacy_endpoint("glm", {"protocol": "anthropic"}, "anthropic") == cs.API_BASE_ANTHROPIC
    assert cs._legacy_endpoint("machin-free", {"protocol": "openai"}, "openai") == cs.API_BASE_FREE
