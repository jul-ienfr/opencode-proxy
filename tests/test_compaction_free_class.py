"""Phase 2 — classe du résumeur de compaction (free vs payant).

Garantie : le résumé de compaction appartient à la MÊME classe que la
conversation active.

  * conversation free (id ``-free``, ou nom payant mappé dans
    ``free_model_map``) → résumeur **free** : endpoint free, ``Bearer public``,
    donc AUCUNE clé payante requise — c'est le correctif du blocage « compte
    sans crédit » quand l'utilisateur travaille sur un modèle gratuit ;
  * conversation réellement payante (aucun équivalent free) → résumeur
    **payant** ;
  * ``summarizer_model_override`` posé reste souverain (choix explicite).

Les tests sont purs : aucun réseau, aucune clé.
"""

import sys

import pytest

sys.path.insert(0, ".")

import opencode as oc  # noqa: E402

FREE_CONVERSATIONS = [
    # noms payants ayant un équivalent free déclaré
    "mimo-v2.5",
    "kimi-k2.6",
    "glm-5.1",
    "muse-spark-1.3-contributor",
    "deepseek-v4-flash",
    # ids déjà free
    "mimo-v2.5-free",
    "jev-1.13-free",
    "deepseek-v4-flash-free",
    "muse-spark-1.3-contributor-free",
]

PAID_CONVERSATIONS = [
    "gpt-6-luna",
    "claude-opus-4-1",
]


@pytest.mark.parametrize("model_id", FREE_CONVERSATIONS)
def test_free_conversation_gets_free_summarizer(model_id):
    """Conversation free → résumeur free, sur endpoint free, sans clé payante."""
    m, ep, proto, is_free = oc._compaction_summarizer_plan(
        model_id, None, endpoint="https://opencode.ai/zen/go/v1/chat/completions", protocol="openai"
    )[:4]
    assert is_free is True, f"{model_id!r} devrait produire un résumeur FREE"
    assert "/zen/go/" not in (ep or ""), f"résumeur free sur endpoint payant: {ep!r}"
    assert "/zen/v1/" in (ep or ""), f"endpoint free attendu, obtenu {ep!r}"


@pytest.mark.parametrize("model_id", FREE_CONVERSATIONS)
def test_free_summarizer_never_uses_paid_key(model_id):
    """La jambe free ne porte jamais la clé payante (Bearer public)."""
    m, ep, proto, is_free = oc._compaction_summarizer_plan(model_id, None, endpoint="", protocol="openai")[:4]
    assert is_free is True
    headers = oc._compaction_summarizer_headers(is_free, proto, ep)
    auth = str(headers.get("Authorization") or headers.get("x-api-key") or "")
    assert "public" in auth, f"jambe free authentifiée avec {auth!r}"
    assert not auth.lower().startswith("bearer sk-"), "clé payante envoyée sur la jambe free"


@pytest.mark.parametrize("model_id", PAID_CONVERSATIONS)
def test_paid_conversation_keeps_paid_summarizer(model_id):
    """Conversation payante → résumeur payant (route inchangée)."""
    ep_in = "https://opencode.ai/zen/go/v1/messages"
    m, ep, proto, is_free = oc._compaction_summarizer_plan(
        model_id, None, endpoint=ep_in, protocol="anthropic"
    )[:4]
    assert is_free is False, f"{model_id!r} ne devrait pas être classé free"
    assert m == model_id
    assert ep == ep_in
    assert proto == "anthropic"


def test_override_wins_over_conversation_class():
    """summarizer_model_override posé reste souverain."""
    m, ep, proto, is_free = oc._compaction_summarizer_plan(
        "mimo-v2.5", "gpt-6-luna", endpoint="https://opencode.ai/zen/go/v1/chat/completions", protocol="openai"
    )[:4]
    assert m == "gpt-6-luna"
    assert is_free is False, "un override payant doit gagner sur une conversation free"


def test_override_to_free_model_stays_free():
    """Un override pointant un modèle mappé free reste servi par la jambe free."""
    m, ep, proto, is_free = oc._compaction_summarizer_plan(
        "gpt-6-luna", "mimo-v2.5", endpoint="https://opencode.ai/zen/go/v1/chat/completions", protocol="openai"
    )[:4]
    assert is_free is True
    assert "/zen/go/" not in (ep or "")


def test_blank_override_ignored():
    """Override vide/espaces = absent (pas de route parasite)."""
    m, ep, proto, is_free = oc._compaction_summarizer_plan(
        "mimo-v2.5", "   ", endpoint="https://opencode.ai/zen/go/v1/chat/completions", protocol="openai"
    )[:4]
    assert is_free is True
    assert m == "mimo-v2.5-free"


def test_plan_is_pure_and_never_raises():
    """Le plan ne lève jamais, même sur entrées hostiles (fail-open)."""
    for bad in ("", None, 42, "inconnu-xyz", "-free"):
        m, ep, proto, is_free = oc._compaction_summarizer_plan(bad, None, endpoint="", protocol="openai")[:4]
        assert isinstance(is_free, bool)


def test_free_request_helper_never_calls_paid_helper(monkeypatch):
    """is_free=True → pool free (_try_free_model_first), JAMAIS _do_request_with_retry.

    [Voie unifiée] Le résumeur d'une conversation free emprunte la même
    machinerie pool (stations/VPN/hedge) que la conversation : ni le helper
    payant, ni le direct résidentiel ne doivent être appelés.
    """
    calls = {"pool": 0, "paid": 0, "direct": 0}

    async def _fake_pool(body, headers, protocol, model_id, forced_pool=None, req_id=None):
        calls["pool"] += 1
        assert model_id == "mimo-v2.5", "le pool doit résoudre via le seed (même route que la conversation)"
        return ("FREERESP", headers, model_id, "9.9.9.9")

    async def _fake_paid(*a, **k):
        calls["paid"] += 1
        return ("PAIDRESP", {})

    async def _fake_direct(*a, **k):
        calls["direct"] += 1
        return ("DIRECTRESP", {})

    monkeypatch.setattr(oc, "_try_free_model_first", _fake_pool)
    monkeypatch.setattr(oc, "_do_request_with_retry", _fake_paid)
    monkeypatch.setattr(oc, "_do_free_direct_request", _fake_direct)

    import asyncio

    out = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        oc._compaction_summarizer_request(True, "https://opencode.ai/zen/v1/chat/completions", {"model": "mimo-v2.5-free", "messages": []}, {}, "openai", seed="mimo-v2.5")
    )
    assert out[0] == "FREERESP"
    assert calls["pool"] == 1
    assert calls["paid"] == 0, "la jambe free ne doit jamais emprunter le helper payant"
    assert calls["direct"] == 0, "la jambe free passe par le pool (VPN/stations), pas le direct résidentiel"


def test_free_pool_exhaustion_is_fail_open_never_paid(monkeypatch):
    """Pool épuisé (None) → UpstreamError (fail-open), toujours pas de payant."""
    import asyncio

    async def _fake_pool_none(*a, **k):
        return None

    async def _fake_paid(*a, **k):
        raise AssertionError("le repli payant silencieux est interdit")

    monkeypatch.setattr(oc, "_try_free_model_first", _fake_pool_none)
    monkeypatch.setattr(oc, "_do_request_with_retry", _fake_paid)

    try:
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            oc._compaction_summarizer_request(True, "https://opencode.ai/zen/v1/chat/completions", {"model": "m", "messages": []}, {}, "openai", seed="mimo-v2.5")
        )
    except oc.UpstreamError:
        pass
    else:
        raise AssertionError("pool épuisé → UpstreamError attendu (run_summarizer le convertit en None)")


def test_summarizer_never_targets_systemone():
    """Un résumeur est un corps chat : jamais l'endpoint systemone (typé)."""
    m, ep, proto, is_free = oc._compaction_summarizer_plan(
        "jev-1.13-free", None, endpoint="", protocol="openai"
    )[:4]
    if is_free:
        assert "/systemone" not in (ep or ""), f"corps chat envoyé sur systemone: {ep!r}"
    else:
        # pas de cible chat connue : on reste fail-open sur la route d'origine
        assert "/systemone" not in (m or ""), "cible systemone conservée à tort"


def test_responses_payload_normalized_to_chat():
    """Une réponse Responses est ramenée en choices (lisible par _extract_text)."""
    import asyncio
    import json

    resp = oc.httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=json.dumps(
            {
                "id": "resp_1",
                "object": "response",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "RESUME-OK"}],
                    }
                ],
            }
        ).encode(),
    )
    out = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        oc._compaction_normalize_response(resp, "https://opencode.ai/zen/v1/responses", {"model": "mimo-v2.5-free"})
    )
    data = json.loads(out.content)
    assert data["choices"][0]["message"]["content"] == "RESUME-OK"


def test_chat_payload_left_untouched():
    """Un endpoint chat n'est jamais reconverti (pas de perte de forme)."""
    import asyncio

    resp = oc.httpx.Response(200, headers={"content-type": "application/json"}, content=b'{"choices":[]}')
    out = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        oc._compaction_normalize_response(resp, "https://opencode.ai/zen/v1/chat/completions", {})
    )
    assert out is resp


def test_normalize_never_raises_on_garbage():
    """Entrée illisible → réponse d'origine, jamais d'exception (fail-open)."""
    import asyncio

    resp = oc.httpx.Response(200, headers={"content-type": "application/json"}, content=b"not-json")
    out = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        oc._compaction_normalize_response(resp, "https://opencode.ai/zen/v1/responses", {})
    )
    assert out is resp


# ── Gate « jambe free » unifié (bug 403 FreeTierError) ──────────────────────
# ``FREE_MODEL_MAP`` est indexée par noms PAYANTS : un id déjà free absent des
# CLÉS sautait la jambe free et partait sur la machinerie payante, d'où un
# ``FreeTierError`` « free tier can only be used from within OpenCode » (403)
# alors qu'aucune clé payante n'est requise pour ce modèle.

ORPHAN_FREE_IDS = [
    "nemotron-3-ultra-free",
    "mimo-v2.6-flash-free",
    "deepseek-v4-flash-free",
    "mimo-v2.5-free",
    "muse-spark-1.3-contributor-free",
]


@pytest.mark.parametrize("model_id", ORPHAN_FREE_IDS)
def test_free_ids_always_have_free_leg(model_id):
    """Un id ``-free`` a TOUJOURS une jambe free, même absent des clés de la table."""
    assert oc._has_free_leg(model_id) is True, f"{model_id!r} privé de jambe free"
    assert oc._compaction_is_free_class(model_id) is True


@pytest.mark.parametrize("model_id", ["gpt-6-luna", "claude-opus-4-1", "", None])
def test_paid_ids_have_no_free_leg(model_id):
    """Un modèle payant (ou vide) n'a pas de jambe free : classe payante intacte."""
    assert oc._has_free_leg(model_id) is False
    assert oc._compaction_is_free_class(model_id) is False


def test_predicate_never_raises():
    """Prédicat pur, jamais d'exception sur entrée hostile."""
    for bad in (42, "-free", "inconnu-xyz", "  "):
        assert isinstance(oc._has_free_leg(bad), bool)


@pytest.mark.parametrize("model_id", ORPHAN_FREE_IDS)
def test_orphan_free_ids_resolve_to_themselves(model_id):
    """Un id déjà free est SA PROPRE cible free.

    Sans ce repli, ``_resolve_free_model`` renvoyait None → la jambe free était
    sautée pour ces modèles et ils partaient sur la machinerie payante (403
    ``FreeTierError``) alors qu'ils n'exigent aucune clé.
    """
    assert oc._resolve_free_model(model_id) == model_id


@pytest.mark.parametrize("model_id", ["gpt-6-luna", "claude-opus-4-1", "", None])
def test_paid_ids_still_resolve_to_none(model_id):
    """Un modèle payant n'a AUCUNE cible free : la jambe payante reste intacte."""
    assert oc._resolve_free_model(model_id) is None


def test_mapped_paid_names_keep_their_destination():
    """Les noms payants mappés gardent leur destination déclarée (non régressé)."""
    assert oc._resolve_free_model("mimo-v2.5") == "mimo-v2.5-free"
    # deepseek-v4-flash-free mort côté amont (0 succès) : remappé sain.
    assert oc._resolve_free_model("glm-5.1") == "mimo-v2.5-free"
    assert oc._resolve_free_model("deepseek-v4-flash") == "mimo-v2.5-free"