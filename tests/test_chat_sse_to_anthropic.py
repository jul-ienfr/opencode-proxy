"""Tests hermétiques du convertisseur SSE Chat → Anthropic (aucun réseau).

Couvre ``app/protocol/chat_sse_to_anthropic.py``, extraction fidèle de
``opencode.py:10561-10710`` (boucle par delta) et ``opencode.py:8556-8660``
(``_finalize_stream``). Les cas visés sont ceux de la « jambe free »
(``opencode.py:9144+``) où un amont Chat alimente un client Anthropic.

Aucun appel réseau, aucun état global partagé entre tests.
"""

import json

import pytest

from app.protocol.chat_sse_to_anthropic import (
    ChatSseToAnthropicState,
    anthropic_stop_reason,
    chat_sse_to_anthropic_events,
)

# Nom d'outil > 64 caractères : cible du rename A8 côté amont Chat.
LONG_TOOL_NAME = "mcp__plugin_example__some_very_long_tool_name_" + "x" * 20
assert len(LONG_TOOL_NAME) > 64
SHORT_TOOL_NAME = LONG_TOOL_NAME[:57] + "-abc123"


def _events(chunks: list[str], state: ChatSseToAnthropicState) -> list[str]:
    """Passe une liste de lignes SSE brutes et concatène les événements émis."""
    out: list[str] = []
    for line in chunks:
        out.extend(chat_sse_to_anthropic_events(line, state=state))
    return out


def _names(events: list[str]) -> list[str]:
    """Types d'événements, dans l'ordre d'émission."""
    return [e.split("\n", 1)[0].removeprefix("event: ") for e in events]


def _data(events: list[str], event_name: str) -> list[dict]:
    """Charges utiles JSON des événements du type demandé."""
    out: list[dict] = []
    for e in events:
        lines = e.split("\n")
        if lines[0] == f"event: {event_name}":
            out.append(json.loads(lines[1].removeprefix("data: ")))
    return out


def _data_line(payload) -> str:
    """Construit une ligne SSE ``data:`` à partir d'un dict (ou d'une chaîne)."""
    if isinstance(payload, str):
        return f"data: {payload}"
    return f"data: {json.dumps(payload, ensure_ascii=False)}"


def _chunk(delta=None, finish_reason=None, usage=None) -> str:
    """Ligne SSE d'un ``chat.completion.chunk`` minimal."""
    body: dict = {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta or {}}]}
    if finish_reason is not None:
        body["choices"][0]["finish_reason"] = finish_reason
    if usage is not None:
        body["usage"] = usage
    return _data_line(body)


def test_texte_multi_chunks_un_seul_bloc():
    """Texte en plusieurs chunks : un seul content_block_start, plusieurs deltas."""
    # ``message_start`` est émis au PREMIER chunk exploitable (ici le delta de
    # rôle) : l'usage amont n'arrive qu'au chunk de fin, donc trop tard pour un
    # événement déjà émis. C'est pourquoi la référence P2 annonce une estimation
    # locale (``stream_in_est``, opencode.py:10584) et non l'usage réel — d'où
    # ``input_tokens_estimate``, que l'appelant fournit.
    st = ChatSseToAnthropicState(model="claude-haiku-4-5", message_id="msg_test1", input_tokens_estimate=12)
    events = _events(
        [
            _chunk({"role": "assistant"}),
            _chunk({"content": "Bon"}),
            _chunk({"content": "jour"}),
            _chunk({}, finish_reason="stop", usage={"prompt_tokens": 12, "completion_tokens": 3}),
            "data: [DONE]",
        ],
        st,
    )
    names = _names(events)

    assert names[0] == "message_start"
    assert names.count("content_block_start") == 1
    assert names.count("content_block_delta") == 2
    assert names.count("content_block_stop") == 1
    assert names[-3:] == ["content_block_stop", "message_delta", "message_stop"]

    start = _data(events, "content_block_start")[0]
    assert start == {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}

    deltas = _data(events, "content_block_delta")
    assert [d["delta"] for d in deltas] == [
        {"type": "text_delta", "text": "Bon"},
        {"type": "text_delta", "text": "jour"},
    ]
    assert {d["index"] for d in deltas} == {0}

    ms = _data(events, "message_start")[0]["message"]
    assert ms["id"] == "msg_test1"
    assert ms["type"] == "message"
    assert ms["role"] == "assistant"
    assert ms["content"] == []
    assert ms["model"] == "claude-haiku-4-5"
    assert ms["stop_reason"] is None
    assert ms["stop_sequence"] is None
    assert ms["usage"] == {"input_tokens": 12, "output_tokens": 0, "cache_read_input_tokens": 0}

    md = _data(events, "message_delta")[0]
    assert md["delta"] == {"stop_reason": "end_turn", "stop_sequence": None}
    assert md["usage"] == {"output_tokens": 3}

    # Le format des chaînes est exactement celui de _sse (opencode.py:7945-7946).
    assert events[0].startswith("event: message_start\ndata: {")
    assert events[0].endswith("}\n\n")


def test_arguments_outil_en_plusieurs_fragments():
    """Un appel d'outil dont les arguments arrivent en fragments : un seul bloc."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_tools_frag")
    events = _events(
        [
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "toolu_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"ci'},
                        }
                    ]
                }
            ),
            _chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'ty": "Par'}}]}),
            _chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'is"}'}}]}),
            _chunk({}, finish_reason="tool_calls"),
            "data: [DONE]",
        ],
        st,
    )
    names = _names(events)

    assert names.count("content_block_start") == 1
    starts = _data(events, "content_block_start")
    assert starts[0]["content_block"] == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "get_weather",
        "input": {},
    }
    assert starts[0]["index"] == 0

    deltas = _data(events, "content_block_delta")
    assert names.count("content_block_delta") == 3
    assert all(d["delta"]["type"] == "input_json_delta" for d in deltas)
    assert {d["index"] for d in deltas} == {0}
    concatenated = "".join(d["delta"]["partial_json"] for d in deltas)
    assert json.loads(concatenated) == {"city": "Paris"}

    assert _data(events, "message_delta")[0]["delta"]["stop_reason"] == "tool_use"
    assert names.count("content_block_stop") == 1


def test_deux_appels_outils_deux_blocs_distincts():
    """Deux tool_calls → deux blocs, indices distincts, deux content_block_stop."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_two_tools")
    events = _events(
        [
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "toolu_a",
                            "function": {"name": "alpha", "arguments": '{"a":1}'},
                        },
                        {
                            "index": 1,
                            "id": "toolu_b",
                            "function": {"name": "beta", "arguments": '{"b":2}'},
                        },
                    ]
                }
            ),
            _chunk({"tool_calls": [{"index": 1, "function": {"arguments": "}"}}]}),
            _chunk({}, finish_reason="tool_calls"),
            "data: [DONE]",
        ],
        st,
    )

    starts = _data(events, "content_block_start")
    assert [s["index"] for s in starts] == [0, 1]
    assert [s["content_block"]["name"] for s in starts] == ["alpha", "beta"]
    assert [s["content_block"]["id"] for s in starts] == ["toolu_a", "toolu_b"]

    # Le fragment tardif de l'appel #1 retombe sur le bloc de l'appel #1.
    late = [d for d in _data(events, "content_block_delta") if d["delta"]["partial_json"] == "}"]
    assert [d["index"] for d in late] == [1]

    stops = _data(events, "content_block_stop")
    assert [s["index"] for s in stops] == [0, 1]
    # Les deux blocs sont clôturés (dans l'ordre) juste avant message_delta.
    assert _names(events) == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]


def test_bloc_thinking_depuis_reasoning_content():
    """reasoning_content → bloc thinking ouvert paresseusement, delta thinking_delta."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_think")
    events = _events(
        [
            _chunk({"reasoning_content": "je réflé"}),
            _chunk({"reasoning_content": "chis"}),
            _chunk({"content": "voilà"}),
            _chunk({}, finish_reason="stop"),
            "data: [DONE]",
        ],
        st,
    )

    starts = _data(events, "content_block_start")
    assert [s["content_block"]["type"] for s in starts] == ["thinking", "text"]
    assert starts[0]["content_block"] == {"type": "thinking", "thinking": ""}
    assert [s["index"] for s in starts] == [0, 1]

    thinking_deltas = [
        d for d in _data(events, "content_block_delta") if d["delta"]["type"] == "thinking_delta"
    ]
    # Type de delta = celui de opencode.py:10648 (jamais "reasoning_delta").
    assert [d["delta"] for d in thinking_deltas] == [
        {"type": "thinking_delta", "thinking": "je réflé"},
        {"type": "thinking_delta", "thinking": "chis"},
    ]
    assert [d["index"] for d in thinking_deltas] == [0, 0]

    text_deltas = [d for d in _data(events, "content_block_delta") if d["delta"]["type"] == "text_delta"]
    assert text_deltas == [
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "voilà"}}
    ]
    # Les deux blocs (thinking puis texte) sont clôturés dans l'ordre d'ouverture.
    assert [s["index"] for s in _data(events, "content_block_stop")] == [0, 1]
    assert _names(events)[-4:] == [
        "content_block_stop",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]


def test_bloc_thinking_depuis_reasoning():
    """La clé `reasoning` (variante) ouvre le même bloc thinking."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_reason")
    events = _events([_chunk({"reasoning": "hmm"}), "data: [DONE]"], st)
    deltas = _data(events, "content_block_delta")
    assert deltas == [
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "hmm"},
        }
    ]


def test_pas_de_bloc_thinking_quand_absent():
    """Aucun raisonnement → aucun bloc thinking (ouverture paresseuse)."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_nothink")
    events = _events([_chunk({"content": "x"}), "data: [DONE]"], st)
    assert [s["content_block"]["type"] for s in _data(events, "content_block_start")] == ["text"]


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [
        ("stop", "end_turn"),
        ("length", "max_tokens"),
        ("tool_calls", "tool_use"),
        ("function_call", "tool_use"),
        ("content_filter", "end_turn"),
        ("autre_inconnu", "end_turn"),
        (None, "end_turn"),
    ],
)
def test_correspondance_stop_reason(finish_reason, expected):
    """Table de correspondance des finish_reason Chat → stop_reason Anthropic."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_stop")
    events = _events([_chunk({"content": "x"}), _chunk({}, finish_reason=finish_reason), "data: [DONE]"], st)
    assert _data(events, "message_delta")[0]["delta"]["stop_reason"] == expected


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [
        ("stop", "end_turn"),
        ("length", "max_tokens"),
        ("tool_calls", "tool_use"),
        ("function_call", "tool_use"),
        ("content_filter", "end_turn"),
        ("inconnu", "end_turn"),
        (None, "end_turn"),
    ],
)
def test_fonction_stop_reason_directe(finish_reason, expected):
    """La fonction de correspondance est exposée et testable isolément."""
    assert anthropic_stop_reason(finish_reason) == expected
    assert anthropic_stop_reason(None, has_tool_use=True) == "tool_use"


def test_done_idempotent():
    """[DONE] n'émet la clôture qu'une fois ; un second appel ne ré-émet rien."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_done")
    first = _events([_chunk({"content": "x"}), "data: [DONE]"], st)
    assert _names(first).count("message_stop") == 1
    assert _names(first).count("content_block_stop") == 1

    second = chat_sse_to_anthropic_events("data: [DONE]", state=st)
    assert second == []

    third = chat_sse_to_anthropic_events("data: [DONE]", state=st)
    assert third == []


def test_chunk_apres_done_ne_rouvre_rien():
    """Après clôture, un chunk tardif ne peut pas rouvrir un bloc ni un message."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_after_done")
    _events([_chunk({"content": "x"}), "data: [DONE]"], st)
    assert chat_sse_to_anthropic_events(_chunk({"content": "tard"}), state=st) == []


def test_flux_vide_recoit_message_start_puis_cloture():
    """Un flux clos sans aucune donnée reçoit tout de même message_start (opencode.py:8588)."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_empty")
    events = chat_sse_to_anthropic_events("data: [DONE]", state=st)
    assert _names(events) == ["message_start", "message_delta", "message_stop"]
    assert _data(events, "message_delta")[0]["delta"]["stop_reason"] == "end_turn"


@pytest.mark.parametrize(
    "line",
    [
        "data: {ceci n'est pas du json}",
        "data: not json at all",
        "data: [1, 2, 3]",
        "data: 42",
        'data: "chaine"',
        "data: null",
        "data:",
        "data:    ",
    ],
)
def test_ligne_invalide_retourne_liste_vide(line):
    """JSON invalide ou non-objet → [] sans exception."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_bad")
    assert chat_sse_to_anthropic_events(line, state=st) == []


@pytest.mark.parametrize(
    "line",
    [
        "",
        ": keep-alive",
        "event: message_start",
        "event: content_block_delta",
        "id: 42",
        "retry: 1000",
        "  ",
    ],
)
def test_lignes_ignorees(line):
    """Ligne vide, commentaire, event:/id:/retry: → [] (ignorées)."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_ignored")
    assert chat_sse_to_anthropic_events(line, state=st) == []
    # Aucune donnée n'a été consommée : le stream n'a pas démarré.
    assert st.started is False


def test_json_deja_parse_est_utilise():
    """`parsed` évite un second parse et est prioritaire sur raw_line."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_parsed")
    events = chat_sse_to_anthropic_events(
        "data: {pas du json}",
        parsed={"choices": [{"delta": {"content": "ok"}}]},
        state=st,
    )
    assert [d["delta"]["text"] for d in _data(events, "content_block_delta")] == ["ok"]


def test_restauration_nom_outil_long():
    """Un nom d'outil > 64 caractères est restauré depuis tool_name_map (A8)."""
    st = ChatSseToAnthropicState(
        model="m",
        message_id="msg_restore",
        tool_name_map={SHORT_TOOL_NAME: LONG_TOOL_NAME},
    )
    events = _events(
        [
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "toolu_long",
                            "function": {"name": SHORT_TOOL_NAME, "arguments": "{}"},
                        }
                    ]
                }
            ),
            "data: [DONE]",
        ],
        st,
    )
    start = _data(events, "content_block_start")[0]
    assert start["content_block"]["name"] == LONG_TOOL_NAME
    assert len(start["content_block"]["name"]) > 64


def test_restauration_nom_outil_sans_map():
    """Sans map, le nom reçu est émis tel quel (pas de rename fantôme)."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_nomap")
    events = _events(
        [_chunk({"tool_calls": [{"index": 0, "id": "t", "function": {"name": "court", "arguments": ""}}]}), "data: [DONE]"],
        st,
    )
    assert _data(events, "content_block_start")[0]["content_block"]["name"] == "court"


def test_state_none_leve_value_error():
    """state=None → ValueError (garantie anti-fuite entre streams)."""
    with pytest.raises(ValueError):
        chat_sse_to_anthropic_events(_chunk({"content": "x"}))
    with pytest.raises(ValueError):
        chat_sse_to_anthropic_events("data: [DONE]")
    with pytest.raises(ValueError):
        chat_sse_to_anthropic_events("event: message_start", state=None)


def test_usage_alimente_message_start_et_message_delta():
    """chunk.usage alimente input_tokens (message_start) et output_tokens (message_delta)."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_usage")
    events = _events(
        [
            _chunk({"content": "a"}, usage={"prompt_tokens": 7, "completion_tokens": 1}),
            _chunk({"content": "b"}, usage={"prompt_tokens": 7, "completion_tokens": 9}),
            "data: [DONE]",
        ],
        st,
    )
    assert _data(events, "message_start")[0]["message"]["usage"]["input_tokens"] == 7
    assert _data(events, "message_delta")[0]["usage"]["output_tokens"] == 9


def test_indices_de_blocks_sequentiels():
    """Indices alloués séquentiellement à partir de 0, un par bloc."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_idx")
    events = _events(
        [
            _chunk({"reasoning_content": "r"}),
            _chunk({"content": "t"}),
            _chunk({"tool_calls": [{"index": 0, "id": "t0", "function": {"name": "n0", "arguments": "{}"}}]}),
            _chunk({"tool_calls": [{"index": 1, "id": "t1", "function": {"name": "n1", "arguments": "{}"}}]}),
            "data: [DONE]",
        ],
        st,
    )
    assert [s["index"] for s in _data(events, "content_block_start")] == [0, 1, 2, 3]
    assert [s["index"] for s in _data(events, "content_block_stop")] == [0, 1, 2, 3]
    assert st.next_block_idx == 4


def test_etancheite_deux_streams_entrelaces():
    """Bug de globals (mapping.py:3720-3726) : deux états entrelacés ne se contaminent pas."""
    map_a = {"court_a": "outil_A_" + "a" * 70}
    map_b = {"court_b": "outil_B_" + "b" * 70}
    st_a = ChatSseToAnthropicState(model="modele_A", message_id="msg_A", tool_name_map=map_a)
    st_b = ChatSseToAnthropicState(model="modele_B", message_id="msg_B", tool_name_map=map_b)

    seq_a = [
        _chunk({"content": "A1"}),
        _chunk({"tool_calls": [{"index": 0, "id": "ta", "function": {"name": "court_a", "arguments": '{"a"'}}]}),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": ":1}"}}]}),
        "data: [DONE]",
    ]
    seq_b = [
        _chunk({"reasoning_content": "B-reflexion"}),
        _chunk({"content": "B1"}),
        _chunk({"tool_calls": [{"index": 0, "id": "tb", "function": {"name": "court_b", "arguments": "{}"}}]}),
        _chunk({}, finish_reason="length"),
        "data: [DONE]",
    ]

    ev_a: list[str] = []
    ev_b: list[str] = []
    # Entrelacement strict : un événement de A, un de B, etc.
    for i in range(max(len(seq_a), len(seq_b))):
        if i < len(seq_a):
            ev_a.extend(chat_sse_to_anthropic_events(seq_a[i], state=st_a))
        if i < len(seq_b):
            ev_b.extend(chat_sse_to_anthropic_events(seq_b[i], state=st_b))

    # Aucune fuite d'identité de stream.
    assert _data(ev_a, "message_start")[0]["message"]["id"] == "msg_A"
    assert _data(ev_a, "message_start")[0]["message"]["model"] == "modele_A"
    assert _data(ev_b, "message_start")[0]["message"]["id"] == "msg_B"
    assert _data(ev_b, "message_start")[0]["message"]["model"] == "modele_B"

    # Aucune fuite de blocs : A = texte + 1 outil, B = thinking + texte + 1 outil.
    starts_a = _data(ev_a, "content_block_start")
    starts_b = _data(ev_b, "content_block_start")
    assert [(s["index"], s["content_block"]["type"]) for s in starts_a] == [(0, "text"), (1, "tool_use")]
    assert [(s["index"], s["content_block"]["type"]) for s in starts_b] == [
        (0, "thinking"),
        (1, "text"),
        (2, "tool_use"),
    ]

    # Aucune fuite de map de noms : chaque stream restaure SON nom d'origine.
    assert starts_a[1]["content_block"]["name"] == map_a["court_a"]
    assert starts_b[2]["content_block"]["name"] == map_b["court_b"]

    # Aucune fuite de stop_reason / usage.
    assert _data(ev_a, "message_delta")[0]["delta"]["stop_reason"] == "tool_use"
    assert _data(ev_b, "message_delta")[0]["delta"]["stop_reason"] == "max_tokens"

    # Chaque stream a sa propre clôture, une seule fois.
    assert _names(ev_a).count("message_stop") == 1
    assert _names(ev_b).count("message_stop") == 1
    assert [s["index"] for s in _data(ev_a, "content_block_stop")] == [0, 1]
    assert [s["index"] for s in _data(ev_b, "content_block_stop")] == [0, 1, 2]


def test_pas_detat_mutable_au_niveau_module():
    """Aucun conteneur mutable module-level : l'état est porté par l'instance."""
    import app.protocol.chat_sse_to_anthropic as mod

    mutables = {
        name: value
        for name, value in vars(mod).items()
        if not name.startswith("__") and isinstance(value, (dict, list, set, bytearray))
    }
    assert mutables == {}, f"état module-level mutable détecté (bug de globals) : {sorted(mutables)}"


def test_constructeur_par_defaut_genere_un_id():
    """Sans message_id, un identifiant `msg_…` est généré ; deux états diffèrent."""
    a = ChatSseToAnthropicState()
    b = ChatSseToAnthropicState()
    assert a.message_id.startswith("msg_")
    assert a.message_id != b.message_id
    assert a.model == ""
    assert a.tool_name_map is None
    assert a.started is False
    assert a.finished is False


def test_reset_remet_letat_a_zero():
    """reset() purge blocs/usage sans changer l'identité du stream."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_reset")
    _events([_chunk({"content": "x"}), _chunk({}, finish_reason="stop"), "data: [DONE]"], st)
    st.reset()
    assert (st.started, st.finished, st.next_block_idx) == (False, False, 0)
    assert st.open_blocks == []
    assert st.tool_block_idx == {}
    assert (st.input_tokens, st.output_tokens, st.finish_reason) == (0, 0, None)
    assert st.message_id == "msg_reset"
