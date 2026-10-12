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
    assert ms["usage"] == {
        "input_tokens": 12,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    md = _data(events, "message_delta")[0]
    assert md["delta"] == {"stop_reason": "end_turn", "stop_sequence": None}
    assert md["usage"] == {"output_tokens": 3, "cache_read_input_tokens": 0}


def test_message_delta_reports_cache_read():
    """F4 : le cache amont remonte au client SSE (avant : toujours 0)."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_cache")
    events = _events(
        [
            _chunk({"content": "x"}),
            _chunk(
                {},
                finish_reason="stop",
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 60},
                },
            ),
            "data: [DONE]",
        ],
        st,
    )
    md = _data(events, "message_delta")[0]
    assert md["usage"] == {"output_tokens": 5, "cache_read_input_tokens": 60}

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
    assert starts[0]["content_block"] == {"type": "thinking", "thinking": "", "signature": ""}
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
    from app.protocol.mapping import _local_signature

    st = ChatSseToAnthropicState(model="m", message_id="msg_reason")
    events = _events([_chunk({"reasoning": "hmm"}), "data: [DONE]"], st)
    deltas = _data(events, "content_block_delta")
    # thinking_delta puis, à la clôture, signature_delta forgée locale (parité
    # chemin natif) — sans elle un client strict abandonne le bloc au tour suivant.
    assert deltas == [
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "hmm"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": _local_signature("hmm")},
        },
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
        # Parité officielle : sans finish_reason amont (coupure), on signale
        # `error` au lieu de déguiser en `end_turn` qui forçait un `continue`.
        (None, "error"),
    ],
)
def test_correspondance_stop_reason(finish_reason, expected):
    """Table de correspondance des finish_reason Chat → stop_reason Anthropic."""
    st = ChatSseToAnthropicState(model="m", message_id="msg_stop")
    if finish_reason is None:
        chunks = [_chunk({"content": "x"}), "data: [DONE]"]
    else:
        chunks = [_chunk({"content": "x"}), _chunk({}, finish_reason=finish_reason), "data: [DONE]"]
    events = _events(chunks, st)
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
    # Parité officielle : vide sans terminal = troncation → `error`, pas `end_turn`.
    assert _data(events, "message_delta")[0]["delta"]["stop_reason"] == "error"


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
        _chunk({}, finish_reason="tool_calls"),
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


# ═══════════════════════════════════════════════════════════════════════
# F1 — panne amont portée par une ligne ``data: {"error": …}``
#
# Défaut d'origine : ``chat.completion.chunk`` n'a pas de clé ``error``, donc
# une passerelle qui signale une panne milieu de flux (HTTP déjà 200) faisait
# tomber la ligne dans le cas « pas de choices » — ``delta`` valant ``{}``,
# ``feed_chunk`` rendait ``[]``. Le message ET le type amont étaient perdus,
# et le client ne voyait qu'un ``stop_reason: error`` nu, sans ``event:
# error`` : impossible de classer la panne, donc impossible de retry.
# ═══════════════════════════════════════════════════════════════════════


class TestF1ErreurAmontDansLeFlux:
    def _err(self, payload) -> str:
        return _data_line({"error": payload})

    def test_erreur_seule_emet_event_error_type_et_message(self):
        """Le cas nu : la ligne d'erreur seule produit un `event: error`."""
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1a")
        ev = _events([self._err({"message": "upstream exploded", "type": "server_error"})], st)

        assert "error" in _names(ev)
        payload = _data(ev, "error")[0]
        assert payload["type"] == "error"
        assert payload["error"]["message"] == "upstream exploded"
        # `server_error` n'appartient pas au vocabulaire Anthropic : il doit
        # être rapproché d'un type que le client sait classer.
        assert payload["error"]["type"] == "overloaded_error"

    def test_event_error_precede_la_cloture(self):
        """Ordre : `error` AVANT `message_stop`. Un client strict lève sur
        l'erreur ; un client souple hérite quand même d'une clôture propre."""
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1b")
        ev = _events([self._err({"message": "boom", "type": "api_error"})], st)
        names = _names(ev)

        assert names[0] == "error"
        assert names.index("error") < names.index("message_stop")
        assert names[-1] == "message_stop"
        assert names.count("message_stop") == 1

    def test_erreur_apres_texte_ne_laisse_pas_de_bloc_ouvert(self):
        """Texte déjà coulé + panne : le bloc doit être fermé, sinon un client
        strict jette tout le tour — la panne coûterait plus cher qu'elle-même."""
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1c")
        ev = _events(
            [
                _chunk({"content": "réponse partielle"}),
                self._err({"message": "connection reset", "type": "internal_error"}),
            ],
            st,
        )
        names = _names(ev)

        assert "error" in names
        # Bloc 0 ouvert par le texte, puis refermé : jamais de `message_stop`
        # sur un bloc encore ouvert.
        assert [s["index"] for s in _data(ev, "content_block_stop")] == [0]
        assert names.index("content_block_stop") < names.index("message_stop")
        # La clôture reste honnête sur la cause.
        assert _data(ev, "message_delta")[0]["delta"]["stop_reason"] == "error"

    def test_done_apres_erreur_ne_reemet_rien(self):
        """Une passerelle émettant `data: [DONE]` APRÈS l'erreur ne doit pas
        produire une seconde clôture (bug de double `message_stop`)."""
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1d")
        ev = _events(
            [
                self._err({"message": "rate limited", "type": "rate_limit_exceeded", "code": 429}),
                "data: [DONE]",
            ],
            st,
        )

        assert _names(ev).count("message_stop") == 1
        assert _names(ev).count("error") == 1
        assert st.finished is True

    def test_chunk_apres_erreur_ignore(self):
        """Même garantie pour un chunk de texte tardif (amont bavard)."""
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1e")
        ev = _events(
            [
                self._err({"message": "boom"}),
                _chunk({"content": "trop tard"}),
            ],
            st,
        )
        assert "content_block_delta" not in _names(ev)
        assert _names(ev).count("message_stop") == 1

    def test_usage_de_lerreur_est_conserve(self):
        """La branche est placée APRÈS la reprise d'usage : une passerelle qui
        rend son usage final à côté de l'erreur ne le perd pas."""
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1f")
        ev = _events(
            [
                _data_line(
                    {
                        "error": {"message": "quota épuisé", "type": "insufficient_quota"},
                        "usage": {"prompt_tokens": 40, "completion_tokens": 12},
                    }
                )
            ],
            st,
        )
        assert (st.input_tokens, st.output_tokens) == (40, 12)
        assert _data(ev, "message_delta")[0]["usage"]["output_tokens"] == 12

    def test_erreur_avant_tout_message_start_quand_meme_emise(self):
        """Flux d'erreur pur : `error` d'abord, puis la clôture complète —
        y compris `message_start`, qu'un flux vide reçoit lui aussi."""
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1g")
        ev = _events([self._err({"message": "refusé"})], st)
        names = _names(ev)

        assert names[0] == "error"
        assert "message_start" in names
        assert names[-1] == "message_stop"
        assert st.started is True

    def test_erreur_chaine_libre_est_relayee(self):
        """Forme `{"error": "texte"}` (pas de dict) : le texte survit quand même."""
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1h")
        ev = _events([self._err("Too Many Requests, slow down")], st)
        payload = _data(ev, "error")[0]
        assert payload["error"]["message"] == "Too Many Requests, slow down"
        assert payload["error"]["type"] == "rate_limit_error"

    def test_erreur_sans_message_rend_la_charge_utile(self):
        """Ni `message`, ni `detail`, ni texte : on rend la charge utile
        entière. Un diagnostic illisible vaut mieux qu'un message vide."""
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1i")
        ev = _events([self._err({"code": "mystery_failure", "upstream_trace": "abc-123"})], st)
        message = _data(ev, "error")[0]["error"]["message"]
        assert "mystery_failure" in message and "abc-123" in message

    @pytest.mark.parametrize(
        "raw",
        [
            '{"error": null}',
            '{"error": ""}',
            '{"error": []}',
            '{"error": {}}',
            '{"error": 0}',
            '{"error": false}',
        ],
    )
    def test_error_vide_ou_nul_ne_declenche_pas_derreur(self, raw):
        """Un `error` vide/nul n'est PAS une panne : le flux continue normalement.

        Sans ce garde, une passerelle posant `error: null` sur chaque chunk
        transformerait chaque token en `event: error`.
        """
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1j")
        ev = _events([f"data: {raw}", _chunk({"content": "ok"}, finish_reason="stop"), "data: [DONE]"], st)
        assert "error" not in _names(ev)
        assert _data(ev, "message_delta")[0]["delta"]["stop_reason"] == "end_turn"
        assert st.finished is True

    def test_error_non_dict_non_vide_est_quand_meme_relaye(self):
        """`error: 503` (un statut nu) porte une information : on la relaie."""
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1k")
        ev = _events([self._err(503)], st)
        payload = _data(ev, "error")[0]
        assert payload["error"]["type"] == "overloaded_error"
        assert payload["error"]["message"] == "503"


class TestF1ClassificationDuType:
    """Le type rendu doit appartenir au vocabulaire Anthropic — c'est ce qui
    permet à Claude Code de décider retry/backoff au lieu d'abandonner."""

    @pytest.mark.parametrize(
        "payload,attendu",
        [
            ({"message": "slow down", "type": "rate_limit_exceeded"}, "rate_limit_error"),
            ({"message": "quota épuisé", "type": "insufficient_quota"}, "rate_limit_error"),
            ({"message": "x", "type": "rate_limit"}, "rate_limit_error"),
            ({"message": "overloaded", "type": "server_error"}, "overloaded_error"),
            ({"message": "x", "type": "internal_error"}, "overloaded_error"),
            ({"message": "temporarily unavailable", "type": "whatever"}, "overloaded_error"),
            ({"message": "prompt too long", "type": "context_length_exceeded"}, "invalid_request_error"),
            ({"message": "bad tool schema", "type": "invalid_request_error"}, "invalid_request_error"),
            ({"message": "no such model", "type": "model_not_found"}, "not_found_error"),
            ({"message": "key invalid", "type": "authentication_error"}, "authentication_error"),
            ({"message": "nope", "type": "permission_error"}, "permission_error"),
            ({"message": "inconnu au bataillon", "type": "some_new_gateway_type"}, "api_error"),
        ],
    )
    def test_type_amont_rapproche_du_vocabulaire_anthropic(self, payload, attendu):
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1t")
        ev = _events([_data_line({"error": payload})], st)
        assert _data(ev, "error")[0]["error"]["type"] == attendu

    @pytest.mark.parametrize(
        "status,attendu",
        [
            (429, "rate_limit_error"),
            (529, "overloaded_error"),
            (503, "overloaded_error"),
            (500, "overloaded_error"),
            (400, "invalid_request_error"),
            (401, "authentication_error"),
            (403, "permission_error"),
            (404, "not_found_error"),
            (413, "request_too_large"),
            ("429", "rate_limit_error"),
            (418, "api_error"),
            (None, "api_error"),
            (True, "api_error"),
            (0, "api_error"),
        ],
    )
    def test_statut_http_amont_classe(self, status, attendu):
        """Même table que ``opencode.py:12127-12136`` (429 → rate_limit,
        529 → overloaded), étendue aux autres statuts courants."""
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1s")
        payload = {"message": "panne", "status": status}
        if status is None:
            del payload["status"]
        ev = _events([_data_line({"error": payload})], st)
        assert _data(ev, "error")[0]["error"]["type"] == attendu

    def test_type_amont_bat_le_statut(self):
        """`type` est un champ dédié, `status` peut être un codage de
        passerelle : en cas de désaccord, le champ dédié gagne."""
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1u")
        ev = _events([_data_line({"error": {"message": "m", "type": "rate_limit_exceeded", "status": 500}})], st)
        assert _data(ev, "error")[0]["error"]["type"] == "rate_limit_error"

    def test_statut_bat_le_libre_du_message(self):
        """À type absent, le statut est lu avant le message : « 500 tokens
        written » ne doit PAS devenir une surcharge."""
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1v")
        ev = _events(
            [_data_line({"error": {"message": "failed after 500 tokens written", "status_code": "429"}})],
            st,
        )
        assert _data(ev, "error")[0]["error"]["type"] == "rate_limit_error"

    def test_message_sans_indice_replie_sur_api_error(self):
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1w")
        ev = _events([_data_line({"error": {"message": "yolo"}})], st)
        assert _data(ev, "error")[0]["error"]["type"] == "api_error"


class TestF1FormeDuWire:
    """La forme doit être EXACTEMENT celle d'Anthropic / de
    ``core/errors.py:74`` — un client qui ne reconnaît pas l'enveloppe
    ignore l'erreur et rend la main sur un tour tronqué."""

    def test_enveloppe_exacte(self):
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1x")
        ev = _events([_data_line({"error": {"message": "boom", "type": "server_error"}})], st)
        raw = ev[0]
        assert raw.startswith("event: error\n")
        assert raw.endswith("\n\n")
        body = json.loads(raw.split("\n")[1].removeprefix("data: "))
        assert set(body) == {"type", "error"}
        assert body["type"] == "error"
        assert set(body["error"]) == {"type", "message"}

    def test_json_non_ascii_preserve(self):
        """Accents et emoji ne doivent pas être échappés en ``\\uXXXX``."""
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1y")
        ev = _events([_data_line({"error": {"message": "clé invalide 🔑", "type": "auth"}})], st)
        assert "clé invalide 🔑" in ev[0]

    def test_pas_doublon_avec_le_relais_event_error_nu(self):
        """Contrat avec ``opencode.py:12181`` : un ``event: error`` nu de
        l'amont est déjà relayé tel quel (et coupe la conversion). Le
        convertisseur, lui, ne voit QUE des lignes ``data:`` — donc jamais
        de doublon. Vérifié ici : une ligne ``event: error`` n'est pas une
        ligne de donnée et ne produit rien.
        """
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1z")
        ev = _events(["event: error"], st) + st.finalize_events()
        assert "error" not in _names(ev)
        assert st.finished is True

    def test_ligne_sans_cle_error_ne_declenche_rien(self):
        """Le miroir de F1 : un chunk Chat normal, sans ``error`` de premier
        niveau, ne doit JAMAIS produire d'``event: error``. C'est lui qui
        garantit l'absence de doublon sur le relais de ``opencode.py:12181``.
        """
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1z2")
        ev = _events([_chunk({"content": "bonjour"})], st) + st.finalize_events()
        assert "error" not in _names(ev)
        assert st.finished is True

    def test_enveloppe_anthropic_est_relayee_telle_quelle(self):
        """Une passerelle qui formate déjà en Anthropic
        (``data: {"type":"error","error":{…}}``) sans ``event: error`` atteint
        quand même F1 (clé ``error`` de premier niveau). On relaie l'enveloppe
        telle quelle — reclasser perdrait ``param``/``request_id`` et
        afficherait au client une erreur qui n'est pas la sienne.
        """
        inner = {"type": "invalid_request_error", "message": "max_tokens trop grand", "param": "max_tokens"}
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1z3")
        ev = _events([_data_line({"type": "error", "error": inner})], st)
        assert _names(ev).count("error") == 1
        payload = _data(ev, "error")
        assert len(payload) == 1
        assert payload[0]["error"] == inner

    def test_erreur_tardive_apres_finalize_ne_reecrit_pas(self):
        """F1 émet sa propre queue de finalize ; un chunk d'erreur qui
        arrive après ``message_stop`` ne doit rien ajouter (fin de flux
        déjà actée côté client).
        """
        st = ChatSseToAnthropicState(model="m", message_id="msg_f1z4")
        first = _events([_data_line({"error": {"message": "boom", "type": "api_error"}})], st)
        assert "message_stop" in _names(first)
        assert _events([_data_line({"error": {"message": "boom2", "type": "api_error"}})], st) == []
        assert st.finished is True

