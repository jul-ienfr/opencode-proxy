"""Fige le correctif tool-names >64 chars (400 upstream `/responses`).

Contrats couverts (protocol_mapping.py + opencode.py) :
- sanitize_tool_names(tools) -> (tools_sanitized, {short: original}),
  TOOL_NAME_MAX_LEN=64, _TOOL_NAME_MAP_KEY="_tool_name_map".
- Aller : _chat_to_responses_request boucle tools + tool_choice nommé
  remappé ; _anthropic_to_responses_request funne dedans.
- Restore : _responses_to_chat_response / _responses_to_anthropic_response
  (name_map=None par défaut), SSE via ResponsesSseState().tool_name_map
  remappé à output_item.added.
- Transport par requête via clé privée req["_tool_name_map"], jamais
  globale ; choke wire _serialize_json_body la strippe sans muter le caller.

Purs unitaires, sans réseau.
"""

import json
import re

import pytest

import protocol_mapping as pm

LONG65 = "mcp__plugin_example_tool_name_" + "x" * (65 - len("mcp__plugin_example_tool_name_"))
assert len(LONG65) == 65

VALID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _chat_req(model="muse-spark", tools=None, tool_choice=None):
    req = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": tools or [],
    }
    if tool_choice is not None:
        req["tool_choice"] = tool_choice
    return req


def _oai_tool(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "d",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _resp_tool_entry(name):
    return {"type": "function", "name": name, "description": "d", "parameters": {}}


# ── 1 : aller, nom 65 chars ──────────────────────────────────────────
class TestLongNameAller:
    def test_65_chars_shortened_deterministic_restorable(self):
        tools = [_resp_tool_entry(LONG65)]
        out1, map1 = pm.sanitize_tool_names(tools)
        out2, map2 = pm.sanitize_tool_names([_resp_tool_entry(LONG65)])
        short = out1[0]["name"]
        assert len(short) <= pm.TOOL_NAME_MAX_LEN == 64
        assert VALID_RE.match(short)
        assert out2[0]["name"] == short  # déterministe
        assert map1 == map2
        assert pm.restore_tool_name(short, map1) == LONG65
        # input non muté
        assert tools[0]["name"] == LONG65


# ── 2 : collision, ordre alphabétique déterministe ───────────────────
class TestCollision:
    def test_two_names_same_candidate_get_distinct_ordered_shorts(self):
        # "a b" et "a.b" -> même candidat "a_b" -> suffixes -02/-03
        # dans l'ordre alphabétique ("a b" < "a.b" car 0x20 < 0x2E).
        tools = [_resp_tool_entry("a.b"), _resp_tool_entry("a b")]  # ordre d'arrivée inversé
        out, mmap = pm.sanitize_tool_names(tools)
        shorts = [t["name"] for t in out]
        assert shorts[0] != shorts[1]
        assert all(len(s) <= 64 and VALID_RE.match(s) for s in shorts)
        # ordre alphabétique : "a b" garde -02 quel que soit l'ordre d'arrivée
        assert mmap[shorts[1]] == "a b" and shorts[1].endswith("-02")
        assert mmap[shorts[0]] == "a.b" and shorts[0].endswith("-03")
        # re-sanitize dans l'autre ordre -> mêmes shorts par nom
        out_b, _ = pm.sanitize_tool_names([_resp_tool_entry("a b"), _resp_tool_entry("a.b")])
        by_orig = {mmap[s]: s for s in shorts}
        assert out_b[0]["name"] == by_orig["a b"]
        assert out_b[1]["name"] == by_orig["a.b"]

    def test_candidate_taken_by_valid_name_gets_suffix(self):
        tools = [_resp_tool_entry("tool_name"), _resp_tool_entry("tool name")]
        out, mmap = pm.sanitize_tool_names(tools)
        assert out[0]["name"] == "tool_name"  # valide inchangé
        assert out[1]["name"] != "tool_name"
        assert pm.restore_tool_name(out[1]["name"], mmap) == "tool name"


# ── 3 : charset ──────────────────────────────────────────────────────
class TestCharset:
    def test_spaces_dots_remapped_to_underscore(self):
        name = "my tool.name with spaces"
        out, mmap = pm.sanitize_tool_names([_resp_tool_entry(name)])
        short = out[0]["name"]
        assert VALID_RE.match(short), short
        assert " " not in short and "." not in short
        assert pm.restore_tool_name(short, mmap) == name


# ── 4 : web_* exclus ─────────────────────────────────────────────────
class TestWebExcluded:
    def test_web_prefix_never_renamed(self):
        tools = [
            {"type": "function", "name": "web_search", "description": "", "parameters": {}},
            _resp_tool_entry("web_search_foo_" + "y" * 60),  # >64 mais web_* : exclu
            {"type": "web_search_2025_03_05"},  # server tool sans name : intact
        ]
        out, mmap = pm.sanitize_tool_names(tools)
        assert out[0]["name"] == "web_search"
        assert out[1]["name"] == "web_search_foo_" + "y" * 60
        assert out[2] == {"type": "web_search_2025_03_05"}
        assert mmap == {}


# ── 5 : tool_choice nommé remappé ────────────────────────────────────
class TestToolChoice:
    def test_named_tool_choice_follows_rename(self):
        tools = [_oai_tool(LONG65)]
        chat = _chat_req(
            tools=tools,
            tool_choice={"type": "function", "function": {"name": LONG65}},
        )
        req = pm._chat_to_responses_request(chat)
        short = req["tools"][0]["name"]
        assert len(short) <= 64
        assert req["tool_choice"] == {"type": "function", "name": short}
        assert req[pm._TOOL_NAME_MAP_KEY][short] == LONG65


# ── 6 : fast-path ────────────────────────────────────────────────────
class TestFastPath:
    def test_valid_names_unchanged_empty_map(self):
        tools = [_resp_tool_entry("Read"), _resp_tool_entry("my-tool_2")]
        out, mmap = pm.sanitize_tool_names(tools)
        assert out is tools or out == tools
        assert mmap == {}
        assert pm._TOOL_NAME_MAP_KEY not in pm._chat_to_responses_request(
            _chat_req(tools=[_oai_tool("Read")])
        )


# ── 7 : restore non-stream ───────────────────────────────────────────
def _responses_resp(short):
    return {
        "id": "resp_123",
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": short,
                "arguments": '{"a": 1}',
            }
        ],
        "usage": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
    }


class TestRestoreNonStream:
    def test_chat_restore(self):
        _, mmap = pm.sanitize_tool_names([_resp_tool_entry(LONG65)])
        short = next(iter(mmap))
        chat = pm._responses_to_chat_response(_responses_resp(short), "m", mmap)
        tc = chat["choices"][0]["message"]["tool_calls"][0]
        assert tc["function"]["name"] == LONG65
        assert tc["id"] == "call_1"

    def test_anthropic_restore(self):
        _, mmap = pm.sanitize_tool_names([_resp_tool_entry(LONG65)])
        short = next(iter(mmap))
        msg = pm._responses_to_anthropic_response(_responses_resp(short), "m", mmap)
        uses = [b for b in msg["content"] if b["type"] == "tool_use"]
        assert uses and uses[0]["name"] == LONG65

    def test_no_map_unchanged(self):
        _, mmap = pm.sanitize_tool_names([_resp_tool_entry(LONG65)])
        short = next(iter(mmap))
        chat = pm._responses_to_chat_response(_responses_resp(short), "m", None)
        assert chat["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == short
        msg = pm._responses_to_anthropic_response(_responses_resp(short), "m", None)
        uses = [b for b in msg["content"] if b["type"] == "tool_use"]
        assert uses[0]["name"] == short


# ── 8 : restore SSE ──────────────────────────────────────────────────
class TestRestoreSse:
    def test_output_item_added_emits_original_name(self):
        _, mmap = pm.sanitize_tool_names([_resp_tool_entry(LONG65)])
        short = next(iter(mmap))
        state = pm.ResponsesSseState()
        state.tool_name_map = mmap
        raw = json.dumps(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "item_1",
                    "call_id": "call_abc",
                    "name": short,
                    "arguments": "{}",
                },
            }
        )
        delta = pm._responses_sse_to_chat_deltas(raw, state=state)
        assert delta is not None
        fn = delta["choices"][0]["delta"]["tool_calls"][0]
        assert fn["function"]["name"] == LONG65
        assert fn["id"] == "call_abc"

    def test_sse_without_map_keeps_short(self):
        _, mmap = pm.sanitize_tool_names([_resp_tool_entry(LONG65)])
        short = next(iter(mmap))
        raw = json.dumps(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "function_call", "id": "i", "call_id": "c", "name": short},
            }
        )
        delta = pm._responses_sse_to_chat_deltas(raw, state=pm.ResponsesSseState())
        assert delta["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == short


# ── 9 : isolation par requête ────────────────────────────────────────
class TestPerRequestIsolation:
    def test_no_global_leak_between_maps(self):
        name_a = "tool_alpha_" + "A" * 60
        name_b = "tool_beta__" + "B" * 60
        _, map_a = pm.sanitize_tool_names([_resp_tool_entry(name_a)])
        _, map_b = pm.sanitize_tool_names([_resp_tool_entry(name_b)])
        short_a = next(iter(map_a))
        short_b = next(iter(map_b))
        # restores croisés : chaque map ne connaît que son nom
        assert pm.restore_tool_name(short_a, map_a) == name_a
        assert pm.restore_tool_name(short_b, map_b) == name_b
        assert pm.restore_tool_name(short_a, map_b) == short_a
        assert pm.restore_tool_name(short_b, map_a) == short_b
        # pas de global module-level portant la map
        assert not hasattr(pm, "_global_tool_name_map")
        for g in ("_responses_tool_cache", "_responses_tool_index_map"):
            assert pm._TOOL_NAME_MAP_KEY not in getattr(pm, g, {})


# ── 10 : strip wire ──────────────────────────────────────────────────
class TestWireStrip:
    def test_serialize_strips_private_key_without_mutating(self):
        oc = pytest.importorskip("opencode", reason="opencode import trop lourd / effets de bord")
        body = {"model": "m", "input": [], pm._TOOL_NAME_MAP_KEY: {"s": "original"}}
        raw = oc._serialize_json_body(body)
        assert b"_tool_name_map" not in raw
        assert json.loads(raw) == {"model": "m", "input": []}
        # caller non muté : la map survit pour le restore-retour
        assert body[pm._TOOL_NAME_MAP_KEY] == {"s": "original"}

    def test_serialize_strips_synthetic_marker_without_mutating(self):
        # [msg_18e84c912f240-1b] le marqueur retry-once est interne : jamais
        # sur le wire (upstream 400 `unknown parameter`), mais présent en
        # local pour le retry-once du caller.
        oc = pytest.importorskip("opencode", reason="opencode import trop lourd / effets de bord")
        body = {
            "model": "m",
            "input": [],
            pm._HAS_SYNTHETIC_REASONING_KEY: True,
            pm._TOOL_NAME_MAP_KEY: {"s": "original"},
        }
        raw = oc._serialize_json_body(body)
        assert b"_has_synthetic_reasoning_items" not in raw
        assert b"_tool_name_map" not in raw
        assert json.loads(raw) == {"model": "m", "input": []}
        assert body[pm._HAS_SYNTHETIC_REASONING_KEY] is True
        assert body[pm._TOOL_NAME_MAP_KEY] == {"s": "original"}

    def test_marker_set_locally_but_absent_on_wire(self):
        # Conversion avec historique reasoning : marqueur posé (retry-once),
        # wire assaini.
        oc = pytest.importorskip("opencode", reason="opencode import trop lourd / effets de bord")
        chat = {
            "model": "m",
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": "done",
                    "reasoning_content": "because",
                    "tool_calls": [],
                },
                {"role": "user", "content": "go"},
            ],
        }
        req = pm._chat_to_responses_request(chat)
        assert req[pm._HAS_SYNTHETIC_REASONING_KEY] is True  # retry-once intact
        raw = oc._serialize_json_body(req)
        assert b"_has_synthetic_reasoning_items" not in raw


# ── 11 : paid+free même chemin ───────────────────────────────────────
class TestSamePathFreePaid:
    @pytest.mark.parametrize("model", ["glm-4.6-free", "kimi-k2.6", "gpt-5"])
    def test_sanitize_applied_regardless_of_model(self, model):
        req = pm._chat_to_responses_request(_chat_req(model=model, tools=[_oai_tool(LONG65)]))
        short = req["tools"][0]["name"]
        assert len(short) <= 64
        assert req[pm._TOOL_NAME_MAP_KEY][short] == LONG65

    def test_anthropic_path_funnels_through_chat(self):
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"name": LONG65, "description": "d", "input_schema": {"type": "object"}}
            ],
            "tool_choice": {"type": "tool", "name": LONG65},
        }
        req = pm._anthropic_to_responses_request(body)
        assert req["tools"][0]["name"] != LONG65
        assert req[pm._TOOL_NAME_MAP_KEY][req["tools"][0]["name"]] == LONG65


# ── 12 : historique multi-tours remappé (P0-1, msg_18d502b1e0b40-e) ────
def _chat_req_history(tools, messages):
    return {"model": "m", "messages": messages, "tools": tools}


def _history_messages(long_name):
    return [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": long_name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "user", "content": "continue"},
    ]


def _function_call_names(req):
    return [
        item["name"]
        for item in req.get("input", [])
        if isinstance(item, dict) and item.get("type") == "function_call"
    ]


class TestHistoryRemap:
    def test_history_follows_tools_rename(self):
        chat = _chat_req_history([_oai_tool(LONG65)], _history_messages(LONG65))
        req = pm._chat_to_responses_request(chat)
        short = req["tools"][0]["name"]
        assert len(short) <= 64
        names = _function_call_names(req)
        assert names and all(n == short for n in names)
        assert req[pm._TOOL_NAME_MAP_KEY][short] == LONG65
        # restore-retour via la même map
        assert pm.restore_tool_name(names[0], req[pm._TOOL_NAME_MAP_KEY]) == LONG65

    def test_history_without_tools_gets_defensive_short(self):
        chat = {"model": "m", "messages": _history_messages(LONG65)}
        req = pm._chat_to_responses_request(chat)
        names = _function_call_names(req)
        assert names and all(len(n) <= 64 and VALID_RE.match(n) for n in names)
        assert req[pm._TOOL_NAME_MAP_KEY][names[0]] == LONG65

    def test_valid_history_names_untouched_no_map(self):
        chat = _chat_req_history([_oai_tool("Read")], _history_messages("Read"))
        req = pm._chat_to_responses_request(chat)
        assert _function_call_names(req) == ["Read"]
        assert pm._TOOL_NAME_MAP_KEY not in req

    def test_double_conversion_idempotent(self):
        chat = _chat_req_history([_oai_tool(LONG65)], _history_messages(LONG65))
        once = pm._chat_to_responses_request(chat)
        twice = pm._chat_to_responses_request(dict(once))
        assert [t["name"] for t in twice["tools"]] == [t["name"] for t in once["tools"]]
        assert _function_call_names(twice) == _function_call_names(once)
        assert twice[pm._TOOL_NAME_MAP_KEY] == once[pm._TOOL_NAME_MAP_KEY]


# ── 13 : tool_choice toutes formes (P0-2) ─────────────────────────────
class TestToolChoiceShapes:
    def test_openai_nested_normalized_to_responses(self):
        chat = _chat_req(
            tools=[_oai_tool(LONG65)],
            tool_choice={"type": "function", "function": {"name": LONG65}},
        )
        before = {"type": "function", "function": {"name": LONG65}}
        req = pm._chat_to_responses_request(chat)
        short = req["tools"][0]["name"]
        assert req["tool_choice"] == {"type": "function", "name": short}
        assert chat["tool_choice"] == before  # caller non muté

    def test_responses_native_shape(self):
        chat = _chat_req(
            tools=[_oai_tool(LONG65)],
            tool_choice={"type": "function", "name": LONG65},
        )
        req = pm._chat_to_responses_request(chat)
        short = req["tools"][0]["name"]
        assert req["tool_choice"] == {"type": "function", "name": short}

    def test_bare_string_name(self):
        chat = _chat_req(tools=[_oai_tool(LONG65)], tool_choice=LONG65)
        req = pm._chat_to_responses_request(chat)
        assert req["tool_choice"] == req["tools"][0]["name"]

    def test_keywords_passthrough(self):
        for kw in ("auto", "required", "none"):
            chat = _chat_req(tools=[_oai_tool(LONG65)], tool_choice=kw)
            req = pm._chat_to_responses_request(chat)
            assert req["tool_choice"] == kw

    def test_unnamed_dict_passthrough(self):
        tc = {"type": "allowed_tools", "mode": "auto"}
        chat = _chat_req(tools=[_oai_tool(LONG65)], tool_choice=tc)
        req = pm._chat_to_responses_request(chat)
        assert req["tool_choice"] == tc


# ── 14 : verbatim natif Responses (P0-1/P0-2) ─────────────────────────
def _native_req(long_name):
    return {
        "model": "m",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": long_name,
                "arguments": "{}",
            },
        ],
        "tools": [_resp_tool_entry(long_name)],
        "tool_choice": {"type": "function", "name": long_name},
    }


class TestNativeVerbatim:
    def test_chat_verbatim_sanitizes_all(self):
        req = pm._chat_to_responses_request(_native_req(LONG65))
        short = req["tools"][0]["name"]
        assert len(short) <= 64
        assert _function_call_names(req) == [short]
        assert req["tool_choice"] == {"type": "function", "name": short}
        assert req[pm._TOOL_NAME_MAP_KEY][short] == LONG65

    def test_anthropic_verbatim_sanitizes_all(self):
        req = pm._anthropic_to_responses_request(_native_req(LONG65))
        assert _function_call_names(req) != [LONG65]
        assert req["tool_choice"] != {"type": "function", "name": LONG65}

    def test_verbatim_does_not_mutate_caller(self):
        body = _native_req(LONG65)
        pm._sanitize_native_responses_request(dict(body))
        assert body["tools"][0]["name"] == LONG65
        assert body["input"][1]["name"] == LONG65

    def test_verbatim_idempotent(self):
        once = pm._sanitize_native_responses_request(_native_req(LONG65))
        twice = pm._sanitize_native_responses_request(dict(once))
        assert twice["tools"] == once["tools"]
        assert _function_call_names(twice) == _function_call_names(once)
