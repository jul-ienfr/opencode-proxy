"""[PLAN_ALIGNEMENT_CLIENT_OFFICIEL H1] Hygiène du catalogue modèles.

**Le module.** `config/models_catalog.py` classe chaque entrée de `models:` contre
le catalogue **live** de l'éditeur, sur les DEUX jambes (`opencode-go` payée,
`opencode` anonyme). Une comparaison par seul suffixe ``-free`` est aveugle :
trois ids de la jambe payée finissent par ``-free``.

**Le piège que ces tests verrouillent.** `get_model_config()` a des défauts
*asymétriques* — `{endpoint, protocol}` seulement, sans `api` ni `capabilities`.
Retirer une entrée de `models:` rétrécit donc la forme du dict : ce n'est pas un
no-op par construction, ça doit être **prouvé**. La preuve tient en trois points,
testés plus bas :

1. le seul consommateur de `capabilities` (`opencode.py`, gate `web-search`)
   est faux pour les ids purgés, donc il retombe sur le repli
   `dashboard/quota.py` dans les DEUX états ;
2. aucun consommateur ne lit `api` sur un cfg de modèle ;
3. `endpoint`/`protocol` résolvent à l'identique du repli implicite.

Le second verrou est le job lui-même : **désactivé par défaut**, il ne part pas
sur le réseau et n'écrit jamais `config.yaml`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from config import models_catalog as mc
from config import settings as cs

_ROOT = Path(cs.__file__).resolve().parent.parent
_CLI = _ROOT / "scripts" / "check_models_catalog.py"

# Les 5 ids morts (absents des DEUX jambes, sans endpoint interne), purgés
# pendant H1 : ils restent nommés ici pour verrouiller la non-régression.
PURGES = (
    "deepseek-flash",
    "hy3-preview",
    "mimo-v2-omni",
    "mimo-v2-pro",
    "omen-alpha",
)


# ─────────── Catalogue synthétique (aucun réseau) ───────────────────────────


def _catalog(**entries) -> dict:
    """`{provider: {"models": {id: {"status": ...}}}}` — seules les clés utiles."""
    return {
        provider: {"models": models}
        for provider, models in (
            (mc.PAID_PROVIDER, entries.get("paid", {})),
            (mc.FREE_PROVIDER, entries.get("free", {})),
        )
    }


def _load_cli():
    spec = importlib.util.spec_from_file_location("_check_models_catalog", _CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ─────────── Classification ─────────────────────────────────────────────────


def test_les_cinq_verdicts():
    catalog = _catalog(
        paid={"vivant": {}, "fini": {"status": "deprecated"}},
        free={"vivant-free": {}, "fini-free": {"status": "deprecated"}},
    )
    models = {
        "vivant": {"protocol": "openai"},           # payé, routé payé
        "fini": {"protocol": "openai"},             # payé, routé payé, déprécié
        "vivant-free": {"protocol": "openai"},      # anonyme, routé anonyme
        "fini-free": {"protocol": "openai"},        # anonyme, routé anonyme, déprécié
        "fantome": {"protocol": "openai"},           # absent des deux jambes
        "interne": {"endpoint": "https://exemple.local/v1"},  # hors catalogue, interne
    }
    got = {mid: mc.classify(mid, cfg, catalog) for mid, cfg in models.items()}
    assert got == {
        "vivant": mc.OK,
        "fini": mc.DEPRECATED,
        "vivant-free": mc.OK,
        "fini-free": mc.DEPRECATED,
        "fantome": mc.MORT,
        "interne": mc.INTERNE,
    }


def test_present_seulement_cote_anonyme_est_hors_jambe_pas_mort():
    """Le défaut historique du suffixe : ``glm-5.1`` n'existe que côté anonyme
    mais `models:` le route côté payé. Ce n'est PAS une entrée morte."""
    catalog = _catalog(free={"glm-5.1": {}})
    assert mc.classify("glm-5.1", {"protocol": "openai"}, catalog) == mc.HORS_JAMBE


def test_endpoint_interne_nest_jamais_declare_mort():
    """``jev-1.13`` vit sur un endpoint interne (``systemone``) : absent du
    catalogue public, pourtant parfaitement valide."""
    catalog = _catalog()
    cfg = {"protocol": "openai", "endpoint": "https://opencode.ai/zen/v1/systemone"}
    assert mc.classify("jev-1.13", cfg, catalog) == mc.INTERNE
    assert mc.is_internal("jev-1.13", cfg) is True


def test_suffixe_free_ne_decide_pas_de_la_jambe():
    """Trois ids de la jambe PAYÉE finissent par ``-free`` : la classification
    suit l'**endpoint résolu**, pas le nom.

    Le suffixe ne fabrique que l'endpoint *par défaut* (`API_BASE_FREE`) ; un
    ``endpoint: go`` explicite l'emporte sur lui. Les deux cas réels existent :
    ``longcat-2.5-preview-free`` et ``step-5-preview-free`` sont sur les deux
    jambes (donc ``ok`` quelle que soit la route), et un id payé-only à suffixe
    ``-free`` serait ``hors-jambe`` s'il était routé côté anonyme."""
    suffixe = {"protocol": "openai"}  # pas d'endpoint explicite → suffixe ⇒ anonyme
    paye = {"protocol": "openai", "endpoint": "go"}  # endpoint explicite ⇒ payé

    # Sans endpoint explicite, le suffixe envoie côté anonyme.
    assert mc.resolved_endpoint("longcat-2.5-preview-free", suffixe) == cs.API_BASE_FREE
    assert mc.classify("longcat-2.5-preview-free", suffixe, _catalog(paid={"longcat-2.5-preview-free": {}})) == mc.HORS_JAMBE
    assert mc.classify("longcat-2.5-preview-free", suffixe, _catalog(free={"longcat-2.5-preview-free": {}})) == mc.OK

    # La route explicite décide : même id, même suffixe, verdict inverse.
    assert mc.resolved_endpoint("longcat-2.5-preview-free", paye) == cs.API_BASE_OPENAI
    assert mc.classify("longcat-2.5-preview-free", paye, _catalog(paid={"longcat-2.5-preview-free": {}})) == mc.OK
    assert mc.classify("longcat-2.5-preview-free", paye, _catalog(free={"longcat-2.5-preview-free": {}})) == mc.HORS_JAMBE

    # Présent sur les deux jambes : n'importe quelle route donne ``ok``.
    deux = _catalog(paid={"longcat-2.5-preview-free": {}}, free={"longcat-2.5-preview-free": {}})
    assert mc.classify("longcat-2.5-preview-free", suffixe, deux) == mc.OK
    assert mc.classify("longcat-2.5-preview-free", paye, deux) == mc.OK


def test_deprecie_sur_lautre_jambe_ne_compte_pas():
    """``space-bunny-free`` est déprécié côté payé, actif côté anonyme : on le
    route côté anonyme, le verdict doit suivre la jambe routée."""
    catalog = _catalog(paid={"space-bunny-free": {"status": "deprecated"}}, free={"space-bunny-free": {}})
    assert mc.classify("space-bunny-free", {"protocol": "openai"}, catalog) == mc.OK


def test_absent_des_deux_jambes_sans_endpoint_interne_est_mort():
    catalog = _catalog(paid={"autre": {}})
    assert mc.classify("fantome", {"protocol": "openai"}, catalog) == mc.MORT


# ─────────── Le module ne démarre rien à l'import ───────────────────────────


def test_pas_dimport_de_settings_au_chargement():
    """Importer ``config.settings`` lance le thread de découverte amont
    (`config/discovery.py:start_background_fetch`). Un module de diagnostic doit
    rester importable sans rien démarrer — d'où l'accesseur paresseux."""
    head = Path(mc.__file__).read_text(encoding="utf-8").split("def _settings")[0]
    assert "from config import settings" not in head
    assert "import config" not in head


def test_resolution_deleguee_a_la_source_unique():
    """Le module ne réplique PAS `_resolve_model_endpoint` : il l'appelle. La
    parité est vérifiée sur chaque id réel de `config.yaml`."""
    for model_id, entry in cs.MODELS.items():
        assert mc.resolved_endpoint(model_id, entry) == entry["endpoint"], model_id


# ─── Non-changement : purger les 5 ids morts ne change AUCUN comportement ────


def test_ids_purges_absents_de_models():
    """Verrou de purge : si l'un revient, c'est que quelqu'un a réintroduit une
    entrée morte sans repasser par le job."""
    for model_id in PURGES:
        assert model_id not in cs.MODELS, f"{model_id} est revenu dans models:"


@pytest.mark.parametrize("model_id", PURGES)
def test_lentree_purgee_resolvait_deja_comme_le_repli(model_id):
    """Le cœur de la preuve. L'entrée d'avant purge était ``{protocol: openai}``
    et rien d'autre (pas d'``endpoint``, pas d'``api``, pas de ``capabilities``,
    pas d'``output_limit``). On la rejoue contre le repli implicite ``{}`` — ce
    que `get_model_config()` reçoit désormais : même endpoint, même protocole."""
    avant = {"protocol": "openai"}
    assert mc.resolved_endpoint(model_id, avant) == mc.resolved_endpoint(model_id, {})
    assert mc.resolved_endpoint(model_id, avant) == cs.API_BASE_OPENAI
    assert cs.get_model_config(model_id) == {
        "endpoint": cs.API_BASE_OPENAI,
        "protocol": "openai",
    }


@pytest.mark.parametrize("model_id", PURGES)
def test_famille_api_inchangee(model_id):
    """Deuxième point : aucun consommateur ne lit ``api`` sur un cfg de modèle,
    mais on verrouille quand même la valeur — ``chat`` avant comme après, donc
    `MODEL_API_FAMILIES` ne change pas de réponse pour ces ids."""
    assert cs.get_model_api(model_id, {"protocol": "openai"}) == "chat"
    assert cs.get_model_api(model_id, {}) == "chat"


@pytest.mark.parametrize("model_id", PURGES)
def test_capabilities_sans_web_search(model_id):
    """Troisième point : le SEUL consommateur de ``capabilities`` exige
    ``web-search`` truthy (`opencode.py`). Il est faux pour ces ids, donc il
    retombe sur le repli `dashboard/quota.py` dans les deux états — la purge ne
    fait que rendre explicite un chemin déjà pris."""
    assert not cs.get_model_capabilities(model_id).get("web-search")
    assert not cs.get_model_capabilities(model_id, {"protocol": "openai"}).get("web-search")


def test_un_seul_consommateur_de_capabilities():
    """Garde anti-révert : si un second site se met à dépendre de la présence de
    la clé ``capabilities`` dans le cfg, retirer une entrée cesserait d'être
    neutre et ce test doit casser."""
    src = _ROOT / "opencode.py"
    body = src.read_text(encoding="utf-8")
    assert body.count('cfg.get("capabilities")') == 1, (
        "un second consommateur de capabilities est apparu : "
        "la purge d'une entrée models: n'est plus un non-changement"
    )


# ─────────── Le job : désactivé par défaut, jamais écrit ────────────────────


def test_job_desactive_par_defaut_dans_config():
    raw = (_ROOT / "config.yaml").read_text(encoding="utf-8")
    import yaml

    section = (yaml.safe_load(raw) or {}).get("models_catalog") or {}
    assert section.get("enabled") in (False, None), "le job doit être désactivé par défaut"


def test_job_refuse_de_partir_quand_desactive(monkeypatch, tmp_path):
    cli = _load_cli()
    monkeypatch.setattr(cli, "_cfg_flag_enabled", lambda default=False: False)
    monkeypatch.setattr(sys, "argv", ["check_models_catalog.py"])
    assert cli.main() == cli.EXIT_DESACTIVE
    assert not list(tmp_path.iterdir()), "le job a écrit alors qu'il est désactivé"


def test_job_ecrit_un_rapport_et_ne_touche_pas_config(monkeypatch, tmp_path):
    cli = _load_cli()
    monkeypatch.setattr(cli, "_cfg_flag_enabled", lambda default=False: True)
    monkeypatch.setattr(cli, "fetch_catalog", lambda *a, **k: _catalog(paid={"x": {}}))
    out = tmp_path / "rapport.json"
    monkeypatch.setattr(sys, "argv", ["check_models_catalog.py", "--out", str(out)])
    avant = (_ROOT / "config.yaml").read_bytes()
    assert cli.main() == cli.EXIT_OK
    apres = (_ROOT / "config.yaml").read_bytes()
    assert avant == apres, "config.yaml a été modifié par le job"
    rapport = json.loads(out.read_text(encoding="utf-8"))
    for cle in ("generated_at", "source", "counts", "configured", "missing_from_config"):
        assert cle in rapport, cle
    assert rapport["source"] == mc.CATALOG_URL


def test_sortie_par_defaut_horodatee_dans_logs(monkeypatch, tmp_path):
    cli = _load_cli()
    monkeypatch.setattr(cli, "_cfg_flag_enabled", lambda default=False: True)
    monkeypatch.setattr(cli, "fetch_catalog", lambda *a, **k: _catalog())
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["check_models_catalog.py"])
    assert cli.main() == cli.EXIT_OK
    produits = sorted((tmp_path / "logs").glob("models_catalog.diff.*.json"))
    assert len(produits) == 1, produits
    assert produits[0].name.endswith("Z.json"), "l'horodatage doit être en UTC"


def test_rapport_texte_couvre_toutes_les_entrees():
    rapport = mc.audit(_catalog(paid={"x": {}}), {"x": {"protocol": "openai"}, "y": {"protocol": "openai"}})
    texte = mc.format_report(rapport)
    assert "MORT (1)" in texte
    assert "y" in texte
    assert rapport["counts"]["configured"] == 2
