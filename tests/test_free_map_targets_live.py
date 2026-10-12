"""Contrôle des cibles du repli libre — `free_model_map`, `web_search_native`,
`free_discovery.default_target` (PLAN N2).

**Le défaut.** Le relevé live du 2026-10-11 contre
<https://models.opencode.ai/api.json> montre que deux des cibles que
`config.yaml` fait viser à la jambe anonyme sont **`deprecated`** côté
`opencode` : `mimo-v2.5-free` (contexte 200000, sortie 32000) et
`muse-spark-1.2-contributor-free` (contexte 1 M, sortie 131072). Rien ne le
savait : le seul module qui lit un statut (`config/models_catalog.py`) est un
diagnostic en lecture seule, et il ne regarde que les **clés** de `models:` —
jamais les **valeurs** de `free_model_map` ni les entrées de
`web_search_native`. Une cible dépréciée n'est pas encore une cible morte,
mais elle porte une date de fin : elle meurt au premier appel, sans que rien
ne l'ait annoncé — exactement le défaut que le catalogue hygiène a été écrit
pour fermer, à l'autre bout de la table.

**La jambe, pas le nom.** Le statut ne vaut que pour la jambe **routée**
(`classify()` de `config/models_catalog.py`, §« Comment la jambe est décidée »
de `docs/models-protocol.md`) : `space-bunny-free` est `deprecated` côté payé
et **vivant** côté anonyme, et ce contrôle ne doit pas le toucher. La jambe
est donc résolue par la **source unique** `_resolve_model_endpoint`, appliquée
au document `config.yaml` lui-même et non à l'état du process — que le thread
de découverte mute 0,5 s après l'import.

**Le périmètre du contrôle.** Neuf sites, pas les « 6 + 1 » de la carte : le
relevé en a trouvé deux de plus, tous deux réels et vivants dans le même
`config.yaml` — `free_model_map["muse-spark-1.2-contributor"]` (le pendant
payant de la carte pointe déjà sur sa cible `-free` dépréciée) et
`free_discovery.default_target`, le repli de la découverte auto. Le contrôle
les couvre tous les trois de la même règle.

**Hermétique.** Aucun test de `tests/` ne tape le réseau : le statut live est
figé ici (`CIBLES_DEPRECIEES`), comme `PURGES` dans
`tests/test_models_catalog.py` et `purged_ids` dans `tests/test_go_only_routing.py`.
Pour rafraîchir la liste après une évolution du catalogue :
`python scripts/check_models_catalog.py` (lecture seule).

**État attendu : VERT depuis le 2026-10-12.** Le recâblage N2 est fait
(`free_model_map[muse-spark-1.2-contributor]` →
`muse-spark-1.3-contributor-free`, `muse-spark-1.2-contributor-free` retiré de
`web_search_native` — le 6 × `mimo-v2.5-free` et `default_target` l'étaient
déjà). Le marqueur `xfail(strict=True)` est donc levé : tout nouveau site
visant une cible dépréciée rend ce test rouge, comme prévu.
"""

from pathlib import Path

import pytest
import yaml

import config.models_catalog as mc
import config.settings as st

_ROOT = Path(mc.__file__).resolve().parent.parent

# Cibles dépréciées **sur la jambe anonyme** (`opencode`, `/zen/v1`), relevé
# live du 2026-10-11. `space-bunny-free` en est volontairement absente :
# dépréciée côté payé, vivante côté anonyme — c'est la jambe routée qui
# compte, et c'est ce que verrouille `test_cible_depreciee_sur_l_autre_jambe`.
CIBLES_DEPRECIEES = ("mimo-v2.5-free", "muse-spark-1.2-contributor-free")

# Endpoint interne de l'exception `jev-1.13*` (`/zen/v1/systemone`) — hors des
# deux jambes, donc hors du statut public. Verrouillé par
# `tests/test_model_catalog.py::test_exception_jev_echappe_au_durcissement`.
_INTERNAL_ENDPOINT = "https://opencode.ai/zen/v1/systemone"


# ─────────── Règle (pure, testable sans config.yaml) ────────────────────


def _jambe(model_id, models):
    """Jambe routée d'un id telle que la résout la SOURCE UNIQUE.

    ``None`` = endpoint interne (`jev-1.13*` sur `/zen/v1/systemone`) : hors
    des deux jambes du catalogue public, donc jamais comparé à un statut.
    """
    cfg = models.get(model_id) if isinstance(models, dict) else None
    endpoint = mc.resolved_endpoint(model_id, cfg if isinstance(cfg, dict) else {})
    return mc.endpoint_leg(endpoint)


def _sites(doc):
    """Chaque ``(site, id)`` qu'un document fait viser par la jambe anonyme.

    Trois familles, et une seule fonction pour les lire : un nouveau site
    ajouté demain (une autre carte, un autre repli) doit être déclaré ici,
    pas découvert par un rouge.
    """
    sites = []
    mapping = doc.get("free_model_map")
    if isinstance(mapping, dict):
        for payant, cible in mapping.items():
            sites.append((f"free_model_map[{payant}]", cible))
    native = doc.get("web_search_native")
    if isinstance(native, list):
        for index, model_id in enumerate(native):
            sites.append((f"web_search_native[{index}]", model_id))
    discovery = doc.get("free_discovery")
    if isinstance(discovery, dict):
        cible = discovery.get("default_target")
        if isinstance(cible, str):
            sites.append(("free_discovery.default_target", cible))
    return [
        (site, str(model_id).strip())
        for site, model_id in sites
        if isinstance(model_id, str) and model_id.strip()
    ]


def _cibles_depreciees(doc, depreciees=CIBLES_DEPRECIEES):
    """Les sites qui visent une cible dépréciée **sur la jambe anonyme**.

    Rend ``[(site, id)]`` — vide = conforme.
    """
    if not isinstance(doc, dict):
        return []
    models = doc.get("models")
    interdites = set(depreciees)
    offenders = []
    for site, model_id in _sites(doc):
        if model_id not in interdites:
            continue
        if _jambe(model_id, models) != mc.FREE_PROVIDER:
            continue  # l'id n'est pas routé anonyme : le statut d'amont ne nous concerne pas
        offenders.append((site, model_id))
    return offenders


def _doc():
    """Le `config.yaml` réel, lu depuis la racine du dépôt."""
    raw = (_ROOT / "config.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(raw) or {}


# ─────────── 1. La mécanique, sur documents synthétiques ────────────────


def test_cible_depreciee_refusee():
    """Le cœur du contrôle : une valeur de `free_model_map` dépréciée est nommée."""
    doc = {
        "models": {"acme-live-free": {"protocol": "openai"}},
        "free_model_map": {
            "acme-paid": "acme-dead-free",
            "acme-other-paid": "acme-live-free",
        },
    }
    offenders = _cibles_depreciees(doc, depreciees=("acme-dead-free",))
    assert offenders == [("free_model_map[acme-paid]", "acme-dead-free")]


def test_cible_depreciee_sur_l_autre_jambe_ignoree():
    """Le piège de `space-bunny-free`, reproduit : un id déprécié côté payé mais
    routé côté anonyme — ou l'inverse — n'est PAS une faute de ce contrôle.

    Le statut ne vaut que pour la jambe **routée** : `endpoint:` explicite gagne
    dans la source unique, donc l'id reste sur le payant et le statut d'amont
    côté anonyme ne le concerne pas.
    """
    doc = {
        "models": {"acme-dead-free": {"endpoint": st.API_BASE_OPENAI, "protocol": "openai"}},
        "free_model_map": {"acme-paid": "acme-dead-free"},
    }
    assert _jambe("acme-dead-free", doc["models"]) == mc.PAID_PROVIDER
    assert _cibles_depreciees(doc, depreciees=("acme-dead-free",)) == []


def test_exception_interne_jamais_refusee():
    """L'exception `jev-1.13*` échappe au contrôle, comme à la purge et au
    durcissement du repli : son endpoint est interne (`/zen/v1/systemone`), il
    n'a donc aucune jambe de catalogue — pas de statut à lire.

    `jev-1.13-free` est un nom `-free` visant un endpoint explicite interne :
    c'est la trace de la règle « l'explicite gagne », la même qui rend
    `space-bunny-free` hors de portée du contrôle.
    """
    doc = {
        "models": {"jev-1.13-free": {"endpoint": _INTERNAL_ENDPOINT, "protocol": "openai"}},
        "free_model_map": {"jev-1.13": "jev-1.13-free"},
    }
    assert _jambe("jev-1.13-free", doc["models"]) is None
    assert _cibles_depreciees(doc, depreciees=("jev-1.13-free",)) == []


def test_web_search_native_distingue_les_deux_variantes():
    """`web_search_native` porte le modèle ET son homonyme `-free` : la variante
    nue est routée côté payé, la variante `-free` côté anonyme. Seule celle qui
    est routée anonyme peut être en faute — même si les deux ids figurent dans
    la même liste d'interdits.

    C'est le site que la carte N2 nommait déjà (« retirer
    `muse-spark-1.2-contributor-free` de `web_search_native` »), vérifié ici
    dans sa mécanique avant de l'appliquer au fichier réel.
    """
    doc = {
        "free_model_native": None,
        "web_search_native": ["acme-x-contributor", "acme-x-contributor-free"],
        "free_model_map": {},
    }
    interdits = ("acme-x-contributor-free", "acme-x-contributor")
    offenders = _cibles_depreciees(doc, depreciees=interdits)
    assert offenders == [("web_search_native[1]", "acme-x-contributor-free")]


def test_default_target_est_couvert():
    """Le site que la carte N2 ne nommait pas : `free_discovery.default_target`,
    le repli de la découverte auto quand aucun id gratuit n'est annoncé.

    Il visait `mimo-v2.5-free` — la même cible dépréciée que six entrées de
    `free_model_map`. Sans ce site dans `_sites()`, le contrôle aurait rendu un
    vert partiel sur un fichier toujours dégradé.
    """
    doc = {"free_discovery": {"default_target": "acme-dead-free"}}
    offenders = _cibles_depreciees(doc, depreciees=("acme-dead-free",))
    assert offenders == [("free_discovery.default_target", "acme-dead-free")]


def test_cibles_vivantes_acceptees():
    """La forme attendue APRÈS recâblage, si l'arbitrage retient les deux
    candidats déjà vivants côté catalogue : les deux cibles du plan
    (`muse-spark-1.3-contributor-free`, `mimo-v2.6-flash-free`) ne déclenchent
    rien. Le contrôle ne juge donc pas le choix — seulement la vitalité.
    """
    doc = {
        "free_model_map": {"muse-spark-1.2-contributor": "muse-spark-1.3-contributor-free"},
        "web_search_native": ["muse-spark-1.3-contributor-free"],
        "free_discovery": {"default_target": "mimo-v2.6-flash-free"},
    }
    assert _cibles_depreciees(doc) == []


@pytest.mark.parametrize("doc", [{}, {"models": None}, {"free_model_map": "acme"}, None])
def test_sections_absentes_ou_mal_typees(doc):
    """Une clé absente, nulle, ou mal typée ne doit ni planter ni créer de faux
    positif — même régime que `_normalize_ids` pour `purged_ids`."""
    assert _cibles_depreciees(doc) == []


# ─────────── 2. Inventaire réel : le `config.yaml` du dépôt ─────────────


def test_inventaire_depreciees_non_vide():
    """Garde : une liste d'interdits vide rendrait tout l'inventaire vert pour
    rien. Même garde que `PURGES` dans `tests/test_models_catalog.py`."""
    assert CIBLES_DEPRECIEES, "inventaire vide"
    assert "space-bunny-free" not in CIBLES_DEPRECIEES


def test_config_reel_ne_vise_aucune_cible_depreciee():
    """Le contrôle proprement dit, sur le fichier réel : AUCUN site de
    `config.yaml` ne doit nommer une cible dépréciée sur la jambe anonyme.

    VERT depuis le recâblage N2 du 2026-10-12 (le 6 × `mimo-v2.5-free` et
    `default_target` l'étaient déjà, les 2 derniers sites
    `muse-spark-1.2-contributor-free` ont été recâblés ce jour-là vers
    `muse-spark-1.3-contributor-free`). Le message liste les sites en faute :
    un rouge futur est un rapport attendu, pas un incident — il nomme le
    nouveau site à recâbler.
    """
    offenders = _cibles_depreciees(_doc())
    assert not offenders, "sites visant une cible dépréciée sur la jambe anonyme :\n" + "\n".join(
        f"  {site:<40} -> {model_id}" for site, model_id in offenders
    )
