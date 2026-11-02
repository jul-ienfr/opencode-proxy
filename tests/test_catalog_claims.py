"""Shared catalog claims: one hostname per station, resets and rebind budget."""

from __future__ import annotations

from free.catalog_claims import CatalogClaims

CANDIDATES = [
    {"host": "a.nordvpn.com", "country": "Albania"},
    {"host": "b.nordvpn.com", "country": "Albania"},
    {"host": "c.nordvpn.com", "country": "Albania"},
]


def test_claim_is_exclusive_per_station(tmp_path):
    now = {"t": 100.0}
    claims = CatalogClaims(str(tmp_path / "claims.db"), clock=lambda: now["t"])
    claims.seed(CANDIDATES)

    first = claims.claim_next(1)
    second = claims.claim_next(2)
    third = claims.claim_next(3)
    fourth = claims.claim_next(4)

    assert first["host"] == "a.nordvpn.com"
    assert second["host"] == "b.nordvpn.com"
    assert third["host"] == "c.nordvpn.com"
    assert fourth is None
    assert claims.stats()["claims"].get("claimed") == 3


def test_429_is_reclaimable_after_its_reset(tmp_path):
    now = {"t": 100.0}
    claims = CatalogClaims(str(tmp_path / "claims.db"), clock=lambda: now["t"])
    claims.seed(CANDIDATES[:1])

    claimed = claims.claim_next(1)
    claims.record(claimed["host"], 1, 429, ip="1.2.3.4", retry_after=60)
    assert claims.claim_next(2) is None

    now["t"] = 161.0
    again = claims.claim_next(2)
    assert again["host"] == "a.nordvpn.com"
    assert again["station"] == 2


def test_rebind_budget_is_global_and_per_minute(tmp_path):
    now = {"t": 100.0}
    claims = CatalogClaims(str(tmp_path / "claims.db"), rebind_limit_per_min=2, clock=lambda: now["t"])
    assert claims.rebind_allowed() is True
    assert claims.rebind_allowed() is True
    assert claims.rebind_allowed() is False
    now["t"] = 161.0
    assert claims.rebind_allowed() is True


def test_a_200_is_never_reclaimed(tmp_path):
    now = {"t": 100.0}
    claims = CatalogClaims(str(tmp_path / "claims.db"), clock=lambda: now["t"])
    claims.seed(CANDIDATES[:1])
    claimed = claims.claim_next(1)
    claims.record(claimed["host"], 1, "ok", ip="5.6.7.8")
    now["t"] = 10_000.0
    assert claims.claim_next(2) is None
    assert claims.stats()["claims"].get("ok") == 1


def test_reseed_updates_priority_order(tmp_path):
    claims = CatalogClaims(str(tmp_path / "claims.db"), clock=lambda: 100.0)
    claims.seed([{"host": "a.nordvpn.com", "country": "Albania"}])
    claims.seed(
        [
            {"host": "b.nordvpn.com", "country": "Albania"},
            {"host": "a.nordvpn.com", "country": "Albania"},
        ]
    )
    assert claims.claim_next(1)["host"] == "b.nordvpn.com"
