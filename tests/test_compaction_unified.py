"""Voie unifiée du compactage : classify/plan/router/transport + natifs + multi-protocole.

Verrous purs (aucun réseau, aucune clé) :
- même classe free/payante que la conversation (jamais de bascule silencieuse) ;
- natifs provider détectés et relayés (exclusion cache via is_compaction_shape) ;
- résumeur construit dans la forme de l'API de destination ;
- transport : headers/request/normalize via DI, tunnel geo décidable.
"""

import sys

import httpx
import pytest

sys.path.insert(0, ".")

from app.compaction import (  # noqa: E402
    build_summarizer_anthropic_body,
    build_summarizer_for_api,
    build_summarizer_responses_body,
    detect_compaction,
    has_free_leg,
    is_compaction_shape,
    is_free_class,
    is_native_compaction,
    should_use_tunnel,
    summarizer_plan,
)


def _free_map():
    return {"mimo-v2.5": "mimo-v2.5-free", "kimi-k2.6": "mimo-v2.5-free"}


def test_classify_free_leg_covers_mapped_paid_and_free_ids():
    assert has_free_leg("mimo-v2.5", free_model_map=_free_map()) is True
    assert has_free_leg("mimo-v2.5-free", is_free_route_fn=lambda m: m.endswith("-free")) is True
    assert has_free_leg("gpt-6-luna", free_model_map=_free_map(), free_models=set()) is False
    assert has_free_leg("", free_model_map=_free_map()) is False
    assert has_free_leg(None, free_model_map=_free_map()) is False


def test_is_free_class_alias():
    assert is_free_class("kimi-k2.6", free_model_map=_free_map()) is True
    assert is_free_class("claude-opus-4-1", free_model_map=_free_map()) is False


def test_plan_free_conversation_stays_free_with_chat_api():
    m, ep, proto, api, is_free, seed = summarizer_plan(
        "mimo-v2.5",
        None,
        endpoint="https://opencode.ai/inference/go/openai/v1/chat/completions",
        protocol="openai",
        api="chat",
        route_fn=lambda x: None,
        model_config_fn=lambda x: {},
        resolve_free_fn=lambda x: "mimo-v2.5-free",
        free_endpoint_fn=lambda x: "https://opencode.ai/inference/openai/v1/chat/completions",
        default_target="mimo-v2.5-free",
        is_free_fn=lambda x: True,
    )
    assert is_free is True
    assert m == "mimo-v2.5-free"
    assert "/inference/" in ep and "/go/" not in ep
    assert api == "chat"


def test_plan_paid_conversation_stays_paid():
    m, ep, proto, api, is_free, seed = summarizer_plan(
        "gpt-6-luna",
        None,
        endpoint="https://opencode.ai/inference/go/openai/v1/chat/completions",
        protocol="openai",
        api="chat",
        route_fn=lambda x: None,
        model_config_fn=lambda x: {},
        resolve_free_fn=lambda x: None,
        free_endpoint_fn=lambda x: None,
        default_target="",
        is_free_fn=lambda x: False,
    )
    assert is_free is False
    assert m == "gpt-6-luna"


def test_plan_systemone_never_gets_chat_body():
    def _ep_for(mid):
        if mid == "mimo-v2.5-free":
            return "https://opencode.ai/inference/openai/v1/chat/completions"
        return "https://opencode.ai/inference/v1/systemone"

    m, ep, proto, api, is_free, seed = summarizer_plan(
        "jev-1.13-free",
        None,
        endpoint="https://opencode.ai/inference/v1/systemone",
        protocol="openai",
        api="chat",
        route_fn=lambda x: None,
        model_config_fn=lambda x: {},
        resolve_free_fn=lambda x: "jev-1.13-free",
        free_endpoint_fn=_ep_for,
        default_target="mimo-v2.5-free",
        is_free_fn=lambda x: True,
    )
    # Repli chat-compatible, jamais systemone, jamais payant silencieux.
    assert "/systemone" not in (ep or "")
    assert is_free is True
    assert m == "mimo-v2.5-free"


def test_native_anthropic_detected_and_counts_as_compaction():
    body = {
        "model": "x",
        "messages": [{"role": "user", "content": "hello"}],
        "context_management": {
            "edits": [{"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": 150000}}]
        },
    }
    assert is_native_compaction(body) is True
    assert is_compaction_shape(body) is True
    is_c, is_n, kind = detect_compaction(body)
    assert is_c is True and is_n is True


def test_native_responses_threshold_detected():
    body = {
        "model": "x",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "context_management": {"compact_threshold": 10000},
        "store": False,
    }
    assert is_native_compaction(body) is True
    assert is_compaction_shape(body) is True


def test_native_responses_official_array_shape_detected():
    """Forme RÉELLE du client officiel V2 : context_management est un tableau."""
    body = {
        "model": "x",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "context_management": [{"type": "compaction", "compact_threshold": 10000}],
        "store": False,
    }
    assert is_native_compaction(body) is True
    assert is_compaction_shape(body) is True
    is_c, is_n, kind = detect_compaction(body)
    assert is_c is True and is_n is True
    assert kind == "native-responses-threshold"
    assert is_native_compaction({**body, "context_management": []}) is False


def test_compaction_item_opaque_detected_not_interpreted():
    body = {
        "model": "x",
        "input": [
            {"type": "compaction", "id": "cmp_1", "encrypted_content": "opaque"},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]},
        ],
    }
    assert is_native_compaction(body) is True
    assert is_compaction_shape(body) is True


def test_official_and_marker_still_detected():
    assert is_compaction_shape(
        {"messages": [{"role": "user", "content": "Summarize the conversation: " + "x" * 1200}]}
    ) is True
    # A long generic prompt is no longer a compaction shape.
    assert is_compaction_shape({"messages": [{"role": "user", "content": "x" * 1200}]}) is False
    assert is_compaction_shape({"messages": [{"role": "user", "content": "<conversation-checkpoint>hi"}]}) is True
    assert is_compaction_shape({"messages": [{"role": "user", "content": "hi"}]}) is False


def test_summarizer_bodies_match_api():
    chat = build_summarizer_for_api("hello", 2048, "m", "chat")
    assert "messages" in chat and "input" not in chat
    assert chat["stream"] is False
    assert "tools" not in chat and "system" not in chat
    resp = build_summarizer_responses_body("hello", 2048, "m")
    assert "input" in resp and "messages" not in resp
    assert resp["store"] is False
    anth = build_summarizer_anthropic_body("hello", 2048, "m")
    assert "messages" in anth and "input" not in anth
    assert build_summarizer_for_api("hello", 99999, "m", "chat")["max_tokens"] == 32000


def test_should_use_tunnel_parity():
    assert should_use_tunnel(is_free=True, vpn_on=True) is True
    assert should_use_tunnel(is_free=False, geo_require_vpn=True) is True
    assert should_use_tunnel(is_free=False, geo_force_tunnel=True) is True
    assert should_use_tunnel(is_free=False) is False


class _FakeUpstreamResp:
    def __init__(self, status=200, content=b"{}", content_type="application/json"):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": content_type}


def _install_native_compact(monkeypatch, *, response=None, enabled=True, endpoint="https://up.example/v1/responses"):
    import opencode as oc

    calls = []

    async def fake_do(url, body, headers, protocol):
        calls.append((url, dict(body), protocol))
        return response or _FakeUpstreamResp(), headers

    monkeypatch.setattr(oc, "_route_for", lambda model: {"model": "upstream-model"})
    monkeypatch.setattr(
        oc,
        "get_model_config",
        lambda model: {"endpoint": endpoint, "protocol": "openai"},
    )

    def _fake_sc(key=None, default=None):
        if key == "native_passthrough":
            return enabled
        return default

    monkeypatch.setattr(oc, "get_server_compaction", _fake_sc)
    monkeypatch.setattr(oc, "_get_auth_headers", lambda protocol: {"Authorization": "Bearer test"})
    monkeypatch.setattr(oc, "_do_request_with_retry", fake_do)
    return oc, calls


@pytest.mark.asyncio
async def test_native_responses_compact_forwards_and_strips_stream(monkeypatch):
    oc, calls = _install_native_compact(
        monkeypatch,
        response=_FakeUpstreamResp(200, b'{"id":"cmp_1","object":"response.compaction"}'),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=oc.app), base_url="http://proxy.test") as client:
        r = await client.post(
            "/v1/responses/compact",
            json={
                "model": "client-alias",
                "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                "stream": True,
            },
        )
    assert r.status_code == 200
    assert r.json()["id"] == "cmp_1"
    assert calls and calls[0][0] == "https://up.example/v1/responses/compact"
    assert calls[0][1]["model"] == "upstream-model"
    assert "stream" not in calls[0][1]


@pytest.mark.asyncio
async def test_native_responses_compact_relays_upstream_errors(monkeypatch):
    oc, _calls = _install_native_compact(
        monkeypatch,
        response=_FakeUpstreamResp(413, b'{"error":"payload too large"}'),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=oc.app), base_url="http://proxy.test") as client:
        r = await client.post("/v1/responses/compact", json={"model": "client-alias", "input": []})
    assert r.status_code == 413
    assert r.content == b'{"error":"payload too large"}'


@pytest.mark.asyncio
async def test_native_responses_compact_refuses_non_native_or_disabled(monkeypatch):
    oc, _calls = _install_native_compact(monkeypatch, enabled=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=oc.app), base_url="http://proxy.test") as client:
        r = await client.post("/v1/responses/compact", json={"model": "client-alias", "input": []})
    assert r.status_code == 400
    assert "disabled" in r.text

    oc2, _calls2 = _install_native_compact(
        monkeypatch, endpoint="https://up.example/v1/chat/completions"
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=oc2.app), base_url="http://proxy.test") as client:
        r2 = await client.post("/v1/responses/compact", json={"model": "client-alias", "input": []})
    assert r2.status_code == 400
    assert "does not support" in r2.text
