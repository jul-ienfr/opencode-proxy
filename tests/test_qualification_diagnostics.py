"""Diagnostic classification for failed qualification attempts."""

from __future__ import annotations

import pytest

from free.qualification.diagnostics import classify_diagnostic


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        ("auth", "AUTH_FAILED", "auth"),
        ("429", "", "quota"),
        ("timeout", "handshake timed out", "timeout"),
        ("bind_error", "Cannot assign requested address", "bind"),
        ("no_egress", "probe returned 000", "probe"),
        ("timeout", "temporary failure in name resolution", "dns"),
        ("200", "", "unknown"),
    ],
)
def test_classify_diagnostic(status, error, expected):
    assert classify_diagnostic(status, error) == expected
