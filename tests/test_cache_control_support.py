"""test_cache_control_support.py — [TROU 8] garde `supports_cache_control` sur les QUATRE sites.

DÉFAUT MESURÉ (lecture + mutation) : dans `anthropic_to_openai`
(`app/protocol/mapping.py`), quatre sites reportent le `cache_control` du dernier bloc
sur le message converti :

    L1377 / L1399 : `if last_cache_control and not is_asst and supports_cache_control:`
    L1363 / L1390 : `if last_cache_control and not is_asst:`          <-- garde absent

La variable existe pourtant dans la même fonction (`supports_cache_control = not
model.startswith("glm-5")`, L1074) : les deux sites non gardés émettaient donc un
`cache_control` vers un modèle qui ne le supporte pas. Asymétrie entre sites jumeaux,
pas décision.

ATTEINDRE CES DEUX SITES est le point délicat — et c'est ce qui a fait échouer ma
première version de ce fichier (les tests passaient par les sites **déjà gardés**, donc
la mutation ne mordait pas). Les deux lignes vivent dans des branches qui exigent
`tool_calls` **et** `not is_asst` : il faut donc un message **utilisateur** contenant un
bloc `tool_use` (un `tool_use` remplit `tool_calls` quel que soit le rôle, cf. L1230-1244).
L1363 exige en plus `image_parts` (branche `if image_parts: if tool_calls:`) ; L1390 est
dans la branche `elif tool_calls:` (donc **sans** image).
"""

import app.protocol.mapping as mp

# `supports_cache_control = not model.startswith("glm-5")` (mapping.py:1074)
MODELE_SANS_CACHE = "glm-5"
MODELE_AVEC_CACHE = "claude-haiku-4-5"

_TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}}
_TEXTE_CACHE = {"type": "text", "text": "suite", "cache_control": {"type": "ephemeral"}}
_IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}


def _corps(avec_image: bool):
    """Message UTILISATEUR avec `tool_use` (+ image pour la branche L1363)."""
    contenu = [_TOOL_USE]
    if avec_image:
        contenu.append(_IMAGE)
    contenu.append(_TEXTE_CACHE)
    return {"messages": [{"role": "user", "content": contenu}]}


def _cache_controls(converti):
    return [m for m in converti.get("messages", []) if "cache_control" in m]


def test_site_sans_image_ne_recoit_aucun_cache_control_si_non_supporte():
    """Branche `elif tool_calls:` — le site L1390."""
    converti = mp.anthropic_to_openai(_corps(avec_image=False), MODELE_SANS_CACHE)
    fautifs = _cache_controls(converti)
    assert not fautifs, f"cache_control emis vers un modele qui ne le supporte pas : {fautifs}"


def test_site_avec_image_ne_recoit_aucun_cache_control_si_non_supporte():
    """Branche `if image_parts: if tool_calls:` — le site L1363."""
    converti = mp.anthropic_to_openai(_corps(avec_image=True), MODELE_SANS_CACHE)
    fautifs = _cache_controls(converti)
    assert not fautifs, f"cache_control emis vers un modele qui ne le supporte pas : {fautifs}"


def test_modele_avec_support_conserve_le_cache_control():
    """Témoin inverse : le garde ne doit pas supprimer la fonctionnalité.

    Sans ce test, un correctif qui supprimerait purement et simplement le report du
    `cache_control` passerait au vert.
    """
    for avec_image in (False, True):
        converti = mp.anthropic_to_openai(_corps(avec_image=avec_image), MODELE_AVEC_CACHE)
        assert _cache_controls(converti), (
            f"cache_control perdu alors que le modele le supporte (avec_image={avec_image})"
        )


# ── [TROU 10] le no-op `cache_control` → `prompt_cache_breakpoint` est une DÉCISION ──
#
# Deux docstrings du même fichier se contredisaient : `_carry_cc` (mapping.py:1455)
# annonçait « on émet AUSSI l'équivalent OpenAI réel (prompt_cache_breakpoint) » alors
# que `_cache_control_to_openai_breakpoint` (mapping.py:621) est un no-op documenté
# (« Réservé — NON ÉMIS »). La docstring fausse a été corrigée ; ce témoin verrouille le
# comportement réel pour que la décision ne dérive pas en silence.


def test_cache_control_sur_outil_est_transporte_sans_inventer_le_champ_openai():
    """Le breakpoint d'un outil survit ; `prompt_cache_breakpoint` n'est pas inventé."""
    corps = {
        "messages": [{"role": "user", "content": "bonjour"}],
        "tools": [
            {
                "name": "get_weather",
                "description": "meteo",
                "input_schema": {"type": "object", "properties": {}},
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }
    converti = mp.anthropic_to_openai(corps, MODELE_AVEC_CACHE)
    outils = converti.get("tools") or []
    assert outils, "l'outil doit survivre a la conversion"
    outil = outils[0]
    assert outil.get("cache_control") == {"type": "ephemeral"}, (
        f"breakpoint d'outil perdu silencieusement : {outil}"
    )
    assert "prompt_cache_breakpoint" not in outil, (
        "prompt_cache_breakpoint emis sur une definition d'outil : placement contraire a B3 "
        "(risque de 400 chez un amont strict) — voir mapping.py:621"
    )
