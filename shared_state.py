"""
Shared state module for cross-module access.

Avoids circular imports and module loading issues between opencode.py
and dashboard/api.py by providing a single source of truth for
shared objects like VPN manager and free IP pool.
"""

vpn_manager = None
vpn_manager_2 = None  # retro-compat alias for station 2 (see vpn_managers)
free_ip_pool = None
# Cross-station shared rotation registry (SharedRotationState) — created by
# opencode.py's lifespan once the VPN subsystem boots (None before then).
shared_rotation = None
# [plan 18/08 §4] Active VPN station registry — SOURCE OF TRUTH for the
# N-station hot-reload (GUI dropdown 1-10). 1-indexed list: [0] = station 1,
# [1] = station 2, ... Set by opencode.py's lifespan and updated by
# `_apply_station_count` on hot reload. `vpn_manager` (and any legacy
# manager_2 references in callers) remain retro-compat aliases — all reads
# here must go through the registry.
vpn_managers: list = []
# [plan v10 §4 Lot 1] Superviseurs par station (StationSupervisor) — alignés
# 1:1 avec vpn_managers (même ordre, même sid). Les managers NUS restent la
# registry source de vérité pour les consumers existants ; les superviseurs
# portent l'état d'isolation (tracker latence, breaker local, warm-up).
# Escape hatch : supervisor.enabled=false dans config.yaml → liste vide.
station_supervisors: list = []
# [v6 100%] boot_error — set by lifespan after gather(m.start()) if connected < n (P0-2)
boot_error: str | None = None
