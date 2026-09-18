"""Tests catalogue explicite API + capabilities (parité models.dev, todo 5).

- `api:` explicite > familles > "chat" ;
- endpoint : `endpoint:` suprême, puis `api:` décliné en variante -free ;
- capabilities : explicite fusionné > famille > inconnu (texte seul) ;
- `get_model_config` expose api + capabilities ;
- `mapping.model_supports_modality` délégué au catalogue (single source).
"""

import config.settings as st
from app.protocol import mapping as pm


def test_get_model_api_explicit_wins():
    assert st.get_model_api("muse-spark-1.3", {"api": "chat"}) == "chat"
    assert st.get_model_api("kimi-k2.6", {"api": "responses"}) == "responses"
    # valeur invalide ignorée → repli famille
    assert st.get_model_api("muse-spark-1.3", {"api": "bananas"}) == "responses"
    assert st.get_model_api("kimi-k2.6", {"api": "BANANAS"}) == "chat"


def test_get_model_api_family_fallback():
    assert st.get_model_api("muse-spark-1.3-contributor") == "responses"
    assert st.get_model_api("muse-spark-1.3-contributor-free") == "responses"
    assert st.get_model_api("spark-9-x") == "responses"
    assert st.get_model_api("kimi-k2.6") == "chat"
    assert st.get_model_api("GLM-5.1") == "chat"
    assert st.get_model_api("modele-inconnu-xyz") == "chat"


def test_resolve_endpoint_explicit_supreme():
    assert st._resolve_model_endpoint("m", {"endpoint": "go"}, "openai") == st.API_BASE_OPENAI
    assert st._resolve_model_endpoint("m", {"endpoint": "free"}, "openai") == st.API_BASE_FREE
    assert st._resolve_model_endpoint("m", {"endpoint": "https://x/y"}, "openai") == "https://x/y"


def test_resolve_endpoint_api_driven():
    # responses décliné en variante -free
    assert (
        st._resolve_model_endpoint("muse-spark-1.3-contributor", {"api": "responses"}, "openai")
        == st._RESPONSES_ENDPOINT
    )
    assert (
        st._resolve_model_endpoint("muse-spark-1.3-contributor-free", {"api": "responses"}, "openai")
        == st._RESPONSES_FREE_ENDPOINT
    )
    # chat : -free → base free, sinon base du protocole
    assert st._resolve_model_endpoint("deepseek-v4-flash-free", {"api": "chat"}, "openai") == st.API_BASE_FREE
    assert st._resolve_model_endpoint("qwen3.7-plus", {"api": "chat"}, "anthropic") == st.API_BASE_ANTHROPIC
    assert st._resolve_model_endpoint("kimi-k2.6", {"api": "chat"}, "openai") == st.API_BASE_OPENAI
    # override explicite : api chat sur famille responses
    assert (
        st._resolve_model_endpoint("muse-spark-1.3-contributor", {"api": "chat"}, "openai")
        == st.API_BASE_OPENAI
    )


def test_resolve_endpoint_heuristic_fallback_unchanged():
    # sans api: → heuristique historique (muse/spark → responses)
    assert st._resolve_model_endpoint("muse-spark-9", {}, "openai") == st._RESPONSES_ENDPOINT
    assert st._resolve_model_endpoint("muse-spark-9-free", {}, "openai") == st._RESPONSES_FREE_ENDPOINT
    assert st._resolve_model_endpoint("kimi-k9", {}, "openai") == st.API_BASE_OPENAI
    assert st._resolve_model_endpoint("qwen9", {}, "anthropic") == st.API_BASE_ANTHROPIC


def test_capabilities_family_and_unknown():
    kimi = st.get_model_capabilities("kimi-k2.6")
    assert set(["text", "image", "pdf"]) <= set(kimi["input"])
    assert kimi["toolcall"] is True
    ds = st.get_model_capabilities("deepseek-v4-flash")
    assert ds["interleaved"] == "reasoning_content"
    unk = st.get_model_capabilities("modele-inconnu-xyz")
    assert unk["input"] == ["text"] and unk["reasoning"] is False


def test_capabilities_explicit_merge_and_fresh():
    caps = st.get_model_capabilities("kimi-k2.6", {"capabilities": {"input": ["text"], "audio": True}})
    assert caps["input"] == ["text"]
    assert caps["audio"] is True
    assert caps["toolcall"] is True  # défaut famille conservé
    caps["input"].append("video")  # ne doit pas polluer le registre
    assert "video" not in st.get_model_capabilities("kimi-k2.6")["input"]


def test_models_entries_and_config():
    st.get_model_config.cache_clear()
    try:
        free = st.get_model_config("muse-spark-1.3-contributor-free")
        assert free["api"] == "responses"
        assert free["endpoint"] == st._RESPONSES_FREE_ENDPOINT
        assert set(["text", "image", "pdf"]) <= set(free["capabilities"]["input"])
        chat = st.get_model_config("kimi-k2.6")
        assert chat["api"] == "chat"
        assert chat["endpoint"] == st.API_BASE_OPENAI
    finally:
        st.get_model_config.cache_clear()


def test_config_exports():
    import config

    assert callable(config.get_model_api)
    assert callable(config.get_model_capabilities)
    assert config.MODEL_API_FAMILIES["muse"] == "responses"


def test_modality_delegates_to_catalog():
    assert pm.model_supports_modality("kimi-k2.6", "image") is True
    assert pm.model_supports_modality("kimi-k2.6", "video") is False
    assert pm.model_supports_modality("modele-inconnu-xyz", "image") is False
    assert pm.model_supports_modality("modele-inconnu-xyz", "text") is True
    # cohérence avec le catalogue
    assert pm.model_supports_modality("deepseek-v4-flash", "pdf") == (
        "pdf" in st.get_model_capabilities("deepseek-v4-flash")["input"]
    )
