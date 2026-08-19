"""
VPN Manager for OpenCode Proxy — gluetun / compose-managed only.

The VPN tunnel is a docker-compose service (vpn-gluetun, container
"opencode-vpn") that survives proxy restarts. Each station owns ONE
service: station N = vpn-gluetun-N (ports 1079+N/8887+N, stations 2+
only when resolved ip_rotation.station_count >= N). This manager never
STOPS containers on shutdown (the tunnel survives proxy restarts) —
only the hot-reload downscale calls stop_container() — but it DOES
bring the service up at startup (and on auto-connect / rotation) via
`docker compose up -d <compose_service>`; it also:

proxy_mode:
  - "vpn": free model requests are routed via socks5://127.0.0.1:<port>
  - "direct": no proxy
"""

import os
import re
import sys
import time
import json
import asyncio
import logging
import subprocess
from typing import Optional

# Cross-module registry of active VPN managers ([plan 18/08 §1]): set by
# opencode.py's lifespan / _apply_station_count, read by _apply_stack to
# scope a stack flip to the currently configured stations. shared_state is
# a plain data module (no imports) — no import cycle.
import shared_state

logger = logging.getLogger(__name__)

# Windows: hide console windows for subprocess calls
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Project root
ROOT = os.path.dirname(os.path.abspath(__file__))


# ── Identity rotation (client fingerprint) ─────────────────────

# Stable desktop impersonation targets of curl_cffi 0.14 (verified by
# Session(impersonate=...) instantiation). Alpha (chrome133a), mobile,
# android, iOS, tor and generic variants are excluded.
_KNOWN_IMPERSONATIONS = frozenset({
    "chrome99", "chrome100", "chrome101", "chrome104", "chrome107",
    "chrome110", "chrome116", "chrome119", "chrome120", "chrome123",
    "chrome124", "chrome131", "chrome136", "chrome142",
    "edge99", "edge101",
    "firefox133", "firefox135", "firefox144",
    "safari153", "safari155", "safari15_3", "safari15_5", "safari170",
    "safari17_0", "safari180", "safari184", "safari18_0", "safari18_4",
    "safari260", "safari2601",
})

_DEFAULT_IDENTITY_PROFILE = {"impersonate": "chrome131", "user_agent": None, "extra_headers": {}}


def _normalize_identity_profiles(profiles) -> list[dict]:
    """Validate user-supplied identity profiles.

    Each entry: {"impersonate": <known target>, "user_agent": str|None,
    "extra_headers": dict}. Invalid entries are skipped with a warning.
    Never returns an empty list — falls back to the chrome131 default.
    """
    result = []
    if isinstance(profiles, list):
        for p in profiles:
            if not isinstance(p, dict):
                logger.warning("[identity] skipping invalid profile (not a dict): %r", p)
                continue
            imp = str(p.get("impersonate", "")).strip()
            if imp not in _KNOWN_IMPERSONATIONS:
                logger.warning("[identity] skipping unknown impersonation target %r", imp)
                continue
            ua = p.get("user_agent")
            extra = p.get("extra_headers") if isinstance(p.get("extra_headers"), dict) else {}
            result.append({
                "impersonate": imp,
                "user_agent": ua if isinstance(ua, str) and ua.strip() else None,
                "extra_headers": dict(extra),
            })
    if not result:
        logger.warning("[identity] no valid profiles — using default %r",
                       _DEFAULT_IDENTITY_PROFILE["impersonate"])
        result = [dict(_DEFAULT_IDENTITY_PROFILE)]
    return result


# Curated User-Agents keyed by impersonation target. curl_cffi does not
# expose its embedded UA strings to Python, but the httpx fallback paths
# (opencode._apply_identity) need a coherent UA — these are derived by
# family from the target name so every _KNOWN_IMPERSONATIONS target has a
# matching desktop UA. The identity pool keeps user_agent=None so this map
# (httpx paths) and the curl_cffi bundle (curl_cffi paths) stay the single
# sources of the outgoing UA.
def _safari_version(raw: str) -> str:
    """Map a Safari impersonation tail (153 / 15_3 / 2601 ...) to a macOS
    Version string."""
    return {
        "153": "15.3", "15_3": "15.3",
        "155": "15.5", "15_5": "15.5",
        "170": "17.0", "17_0": "17.0",
        "180": "18.0", "18_0": "18.0",
        "184": "18.4", "18_4": "18.4",
        "260": "26.0", "2601": "26.1",
    }.get(raw, "17.0")


def _ua_for_target(imp: str) -> str:
    """Deterministic desktop UA for an impersonation target (by family)."""
    if imp.startswith("chrome"):
        ver = imp[len("chrome"):]
        return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36")
    if imp.startswith("edge"):
        ver = imp[len("edge"):]
        return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{ver}.0.4951.64 Safari/537.36 "
                f"Edg/{ver}.0.1210.53")
    if imp.startswith("firefox"):
        ver = imp[len("firefox"):]
        return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{ver}.0) "
                f"Gecko/20100101 Firefox/{ver}.0")
    if imp.startswith("safari"):
        ver = _safari_version(imp[len("safari"):])
        return (f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                f"AppleWebKit/605.1.15 (KHTML, like Gecko) "
                f"Version/{ver} Safari/605.1.15")
    return ""


# One entry per _KNOWN_IMPERSONATIONS target — the map can never drift
# out of sync with the supported impersonations.
_UA_BY_IMPERSONATE = {imp: _ua_for_target(imp) for imp in _KNOWN_IMPERSONATIONS}

# Accept-Language variants for the identity pool (real-language groups
# with realistic q-values). Chrome/edge targets combine these with two
# sec-ch-ua brand-order permutations; firefox/safari send no client hints
# and only vary the language.
_LANG_VARIANTS = (
    "fr-FR,fr;q=0.9,en;q=0.8",
    "en-US,en;q=0.9,fr;q=0.8,fr-FR;q=0.7",
    "de-DE,de;q=0.9,en;q=0.8",
    "es-ES,es;q=0.9,en;q=0.8",
    "it-IT,it;q=0.9,en;q=0.8,fr;q=0.7",
)


def _identity_header_variants(target: str) -> list[dict]:
    """Per-target extra-header variants for the identity pool.

    chrome/edge -> 2 sec-ch-ua brand-order permutations (same major, greased
    brand list permuted) x 5 Accept-Language; firefox/safari -> the 5
    Accept-Languages only (they emit no User-Agent client hints).
    """
    langs = [{"Accept-Language": lang} for lang in _LANG_VARIANTS]
    if target.startswith(("chrome", "edge")):
        major = "".join(ch for ch in target if ch.isdigit())
        browser = "Microsoft Edge" if target.startswith("edge") else "Google Chrome"
        grease = "99" if (major and int(major) >= 113) else "8"
        chua = (
            f'"Not/A)Brand";v="{grease}", "Chromium";v="{major}", "{browser}";v="{major}"',
            f'"{browser}";v="{major}", "Chromium";v="{major}", "Not/A)Brand";v="{grease}"',
        )
        variants = []
        for order in chua:
            for lang in langs:
                v = dict(lang)
                v["sec-ch-ua"] = order
                variants.append(v)
        return variants
    return langs


def _headers_key(headers: dict) -> tuple:
    """Hashable, order-insensitive key for an extra_headers dict."""
    return tuple(sorted((str(k).lower(), str(v)) for k, v in (headers or {}).items()))


def _build_identity_pool(profiles, diversity: bool, max_profiles: int) -> list[dict]:
    """Expand explicit identities into a large, varied pool.

    diversity=False (default, backward compatible): returns the normalized
    explicit profiles untouched — with only a chrome131 seed the len<=1 gate
    in current_identity pins everyone to profile[0], exactly as before.

    diversity=True: returns the explicit profiles plus, for every known
    impersonation target, a deterministic cartesian grid of per-target
    header variants (chrome/edge x2 sec-ch-ua x5 languages, others x5
    languages) — hundreds of distinct client fingerprints, one per IP.
    Dedup by (impersonate, user_agent, extra_headers), deterministic order,
    capped at max_profiles. user_agent stays None so the curl_cffi bundle
    and the curated _UA_BY_IMPERSONATE map remain the UA sources.
    """
    base = _normalize_identity_profiles(profiles)
    if not diversity:
        return base
    pool = list(base)
    seen = {
        (p["impersonate"], p["user_agent"], _headers_key(p["extra_headers"]))
        for p in base
    }
    for imp in sorted(_KNOWN_IMPERSONATIONS):
        for headers in _identity_header_variants(imp):
            profile = {"impersonate": imp, "user_agent": None, "extra_headers": headers}
            key = (imp, None, _headers_key(headers))
            if key in seen:
                continue
            seen.add(key)
            pool.append(profile)
    pool.sort(key=lambda p: (p["impersonate"], p.get("user_agent") or "",
                             str(sorted((k.lower(), v) for k, v in p["extra_headers"].items()))))
    if len(pool) > max_profiles:
        pool = pool[:max_profiles]
    logger.info("[identity] pool: %d profiles (diversity=%s, cap=%d)",
                len(pool), diversity, max_profiles)
    return pool


# ── State Machine ──────────────────────────────────────────────

class VPNState:
    """Valid VPN connection states and transitions."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    ERROR = "error"

    # Valid transitions: from_state -> set of allowed to_states
    TRANSITIONS = {
        DISCONNECTED: {CONNECTING, ERROR},
        CONNECTING: {CONNECTED, DISCONNECTING, ERROR, DISCONNECTED},
        CONNECTED: {CONNECTING, DISCONNECTING, ERROR},
        DISCONNECTING: {DISCONNECTED, ERROR},
        ERROR: {DISCONNECTED, CONNECTING},
    }

    @classmethod
    def can_transition(cls, from_state: str, to_state: str) -> bool:
        return to_state in cls.TRANSITIONS.get(from_state, set())


# ── Circuit Breaker ────────────────────────────────────────────

class CircuitBreaker:
    """Per-server circuit breaker: tracks consecutive failures.

    States: closed (normal) -> open (failing, skip server) -> half-open (testing)
    """

    def __init__(self, failure_threshold: int = 3, recovery_time: float = 300.0):
        """
        Args:
            failure_threshold: consecutive failures before opening circuit
            recovery_time: seconds to wait before trying again (half-open)
        """
        self._failure_threshold = failure_threshold
        self._recovery_time = recovery_time
        self._servers: dict[str, dict] = {}  # server_name -> {failures, opened_at, state}

    def record_success(self, server_name: str):
        """Record a successful connection — reset failure count."""
        self._servers[server_name] = {"failures": 0, "opened_at": 0, "state": "closed"}

    def record_failure(self, server_name: str):
        """Record a failed connection — increment count, open if threshold reached."""
        info = self._servers.get(server_name, {"failures": 0, "opened_at": 0, "state": "closed"})
        info["failures"] += 1
        if info["failures"] >= self._failure_threshold:
            info["state"] = "open"
            info["opened_at"] = time.monotonic()
            logger.warning("[circuit-breaker] server %s OPEN after %d failures",
                           server_name, info["failures"])
        self._servers[server_name] = info

    def is_available(self, server_name: str) -> bool:
        """Check if a server is available (circuit closed or half-open ready)."""
        info = self._servers.get(server_name)
        if not info or info["state"] == "closed":
            return True
        if info["state"] == "open":
            # Check if recovery time has passed -> half-open
            if time.monotonic() - info["opened_at"] >= self._recovery_time:
                info["state"] = "half-open"
                logger.info("[circuit-breaker] server %s -> half-open (testing)", server_name)
                return True
            return False
        # half-open: allow one attempt
        return True

    def get_status(self) -> dict:
        """Return status of all tracked servers."""
        return {
            name: {"failures": info["failures"], "state": info["state"]}
            for name, info in self._servers.items()
        }


# ── Backoff Timer ──────────────────────────────────────────────

class BackoffTimer:
    """Exponential backoff for connection attempts."""

    def __init__(self, base_delay: float = 5.0, max_delay: float = 60.0, multiplier: float = 2.0):
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._multiplier = multiplier
        self._current_delay = base_delay
        self._consecutive_failures = 0

    def record_success(self):
        """Reset backoff on success."""
        self._consecutive_failures = 0
        self._current_delay = self._base_delay

    def record_failure(self):
        """Increase backoff on failure."""
        self._consecutive_failures += 1
        self._current_delay = min(
            self._base_delay * (self._multiplier ** self._consecutive_failures),
            self._max_delay
        )

    @property
    def delay(self) -> float:
        return self._current_delay

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures


# ── VPN Manager ────────────────────────────────────────────────

class RotationFailed(RuntimeError):
    """IP rotation failed or was gated (circuit breaker open, fail-fast
    cooldown after a recent failed rotation, or all attempts failed).

    Raised by ``connect_next`` so callers can distinguish a genuine
    rotation failure from "no rotation needed". Never treat a caught
    RotationFailed as a silent success (CRITIC(5)).
    """


def _sh_quote(value: str) -> str:
    """Quote a string for POSIX sh embedded in a ``docker exec ... sh -c``
    command. gluetun's wget needs the control key from the CONTAINER'S OWN
    env ($VPN_CONTROL_API_KEY) — wget doesn't expand env vars in --header
    values and docker exec doesn't propagate host env — so the JSON body is
    interpolated by sh inside the container. Single quotes are escaped
    defensively; the JSON is always compact ASCII."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


# [incident 17/08] gluetun's country enum uses SPACED canonical names
# ("United Kingdom", "Czech Republic"). NordVPN file-style names
# ("United_Kingdom", "Czechia") are REJECTED by gluetun with a WARN
# "values ... are not in choices" — a pin landing on one silently fails
# the rotation. Map the known divergent spellings to gluetun's canonical
# name so a pin can never emit an invalid country (the underscore→space +
# title() normalization handles the rest).
_COUNTRY_ALIASES = {
    "Czechia": "Czech Republic",
    "Lao Peoples Democratic Republic": "Lao People's Democratic Republic",
}


def _normalize_country(name: str) -> str:
    """Canonical gluetun country name: strip, underscore→space, title-case,
    then apply the alias map for spellings title() alone cannot fix."""
    c = name.strip().replace("_", " ").strip().title()
    return _COUNTRY_ALIASES.get(c, c)


# [plan 18/08] NordVPN OpenVPN hostnames: 2-letter country code + 2-4 digit
# server number (uk2757, it460, hu73, de1372). {2,4} NOT {3,4}: 2-digit hosts
# (hu73/gr80/ee77) would never match and the blacklist would stay silent
# (Phase 1b/1c dead in silence). OpenVPN verbosity >= 4 (OPENVPN_VERBOSITY=4
# in compose) logs "Connecting to [<host>]" once per connection attempt.
_NORDVPN_HOST_RE = re.compile(r"[a-z]{2}[0-9]{2,4}\.nordvpn\.com")

# Blacklist TTL: the OpenVPN sunset kills hosts within a daily window — one
# wall-clock day covers it, and a host that starts working again simply
# expires back into rotation.
_FAILED_HOST_TTL = 24 * 3600


def _extract_current_hostname(text: str) -> Optional[str]:
    """Last NordVPN hostname present in gluetun log text.

    gluetun's SIGUSR1 retry loop re-logs the SAME remote every ~11 s, so
    the last "Connecting to [...]" line is the host currently in play.
    None when no hostname is logged (OpenVPN verbosity < 4).
    """
    matches = _NORDVPN_HOST_RE.findall(text)
    return matches[-1] if matches else None


class VPNManager:
    """Manages the compose-managed gluetun VPN container.

    Container lifecycle is owned by docker-compose: this class only
    inspects/restarts the container and reads its logs. The tunnel
    survives proxy restarts.

    Config keys (ip_rotation section of config.yaml):
        enabled, proxy_mode, quota_per_ip, switch_delay,
        docker_container, docker_compose_file, vpn_proxy_port, socks5_proxy_port,
        credentials_file, server_countries, server_provider, circuit_breaker_threshold,
        circuit_breaker_recovery, backoff_max_delay, watchdog_interval, ip_check_url,
        ip_check_urls, control_api_key, country_rotation, country_offset,
        wait_healthy_poll, status_cache_seconds, rotation_fail_cooldown,
        identity_rotation, identity_profiles
    """

    def __init__(self, cfg: dict, station: int = 1, shared=None):
        """Build a VPN station manager.

        ``station`` selects per-station config keys (suffix ``_N`` for
        station N: ``docker_container_N``, ``vpn_proxy_port_N``,
        ``socks5_proxy_port_N``, ``compose_service_N``, ``state_file_N``) —
        the N-station setup runs N gluetun containers in parallel and each
        station has its own ports/container/state file. Station 1 keeps the
        legacy key names, so ``VPNManager(IP_ROTATION)`` is unchanged.

        ``shared`` is the cross-station SharedRotationState (IP registry +
        global identity cursor). When absent (or None) every cross-station
        concern degrades to per-station local behavior — exact backward
        compatibility.
        """
        self._shared: Optional[object] = shared
        self._config = cfg
        self._station = station
        self._mode = "docker"  # sole mode: compose-managed gluetun (free_ip_pool compat)
        self._enabled = cfg.get("enabled", False)
        self._proxy_mode = cfg.get("proxy_mode", "vpn")  # vpn | direct
        self._quota_per_ip = cfg.get("quota_per_ip", 300)
        self._switch_delay = cfg.get("switch_delay", 5)
        # [plan 18/08 §1] N-station suffix: station 1 keeps the legacy key
        # names, stations 2+ read ``_N``-suffixed keys with derived defaults
        # (opencode-vpn-{N} / vpn-gluetun-{N} / socks5 1079+N / http 8887+N).
        # Stations 1 and 2 behave exactly as before.
        suffix = "" if station <= 1 else f"_{station}"
        self._docker_container = cfg.get(
            f"docker_container{suffix}",
            f"opencode-vpn-{station}" if station > 1 else "opencode-vpn")
        self._docker_compose_file = cfg.get("docker_compose_file", "docker-compose.yml")
        # The compose SERVICE name (not container name) used in `docker
        # compose -f … up -d <service>` / `pull <service>` invocations.
        self._compose_service = cfg.get(
            f"compose_service{suffix}",
            f"vpn-gluetun-{station}" if station > 1 else "vpn-gluetun")
        # Per-station state file (station N must not clobber another
        # station's IP history/circuit breaker, and vice versa).
        self._state_file = cfg.get(
            f"state_file{suffix}",
            os.path.join(ROOT, "logs", f"vpn_state{station}.json" if station > 1 else "vpn_state.json"))
        self._proxy_port = cfg.get(
            f"vpn_proxy_port{suffix}", 8887 + station if station > 1 else 8888)
        self._socks5_port = cfg.get(
            f"socks5_proxy_port{suffix}", 1079 + station if station > 1 else 1080)
        self._auth_file = cfg.get(
            "credentials_file", os.path.join(ROOT, "vpn_configs", "credentials.txt"))
        self._server_countries = cfg.get("server_countries", "Germany")
        self._server_provider = cfg.get("server_provider", "nordvpn")  # gluetun update -providers
        self._ip_check_url = cfg.get("ip_check_url", "https://api.ipify.org")
        # IP-check fallback chain ([plan] C): ordered endpoints probed through
        # the SAME SOCKS5 tunnel with a sticky index (the endpoint that works
        # is kept until its first failure). Legacy direct mutations of
        # _ip_check_url (tests) stay honored — _probe_urls() falls back to
        # [self._ip_check_url, defaults] when ip_check_urls is absent.
        _urls = cfg.get("ip_check_urls")
        self._ip_check_urls = ([str(u).strip() for u in _urls if str(u).strip()]
                               if isinstance(_urls, list) else [])
        self._ip_check_idx = 0
        # gluetun control server (docker exec; the key lives in the CONTAINER
        # env as VPN_CONTROL_API_KEY — never in process args, never logged).
        # Country rotation pins the country via PUT /v1/vpn/settings; both
        # default OFF so existing configs/tests keep the legacy random-pool
        # path ([plan] A).
        self._control_api_key = str(cfg.get("control_api_key") or "").strip()
        # Host-side enable gate [incident 17/08]: config.yaml ships
        # control_api_key: '' — the real key lives in credentials.env,
        # expanded INSIDE the container only. `control_enabled` tells the
        # HOST the control server is deployed; the gate is a BOOL, never
        # the secret. Defaults to the key being set (backward-compatible).
        self._control_enabled = bool(cfg.get("control_enabled", bool(self._control_api_key)))
        self._country_rotation = bool(cfg.get("country_rotation", False))
        self._country_offset = max(0, int(cfg.get("country_offset", 0) or 0))
        self._current_country: Optional[str] = None  # pinned country (control server)
        self._country_pinned_at: Optional[float] = None
        self._country_index = 0  # local cursor fallback (no shared state)
        # [plan 18/08] Hostname blacklist (LIVE AUTH_FAILED, TTL 24 h) —
        # consumed ONLY by the fast-pin path (Phase 1c); free_ip_pool never
        # sees it. Wall-clock epoch timestamps so the TTL survives restarts.
        self._failed_hosts: dict[str, dict] = {}
        # [plan 18/08 §2d/3c] VPN stack: "auto" | "wireguard" | "openvpn".
        # auto starts on WireGuard when the NordLynx key is present (the
        # most reliable stack — OpenVPN AUTH_FAILED cannot exist on WG) and
        # falls back to OpenVPN when the WG tunnel loses egress; returns to
        # the preferred WG stack after the prudent thresholds (Phase 3).
        self._stack = str(cfg.get("vpn_stack", "auto")).strip().lower()
        if self._stack not in ("auto", "wireguard", "openvpn"):
            self._stack = "auto"
        # Key file lives NEXT TO the compose file (vpn_configs/ is gitignored
        # as a whole; the key never enters config.yaml, .env or state).
        self._wg_key_file = os.path.join(
            os.path.dirname(self._compose_file_path()), "vpn_configs", "wireguard.env")
        if self._stack == "wireguard":
            self._stack_effective = "wireguard"
        elif self._stack == "openvpn":
            self._stack_effective = "openvpn"
        else:  # auto
            self._stack_effective = "wireguard" if os.path.exists(self._wg_key_file) else "openvpn"
        # [plan 18/08 §1d/§E2] Shared dead-tunnel counter (WG AND OpenVPN):
        # the light egress probe below is the single authority for egress_dead
        # on both stacks — the old "stack not WG: counter is meaningless" reset
        # is gone, so detection without traffic works in OV too.
        self._egress_failures = 0
        # [plan 18/08 §C] a rotation that died probing a REAL public IP
        # means the tunnel itself is dead — armed once the rotation gives
        # up so the egress watchdog repairs on the next tick (~1 s wake).
        # Never set by "IP unchanged"/"recently used" (the tunnel answers
        # there — the rotation just lost the lottery).
        self._rotation_probe_dead = False
        self._auto_wg_egress_ticks = max(1, int(cfg.get("auto_wg_egress_ticks", 3)))
        # Armed cadence: while failures are pending the watchdog probes every
        # _egress_failure_tick_interval seconds (light probe only — the docker
        # CLI refresh would blow past the cadence). ip_probe_budget bounds the
        # probe's SOCKS5 CONNECT (an httpx timeout does not, am.10).
        self._egress_failure_tick_interval = max(
            0.5, float(cfg.get("egress_failure_tick_interval", 2.0)))
        self._ip_probe_budget = max(1.0, float(cfg.get("ip_probe_budget", 8.0)))
        # [plan 18/08 §B] Control-pin budget: how long a rotation pin may
        # poll "running" (timeout) + how long it then waits for a REAL IP
        # through the tunnel (catch-up, a "running but unreachable" guard —
        # the 445 s stall class). Defaults keep the legacy 60 s no-catchup
        # behavior; config.yaml ships 20/25 (45 s wall, am.12). The
        # fast-recover path threads its own tighter 15/15 explicitly (am.3)
        # — these attrs only feed normal rotation pins.
        self._control_pin_timeout = max(5.0, float(cfg.get("control_pin_timeout", 60.0)))
        self._control_pin_catchup = max(0.0, float(cfg.get("control_pin_catchup", 0.0)))
        # [plan 18/08 §A/am.14] Per-round recovery bound in _finalize_ip:
        # the 445 s incident stall accumulated over 3 rounds ×
        # _wait_healthy(120) + docker legs. 45 s per round caps the worst
        # finalize path at ~150-180 s without starving a slow-but-real
        # reconnect (8-15 s). Hardcoded on purpose — rotation_max_duration
        # (global wall) is the single tunable rotation knob.
        self._rotation_recovery_timeout = 45.0
        # [plan 18/08 §D] Global wall for a whole rotation (config
        # rotation_max_duration, default 240 s): _connect_next_impl burns
        # at most this before giving up — the dead-tunnel incident ran
        # 10-13 min (unbounded 3 × pin/restart + probes). Per-attempt
        # floor of 1 s keeps a near-deadline essay from being starved
        # mid-primitive. am.8 accounting: a timeout must not race a live
        # docker thread (asyncio.to_thread is not cancellable) — the funnel
        # in _docker_run counts in-flight ops and _await_rotation_ops_drained
        # waits for their REAL end before the next attempt.
        self._rotation_max_duration = max(5.0, float(cfg.get("rotation_max_duration", 240.0)))
        self._rotation_op_count = 0
        # [review 18/08] funnel generation: bumped by the rotation teardown
        # so a stale worker thread finishing late skips its decrement instead
        # of corrupting the NEXT rotation's count (see _docker_run).
        self._rotation_op_generation = 0
        self._rotation_op_event: Optional[asyncio.Event] = None
        self._rotation_loop: Optional[asyncio.AbstractEventLoop] = None
        # Pool-signal observability (am.23): the last real connection-
        # failure report (time + count) and the skipped-tick trace — per
        # ROTATION TASK, not a global flag (a flag that resets on any normal
        # tick loses the "lost pool wake on rotation X" trace; a per-task
        # pointer re-logs for each distinct rotation).
        self._skipped_rotation_task: Optional[asyncio.Task] = None
        self._last_conn_failure_at: Optional[float] = None
        self._conn_failure_signal_count = 0
        # [plan 18/08 §3c] Auto-mode reliability counters. Clock is
        # INJECTABLE (_now_fn) so tests can advance time without patching
        # time.monotonic globally (process-wide side effect); production uses
        # time.monotonic.
        self._now_fn = time.monotonic
        self._auth_failed_window: list[float] = []  # monotonic ts, 30-min sliding
        self._last_auto_flip_at: Optional[float] = None  # cooldown (monotonic)
        self._stack_since: Optional[float] = None  # when the effective stack took over
        self._auto_ov_fail_threshold = max(1, int(cfg.get("auto_ov_fail_threshold", 3)))
        self._auto_ov_return_min = max(1, int(cfg.get("auto_ov_return_min", 60)))
        self._auto_flip_cooldown_min = max(1, int(cfg.get("auto_flip_cooldown_min", 30)))
        self._flips: list[dict] = []  # journal [{time, from, to, reason}], cap 20
        self._pending_flip: Optional[tuple] = None  # set inside the tick lock,
        # applied AFTER it (_apply_stack takes the lock — not reentrant).

        # Identity rotation (client fingerprint, advanced on IP rotation).
        # identity_diversity (default False, backward-compatible) expands the
        # explicit profiles into a large deterministic pool (targets x UA-map x
        # header variants), capped at identity_max_profiles — the "one profile
        # per IP" requirement. The base (explicit) profiles are kept separate
        # so a config hot-reload can rebuild from the seed, not from the pool.
        self._identity_rotation_enabled = cfg.get("identity_rotation", True)
        self._identity_diversity = bool(cfg.get("identity_diversity", False))
        self._identity_max_profiles = max(1, int(cfg.get("identity_max_profiles", 256)))
        self._identity_profiles_base = _normalize_identity_profiles(cfg.get("identity_profiles"))
        self._identity_profiles = _build_identity_pool(
            self._identity_profiles_base, self._identity_diversity, self._identity_max_profiles)
        self._identity_index = 0  # restored/clamped by load_state()
        self._rotation_task: Optional[asyncio.Task] = None  # single-flight rotation ([1]+[18])

        # Auto-update (gluetun image)
        self._update_enabled = cfg.get("update_enabled", True)
        self._update_check_interval = cfg.get("update_check_interval", 21600)
        # Honor the configured value (config.yaml ships 7 for fast AUTH_FAILED
        # detection); the floor only catches pathological values, it must not
        # defeat the shipped cadence.
        self._watchdog_interval = max(1, int(cfg.get("watchdog_interval", 60)))
        # Poll cadence for _wait_healthy / control-server "running" wait
        # (config ships 0.5 s — the legacy hard-coded sleep(2) is gone).
        self._wait_healthy_poll = max(0.1, float(cfg.get("wait_healthy_poll", 2.0)))
        self._update_apply_window = cfg.get("update_apply_window", "03:00-05:00")
        self._update_idle_minutes = cfg.get("update_apply_idle_minutes", 15)
        self._update_max_defer_hours = cfg.get("update_apply_max_defer_hours", 24)
        self._update_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        # Docker-events wake-up for the watchdog ([plan] C): created in
        # start() — inside the running loop — so a container die/restart is
        # acted on immediately instead of at the next interval.
        self._watchdog_event: Optional[asyncio.Event] = None
        self._startup_connect_task: Optional[asyncio.Task] = None
        self._update_available = False
        self._update_current_digest: Optional[str] = None
        self._update_new_digest: Optional[str] = None
        self._update_old_image_id: Optional[str] = None
        self._update_known_since: Optional[float] = None
        self._update_checked_at: Optional[str] = None
        self._update_applied_at: Optional[str] = None
        self._update_last_error: Optional[str] = None
        self._last_free_request_at: Optional[float] = None

        # Runtime state
        self._status = VPNState.DISCONNECTED
        self._error: Optional[str] = None
        self._current_ip: Optional[str] = None
        self._current_server: Optional[dict] = None
        self._connected_at: Optional[float] = None
        self._auth_failed = False
        self._server_issue = False  # TLS negotiation failure (stale/crashed server)
        self._total_switches = 0
        self._ip_history: list[dict] = []
        self._lock = asyncio.Lock()
        # Fail-fast rotation cooldown (CRITIC(6)): after a total rotation
        # failure, refuse new rotations for rotation_fail_cooldown
        # (default 300 s, config ships 60) — covers every rotation path
        # (ensure_connected, switch_ip, on_quota_exhausted, manual) instead
        # of the old per-caller timer in free_ip_pool only.
        self._ROTATION_FAIL_COOLDOWN = max(5.0, float(cfg.get("rotation_fail_cooldown", 300)))
        self._last_rotation_failed_at: Optional[float] = None
        # Last rotation failure detail (G7): exposed via get_status — a dead
        # rotation must be visible in the dashboard/debug.log, not silent.
        self._last_rotation_error: Optional[str] = None
        # [37] Dashboard cache: /api/vpn-status polls every 10 s and each
        # full refresh costs 2 docker subprocess calls + a tunnel HTTP probe.
        # Within _STATUS_CACHE_SECONDS of a completed refresh, refresh_status
        # returns the live in-memory state instead of re-probing docker.
        self._STATUS_CACHE_SECONDS = max(0.5, float(cfg.get("status_cache_seconds", 5.0)))
        self._last_status_refresh_at: Optional[float] = None
        # Free streams currently open (updated by opencode.py via
        # note_free_stream_start/end) — auto-update must not interrupt them ([21]).
        self._active_free_streams = 0

        # Reliability
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=cfg.get("circuit_breaker_threshold", 3),
            recovery_time=cfg.get("circuit_breaker_recovery", 300),
        )
        self._backoff = BackoffTimer(
            base_delay=cfg.get("switch_delay", 5),
            max_delay=cfg.get("backoff_max_delay", 60),
        )
        # Auth watchdog backoff (AUTH_FAILED/TLS auto-restart): failed restarts
        # space out base→…→max so the daemon is not hammered while a failure
        # persists. Config keys watchdog_backoff_base/max (defaults 30/300 —
        # legacy behavior); config.yaml ships 15/60 so recovery probes come
        # back every ~15 s instead of every 60 s.
        _wbase = max(1.0, float(cfg.get("watchdog_backoff_base", 30.0)))
        _wmax = max(_wbase, float(cfg.get("watchdog_backoff_max", 300.0)))
        self._watchdog_backoff = BackoffTimer(base_delay=_wbase, max_delay=_wmax)
        self._watchdog_escalated_at: Optional[float] = None  # escalation re-arm: 30 min

        self.load_state()
        if self._shared is not None:
            # Make the shared identity cursor aware of this station's live
            # index at boot so next_identity immediately avoids a collision.
            self._shared.register_station(self._station, self._identity_index)

    # ── Public properties ──────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = bool(value)
        # Watchdog self-heal: start() is only called at boot when enabled,
        # and /api/vpn/toggle only flips this flag — spawn the watchdog loop
        # here so enabling mid-session activates it too. All call sites run
        # inside the event loop; tolerate otherwise (start() creates it).
        if self._enabled and (not self._watchdog_task or self._watchdog_task.done()):
            try:
                self._watchdog_task = asyncio.create_task(self._watchdog_loop())
            except RuntimeError:
                pass  # constructed outside a running loop — start() creates the task

    @property
    def proxy_mode(self) -> str:
        return self._proxy_mode

    @proxy_mode.setter
    def proxy_mode(self, value: str):
        if value in ("vpn", "direct"):
            self._proxy_mode = value

    @property
    def status(self) -> str:
        return self._status

    @property
    def current_ip(self) -> Optional[str]:
        return self._current_ip

    @property
    def identity_index(self) -> int:
        return self._identity_index

    @property
    def current_identity(self) -> dict:
        """Active identity profile (dict with impersonate/user_agent/extra_headers).

        Returns profile[0] whenever rotation is disabled, proxy_mode is not
        "vpn", the VPN is not connected, or only one profile is configured.
        """
        if (not self._identity_rotation_enabled or self._proxy_mode != "vpn"
                or not self._enabled or self._status != VPNState.CONNECTED
                or len(self._identity_profiles) <= 1):
            return self._identity_profiles[0]
        return self._identity_profiles[self._identity_index % len(self._identity_profiles)]

    @property
    def _live_identity(self) -> dict:
        """Identity the proxy will serve once connected.

        Index-based and NOT gated on ``_status == CONNECTED`` on purpose —
        during a transition (connect/rotation) the index has already
        advanced but `current_identity` would still return profile[0],
        so history/journal entries must read THIS to carry the NEW face.
        """
        if not self._identity_rotation_enabled or self._proxy_mode != "vpn":
            return self._identity_profiles[0]
        return self._identity_profiles[self._identity_index % len(self._identity_profiles)]

    @property
    def current_server(self) -> Optional[dict]:
        return self._current_server

    @property
    def proxy_url(self) -> str:
        """HTTP proxy URL (gluetun HTTP proxy on 8888).

        Env override resolution: ``VPN_PROXY_URL_{station}`` first
        (per-station, e.g. the compose-internal URL of station 2), then
        ``VPN_PROXY_URL`` for station 1 only (legacy [13]: inside the
        compose deployment the loopback ports are unreachable from the
        proxy container — docker-compose.yml points it at the internal
        network instead), then the localhost port.
        """
        env_key = f"VPN_PROXY_URL_{self._station}" if self._station > 1 else "VPN_PROXY_URL"
        value = os.environ.get(env_key)
        if not value and self._station == 1:
            value = os.environ.get("VPN_PROXY_URL")
        return value or f"http://127.0.0.1:{self._proxy_port}"

    @property
    def socks5_url(self) -> str:
        """SOCKS5 proxy URL (gluetun SOCKS5 on 1080 — the reliable path on
        Windows Docker Desktop, where the HTTP proxy is not routed).

        Env override resolution: ``VPN_SOCKS5_URL_{station}`` first
        (per-station), then ``VPN_SOCKS5_URL`` for station 1 only (legacy
        [13]), then the localhost port.
        """
        env_key = f"VPN_SOCKS5_URL_{self._station}" if self._station > 1 else "VPN_SOCKS5_URL"
        value = os.environ.get(env_key)
        if not value and self._station == 1:
            value = os.environ.get("VPN_SOCKS5_URL")
        return value or f"socks5://127.0.0.1:{self._socks5_port}"

    def _compose_file_path(self) -> str:
        """Compose file path for `docker compose` invocations.

        VPN_DOCKER_COMPOSE_FILE overrides the ROOT-relative default: inside
        the containerized deployment, compose runs against the HOST daemon,
        so its relative paths (./logs, .env, credentials.env...) resolve on
        the host — a /app path would resolve against a nonexistent host /app.
        docker-compose.yml then points at the host project dir, which is
        mounted read-only at the same absolute path ([13]).
        """
        return os.environ.get("VPN_DOCKER_COMPOSE_FILE") or os.path.join(ROOT, self._docker_compose_file)

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        """Startup: load persisted state, reconcile with container reality,
        and bring the tunnel up automatically when it is expected to be up.

        ``refresh_status()`` only inspects the container — on a fresh boot or
        a Docker daemon restart the container is absent and the status stays
        disconnected. Nothing else issues ``docker compose up -d`` at boot
        (the old lazy path waited for the first free request). So, when the
        VPN is enabled, run ``connect()`` (compose up + wait healthy + IP
        probe) unless the container is already connected. Fail-soft: if
        docker is down or the compose file is missing, log a warning and
        continue — the server still starts, and recovery happens via the
        watchdog / lazy ``ensure_connected`` on the first free request, or
        the manual dashboard button. A single startup failure neither opens
        the circuit breaker (threshold 3) nor arms ``connect_next``'s
        fail-fast cooldown (``_last_rotation_failed_at`` is only set by
        ``connect_next``), so a later retry is not gated.
        """
        self.load_state()
        if self._watchdog_event is None:
            self._watchdog_event = asyncio.Event()
        await self.refresh_status()
        if (self._enabled and self._proxy_mode == "vpn"
                and self._status != VPNState.CONNECTED):
            # Startup must NOT block the HTTP server: compose up +
            # wait_healthy (≤120s) + up to 3 _finalize_ip rotation rounds can
            # take minutes. Connect in a background task — the watchdog and
            # the lazy ensure_connected on the first free request overlap it
            # safely (connect is single-flight under self._lock).
            self._startup_connect_task = asyncio.create_task(self._startup_connect())
        if not self._watchdog_task or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        if self._update_enabled and not self._update_task:
            self._update_task = asyncio.create_task(self._updater_loop())

    async def _startup_connect(self) -> None:
        """Background boot connect (see start()). Failure is recorded by
        connect() itself (ERROR status, circuit/backoff) — the server stays
        up and the watchdog / lazy ensure_connected retries."""
        try:
            await self.connect()
        except Exception as e:
            logger.warning(
                "[vpn] startup connect failed (docker compose up) — "
                "retried on first free request / manual connect: %s", e)

    async def stop(self) -> None:
        """Shutdown: persist state only. The tunnel is left running
        (it is compose-managed and survives proxy restarts)."""
        if self._startup_connect_task:
            self._startup_connect_task.cancel()
            self._startup_connect_task = None
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        if self._update_task:
            self._update_task.cancel()
            self._update_task = None
        self.save_state()

    async def stop_container(self) -> None:
        """Downscale path only: stop the service, then DELETE the container.

        [fix 19/08] compose stop ALONE left a retired station sitting in
        ``Exited`` state — read by the operator as "les stations désactivées
        ne sont pas supprimées". The container is now removed via
        ``docker rm -f`` (idempotent: stale containers that already died —
        or were created but never started — are gone too). ``rm`` without
        ``-v`` KEEPS the named gluetun volume, so an upscale recreates the
        container from the same volume and the persisted config survives.
        ``stop()`` never calls this: proxy shutdown leaves the tunnels
        running (compose-managed)."""
        # 1) graceful gluetun shutdown — best-effort: the container may
        #    already be dead, or live under a different compose project.
        compose_file = self._compose_file_path()
        result = await asyncio.to_thread(
            self._docker_run, ["compose", "-f", compose_file, "stop",
                               self._compose_service], 120)
        if result.returncode != 0:
            logger.warning("[vpn] compose stop failed (continuing to rm): %s",
                           result.stderr.strip() or result.stdout.strip())
        # 2) actually delete the container (keep the volume). "No such
        #    container" is success — already removed by an earlier pass.
        rm = await asyncio.to_thread(
            self._docker_run, ["rm", "-f", self._docker_container], 120)
        if rm.returncode != 0 and "No such container" not in (rm.stderr or ""):
            raise RuntimeError(
                f"docker rm failed: {rm.stderr.strip() or rm.stdout.strip()}")

    async def connect(self) -> None:
        """Bring the tunnel up: compose up + wait healthy + record IP."""
        async with self._lock:
            if self._status == VPNState.CONNECTED and self._current_ip:
                return
            if not VPNState.can_transition(self._status, VPNState.CONNECTING):
                raise RuntimeError(f"Cannot transition from {self._status} to connecting")
            self._set_status(VPNState.CONNECTING)
            self._error = None
            try:
                await self._compose_up()
                started_at = await self._wait_healthy(timeout=120)
                if started_at is None:
                    raise RuntimeError("gluetun not healthy within 120s")
                if await self._check_auth_failed(started_at):
                    self._auth_failed = True
                    self._current_ip = None  # stale IP must not be served ([5])
                    raise RuntimeError("AUTH_FAILED - NordVPN service credentials rejected")
                self._auth_failed = False
                # Boot with country rotation: pin the next country via the
                # control server — the SAME machine as rotation ([plan] A).
                # On failure the tunnel is already healthy and _finalize_ip
                # validates whatever country gluetun picked; the first
                # rotation re-pins.
                await self._pin_country_for_rotation()
                # Validate + commit a fresh IP (never recent on either
                # station): boot with containers still up forces a rotation
                # here, so the very first IP is recorded with a NEW identity
                # ([plan] C.1 — the old code never journalized the boot IP).
                # allow_stale=True arms the last-attempt acceptance inside
                # _finalize_ip: after two full container recreations a still-
                # recent IP is accepted rather than failing boot outright —
                # the safety valve that stops a constrained boot from
                # looping forever (the watchdog/update paths instead escalate
                # or roll back on False, so they must stay strict).
                if not await self._finalize_ip(allow_stale=True):
                    raise RuntimeError("could not finalize a fresh IP through tunnel")
                self._connected_at = time.monotonic()
                self._set_status(VPNState.CONNECTED)
                self._current_server = {
                    "name": self._docker_container,
                    "country": self._current_country or self._server_countries}
                self._circuit_breaker.record_success(self._docker_container)
                self._backoff.record_success()
                self._last_rotation_failed_at = None  # tunnel is up: clear cooldown
                self._last_rotation_error = None
                self.save_state()
                logger.info("[vpn] connected — IP %s", self._current_ip)
            except asyncio.CancelledError:
                # Client disconnect mid-connect must not leave the state
                # stuck in CONNECTING ([17]).
                self._set_status(VPNState.ERROR)
                self._error = "connect cancelled"
                raise
            except Exception as e:
                self._set_status(VPNState.ERROR)
                self._error = str(e)
                self._circuit_breaker.record_failure(self._docker_container)
                self._backoff.record_failure()
                logger.error("[vpn] connect failed: %s", e)
                raise

    async def connect_next(self) -> str:
        """Rotate to a fresh IP — single-flight.

        Concurrent callers (429 bursts, quota-exhausted, manual
        /api/vpn/next) share one rotation: the first caller starts it, the
        others await the same task. One rotation at a time — no more
        cascades of docker restarts (previously up to N concurrent callers
        × 3 attempts each). The stored task reference also prevents
        mid-flight GC.

        Raises:
            RotationFailed: rotation refused (fail-fast cooldown after a
                recent failed rotation (CRITIC(6)) or circuit breaker open
                ([25])) or all attempts failed (CRITIC(5)). Callers must
                treat this as a real failure, never as a silent success.

        Returns:
            The new IP on success.
        """
        if self._rotation_task and not self._rotation_task.done():
            return await asyncio.shield(self._rotation_task)
        # Fail-fast cooldown: after a total rotation failure, refuse new
        # rotations for 300 s. Covers every rotation path (ensure_connected,
        # switch_ip, on_quota_exhausted, manual) instead of the old
        # per-caller timer in free_ip_pool only (CRITIC(6)).
        if self._last_rotation_failed_at is not None:
            since = time.monotonic() - self._last_rotation_failed_at
            if since < self._ROTATION_FAIL_COOLDOWN:
                raise RotationFailed(
                    f"rotation cooldown active ({self._ROTATION_FAIL_COOLDOWN - int(since)}s left)")
        # Circuit breaker gate ([25]): no rotation while the breaker is open.
        if not self._circuit_breaker.is_available(self._docker_container):
            raise RotationFailed("circuit breaker open — skipping rotation")
        task = asyncio.create_task(self._connect_next_impl())
        self._rotation_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._rotation_task is task:
                self._rotation_task = None

    async def _connect_next_impl(self) -> str:
        """Actual rotation: compose up if the container is absent,
        else restart it. Validates the new IP (different from current and
        not in the last 10) with 3 attempts + backoff.

        Returns the new IP on success; raises RotationFailed otherwise
        (CRITIC(5)).
        """
        async with self._lock:
            if not self._enabled:
                raise RotationFailed("VPN disabled — cannot rotate")
            if not VPNState.can_transition(self._status, VPNState.CONNECTING):
                raise RuntimeError(f"Cannot transition from {self._status} to connecting")

            old_ip = self._current_ip
            last_error: Optional[Exception] = None
            # [plan 18/08 §C] per-rotation reset — the flag lives only for
            # this rotation's lifetime.
            self._rotation_probe_dead = False
            # [plan 18/08 §D] global wall: the whole rotation (all 3
            # attempts) burns at most rotation_max_duration before giving
            # up — the dead-tunnel incident ran 10-13 min with no bound.
            # Per-attempt floor of 1 s (max(1.0, remaining)) keeps a
            # near-deadline essay from being starved mid-primitive.
            deadline = self._now_fn() + self._rotation_max_duration
            # am.8 rotation-scoped op accounting: _docker_run's funnel
            # counts in-flight docker threads against this event so a
            # deadline can wait for their REAL end (asyncio.to_thread is
            # not cancellable) before the next attempt — a second
            # pin/compose must never race the first. Torn down in the
            # finally below (success, exhaustion, or client cancel).
            self._rotation_loop = asyncio.get_running_loop()
            self._rotation_op_event = asyncio.Event()
            self._rotation_op_count = 0

            async def _attempt() -> str:
                # One rotation attempt: pin country via the control server
                # (a real reconnect when it works), else the legacy
                # container-restart branch; then probe a REAL IP through the
                # tunnel. Returns the new IP on success; raises on failure
                # (dead probe / unchanged / recent). Extracted so each
                # attempt can run under asyncio.wait_for(deadline).
                # Country rotation first: the control-server pin IS the
                # reconnect (PUT settings -> stop+start) and the cursor
                # always advances, so consecutive pins never repeat a
                # country. When the pin is unavailable or fails, fall
                # through to the legacy container-restart branch.
                pinned = await self._pin_country_for_rotation()
                if pinned is None:
                    await self._ensure_container()
                    started_at = await self._wait_healthy(timeout=120)
                    if started_at is None:
                        raise RuntimeError("gluetun not healthy after restart")
                    if await self._check_auth_failed(started_at):
                        self._auth_failed = True
                        self._current_ip = None  # stale IP must not be served ([5])
                        raise RuntimeError("AUTH_FAILED after restart")
                    self._auth_failed = False
                else:
                    # The pin polled status:running inside the control
                    # server — the tunnel is up; no container action.
                    self._auth_failed = False

                # Let the new tunnel stabilize before probing the IP
                await asyncio.sleep(self._switch_delay)

                new_ip = await self.get_public_ip()
                if not new_ip:
                    raise RuntimeError("could not determine public IP")
                # [review 18/08 §C] the probe ANSWERED — the tunnel
                # lives. Clear the latch: a later "IP unchanged"/
                # "recently used" failure is the lottery, not death,
                # and must not arm behind an earlier dead probe (the
                # final WARN would overstate — attempt 1 did answer).
                self._rotation_probe_dead = False
                if old_ip and new_ip == old_ip:
                    raise RuntimeError(f"IP unchanged after restart ({new_ip})")
                if self._ip_recent(new_ip) and attempt < 2:
                    raise RuntimeError(f"IP {new_ip} recently used")

                # Success — advance the identity BEFORE journalizing so
                # the history entry carries the NEW face ([plan] C.2 —
                # the old order logged the pre-advance identity).
                self._current_ip = new_ip
                self._current_server = {
                    "name": self._docker_container,
                    "country": self._current_country or self._server_countries}
                self._connected_at = time.monotonic()
                self._set_status(VPNState.CONNECTED)
                self._total_switches += 1
                self._record_ip_change(new_ip)
                self._advance_identity()
                self._ip_history.append({
                    "ip": new_ip,
                    "server": self._docker_container,
                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "identity": self._live_identity.get("impersonate") or "",
                    "identity_index": self._identity_index,
                })
                self._ip_history = self._ip_history[-100:]
                self._circuit_breaker.record_success(self._docker_container)
                self._backoff.record_success()
                self._last_rotation_failed_at = None  # success: clear cooldown
                self._last_rotation_error = None
                self._rotation_probe_dead = False  # success: tunnel answers
                self.save_state()
                logger.info("[vpn] rotated → IP %s (switch #%d)", new_ip, self._total_switches)
                return new_ip

            deadline_hit = False
            try:
                for attempt in range(3):
                    if self._now_fn() >= deadline:
                        # The wall is gone — a new attempt could only
                        # overshoot it. last_error may still be None when
                        # the wall expired before the first attempt.
                        deadline_hit = True
                        last_error = last_error or RuntimeError("rotation wall exceeded")
                        break
                    remaining = deadline - self._now_fn()
                    self._set_status(VPNState.CONNECTING)
                    self._error = None
                    try:
                        new_ip = await asyncio.wait_for(
                            _attempt(), max(1.0, remaining))
                    except asyncio.CancelledError:
                        # Client disconnect mid-rotation must not leave the
                        # state stuck in CONNECTING ([17]).
                        self._set_status(VPNState.ERROR)
                        self._error = "rotation cancelled by client disconnect"
                        raise
                    except asyncio.TimeoutError:
                        # The attempt burnt its budget. am.8: whatever
                        # docker op it left running is NOT finished
                        # (to_thread cannot be cancelled) — wait for its
                        # real end so the next attempt (or the repair rung)
                        # never races it. The loop-top check then decides:
                        # budget left → next attempt, else bail.
                        deadline_hit = True
                        last_error = RuntimeError("rotation deadline exceeded")
                        self._circuit_breaker.record_failure(self._docker_container)
                        self._backoff.record_failure()
                        logger.warning(
                            "[vpn] rotation attempt %d/3 hit the %.0f s wall — "
                            "draining in-flight docker op", attempt + 1,
                            self._rotation_max_duration)
                        await self._await_rotation_ops_drained()
                    except Exception as e:
                        last_error = e
                        if "could not determine public IP" in str(e):
                            # The rotation died asking for a REAL IP through
                            # the tunnel — the tunnel itself is dead (the
                            # 445 s stall class, incident 18/08 S2). NOT set
                            # on "IP unchanged"/"recently used": those prove
                            # the tunnel answers.
                            self._rotation_probe_dead = True
                        self._circuit_breaker.record_failure(self._docker_container)
                        self._backoff.record_failure()
                        logger.warning("[vpn] rotation attempt %d/3 failed: %s",
                                       attempt + 1, e)
                        if attempt < 2:
                            # Cap the retry pause to the remaining wall so a
                            # late backoff (≤ 60 s) cannot push the rotation
                            # past its deadline. Recompute against the LIVE
                            # wall: the pre-try `remaining` predates this
                            # failed attempt and could overshoot it by up to
                            # the attempt's own duration (review 18/08).
                            remaining = max(0.0, deadline - self._now_fn())
                            await asyncio.sleep(
                                min(self._backoff.delay, max(0.05, remaining)))
                    else:
                        return new_ip
            finally:
                # [plan 18/08 §D] tear down the rotation-scoped op
                # accounting (success, exhaustion, or client cancel). A
                # racing thread's funnel keeps its own loop/event refs and
                # call_soon_threadsafe is safe after this (no-op loop).
                self._rotation_loop = None
                self._rotation_op_event = None
                self._rotation_op_count = 0
                # [review 18/08] bump the funnel generation: any thread that
                # is STILL inside subprocess.run belongs to this rotation and
                # must not decrement into the next rotation's accounting.
                self._rotation_op_generation += 1

            self._set_status(VPNState.ERROR)
            if deadline_hit:
                self._error = (
                    f"IP rotation gave up after {self._rotation_max_duration:.0f}s"
                    f" (wall; last: {last_error})")
            else:
                self._error = f"IP rotation failed after 3 attempts (last: {last_error})"
            logger.error("[vpn] %s", self._error)
            # CRITIC(5): a failed rotation is a failure, not a silent None —
            # raise so callers can react honestly. Also arm the fail-fast
            # cooldown (CRITIC(6)) so we don't hammer a dead tunnel.
            self._last_rotation_failed_at = time.monotonic()
            self._last_rotation_error = f"{type(last_error).__name__}: {last_error}"
            # [plan 18/08 §C] the rotation gave up AND the tunnel never
            # answered the IP probe: hand off to the egress watchdog —
            # arm it + wake it so the repair starts on the next tick
            # (~1 s), not after N idle ticks (the incident's 11 min 45 s
            # stall: the dead rotation then went silent). max() keeps the
            # counter monotone (idempotent — an already-armed counter
            # stays where it is); no manual reset: the tick's light probe
            # is the single authority and clears it on recovery. No
            # skipped-tick trace reset needed — the guard is per-ROTATION
            # task (_skipped_rotation_task, commit 1), and this arm runs
            # inside the dying rotation itself.
            if self._rotation_probe_dead:
                self._egress_failures = max(self._egress_failures,
                                            self._auto_wg_egress_ticks)
                if self._watchdog_event is not None:
                    self._watchdog_event.set()  # loop wait(timeout) returns → live tick
                logger.warning(
                    "[vpn] rotation died on a dead tunnel — egress watchdog "
                    "armed (real IP probe never answered)")
            raise RotationFailed(self._error)

    async def connect_wait(self, timeout: float = 120.0) -> bool:
        """Wait for an externally managed tunnel to come up (no container action).

        Returns True once the public IP through the SOCKS5 tunnel answers.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ip = await self.get_public_ip()
            if ip:
                self._current_ip = ip
                self._connected_at = time.monotonic()
                self._set_status(VPNState.CONNECTED)
                self._current_server = {
                    "name": self._docker_container,
                    "country": self._current_country or self._server_countries}
                return True
            await asyncio.sleep(3)
        self._set_status(VPNState.ERROR)
        self._error = f"Timeout waiting for VPN connection ({int(timeout)}s)"
        logger.error("[vpn] %s", self._error)
        return False

    async def disconnect(self) -> None:
        """Disconnect: update state + persist. Never stops the container —
        the tunnel is owned by docker-compose and stays up."""
        async with self._lock:
            if self._status == VPNState.DISCONNECTED:
                return
            self._set_status(VPNState.DISCONNECTING)
            self._current_ip = None
            self._connected_at = None
            self._set_status(VPNState.DISCONNECTED)
            self.save_state()
            logger.info("[vpn] disconnected (tunnel left running, compose-managed)")

    # ── State transitions + events ─────────────────────────────

    def _set_status(self, status) -> None:
        """Transition to ``status``, publishing a vpn_event on change
        (dashboard SSE). No-op when the status is unchanged — only REAL
        transitions surface, so kill/restart bursts coalesce naturally."""
        if status == self._status:
            return
        self._status = status
        self._publish_vpn_event()

    def _publish_vpn_event(self) -> None:
        """Broadcast the full status snapshot to SSE subscribers. Fail-open:
        dashboard module import or publish errors must never crash a VPN
        state transition."""
        try:
            from dashboard.events import get_event_manager
            get_event_manager().publish("vpn_event", self.get_status())
        except Exception as e:
            logger.debug("[vpn] vpn_event publish skipped: %s", e)

    def _on_container_event(self, event: dict) -> None:
        """Handle a docker event for THIS station's container (loop context).

        Every status change wakes the watchdog so recovery starts
        immediately instead of on the next interval tick. A die/stop/kill
        of a CONNECTED container flips the state to ERROR right away --
        guarded so an in-flight rotation (CONNECTING) keeps its own
        outcome instead of being corrupted by the event.
        """
        try:
            if self._watchdog_event is not None:
                self._watchdog_event.set()  # not awaited -- simply wake the loop
            status = event.get("status")
            if status in ("die", "stop", "kill") and self._status == VPNState.CONNECTED:
                self._set_status(VPNState.ERROR)
                self._error = f"container event {status} -- VPN down"
        except Exception as e:
            logger.debug("[vpn] container event handling failed: %s", e)

    # ── Status & health ────────────────────────────────────────

    async def refresh_status(self, force: bool = False) -> dict:
        """Reconcile state with container reality:
        - container absent          → disconnected
        - container not running     → error
        - AUTH_FAILED in recent logs → error + auth_failed
        - tunnel answering          → connected (IP refreshed)

        Cache ([37]): within _STATUS_CACHE_SECONDS of a completed refresh,
        return the live in-memory state instead of re-probing docker —
        get_status() reads current fields, so only the docker/network probe
        is deferred (≤5 s staleness, fine for the dashboard's 10 s poll).
        Pass force=True from paths that must see reality immediately
        (startup reconcile, image update apply/rollback).
        """
        if not force and self._last_status_refresh_at is not None and \
                time.monotonic() - self._last_status_refresh_at < self._STATUS_CACHE_SECONDS:
            return self.get_status()
        # Stamp before probing: concurrent calls within the window coalesce
        # onto this one instead of firing their own docker subprocesses.
        self._last_status_refresh_at = time.monotonic()
        info = await self._docker_inspect()
        if not info:
            if self._status != VPNState.DISCONNECTED:
                logger.warning("[vpn] container %s not found", self._docker_container)
            self._set_status(VPNState.DISCONNECTED)
            self._current_ip = None
            self._error = None
            return self.get_status()

        if not info.get("running"):
            self._set_status(VPNState.ERROR)
            self._error = "container not running"
            return self.get_status()

        auth_failed = await self._check_auth_failed(info.get("started_at", ""))
        server_issue = await self._check_server_issue(info.get("started_at", ""))
        if auth_failed or server_issue:
            self._auth_failed = auth_failed
            self._server_issue = server_issue
            self._set_status(VPNState.ERROR)
            self._error = ("AUTH_FAILED - NordVPN service credentials rejected" if auth_failed
                           else "VPN server unreachable - TLS negotiation failed (stale server list?)")
            self._current_ip = None  # stale IP must not be served ([5])
            logger.error("[vpn] %s", self._error)
            return self.get_status()
        self._auth_failed = False
        self._server_issue = False

        # Control server first: 2 short docker-exec calls, self-reported
        # truth from gluetun itself — no egress needed, answers even when
        # the tunnel is half-dead. Country comes from OUR pinned state (the
        # control API discloses credentials via GET /v1/vpn/settings — we
        # never call it). SOCKS5 probe remains the fallback.
        if self._control_enabled:
            ctl = await self._control_status()
            if ctl is True:
                ctl_ip = await self._control_public_ip()
                if ctl_ip:
                    self._current_ip = ctl_ip
                    self._connected_at = time.monotonic()
                    self._set_status(VPNState.CONNECTED)
                    self._error = None
                    self._current_server = {
                        "name": self._docker_container,
                        "country": self._current_country or self._server_countries}
                    return self.get_status()
            elif ctl is False:
                # gluetun itself reports the VPN stopped — honest error, no
                # SOCKS5 probe needed.
                self._set_status(VPNState.ERROR)
                self._error = "gluetun control server reports VPN stopped"
                return self.get_status()
            # ctl is None (control server unreachable) → fall through to the
            # SOCKS5 probe below; "can't ask" must not read as "stopped".

        ip = await self.get_public_ip()
        if ip:
            self._current_ip = ip
            self._connected_at = time.monotonic()
            self._set_status(VPNState.CONNECTED)
            self._error = None
            self._current_server = {
                "name": self._docker_container,
                "country": self._current_country or self._server_countries}
        else:
            self._set_status(VPNState.ERROR)
            self._error = "container running but tunnel not answering"
        return self.get_status()

    async def health_check(self) -> dict:
        """Probe the tunnel through SOCKS5 and measure latency."""
        result = {"ok": False, "ip_changed": False, "latency_ms": None, "error": None}
        if self._status != VPNState.CONNECTED:
            result["error"] = "Not connected"
            return result
        try:
            import httpx
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=15, proxy=self.socks5_url) as client:
                # [plan 18/08 §A] same stall class as get_public_ip: an
                # httpx timeout does NOT bound a stuck SOCKS5 CONNECT —
                # wait_for ip_probe_budget; the except below absorbs the
                # TimeoutError into the error result.
                resp = await asyncio.wait_for(
                    client.get(self._ip_check_url), self._ip_probe_budget)
                elapsed_ms = int((time.monotonic() - start) * 1000)
            new_ip = resp.text.strip()
            result["latency_ms"] = elapsed_ms
            if new_ip:
                result["ok"] = True
                # Network probe above stays outside the lock; only the state
                # mutation is guarded ([23]). The ip_changed COMPARISON is
                # made under the same lock: an unlocked read could observe the
                # old IP, then a concurrent rotation (single-flight ... next)
                # advances the identity while we wait for the lock, and we'd
                # advance it a SECOND time for the same IP (finding j).
                async with self._lock:
                    if new_ip != self._current_ip:
                        result["ip_changed"] = True
                        logger.warning("[vpn] health check: IP changed %s → %s",
                                       self._current_ip, new_ip)
                        self._current_ip = new_ip
                        # The tunnel re-picked an IP outside a rotation — keep
                        # the shared registry and identity in sync so the next
                        # rotation never re-enters it and the history shows the
                        # new face ([plan] C.5).
                        self._record_ip_change(new_ip)
                        self._advance_identity()
                        self._ip_history.append({
                            "ip": new_ip,
                            "server": self._docker_container,
                            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "identity": self._live_identity.get("impersonate") or "",
                            "identity_index": self._identity_index,
                        })
                        self._ip_history = self._ip_history[-100:]
                        self.save_state()
            else:
                result["error"] = "Could not determine IP"
        except Exception as e:
            result["error"] = str(e)
            logger.error("[vpn] health check failed: %s", e)
        return result

    async def _probe_url(self, url: str) -> Optional[str]:
        """[plan 18/08 §A] One bounded IP GET through the SOCKS5 tunnel —
        the parallel unit of get_public_ip. Fresh coroutine per endpoint:
        the sweep's wait_for cancels it on budget overrun, __aexit__ closes
        the client on cancellation — no orphan task, no leaked client.
        Returns the IP text (None on any failure)."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5, proxy=self.socks5_url) as client:
                resp = await client.get(url)
                ip = resp.text.strip()
            return ip or None
        except Exception:
            return None

    async def get_public_ip(self) -> Optional[str]:
        """Query the public IP through the SOCKS5 tunnel (127.0.0.1:1080).

        Tries the ordered ``ip_check_urls`` chain (``ip_check_url`` stays
        the legacy alias — entry 1 when no chain is configured), every
        endpoint through the SAME tunnel: resolving outside it would
        falsify rotation validation. [plan 18/08 §A] ALL endpoints are
        probed in PARALLEL under ONE bounded sweep (``ip_probe_budget``)
        — the old sequential chain cost n × per-request timeout stacked on
        repeated callers (the 445 s stall class), with no sweep-level cap.
        The first success in endpoint order from ``_ip_check_idx`` is
        returned and kept sticky; a fully failed OR budget-overrun sweep
        resets the index. Returns None only when every endpoint failed —
        never "unknown" as a success value.
        """
        urls = self._ip_check_urls or [self._ip_check_url]
        base = self._ip_check_idx                       # read BEFORE the sweep
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(self._probe_url(u) for u in urls)),
                self._ip_probe_budget)
        except asyncio.TimeoutError:
            results = []                                # sweep cancelled → total failure
        for i in range(len(urls)):
            idx = (base + i) % len(urls)        # scan in rotated order...
            if i >= len(results):
                break                           # cancelled sweep → the total-failure tail
            ip = results[idx]                   # ...but results[] is URLS order
            if ip:
                # Sticky: remember the working endpoint (i==0 keeps it).
                self._ip_check_idx = idx
                return ip
        self._ip_check_idx = 0  # full sweep failed — restart at the top next call
        logger.error("[vpn] public IP probe failed on all %d endpoints via SOCKS5",
                     len(urls))
        return None

    async def _probe_tunnel_light(self) -> bool:
        """[plan 18/08 §E1/am.10] Light egress probe — SOCKS5 handshake +
        CONNECT to the ip_check endpoint (same chain as get_public_ip). NO
        GET: dead/alive is all we need — _finalize_ip does the full check
        once the tunnel is back. Single authority for egress_dead on BOTH
        stacks: the incident OV tunnel was "connected" with no AUTH_FAILED/
        TLS marker, invisible without a probe. Never called without an
        explicit wait_for budget: an httpx timeout does NOT bound the SOCKS5
        CONNECT (the 445 s stall bug class).
        """
        import httpx
        try:
            urls = self._ip_check_urls or [self._ip_check_url]
            # [review F2] the single-endpoint probe would false-death a
            # healthy tunnel when the ip_check endpoint itself is down.
            # Rotated sweep over ALL endpoints, sticky-first, bounded PER
            # attempt: `min(2.0, budget)` keeps the worst case under budget
            # across the sweep (n endpoints × per_attempt).
            per_attempt = min(2.0, self._ip_probe_budget)
            base = self._ip_check_idx
            for i in range(len(urls)):
                url = urls[(base + i) % len(urls)]
                if await self._probe_connect(url, per_attempt=per_attempt):
                    if i > 0:
                        self._ip_check_idx = (base + i) % len(urls)
                    return True
            return False
        except Exception:
            return False

    async def _probe_connect(self, url: str, *, per_attempt: float) -> bool:
        """[review F2] One bounded SOCKS5 handshake + CONNECT toward ``url``.
        NO GET: dead/alive is all we need — _finalize_ip does the full check
        once the tunnel is back. Never called without an explicit wait_for
        budget: an httpx timeout does NOT bound the SOCKS5 CONNECT (the
        445 s stall bug class).
        """
        import httpx
        client = httpx.AsyncClient(
            proxy=self.socks5_url,
            timeout=httpx.Timeout(per_attempt))
        try:
            resp = await asyncio.wait_for(
                client.send(httpx.Request("CONNECT", url), stream=True),
                per_attempt)
            # Release the unread stream without downloading the body
            # (bounded — a stuck tunnel must not cost the whole budget).
            await asyncio.wait_for(resp.aclose(), 1.0)
            return True
        except Exception:
            return False
        finally:
            await client.aclose()

    # ── IP freshness + identity advance (cross-station) ─────────

    def _ip_recent(self, ip: str) -> bool:
        """True when ``ip`` was used recently by EITHER station.

        Shared registry when present (cross-station anti-reuse); else the
        per-station history tail — exact backward-compatible local behavior.
        """
        if not ip:
            return False
        if self._shared is not None:
            try:
                return self._shared.is_recent(ip)
            except Exception:
                pass
        return ip in [s.get("ip") for s in self._ip_history[-10:]]

    def _record_ip_change(self, ip: str) -> None:
        """Register ``ip`` as now-active for this station (shared registry)."""
        if self._shared is None:
            return
        try:
            self._shared.record_ip(ip, self._station)
        except Exception as e:
            logger.debug("[vpn] shared IP record failed: %s", e)

    def _advance_identity(self) -> int:
        """Move this station's identity index to a NEW profile.

        Cross-station via the shared absolute cursor (guarantees the two
        connected stations never hold the same live index at once — the skip
        loop in shared_rotation.next_identity); local ``(idx+1) % n``
        otherwise. Sets and returns the new index.
        """
        if not self._identity_rotation_enabled or self._proxy_mode != "vpn":
            return self._identity_index
        n = len(self._identity_profiles)
        if n <= 1:
            return 0
        if self._shared is not None:
            try:
                self._identity_index = self._shared.next_identity(self._station, n)
                return self._identity_index
            except Exception as e:
                logger.debug("[vpn] shared identity advance failed: %s", e)
        self._identity_index = (self._identity_index + 1) % n
        return self._identity_index

    async def _finalize_ip(self, allow_stale: bool = False) -> bool:
        """Validate + commit a fresh tunnel IP (up to 3 recovery rounds).

        Probes the public IP inside try/except — a dead network is a failed
        ATTEMPT, never an exception escaping to ``_watchdog_tick``. On a
        fresh IP (not recent on either station): commit the new face —
        registry, identity advance, history entry carrying the NEW identity,
        persist. When the IP is missing/recent: force a container restart
        (``_ensure_container`` — re-picks a server) and probe again.
        ``allow_stale`` accepts a recent IP on the last attempt (legacy
        recent_ips semantics) so a constrained boot cannot loop forever.
        """
        for attempt in range(3):
            try:
                new_ip = await self.get_public_ip()
            except Exception as e:
                new_ip = None
                logger.debug("[vpn] finalize probe %d failed: %s", attempt, e)
            if new_ip and not self._ip_recent(new_ip):
                return self._commit_ip(new_ip)
            if new_ip and self._ip_recent(new_ip) and allow_stale and attempt == 2:
                return self._commit_ip(new_ip)
            if attempt < 2:
                try:
                    await self._ensure_container()
                    # [plan 18/08 §A/am.14] per-round bound, not the old
                    # flat 120 s: 3 recovery rounds × _wait_healthy(120) +
                    # _ensure_container legs summed to the measured 445 s
                    # stall. The budget is carried over from the rotation
                    # config (no new knob — rotation_max_duration is the
                    # global wall).
                    started_at = await self._wait_healthy(
                        timeout=self._rotation_recovery_timeout)
                    if started_at:
                        auth = await self._check_auth_failed(started_at)
                        tls = await self._check_server_issue(started_at)
                        if auth or tls:
                            self._auth_failed = auth
                            self._server_issue = tls
                            logger.warning(
                                "[vpn] finalize recovery blocked: %s",
                                "AUTH_FAILED" if auth else "TLS negotiation timeout")
                    # The container action reset the country pool — re-pin so
                    # the recovery doesn't undo the rotation ([plan] A). The
                    # cursor always advances, so this picks a NEW country.
                    await self._pin_country_for_rotation()
                    await asyncio.sleep(self._switch_delay)
                except Exception as e:
                    logger.debug("[vpn] finalize recovery round failed: %s", e)
        return False

    def _commit_ip(self, new_ip: str) -> bool:
        """Commit a chosen IP: current IP, shared registry, identity advance
        BEFORE the history append (the entry must carry the NEW face — the
        old order logged the pre-advance identity), persist."""
        self._current_ip = new_ip
        self._auth_failed = False
        self._server_issue = False
        self._record_ip_change(new_ip)
        self._advance_identity()
        self._ip_history.append({
            "ip": new_ip,
            "server": self._docker_container,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "identity": self._live_identity.get("impersonate") or "",
            "identity_index": self._identity_index,
        })
        self._ip_history = self._ip_history[-100:]
        self.save_state()
        return True

    # ── Config ─────────────────────────────────────────────────

    def get_config(self) -> dict:
        """Return current configuration for the dashboard."""
        # Pool-level keys ("double embrayage") — read live from the config
        # mirror so the dashboard reflects the persisted state (they are
        # not per-station manager settings).
        try:
            from config.settings import _yaml_data as _cfg_data
            from config.settings import resolved_station_count
            _dual = _cfg_data.get("ip_rotation", {}).get("dual_station", False)
            _strict = _cfg_data.get("ip_rotation", {}).get("strict_free", False)
            _vpn_stack = _cfg_data.get("ip_rotation", {}).get("vpn_stack", "auto")
            _station_count = resolved_station_count(_cfg_data.get("ip_rotation", {}))
        except Exception:
            _dual = _strict = False
            _vpn_stack = "auto"
            _station_count = 2 if _dual else 1
        return {
            "enabled": self._enabled,
            "proxy_mode": self._proxy_mode,
            "mode": self._mode,
            "quota_per_ip": self._quota_per_ip,
            "switch_delay": self._switch_delay,
            "docker_container": self._docker_container,
            "docker_compose_file": self._docker_compose_file,
            "vpn_proxy_port": self._proxy_port,
            "socks5_proxy_port": self._socks5_port,
            "credentials_file": self._auth_file,
            "server_countries": self._server_countries,
            "server_provider": self._server_provider,
            "ip_check_url": self._ip_check_url,
            "ip_check_urls": self._ip_check_urls,
            # Never the control key itself — the dashboard only needs to
            # know whether control-server auth is armed.
            "control_api_key_set": bool(self._control_api_key),
            "control_enabled": self._control_enabled,
            "country_rotation": self._country_rotation,
            "country_offset": self._country_offset,
            "current_country": self._current_country,
            "wait_healthy_poll": self._wait_healthy_poll,
            "rotation_fail_cooldown": self._ROTATION_FAIL_COOLDOWN,
            "status_cache_seconds": self._STATUS_CACHE_SECONDS,
            "update_enabled": self._update_enabled,
            "update_check_interval": self._update_check_interval,
            "update_apply_window": self._update_apply_window,
            "update_apply_idle_minutes": self._update_idle_minutes,
            "update_apply_max_defer_hours": self._update_max_defer_hours,
            "circuit_breaker_threshold": self._circuit_breaker._failure_threshold,
            "circuit_breaker_recovery": self._circuit_breaker._recovery_time,
            "backoff_max_delay": self._backoff._max_delay,
            "watchdog_interval": self._watchdog_interval,
            "egress_failure_tick_interval": self._egress_failure_tick_interval,
            "ip_probe_budget": self._ip_probe_budget,
            "auto_wg_egress_ticks": self._auto_wg_egress_ticks,
            "control_pin_timeout": self._control_pin_timeout,
            "control_pin_catchup": self._control_pin_catchup,
            "rotation_max_duration": self._rotation_max_duration,
            "watchdog_backoff_base": self._watchdog_backoff._base_delay,
            "watchdog_backoff_max": self._watchdog_backoff._max_delay,
            "identity_rotation": self._identity_rotation_enabled,
            "identity_diversity": self._identity_diversity,
            "identity_max_profiles": self._identity_max_profiles,
            "identity_profiles": self._identity_profiles,
            "profiles_count": len(self._identity_profiles),
            "recent_ip_window": self._config.get("recent_ip_window", 20),
            "recent_ip_max_age": self._config.get("recent_ip_max_age", 1800),
            "shared_rotation_file": self._config.get("shared_rotation_file",
                                                      "logs/shared_rotation.json"),
            "dual_station": _dual,
            "strict_free": _strict,
            # [plan 18/08 §3d] stack selection (auto/wireguard/openvpn) —
            # read from the config mirror so the dashboard reflects what was
            # persisted, like dual_station above.
            "vpn_stack": _vpn_stack,
            # [plan 18/08 §1] parallel station count (1-10, resolved from
            # station_count / dual_station — same canonical value the
            # dropdown posts back).
            "station_count": _station_count,
        }

    async def update_config(self, updates: dict) -> dict:
        """Apply config updates from the dashboard (hot-reload, no restart)."""
        # Keep the config dict in sync: get_config() reads the window keys
        # back from self._config (not from live fields), so a stale dict
        # would re-show the OLD values in the dashboard form and re-submitting
        # would silently revert the hot-reload. In-place update — both
        # stations share the same IP_ROTATION dict, so both stay symmetric.
        self._config.update(updates)
        if "enabled" in updates:
            self._enabled = bool(updates["enabled"])
        if "proxy_mode" in updates and updates["proxy_mode"] in ("vpn", "direct"):
            self._proxy_mode = updates["proxy_mode"]
        if "quota_per_ip" in updates:
            self._quota_per_ip = max(1, int(updates["quota_per_ip"]))
        if "switch_delay" in updates:
            self._switch_delay = max(0.0, float(updates["switch_delay"]))
        if "server_countries" in updates:
            self._server_countries = str(updates["server_countries"])
        if "ip_check_urls" in updates:
            urls = updates["ip_check_urls"]
            if isinstance(urls, list):
                self._ip_check_urls = [str(u).strip() for u in urls if str(u).strip()]
                self._ip_check_idx = 0  # re-base on the new chain
        if "control_api_key" in updates and str(updates["control_api_key"]).strip():
            # Hot-reload the control key (never echoed back via get_config).
            self._control_api_key = str(updates["control_api_key"]).strip()
        if "country_rotation" in updates:
            self._country_rotation = bool(updates["country_rotation"])
        if "country_offset" in updates:
            self._country_offset = max(0, int(updates["country_offset"] or 0))
        if "wait_healthy_poll" in updates:
            self._wait_healthy_poll = max(0.1, float(updates["wait_healthy_poll"]))
        if "rotation_fail_cooldown" in updates:
            self._ROTATION_FAIL_COOLDOWN = max(5.0, float(updates["rotation_fail_cooldown"]))
        if "status_cache_seconds" in updates:
            self._STATUS_CACHE_SECONDS = max(0.5, float(updates["status_cache_seconds"]))
        if "server_provider" in updates:
            self._server_provider = str(updates["server_provider"]).strip() or "nordvpn"
        if "circuit_breaker_threshold" in updates or "circuit_breaker_recovery" in updates:
            threshold = updates.get(
                "circuit_breaker_threshold", self._circuit_breaker._failure_threshold)
            recovery = updates.get(
                "circuit_breaker_recovery", self._circuit_breaker._recovery_time)
            self._circuit_breaker = CircuitBreaker(
                failure_threshold=int(threshold), recovery_time=float(recovery))
        if "backoff_max_delay" in updates:
            self._backoff = BackoffTimer(
                base_delay=self._switch_delay,
                max_delay=float(updates["backoff_max_delay"]))
        if "watchdog_interval" in updates:
            self._watchdog_interval = max(1, int(updates["watchdog_interval"]))
        # [plan 18/08] armed cadence + probe budget + egress threshold — hot-
        # reload handlers (piège 8: two mandatory echo points; auto_wg_egress
        # ticks had NONE before this commit — read once at init).
        # [review 18/08 hot-reload] guarded: a malformed form value
        # (float(None)/int("")) raised into update_config → 500 → the whole
        # update_config fan-out was aborted. One bad key must skip itself,
        # not fail the rest.
        for _key, _conv, _floor in (
                ("egress_failure_tick_interval", float, 0.5),
                ("ip_probe_budget", float, 1.0),
                ("auto_wg_egress_ticks", int, 1),
                ("control_pin_timeout", float, 5.0),
                ("control_pin_catchup", float, 0.0),
                ("rotation_max_duration", float, 5.0)):
            if _key not in updates:
                continue
            try:
                setattr(self, f"_{_key}", max(_floor, _conv(updates[_key])))
            except (TypeError, ValueError):
                logger.warning("[vpn] ignored invalid hot-reload %s=%r",
                               _key, updates[_key])
        if "update_enabled" in updates:
            self._update_enabled = bool(updates["update_enabled"])
        if "update_check_interval" in updates:
            self._update_check_interval = max(300, int(updates["update_check_interval"]))
        if "update_apply_window" in updates:
            self._update_apply_window = str(updates["update_apply_window"])
        if "update_apply_idle_minutes" in updates:
            self._update_idle_minutes = max(1, int(updates["update_apply_idle_minutes"]))
        if "update_apply_max_defer_hours" in updates:
            self._update_max_defer_hours = max(1, int(updates["update_apply_max_defer_hours"]))
        if "identity_rotation" in updates:
            self._identity_rotation_enabled = bool(updates["identity_rotation"])
        if "identity_diversity" in updates:
            self._identity_diversity = bool(updates["identity_diversity"])
        if "identity_max_profiles" in updates:
            self._identity_max_profiles = max(1, int(updates["identity_max_profiles"]))
        if "identity_profiles" in updates:
            self._identity_profiles_base = _normalize_identity_profiles(updates["identity_profiles"])
        if any(k in updates for k in ("identity_diversity", "identity_max_profiles",
                                      "identity_profiles")):
            # Rebuild from the explicit base (seed), not from the current pool.
            self._identity_profiles = _build_identity_pool(
                self._identity_profiles_base,
                self._identity_diversity,
                self._identity_max_profiles,
            )
            if self._identity_profiles:
                self._identity_index %= len(self._identity_profiles)  # clamp (config may shrink)
        if "watchdog_backoff_base" in updates or "watchdog_backoff_max" in updates:
            _wbase = max(1.0, float(updates.get(
                "watchdog_backoff_base", self._watchdog_backoff._base_delay)))
            _wmax = max(_wbase, float(updates.get(
                "watchdog_backoff_max", self._watchdog_backoff._max_delay)))
            self._watchdog_backoff = BackoffTimer(base_delay=_wbase, max_delay=_wmax)
        if self._shared is not None and any(
                k in updates for k in ("recent_ip_window", "recent_ip_max_age",
                                       "shared_rotation_file")):
            # Hot-reload the cross-station windows (the shared registry is
            # created once by opencode's lifespan; it re-reads these itself).
            self._shared.set_window(self._config)
        return self.get_config()

    def get_status(self) -> dict:
        """Return current VPN status for the dashboard."""
        elapsed = int(time.monotonic() - self._connected_at) if self._connected_at else None
        return {
            "enabled": self._enabled,
            "proxy_mode": self._proxy_mode,
            "mode": self._mode,
            "status": self._status,
            "ip": self._current_ip,
            "server": self._current_server.get("name") if self._current_server else None,
            "country": self._current_server.get("country") if self._current_server else None,
            "current_country": self._current_country,
            "country_pinned_at": self._country_pinned_at,
            "next_country": self._next_country_preview(),
            "country_rotation": self._country_rotation,
            "connected_seconds": elapsed,
            "total_switches": self._total_switches,
            "station": self._station,
            "error": self._error,
            "auth_failed": self._auth_failed,
            "last_rotation_error": self._last_rotation_error,
            "rotation_failed_at": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._last_rotation_failed_at))
                                   if self._last_rotation_failed_at else None),
            "ip_history": self._ip_history[-10:],
            "proxy_port": self._proxy_port,
            "proxy_url": self.proxy_url,
            "socks5_url": self.socks5_url,
            "container": self._docker_container,
            "circuit_breaker": self._circuit_breaker.get_status(),
            "backoff_failures": self._backoff.consecutive_failures,
            "backoff_delay": self._backoff.delay,
            "watchdog": {
                "interval": self._watchdog_interval,
                "failures": self._watchdog_backoff.consecutive_failures,
                "next_delay": self._watchdog_backoff.delay,
                "server_issue": self._server_issue,
                # [plan 18/08 am.23] pool-signal observability — debug a
                # parasitic fast-recover / missed wake straight from the API.
                "egress_failures": self._egress_failures,
                "egress_armed": self._egress_failures > 0,
                "egress_threshold": self._auto_wg_egress_ticks,
                "egress_tick_interval": self._egress_failure_tick_interval,
                "last_conn_failure_at": self._last_conn_failure_at,
                # [review 18/08] key aligned with stack_info's signal_count
                # (the dashboard static reads egress_failures only).
                "signal_count": self._conn_failure_signal_count,
            },
            "identity_index": self._identity_index,
            "profiles_count": len(self._identity_profiles),
            "current_identity": self.current_identity,
            "update": {
                "enabled": self._update_enabled,
                "check_interval": self._update_check_interval,
                "apply_window": self._update_apply_window,
                "idle_minutes": self._update_idle_minutes,
                "max_defer_hours": self._update_max_defer_hours,
                "available": self._update_available,
                "current_digest": self._update_current_digest,
                "new_digest": self._update_new_digest,
                "checked_at": self._update_checked_at,
                "applied_at": self._update_applied_at,
                "last_error": self._update_last_error,
            },
        }

    # ── Docker helpers (all blocking — run via asyncio.to_thread) ──

    def _docker_run(self, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
        """Run a docker CLI command (blocking — call via asyncio.to_thread).

        [plan 18/08 §D am.8] Rotation-op funnel: while a rotation is in
        flight (_rotation_loop set by _connect_next_impl), count in-flight
        docker threads and signal their REAL end via the rotation op event
        (call_soon_threadsafe from the worker). asyncio.to_thread is NOT
        cancellable — a rotation deadline must wait for the thread to
        actually finish before launching the next attempt, or a second
        pin/compose would race the first. Reads/writes of the count are
        single-word ops (GIL-stable) and the increment happens before
        subprocess.run (which releases the GIL anyway).

        [review 18/08] funnel generation: the worker captures the rotation
        generation at op-start and only decrements if it still matches —
        the rotation teardown bumps the generation, so a thread left over
        from a cancelled rotation finishing while the NEXT rotation runs
        can no longer zero the new rotation's count.
        """
        loop = self._rotation_loop
        op_ev = self._rotation_op_event
        gen = self._rotation_op_generation
        if loop is not None and op_ev is not None:
            self._rotation_op_count += 1
        try:
            return subprocess.run(
                ["docker", *args],
                capture_output=True, text=True, timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
                encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            raise RuntimeError("docker CLI not found on PATH")
        finally:
            if loop is not None and op_ev is not None:
                # Stale-decrement guard: the rotation's teardown bumped the
                # generation, so this op belongs to a dead rotation — leave
                # the (new) count untouched. max(0, …) is belt-and-braces.
                if self._rotation_op_generation == gen:
                    self._rotation_op_count = max(0, self._rotation_op_count - 1)
                try:
                    loop.call_soon_threadsafe(op_ev.set)
                except RuntimeError:
                    pass  # loop already closed at shutdown — nothing to wake

    async def _await_rotation_ops_drained(self) -> None:
        """[plan 18/08 §D am.8] Wait for in-flight docker threads to end.

        Called when a rotation attempt hits the wall — the worker thread is
        inside asyncio.to_thread and CANNOT be cancelled, so a subsequent
        attempt (or the repair rung) must wait for its real finish before
        issuing another docker primitive, or two pins/composes would race
        each other. Level-triggered hazard: op_ev may already be set when
        we look (the real end raced our drain) — clear BEFORE each wait so
        a stale set cannot return us early while an op is still running.
        """
        while self._rotation_op_count > 0:
            self._rotation_op_event.clear()
            if self._rotation_op_count > 0:
                await self._rotation_op_event.wait()

    async def _docker_inspect(self) -> dict:
        """Inspect the gluetun container. Returns {} if absent or docker unavailable."""
        try:
            result = await asyncio.to_thread(
                self._docker_run, ["inspect", self._docker_container], 15)
        except RuntimeError as e:
            logger.warning("[vpn] %s", e)
            return {}
        if result.returncode != 0:
            return {}
        try:
            info = json.loads(result.stdout)[0]
        except (json.JSONDecodeError, IndexError):
            return {}
        state = info.get("State", {})
        return {
            "running": state.get("Running", False),
            "healthy": state.get("Health", {}).get("Status") == "healthy",
            "restarting": state.get("Restarting", False),
            "started_at": state.get("StartedAt", ""),
            "mounts": info.get("Mounts", []),
        }

    async def _docker_restart(self) -> None:
        """Restart the gluetun container to get a fresh IP.

        Note ([25]): this deliberately bypasses docker compose — a plain
        ``docker restart`` keeps the compose definition untouched (no drift
        of the declared service). Image updates go through ``apply_update``
        instead, which recreates the container from the new image.
        """
        result = await asyncio.to_thread(
            self._docker_run, ["restart", self._docker_container], 60)
        if result.returncode != 0:
            raise RuntimeError(
                f"docker restart failed: {result.stderr.strip() or result.stdout.strip()}")

    # ── gluetun control server client ────────────────────────────
    #
    # All access is via `docker exec <ctn> sh -c 'wget ...'`. The control
    # key comes from the CONTAINER's OWN env ($VPN_CONTROL_API_KEY, set by
    # compose credentials.env and used by the healthcheck) — wget doesn't
    # expand env vars inside --header values and docker exec never
    # propagates host env, so sh does the expansion inside the container.
    # The key therefore never appears in host argv, `ps`, or logs.
    # Port 8000 is gluetun's default control-server port, never published.
    # Routes (v3.41.3): GET /v1/vpn/status -> {"status": running|stopped},
    # GET /v1/publicip/ip -> {"public_ip": ...}, PUT /v1/vpn/settings (partial
    # merge -> validate -> stop+start). NEVER call GET /v1/vpn/settings — it
    # discloses credentials; we track the country in our own state.

    async def _control_exec(self, method: str, path: str,
                            body: Optional[str] = None,
                            timeout: float = 10.0) -> list[str]:
        """Run one wget call against gluetun's control server inside the
        container. Returns the decoded stdout lines on success, [] on any
        failure (non-zero exit, wget absent, control server down, timeout).
        Never logs the key. A 204 (no body) is still a success -> [].
        """
        key_var = "VPN_CONTROL_API_KEY"
        cmd = ["exec", self._docker_container]
        script = f'wget -q -O - -T {int(timeout)}' \
                 f' --header="X-API-Key: ${key_var}"'
        if method != "GET":
            script += f' --method={method}'
            if body:
                script += f' --body-data={_sh_quote(body)}'
        script += f" http://127.0.0.1:8000{path}"
        cmd += ["sh", "-c", script]
        try:
            result = await asyncio.to_thread(
                self._docker_run, cmd, int(timeout) + 5)
        except RuntimeError as e:
            logger.debug("[vpn] control server call %s %s failed: %s",
                         method, path, e)
            return []
        if result.returncode != 0:
            logger.debug("[vpn] control server %s %s rc=%d: %s", method, path,
                         result.returncode,
                         (result.stderr or result.stdout or "").strip()[:200])
            return []
        out = (result.stdout or "").splitlines()
        return [ln.strip() for ln in out if ln.strip()]

    async def _control_status(self) -> Optional[bool]:
        """True when gluetun reports the VPN running, False when stopped,
        None when the control server is unreachable (fail toward the
        SOCKS5/status fallback chain)."""
        if not self._control_enabled:
            return None
        lines = await self._control_exec("GET", "/v1/vpn/status", timeout=5)
        for ln in lines:
            try:
                return json.loads(ln).get("status") == "running"
            except (json.JSONDecodeError, AttributeError):
                continue
        return None

    async def _control_public_ip(self) -> Optional[str]:
        """IP as reported BY gluetun (self-reported, not a tunnel probe)."""
        if not self._control_enabled:
            return None
        lines = await self._control_exec("GET", "/v1/publicip/ip", timeout=5)
        for ln in lines:
            try:
                ip = json.loads(ln).get("public_ip")
            except (json.JSONDecodeError, AttributeError):
                continue
            if ip:
                return str(ip)
        return None

    async def _control_pin_country(self, country: str,
                                   timeout: float = 60.0,
                                   catchup: float = 0.0) -> bool:
        """Ask gluetun to connect through ``country`` (PUT settings -> real
        stop+start reconnect). Polls ``status: running`` at the healthy-poll
        cadence until ``timeout``. Returns True only when the VPN came back
        up (the IP itself is validated separately by the rotation path).

        ``catchup`` (am.3/am.12): once status flips to 'running', keep
        waiting up to ``catchup`` seconds for a REAL public IP to answer
        THROUGH the tunnel (cheap probe on the same SOCKS5 stack the request
        path uses). A tunnel that reports running but never answers is the
        445 s stall class — the catch-up abandons it early on an auth/TLS
        signature but otherwise yields True at the wall (running is the pin
        verdict; the IP is a speed bonus, not a gate — catchup=0 preserves
        the exact legacy behavior).

        [incident 17/08] A pin that lands on an auth/TLS-failing server
        NEVER recovers on its own (openvpn SIGUSR1-retries forever). We
        fail fast by scanning the container logs since the pin started: an
        AUTH_FAILED / TLS-negotiation line aborts the pin within
        ``_wait_healthy_poll`` so the rotation cursor advances to the next
        country immediately — instead of sitting in ``timeout`` (outage
        was ~2 min before this fix).
        """
        if not self._control_enabled or not country:
            return False
        country = _normalize_country(country)
        payload = json.dumps(
            {"provider": {"server_selection": {"countries": [country]}}},
            separators=(",", ":"))
        lines = await self._control_exec(
            "PUT", "/v1/vpn/settings", body=payload, timeout=10)
        # [17/08 live] gluetun v3.41.3 answers a SUCCESSFUL settings PUT
        # with "200 OK" + body "running" (7 bytes, text/plain) — NOT 204
        # No Content. We must not treat that body as a rejection, or every
        # successful pin falls back to the slow --force-recreate path.
        # Only a non-"running" body is an error response.
        if lines:
            if lines[0].strip().lower() == "running":
                pass  # accepted — proceed to the status/auth poll below
            else:
                hint = ""
                if "choices" in lines[0].lower():
                    hint = (f" (gluetun reported this country name as "
                            f"invalid / \"not in choices\" — check "
                            f"server_countries)")
                logger.warning("[vpn] control pin %s rejected: %s%s",
                               country, lines[0][:200], hint)
                return False
        # Bound the failure scan to logs written AFTER the pin — a stale
        # AUTH_FAILED from an earlier reconnect in the same container must
        # not abort a healthy pin.
        since_pin = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        deadline = time.monotonic() + timeout
        stopped_warned = False          # [incident 17/08] one WARN per pin — 59/line spam
        while time.monotonic() < deadline:
            # Scan BEFORE leaning on the status reply: gluetun can report
            # "running" while openvpn is caught in an AUTH_FAILED retry
            # loop, so the auth scan is the decisive signal.
            if await self._check_auth_failed(since_pin) \
                    or await self._check_server_issue(since_pin):
                logger.warning(
                    "[vpn] control pin %s: auth/TLS failure since pin start "
                    "— abandoning, the next country will be pinned "
                    "immediately", country)
                return False
            status = await self._control_status()
            if status is True:
                if catchup > 0:
                    # [plan 18/08 §B] "running" is not enough: a tunnel that
                    # never answers a real IP probe is the 445 s stall class
                    # (SOCKS5 CONNECT accepted but no data). Local loop,
                    # never _wait_healthy (its fail-fast "not running" is a
                    # false friend mid-reconnect, piège 7). Abort at once on
                    # an auth/TLS signature; otherwise yield True at the
                    # catch-up wall (running is the pin verdict).
                    catchup_deadline = time.monotonic() + catchup
                    while time.monotonic() < catchup_deadline:
                        if await self._check_auth_failed(since_pin) \
                                or await self._check_server_issue(since_pin):
                            # [audit 18/08] same WARN as the pre-status scan —
                            # a TLS failure mid-catch-up was silent before
                            # (the incident was diagnosed through logs).
                            logger.warning(
                                "[vpn] control pin %s: auth/TLS failure "
                                "during catch-up — abandoning", country)
                            return False
                        try:
                            if await self.get_public_ip() is not None:
                                return True    # a real IP through the tunnel
                        except Exception:
                            pass               # probe slipped — keep waiting
                        await asyncio.sleep(self._wait_healthy_poll)
                return True
            if status is False:
                if not stopped_warned:
                    logger.warning("[vpn] control pin %s: VPN reports stopped",
                                   country)
                    stopped_warned = True
            await asyncio.sleep(self._wait_healthy_poll)
        logger.warning("[vpn] control pin %s: not running after %.0fs",
                       country, timeout)
        return False

    def _countries_list(self) -> list[str]:
        """Ordered rotation list from config server_countries (normalized to
        gluetun's canonical names via _normalize_country — "United_Kingdom"
        → "United Kingdom", "Czechia" → "Czech Republic"), or [] when unset."""
        raw = getattr(self, "_server_countries", "") or ""
        if not isinstance(raw, str):
            raw = str(raw)
        names = [c.strip() for c in raw.split(",") if c.strip()]
        out = []
        for c in names:
            c = _normalize_country(c)
            if c and c not in out:
                out.append(c)
        return out

    def _local_next_country(self, previous: Optional[str]) -> Optional[str]:
        """Deterministic local fallback when the shared state / control
        server isn't available: walk the config list from the last pinned
        country. Never returns the same country twice in a row."""
        countries = self._countries_list()
        if not countries:
            return None
        self._country_index = self._country_index % len(countries)
        nxt = countries[self._country_index]
        if countries[self._country_index] == previous:
            self._country_index = (self._country_index + 1) % len(countries)
            nxt = countries[self._country_index]
        self._country_index = (self._country_index + 1) % len(countries)
        return nxt

    async def _pin_country_for_rotation(self,
                                        timeout: Optional[float] = None,
                                        catchup: Optional[float] = None) -> Optional[str]:
        """Advance the shared country cursor and pin the next country via
        the control server (PUT /v1/vpn/settings — a real reconnect).

        ``timeout``/``catchup`` (am.3): explicit budgets override the
        configured attrs — None keeps the config defaults (60/0 = legacy).
        The fast-recover path threads 15/15 so ITS pins hit the 30 s/pin,
        120 s/4-pin wall without touching normal rotation behavior.

        Returns the pinned country name when the control server accepted
        it and the VPN came back up; None when country rotation is off,
        the control server is unavailable, or the pin failed — callers
        then fall through to the legacy restart path. On success the
        cursor has advanced (the next pin differs), so a "settings left
        unchanged" reply can never repeat a country.
        """
        if not self._country_rotation or not self._control_enabled:
            return None
        countries = self._countries_list()
        if len(countries) < 2:
            return None
        idx = None
        if self._shared is not None and hasattr(self._shared, "next_country"):
            try:
                idx = self._shared.next_country(
                    self._station, self._country_offset, len(countries))
            except Exception as e:
                logger.debug("[vpn] shared country cursor failed: %s", e)
        nxt = countries[idx] if idx is not None else \
            self._local_next_country(self._current_country)
        if nxt is None or nxt == self._current_country:
            return None
        pin_timeout = self._control_pin_timeout if timeout is None else timeout
        pin_catchup = self._control_pin_catchup if catchup is None else catchup
        if await self._control_pin_country(nxt, timeout=pin_timeout,
                                           catchup=pin_catchup):
            self._current_country = nxt
            self._country_pinned_at = time.monotonic()
            logger.info("[vpn] station %d pinned country %s",
                        self._station, nxt)
            return nxt
        return None

    async def _current_hostname(self, since: str) -> Optional[str]:
        """Hostname gluetun is currently trying — the LAST "Connecting to
        [...]" line in container logs written since ``since``. The SIGUSR1
        retry loop re-logs the same remote every ~11 s, so the last
        occurrence is the host in play. None when the container is absent
        or no hostname is logged (verbosity < 4)."""
        result = await asyncio.to_thread(
            self._docker_run, ["logs", "--since", since, self._docker_container], 30)
        if result.returncode != 0:
            return None
        return _extract_current_hostname(result.stdout)

    async def _fast_recover_via_control(self, max_skips: int = 3,
                                        timeout: float = 15.0,
                                        catchup: float = 15.0) -> bool:
        """Recover an AUTH_FAILED/TLS tunnel WITHOUT compose: re-pin the
        next country via the control API (PUT /v1/vpn/settings — a real
        stop+start reconnect, ~8-15 s, vs minutes for --force-recreate).

        ``timeout=15, catchup=15`` (am.3): every pin here is 30 s max and
        the loop is bounded at ``max_skips+1`` pins → 120 s absolute wall
        for the watchdog escalation (vs 60 s wall per pin before). Normal
        rotation paths keep the configured 20/25 → 45 s wall.

        Skips blacklisted hosts without waiting for their ~11 s SIGUSR1
        retry cycle: after a successful pin the current hostname is
        extracted from logs, and a blacklisted host triggers an immediate
        re-pin (the shared cursor guarantees a different country). A FAILED
        pin is retried only when a live auth/TLS rejection is visible since
        the pin (dead-host case — the next country can work); infrastructure
        failures (control server unreachable, timeout) fall through to the
        compose path right away instead of burning the remaining attempts.

        Returns True when the tunnel is healthy on a fresh IP; False lets
        the watchdog's compose path (unchanged) be the escalation. Never
        calls compose itself — callers run under the same lock, so at most
        one recovery per tick.
        """
        if not self._country_rotation or not self._control_enabled:
            return False
        for attempt in range(max_skips + 1):
            since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            nxt = await self._pin_country_for_rotation(timeout=timeout,
                                                       catchup=catchup)
            if nxt is None:
                if await self._check_auth_failed(since) \
                        or await self._check_server_issue(since):
                    logger.warning(
                        "[vpn] fast-pin: still rejecting (attempt %d/%d) — "
                        "re-pinning a different country",
                        attempt + 1, max_skips + 1)
                    continue  # dead host: the next country can work
                return False  # infra failure: compose path is the escalation
            host = await self._current_hostname(since)
            if host and self._host_blacklisted(host):
                logger.warning(
                    "[vpn] fast-pin: %s blacklisted — skipping it "
                    "(attempt %d/%d)", host, attempt + 1, max_skips + 1)
                continue
            if await self._finalize_ip(allow_stale=False):
                self._watchdog_backoff.record_success()
                self._set_status(VPNState.CONNECTED)
                self._error = None
                await self.refresh_status(force=True)
                self.save_state()
                logger.info("[vpn] fast-pin: recovered — tunnel healthy (IP %s)",
                            self._current_ip)
                return True
            # Finalize failed to land a fresh IP — try the next country.
        return False

    def _next_country_preview(self) -> Optional[str]:
        """Best-effort preview of the next pinned country for the dashboard
        (the 'next country' cell). Mirrors _pin_country_for_rotation's pick
        WITHOUT advancing the shared cursor or the local fallback cursor."""
        if not self._country_rotation or not self._control_enabled:
            return None
        countries = self._countries_list()
        if len(countries) < 2:
            return None
        idx = None
        if self._shared is not None and hasattr(self._shared, "peek_next_country"):
            try:
                idx = self._shared.peek_next_country(
                    self._station, self._country_offset, len(countries))
            except Exception:
                idx = None
        if idx is not None:
            nxt = countries[idx]
        else:
            # Local mirror of _local_next_country's walk — read-only.
            cursor = self._country_index % len(countries)
            nxt = countries[cursor]
            if nxt == self._current_country:
                nxt = countries[(cursor + 1) % len(countries)]
        return nxt if nxt != self._current_country else None

    async def _compose_up(self, force_recreate: bool = False) -> None:
        """Start the gluetun service via docker compose (idempotent)."""
        compose_file = self._compose_file_path()
        cmd = ["compose", "-f", compose_file, "up", "-d", self._compose_service]
        if force_recreate:
            # A plain `docker restart` does NOT apply a changed env — only
            # recreation re-reads the compose definition. AUTH_FAILED
            # recovery must go through `compose up --force-recreate` so the
            # wider pool (SERVER_COUNTRIES) is actually in effect.
            cmd.insert(4, "--force-recreate")
        result = await asyncio.to_thread(self._docker_run, cmd, 120)
        if result.returncode != 0:
            raise RuntimeError(
                f"docker compose up failed: {result.stderr.strip() or result.stdout.strip()}")

    async def _ensure_container(self, force_recreate: Optional[bool] = None) -> None:
        """Compose up if the container is absent, else restart it.

        ``force_recreate``: None → recreate when the last restart was a
        failure (AUTH_FAILED / server issue), so the changed compose
        definition is re-read via ``--force-recreate``; True → always
        recreate; False → plain ``docker restart``.
        """
        if force_recreate is None:
            force_recreate = bool(self._auth_failed or self._server_issue)
        info = await self._docker_inspect()
        if not info:
            await self._compose_up()
        elif force_recreate:
            await self._compose_up(force_recreate=True)
        else:
            await self._docker_restart()

    # ------------------------------------------------------------------
    # [plan 18/08 §3b/3c] Stack selector (auto / wireguard / openvpn)
    # ------------------------------------------------------------------

    def _auto_flip_decision(self) -> Optional[tuple]:
        """Auto-mode policy, called every watchdog tick INSIDE the lock with
        fresh counters. Returns (mode, reason) when a flip is due, else None.
        Only meaningful when self._stack == "auto"; a manual selection never
        flips on its own (the user's choice wins — _apply_stack is only
        reached through set_stack() in that case).

        - effective wireguard + egress dead ≥ auto_wg_egress_ticks → openvpn
          (dead tunnel; the flip itself is the recovery).
        - effective openvpn + ≥ auto_ov_fail_threshold live AUTH_FAILED in the
          sliding 30-min window → wireguard (preferred stack).
        - effective openvpn + window empty for auto_ov_return_min (healthy
          OV for an hour) → back to wireguard (sticky: WG is preferred).
        - cooldown auto_flip_cooldown_min between auto flips (anti-flapping);
          never flip to wireguard when the key file is missing (guard).
        """
        if self._stack != "auto":
            return None
        now = self._now_fn()
        # Lazy prune of the sliding window.
        cutoff = now - 30 * 60
        self._auth_failed_window = [t for t in self._auth_failed_window if t >= cutoff]
        if self._last_auto_flip_at is not None and now - self._last_auto_flip_at < \
                self._auto_flip_cooldown_min * 60:
            return None  # cooldown — anti-flapping
        if self._stack_effective == "wireguard":
            if self._egress_failures >= self._auto_wg_egress_ticks:
                return ("openvpn", f"egress dead {self._egress_failures} ticks")
            return None
        # Effective OpenVPN below.
        if self._stack_effective != "openvpn":
            return None
        if not self._wg_key_present():
            return None  # cannot flip to wireguard without the key
        if len(self._auth_failed_window) >= self._auto_ov_fail_threshold:
            return ("wireguard", f"{len(self._auth_failed_window)} AUTH_FAILED/30min")
        if self._stack_since is not None and now - self._stack_since >= \
                self._auto_ov_return_min * 60 and not self._auth_failed_window:
            return ("wireguard", f"OV healthy {self._auto_ov_return_min} min — return to WG")
        return None

    def _wg_key_present(self) -> bool:
        try:
            return os.path.isfile(self._wg_key_file)
        except Exception:
            return False

    async def _apply_stack(self, mode: str, reason: str = "manual",
                           auto: bool = False) -> bool:
        """Switch the effective stack of ALL ACTIVE stations (compose
        substitution) and record the flip. mode ∈ {"wireguard", "openvpn"}
        (auto is resolved by the caller). Refuses wireguard when
        vpn_configs/wireguard.env is missing — the keys are NEVER generated
        or stored by the proxy.

        Writes VPN_TYPE_STATION{1..N} for the active stations into the .env
        next to the compose file (read-modify-write + os.replace, atomic —
        same pattern as save_state) and PRUNES stale keys from downscaled
        stations, then `compose up -d --force-recreate` on the active
        services.

        No-op semantics [fix 19/08]: when ``mode == _stack_effective`` the
        .env is STILL re-synced (an upscaled station must join the stack the
        active set already runs — without it the compose default
        ``${VPN_TYPE_STATIONn:-openvpn}`` boots it on OpenVPN under a running
        WireGuard fleet) — only the compose recreate is skipped.
        """
        if mode not in ("wireguard", "openvpn"):
            logger.error("[vpn] _apply_stack: invalid mode %r", mode)
            return False
        if mode == "wireguard" and not self._wg_key_present():
            logger.warning("[vpn] _apply_stack: refusing wireguard — %s missing",
                           self._wg_key_file)
            return False
        # [plan 18/08 §1] Active stations: the live registry (set by the
        # lifespan / _apply_station_count). Fallback to the legacy station
        # 1/2 pair when there is no registry (standalone manager, unit
        # tests) so past dual-station behavior is unchanged.
        _managers = list(getattr(shared_state, "vpn_managers", None) or [])
        stations, services = [], []
        for _m in _managers:
            if _m is not None and _m._station not in stations:
                stations.append(_m._station)
                services.append(_m._compose_service)
        if not stations:
            stations = [1, 2] if self._station <= 2 else [self._station]
            services = (["vpn-gluetun", "vpn-gluetun-2"] if self._station <= 2
                        else [self._compose_service])
        station_keys = {f"VPN_TYPE_STATION{s}" for s in stations}
        compose_path = self._compose_file_path()
        env_path = os.path.join(os.path.dirname(compose_path), ".env")
        try:
            # Read-modify-write: NEVER touch the other .env keys (secrets
            # live there) — only the active stations' VPN_TYPE_STATION vars.
            data = ""
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    data = f.read()
            except FileNotFoundError:
                pass
            lines = data.splitlines()
            out, seen = [], set()
            for ln in lines:
                stripped = ln.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    out.append(ln)
                    continue
                key = stripped.split("=", 1)[0].strip()
                if key in station_keys:
                    seen.add(key)
                    if stripped == f"{key}={mode}":
                        out.append(ln)  # already the target value
                    else:
                        out.append(f"{key}={mode}")
                    continue
                # Prune stale per-station vars from downscaled stations so a
                # later upscale never resurrects a leftover value.
                if key.startswith("VPN_TYPE_STATION") and re.fullmatch(
                        r"VPN_TYPE_STATION\d+", key):
                    continue
                out.append(ln)
            for key in sorted(station_keys - seen):
                out.append(f"{key}={mode}")
            tmp = env_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(out) + "\n")
            os.replace(tmp, env_path)
        except OSError as e:
            logger.error("[vpn] _apply_stack: cannot write %s: %s", env_path, e)
            return False
        # [fix 19/08] No-op check AFTER the .env write: the env must be
        # re-synced even when the stack is already effective — the compose
        # default ${VPN_TYPE_STATIONn:-openvpn} would otherwise boot a newly
        # upscaled station on OpenVPN under a running WireGuard fleet.
        if mode == self._stack_effective:
            logger.info("[vpn] stack already %s — no-op (env synced)", mode)
            return True
        # Recreate ALL active stations in the target stack in one compose
        # call — profiled services run when explicitly targeted (same
        # mechanism a station brings itself up). 300 s: N recreations,
        # one command.
        try:
            cmd = (["compose", "-f", compose_path, "up", "-d", "--force-recreate"]
                   + sorted(services))
            result = await asyncio.to_thread(self._docker_run, cmd, 300)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        except Exception as e:
            logger.error("[vpn] _apply_stack: compose recreate failed: %s", e)
            return False
        previous = self._stack_effective
        self._stack_effective = mode
        self._stack_since = self._now_fn()
        if auto:
            self._last_auto_flip_at = self._now_fn()
        self._flips.append({
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "from": previous,
            "to": mode,
            "reason": reason,
        })
        self._flips = self._flips[-20:]
        logger.info("[vpn] stack: %s→%s (%s)", previous, mode, reason)
        return True

    async def set_stack(self, mode: str, propagate: bool = True) -> dict:
        """API entry point (dashboard). mode ∈ {"auto", "wireguard", "openvpn"}.
        For a manual selection: apply it immediately (reason="manual").
        propagate=False → state-only sync (used for the other managers:
        _apply_stack already recreated ALL active containers — a second
        compose call would be a duplicate).
        Returns a result dict for the dashboard."""
        if mode not in ("auto", "wireguard", "openvpn"):
            return {"ok": False, "error": f"unknown stack {mode!r}"}
        if mode == "auto":
            # Back to auto: no forced flip — the watchdog policy resumes at
            # the next tick from the current effective stack (WG stays WG).
            self._stack = "auto"
            self._auth_failed_window = []
            self._last_auto_flip_at = None
            logger.info("[vpn] stack: auto (effective %s, watchdog resumes)",
                        self._stack_effective)
            return {"ok": True, "effective": self._stack_effective}
        if propagate:
            ok = await self._apply_stack(mode, reason="manual")
            if ok:
                self._stack = mode  # manual selection wins over auto policy
        else:
            # State-only: station-2 manager mirrors the flip already applied
            # by station 1's _apply_stack (both containers were recreated).
            self._stack = mode
            self._stack_effective = mode
            self._stack_since = self._now_fn()
            self._auth_failed_window = []
            self._last_auto_flip_at = None
            ok = True
        return {"ok": ok, "effective": self._stack_effective}

    def stack_info(self) -> dict:
        """Snapshot for GET /api/vpn-stack-info (dashboard rendering)."""
        return {
            "selected": self._stack,
            "effective": self._stack_effective,
            "keys_present": self._wg_key_present(),
            "egress_failures": self._egress_failures,
            "egress_armed": self._egress_failures > 0,
            "wg_egress_ticks": self._auto_wg_egress_ticks,
            "last_conn_failure_at": self._last_conn_failure_at,
            "signal_count": self._conn_failure_signal_count,
            "auth_failed_window": len(self._auth_failed_window),
            "auth_failed_threshold": self._auto_ov_fail_threshold,
            "cooldown_min": self._auto_flip_cooldown_min,
            "stack_since": self._stack_since,
            "flips": self._flips[-5:],
        }

    async def _check_auth_failed(self, started_at: str = "") -> bool:
        """Scan container logs for AUTH_FAILED (OpenVPN auth rejection).

        RECOVERY-AWARE ([incident 17/08]: gluetun's OpenVPN retry loop logs
        an AUTH_FAILED push then lands on a working server and prints
        "Initialization Sequence Completed" — the failure is recovered, not
        live. An AUTH_FAILED before the LAST successful sequence is stale
        and must not flip the state to ERROR; only an AUTH_FAILED AFTER the
        last success (or with no success at all) is a live rejection. This
        also covers the old StartedAt-bounding case: a pre-restart failure
        in the window is likewise followed by a success after the restart.
        Falls back to a 10-minute window when the start time is unknown.
        """
        since = started_at if started_at else "10m"
        result = await asyncio.to_thread(
            self._docker_run, ["logs", "--since", since, self._docker_container], 30)
        if result.returncode != 0:
            return False
        text = result.stdout
        if "AUTH_FAILED" not in text and "auth failed" not in text.lower():
            return False                       # never auth-failed
        # Recovered tunnel: the last logged openvpn success supersedes the
        # last AUTH_FAILED. rfind on the chunk is cheap (str scan).
        if text.rfind("Initialization Sequence Completed") < text.rfind("AUTH_FAILED"):
            # [plan 18/08] LIVE rejection (after the last success) — blacklist
            # the current hostname for fast-pin (Phase 1c). Recorded only
            # here, where live-ness is already established: an AUTH_FAILED
            # superseded by a success must never poison the blacklist.
            self._record_auth_failure(text)
            # [plan 18/08 §3c] auto-mode counter: sliding 30-min window of
            # LIVE rejections (threshold in config: auto_ov_fail_threshold).
            # Monotonic timestamps; pruned lazily by _auto_flip_decision.
            self._auth_failed_window.append(self._now_fn())
            return True
        return False

    def _record_auth_failure(self, text: str) -> None:
        """Blacklist the current NordVPN hostname after a LIVE AUTH_FAILED.

        The caller has already established the rejection is live (no later
        "Initialization Sequence Completed"). The blacklist is consumed ONLY
        by the fast-pin path (Phase 1c) — free_ip_pool never sees it. TTL
        24 h covers the daily sunset window; a host that works again simply
        expires back into rotation.
        """
        host = _extract_current_hostname(text)
        if not host:
            return  # verbosity < 4: no hostname in logs — nothing to blacklist
        now = time.time()
        entry = self._failed_hosts.get(host)
        if entry is None:
            entry = {"failures": 0, "first_failed_at": now, "bad_until": 0.0}
            self._failed_hosts[host] = entry
        entry["failures"] += 1
        entry["bad_until"] = now + _FAILED_HOST_TTL
        logger.warning(
            "[vpn] AUTH_FAILED on %s (failure #%d, TTL 24h) — fast-pin will skip it",
            host, entry["failures"])

    def _host_blacklisted(self, host: str) -> bool:
        """True while a host is inside its 24 h blacklist window. Prunes the
        entry when it expires, so the dict never grows unbounded."""
        entry = self._failed_hosts.get(host)
        if entry is None:
            return False
        if time.time() >= entry.get("bad_until", 0):
            del self._failed_hosts[host]
            return False
        return True

    async def _check_server_issue(self, started_at: str = "") -> bool:
        """Scan container logs for a TLS negotiation failure (stale server
        IP or crashed server — gluetun's 🔌 'server no longer valid'
        guidance). Detection: the raw openvpn line that guidance attaches to
        in internal/openvpn/logs.go. Same recovery-aware bounding as
        _check_auth_failed: a TLS failure superseded by a later successful
        connection is stale, not live."""
        since = started_at if started_at else "10m"
        result = await asyncio.to_thread(
            self._docker_run, ["logs", "--since", since, self._docker_container], 30)
        if result.returncode != 0:
            return False
        text = result.stdout.lower()
        if "tls key negotiation failed" not in text:
            return False
        return text.rfind("initialization sequence completed") < text.rfind("tls key negotiation failed")

    async def _wait_healthy(self, timeout: float = 120.0) -> Optional[str]:
        """Wait until the container runs AND the SOCKS5 tunnel answers.

        Returns the container's StartedAt on success — callers bind their
        AUTH_FAILED scan to it, so a pre-restart AUTH_FAILED still in the
        logs cannot flip state (docker logs span container restarts, [15]).
        Returns None on failure/timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = await self._docker_inspect()
            if info.get("running") and await self.get_public_ip():
                return info.get("started_at", "")
            # Fail fast: AUTH_FAILED (rejected credentials) and TLS negotiation
            # failures (dead server) never recover on their own — do not sit
            # out the timeout, report immediately (TLS fails in ~20 s).
            if not info or not info.get("running") \
                    or await self._check_auth_failed(info.get("started_at", "")) \
                    or await self._check_server_issue(info.get("started_at", "")):
                return None
            await asyncio.sleep(self._wait_healthy_poll)
        return None

    # ── Auto-update (gluetun image) ────────────────────────────

    _UPDATE_LOCK_PATH = os.path.join(ROOT, "logs", "vpn_update.lock")

    def note_free_request(self) -> None:
        """Record free-request activity (used to pick an opportune
        moment for applying a pending update)."""
        self._last_free_request_at = time.monotonic()

    def note_free_stream_start(self) -> None:
        """Count an open free stream (called by opencode.py when a free
        stream starts). Auto-update must not interrupt live streams ([21])."""
        self._active_free_streams += 1

    def note_free_stream_end(self) -> None:
        """Counterpart of note_free_stream_start — called when a free
        stream closes, in a finally block."""
        if self._active_free_streams > 0:
            self._active_free_streams -= 1

    async def _docker_image_id(self, image: str) -> Optional[str]:
        result = await asyncio.to_thread(
            self._docker_run,
            ["image", "inspect", image, "--format", "{{.Id}}"], 30)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    async def _docker_repo_digest(self, image: str) -> Optional[str]:
        result = await asyncio.to_thread(
            self._docker_run,
            ["image", "inspect", image, "--format", "{{index .RepoDigests 0}}"], 30)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    async def check_update(self) -> bool:
        """Check whether a newer gluetun image is available (non-disruptive).

        `docker compose pull` downloads the new image but never touches the
        running container. A digest change before/after means an update is
        available; the previous image ID is kept for rollback.
        """
        if not self._update_enabled:
            return False
        try:
            image = "qmcgaw/gluetun"
            old_digest = await self._docker_repo_digest(image)
            if not old_digest:
                return False  # image not installed yet — nothing to update
            old_id = await self._docker_image_id(image)
            compose_file = self._compose_file_path()
            result = await asyncio.to_thread(
                self._docker_run,
                ["compose", "-f", compose_file, "pull", self._compose_service], 300)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            new_digest = await self._docker_repo_digest(image)
            self._update_checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._update_last_error = None
            if new_digest and new_digest != old_digest:
                self._update_available = True
                self._update_current_digest = old_digest
                self._update_new_digest = new_digest
                self._update_old_image_id = old_id
                self._update_known_since = self._update_known_since or time.monotonic()
                logger.info("[vpn-update] new gluetun image available: %s", new_digest[:19])
                return True
            self._update_available = False
            self._update_new_digest = None
            return False
        except Exception as e:
            self._update_last_error = str(e)
            logger.warning("[vpn-update] check failed: %s", e)
            return False

    def _acquire_update_lock(self) -> bool:
        """Cross-instance lock: refuse if another process is applying an
        update right now. Stale locks (older than 15 min) are broken."""
        try:
            os.makedirs(os.path.dirname(self._UPDATE_LOCK_PATH), exist_ok=True)
            fd = os.open(self._UPDATE_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(self._UPDATE_LOCK_PATH) > 900:
                    os.remove(self._UPDATE_LOCK_PATH)
                    return self._acquire_update_lock()
            except OSError:
                pass
            return False
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True

    def _release_update_lock(self) -> None:
        try:
            os.remove(self._UPDATE_LOCK_PATH)
        except OSError:
            pass

    async def apply_update(self, check_opportune: bool = False) -> dict:
        """Apply a pending update: recreate the container with the new image,
        verify the tunnel, roll back on failure (best-effort).

        Args:
            check_opportune: re-check opportune-ness INSIDE the lock
                ([21] TOCTOU fix) and skip when live free streams would be
                cut. The background updater and the manual /api/vpn/update
                endpoint pass True; the lock is held across the whole apply
                (which can take minutes), so the check must happen here, not
                before acquiring.
        """
        async with self._lock:
            if not self._update_available:
                return {"ok": False, "error": "no update available"}
            if check_opportune and not self._update_opportune():
                return {"ok": False, "error": "not opportune (traffic active)"}
            if check_opportune and self._active_free_streams > 0:
                return {"ok": False, "error": "free streams active — deferring update"}
            if not self._acquire_update_lock():
                return {"ok": False, "error": "another instance is applying an update"}
            try:
                compose_file = self._compose_file_path()
                result = await asyncio.to_thread(
                    self._docker_run,
                    ["compose", "-f", compose_file, "up", "-d", "--pull", "never", self._compose_service], 120)
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip() or result.stdout.strip())
                started_at = await self._wait_healthy(timeout=120)
                if started_at is None:
                    raise RuntimeError("tunnel not healthy after update")
                if await self._check_auth_failed(started_at):
                    self._auth_failed = True
                    raise RuntimeError("AUTH_FAILED after update")
                self._auth_failed = False
                # Fresh IP + NEW identity: the recreate re-picked a server, so
                # validate the IP is not recent on either station and advance
                # the identity ([plan] C.4). Failure rolls back below.
                if not await self._finalize_ip(allow_stale=False):
                    raise RuntimeError("could not finalize a fresh IP after update")
                # force: container was just recreated — a cached status
                # would report the pre-update container ([37]).
                await self.refresh_status(force=True)
                self._update_available = False
                self._update_applied_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self._update_known_since = None
                self._update_old_image_id = None
                self.save_state()
                logger.info("[vpn-update] applied — container recreated with new image (IP %s)",
                            self._current_ip)
                return {"ok": True, "ip": self._current_ip}
            except Exception as e:
                self._update_last_error = str(e)
                logger.error("[vpn-update] apply failed, rolling back: %s", e)
                await self._rollback_update(self._update_old_image_id)
                return {"ok": False, "error": str(e)}
            finally:
                self._release_update_lock()

    async def _rollback_update(self, old_image_id: Optional[str]) -> None:
        """Best-effort rollback: re-tag the previous image and recreate."""
        if not old_image_id:
            logger.error("[vpn-update] no previous image ID — cannot roll back")
            return
        try:
            image = "qmcgaw/gluetun"
            await asyncio.to_thread(self._docker_run, ["tag", old_image_id, image], 30)
            compose_file = self._compose_file_path()
            await asyncio.to_thread(
                self._docker_run,
                ["compose", "-f", compose_file, "up", "-d", "--pull", "never", self._compose_service], 120)
            # force: container was just recreated — see _apply_update ([37]).
            await self.refresh_status(force=True)
            logger.warning("[vpn-update] rolled back to previous image %s", old_image_id[:19])
        except Exception as e:
            logger.error("[vpn-update] rollback failed: %s", e)

    def _update_opportune(self) -> bool:
        """Decide whether NOW is a good moment to apply a pending update.

        - tunnel down           → apply immediately (no traffic at risk)
        - nightly window (03:00-05:00 local) AND idle ≥ N min → apply
        - known for > max_defer_hours → apply anyway
        - otherwise             → wait for the next tick
        """
        if self._status != VPNState.CONNECTED:
            return True  # tunnel down: nothing to disrupt
        if self._update_known_since and \
                time.monotonic() - self._update_known_since > self._update_max_defer_hours * 3600:
            logger.info("[vpn-update] deferring >%dh — applying anyway",
                        self._update_max_defer_hours)
            return True
        try:
            start_str, end_str = self._update_apply_window.split("-")
            start_h = int(start_str.split(":")[0])
            end_h = int(end_str.split(":")[0])
        except (ValueError, AttributeError):
            start_h, end_h = 3, 5
        in_window = start_h <= time.localtime().tm_hour < end_h
        if not in_window:
            return False
        if self._last_free_request_at is None or \
                time.monotonic() - self._last_free_request_at >= self._update_idle_minutes * 60:
            return True
        return False

    async def _updater_loop(self) -> None:
        """Background loop: detect available updates and apply them at the
        most opportune moment. First iteration runs immediately, then every
        update_check_interval seconds."""
        while True:
            try:
                if await self.check_update() and self._update_opportune():
                    # Re-check opportune-ness inside the lock ([21]): traffic
                    # may have started between the outer check and the apply.
                    await self.apply_update(check_opportune=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[vpn-update] background loop error: %s", e)
            await asyncio.sleep(self._update_check_interval)

    # ── Auth watchdog ──────────────────────────────────────────

    def arm_egress_watchdog(self) -> None:
        """[plan 18/08 §1c] Pool signal: a REAL connection failed on this
        tunnel (SOCKS5 dead). Arms the immediate recovery — the next tick
        probes/recovers in ~1 s instead of waiting N idle ticks. The tick's
        light probe decides alone: healthy → reset (a transient blip is
        absorbed, no recovery at all), dead → threshold reached → recovery.
        Both stacks: the shared counter reaches the threshold in every mode.
        """
        self._egress_failures = max(self._egress_failures,
                                    self._auto_wg_egress_ticks)
        self._last_conn_failure_at = self._now_fn()
        self._conn_failure_signal_count += 1
        if self._watchdog_event is not None:
            self._watchdog_event.set()  # loop wait(timeout) returns → live tick

    async def _watchdog_recover_fresh_ip(self) -> bool:
        """[plan 18/08 §E3/am.20] Shared recovery tail for the watchdog tick,
        used by BOTH rungs (light restart and compose): re-pin the next
        country (the container action reset the pool — the shared cursor
        always advances, so this picks a NEW country, [plan] A), then
        finalize a FRESH IP — the watchdog must never land back on the
        failed server/IP (_finalize_ip probes + re-picks ≤3 rounds). On
        success: backoff reset, CONNECTED, refresh, persist. Returns True
        when the tunnel is healthy on a fresh IP (the tick can return),
        else False (the caller keeps its recovery rung or backs off)."""
        await self._pin_country_for_rotation()
        if not await self._finalize_ip(allow_stale=False):
            return False
        self._watchdog_backoff.record_success()
        self._set_status(VPNState.CONNECTED)
        self._error = None
        await self.refresh_status(force=True)
        self.save_state()
        logger.info("[vpn-watchdog] recovered — tunnel healthy (IP %s)",
                    self._current_ip)
        return True

    async def _watchdog_loop(self) -> None:
        """Background watchdog: auto-restart the gluetun container when
        OpenVPN hits AUTH_FAILED or a TLS negotiation failure (stale/crashed
        server) — neither recovers on its own, but a fresh connection attempt
        eventually succeeds, so each detection triggers one `docker restart`.
        After a restart that did not recover, the next scan runs on the
        exponential backoff cadence (base x2, capped) so the first retries land
        within the first minutes; while healthy it paces at the interval.
        """
        logger.info("[vpn-watchdog] active — interval %ds, backoff %ds → %ds",
                    self._watchdog_interval,
                    self._watchdog_backoff._base_delay,
                    self._watchdog_backoff._max_delay)
        while True:
            try:
                await self._watchdog_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[vpn-watchdog] loop error: %s", e)
            # Failure cadence = backoff (base→max); armed cadence = light
            # probe every _egress_failure_tick_interval s (WG AND OV — the
            # counter is shared); healthy cadence = normal interval.
            delay = (self._watchdog_backoff.delay
                     if self._watchdog_backoff.consecutive_failures > 0
                     else self._egress_failure_tick_interval
                     if self._egress_failures > 0
                     else self._watchdog_interval)
            # Sleep dually on the interval AND container events: every
            # die/stop/kill/start (docker events watcher sets
            # _watchdog_event) wakes the loop immediately, so recovery
            # starts within ms instead of on the next tick. None-guarded:
            # the watcher may be disabled. A stale set (event set while
            # wait_for raced its timeout) costs one extra tick — harmless.
            if self._watchdog_event is not None:
                try:
                    await asyncio.wait_for(self._watchdog_event.wait(),
                                           timeout=delay)
                    self._watchdog_event.clear()
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(delay)

    async def _watchdog_tick(self) -> None:
        """One watchdog pass: scan for AUTH_FAILED/TLS failure; restart when found."""
        if not self._enabled or self._proxy_mode != "vpn":
            return  # VPN feature off or tunnel bypassed — nothing to watch
        if self._rotation_task and not self._rotation_task.done():
            # Rotation in flight — a restart would race its IP validation
            # (the skip MUST stay). Trace it once per rotation so a lost
            # pool wake (plan guard 2529) is visible in the logs: the
            # per-task pointer re-logs for a NEW rotation, and stays quiet
            # while the SAME rotation keeps the tick out.
            if self._skipped_rotation_task is not self._rotation_task:
                self._skipped_rotation_task = self._rotation_task
                logger.info("[vpn-watchdog] tick skipped — rotation in flight")
            return
        if self._lock.locked():
            return  # connect/rotate/apply_update in progress — skip this tick
        escalate = False
        async with self._lock:
            # [plan 18/08 §1d/§E2] Armed state: skip the full refresh
            # (~3-8 s of docker CLI — the tick would blow past the 2 s
            # cadence). The light probe decides alone; the refresh returns
            # to transitions (recovery/compose, below) and idle ticks.
            if not self._egress_failures:
                await self.refresh_status(force=True)
            egress_dead = False
            # [plan 18/08 §E1] Light egress probe — SOCKS5 handshake +
            # CONNECT to the ip_check endpoint, bounded by the probe budget,
            # NO GET (dead/alive suffices; _finalize_ip does the full GET
            # when the tunnel returns). Stack-agnostic: the incident tunnel
            # was "connected" with no AUTH_FAILED/TLS marker — invisible
            # without a probe. Same connect chain as get_public_ip.
            if not await asyncio.wait_for(self._probe_tunnel_light(),
                                          self._ip_probe_budget):
                self._egress_failures += 1
                if self._egress_failures >= self._auto_wg_egress_ticks:
                    egress_dead = True
                    # [plan 18/08 §am.22] Egress death CONFIRMED — cancel the
                    # in-flight free streams on this tunnel (the pool
                    # registered them). Sync call, safe inside the lock. Each
                    # task's CancelledError surfaces in its handler as a
                    # watchdog-cancel → failover retry (the bad-mark already
                    # excludes the dead station) — never a passive up-to-600 s
                    # read-timeout stall on a dead tunnel.
                    pool = getattr(shared_state, "free_ip_pool", None)
                    if pool is not None:
                        pool.cancel_streams(self)
                else:
                    logger.warning("[vpn-watchdog] egress dead %d/%d ticks — waiting",
                                   self._egress_failures, self._auto_wg_egress_ticks)
                    return
            else:
                if self._egress_failures:
                    logger.info("[vpn-watchdog] egress OK — counter reset")
                self._egress_failures = 0
            # [plan 18/08 §3c] auto-mode policy — decided inside the lock on
            # fresh counters, applied AFTER it (_apply_stack takes the lock;
            # asyncio.Lock is not reentrant, calling it here would deadlock).
            self._pending_flip = self._auto_flip_decision()
            if not (self._auth_failed or self._server_issue) and not egress_dead:
                self._watchdog_backoff.record_success()  # failure cleared: full cadence
                if self._pending_flip is None:
                    return
                # Healthy tick but a flip is due (auto: OV→WG return): skip
                # the recovery block entirely — nothing is failing — the
                # flip applies after the lock below.
            else:
                # Failing tick: the WG recovery path below may still succeed
                # (fast-pin/compose revive the tunnel) — it returns early,
                # which cancels the flip; only a failed recovery reaches the
                # flip application after the lock. Intended: prefer keeping
                # the current stack when a plain recovery works.
                info = await self._docker_inspect()
                if not info or not info.get("running"):
                    return  # absent/stopped/restarting — other paths own the lifecycle
                if egress_dead:
                    kind = "egress dead"
                elif self._auth_failed:
                    kind = "AUTH_FAILED"
                else:
                    kind = "TLS negotiation timeout"
                logger.warning("[vpn-watchdog] %s detected — restarting %s",
                               kind, self._docker_container)
                # [plan 18/08] Fast recovery via the control API BEFORE
                # compose: re-pin the next country (PUT /v1/vpn/settings) — a
                # real stop+start reconnect in ~8-15 s with zero compose, vs
                # minutes for a --force-recreate. Skips blacklisted hosts. On
                # failure the compose path below (unchanged) is the
                # escalation; same lock, so at most one recovery action per
                # tick.
                if await self._fast_recover_via_control(max_skips=3):
                    return
                try:
                    # [plan 18/08 §E3/am.20] LIGHT rung first — a plain
                    # `docker restart` (~1-2 s) before the heavy compose
                    # escalation: on a healthy stack it is all that is needed.
                    # The compose rung below is the ESCALATION only —
                    # --force-recreate exists solely to apply a WIDENED
                    # SERVER_COUNTRIES pool after a restart that did NOT clear
                    # the failure marker (a plain `docker restart` could never
                    # apply it). Both rungs share the recovery tail
                    # (_watchdog_recover_fresh_ip): re-pin the next country +
                    # finalize a FRESH IP — the watchdog must never land back
                    # on the failed server/IP. Per-rung guards: a failing
                    # rung must not abort the tick — the compose rung IS the
                    # escape hatch for a failed light restart.
                    try:
                        await self._docker_restart()
                        started_at = await self._wait_healthy(timeout=60)
                    except Exception as e:
                        logger.warning("[vpn-watchdog] light restart failed: %s — "
                                       "escalating to compose", e)
                        started_at = None
                    healed = bool(started_at) and not (
                        await self._check_auth_failed(started_at)
                        or await self._check_server_issue(started_at))
                    if healed and await self._watchdog_recover_fresh_ip():
                        return
                    if not healed:
                        logger.info("[vpn-watchdog] restart did not clear %s — "
                                    "compose escalation", kind)
                        try:
                            await self._ensure_container()
                            started_at = await self._wait_healthy(timeout=120)
                        except Exception as e:
                            logger.warning("[vpn-watchdog] compose escalation "
                                           "failed: %s", e)
                            started_at = None
                        healed = bool(started_at) and not (
                            await self._check_auth_failed(started_at)
                            or await self._check_server_issue(started_at))
                        if healed and await self._watchdog_recover_fresh_ip():
                            return
                    # Still failing: keep the error state and back off. This
                    # tail is inside the try — with no exception it runs ONCE
                    # after both rungs failed to heal; with an exception the
                    # except below records the same single failure.
                    self._watchdog_backoff.record_failure()
                    escalate = self._watchdog_backoff.consecutive_failures >= 2
                    logger.error("[vpn-watchdog] restart did not recover — next "
                                 "attempt in %ds (failure #%d)",
                                 int(self._watchdog_backoff.delay),
                                 self._watchdog_backoff.consecutive_failures)
                except Exception as e:
                    self._watchdog_backoff.record_failure()
                    escalate = self._watchdog_backoff.consecutive_failures >= 2
                    logger.error("[vpn-watchdog] restart failed: %s — next attempt "
                                 "in %ds", e, int(self._watchdog_backoff.delay))
        # [plan 18/08 §3c] auto flip — applied OUTSIDE the lock (compose).
        # A successful flip supersedes the escalation: the tunnel was just
        # recreated in the other stack, no need to refresh servers/image too.
        if self._pending_flip is not None:
            mode, reason = self._pending_flip
            self._pending_flip = None
            ok = await self._apply_stack(mode, reason="auto: " + reason, auto=True)
            if ok:
                return
            # Flip failed (e.g. compose error): fall through to the escalation
            # below if the tick was failing — recovery is still needed.
            escalate = escalate or self._watchdog_backoff.consecutive_failures >= 2
        # Escalation OUTSIDE the lock: apply_update() takes self._lock —
        # asyncio.Lock is not reentrant, calling it here would deadlock.
        if escalate:
            await self._watchdog_escalate()

    async def _watchdog_escalate(self) -> None:
        """≥2 failed restarts: the server list may be stale. Refresh it, then
        try an image update (fresh list embedded per release). apply_update()
        applies immediately: the tunnel is down, so _update_opportune()
        returns True. Re-armed every 30 min to keep retrying long-lived
        outages without hammering docker."""
        if self._watchdog_escalated_at and \
                time.monotonic() - self._watchdog_escalated_at < 1800:
            return
        self._watchdog_escalated_at = time.monotonic()
        logger.warning("[vpn-watchdog] escalating: refreshing %s servers list "
                       "+ checking image update", self._server_provider)
        await self._refresh_servers_list()
        applied = False
        if self._update_enabled:
            try:
                if await self.check_update():
                    result = await self.apply_update(check_opportune=True)
                    applied = bool(result.get("ok"))
            except Exception as e:
                logger.error("[vpn-watchdog] escalation update failed: %s", e)
        if not applied:
            # No new image applied: plain restart re-picks a server from the
            # freshly refreshed list. Re-check the lock — a rotation/connect
            # may have started while the update check was running.
            try:
                if not self._lock.locked():
                    await self._ensure_container()
            except Exception as e:
                logger.error("[vpn-watchdog] escalation restart failed: %s", e)

    async def _resolve_gluetun_volume(self) -> str:
        """Name of the named volume backing /gluetun in the running container.

        docker compose names the volume '<project>_gluetun' (project = the
        compose file's directory basename), which varies per deployment path
        (e.g. opencode-proxy vs opencode-proxy-main) — never hardcode it.
        Falls back to the bare 'gluetun' when the container is absent (its
        own docker run-era name) so the refresh still has a target."""
        info = await self._docker_inspect()
        for m in info.get("mounts") or []:
            if m.get("Type") == "volume" and m.get("Destination", "").rstrip("/") == "/gluetun" \
                    and m.get("Name"):
                return m["Name"]
        return "gluetun"

    async def _refresh_servers_list(self) -> None:
        """One-shot `gluetun update -providers <provider>` into the named
        volume backing /gluetun: rewrites the servers file with the current
        list, works without a running tunnel, and the local file takes
        precedence over the embedded list at next start. Fails soft."""
        try:
            volume = await self._resolve_gluetun_volume()
            result = await asyncio.to_thread(
                self._docker_run,
                ["run", "--rm", "-v", f"{volume}:/gluetun", "qmcgaw/gluetun",
                 "update", "-providers", self._server_provider], 180)
            if result.returncode != 0:
                logger.error("[vpn-watchdog] servers refresh returned %d: %s",
                             result.returncode, result.stdout[-500:])
        except Exception as e:
            logger.error("[vpn-watchdog] servers refresh failed: %s", e)

    # ── State persistence ──────────────────────────────────────

    def _get_state_path(self) -> str:
        """Return path to the VPN state file (per-station: station 2 keeps
        its own history/circuit breaker in logs/vpn_state2.json)."""
        return self._state_file

    def save_state(self):
        """Persist IP history, stats, and circuit breaker state to disk.

        Atomic write ([20]): temp file + os.replace, so a crash mid-write
        can never leave a truncated state file.
        """
        try:
            state = {
                "ip_history": self._ip_history,
                "total_switches": self._total_switches,
                "circuit_breaker": self._circuit_breaker.get_status(),
                "current_ip": self._current_ip,
                "identity_index": self._identity_index,
                "current_country": self._current_country,
                # [plan 18/08] hostname blacklist (wall-clock TTL)
                "failed_hosts": self._failed_hosts,
                # [plan 18/08 §3b] stack selection + flip journal (cap 20)
                "stack": self._stack,
                "flips": self._flips,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            state_path = self._get_state_path()
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            tmp_path = state_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, state_path)
            logger.debug("[vpn] state saved to %s", state_path)
        except Exception as e:
            logger.debug("[vpn] failed to save state: %s", e)

    def load_state(self):
        """Load persisted state from disk."""
        try:
            state_path = self._get_state_path()
            if not os.path.exists(state_path):
                return
            with open(state_path, "r") as f:
                state = json.load(f)
            self._ip_history = state.get("ip_history", [])
            self._total_switches = state.get("total_switches", 0)
            # Restore identity index with clamp (config may have shrunk)
            self._identity_index = state.get("identity_index", 0) % len(self._identity_profiles)
            # Restore circuit breaker state ([20]): an open breaker survives a
            # restart. get_status() saves failures/state but no opened_at, so
            # a restored open breaker goes half-open immediately — intended
            # (a reboot is a natural recovery attempt).
            cb = state.get("circuit_breaker")
            if isinstance(cb, dict):
                self._circuit_breaker._servers = {
                    k: {"failures": v.get("failures", 0),
                        "opened_at": v.get("opened_at", 0),
                        "state": v.get("state", "closed")}
                    for k, v in cb.items() if isinstance(v, dict)
                }
            # Restore the last known IP so a fresh start doesn't show "unknown"
            self._current_ip = state.get("current_ip") or self._current_ip
            # Fail-open: a persisted country is only a hint — the next pin
            # advances the cursor anyway, and an empty/unknown value falls
            # back to None (no country labeled until the next pin).
            # Normalized [incident 17/08]: a stale "Czechia"/"United_Kingdom"
            # persisted before the name fix must not label the dashboard or
            # skew the nxt == _current_country comparison.
            restored_country = state.get("current_country")
            if isinstance(restored_country, str) and restored_country:
                self._current_country = _normalize_country(restored_country)
            # [plan 18/08] Restore the hostname blacklist; prune entries whose
            # 24 h window has passed (wall-clock epoch, monotonic-independent).
            now = time.time()
            self._failed_hosts = {
                k: v for k, v in state.get("failed_hosts", {}).items()
                if isinstance(v, dict) and v.get("bad_until", 0) > now
            }
            # [plan 18/08 §3b] Restore the stack selection (auto default) and
            # the flip journal. Restoring the selection across restarts is
            # what makes the GUI choice persistent without touching .env;
            # effective stays as computed at init (WG if keys present else
            # OV) — the watchdog reconciles auto at the next tick.
            restored_stack = state.get("stack")
            if restored_stack in ("auto", "wireguard", "openvpn"):
                self._stack = restored_stack
            restored_flips = state.get("flips")
            if isinstance(restored_flips, list):
                self._flips = [f for f in restored_flips if isinstance(f, dict)][-20:]
            logger.info("[vpn] state loaded: %d IPs in history, %d total switches",
                        len(self._ip_history), self._total_switches)
        except Exception as e:
            logger.debug("[vpn] failed to load state: %s", e)
