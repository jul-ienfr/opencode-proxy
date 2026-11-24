"""T036 — rotation journal: every rotation records its cause and its proof.

The journal is an append-only JSONL file (``logs/rotation_journal.jsonl``).
It must never raise: a rotation that fails (or a disk that refuses the write)
must not be able to stop the chain itself.
"""

from __future__ import annotations

import json

from scripts import fresh_chain


def _read(path):
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [json.loads(line) for line in lines]


def test_rotation_journal_records_cause_and_proof(tmp_path):
    journal = tmp_path / "rotation_journal.jsonl"

    class FakeEvidence:
        wire_fp = "fp-abc"
        evidence_ref = "ev-123"
        readers = ["api", "probe"]

    with fresh_chain.rotation_journal(
        station=4,
        transport="wireguard",
        cause="quota",
        mode="sequential",
        old_host="old.example",
        new_host="new.example",
        old_ip="1.1.1.1",
        new_ip="2.2.2.2",
        evidence=FakeEvidence(),
        path=journal,
    ) as record:
        record["result"] = "committed"
        record["proof"] = {"probe_status": 200, "latency_ms": 12.5}

    entries = _read(journal)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["station"] == 4
    assert entry["cause"] == "quota"
    assert entry["wire_fp"] == "fp-abc"
    assert entry["evidence_ref"] == "ev-123"
    assert entry["old_host"] == "old.example"
    assert entry["new_host"] == "new.example"
    assert entry["old_ip"] == "1.1.1.1"
    assert entry["new_ip"] == "2.2.2.2"
    assert entry["readers"] == ["api", "probe"]
    assert entry["result"] == "committed"
    assert entry["proof"]["probe_status"] == 200
    assert "started_at" in entry and "finished_at" in entry


def test_rotation_journal_accepts_fake_evidence_dict(tmp_path):
    journal = tmp_path / "rotation_journal.jsonl"
    with fresh_chain.rotation_journal(
        station=2,
        transport="auto",
        cause="mismatch",
        evidence={"wire_fp": "wfp", "evidence_ref": "body-hash", "readers": ["r1"]},
        path=journal,
    ):
        pass

    entry = _read(journal)[0]
    assert entry["wire_fp"] == "wfp"
    assert entry["evidence_ref"] == "body-hash"
    assert entry["readers"] == ["r1"]
    assert entry["cause"] == "mismatch"


def test_rotation_journal_writes_terminal_result_on_exception(tmp_path):
    journal = tmp_path / "rotation_journal.jsonl"

    try:
        with fresh_chain.rotation_journal(
            station=7,
            transport="auto",
            cause="burned",
            path=journal,
        ) as record:
            record["proof"] = {"probe_status": 429}
            raise RuntimeError("rotate exploded")
    except RuntimeError:
        pass

    entries = _read(journal)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["result"] == "error"
    assert "rotate exploded" in entry["error"]
    assert entry["proof"]["probe_status"] == 429
    assert "rotate exploded" in entry["proof"]["error"]
    # The finally block always closes the interval.
    assert "finished_at" in entry


def test_rotation_cause_vocabulary():
    assert fresh_chain._rotation_cause() == "manual"
    assert fresh_chain._rotation_cause(quota=True) == "quota"
    assert fresh_chain._rotation_cause(burned=True) == "burned"
    assert fresh_chain._rotation_cause(mismatch=True) == "mismatch"
    # Mismatch is the strongest signal (constitution XVIII).
    assert fresh_chain._rotation_cause(quota=True, mismatch=True) == "mismatch"
    assert set(fresh_chain.ROTATION_CAUSES) == {"quota", "burned", "mismatch", "manual"}


def test_log_rotation_journal_normalizes_schema(tmp_path):
    journal = tmp_path / "rotation_journal.jsonl"
    fresh_chain._log_rotation_journal(
        {"station": 1, "new_host": "h", "result": "ok"}, path=journal
    )

    entry = _read(journal)[0]
    # Missing fields are filled with explicit, non-crashing defaults.
    for field in (
        "ts",
        "started_at",
        "finished_at",
        "cause",
        "wire_fp",
        "evidence_ref",
        "readers",
        "proof",
        "result",
    ):
        assert field in entry
    assert entry["readers"] == []
    assert entry["proof"] == {}


def test_log_rotation_journal_is_fail_soft(tmp_path):
    # A plain file used as a parent directory makes the append impossible.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    target = blocker / "rotation_journal.jsonl"

    # Must not raise even though the path cannot be created.
    fresh_chain._log_rotation_journal({"station": 1}, path=target)
