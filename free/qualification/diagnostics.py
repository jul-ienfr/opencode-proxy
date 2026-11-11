"""Classify an attempt failure into an operator-facing diagnostic.

The categories mirror the OpenVPN failure modes kept in the spec: bind,
AUTH, probe/egress, DNS, timeout and quota.  Classification is pure text
analysis; it never closes a hostname and never replaces the transport cycle.
"""

from __future__ import annotations

from .domain import AttemptStatus

DIAGNOSTICS = (
    "bind",
    "auth",
    "dns",
    "probe",
    "timeout",
    "quota",
    "unknown",
)


def classify_diagnostic(status: str, error: str = "") -> str:
    """Return the diagnostic bucket for ``status``/``error``."""

    raw_status = str(status or "").strip().lower()
    text = str(error or "").strip().lower()
    if raw_status == str(AttemptStatus.AUTH) or "auth_failed" in text or "auth failed" in text:
        return "auth"
    if raw_status == str(AttemptStatus.QUOTA) or "429" in text or "quota" in text:
        return "quota"
    if (
        "dns" in text
        or "resolve" in text
        or "name or service not known" in text
        or "temporary failure in name resolution" in text
    ):
        return "dns"
    if (
        raw_status == str(AttemptStatus.BIND_ERROR)
        or "bind" in text
        or "address already in use" in text
        or "cannot assign requested address" in text
    ):
        return "bind"
    if raw_status == str(AttemptStatus.TIMEOUT) or "timeout" in text or "timed out" in text:
        return "timeout"
    if raw_status == str(AttemptStatus.NO_EGRESS) or "probe" in text or "egress" in text:
        return "probe"
    return "unknown"


__all__ = ["DIAGNOSTICS", "classify_diagnostic"]
