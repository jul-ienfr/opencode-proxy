"""Régression 2026-10-29 — le 429 « quota épuisé » ne doit pas être fabriqué.

Deux défauts mesurés en direct :

1. `big-pickle` (gratuit **sans suffixe `-free`**) avait une chaîne de
   candidats vide (`paid == target` n'était pas reconnu). Le garde
   `strict_free` concluait « toutes les stations épuisées » puis attendait
   ~45 s avant de rendre un 429, **sans aucun appel upstream**, alors que les
   6 stations étaient disponibles.
2. Même effet pour un mapping explicite `glm-5.1 → glm-5.3-flash-free` :
   `_resolve_free_model` le retenait, mais `_free_candidate_chain` le
   re-filtrait par la politique et rendait `[]`. Les alias Claude
   (`sonnet → glm-5.1`) tombaient donc en 429/refus au lieu de servir le free.

Le contrat verrouillé ici : la chaîne contient toujours la cible résolue, et
le garde ne refuse jamais sur une chaîne vide (fail-open vers la tentative).
"""

import pytest
from test_free_multi_attempt import (  # noqa: F401  (fixtures ré-exportées)
    _FakeFreeCurl,
    _FakeResp,
    _PoolMulti,
    _Station,
    free_cfg,  # noqa: F401
    free_vpn_env,  # noqa: F401
)

import opencode as oc
from config.free_policy import target_allowed


def test_self_target_of_unsuffixed_free_model_is_allowed():
    """`big-pickle` est sa propre cible : `paid == target` autorisé."""
    assert target_allowed("big-pickle", "big-pickle", "homonym") is True
    assert target_allowed("deepseek-v4.1-flash-free", "deepseek-v4.1-flash-free", "homonym") is True
    assert target_allowed("glm-5.1", "glm-5.3-flash-free", "homonym") is False


@pytest.mark.usefixtures("free_vpn_env")
def test_candidate_chain_always_contains_the_resolved_base(monkeypatch):
    """La cible explicite/résolue reste en tête, même hors politique."""
    monkeypatch.setattr(oc, "FREE_MODEL_MAP", {"glm-5.1": "glm-5.3-flash-free"})
    monkeypatch.setattr(oc._cfg_settings, "get_free_substitution_policy", lambda: "homonym")
    assert oc._free_candidate_chain("glm-5.1", "glm-5.3-flash-free") == ["glm-5.3-flash-free"]

    # Modèle free sans suffixe : la chaîne n'est pas vide.
    monkeypatch.setattr(oc, "FREE_MODEL_MAP", {})
    assert oc._free_candidate_chain("big-pickle", "big-pickle") == ["big-pickle"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("free_vpn_env", "free_cfg")
async def test_unsuffixed_free_model_is_attempted_not_refused(monkeypatch):
    """`big-pickle` doit appeler l'upstream, jamais rendre un 429 préfabriqué."""
    oc.IP_ROTATION["strict_free"] = True
    oc.IP_ROTATION["auto_max_free_attempts"] = False
    oc.IP_ROTATION["max_free_attempts"] = 2
    monkeypatch.setattr(oc, "FREE_MODEL_MAP", {})
    monkeypatch.setattr(oc._cfg_settings, "FREE_MODELS", {"big-pickle"})
    monkeypatch.setattr(oc, "MODELS", {"big-pickle": {"protocol": "openai"}})
    pool = _PoolMulti(
        [
            _Station(1, "10.0.0.1", "socks5://s1"),
            _Station(2, "10.0.0.2", "socks5://s2"),
        ]
    )
    monkeypatch.setattr(oc, "_free_ip_pool", pool)
    fake = _FakeFreeCurl(
        [
            _FakeResp(
                200,
                {"content-type": "application/json"},
                usage={"prompt_tokens": 3, "completion_tokens": 5},
            )
        ]
    )
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", fake)

    async def _must_not_wait(*_a, **_k):
        raise AssertionError("strict_free a refusé sur une chaîne vide au lieu de tenter l'upstream")

    monkeypatch.setattr(oc, "_wait_for_fresh_free", _must_not_wait)

    out = await oc._try_free_model_first(
        {"model": "big-pickle", "messages": [{"role": "user", "content": "hello"}]},
        {},
        "openai",
        "big-pickle",
        req_id="req-big-pickle-chain",
    )

    assert out is not None, "la tentative free aurait dû réussir"
    assert fake.calls, "aucun appel upstream : le 429 a encore été fabriqué avant la tentative"
    assert pool.requests == 1
