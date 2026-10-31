"""Tests HTTP du tier Gratuit/Payant : /v1/models et en-têtes par appel.

Aucun réseau : le catalogue vivant est semé en mémoire et ``get_model_config``
est remplacé par une route explicite. Les cas reproduisent les décisions
mesurées sur l'instance locale (``big-pickle`` free sans suffixe, mapping
``glm-5.1`` ignoré en homonym, repli ``mimo-v2.6-flash``).
"""

from __future__ import annotations

import asyncio

import pytest

import config.live_models as lm
import opencode as oc

FREE = "https://opencode.ai/inference/openai/v1/chat/completions"
PAID = "https://opencode.ai/inference/go/openai/v1/chat/completions"


def _catalog(**entries) -> dict:
    return {
        provider: {"models": models}
        for provider, models in (
            (lm.PAID_LEG, entries.get("paid", {})),
            (lm.FREE_LEG, entries.get("free", {})),
        )
    }


@pytest.fixture(autouse=True)
def _isolated_live_state():
    lm.reset_state()
    yield
    lm.reset_state()


def _seed_tier_models(monkeypatch):
    local_models = {
        "free-one-free": {"protocol": "openai"},
        "big-pickle": {"protocol": "openai", "source": "free_verified"},
        "glm-5.1": {"protocol": "openai"},
        "mimo-v2.6-flash": {"protocol": "openai"},
    }
    free_map = {
        "mimo-v2.6-flash": "mimo-v2.6-flash-free",
        "glm-5.1": "glm-5.3-flash-free",
    }
    snapshot = lm.build_snapshot(
        live_by_leg={
            lm.PAID_LEG: {"glm-5.1", "mimo-v2.6-flash"},
            lm.FREE_LEG: {"free-one-free", "big-pickle"},
        },
        current_models=dict(local_models),
        catalog=_catalog(
            paid={"glm-5.1": {}, "mimo-v2.6-flash": {}},
            free={"free-one-free": {}, "big-pickle": {}},
        ),
        fetched_legs={lm.PAID_LEG, lm.FREE_LEG},
    )
    applied = lm.apply_snapshot(
        snapshot,
        models=local_models,
        free_models=set(),
        free_model_map={},
        free_model_pool=[],
        route_fn=lambda mid, entry: {"endpoint": PAID, "protocol": "openai"},
    )
    lm.commit_snapshot(snapshot, applied, source="test", model_ids=set(local_models))

    monkeypatch.setattr(oc, "MODELS", local_models)
    monkeypatch.setattr(oc, "FREE_MODEL_MAP", free_map)
    monkeypatch.setattr(oc, "_route_for", lambda mid: {"model": mid})
    monkeypatch.setattr(oc._cfg_settings, "FREE_MODELS", {"big-pickle"})
    monkeypatch.setattr(
        oc._cfg_settings, "get_free_substitution_policy", lambda: "homonym"
    )
    monkeypatch.setattr(
        oc,
        "get_model_config",
        lambda mid: {
            "endpoint": FREE
            if mid in ("free-one-free", "big-pickle")
            else PAID,
            "protocol": "openai",
            "source": "free_verified" if mid == "big-pickle" else None,
        },
    )
    return local_models


def test_models_endpoint_exposes_tier_and_counts(monkeypatch):
    _seed_tier_models(monkeypatch)

    payload = asyncio.run(oc.list_models())
    rows = {row["id"]: row for row in payload["data"]}

    assert rows["big-pickle"]["tier"] == "free"
    assert rows["big-pickle"]["free"] is True
    assert rows["big-pickle"]["proxy"]["tier_source"] == "discovery"
    assert rows["big-pickle"]["proxy"]["route_leg"] == "free"

    assert rows["mimo-v2.6-flash"]["tier"] == "paid"
    assert rows["mimo-v2.6-flash"]["free"] is False
    assert rows["mimo-v2.6-flash"]["proxy"]["tier_source"] == "mapped"
    assert rows["mimo-v2.6-flash"]["proxy"]["free_target"] == "mimo-v2.6-flash-free"
    assert rows["mimo-v2.6-flash"]["proxy"]["free_first"] is True

    assert rows["glm-5.1"]["tier"] == "paid"
    assert rows["glm-5.1"]["proxy"]["free_first"] is True
    assert rows["glm-5.1"]["proxy"]["free_mapping"] == "glm-5.3-flash-free"
    assert rows["glm-5.1"]["proxy"]["free_mapping_active"] is True
    assert rows["glm-5.1"]["proxy"]["free_target"] == "glm-5.3-flash-free"

    counts = payload["counts"]
    assert counts["total"] == 4
    assert counts["free"] == 2
    assert counts["paid"] == 2
    assert counts["paid_free_fallback"] == 2
    assert counts["paid_free_ignored"] == 0


def test_models_endpoint_filters_tier_before_pagination(monkeypatch):
    _seed_tier_models(monkeypatch)

    paid = asyncio.run(oc.list_models(tier="paid"))
    assert {row["id"] for row in paid["data"]} == {"glm-5.1", "mimo-v2.6-flash"}
    assert paid["counts"]["total"] == 2
    assert paid["counts"]["paid"] == 2

    free = asyncio.run(oc.list_models(tier="free", limit=1))
    assert len(free["data"]) == 1
    assert free["data"][0]["tier"] == "free"
    assert free["counts"]["total"] == 2
    assert free["has_more"] is True

    fallback = asyncio.run(oc.list_models(tier="paid", free_first=True))
    assert {row["id"] for row in fallback["data"]} == {"mimo-v2.6-flash", "glm-5.1"}


def test_models_endpoint_keeps_legacy_proxy_leg(monkeypatch):
    _seed_tier_models(monkeypatch)
    payload = asyncio.run(oc.list_models())
    rows = {row["id"]: row for row in payload["data"]}
    assert rows["free-one-free"]["proxy"]["leg"] == lm.FREE_LEG
    assert rows["glm-5.1"]["proxy"]["leg"] == lm.PAID_LEG
    assert rows["free-one-free"]["proxy"]["leg"] != rows["free-one-free"]["proxy"]["tier"]


def test_models_endpoint_exposes_route_alias(monkeypatch):
    _seed_tier_models(monkeypatch)
    monkeypatch.setattr(
        oc,
        "_route_for",
        lambda mid: {"model": "big-pickle"} if mid == "glm-5.1" else {"model": mid},
    )

    payload = asyncio.run(oc.list_models())
    row = {r["id"]: r for r in payload["data"]}["glm-5.1"]
    assert row["tier"] == "paid"
    assert row["free"] is False
    assert row["proxy"]["tier_source"] == "route"
    assert row["proxy"]["route_target"] == "big-pickle"
    assert row["proxy"]["free_target"] == "big-pickle"
    assert row["proxy"]["route_leg"] == "paid"


def test_models_endpoint_exposes_auto_probe_verdict(monkeypatch):
    _seed_tier_models(monkeypatch)
    monkeypatch.setattr(
        oc._cfg_settings,
        "_FREE_DISCOVERY_STATE",
        {"probe": {"results": {"big-pickle": {"verdict": "limited", "status": 429}}}},
    )

    payload = asyncio.run(oc.list_models())
    row = {r["id"]: r for r in payload["data"]}["big-pickle"]
    assert row["proxy"]["probe"]["verdict"] == "limited"
    assert row["proxy"]["probe"]["status"] == 429


def test_tier_header_middleware_injects_and_replaces(monkeypatch):
    sent: list[dict] = []

    async def _app(scope, receive, send):
        oc._current_requested_model.set("mimo-v2.6-flash")
        oc._current_requested_tier.set("paid")
        oc._current_actual_model.set("mimo-v2.6-flash-free")
        oc._current_actual_tier.set("free")
        oc._current_fallback.set("paid_to_free")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"x-opencode-proxy-tier", b"stale")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    middleware = oc.ModelTierHeaderMiddleware(_app)
    scope = {"type": "http", "path": "/v1/chat/completions"}

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        sent.append(message)

    asyncio.run(middleware(scope, _receive, _send))

    headers = [(name.lower(), value) for name, value in sent[0]["headers"]]
    assert headers.count((b"x-opencode-proxy-tier", b"free")) == 1
    assert (b"x-opencode-proxy-requested-tier", b"paid") in headers
    assert (b"x-opencode-proxy-model", b"mimo-v2.6-flash-free") in headers
    assert (b"x-opencode-proxy-requested-model", b"mimo-v2.6-flash") in headers
    assert (b"x-opencode-proxy-fallback", b"paid_to_free") in headers
    assert all(name != b"x-opencode-proxy-tier" or value == b"free" for name, value in headers)
    assert sent[-1]["type"] == "http.response.body"


def test_tier_header_middleware_skips_models_list():
    sent: list[dict] = []

    async def _app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    middleware = oc.ModelTierHeaderMiddleware(_app)
    scope = {"type": "http", "path": "/v1/models"}

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        sent.append(message)

    asyncio.run(middleware(scope, _receive, _send))
    assert sent[0]["headers"] == []


def test_stream_actual_marker_only_on_200():
    class Resp:
        def __init__(self, status_code):
            self.status_code = status_code

    oc._current_requested_tier.set("paid")
    oc._current_actual_tier.set(None)
    oc._current_actual_model.set(None)
    oc._current_fallback.set(None)

    oc._mark_actual_stream(True, {"model": "free-x"}, Resp(429))
    assert oc._current_actual_tier.get() is None
    assert oc._current_fallback.get() is None

    oc._mark_actual_stream(True, {"model": "free-x"}, Resp(200))
    assert oc._current_actual_tier.get() == "free"
    assert oc._current_actual_model.get() == "free-x"
    assert oc._current_fallback.get() == "paid_to_free"

    oc._mark_actual_stream(False, {"model": "paid-x"}, Resp(200))
    assert oc._current_actual_tier.get() == "paid"
    assert oc._current_actual_model.get() == "paid-x"
    assert oc._current_fallback.get() == "paid_to_free"


def test_stream_start_preannounces_free_before_headers(monkeypatch):
    monkeypatch.setattr(oc, "_resolve_free_model", lambda model: "free-x")
    oc._current_actual_tier.set(None)
    oc._current_actual_model.set(None)
    oc._current_fallback.set(None)

    oc._mark_stream_start("paid-x")
    assert oc._current_requested_model.get() == "paid-x"
    assert oc._current_requested_tier.get() == "paid"
    assert oc._current_actual_tier.get() == "free"
    assert oc._current_actual_model.get() == "free-x"
    assert oc._current_fallback.get() == "paid_to_free"


def test_requested_model_is_the_client_alias_not_the_route_target():
    oc._current_requested_model.set(None)
    oc._current_requested_tier.set("paid")

    oc._mark_requested_model("client-alias")
    oc._mark_requested_model("routed-target")
    assert oc._current_requested_model.get() == "client-alias"
    assert oc._current_requested_tier.get() == "paid"


def test_tier_wrapper_resolves_route_alias(monkeypatch):
    monkeypatch.setattr(
        oc,
        "_route_for",
        lambda mid: {"model": "free-target-free"} if mid == "paid-alias" else {"model": mid},
    )
    monkeypatch.setattr(
        oc,
        "get_model_config",
        lambda mid: {
            "endpoint": FREE if str(mid).endswith("-free") else PAID,
            "protocol": "openai",
        },
    )
    monkeypatch.setattr(oc, "_live_model_entry", lambda mid: None)
    monkeypatch.setattr(oc._cfg_settings, "FREE_MODELS", set())
    monkeypatch.setattr(oc, "FREE_MODEL_MAP", {})

    verdict = oc._model_tier_for("paid-alias")
    assert verdict["tier"] == "paid"
    assert verdict["tier_source"] == "route"
    assert verdict["route_target"] == "free-target-free"
    assert verdict["free_target"] == "free-target-free"
    assert verdict["route_leg"] == "paid"
