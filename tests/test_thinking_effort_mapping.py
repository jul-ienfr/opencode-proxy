"""test_thinking_effort_mapping.py — [TROU 11] « `thinking` racine jamais copié » : RÉFUTÉ.

Mon audit déclarait : « `thinking` top-level jamais copié » vers un corps Responses.
La mesure contredit cette déclaration pour le constructeur de requête vers l'endpoint
`/responses` (`_anthropic_to_responses_request`, `app/protocol/mapping.py`) :

    sans `thinking`          -> pas de champ `reasoning`
    `thinking: {type: disabled}` -> pas de champ `reasoning`
    `thinking: {type: enabled, budget_tokens: N}` -> `reasoning: {summary: detailed, effort: ...}`

et l'effort n'est pas figé : il dérive du budget (256 -> low, 8000 -> medium,
32000 -> high). L'axe était donc **déjà couvert**, sans témoin pour le verrouiller.

Ces tests transforment une affirmation fausse en comportement mesuré et verrouillé.
Portée exacte : le constructeur de requête ci-dessus. Les autres chemins qui bâtissent
un corps Responses ne sont pas couverts par ce fichier.
"""

import pytest

import app.protocol.mapping as mp

_BASE = {"messages": [{"role": "user", "content": "bonjour"}], "max_tokens": 100}


def _build(thinking=None):
    corps = dict(_BASE)
    if thinking is not None:
        corps["thinking"] = thinking
    return mp._anthropic_to_responses_request(corps)


def test_sans_thinking_aucun_reasoning():
    assert "reasoning" not in _build(), "un `reasoning` apparait sans que le client l'ait demande"


def test_thinking_desactive_aucun_reasoning():
    assert "reasoning" not in _build({"type": "disabled"}), (
        "`thinking: disabled` doit etre un non-evenement, pas un reasoning"
    )


def test_thinking_actif_produit_reasoning_avec_resume():
    reasoning = _build({"type": "enabled", "budget_tokens": 8000}).get("reasoning")
    assert reasoning, "`thinking` racine perdu : aucun `reasoning` emis"
    # Parité SDK : le défaut est 'detailed' (riche), pas 'auto' (condensé).
    assert reasoning.get("summary") == "detailed", f"resume non demande : {reasoning!r}"


@pytest.mark.parametrize(
    ("budget", "effort_attendu"),
    [
        (256, "low"),
        (1024, "low"),
        (8000, "medium"),
        (32000, "high"),
        (200000, "high"),
    ],
)
def test_le_budget_pilote_l_effort(budget, effort_attendu):
    """Le budget ne doit pas etre jeté : la mesure montre une échelle low/medium/high."""
    reasoning = _build({"type": "enabled", "budget_tokens": budget}).get("reasoning") or {}
    assert reasoning.get("effort") == effort_attendu, (
        f"budget_tokens={budget} -> effort={reasoning.get('effort')!r}, attendu {effort_attendu!r}"
    )
