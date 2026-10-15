"""Lot Z (plan §19.5, vague V1b) — les routes ``custom_routes`` de muse-spark, verrouillées.

Historique : une route custom a un temps réécrit ``muse-spark-1.3-contributor``
vers ``step-5-preview-free`` (jambe anonyme) AVANT la résolution d'endpoint.
Ce shadow a été retiré de la config déployée : la route ``musespark13contributor``
est désormais une IDENTITÉ (match = model = ``muse-spark-1.3-contributor``).

Comportement vivant actuel, sur la config réelle :

    muse-spark-1.3-contributor  -->  muse-spark-1.3-contributor  (custom_routes, identité)
                                -->  jambe free via FREE_MODEL_MAP
                                     (muse-spark-1.3-contributor-free, api: responses,
                                      geo meta-muse-spark, endpoint zen/v1/responses anonyme)

L'entrée ``models.muse-spark-1.3-contributor`` (``api: responses``, geo
``meta-muse-spark``…) EST lue pour cet id. Ce fichier existe pour que toute
déviation (réintroduction d'un shadow, changement de cible) casse bruyamment
au lieu de laisser croire que la jambe free est ce qu'elle était.

Contrairement à ``tests/test_e2e_protocol_matrix.py`` — qui neutralise
``SORTED_CUSTOM_ROUTES`` pour mesurer la seule matrice de protocoles — ce fichier
lit la config telle qu'elle est déployée.
"""

import config.settings as _cfg_settings
from app.router import route_for


def _resolve(name):
    """Route fraîche (cache local, aucun état partagé avec les autres tests)."""
    res, _ = route_for(
        name,
        models=_cfg_settings.MODELS,
        custom_routes=_cfg_settings.CUSTOM_ROUTES,
        disable_mapping=_cfg_settings.DISABLE_MAPPING,
        sorted_routes=_cfg_settings.SORTED_ROUTES,
        sorted_custom_routes=_cfg_settings.SORTED_CUSTOM_ROUTES,
        route_cache={},
        route_cache_max=100,
        seen_version=-1,
        current_version=_cfg_settings.ROUTE_VERSION,
    )
    return res


def test_muse_spark_13_contributor_is_identity_not_shadowed():
    """Plus de shadow : l'id demandé sort tel quel (route identité)."""
    assert _resolve("muse-spark-1.3-contributor")["model"] == "muse-spark-1.3-contributor"


def test_free_leg_comes_from_free_model_map():
    """La jambe free passe par FREE_MODEL_MAP, pas par custom_routes."""
    assert (
        _cfg_settings.FREE_MODEL_MAP.get("muse-spark-1.3-contributor")
        == "muse-spark-1.3-contributor-free"
    )
    # …et aucune route custom ne réécrit cet id vers autre chose :
    for r in _cfg_settings.SORTED_CUSTOM_ROUTES:
        if "muse-spark-1.3-contributor" in (r.get("match") or []):
            assert r.get("model") == "muse-spark-1.3-contributor"


def test_free_target_lands_on_the_anonymous_responses_leg():
    """La cible free est api: responses, geo meta-muse-spark, endpoint anonyme."""
    cfg = _cfg_settings.get_model_config("muse-spark-1.3-contributor-free")
    assert cfg["endpoint"] == "https://opencode.ai/inference/v1/responses"
    assert cfg.get("api") == "responses"


def test_paid_entry_of_the_id_is_reached_at_runtime():
    """L'entrée ``models:`` de l'id demandé dit /responses… et elle est lue."""
    paid = _cfg_settings.get_model_config("muse-spark-1.3-contributor")
    assert "responses" in paid["endpoint"]
    # …et la route réellement prise mène à cet id, pas à un shadow :
    resolved = _resolve("muse-spark-1.3-contributor")["model"]
    assert resolved == "muse-spark-1.3-contributor"


def test_pass_is_not_transitive():
    """L'id réécrit ne repasse PAS dans custom_routes (sinon boucle / surprise)."""
    target = _resolve("muse-spark-1.3-contributor")["model"]
    # muse-spark-1.3-contributor EST le match d'une route custom (identité) :
    # le passage unique s'arrête dessus sans boucle.
    assert target in _cfg_settings.MODELS


def test_gpt56luna_routes_to_muse_spark_identity():
    """gpt-5.6-luna route vers muse-spark-1.3-contributor (identité ensuite)."""
    assert _resolve("gpt-5.6-luna")["model"] == "muse-spark-1.3-contributor"
    # Son intention (/responses) est satisfaite par l'entrée models:.
    assert "responses" in _cfg_settings.get_model_config("muse-spark-1.3-contributor")["endpoint"]
    assert "responses" in _cfg_settings.get_model_config("gpt-5.6-luna")["endpoint"]


def test_unshadowed_model_resolves_to_itself():
    """Contre-épreuve : sans route custom qui matche exactement, l'identité tient."""
    assert _resolve("grok-4.7")["model"] == "grok-4.7"
    assert _resolve("glm-5")["model"] == "glm-5"
