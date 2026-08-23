"""test_vpn_stack_nstation.py — N-station stack switch + downscale stop (plan 18/08 §4).

[plan 18/08 §4] ``_apply_stack`` is no longer a station-1/2 affair: the
active set comes from ``shared_state.vpn_managers`` (registry set by the
lifespan / ``_apply_station_count``) and every active station gets a
``VPN_TYPE_STATION{n}`` substitution in the .env next to the compose file,
with stale keys from downscaled stations PRUNED so an upscale never
resurrects a leftover value.

Covered here (offline — FakeVPNManager from test_vpn_freshness: fake
docker, per-station state files in tmp_path, never touches the live
system):
  * _apply_stack over 4 active stations: writes STATION1..4, preserves the
    OTHER .env keys (secrets live there), purges stale STATION5/7 vars,
    compose argv = 4 active services sorted.
  * no-op when the stack is already effective (no compose, env untouched).
  * wireguard refused when vpn_configs/wireguard.env is missing.
  * downscale 3: stale STATION4 var purged, compose argv = 3 services.
  * fallback legacy station 1/2 pair when the registry is empty (standalone
    manager / unit tests), [self] for a standalone station 3.
  * compose failure → False (and the effective stack is not advanced).
  * stop_container(): exact `compose stop` argv THEN `docker rm -f`
    (downscale-only path — [fix 19/08] the container must be DELETED, not
    left in Exited state) + RuntimeError only when docker rm fails for a
    real reason ("No such container" is success). stop() (shutdown) never
    calls docker.
"""

import os
import subprocess

import pytest

import shared_state
from test_vpn_freshness import FakeVPNManager, _cfg


class _Rec(FakeVPNManager):
    """FakeVPNManager that records every `_docker_run` invocation.

    `_apply_stack` executes its compose command through `_docker_run`
    (asyncio.to_thread — NOT `_compose_up`, which is the connect() path),
    so the recorder witnesses the exact compose argv."""

    def __init__(self, cfg, station=1, shared=None, tmp_path=None, **kw):
        super().__init__(cfg, station=station, shared=shared, tmp_path=tmp_path, **kw)
        self.cmds = []

    def _docker_run(self, args, timeout=30, env=None):
        self.cmds.append((list(args), timeout, dict(env) if env else None))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _env_map(path):
    """Parse a KEY=VALUE .env (or tmp) file into a dict — no exports."""
    vals = {}
    if os.path.exists(path):
        for ln in open(path, "r", encoding="utf-8"):
            line = ln.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip()
    return vals


def _seed_env(tmp_path, **kv):
    with open(tmp_path / ".env", "a", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def _managers(tmp_path, n):
    """n recorder managers wired into the live registry."""
    return [_Rec(_cfg(tmp_path), station=s, tmp_path=tmp_path) for s in range(1, n + 1)]


# ── _apply_stack over the N-station registry ─────────────────────


@pytest.mark.asyncio
async def test_apply_stack_four_stations_writes_env_and_composes(tmp_path, monkeypatch):
    managers = _managers(tmp_path, 4)
    monkeypatch.setattr(shared_state, "vpn_managers", managers, raising=False)
    # WG key present (FakeVPNManager pins it to tmp_path/wireguard.env) …
    (tmp_path / "wireguard.env").write_text("PRIVATE_KEY=x\n", encoding="utf-8")
    # … and every manager forced off the "auto→wireguard" no-op trap.
    for m in managers:
        m._stack_effective = "openvpn"
    # .env next to the compose file carries OTHER keys (VPN_TYPE plain, a
    # secret) plus stale per-station vars from a former N=7 deployment.
    compose = tmp_path / "docker-compose.yml"
    _seed_env(
        tmp_path,
        VPN_TYPE="openvpn",
        SOME_SECRET="abc123",
        VPN_TYPE_STATION5="openvpn",
        VPN_TYPE_STATION7="wireguard",
    )
    monkeypatch.setenv("VPN_DOCKER_COMPOSE_FILE", str(compose))

    assert await managers[0]._apply_stack("wireguard") is True

    env = _env_map(tmp_path / ".env")
    for s in range(1, 5):
        assert env[f"VPN_TYPE_STATION{s}"] == "wireguard"
    assert "VPN_TYPE_STATION5" not in env, "stale downscaled var pruned"
    assert "VPN_TYPE_STATION7" not in env
    assert env["VPN_TYPE"] == "openvpn", "non-station vars preserved"
    assert env["SOME_SECRET"] == "abc123", "secrets preserved"
    (args, timeout, _env) = managers[0].cmds[-1]
    assert args == [
        "compose",
        "-f",
        str(compose),
        "up",
        "-d",
        "--force-recreate",
        "vpn-gluetun",
        "vpn-gluetun-2",
        "vpn-gluetun-3",
        "vpn-gluetun-4",
    ]
    assert timeout == 300
    assert managers[0]._stack_effective == "wireguard"
    assert managers[0]._flips[-1]["to"] == "wireguard"
    assert managers[0]._flips[-1]["reason"] == "manual"


@pytest.mark.asyncio
async def test_apply_stack_noop_when_already_effective(tmp_path, monkeypatch):
    # vpn_stack="auto" + key file present → effective stack resolves to
    # wireguard at __init__ time (the bare _cfg() default openvpn would
    # defeat the setup) — hence the key file is seeded BEFORE construction.
    (tmp_path / "wireguard.env").write_text("PRIVATE_KEY=x\n", encoding="utf-8")
    m = _Rec(_cfg(tmp_path, vpn_stack="auto"), tmp_path=tmp_path)
    assert m._stack_effective == "wireguard"  # key present + auto stack
    monkeypatch.setattr(shared_state, "vpn_managers", [m], raising=False)
    monkeypatch.setenv("VPN_DOCKER_COMPOSE_FILE", str(tmp_path / "c.yml"))

    assert await m._apply_stack("wireguard") is True  # short-circuit
    assert m.cmds == [], "no compose call on a no-op"
    assert not m._flips, "no flip journaled"
    # [fix 19/08] the no-op still re-syncs the .env: an upscaled station
    # must see VPN_TYPE_STATION{n} BEFORE its first compose up (compose
    # default ${VPN_TYPE_STATIONn:-openvpn} would boot it on OpenVPN under
    # a running WireGuard fleet).
    assert _env_map(tmp_path / ".env").get("VPN_TYPE_STATION1") == "wireguard"


@pytest.mark.asyncio
async def test_apply_stack_refuses_wireguard_without_key(tmp_path, monkeypatch):
    m = _Rec(_cfg(tmp_path), tmp_path=tmp_path)
    m._stack_effective = "openvpn"  # no wireguard.env present
    monkeypatch.setattr(shared_state, "vpn_managers", [m], raising=False)
    monkeypatch.setenv("VPN_DOCKER_COMPOSE_FILE", str(tmp_path / "c.yml"))

    assert await m._apply_stack("wireguard") is False
    assert m.cmds == []
    assert not os.path.exists(tmp_path / ".env")


@pytest.mark.asyncio
async def test_apply_stack_downscale_prunes_stale_keys(tmp_path, monkeypatch):
    managers = _managers(tmp_path, 3)  # registry now 3 stations
    monkeypatch.setattr(shared_state, "vpn_managers", managers, raising=False)
    for m in managers:
        m._stack_effective = "wireguard"
    _seed_env(tmp_path, VPN_TYPE_STATION4="wireguard", VPN_TYPE_STATION6="openvpn")
    compose = tmp_path / "docker-compose.yml"
    monkeypatch.setenv("VPN_DOCKER_COMPOSE_FILE", str(compose))

    assert await managers[0]._apply_stack("openvpn") is True

    env = _env_map(tmp_path / ".env")
    for s in range(1, 4):
        assert env[f"VPN_TYPE_STATION{s}"] == "openvpn"
    assert "VPN_TYPE_STATION4" not in env, "downscaled station-4 var pruned"
    assert "VPN_TYPE_STATION6" not in env
    (args, _, _) = managers[0].cmds[-1]
    assert args == [
        "compose",
        "-f",
        str(compose),
        "up",
        "-d",
        "--force-recreate",
        "vpn-gluetun",
        "vpn-gluetun-2",
        "vpn-gluetun-3",
    ]


@pytest.mark.asyncio
async def test_apply_stack_fallback_legacy_pair_when_registry_empty(tmp_path, monkeypatch):
    """No registry (standalone manager / unit test): station 1/2 behave
    exactly as before, a standalone station 3 only targets itself."""
    monkeypatch.setattr(shared_state, "vpn_managers", [], raising=False)
    compose = tmp_path / "docker-compose.yml"
    monkeypatch.setenv("VPN_DOCKER_COMPOSE_FILE", str(compose))
    (tmp_path / "wireguard.env").write_text("PRIVATE_KEY=x\n", encoding="utf-8")

    m1 = _Rec(_cfg(tmp_path), station=1, tmp_path=tmp_path)
    m1._stack_effective = "openvpn"
    assert await m1._apply_stack("wireguard") is True
    env = _env_map(tmp_path / ".env")
    assert env["VPN_TYPE_STATION1"] == "wireguard"
    assert env["VPN_TYPE_STATION2"] == "wireguard"
    assert m1.cmds[-1][0][-2:] == ["vpn-gluetun", "vpn-gluetun-2"]

    m3 = _Rec(_cfg(tmp_path), station=3, tmp_path=tmp_path)
    m3._stack_effective = "openvpn"
    os.remove(tmp_path / ".env")  # fresh env for station 3
    assert await m3._apply_stack("wireguard") is True
    env3 = _env_map(tmp_path / ".env")
    assert set(k for k in env3 if k.startswith("VPN_TYPE_STATION")) == {"VPN_TYPE_STATION3"}
    assert m3.cmds[-1][0][-1] == "vpn-gluetun-3"


@pytest.mark.asyncio
async def test_apply_stack_compose_failure_does_not_advance(tmp_path, monkeypatch):
    class _Fail(_Rec):
        def _docker_run(self, args, timeout=30, env=None):
            self.cmds.append((list(args), timeout, dict(env) if env else None))
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")

    m = _Fail(_cfg(tmp_path), tmp_path=tmp_path)
    (tmp_path / "wireguard.env").write_text("PRIVATE_KEY=x\n", encoding="utf-8")
    m._stack_effective = "openvpn"
    monkeypatch.setattr(shared_state, "vpn_managers", [m], raising=False)
    monkeypatch.setenv("VPN_DOCKER_COMPOSE_FILE", str(tmp_path / "c.yml"))

    assert await m._apply_stack("wireguard") is False
    assert m._stack_effective == "openvpn", "effective stack not advanced"
    assert m._flips == [], "no flip recorded on failure"


# ── stop_container (downscale) vs stop (shutdown) ────────────────


@pytest.mark.asyncio
async def test_stop_container_argv_exact(tmp_path, monkeypatch):
    """[fix 19/08] downscale = compose stop THEN docker rm -f (the retired
    container must disappear from `docker ps -a`; the named volume survives
    since rm is without -v)."""
    m = _Rec(_cfg(tmp_path), tmp_path=tmp_path)
    compose = tmp_path / "docker-compose.yml"
    monkeypatch.setenv("VPN_DOCKER_COMPOSE_FILE", str(compose))
    await m.stop_container()
    assert m.cmds[0][0] == ["compose", "-f", str(compose), "stop", "vpn-gluetun"]
    assert m.cmds[0][1] == 120
    assert m.cmds[0][2]["VPN_TYPE_STATION1"] == m._stack_effective, (
        "compose stop child carries the explicit stack env (garde 2.1)"
    )
    assert m.cmds[1] == (["rm", "-f", "opencode-vpn"], 120, None)


@pytest.mark.asyncio
async def test_stop_container_rm_failed_is_real_error(tmp_path, monkeypatch):
    """compose stop failure is best-effort (container may already be dead) —
    only a docker rm failure that is NOT 'No such container' raises."""

    class _Fail(_Rec):
        def _docker_run(self, args, timeout=30, env=None):
            self.cmds.append((list(args), timeout, dict(env) if env else None))  # record, then fail
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")

    m = _Fail(_cfg(tmp_path), tmp_path=tmp_path)
    monkeypatch.setenv("VPN_DOCKER_COMPOSE_FILE", str(tmp_path / "c.yml"))
    # "docker rm failed" (not "compose stop failed") proves the rm WAS
    # attempted after the degraded compose stop.
    with pytest.raises(RuntimeError, match="docker rm failed"):
        await m.stop_container()


@pytest.mark.asyncio
async def test_stop_container_already_removed_is_ok(tmp_path, monkeypatch):
    """'No such container' from docker rm is success — a station deleted by
    an earlier pass must not raise (downscale is idempotent)."""

    class _Gone(_Rec):
        def _docker_run(self, args, timeout=30, env=None):
            stderr = "Not Found: No such container: opencode-vpn" if args[0] == "rm" else ""
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=stderr)

    m = _Gone(_cfg(tmp_path), tmp_path=tmp_path)
    monkeypatch.setenv("VPN_DOCKER_COMPOSE_FILE", str(tmp_path / "c.yml"))
    await m.stop_container()  # must NOT raise


@pytest.mark.asyncio
async def test_stop_never_calls_docker(tmp_path, monkeypatch):
    """Proxy shutdown is state-only: stop() persists, docker is untouched
    (the tunnel is compose-managed and survives restarts)."""
    m = _Rec(_cfg(tmp_path), tmp_path=tmp_path)
    m._current_ip = "1.2.3.4"
    monkeypatch.setenv("VPN_DOCKER_COMPOSE_FILE", str(tmp_path / "c.yml"))
    await m.stop()
    assert m.cmds == [], "stop() must never touch docker"
    assert m._state_file and os.path.exists(m._state_file), "stop() persists the vpn state"
