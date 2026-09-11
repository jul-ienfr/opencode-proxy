"""[PLAN_AUDIT_CONVERSIONS Lot L12] Usage & display — A18, A19.

**A18 — ventilation d'usage manquante.**
Les conversions Responses écrivaient ``reasoning_tokens: 0`` **en dur**. Le
dashboard affichait donc toujours zéro token de raisonnement, alors que c'est la
part facturée la plus chère d'un modèle de raisonnement, et l'indicateur qui
permet de détecter un modèle qui « réfléchit » moins que prévu.

**A19 — `thinking.display` non géré.**
Avec ``display: "omitted"``, Anthropic n'émet **aucun** ``thinking_delta`` : seul
un ``signature_delta`` arrive. Un proxy qui attend des deltas de thinking pour
ouvrir/fermer un bloc, ou qui prend ``thinking_acc`` non vide comme condition
d'émission, casse silencieusement (bloc jamais fermé, signature perdue).
"""

import pytest

from app.protocol import mapping as _canon

# ─────────────────────── A18 : reasoning_tokens ───────────────────────


@pytest.mark.parametrize(
    "usage,expected",
    [
        # Formes officielles Responses / Chat
        ({"output_tokens_details": {"reasoning_tokens": 512}}, 512),
        ({"completion_tokens_details": {"reasoning_tokens": 512}}, 512),
        # À plat, certains upstreams compatibles
        ({"reasoning_tokens": 512}, 512),
        # Passthrough Anthropic-compat
        ({"output_tokens_details": {"thinking_tokens": 512}}, 512),
        # Absent → 0 (un upstream sans raisonnement n'écrit pas le champ)
        ({}, 0),
        ({"output_tokens": 100}, 0),
        # Zéro explicite → 0
        ({"output_tokens_details": {"reasoning_tokens": 0}}, 0),
    ],
)
def test_reasoning_tokens_all_forms(usage, expected):
    """A18 : toutes les conventions de ventilation sont lues."""
    assert _canon._extract_reasoning_tokens(usage) == expected


def test_reasoning_tokens_priority_is_stable():
    """Priorité déterministe quand plusieurs formes coexistent."""
    usage = {
        "output_tokens_details": {"reasoning_tokens": 111},
        "completion_tokens_details": {"reasoning_tokens": 222},
        "reasoning_tokens": 333,
    }
    assert _canon._extract_reasoning_tokens(usage) == 111


@pytest.mark.parametrize(
    "broken",
    [
        None,
        "pas un dict",
        {"output_tokens_details": None},
        {"output_tokens_details": "x"},
        {"output_tokens_details": {"reasoning_tokens": "beaucoup"}},
        {"output_tokens_details": {"reasoning_tokens": None}},
        {"completion_tokens_details": 42},
        {"output_tokens_details": {"reasoning_tokens": -5}},
    ],
)
def test_reasoning_tokens_tolerates_malformed(broken):
    """Robustesse : `usage` vient du réseau, jamais d'exception."""
    assert _canon._extract_reasoning_tokens(broken) == 0


def test_negative_reasoning_tokens_is_rejected():
    """Une valeur négative est un upstream cassé : on préfère 0 à un compteur
    négatif qui polluerait les totaux du dashboard."""
    assert _canon._extract_reasoning_tokens({"reasoning_tokens": -1}) == 0


# ── A18 : intégration dans les deux conversions Responses ──


def test_anthropic_to_responses_reports_reasoning_tokens():
    """A18 — LE test : la conversion P5 remonte la ventilation réelle au lieu
    du 0 codé en dur."""
    anthro = {
        "id": "msg_1",
        "model": "test",
        "content": [
            {"type": "thinking", "thinking": "je réfléchis", "signature": "sig"},
            {"type": "text", "text": "Voici la réponse."},
        ],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 200,
            "output_tokens_details": {"reasoning_tokens": 150},
        },
    }
    out = _canon.anthropic_to_openai_responses(anthro, "test-model")
    assert out["usage"]["output_tokens_details"]["reasoning_tokens"] == 150
    # La ventilation ne doit pas fausser les totaux d'entrée/sortie.
    assert out["usage"]["input_tokens"] == 100
    assert out["usage"]["output_tokens"] == 200


def test_anthropic_to_responses_zero_when_absent():
    """Contre-preuve : sans ventilation amont, on reste à 0 (pas d'invention)."""
    anthro = {
        "id": "msg_1",
        "model": "test",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    out = _canon.anthropic_to_openai_responses(anthro, "test-model")
    assert out["usage"]["output_tokens_details"]["reasoning_tokens"] == 0


def test_chat_to_responses_reports_reasoning_tokens():
    """A18 : la seconde conversion (Chat → Responses) remonte aussi la
    ventilation — les deux sites étaient concernés."""
    chat = {
        "id": "chatcmpl_1",
        "model": "test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "ok"},
            }
        ],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 80,
            "completion_tokens_details": {"reasoning_tokens": 60},
        },
    }
    out = _canon.openai_chat_to_responses(chat, "test-model")
    assert out["usage"]["output_tokens_details"]["reasoning_tokens"] == 60


def test_chat_to_responses_zero_when_absent():
    """Contre-preuve symétrique sur la seconde conversion."""
    chat = {
        "id": "c1",
        "model": "test",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5},
    }
    out = _canon.openai_chat_to_responses(chat, "test-model")
    assert out["usage"]["output_tokens_details"]["reasoning_tokens"] == 0


def test_reasoning_tokens_never_exceeds_output_tokens():
    """Cohérence : la ventilation est un SOUS-ensemble de la sortie. Si l'amont
    se contredit, on ne fabrique pas un total incohérent (le dashboard afficherait
    un pourcentage > 100 %)."""
    anthro = {
        "id": "msg_1",
        "model": "test",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        # Amont incohérent : 500 tokens de raisonnement pour 100 en sortie.
        "usage": {
            "input_tokens": 10,
            "output_tokens": 100,
            "output_tokens_details": {"reasoning_tokens": 500},
        },
    }
    out = _canon.anthropic_to_openai_responses(anthro, "test-model")
    usage = out["usage"]
    reasoning = usage["output_tokens_details"]["reasoning_tokens"]
    assert reasoning <= usage["output_tokens"], (
        f"reasoning_tokens ({reasoning}) > output_tokens ({usage['output_tokens']}) : "
        "pourcentage de raisonnement > 100 % dans le dashboard"
    )


# ─────────────────────── A19 : thinking.display ───────────────────────


def test_thinking_display_omitted_does_not_break_response_conversion():
    """A19 : une réponse Anthropic dont le thinking a `display:"omitted"`
    (bloc thinking présent mais VIDE, sans deltas) se convertit sans erreur et
    ne produit pas de faux raisonnement."""
    anthro = {
        "id": "msg_1",
        "model": "test",
        "content": [
            # display:"omitted" → le texte n'est pas fourni.
            {"type": "thinking", "thinking": "", "signature": "sig_abc"},
            {"type": "text", "text": "La réponse."},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    out = _canon.anthropic_to_openai_response(anthro, "test-model")
    msg = out["choices"][0]["message"]
    # Le contenu utile survit.
    assert "La réponse." in str(msg.get("content"))
    # Un thinking vide ne doit pas devenir un raisonnement fantôme.
    assert msg.get("reasoning_content") in (None, ""), (
        f"raisonnement fantôme produit depuis un thinking vide : {msg.get('reasoning_content')!r}"
    )


def test_thinking_with_display_omitted_keeps_signature_usable():
    """A19 : même sans texte de thinking, le bloc reste exploitable (signature
    présente) — c'est ce que le multi-tours Anthropic exige pour rejouer le tour."""
    anthro = {
        "id": "msg_1",
        "model": "test",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig_xyz"},
            {"type": "text", "text": "ok"},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    out = _canon.anthropic_to_openai_responses(anthro, "test-model")
    # La conversion ne doit pas lever et doit produire un item de sortie exploitable.
    assert out["output"], "aucun item de sortie produit"
    assert out["status"] == "completed"


def test_empty_thinking_does_not_produce_empty_summary_item():
    """A19 : un thinking vide ne doit pas créer un item `reasoning` avec un
    résumé vide — un client qui affiche ce résumé montrerait un bloc « réflexion »
    vide pour chaque tour."""
    anthro = {
        "id": "msg_1",
        "model": "test",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "ok"},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    out = _canon.anthropic_to_openai_responses(anthro, "test-model")
    for item in out["output"]:
        if item.get("type") == "reasoning":
            for summary in item.get("summary", []):
                assert summary.get("text"), "résumé de raisonnement vide exposé au client"


def test_text_response_survives_alongside_omitted_thinking():
    """A19 : le point qui compte vraiment — le texte utilisateur n'est pas perdu
    quand le thinking est omis."""
    anthro = {
        "id": "msg_1",
        "model": "test",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "Réponse complète et utile."},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    out = _canon.anthropic_to_openai_response(anthro, "test-model")
    assert "Réponse complète et utile." in str(out["choices"][0]["message"].get("content"))
