"""Unitaire config-driven des plafonds d'effort (config/effort_caps.py).

Ces tests ne dépendent PAS du contenu de ``config.yaml`` : chaque test
isole ``_yaml_data`` de ``config.settings`` (même pattern que
``tests/test_hot_reload*.py``) pour injecter ``thinking.effort_order`` /
``thinking.effort_caps`` ad hoc. Lecture live (pas de snapshot import) :
changer le dict entre deux appels prend effet immédiatement (hot-reload
logique, sans reimport).
"""

import pytest

import config.effort_caps as ec
import config.settings as cfg


@pytest.fixture
def isolated_thinking(monkeypatch):
    """Copie _yaml_data, injecte une section thinking ad hoc, restaure après."""
    original = cfg._yaml_data.get("thinking")
    import copy

    saved = copy.deepcopy(original) if original is not None else None

    def _set(order, caps):
        cfg._yaml_data["thinking"] = {"effort_order": order, "effort_caps": caps}

    _set(
        ["minimal", "low", "medium", "high", "xhigh", "max"],
        {
            "default": "high",
            "glm-5": "high",
            "deepseek-v4": "max",
            "muse-spark": "xhigh",
        },
    )
    yield _set
    if saved is None:
        cfg._yaml_data.pop("thinking", None)
    else:
        cfg._yaml_data["thinking"] = saved


def test_longest_prefix_wins(isolated_thinking):
    """Un préfixe long gagne sur un court quel que soit l'ordre du dict."""
    isolated_thinking(
        ["low", "medium", "high", "xhigh", "max"],
        {"default": "high", "spark": "low", "muse-spark": "xhigh"},
    )
    assert ec.get_max_effort_for_model("muse-spark-1.3-contributor") == "xhigh"
    assert ec.get_max_effort_for_model("spark-xyz") == "low"


def test_prefix_case_insensitive(isolated_thinking):
    isolated_thinking(["low", "medium", "high"], {"default": "high", "GLM-5": "high"})
    assert ec.get_max_effort_for_model("GLM-5-Air") == "high"
    assert ec.get_max_effort_for_model("  glm-5-flash  ") == "high"


def test_fallback_default_unknown_model(isolated_thinking):
    assert ec.get_max_effort_for_model("totally-unknown-xyz") == "high"
    assert ec.get_max_effort_for_model("") == "high"
    assert ec.get_max_effort_for_model(None) == "high"


def test_invalid_cap_falls_back_to_default(isolated_thinking):
    isolated_thinking(
        ["low", "medium", "high", "xhigh", "max"],
        {"default": "medium", "glm-5": "banana"},
    )
    assert ec.get_max_effort_for_model("glm-5-air") == "medium"


def test_missing_section_falls_back_to_hardcoded(monkeypatch):
    monkeypatch.setitem(cfg._yaml_data, "thinking", None)
    assert ec.get_effort_order() == ec.EFFORT_ORDER_DEFAULT
    assert ec.get_max_effort_for_model("anything") == ec.DEFAULT_CAP_FALLBACK


def test_custom_effort_order(isolated_thinking):
    """Un ordre custom valide fait foi pour la comparaison min()."""
    isolated_thinking(
        ["low", "medium", "high", "ultra"],
        {"default": "ultra"},
    )
    assert ec.clamp_effort("ultra", "anything") == "ultra"
    # ordre custom sans low/medium/high → rejeté, défaut utilisé
    isolated_thinking(["low", "medium"], {"default": "medium"})
    assert ec.get_effort_order() == ec.EFFORT_ORDER_DEFAULT


def test_hot_reload_logical_no_reimport(isolated_thinking):
    """Changer le dict caps entre deux appels → prise en compte immédiate."""
    assert ec.clamp_effort("max", "glm-5-air") == "high"
    isolated_thinking(
        ["minimal", "low", "medium", "high", "xhigh", "max"],
        {"default": "high", "glm-5": "max"},
    )
    assert ec.clamp_effort("max", "glm-5-air") == "max"


@pytest.mark.parametrize(
    "effort,expected",
    [
        (None, None),
        ("", None),
        ("none", None),
        ("NONE", None),
        ("  none  ", None),
    ],
)
def test_disabled_passthrough(isolated_thinking, effort, expected):
    assert ec.clamp_effort(effort, "glm-5-air") is expected


def test_unknown_level_passthrough(isolated_thinking):
    """Niveau hors effort_order → inchangé (robustesse forward)."""
    assert ec.clamp_effort("ultra", "glm-5-air") == "ultra"


@pytest.mark.parametrize(
    "effort,model,expected",
    [
        ("max", "glm-5-air", "high"),  # au-delà du cap → relegué
        ("xhigh", "glm-5-air", "high"),
        ("high", "glm-5-air", "high"),  # au cap → inchangé
        ("medium", "glm-5-air", "medium"),  # sous le cap → inchangé
        ("minimal", "glm-5-air", "minimal"),
        ("MAX", "glm-5-air", "high"),  # casse/espaces normalisés
        (" XHigh ", "muse-spark-1.3-contributor", "xhigh"),
        ("max", "muse-spark-1.3-contributor", "xhigh"),
        ("max", "deepseek-v4-flash", "max"),
        ("max", "mimo-v2.5", "high"),  # défaut
    ],
)
def test_clamp_matrix(isolated_thinking, effort, model, expected):
    assert ec.clamp_effort(effort, model) == expected
