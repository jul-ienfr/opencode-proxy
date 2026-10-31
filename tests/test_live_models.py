"""Tests du catalogue vivant : ajout/retrait automatique + capacités d'effort.

Aucun réseau : les snapshots sont construits depuis un catalogue synthétique,
puis appliqués aux dictionnaires locaux. Les tests d'API utilisent le même
état, sans démarrer le lifespan ni contacter les jambes opencode.
"""

from __future__ import annotations

import asyncio

import pytest

import config.live_models as lm
from config import effort_caps, effort_policy, reasoning_options

FREE = lm.FREE_LEG
PAID = lm.PAID_LEG


def test_effective_model_limits_free_first_uses_target(monkeypatch):
    """La fenêtre exposée est le min demandé/free quand la route est free-first."""
    import opencode as oc

    def _entry(model):
        if model == "free-target":
            return {"metadata": {"limit": {"context": 200000, "output": 32000}}}
        return {"metadata": {"limit": {"context": 1000000, "output": 128000}}}

    monkeypatch.setattr(oc, "_live_model_entry", _entry)
    tier = {"free_first": True, "free_target": "free-target"}
    assert oc._effective_model_limits("paid-alias", tier, {"context": 1000000, "output": 128000}) == (
        200000,
        32000,
    )
    # Pas de free-first : la limite du modèle demandé reste souveraine.
    tier2 = {"free_first": False, "free_target": "free-target"}
    assert oc._effective_model_limits("paid-alias", tier2, {"context": 1000000, "output": 128000}) == (
        1000000,
        128000,
    )
    # Free direct : sa propre limite.
    assert oc._effective_model_limits("free-target", {"free_first": True, "free_target": None}, {"context": 200000}) == (
        200000,
        None,
    )


def test_model_capabilities_context_management_native():
    """context_management = true seulement pour Anthropic/Responses natifs."""
    import opencode as oc

    native_anth = {"protocol": "anthropic", "endpoint": "https://x/anthropic/v1/messages"}
    native_resp = {"protocol": "openai", "endpoint": "https://x/v1/responses"}
    chat = {"protocol": "openai", "endpoint": "https://x/v1/chat/completions"}
    gem = {"protocol": "gemini", "endpoint": "https://x/v1beta/models"}
    assert oc._model_capabilities("m", native_anth, None)["context_management"]["supported"] is True
    assert oc._model_capabilities("m", native_resp, None)["context_management"]["supported"] is True
    assert oc._model_capabilities("m", chat, None)["context_management"]["supported"] is False
    assert oc._model_capabilities("m", gem, None)["context_management"]["supported"] is False


def _catalog(**entries) -> dict:
    return {
        provider: {"models": models}
        for provider, models in (
            (PAID, entries.get("paid", {})),
            (FREE, entries.get("free", {})),
        )
    }


@pytest.fixture(autouse=True)
def _isolated_live_state():
    lm.reset_state()
    yield
    lm.reset_state()


def _seed(
    *,
    paid: set[str],
    free: set[str],
    catalog: dict | None = None,
    models: dict | None = None,
    current_legs: dict | None = None,
) -> tuple[dict, dict]:
    local_models = models if models is not None else {}
    snapshot = lm.build_snapshot(
        live_by_leg={PAID: set(paid), FREE: set(free)},
        current_models=dict(local_models),
        current_legs=current_legs or {},
        catalog=catalog or _catalog(),
        fetched_legs={PAID, FREE},
        source="test",
    )
    applied = lm.apply_snapshot(
        snapshot,
        models=local_models,
        free_models=set(),
        free_model_map={},
        free_model_pool=[],
        route_fn=lambda mid, entry: {
            "endpoint": f"https://{entry['leg']}.test/v1/chat/completions",
            "protocol": "openai",
        },
    )
    lm.commit_snapshot(snapshot, applied, source="test", model_ids=set(local_models))
    return snapshot, applied


# ─────────── Ajout / retrait / migration ───────────


def test_new_live_model_is_added_and_deprecated_is_removed():
    catalog = _catalog(
        paid={"new": {}, "old": {"status": "deprecated"}},
        free={"old": {"status": "deprecated"}},
    )
    models = {"old": {"protocol": "openai"}, "dead": {"protocol": "openai"}}
    snapshot, applied = _seed(paid={"new", "old"}, free={"old"}, catalog=catalog, models=models)
    assert "new" in applied["added"]
    assert set(applied["removed"]) == {"old", "dead"}
    assert set(snapshot["removed_deprecated"]) == {"old"}
    assert set(snapshot["removed_unavailable"]) == {"dead"}
    assert "new" in models and "old" not in models and "dead" not in models


def test_free_leg_model_is_a_candidate_until_the_probe_promotes_it():
    models: dict = {}
    free_models: set[str] = set()
    snapshot = lm.build_snapshot(
        live_by_leg={PAID: set(), FREE: {"future-nonsuffix"}},
        current_models=dict(models),
        catalog=_catalog(free={"future-nonsuffix": {}}),
        fetched_legs={PAID, FREE},
    )
    applied = lm.apply_snapshot(
        snapshot,
        models=models,
        free_models=free_models,
        free_model_map={},
        free_model_pool=[],
        route_fn=lambda _mid, _entry: {"endpoint": "https://free.test", "protocol": "openai"},
    )
    assert "future-nonsuffix" in models
    assert "future-nonsuffix" in applied["free_candidates"]
    assert "future-nonsuffix" not in free_models

    free_models.add("future-nonsuffix")
    applied2 = lm.apply_snapshot(
        snapshot,
        models=models,
        free_models=free_models,
        free_model_map={},
        free_model_pool=[],
        route_fn=lambda _mid, _entry: {"endpoint": "https://free.test", "protocol": "openai"},
    )
    assert applied2["free_candidates"] == []


def test_new_suffix_is_free_immediately_but_still_probed_once():
    models: dict = {}
    free_models: set[str] = set()
    snapshot = lm.build_snapshot(
        live_by_leg={PAID: set(), FREE: {"future-suffix-free"}},
        current_models=dict(models),
        catalog=_catalog(free={"future-suffix-free": {}}),
        fetched_legs={PAID, FREE},
    )
    applied = lm.apply_snapshot(
        snapshot,
        models=models,
        free_models=free_models,
        free_model_map={},
        free_model_pool=[],
        route_fn=lambda _mid, _entry: {"endpoint": "https://free.test", "protocol": "openai"},
    )

    assert "future-suffix-free" in free_models
    assert "future-suffix-free" in applied["free_candidates"]


def test_deprecated_on_one_leg_migrates_to_the_active_leg():
    catalog = _catalog(
        paid={"moved": {"status": "deprecated"}},
        free={"moved": {}},
    )
    models = {"moved": {"protocol": "openai", "endpoint": "https://paid.test"}}
    snapshot, applied = _seed(
        paid={"moved"},
        free={"moved"},
        catalog=catalog,
        models=models,
        current_legs={"moved": PAID},
    )
    assert "moved" in applied["migrated"]
    assert snapshot["available"]["moved"]["leg"] == FREE


def test_commit_does_not_replay_the_same_catalog_on_restart():
    catalog = _catalog(paid={"aa": {}})
    models = {"aa": {}}
    snapshot = lm.build_snapshot(
        live_by_leg={PAID: {"aa"}, FREE: set()},
        current_models=dict(models),
        catalog=catalog,
        fetched_legs={PAID, FREE},
    )
    applied = lm.apply_snapshot(
        snapshot,
        models=models,
        route_fn=lambda _mid, _entry: {"endpoint": "https://x", "protocol": "openai"},
    )
    first = lm.commit_snapshot(snapshot, applied, model_ids=set(models))
    assert first["changed"] is False  # premier relevé : pas de pop-up
    persisted = lm.persisted()

    # Redémarrage simulé : même inventaire, aucun événement ne doit rejouer.
    lm.reset_state()
    lm.load_persisted(available_ids=persisted["available_ids"], revision=0)
    second = lm.commit_snapshot(snapshot, applied, model_ids=set(models))
    assert second["changed"] is False
    assert second["event"] is None

    # Un vrai ajout doit, lui, produire un événement.
    snapshot2 = lm.build_snapshot(
        live_by_leg={PAID: {"aa", "bb"}, FREE: set()},
        current_models=dict(models),
        catalog=_catalog(paid={"aa": {}, "bb": {}}),
        fetched_legs={PAID, FREE},
    )
    applied2 = lm.apply_snapshot(
        snapshot2,
        models=models,
        route_fn=lambda _mid, _entry: {"endpoint": "https://x", "protocol": "openai"},
    )
    third = lm.commit_snapshot(snapshot2, applied2, model_ids=set(models))
    assert third["changed"] is True
    assert "bb" in third["event"]["added"]


def test_partial_fetch_never_removes_a_model():
    catalog = _catalog(paid={"kept": {}})
    snapshot = lm.build_snapshot(
        live_by_leg={FREE: set()},
        current_models={"kept": {"protocol": "openai"}},
        catalog=catalog,
        fetched_legs={FREE},
        previous_by_leg={PAID: {"kept"}, FREE: set()},
    )
    assert snapshot["partial"] is True
    assert snapshot["removed_ids"] == []
    assert "kept" in snapshot["available"]


def test_truncated_catalog_does_not_remove_everything():
    current = {f"m{i}": {"protocol": "openai"} for i in range(20)}
    snapshot = lm.build_snapshot(
        live_by_leg={PAID: {"m0"}, FREE: set()},
        current_models=current,
        catalog=_catalog(paid={"m0": {}}),
        fetched_legs={PAID, FREE},
    )
    assert snapshot["guarded"] is True
    assert snapshot["partial"] is True
    assert snapshot["removed_ids"] == []
    assert len(snapshot["available"]) == 20


def test_internal_model_is_never_removed():
    snapshot = lm.build_snapshot(
        live_by_leg={PAID: set(), FREE: set()},
        current_models={"jev-1.13": {"endpoint": "https://interne.test/v1"}},
        catalog=_catalog(),
        fetched_legs={PAID, FREE},
        is_internal_fn=lambda _mid, _cfg: True,
    )
    assert snapshot["removed_ids"] == []
    assert snapshot["kept_internal"] == ["jev-1.13"]


def test_purged_and_ignored_ids_are_not_added():
    snapshot = lm.build_snapshot(
        live_by_leg={FREE: {"test", "dead", "ok"}},
        current_models={},
        catalog=_catalog(free={"test": {}, "dead": {}, "ok": {}}),
        fetched_legs={FREE},
        purged_ids={"dead"},
        ignored_ids={"test"},
    )
    assert snapshot["added_ids"] == ["ok"]


# ─────────── Effort : max seulement si réellement autorisé ───────────


def test_effort_levels_come_from_the_catalog():
    catalog = _catalog(
        paid={
            "maxi": {"reasoning_options": [{"type": "effort", "values": ["low", "high", "max"]}]},
            "highi": {"reasoning_options": [{"type": "effort", "values": ["low", "high"]}]},
            "toggle": {"reasoning_options": [{"type": "toggle"}]},
        }
    )
    _seed(paid={"maxi", "highi", "toggle"}, free=set(), catalog=catalog)
    assert lm.effort_levels("maxi") == ["low", "high", "max"]
    assert lm.max_effort("maxi") == "max"
    assert lm.max_effort("highi") == "high"
    assert lm.effort_levels("toggle") == []
    assert lm.effort_supported("toggle") is False
    assert lm.effort_supported("inconnu") is None


def test_effort_caps_prefers_live_catalog_over_manual_table():
    catalog = _catalog(
        paid={
            "highi": {"reasoning_options": [{"type": "effort", "values": ["low", "high"]}]},
            "maxi": {"reasoning_options": [{"type": "effort", "values": ["low", "max"]}]},
        }
    )
    _seed(paid={"highi", "maxi"}, free=set(), catalog=catalog)
    # "highi" n'est pas dans la table manuelle, le défaut serait high ;
    # "maxi" prouve que le catalogue peut aussi REMONTER au-delà du défaut.
    assert effort_caps.get_max_effort_for_model("highi") == "high"
    assert effort_caps.get_max_effort_for_model("maxi") == "max"
    assert effort_caps.get_max_effort_for_model("inconnu-zzz") == "high"


def test_allow_list_uses_live_catalog_even_when_flag_is_false(monkeypatch):
    monkeypatch.setattr(reasoning_options, "allow_list_enabled", lambda: False)
    catalog = _catalog(paid={"highi": {"reasoning_options": [{"type": "effort", "values": ["low", "high"]}]}})
    _seed(paid={"highi"}, free=set(), catalog=catalog)
    assert reasoning_options.get_allowed_variants("highi") == ["low", "high"]
    assert reasoning_options.clamp_effort_to_allow_list("max", "highi") == "high"


def test_model_without_effort_option_does_not_emit_reasoning_effort():
    catalog = _catalog(paid={"toggle": {"reasoning_options": [{"type": "toggle"}]}})
    _seed(paid={"toggle"}, free=set(), catalog=catalog)
    assert lm.effort_supported("toggle") is False
    decision = effort_policy.resolve_effort({"reasoning_effort": "high"}, "toggle")
    assert decision.wants is False
    assert decision.level is None


# ─────────── Route dérivée du catalogue ───────────


def test_build_route_uses_catalog_family_and_leg():
    entry = {
        "leg": FREE,
        "metadata": {
            "family_api": "messages",
            "base_url": "https://opencode.ai/inference/anthropic/v1",
        },
    }
    route = lm.build_route(
        "claude-free",
        entry,
        api_base_free="https://free.test/chat/completions",
        api_base_openai="https://paid.test/chat/completions",
        api_base_anthropic="https://paid.test/messages",
        responses_free="https://free.test/responses",
        responses_paid="https://paid.test/responses",
        resolve_protocol_fn=lambda _m: "openai",
    )
    assert route == {
        "endpoint": "https://opencode.ai/inference/anthropic/v1/messages",
        "protocol": "anthropic",
        "api": "messages",
        "source": "live_catalog",
    }


def test_build_route_refuses_gemini_instead_of_inventing_chat():
    entry = {"leg": FREE, "metadata": {"family_api": "gemini"}}
    route = lm.build_route(
        "gemini-x",
        entry,
        api_base_free="https://free.test/chat/completions",
        api_base_openai="https://paid.test/chat/completions",
        api_base_anthropic="https://paid.test/messages",
        responses_free="https://free.test/responses",
        responses_paid="https://paid.test/responses",
        resolve_protocol_fn=lambda _m: "openai",
    )
    assert route is None


# ─────────── Notices clients ───────────


def test_notices_are_per_client_and_deduplicated():
    lm.record_deprecation_notices(
        {
            "old": [
                {"client_ip": "1.2.3.4", "account_alias": "main"},
                {"client_ip": "5.6.7.8", "account_alias": ""},
            ]
        }
    )
    assert lm.notice_for("1.2.3.4")["model"] == "old"
    assert lm.notice_for("", "main")["model"] == "old"
    assert lm.notice_for("5.6.7.8")["model"] == "old"
    assert lm.notice_for("9.9.9.9") is None
    summary = lm.notices_summary()
    assert summary == [
        {
            "model": "old",
            "clients": 2,
            "message": lm.notice_message("old"),
        }
    ]
    # Une seconde passe ne duplique pas la notice.
    lm.record_deprecation_notices({"old": [{"client_ip": "1.2.3.4"}]})
    assert lm.notices_summary()[0]["clients"] == 2


# ─────────── API /v1/models : capabilities ───────────


def test_models_endpoint_exposes_effort_levels(monkeypatch):
    import opencode as oc

    catalog = _catalog(
        paid={
            "maxi": {"reasoning_options": [{"type": "effort", "values": ["low", "high", "max"]}]},
            "highi": {"reasoning_options": [{"type": "effort", "values": ["low", "high"]}]},
        }
    )
    local_models = {"maxi": {}, "highi": {}}
    _seed(paid={"maxi", "highi"}, free=set(), catalog=catalog, models=local_models)
    monkeypatch.setattr(oc, "MODELS", local_models)
    monkeypatch.setattr(
        oc,
        "get_model_config",
        lambda mid: {"endpoint": "https://upstream.test/v1", "protocol": "openai"},
    )
    payload = asyncio.run(oc.list_models())
    rows = {row["id"]: row for row in payload["data"]}
    assert rows["maxi"]["capabilities"]["effort"]["max"]["supported"] is True
    assert rows["maxi"]["capabilities"]["effort"]["high"]["supported"] is True
    assert rows["highi"]["capabilities"]["effort"]["max"]["supported"] is False
    assert rows["highi"]["capabilities"]["effort"]["high"]["supported"] is True
    assert rows["maxi"]["type"] == "model"
    assert "max_tokens" in rows["maxi"]


def test_models_endpoint_paginates_and_filters_lifecycle(monkeypatch):
    import opencode as oc

    catalog = _catalog(
        paid={
            "aa": {"reasoning_options": [{"type": "effort", "values": ["high"]}]},
            "bb": {"reasoning_options": [{"type": "effort", "values": ["high"]}]},
            "old": {"status": "deprecated"},
        }
    )
    local_models = {"aa": {}, "bb": {}, "old": {}}
    _seed(paid={"aa", "bb", "old"}, free=set(), catalog=catalog, models=local_models)
    monkeypatch.setattr(oc, "MODELS", local_models)
    monkeypatch.setattr(
        oc,
        "get_model_config",
        lambda mid: {"endpoint": "https://upstream.test/v1", "protocol": "openai"},
    )
    page = asyncio.run(oc.list_models(limit=1))
    assert len(page["data"]) == 1
    assert page["has_more"] is True
    deprecated = asyncio.run(oc.list_models(lifecycle=["deprecated"]))
    assert [row["id"] for row in deprecated["data"]] == ["old"]
    assert deprecated["data"][0]["lifecycle"] == "deprecated"


def test_deprecated_model_gets_an_explicit_410(monkeypatch):
    import opencode as oc

    catalog = _catalog(paid={"old": {"status": "deprecated"}, "new": {}})
    local_models = {"old": {}, "new": {}}
    _seed(paid={"old", "new"}, free=set(), catalog=catalog, models=local_models)
    monkeypatch.setattr(oc, "MODELS", local_models)
    resp = oc._deprecated_model_response("old", "anthropic")
    assert resp is not None
    assert resp.status_code == 410
    assert b"deprecated" in resp.body.lower()
    assert oc._deprecated_model_response("new", "anthropic") is None


def test_notice_middleware_adds_headers_without_buffering():
    import opencode as oc

    lm.record_deprecation_notices({"old": [{"client_ip": "1.2.3.4", "account_alias": ""}]})
    sent: list[dict] = []

    async def _app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = oc.ModelDeprecationNoticeMiddleware(_app)
    scope = {"type": "http", "path": "/v1/messages", "client": ("1.2.3.4", 1234)}

    async def _run():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await middleware(scope, receive, send)

    asyncio.run(_run())
    headers = dict(sent[0].get("headers") or [])
    assert headers[b"x-opencode-model-deprecated"] == b"old"
    assert b"x-opencode-notice" in headers
    assert sent[-1]["type"] == "http.response.body"
