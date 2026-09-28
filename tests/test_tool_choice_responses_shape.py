"""FIX « tour vide » : normalisation de ``tool_choice`` vers ``/v1/responses``.

Contexte (preuve live, ``muse-spark-1.3-contributor-free``) :

    tool_choice envoyé             résultat amont
    -----------------------------  ---------------------------
    absent / "auto"                200, contenu normal
    "none" / "required" / "any"    refus
    {"type": <auto|none|...>}      refus  ← cas du client courant
    {"type": "function", ...}      refus

Le client envoie ``{"type": "auto"}``. Recopié verbatim par la conversion
Chat→Responses, il faisait refuser la requête ; sur la jambe free l'échec était
avalé en **tour vide** (input ~50 000, output ~40 tokens), donc le compactage
client recevait un résumé vide et la tâche se figeait.

Ces tests verrouillent la normalisation et, surtout, l'absence de régression sur
les formes nommées (qui relèvent de ``_remap_responses_tool_choice``).
"""
import json

import pytest

from app.protocol import mapping as pm

TOOLS_CHAT = [
    {
        "type": "function",
        "function": {"name": "read", "parameters": {"type": "object", "properties": {}}},
    }
]


def _chat(tool_choice):
    body = {
        "model": "muse-spark-1.3-contributor",
        "messages": [{"role": "user", "content": "x"}],
        "tools": TOOLS_CHAT,
    }
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    return body


# ── 1 : les formes objet non nommées sont ramenées à "auto" ──────────────
@pytest.mark.parametrize("incoming", ["auto", "any", "none", "required"])
def test_object_forms_are_normalized_to_auto(incoming):
    """Toute forme ``{"type": X}`` non nommée → ``"auto"`` (seule valeur admise)."""
    out = pm._chat_to_responses_request(_chat({"type": incoming}))
    assert out["tool_choice"] == "auto"


def test_the_exact_client_form_is_fixed():
    """Le cas réel : ``{"type":"auto"}`` — celui du client et du compactage."""
    out = pm._chat_to_responses_request(_chat({"type": "auto"}))
    assert out["tool_choice"] == "auto", (
        "l'upstream /responses refuse {'type':'auto'} : un tour vide s'ensuit"
    )


@pytest.mark.parametrize("incoming", ["auto", "none", "required"])
def test_string_forms_are_preserved(incoming):
    """Les chaînes déjà valides ne sont pas touchées par la normalisation."""
    out = pm._chat_to_responses_request(_chat(incoming))
    assert out["tool_choice"] == incoming


# ── 2 : les formes nommées relèvent du remap, pas de la normalisation ────
@pytest.mark.parametrize(
    "incoming,expected",
    [
        ({"type": "function", "function": {"name": "read"}}, {"type": "function", "name": "read"}),
        ({"type": "function", "name": "read"}, {"type": "function", "name": "read"}),
    ],
)
def test_named_forms_still_follow_the_rename(incoming, expected):
    """Non-régression : le choix d'outil nommé garde sa forme Responses."""
    out = pm._chat_to_responses_request(_chat(incoming))
    assert out["tool_choice"] == expected


def test_unknown_type_is_left_untouched():
    """Un type inconnu n'est ni deviné ni écrasé (aucune invention de valeur)."""
    tc = {"type": "allowed_tools", "mode": "auto"}
    out = pm._chat_to_responses_request(_chat(tc))
    assert out["tool_choice"] == tc


def test_absent_tool_choice_stays_absent():
    """Pas de ``tool_choice`` posé d'office : la sémantique amont est préservée."""
    out = pm._chat_to_responses_request(_chat(None))
    assert "tool_choice" not in out


# ── 3 : chemin Responses NATIF (contournait le premier correctif) ─────────
def test_native_responses_path_is_normalized_too():
    """``_sanitize_native_responses_request`` applique la même normalisation."""
    req = {
        "model": "muse-spark-1.3-contributor",
        "input": [{"role": "user", "content": "x"}],
        "tools": [{"type": "function", "name": "read", "parameters": {}}],
        "tool_choice": {"type": "auto"},
    }
    out = pm._sanitize_native_responses_request(req)
    assert out["tool_choice"] == "auto"


def test_native_path_keeps_named_form():
    """Non-régression sur le chemin natif : la forme nommée est préservée."""
    req = {
        "model": "m",
        "input": [{"role": "user", "content": "x"}],
        "tools": [{"type": "function", "name": "read", "parameters": {}}],
        "tool_choice": {"type": "function", "name": "read"},
    }
    out = pm._sanitize_native_responses_request(req)
    assert out["tool_choice"] == {"type": "function", "name": "read"}


# ── 4 : chemin Anthropic (le client de compactage) ───────────────────────
def test_anthropic_to_responses_normalizes_client_form():
    """``/v1/messages`` → ``/responses`` : le corps Anthropic est normalisé."""
    anthro = {
        "model": "muse-spark-1.3-contributor",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "read", "input_schema": {"type": "object", "properties": {}}}],
        "tool_choice": {"type": "auto"},
    }
    out = pm._anthropic_to_responses_request(anthro)
    assert out["tool_choice"] == "auto"


# ── 5 : le corps émis est sérialisable et cohérent ───────────────────────
def test_emitted_body_is_wire_serializable():
    """Le corps final doit porter une chaîne, pas un objet, côté wire."""
    out = pm._chat_to_responses_request(_chat({"type": "auto"}))
    wire = json.loads(json.dumps(out))
    assert isinstance(wire["tool_choice"], str)
    assert wire["tool_choice"] == "auto"