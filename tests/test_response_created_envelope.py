"""Tests V5.1 : response.created unwrap + poll + strict sémantique."""
import protocol_mapping as pm
import opencode as oc

def test_nonstream_response_created_unwrapped():
    data = {"type": "response.created", "sequence_number": 0, "response": {"id": "resp_123", "object": "response", "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}], "usage": {"input_tokens": 1, "output_tokens": 1}}}
    inner = oc._unwrap_responses_envelope(data)
    assert inner["id"] == "resp_123"
    assert "type" not in inner or inner.get("type") != "response.created"
    # conversion vers chat
    chat = pm._responses_to_chat_response(inner, "muse-spark-1.2-contributor-free")
    assert "choices" in chat
    assert chat["choices"][0]["message"]["content"] == "hi"

def test_stream_response_created_ignored():
    # streaming déjà géré : _responses_sse_to_chat_deltas retourne None
    assert pm._responses_sse_to_chat_deltas('{"type":"response.created","response":{"id":"resp_1"}}') is None
    assert pm._responses_sse_to_chat_deltas('{"type":"response.in_progress","response":{}}') is None
    assert pm._responses_sse_to_chat_deltas('{"type":"response.queued","response":{}}') is None

def test_strict_false_only_on_fallback_semantic():
    # simple bash sans anyOf → pas de strict:false
    chat_simple = {"model": "muse-spark-1.2-contributor", "messages": [{"role": "user", "content": "hi"}], "tools": [{"type": "function", "function": {"name": "bash", "description": "run", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}]}
    r1 = pm._chat_to_responses_request(chat_simple)
    assert "strict" not in r1["tools"][0]
    # avec anyOf → strict:false
    chat_fallback = {"model": "muse-spark-1.2-contributor", "messages": [{"role": "user", "content": "hi"}], "tools": [{"type": "function", "function": {"name": "bash", "description": "run", "parameters": {"type": "object", "properties": {"q": {"anyOf": [{"type": "string"}, {"type": "number"}]}}}}}]}
    r2 = pm._chat_to_responses_request(chat_fallback)
    assert r2["tools"][0]["strict"] is False
    # description contenant '"anyOf"' ne doit pas déclencher fallback (walk sémantique)
    chat_desc = {"model": "muse-spark-1.2-contributor", "messages": [{"role": "user", "content": "hi"}], "tools": [{"type": "function", "function": {"name": "bash", "description": "run", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": 'contains "anyOf" word'}}}}}]}
    r3 = pm._chat_to_responses_request(chat_desc)
    assert "strict" not in r3["tools"][0]

def test_reasoning_high_preserved_after_unwrap():
    data = {"type": "response.created", "response": {"id": "resp_1", "status": "completed", "output": [], "reasoning": {"effort": "high"}}}
    inner = oc._unwrap_responses_envelope(data)
    assert inner["reasoning"]["effort"] == "high"

def test_in_progress_nonstream_is_retryable():
    data = {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress", "output": []}}
    inner = oc._unwrap_responses_envelope(data)
    assert inner["status"] == "in_progress"
    # le handler doit répondre 503, pas 200 vide — on vérifie que l'enveloppe est détectée comme non completed
    assert inner["status"] in ("queued", "in_progress")
