"""IP registry and station allocator — Phase 0 foundation.

Pure domain, scoring and eligibility rules.  No I/O, no provider import.
"""

from .adapters import (
    ComposeHostnameBinder,
    GluetunControlBinder,
    HybridBinder,
    StationEgress,
    StationProbe,
    host_public_ip,
)
from .allocator import AllocationOutcome, AllocationReport, IpAllocator, StationNeed
from .catalog import NordVpnCatalog
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
from .dns import DnsFallback
from .eligibility import EligibilityDecision, check_eligibility
from .scoring import ScoreWeights, score_record
from .sources import SourceManifest, SourceRegistry, SourceSnapshot
from .sqlite_registry import SqliteIpRegistry
from .status import registry_status

__all__ = [
    "AllocationOutcome",
    "AllocationReport",
    "BindResult",
    "ComposeHostnameBinder",
    "DnsFallback",
    "GluetunControlBinder",
    "HybridBinder",
    "EligibilityDecision",
    "GeoFilter",
    "IpAllocator",
    "IpRecord",
    "IpState",
    "Lease",
    "NeedReason",
    "NordVpnCatalog",
    "ProbeResult",
    "RuntimeResult",
    "ScoreWeights",
    "Server",
    "SourceManifest",
    "SourceRegistry",
    "SourceSnapshot",
    "SqliteIpRegistry",
    "StationEgress",
    "StationNeed",
    "StationProbe",
    "host_public_ip",
    "can_transition",
    "check_eligibility",
    "registry_status",
    "score_record",
]
