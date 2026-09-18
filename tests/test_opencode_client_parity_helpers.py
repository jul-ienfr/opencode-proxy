"""Tests parité OpenCodeClient (sst/opencode provider/transform.ts).

Couvre les helpers portés dans ``app.protocol.mapping`` (normalizeMessages /
temperature / topP / topK / applyCaching) et la session par conversation dans
``opencode.py`` (request.ts : sessionID stable par conversation) :
- sanitize_surrogates : no-op sur texte propre, U+FFFD sur surrogat solitaire ;
- scrub tool_call_id claude/mistral : no-op sur IDs valides ;
- fix mistral tool→user ("Done.") : mistral seul ;
- deepseek reasoning_content par défaut : deepseek seul ;
- sampling defaults : jamais d'écrasement d'une valeur cliente ;
- cache : système + 2 derniers non-système, setdefault, plafond 4 ;
- session : même clé → même ses_, clés distinctes → ses_ distincts,
  sans clé → session globale (repli inchangé) ; headers stables.
"""

import re

import opencode as oc
from app.protocol import mapping as pm


def test_sanitize_surrogates():
    assert pm.sanitize_surrogates("hello") == "hello"
    assert pm.sanitize_surrogates("") == ""
    assert pm.sanitize_surrogates(None) is None
    assert pm.sanitize_surrogates("a\ud800b") == "a\ufffdb"
    # paire valide préservée
    assert pm.sanitize_surrogates("a\U0001f600b") == "a\U0001f600b"


def test_scrub_claude_tool_id_noop_on_valid():
    assert pm.scrub_claude_tool_id("toolu_abc-123_X") == "toolu_abc-123_X"
    assert pm.scrub_claude_tool_id("a/b:c d") == "a_b_c_d"
    assert pm.scrub_claude_tool_id("") == ""


def test_scrub_mistral_tool_id_shape():
    out = pm.scrub_mistral_tool_id("toolu_abc-123_XYZ!")
    assert re.fullmatch(r"[A-Za-z0-9]{9}", out), out
    assert pm.scrub_mistral_tool_id("ab") == "ab0000000"


def test_scrub_openai_tool_ids_claude_only():
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a/b", "function": {}}]},
        {"role": "tool", "tool_call_id": "c:d", "content": "x"},
    ]
    assert pm.scrub_openai_tool_ids(msgs, "claude-sonnet-4-6") == 2
    assert msgs[0]["tool_calls"][0]["id"] == "a_b"
    assert msgs[1]["tool_call_id"] == "c_d"
    # modèle hors famille → no-op
    msgs2 = [{"role": "tool", "tool_call_id": "a/b", "content": "x"}]
    assert pm.scrub_openai_tool_ids(msgs2, "gpt-5.2") == 0
    assert msgs2[0]["tool_call_id"] == "a/b"


def test_fix_mistral_tool_user_sequence():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "abc000000"}]},
        {"role": "tool", "tool_call_id": "abc000000", "content": "ok"},
        {"role": "user", "content": "next"},
    ]
    assert pm.fix_mistral_tool_user_sequence(msgs, "mistral-large-latest") == 1
    assert msgs[3] == {"role": "assistant", "content": "Done."}
    assert msgs[4]["role"] == "user"
    # non-mistral : inchangé
    msgs2 = [{"role": "tool", "tool_call_id": "x", "content": "ok"}, {"role": "user", "content": "n"}]
    assert pm.fix_mistral_tool_user_sequence(msgs2, "kimi-k2.6") == 0
    assert len(msgs2) == 2


def test_ensure_deepseek_reasoning():
    msgs = [{"role": "assistant", "content": "hi"}, {"role": "user", "content": "q"}]
    assert pm.ensure_deepseek_reasoning(msgs, "deepseek-chat") == 1
    assert msgs[0]["reasoning_content"] == " "
    assert "reasoning_content" not in msgs[1]
    assert pm.ensure_deepseek_reasoning(msgs, "glm-5.1") == 0


def test_sampling_defaults_table():
    assert pm.sampling_defaults_for_model("claude-opus-4-7") == {}
    assert pm.sampling_defaults_for_model("unknown-model-xyz") == {}
    assert pm.sampling_defaults_for_model("kimi-k2") == {"temperature": 0.6}
    assert pm.sampling_defaults_for_model("kimi-k2-thinking") == {"temperature": 1.0, "top_p": 0.95}
    assert pm.sampling_defaults_for_model("glm-4.6") == {"temperature": 1.0}
    mm = pm.sampling_defaults_for_model("minimax-m2.5")
    assert mm["temperature"] == 1.0 and mm["top_p"] == 0.95 and mm["top_k"] in (20, 40)
    assert pm.sampling_defaults_for_model("gemini-2.5-pro") == {"temperature": 1.0, "top_p": 0.95, "top_k": 64}


def test_sampling_defaults_never_override_client():
    oai = {"model": "m", "messages": [], "temperature": 0.2}
    assert pm.apply_opencode_sampling_defaults(oai, "minimax-m2.5") is True
    assert oai["temperature"] == 0.2  # valeur cliente intacte
    assert oai["top_p"] == 0.95  # défauts manquants complétés
    oai2 = {"model": "m", "messages": []}
    assert pm.apply_opencode_sampling_defaults(oai2, "claude-x") is False


def _body_n_users(n):
    msgs = []
    for i in range(n):
        msgs.append({"role": "user", "content": [{"type": "text", "text": f"q{i}"}]})
        if i < n - 1:
            msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]})
    return {"model": "kimi-k2.6", "messages": msgs}


def test_cache_last_user_only_pinned_contract():
    # DIVERGENCE ASSUMÉE vs applyCaching officiel (système + 2 derniers) :
    # le contrat verrouillé du proxy reste système + dernier user (plafond 4
    # préservé pour les breakpoints clients).
    out = pm.anthropic_to_openai(_body_n_users(3), "kimi-k2.6")
    users = [m for m in out["messages"] if m.get("role") == "user"]
    assert len(users) == 3
    assert "cache_control" not in users[0]
    assert "cache_control" not in users[1]
    assert users[2].get("cache_control") == {"type": "ephemeral"}
    assert pm._count_cache_breakpoints(out["messages"]) <= pm.ANTHROPIC_MAX_CACHE_BREAKPOINTS


def test_cache_client_breakpoint_not_overwritten():
    body = _body_n_users(2)
    body["messages"][-1]["content"][0]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    out = pm.anthropic_to_openai(body, "kimi-k2.6")
    users = [m for m in out["messages"] if m.get("role") == "user"]
    assert users[-1].get("cache_control") == {"type": "ephemeral", "ttl": "1h"}


def test_conversation_session_stable_per_key(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "_FREE_SESSION_FILE", str(tmp_path / "_free_session_id"))
    oc._FREE_SESSION_CACHE = None
    oc._FREE_SESSION_TS = 0.0
    oc._CONVERSATION_SESSIONS.clear()
    try:
        s1 = oc._conversation_session_id("conv-A")
        s2 = oc._conversation_session_id("conv-A")
        s3 = oc._conversation_session_id("conv-B")
        assert s1 == s2 and s1.startswith("ses_") and len(s1) == 30
        assert s3 != s1
        # sans clé → session globale (repli inchangé)
        assert oc._conversation_session_id(None) == oc._free_session_id()
        assert oc._conversation_session_id("") == oc._free_session_id()
    finally:
        oc._CONVERSATION_SESSIONS.clear()
        oc._FREE_SESSION_CACHE = None


def test_official_headers_conversation_key(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "_FREE_SESSION_FILE", str(tmp_path / "_free_session_id"))
    oc._FREE_SESSION_CACHE = None
    oc._FREE_SESSION_TS = 0.0
    oc._CONVERSATION_SESSIONS.clear()
    oc._free_msg_id.set(None)
    try:
        h1 = oc._official_free_headers("", "conv-A")
        h2 = oc._official_free_headers("", "conv-A")
        h3 = oc._official_free_headers("", "conv-B")
        h0 = oc._official_free_headers("")
        assert h1["x-opencode-session"] == h2["x-opencode-session"]
        assert h3["x-opencode-session"] != h1["x-opencode-session"]
        assert h0["x-opencode-session"] == oc._free_session_id()
        assert list(h1.keys()) == list(h0.keys())  # jeu/ordre inchangés
    finally:
        oc._CONVERSATION_SESSIONS.clear()
        oc._free_msg_id.set(None)
        oc._FREE_SESSION_CACHE = None


def test_body_conversation_key():
    assert oc._body_conversation_key({"conversation": "conv_123"}) == "conv_123"
    assert oc._body_conversation_key({"model": "x"}) is None
    assert oc._body_conversation_key(None) is None
    assert oc._body_conversation_key("nope") is None


# ── Filtrage vide (normalizeMessages, jambes Anthropic) ──

def test_filter_empty_text_parts_keeps_signed_thinking():
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": ""}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": ""},
                {"type": "thinking", "thinking": "", "signature": "sig-auth"},
                {"type": "thinking", "thinking": "  "},
            ],
        },
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "n", "input": {}}]},
    ]
    out = pm.filter_empty_anthropic_messages(msgs)
    assert len(out) == 2  # message user vide retiré
    assert out[0]["content"] == [{"type": "thinking", "thinking": "", "signature": "sig-auth"}]
    assert out[1]["content"][0]["type"] == "tool_use"
    # entrée non mutée
    assert msgs[0]["content"] == [{"type": "text", "text": ""}]


def test_filter_request_body_never_empties():
    body = {"model": "m", "messages": [{"role": "user", "content": ""}], "system": ""}
    out = pm.filter_anthropic_request_body(body)
    assert out is body or out["messages"] == body["messages"]  # repli intact
    assert body["messages"] == [{"role": "user", "content": ""}]  # non muté


def test_empty_text_dropped_in_chat_conversion():
    body = {
        "model": "kimi-k2.6",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "q"}, {"type": "text", "text": ""}]},
        ],
    }
    out = pm.anthropic_to_openai(body, "kimi-k2.6")
    assert out["messages"][-1]["content"] == "q"


def test_empty_text_dropped_in_anthropic_request():
    oai = {
        "model": "claude-opus-4-7",
        "messages": [
            {"role": "user", "content": ""},
            {"role": "user", "content": "hello"},
        ],
        "max_tokens": 16,
    }
    out = pm.openai_to_anthropic_request(oai)
    assert len(out["messages"]) == 1
    assert out["messages"][0]["content"] == [{"type": "text", "text": "hello"}]


# ── Modalités (unsupportedParts : table + texte d'erreur) ──

def test_modalities_table():
    assert pm.model_supports_modality("claude-opus-4-7", "image")
    assert pm.model_supports_modality("kimi-k2.6", "pdf")
    assert not pm.model_supports_modality("kimi-k2.6", "video")
    assert not pm.model_supports_modality("modele-inconnu-xyz", "image")
    assert pm.model_supports_modality("modele-inconnu-xyz", "text")


def test_unsupported_modality_error_text():
    t = pm.unsupported_modality_error_text("video", "clip.mp4")
    assert "clip.mp4" in t and "video" in t and "Inform the user" in t


# ── Thinking moderne (display + blockBinding) ──

def test_anthropic_version_classifiers():
    assert pm.anthropic_modern_adaptive_thinking("claude-opus-4-7")
    assert pm.anthropic_modern_adaptive_thinking("claude-4.7-opus")
    assert not pm.anthropic_modern_adaptive_thinking("claude-sonnet-4-6")
    assert not pm.anthropic_modern_adaptive_thinking("claude-opus-4-20250514")
    assert pm.anthropic_binds_thinking("claude-opus-5-1")
    assert not pm.anthropic_binds_thinking("claude-mythos-5-1")
    assert not pm.anthropic_binds_thinking("claude-sonnet-4-6")


def test_apply_effort_adds_display_and_binding():
    res: dict = {}
    assert pm._apply_anthropic_effort(res, "high", "claude-opus-4-7", source="test") is True
    assert res["thinking"]["type"] == "adaptive"
    assert res["thinking"]["display"] == "summarized"
    assert "blockBinding" not in res["thinking"]  # 4.7 ne lie pas

    res2: dict = {}
    pm._apply_anthropic_effort(res2, "high", "claude-opus-5-1", source="test")
    assert res2["thinking"]["blockBinding"] == {"prefixMismatchBehavior": "drop_block"}

    # opt-out explicite respecté, display conservé
    res3: dict = {"thinking": {"type": "adaptive", "blockBinding": False}}
    pm._apply_anthropic_effort(res3, "high", "claude-opus-5-1", source="test")
    assert "blockBinding" not in res3["thinking"]
    assert res3["thinking"]["display"] == "summarized"


def test_beta_binding_controls_only_with_block_binding():
    oc._current_client_anthropic_betas.set({})
    try:
        h = oc._ensure_anthropic_beta({"x-api-key": "k"})
        assert "thinking-binding-controls" not in h["anthropic-beta"]
        h2 = oc._ensure_anthropic_beta(
            {"x-api-key": "k"}, {"thinking": {"type": "adaptive", "blockBinding": {"prefixMismatchBehavior": "drop_block"}}}
        )
        assert "thinking-binding-controls" in h2["anthropic-beta"]
        assert "interleaved-thinking-2025-05-14" in h2["anthropic-beta"]
        # idempotent : pas de doublon
        h3 = oc._ensure_anthropic_beta(h2, {"thinking": {"type": "adaptive", "blockBinding": {}}})
        assert h3["anthropic-beta"].count("thinking-binding-controls") == 1
    finally:
        oc._current_client_anthropic_betas.set({})
