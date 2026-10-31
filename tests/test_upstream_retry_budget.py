"""[PLAN_ALIGNEMENT_CLIENT_OFFICIEL G2] Budget de reprises amont.

**Le défaut.** Le budget de ré-essais se lisait à cinq endroits différents
(relais payant non-stream + les quatre bornes de flux), chacun avec son propre
``yaml_get("streaming", "retry_attempts", 2)``. Monter le budget à 3-4
tentatives sur les 502 — le seul point où le client officiel fait mieux que
nous (5 tentatives, base 2 s, ×2, cap 30 s) — imposait une chasse à cinq
endroits, dont deux jambes de flux dont le plancher dépend d'autres règles
(lot I3 : ``max(budget, len(API_KEYS))`` + coups libres). Une valeur illisible
(``retry_attempts: 0``) produisait en plus ``while attempt < 0`` / ``range(0)`` :
la requête client échouait SANS qu'aucun appel amont ne parte.

**La réparation.** UN SEUL point de lecture (``opencode._upstream_max_retries``),
qui résout ``upstream.max_retries`` (nouveau) puis l'historique
``streaming.retry_attempts``, puis ``2``, et délègue le bornage/comportement
illisible au module pur ``upstream.retry.resolve_max_retries``. Tant que la
nouvelle clé n'est pas écrite, la valeur effective est EXACTEMENT celle
d'avant.

**Le piège.** Les quatre bornes de la jambe libre gardent leur plancher
``max(budget, len(API_KEYS))`` et leurs coups libres (lot I3) : le nouveau
réglage ne doit JAMAIS abaisser ce plancher. Ces tests le verrouillent.
"""

from pathlib import Path

import pytest
import yaml

import opencode as oc
from upstream.retry import RETRY_BUDGET_MAX, resolve_max_retries

_ROOT = Path(oc.__file__).resolve().parent
_CFG = _ROOT / "config.yaml"


def _read_cfg():
    return yaml.safe_load(_CFG.read_text(encoding="utf-8")) or {}


# ─────────── Non-changement : le défaut effectif est celui d'avant ───────────


def _legacy_expected(cfg):
    """Ce que le proxy calculait AVANT G2, recalculé depuis le disque :
    ``int(streaming.retry_attempts)``, ``2`` si absent, ``2`` si illisible."""
    raw = (cfg.get("streaming") or {}).get("retry_attempts")
    try:
        return max(int(raw), 1)
    except (TypeError, ValueError):
        return 2


def test_defaut_effectif_identique_a_avant():
    """Le test de non-changement du plan : avec le ``config.yaml`` du dépôt
    (où la nouvelle clé vaut ``2``), la valeur effective est exactement celle
    d'avant. La valeur attendue est RECALCULÉE depuis le disque et non codée en
    dur — si quelqu'un monte un jour le budget, ce test suit au lieu de casser."""
    cfg = _read_cfg()
    got = oc._upstream_max_retries()
    assert got == _legacy_expected(cfg), (
        f"G2 change le budget effectif sur la config du dépôt — attendu {_legacy_expected(cfg)}, obtenu {got}"
    )


def test_cle_nouvelle_presente_et_coherente_avec_lancielle():
    """La nouvelle clé existe bien dans ``upstream:`` et vaut la même chose que
    l'historique : les deux réglages restent alignés pendant la transition, si
    bien que retirer l'un ou l'autre ne change rien."""
    cfg = _read_cfg()
    nouveau = (cfg.get("upstream") or {}).get("max_retries")
    ancien = (cfg.get("streaming") or {}).get("retry_attempts")
    assert nouveau is not None, "upstream.max_retries absent de config.yaml"
    assert resolve_max_retries(nouveau) == resolve_max_retries(ancien)


# ─────────── Chaîne de résolution : nouvelle clé → historique → 2 ───────────


@pytest.fixture
def fake_yaml(monkeypatch):
    """Remplace la lecture config dans le namespace de l'hôte (même idiote que
    ``tests/test_suspect_tiny_output.py``) — sans toucher au fichier."""

    def _install(fake):
        monkeypatch.setattr(
            oc, "yaml_get", lambda section, key=None, default=None: (fake.get(section) or {}).get(key, default)
        )
        return fake

    return _install


def test_nouvelle_cle_gagne_sur_lhistorique(fake_yaml):
    fake_yaml({"upstream": {"max_retries": 4}, "streaming": {"retry_attempts": 2}})
    assert oc._upstream_max_retries() == 4


def test_historique_est_repris_si_nouvelle_cle_absente(fake_yaml):
    """Rétro-compatibilité : celui qui n'a réglé que l'ancienne clé garde sa
    valeur, sans avoir à migrer quoi que ce soit."""
    fake_yaml({"streaming": {"retry_attempts": 3}})
    assert oc._upstream_max_retries() == 3


def test_repli_sur_deux_si_aucune_cle(fake_yaml):
    fake_yaml({})
    assert oc._upstream_max_retries() == 2


@pytest.mark.parametrize("vide", [None, "", "   "])
def test_nouvelle_cle_vide_ne_masque_pas_lhistorique(fake_yaml, vide):
    """Une clé nouvelle présente mais vide (``max_retries:`` dans le YAML → `None`,
    ``max_retries: ''`` → chaîne vide) doit laisser l'historique s'exprimer, pas
    faire tomber sur ``2`` en dur. Un YAML à moitié rempli ne doit pas effacer
    silencieusement le réglage que l'utilisateur a mis sur l'ancienne clé."""
    fake_yaml({"upstream": {"max_retries": vide}, "streaming": {"retry_attempts": 5}})
    assert oc._upstream_max_retries() == 5


@pytest.mark.parametrize("valeur", [0, -3, 99, True, "beaucoup", {"a": 1}])
def test_valeur_absurde_en_config_est_normalisee_pas_levee(fake_yaml, valeur):
    """Config éditable à chaud : ``max_retries: 0`` ou ``max_retries: beaucoup``
    ne doit ni faire échouer la requête SANS appel amont (le 0 d'avant) ni
    échapper au plafond. Et JAMAIS lever — le helper est sur le chemin chaud."""
    fake_yaml({"upstream": {"max_retries": valeur}})
    got = oc._upstream_max_retries()
    assert got == resolve_max_retries(valeur)
    assert 1 <= got <= RETRY_BUDGET_MAX


def test_config_illisible_retombe_sur_le_budget_connu(fake_yaml, monkeypatch):
    """Même si la lecture de config explose, on rend l'ancien budget plutôt que
    de propager l'exception dans le handler de requête."""

    def boom(*a, **k):
        raise RuntimeError("config cassée")

    monkeypatch.setattr(oc, "yaml_get", boom)
    assert oc._upstream_max_retries() == 2


# ─────────── Gardes anti-révert partiel (les sites d'appel) ───────────


def _src():
    with open(oc.__file__, encoding="utf-8") as fh:
        return fh.read()


def test_un_seul_point_de_lecture_du_budget():
    """Le cœur de G2 : plus aucun site ne lit le budget lui-même. Les cinq
    appels (définition incluse) passent par le helper unique."""
    body = _src()
    assert body.count("_upstream_max_retries()") == 6, "5 sites + la définition attendus"


def test_lecture_historique_factorisee():
    """``streaming.retry_attempts`` n'est plus lu QUE dans le helper. S'il
    restait un site, monter le budget deviendrait de nouveau une chasse."""
    body = _src()
    assert body.count('yaml_get("streaming", "retry_attempts"') == 1, "la lecture historique a essaimé"
    assert body.count('yaml_get("upstream", "max_retries"') == 1


def test_plancher_jambe_libre_preserve_sur_les_quatre_sites():
    """LE piège du plan (lot I3). La borne de la jambe libre est
    ``max(budget, len(API_KEYS))`` puis += coups libres : quelle que soit la
    valeur du nouveau réglage, elle ne doit JAMAIS descendre sous le nombre de
    clés du pool. Chaque site doit donc garder son plancher sur la ligne qui
    suit immédiatement l'appel au helper."""
    lines = _src().splitlines()
    sites = [i for i, ln in enumerate(lines) if "_free_bound = _upstream_max_retries()" in ln]
    assert len(sites) == 4, f"{len(sites)} site(s) de borne libre, 4 attendus"
    for i in sites:
        suivante = lines[i + 1]
        assert "_free_bound = max(_free_bound, len(API_KEYS))" in suivante, (
            f"ligne {i + 2} : le plancher len(API_KEYS) a disparu du site de borne libre"
        )


def test_aucun_durcissement_deffective_free_max_attempts_touche():
    """La politique de délais propres à la jambe libre (0,5 s plafonné à 2 s,
    idle 120 s) vit ailleurs : G2 n'y touche pas. Verrouillage par constante —
    si un jour le budget commun se met à piloter ces délais, ce test casse."""
    body = _src()
    assert "effective_free_max_attempts" in body, "helper de coups libres jambe libre introuvable"
    assert "1 * 2**attempt" not in body  # la jambe libre n'utilise pas le backoff payant
