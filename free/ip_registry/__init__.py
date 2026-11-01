"""IP registry and station allocator — Phase 0 foundation.

Pure domain, scoring and eligibility rules.  No I/O, no provider import.
"""

from .domain import (
    BindResult,
    GeoFilter,
    IpRecord,
    IpState,
    Lease,
    NeedReason,
    ProbeResult,
    RuntimeResult,
    Server,
    can_transition,
)
from .eligibility import EligibilityDecision, check_eligibility
from .scoring import ScoreWeights, score_record

__all__ = [
    "BindResult",
    "EligibilityDecision",
    "GeoFilter",
    "IpRecord",
    "IpState",
    "Lease",
    "NeedReason",
    "ProbeResult",
    "RuntimeResult",
    "ScoreWeights",
    "Server",
    "can_transition",
    "check_eligibility",
    "score_record",
]
