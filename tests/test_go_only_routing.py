"""test_go_only_routing.py — endpoint explicite + exclusion du pool anonyme.

Couvre la mécanique GÉNÉRIQUE (aucun id live — fixtures "acme-*") :
  * _resolve_model_endpoint — override explicite (`go` / `free` / URL)
    prime sur l'heuristique suffixe -free / muse-spark ;
  * get_model_config — le seam utilisé par les handlers /v1/messages ;
  * _apply_discovered_free_models — free_discovery.go_only_ids exclut
    l'id du pool FREE_MODELS / FREE_MODEL_POOL sans altérer son
    entrée MODELS ; sans exclusion il est ajouté comme tout -free.
"""

import config.settings as st


# ── Résolution d'endpoint ──────────────────────────────────────────


class TestResolveModelEndpoint:
    def test_explicit_go(self):
        assert (
            st._resolve_model_endpoint("acme-proto-free", {"endpoint": "go"}, "openai")
            == st.API_BASE_OPENAI
        )

    def test_explicit_go_case_insensitive(self):
        assert (
            st._resolve_model_endpoint("acme-proto-free", {"endpoint": " GO "}, "openai")
            == st.API_BASE_OPENAI
        )

    def test_explicit_free(self):
        assert (
            st._resolve_model_endpoint("some-free", {"endpoint": "free"}, "openai")
            == st.API_BASE_FREE
        )

    def test_explicit_full_url_verbatim(self):
        url = "https://example.com/v1/chat/completions"
        assert st._resolve_model_endpoint("custom-model", {"endpoint": url}, "openai") == url

    def test_free_suffix_default(self):
        assert st._resolve_model_endpoint("acme-thing-free", {}, "openai") == st.API_BASE_FREE

    def test_muse_free_responses(self):
        assert (
            st._resolve_model_endpoint("muse-spark-1.2-contributor-free", {}, "openai")
            == st._RESPONSES_FREE_ENDPOINT
        )

    def test_muse_paid_responses(self):
        assert (
            st._resolve_model_endpoint("muse-spark-1.2-contributor", {}, "openai")
            == st._RESPONSES_ENDPOINT
        )

    def test_muse_13_free_responses(self):
        assert (
            st._resolve_model_endpoint("muse-spark-1.3-contributor-free", {}, "openai")
            == st._RESPONSES_FREE_ENDPOINT
        )

    def test_muse_13_paid_responses(self):
        assert (
            st._resolve_model_endpoint("muse-spark-1.3-contributor", {}, "openai")
            == st._RESPONSES_ENDPOINT
        )

    def test_paid_openai_default(self):
        assert st._resolve_model_endpoint("kimi-k2.6", {}, "openai") == st.API_BASE_OPENAI

    def test_paid_anthropic_default(self):
        assert st._resolve_model_endpoint("minimax-m2.5", {}, "anthropic") == st.API_BASE_ANTHROPIC


# ── Parité muse-spark 1.3 == 1.2 (seam des handlers) ───────────────


def test_live_models_muse_spark_13_endpoints():
    for mid, expected in (
        ("muse-spark-1.3-contributor", st._RESPONSES_ENDPOINT),
        ("muse-spark-1.3-contributor-free", st._RESPONSES_FREE_ENDPOINT),
    ):
        cfg = st.get_model_config(mid)
        assert cfg["endpoint"] == expected
        assert cfg["protocol"] == "openai"


def test_live_models_muse_spark_13_free_map():
    assert st.FREE_MODEL_MAP["muse-spark-1.3-contributor"] == "muse-spark-1.3-contributor-free"


def test_live_models_muse_spark_13_geo():
    for mid in ("muse-spark-1.3-contributor", "muse-spark-1.3-contributor-free"):
        route = st.MODELS[mid]
        res = st.resolve_geo({"model": mid, "geo": route.get("geo", {})})
        ref = st.resolve_geo(
            {
                "model": mid.replace("1.3", "1.2"),
                "geo": st.MODELS[mid.replace("1.3", "1.2")].get("geo", {}),
            }
        )
        assert res["geo_status"] == ref["geo_status"]
        assert res["mode"] == ref["mode"] == "strict"
        assert res["require_vpn"] == ref["require_vpn"]
        assert res["effective_allowed"] == ref["effective_allowed"]


def test_web_search_native_parity_13():
    for mid in (
        "muse-spark-1.2-contributor",
        "muse-spark-1.2-contributor-free",
        "muse-spark-1.3-contributor",
        "muse-spark-1.3-contributor-free",
    ):
        assert mid in st.WEB_SEARCH_NATIVE_MODELS


# ── Exclusion découverte auto (go_only_ids, mécanique générique) ───


def _snapshot_settings():
    return {
        "models": {k: dict(v) for k, v in st.MODELS.items()},
        "free": set(st.FREE_MODELS),
        "pool_obj": st.FREE_MODEL_POOL,
        "map": dict(st.FREE_MODEL_MAP),
        "state": dict(st._FREE_DISCOVERY_STATE),
        "go_only": set(st.GO_ONLY_IDS),
    }


def _restore_settings(snap):
    st.MODELS.clear()
    st.MODELS.update(snap["models"])
    st.FREE_MODELS.clear()
    st.FREE_MODELS.update(snap["free"])
    st.FREE_MODEL_POOL = snap["pool_obj"]
    st.FREE_MODEL_MAP.clear()
    st.FREE_MODEL_MAP.update(snap["map"])
    st._FREE_DISCOVERY_STATE.clear()
    st._FREE_DISCOVERY_STATE.update(snap["state"])
    st.GO_ONLY_IDS.clear()
    st.GO_ONLY_IDS.update(snap["go_only"])


def _install_go_only(monkeypatch, snap, go_only_id, endpoint):
    """Isole le test du config.yaml réel : GO_ONLY_IDS + entrée MODELS
    temporaires, restaurés après."""
    monkeypatch.setattr(st, "GO_ONLY_IDS", {go_only_id})
    st.MODELS[go_only_id] = {"endpoint": endpoint, "protocol": "openai"}
    return go_only_id


def test_discovery_excludes_go_only_ids(monkeypatch):
    snap = _snapshot_settings()
    try:
        go_only = _install_go_only(
            monkeypatch, snap, "acme-proto-free", st.API_BASE_OPENAI
        )
        added = st._apply_discovered_free_models(
            {go_only, "mimo-v2.5-free"}, source="test"
        )
        assert isinstance(added, int)
        assert go_only not in st.FREE_MODELS
        assert go_only not in st.FREE_MODEL_POOL
        assert "mimo-v2.5-free" in st.FREE_MODELS
        assert "mimo-v2.5-free" in st.FREE_MODEL_POOL
        assert st.MODELS[go_only]["endpoint"] == st.API_BASE_OPENAI
        assert st.MODELS["mimo-v2.5-free"]["endpoint"] == st.API_BASE_FREE
        assert not any(v == go_only for v in st.FREE_MODEL_MAP.values())
    finally:
        _restore_settings(snap)


def test_discovery_adds_go_only_id_when_filter_disabled(monkeypatch):
    snap = _snapshot_settings()
    try:
        monkeypatch.setattr(st, "GO_ONLY_IDS", set())
        st._apply_discovered_free_models({"acme-proto-free"}, source="test")
        assert "acme-proto-free" in st.FREE_MODELS
        assert "acme-proto-free" in st.FREE_MODEL_POOL
    finally:
        _restore_settings(snap)


# ── Garde-fou /v1/models : chaque id listé existe dans MODELS ───


def test_list_models_ids_subset_of_models():
    """GET /v1/models ne doit annoncer que des ids routables (MODELS).

    Régression couverte : les 5 alias factices (gpt-5-codex, gpt-5, gpt-4o,
    codex, deepseek-chat) injectés à la main dans list_models() — 404
    upstream — retirés le 2026-09-09.Pattern : appel direct de la coroutine
    (pas de TestClient — opencode.app monte tout le lifespan/VPN)."""
    import asyncio

    import opencode as oc

    payload = asyncio.run(oc.list_models())
    assert payload["object"] == "list"
    ids = [m["id"] for m in payload["data"]]
    assert len(ids) == len(set(ids)), "doublons dans /v1/models"
    ghosts = [i for i in ids if i not in st.MODELS]
    assert not ghosts, f"/v1/models annonce des ids non routables : {ghosts}"
    for banned in ("gpt-5-codex", "gpt-5", "gpt-4o", "codex", "deepseek-chat"):
        assert banned not in ids, f"alias factice {banned!r} de retour dans /v1/models"
