"""NordVPN qualification pipeline.

Public surface:

- ``Candidate``/``Attempt`` and the pure classifications;
- ``potential_score``/``measured_score``;
- ``TransportPlan`` and ``build_plan``;
- ``QualificationStore``;
- ``NordVpnCatalogSource``;
- ``QualificationSweep`` and its injectable station adapter.

The package is adapter-driven: no Docker, network or provider import at module
load.  Production injects the existing Gluetun/manager path; tests inject fakes.
"""

from __future__ import annotations

from .cutover import CutoverPorts, CutoverResult, CutoverService, CutoverTarget
from .discovery import DiscoveryConfig, DiscoveryReport, DiscoveryService
from .domain import (
    Attempt,
    AttemptOutcome,
    AttemptStatus,
    Candidate,
    CandidateStatus,
    classify_outcome,
    infra_backoff,
    is_claimable,
    next_candidate_status,
    order_key,
)
from .preflight import PreflightReport, PreflightService
from .qualifiers import (
    QualifierConfig,
    QualifierManager,
    build_qualifier_manager,
    persist_qualifier_request,
    qualifier_config_from_settings,
    qualifier_config_snapshot,
)
from .repair_adapter import SupervisorRepairAdapter, build_supervisor_repair_adapter
from .rotation import RotationDecision, RotationPlanner, StationSnapshot
from .rotation_batch import RotationBatch, RotationBatchResult, build_rotation_batch
from .rotation_executor import RotationExecutor, RotationResult
from .runtime import (
    QualificationRuntime,
    build_cutover_service,
    build_qualification,
    build_qualification_scheduler,
)
from .scoring import MeasuredWeights, PotentialWeights, measured_score, potential_score, reliability
from .source import CatalogStats, NordVpnCatalogSource
from .station_adapter import RegistryStationAdapter, build_registry_station_adapter, transport_parts
from .station_probe import HttpStationProbe, build_http_station_probe
from .station_runtime import (
    ParallelReport,
    ParallelStationRunner,
    StationWorker,
)
from .store import QualificationStore
from .sweep import QualificationSweep, StationAdapter, SweepConfig, SweepResult
from .transport import DEFAULT_TRANSPORT_ORDER, TransportPlan, build_plan, next_transport
from .watchdog import (
    StationObservation,
    WatchdogConfig,
    WatchdogReport,
    WatchdogService,
    classify_observation,
)

__all__ = [
    "Attempt",
    "AttemptOutcome",
    "AttemptStatus",
    "Candidate",
    "CandidateStatus",
    "CatalogStats",
    "DEFAULT_TRANSPORT_ORDER",
    "CutoverPorts",
    "CutoverResult",
    "CutoverService",
    "CutoverTarget",
    "DiscoveryConfig",
    "DiscoveryReport",
    "DiscoveryService",
    "HttpStationProbe",
    "MeasuredWeights",
    "NordVpnCatalogSource",
    "ParallelReport",
    "ParallelStationRunner",
    "PotentialWeights",
    "PreflightReport",
    "PreflightService",
    "QualificationRuntime",
    "QualificationStore",
    "QualificationSweep",
    "QualifierConfig",
    "QualifierManager",
    "RegistryStationAdapter",
    "RotationDecision",
    "RotationExecutor",
    "RotationPlanner",
    "RotationResult",
    "RotationBatch",
    "RotationBatchResult",
    "build_rotation_batch",
    "StationAdapter",
    "StationObservation",
    "StationSnapshot",
    "StationWorker",
    "SupervisorRepairAdapter",
    "SweepConfig",
    "SweepResult",
    "TransportPlan",
    "WatchdogConfig",
    "WatchdogReport",
    "WatchdogService",
    "build_plan",
    "build_qualification",
    "build_qualification_scheduler",
    "build_qualifier_manager",
    "build_cutover_service",
    "build_http_station_probe",
    "build_registry_station_adapter",
    "build_supervisor_repair_adapter",
    "classify_outcome",
    "classify_observation",
    "infra_backoff",
    "is_claimable",
    "measured_score",
    "next_candidate_status",
    "next_transport",
    "order_key",
    "persist_qualifier_request",
    "potential_score",
    "qualifier_config_from_settings",
    "qualifier_config_snapshot",
    "reliability",
    "transport_parts",
]
