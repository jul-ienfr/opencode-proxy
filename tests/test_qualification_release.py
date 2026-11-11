"""Release evidence manifest tests."""

from __future__ import annotations

import pytest

from free.qualification.release import build_release_manifest


def _canary(passed=True):
    return {
        "passed": passed,
        "generated_at": "2026-11-10T12:00:00Z",
        "api_base": "http://127.0.0.1:4000",
        "target": 6,
        "evidence": {"stations_usable": 6},
    }


def test_manifest_builds_with_all_checks_and_files():
    manifest = build_release_manifest(
        canary=_canary(),
        checklist_text="- [x] code\n- [x] tests\n- [x] canary\n",
        files={"a.json": b"alpha", "b.md": b"beta"},
        generated_at="2026-11-10T12:00:00Z",
    )
    assert manifest["passed"] is True
    assert manifest["checklist"] == {"checked": 3, "unchecked": 0}
    assert manifest["canary"]["evidence"] == {"stations_usable": 6}
    assert len(manifest["files"]["a.json"]["sha256"]) == 64
    assert manifest["files"]["b.md"]["size"] == 4


def test_manifest_refuses_unchecked_checklist():
    with pytest.raises(ValueError, match="checklist incomplète"):
        build_release_manifest(
            canary=_canary(),
            checklist_text="- [x] code\n- [ ] canary\n",
            files={"a.json": b"alpha"},
            generated_at="2026-11-10T12:00:00Z",
        )


def test_manifest_refuses_failed_canary():
    with pytest.raises(ValueError, match="canary non validé"):
        build_release_manifest(
            canary=_canary(False),
            checklist_text="- [x] code\n",
            files={"a.json": b"alpha"},
            generated_at="2026-11-10T12:00:00Z",
        )


def test_manifest_refuses_empty_evidence():
    with pytest.raises(ValueError, match="aucune preuve"):
        build_release_manifest(
            canary=_canary(),
            checklist_text="- [x] code\n",
            files={},
            generated_at="2026-11-10T12:00:00Z",
        )
