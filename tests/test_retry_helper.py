"""P0-5 — upstream.retry : parse Retry-After + backoff + jitter.

+ [PLAN_ALIGNEMENT_CLIENT_OFFICIEL G2] budget de reprises
(``resolve_max_retries``) : normalise la valeur lue en config sans jamais
lever et sans jamais descendre sous 1 tentative.
"""

import email.utils
import time

import pytest

from upstream.retry import RETRY_BUDGET_MAX, compute_retry_delay, parse_retry_after, resolve_max_retries


def test_parse_seconds_ok():
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(30) == 30.0
    assert parse_retry_after("  5 ") == 5.0


def test_parse_rejects_garbage_and_out_of_range():
    assert parse_retry_after("") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("nonsense") is None
    assert parse_retry_after("0") is None
    assert parse_retry_after("-3") is None
    assert parse_retry_after("601") is None  # > max_s défaut 600
    assert parse_retry_after("61", max_s=60.0) is None  # borne free-direct
    assert parse_retry_after("60", max_s=60.0) == 60.0


def test_parse_http_date():
    future = email.utils.formatdate(time.time() + 45, usegmt=True)
    got = parse_retry_after(future)
    assert got is not None and 0 < got <= 45.5
    past = email.utils.formatdate(time.time() - 60, usegmt=True)
    assert parse_retry_after(past) is None


def test_backoff_exponential_no_jitter():
    assert compute_retry_delay(0, jitter=False) == 1.0
    assert compute_retry_delay(1, jitter=False) == 2.0
    assert compute_retry_delay(2, jitter=False) == 4.0
    assert compute_retry_delay(10, jitter=False) == 600.0  # cap
    assert compute_retry_delay(-1, jitter=False) == 1.0


def test_retry_after_wins_over_backoff():
    assert compute_retry_delay(0, retry_after=42.0, jitter=False) == 42.0
    # consigne farfelue → plafonnée au cap, pas de blocage worker
    assert compute_retry_delay(0, retry_after=9999.0, jitter=False) == 600.0


def test_jitter_bounds():
    for _ in range(200):
        w = compute_retry_delay(1)  # base 2.0
        assert 1.5 <= w < 2.5


# ─────────── G2 : budget de reprises (resolve_max_retries) ───────────


def test_absent_et_none_prennent_le_defaut():
    """Clé absente (``None``) ou chaîne vide → le budget d'aujourd'hui. C'est
    la rétro-compatibilité de G2 : tant que ``upstream.max_retries`` n'est pas
    écrit, la valeur effective est EXACTEMENT l'ancienne."""
    assert resolve_max_retries(None) == 2
    assert resolve_max_retries(None, default=4) == 4
    assert resolve_max_retries("") == 2
    assert resolve_max_retries("   ") == 2


def test_entier_et_chaine_numerique_sont_acceptes():
    assert resolve_max_retries(3) == 3
    assert resolve_max_retries("3") == 3
    assert resolve_max_retries(3.0) == 3
    assert resolve_max_retries(" 3.7 ") == 3  # int(floor(3.7)) — arrondi vers le bas
    assert resolve_max_retries(1) == 1


@pytest.mark.parametrize("raw", [True, False, "yes", "on", "off"])
def test_booleen_retombe_sur_le_defaut(raw):
    """YAML 1.1 lit ``yes`` / ``on`` en ``True``, et ``True`` est un ``int`` en
    Python (il vaut 1). Prendre un booléen pour sa valeur entière transformerait
    un « active les reprises » tapé trop vite en « une seule tentative, aucun
    ré-essai » — exactement l'inverse de l'intention."""
    assert resolve_max_retries(raw) == 2
    assert resolve_max_retries(raw, default=3) == 3


def test_zero_et_negatif_remontent_a_une_tentative():
    """``retry_attempts: 0`` produisait ``while attempt < 0`` / ``range(0)`` :
    la requête client échouait SANS qu'aucun appel amont ne parte. Une tentative
    minimum est le seul comportement défendable — jamais zéro."""
    assert resolve_max_retries(0) == 1
    assert resolve_max_retries(-5) == 1
    assert resolve_max_retries("-5") == 1
    assert resolve_max_retries(0.0) == 1


def test_valeur_excessive_est_plafonnee():
    """La jambe libre DÉRIVE sa borne de ce budget
    (``max(budget, len(API_KEYS))`` + coups libres) : sans plafond, une faute de
    frappe (``500``) multiplierait les connexions amont par requête client.
    Le plafond ne fait que BAISSER une valeur absurde, jamais monter la nôtre."""
    assert resolve_max_retries(10**6) == RETRY_BUDGET_MAX
    assert resolve_max_retries(50) == RETRY_BUDGET_MAX
    assert resolve_max_retries(RETRY_BUDGET_MAX) == RETRY_BUDGET_MAX
    assert resolve_max_retries(RETRY_BUDGET_MAX + 1) == RETRY_BUDGET_MAX


@pytest.mark.parametrize("bad", [{}, [], object(), {"max_retries": 3}, "abc", "3,5", float("nan"), float("inf"), b"3"])
def test_entrees_illisibles_retombent_sur_le_defaut(bad):
    """Config éditable à chaud : n'importe quelle valeur peut s'y trouver
    (dict, liste, objet, texte non numérique). Mieux vaut l'ancien budget, connu,
    qu'une valeur qu'on ne comprend pas — et dans tous les cas, JAMAIS un raise
    (le helper est appelé sur le chemin de la requête)."""
    assert resolve_max_retries(bad) == 2
    assert resolve_max_retries(bad, default=5) == 5


@pytest.mark.parametrize("bad_default", [None, 0, -1, "abc", True, 10**6])
def test_defaut_lui_meme_absurde_est_normalise(bad_default):
    """Le défaut passé par l'appelant est borné de la même façon : ``0``
    deviendrait une jambe sans ré-essai ET sans appel amont."""
    got = resolve_max_retries(None, default=bad_default)
    assert 1 <= got <= RETRY_BUDGET_MAX


def test_jamais_de_raise_sur_quelque_entree_que_ce_soit():
    """Garde finale : la normalisation ne lève sur aucune entrée, sinon la
    lecture de la config deviendrait elle-même une 500."""
    for raw in (None, True, 0, -1, 10**9, "abc", {}, [], object(), float("nan"), bytes(3)):
        for default in (2, None, 0, "x", True):
            assert isinstance(resolve_max_retries(raw, default=default), int)
