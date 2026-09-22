"""Parité wire paid : la jambe payante parle comme le client officiel.

Le client officiel envoie sur TOUTES les jambes (y compris payante) : UA
``opencode/<ver> ai-sdk/provider-utils/<v> runtime/bun/<v>`` (4.0.40 sur
/responses, 4.0.23 sur /chat — deux bundles ai-sdk) + x-opencode-*
(ses_/msg_ temporels). La jambe paid n'envoyait que Authorization +
Content-Type (+ UA python-httpx par défaut).
"""

import opencode as oc

RESP_EP = "https://opencode.ai/zen/go/v1/responses"
CHAT_EP = "https://opencode.ai/zen/go/v1/chat/completions"
MSG_EP = "https://opencode.ai/zen/go/v1/messages"


def test_responses_leg_gets_responses_ua():
    h = oc._enrich_paid_wire_headers({"Authorization": "Bearer k"}, RESP_EP)
    assert h["User-Agent"] == oc._OPENCODE_OFFICIAL_UA_RESPONSES
    assert "4.0.40" in h["User-Agent"]


def test_chat_leg_gets_chat_ua():
    h = oc._enrich_paid_wire_headers({"Authorization": "Bearer k"}, CHAT_EP)
    assert h["User-Agent"] == oc._OPENCODE_OFFICIAL_UA
    assert "4.0.23" in h["User-Agent"]


def test_anthropic_leg_untouched():
    h = {"Authorization": "Bearer k", "x-api-key": "k"}
    assert oc._enrich_paid_wire_headers(dict(h), MSG_EP) == h


def test_authorization_never_touched_and_existing_ua_kept():
    h = oc._enrich_paid_wire_headers(
        {"Authorization": "Bearer paid-key", "User-Agent": "custom"},
        RESP_EP,
    )
    assert h["Authorization"] == "Bearer paid-key"
    assert h["User-Agent"] == "custom"


def test_opencode_ids_present_and_temporal():
    h = oc._enrich_paid_wire_headers({"Authorization": "Bearer k"}, RESP_EP)
    assert h["x-opencode-client"] == oc._OPENCODE_CLIENT_NAME
    assert h["x-opencode-project"] == oc._OPENCODE_PROJECT
    assert h["x-opencode-session"].startswith("ses_")
    assert len(h["x-opencode-session"]) == 30
    assert h["x-opencode-request"].startswith("msg_")


def test_paid_session_stable_within_window():
    assert oc._paid_session_id() == oc._paid_session_id()


def test_enrichment_is_idempotent():
    once = oc._enrich_paid_wire_headers({"Authorization": "Bearer k"}, RESP_EP)
    twice = oc._enrich_paid_wire_headers(dict(once), RESP_EP)
    assert twice == once
