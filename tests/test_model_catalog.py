"""Tests catalogue explicite API + capabilities (parité models.dev, todo 5).

- `api:` explicite > familles > "chat" ;
- endpoint : `endpoint:` suprême, puis `api:` décliné en variante -free ;
- capabilities : explicite fusionné > famille > inconnu (texte seul) ;
- `get_model_config` expose api + capabilities ;
- `mapping.model_supports_modality` délégué au catalogue (single source) ;
- [PLAN N4] repli d'un id absent de `models:` : passe par la source unique, la
  jambe n'est plus choisie à la main.
"""

import config.settings as st
from app.protocol import mapping as pm
from config import models_catalog as mc
from core.errors import ModelOutOfScope


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
    assert {"text", "image", "pdf"} <= set(kimi["input"])
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
        assert {"text", "image", "pdf"} <= set(free["capabilities"]["input"])
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


# ─────────── [PLAN N4] Repli d'un id du catalogue absent de `models:` ────────
#
# 41 ids que seule la jambe anonyme sert ne sont déclarés nulle part dans
# `config.yaml` (relevé live du 2026-10-11). Un client qui demandait l'un d'eux
# retombait sur `{endpoint: API_BASE_OPENAI}` — la jambe **payée**, en famille
# `chat` quelles que soient les 25 entrées `/responses` — d'où un 404 amont qui
# ne disait jamais pourquoi.
#
# Le repli ne choisit plus la jambe à la main : il repasse par
# `_resolve_model_endpoint` (déjà verrouillé plus haut par
# `test_resolve_endpoint_heuristic_fallback_unchanged`, et §« Comment la jambe
# est décidée » de `docs/models-protocol.md`), la SOURCE UNIQUE qui bâtait déjà
# `MODELS` à l'import. Il reste **local** : ce qu'il lit sur un id, c'est son
# suffixe `-free` et sa famille d'API (`muse-*` / `spark-*` → `responses`). La
# jambe réelle d'un id que son nom ne trahit pas vit dans le catalogue live ;
# à l'exécution elle arrive par la découverte auto (`free_discovery_urls`
# réunit les `/models` des deux jambes, `detect_free_ids` classe sur
# `pricing 0/0` / `free: true`) qui écrit `MODELS[id] = {endpoint: ...}` — un
# `endpoint:` **explicite**, donc gagnant sur le repli. Le repli couvre donc la
# fenêtre d'avant découverte et le offline : c'est exactement la limite que
# `test_repli_id_muet_...` verrouille telle quelle, sans l'arrondir.

_ANON_RESPONSES = "acme-muse-9.9-contributor-free"  # anonyme + famille `responses`
_ANON_CHAT = "acme-longcat-9.9-free"  # anonyme, seul le suffixe parle
_ANON_MUET = "acme-longcat-9.9"  # anonyme, le nom ne dit rien
_TIER = "acme-google-9.9"  # hébergé seulement chez un provider tiers

_ANONYMES = (_ANON_MUET, _ANON_CHAT, _ANON_RESPONSES)
_IDS_SYNTHETIQUES = _ANONYMES + ("acme-spark-9.9-free", "acme-spark-9.9", "acme-9.9")


def _non_declare(model_id):
    """Garde : si l'id entre dans `models:`, on n'éprouve plus le repli du tout.

    La découverte auto s'exécute 0,5 s après l'import et ajoute tout id annoncé
    en amont ; les préfixes `acme-` et la version `9.9` sont hors catalogue,
    mais la garde doit le **dire** au lieu de le supposer — sinon un id qui
    entrerait rendrait ces tests verts sur le mauvais chemin.
    """
    assert model_id not in st.MODELS, f"{model_id} est déclaré : plus de repli à éprouver"


def _repli(model_id):
    """`get_model_config()` sur un id absent de `models:`, cache vidé autour."""
    _non_declare(model_id)
    st.get_model_config.cache_clear()
    try:
        return st.get_model_config(model_id)
    finally:
        st.get_model_config.cache_clear()


def test_repli_id_anonyme_en_responses_vise_la_jambe_anonyme():
    """La moitié « bon endpoint » de la carte N4.

    Un id de la famille `responses` au suffixe `-free` visait `API_BASE_OPENAI`
    — `/chat/completions` sur la jambe payée, donc 404. Il vise maintenant
    `_RESPONSES_FREE_ENDPOINT`, comme un id **déclaré** du même nom
    (`test_resolve_endpoint_api_driven`) : le repli et la règle de construction
    ne sont plus deux implémentations qui peuvent diverger.
    """
    cfg = _repli(_ANON_RESPONSES)
    assert st.get_model_api(_ANON_RESPONSES) == "responses"
    assert cfg["endpoint"] == st._RESPONSES_FREE_ENDPOINT
    assert mc.endpoint_leg(cfg["endpoint"]) == mc.FREE_PROVIDER
    # le module de diagnostic et le routeur voient le même endpoint
    assert mc.resolved_endpoint(_ANON_RESPONSES) == cfg["endpoint"]


def test_repli_id_anonyme_en_chat_vise_la_jambe_anonyme():
    """Le même durcissement sur la famille `chat`, là où seul le suffixe parle.

    `_ANON_CHAT` n'a aucune famille reconnue : avant, il partait payé ; il part
    maintenant sur `API_BASE_FREE`, la jambe que son suffixe `-free` désigne.
    """
    cfg = _repli(_ANON_CHAT)
    assert st.get_model_api(_ANON_CHAT) == "chat"
    assert cfg["endpoint"] == st.API_BASE_FREE
    assert mc.endpoint_leg(cfg["endpoint"]) == mc.FREE_PROVIDER
    assert mc.resolved_endpoint(_ANON_CHAT) == cfg["endpoint"]


def test_repli_id_muet_reste_sur_la_jambe_payee():
    """La limite honnête du durcissement, verrouillée telle quelle.

    Un id que seule la jambe anonyme sert mais dont le nom ne porte aucun signal
    local — pas de `-free`, pas de famille `responses` — reste résolu sur la
    jambe payée. Le repli ne peut pas faire mieux : il ne lit que le nom. La
    jambe réelle arrive par la découverte auto (`detect_free_ids` classe sur
    `pricing` / `free`, que seul le payload amont porte), qui écrit alors un
    `endpoint:` explicite — et l'explicite gagne.

    Le test verrouille les deux bouts : le repli payé **et** le fait que la règle
    de périmètre, elle, sait déjà (elle lit le catalogue live) — cf.
    `test_hors_perimetre_refuse_au_lieu_de_deviner`.
    """
    cfg = _repli(_ANON_MUET)
    assert st.get_model_api(_ANON_MUET) == "chat"
    # le repli ne rend que ce qu'il sait calculer : pas d'`api` ni de
    # `capabilities` inventées pour un id qu'aucune entrée ne décrit
    assert set(cfg) == {"endpoint", "protocol"}
    assert cfg["endpoint"] == st.API_BASE_OPENAI
    assert mc.endpoint_leg(cfg["endpoint"]) == mc.PAID_PROVIDER


def test_repli_ne_change_rien_pour_les_ids_declares():
    """Le piège nommé par la carte : le durcissement ne touche QUE les ids
    absents de `models:`.

    Pour tout id déclaré, `get_model_config()` rend la même chose qu'avant —
    l'entrée explicite recouvre le défaut calculé, y compris `endpoint:`,
    `protocol:` et `capabilities:`. Sans cette borne, un repli « intelligent »
    casserait des routes internes bien vivantes.
    """
    st.get_model_config.cache_clear()
    try:
        for model_id, entry in st.MODELS.items():
            cfg = st.get_model_config(model_id)
            assert cfg["endpoint"] == entry["endpoint"], model_id
            assert cfg["protocol"] == entry.get("protocol", "openai"), model_id
            for key, value in entry.items():
                assert cfg[key] == value, f"{model_id}.{key} a changé"
    finally:
        st.get_model_config.cache_clear()


def test_exception_jev_echappe_au_durcissement():
    """L'exception de périmètre : `jev-1.13*` porte un `endpoint:` interne
    (`/zen/v1/systemone`), il ressort donc VERBATIM du repli — le durcissement
    est conditionné à l'absence d'`endpoint:` et ne l'atteint jamais.
    `jev-1.13-free` montre que le suffixe ne suffit pas à détourner un endpoint
    explicite : l'exception continue d'être servie telle quelle.
    """
    st.get_model_config.cache_clear()
    try:
        for model_id in mc.INTERNAL_IDS:
            assert model_id in st.MODELS, f"{model_id} a quitté models:"
            cfg = st.get_model_config(model_id)
            assert cfg["endpoint"] == st.MODELS[model_id]["endpoint"]
            assert cfg["endpoint"] == "https://opencode.ai/inference/v1/systemone"
            # endpoint interne : ni la jambe payée, ni l'anonyme
            assert mc.endpoint_leg(cfg["endpoint"]) is None
    finally:
        st.get_model_config.cache_clear()


def test_repli_ne_sait_rendre_quune_base_connue():
    """Borne dure : le repli visera toujours l'un des endpoints que `config.yaml`
    déclare (`upstream.*`), jamais une URL inventée.

    C'est ce qui rend le durcissement sans risque — un id inconnu ne peut pas
    faire viser au proxy un hôte qu'il n'aurait pas configuré, seulement la
    mauvaise jambe d'un hôte déjà configuré.
    """
    for model_id in _IDS_SYNTHETIQUES:
        endpoint = _repli(model_id)["endpoint"]
        assert endpoint in mc.known_bases(), model_id
        assert mc.endpoint_leg(endpoint) in (mc.PAID_PROVIDER, mc.FREE_PROVIDER)


def test_hors_perimetre_refuse_au_lieu_de_deviner():
    """L'autre moitié de la carte : « bon endpoint **ou refus nommé** ».

    `test_repli_id_muet_reste_sur_la_jambe_payee` montrait la limite locale du
    repli. La règle de périmètre, elle, lit le catalogue live : elle SAIT que
    `_ANON_MUET` est servi par la jambe anonyme, donc elle ne le refuse jamais ;
    et elle refuse `_TIER`, que seul un provider tiers héberge, en nommant l'id,
    les hôtes et le `npm` de chacun. Symétrie : un id qu'un provider opencode
    sert n'est JAMAIS refusable (`tests/test_npm_routing.py`).
    """
    catalogue = {
        mc.PAID_PROVIDER: {"npm": "@ai-sdk/openai-compatible", "models": {}},
        mc.FREE_PROVIDER: {
            "npm": "@ai-sdk/openai-compatible",
            "models": {model_id: {} for model_id in _ANONYMES},
        },
        "302ai": {"npm": "@ai-sdk/openai-compatible", "models": {_TIER: {}}},
        "aihubmix": {"npm": "@aihubmix/ai-sdk-provider", "models": {_TIER: {}}},
    }
    # 1. ce que la jambe anonyme sert n'est jamais refusé, même au nom muet
    for model_id in _ANONYMES:
        assert mc.serving_provider(model_id, catalogue) == mc.FREE_PROVIDER, model_id
        assert mc.refusal_reason(model_id, catalogue) is None, model_id
        assert mc.is_servable(model_id, catalogue), model_id
    # 2. là où le nom est honnête, la jambe du catalogue et celle du repli concordent
    for model_id in (_ANON_CHAT, _ANON_RESPONSES):
        jambe_repli = mc.endpoint_leg(mc.resolved_endpoint(model_id))
        assert jambe_repli == mc.serving_provider(model_id, catalogue), model_id
    # 3. hors périmètre : refus nommé, puis transporté par `ModelOutOfScope`
    reason = mc.refusal_reason(_TIER, catalogue)
    assert reason is not None
    assert _TIER in reason
    assert "302ai (npm @ai-sdk/openai-compatible, api chat)" in reason
    assert "aihubmix (npm @aihubmix/ai-sdk-provider, outside the known table" in reason
    assert not mc.is_servable(_TIER, catalogue)
    exc = ModelOutOfScope(_TIER, reason)
    assert exc.model_id == _TIER
    assert exc.reason == reason
    assert str(exc) == reason
