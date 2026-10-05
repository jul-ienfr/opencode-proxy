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
    body["messages"] = [{"role": "user", "content": "x" * 1000}]
    assert is_compaction_shape(body) is True


def test_min_chars_implicit_is_configurable():
    # La borne est un paramètre (config.yaml:server_compaction.min_chars_implicit),
    # pas une constante en dur : un texte de 500 chars passe avec borne=100.
    body = {
        "model": "haiku",
        "messages": [{"role": "user", "content": "x" * 500}],
    }
    assert is_compaction_shape(body) is False
    assert is_compaction_shape(body, min_chars_implicit=100) is True
    # Marqueur explicite : pas de borne, même courte (on croit le client).
    marked = {"messages": [{"role": "user", "content": "<conversation-checkpoint><summary>s</summary></conversation-checkpoint>"}]}
    assert is_compaction_shape(marked, min_chars_implicit=10**9) is True


def test_official_multi_user_text_shape():
    assert is_compaction_shape(_official(n=3)) is True


def test_official_blocks_form():
    body = {
        "model": "opus",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hello world" * 200}]}],
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
            {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "tool_result", "tool_use_id": "t1", "content": "out"}]},
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
            {"role": "user", "content": "<conversation-checkpoint><summary>done X</summary><recent-context>last tool</recent-context></conversation-checkpoint>"},
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
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "summary text here" * 100}]}],
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
        (400, ""),
        (400, None),
        (400, b"input too long for model"),  # bytes OK → True, cas séparé ci-dessous
    ],
)
def test_overflow_false_cases(status, body):
    expected = True if isinstance(body, bytes) else False
    assert is_overflow(status, body) is expected


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
    return {"role": "assistant", "content": [{"type": "text", "text": "go"}, {"type": "tool_use", "id": i, "name": "bash", "input": {}}]}


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
    assert old == msgs and recent == []
    assert split_keep_recent(None, 2) == ([], [])
    assert split_keep_recent("x", 2) == ([], [])


def test_build_condensed_history_shape():
    msgs = [_u("x"), _a("y"), _u("next"), _a("ok")]
    condensed, kept = build_condensed_history(msgs, "  RESUME  ", 1)
    assert condensed[0]["role"] == "user"
    assert "<conversation-checkpoint><summary>RESUME</summary>" in condensed[0]["content"]
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


def test_summarizer_body_caps_at_4096():
    assert build_summarizer_body("x", 128000)["max_tokens"] == 4096  # cap officiel
    assert build_summarizer_body("x", 512)["max_tokens"] == 512
    assert build_summarizer_body("x", -5)["max_tokens"] == 2048  # repli sûr


def test_previous_summary_chaining():
    msgs = [
        {"role": "user", "content": "<conversation-checkpoint><summary>ANCIEN</summary><recent-context>r</recent-context></conversation-checkpoint>"},
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
        return _FakeResp(200, b'{"content": [{"type": "text", "text": "RESUME"}]}'), h

    r = await run_summarizer(
        [{"role": "user", "content": "hello world"}],
        model_id="m", endpoint="e", protocol="anthropic",
        auth_headers_fn=lambda p: {}, do_request_fn=do,
    )
    assert r == "RESUME"


@pytest.mark.asyncio
async def test_run_summarizer_openai_success():
    async def do(ep, body, h, p):
        return _FakeResp(200, b'{"choices": [{"message": {"content": "RES2"}}]}'), h

    r = await run_summarizer(
        [{"role": "user", "content": "hi there"}],
        model_id="m", endpoint="e", protocol="openai",
        auth_headers_fn=lambda p: {}, do_request_fn=do,
    )
    assert r == "RES2"


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
    content = b'{"choices": [{"message": {"content": "RESUME-CONDENSE"}}]}'


async def _ok_do(endpoint, body, headers, protocol):
    return _OkResp(), headers


@pytest.mark.asyncio
async def test_maybe_condense_disabled_returns_none():
    # enabled:false (défaut) → None, jamais d'appel résumeur (fail-open).
    async def _boom(*a, **k):
        raise AssertionError("do_request_fn ne doit pas être appelé")

    r = await maybe_condense(
        _hist(), status_code=400, body_text="prompt too long",
        model_id="m", endpoint="e", protocol="openai",
        auth_headers_fn=lambda p: {}, do_request_fn=_boom, enabled=False,
    )
    assert r is None


@pytest.mark.asyncio
async def test_maybe_condense_non_overflow_returns_none():
    # 429/500/400-non-marqueur → None (pas de compaction sur backoff/erreur).
    kw = {"model_id": "m", "endpoint": "e", "protocol": "openai",
          "auth_headers_fn": lambda p: {}, "do_request_fn": _ok_do, "enabled": True}
    hist = _hist()
    assert await maybe_condense(hist, status_code=429, body_text="prompt too long", **kw) is None
    assert await maybe_condense(hist, status_code=500, body_text="prompt too long", **kw) is None
    assert await maybe_condense(hist, status_code=400, body_text="rate limit exceeded", **kw) is None


@pytest.mark.asyncio
async def test_maybe_condense_overflow_condenses():
    # 400-overflow + enabled → [checkpoint, *recent] prêt à forwarder.
    r = await maybe_condense(
        _hist(3), status_code=400, body_text="input is too long for model",
        model_id="m", endpoint="e", protocol="openai",
        auth_headers_fn=lambda p: {}, do_request_fn=_ok_do,
        enabled=True, keep_recent_pairs=1,
    )
    assert isinstance(r, list) and len(r) == 3  # checkpoint + 1 paire récente
    assert "<conversation-checkpoint><summary>RESUME-CONDENSE</summary>" in r[0]["content"]
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

    kw = {"status_code": 400, "body_text": "prompt too long", "model_id": "m",
          "endpoint": "e", "protocol": "openai", "auth_headers_fn": lambda p: {},
          "enabled": True, "timeout_s": 5}
    hist = _hist()
    assert await maybe_condense(hist, do_request_fn=_do_400, **kw) is None
    assert await maybe_condense(hist, do_request_fn=_do_boom, **kw) is None


@pytest.mark.asyncio
async def test_maybe_condense_never_raises():
    # Garbage partout → None, jamais d'exception (fail-open total).
    r = await maybe_condense(
        "pas-une-liste", status_code="???", body_text=object(),
        model_id="m", endpoint="e", protocol="openai",
        auth_headers_fn=lambda p: 1 / 0, do_request_fn=None, enabled=True,
    )
    assert r is None
    assert await maybe_condense(None, status_code=400, body_text="prompt too long",
                                model_id="m", endpoint="e", protocol="openai",
                                auth_headers_fn=lambda p: {}, do_request_fn=_ok_do,
                                enabled=True) is None


def _rin(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def test_build_condensed_input_shape():
    # Miroir Responses de build_condensed_history : [checkpoint_input, *recent],
    # forme input (jamais messages), coupe aux frontières de paires.
    hist = [_rin("a"), {"role": "assistant", "content": "b"}, _rin("c"), {"role": "assistant", "content": "d"}]
    condensed, kept = build_condensed_input(hist, "  RESUME  ", 1)
    assert condensed[0]["type"] == "message" and condensed[0]["role"] == "user"
    assert "<conversation-checkpoint><summary>RESUME</summary>" in condensed[0]["content"][0]["text"]
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
        return _FakeResp(200, b'{"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "R"}]}]}'), h

    hist = [_rin("vieux 1"), _rin("vieux 2"), _rin("recent"), {"role": "assistant", "content": "ok"}]
    r = await maybe_condense(
        hist, status_code=400, body_text="context window exceeded",
        model_id="m", endpoint="https://x/v1/responses", protocol="openai",
        auth_headers_fn=lambda p: {}, do_request_fn=_ok_resp,
        enabled=True, keep_recent_pairs=1, api="responses",
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
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": big}]},            {"role": "user", "content": [{"type": "text", "text": "note " + "N" * 5000}]},
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


# ── cap officiel 4096 : should_clamp_summary / cap_summary_max_tokens ──


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
