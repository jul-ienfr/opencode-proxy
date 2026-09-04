"""test_b2_stream_double_failover.py — [Lot B2 / P6] failover stream au-delà de attempt==0.

Régression ciblée : l'ancien garde
`if attempt == 0 and len(API_KEYS) > 1 and status_code in (429,401,403)`
limitait le failover paid stream à la première tentative — une clé qui
tombait en 429 à attempt>=1 coupait le stream au lieu de basculer.
Le fix ([Lot B2] dans `_make_stream_retry_loop`) pause CHAQUE clé en échec
avant de chercher une alternative, donc chaque failover progresse vers une
clé fraîche quel que soit `attempt`.

Cas couverts :
  (a) 429 sur K1 à attempt=0 → headers K2, retry=True, K1 pausée
  (b) 429 sur K2 à attempt=1 → headers K3, retry=True, K2 pausée
      (LE cas B2 : l'ancien code retournait (headers, False) ici)
  (c) 429 sur K3 à attempt=2 → (headers, False), K3 pausée (épuisement)
  (d) 401 sur K1 à attempt=1 → headers K2, retry=True (cohérence
      §14.1.15 v10 : même pause stream/non-stream)

Never touches the live system: quota fetch stubbé (pas de réseau), pauser
frais par test, API_KEYS monkeypatchées.
"""

import pytest

import opencode as oc

K1 = "sk-ant-b2-key-AAAAAAAAAAAAAAAAAAAAAAAA"
K2 = "sk-ant-b2-key-BBBBBBBBBBBBBBBBBBBBBBBB"
K3 = "sk-ant-b2-key-CCCCCCCCCCCCCCCCCCCCCCCC"


@pytest.fixture
def three_paid_keys(monkeypatch):
    """3 clés paid, pauser frais, quota-fetch stubbé (pause directe 60 s)."""
    monkeypatch.setattr(
        oc,
        "API_KEYS",
        [
            {"api_key": K1, "alias": "b2-k1", "enabled": True},
            {"api_key": K2, "alias": "b2-k2", "enabled": True},
            {"api_key": K3, "alias": "b2-k3", "enabled": True},
        ],
    )
    monkeypatch.setattr(oc, "_key_pauser", oc._KeyPauser())
    oc._rebuild_key_cache()

    async def _fake_pause(api_key: str):
        oc._key_pauser.pause_key(api_key, 60, "429 (test B2)")

    monkeypatch.setattr(oc, "_pause_key_for_quota_reset", _fake_pause)
    return oc


def _h(key):
    return oc._get_auth_headers("anthropic", entry={"api_key": key})


@pytest.mark.asyncio
async def test_b2_double_failover_progresses_to_fresh_keys(three_paid_keys):
    """(a/b/c) : K1→K2 (attempt 0), K2→K3 (attempt 1 = cas B2), puis False."""
    loop = oc._make_stream_retry_loop("anthropic")

    h2, retry1 = await loop(_h(K1), 429, 0)
    assert retry1 is True
    assert h2.get("x-api-key") == K2
    assert oc._key_pauser.is_paused(K1)

    # LE cas B2 : attempt=1 — l'ancien garde `attempt == 0` retournait False.
    h3, retry2 = await loop(h2, 429, 1)
    assert retry2 is True
    assert h3.get("x-api-key") == K3
    assert oc._key_pauser.is_paused(K2)

    # Épuisement : plus d'alternative fraîche → pas de retry, K3 pausée.
    h4, retry3 = await loop(h3, 429, 2)
    assert retry3 is False
    assert oc._key_pauser.is_paused(K3)


@pytest.mark.asyncio
async def test_b2_401_failover_beyond_attempt_zero(three_paid_keys):
    """(d) : 401 à attempt=1 bascule aussi (pause KEY_PAUSE_401_SEC)."""
    loop = oc._make_stream_retry_loop("anthropic")

    h2, retry = await loop(_h(K1), 401, 1)
    assert retry is True
    assert h2.get("x-api-key") == K2
    assert oc._key_pauser.is_paused(K1)
