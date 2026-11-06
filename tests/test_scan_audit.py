"""Scan audit: persistent runs/attempts, fail-soft, crash reconciliation."""

from __future__ import annotations

from free.scan_audit import ScanAudit


def test_run_and_attempt_are_replayable(tmp_path):
    now = {"t": 100.0}
    audit = ScanAudit(tmp_path / "ip_registry.db", clock=lambda: now["t"])
    run_id = audit.start_run(3, transport="openvpn-udp", reason="repair", candidates=40)
    attempt_id = audit.begin_attempt(
        run_id, 3, "al37.nordvpn.com", country="Albania", transport="openvpn-udp"
    )
    now["t"] = 101.0
    audit.finish_attempt(
        attempt_id,
        "200",
        egress_ip="87.120.102.20",
        valid=True,
        validation="content",
        fingerprint="abc",
    )
    audit.finish_run(run_id, "recovered", winner_host="al37.nordvpn.com", winner_ip="87.120.102.20")
    snap = audit.snapshot()
    audit.close()

    assert snap["runs"][0]["outcome"] == "recovered"
    assert snap["runs"][0]["winner_ip"] == "87.120.102.20"
    assert snap["attempts"][0]["status"] == "200"
    assert snap["attempts"][0]["egress_ip"] == "87.120.102.20"
    assert snap["attempts"][0]["valid"] == 1
    assert snap["attempts"][0]["validation"] == "content"
    assert snap["attempts"][0]["fingerprint"] == "abc"


def test_stale_open_runs_are_closed_on_startup(tmp_path):
    now = {"t": 100.0}
    audit = ScanAudit(tmp_path / "ip_registry.db", clock=lambda: now["t"])
    run_id = audit.start_run(2, transport="wireguard", reason="repair")
    attempt_id = audit.begin_attempt(run_id, 2, "at160.nordvpn.com")

    now["t"] = 10_000.0
    closed = audit.reconcile_stale(max_age_s=60)
    snap = audit.snapshot()
    audit.close()

    assert closed == 1
    assert snap["runs"][0]["outcome"] == "interrupted"
    assert snap["runs"][0]["ended_at"] == 10_000.0
    assert snap["attempts"][0]["status"] == "interrupted"
    assert snap["attempts"][0]["finished_at"] == 10_000.0
    assert attempt_id


def test_open_runs_and_interrupt_attempts(tmp_path):
    audit = ScanAudit(tmp_path / "ip_registry.db", clock=lambda: 100.0)
    run_id = audit.start_run(4, transport="openvpn-udp")
    attempt_id = audit.begin_attempt(run_id, 4, "al37.nordvpn.com")
    assert len(audit.open_runs()) == 1
    assert attempt_id

    audit.interrupt_attempts(run_id)
    assert audit.snapshot()["attempts"][0]["status"] == "interrupted"
    audit.finish_run(run_id, "interrupted")
    assert audit.open_runs() == []
    audit.close()


def test_audit_is_fail_soft_when_the_db_cannot_open(tmp_path):
    # A directory path is not a SQLite database.
    audit = ScanAudit(tmp_path)
    run_id = audit.start_run(1)
    attempt_id = audit.begin_attempt(run_id, 1, "x.nordvpn.com")
    audit.finish_attempt(attempt_id, "bind_error")
    audit.finish_run(run_id, "exhausted")
    assert audit.snapshot() == {"runs": [], "attempts": []}
    audit.close()
