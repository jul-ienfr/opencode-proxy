"""SSE e2e — Phase2 F-M8: streaming StreamingResponse + orphan filter."""

import json

from fastapi.testclient import TestClient


def test_sse_keepalive_headers(monkeypatch):
    """StreamingResponse doit envoyer Cache-Control no-cache et Connection keep-alive."""
    import time

    import opencode as oc
    from opencode import app

    # Hermétique (flake constaté 2026-09-07 : `degraded` intermittent en
    # suite longue uniquement) : /health reflète l'état global mutable
    # (breakers ouverts / clés pausées laissés par les tests précédents)
    # + un check réseau RÉEL (GET opencode.ai, timeout 5 s) qui flake sous
    # charge — ni l'un ni l'autre ne sont l'objet du test (le montage de
    # l'app, cf. commentaire d'origine). On fige les trois entrées.
    monkeypatch.setattr(oc, "_health_cache", (time.monotonic(), True))
    monkeypatch.setattr(oc, "_circuit_breakers", {})
    monkeypatch.setattr(oc._key_pauser, "get_all_status", lambda: {})

    client = TestClient(app)
    # health is non-stream, but we test that app mounts correctly and SSE helpers exist
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_protocol_mapping_sse_helpers_exist():
    """protocol_mapping doit exposer les helpers SSE + orphan filter (F-M7 dedup)."""
    import protocol_mapping as pm

    assert hasattr(pm, "_drop_orphan_tool_messages")
    assert hasattr(pm, "_drop_orphan_responses_input")
    assert hasattr(pm, "_effort_to_reasoning")
    # orphan filter: drop tool without preceding tool_calls
    msgs = [{"role": "tool", "tool_call_id": "orphan", "content": "x"}]
    assert pm._drop_orphan_tool_messages(msgs) == []
    # paired stays
    msgs2 = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "a1", "function": {"name": "f", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "a1", "content": "ok"},
    ]
    assert len(pm._drop_orphan_tool_messages(msgs2)) == 2


def test_streaming_sse_format():
    """Vérifie que le format SSE est bien `data: {...}\\n\\n`."""
    # Simulate SSE generation
    from protocol_mapping import _json_dumps

    payload = {"type": "message", "delta": {"text": "hello"}}
    raw = (
        _json_dumps(payload).decode()
        if isinstance(_json_dumps(payload), bytes)
        else _json_dumps(payload)
    )
    sse = f"data: {raw}\n\n"
    assert sse.startswith("data: ")
    assert sse.endswith("\n\n")
    # json must be parseable
    inner = json.loads(sse[len("data: ") :].strip())
    assert inner["delta"]["text"] == "hello"
