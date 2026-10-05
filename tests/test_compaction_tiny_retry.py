"""Retry-on-tiny bufferisé des streams de compactage (option 1, free-only).

- pur : gate, prédicat tiny (miroir du garde suspect_tiny_output), synthèse SSE ;
- worker : tiny → 1 refetch station fraîche pré-réponse, puis SSE synthétisé ;
  échec quelconque → None (live streaming inchangé, fail-open total).
"""

import sys

import pytest

sys.path.insert(0, ".")

import opencode as oc  # noqa: E402
from app.compaction import (  # noqa: E402
    anthropic_sse_from_completion,
    chat_sse_from_completion,
    completion_text,
    is_tiny_result,
    should_buffer_compaction,
)


def test_gate_matrix():
    assert should_buffer_compaction(
        is_stream=True, is_compaction=True,
        tiny_retry_enabled=True, has_free_leg=True,
    ) is True
    kw = {"is_stream": True, "is_compaction": True,
          "tiny_retry_enabled": True, "has_free_leg": True}
    assert should_buffer_compaction(**{**kw, "is_stream": False}) is False
    assert should_buffer_compaction(**{**kw, "is_compaction": False}) is False
    assert should_buffer_compaction(**{**kw, "tiny_retry_enabled": False}) is False
    assert should_buffer_compaction(**{**kw, "has_free_leg": False}) is False
    # Pas de veto outils : les compactions Claude Code déclarent 15+ outils.
    assert should_buffer_compaction(**kw) is True
    assert should_buffer_compaction() is False
    assert should_buffer_compaction(is_stream=1, is_compaction={},
                                    tiny_retry_enabled=1, has_free_leg="x") is False


def test_final_has_nontext_blocks_matrix():
    from app.compaction import final_has_nontext_blocks

    assert final_has_nontext_blocks({"content": [{"type": "text", "text": "ok"}]}) is False
    assert final_has_nontext_blocks({"content": [{"type": "tool_use", "name": "Bash"}]}) is True
    assert final_has_nontext_blocks({"content": [{"type": "thinking", "thinking": "h"}]}) is True
    assert final_has_nontext_blocks({"content": [{"type": "compaction", "x": 1}]}) is True
    assert final_has_nontext_blocks({"choices": [{"message": {"content": "ok"}}]}) is False
    assert final_has_nontext_blocks({"choices": [{"message": {"content": "ok", "tool_calls": [{"id": "1"}]}}]}) is True
    assert final_has_nontext_blocks({"choices": [{"message": {"content": "ok", "reasoning_content": "r"}}]}) is True
    assert final_has_nontext_blocks({"output": [{"type": "message"}]}) is False
    assert final_has_nontext_blocks({"output": [{"type": "function_call"}]}) is True
    assert final_has_nontext_blocks({"output": [{"type": "compaction"}]}) is True
    assert final_has_nontext_blocks({}) is False
    assert final_has_nontext_blocks(None) is True
    assert final_has_nontext_blocks(object()) is True


def test_tiny_mirrors_server_guard():
    cases = [
        (50000, 17, [], True),
        (40000, 99, [], True),
        (39999, 17, [], False),
        (50000, 100, [], False),
        (100000, 20, ["bash"], False),
    ]
    for inp, out, tools, expected in cases:
        assert is_tiny_result(inp, out, tools) is expected
        assert oc._is_suspect_tiny_output(inp, out, tools) is expected


def test_chat_sse_roundtrip():
    evs = chat_sse_from_completion(
        "RESUME", model="m", msg_id="c1", created=1,
        prompt_tokens=50000, completion_tokens=500,
    )
    raw = b"".join(evs).decode()
    assert "RESUME" in raw
    assert raw.rstrip().endswith("[DONE]")
    assert '"finish_reason": "stop"' in raw or '"finish_reason":"stop"' in raw


def test_anthropic_sse_sequence():
    evs = anthropic_sse_from_completion(
        "RESUME", model="m", msg_id="msg1",
        input_tokens=50000, output_tokens=500, cache_read=49000,
    )
    raw = b"".join(evs).decode()
    order = ["message_start", "content_block_start", "content_block_delta",
             "content_block_stop", "message_delta", "message_stop"]
    idx = -1
    for name in order:
        j = raw.find("event: " + name)
        assert j > idx, name
        idx = j
    assert "RESUME" in raw
    assert "49000" in raw


def test_completion_text_formats():
    assert completion_text({"content": [{"type": "text", "text": "A"}]}) == "A"
    assert completion_text({"choices": [{"message": {"content": "B"}}]}) == "B"
    assert completion_text({"output": [
        {"type": "compaction", "id": "c", "encrypted_content": "zzz"},
        {"type": "message", "content": [{"type": "output_text", "text": "C"}]},
    ]}) == "C"
    assert completion_text({}) == ""
    assert completion_text(None) == ""


class _FakeResp:
    def __init__(self, payload, status=200):
        import json

        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self.content = json.dumps(payload).encode()
        self.text = self.content.decode()

    def json(self):
        import json

        return json.loads(self.content.decode())


def _anthropic_payload(text, inp, out):
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": inp, "output_tokens": out},
    }


def test_tiny_retry_refetches_fresh_station(monkeypatch):
    """Tiny → cooldown + 1 refetch ; le 2e résultat est rendu tel quel."""
    calls = []

    async def _fake_pool(body, headers, protocol, model_id, forced_pool=None, req_id=None):
        calls.append(dict(body).get("stream"))
        if len(calls) == 1:
            return _FakeResp(_anthropic_payload("x", 50000, 17)), {}, "free-m", "9.9.9.9"
        return _FakeResp(_anthropic_payload("RESUME-COMPLET", 50000, 500)), {}, "free-m", "9.9.9.10"

    monkeypatch.setattr(oc, "_try_free_model_first", _fake_pool)
    import asyncio

    out = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        oc._try_free_with_tiny_retry({"messages": []}, {}, "anthropic", "paid-m")
    )
    assert len(calls) == 2, "un seul refetch attendu"
    data = out[0].json()
    assert data["content"][0]["text"] == "RESUME-COMPLET"


def test_tiny_retry_fail_open_on_pool_exhaustion(monkeypatch):
    async def _fake_none(*a, **k):
        return None

    monkeypatch.setattr(oc, "_try_free_model_first", _fake_none)
    import asyncio

    out = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        oc._try_free_with_tiny_retry({"messages": []}, {}, "anthropic", "paid-m")
    )
    assert out is None


def test_tiny_retry_propagates_refusal(monkeypatch):
    async def _fake_refuse(*a, **k):
        raise oc.FreeRefusal(status=429, body="q", retry_after="1")

    monkeypatch.setattr(oc, "_try_free_model_first", _fake_refuse)
    import asyncio

    with pytest.raises(oc.FreeRefusal):
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            oc._try_free_with_tiny_retry({"messages": []}, {}, "anthropic", "paid-m")
        )


@pytest.mark.asyncio
async def test_buffered_worker_streams_synthesized_sse(monkeypatch):
    """Worker : tiny puis complet → StreamingResponse avec le résumé, jamais de live."""
    from types import SimpleNamespace

    calls = []

    async def _fake_pool(body, headers, protocol, model_id, forced_pool=None, req_id=None):
        calls.append(1)
        if len(calls) == 1:
            return _FakeResp(_anthropic_payload("x", 50000, 17)), {}, "free-m", "9.9.9.9"
        return _FakeResp(_anthropic_payload("RESUME-COMPLET", 50000, 500)), {}, "free-m", "9.9.9.10"

    async def _no_save(*a, **k):
        return None

    monkeypatch.setattr(oc, "_try_free_model_first", _fake_pool)
    monkeypatch.setattr(oc, "_save_and_log_request", _no_save)
    monkeypatch.setattr(oc, "_update_token_usage", lambda *a, **k: None)

    req = SimpleNamespace(state=SimpleNamespace())
    body = {"model": "mimo-v2.5", "messages": [{"role": "user", "content": "x" * 1500}]}
    resp = await oc._buffered_compaction_stream(
        porte="messages", request=req, req_id="t1", original_model="mimo-v2.5",
        model_id="mimo-v2.5", endpoint="https://e", protocol="anthropic",
        headers={}, send_body=body, paid_body=body, client_ip="127.0.0.1",
        start_time=0.0, is_stream=True, thinking_type="none", effort="none",
        tool_names=[], request_body=body, is_compaction=True,
    )
    assert resp is not None, "le worker doit prendre en charge ce cas"
    assert len(calls) == 2, "refetch sur tiny attendu"
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode())
    raw = b"".join(chunks).decode()
    assert "RESUME-COMPLET" in raw
    assert "message_stop" in raw


@pytest.mark.asyncio
async def test_buffered_worker_falls_back_to_live_on_failure(monkeypatch):
    """Refus free → None : le live streaming tranche comme aujourd'hui."""
    from types import SimpleNamespace

    async def _fake_refuse(*a, **k):
        raise oc.FreeRefusal(status=429, body="q", retry_after="1")

    monkeypatch.setattr(oc, "_try_free_model_first", _fake_refuse)
    req = SimpleNamespace(state=SimpleNamespace())
    body = {"model": "mimo-v2.5", "messages": [{"role": "user", "content": "x" * 1500}]}
    resp = await oc._buffered_compaction_stream(
        porte="messages", request=req, req_id="t1", original_model="mimo-v2.5",
        model_id="mimo-v2.5", endpoint="https://e", protocol="anthropic",
        headers={}, send_body=body, paid_body=body, client_ip="127.0.0.1",
        start_time=0.0, is_stream=True, thinking_type="none", effort="none",
        tool_names=[], request_body=body, is_compaction=True,
    )
    assert resp is None


@pytest.mark.asyncio
async def test_buffered_worker_engages_with_tools_declared(monkeypatch):
    """Cas Claude Code réel : 15+ outils déclarés, tiny sans tool_use → retry + SSE.

    Sans le correctif du gate, ce cas retombait en live et le tiny atteignait
    le client (boucle de thrash). Sortie avec tool_use → live (fidélité).
    """
    from types import SimpleNamespace

    calls = []

    async def _fake_pool(body, headers, protocol, model_id, forced_pool=None, req_id=None):
        calls.append(1)
        if len(calls) == 1:
            return _FakeResp(_anthropic_payload("x", 50000, 17)), {}, "free-m", "9.9.9.9"
        return _FakeResp(_anthropic_payload("RESUME-OUTILS", 50000, 500)), {}, "free-m", "9.9.9.10"

    async def _no_save(*a, **k):
        return None

    monkeypatch.setattr(oc, "_try_free_model_first", _fake_pool)
    monkeypatch.setattr(oc, "_save_and_log_request", _no_save)
    monkeypatch.setattr(oc, "_update_token_usage", lambda *a, **k: None)

    req = SimpleNamespace(state=SimpleNamespace())
    body = {
        "model": "mimo-v2.5",
        "messages": [{"role": "user", "content": "x" * 1500}],
        "tools": [{"name": f"tool{i}", "description": "d", "input_schema": {"type": "object"}} for i in range(15)],
    }
    resp = await oc._buffered_compaction_stream(
        porte="messages", request=req, req_id="t1", original_model="mimo-v2.5",
        model_id="mimo-v2.5", endpoint="https://e", protocol="anthropic",
        headers={}, send_body=body, paid_body=body, client_ip="127.0.0.1",
        start_time=0.0, is_stream=True, thinking_type="none", effort="none",
        tool_names=[], request_body=body, is_compaction=True,
    )
    assert resp is not None, "avec outils déclarés le bufferisé doit s'enclencher"
    assert len(calls) == 2, "refetch sur tiny attendu"
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode())
    assert "RESUME-OUTILS" in b"".join(chunks).decode()


@pytest.mark.asyncio
async def test_buffered_worker_yields_to_live_on_tool_use_output(monkeypatch):
    """Sortie avec tool_use → None (live) : jamais de résumé amputé."""
    from types import SimpleNamespace

    async def _fake_tool_out(body, headers, protocol, model_id, forced_pool=None, req_id=None):
        return _FakeResp({
            "content": [
                {"type": "text", "text": "appel outil"},
                {"type": "tool_use", "id": "c1", "name": "Bash", "input": {}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 50000, "output_tokens": 120},
        }), {}, "free-m", "9.9.9.9"

    async def _no_save(*a, **k):
        return None

    monkeypatch.setattr(oc, "_try_free_model_first", _fake_tool_out)
    monkeypatch.setattr(oc, "_save_and_log_request", _no_save)
    monkeypatch.setattr(oc, "_update_token_usage", lambda *a, **k: None)

    req = SimpleNamespace(state=SimpleNamespace())
    body = {"model": "mimo-v2.5", "messages": [{"role": "user", "content": "x" * 1500}]}
    resp = await oc._buffered_compaction_stream(
        porte="messages", request=req, req_id="t1", original_model="mimo-v2.5",
        model_id="mimo-v2.5", endpoint="https://e", protocol="anthropic",
        headers={}, send_body=body, paid_body=body, client_ip="127.0.0.1",
        start_time=0.0, is_stream=True, thinking_type="none", effort="none",
        tool_names=[], request_body=body, is_compaction=True,
    )
    assert resp is None
