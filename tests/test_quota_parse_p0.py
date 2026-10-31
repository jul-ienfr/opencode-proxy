"""[audit 2026-10-30] Un parse partiel de la page Go ne doit jamais être
affiché comme un quota sain à 0 %."""

import pytest

from dashboard.quota import parse_quota_html


def _page(*windows: str) -> str:
    return "<script>" + "\n".join(windows) + "</script>"


def test_full_page_parses_all_windows():
    out = parse_quota_html(
        _page(
            "rollingUsage: {usagePercent: 10, resetInSec: 100},",
            "weeklyUsage: {usagePercent: 20, resetInSec: 200},",
            "monthlyUsage: {usagePercent: 30, resetInSec: 300},",
        )
    )
    assert out["rolling"]["usage_percent"] == 10
    assert out["weekly"]["usage_percent"] == 20
    assert out["monthly"]["usage_percent"] == 30


def test_partial_page_is_an_error_not_zero_percent():
    with pytest.raises(ValueError) as exc:
        parse_quota_html(_page("rollingUsage: {usagePercent: 99, resetInSec: 100},"))
    assert "missing windows" in str(exc.value)


def test_changed_page_is_an_error():
    with pytest.raises(ValueError):
        parse_quota_html("<html><body>nothing useful</body></html>")
