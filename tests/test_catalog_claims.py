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


def test_legacy_candidate_skips_host_with_exhausted_egress(tmp_path):
    """[P0 2026-11-10] Le catalogue legacy ne re-pioche pas une IP épuisée."""
    now = {"t": 100.0}
    claims = CatalogClaims(
        str(tmp_path / "claims.db"), lease_s=10.0, clock=lambda: now["t"]
    )
    claims.seed(CANDIDATES)
    first = claims.claim_next(1)
    claims.record(first["host"], 1, "ok", ip="1.2.3.4")

    now["t"] = 120.0  # lease expirée : le host est de nouveau candidat…
    skipped = claims.claim_next(2, forbidden_ips={"1.2.3.4"})
    # …mais son egress est épuisé : un AUTRE hostname doit être choisi.
    assert skipped is not None
    assert skipped["host"] != first["host"]


def test_qualification_candidate_skips_claim_egress_even_without_last_ip(tmp_path):
    """[P0 2026-11-10] claims.egress_ip est la source récente, pas last_egress_ip."""
    claims = CatalogClaims(str(tmp_path / "claims.db"), lease_s=10.0)
    claims._conn.execute(
        """
        CREATE TABLE qualification_candidates (
            hostname TEXT PRIMARY KEY,
            country TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            transports TEXT NOT NULL DEFAULT '[]',
            measured_score REAL NOT NULL DEFAULT 0,
            potential_score REAL NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            quota_remaining INTEGER,
            rtt_ms REAL,
            load REAL NOT NULL DEFAULT 0,
            last_egress_ip TEXT NOT NULL DEFAULT '',
            next_eligible_at REAL NOT NULL DEFAULT 0,
            quota_reset_at REAL,
            updated_at REAL NOT NULL DEFAULT 0
        )
        """
    )
    claims._conn.execute(
        "INSERT INTO qualification_candidates("
        "hostname,country,status,transports,success_count,last_egress_ip,"
        "next_eligible_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("q1.nordvpn.com", "Albania", "pending", '["wireguard"]', 5, "", 0.0, 0.0),
    )
    claims._conn.commit()
    claims._conn.execute(
        "INSERT INTO claims(hostname,station,status,egress_ip,retry_after,"
        "reset_at,error,updated_at,lease_until,fail_count) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("q1.nordvpn.com", 1, "ok", "9.9.9.9", 0.0, 0.0, "", 0.0, 0.0, 0),
    )
    claims._conn.commit()

    assert claims.claim_next(2, forbidden_ips={"9.9.9.9"}) is None
    again = claims.claim_next(2)
    assert again is not None and again["host"] == "q1.nordvpn.com"


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


def test_country_backoff_is_not_a_long_gate(tmp_path):
    claims = CatalogClaims(str(tmp_path / "claims.db"))
    claims.seed([{"host": "a.nordvpn.com", "country": "Germany"}])
    claimed = claims.claim_next(1, now=100.0)
    assert claimed["host"] == "a.nordvpn.com"
    claims.record(
        claimed["host"], 1, "country_backoff", error="country-backoff", now=100.0
    )
    assert claims.claim_next(1, now=101.0) is None
    # The default lease is 300 s; a country gate would have blocked until 1900 s.
    assert claims.claim_next(1, now=401.0)["host"] == "a.nordvpn.com"


def test_claim_next_prefers_qualification_queue(tmp_path):
    from free.qualification import Candidate, QualificationStore

    db = str(tmp_path / "claims.db")
    claims = CatalogClaims(db)
    claims.seed([{"host": "legacy.nordvpn.com", "country": "Germany"}])
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                Candidate(
                    hostname="queue.nordvpn.com",
                    country="Sweden",
                    transports=("wireguard",),
                    potential_score=90.0,
                    measured_score=80.0,
                    success_count=1,
                    first_seen_at=100.0,
                    last_seen_at=100.0,
                    updated_at=100.0,
                )
            ]
        )
    finally:
        store.close()
    claimed = claims.claim_next(1, now=100.0)
    assert claimed["host"] == "queue.nordvpn.com"
    assert claimed["country"] == "Sweden"
    store = QualificationStore(db)
    try:
        queued = store.get("queue.nordvpn.com")
        assert queued is not None
        assert str(queued.status) == "claimed"
    finally:
        store.close()


def test_unmeasured_qualification_does_not_preempt_known_catalog(tmp_path):
    from free.qualification import Candidate, QualificationStore

    db = str(tmp_path / "claims.db")
    claims = CatalogClaims(db)
    claims.seed([{"host": "legacy.nordvpn.com", "country": "Germany"}])
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                Candidate(
                    hostname="queue.nordvpn.com",
                    country="Sweden",
                    transports=("wireguard",),
                    potential_score=99.0,
                    first_seen_at=100.0,
                    last_seen_at=100.0,
                    updated_at=100.0,
                )
            ]
        )
    finally:
        store.close()
    claimed = claims.claim_next(1, now=100.0)
    assert claimed["host"] == "legacy.nordvpn.com"


def test_unmeasured_qualification_is_used_when_catalog_is_dry(tmp_path):
    from free.qualification import Candidate, QualificationStore

    db = str(tmp_path / "claims.db")
    claims = CatalogClaims(db)
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                Candidate(
                    hostname="queue.nordvpn.com",
                    country="Sweden",
                    transports=("wireguard",),
                    potential_score=99.0,
                    first_seen_at=100.0,
                    last_seen_at=100.0,
                    updated_at=100.0,
                )
            ]
        )
    finally:
        store.close()
    claimed = claims.claim_next(1, now=100.0)
    assert claimed["host"] == "queue.nordvpn.com"


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


def _qualification_claim_setup(tmp_path):
    from free.qualification import Candidate, QualificationStore

    db = str(tmp_path / "claims.db")
    claims = CatalogClaims(db)
    store = QualificationStore(db)
    store.upsert_candidates(
        [
            Candidate(
                hostname="queue.nordvpn.com",
                country="Sweden",
                transports=("wireguard",),
                potential_score=90.0,
                first_seen_at=100.0,
                last_seen_at=100.0,
                updated_at=100.0,
            )
        ]
    )
    return claims, store


def test_no_egress_releases_qualification_claim_to_pending(tmp_path):
    claims, store = _qualification_claim_setup(tmp_path)
    try:
        claimed = claims.claim_next(1, now=100.0)
        assert claimed["host"] == "queue.nordvpn.com"
        assert str(store.get("queue.nordvpn.com").status) == "claimed"
        claims.record(
            claimed["host"], 1, "no_egress", now=101.0, error="bind-timeout"
        )
        row = store.get("queue.nordvpn.com")
        assert str(row.status) == "pending"
        assert row.infra_failures == 1
        assert row.next_eligible_at > 101.0
        # Before the backoff the host is not claimable again.
        assert claims.claim_next(2, now=102.0) is None
    finally:
        store.close()
        claims.close()


def test_ok_promotes_qualification_candidate_to_usable(tmp_path):
    claims, store = _qualification_claim_setup(tmp_path)
    try:
        claimed = claims.claim_next(1, now=100.0)
        claims.record(claimed["host"], 1, "ok", ip="198.51.100.9", now=120.0)
        row = store.get("queue.nordvpn.com")
        assert str(row.status) == "usable"
        assert row.success_count == 1
        assert row.last_egress_ip == "198.51.100.9"
    finally:
        store.close()
        claims.close()


def test_429_sets_qualification_quota_reset(tmp_path):
    claims, store = _qualification_claim_setup(tmp_path)
    try:
        claimed = claims.claim_next(1, now=100.0)
        claims.record(claimed["host"], 1, "429", now=110.0, retry_after=60.0)
        row = store.get("queue.nordvpn.com")
        assert str(row.status) == "quota"
        assert row.fail_count == 1
        assert row.quota_reset_at == 110.0 + 60.0
        assert row.next_eligible_at == 110.0 + 60.0
    finally:
        store.close()
        claims.close()


def test_catalog_priority_beats_old_failure_count(tmp_path):
    claims = CatalogClaims(str(tmp_path / "claims.db"), clock=lambda: 100.0)
    claims.seed(
        [
            {"host": "known.nordvpn.com", "country": "Albania"},
            {"host": "untried.nordvpn.com", "country": "Albania"},
        ]
    )
    for offset in range(4):
        claims.record(
            "known.nordvpn.com", 1, "no_egress", now=100.0 + offset
        )
    claimed = claims.claim_next(1, now=10_000.0)
    assert claimed["host"] == "known.nordvpn.com"


def test_forbidden_egress_ip_skips_the_qualification_candidate(tmp_path):
    from free.qualification import Candidate, QualificationStore

    db = str(tmp_path / "claims.db")
    claims = CatalogClaims(db)
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                Candidate(
                    hostname="burned.nordvpn.com",
                    country="Netherlands",
                    transports=("wireguard", "openvpn-tcp"),
                    potential_score=99.0,
                    last_egress_ip="204.8.96.147",
                    first_seen_at=100.0,
                    last_seen_at=100.0,
                    updated_at=100.0,
                ),
                Candidate(
                    hostname="fresh.nordvpn.com",
                    country="Sweden",
                    transports=("wireguard", "openvpn-tcp"),
                    potential_score=10.0,
                    last_egress_ip="130.195.218.110",
                    first_seen_at=100.0,
                    last_seen_at=100.0,
                    updated_at=100.0,
                ),
            ]
        )
    finally:
        store.close()
    claimed = claims.claim_next(
        1,
        now=100.0,
        forbidden_ips={"204.8.96.147"},
    )
    assert claimed["host"] == "fresh.nordvpn.com"


def test_forbidden_ips_fall_back_to_legacy_catalog(tmp_path):
    from free.qualification import Candidate, QualificationStore

    db = str(tmp_path / "claims.db")
    claims = CatalogClaims(db)
    claims.seed([{"host": "legacy.nordvpn.com", "country": "Germany"}])
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                Candidate(
                    hostname="burned.nordvpn.com",
                    country="Netherlands",
                    transports=("wireguard",),
                    potential_score=99.0,
                    last_egress_ip="204.8.96.147",
                    first_seen_at=100.0,
                    last_seen_at=100.0,
                    updated_at=100.0,
                )
            ]
        )
    finally:
        store.close()
    claimed = claims.claim_next(
        1,
        now=100.0,
        forbidden_ips={"204.8.96.147"},
    )
    assert claimed["host"] == "legacy.nordvpn.com"


def test_no_global_transport_lease_remains(tmp_path):
    """Each station may use WireGuard independently: no shared transport lock."""
    claims = CatalogClaims(str(tmp_path / "claims.db"))
    try:
        assert not hasattr(claims, "acquire_transport")
        assert not hasattr(claims, "release_transport")
        tables = {
            str(row[0])
            for row in claims._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "transport_leases" not in tables
    finally:
        claims.close()
