"""Verrouille la copie exacte du client officiel (gate gratuit durci).

Contexte mesuré le 2026-10-25 : depuis le 2026-10-23T23:17:27Z, le gate
gratuit exige la signature COMPLÈTE du client (10 tools + prompt système).
Un corps réduit est refusé 403 (``FreeTierError``) ou 429.

Ces tests empêchent une future « optimisation » de re-minimiser le payload
et de re-casser silencieusement toute la jambe free.
"""

import json

import opencode as oc

OFFICIAL_TOOL_NAMES = [
    "bash",
    "edit",
    "glob",
    "grep",
    "read",
    "skill",
    "task",
    "todowrite",
    "webfetch",
    "write",
]

SYSTEM_MARKER = "You are opencode, an interactive CLI tool"


def test_le_fichier_de_capture_est_complet():
    """Le payload figé porte bien les 10 tools et le prompt système."""
    payload = oc._official_free_payload()
    names = [t["function"]["name"] for t in payload["tools"]]
    assert names == OFFICIAL_TOOL_NAMES
    assert payload["names"] == set(OFFICIAL_TOOL_NAMES)
    assert payload["system"]["role"] == "system"
    assert SYSTEM_MARKER in payload["system"]["content"]
    assert len(payload["system"]["content"]) > 20000


def test_un_corps_minimal_recoit_la_copie_integrale():
    """Un client générique (4 mots) doit partir avec la signature officielle."""
    body = {"model": "m", "messages": [{"role": "user", "content": "Dis OK"}]}
    wire, forced = oc._free_wire_body(body, force_stream=True)

    assert forced is True
    assert wire["stream"] is True
    assert wire["tool_choice"] == "auto"
    assert [t["function"]["name"] for t in wire["tools"]] == OFFICIAL_TOOL_NAMES
    assert wire["messages"][0]["role"] == "system"
    assert SYSTEM_MARKER in wire["messages"][0]["content"]
    # Les messages du client sont conservés après le prompt officiel.
    assert wire["messages"][-1] == {"role": "user", "content": "Dis OK"}
    # L'entrée n'est jamais mutée (retries/hedges réutilisent le dict).
    assert "tools" not in body and "stream" not in body


def test_un_corps_deja_conforme_traverse_sans_etre_modifie():
    """Le vrai client (payload complet) ne doit pas être altéré par le proxy."""
    payload = oc._official_free_payload()
    body = {
        "model": "m",
        "max_tokens": 32,
        "messages": [dict(payload["system"]), {"role": "user", "content": "hi"}],
        "tools": payload["tools"],
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    wire, forced = oc._free_wire_body(body, force_stream=True)

    assert forced is False
    assert wire == body
    assert oc._serialize_json_body(wire) == oc._serialize_json_body(body)


def test_l_ordre_des_cles_reste_celui_du_client():
    """Ordre officiel : model, max_tokens, messages, tools, tool_choice, stream."""
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    wire, _ = oc._free_wire_body(body, force_stream=True)
    cles = [k for k in wire if k in ("model", "max_tokens", "messages", "tools", "tool_choice", "stream")]
    assert cles == ["model", "messages", "tools", "tool_choice", "stream"]


def test_le_payload_est_serialisable_et_stable():
    """Deux appels donnent exactement les mêmes octets (pas de dérive)."""
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    a, _ = oc._free_wire_body(body, force_stream=True)
    b, _ = oc._free_wire_body(body, force_stream=True)
    assert oc._serialize_json_body(a) == oc._serialize_json_body(b)
    # Le payload figé n'est pas modifié par les passages successifs.
    assert json.loads(json.dumps(oc._official_free_payload()["tools"])) == oc._official_free_payload()["tools"]
