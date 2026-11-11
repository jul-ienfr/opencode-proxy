"""Architecture gates for the qualification package.

- the pure domain never imports I/O, providers, SQLite or the legacy manager;
- no import cycles inside ``free.qualification``;
- every new module stays under 1 500 lines;
- the country gate and one-rotation-at-a-time rules stay removed.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUALIFICATION = ROOT / "free" / "qualification"
DOMAIN_FILES = (
    QUALIFICATION / "domain.py",
    QUALIFICATION / "scoring.py",
    QUALIFICATION / "transport.py",
    QUALIFICATION / "rotation.py",
)
FORBIDDEN_PREFIXES = (
    "sqlite3",
    "docker",
    "requests",
    "httpx",
    "free.ip_registry",
    "free.egress_store",
    "free.pool",
    "vpn",
    "ops",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = "free.qualification"
                found.add(f"{base}.{node.module}" if node.module else base)
            elif node.module:
                found.add(node.module)
    return found


def test_domain_is_pure():
    for path in DOMAIN_FILES:
        for module in _imports(path):
            assert not module.startswith(FORBIDDEN_PREFIXES), f"{path.name} imports {module}"


def test_qualification_modules_stay_under_1500_lines():
    for path in sorted(QUALIFICATION.glob("*.py")):
        lines = len(path.read_text(encoding="utf-8").splitlines())
        assert lines <= 1500, f"{path.name} has {lines} lines"


def test_no_import_cycle_in_qualification():
    graph: dict[str, set[str]] = {}
    for path in QUALIFICATION.glob("*.py"):
        name = path.stem
        graph[name] = {
            module.rsplit(".", 1)[-1]
            for module in _imports(path)
            if module.startswith("free.qualification") or module.startswith(".")
        }
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        assert node not in visiting, f"import cycle at {node}"
        if node in done or node not in graph:
            return
        visiting.add(node)
        for child in graph[node]:
            visit(child)
        visiting.remove(node)
        done.add(node)

    for name in graph:
        visit(name)


def test_country_gate_is_removed():
    claims = (ROOT / "free" / "catalog_claims.py").read_text(encoding="utf-8")
    fresh = (ROOT / "scripts" / "fresh_chain.py").read_text(encoding="utf-8")
    assert 'code == "country_backoff"' not in claims
    assert "country_fails" not in fresh
    assert "country_noegress" not in fresh
    assert "country_backoff[" not in fresh


def test_rotation_planner_allows_concurrency():
    source = (ROOT / "free" / "rotation_planner.py").read_text(encoding="utf-8")
    assert "max_concurrent: int = 6" in source
    assert "jamais deux rotations" not in source


def test_global_pause_is_opt_in_and_disabled_by_default():
    supervisor = (ROOT / "scripts" / "fresh_chain_supervisor.py").read_text(encoding="utf-8")
    config = (ROOT / "config.yaml").read_text(encoding="utf-8")
    assert "global_pause_enabled" in supervisor
    assert "if global_pause_enabled and paused_until" in supervisor
    assert "global_pause_enabled: false" in config


def test_fast_discovery_shortcut_is_off_in_auto_mode():
    fresh = (ROOT / "scripts" / "fresh_chain.py").read_text(encoding="utf-8")
    assert 'if str(args.transport) == "auto":' in fresh
    assert "fast_discovery = False" in fresh
    assert "auto mode walks every transport" in fresh
