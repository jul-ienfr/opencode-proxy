"""
Unit tests for dashboard date-bound normalization.

Covers the subtlest part of the mixed-timestamps fix: bare YYYY-MM-DD
filters are interpreted as LOCAL calendar days and converted to UTC+Z
bounds, while complete timestamps pass through untouched.

The machine running the proxy writes naive rows in ITS local time, so the
conversion depends on the system timezone — exact-value cases are gated
behind a gmtoff==+7200 (UTC+2, DST summer) check; the property-based cases
run everywhere.
"""

import os
import re
import sys
import time
from datetime import UTC, datetime

import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.api import (
    _DATE_ONLY_RE,
    _date_bound_to_utc,
    _normalize_date_bound,
    daysAgo,
)

Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
GMTOFF = time.localtime().tm_gmtoff


def _utc_to_local(ts: str) -> str:
    """UTC+Z timestamp → naive local wall time (round-trip check helper)."""
    return datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%dT%H:%M:%S")


class TestDateBoundToUtc:
    """Bare YYYY-MM-DD is a LOCAL calendar day → UTC+Z bound."""

    def test_returns_utc_z_format(self):
        assert Z_RE.match(_date_bound_to_utc("2026-08-16", end_of_day=False))
        assert Z_RE.match(_date_bound_to_utc("2026-08-16", end_of_day=True))

    def test_start_of_day_round_trips_to_local_midnight(self):
        # Whatever the system timezone, the UTC bound must land on local 00:00:00.
        local = _utc_to_local(_date_bound_to_utc("2026-08-16", end_of_day=False))
        assert local == "2026-08-16T00:00:00", local

    def test_end_of_day_round_trips_to_local_235959(self):
        local = _utc_to_local(_date_bound_to_utc("2026-08-16", end_of_day=True))
        assert local == "2026-08-16T23:59:59", local

    @pytest.mark.skipif(GMTOFF != 7200, reason="exact values assume UTC+2 (summer)")
    def test_exact_utc2_values(self):
        # 2026-08-16 local midnight == 2026-08-15T22:00:00Z ; end of day == 21:59:59Z
        assert _date_bound_to_utc("2026-08-16", end_of_day=False) == "2026-08-15T22:00:00Z"
        assert _date_bound_to_utc("2026-08-16", end_of_day=True) == "2026-08-16T21:59:59Z"
        assert _date_bound_to_utc("2026-08-01", end_of_day=False) == "2026-07-31T22:00:00Z"

    def test_bounds_are_contiguous(self):
        start = _date_bound_to_utc("2026-08-16", end_of_day=False)
        end = _date_bound_to_utc("2026-08-16", end_of_day=True)
        # No overlap and no gap with the next day's start.
        assert end > start
        next_start = _date_bound_to_utc("2026-08-17", end_of_day=False)
        assert next_start > end
        gap = (datetime.fromisoformat(next_start) - datetime.fromisoformat(end)).total_seconds()
        assert gap == 1.0


class TestNormalizeDateBound:
    """Pass-through rule: only a bare YYYY-MM-DD is treated as a local day."""

    def test_bare_date_is_converted(self):
        out = _normalize_date_bound("2026-08-16", end_of_day=False)
        assert isinstance(out, str) and out.endswith("Z") and out.startswith("2026-08-15")

    def test_z_timestamp_passes_through(self):
        assert (
            _normalize_date_bound("2026-08-16T15:21:29Z", end_of_day=False)
            == "2026-08-16T15:21:29Z"
        )
        assert (
            _normalize_date_bound("2026-08-16T15:21:29Z", end_of_day=True) == "2026-08-16T15:21:29Z"
        )

    def test_complete_timestamp_without_z_passes_through(self):
        # Space-separated or any non-bare-date string is left alone (daysAgo
        # and the DB now converge on UTC+Z, so nothing else should appear).
        assert (
            _normalize_date_bound("2026-08-16 15:21:29", end_of_day=False) == "2026-08-16 15:21:29"
        )

    def test_non_string_passes_through(self):
        assert _normalize_date_bound(None, end_of_day=False) is None
        assert _normalize_date_bound(1720000000, end_of_day=True) == 1720000000
        assert _normalize_date_bound("", end_of_day=False) == ""

    def test_regex_matches_only_bare_dates(self):
        assert _DATE_ONLY_RE.match("2026-08-16")
        assert not _DATE_ONLY_RE.match("2026-8-16")
        assert not _DATE_ONLY_RE.match("2026-08-16T15:21:29Z")
        assert not _DATE_ONLY_RE.match("2026-08-16 15:21:29")
        assert not _DATE_ONLY_RE.match("16/08/2026")


class TestDaysAgo:
    """daysAgo emits a full UTC+Z timestamp — never a bare date."""

    def test_emits_utc_z_format(self):
        assert Z_RE.match(daysAgo(0))
        assert Z_RE.match(daysAgo(30))

    def test_value_is_instant_now_minus_n_days(self):
        now = datetime.now(UTC)
        ts = datetime.fromisoformat(daysAgo(0))
        assert abs((now - ts).total_seconds()) < 10
        assert daysAgo(1) < daysAgo(0)

    def test_passes_through_build_where_unchanged(self):
        # daysAgo output is a complete Z timestamp → normalization is a no-op.
        assert _normalize_date_bound(daysAgo(30), end_of_day=False) == daysAgo(30)
