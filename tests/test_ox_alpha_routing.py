"""test_ox_alpha_routing.py — Ox Alpha : routage Go authentifié de
ox-alpha-free + alias de saisie + exclusion du pool anonyme.

Couvre :
  * _resolve_model_endpoint — override explicite (`go` / `free` / URL)
    prime sur l'heuristique suffixe -free / muse-spark ;
  * MODELS construits depuis config.yaml : ox-alpha-free → endpoint Go
    authentifié, x-preview-f-free → endpoint free anonyme ;
  * get_model_config — le seam utilisé par les handlers /v1/messages ;
  * _apply_discovered_free_models — free_discovery.go_only_ids exclut
    ox-alpha-free de FREE_MODELS / FREE_MODEL_POOL sans altérer son
    entrée MODELS ; sans exclusion il serait ajouté comme tout -free ;
  * _route_for — alias 0xalpha / ox-alpha / oxalpha → ox-alpha-free,
    identité directe non shadowée par le pattern « ox-alpha ».
"""

import config.settings as st
import opencode as oc

# ── Résolution d'endpoint ──────────────────────────────────────────


class TestResolveModelEndpoint:
    def test_explicit_go(self):
        assert (
            st._resolve_model_endpoint("ox-alpha-free", {"endpoint": "go"}, "openai")
            == st.API_BASE_OPENAI
        )

    def test_explicit_go_case_insensitive(self):
        assert (
            st._resolve_model_endpoint("ox-alpha-free", {"endpoint": " GO "}, "openai")
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
        assert st._resolve_model_endpoint("x-preview-f-free", {}, "openai") == st.API_BASE_FREE

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

    def test_paid_openai_default(self):
        assert st._resolve_model_endpoint("kimi-k2.6", {}, "openai") == st.API_BASE_OPENAI

    def test_paid_anthropic_default(self):
        assert st._resolve_model_endpoint("minimax-m2.5", {}, "anthropic") == st.API_BASE_ANTHROPIC


# ── MODELS réels (config.yaml du dépôt) + seam des handlers ────────


def test_live_models_ox_alpha_endpoints():
    assert st.MODELS["ox-alpha-free"]["endpoint"] == st.API_BASE_OPENAI
    assert st.MODELS["ox-alpha-free"]["protocol"] == "openai"
    assert st.MODELS["x-preview-f-free"]["endpoint"] == st.API_BASE_FREE


def test_get_model_config_go_seam():
    cfg = st.get_model_config("ox-alpha-free")
    assert cfg["endpoint"] == st.API_BASE_OPENAI
    assert cfg["protocol"] == "openai"


# ── Exclusion découverte auto (go_only_ids) ─────────────────────────


def _snapshot_settings():
    return {
        "models": {k: dict(v) for k, v in st.MODELS.items()},
        "free": set(st.FREE_MODELS),
        "pool_obj": st.FREE_MODEL_POOL,
        "map": dict(st.FREE_MODEL_MAP),
        "state": dict(st._FREE_DISCOVERY_STATE),
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


def test_discovery_excludes_go_only_ids():
    snap = _snapshot_settings()
    try:
        added = st._apply_discovered_free_models(
            {"ox-alpha-free", "x-preview-f-free"}, source="test"
        )
        assert isinstance(added, int)
        assert "ox-alpha-free" not in st.FREE_MODELS
        assert "ox-alpha-free" not in st.FREE_MODEL_POOL
        assert "x-preview-f-free" in st.FREE_MODELS
        assert "x-preview-f-free" in st.FREE_MODEL_POOL
        assert st.MODELS["ox-alpha-free"]["endpoint"] == st.API_BASE_OPENAI
        assert st.MODELS["x-preview-f-free"]["endpoint"] == st.API_BASE_FREE
        assert not any(v == "ox-alpha-free" for v in st.FREE_MODEL_MAP.values())
    finally:
        _restore_settings(snap)


def test_discovery_adds_go_only_id_when_filter_disabled(monkeypatch):
    monkeypatch.setattr(st, "GO_ONLY_IDS", set())
    snap = _snapshot_settings()
    try:
        st._apply_discovered_free_models({"ox-alpha-free"}, source="test")
        assert "ox-alpha-free" in st.FREE_MODELS
        assert "ox-alpha-free" in st.FREE_MODEL_POOL
    finally:
        _restore_settings(snap)


# ── Alias de saisie (custom route oxalpha) ──────────────────────────


def test_alias_routes_to_ox_alpha_free():
    for name in ("0xalpha", "ox-alpha", "oxalpha"):
        route = oc._route_for(name)
        assert route is not None, f"no route for {name!r}"
        assert route["model"] == "ox-alpha-free", name


def test_alias_case_insensitive():
    route = oc._route_for("0XAlpha")
    assert route is not None
    assert route["model"] == "ox-alpha-free"


def test_direct_identity_not_shadowed_by_alias_pattern():
    route = oc._route_for("ox-alpha-free")
    assert route is not None
    assert route["model"] == "ox-alpha-free"
    cfg = st.get_model_config(route["model"])
    assert cfg["endpoint"] == st.API_BASE_OPENAI
