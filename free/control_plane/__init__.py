"""Typed client for the Gluetun 016 control plane.

The package owns no registry state: it sends ordered candidates, constraints
and budgets, then reports the fork's terminal job.  IP claims, leases, quotas
and scores stay in ``registry.db``.
"""

from __future__ import annotations

from .client import GluetunControlClient
from .config import ControlPlaneConfig, load_control_plane_config
from .errors import (
    ActiveJob,
    ConfigError,
    ControlPlaneError,
    ControlPlaneTimeout,
    ControlPlaneUnavailable,
    Exhausted,
    LegacyFallback,
    UnknownTarget,
    VerificationFailed,
)
from .models import (
    Attempt,
    Capabilities,
    Constraints,
    ControlEvent,
    DNSState,
    EgressVerification,
    ServerCandidate,
    SwitchJob,
    TransportState,
    TunnelState,
    TunnelTarget,
)
from .shadow import ShadowComparison, ShadowObserver, ShadowPlan
from .telemetry import ShadowTelemetry

__all__ = [
    "ActiveJob",
    "Attempt",
    "Capabilities",
    "ConfigError",
    "Constraints",
    "ControlEvent",
    "ControlPlaneConfig",
    "ControlPlaneError",
    "ControlPlaneTimeout",
    "ControlPlaneUnavailable",
    "DNSState",
    "EgressVerification",
    "Exhausted",
    "GluetunControlClient",
    "LegacyFallback",
    "ServerCandidate",
    "ShadowComparison",
    "ShadowObserver",
    "ShadowPlan",
    "ShadowTelemetry",
    "SwitchJob",
    "TransportState",
    "TunnelState",
    "TunnelTarget",
    "UnknownTarget",
    "VerificationFailed",
    "load_control_plane_config",
]
