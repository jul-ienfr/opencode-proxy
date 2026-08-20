"""test_env_staleness_guard.py — garde anti-env périmé (plan 18/08 §2.1).

The 19/08 root cause: settings.load_env_file() only fills os.environ when a
key is ABSENT, so a parent env loaded at boot wins over the .env file for
every `docker compose` child. A sed on the file was therefore invisible
until the process restarted, and each station rebooted on the STALE stack.

The fix has two halves, both covered here offline (FakeVPNManager — fake
docker, per-station state in tmp_path, never a live container):

  * Déterministe — every compose call site hands the child an EXPLICIT
    env (vpn_manager._compose_env) that carries VPN_TYPE_STATION{n} = the
    stack, overriding whatever stale value the parent inherited. The
    compose interpolation ${VPN_TYPE_STATION{n}:-openvpn} is the ONLY
    surface that decides a station's stack, so this is the single lever.
  * Détection — load_env_file() compares the process env against the file
    at boot: a VPN_* key whose os.environ value differs from the file is
    journaled in settings.ENV_DIVERGENCE and warned (compose children
    would inherit the stale env) → exposed by /api/vpn-status as
    env_divergence (dashboard banner).

Covered here:
  * _compose_env: single station, N stations, explicit target stack,
    parent env inherited, parent never mutated
  * all six compose call sites pass the env through the fake runner:
    _compose_up (start path), _apply_stack (flip → TARGET stack, not the
    effective one), stop_container (downscale), check_update pull,
    apply_update compose, rollback compose
  * load_env_file: divergent VPN_* flagged + warned; aligned keys not
    flagged; non-VPN_* divergence ignored (the compose interpolation only
    reads VPN_TYPE_STATION*); absent keys loaded
  * /api/vpn-status exposes env_divergence (the dashboard wiring)
"""
import logging
import os
import sqlite3
import subprocess

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import vpn_manager as vm
import config.settings as settings
import shared_state
from dashboard.api import register_dashboard
from test_vpn_freshness import FakeVPNManager, _cfg


class _EnvRecFake(FakeVPNManager):
    """FakeVPNManager that records the env of EVERY `_docker_run` call and
    scripts the update-check repo digest through (the pull path needs a
    digest BEFORE and AFTER the pull to reach the compose call).

    `_compose_up` is delegated to the REAL one: the env= kwarg only exists
    at the `_docker_run` boundary (the fake short-circuit never sees it),
    and the real method's only side effect besides `_docker_run` is the
    returncode check — the fake runner always succeeds."""

    def __init__(self, cfg, station=1, shared=None, tmp_path=None, **kw):
        super().__init__(cfg, station=station, shared=shared,
                         tmp_path=tmp_path, **kw)
        self.digest = "sha256:same"        # same before/after → no update
        self.image_id = "sha256:old"
        self.envs = []                     # env of every _docker_run call

    async def _docker_repo_digest(self, image):
        return self.digest

    async def _docker_image_id(self, image):
        return self.image_id

    async def _compose_up(self, force_recreate=False):
        await vm.VPNManager._compose_up(self, force_recreate=force_recreate)

    def _docker_run(self, args, timeout=30, env=None):
        self.envs.append(dict(env) if env else None)
        return super()._docker_run(args, timeout=timeout, env=env)


def _cfg_with(tmp_path, **over):
    """Engineered auto-stack config: key file present → effective resolves
    to wireguard at __init__ even though the bare _cfg() default stack is
    "auto" (needs the key pre-seeded BEFORE construction)."""
    (tmp_path / "wireguard.env").write_text("PRIVATE_KEY=x\n", encoding="utf-8")
    return _cfg(tmp_path, vpn_stack=over.pop("vpn_stack", "auto")), over


def _compose(tmp_path, monkeypatch):
    compose = tmp_path / "docker-compose.yml"
    monkeypatch.setenv("VPN_DOCKER_COMPOSE_FILE", str(compose))
    return compose


# ── _compose_env (déterministe — construction du dict env) ─────────

class TestComposeEnv:
    def test_single_station_carries_station_var(self, tmp_path, monkeypatch):
        # Importing config.settings already loaded the repo .env into
        # os.environ once — purge VPN_* so "not mutated" is testable.
        for k in list(os.environ):
            if k.startswith("VPN_"):
                monkeypatch.delenv(k, raising=False)
        cfg, _ = _cfg_with(tmp_path)
        m = _EnvRecFake(cfg, station=3, tmp_path=tmp_path)
        # key present → auto stack resolves to wireguard
        assert m._stack_effective == "wireguard"
        monkeypatch.setenv("SOME_PARENT_MARKER", "yes")

        env = m._compose_env()

        assert env["VPN_TYPE_STATION3"] == "wireguard", \
            "own station's var = effective stack"
        assert env["SOME_PARENT_MARKER"] == "yes", \
            "parent env inherited (compose needs ALL the other vars)"
        assert os.environ.get("VPN_TYPE_STATION3") is None, (
            "parent env must NOT be mutated (the divergence detector "
            "relies on it staying stale)")

    def test_multiple_stations_all_present(self, tmp_path):
        cfg, _ = _cfg_with(tmp_path)
        m = _EnvRecFake(cfg, station=1, tmp_path=tmp_path)
        env = m._compose_env(stations=[1, 2, 3])
        assert {env[f"VPN_TYPE_STATION{s}"] for s in (1, 2, 3)} == {"wireguard"}

    def test_explicit_stack_override_wins(self, tmp_path):
        """During a flip, _apply_stack passes the TARGET stack — the child
        must recreate in the requested mode even if the parent env / the
        effective stack still carries the old one (the 19/08 case)."""
        cfg, _ = _cfg_with(tmp_path)
        m = _EnvRecFake(cfg, station=1, tmp_path=tmp_path)
        m._stack_effective = "wireguard"
        env = m._compose_env(stack="openvpn", stations=[1, 2])
        assert env["VPN_TYPE_STATION1"] == "openvpn"
        assert env["VPN_TYPE_STATION2"] == "openvpn"

    def test_missing_effective_falls_back_openvpn(self, tmp_path):
        cfg, _ = _cfg_with(tmp_path)
        m = _EnvRecFake(cfg, station=7, tmp_path=tmp_path)
        m._stack_effective = None
        assert m._compose_env()["VPN_TYPE_STATION7"] == "openvpn"


# ── custom .ovpn — gate OPENVPN_CUSTOM_CONFIG (Axe 3.1) ────────────

class TestCustomOvpnEnv:
    """[fix 20/08][Axe 3.1] The dashboard upload persists `custom_ovpn_file`
    (compose-root-relative, `vpn_configs/custom/…`). `_compose_env` must
    point OPENVPN_CUSTOM_CONFIG at its in-container mirror
    `/vpn-custom/<basename>` — openvpn stack + file present ONLY: a stale
    path must never leak into a WireGuard stanza, and an absent file is a
    silent no-op (compose's ${VAR:-} interpolation stays inert), not an
    error."""

    def _ovpn_mgr(self, tmp_path, monkeypatch, *, stack="openvpn", rel=None,
                  create_file=True):
        cfg, _ = _cfg_with(tmp_path, vpn_stack=stack)
        if rel is not None:
            cfg["custom_ovpn_file"] = rel
        m = _EnvRecFake(cfg, station=1, tmp_path=tmp_path)
        _compose(tmp_path, monkeypatch)        # VPN_DOCKER_COMPOSE_FILE
        if create_file and rel is not None:
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("client\nremote 1.2.3.4 1194\n", encoding="utf-8")
        return m

    def test_openvpn_file_present_sets_var(self, tmp_path, monkeypatch):
        m = self._ovpn_mgr(tmp_path, monkeypatch,
                           rel="vpn_configs/custom/nl-01.ovpn")
        env = m._compose_env()
        assert env["OPENVPN_CUSTOM_CONFIG"] == "/vpn-custom/nl-01.ovpn", \
            "in-container path = bind-mount mirror of the uploaded file"

    def test_openvpn_without_custom_key_no_var(self, tmp_path, monkeypatch):
        m = self._ovpn_mgr(tmp_path, monkeypatch, rel=None)
        env = m._compose_env()
        assert "OPENVPN_CUSTOM_CONFIG" not in env, \
            "no custom configured → compose's ${VAR:-} interpolation is inert"

    def test_openvpn_missing_file_no_var(self, tmp_path, monkeypatch):
        """Upload persisted but the file vanished (manual cleanup / partial
        deploy) — the gate must NOT fabricate a path the container can't
        see (gluetun would fail on a missing /vpn-custom/… file)."""
        m = self._ovpn_mgr(tmp_path, monkeypatch,
                           rel="vpn_configs/custom/gone.ovpn",
                           create_file=False)
        env = m._compose_env()
        assert "OPENVPN_CUSTOM_CONFIG" not in env

    def test_wireguard_never_carries_custom(self, tmp_path, monkeypatch):
        """Stale custom_ovpn_file + wireguard stack → no var (gluetun would
        ignore the setting under WG anyway; the guard keeps the surface
        honest — the 19/08 lesson: the env must never leak across stacks)."""
        m = self._ovpn_mgr(tmp_path, monkeypatch, stack="wireguard",
                           rel="vpn_configs/custom/nl-01.ovpn")
        assert m._stack_effective == "wireguard"
        env = m._compose_env()
        assert "OPENVPN_CUSTOM_CONFIG" not in env

    def test_os_separators_normalized(self, tmp_path, monkeypatch):
        """The persisted rel may carry OS separators (dashboard uploads use
        forward slashes, but hand-edited config.yaml may not) — the gate
        normalizes before the existence check, so /vpn-custom/<basename>
        stays stable on every host."""
        m = self._ovpn_mgr(tmp_path, monkeypatch,
                           rel=os.path.join("vpn_configs", "custom",
                                            "nl-01.ovpn"))
        env = m._compose_env()
        assert env["OPENVPN_CUSTOM_CONFIG"] == "/vpn-custom/nl-01.ovpn"


# ── call sites — env passe à travers le runner factice ─────────────

class TestCallSitesPassEnv:
    def _mgr(self, tmp_path, monkeypatch, station=1, **over):
        cfg, _ = _cfg_with(tmp_path)
        m = _EnvRecFake(cfg, station=station, tmp_path=tmp_path, **over)
        _compose(tmp_path, monkeypatch)
        return m

    @pytest.mark.asyncio
    async def test_compose_up_passes_env(self, tmp_path, monkeypatch):
        m = self._mgr(tmp_path, monkeypatch)
        await m._compose_up()
        assert len(m.envs) == 1
        assert m.envs[0]["VPN_TYPE_STATION1"] == "wireguard"

    @pytest.mark.asyncio
    async def test_apply_stack_env_is_target_stack(self, tmp_path, monkeypatch):
        """The flip's compose child must see the TARGET stack: with a stale
        parent env (openvpn) and effective openvpn, a plain effective-stack
        env would recreate the fleet on OpenVPN — the bug being killed."""
        cfg, _ = _cfg_with(tmp_path)
        managers = [_EnvRecFake(cfg, station=s, tmp_path=tmp_path)
                    for s in (1, 2, 3)]
        for m in managers:
            m._stack_effective = "openvpn"     # stale parent / effective
        monkeypatch.setattr(shared_state, "vpn_managers", managers,
                            raising=False)
        _compose(tmp_path, monkeypatch)

        assert await managers[0]._apply_stack("wireguard") is True
        env = managers[0].last_env
        for s in (1, 2, 3):
            assert env[f"VPN_TYPE_STATION{s}"] == "wireguard", \
                "TARGET stack reaches the compose child despite the stale " \
                "parent env (19/08 root cause)"
        assert managers[0]._stack_effective == "wireguard"

    @pytest.mark.asyncio
    async def test_stop_container_env_is_effective_stack(self, tmp_path,
                                                         monkeypatch):
        m = self._mgr(tmp_path, monkeypatch)
        await m.stop_container()
        assert m.envs[0]["VPN_TYPE_STATION1"] == "wireguard", (
            "compose stop child (the FIRST docker op) sees the stack; the "
            "`docker rm` that follows carries no env")
        assert m.envs[-1] is None, "docker rm -f needs no compose env"

    @pytest.mark.asyncio
    async def test_check_update_pull_passes_env(self, tmp_path, monkeypatch):
        m = self._mgr(tmp_path, monkeypatch)
        m._update_enabled = True
        assert await m.check_update() is False     # same digest → no update
        assert m.last_env["VPN_TYPE_STATION1"] == "wireguard", \
            "`compose pull` child inherits the explicit stack too"

    @pytest.mark.asyncio
    async def test_apply_update_compose_passes_env(self, tmp_path,
                                                   monkeypatch):
        m = self._mgr(tmp_path, monkeypatch)
        m._update_available = True
        m._update_old_image_id = "sha256:old"
        m._UPDATE_LOCK_PATH = str(tmp_path / "vpn_update.lock")
        m.ips = ["9.9.9.9"]
        m._finalize_ip = lambda: None            # never reached — see below

        async def _finalize(allow_stale=False):
            m._current_ip = "9.9.9.9"
            return True
        m._finalize_ip = _finalize
        result = await m.apply_update()

        assert result["ok"] is True
        assert m.envs[-1]["VPN_TYPE_STATION1"] == "wireguard", \
            "recreate child after update sees the effective stack"

    @pytest.mark.asyncio
    async def test_rollback_compose_passes_env(self, tmp_path, monkeypatch):
        """A failed apply rolls back through a final compose up — that child
        must also get the explicit stack (same 19/08 rationale)."""
        m = self._mgr(tmp_path, monkeypatch)
        m._update_available = True
        m._update_old_image_id = "sha256:old"
        m._UPDATE_LOCK_PATH = str(tmp_path / "vpn_update.lock")
        m.log_text = "AUTH_FAILED"               # post-recreate auth failure

        async def _finalize(allow_stale=False):
            m._current_ip = "9.9.9.9"
            return True
        m._finalize_ip = _finalize
        result = await m.apply_update()

        assert result["ok"] is False
        assert "AUTH_FAILED" in result["error"]
        # last docker op is the rollback compose (tag happened before it)
        assert m.envs[-1]["VPN_TYPE_STATION1"] == "wireguard"


# ── detection — load_env_file vs process env (boot) ────────────────

@pytest.fixture
def _env_file(tmp_path, monkeypatch):
    """Point settings at a tmp .env, reset the flag, and purge every VPN_*
    key from os.environ — importing config.settings already ran the real
    load_env_file() once (settings.py:161) against the repo .env, so the
    divergence check must start from a clean slate, not the import-time
    values (monkeypatch restores them at teardown)."""
    monkeypatch.setattr(settings, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(settings, "ENV_DIVERGENCE", [])
    for k in list(os.environ):
        if k.startswith("VPN_"):
            monkeypatch.delenv(k, raising=False)
    return tmp_path / ".env"


def _write_env(path, **kv):
    with open(path, "w", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


class TestLoadEnvFile:
    @pytest.mark.asyncio
    async def test_divergent_vpn_key_flagged_and_warned(self, _env_file,
                                                        monkeypatch,
                                                        caplog):
        env = _env_file
        monkeypatch.setenv("VPN_TYPE_STATION1", "openvpn")  # stale parent
        _write_env(env, VPN_TYPE="openvpn",
                   VPN_TYPE_STATION1="wireguard",           # file says WG
                   SOME_SECRET="abc123")
        with caplog.at_level(logging.WARNING,
                             logger="config.settings"):
            settings.load_env_file()
        assert settings.ENV_DIVERGENCE == [
            ("VPN_TYPE_STATION1", "wireguard", "openvpn")]
        assert "env divergence" in caplog.text
        assert "VPN_TYPE_STATION1" in caplog.text
        # the 19/08 read-back: file and process disagree on what the fleet
        # runs — the process env (openvpn) is what compose would inherit.
        assert os.environ["VPN_TYPE_STATION1"] == "openvpn", \
            "stale env NOT overwritten (a later compose would inherit it)"

    @pytest.mark.asyncio
    async def test_aligned_key_not_flagged(self, _env_file, monkeypatch):
        env = _env_file
        monkeypatch.setenv("VPN_TYPE_STATION1", "wireguard")
        _write_env(env, VPN_TYPE_STATION1="wireguard")     # file matches
        settings.load_env_file()
        assert settings.ENV_DIVERGENCE == []

    @pytest.mark.asyncio
    async def test_non_vpn_divergent_key_ignored(self, _env_file, monkeypatch):
        """Only VPN_* keys feed the compose interpolation — a divergent
        OPENCODE_API_KEY (etc.) is expected (the process env legitimately
        supersedes deployment-specific secrets). Never flagged."""
        env = _env_file
        monkeypatch.setenv("OPENCODE_API_KEY", "from-process")
        monkeypatch.setenv("VPN_TYPE_STATION1", "openvpn")
        _write_env(env, VPN_TYPE_STATION1="openvpn",
                   OPENCODE_API_KEY="from-file")
        settings.load_env_file()
        assert settings.ENV_DIVERGENCE == []

    @pytest.mark.asyncio
    async def test_absent_key_loaded_not_flagged(self, _env_file, monkeypatch):
        env = _env_file
        _write_env(env, VPN_TYPE_STATION1="wireguard",
                   VPN_TYPE_STATION2="openvpn")
        settings.load_env_file()
        assert settings.ENV_DIVERGENCE == []
        assert os.environ["VPN_TYPE_STATION1"] == "wireguard"
        assert os.environ["VPN_TYPE_STATION2"] == "openvpn"

    @pytest.mark.asyncio
    async def test_no_env_file_is_noop(self, _env_file, monkeypatch):
        # ENV_PATH → missing file
        settings.load_env_file()
        assert settings.ENV_DIVERGENCE == []

    @pytest.mark.asyncio
    async def test_partially_aligned_and_divergent_mixed(self, _env_file,
                                                         monkeypatch,
                                                         caplog):
        """Only the divergent VPN_* keys are journaled; aligned stations and
        non-VPN_* keys stay quiet — the banner list is precise, not noisy."""
        env = _env_file
        monkeypatch.setenv("VPN_TYPE_STATION1", "openvpn")   # stale
        monkeypatch.setenv("VPN_TYPE_STATION2", "openvpn")   # aligned
        monkeypatch.setenv("VPN_TYPE_STATION3", "edge")      # stale
        monkeypatch.setenv("OPENCODE_API_KEY", "proc-key")
        _write_env(env, VPN_TYPE_STATION1="wireguard",
                   VPN_TYPE_STATION2="openvpn",
                   VPN_TYPE_STATION3="openvpn",
                   OPENCODE_API_KEY="file-key")
        with caplog.at_level(logging.WARNING, logger="config.settings"):
            settings.load_env_file()
        assert settings.ENV_DIVERGENCE == [
            ("VPN_TYPE_STATION1", "wireguard", "openvpn"),
            ("VPN_TYPE_STATION3", "openvpn", "edge"),
        ]
        assert caplog.text.count("env divergence") == 2

    @pytest.mark.asyncio
    async def test_runtime_rewrite_after_apply_stack_not_flagged(
            self, _env_file, monkeypatch):
        """A boot-time load writes the file values into os.environ (absent
        keys); a focused re-test call after _apply_stack has synced BOTH the
        file AND os.environ to the new stack sees aligned values and flags
        nothing (the no-op re-sync path — plan 18/08 §4 [fix] re-writes the
        env every _apply_stack call, even on a stack no-op)."""
        env = _env_file
        _write_env(env, VPN_TYPE_STATION1="wireguard")
        settings.load_env_file()                       # boot load
        # _apply_stack's read-modify-write has now synced the process env:
        assert os.environ["VPN_TYPE_STATION1"] == "wireguard"
        # …so the re-test call must NOT report a divergence.
        _write_env(env, VPN_TYPE_STATION1="wireguard")
        settings.load_env_file()
        assert settings.ENV_DIVERGENCE == []


# ── dashboard wiring — /api/vpn-status exposes the flag ────────────

class TestDashboardExposesDivergence:
    """/api/vpn-status carries env_divergence when settings.ENV_DIVERGENCE is
    populated — the banner data. Exercised through the real FastAPI route
    (same fixture pattern as test_vpn_config_http.py; no managers → the
    not_configured branch, _ip_stats_db over an empty :memory: db)."""

    def _client(self, tmp_path, monkeypatch):
        fast = FastAPI()
        (tmp_path / "index.html").write_text("<html></html>")
        register_dashboard(fast, str(tmp_path), sqlite3.connect(":memory:"))
        monkeypatch.setattr(shared_state, "vpn_managers", [], raising=False)
        return TestClient(fast)

    def test_env_divergence_field_present_when_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "ENV_DIVERGENCE",
                            [("VPN_TYPE_STATION1", "wireguard", "openvpn")])
        with self._client(tmp_path, monkeypatch) as client:
            data = client.get("/api/vpn-status").json()
        assert data["env_divergence"] == [
            {"key": "VPN_TYPE_STATION1", "file": "wireguard", "env": "openvpn"}]

    def test_env_divergence_absent_when_flag_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "ENV_DIVERGENCE", [])
        with self._client(tmp_path, monkeypatch) as client:
            data = client.get("/api/vpn-status").json()
        assert "env_divergence" not in data, \
            "no banner field when the process env matches the .env"