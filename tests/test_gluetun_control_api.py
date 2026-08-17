"""test_gluetun_control_api.py — gluetun control server client (vpn_manager.py).

The control client talks to gluetun's REST control server through
`docker exec <ctn> sh -c 'wget ... --header="X-API-Key: $VPN_CONTROL_API_KEY" ...'`
— the KEY is injected by the CONTAINER's env, never written into process
args (never visible in `docker ps`/`ps aux`, never logged). This suite pins
the exact endpoints/payloads the plan requires ([plan] A, verified against
gluetun v3.41.3 and the mandatory auth since v3.39.1):

  * GET  /v1/vpn/status    -> {"status":"running"|"stopped"}
  * GET  /v1/publicip/ip   -> {"public_ip":"..."}
  * PUT  /v1/vpn/settings  -> partial merge payload {"provider":{"server_selection":
                              {"countries":["France"]}}}; 204 No Content = [] = success

Offline: `_docker_run` is faked to capture argv and return scripted stdout —
the REAL script-building / parsing / polling logic runs unchanged.

Never touches the live system (no docker, no network).
"""
import json
import subprocess

import pytest

import vpn_manager as vm
from test_vpn_freshness import FakeVPNManager, _cfg, _shared


class ControlFakeVPNManager(FakeVPNManager):
    """FakeVPNManager whose `_docker_run` records the argv and answers with
    a scripted stdout (keyed by substring of the sh script), so the control
    client's parsing/polling logic can run against canned responses."""

    def __init__(self, cfg, station=1, shared=None, tmp_path=None, **kw):
        super().__init__(cfg, station=station, shared=shared, tmp_path=tmp_path, **kw)
        self.calls["docker_run"] = self.calls.get("docker_run", 0)
        self.control_scripts = []          # every sh script passed to docker exec
        self.control_rc = 0                # return code for ALL control calls
        self.control_stdout = ""           # default stdout (fragments below override)
        self.stdout_by_fragment = {}       # fragment -> stdout override

    def _docker_run(self, args, timeout=30):
        self.calls["docker_run"] += 1
        # Only log the args for control-server invocations (sh -c ... wget ...)
        # The script is the LAST element of ["exec", ctn, "sh", "-c", script].
        if args and args[:2] == ["exec", self._docker_container] \
                and "sh" in args and "-c" in args:
            self.control_scripts.append(args[-1])
        script = args[-1] if args else ""
        stdout = self.control_stdout
        for frag, out in self.stdout_by_fragment.items():
            if frag in script:
                stdout = out
                break
        return subprocess.CompletedProcess(args, self.control_rc, stdout=stdout, stderr="")

    def last_script(self) -> str:
        return self.control_scripts[-1] if self.control_scripts else ""

    def settings_script(self) -> str:
        """The most recent PUT /v1/vpn/settings script (the pin payload) —
        last_script() is the newest call overall, which after a successful
        pin is a status poll, not the PUT."""
        for s in reversed(self.control_scripts):
            if "/v1/vpn/settings" in s:
                return s
        return ""


async def _server_issue_true(*args, **kwargs):
    """Flag-driven TLS-failure override for the pin-abort tests (the real
    _check_server_issue scans docker logs; the fake always returns False)."""
    return True


# ── _control_exec: script shape + key secrecy ────────────────────

class TestControlExec:
    @pytest.mark.asyncio
    async def test_get_script_shape_and_key_secrecy(self, tmp_path):
        """GET /v1/vpn/status: the sh script references the key as
        $VPN_CONTROL_API_KEY (expanded inside the container) — the raw key
        value NEVER appears in any argparse element."""
        secret = "s3cr3t-k3y-!!"
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key=secret), tmp_path=tmp_path)
        lines = await mgr._control_exec("GET", "/v1/vpn/status", timeout=5)
        assert lines == []                       # empty stdout -> []
        script = mgr.last_script()
        assert "wget -q -O - -T 5" in script
        assert "--header=\"X-API-Key: $VPN_CONTROL_API_KEY\"" in script
        assert " http://127.0.0.1:8000/v1/vpn/status" in script
        # Key secrecy: the raw value is in the container env, not the argv.
        argv_flat = json.dumps(mgr.calls) + "|" + str(mgr.control_scripts)
        assert secret not in argv_flat

    @pytest.mark.asyncio
    async def test_put_payload_and_method(self, tmp_path):
        """PUT /v1/vpn/settings carries --method=PUT and the EXACT
        country payload the plan specifies, sh-quoted for --body-data."""
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k"), tmp_path=tmp_path)
        await mgr._control_exec("PUT", "/v1/vpn/settings",
                                body=json.dumps({"provider": {
                                    "server_selection": {"countries": ["France"]}}},
                                    separators=(",", ":")),
                                timeout=10)
        script = mgr.last_script()
        assert " --method=PUT" in script
        assert "--body-data='{\"provider\":{\"server_selection\":{\"countries\":[\"France\"]}}}'" \
            in script
        assert " http://127.0.0.1:8000/v1/vpn/settings" in script

    @pytest.mark.asyncio
    async def test_sh_quote_escapes_single_quotes(self, tmp_path):
        """A body containing a single quote must not break the sh quoting
        (a POI like L'... is impossible in country names, but pin the_
        escaping contract anyway)."""
        assert vm._sh_quote("a'b") == "'a'\"'\"'b'"

    @pytest.mark.asyncio
    async def test_nonzero_rc_returns_empty(self, tmp_path):
        """A failing wget (rc != 0) degrades to [] — the callers treat [] as
        'unavailable' and fall back to the legacy path."""
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k"), tmp_path=tmp_path)
        mgr.control_rc = 1
        assert await mgr._control_exec("GET", "/v1/vpn/status", timeout=5) == []

    @pytest.mark.asyncio
    async def test_missing_docker_cli_returns_empty(self, tmp_path):
        """docker CLI absent (RuntimeError from _docker_run) -> [] (fail-open)."""
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k"), tmp_path=tmp_path)
        mgr._docker_run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("docker CLI not found"))
        assert await mgr._control_exec("GET", "/v1/vpn/status", timeout=5) == []

    @pytest.mark.asyncio
    async def test_no_key_short_circuits_docker(self, tmp_path):
        """Without a configured key the control client never touches docker."""
        mgr = ControlFakeVPNManager(_cfg(tmp_path), tmp_path=tmp_path)  # no key
        assert await mgr._control_status() is None
        assert await mgr._control_public_ip() is None
        assert mgr.calls["docker_run"] == 0


# ── _control_status / _control_public_ip parsing ─────────────────

class TestControlReading:
    @pytest.mark.asyncio
    async def test_status_running(self, tmp_path):
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k"), tmp_path=tmp_path)
        mgr.control_stdout = '{"status":"running"}'
        assert await mgr._control_status() is True

    @pytest.mark.asyncio
    async def test_status_stopped(self, tmp_path):
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k"), tmp_path=tmp_path)
        mgr.control_stdout = '{"status":"stopped"}'
        assert await mgr._control_status() is False

    @pytest.mark.asyncio
    async def test_status_garbage_returns_none(self, tmp_path):
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k"), tmp_path=tmp_path)
        mgr.control_stdout = "not json"
        assert await mgr._control_status() is None

    @pytest.mark.asyncio
    async def test_public_ip(self, tmp_path):
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k"), tmp_path=tmp_path)
        mgr.control_stdout = '{"public_ip":"1.2.3.4"}'
        assert await mgr._control_public_ip() == "1.2.3.4"


# ── _control_pin_country: PUT + poll-until-running ───────────────

class TestControlPinCountry:
    @pytest.mark.asyncio
    async def test_pin_success_polls_to_running(self, tmp_path):
        """204 (empty stdout) → poll status until 'running' → True."""
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k",
                 wait_healthy_poll=0.01),  # fast poll for the test
            tmp_path=tmp_path)
        # PUT returns [] (204 success); status returns running.
        mgr.stdout_by_fragment = {
            "/v1/vpn/settings": "",
            "/v1/vpn/status": '{"status":"running"}',
        }
        assert await mgr._control_pin_country("France", timeout=2) is True
        # The PUT body is the exact country payload (settings_script = the
        # PUT call; last_script would be the trailing status poll).
        assert '{"provider":{"server_selection":{"countries":["France"]}}}' in mgr.settings_script()

    @pytest.mark.asyncio
    async def test_pin_normalizes_country_alias(self, tmp_path):
        """"[incident 17/08] A NordVPN file-style country name is normalized
        to gluetun's canonical name BEFORE the PUT — 'Czechia' must be sent
        as 'Czech Republic' (never a 'not in choices' WARN)."""
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k"), tmp_path=tmp_path)
        mgr.stdout_by_fragment = {
            "/v1/vpn/settings": "",
            "/v1/vpn/status": '{"status":"running"}',
        }
        assert await mgr._control_pin_country("Czechia", timeout=2) is True
        assert '{"provider":{"server_selection":{"countries":["Czech Republic"]}}}' \
            in mgr.settings_script()
        assert "Czechia" not in mgr.settings_script()

    @pytest.mark.asyncio
    async def test_pin_aborts_fast_on_auth_failed(self, tmp_path):
        """[incident 17/08] A pin whose server auth-fails is ABANDONED at
        once (AUTH_FAILED in the logs since the pin) — before any status
        poll, without waiting out the timeout — so the rotation cursor
        advances to the next country immediately. This is the fast
        self-correction: the 18:11:55Z AUTH_FAILED took ~2 min to clear
        before this fix."""
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k",
                 wait_healthy_poll=0.01), tmp_path=tmp_path)
        mgr.stdout_by_fragment = {
            "/v1/vpn/settings": "",
            # even if gluetun claims "running", the auth scan decides first
            "/v1/vpn/status": '{"status":"running"}',
        }
        mgr.log_text = "AUTH: Received control message: AUTH_FAILED"
        assert await mgr._control_pin_country("Germany", timeout=5) is False
        # Only the PUT ran — the auth scan fired before the first status poll.
        assert mgr.calls["docker_run"] == 1

    @pytest.mark.asyncio
    async def test_pin_aborts_fast_on_tls_issue(self, tmp_path):
        """A TLS negotiation failure (dead server) also aborts the pin fast.

        The real _check_auth_failed/_check_server_issue are flag-driven in
        the fake; this pins the wider failure surface: BOTH dead-server
        signatures short-circuit a pin."""
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k",
                 wait_healthy_poll=0.01), tmp_path=tmp_path)
        mgr.stdout_by_fragment = {
            "/v1/vpn/settings": "",
            "/v1/vpn/status": '{"status":"stopped"}',
        }
        mgr._check_server_issue = \
            _server_issue_true  # flag-driven TLS failure
        assert await mgr._control_pin_country("Germany", timeout=5) is False
        assert mgr.calls["docker_run"] == 1

    @pytest.mark.asyncio
    async def test_pin_rejected_nonempty_stdout(self, tmp_path):
        """A non-empty PUT response is an error body → False, no status poll."""
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k"), tmp_path=tmp_path)
        mgr.control_stdout = '{"message":"bad request"}'
        assert await mgr._control_pin_country("France", timeout=1) is False
        assert mgr.calls["docker_run"] == 1     # only the PUT, no status poll

    @pytest.mark.asyncio
    async def test_pin_rejection_flags_invalid_country_name(self, tmp_path, caplog):
        """"[incident 17/08] A 'not in choices' rejection (an unknown/typo'd
        country name) must be surfaced to the operator with an explicit
        'invalid name → check server_countries' hint, not swallowed as a
        generic error — that WARN is exactly what went unnoticed in the
        live log."""
        import logging
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k"), tmp_path=tmp_path)
        mgr.control_stdout = ('ERROR: provider.server_selection.countries: '
                              'values atlantis are not in choices Afghanistan, '
                              'Albania, ... Malta')
        with caplog.at_level(logging.WARNING, logger="vpn_manager"):
            assert await mgr._control_pin_country("Atlantis", timeout=1) is False
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "not in choices" in joined
        assert "server_countries" in joined             # the actionable hint

    @pytest.mark.asyncio
    async def test_pin_stopped_until_timeout(self, tmp_path):
        """Status stays 'stopped' → poll until the deadline → False."""
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k",
                 wait_healthy_poll=0.01), tmp_path=tmp_path)
        mgr.stdout_by_fragment = {
            "/v1/vpn/settings": "",
            "/v1/vpn/status": '{"status":"stopped"}',
        }
        assert await mgr._control_pin_country("France", timeout=0.1) is False

    @pytest.mark.asyncio
    async def test_pin_control_down_falls_back(self, tmp_path):
        """Control server unreachable (rc != 0) → pin fails → the rotation
        path falls back to the legacy container-restart branch."""
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k"), tmp_path=tmp_path)
        mgr.control_rc = 1
        mgr.ips = ["1.2.3.4"]
        mgr._status = vm.VPNState.DISCONNECTED
        old = await mgr._pin_country_for_rotation()
        assert old is None                       # no pin, no docker calls
        # Legacy fallback still rotates fine (compose up + fresh IP).
        new_ip = await mgr.connect_next()
        assert new_ip == "1.2.3.4"
        assert mgr._status == vm.VPNState.CONNECTED
        assert mgr._current_country is None      # nothing pinned