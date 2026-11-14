"""ComposeHostnameBinder tests (no Docker)."""

from __future__ import annotations

import subprocess

from free.ip_registry.adapters import compose_binder as compose_module
from free.ip_registry.adapters.compose_binder import ComposeHostnameBinder


class FakeRunner:
    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, command, cwd):
        self.calls.append((list(command), cwd))
        return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)


def test_run_drops_stale_station_env(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs.get("env") or {})
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("VPN_TYPE_STATION5", "openvpn")
    monkeypatch.setenv("SERVER_HOSTNAMES_STATION5", "old.nordvpn.com")
    monkeypatch.setenv("OPENVPN_PROTOCOL_STATION5", "tcp")
    monkeypatch.setenv("KEEP_ME", "yes")
    monkeypatch.setattr(compose_module.subprocess, "run", fake_run)

    ComposeHostnameBinder(tmp_path)._run(["docker"], str(tmp_path))

    assert "VPN_TYPE_STATION5" not in captured
    assert "SERVER_HOSTNAMES_STATION5" not in captured
    assert "OPENVPN_PROTOCOL_STATION5" not in captured
    assert captured["KEEP_ME"] == "yes"


async def test_assign_writes_override_and_recreates(tmp_path):
    runner = FakeRunner()

    async def egress(station: int) -> str:
        return "186.247.44.61"

    binder = ComposeHostnameBinder(
        tmp_path,
        settle_s=0,
        runner=runner,
        egress=egress,
    )

    result = await binder.assign(2, "al110.nordvpn.com")

    assert result.ok
    assert result.egress_ip == "186.247.44.61"
    assert result.hostname == "al110.nordvpn.com"
    override = binder.override_path_for(2).read_text(encoding="utf-8")
    assert "vpn-gluetun-2" in override
    assert "SERVER_HOSTNAMES=al110.nordvpn.com" in override
    command, cwd = runner.calls[0]
    assert "vpn-gluetun-2" in command
    assert "--force-recreate" in command
    assert cwd == str(tmp_path)


async def test_assign_writes_a_country_override(tmp_path):
    runner = FakeRunner()

    async def egress(station: int) -> str:
        return "146.70.238.51"

    binder = ComposeHostnameBinder(
        tmp_path,
        settle_s=0,
        runner=runner,
        egress=egress,
    )

    result = await binder.assign(5, "ae73.nordvpn.com", country="United Arab Emirates")

    assert result.ok
    override = binder.override_path_for(5).read_text(encoding="utf-8")
    assert "SERVER_HOSTNAMES=ae73.nordvpn.com" in override
    assert "SERVER_COUNTRIES=United Arab Emirates" in override


async def test_assign_writes_an_openvpn_override(tmp_path):
    runner = FakeRunner()

    async def egress(station: int) -> str:
        return "187.15.106.56"

    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=runner, egress=egress)

    result = await binder.assign(
        5,
        "bg95.nordvpn.com",
        country="Bulgaria",
        vpn_type="openvpn",
        openvpn_protocol="tcp",
    )

    assert result.ok
    override = binder.override_path_for(5).read_text(encoding="utf-8")
    assert "VPN_TYPE=openvpn" in override
    assert "OPENVPN_PROTOCOL=tcp" in override


def test_wireguard_override_sets_vpn_type_explicitly(tmp_path):
    binder = ComposeHostnameBinder(tmp_path)
    override = binder.override_yaml(
        5,
        "al37.nordvpn.com",
        country="Albania",
        vpn_type="wireguard",
    )
    assert "VPN_TYPE=wireguard" in override
    assert "OPENVPN_PROTOCOL" not in override


async def test_assign_reports_recreate_failure(tmp_path):
    runner = FakeRunner(returncode=1, stderr="boom")
    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=runner)

    result = await binder.assign(1, "al110.nordvpn.com")

    assert not result.ok
    assert result.error is not None and "recreate" in result.error


async def test_assign_rejects_empty_hostname(tmp_path):
    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=FakeRunner())
    result = await binder.assign(1, "")
    assert not result.ok
    assert result.error == "empty-hostname"


async def test_assign_reports_no_egress(tmp_path):
    async def egress(station: int) -> str:
        return ""

    binder = ComposeHostnameBinder(
        tmp_path, settle_s=0, runner=FakeRunner(), egress=egress
    )
    result = await binder.assign(1, "al110.nordvpn.com")
    assert not result.ok
    assert result.error == "no-egress"


async def test_assign_retries_egress_until_the_tunnel_answers(tmp_path):
    calls = []

    async def egress(station: int) -> str:
        calls.append(station)
        return "" if len(calls) < 3 else "186.247.44.77"

    binder = ComposeHostnameBinder(
        tmp_path,
        settle_s=0,
        egress_wait_s=0.25,
        egress_poll_s=0.1,
        runner=FakeRunner(),
        egress=egress,
    )
    result = await binder.assign(1, "al110.nordvpn.com")
    assert result.ok
    assert result.egress_ip == "186.247.44.77"
    assert calls == [1, 1, 1]


async def test_assign_gives_up_after_the_egress_deadline(tmp_path):
    calls = []

    async def egress(station: int) -> str:
        calls.append(station)
        return ""

    binder = ComposeHostnameBinder(
        tmp_path,
        settle_s=0,
        egress_wait_s=0.25,
        egress_poll_s=0.1,
        runner=FakeRunner(),
        egress=egress,
    )
    result = await binder.assign(1, "al110.nordvpn.com")
    assert not result.ok
    assert result.error == "no-egress"
    assert len(calls) >= 2 and set(calls) == {1}


def test_auto_override_leaves_vpn_type_for_the_fork_ladder(tmp_path):
    binder = ComposeHostnameBinder(tmp_path)
    override = binder.override_yaml(
        6, "us13183.nordvpn.com", vpn_type="auto"
    )
    assert "VPN_TYPE=" in override
    assert "VPN_TYPE=openvpn" not in override
    assert "VPN_TYPE=wireguard" not in override
    assert "OPENVPN_PROTOCOL" not in override


def test_credentials_stale_detects_a_rotation(tmp_path):
    (tmp_path / "credentials.env").write_text(
        "OPENVPN_USER=new-user\nOPENVPN_PASSWORD=new-pass\n", encoding="utf-8"
    )
    wireguard = tmp_path / "vpn_configs"
    wireguard.mkdir()
    (wireguard / "wireguard.env").write_text(
        "WIREGUARD_PRIVATE_KEY=new-key\n", encoding="utf-8"
    )

    class Runner:
        def __call__(self, command, timeout):
            return subprocess.CompletedProcess(
                command,
                0,
                "OPENVPN_USER=old-user\nOPENVPN_PASSWORD=old-pass\n"
                "WIREGUARD_PRIVATE_KEY=old-key\n",
                "",
            )

    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=Runner())
    assert binder.credentials_stale(2) is True


def test_credentials_fresh_does_not_force_a_recreate(tmp_path):
    (tmp_path / "credentials.env").write_text(
        "OPENVPN_USER=same-user\nOPENVPN_PASSWORD=same-pass\n", encoding="utf-8"
    )
    wireguard = tmp_path / "vpn_configs"
    wireguard.mkdir()
    (wireguard / "wireguard.env").write_text(
        "WIREGUARD_PRIVATE_KEY=same-key\n", encoding="utf-8"
    )

    class Runner:
        def __call__(self, command, timeout):
            return subprocess.CompletedProcess(
                command,
                0,
                "OPENVPN_USER=same-user\nOPENVPN_PASSWORD=same-pass\n"
                "WIREGUARD_PRIVATE_KEY=same-key\n",
                "",
            )

    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=Runner())
    assert binder.credentials_stale(2) is False


def test_image_stale_detects_an_old_container(tmp_path):
    class Runner:
        def __call__(self, command, timeout):
            if command[:2] == ["docker", "image"]:
                output = "sha256:new"
            elif "{{.Image}}" in command:
                output = "sha256:old"
            elif "{{.Config.Image}}" in command:
                output = "gluetun-016:local"
            else:
                output = ""
            return subprocess.CompletedProcess(command, 0, output, "")

    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=Runner())
    assert binder.image_stale(2) is True


def test_image_stale_detects_a_legacy_container(tmp_path):
    class Runner:
        def __call__(self, command, timeout):
            if "{{.Image}}" in command:
                output = "sha256:legacy"
            elif "{{.Config.Image}}" in command:
                output = "qmcgaw/gluetun"
            else:
                output = ""
            return subprocess.CompletedProcess(command, 0, output, "")

    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=Runner())
    assert binder.image_stale(2) is True


def test_dns_ladder_stale_detects_a_changed_override(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "compose-control-plane-2.yml").write_text(
        "services:\n  vpn-gluetun-2:\n    environment:\n"
        "      DNS_LADDER: plain,dot\n",
        encoding="utf-8",
    )

    class Runner:
        def __call__(self, command, cwd):
            return subprocess.CompletedProcess(
                command, 0, "DNS_LADDER=dot,plain\n", ""
            )

    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=Runner())
    assert binder.dns_ladder_stale(2) is True


def test_dns_ladder_fresh_does_not_force_a_recreate(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "compose-control-plane-2.yml").write_text(
        "services:\n  vpn-gluetun-2:\n    environment:\n"
        "      DNS_LADDER: plain,dot\n",
        encoding="utf-8",
    )

    class Runner:
        def __call__(self, command, cwd):
            return subprocess.CompletedProcess(
                command, 0, "DNS_LADDER=plain,dot\n", ""
            )

    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=Runner())
    assert binder.dns_ladder_stale(2) is False


def test_transport_timeouts_stale_detects_a_changed_budget(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "compose-control-plane-2.yml").write_text(
        "services:\n  vpn-gluetun-2:\n    environment:\n"
        '      TRANSPORT_ATTEMPT_TIMEOUT_S: "100"\n'
        '      TRANSPORT_ATTEMPT_TIMEOUT_TCP_S: "100"\n'
        '      TRANSPORT_MAX_CYCLES: "1"\n',
        encoding="utf-8",
    )

    class Runner:
        def __call__(self, command, cwd):
            return subprocess.CompletedProcess(
                command,
                0,
                "TRANSPORT_ATTEMPT_TIMEOUT_S=20\n"
                "TRANSPORT_ATTEMPT_TIMEOUT_TCP_S=10\n"
                "TRANSPORT_MAX_CYCLES=2\n",
                "",
            )

    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=Runner())
    assert binder.transport_timeouts_stale(2) is True


def test_transport_timeouts_fresh_does_not_force_a_recreate(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "compose-control-plane-2.yml").write_text(
        "services:\n  vpn-gluetun-2:\n    environment:\n"
        '      TRANSPORT_ATTEMPT_TIMEOUT_S: "100"\n'
        '      TRANSPORT_ATTEMPT_TIMEOUT_TCP_S: "100"\n'
        '      TRANSPORT_MAX_CYCLES: "1"\n',
        encoding="utf-8",
    )

    class Runner:
        def __call__(self, command, cwd):
            return subprocess.CompletedProcess(
                command,
                0,
                "TRANSPORT_ATTEMPT_TIMEOUT_S=100\n"
                "TRANSPORT_ATTEMPT_TIMEOUT_TCP_S=100\n"
                "TRANSPORT_MAX_CYCLES=1\n",
                "",
            )

    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=Runner())
    assert binder.transport_timeouts_stale(2) is False


def test_egress_sources_stale_detects_a_changed_source_list(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "compose-control-plane-2.yml").write_text(
        "services:\n  vpn-gluetun-2:\n    environment:\n"
        "      CONTROL_EGRESS_SOURCES: http:cloudflare,http:ipify\n",
        encoding="utf-8",
    )

    class Runner:
        def __call__(self, command, cwd):
            return subprocess.CompletedProcess(
                command, 0, "CONTROL_EGRESS_SOURCES=http:ipify\n", ""
            )

    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=Runner())
    assert binder.egress_sources_stale(2) is True


def test_egress_sources_fresh_does_not_force_a_recreate(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "compose-control-plane-2.yml").write_text(
        "services:\n  vpn-gluetun-2:\n    environment:\n"
        "      CONTROL_EGRESS_SOURCES: http:cloudflare,http:ipify\n",
        encoding="utf-8",
    )

    class Runner:
        def __call__(self, command, cwd):
            return subprocess.CompletedProcess(
                command,
                0,
                "CONTROL_EGRESS_SOURCES=http:cloudflare,http:ipify\n",
                "",
            )

    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=Runner())
    assert binder.egress_sources_stale(2) is False


def test_inspect_uses_the_project_cwd(tmp_path):
    seen = []

    class Runner:
        def __call__(self, command, cwd):
            seen.append(cwd)
            return subprocess.CompletedProcess(command, 0, "sha256:x", "")

    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=Runner())
    binder._inspect_value("opencode-vpn-2", "{{.Image}}")
    binder._container_env(2)

    assert seen == [str(tmp_path), str(tmp_path)]


def test_override_path_is_per_station(tmp_path):
    binder = ComposeHostnameBinder(tmp_path, settle_s=0, runner=FakeRunner())
    p1 = binder.override_path_for(1)
    p2 = binder.override_path_for(2)
    assert p1 != p2
    assert p1.name.endswith("-1.yml")
    assert p2.name.endswith("-2.yml")


async def test_assign_layers_existing_extra_compose_files(tmp_path):
    runner = FakeRunner()
    extra = tmp_path / "logs" / "compose-control-plane-2.yml"
    extra.parent.mkdir(parents=True)
    extra.write_text("services: {}\n", encoding="utf-8")
    missing = tmp_path / "logs" / "compose-control-plane-3.yml"
    binder = ComposeHostnameBinder(
        tmp_path,
        settle_s=0,
        runner=runner,
        extra_compose_files=(str(extra), str(missing)),
    )

    result = await binder.assign(2, "al110.nordvpn.com")

    assert result.ok
    command, _ = runner.calls[0]
    assert command.count("-f") == 3
    assert str(extra) in command
    assert str(missing) not in command


def test_override_path_already_station_suffixed_is_kept(tmp_path):
    binder = ComposeHostnameBinder(
        tmp_path,
        override_path="logs/compose-hostname-3.yml",
        settle_s=0,
        runner=FakeRunner(),
    )
    assert binder.override_path_for(3) == tmp_path / "logs" / "compose-hostname-3.yml"


async def test_assign_pins_the_compose_project(tmp_path):
    runner = FakeRunner()

    async def egress(station: int) -> str:
        return "186.247.44.61"

    binder = ComposeHostnameBinder(
        tmp_path,
        settle_s=0,
        project_name="opencode-proxy-one",
        runner=runner,
        egress=egress,
    )
    await binder.assign(2, "al110.nordvpn.com")

    command, _ = runner.calls[0]
    assert command[:4] == ["docker", "compose", "-p", "opencode-proxy-one"]


def test_project_name_falls_back_to_dotenv(monkeypatch, tmp_path):
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    (tmp_path / ".env").write_text(
        "KEEP=1\nCOMPOSE_PROJECT_NAME=opencode-proxy-one\n", encoding="utf-8"
    )
    binder = ComposeHostnameBinder(tmp_path)
    assert binder._project_name == "opencode-proxy-one"


def test_run_exports_the_resolved_project_name(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs.get("env") or {})
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(compose_module.subprocess, "run", fake_run)
    binder = ComposeHostnameBinder(tmp_path, project_name="opencode-proxy-one")
    binder._run(["docker"], str(tmp_path))
    assert captured["COMPOSE_PROJECT_NAME"] == "opencode-proxy-one"
