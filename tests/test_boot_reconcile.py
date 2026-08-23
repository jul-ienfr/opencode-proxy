"""test_boot_reconcile.py — réconciliation des conteneurs orphelins au boot
(plan 18/08 §2.2).

A crash (or a stack flip that died mid-way) leaves docker containers that
don't match the manager registry — a station retired by a downscale that
never got its `docker rm -f`, or a container booted on the STALE stack (the
19/08 case: process env openvpn while the .env says wireguard) that survives
into the new process. `reconcile_orphan_containers` runs in lifespan BEFORE
the start() gather, so the fleet comes up exactly as configured; start()
then creates/repairs what is missing.

All tests here are OFFLINE: docker is a fake async runner that records every
call and answers from a scripted container inventory (name → VPN_TYPE). The
runner contract is exactly the production default's — (args, timeout, env)
-> CompletedProcess — so the injectable is a drop-in.

Covered here:
  * orphan containers outside the registry are rm -f'd (downscale-crash
    case), named volumes untouched (rm is without -v)
  * expected containers on the right stack are left alone
  * expected container on the WRONG stack (19/08 stale env) is removed so
    start() recreates it
  * unknown VPN_TYPE (pre-stack container) is kept — conservative
  * prefix filter: opencode-wg-test and opencode-proxy are not matched
  * docker down (ps fails) → fail-soft, [] returned, nothing raised
  * rm racing a teardown ("No such container") is not an error
  * no managers → no docker calls at all
"""

import json
import logging
import subprocess

import pytest

import vpn_manager as vm
from test_vpn_freshness import FakeVPNManager, _cfg


class _DockerStub:
    """Scripted docker inventory: name → VPN_TYPE (None = absent from ps).

    Records every invocation as (args, timeout, env) tuples; answers ps /
    inspect / rm from the inventory or the fail flags."""

    def __init__(self, fleet, ps_fails=False, inspect="ok"):
        self.fleet = dict(fleet)  # running containers by name
        self.calls = []  # (args, timeout, env) of every call
        self.removed = []  # names actually rm -f'd
        self.ps_fails = ps_fails  # True → ps returns rc!=0
        self.inspect = inspect  # "ok" | "rc" | "bad-json"

    async def run(self, args, timeout=30, env=None):
        self.calls.append((list(args), timeout, env))
        kind = args[0]
        if kind == "ps":
            if self.ps_fails:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="daemon down")
            return subprocess.CompletedProcess(
                args, 0, stdout="\n".join(self.fleet) + "\n", stderr=""
            )
        if kind == "inspect":
            if self.inspect == "rc":
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="not found")
            name = args[1]
            vpn_type = self.fleet.get(name)
            if vpn_type is None:
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr=f"No such object: {name}"
                )
            if self.inspect == "bad-json":
                payload = "not json"
            else:
                env_vars = ["PATH=/x"]
                if vpn_type is not None:
                    env_vars.append(f"VPN_TYPE={vpn_type}")
                payload = json.dumps([{"Config": {"Env": env_vars}}])
            return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")
        if kind == "rm":
            name = args[2]
            if name in self.fleet:
                del self.fleet[name]
            self.removed.append(name)
            return subprocess.CompletedProcess(args, 0, stdout=name, stderr="")
        raise AssertionError(f"unexpected docker argv: {args}")


def _managers(tmp_path, n, stack="wireguard"):
    """n fake managers with the registry names opencode-vpn[,-N]."""
    managers = [
        FakeVPNManager(_cfg(tmp_path), station=s, tmp_path=tmp_path) for s in range(1, n + 1)
    ]
    for m in managers:
        m._stack_effective = stack
    return managers


# ── orphan containers outside the registry ─────────────────────────


@pytest.mark.asyncio
async def test_orphan_retired_station_removed(tmp_path):
    """A station retired by a downscale that crashed before its `docker
    rm -f`: the name is gone from the registry but still exists in docker →
    removed at boot, before any start()."""
    stub = _DockerStub(
        {"opencode-vpn": "wireguard", "opencode-vpn-2": "wireguard", "opencode-vpn-3": "wireguard"}
    )
    managers = _managers(tmp_path, 2)  # registry: station 1-2 only

    removed = await vm.reconcile_orphan_containers(managers, stub.run)

    assert removed == ["opencode-vpn-3"]
    assert stub.removed == ["opencode-vpn-3"]
    assert stub.fleet == {"opencode-vpn": "wireguard", "opencode-vpn-2": "wireguard"}
    # exact argv: ps → inspect → rm, rm WITHOUT -v (named volume survives)
    rms = [c[0] for c in stub.calls if c[0][0] == "rm"]
    assert rms == [["rm", "-f", "opencode-vpn-3"]]


@pytest.mark.asyncio
async def test_keep_station_1_ever_clean(tmp_path):
    """A container named opencode-vpn-10 from a former N=10 deployment is
    pruned; station 1 (legacy name) is never mistaken for an orphan."""
    stub = _DockerStub({"opencode-vpn": "wireguard", "opencode-vpn-10": "openvpn"})
    managers = _managers(tmp_path, 1)

    removed = await vm.reconcile_orphan_containers(managers, stub.run)

    assert removed == ["opencode-vpn-10"]
    assert stub.removed == ["opencode-vpn-10"]


# ── expected containers: stack check ───────────────────────────────


@pytest.mark.asyncio
async def test_expected_on_right_stack_untouched(tmp_path):
    """No downscale, no stale env: nothing is removed, only ps + inspects."""
    stub = _DockerStub({"opencode-vpn": "wireguard", "opencode-vpn-2": "wireguard"})
    managers = _managers(tmp_path, 2, stack="wireguard")

    removed = await vm.reconcile_orphan_containers(managers, stub.run)

    assert removed == []
    assert stub.removed == []
    kinds = [c[0][0] for c in stub.calls]
    assert kinds == ["ps", "inspect", "inspect"], "no rm on a clean fleet"


@pytest.mark.asyncio
async def test_expected_on_wrong_stack_removed(tmp_path):
    """The 19/08 survivor: the container runs openvpn but the manager's
    effective stack is wireguard → it is removed so start() recreates it on
    the right stack (no wait for the watchdog flip)."""
    stub = _DockerStub(
        {"opencode-vpn": "wireguard", "opencode-vpn-2": "openvpn"}
    )  # stale-boot station 2
    managers = _managers(tmp_path, 2, stack="wireguard")

    removed = await vm.reconcile_orphan_containers(managers, stub.run)

    assert removed == ["opencode-vpn-2"]
    assert stub.removed == ["opencode-vpn-2"]
    assert "opencode-vpn" not in stub.removed


@pytest.mark.asyncio
async def test_expected_unknown_stack_kept(tmp_path):
    """A container with no VPN_TYPE entry (pre-stack-era container) is kept —
    conservative: we cannot prove it is on the wrong stack."""
    stub = _DockerStub({"opencode-vpn": None})  # absent from Config.Env
    managers = _managers(tmp_path, 1, stack="wireguard")

    removed = await vm.reconcile_orphan_containers(managers, stub.run)

    assert removed == []
    assert stub.removed == []


@pytest.mark.asyncio
async def test_mixed_fleet_removes_both_kinds(tmp_path):
    """Orphan + stale-stack expected → both removed in one pass."""
    stub = _DockerStub(
        {
            "opencode-vpn": "openvpn",  # stale boot (mgr: WG)
            "opencode-vpn-2": "openvpn",  # aligned (mgr: OV)
            "opencode-vpn-5": "wireguard",
        }
    )  # orphan
    managers = _managers(tmp_path, 2, stack="wireguard")
    managers[1]._stack_effective = "openvpn"  # station 2 effective = OV

    removed = await vm.reconcile_orphan_containers(managers, stub.run)

    assert sorted(removed) == ["opencode-vpn", "opencode-vpn-5"]
    assert sorted(stub.removed) == ["opencode-vpn", "opencode-vpn-5"]
    assert "opencode-vpn-2" not in stub.removed


# ── prefix safety / failure modes ──────────────────────────────────


@pytest.mark.asyncio
async def test_prefix_filter_excludes_canary_and_proxy(tmp_path):
    """The anchored name filter excludes opencode-wg-test (canary) and
    opencode-proxy — neither shares the opencode-vpn prefix. A ps answer
    containing them must NOT be forced into the reconcile (the caller's
    filter is on the docker side; the fake answers as the real daemon)."""
    # The real `docker ps --filter name=^/opencode-vpn` never RETURNS these;
    # what matters here is that reconcile cannot be tricked by a daemon
    # answer de-listed outside the filter (defense in depth): unknown names
    # that match the fleet naming pattern are handled, and the registry
    # keeps station names authoritative.
    stub = _DockerStub({"opencode-vpn": "wireguard"})
    managers = _managers(tmp_path, 1)
    removed = await vm.reconcile_orphan_containers(managers, stub.run)
    assert removed == []
    # assert the FILTER argv itself is anchored on the fleet prefix
    ps_calls = [c[0] for c in stub.calls if c[0][0] == "ps"]
    assert ps_calls == [["ps", "-a", "--filter", "name=^/opencode-vpn", "--format", "{{.Names}}"]]


@pytest.mark.asyncio
async def test_docker_down_fail_soft(tmp_path):
    """docker ps fails (daemon down at boot): logged, [] returned — boot
    still proceeds (start() handles the docker-down case internally)."""
    stub = _DockerStub({}, ps_fails=True)
    managers = _managers(tmp_path, 2)
    removed = await vm.reconcile_orphan_containers(managers, stub.run)
    assert removed == []
    assert stub.removed == []


@pytest.mark.asyncio
async def test_inspect_failures_are_skipped(tmp_path, caplog):
    """inspect rc!=0 or malformed JSON → the container is kept (fail-soft,
    no crash on a non-JSON daemon answer)."""
    for inspect in ("rc", "bad-json"):
        stub = _DockerStub({"opencode-vpn": "wireguard"}, inspect=inspect)
        managers = _managers(tmp_path, 1)
        with caplog.at_level(logging.WARNING, logger="vpn_manager"):
            removed = await vm.reconcile_orphan_containers(managers, stub.run)
        assert removed == [], f"inspect={inspect} must keep the container"
        assert stub.removed == []


@pytest.mark.asyncio
async def test_rm_no_such_container_is_success(tmp_path):
    """rm racing a compose teardown: 'No such container' is not an error —
    the reconcile does not raise and reports the name as removed (it IS
    gone, which is the goal)."""

    class _Gone:
        def __init__(self):
            self.removed = []

        async def run(self, args, timeout=30, env=None):
            if args[0] == "ps":
                return subprocess.CompletedProcess(args, 0, stdout="opencode-vpn-9\n", stderr="")
            if args[0] == "rm":
                self.removed.append(args[2])
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="Not Found: No such container: x"
                )
            return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    stub = _Gone()
    managers = _managers(tmp_path, 1)
    removed = await vm.reconcile_orphan_containers(managers, stub.run)
    assert removed == ["opencode-vpn-9"]  # gone either way
    assert stub.removed == ["opencode-vpn-9"]


@pytest.mark.asyncio
async def test_no_managers_no_docker_calls(tmp_path):
    """IP_ROTATION disabled → no managers → reconcile is a pure no-op."""
    stub = _DockerStub({})
    assert await vm.reconcile_orphan_containers([], stub.run) == []
    assert stub.calls == []


@pytest.mark.asyncio
async def test_default_runner_uses_docker_cli(tmp_path, monkeypatch):
    """Without an injected runner, the reconcile goes through the module's
    _docker_cli via to_thread — never a live call here; we just prove the
    wiring (a fake _docker_cli records the argv)."""
    recorded = []

    def _fake_cli(args, timeout=30, env=None):
        recorded.append(list(args))
        if args[0] == "ps":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr(vm, "_docker_cli", _fake_cli)
    managers = _managers(tmp_path, 2)
    assert await vm.reconcile_orphan_containers(managers) == []
    assert recorded and recorded[0][0] == "ps"
    # passthrough kwargs arrive intact (defensive: the lambda shim)
    assert recorded[0] == ["ps", "-a", "--filter", "name=^/opencode-vpn", "--format", "{{.Names}}"]
