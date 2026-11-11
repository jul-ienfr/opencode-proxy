"""Security gate: qualification projections never expose secrets.

The dashboard, metrics and SLO views are read-only.  This test serializes every
projection and rejects credential-bearing field names; it also verifies that
the paid/second-account path is absent from the qualification package.
"""

from __future__ import annotations

import json
import time

from free.qualification import metrics, readmodel
from free.qualification.domain import Attempt, AttemptStatus, Candidate, CandidateStatus
from free.qualification.store import QualificationStore

FORBIDDEN = (
    "password",
    "api_key",
    "apikey",
    "secret",
    "token",
    "credential",
    "transport_url",
    "authorization",
)


def _candidate(hostname: str) -> Candidate:
    return Candidate(
        hostname=hostname,
        country="Sweden",
        transports=("wireguard",),
        potential_score=80.0,
        first_seen_at=100.0,
        last_seen_at=100.0,
        updated_at=100.0,
    )


def test_projections_never_expose_secrets(tmp_path):
    db = tmp_path / "registry.db"
    now = time.time()
    store = QualificationStore(db)
    try:
        store.upsert_candidates([_candidate("se1.nordvpn.com")], now=now)
        store.upsert_server_transports([_candidate("se1.nordvpn.com")])
        attempt = Attempt(
            hostname="se1.nordvpn.com",
            station=1,
            transport="wireguard",
            status=AttemptStatus.AUTH,
            error="AUTH_FAILED",
            started_at=now,
            finished_at=now,
        )
        store.record_attempt(attempt)
        store.apply_attempt(
            "se1.nordvpn.com",
            attempt,
            candidate_status=CandidateStatus.AUTH,
            now=now,
        )
        store.upsert_station_state(
            1,
            state="degraded",
            current_ip="1.2.3.4",
            current_host="se1.nordvpn.com",
            client_usable=False,
            now=now,
        )
        store.record_repair_job(1, "auth", now=now)
    finally:
        store.close()

    payloads = [
        readmodel.summary(db),
        readmodel.queue(db),
        readmodel.next_candidates(db),
        readmodel.epochs(db),
        readmodel.attempts(db),
        readmodel.transports(db),
        readmodel.stations(db),
        readmodel.watchdog(db),
        readmodel.discovery(db),
        readmodel.repairs(db),
        readmodel.quota(db),
        readmodel.slo(db),
        metrics.snapshot(db),
    ]
    text = json.dumps(payloads, default=str).lower()
    for forbidden in FORBIDDEN:
        assert forbidden not in text, f"secret field leaked: {forbidden}"


def test_no_paid_or_second_account_branch():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "free" / "qualification"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for needle in ("second_account", "second account", "paid_plan", "premium_plan"):
            assert needle not in text, f"{path.name} contains {needle}"
