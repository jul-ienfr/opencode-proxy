"""Phase 1 — is_compaction_shape : détection « shape compaction ».

Tests unitaires purs (aucun boot réseau, aucun touch logs/requests.db).
Formes couvertes : officielle OpenCode (1..n msg user texte, sans
outils/system), Responses API équivalente, marqueurs Hermes
([CONTEXT COMPACTION – REFERENCE ONLY]) et Claude-Code
(<conversation-checkpoint>).
"""

import json

import pytest

from app.compaction import (
    build_checkpoint_input,
    build_checkpoint_summary,
    build_condensed_history,
    build_condensed_input,
    build_summarizer_body,
    build_summary_user_text,
    extract_previous_summary,
    is_compaction_shape,
    is_overflow,
    maybe_condense,
    run_summarizer,
    split_keep_recent,
)


def _official(n=1, max_tokens=4096):
    return {
        "model": "opus",
        "messages": [{"role": "user", "content": "résumé : " + "x" * 1200} for _ in range(n)],
        "max_tokens": max_tokens,
        "stream": True,
    }


def test_official_single_user_text_shape():
    assert is_compaction_shape(_official()) is True


def test_short_user_text_is_not_compaction():
    # Garde-fou anti-faux-positif : un simple "hi" (ou tout texte < 1000 chars)
    # n'est PAS une shape-compaction — sinon les tests P4/A27 et tout tour
    # user court bypasseraient le 503.
    body = {
        "model": "haiku",
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert is_compaction_shape(body) is False
    body["messages"] = [{"role": "user", "content": "x" * 999}]
    assert is_compaction_shape(body) is False
    body["messages"] = [{"role": "user", "content": "Summarize the conversation: " + "x" * 1000}]
    assert is_compaction_shape(body) is True
    # A long normal user prompt is NOT compaction any more.
    body["messages"] = [{"role": "user", "content": "x" * 5000}]
    assert is_compaction_shape(body) is False


def test_min_chars_implicit_is_configurable():
    # La borne est un paramètre (config.yaml:server_compaction.min_chars_implicit),
    # pas une constante en dur : un texte de 500 chars passe avec borne=100.
    body = {
        "model": "haiku",
        "messages": [{"role": "user", "content": "Summarize the conversation: " + "x" * 500}],
    }
    assert is_compaction_shape(body) is False
    assert is_compaction_shape(body, min_chars_implicit=100) is True
    # Marqueur explicite : pas de borne, même courte (on croit le client).
    marked = {
        "messages": [
            {"role": "user", "content": "<conversation-checkpoint><summary>s</summary></conversation-checkpoint>"}
        ]
    }
    assert is_compaction_shape(marked, min_chars_implicit=10**9) is True


def test_official_multi_user_text_shape():
    assert is_compaction_shape(_official(n=3)) is True


def test_official_blocks_form():
    body = {
        "model": "opus",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Summarize the conversation. " + "hello world" * 200}]}
        ],
        "max_tokens": 4096,
    }
    assert is_compaction_shape(body) is True


def test_no_max_tokens_bound():
    # Pas de borne magique : 128000 (Hermes) reste une shape si user-only.
    assert is_compaction_shape(_official(max_tokens=128000)) is True


def test_tools_present_is_not_compaction():
    body = _official()
    body["tools"] = [{"type": "function", "function": {"name": "bash"}}]
    assert is_compaction_shape(body) is False


def test_empty_tools_is_compaction():
    body = _official()
    body["tools"] = []
    assert is_compaction_shape(body) is True


def test_tool_result_present_is_not_compaction():
    body = {
        "model": "opus",
        "messages": [
            {"role": "user", "content": "run this"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "a"},
                    {"type": "tool_result", "tool_use_id": "t1", "content": "out"},
                ],
            },
        ],
    }
    assert is_compaction_shape(body) is False


def test_assistant_message_is_not_compaction():
    body = {
        "model": "opus",
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ],
    }
    assert is_compaction_shape(body) is False


def test_nonempty_system_is_not_compaction():
    body = _official()
    body["system"] = "You are Hermes Agent, be direct."
    assert is_compaction_shape(body) is False


def test_empty_system_is_compaction():
    body = _official()
    body["system"] = ""
    assert is_compaction_shape(body) is True
    body["system"] = []
    assert is_compaction_shape(body) is True


def test_hermes_marker_shape():
    body = {
        "model": "mimo-v2.5",
        "system": "You are Hermes Agent.",
        "messages": [
            {"role": "user", "content": "[CONTEXT COMPACTION – REFERENCE ONLY]\n...summary..."},
            {"role": "assistant", "content": "ack"},
        ],
        "max_tokens": 128000,
    }
    assert is_compaction_shape(body) is True


def test_checkpoint_marker_shape():
    body = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {
                "role": "user",
                "content": "<conversation-checkpoint><summary>done X</summary><recent-context>last tool</recent-context></conversation-checkpoint>",
            },
            {"role": "assistant", "content": "ok"},
        ],
        "stream": True,
    }
    assert is_compaction_shape(body) is True


def test_marker_with_tools_is_compaction():
    # Les compactions Claude Code rejouent l'historique AVEC outils (prouvé
    # sur traces : 40-76k tokens, sorties tiny) : le marqueur explicite fait
    # foi même avec tools (aucun tour normal ne contient ces marqueurs).
    body = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {"role": "user", "content": "<conversation-checkpoint><summary>s</summary></conversation-checkpoint>"},
        ],
        "tools": [{"type": "function", "function": {"name": "bash"}}],
    }
    assert is_compaction_shape(body) is True


def test_normal_agent_turn_is_not_compaction():
    body = {
        "model": "opus",
        "system": "You are a coding agent.",
        "messages": [
            {"role": "user", "content": "fix this bug"},
            {"role": "assistant", "content": "on it"},
        ],
        "tools": [{"type": "function", "function": {"name": "bash"}}],
    }
    assert is_compaction_shape(body) is False


def test_responses_input_shape():
    body = {
        "model": "muse-spark-1.3-contributor",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "summary text here" * 100}]}
        ],
    }
    assert is_compaction_shape(body) is True


def test_responses_short_input_is_not_compaction():
    body = {
        "model": "muse-spark-1.3-contributor",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
    }
    assert is_compaction_shape(body) is False


def test_responses_function_call_is_not_compaction():
    body = {
        "model": "muse-spark-1.3-contributor",
        "input": [
            {"type": "message", "role": "user", "content": "run it"},
            {"type": "function_call", "call_id": "c1", "name": "bash", "arguments": "{}"},
        ],
    }
    assert is_compaction_shape(body) is False


@pytest.mark.parametrize("bad", [None, {}, [], "str", {"messages": []}, {"messages": "x"}, {"input": []}])
def test_garbage_is_not_compaction(bad):
    assert is_compaction_shape(bad) is False


def test_never_raises():
    assert is_compaction_shape({"messages": [{"role": "user", "content": [{"type": 42}]}]}) is False
    assert is_compaction_shape({"messages": [[[{"weird": object()}]]]}) is False


# ── is_overflow : matcher 400-overflow upstream (Phase 2, déclencheur réactif) ──


@pytest.mark.parametrize(
    "body",
    [
        "prompt too long: max 200k tokens",
        "Anthropic: prompt is too long",
        "input is too long for requested model",
        '{"error": {"code": "context_length_exceeded", "message": "too many tokens"}}',
        "This model's maximum context length is 200000 tokens",
        "Requested 250000 tokens, context window is 200000",
        "input too long for model, reduce max_tokens",
    ],
)
def test_overflow_true_on_400_with_marker(body):
    assert is_overflow(400, body) is True


@pytest.mark.parametrize(
    "status, body",
    [
        (429, "prompt too long"),  # 429 + marqueur ≠ overflow (backoff, pas compaction)
        (413, "Request body too large"),  # 413 proxy ≠ overflow modèle
        (500, "prompt too long"),
        (503, "overloaded"),
        (400, "rate limit exceeded, retry later"),
        (400, "request took too long to process"),  # timeout ≠ overflow
        (400, b"input too long for model"),  # bytes OK → True, cas séparé ci-dessous
    ],
)
def test_overflow_false_cases(status, body):
    expected = True if isinstance(body, bytes) else False
    assert is_overflow(status, body) is expected


def test_overflow_no_body_matches_official():
    """Parité `provider-error.ts` : `400/413 (no body)` = overflow."""
    assert is_overflow(400, "") is True
    assert is_overflow(400, None) is True
    assert is_overflow(413, "") is True
    assert is_overflow(422, "") is False  # l'officiel ne classe que 400/413 vide


@pytest.mark.parametrize(
    "body",
    [
        "input token count (250000) exceeds the maximum allowed",
        "number of input tokens exceeds maximum length",
        "maximum prompt length is 4096",
        "reduce the length of the messages",
        "input (200000 tokens) is longer than the model's context length",
        "exceeds the available context size",
        "`inputs` tokens + `max_new_tokens` must be <= 4096",
        "model_context_window_exceeded",
        "prompt has 300000 tokens, but the configured context size is 200000 tokens",
        "request_too_large",
    ],
)
def test_overflow_official_provider_patterns(body):
    """Formulations officielles qui n'étaient pas couvertes par les marqueurs."""
    assert is_overflow(400, body) is True


@pytest.mark.parametrize(
    "body",
    [
        "max_tokens must be less than 4096",  # plafond de SORTIE, pas le contexte
        "rate limit exceeded",
        "Too Many Requests",
        "throttling error: slow down",
    ],
)
def test_overflow_official_exclusions(body):
    assert is_overflow(400, body) is False


def test_overflow_bad_status_never_raises():
    assert is_overflow(None, "prompt too long") is False
    assert is_overflow("bad", "prompt too long") is False
    assert is_overflow(400, {"weird": object()}) is False
    assert is_overflow("400", "too many tokens") is True


def test_overflow_custom_markers():
    assert is_overflow(400, "quota regionale depassee", markers=["regionale"]) is True
    assert is_overflow(400, "autre chose", markers=["regionale"]) is False
    # Liste vide / type invalide → repli sur les défauts.
    assert is_overflow(400, "prompt too long", markers=[]) is True
    assert is_overflow(400, "prompt too long", markers="nope") is True


# ── truncate : forward condensé par paires complètes (Phase 2) ──


def _u(t):
    return {"role": "user", "content": t}


def _a(t):
    return {"role": "assistant", "content": t}


def _tc(i):
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": "go"}, {"type": "tool_use", "id": i, "name": "bash", "input": {}}],
    }


def _tr(i):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content": "out"}]}


def test_split_keep_recent_simple_pairs():
    msgs = [_u("a"), _a("b"), _u("c"), _a("d"), _u("e"), _a("f")]
    old, recent = split_keep_recent(msgs, 1)
    assert [m["content"] for m in old] == ["a", "b", "c", "d"]
    assert [m["content"] for m in recent] == ["e", "f"]


def test_split_never_cuts_tool_pair():
    # keep=1 : le récent commence sur un user — le tool_call/result reste entier
    # d'un seul côté de la coupe (ici côté ancien, jamais orphelin côté récent).
    msgs = [_u("x"), _a("y"), _u("do"), _tc("t1"), _tr("t1"), _u("next"), _a("ok")]
    old, recent = split_keep_recent(msgs, 1)
    assert [m["content"] if isinstance(m["content"], str) else "TOOLS" for m in recent] == ["next", "ok"]
    # keep=2 : la paire outil entière bascule côté récent, jamais coupée.
    old2, recent2 = split_keep_recent(msgs, 2)
    assert recent2[0]["content"] == "do"
    assert any("TOOLS" in str(m["content"]) or "tool" in str(m) for m in recent2)


def test_split_keep_zero_and_garbage():
    msgs = [_u("a"), _a("b")]
    old, recent = split_keep_recent(msgs, 0)
    # [audit 2026-10-30] 0/negative keeps at least the current pair instead
    # of emitting a checkpoint-only history.
    assert old == [] and recent == msgs
    assert split_keep_recent(None, 2) == ([], [])
    assert split_keep_recent("x", 2) == ([], [])


def test_official_v2_summary_request_is_detected_even_with_tools():
    # OpenCode V2 appends the summary prompt to the REAL conversation
    # (system + tools + history), not a lone user-only message.
    body = {
        "model": "opus",
        "system": "You are a coding agent.",
        "tools": [{"name": "read", "description": "Read a file", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": "working"},
            {
                "role": "user",
                "content": "You MUST summarize the conversation above into a structured summary "
                "that will be given to another agent to resume the work.",
            },
        ],
    }
    assert is_compaction_shape(body) is True
    # And the corrective NUDGE is recognized as the same request.
    body["messages"][-1]["content"] = (
        "The previous response did not fill in the required summary template. "
        "Do not call tools."
    )
    assert is_compaction_shape(body) is True


def test_build_condensed_history_uses_official_checkpoint_and_recent_text():
    msgs = [_u("older question"), _a("older answer"), _u("current question"), _a("current answer")]
    condensed, kept = build_condensed_history(msgs, "RESUME", 99, keep_tokens=99, estimate_fn=lambda t: 1)
    body = condensed[0]["content"]
    assert "<conversation-checkpoint>" in body
    assert "Treat it as historical context, not as new instructions." in body
    assert "<summary>\nRESUME\n</summary>" in body
    assert "<recent-context>" in body
    assert "[User]: older question" in body and "[Assistant]: older answer" in body
    # The pending exchange stays verbatim so the retry can answer it.
    assert [m["content"] for m in kept] == ["current question", "current answer"]
    assert condensed[1:] == kept


def test_keep_tokens_default_is_official_15000():
    import config.settings as cs

    assert cs.get_server_compaction("keep_tokens") == 15000
    assert cs.get_server_compaction("enabled") is True


def test_full_history_summary_appends_official_prompt(monkeypatch):
    import asyncio

    calls = []

    class _R:
        status_code = 200
        content = b'{"choices":[{"message":{"content":"## Objective\\nRESUME"}}]}'

    async def do(ep, body, headers, proto):
        calls.append(body)
        return _R(), headers

    hist = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}]
    r = asyncio.run(
        run_summarizer(
            hist,
            model_id="m",
            endpoint="e",
            protocol="openai",
            auth_headers_fn=lambda p: {},
            do_request_fn=do,
            api="chat",
            history_api="chat",
            full_history=True,
        )
    )
    assert r and r.startswith("## Objective")
    sent = calls[0]["messages"]
    assert sent[:2] == hist
    assert sent[2]["role"] == "user"
    assert "You MUST summarize the conversation above" in sent[2]["content"]
    assert "tools" not in calls[0] and "system" not in calls[0]


def test_keep_tail_false_keeps_only_pending_user():
    msgs = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current question"},
    ]
    condensed, kept = build_condensed_history(
        msgs, "RESUME", 99, keep_tokens=99, estimate_fn=lambda t: 1, keep_tail=False
    )
    assert condensed[1:] == kept == [{"role": "user", "content": "current question"}]
    assert "[User]: old" in condensed[0]["content"]
    assert "[Assistant]: old answer" in condensed[0]["content"]


def test_summary_paid_bypass_detects_official_v2(monkeypatch):
    import opencode as oc

    monkeypatch.setattr(
        oc, "get_server_compaction", lambda key, default=None: True if key == "summary_paid_bypass" else default
    )
    body = {
        "model": "x",
        "tools": [{"type": "function", "function": {"name": "read"}}],
        "messages": [
            {"role": "user", "content": "do work"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "You MUST summarize the conversation above into a structured summary."},
        ],
    }
    assert oc._summary_paid_bypass_enabled(body) is True
    body["messages"][-1]["content"] = "ordinary question"
    assert oc._summary_paid_bypass_enabled(body) is False


@pytest.mark.asyncio
async def test_maybe_condense_honors_max_attempts(monkeypatch):
    import app.compaction.react as react

    calls = []

    async def fake(*args, **kwargs):
        calls.append(1)
        return "## Objective\nRESUME" if len(calls) > 1 else None

    monkeypatch.setattr(react, "run_summarizer", fake)
    out = await maybe_condense(
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
        status_code=400,
        body_text="prompt too long",
        model_id="m",
        endpoint="e",
        protocol="openai",
        auth_headers_fn=lambda p: {},
        do_request_fn=lambda *a: None,
        enabled=True,
        max_attempts=2,
    )
    assert out is not None
    assert len(calls) == 2
    assert out[0]["role"] == "user"


@pytest.mark.asyncio
async def test_proactive_condense_uses_session_cache(monkeypatch):
    import opencode as oc

    calls = []

    async def fake_condense(history, **kwargs):
        calls.append(list(history))
        return [{"role": "user", "content": "<conversation-checkpoint>CP</conversation-checkpoint>"}]

    monkeypatch.setattr(oc, "_maybe_condense", fake_condense)
    _sc_cfg = {
        "enabled": True,
        "auto": True,
        "buffer": 100,
        "max_attempts": 1,
        "keep_tail": True,
        "keep_tokens": 15000,
    }

    def _fake_sc(key=None, default=None):
        if key is None:
            return dict(_sc_cfg)
        return _sc_cfg.get(key, default)

    monkeypatch.setattr(oc, "get_server_compaction", _fake_sc)
    monkeypatch.setattr(oc, "_estimate_input_tokens", lambda body: 1000)
    monkeypatch.setattr(
        oc,
        "_compaction_summarizer_plan_full",
        lambda *a, **k: ("m", "e", "openai", "chat", False, None),
    )
    monkeypatch.setattr("config.limits.context_limit", lambda model: 1000)

    class _Req:
        headers = {"x-opencode-session-id": "ses_test_proactive"}

    body = {"model": "m", "messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]}
    out = await oc._maybe_proactive_condense(
        body,
        b"x" * 10000,
        model_id="m",
        endpoint="e",
        protocol="openai",
        history_api="chat",
        history_messages=body["messages"],
        request=_Req(),
        req_id="r1",
    )
    assert out and out[0]["content"].startswith("<conversation-checkpoint>")
    assert len(calls) == 1
    # Same prefix on the next call -> cached checkpoint, no second summary.
    out2 = await oc._maybe_proactive_condense(
        body,
        b"x" * 10000,
        model_id="m",
        endpoint="e",
        protocol="openai",
        history_api="chat",
        history_messages=body["messages"],
        request=_Req(),
        req_id="r2",
    )
    assert out2 == out
    assert len(calls) == 1
    # Le client a réécrit son PREMIER message (compaction côté client) : le
    # checkpoint ne couvre plus ce préfixe, on re-résume.
    edited = dict(body)
    edited["messages"] = [{"role": "user", "content": "a modifié"}] + list(body["messages"][1:])
    out3 = await oc._maybe_proactive_condense(
        edited,
        b"x" * 10000,
        model_id="m",
        endpoint="e",
        protocol="openai",
        history_api="chat",
        history_messages=edited["messages"],
        request=_Req(),
        req_id="r3",
    )
    assert out3 and out3[0]["content"].startswith("<conversation-checkpoint>")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_proactive_content_key_without_session_header(monkeypatch):
    """Harnais tiers sans x-opencode-* : checkpoint quand même, par empreinte."""
    import opencode as oc

    calls = []

    async def fake_condense(history, **kwargs):
        calls.append(list(history))
        return [{"role": "user", "content": "<conversation-checkpoint>CP-content</conversation-checkpoint>"}]

    monkeypatch.setattr(oc, "_maybe_condense", fake_condense)
    _sc_cfg = {
        "enabled": True,
        "auto": True,
        "buffer": 100,
        "max_attempts": 1,
        "keep_tail": True,
        "keep_tokens": 15000,
    }

    def _fake_sc(key=None, default=None):
        if key is None:
            return dict(_sc_cfg)
        return _sc_cfg.get(key, default)

    monkeypatch.setattr(oc, "get_server_compaction", _fake_sc)
    monkeypatch.setattr(oc, "_estimate_input_tokens", lambda body: 1000)
    monkeypatch.setattr(
        oc,
        "_compaction_summarizer_plan_full",
        lambda *a, **k: ("m", "e", "openai", "chat", False, None),
    )
    monkeypatch.setattr("config.limits.context_limit", lambda model: 1000)

    class _Req:
        headers = {}

    history = [
        {"role": "user", "content": "tâche unique content-key 31/10"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "suite"},
        {"role": "assistant", "content": "suite ok"},
    ]
    body = {"model": "m", "messages": history}
    out = await oc._maybe_proactive_condense(
        body,
        b"x" * 10000,
        model_id="m",
        endpoint="e",
        protocol="openai",
        history_api="chat",
        history_messages=history,
        request=_Req(),
        req_id="r-content-1",
    )
    assert out and out[0]["content"].startswith("<conversation-checkpoint>")
    assert len(calls) == 1
    out2 = await oc._maybe_proactive_condense(
        body,
        b"x" * 10000,
        model_id="m",
        endpoint="e",
        protocol="openai",
        history_api="chat",
        history_messages=history,
        request=_Req(),
        req_id="r-content-2",
    )
    assert out2 == out
    assert len(calls) == 1


def test_proactive_buffer_matches_official_formula():
    """`calculateCeiling` : fenêtre − max(10 %, 16k), 16k seulement >= 32k."""
    import opencode as oc

    assert oc._proactive_buffer(20_000) == 2_000
    assert oc._proactive_buffer(100_000) == 16_000
    assert oc._proactive_buffer(200_000) == 20_000
    assert oc._proactive_buffer(1_000_000) == 100_000
    # Un buffer configuré explicitement reste souverain.
    assert oc._proactive_buffer(200_000, 1234) == 1234
    # Buffer >= fenêtre : repli 10 %, jamais une fenêtre négative.
    assert 0 < oc._proactive_buffer(10_000, 20_000) < 10_000


@pytest.mark.asyncio
async def test_proactive_free_wire_trigger_uses_gate_budget(monkeypatch):
    """La jambe free compacte au plafond officiel (~900 Ko), pas à 200k/1M.

    Sans cela un historique de >900 Ko était seulement tronqué : les vieux
    tours disparaissaient du contexte au lieu d'être résumés.
    """
    import opencode as oc

    calls = []

    async def fake_condense(history, **kwargs):
        calls.append((list(history), dict(kwargs)))
        return [{"role": "user", "content": "<conversation-checkpoint>CP</conversation-checkpoint>"}]

    monkeypatch.setattr(oc, "_maybe_condense", fake_condense)
    monkeypatch.setattr(oc, "_free_max_wire_bytes", lambda: 1000)
    monkeypatch.setattr(oc, "_free_wire_baseline_bytes", lambda body: 300)
    monkeypatch.setattr(oc, "_effective_free_model_map", lambda: {"paid": "paid-free"})
    monkeypatch.setattr(
        oc,
        "_compaction_summarizer_plan_full",
        lambda *a, **k: ("paid-free", "e", "openai", "chat", True, None),
    )
    monkeypatch.setattr(oc, "_estimate_input_tokens", lambda body: 10**9)
    monkeypatch.setattr("config.limits.context_limit", lambda model: 1_000_000)
    _sc_cfg = {
        "enabled": True,
        "auto": True,
        "buffer": 0,
        "max_attempts": 1,
        "keep_tail": True,
        "keep_tokens": 15000,
        "summary_max_tokens": 2048,
    }

    def _fake_sc(key=None, default=None):
        if key is None:
            return dict(_sc_cfg)
        return _sc_cfg.get(key, default)

    monkeypatch.setattr(oc, "get_server_compaction", _fake_sc)

    class _Req:
        headers = {"x-opencode-session-id": "ses_free_wire"}

    history = [
        {"role": "user", "content": "tâche"},
        {"role": "assistant", "content": "réponse"},
        {"role": "user", "content": "suite"},
        {"role": "assistant", "content": "réponse 2"},
    ]
    body = {"model": "paid", "messages": history}
    out = await oc._maybe_proactive_condense(
        body,
        b"x" * 5000,
        model_id="paid",
        endpoint="e",
        protocol="openai",
        history_api="chat",
        history_messages=history,
        request=_Req(),
        req_id="r1",
    )
    assert out and out[0]["content"].startswith("<conversation-checkpoint>")
    assert len(calls) == 1
    # budget = 1000 - baseline 300 - réserve checkpoint 20000 -> plancher 512
    assert calls[0][1]["keep_tokens"] == 512

    # Même préfixe : le checkpoint est réutilisé.
    out2 = await oc._maybe_proactive_condense(
        body,
        b"x" * 5000,
        model_id="paid",
        endpoint="e",
        protocol="openai",
        history_api="chat",
        history_messages=history,
        request=_Req(),
        req_id="r2",
    )
    assert out2 == out
    assert len(calls) == 1

    # La queue a grossi au-delà de 45 % du gate : on re-résume au lieu de
    # laisser le trimmer jeter les nouveaux tours.
    grown = history + [{"role": "user", "content": "nouveau " * 500}]
    out3 = await oc._maybe_proactive_condense(
        body,
        b"x" * 5000,
        model_id="paid",
        endpoint="e",
        protocol="openai",
        history_api="chat",
        history_messages=grown,
        request=_Req(),
        req_id="r3",
    )
    assert out3 and out3[0]["content"].startswith("<conversation-checkpoint>")
    assert len(calls) == 2


def test_recent_context_includes_reasoning_and_tool_error():
    """Parité messageToText : raisonnement + distinction Tool result/error."""
    from app.compaction.truncate import serialize_recent_context

    msgs = [
        {
            "role": "assistant",
            "reasoning_content": "je réfléchis",
            "content": [
                {"type": "text", "text": "je lis"},
                {"type": "tool_use", "name": "read", "input": {"path": "a.py"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "is_error": True, "content": "boom"},
            ],
        },
    ]
    txt = serialize_recent_context(msgs)
    assert "[Assistant reasoning]: je réfléchis" in txt
    assert "[Assistant]: je lis" in txt
    assert "[Assistant tool call]: read" in txt
    assert "[Tool error]: boom" in txt
    assert "[User]:" not in txt, "un tool_result ne doit pas être présenté comme un message user"


def test_recent_context_labels_chat_tool_error():
    from app.compaction.truncate import serialize_recent_context

    txt = serialize_recent_context([{"role": "tool", "content": "échec", "is_error": True}])
    assert txt == "[Tool error]: échec"


def test_recent_context_attached_label_uses_mime():
    from app.compaction.truncate import serialize_recent_context

    txt = serialize_recent_context(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "regarde"},
                    {
                        "type": "image",
                        "filename": "capture.png",
                        "source": {"type": "base64", "media_type": "image/png", "data": "AAA"},
                    },
                ],
            }
        ]
    )
    assert "[User]: regarde" in txt
    assert "[Attached image/png: capture.png]" in txt
    assert "AAA" not in txt


def test_build_condensed_history_shape():
    msgs = [_u("x"), _a("y"), _u("next"), _a("ok")]
    condensed, kept = build_condensed_history(msgs, "  RESUME  ", 1)
    assert condensed[0]["role"] == "user"
    assert "<summary>\nRESUME\n</summary>" in condensed[0]["content"]
    assert condensed[1:] == kept == [_u("next"), _a("ok")]
    # Le résumé REMPLACE l'ancien : "x"/"y" ne sont plus dans le forward.
    assert all("RESUME" not in str(m) or m is condensed[0] for m in condensed)


def test_build_condensed_fallback_never_raises():
    msgs = [_u("a")]
    assert build_condensed_history(msgs, "   ", 1)[0] is None  # résumé vide
    assert build_condensed_history([], "r", 1)[0] is None  # historique vide
    assert build_condensed_history(None, "r", 1) == (None, None)


def test_checkpoint_marker_is_detected_as_compaction():
    # Le forward condensé lui-même porte le marqueur → détecté shape-compaction
    # (exclu du cache, bypass 503) même avec un texte court.
    cp = build_checkpoint_summary("s", 2)
    assert is_compaction_shape({"messages": [cp]}, min_chars_implicit=10**9) is True


# ── summarizer : réplique exacte de la requête officielle (Phase 2) ──


def test_summarizer_body_is_official_replica():
    b = build_summarizer_body("x" * 10, 2048)
    # 1 seul msg user, stream:false, PAS de tools/system/thinking.
    assert b == {"messages": [{"role": "user", "content": "x" * 10}], "max_tokens": 2048, "stream": False}


def test_summarizer_body_caps_at_official_max():
    assert build_summarizer_body("x", 128000)["max_tokens"] == 32000  # SUMMARY_OUTPUT_MAX officiel
    assert build_summarizer_body("x", 512)["max_tokens"] == 512
    assert build_summarizer_body("x", -5)["max_tokens"] == 2048  # repli sûr


def test_previous_summary_chaining():
    msgs = [
        {
            "role": "user",
            "content": "<conversation-checkpoint><summary>ANCIEN</summary><recent-context>r</recent-context></conversation-checkpoint>",
        },
        {"role": "user", "content": "suite"},
    ]
    assert extract_previous_summary(msgs) == "ANCIEN"
    t = build_summary_user_text("HIST", "ANCIEN")
    assert "<previous-summary>" in t and "ANCIEN" in t and "HIST" in t
    t2 = build_summary_user_text("HIST", None)
    assert "<previous-summary>" not in t2 and "HIST" in t2
    assert extract_previous_summary([{"role": "user", "content": "rien"}]) is None
    assert extract_previous_summary(None) is None


class _FakeResp:
    def __init__(self, status, content):
        self.status_code = status
        self.content = content


@pytest.mark.asyncio
async def test_run_summarizer_anthropic_success():
    async def do(ep, body, h, p):
        assert body["model"] == "m" and body["stream"] is False
        assert "tools" not in body and "system" not in body
        return _FakeResp(200, b'{"content": [{"type": "text", "text": "## Objective\\nRESUME"}]}'), h

    r = await run_summarizer(
        [{"role": "user", "content": "hello world"}],
        model_id="m",
        endpoint="e",
        protocol="anthropic",
        auth_headers_fn=lambda p: {},
        do_request_fn=do,
    )
    assert r == "## Objective\nRESUME"


@pytest.mark.asyncio
async def test_run_summarizer_openai_success():
    async def do(ep, body, h, p):
        return _FakeResp(200, b'{"choices": [{"message": {"content": "## Decisions\\nRES2"}}]}'), h

    r = await run_summarizer(
        [{"role": "user", "content": "hi there"}],
        model_id="m",
        endpoint="e",
        protocol="openai",
        auth_headers_fn=lambda p: {},
        do_request_fn=do,
    )
    assert r == "## Decisions\nRES2"


@pytest.mark.asyncio
async def test_run_summarizer_rewrite_once_then_fail_open():
    # Sans rubriques → 1 corrective ; si toujours invalide → None (fail-open).
    calls = []

    async def do_bad(ep, body, h, p):
        calls.append(body)
        return _FakeResp(200, b'{"choices": [{"message": {"content": "bavardage sans structure"}}]}'), h

    r = await run_summarizer(
        [{"role": "user", "content": "hello world"}],
        model_id="m",
        endpoint="e",
        protocol="openai",
        auth_headers_fn=lambda p: {},
        do_request_fn=do_bad,
    )
    assert r is None
    assert len(calls) == 2, "exactement 1 corrective, jamais de boucle"
    assert "Previous attempt" in json.dumps(calls[1])

    # Corrective fructueuse → résumé validé rendu.
    async def do_fixed(ep, body, h, p):
        if len([c for c in [body] if "Previous attempt" in json.dumps(c)]) == 0:
            return _FakeResp(200, b'{"choices": [{"message": {"content": "bavardage"}}]}'), h
        return _FakeResp(200, b'{"choices": [{"message": {"content": "## Objective\\nok"}}]}'), h

    r = await run_summarizer(
        [{"role": "user", "content": "hello world"}],
        model_id="m",
        endpoint="e",
        protocol="openai",
        auth_headers_fn=lambda p: {},
        do_request_fn=do_fixed,
    )
    assert r == "## Objective\nok"


@pytest.mark.asyncio
async def test_run_summarizer_fail_open_returns_none():
    async def do_400(ep, body, h, p):
        return _FakeResp(400, b"prompt too long"), h

    async def do_boom(ep, body, h, p):
        raise TimeoutError()

    kw = {"model_id": "m", "endpoint": "e", "protocol": "openai", "auth_headers_fn": lambda p: {}}
    hist = [{"role": "user", "content": "hi there"}]
    assert await run_summarizer(hist, do_request_fn=do_400, **kw) is None  # overflow du résumé
    assert await run_summarizer(hist, do_request_fn=do_boom, timeout_s=5, **kw) is None  # timeout
    assert await run_summarizer([], do_request_fn=do_400, **kw) is None  # historique vide
    assert await run_summarizer("x", do_request_fn=do_400, **kw) is None  # garbage


# ── react : maybe_condense (Phase 2, orchestration, jamais de raise) ──


def _hist(n_pairs=2):
    msgs = []
    for i in range(n_pairs):
        msgs.append({"role": "user", "content": f"q{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    return msgs


class _OkResp:
    status_code = 200
    content = b'{"choices": [{"message": {"content": "## Objective\\nRESUME-CONDENSE"}}]}'


async def _ok_do(endpoint, body, headers, protocol):
    return _OkResp(), headers


@pytest.mark.asyncio
async def test_maybe_condense_disabled_returns_none():
    # enabled:false (défaut) → None, jamais d'appel résumeur (fail-open).
    async def _boom(*a, **k):
        raise AssertionError("do_request_fn ne doit pas être appelé")

    r = await maybe_condense(
        _hist(),
        status_code=400,
        body_text="prompt too long",
        model_id="m",
        endpoint="e",
        protocol="openai",
        auth_headers_fn=lambda p: {},
        do_request_fn=_boom,
        enabled=False,
    )
    assert r is None


@pytest.mark.asyncio
async def test_maybe_condense_non_overflow_returns_none():
    # 429/500/400-non-marqueur → None (pas de compaction sur backoff/erreur).
    kw = {
        "model_id": "m",
        "endpoint": "e",
        "protocol": "openai",
        "auth_headers_fn": lambda p: {},
        "do_request_fn": _ok_do,
        "enabled": True,
    }
    hist = _hist()
    assert await maybe_condense(hist, status_code=429, body_text="prompt too long", **kw) is None
    assert await maybe_condense(hist, status_code=500, body_text="prompt too long", **kw) is None
    assert await maybe_condense(hist, status_code=400, body_text="rate limit exceeded", **kw) is None


@pytest.mark.asyncio
async def test_maybe_condense_overflow_condenses():
    # 400-overflow + enabled → [checkpoint, *recent] prêt à forwarder.
    r = await maybe_condense(
        _hist(3),
        status_code=400,
        body_text="input is too long for model",
        model_id="m",
        endpoint="e",
        protocol="openai",
        auth_headers_fn=lambda p: {},
        do_request_fn=_ok_do,
        enabled=True,
        keep_recent_pairs=1,
    )
    assert isinstance(r, list) and len(r) == 3  # checkpoint + 1 paire récente
    assert "<summary>\n## Objective" in r[0]["content"]
    assert "RESUME-CONDENSE\n</summary>" in r[0]["content"]
    assert r[1:] == _hist(3)[-2:]


@pytest.mark.asyncio
async def test_maybe_condense_summarizer_failure_returns_none():
    # Résumeur en échec (400/timeout) → None, l'appelant relaie l'overflow intact.
    async def _do_400(ep, body, h, p):
        class _R:
            status_code = 400
            content = b"prompt too long"

        return _R(), h

    async def _do_boom(ep, body, h, p):
        raise TimeoutError()

    kw = {
        "status_code": 400,
        "body_text": "prompt too long",
        "model_id": "m",
        "endpoint": "e",
        "protocol": "openai",
        "auth_headers_fn": lambda p: {},
        "enabled": True,
        "timeout_s": 5,
    }
    hist = _hist()
    assert await maybe_condense(hist, do_request_fn=_do_400, **kw) is None
    assert await maybe_condense(hist, do_request_fn=_do_boom, **kw) is None


@pytest.mark.asyncio
async def test_maybe_condense_never_raises():
    # Garbage partout → None, jamais d'exception (fail-open total).
    r = await maybe_condense(
        "pas-une-liste",
        status_code="???",
        body_text=object(),
        model_id="m",
        endpoint="e",
        protocol="openai",
        auth_headers_fn=lambda p: 1 / 0,
        do_request_fn=None,
        enabled=True,
    )
    assert r is None
    assert (
        await maybe_condense(
            None,
            status_code=400,
            body_text="prompt too long",
            model_id="m",
            endpoint="e",
            protocol="openai",
            auth_headers_fn=lambda p: {},
            do_request_fn=_ok_do,
            enabled=True,
        )
        is None
    )


def _rin(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def test_build_condensed_input_shape():
    # Miroir Responses de build_condensed_history : [checkpoint_input, *recent],
    # forme input (jamais messages), coupe aux frontières de paires.
    hist = [_rin("a"), {"role": "assistant", "content": "b"}, _rin("c"), {"role": "assistant", "content": "d"}]
    condensed, kept = build_condensed_input(hist, "  RESUME  ", 1)
    assert condensed[0]["type"] == "message" and condensed[0]["role"] == "user"
    assert "<summary>\nRESUME\n</summary>" in condensed[0]["content"][0]["text"]
    assert "messages" not in condensed[0]
    assert condensed[1:] == kept == hist[-2:]
    assert build_condensed_input(hist, "   ", 1)[0] is None  # résumé vide
    assert build_condensed_input([], "r", 1)[0] is None  # historique vide
    assert build_condensed_input(None, "r", 1) == (None, None)


def test_checkpoint_input_never_forges_compaction_item():
    # Le proxy ne forge jamais d'item compaction opaque (chiffré provider) :
    # le checkpoint est un message user ordinaire portant le marqueur texte.
    cp = build_checkpoint_input("s", 2)
    assert cp.get("type") == "message"
    # ... qui reste détecté comme compactage (exclu du cache, bypass 503),
    # comme son jumeau chat (test_checkpoint_marker_is_detected_as_compaction).
    assert is_compaction_shape({"input": [cp]}, min_chars_implicit=10**9) is True


@pytest.mark.asyncio
async def test_maybe_condense_responses_api_builds_input():
    # api=responses → fenêtre input (pas de messages), prête pour /responses.
    async def _ok_resp(ep, body, h, p):
        assert body.get("input") and "messages" not in body
        return _FakeResp(
            200,
            b'{"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "## Decisions\\nR"}]}]}',
        ), h

    hist = [_rin("vieux 1"), _rin("vieux 2"), _rin("recent"), {"role": "assistant", "content": "ok"}]
    r = await maybe_condense(
        hist,
        status_code=400,
        body_text="context window exceeded",
        model_id="m",
        endpoint="https://x/v1/responses",
        protocol="openai",
        auth_headers_fn=lambda p: {},
        do_request_fn=_ok_resp,
        enabled=True,
        keep_recent_pairs=1,
        api="responses",
    )
    assert isinstance(r, list) and r[0].get("type") == "message"
    assert r[0]["content"][0]["text"].startswith("<conversation-checkpoint>")
    assert r[1:] == hist[-2:]


# ── lean : amaigrissement des sorties d'outils (résumés seuls) ──


def test_lean_truncates_tool_outputs_only():
    from app.compaction import lean_summary_body

    big = "R" * 5000
    body = {
        "model": "m",
        "system": "SYS" + "S" * 5000,
        "messages": [
            {"role": "user", "content": "question " + "Q" * 5000},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "Bash", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": big}]},
            {"role": "user", "content": [{"type": "text", "text": "note " + "N" * 5000}]},
        ],
        "tools": [{"name": "Bash"}],
    }
    snap = json.dumps(body)
    out, cut = lean_summary_body(body, max_chars=2000)
    assert cut == 3000
    # Sortie d'outil tronquée + marqueur ; tout le reste intact.
    tr = out["messages"][2]["content"][0]
    assert len(tr["content"]) < len(big) and "omitted by proxy summary-lean" in tr["content"]
    assert out["messages"][0]["content"].endswith("Q" * 5000)
    assert out["messages"][3]["content"][0]["text"].endswith("N" * 5000)
    assert out["system"].endswith("S" * 5000)
    assert out["tools"] == [{"name": "Bash"}]
    assert json.dumps(body) == snap, "l'entrée ne doit jamais être mutée"


def test_lean_chat_and_responses_shapes():
    from app.compaction import lean_summary_body

    chat = {"messages": [{"role": "tool", "content": "T" * 3000}]}
    out, cut = lean_summary_body(chat, max_chars=2000)
    assert cut == 1000 and len(out["messages"][0]["content"]) < 3000

    resp = {"input": [{"type": "function_call_output", "output": "O" * 3000}]}
    out, cut = lean_summary_body(resp, max_chars=2000)
    assert cut == 1000

    short = {"messages": [{"role": "tool", "content": "ok"}]}
    out, cut = lean_summary_body(short, max_chars=2000)
    assert cut == 0 and out == short

    assert lean_summary_body(None, max_chars=2000) == (None, 0)
    assert lean_summary_body({}, max_chars=2000) == ({}, 0)
    assert lean_summary_body({"messages": []}, max_chars=-5)[1] == 0


def test_lean_never_raises():
    from app.compaction import lean_summary_body

    assert lean_summary_body(object(), max_chars=2000)[1] == 0
    assert lean_summary_body({"messages": [{"role": "tool"}]}, max_chars=2000)[1] == 0
    assert lean_summary_body({"messages": [None, 42]}, max_chars=2000)[1] == 0


# ── template structuré officiel : rubriques + validation ──


def test_summary_template_has_required_outline():
    from app.compaction import build_prompt, build_summary_user_text, has_required_outline

    t = build_summary_user_text("HIST", "ANCIEN")
    assert has_required_outline(t) is True
    assert "## Objective" in t and "<previous-summary>" in t
    # Branche update officielle : ne pas reproduire les wrappers.
    assert "Do not reproduce the" in build_prompt(True) and "<summary>" in build_prompt(True)
    t2 = build_summary_user_text("HIST", None)
    assert has_required_outline(t2) is True
    assert "## Requirements" in t2 and "## Work State" in t2
    assert "Do not continue the task or call tools." in t2
    assert has_required_outline("bavardage sans structure") is False
    assert has_required_outline("") is False
    assert has_required_outline(None) is False
    # Sémantique officielle filled() : ligne exacte, sensible à la casse.
    assert has_required_outline("## decisions\nok") is False
    assert has_required_outline("## Decisions\nok") is True
    assert has_required_outline("texte ## Objective dans la phrase") is False


def test_serialize_strips_media_to_descriptors():
    import app.compaction.summarizer as _summ

    hist = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "regarde"},
                {"type": "image", "source": {"type": "base64", "data": "AAA"}},
                {"type": "file", "filename": "doc.pdf"},
            ],
        },
    ]
    txt = _summ._serialize_history_for_summary(hist)
    assert "[Attached image:" in txt and "[Attached file: doc.pdf]" in txt
    assert "AAA" not in txt, "octets embarqués ne partent jamais"
    assert _summ._serialize_history_for_summary(None) == ""
    assert _summ._serialize_history_for_summary(object()) == ""


# ── keep_tokens budgété + garde huge_context (aligné officiel, opt-in) ──


def _mk_msgs(n_pairs=4, size=100):
    msgs = []
    for i in range(n_pairs):
        msgs.append({"role": "user", "content": "q" * size})
        msgs.append({"role": "assistant", "content": "a" * size})
    return msgs


def test_split_budget_keeps_newest_within_tokens():
    from app.compaction import split_keep_recent

    def est(t):
        return len(t) // 4  # ~50 tokens/paire de 200 chars

    old, recent = split_keep_recent(_mk_msgs(4), 99, keep_tokens=120, estimate_fn=est)
    assert len(recent) == 4  # 2 paires ≈ 100 tokens ≤ 120
    assert len(old) == 4
    old, recent = split_keep_recent(_mk_msgs(4), 99, keep_tokens=10, estimate_fn=est)
    assert len(recent) == 2, "toujours au moins la paire la plus récente"
    # Legacy inchangé sans budget.
    old, recent = split_keep_recent(_mk_msgs(4), 1)
    assert len(recent) == 2 and len(old) == 6
    # Estimateur fautif → repli legacy, jamais d'exception.
    old, recent = split_keep_recent(_mk_msgs(4), 1, keep_tokens=120, estimate_fn=lambda t: 1 / 0)
    assert len(recent) == 2
    assert split_keep_recent(None, 1, keep_tokens=10, estimate_fn=est) == ([], [])


def test_split_budget_never_cuts_tool_pair():
    from app.compaction import split_keep_recent

    msgs = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "out"}]},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    old, recent = split_keep_recent(msgs, 99, keep_tokens=10, estimate_fn=lambda t: len(t))
    # La coupe tombe sur un vrai user, jamais au milieu d'une paire outil.
    assert recent and recent[0].get("role") == "user"
    assert not any(
        isinstance(m, dict)
        and m.get("role") == "user"
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in (m.get("content") or []))
        for m in ([recent[0]] if recent else [])
    )


def test_huge_context_check_matrix():
    from app.compaction import huge_context_check

    assert huge_context_check(100, 1000) == (None, "")
    assert huge_context_check(10**7, 10**6)[0] is None  # seuils à 0 = off
    level, msg = huge_context_check(10**6, 250000, warn_tokens=200000)
    assert level == "warn" and "250000" in msg
    level, msg = huge_context_check(10**6, 500000, warn_tokens=200000, deny_tokens=450000)
    assert level == "deny" and "450000" in msg and "/compact" in msg
    level, _ = huge_context_check(10**7, 1000, deny_tokens=450000)
    assert level == "deny", "le volume brut suffit (pas d'estimation)"
    assert huge_context_check(None, None, warn_tokens=1) == (None, "")
    assert huge_context_check(object(), object(), warn_tokens=1, deny_tokens=2) == (None, "")


# ── cap officiel 32000 (SUMMARY_OUTPUT_MAX) : should_clamp / cap ──


def test_should_clamp_prefilter():
    from app.compaction import should_clamp_summary

    assert should_clamp_summary({"max_tokens": 128000}, 4096) is True
    assert should_clamp_summary({"max_tokens": 4096}, 4096) is False
    assert should_clamp_summary({"max_tokens": 32}, 4096) is False
    assert should_clamp_summary({"max_output_tokens": 128000}, 4096) is True
    assert should_clamp_summary({}, 4096) is False
    assert should_clamp_summary(None, 4096) is False
    assert should_clamp_summary({"max_tokens": True}, 4096) is False
    assert should_clamp_summary({"max_tokens": "x"}, 4096) is False
    assert should_clamp_summary(object(), 4096) is False


def test_cap_summary_max_tokens_official():
    from app.compaction import cap_summary_max_tokens

    body = {"model": "m", "messages": [{"role": "user", "content": "x"}], "max_tokens": 128000}
    snap = json.dumps(body)
    out, changed = cap_summary_max_tokens(body, 4096)
    assert changed is True
    assert out["max_tokens"] == 4096
    assert json.dumps(body) == snap, "original intact pour logs/DB"

    out, changed = cap_summary_max_tokens({"max_tokens": 4096}, 4096)
    assert (changed, out["max_tokens"]) == (False, 4096)
    out, changed = cap_summary_max_tokens({"max_tokens": 32}, 4096)
    assert (changed, out["max_tokens"]) == (False, 32)
    out, changed = cap_summary_max_tokens({"input": [], "max_output_tokens": 128000}, 4096)
    assert (changed, out["max_output_tokens"]) == (True, 4096)
    # Jamais de remontée, jamais d'ajout de clé.
    out, changed = cap_summary_max_tokens({"messages": []}, 4096)
    assert changed is False and "max_tokens" not in out
    assert cap_summary_max_tokens(None, 4096) == (None, False)
    assert cap_summary_max_tokens({}, 0) == ({}, False)
