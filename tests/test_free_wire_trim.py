"""Trim de la jambe free : le contexte recent doit survivre.

Regression 2026-10-30 : ``_trim_free_wire_messages`` supprimait tout message
``assistant`` en tete du suffixe conserve. Entre deux tours, le modele ne
voyait donc plus sa propre reponse precedente (souvent le travail en cours)
et repondait « je ne sais plus ou j'en suis ». Ces tests verrouillent les
invariants du suffixe : on ne coupe que les tetes structurellement
inutilisables (tool orphelin, tool_calls sans resultat).
"""

import sys

sys.path.insert(0, ".")

import opencode as oc  # noqa: E402


def _msg(role, content, **extra):
    return {"role": role, "content": content, **extra}


def _wire(*messages):
    return {"model": "m", "stream": True, "messages": list(messages)}


def _cap_for(wire, keep):
    """Budget exact pour ne garder que ``keep`` (hors systemes)."""
    base = {k: v for k, v in wire.items() if k != "messages"}
    total = len(oc._serialize_json_body(base))
    total += sum(oc._free_message_size(m) for m in wire["messages"] if m.get("role") == "system")
    total += sum(oc._free_message_size(m) for m in keep)
    return total


def test_previous_assistant_reply_survives_trim():
    system = _msg("system", "official prompt")
    task = _msg("user", "TASK: refactor the parser")
    previous_question = _msg("user", "can you update the tests?")
    previous_answer = _msg("assistant", "I updated tests/test_parser.py and the suite passes.")
    current = _msg("user", "continue")
    wire = _wire(system, task, previous_question, previous_answer, current)

    out, removed = oc._trim_free_wire_messages(wire, _cap_for(wire, [previous_answer, current]))

    roles = [m.get("role") for m in out["messages"]]
    assert removed == 2
    assert roles == ["system", "assistant", "user"]
    assert out["messages"][1] is previous_answer, "la reponse precedente doit rester"
    assert out["messages"][2] is current
    # Le corps client n'est jamais mute.
    assert wire["messages"][3] is previous_answer


def test_orphan_tool_result_is_dropped_from_suffix_head():
    system = _msg("system", "official prompt")
    task = _msg("user", "TASK")
    tool_result = _msg("tool", "file contents", tool_call_id="call_1")
    current = _msg("user", "continue")
    wire = _wire(system, task, tool_result, current)

    out, _ = oc._trim_free_wire_messages(wire, _cap_for(wire, [tool_result, current]))

    roles = [m.get("role") for m in out["messages"]]
    assert "tool" not in roles
    assert out["messages"][-1] is current


def test_incomplete_tool_call_chain_is_dropped():
    system = _msg("system", "official prompt")
    task = _msg("user", "TASK")
    call = _msg(
        "assistant",
        "",
        tool_calls=[
            {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            {"id": "call_2", "type": "function", "function": {"name": "read", "arguments": "{}"}},
        ],
    )
    partial_result = _msg("tool", "only one result", tool_call_id="call_1")
    current = _msg("user", "continue")
    wire = _wire(system, task, call, partial_result, current)

    out, _ = oc._trim_free_wire_messages(wire, _cap_for(wire, [call, partial_result, current]))

    roles = [m.get("role") for m in out["messages"]]
    assert "tool" not in roles
    assert "assistant" not in roles
    assert out["messages"][-1] is current


def test_complete_tool_call_chain_is_kept():
    system = _msg("system", "official prompt")
    task = _msg("user", "TASK")
    call = _msg(
        "assistant",
        "",
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
    )
    result = _msg("tool", "file contents", tool_call_id="call_1")
    current = _msg("user", "continue")
    wire = _wire(system, task, call, result, current)

    out, _ = oc._trim_free_wire_messages(wire, _cap_for(wire, [call, result, current]))

    roles = [m.get("role") for m in out["messages"]]
    assert roles == ["system", "assistant", "tool", "user"]
    assert out["messages"][1] is call
    assert out["messages"][2] is result


def test_huge_tool_result_is_truncated_not_dropped():
    """Un dump d'outil de 60 Ko ne doit pas emporter tout le tour courant.

    Régression 2026-10-30 (harnais DeepSeek) : le `break` sur un message trop
    gros supprimait l'assistant tool_calls ET son résultat, alors que la
    question courante suivait. Le modèle ne voyait plus que la question.
    """
    system = _msg("system", "official prompt")
    task = _msg("user", "TASK: le mot de passe est BANANE-42")
    call = _msg(
        "assistant",
        "",
        tool_calls=[
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ],
    )
    result = _msg("tool", "x" * 60000, tool_call_id="call_1")
    current = _msg("user", "donne-moi le mot de passe")
    wire = _wire(system, task, call, result, current)

    out, _removed = oc._trim_free_wire_messages(wire, 20000)

    roles = [m.get("role") for m in out["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert out["messages"][1] is task, "la tâche initiale reste l'ancre du fil"
    assert out["messages"][2] is call, "l'appel d'outil reste exécutable"
    assert "truncated by proxy" in out["messages"][3]["content"]
    assert out["messages"][-1] is current
    assert len(oc._serialize_json_body(out)) <= 20000
    # Le corps client n'est jamais tronqué.
    assert wire["messages"][3] is result
    assert wire["messages"][3]["content"] == "x" * 60000


def test_trim_preserves_utf8_when_truncating():
    system = _msg("system", "official prompt")
    task = _msg("user", "TASK")
    result = _msg("tool", "é" * 40000, tool_call_id="call_1")
    call = _msg(
        "assistant",
        "",
        tool_calls=[
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ],
    )
    current = _msg("user", "continue")
    wire = _wire(system, task, call, result, current)

    out, _ = oc._trim_free_wire_messages(wire, 12000)
    body = oc._serialize_json_body(out)
    assert len(body) <= 12000
    # Décodage UTF-8 valide (aucun caractère de remplacement injecté).
    body.decode("utf-8")
    assert "truncated by proxy" in out["messages"][3]["content"]


def test_first_user_message_is_added_only_when_it_fits():
    system = _msg("system", "official prompt")
    task = _msg("user", "TASK")
    previous = _msg("user", "recent question")
    answer = _msg("assistant", "recent answer")
    current = _msg("user", "continue")
    wire = _wire(system, task, previous, answer, current)

    # Budget pour le suffixe recent seulement : la tache initiale ne tient pas.
    tight, _ = oc._trim_free_wire_messages(wire, _cap_for(wire, [previous, answer, current]))
    assert task not in tight["messages"]

    # Budget plus large : la tache initiale revient en tete, sans ecraser le
    # suffixe (elle est ajoutee apres la descente, pas reservee).
    roomy, _ = oc._trim_free_wire_messages(wire, _cap_for(wire, [task, previous, answer, current]))
    roles = [m.get("role") for m in roomy["messages"]]
    assert roles == ["system", "user", "user", "assistant", "user"]
    assert roomy["messages"][1] is task
    assert roomy["messages"][-1] is current
