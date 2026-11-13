"""Typed errors raised by the control plane client.

The client never falls back by itself: it raises a precise error and the
rotation adapter decides whether the cold legacy path is allowed.  In
particular a 422 is a deterministic rejection and must never trigger a legacy
fallback.
"""

from __future__ import annotations

from typing import Any


class ControlPlaneError(Exception):
    """Base error for every control plane interaction."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = dict(details or {})


class ControlPlaneUnavailable(ControlPlaneError):
    """Transport failure or retryable 5xx exhausted the retry budget."""


class ControlPlaneTimeout(ControlPlaneUnavailable):
    """The HTTP call timed out; the job may still be running on the fork."""


class UnknownTarget(ControlPlaneError):
    """The fork rejected the target or policy deterministically (422)."""


class ActiveJob(ControlPlaneError):
    """Another job is already active (409 active_job)."""

    def __init__(
        self,
        message: str,
        *,
        active_job_id: str = "",
        status: int | None = None,
        code: str = "active_job",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, status=status, code=code, details=details)
        self.active_job_id = str(active_job_id or "")


class VerificationFailed(ControlPlaneError):
    """The egress proof failed and the job did not commit."""

    def __init__(
        self,
        message: str,
        *,
        verification: Any = None,
        status: int | None = None,
        code: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, status=status, code=code, details=details)
        self.verification = verification


class Exhausted(ControlPlaneError):
    """Every ordered candidate was tried and none committed."""

    def __init__(
        self,
        message: str,
        *,
        attempts: list[Any] | None = None,
        job: Any = None,
        status: int | None = None,
        code: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, status=status, code=code, details=details)
        self.attempts = list(attempts or [])
        self.job = job


class LegacyFallback(ControlPlaneError):
    """The cold legacy path must run instead of the control plane."""

    def __init__(self, reason: str, *, cause: BaseException | None = None) -> None:
        super().__init__(reason)
        self.reason = str(reason)
        self.cause = cause


class ConfigError(ValueError):
    """Invalid ``control_plane`` configuration."""
