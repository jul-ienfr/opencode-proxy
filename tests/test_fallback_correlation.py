"""test_fallback_correlation.py — [Étape 2C] corrélation fallback free→paid (C1).

Périmètre STRICTEMENT C1 (one-shot + LAST + trim). Ne duplique PAS :
  - B-TEST (tests/test_403_split_datapolicy.py) : split datapolicy/région,
    garde pré-forward, _is_retriable False, préfixe corrélé de base,
    message inchangé sans ctx ;
  - A-TEST (tests/test_no_valid_keys_guard.py) : vérité _has_usable_paid_key,
    distinction AllKeysPaused free-direct vs placeholder (A3/C2) ;
  - O3-TEST (tests/test_o3_fallback_metrics.py) : compteurs par cause.

Contrats runtime (opencode.py) :
  C1/one-shot : _fallback_ctx_push au swap free→paid, _correlated_403_message
    poppe (destructif) à l'émission B1 → 2e appel inchangé (l.6355-6371).
  C1/LAST : le pop copie dans _FALLBACK_CTX_LAST (O2) pour le peek
    persistance de _save_request (l.1025, SANS pop) — fige la copie isolée
    (dict, pas alias) + le câblage du peek (inspect, sans DB).
  C1/mémoire : _fallback_ctx_trim borne les deux registres à
    _FALLBACK_CTX_MAX.

Never touches the live system : pas d'upstream, pas de VPN, pas de DB
(le câblage _save_request est vérifié par inspect.getsource, jamais appelé).
"""

import inspect

import opencode as oc
from test_free_multi_attempt import (  # noqa: F401  (doubles partagés, pattern test_free_vpn_required.py)
    FREE_MODEL,
    free_cfg,  # noqa: F401  (fixture ré-exportée)
    free_vpn_env,  # noqa: F401
)

import pytest


@pytest.fixture
def _clean_ctx():
    """Isole _FALLBACK_CTX/_FALLBACK_CTX_LAST (save/clear/restore)."""
    saved = (dict(oc._FALLBACK_CTX), dict(oc._FALLBACK_CTX_LAST))
    oc._FALLBACK_CTX.clear()
    oc._FALLBACK_CTX_LAST.clear()
    try:
        yield oc
    finally:
        oc._FALLBACK_CTX.clear()
        oc._FALLBACK_CTX_LAST.clear()
        oc._FALLBACK_CTX.update(saved[0])
        oc._FALLBACK_CTX_LAST.update(saved[1])


def test_c1_push_400_pop_prefixes_once_then_gone(_clean_ctx):
    """C1/one-shot : push(400) → préfixe free→paid UNE fois, ctx consommé."""
    oc._fallback_ctx_push("req-c-corr", FREE_MODEL, 400)
    assert "req-c-corr" in oc._FALLBACK_CTX
    out = oc._correlated_403_message("UPSTREAM-MSG", "req-c-corr")
    assert out == f"free {FREE_MODEL} → 400 (échec jambe free), paid fallback → UPSTREAM-MSG"
    # Pop destructif : le registre live est vide pour ce req_id…
    assert "req-c-corr" not in oc._FALLBACK_CTX
    # …donc le 2e appel ne préfixe plus (one-shot, pas de re-préfixe).
    assert oc._correlated_403_message("SECOND", "req-c-corr") == "SECOND"


def test_c1_last_isolated_copy_and_save_request_peek_wired(_clean_ctx):
    """C1/O2 : après le pop d'émission, LAST garde une COPIE isolée, et le
    peek persistance de _save_request est câblé dessus (sans DB)."""
    oc._fallback_ctx_push("req-c-last", "free-m-b", 500)
    popped = oc._fallback_ctx_pop("req-c-last")
    assert popped == {"free_model": "free-m-b", "free_status": 500}
    # Registre live vide (le pop a consommé)…
    assert "req-c-last" not in oc._FALLBACK_CTX
    # …mais LAST conserve la valeur pour la persistance…
    kept = oc._FALLBACK_CTX_LAST.get("req-c-last")
    assert kept == {"free_model": "free-m-b", "free_status": 500}
    # …et c'est une copie isolée, pas un alias du dict poppé.
    assert kept is not popped
    popped["free_status"] = 999
    assert oc._FALLBACK_CTX_LAST["req-c-last"]["free_status"] == 500
    # Câblage O2 : _save_request resolt free_status via LAST (peek sans pop).
    # Vérifié par source (pas d'appel DB) — casse si le peek est supprimé.
    src = inspect.getsource(oc._save_request)
    assert "_FALLBACK_CTX_LAST" in src
    assert "_FALLBACK_CTX.get" in src


def test_c1_trim_bounds_registries_at_max(_clean_ctx):
    """C1/mémoire : _fallback_ctx_trim borne les deux registres à _FALLBACK_CTX_MAX."""
    n = oc._FALLBACK_CTX_MAX + 3
    for i in range(n):
        oc._FALLBACK_CTX[f"old-{i}"] = {"free_model": "m", "free_status": 400}
        oc._FALLBACK_CTX_LAST[f"old-{i}"] = {"free_model": "m", "free_status": 400}
    oc._fallback_ctx_trim()
    assert len(oc._FALLBACK_CTX) == oc._FALLBACK_CTX_MAX
    assert len(oc._FALLBACK_CTX_LAST) == oc._FALLBACK_CTX_MAX
    assert "old-0" not in oc._FALLBACK_CTX and "old-0" not in oc._FALLBACK_CTX_LAST
