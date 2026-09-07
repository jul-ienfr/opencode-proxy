"""Audit une fois : attributs lus sur les modules shimés par les tests."""

import ast
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402

SHIM_ALIASES = {
    "vm": "vpn_manager",
    "vpn_manager": "vpn_manager",
    "fp": "free_ip_pool",
    "fip": "free_ip_pool",
    "pm": "protocol_mapping",
    "tc_module": "traffic_capture",
    "shared_rotation": "shared_rotation",
    "latency_rotation": "latency_rotation",
    "_app_db": "app.db",
}

used: dict[str, set[str]] = {m: set() for m in set(SHIM_ALIASES.values())}


class V(ast.NodeVisitor):
    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Name) and node.value.id in SHIM_ALIASES:
            used[SHIM_ALIASES[node.value.id]].add(node.attr)
        self.generic_visit(node)

    def visit_Call(self, node):
        # monkeypatch.setattr(vm, "_docker_cli", ...) — cible string en 2e position
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in SHIM_ALIASES
            and isinstance(node.args[1], ast.Constant)
        ):
            used[SHIM_ALIASES[node.args[0].id]].add(str(node.args[1].value) + " [rebind]")
        # mock.patch("vpn_manager.X") — cible string en 1re position
        args = node.args
        if args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
            val = args[0].value
            if "." in val:
                mod, _, attr = val.partition(".")
                if mod in SHIM_ALIASES and attr.isidentifier():
                    used[SHIM_ALIASES[mod]].add(attr + " [via-string]")
        self.generic_visit(node)


for fn in sorted(os.listdir("tests")):
    if not fn.startswith("test_") or not fn.endswith(".py"):
        continue
    tree = ast.parse(open(os.path.join("tests", fn), encoding="utf-8").read())
    V().visit(tree)

for mod in sorted(used):
    m = importlib.import_module(mod)
    have = {n for n in dir(m) if not n.startswith("__")}
    missing = sorted(a for a in used[mod] if a not in have)
    print(f"== {mod}: {len(used[mod])} attrs lus, manquants={missing}")
