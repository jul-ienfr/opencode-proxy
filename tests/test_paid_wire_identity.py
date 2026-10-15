"""Parité wire paid v2 : la jambe payante parle comme le client officiel v2.

Officiel v2 (`model-request.ts`, `app.ts:useragent`) : UA
`opencode/<channel>/<version>/<name>` (ex. `opencode/prod/2.0.28/cli`),
`x-opencode-session-id` + `x-session-affinity`/`X-Session-Id` + `x-opencode-session`
+ client/project/request. Jambe free exclue (verrou v1 octet-exact).
"""

import opencode as oc

RESP_EP = "https://opencode.ai/zen/go/v1/responses"
CHAT_EP = "https://opencode.ai/zen/go/v1/chat/completions"
MSG_EP = "https://opencode.ai/zen/go/v1/messages"


def test_responses_leg_gets_responses_ua():
    h = oc._enrich_paid_wire_headers({"Authorization": "Bearer k"}, RESP_EP)
    assert h["User-Agent"] == oc._OPENCODE_OFFICIAL_UA_V2
    assert h["User-Agent"] == "opencode/prod/2.0.28/cli"


def test_chat_leg_gets_chat_ua():
    h = oc._enrich_paid_wire_headers({"Authorization": "Bearer k"}, CHAT_EP)
    assert h["User-Agent"] == oc._OPENCODE_OFFICIAL_UA_V2
    assert h["User-Agent"].startswith("opencode/prod/")


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


# ---------------------------------------------------------------------------
# §6 D2 — `x-opencode-session-id` (le client officiel l'émet sur TOUTES les
# requêtes, request.ts:188-189, avant les en-têtes `opencode*`). Même sessionID
# que `x-opencode-session`, deux noms côté amont.
# ---------------------------------------------------------------------------


def test_session_id_present_on_both_openai_legs():
    for ep in (RESP_EP, CHAT_EP):
        h = oc._enrich_paid_wire_headers({"Authorization": "Bearer k"}, ep)
        assert h["x-opencode-session-id"].startswith("ses_")
        assert len(h["x-opencode-session-id"]) == 30
        # deux noms, une valeur : c'est tout l'objet de D2
        assert h["x-opencode-session-id"] == h["x-opencode-session"]


def test_session_id_absent_on_anthropic_leg():
    h = {"Authorization": "Bearer k", "x-api-key": "k"}
    out = oc._enrich_paid_wire_headers(dict(h), MSG_EP)
    assert out == h
    assert "x-opencode-session-id" not in {k.lower() for k in out}


def test_session_id_follows_the_conversation_session():
    """Avec clé de conversation : session-id miroir de `x-opencode-session`,
    donc affinité sticky par conversation, comme la jambe free."""
    h = oc._enrich_paid_wire_headers(
        {"Authorization": "Bearer k"}, RESP_EP, "conv-D2"
    )
    assert h["x-opencode-session"] == oc._conversation_session_id("conv-D2")
    assert h["x-opencode-session-id"] == h["x-opencode-session"]


def test_session_id_mirrors_a_client_supplied_session():
    """setdefault : un `ses_` déjà posé par l'appelant est reproduit tel quel
    dans session-id (aucune valeur inventée par-dessus)."""
    h = oc._enrich_paid_wire_headers(
        {"Authorization": "Bearer k", "x-opencode-session": "ses_" + "a" * 26},
        RESP_EP,
    )
    assert h["x-opencode-session"] == "ses_" + "a" * 26
    assert h["x-opencode-session-id"] == "ses_" + "a" * 26


def test_session_id_respects_a_client_supplied_value():
    """Une session-id déjà présente, même différente de `x-opencode-session`,
    n'est jamais réécrite."""
    h = oc._enrich_paid_wire_headers(
        {
            "Authorization": "Bearer k",
            "x-opencode-session-id": "ses_client_supplied_value",
            "x-opencode-session": "ses_" + "b" * 26,
        },
        RESP_EP,
    )
    assert h["x-opencode-session-id"] == "ses_client_supplied_value"


def test_session_id_absent_when_no_session_could_be_minted(monkeypatch):
    """Pas de `ses_` en amont → pas d'en-tête orphelin qui mentirait sur
    l'identité de session (la même prudence que `x-opencode-session`)."""
    monkeypatch.setattr(oc, "_paid_session_id", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(
        oc, "_conversation_session_id", lambda k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    h = oc._enrich_paid_wire_headers({"Authorization": "Bearer k"}, RESP_EP)
    assert "x-opencode-session" not in h
    assert "x-opencode-session-id" not in h
    # le reste de l'identité paid survit : l'échec du ses_ n'annule rien
    assert h["x-opencode-client"] == oc._OPENCODE_CLIENT_NAME
    assert h["x-opencode-request"].startswith("msg_")


def test_session_id_is_case_insensitive_on_the_incoming_key():
    """`lowered` est calculé avant écriture : une clé reçue en `X-Opencode-…`
    compte comme déjà posée (même sémantique que les voisins)."""
    h = oc._enrich_paid_wire_headers(
        {"Authorization": "Bearer k", "X-Opencode-Session-Id": "ses_FROM_CLIENT"},
        RESP_EP,
    )
    assert h["X-Opencode-Session-Id"] == "ses_FROM_CLIENT"
    assert "x-opencode-session-id" not in h


def test_free_leg_still_has_no_session_id():
    """Jambe free : `x-opencode-session-id` = même ses_ (re-mesuré 1.19.0)."""
    h = oc._official_free_headers("")
    assert h["x-opencode-session-id"] == h["x-opencode-session"]
    assert "x-opencode-session" in h


def test_paid_session_stable_within_window():
    assert oc._paid_session_id() == oc._paid_session_id()


def test_enrichment_is_idempotent():
    once = oc._enrich_paid_wire_headers({"Authorization": "Bearer k"}, RESP_EP)
    twice = oc._enrich_paid_wire_headers(dict(once), RESP_EP)
    assert twice == once
