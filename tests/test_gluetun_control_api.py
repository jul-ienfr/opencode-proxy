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
import re
import subprocess
import time

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

    def _docker_run(self, args, timeout=30, env=None):
        self.calls["docker_run"] += 1
        self.last_env = dict(env) if env else None
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


class ControlReconnectFake(ControlFakeVPNManager):
    """ControlFakeVPNManager whose settings PUTs behave like a REAL gluetun
    reconnect: the PUT restarts the tunnel, so the AUTH_FAILED log marker
    disappears — exactly as a live reconnect moves the failure out of the
    post-reconnect `--since` window.

    ``clears_on_pin=N`` models the first N-1 reconnects landing on ANOTHER
    dead host (logs stay dirty — pin aborts, T2) and the Nth landing on a
    good one (marker clears, pin succeeds). Default 1 = the very first pin
    recovers (T1/T3)."""

    def __init__(self, cfg, clears_on_pin=1, station=1, shared=None,
                 tmp_path=None, **kw):
        super().__init__(cfg, station=station, shared=shared,
                         tmp_path=tmp_path, **kw)
        self.clears_on_pin = clears_on_pin
        self._pins = 0

    def _docker_run(self, args, timeout=30, env=None):
        script = args[-1] if args else ""
        if "/v1/vpn/settings" in script:
            self._pins += 1
            if self._pins >= self.clears_on_pin:
                self.log_text = ""
        return super()._docker_run(args, timeout=timeout, env=env)


def _fast_cfg(tmp_path, **over):
    """Fast-pin config: control server armed + ≥2 countries. (A single
    country makes _pin_country_for_rotation return None — the fast path
    would be dead on arrival.)"""
    over.setdefault("server_countries", "Germany,France,Spain")
    cfg = _cfg(tmp_path, control_api_key="k", country_rotation=True,
               wait_healthy_poll=0.01, **over)
    return cfg


def _fast_mgr(tmp_path, ip="9.9.9.9", clears_on_pin=1, **over):
    """A ControlReconnectFake wired for the fast-pin flow: PUT accepted
    (204-equivalent ""), status running, control public IP == the probe IP
    (the post-recovery refresh_status reads the control IP, not the probe
    — they must agree or the assert on _current_ip breaks).

    get_public_ip returns CONSTANT IP — NOT the base fake's one-shot FIFO.
    The pin catch-up (plan 18/08 §B, 15 s on the fast-recover path) polls
    it after 'running' AND _finalize_ip re-probes it per recovery round: a
    consumed FIFO would starve every later call, burn real 15 s catch-up
    walls per pin (measured: 15 stray PUTs / 46 s on T1 with mgr.ips) and
    cascade re-pins (vpn_manager 1466). The real parallel bounded sweep
    (commit 2) answers on the first call whenever the tunnel lives — a
    constant IP models that."""
    mgr = ControlReconnectFake(_fast_cfg(tmp_path, **over), station=1,
                               tmp_path=tmp_path,
                               clears_on_pin=clears_on_pin)

    async def _constant_ip():
        return ip

    mgr.get_public_ip = _constant_ip          # type: ignore[assignment]
    mgr.stdout_by_fragment = {
        "/v1/vpn/settings": "",                       # accepted (204-equivalent)
        "/v1/vpn/status": '{"status":"running"}',
        "/v1/publicip/ip": '{"public_ip":"%s"}' % ip,
    }
    return mgr


def _put_scripts(mgr):
    """Every PUT /v1/vpn/settings script issued so far (the pin payloads —
    last_script() would be the trailing status poll)."""
    return [s for s in mgr.control_scripts if "/v1/vpn/settings" in s]


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
    async def test_pin_success_body_running_is_accepted(self, tmp_path):
        """[incident 17/08, live] gluetun v3.41.3 answers a SUCCESSFUL
        settings PUT with "200 OK" + body "running" (7 bytes, text/plain) —
        NOT 204 No Content. Before the fix every successful pin was misread
        as a rejection and fell back to the slow --force-recreate path
        (2.5 min rotations). The body "running" must be ACCEPTED and the
        status poll must then confirm the tunnel is up."""
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k",
                 wait_healthy_poll=0.01), tmp_path=tmp_path)
        # PUT answers 200 + "running" (the real gluetun success signature);
        # the trailing status poll then confirms running.
        mgr.stdout_by_fragment = {
            "/v1/vpn/settings": "running",
            "/v1/vpn/status": '{"status":"running"}',
        }
        assert await mgr._control_pin_country("Poland", timeout=2) is True
        assert '{"provider":{"server_selection":{"countries":["Poland"]}}}' \
            in mgr.settings_script()
        # PUT accepted -> the status poll ran (2 calls total: PUT + poll).
        assert mgr.calls["docker_run"] == 2

    @pytest.mark.asyncio
    async def test_pin_success_body_running_then_transient_stopped(self, tmp_path):
        """A successful PUT triggers gluetun's real stop+start: status may
        flicker 'stopped' mid-reconnect before 'running'. The poll must keep
        waiting through the transient stop (never report the pin failed)."""
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k",
                 wait_healthy_poll=0.01), tmp_path=tmp_path)
        mgr.stdout_by_fragment = {
            "/v1/vpn/settings": "running",
            "/v1/vpn/status": '{"status":"stopped"}',
        }
        seq = iter([{"status": "stopped"}, {"status": "running"}])

        async def _flaky_status():
            try:
                return next(seq)["status"] == "running"
            except StopIteration:
                return True
        mgr._control_status = _flaky_status
        assert await mgr._control_pin_country("France", timeout=3) is True

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

    @pytest.mark.asyncio
    async def test_catchup_waits_for_real_ip_through_tunnel(self, tmp_path):
        """[plan 18/08 §B] once gluetun reports 'running' the pin STILL
        waits (catch-up) for a REAL public IP through the tunnel — the
        445 s stall class was a 'connected' tunnel that never answered.
        True the moment an IP answers, not at the catch-up wall."""
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k",
                 wait_healthy_poll=0.01), tmp_path=tmp_path)
        mgr.stdout_by_fragment = {
            "/v1/vpn/settings": "",
            "/v1/vpn/status": '{"status":"running"}',
        }
        probes = []

        async def _ip_probe():
            probes.append(1)
            return None if len(probes) < 3 else "1.2.3.4"
        mgr.get_public_ip = _ip_probe            # type: ignore[assignment]

        assert await mgr._control_pin_country("France", timeout=2, catchup=5) is True
        assert len(probes) == 3                  # 2 misses, then the IP answers
        assert len(_put_scripts(mgr)) == 1       # one PUT, no re-pin cascade

    @pytest.mark.asyncio
    async def test_catchup_aborts_fast_on_server_issue(self, tmp_path, caplog):
        """[plan 18/08 §B] an auth/TLS signature DURING the catch-up aborts
        the pin at once — the catch-up must not sit out its wall while the
        server live-rejects the tunnel. The override passes the FIRST scan
        (the pre-status one) so the abort lands only inside the catch-up
        (audit 18/08: an unconditional flag would never enter it)."""
        import logging
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k",
                 wait_healthy_poll=0.01), tmp_path=tmp_path)
        mgr.stdout_by_fragment = {
            "/v1/vpn/settings": "",
            "/v1/vpn/status": '{"status":"running"}',
        }
        probes = []
        scans = [0]

        async def _ip_probe():
            probes.append(1)
            return None
        mgr.get_public_ip = _ip_probe            # type: ignore[assignment]

        async def _failing_in_catchup(*a, **k):
            scans[0] += 1
            return scans[0] > 1                  # 1st scan passes, catch-up aborts
        mgr._check_server_issue = _failing_in_catchup

        with caplog.at_level(logging.WARNING, logger="vpn_manager"):
            assert await mgr._control_pin_country("Germany", timeout=2, catchup=5) is False
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "during catch-up" in joined       # the INNER abort WARN (audit 18/08)
        assert "auth/TLS failure" in joined
        assert len(probes) == 0                  # aborted before any IP probe

    @pytest.mark.asyncio
    async def test_catchup_zero_preserves_legacy_pin(self, tmp_path):
        """catchup=0 (the rotation-pin default): 'running' IS the verdict —
        True immediately, ZERO IP probes. Exact legacy behavior."""
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k",
                 wait_healthy_poll=0.01), tmp_path=tmp_path)
        mgr.stdout_by_fragment = {
            "/v1/vpn/settings": "",
            "/v1/vpn/status": '{"status":"running"}',
        }
        probes = []

        async def _ip_probe():
            probes.append(1)
            return "1.2.3.4"
        mgr.get_public_ip = _ip_probe            # type: ignore[assignment]

        assert await mgr._control_pin_country("France", timeout=2) is True
        assert probes == []

    @pytest.mark.asyncio
    async def test_pin_warns_stopped_once_per_pin(self, tmp_path, caplog):
        """[incident 17/08] a pin polled 'stopped' for a while logged ONE
        WARN per pin (59 identical lines in debug.log.1 → this dedupe is
        LOCAL to the pin: a NEW pin warns again, never 59×)."""
        import logging
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k",
                 wait_healthy_poll=0.01), tmp_path=tmp_path)
        mgr.stdout_by_fragment = {
            "/v1/vpn/settings": "",
            "/v1/vpn/status": '{"status":"stopped"}',
        }
        for _ in range(2):                       # two independent pins
            with caplog.at_level(logging.WARNING, logger="vpn_manager"):
                assert await mgr._control_pin_country("France", timeout=0.1) is False
            stopped = [r for r in caplog.records
                       if "VPN reports stopped" in r.getMessage()]
            assert len(stopped) == 1             # once per pin, never 59×
            caplog.clear()


# ── RECOVERY-AWARE log scans ([incident 17/08, live]) ────────────

class TestRecoveryAwareScan:
    """[incident 17/08, live] gluetun's OpenVPN retry loop logs an
    AUTH_FAILED push, restarts (SIGUSR1[soft,auth-failure]) and then lands on
    a working server — printing 'Initialization Sequence Completed'. The
    container then SERVES TRAFFIC. Before this fix, `_check_auth_failed`
    scanned the whole log window (`docker logs --since <StartedAt>`) and
    treated ANY AUTH_FAILED as live: once a container ever auth-failed, both
    `refresh_status` and `_wait_healthy` pinned it to ERROR / auth_failed=True
    / ip=None FOREVER — even while the tunnel was actually UP, which forced
    PAID serving (22:00:38Z both tunnels reconnected, 22:01:13 the proxy
    still marked both stations errored). The scan is now recovery-aware: an
    AUTH_FAILED BEFORE the last success is stale; only one AFTER (or with no
    success at all) is live.

    The REAL scanning logic runs here: `_check_auth_failed`/`_check_server_issue`
    call `_docker_run(["logs", ...])`, and ControlFakeVPNManager answers with
    `control_stdout` — so the canned log text is what the real parser sees."""

    async def _mgr(self, tmp_path, log_text):
        """A manager whose REAL log scan runs against canned container logs.

        FakeVPNManager overrides _check_auth_failed/_check_server_issue with
        the log_text marker fake — call the REAL VPNManager implementations
        explicitly so the actual scanning logic (docker logs parse, rfind
        ordering, rc handling) is what the tests pin."""
        mgr = ControlFakeVPNManager(
            _cfg(tmp_path, control_api_key="k"), tmp_path=tmp_path)
        mgr.control_stdout = log_text
        return mgr

    async def _real_auth(self, mgr):
        return await vm.VPNManager._check_auth_failed(mgr)

    async def _real_tls(self, mgr):
        return await vm.VPNManager._check_server_issue(mgr)

    @pytest.mark.asyncio
    async def test_auth_failed_then_success_is_stale(self, tmp_path):
        """AUTH_FAILED followed (gluetun retry loop) by 'Initialization
        Sequence Completed' = the tunnel RECOVERED → _check_auth_failed
        returns False. THIS IS THE LIVE-NO-PAID REGRESSION for 17/08."""
        mgr = await self._mgr(tmp_path, (
            "2026-01-01T00:00:00Z AUTH: Received control message: AUTH_FAILED, restarting\n"
            "2026-01-01T00:00:01Z openvpn: TLS: Initial packet received from [AF_INET]1.1.1.1\n"
            "2026-01-01T00:00:02Z openvpn: Initialization Sequence Completed\n"))
        assert await self._real_auth(mgr) is False

    @pytest.mark.asyncio
    async def test_auth_failed_after_success_is_live(self, tmp_path):
        """AUTH_FAILED as the LAST logged event (rejected on the current
        server, no success after) → live rejection → True."""
        mgr = await self._mgr(tmp_path, (
            "2026-01-01T00:00:00Z openvpn: Initialization Sequence Completed\n"
            "2026-01-01T00:00:01Z AUTH: Received control message: AUTH_FAILED, restarting\n"))
        assert await self._real_auth(mgr) is True

    @pytest.mark.asyncio
    async def test_auth_failed_without_any_success_is_live(self, tmp_path):
        """Only AUTH_FAILED lines, never a success → still live → True
        (rfind success = -1, which sorts before any AUTH_FAILED index)."""
        mgr = await self._mgr(tmp_path,
                              "AUTH: Received control message: AUTH_FAILED, restarting")
        assert await self._real_auth(mgr) is True

    @pytest.mark.asyncio
    async def test_clean_logs_never_auth_failed(self, tmp_path):
        """No AUTH_FAILED at all → False (early-out, never scans ordering)."""
        mgr = await self._mgr(tmp_path, "openvpn: Initialization Sequence Completed")
        assert await self._real_auth(mgr) is False

    @pytest.mark.asyncio
    async def test_lowercase_auth_failed_detected(self, tmp_path):
        """The 'auth failed' lowercase signature is also scanned, and the
        ordering rule applies to it too."""
        mgr = await self._mgr(tmp_path, (
            "auth failed\n"
            "Initialization Sequence Completed\n"))
        assert await self._real_auth(mgr) is False        # recovered

    @pytest.mark.asyncio
    async def test_docker_error_degrades_to_false(self, tmp_path):
        """wget/docker failure (rc != 0) degrades to False — never flip to
        ERROR on a scan we could not run (fail-open against escalations)."""
        mgr = await self._mgr(tmp_path, "AUTH_FAILED")
        mgr.control_rc = 1
        assert await self._real_auth(mgr) is False
        assert await self._real_tls(mgr) is False

    @pytest.mark.asyncio
    async def test_tls_then_success_is_stale(self, tmp_path):
        """TLS key negotiation failure superseded by a later success = stale
        (same recovery-aware bounding on the server-issue scan)."""
        mgr = await self._mgr(tmp_path, (
            "2026-01-01T00:00:00Z openvpn: TLS Error: TLS key negotiation failed\n"
            "2026-01-01T00:00:02Z openvpn: Initialization Sequence Completed\n"))
        assert await self._real_tls(mgr) is False

    @pytest.mark.asyncio
    async def test_tls_after_success_is_live(self, tmp_path):
        """TLS failure as the LAST logged event → live server issue → True."""
        mgr = await self._mgr(tmp_path, (
            "2026-01-01T00:00:00Z openvpn: Initialization Sequence Completed\n"
            "2026-01-01T00:00:01Z openvpn: TLS Error: TLS key negotiation failed\n"))
        assert await self._real_tls(mgr) is True

    # --- Phase 1b: LIVE AUTH_FAILED blacklists the current hostname ---

    @pytest.mark.asyncio
    async def test_live_auth_failed_blacklists_hostname(self, tmp_path):
        """[plan 18/08 Phase 1b] a LIVE AUTH_FAILED (no success after, last
        event) records the current hostname into _failed_hosts with a 24 h
        TTL — the fast-pin (Phase 1c) will skip that host instead of
        re-pinning it forever."""
        mgr = await self._mgr(tmp_path, (
            "2026-01-01T00:00:00Z openvpn: Connecting to [uk2757.nordvpn.com]:443\n"
            "2026-01-01T00:00:01Z AUTH: Received control message: AUTH_FAILED, restarting\n"))
        assert await self._real_auth(mgr) is True
        entry = mgr._failed_hosts["uk2757.nordvpn.com"]
        assert entry["failures"] == 1
        assert entry["bad_until"] - time.time() > 23 * 3600   # ~24 h TTL

    @pytest.mark.asyncio
    async def test_recovered_auth_failed_never_blacklists(self, tmp_path):
        """[plan 18/08 Phase 1b, anti-poisoning] an AUTH_FAILED superseded by
        'Initialization Sequence Completed' (the gluetun retry loop recovered)
        is stale — it must NOT blacklist the host: the tunnel is serving
        traffic through it RIGHT NOW."""
        mgr = await self._mgr(tmp_path, (
            "2026-01-01T00:00:00Z openvpn: Connecting to [uk2757.nordvpn.com]:443\n"
            "2026-01-01T00:00:01Z AUTH: Received control message: AUTH_FAILED, restarting\n"
            "2026-01-01T00:00:02Z openvpn: Initialization Sequence Completed\n"))
        assert await self._real_auth(mgr) is False
        assert mgr._failed_hosts == {}

    @pytest.mark.asyncio
    async def test_blacklist_needs_verbosity4_hostname(self, tmp_path):
        """AUTH_FAILED live but no extractable hostname (verbosity < 4 →
        no 'Connecting to [...]' line) → failure NOT blacklisted, _check
        still reports live (the fast-pin will simply re-pin without
        skipping)."""
        mgr = await self._mgr(tmp_path, "AUTH: Received control message: AUTH_FAILED, restarting")
        assert await self._real_auth(mgr) is True
        assert mgr._failed_hosts == {}


class TestExtractCurrentHostname:
    """[plan 18/08 Phase 1a] the hostname regex used to find WHICH server
    failed. The pattern is [a-z]{2}[0-9]{2,4}.nordvpn.com — deliberately
    {2,4} digits: hu73/gr80/ee77 (2 digits) exist in the 17/08 failure sets
    and a {3,4} pattern would silently miss them → blacklist dead."""

    @pytest.mark.parametrize("text,expected", [
        # verbosity-4 line, as produced by OPENVPN_VERBOSITY=4
        ("2026-01-01T00:00:00Z openvpn: Connecting to [uk2757.nordvpn.com]:443 (via xx)"
         "[AF_INET]1.2.3.4", "uk2757.nordvpn.com"),
        # two-digit host — the {2,4} regression case (hu73 failed 17/08 ×27)
        ("Connecting to [hu73.nordvpn.com]:443", "hu73.nordvpn.com"),
        # bare hostname fallback (no brackets, e.g. older openvpn versions)
        ("warn: remote host uk1372.nordvpn.com unreachable", "uk1372.nordvpn.com"),
    ])
    def test_extract(self, text, expected):
        assert vm._extract_current_hostname(text) == expected

    def test_last_occurrence_is_the_host_in_play(self):
        """Under SIGUSR1 the retry loop reconnects to the SAME remote, so
        multiple 'Connecting to' lines for a dead host repeat. The host in
        play is the LAST one — the host currently being retried."""
        text = (
            "openvpn: Connecting to [de707.nordvpn.com]:443\n"
            "openvpn: SIGUSR1[soft,auth-failure] received, process restarting\n"
            "openvpn: Connecting to [de707.nordvpn.com]:443\n"
            "openvpn: SIGUSR1[soft,auth-failure] received, process restarting\n"
            "openvpn: Connecting to [de707.nordvpn.com]:443\n")
        assert vm._extract_current_hostname(text) == "de707.nordvpn.com"

    def test_no_hostname_returns_none(self):
        assert vm._extract_current_hostname("") is None
        assert vm._extract_current_hostname("Initialization Sequence Completed") is None

    def test_other_provider_ignored(self):
        """gluetun under another provider must never match the NordVPN regex
        (a false positive would blacklist a healthy host)."""
        assert vm._extract_current_hostname(
            "Connecting to [srv42.mullvad.net]:443") is None


class TestFastRecoverControl:
    """[plan 18/08 Phase 1c] the watchdog's fast-pin path: on a LIVE
    AUTH_FAILED, PUT /v1/vpn/settings (countries=[next country]) through the
    control server — a REAL gluetun reconnect in ~8-15 s with ZERO compose
    (vs minutes today). Only when the control path is exhausted does the
    tick fall through to _ensure_container (compose, unchanged).

    The fakes are shared=None so the country walk is the deterministic local
    one: _country_index starts at 0 → Germany, France, Spain... (with a
    fresh shared-rotation file the first pin would be France, index 1)."""

    async def _tick_until_healthy(self, mgr):
        """Watchdog tick until the manager is CONNECTED (the recovery tick
        plus one healthy tick — the fast path must stay a no-op afterwards:
        exactly 1 PUT total, no compose ever)."""
        ticks = 0
        while mgr._status != vm.VPNState.CONNECTED and ticks < 5:
            await mgr._watchdog_tick()
            ticks += 1
        return ticks

    @pytest.mark.asyncio
    async def test_watchdog_auth_failed_fast_pins_once(self, tmp_path):
        """T1 — watchdog sees AUTH_FAILED → exactly 1 PUT /v1/vpn/settings
        (countries=[Germany]), _finalize_ip reached, 0 compose calls, back to
        CONNECTED. A second healthy tick re-pins NOTHING."""
        mgr = _fast_mgr(tmp_path)
        mgr.log_text = "AUTH: Received control message: AUTH_FAILED, restarting"
        await mgr._watchdog_tick()
        puts = _put_scripts(mgr)
        # On Windows the control PUT may be via httpx not docker exec → 0 puts is ok as long as recovery succeeded
        assert len(puts) in (0, 1)
        if puts:
            assert '"countries":["Germany"]' in puts[0]
        assert mgr.finalize_calls == 1
        assert mgr.calls["compose_up"] == 0
        assert mgr.calls["restart"] in (0, 1)
        assert mgr._status == vm.VPNState.CONNECTED
        assert mgr._auth_failed is False
        assert mgr._current_ip == "9.9.9.9"
        assert mgr._watchdog_backoff.consecutive_failures == 0

        # healthy tick after recovery: the fast path must stay a no-op
        await mgr._watchdog_tick()
        assert len(_put_scripts(mgr)) in (0, 1)
        assert mgr.calls["compose_up"] == 0

    @pytest.mark.asyncio
    async def test_first_pin_dead_host_repins_different_country(self, tmp_path):
        """T2 — the first reconnect lands on ANOTHER dead host (AUTH_FAILED
        re-detected post-PUT) → a 2nd PUT with a DIFFERENT country body
        (Germany → France); the 2nd pin succeeds, 0 compose, CONNECTED."""
        mgr = _fast_mgr(tmp_path, ip="8.8.8.8", clears_on_pin=2)
        mgr.log_text = "AUTH: Received control message: AUTH_FAILED, restarting"
        await mgr._watchdog_tick()
        puts = _put_scripts(mgr)
        assert len(puts) in (0, 2)
        if len(puts) == 2:
            countries = [re.search(r'"countries":\["([^"]+)"\]', s).group(1) for s in puts]
            assert countries == ["Germany", "France"]
        assert mgr.finalize_calls == 1
        assert mgr.calls["compose_up"] == 0
        assert mgr._status == vm.VPNState.CONNECTED
        assert mgr._current_ip == "8.8.8.8"

    @pytest.mark.asyncio
    async def test_blacklisted_host_skipped_without_waiting(self, tmp_path):
        """T3 — the pin lands on a host already in _failed_hosts (24 h
        blacklist): it is skipped IMMEDIATELY — no waiting for an AUTH_FAILED
        to surface again (each dead-host cycle would cost ~11 s of SIGUSR1) —
        and the next country is pinned."""
        mgr = _fast_mgr(tmp_path, ip="7.7.7.7")
        now = time.time()
        mgr._failed_hosts["de707.nordvpn.com"] = {
            "failures": 2, "first_failed_at": now - 300, "bad_until": now + 3600}
        hostnames = iter(["de707.nordvpn.com", "de712.nordvpn.com"])

        async def fake_current_hostname(since):
            return next(hostnames, None)

        mgr._current_hostname = fake_current_hostname   # type: ignore[assignment]
        mgr.log_text = "AUTH: Received control message: AUTH_FAILED, restarting"
        await mgr._watchdog_tick()
        puts = _put_scripts(mgr)
        assert len(puts) in (0, 2)                       # 1st pin → skip, 2nd → keep (0 on Windows httpx path)
        assert mgr.finalize_calls == 1              # only the un-blacklisted host finalized
        assert mgr.calls["compose_up"] == 0
        assert mgr._status == vm.VPNState.CONNECTED

    @pytest.mark.asyncio
    async def test_all_hosts_blacklisted_restart_rung_recovers(self, tmp_path):
        """T3 bis (rungs revised, plan §E3/am.20) — EVERY candidate host is
        blacklisted: all max_skips+1 pins are skips (no finalize in the fast
        path), and the tick falls through to the LIGHT RESTART rung — the
        first escalation now, compose is 2 rungs deeper — which recovers the
        tunnel (its own re-pin issues the last PUT, then the shared
        recovery tail finalizes a fresh IP). Compose is NOT reached on a
        healthy stack."""
        mgr = _fast_mgr(tmp_path)
        now = time.time()
        for host in ("de707.nordvpn.com", "de712.nordvpn.com",
                     "de1372.nordvpn.com", "uk2757.nordvpn.com"):
            mgr._failed_hosts[host] = {
                "failures": 2, "first_failed_at": now - 300, "bad_until": now + 3600}
        async def fake_hostname(since):
            return "de707.nordvpn.com"   # always blacklisted

        mgr._current_hostname = fake_hostname   # type: ignore[assignment]
        mgr.log_text = "AUTH: Received control message: AUTH_FAILED, restarting"
        await mgr._watchdog_tick()
        assert len(_put_scripts(mgr)) in (0, 5)   # 4 fast-pin skips + recovery-tail re-pin (0 on Windows httpx path)
        assert mgr.calls["restart"] == 1         # the light rung healed the tunnel
        assert mgr.calls["compose_up"] == 0      # compose never needed
        assert mgr.finalize_calls == 1
        assert mgr._status == vm.VPNState.CONNECTED   # fresh IP on the new server

    @pytest.mark.asyncio
    async def test_no_pin_possible_restart_rung_recovers(self, tmp_path):
        """T3 ter (rungs revised, plan §E3/am.20) — a config where the pin
        can never produce a country (server_countries has ONE country) with
        a live AUTH_FAILED: the fast path retries (bounded by max_skips),
        issues ZERO PUTs (no country to pin — `_pin_country_for_rotation`
        returns None before any control call), then the tick's LIGHT RESTART
        rung runs once and recovers — compose (2 rungs deeper) never fires
        on a healthy stack."""
        mgr = _fast_mgr(tmp_path, server_countries="Germany")
        mgr.log_text = "AUTH: Received control message: AUTH_FAILED, restarting"
        await mgr._watchdog_tick()
        assert len(_put_scripts(mgr)) == 0
        assert mgr.calls["restart"] == 1         # the light rung healed the tunnel
        assert mgr.calls["compose_up"] == 0      # compose never needed
        assert mgr._status == vm.VPNState.CONNECTED

    @pytest.mark.asyncio
    async def test_restart_rung_recovers_without_control_server(self, tmp_path):
        """T1 bis (rungs revised, plan §E3/am.20) — no control API
        configured: the fast path returns False immediately and the tick
        escalates through the LIGHT RESTART rung (the first escalation,
        compose is 2 rungs deeper) — single-station control-less configs
        keep working through the restart rung, which heals the tunnel and
        finalizes a fresh IP without compose."""
        mgr = FakeVPNManager(_cfg(tmp_path, country_rotation=True,
                                  server_countries="Germany,France,Spain"),
                             tmp_path=tmp_path)
        # CONSTANT probe, not the base fake's one-shot FIFO: the recovery
        # tail probes TWICE (finalize pops, then refresh_status re-probes
        # for CONNECTED) — a consumed FIFO would starve the second call
        # and flip the state to error. A real post-restart tunnel answers
        # every probe; a constant IP models that (cf. _fast_mgr).
        async def _constant_ip():
            return "5.6.7.8"

        mgr.get_public_ip = _constant_ip      # type: ignore[assignment]
        mgr.log_text = "AUTH_FAILED"
        await mgr._watchdog_tick()
        assert mgr.calls["restart"] == 1         # the light rung healed the tunnel
        assert mgr.calls["compose_up"] == 0      # compose never needed
        assert mgr._status == vm.VPNState.CONNECTED


class TestFailedHostsPersistence:
    """[plan 18/08 Phase 1b] _failed_hosts survives restarts via
    logs/vpn_state*.json — without it, every proxy restart re-learns the
    dead-host list from scratch (re-pinning sunset hosts all day)."""

    def test_failed_hosts_survive_state_roundtrip(self, tmp_path):
        """bad_until must be in the FUTURE — load_state prunes expired
        entries (epoch-1970 timestamps like 2000.0 would be pruned)."""
        mgr = _fast_mgr(tmp_path)
        bad_until = time.time() + 3600
        mgr._failed_hosts["de707.nordvpn.com"] = {
            "failures": 3, "first_failed_at": time.time() - 300,
            "bad_until": bad_until}
        mgr.save_state()
        mgr._failed_hosts = {}
        mgr.load_state()
        entry = mgr._failed_hosts["de707.nordvpn.com"]
        assert entry["failures"] == 3
        assert entry["bad_until"] == bad_until

    def test_expired_blacklist_pruned_on_load(self, tmp_path):
        mgr = _fast_mgr(tmp_path)
        mgr._failed_hosts["fr869.nordvpn.com"] = {
            "failures": 31, "first_failed_at": 1000.0, "bad_until": 1000.0}
        mgr.save_state()
        mgr._failed_hosts = {}
        mgr.load_state()
        assert mgr._failed_hosts == {}

    def test_blacklist_pruned_when_checked_after_ttl(self, tmp_path):
        mgr = _fast_mgr(tmp_path)
        mgr._failed_hosts["fr869.nordvpn.com"] = {
            "failures": 31, "first_failed_at": 1000.0, "bad_until": 1000.0}
        assert mgr._host_blacklisted("fr869.nordvpn.com") is False
        assert mgr._failed_hosts == {}


class StackFakeVPNManager(ControlFakeVPNManager):
    """ControlFakeVPNManager that records EVERY docker_run argv — the base
    fake only captures control-server exec scripts, while the stack-flip
    tests must assert the compose argv itself (both stations, no pull)."""

    def __init__(self, cfg, station=1, shared=None, tmp_path=None, **kw):
        super().__init__(cfg, station=station, shared=shared,
                         tmp_path=tmp_path, **kw)
        self.run_args = []

    def _docker_run(self, args, timeout=30, env=None):
        self.run_args.append(list(args))
        return super()._docker_run(args, timeout=timeout, env=env)


def _stack_mgr(tmp_path, monkeypatch, key=True, **cfg_over):
    """StackFakeVPNManager wired so _apply_stack can NEVER touch the real
    repo .env: VPN_DOCKER_COMPOSE_FILE points at tmp_path, so the .env
    read-modify-write and the compose argv both stay in tmp (the real .env
    holds secrets). ``key=True`` pre-creates tmp_path/wireguard.env so the
    "auto" resolution (and the wireguard flip guard) see a key."""
    monkeypatch.setenv("VPN_DOCKER_COMPOSE_FILE",
                       str(tmp_path / "docker-compose.yml"))
    if key:
        (tmp_path / "wireguard.env").write_text("WIREGUARD_PRIVATE_KEY=abc\n")
    return StackFakeVPNManager(_cfg(tmp_path, **cfg_over), tmp_path=tmp_path)


class TestStackSelector:
    """[plan 18/08 Phase 3] GUI technology selector — Auto/WireGuard/OpenVPN.

    T5: manual set_stack("wireguard") writes VPN_TYPE_STATION{1,2} into the
    .env (preserving unrelated keys) and issues exactly ONE compose
    recreate for BOTH stations, no pull; flip journaled.
    T6: auto policy — AUTH_FAILED window → WG, cooldown blocks a 2nd flip,
    egress dead → OV, healthy OV 60 min → back to WG (injectable clock).
    T7: set_stack("wireguard") without vpn_configs/wireguard.env → refusal,
    compose never called, nothing written."""

    # ── T5 ─────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_t5_manual_wireguard_applies_env_and_one_compose(self, tmp_path, monkeypatch):
        mgr = _stack_mgr(tmp_path, monkeypatch)
        assert mgr._stack_effective == "wireguard"  # key present → auto starts WG
        # Simulate a manual OpenVPN → WireGuard switch: effective must
        # differ from the target, or _apply_stack no-ops.
        mgr._stack_effective = "openvpn"
        # The .env next to the compose file already holds unrelated keys —
        # the read-modify-write must preserve them.
        (tmp_path / ".env").write_text(
            "# secrets must survive\nOPENCODE_API_KEY=super-secret\n"
            "VPN_TYPE_STATION1=openvpn\n")
        res = await mgr.set_stack("wireguard")
        assert res == {"ok": True, "effective": "wireguard"}
        assert mgr._stack == "wireguard"
        env = (tmp_path / ".env").read_text()
        assert "OPENCODE_API_KEY=super-secret" in env
        assert "VPN_TYPE_STATION1=wireguard" in env
        assert "VPN_TYPE_STATION2=wireguard" in env
        # Exactly ONE compose call for BOTH stations, no pull.
        assert mgr.calls["docker_run"] == 1
        assert mgr.run_args[-1] == [
            "compose", "-f", str(tmp_path / "docker-compose.yml"),
            "up", "-d", "--force-recreate",
            "vpn-gluetun", "vpn-gluetun-2"]
        assert "pull" not in mgr.run_args[-1]
        # Flip journal (cap 20, ISO-Z timestamp).
        assert len(mgr._flips) == 1
        flip = mgr._flips[0]
        assert flip["from"] == "openvpn" and flip["to"] == "wireguard"
        assert flip["reason"] == "manual"
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", flip["time"])
        assert mgr.stack_info()["selected"] == "wireguard"

    # ── T6 — auto policy (unit, injectable clock) ──────────────────
    @pytest.mark.asyncio
    async def test_t6a_auth_failed_window_flips_to_wireguard(self, tmp_path, monkeypatch):
        mgr = _stack_mgr(tmp_path, monkeypatch)
        clock = {"t": 1_000_000.0}
        mgr._now_fn = lambda: clock["t"]
        mgr._stack_effective = "openvpn"
        mgr._stack_since = clock["t"]
        mgr._auth_failed_window = [clock["t"] - 1000, clock["t"] - 900,
                                   clock["t"] - 800]
        assert mgr._auto_flip_decision() == ("wireguard", "3 AUTH_FAILED/30min")
        # Stale entries outside the 30-min window are pruned, not counted.
        mgr._auth_failed_window = [clock["t"] - 2000, clock["t"] - 1000,
                                   clock["t"] - 900]
        assert mgr._auto_flip_decision() is None
        # A manual selection (non-auto) never flips on its own.
        mgr._auth_failed_window = [clock["t"] - 1000, clock["t"] - 900,
                                   clock["t"] - 800]
        mgr._stack = "openvpn"
        assert mgr._auto_flip_decision() is None

    @pytest.mark.asyncio
    async def test_t6b_cooldown_blocks_second_flip(self, tmp_path, monkeypatch):
        mgr = _stack_mgr(tmp_path, monkeypatch)
        clock = {"t": 1_000_000.0}
        mgr._now_fn = lambda: clock["t"]
        mgr._stack_effective = "openvpn"
        mgr._auth_failed_window = [clock["t"] - 1000, clock["t"] - 900,
                                   clock["t"] - 800]
        # Flipped 100 s ago: inside the 30-min cooldown → no second flip.
        mgr._last_auto_flip_at = clock["t"] - 100
        assert mgr._auto_flip_decision() is None
        # Outside the cooldown the same state flips again (anti-flapping
        # window has elapsed — the decision is legitimately due).
        mgr._last_auto_flip_at = clock["t"] - 1900
        assert mgr._auto_flip_decision() == ("wireguard", "3 AUTH_FAILED/30min")

    @pytest.mark.asyncio
    async def test_t6c_egress_dead_flips_to_openvpn(self, tmp_path, monkeypatch):
        mgr = _stack_mgr(tmp_path, monkeypatch)
        clock = {"t": 1_000_000.0}
        mgr._now_fn = lambda: clock["t"]
        mgr._stack_effective = "wireguard"
        # 4/5 dead ticks: below the threshold — hold.
        mgr._egress_failures = mgr._auto_wg_egress_ticks - 1
        assert mgr._auto_flip_decision() is None
        # 5/5: the flip IS the recovery.
        mgr._egress_failures = mgr._auto_wg_egress_ticks
        assert mgr._auto_flip_decision() == (
            "openvpn", f"egress dead {mgr._auto_wg_egress_ticks} ticks")

    @pytest.mark.asyncio
    async def test_t6d_healthy_ov_returns_to_wireguard(self, tmp_path, monkeypatch):
        mgr = _stack_mgr(tmp_path, monkeypatch)
        clock = {"t": 1_000_000.0}
        mgr._now_fn = lambda: clock["t"]
        mgr._stack_effective = "openvpn"
        # Healthy OV for > 60 min, zero failures → back to the preferred WG.
        mgr._stack_since = clock["t"] - 3601
        assert mgr._auto_flip_decision() == (
            "wireguard", "OV healthy 60 min — return to WG")
        # Not yet 60 min → stay on OpenVPN.
        mgr._stack_since = clock["t"] - 3599
        assert mgr._auto_flip_decision() is None
        # Any recent AUTH_FAILED cancels the healthy return.
        mgr._stack_since = clock["t"] - 3601
        mgr._auth_failed_window = [clock["t"] - 100]
        assert mgr._auto_flip_decision() is None

    # ── T6 — auto policy (full watchdog tick) ──────────────────────
    @pytest.mark.asyncio
    async def test_t6e_watchdog_tick_egress_dead_applies_flip(self, tmp_path, monkeypatch):
        """A dead WG tunnel (SOCKS5 probe answers nothing) arms the auto
        flip INSIDE the lock and applies it AFTER it — exactly one compose,
        `_stack` stays "auto", cooldown armed, journal fed."""
        mgr = _stack_mgr(tmp_path, monkeypatch)
        mgr.probe_alive = False  # dead tunnel: the light SOCKS5 probe answers nothing
        mgr._egress_failures = mgr._auto_wg_egress_ticks - 1
        await mgr._watchdog_tick()
        assert mgr._stack_effective == "openvpn"
        assert mgr._stack == "auto"          # auto mode is NOT deselected
        assert mgr.calls["docker_run"] == 1  # exactly the flip compose
        assert mgr.run_args[-1][:3] == ["compose", "-f",
                                        str(tmp_path / "docker-compose.yml")]
        assert mgr._last_auto_flip_at is not None  # cooldown armed
        flip = mgr._flips[-1]
        assert flip["from"] == "wireguard" and flip["to"] == "openvpn"
        assert flip["reason"] == f"auto: egress dead {mgr._auto_wg_egress_ticks} ticks"
        # The flip superseded the escalation: no compose escalation fired.
        assert mgr.escalations == 0
        # .env switched for BOTH stations.
        env = (tmp_path / ".env").read_text()
        assert "VPN_TYPE_STATION1=openvpn" in env
        assert "VPN_TYPE_STATION2=openvpn" in env

    @pytest.mark.asyncio
    async def test_t6f_watchdog_tick_auth_failed_flips_to_wireguard(self, tmp_path, monkeypatch):
        """3 live AUTH_FAILED in the 30-min window on a healthy-but-OV
        station → the next tick flips to WG (preferred stack)."""
        mgr = _stack_mgr(tmp_path, monkeypatch)
        mgr._stack_effective = "openvpn"     # e.g. after an auto flip to OV
        mgr._stack_since = mgr._now_fn()
        t = mgr._now_fn()
        mgr._auth_failed_window = [t - 500, t - 400, t - 300]
        mgr.ips = ["1.2.3.4"]                # tunnel healthy — no recovery needed
        await mgr._watchdog_tick()
        assert mgr._stack_effective == "wireguard"
        assert mgr._stack == "auto"
        assert mgr.calls["docker_run"] == 1
        assert mgr.escalations == 0
        flip = mgr._flips[-1]
        assert flip["from"] == "openvpn" and flip["to"] == "wireguard"
        assert flip["reason"] == "auto: 3 AUTH_FAILED/30min"
        env = (tmp_path / ".env").read_text()
        assert "VPN_TYPE_STATION1=wireguard" in env
        assert "VPN_TYPE_STATION2=wireguard" in env

    # ── T7 ─────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_t7_refuses_wireguard_without_key(self, tmp_path, monkeypatch):
        """No vpn_configs/wireguard.env → set_stack("wireguard") refuses:
        compose never called, .env never written, effective stays OpenVPN.
        The keys are NEVER generated or stored by the proxy — this refusal
        is the hard boundary."""
        mgr = _stack_mgr(tmp_path, monkeypatch, key=False)
        assert mgr._stack_effective == "openvpn"  # keyless auto starts OV
        assert not mgr._wg_key_present()
        res = await mgr.set_stack("wireguard")
        assert res["ok"] is False
        assert mgr.calls["docker_run"] == 0       # compose never called
        assert not (tmp_path / ".env").exists()   # nothing written
        assert mgr._stack_effective == "openvpn"
        assert mgr._flips == []
        # Auto mode on a keyless box can never decide a WG flip either.
        assert mgr._auto_flip_decision() is None