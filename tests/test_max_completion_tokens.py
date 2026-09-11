"""[PLAN_AUDIT_CONVERSIONS Lot L14] Limite de sortie : `max_completion_tokens` — B2/A17.

**A17 (confirmé).** ``max_completion_tokens`` avait **0 occurrence dans tout le
dépôt** : nos conversions n'émettaient que ``max_tokens``, et P4 ne *lisait* que
``max_tokens``. Conséquence mesurée : un client Chat envoyant
``max_completion_tokens`` (la forme moderne recommandée, la seule acceptée par
les modèles o-series) voyait sa limite **silencieusement remplacée par le défaut
16384**. Une limite courte posée pour borner le coût devenait inopérante — perte
invisible, sans erreur ni trace.

**B2 (confirmé).** ``max_tokens`` est déprécié et **incompatible avec les
modèles o-series**, qui exigent ``max_completion_tokens``.

Le correctif est volontairement **asymétrique**, et c'est le point à ne pas
casser : on *lit* les trois formes partout (aucune perte), mais on n'*émet* la
forme moderne que vers les préfixes de modèles qui l'exigent réellement. Basculer
trop large serait pire que le bug : une passerelle tierce ignorant le champ
inconnu n'appliquerait **aucune** limite de sortie (coût non borné).
"""

import pytest

from app.protocol.mapping import (
    _MAX_COMPLETION_MODELS_DEFAULT,
    _set_output_token_limit,
    _wants_max_completion_tokens,
    anthropic_to_openai,
    openai_responses_to_anthropic,
    openai_to_anthropic_request,
)

# ─────────────────────── A17 : la perte de la limite client ───────────────────────


def test_p4_chain_reads_max_completion_tokens():
    """A17 — LE point : la limite du client n'est plus remplacée par 16384.

    Avant correctif : ``max_completion_tokens=4096`` entrait, ``16384`` sortait.
    """
    out = openai_to_anthropic_request(
        {
            "model": "x",
            "max_completion_tokens": 4096,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert out.get("max_tokens") == 4096, "limite du client perdue (défaut 16384 substitué)"


def test_p4_chain_still_reads_classic_max_tokens():
    """Non-régression : la forme historique continue de fonctionner."""
    out = openai_to_anthropic_request(
        {"model": "x", "max_tokens": 512, "messages": [{"role": "user", "content": "hi"}]}
    )
    assert out.get("max_tokens") == 512


def test_p4_chain_defaults_when_client_sends_nothing():
    """Aucune limite demandée → défaut historique préservé (pas de champ inventé
    à partir de rien, mais le contrat V1 attend 16384)."""
    out = openai_to_anthropic_request(
        {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert out.get("max_tokens") == 16384


def test_p6_chain_reads_max_output_tokens():
    """Responses → Anthropic : ``max_output_tokens`` est bien transporté."""
    out = openai_responses_to_anthropic(
        {
            "model": "x",
            "max_output_tokens": 4096,
            "input": [{"role": "user", "content": "hi"}],
        }
    )
    assert out.get("max_tokens") == 4096


def test_p2_chain_transports_the_limit():
    """Anthropic → Chat : la limite ``max_tokens`` du client est transportée."""
    out = anthropic_to_openai(
        {"model": "x", "max_tokens": 4096, "messages": [{"role": "user", "content": "hi"}]},
        "un-modele-inconnu-xyz",
    )
    assert out.get("max_tokens") == 4096


# ─────────────────────── B2 : la forme émise ───────────────────────


def test_reasoning_model_gets_max_completion_tokens():
    """B2 : vers un modèle o-series, on émet ``max_completion_tokens`` — émettre
    ``max_tokens`` expose à un 400 sur un upstream strict."""
    out = anthropic_to_openai(
        {"model": "x", "max_tokens": 4096, "messages": [{"role": "user", "content": "hi"}]},
        "o3-mini",
    )
    assert out.get("max_completion_tokens") == 4096
    assert "max_tokens" not in out, "max_tokens émis vers un modèle o-series (déprécié/rejeté)"


def test_generic_model_keeps_max_tokens():
    """L'inverse, et c'est le point sensible : une passerelle tierce qui ignore
    ``max_completion_tokens`` n'appliquerait **aucune** limite de sortie."""
    out = anthropic_to_openai(
        {"model": "x", "max_tokens": 4096, "messages": [{"role": "user", "content": "hi"}]},
        "deepseek-v4-flash",
    )
    assert out.get("max_tokens") == 4096
    assert "max_completion_tokens" not in out


@pytest.mark.parametrize("model", ["o1", "o1-mini", "o3", "o3-mini", "o3-pro", "o4-mini", "gpt-5", "GPT-5-turbo"])
def test_all_o_series_prefixes_are_recognized(model):
    """La famille o-series/gpt-5 est reconnue, insensible à la casse."""
    assert _wants_max_completion_tokens(model) is True, model


@pytest.mark.parametrize(
    "model",
    ["deepseek-v4-flash", "glm-5.1", "mimo-v2.5", "muse-spark", "ling-3.0-flash-fin", "", None, 42],
)
def test_non_o_series_models_are_never_switched(model):
    """Aucun de nos upstreams réels ne bascule : ils acceptent ``max_tokens``."""
    assert _wants_max_completion_tokens(model) is False, model


def test_default_prefix_list_is_restricted_to_o_series():
    """Garde-fou : élargir cette liste par défaut est une décision délibérée.

    Si ce test casse, quelqu'un a ajouté un préfixe (deepseek/glm/…) : vérifier
    que l'upstream concerné *rejette* réellement ``max_tokens``, sinon c'est un
    risque de limite de sortie ignorée.
    """
    assert set(_MAX_COMPLETION_MODELS_DEFAULT) == {"o1", "o3", "o4", "gpt-5"}


def test_unknown_model_keeps_backward_compatible_field():
    """Un modèle inconnu garde le comportement historique."""
    out = anthropic_to_openai(
        {"model": "x", "max_tokens": 777, "messages": [{"role": "user", "content": "hi"}]},
        "modele-inexistant-zzz",
    )
    assert out.get("max_tokens") == 777


# ─────────────────────── helper unitaire ───────────────────────


def test_helper_reads_all_three_field_forms():
    """Les trois conventions client sont acceptées, par ordre de priorité."""
    target = {}
    assert _set_output_token_limit(target, {"max_tokens": 11}, "x") == 11
    assert target["max_tokens"] == 11

    target = {}
    assert _set_output_token_limit(target, {"max_completion_tokens": 22}, "x") == 22
    assert target["max_tokens"] == 22

    target = {}
    assert _set_output_token_limit(target, {"max_output_tokens": 33}, "x") == 33
    assert target["max_tokens"] == 33


def test_helper_prefers_max_tokens_when_several_present():
    """Ambiguïté : priorité stable et documentée à ``max_tokens``."""
    target = {}
    limit = _set_output_token_limit(target, {"max_tokens": 100, "max_output_tokens": 200}, "x")
    assert limit == 100


def test_helper_ignores_non_positive_and_non_int_limits():
    """Une limite absurde (0, négative, chaîne) ne doit pas être transportée."""
    for bad in (0, -5, "4096", None, 3.5):
        target = {}
        _set_output_token_limit(target, {"max_tokens": bad}, "x")
        assert target.get("max_tokens") == 16384, f"{bad!r} accepté comme limite"


def test_helper_anthropic_target_never_gets_completion_tokens():
    """Anthropic ne connaît que ``max_tokens``, même pour un modèle o-series."""
    target = {}
    _set_output_token_limit(target, {"max_completion_tokens": 55}, "o3-mini", target_protocol="anthropic")
    assert target.get("max_tokens") == 55
    assert "max_completion_tokens" not in target


def test_helper_default_none_omits_the_field():
    """``default=None`` : ne rien inventer quand le client n'a rien demandé."""
    target = {}
    _set_output_token_limit(target, {}, "x", default=None)
    assert "max_tokens" not in target
    assert "max_completion_tokens" not in target


def test_helper_emits_the_modern_field_toward_o_series():
    target = {}
    _set_output_token_limit(target, {"max_tokens": 99}, "o4-mini")
    assert target.get("max_completion_tokens") == 99
    assert "max_tokens" not in target


# ─────────────────────── bout en bout : les deux formes coexistent ───────────────────────


def test_limit_survives_a_chain_without_default_substitution():
    """La valeur exacte traverse la chaîne : aucune substitution par le défaut."""
    for value in (1, 128, 4096, 32768):
        out = openai_to_anthropic_request(
            {
                "model": "x",
                "max_completion_tokens": value,
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        assert out.get("max_tokens") == value, f"{value} → {out.get('max_tokens')}"


# ─────────────────────── piège des alias client ───────────────────────


def test_only_the_routed_model_name_decides_the_field():
    """Le nom qui décide est le modèle **routé**, pas l'alias du client.

    Piège réel de ce dépôt : ``gpt-5.6-luna`` n'est qu'un alias client qui route
    vers ``muse-spark-1.3-contributor`` (passerelle tierce, qui accepte
    ``max_tokens``). Les handlers passent ``model_id`` — le nom routé — donc
    l'alias ne déclenche pas le basculement. Si un jour un handler passait
    l'alias, ``gpt-5`` préfixerait et on enverrait ``max_completion_tokens`` à une
    passerelle qui l'ignore → **aucune limite de sortie appliquée**.
    """
    out = anthropic_to_openai(
        {"model": "x", "max_tokens": 500, "messages": [{"role": "user", "content": "hi"}]},
        "muse-spark-1.3-contributor",
    )
    assert out.get("max_tokens") == 500
    assert "max_completion_tokens" not in out

    # Et l'inverse, documenté explicitement comme NON utilisé par les handlers :
    # le nom d'alias, lui, bascule — d'où l'importance de passer `model_id`.
    aliased = anthropic_to_openai(
        {"model": "x", "max_tokens": 500, "messages": [{"role": "user", "content": "hi"}]},
        "gpt-5.6-luna",
    )
    assert aliased.get("max_completion_tokens") == 500


def test_handlers_pass_the_routed_model_not_the_client_alias():
    """Garde structurelle : les handlers appellent ``anthropic_to_openai`` avec
    ``model_id`` (nom routé), jamais avec ``original_model`` (alias client).

    C'est ce qui rend le basculement sûr vis-à-vis des alias ``gpt-5*``.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent.joinpath("opencode.py").read_text(
        encoding="utf-8", errors="replace"
    )
    # 2e argument positionnel de l'appel (le 1er est le body à convertir).
    calls = re.findall(
        r"anthropic_to_openai\(\s*[A-Za-z_][A-Za-z0-9_]*\s*,\s*([A-Za-z_][A-Za-z0-9_]*)", src
    )
    bad = [c for c in calls if c != "model_id"]
    assert not bad, (
        f"anthropic_to_openai appelé avec {bad} au lieu de `model_id` : un alias "
        f"client pourrait déclencher le basculement max_completion_tokens"
    )
    assert calls, "aucun appel trouvé — le motif de recherche est obsolète"
