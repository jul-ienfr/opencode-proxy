"""[PLAN E1 2026-10-12] `tool_choice` émis sans demande — parité officielle.

L'officiel n'envoie `tool_choice` que si le client en a posé un. Avant ce
correctif, `anthropic_to_openai` posait `tool_choice: "auto"` d'office dès que
des outils étaient présents, même sur un corps sans la clé.

Couverture (site A uniquement — `app/protocol/mapping.py`, bloc `tools`) :
  1. corps avec outils SANS clé `tool_choice` → champ ABSENT de la sortie ;
  2. clé client explicite → conversion inchangée (auto/any/none/nommé) ;
  3. `disable_parallel_tool_use: true` seul (dict sans `type`) → `auto` émis
     (la clé client existe) + `parallel_tool_calls: false` toujours émis ;
  4. drapeau `protocol.emit_default_tool_choice: true` → ancien comportement
     (`"auto"` posé d'office) ;
  5. corps sans outils et sans clé → inchangé (pas de champ inventé).

Le drapeau est lu via `yaml_get` au moment de la conversion (hot-reload OK,
pas de cache module) ; les tests patchent le nom tel que vu par
`app.protocol.mapping` (pattern `tests/test_proxy.py`) et vident
`_anthropic_cache` (pattern `tests/test_conversion_cache.py`) — jamais
d'écriture du vrai `config.yaml` (`tests/test_no_real_config_write.py`).
"""

import pytest

from app.protocol import mapping as m


def _body(tool_choice_marker=True, tool_choice=None, parallel_only=False):
    body = {
        "model": "m",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [
            {
                "name": "read",
                "description": "Lire.",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    }
    if parallel_only:
        body["tool_choice"] = {"disable_parallel_tool_use": True}
    elif tool_choice_marker:
        body["tool_choice"] = tool_choice
    return body


@pytest.fixture(autouse=True)
def _clean_cache():
    m._anthropic_cache.clear()
    yield
    m._anthropic_cache.clear()


def test_absent_sans_cle_client():
    """Le défaut : outils présents, pas de clé client → pas de champ émis."""
    out = m.anthropic_to_openai(_body(tool_choice_marker=False), "m")
    assert "tool_choice" not in out


def test_sans_outils_sans_cle():
    """Corps nu : rien n'est inventé (ni champ, ni parallèle)."""
    body = {
        "model": "m",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "x"}],
    }
    out = m.anthropic_to_openai(body, "m")
    assert "tool_choice" not in out
    assert "parallel_tool_calls" not in out


@pytest.mark.parametrize(
    "incoming,expected",
    [
        ({"type": "auto"}, "auto"),
        ({"type": "any"}, "required"),
        ({"type": "none"}, "none"),
        ({"type": "tool", "name": "read"}, {"type": "function", "function": {"name": "read"}}),
        ("auto", "auto"),
        ("any", "required"),
        ("none", "none"),
    ],
)
def test_cle_explicite_inchangee(incoming, expected):
    """Une clé posée par le client suit la conversion historique."""
    out = m.anthropic_to_openai(_body(tool_choice=incoming), "m")
    assert out["tool_choice"] == expected


def test_disable_parallel_seul():
    """`disable_parallel_tool_use` sans `type` : la clé client EXISTE (un dict
    `tool_choice` a été posé), donc `auto` est émis comme avant — et le
    parallèle reste coupé. « Fonctionner seul » = pas de régression sur ce
    cas, pas une absence inventée."""
    out = m.anthropic_to_openai(_body(parallel_only=True), "m")
    assert out["parallel_tool_calls"] is False
    assert out["tool_choice"] == "auto"


def test_drapeau_true_restaure_ancien_comportement(monkeypatch):
    """Repli `protocol.emit_default_tool_choice: true` → `"auto"` d'office."""
    monkeypatch.setattr(
        m, "yaml_get", lambda s, k, d=None: True if (s, k) == ("protocol", "emit_default_tool_choice") else d
    )
    out = m.anthropic_to_openai(_body(tool_choice_marker=False), "m")
    assert out["tool_choice"] == "auto"


def test_drapeau_false_explicite(monkeypatch):
    """Drapeau à `false` explicite = défaut : champ absent sans clé client."""
    monkeypatch.setattr(
        m, "yaml_get", lambda s, k, d=None: False if (s, k) == ("protocol", "emit_default_tool_choice") else d
    )
    out = m.anthropic_to_openai(_body(tool_choice_marker=False), "m")
    assert "tool_choice" not in out
