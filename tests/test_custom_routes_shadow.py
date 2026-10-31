"""Les routes ``custom_routes`` déployées sont exécutées à la lettre.

Historique (Lot Z §19.5, vague V1b) : ce fichier verrouillait l'IDENTITÉ d'un
shadow anonyme retiré de la config déployée (``muse-spark-1.3-contributor`` →
``step-5-preview-free``). Depuis le 2026-10-17, le custom mapping est
SOUVERAIN : chaque redir (payant → free inclus) est une décision de
l'utilisateur, jamais une dérive à « réparer » — voir
``docs/custom-mapping-gouvernance.md``.

Le contrat testé ici n'est donc plus le CONTENU du mapping, mais son
EXÉCUTION :

1. l'objet ``custom_routes`` chargé au runtime est exactement celui du yaml
   disque (aucun writer automatique — persist mémoire, discovery, agent — n'a
   réécrit les déclarations humaines, c'est l'écrasement du 2026-10-17) ;
2. pour chaque route déclarée : chaque id du ``match`` sort EXACTEMENT sur la
   ``model`` déclarée, en un unique hop (pas de passage transitif quand la
   cible est elle-même le ``match`` d'une autre route) ;
3. tout id couvert par aucune route sort à l'identité ;
4. la jambe *free-first* reste accessible via ``FREE_MODEL_MAP`` (indépendante
   des ``custom_routes``).

Contrairement à ``tests/test_e2e_protocol_matrix.py`` — qui neutralise
``SORTED_CUSTOM_ROUTES`` pour mesurer la seule matrice de protocoles — ce
fichier lit la config telle qu'elle est déployée.
"""

import yaml

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


def _match_to_target():
    """Match → cible de toutes les routes déclarées (pour les contre-épreuves)."""
    m2t = {}
    for r in _cfg_settings.SORTED_CUSTOM_ROUTES:
        for _id in r.get("match") or []:
            m2t[_id] = r.get("model")
    return m2t


def test_declared_mapping_is_what_the_runtime_carries():
    """Aucun writer automatique n'a écrasé les déclarations humaines.

    L'objet ``custom_routes`` du runtime (``CUSTOM_ROUTES`` et sa liste
    triée) est exactement celui du ``config.yaml`` sur disque.
    """
    with open(_cfg_settings.CONFIG_PATH, encoding="utf-8") as f:
        on_disk = (yaml.safe_load(f) or {}).get("custom_routes") or {}
    assert _cfg_settings.CUSTOM_ROUTES == on_disk
    runtime_set = {(tuple(r.get("match") or []), r.get("model")) for r in _cfg_settings.SORTED_CUSTOM_ROUTES}
    disk_set = {(tuple(r.get("match") or []), r.get("model")) for r in on_disk.values()}
    assert runtime_set == disk_set


def test_every_declared_route_is_followed_literally():
    """Chaque id d'un ``match`` sort sur la ``model`` déclarée — sans jugement."""
    for r in _cfg_settings.SORTED_CUSTOM_ROUTES:
        target = r.get("model")
        assert target in _cfg_settings.MODELS, (
            f"cible {target!r} absente de models: — la route est déclarée mais non servable"
        )
        for _id in r.get("match") or []:
            assert _resolve(_id)["model"] == target, f"{_id!r} ne suit pas sa route déclarée ({target!r})"


def test_custom_redirect_never_transits_a_second_hop():
    """La cible d'une route qui est aussi le match d'une autre route s'arrête là.

    Cas réel du yaml déployé : la cible d'un alias peut être elle-même matchée
    par une autre route (ex. un id payant cible d'un alias ET match d'une
    route propre). Le passage unique s'arrête sur la première cible.
    """
    m2t = _match_to_target()
    for r in _cfg_settings.SORTED_CUSTOM_ROUTES:
        target = r.get("model")
        if target not in m2t:
            continue  # pas de cas de chaîne déclaré pour cette cible
        for _id in r.get("match") or []:
            assert _resolve(_id)["model"] == target, (
                f"{_id!r} a transité un second hop vers {m2t[target]!r} au lieu de s'arrêter sur {target!r}"
            )


#: Ids « plats » historiques (aucune route, aucune réécriture attendue). Garde
#: explicite : si la config générale un jour les réécrit, le test le criera.
_PLAIN_IDS = ("grok-4.7", "glm-5")


def test_ids_without_declared_route_resolve_to_identity():
    """Contre-épreuve : sans route custom qui matche, l'identité tient."""
    matched = set(_match_to_target())
    present = [i for i in _PLAIN_IDS if i in _cfg_settings.MODELS and i not in matched]
    if not present:
        candidates = [_id for _id in _cfg_settings.MODELS if _id not in matched]
        present = candidates[:2]
    for _id in present:
        assert _resolve(_id)["model"] == _id, _id


def test_free_first_leg_still_holds_for_mapped_paid_ids():
    """La jambe *free-first* passe par ``FREE_MODEL_MAP``, indépendamment.

    Pour un id payant mappé (jambe free-first), la cible ``-free`` déclarée
    dans la map reste servable (présente dans ``models:``) — même si une
    ``custom_routes`` réécrit l'id vers autre chose : les deux mécanismes
    coexistent, le mapping humain ne détruit pas la jambe free de la map.
    """
    covered = [
        (paid, free)
        for paid, free in _cfg_settings.FREE_MODEL_MAP.items()
        if paid in _match_to_target() and free in _cfg_settings.MODELS
    ]
    assert covered, "aucun couple map couvert + servable : la map a-t-elle été vidée ?"
    for paid, free in covered:
        assert free in _cfg_settings.MODELS, (paid, free)
        cfg = _cfg_settings.get_model_config(free)
        assert cfg.get("endpoint"), (paid, free)
