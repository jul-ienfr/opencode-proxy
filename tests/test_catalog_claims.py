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


def test_transport_lease_serializes_the_shared_wireguard_key(tmp_path):
    now = {"t": 1000.0}
    claims = CatalogClaims(str(tmp_path / "claims.db"), clock=lambda: now["t"])

    assert claims.acquire_transport("wireguard", 1, ttl_s=100) is True
    assert claims.acquire_transport("wireguard", 2, ttl_s=100) is False
    # The owner can renew without releasing.
    assert claims.acquire_transport("wireguard", 1, ttl_s=100) is True
    now["t"] = 1200.0
    assert claims.acquire_transport("wireguard", 2, ttl_s=100) is True
    assert claims.release_transport("wireguard", 2) is True
    assert claims.acquire_transport("wireguard", 3, ttl_s=100) is True


def test_claim_lease_covers_a_full_bind_and_renews(tmp_path):
    now = {"t": 100.0}
    claims = CatalogClaims(
        str(tmp_path / "claims.db"), lease_s=600.0, clock=lambda: now["t"]
    )
    claims.seed(CANDIDATES[:1])
    claimed = claims.claim_next(1)
    row = claims._conn.execute(
        "SELECT lease_until FROM claims WHERE hostname=?",
        (claimed["host"],),
    ).fetchone()
    assert row[0] - now["t"] == 600.0

    now["t"] = 500.0
    assert claims.renew(claimed["host"], 1) is True
    row = claims._conn.execute(
        "SELECT lease_until FROM claims WHERE hostname=?",
        (claimed["host"],),
    ).fetchone()
    assert row[0] - now["t"] == 600.0


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


def test_429_host_cooldown_is_short_even_with_a_daily_retry_after(tmp_path):
    """The IP stays burned until its daily reset; the hostname does not."""

    now = {"t": 100.0}
    claims = CatalogClaims(
        str(tmp_path / "claims.db"),
        host_429_cooldown_s=600,
        clock=lambda: now["t"],
    )
    claims.seed(CANDIDATES[:1])
    claimed = claims.claim_next(1)
    claims.record(claimed["host"], 1, 429, ip="1.2.3.4", retry_after=65000)

    now["t"] = 699.0
    assert claims.claim_next(2) is None
    now["t"] = 701.0
    again = claims.claim_next(2)
    assert again["host"] == "a.nordvpn.com"
    assert again["station"] == 2


def test_no_egress_backoff_is_exponential_then_resets_on_ok(tmp_path):
    now = {"t": 100.0}
    claims = CatalogClaims(
        str(tmp_path / "claims.db"),
        no_egress_backoff_s=300,
        no_egress_backoff_max_s=3600,
        clock=lambda: now["t"],
    )
    claims.seed(CANDIDATES[:1])
    first = claims.claim_next(1)
    claims.record(first["host"], 1, "no_egress")
    now["t"] = 399.0
    assert claims.claim_next(2) is None
    now["t"] = 401.0
    second = claims.claim_next(2)
    assert second["host"] == "a.nordvpn.com"
    claims.record(second["host"], 2, "no_egress")
    now["t"] = 1000.0
    assert claims.claim_next(3) is None
    now["t"] = 1002.0
    third = claims.claim_next(3)
    assert third["host"] == "a.nordvpn.com"

    # A real 200 resets the failure counter.
    claims.record(third["host"], 3, "ok")
    row = claims._conn.execute(
        "SELECT fail_count FROM claims WHERE hostname='a.nordvpn.com'"
    ).fetchone()
    assert int(row[0]) == 0


def test_country_backoff_is_not_reclaimed_before_its_lease(tmp_path):
    claims = CatalogClaims(str(tmp_path / "claims.db"))
    claims.seed([{"host": "a.nordvpn.com", "country": "Germany"}])
    claimed = claims.claim_next(1, now=100.0)
    assert claimed["host"] == "a.nordvpn.com"
    claims.record(
        claimed["host"], 1, "country_backoff", error="country-backoff", now=100.0
    )
    assert claims.claim_next(1, now=101.0) is None
    assert claims.claim_next(1, now=2000.0)["host"] == "a.nordvpn.com"


def test_rebind_budget_is_global_and_per_minute(tmp_path):
    now = {"t": 100.0}
    claims = CatalogClaims(str(tmp_path / "claims.db"), rebind_limit_per_min=2, clock=lambda: now["t"])
    assert claims.rebind_allowed() is True
    assert claims.rebind_allowed() is True
    assert claims.rebind_allowed() is False
    now["t"] = 161.0
    assert claims.rebind_allowed() is True


def test_ok_is_reserved_then_reclaimable(tmp_path):
    now = {"t": 100.0}
    claims = CatalogClaims(str(tmp_path / "claims.db"), clock=lambda: now["t"])
    claims.seed(CANDIDATES[:1])
    claimed = claims.claim_next(1)
    claims.record(claimed["host"], 1, "ok", ip="5.6.7.8")
    now["t"] = 200.0
    # The winner stays reserved for its station during the lease window.
    assert claims.claim_next(2) is None
    now["t"] = 401.0
    again = claims.claim_next(2)
    assert again["host"] == "a.nordvpn.com"
    assert again["station"] == 2


def test_prune_removes_stale_catalog_rows(tmp_path):
    claims = CatalogClaims(str(tmp_path / "claims.db"), clock=lambda: 100.0)
    claims.seed(
        [
            {"host": "a.nordvpn.com", "country": "Albania"},
            {"host": "stale.nordvpn.com", "country": "Albania"},
        ]
    )
    claims.seed(
        [{"host": "a.nordvpn.com", "country": "Albania"}], prune=True
    )
    assert claims.stats()["catalog"] == 1
    assert claims.claim_next(1)["host"] == "a.nordvpn.com"
    assert claims.claim_next(2) is None


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
