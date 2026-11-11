"""Module size ratchet tests."""

from __future__ import annotations

from scripts.module_size_audit import check


def test_new_file_over_max_is_a_violation():
    violations = check({"new.py": 1501}, max_lines=1500)
    assert violations[0]["reason"] == "over-max"


def test_new_file_at_max_is_allowed():
    assert check({"new.py": 1500}, max_lines=1500) == []


def test_baseline_growth_is_a_violation():
    violations = check({"legacy.py": 1601}, max_lines=1500, baseline={"legacy.py": 1600})
    assert violations[0]["reason"] == "grew"
    assert violations[0]["baseline"] == 1600


def test_baseline_under_max_can_grow_up_to_max():
    assert check({"legacy.py": 200}, max_lines=1500, baseline={"legacy.py": 100}) == []
    violations = check({"legacy.py": 1501}, max_lines=1500, baseline={"legacy.py": 100})
    assert violations[0]["reason"] == "over-max"


def test_baseline_same_or_smaller_is_allowed():
    assert check({"legacy.py": 1600}, max_lines=1500, baseline={"legacy.py": 1600}) == []
    assert check({"legacy.py": 1500}, max_lines=1500, baseline={"legacy.py": 1600}) == []


def test_baseline_does_not_hide_new_monolith():
    violations = check(
        {"legacy.py": 50, "new.py": 2000},
        max_lines=1500,
        baseline={"legacy.py": 100},
    )
    assert violations[0]["file"] == "new.py"
