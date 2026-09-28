"""Voie unifiée du compactage : classify/plan/router/transport + natifs + multi-protocole.

Verrous purs (aucun réseau, aucune clé) :
- même classe free/payante que la conversation (jamais de bascule silencieuse) ;
- natifs provider détectés et relayés (exclusion cache via is_compaction_shape) ;
- résumeur construit dans la forme de l'API de destination ;
- transport : headers/request/normalize via DI, tunnel geo décidable.
"""

import sys

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
        endpoint="https://opencode.ai/zen/go/v1/chat/completions",
        protocol="openai",
        api="chat",
        route_fn=lambda x: None,
        model_config_fn=lambda x: {},
        resolve_free_fn=lambda x: "mimo-v2.5-free",
        free_endpoint_fn=lambda x: "https://opencode.ai/zen/v1/chat/completions",
        default_target="mimo-v2.5-free",
        is_free_fn=lambda x: True,
    )
    assert is_free is True
    assert m == "mimo-v2.5-free"
    assert "/zen/v1/" in ep
    assert api == "chat"


def test_plan_paid_conversation_stays_paid():
    m, ep, proto, api, is_free, seed = summarizer_plan(
        "gpt-6-luna",
        None,
        endpoint="https://opencode.ai/zen/go/v1/chat/completions",
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
            return "https://opencode.ai/zen/v1/chat/completions"
        return "https://opencode.ai/zen/v1/systemone"

    m, ep, proto, api, is_free, seed = summarizer_plan(
        "jev-1.13-free",
        None,
        endpoint="https://opencode.ai/zen/v1/systemone",
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
        "context_management": {"edits": [{"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": 150000}}]},
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
    assert is_compaction_shape({"messages": [{"role": "user", "content": "x" * 1200}]}) is True
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
    assert build_summarizer_for_api("hello", 99999, "m", "chat")["max_tokens"] == 4096


def test_should_use_tunnel_parity():
    assert should_use_tunnel(is_free=True, vpn_on=True) is True
    assert should_use_tunnel(is_free=False, geo_require_vpn=True) is True
    assert should_use_tunnel(is_free=False, geo_force_tunnel=True) is True
    assert should_use_tunnel(is_free=False) is False
