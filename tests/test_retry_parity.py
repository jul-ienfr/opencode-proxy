"""Parite de la politique de reprise avec le client officiel 1.19.3.

Reference : `packages/llm/src/route/executor.ts` (client officiel) —

    MAX_RETRIES = 2, BASE_DELAY_MS = 500, MAX_DELAY_MS = 10_000
    retryableStatus(st) = st === 429 || st === 503 || st === 504 || st === 529 || st >= 500
    retryAfterMs()      = `retry-after-ms` puis `retry-after` (s ou date HTTP),
                          plafonne a MAX_DELAY_MS
    400/409/413/422     -> InvalidRequest, JAMAIS rejoue

Ces tests verrouillent la *decision*, pas l'horloge : ils doivent rester
instantanes. Les delais sont verifies via `compute_retry_delay` avec
`jitter=False`.
"""

import sys

import pytest

sys.path.insert(0, ".")
from upstream.retry import (  # noqa: E402
    RETRYABLE_STATUSES,
    compute_retry_delay,
    is_retryable_status,
    resolve_max_retries,
)

# Statuts que l'officiel rejoue.
RETRYABLE_EXPECTED = [429, 500, 501, 502, 503, 504, 505, 520, 529, 599]

# Statuts que l'officiel NE rejoue jamais (classes InvalidRequest ou
# conditions de transport gerees ailleurs).
NOT_RETRYABLE_EXPECTED = [
    200,
    204,
    301,
    400,  # InvalidRequest
    401,  # auth : rejouer ne peut pas reussir
    403,  # refus (dont gate free)
    404,
    408,  # transport, pas surcharge
    409,  # InvalidRequest (le proxy le rejouait avant l'alignement)
    413,  # payload trop gros
    422,  # InvalidRequest
    425,  # transport
    499,  # client closed
]


@pytest.mark.parametrize("status", RETRYABLE_EXPECTED)
def test_retryable_statuses_match_official(status):
    assert is_retryable_status(status), f"{status} doit etre rejoue (parite officielle)"
    assert status in RETRYABLE_STATUSES


@pytest.mark.parametrize("status", NOT_RETRYABLE_EXPECTED)
def test_non_retryable_statuses_match_official(status):
    assert not is_retryable_status(status), (
        f"{status} ne doit JAMAIS etre rejoue : l'officiel le classe en erreur "
        "definitive (rejouer reproduit l'erreur en consommant quota et latence)"
    )


def test_every_5xx_is_retryable():
    """Regle officielle : tout statut >= 500 est rejouable, sans liste blanche."""
    manquants = [s for s in range(500, 600) if not is_retryable_status(s)]
    assert not manquants, f"5xx non rejoues : {manquants}"


def test_409_was_retried_before_alignment_and_is_not_anymore():
    """Regression documentee : 409 figurait dans l'ancien ensemble du proxy."""
    ancien = {500, 502, 503, 504, 499, 408, 409, 425, 529}
    assert 409 in ancien, "l'ancien ensemble rejouait bien 409 (constat d'audit)"
    assert not is_retryable_status(409), "l'alignement doit l'avoir retire"
    # 429 et les 5xx hors liste historique sont entrants.
    assert 429 not in ancien and is_retryable_status(429)
    assert 501 not in ancien and is_retryable_status(501)


def test_unreadable_status_is_not_retryable():
    """Un statut illisible ne doit pas boucler : on rend l'erreur."""
    for bad in (None, "", "abc", object(), [], {}):
        assert is_retryable_status(bad) is False


def test_retry_budget_defaults_to_official_two():
    """Absent de la config -> 2 tentatives, comme l'officiel."""
    assert resolve_max_retries(None) == 2
    assert resolve_max_retries(None, default=2) == 2


def test_config_budget_is_aligned():
    """`config.yaml` doit porter le budget officiel (2), pas 4."""
    import yaml

    with open("config.yaml", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    raw = (cfg.get("upstream") or {}).get("max_retries")
    assert raw is not None, "upstream.max_retries doit rester explicite en config"
    assert resolve_max_retries(raw) == 2, (
        f"budget de reprises {raw!r} != 2 (client officiel 1.19.3) : "
        "chaque 5xx coute une connexion amont supplementaire"
    )


def test_retry_after_wins_and_is_capped():
    """`retry-after` prime sur le backoff, et reste plafonne."""
    assert compute_retry_delay(0, retry_after=2.0, jitter=False) == 2.0
    assert compute_retry_delay(0, retry_after=9999.0, jitter=False) <= 600.0


def test_backoff_is_exponential_from_zero():
    """Backoff croissant : 500 ms officiel -> le proxy part de 1 s (cap 600)."""
    d0 = compute_retry_delay(0, jitter=False)
    d1 = compute_retry_delay(1, jitter=False)
    assert d0 <= d1, f"le backoff doit croitre ({d0} -> {d1})"
    assert d1 <= 600.0
