"""[Étape 2B] Split 403 DataPolicyError vs vrai-403 région (B1/B2).

Contrats runtime (opencode.py) :
  B1 — tout 403 paid passe par _check_datapolicy_guard() AVANT le message
       client : body DataPolicyError → _datapolicy_client_message() (cause
       exacte + URL d'opt-in complète + alias de compte), JAMAIS le texte
       générique « model/region may be restricted ». Vrai 403 (body sans
       DataPolicyError) → _auth_window_message(403) conservé tel quel.
  B2 — garde pré-forward : un 403 DataPolicyError ne doit NI retry same-key
       (_is_retriable_datapolicy() retourne toujours False — l'opt-in
       manquant ne se répare jamais par retry) NI basculer alt-key
       (requête condamnée : répondre immédiatement).
  C1-adjacent : _correlated_403_message() préfixe « free <modèle> →
       <free_status> (échec jambe free), paid fallback → » SEULEMENT si un
       contexte C1 existe pour ce req_id (requête réellement passée par le
       fallback), sinon message B1 inchangé (paid direct).

Sites d'émission couverts : 6 non-stream (_openai_error/_anthropic_error,
l.8313/9321/10805/11702/12668/12984) + 3 stream SSE (l.8728/11196/12021).
Ici : tests unitaires des briques (message + split + corrélation +
non-retry) — pas d'upstream, pas de VPN, pas de DB.

Réécriture Étape 2 : l'ancien test_datapolicy_retry.py (V4) assert
_is_retriable_datapolicy(...) is True — contredit l'impl unifiée
(return False, docstring B2/C3 l.6233-6247). Il est SUPPRIMÉ par cette
réécriture (fichier remplacé, pas dupliqué).
"""

import types

import pytest

import opencode as oc

OPTIN_URL = "https://opencode.ai/workspace/wrk_01KQQG13W80W15YQPS4CZGY9FM/go"
DP_BODY = (
    '{"type":"error","error":{"type":"DataPolicyError","message":'
    f'"This model collects data used to improve its quality and requires explicit opt in: {OPTIN_URL}"}}}}'
)


def _resp(code, text):
    return types.SimpleNamespace(
        status_code=code, text=text, headers={}, content=text.encode()
    )


# ── B1 : garde → message explicite (URL + alias), jamais texte région ──

def test_b1_datapolicy_guard_returns_explicit_message_with_url_and_alias():
    msg = oc._check_datapolicy_guard(_resp(403, DP_BODY), "test-acct")
    assert msg is not None
    assert OPTIN_URL in msg
    assert "test-acct" in msg
    assert "DataPolicyError" in msg
    assert "model/region may be restricted" not in msg


def test_b1_true_403_region_guard_returns_none_text_region_kept():
    assert oc._check_datapolicy_guard(_resp(403, "Region not allowed")) is None
    assert "model/region" in oc._auth_window_message(403)


def test_b1_optin_url_extraction_and_case_insensitivity():
    assert oc._datapolicy_optin_info(_resp(403, DP_BODY)) == OPTIN_URL
    # "illegal invocation" seul (sans URL) → garde active, placeholder URL
    msg = oc._check_datapolicy_guard(_resp(403, "ILLEGAL INVOCATION by third party"))
    assert msg is not None and "DataPolicyError" in msg
    # non-403 ou body vide → garde silencieuse
    assert oc._check_datapolicy_guard(_resp(429, DP_BODY)) is None
    assert oc._check_datapolicy_guard(_resp(200, DP_BODY)) is None
    assert oc._check_datapolicy_guard(_resp(403, "")) is None


# ── B2 : jamais retriable (garde pré-forward, pas retry aveugle) ──

@pytest.mark.parametrize(
    "code,text",
    [
        (403, '{"reason":"DataPolicyError"}'),
        (400, "DataPolicyError illegal invocation"),
        (403, "ILLEGAL INVOCATION by third party"),
        (403, "datapolicyerror"),
        (403, "Region not allowed"),
        (429, "DataPolicyError"),
        (200, "DataPolicyError"),
        (403, ""),
    ],
)
def test_b2_datapolicy_never_retriable(code, text):
    """Tout retry DataPolicyError est supprimé (B2/C3 unifié) : toujours False."""
    assert oc._is_retriable_datapolicy(_resp(code, text)) is False


# ── C1 : corrélation free→paid par req_id (préfixe conditionnel) ──

def test_c1_correlated_prefix_only_with_fallback_ctx():
    oc._fallback_ctx_push("req-b-corr", "muse-spark-1.3-contributor-free", 400)
    out = oc._correlated_403_message("UPSTREAM-MSG", "req-b-corr")
    assert out.startswith("free muse-spark-1.3-contributor-free → 400")
    assert "paid fallback → " in out
    assert out.endswith("UPSTREAM-MSG")


def test_c1_no_ctx_message_unchanged_paid_direct():
    out = oc._correlated_403_message("UPSTREAM-MSG", "req-b-noctx")
    assert out == "UPSTREAM-MSG"


# ── B1 bout-à-bout : split complet garde → message ──

def test_b1_end_to_end_split_datapolicy_vs_region():
    dp = oc._check_datapolicy_guard(_resp(403, DP_BODY), "acct-1") or oc._auth_window_message(403)
    assert OPTIN_URL in dp and "model/region may be restricted" not in dp
    region = oc._check_datapolicy_guard(_resp(403, "forbidden region XYZ"), "acct-1") or oc._auth_window_message(403)
    assert "model/region" in region and OPTIN_URL not in region
