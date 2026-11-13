"""T078: the rollout dry-run is safe, ordered 6 -> 1 and bounded to six stations."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rollout_control_plane.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_dry_run_prints_the_6_to_1_plan_without_contacting_a_station():
    result = run("--dry-run")
    assert result.returncode == 0
    output = result.stdout
    assert "6 -> 5 -> 4 -> 3 -> 2 -> 1" in output
    assert "no station contacted" in output
    assert "stations in this rollout: 6" in output


def test_dry_run_accepts_a_restricted_order():
    result = run("--dry-run", "--stations", "6,5")
    assert result.returncode == 0
    assert "rollout plan: 6 -> 5" in result.stdout
    assert "stations in this rollout: 2" in result.stdout


def test_a_seventh_station_is_rejected():
    result = run("--dry-run", "--stations", "7")
    assert result.returncode != 0
    assert "outside 1..6" in result.stderr
