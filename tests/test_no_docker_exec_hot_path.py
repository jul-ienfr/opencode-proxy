"""T079: the active path must never shell out to Docker.

``docker exec``/``docker cp`` + parsing are the cold legacy wrapper.  The hot
path (typed client, rotation binder, dashboard actions, rollout) talks HTTP to
the fork and nothing else; this test fails the build if a shell command sneaks
back in.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

HOT_PATH = (
    "free/control_plane/client.py",
    "free/control_plane/models.py",
    "free/control_plane/errors.py",
    "free/control_plane/config.py",
    "free/control_plane/events.py",
    "free/control_plane/shadow.py",
    "free/control_plane/telemetry.py",
    "free/ip_registry/adapters/gluetun_control_v2.py",
    "dashboard/routers/station_control_routes.py",
    "scripts/rollout_control_plane.py",
)

SHELL_MODULES = {"subprocess", "shlex"}
SHELL_CALLS = {
    "system",
    "popen",
    "run",
    "call",
    "check_call",
    "check_output",
    "Popen",
}


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _offenders(source: str, relative: str) -> list[str]:
    findings: list[str] = []
    tree = ast.parse(source, filename=relative)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in SHELL_MODULES:
                    findings.append(f"{relative}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if module in SHELL_MODULES:
                findings.append(f"{relative}: from {node.module} import")
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name not in SHELL_CALLS:
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                owner = func.value
                if isinstance(owner, ast.Name) and owner.id in {"os", "subprocess"}:
                    findings.append(f"{relative}: {owner.id}.{name}()")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.strip().lower()
            if text in {"docker", "docker.exe"} or text.startswith("docker "):
                findings.append(f"{relative}: command literal {text!r}")
    return findings


def test_hot_path_never_shells_out():
    offenders: list[str] = []
    for relative in HOT_PATH:
        source = (ROOT / relative).read_text(encoding="utf-8")
        offenders.extend(_offenders(source, relative))
    assert offenders == [], offenders


def test_hot_binder_does_not_import_the_legacy_wrapper():
    source = (ROOT / "free/ip_registry/adapters/gluetun_control_v2.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[-1])
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[-1] for alias in node.names)
    assert "gluetun_control" not in imported
    assert "hybrid_binder" not in imported


def test_legacy_adapter_is_explicitly_marked_cold():
    text = (ROOT / "free/ip_registry/adapters/gluetun_control.py").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "cold path only" in lowered
    assert "legacyfallback" in lowered
    assert "docker exec" in lowered
