#!/usr/bin/env python3
"""Release gate for the qualification feature.

Runs the test suite, lint, config audit, module ratchet, dependency cycles,
benchmark budgets and coverage.  Any red step blocks the release.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUALIFICATION_TESTS = [
    "tests/test_qualification_domain.py",
    "tests/test_qualification_scoring.py",
    "tests/test_qualification_transport.py",
    "tests/test_qualification_store.py",
    "tests/test_qualification_source.py",
    "tests/test_qualification_sweep.py",
    "tests/test_qualification_preflight.py",
    "tests/test_qualification_station_adapter.py",
    "tests/test_qualification_readmodel.py",
    "tests/test_qualification_diagnostics.py",
    "tests/test_qualification_metrics.py",
    "tests/test_qualification_faults.py",
    "tests/test_qualification_contracts.py",
    "tests/test_qualification_daemon_control.py",
    "tests/test_qualification_resume.py",
    "tests/test_qualification_security.py",
    "tests/test_qualification_observability.py",
    "tests/test_qualification_scheduler.py",
    "tests/test_egress_runtime_qualification.py",
    "tests/test_qualification_discovery.py",
    "tests/test_qualification_watchdog.py",
    "tests/test_qualification_station_probe.py",
    "tests/test_qualification_repair_adapter.py",
    "tests/test_qualification_cutover.py",
    "tests/test_qualification_cutover_adapter.py",
    "tests/test_qualification_station_runtime.py",
    "tests/test_qualification_rotation.py",
    "tests/test_qualification_rotation_executor.py",
    "tests/test_qualification_rotation_batch.py",
    "tests/test_qualification_runtime.py",
    "tests/test_qualification_api.py",
    "tests/test_qualification_launcher.py",
    "tests/test_pool_characterization.py",
    "tests/test_quota_ledger_characterization.py",
    "tests/test_catalog_claims.py",
    "tests/test_repair_queue.py",
    "tests/test_fresh_chain_lock.py",
    "tests/test_fresh_chain_candidates.py",
    "tests/test_architecture_reuse.py",
    "tests/test_dashboard_parity.py",
    "tests/test_station_isolation.py",
    "tests/test_canary_runner.py",
    "tests/test_qualification_release.py",
    "tests/test_qualification_canary.py",
    "tests/test_qualification_ui_contract.py",
    "tests/test_egress_catalog_routes.py",
    "tests/test_egress_catalog_mutations.py",
    "tests/test_nordvpn_routes.py",
    "tests/test_station_routes.py",
    "tests/test_free_routes.py",
    "tests/test_pool_routes.py",
    "tests/test_retry_routes.py",
    "tests/test_usage_routes.py",
    "tests/test_quota_routes.py",
    "tests/test_event_routes.py",
    "tests/test_capacity_routes.py",
    "tests/test_vpn_config_routes.py",
    "tests/test_vpn_control_routes.py",
    "tests/test_vpn_fleet_routes.py",
    "tests/test_vpn_health_routes.py",
    "tests/test_vpn_heal_routes.py",
    "tests/test_vpn_next_routes.py",
    "tests/test_vpn_update_routes.py",
    "tests/test_station_control_routes.py",
    "tests/test_vpn_state_routes.py",
    "tests/test_vpn_import_routes.py",
    "tests/test_vpn_jobs_routes.py",
    "tests/test_vpn_config_update_routes.py",
    "tests/test_config_extra_routes.py",
    "tests/test_config_routes.py",
    "tests/test_debug_routes.py",
    "tests/test_egress_mode_routes.py",
    "tests/test_proxy_backend_routes.py",
    "tests/test_proxy_routes.py",
    "tests/test_root_routes.py",
    "tests/test_vpn_status_routes.py",
    "tests/test_route_table_routes.py",
    "tests/test_vpn_diagnostic_routes.py",
    "tests/test_geo_routes.py",
    "tests/test_cost_routes.py",
    "tests/test_adaptive_routes.py",
    "tests/test_stats_routes.py",
    "tests/test_history_routes.py",
    "tests/test_traffic_routes.py",
    "tests/test_station_logs_routes.py",
    "tests/test_manager_characterization.py",
    "tests/test_qualification_module_size.py",
    "tests/test_qualification_architecture.py",
    "tests/test_option_map.py",
    "tests/test_rotation_schema.py",
]
RUFF_TARGETS = [
    "free/qualification",
    "scripts/qualification_sweep.py",
    "scripts/bench_qualification.py",
    "scripts/dependency_graph.py",
    "scripts/module_size_audit.py",
    "scripts/quality_gate.py",
    "scripts/soak_qualification.py",
    "dashboard/api.py",
    "dashboard/routers",
]


def run(label: str, command: list[str], *, timeout: int = 300) -> bool:
    print(f"\n=== {label} ===")
    result = subprocess.run(command, cwd=ROOT, text=True, timeout=timeout)
    if result.returncode != 0:
        print(f"FAIL {label} (exit {result.returncode})")
        return False
    print(f"PASS {label}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-bench", action="store_true")
    parser.add_argument("--skip-coverage", action="store_true")
    parser.add_argument("--with-soak", action="store_true")
    args = parser.parse_args()

    steps: list[tuple[str, bool]] = []
    steps.append(("pytest", run("pytest", [sys.executable, "-m", "pytest", *QUALIFICATION_TESTS, "-q"])))
    steps.append(("ruff", run("ruff", [sys.executable, "-m", "ruff", "check", *RUFF_TARGETS])))
    steps.append(
        (
            "mypy",
            run(
                "mypy",
                [
                    sys.executable,
                    "-m",
                    "mypy",
                    "free/qualification",
                    "--ignore-missing-imports",
                    "--follow-imports=skip",
                    "--no-error-summary",
                ],
            ),
        )
    )
    steps.append(("config_audit", run("config_audit", [sys.executable, "scripts/config_audit.py"])))
    steps.append(
        (
            "module_ratchet",
            run(
                "module_ratchet",
                [
                    sys.executable,
                    "scripts/module_size_audit.py",
                    "--root",
                    ".",
                    "--max-lines",
                    "1500",
                    "--ratchet",
                    "specs/014-nordvpn-exhaustive-rotation/module-baseline.json",
                ],
            ),
        )
    )
    steps.append(
        (
            "dependency_cycles",
            run(
                "dependency_cycles",
                [sys.executable, "scripts/dependency_graph.py", "--root", "free/qualification", "--fail-on-cycles"],
            ),
        )
    )
    if not args.skip_bench:
        steps.append(
            (
                "benchmark",
                run(
                    "benchmark",
                    [sys.executable, "scripts/bench_qualification.py", "--candidates", "5000", "--json"],
                ),
            )
        )
    if args.with_soak:
        steps.append(
            (
                "soak",
                run(
                    "soak",
                    [sys.executable, "scripts/soak_qualification.py", "--duration", "10", "--json"],
                    timeout=120,
                ),
            )
        )
    if not args.skip_coverage:
        coverage_run = run(
            "coverage_run",
            [sys.executable, "-m", "coverage", "run", "--source=free/qualification", "-m", "pytest", *QUALIFICATION_TESTS, "-q"],
            timeout=300,
        )
        coverage_report = run(
            "coverage_report",
            [
                sys.executable,
                "-m",
                "coverage",
                "report",
                "--include=free/qualification/*",
                "--fail-under=85",
            ],
        )
        steps.append(("coverage", coverage_run and coverage_report))

    failed = [name for name, ok in steps if not ok]
    print("\n=== SUMMARY ===")
    for name, ok in steps:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
