"""[PLAN_AUDIT_CONVERSIONS Lot L2/L9/L10] Contrat de la source unique d'effort.

Ce fichier verrouille la propriété qui manquait avant L2 : **une même demande
d'effort produit le même résultat, quel que soit le chemin** (A1). Il teste
aussi le vocabulaire complet (A2) et le plafond par modèle (config-driven).

``config.effort_policy`` est la source unique ; les 4 sites historiques
(P2, P4, P6, handler P3) y délèguent désormais.
"""

import pytest

import protocol_mapping as pm
from config.effort_policy import (
    BUDGET_TO_LEVEL_TABLE,
    MODEL_MAX,
    budget_to_level,
    extract_requested_level,
    max_level_for_model,
    normalize_level,
    resolve_effort,
)

# Un modèle à plafond max : permet de tester le passthrough des niveaux hauts.
UNCAPPED = "deepseek-v4-flash"
# Un modèle plafonné à high (effort_caps.default = high).
CAPPED = "modele-inconnu-plafonne"


# ───────────────────────── extraction ─────────────────────────


@pytest.mark.parametrize(
    "fields,expected_level,expected_source",
    [
        ({"output_config": {"effort": "high"}}, "high", "output_config.effort"),
        ({"effort": "low"}, "low", "effort"),
        ({"reasoning_effort": "medium"}, "medium", "reasoning_effort"),
        ({"reasoning": {"effort": "xhigh"}}, "xhigh", "reasoning.effort"),
        ({"thinking": {"type": "adaptive"}}, MODEL_MAX, "thinking.adaptive"),
        ({"thinking": {"type": "enabled"}}, MODEL_MAX, "thinking.enabled"),
        ({"thinking": {"type": "enabled", "budget_tokens": 2000}}, "low", "thinking.budget_tokens"),
        ({"thinking": {"type": "enabled", "budget_tokens": 5000}}, "medium", "thinking.budget_tokens"),
        ({"thinking": {"type": "enabled", "budget_tokens": 12000}}, "high", "thinking.budget_tokens"),
        ({"thinking": {"type": "enabled", "budget_tokens": 20000}}, "xhigh", "thinking.budget_tokens"),
    ],
)
def test_every_client_form_is_understood(fields, expected_level, expected_source):
    """Toutes les formes réellement observées sont lues par UN seul code."""
    level, source, explicit = extract_requested_level(fields)
    assert level == expected_level
    assert source == expected_source


def test_output_config_effort_has_priority_over_legacy():
    """La forme ACTUELLE documentée prime sur la forme historique."""
    level, source, _ = extract_requested_level(
        {"output_config": {"effort": "max"}, "effort": "low"}
    )
    assert level == "max"
    assert source == "output_config.effort"


def test_output_config_effort_has_priority_over_reasoning_effort():
    """Un corps portant les deux (client mixte) suit le champ documenté."""
    level, _, _ = extract_requested_level(
        {"output_config": {"effort": "medium"}, "reasoning_effort": "low"}
    )
    assert level == "medium"


@pytest.mark.parametrize("value", ["none", "NONE", " none ", "disabled"])
def test_explicit_disable_is_respected(value):
    """`none`/`disabled` = désactivation explicite, jamais un niveau."""
    level, source, explicit = extract_requested_level({"effort": value})
    assert level is None
    assert explicit is True
    assert source == "disabled"


def test_thinking_disabled_is_respected():
    level, source, explicit = extract_requested_level({"thinking": {"type": "disabled"}})
    assert level is None
    assert explicit is True


def test_nothing_requested_means_no_reasoning():
    """Absence totale d'effort → aucun raisonnement (pas de défaut imposé)."""
    decision = resolve_effort({}, UNCAPPED)
    assert decision.wants is False
    assert decision.level is None


def test_default_when_unspecified_is_opt_in():
    """Le défaut n'est appliqué que si l'appelant le demande explicitement."""
    decision = resolve_effort({}, UNCAPPED, default_when_unspecified="medium")
    assert decision.wants is True
    assert decision.level == "medium"


# ───────────────────────── vocabulaire (A2) ─────────────────────────


@pytest.mark.parametrize("level", ["minimal", "low", "medium", "high", "xhigh", "max"])
def test_full_enum_is_recognized(level):
    """A2 : l'énumération OpenAI COMPLÈTE doit passer — `minimal`, `xhigh` et
    `max` étaient auparavant écrasés ou filtrés."""
    decision = resolve_effort({"reasoning_effort": level}, UNCAPPED)
    assert decision.wants is True
    assert decision.level == level


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_unknown_level_is_not_silently_downgraded(level):
    """Robustesse forward : un niveau inconnu n'est pas écrasé par un défaut."""
    decision = resolve_effort({"effort": "niveau-futur"}, UNCAPPED)
    assert decision.level == "niveau-futur"


def test_normalize_level_handles_noise():
    assert normalize_level("  HIGH  ") == "high"
    assert normalize_level("") is None
    assert normalize_level(None) is None
    assert normalize_level(42) is None


# ───────────────────────── table de budget ─────────────────────────


@pytest.mark.parametrize(
    "budget,expected",
    [
        (0, None),
        (-5, None),
        (None, None),
        ("abc", None),
        (1, "low"),
        (3999, "low"),
        (4000, "medium"),
        (9999, "medium"),
        (10000, "high"),
        (15999, "high"),
        (16000, "xhigh"),
        (999999, "xhigh"),
    ],
)
def test_budget_table_is_single_and_total(budget, expected):
    """La table budget → niveau est totale et unique (A1)."""
    assert budget_to_level(budget) == expected


def test_budget_table_is_capped_at_xhigh():
    """Anthropic n'a pas de niveau au-delà de `xhigh` côté budget : un budget
    énorme ne doit pas inventer un niveau."""
    assert budget_to_level(10**9) == "xhigh"


def test_budget_table_thresholds_are_descending():
    """Invariant de la table : bornes décroissantes, sinon la 1ʳᵉ gagne
    n'importe quoi."""
    thresholds = [t for t, _ in BUDGET_TO_LEVEL_TABLE]
    assert thresholds == sorted(thresholds, reverse=True)


# ───────────────────────── plafonds par modèle ─────────────────────────


def test_cap_lowers_request():
    """Un plafond modèle abaisse la demande (glm-5 → high)."""
    decision = resolve_effort({"effort": "max"}, "glm-5-air")
    assert decision.level == "high"


def test_cap_does_not_raise_request():
    """Un plafond modèle n'AUGMENTE jamais la demande."""
    decision = resolve_effort({"effort": "minimal"}, UNCAPPED)
    assert decision.level == "minimal"


def test_uncapped_model_passes_through_max():
    decision = resolve_effort({"effort": "max"}, UNCAPPED)
    assert decision.level == "max"


def test_cap_is_config_driven_not_hardcoded():
    """A1 : le plafond vient de `thinking.effort_caps`, pas d'un `if model.
    startswith("glm-5")` codé en dur dans un handler."""
    from config.effort_caps import get_max_effort_for_model

    assert get_max_effort_for_model("glm-5") == "high"  # config.yaml
    assert get_max_effort_for_model(UNCAPPED) == "max"  # config.yaml
    assert get_max_effort_for_model("inconnu") == "high"  # default


# ─────────── PARITÉ DES 4 SITES (le cœur de L2) ───────────


@pytest.mark.parametrize(
    "level,model",
    [
        ("low", UNCAPPED),
        ("medium", UNCAPPED),
        ("high", UNCAPPED),
        ("xhigh", UNCAPPED),
        ("max", UNCAPPED),
        ("minimal", UNCAPPED),
        ("max", "glm-5-air"),  # plafonné → high
        ("xhigh", "glm-5-air"),  # plafonné → high
        ("minimal", "glm-5-air"),  # sous le plafond → inchangé
    ],
)
def test_p2_and_p4_agree_on_the_same_request(level, model):
    """A1 : P2 (`anthropic_to_openai`) et P4 (`openai_to_anthropic_request`)
    doivent produire le MÊME niveau pour la même demande — plafond modèle
    compris.

    C'est exactement ce qui était faux avant L2 (4 tables divergentes) : le
    plafond de ``glm-5`` n'était appliqué ni en P4 ni en P3.

    Seule divergence tolérée : P4 exprime le résultat dans le vocabulaire
    Anthropic, où ``minimal`` se replie sur ``low`` (``output_config.effort``
    n'accepte pas ``minimal``).
    """
    reference = resolve_effort({"effort": level}, model)
    assert reference.level is not None

    from_p2 = pm.anthropic_to_openai(
        {"model": model, "max_tokens": 4096, "messages": [], "effort": level}, model
    ).get("reasoning_effort")
    from_p4 = pm.openai_to_anthropic_request(
        {"model": model, "max_tokens": 4096, "messages": [], "reasoning_effort": level}
    ).get("output_config", {}).get("effort")

    assert from_p2 == reference.level, "P2 diverge de la politique"
    assert from_p4 == ("low" if reference.level == "minimal" else reference.level), (
        "P4 diverge de la politique"
    )


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max", "minimal"])
def test_p6_agrees_with_policy(level):
    """A3/A15 : P6 applique la même politique, et n'émet jamais
    `reasoning_effort` (nom OpenAI) vers un amont Anthropic."""
    out = pm.openai_responses_to_anthropic(
        {"model": UNCAPPED, "input": [], "reasoning": {"effort": level}}
    )
    assert "reasoning_effort" not in out
    expected = "low" if level == "minimal" else level
    assert out.get("output_config", {}).get("effort") == expected


def test_policy_and_p2_agree_on_budget_derivation():
    """Un budget client donne le même niveau via la politique et via P2."""
    for budget, expected in [(2000, "low"), (5000, "medium"), (12000, "high"), (20000, "xhigh")]:
        body = {
            "model": UNCAPPED,
            "max_tokens": 4096,
            "messages": [],
            "thinking": {"type": "enabled", "budget_tokens": budget},
        }
        from_p2 = pm.anthropic_to_openai(body, UNCAPPED).get("reasoning_effort")
        from_policy = resolve_effort(body, UNCAPPED).level
        assert from_p2 == from_policy == expected


def test_adaptive_without_budget_takes_the_model_maximum():
    """Décision produit : `thinking` sans niveau = le MAXIMUM DU MODÈLE.

    Un client qui demande du raisonnement sans préciser de niveau veut « le
    mieux que ce modèle sait faire » — pas un défaut arbitraire. Le niveau est
    donc adapté à chaque modèle, borné par son plafond configuré.
    """
    from_p2 = pm.anthropic_to_openai(
        {"model": UNCAPPED, "max_tokens": 4096, "messages": [], "thinking": {"type": "adaptive"}},
        UNCAPPED,
    ).get("reasoning_effort")
    assert from_p2 == max_level_for_model(UNCAPPED) == "max"

    # Adapté au modèle : un modèle plafonné plus bas reçoit SON plafond.
    capped = pm.anthropic_to_openai(
        {"model": CAPPED, "max_tokens": 4096, "messages": [], "thinking": {"type": "adaptive"}},
        CAPPED,
    ).get("reasoning_effort")
    assert capped == max_level_for_model(CAPPED) == "high"
    assert capped != from_p2


def test_explicit_level_is_never_raised_to_the_model_maximum():
    """Un niveau explicite garde la main : le max du modèle n'est qu'un défaut."""
    decision = resolve_effort({"output_config": {"effort": "low"}}, UNCAPPED)
    assert decision.level == "low"
    assert decision.explicit is True


# ─────────── A14/A23 : jamais de budget émis ───────────


@pytest.mark.parametrize("max_tokens", [1, 16, 256, 512, 1024, 4096, 32768])
def test_no_path_ever_emits_budget_tokens(max_tokens):
    """A14/A23 : aucun chemin n'émet `thinking.budget_tokens`, donc l'invariant
    Anthropic `max_tokens > budget_tokens` ne peut jamais être violé."""
    outs = [
        pm.openai_to_anthropic_request(
            {"model": UNCAPPED, "max_tokens": max_tokens, "messages": [], "reasoning_effort": "high"}
        ),
        pm.openai_responses_to_anthropic(
            {"model": UNCAPPED, "input": [], "reasoning": {"effort": "high"}, "max_output_tokens": max_tokens}
        ),
    ]
    for out in outs:
        thinking = out.get("thinking") or {}
        assert "budget_tokens" not in thinking
        if out.get("output_config", {}).get("effort"):
            assert thinking.get("type") == "adaptive"


def test_thinking_disabled_input_is_not_overridden_by_effort():
    """Un client qui désactive explicitement le raisonnement garde la main."""
    out = pm.openai_to_anthropic_request(
        {
            "model": UNCAPPED,
            "max_tokens": 4096,
            "messages": [],
            "thinking": {"type": "disabled"},
        }
    )
    thinking = out.get("thinking") or {}
    assert thinking.get("type") != "adaptive"


# ─────────── L9 : plafonds de modèle côté sortie Anthropic ───────────


def test_anthropic_output_never_exceeds_model_cap():
    """L9 : le plafond modèle s'applique aussi à `output_config.effort`."""
    out = pm.openai_to_anthropic_request(
        {"model": "glm-5-air", "max_tokens": 4096, "messages": [], "reasoning_effort": "max"}
    )
    assert out["output_config"]["effort"] == "high"


def test_anthropic_output_level_is_always_in_vocabulary():
    """Un niveau hors vocabulaire Anthropic ne doit jamais partir tel quel
    (l'amont rejetterait en 400)."""
    out = pm.openai_to_anthropic_request(
        {"model": UNCAPPED, "max_tokens": 4096, "messages": [], "reasoning_effort": "niveau-futur"}
    )
    effort = out.get("output_config", {}).get("effort")
    assert effort in ("low", "medium", "high", "xhigh", "max"), effort
