"""Formes courtes de `input` / `content` sur la façade Responses.

Contexte mesuré le 2026-10-25 : `openai_responses_to_anthropic` itérait
`body["input"]` comme une liste. Une chaîne (`"input": "texte"`) était donc
parcourue caractère par caractère, chaque caractère écarté par le test
`isinstance(item, dict)` — zéro message. Le modèle recevait une requête vide
et répondait « Bonjour, comment puis-je vous aider ? ». Même problème pour
`"content": "texte"` dans un item.

Ces formes sont très répandues (SDK OpenAI, appels manuels) : elles doivent
produire exactement la même chose que la forme canonique à parts
`{"type": "input_text", ...}`.
"""

from app.protocol.mapping import openai_responses_to_anthropic


def _texts(out):
    return [
        b.get("text", "")
        for m in out.get("messages") or []
        for b in (m.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "text"
    ]


def test_input_string_produit_un_message_utilisateur():
    out = openai_responses_to_anthropic({"input": "Bonjour le monde"})
    assert _texts(out) == ["Bonjour le monde"]
    assert out["messages"][0]["role"] == "user"


def test_content_string_produit_un_bloc_texte():
    out = openai_responses_to_anthropic({"input": [{"role": "user", "content": "Combien font 2+2 ?"}]})
    assert _texts(out) == ["Combien font 2+2 ?"]


def test_system_string_devient_le_systeme():
    out = openai_responses_to_anthropic(
        {
            "input": [
                {"role": "system", "content": "Tu es bref."},
                {"role": "user", "content": "Salut"},
            ]
        }
    )
    assert out.get("system") == "Tu es bref."
    assert _texts(out) == ["Salut"]


def test_forme_canonique_inchangee():
    out = openai_responses_to_anthropic(
        {"input": [{"role": "user", "content": [{"type": "input_text", "text": "ok"}]}]}
    )
    assert _texts(out) == ["ok"]


def test_input_absent_ne_leve_pas():
    out = openai_responses_to_anthropic({})
    assert out.get("messages") == []
