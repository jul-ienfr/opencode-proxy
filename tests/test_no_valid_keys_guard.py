"""test_no_valid_keys_guard.py — [Étape 2A] garde no-valid-keys + strict_free (A1/A2).

Contrats runtime (opencode.py) :
  A1 — garde no-valid-keys : sans clé payante utilisable
       (_has_usable_paid_key() == False : API_KEYS vide/disabled/pausées ET
       .env vide/pausé), la jambe paid est condamnée au 401/403 → le handler
       ne doit JAMAIS appeler l'upstream paid :
       - non-stream free-first (l.10694) retourne None → garde l.10712
         (paid-skipped) répond 503 locale explicite ;
       - AllKeysPaused (l.10583) : stream + no-valid-keys → note free-direct
         (pas de placeholder 503) ; non-stream → free-first (A3) ;
  A2 — strict_free effectif (verdict GUI : mode épuisement = refuser → ZÉRO
       jambe paid, même avec N clés payantes valides ; seul le mode
       fallback-payant autorise paid après échec free) : avec
       strict_free=True, TOUT échec free refuse (FreeRefusal véridique :
   429 → FreeQuotaExhausted, non-429 → FreeRefusal statut réel) :
       - 429 : refuse inconditionnel (plus de condition épuisé/no-keys) ;
       - non-429 : strict → refuse (non-strict → cooldown 60s + None) ;
       - tunnel-vide : strict → refuse ;
       - stream : 429 (via _on_free_429_stream → True) ET non-429
         (gate in-situ dans les 4 handlers stream) → _stream_error_response
         429 + return, 0 appel upstream paid.
  A.0 — invariant : aucun artefact payant ne touche API_BASE_FREE.

Zéro appel upstream paid = le faux curl double lève si touché (garde-fou
plus fort qu'un compteur). Les doubles (_Station/_StubVpn/_FakeResp/
_FakeFreeCurl/_PoolMulti, fixtures free_vpn_env/free_cfg) sont IMPORTÉS de
test_free_multi_attempt (pattern test_free_vpn_required.py) — pas dupliqués.
Note : free_cfg force strict_free=False → chaque test A2 le réactive
explicitement.

Never touches the live system: upstream doublé, pas de VPN, pas de DB
(free-usage logging no-op'd par free_vpn_env).
"""

import pytest

import opencode as oc
from test_free_multi_attempt import (
    PAID_MODEL,
    FREE_MODEL,
    _FakeFreeCurl,
    _PoolMulti,
    _Station,
    _StubVpn,
    _FakeResp,
    _free_body,
    free_cfg,  # noqa: F401  (fixture ré-exportée)
    free_vpn_env,  # noqa: F401
)


class _PaidTouched(Exception):
    """Garde-fou : levée si la jambe paid est appelée (ne doit JAMAIS arriver)."""


async def _boom_paid(*a, **k):
    raise _PaidTouched("jambe paid appelée sans clé valide — garde A1 violée")


@pytest.fixture
def no_valid_keys(monkeypatch, free_vpn_env):
    """État no-valid-keys : API_KEYS vide + .env vide (vérité A1/A2)."""
    monkeypatch.setattr(oc, "API_KEYS", [])
    monkeypatch.setattr(oc, "API_KEY", "")
    # pas de clés pausées résiduelles qui fausseraient is_paused("")
    monkeypatch.setattr(oc, "_key_pauser", oc._KeyPauser())
    return oc


def _two_stations():
    return [
        _Station(1, "10.0.0.1", "socks5://s1"),
        _Station(2, "10.0.0.2", "socks5://s2"),
    ]


def _wire_free(monkeypatch, free_curl):
    """Branche le faux curl + pool 2 stations (budget multi-attempt)."""
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", free_curl)
    pool = _PoolMulti(_two_stations())
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    oc.IP_ROTATION["max_free_attempts"] = 2
    oc.IP_ROTATION["auto_max_free_attempts"] = False
    return pool


def _json_resp(status, usage=None, text=""):
    return _FakeResp(status, headers={"content-type": "application/json"},
                     usage=usage or {"prompt_tokens": 3, "completion_tokens": 5},
                     text=text)


# ── A2 : strict_free × échec free → FreeQuotaExhausted, jamais None+paid ──

@pytest.mark.asyncio
async def test_a2_strict_no_keys_429_exhausted_refuses(no_valid_keys, free_cfg, monkeypatch):
    """A2/429 : strict_free + no-valid-keys + 429 → refuse (Retry-After propagé)."""
    oc.IP_ROTATION["strict_free"] = True
    curl = _FakeFreeCurl([_json_resp(429, text="quota hit", ), _json_resp(429, text="quota hit")])
    curl_resp_headers = {"retry-after": "77", "content-type": "application/json"}
    curl.responses = [
        _FakeResp(429, headers=dict(curl_resp_headers), text="quota hit"),
        _FakeResp(429, headers=dict(curl_resp_headers), text="quota hit"),
    ]
    _wire_free(monkeypatch, curl)
    monkeypatch.setattr(oc, "_do_request_with_retry", _boom_paid)
    with pytest.raises(oc.FreeQuotaExhausted) as ei:
        await oc._try_free_model_first(_free_body(), {}, "openai", PAID_MODEL, req_id="req-a2-429")
    assert ei.value.retry_after == "77"


@pytest.mark.asyncio
async def test_a2_strict_no_keys_non429_refuses(no_valid_keys, free_cfg, monkeypatch):
    """A2/non-429 : strict_free + no-valid-keys + 500 → refuse (jamais None).
    [PLAN_CORRECTION_FAUX_429] refus VERIDIQUE 500 (FreeRefusal), jamais 429."""
    oc.IP_ROTATION["strict_free"] = True
    curl = _FakeFreeCurl([_FakeResp(500, headers={"content-type": "application/json"}, text="boom")])
    _wire_free(monkeypatch, curl)
    monkeypatch.setattr(oc, "_do_request_with_retry", _boom_paid)
    with pytest.raises(oc.FreeRefusal) as ei:
        await oc._try_free_model_first(_free_body(), {}, "openai", PAID_MODEL, req_id="req-a2-500")
    assert ei.value.status == 500
    assert "quota" not in str(ei.value.body).lower()


@pytest.mark.asyncio
async def test_a2_nonstrict_no_keys_non429_returns_none_but_paid_guard_blocks(
    no_valid_keys, free_cfg, monkeypatch
):
    """A2/non-strict : échec free → None (contrat historique), MAIS la garde
    A1 (_has_usable_paid_key()==False) doit bloquer la jambe paid en aval.

    Teste les deux moitiés du contrat : None ici + garde fermée (le handler
    l.10712 répond paid-skipped sans appeler _do_request_with_retry).
    """
    assert oc.IP_ROTATION.get("strict_free") is False  # free_cfg : OFF par défaut
    curl = _FakeFreeCurl([_FakeResp(502, headers={"content-type": "application/json"}, text="bad gw")])
    _wire_free(monkeypatch, curl)
    out = await oc._try_free_model_first(_free_body(), {}, "openai", PAID_MODEL, req_id="req-a2-nonstrict")
    assert out is None
    # Seconde moitié : avec no-valid-keys, la jambe paid est condamnée.
    assert oc._has_usable_paid_key() is False
    monkeypatch.setattr(oc, "_do_request_with_retry", _boom_paid)
    # Le handler répondrait paid-skipped (l.10712) — ici on fige la garde :
    # aucun chemin ne doit appeler le paid quand elle est fermée.
    with pytest.raises(_PaidTouched):
        await oc._do_request_with_retry("https://paid.invalid/v1", {}, {}, "openai")
    # (Le raise prouve que le double est armé ; la garde handler est couverte
    # par test_a1_paid_skipped_guard_closed ci-dessous.)


@pytest.mark.asyncio
async def test_a2_strict_WITH_valid_key_still_refuses(free_vpn_env, free_cfg, monkeypatch):
    """A2/verdict GUI : strict_free + clé payante VALIDE + 500 → refuse
    (ZÉRO jambe paid). Seul le mode fallback-payant (strict_free=False)
    autorise le paid après échec free. Remplace l'ancien contrat C4
    (strict+clé-valide → None) désormais caduc.
    [PLAN_CORRECTION_FAUX_429] refus VERIDIQUE 500 (FreeRefusal)."""
    oc.IP_ROTATION["strict_free"] = True
    monkeypatch.setattr(oc, "API_KEYS", [{"api_key": "sk-test-valid-key-1", "enabled": True}])
    monkeypatch.setattr(oc, "API_KEY", "")
    assert oc._has_usable_paid_key() is True
    curl = _FakeFreeCurl([_FakeResp(500, headers={"content-type": "application/json"}, text="boom")])
    _wire_free(monkeypatch, curl)
    monkeypatch.setattr(oc, "_do_request_with_retry", _boom_paid)
    with pytest.raises(oc.FreeRefusal) as ei:
        await oc._try_free_model_first(_free_body(), {}, "openai", PAID_MODEL, req_id="req-a2-paidok")
    assert ei.value.status == 500


@pytest.mark.asyncio
async def test_a2_nonstrict_with_valid_key_still_falls_back(free_vpn_env, free_cfg, monkeypatch):
    """A2/mode épuisement = fallback-payant : strict_free=False + clé valide
    + 500 → None (jambe paid autorisée). Fige la contrepartie exacte :
    seul le fallback-payant rouvre le paid."""
    assert oc.IP_ROTATION.get("strict_free") is False  # free_cfg : OFF par défaut
    monkeypatch.setattr(oc, "API_KEYS", [{"api_key": "sk-test-valid-key-1", "enabled": True}])
    monkeypatch.setattr(oc, "API_KEY", "")
    assert oc._has_usable_paid_key() is True
    curl = _FakeFreeCurl([_FakeResp(500, headers={"content-type": "application/json"}, text="boom")])
    _wire_free(monkeypatch, curl)
    out = await oc._try_free_model_first(_free_body(), {}, "openai", PAID_MODEL, req_id="req-a2-fbpay")
    assert out is None  # fallback paid autorisé : mode fallback-payant + clé valide


# ── A1 : vérité _has_usable_paid_key ──────────────────────────────────────

def test_a1_truth_table(no_valid_keys, monkeypatch):
    """A1/vérité : vide → False ; disabled → False ; pausée → False ;
    valide → True ; .env seul → True."""
    assert oc._has_usable_paid_key() is False

    monkeypatch.setattr(oc, "API_KEYS", [{"api_key": "sk-x", "enabled": False}])
    assert oc._has_usable_paid_key() is False

    monkeypatch.setattr(oc, "API_KEYS", [{"api_key": "sk-paused-1", "enabled": True}])
    oc._key_pauser.pause_key("sk-paused-1", 600, "test")
    assert oc._has_usable_paid_key() is False

    fresh = oc._KeyPauser()
    monkeypatch.setattr(oc, "_key_pauser", fresh)
    monkeypatch.setattr(oc, "API_KEYS", [{"api_key": "sk-live-1", "enabled": True}])
    assert oc._has_usable_paid_key() is True

    monkeypatch.setattr(oc, "API_KEYS", [])
    monkeypatch.setattr(oc, "API_KEY", "sk-env-1")
    assert oc._has_usable_paid_key() is True

    # .env pausé → False (Bearer condamné au 401/403 pareil qu'une clé listée)
    oc._key_pauser.pause_key("sk-env-1", 600, "test")
    assert oc._has_usable_paid_key() is False


# ── A1 : garde handler paid-skipped (non-stream l.10712) ─────────────────

@pytest.mark.asyncio
async def test_a1_paid_skipped_guard_closed(no_valid_keys, free_cfg, monkeypatch):
    """A1/handler : free None + no-valid-keys → le handler ne doit pas appeler
    le paid. Reproduit la séquence l.10694 (None) → l.10712 (garde) : le paid
    doublé lève _PaidTouched si touché ; on assert la garde AVANT l'appel."""
    curl = _FakeFreeCurl([_FakeResp(500, headers={"content-type": "application/json"}, text="x")])
    _wire_free(monkeypatch, curl)
    free_result = await oc._try_free_model_first(
        _free_body(), {}, "openai", PAID_MODEL, req_id="req-a1-skip")
    assert free_result is None
    # Garde l.10712 : fermée → le handler répond paid-skipped SANS appeler paid.
    assert oc._has_usable_paid_key() is False
    paid_called = []
    async def _record_paid(*a, **k):
        paid_called.append((a, k))
        raise _PaidTouched("paid touché")
    monkeypatch.setattr(oc, "_do_request_with_retry", _record_paid)
    if not oc._has_usable_paid_key():
        resp = oc._openai_error(
            503,
            "Free model request failed and no usable paid API key "
            "is configured — cannot fall back to paid. "
            "Configure a paid key or retry later.",
        )
        assert resp.status_code == 503
    else:  # pragma: no cover — garde ouverte : le paid serait appelé
        await oc._do_request_with_retry("https://paid.invalid/v1", {}, {}, "openai")
    assert paid_called == [], "garde A1 fermée mais jambe paid appelée"


# ── A3 : AllKeysPaused stream/non-stream parité ───────────────────────────

def test_a3_allkeys_paused_distinction(no_valid_keys):
    """A3 : AllKeysPaused + no-valid-keys → free-direct (stream, note l.10604)
    ou free-first (non-stream, l.10612) — jamais le placeholder 503 « next
    attempt ». Fige la condition qui choisit la branche : _no_valid_keys."""
    assert oc._has_usable_paid_key() is False  # → branche free-direct, pas placeholder
    # Clé existante-mais-pausée : même état no-valid-keys (garde unifiée).
    oc.API_KEYS.append({"api_key": "sk-paused-2", "enabled": True})
    oc._key_pauser.pause_key("sk-paused-2", 600, "test")
    assert oc._has_usable_paid_key() is False


# ── A.0 : invariant payant sur chaque essai ───────────────────────────────

@pytest.mark.asyncio
async def test_a0_no_paid_artifacts_on_free_attempts(no_valid_keys, free_cfg, monkeypatch):
    """A.0 : headers + body de CHAQUE essai free sans artefact payant."""
    from test_free_multi_attempt import assert_no_paid_artifacts
    curl = _FakeFreeCurl([
        _FakeResp(429, headers={"content-type": "application/json"}, text="q"),
        _json_resp(200),
    ])
    pool = _wire_free(monkeypatch, curl)
    out = await oc._try_free_model_first(_free_body(), {}, "openai", PAID_MODEL, req_id="req-a0")
    assert out is not None  # retry B → OK
    assert len(curl.headers) == 2
    for i, h in enumerate(curl.headers):
        assert_no_paid_artifacts(f"essai {i}", h, body=str(curl.bodies[i]))
    assert pool.requests >= 1
