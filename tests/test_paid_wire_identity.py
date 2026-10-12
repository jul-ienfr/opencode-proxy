"""Parité wire paid : la jambe payante parle comme le client officiel.

Le client officiel envoie sur TOUTES les jambes (y compris payante) : UA
``opencode/<ver> ai-sdk/provider-utils/<v> runtime/bun/<v>`` (4.0.40 sur
/responses, 4.0.23 sur /chat — deux bundles ai-sdk) + ``x-opencode-session-id``
(toutes requêtes, même valeur que ``x-opencode-session``) +, pour les providers
``opencode*`` seulement, x-opencode-client / -project / -session / -request
(ses_/msg_ temporels). La jambe paid n'envoyait que Authorization +
Content-Type (+ UA python-httpx par défaut). La jambe free est exclue : son
wire est verrouillé octet-exact par la capture 243 requêtes de
``tests/test_official_client_parity.py``.
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
    """Jambe free verrouillée octet-exact par la capture 243 requêtes : aucun
    `session-id` n'y est ajouté (demanderait une re-mesure)."""
    h = oc._official_free_headers("")
    assert "x-opencode-session-id" not in {k.lower() for k in h}
    assert "x-opencode-session" in h


def test_paid_session_stable_within_window():
    assert oc._paid_session_id() == oc._paid_session_id()


def test_enrichment_is_idempotent():
    once = oc._enrich_paid_wire_headers({"Authorization": "Bearer k"}, RESP_EP)
    twice = oc._enrich_paid_wire_headers(dict(once), RESP_EP)
    assert twice == once
