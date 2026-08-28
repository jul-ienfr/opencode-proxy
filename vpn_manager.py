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

import asyncio
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

import shared_state

if TYPE_CHECKING:  # annotation seule — pas d'import runtime (déjà injecté en paramètre)
    from shared_rotation import SharedRotationState

# [plan v10 §14.1.9] Sérialise le read-modify-write du .env entre managers
# concurrents (hot-reload vs auto-flip hétérogène). La section critique est
# 100% synchrone (file I/O, aucun await) → threading.Lock suffit.
_ENV_RW_LOCK = threading.Lock()

# ── Docker Desktop auto-launch (Windows) ───────────────────────────────
# If Docker Desktop is not running, `docker ps` fails with "error during connect".
# On Windows, the proxy must auto-launch it (user request) — otherwise 5 stations
# stay `disconnected` after a reboot. Fail-soft: if launch fails, the caller logs
# and continues (watchdog will retry).
_DOCKER_DESKTOP_LAUNCHED = False


async def ensure_docker_running(timeout: int = 60) -> bool:
    """Ensure the Docker daemon is reachable; on Windows auto-launch Desktop if needed.

    Returns True if `docker ps` succeeds within `timeout` seconds, False otherwise.
    Launch is best-effort: if Docker Desktop is not installed or fails to start,
    the caller should log and continue (watchdog will retry).
    """
    global _DOCKER_DESKTOP_LAUNCHED
    # Quick probe: is daemon already reachable?
    try:
        r = await asyncio.to_thread(
            subprocess.run,
            ["docker", "ps"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if r.returncode == 0:
            return True
    except Exception:
        pass
    if sys.platform != "win32":
        return False
    # Windows: try to launch Docker Desktop if not already attempted this boot
    if _DOCKER_DESKTOP_LAUNCHED:
        # Already tried once this process — don't loop forever, just wait a bit
        for _ in range(timeout // 2):
            await asyncio.sleep(2)
            try:
                r = await asyncio.to_thread(
                    subprocess.run,
                    ["docker", "ps"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=CREATE_NO_WINDOW,
                )
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False
    # [v10 §14.3.13] latch posé uniquement après un Popen RÉUSSI (voir plus bas)
    # Common install paths
    candidates = [
        r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
        r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
        os.path.expandvars(r"%ProgramFiles%\Docker\Docker\Docker Desktop.exe"),
        os.path.expandvars(r"%LocalAppData%\Docker\Docker Desktop.exe"),
    ]
    exe = None
    for p in candidates:
        try:
            if p and os.path.exists(p) and p.lower().endswith("docker desktop.exe"):
                exe = p
                break
        except Exception:
            continue
    if not exe:
        # Fallback: try `start` via shell (may be on PATH)
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["cmd", "/c", "start", "", "Docker Desktop"],
                capture_output=True,
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            pass
    else:
        try:
            # Use Popen to not block; Desktop will daemonize
            subprocess.Popen(
                [exe],
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            # [plan v10 §14.3.13] le latch ne se pose qu'après un lancement
            # EFFECTIF : sinon un Popen raté verrouillait _LAUNCHED=True pour
            # tout le process → plus aucune relance jusqu'au restart.
            logging.getLogger(__name__).warning("[docker] failed to launch Desktop %s: %s", exe, e)
            return False
        _DOCKER_DESKTOP_LAUNCHED = True
    # Wait for daemon to become reachable
    for _ in range(timeout // 2):
        await asyncio.sleep(2)
        try:
            r = await asyncio.to_thread(
                subprocess.run,
                ["docker", "ps"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode == 0:
                logging.getLogger(__name__).info("[docker] daemon reachable after Desktop launch")
                return True
        except Exception:
            pass
    logging.getLogger(__name__).warning("[docker] daemon still not reachable after %ds", timeout)
    return False

# Cross-module registry of active VPN managers ([plan 18/08 §1]): set by
# opencode.py's lifespan / _apply_station_count, read by _apply_stack to
# scope a stack flip to the currently configured stations. shared_state est
# un module de données pur (aucun import) — l'import en tête ne crée aucun
# cycle (fix E402 v10).

logger = logging.getLogger(__name__)

# Windows: hide console windows for subprocess calls
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Project root
ROOT = os.path.dirname(os.path.abspath(__file__))


# ── Identity rotation (client fingerprint) ─────────────────────

# Stable desktop impersonation targets of curl_cffi 0.14 (verified by
# Session(impersonate=...) instantiation). Alpha (chrome133a), mobile,
# android, iOS, tor and generic variants are excluded.
_KNOWN_IMPERSONATIONS = frozenset(
    {
        "chrome99",
        "chrome100",
        "chrome101",
        "chrome104",
        "chrome107",
        "chrome110",
        "chrome116",
        "chrome119",
        "chrome120",
        "chrome123",
        "chrome124",
        "chrome131",
        "chrome136",
        "chrome142",
        "edge99",
        "edge101",
        "firefox133",
        "firefox135",
        "firefox144",
        "safari153",
        "safari155",
        "safari15_3",
        "safari15_5",
        "safari170",
        "safari17_0",
        "safari180",
        "safari184",
        "safari18_0",
        "safari18_4",
        "safari260",
        "safari2601",
    }
)

_DEFAULT_IDENTITY_PROFILE: dict = {"impersonate": "chrome131", "user_agent": None, "extra_headers": {}}


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
            result.append(
                {
                    "impersonate": imp,
                    "user_agent": ua if isinstance(ua, str) and ua.strip() else None,
                    "extra_headers": dict(extra) if isinstance(extra, dict) else {},
                }
            )
    if not result:
        logger.warning(
            "[identity] no valid profiles — using default %r",
            _DEFAULT_IDENTITY_PROFILE["impersonate"],
        )
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
        "153": "15.3",
        "15_3": "15.3",
        "155": "15.5",
        "15_5": "15.5",
        "170": "17.0",
        "17_0": "17.0",
        "180": "18.0",
        "18_0": "18.0",
        "184": "18.4",
        "18_4": "18.4",
        "260": "26.0",
        "2601": "26.1",
    }.get(raw, "17.0")


def _ua_for_target(imp: str) -> str:
    """Deterministic desktop UA for an impersonation target (by family)."""
    if imp.startswith("chrome"):
        ver = imp[len("chrome") :]
        return (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36"
        )
    if imp.startswith("edge"):
        ver = imp[len("edge") :]
        return (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{ver}.0.4951.64 Safari/537.36 "
            f"Edg/{ver}.0.1210.53"
        )
    if imp.startswith("firefox"):
        ver = imp[len("firefox") :]
        return (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{ver}.0) Gecko/20100101 Firefox/{ver}.0"
        )
    if imp.startswith("safari"):
        ver = _safari_version(imp[len("safari") :])
        return (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            f"AppleWebKit/605.1.15 (KHTML, like Gecko) "
            f"Version/{ver} Safari/605.1.15"
        )
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
    seen = {(p["impersonate"], p["user_agent"], _headers_key(p["extra_headers"])) for p in base}
    for imp in sorted(_KNOWN_IMPERSONATIONS):
        for headers in _identity_header_variants(imp):
            profile = {"impersonate": imp, "user_agent": None, "extra_headers": headers}
            key = (imp, None, _headers_key(headers))
            if key in seen:
                continue
            seen.add(key)
            pool.append(profile)
    pool.sort(
        key=lambda p: (
            p["impersonate"],
            p.get("user_agent") or "",
            str(sorted((k.lower(), v) for k, v in p["extra_headers"].items())),
        )
    )
    if len(pool) > max_profiles:
        pool = pool[:max_profiles]
    logger.info(
        "[identity] pool: %d profiles (diversity=%s, cap=%d)", len(pool), diversity, max_profiles
    )
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
        # [plan v10 §14.1.11] sonde half-open UNIQUE : sans ce garde, toutes
        # les coroutines qui arrivent pendant la fenêtre de recovery passent
        # (thundering herd sur un endpoint malade à chaque recovery).
        self._half_open_probe: set[str] = set()

    def record_success(self, server_name: str):
        """Record a successful connection — reset failure count."""
        self._servers[server_name] = {"failures": 0, "opened_at": 0, "state": "closed"}
        self._half_open_probe.discard(server_name)

    def record_failure(self, server_name: str):
        """Record a failed connection — increment count, open if threshold reached.

        [§14.1.11] une sonde half-open qui échoue RE-OUVRE immédiatement
        (sinon les appelants suivants repassaient tous jusqu'au threshold)."""
        info = self._servers.get(server_name, {"failures": 0, "opened_at": 0, "state": "closed"})
        was_half_open = info.get("state") == "half-open"
        info["failures"] += 1
        if was_half_open or info["failures"] >= self._failure_threshold:
            info["state"] = "open"
            info["opened_at"] = time.monotonic()
            logger.warning(
                "[circuit-breaker] server %s OPEN after %d failures%s",
                server_name,
                info["failures"],
                " (sonde half-open échouée — re-open immédiat)" if was_half_open else "",
            )
        self._servers[server_name] = info
        self._half_open_probe.discard(server_name)

    def is_available(self, server_name: str) -> bool:
        """Check if a server is available (circuit closed or half-open ready).

        [§14.1.11] half-open = UNE seule sonde en vol ; les appelants suivants
        reçoivent False jusqu'au verdict (success/failure) de la sonde."""
        info = self._servers.get(server_name)
        if not info or info["state"] == "closed":
            return True
        if info["state"] == "open":
            # Check if recovery time has passed -> half-open
            if time.monotonic() - info["opened_at"] >= self._recovery_time:
                if server_name in self._half_open_probe:
                    return False  # une sonde est déjà en cours
                info["state"] = "half-open"
                self._half_open_probe.add(server_name)
                logger.info("[circuit-breaker] server %s -> half-open (testing, sonde unique)", server_name)
                return True
            return False
        # half-open: la sonde unique détient le volant — tout autre appelant
        # attend le verdict (record_success→closed / record_failure→open).
        return False

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
        """Increase backoff on failure.

        [plan v10 §14.3.6] off-by-one corrigé : l'incrément AVANT le calcul
        donnait base×mult¹ au premier échec (10s au lieu de 5s) — toute
        l'échelle était décalée d'un cran. Le 1er échec attend `base`."""
        self._consecutive_failures += 1
        self._current_delay = min(
            self._base_delay * (self._multiplier ** (self._consecutive_failures - 1)),
            self._max_delay,
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
# [P1 geo] aliases USA/UK/UAE added for geographic policy normalization
# (title-cased keys because _normalize_country title()s before lookup).
_COUNTRY_ALIASES = {
    "Czechia": "Czech Republic",
    "Usa": "United States",
    "USA": "United States",
    "Uk": "United Kingdom",
    "UK": "United Kingdom",
    "Uae": "United Arab Emirates",
    "UAE": "United Arab Emirates",
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
# (Phase 1b/1c dead in silence). [audit 18/08] the hostname line is
# "[<host>] Peer Connection Initiated with [AF_INET]<ip>:1194" (M_INFO,
# visible from the default verbosity 1) — never "Connecting to [<host>]".
_NORDVPN_HOST_RE = re.compile(r"[a-z]{2}[0-9]{2,4}\.nordvpn\.com")

# Blacklist TTL: the OpenVPN sunset kills hosts within a daily window — one
# wall-clock day covers it, and a host that starts working again simply
# expires back into rotation.
_FAILED_HOST_TTL = 24 * 3600


def _classify_probe_exc(exc: BaseException) -> str:
    """[Axe 1.3] Structural probe-exception classification -> verdict string.

    "timeout" (slow — grace phase applies) vs "refused" (definitive dead)
    vs "error" (unknown, treated as dead, no grace). STRUCTURAL on purpose:
    type-name + cause-chain + message inspection, never isinstance on
    httpx.* exceptions — the offline tests stub ``httpx`` with a fake
    module that has no httpx exception types (an isinstance there would
    AttributeError). A bare RuntimeError (fake or real) => "error".
    """
    name = type(exc).__name__.lower()
    if isinstance(exc, asyncio.TimeoutError) or name == "timeouterror":
        return "timeout"
    msg = str(exc).lower()
    if name.endswith("timeout") or "timed out" in msg:
        return "timeout"
    # warm-avalanche Q4: EOF = refused immédiat (pas de grace timeout)
    if "eof" in msg or "reading header" in msg or "connection closed" in msg:
        return "refused"
    if (
        isinstance(exc, ConnectionRefusedError)
        or "refused" in msg
        or "10061" in msg
        or "econnrefused" in msg
    ):
        return "refused"
    # Walk the cause chain: httpx/wireproxy tend to wrap the real socket
    # error (ConnectionRefusedError) one or two layers down.
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, ConnectionRefusedError):
            return "refused"
        cur = cur.__cause__
    return "error"


def _classify_error_kind(manager) -> dict:
    """Build error_detail for dashboard — kind enum + control/probe flags.

    kind ∈ {timeout, refused, auth_failed, tls, control_unreachable, budget_exceeded, error, none}
    """
    err = (getattr(manager, "_error", "") or "") + " " + (getattr(manager, "_last_rotation_error", "") or "")
    low = err.lower()
    if getattr(manager, "_auth_failed", False) or "auth_failed" in low:
        kind = "auth_failed"
    elif "timeout" in low or "délai dépassé" in low:
        kind = "timeout"
    elif "refused" in low or "refusé" in low or "10061" in low or "econnrefused" in low:
        kind = "refused"
    elif "tls" in low or "negotiation" in low or "négociation" in low:
        kind = "tls"
    elif "budget" in low or "probe" in low and "dead" in low:
        kind = "budget_exceeded"
    elif "control" in low and ("unreachable" in low or "injoignable" in low):
        kind = "control_unreachable"
    elif low.strip():
        kind = "error"
    else:
        kind = "none"
    # probe/control observability — cheap synchronous flags
    try:
        control_ok = bool(getattr(manager, "_control_api_key", "")) and bool(getattr(manager, "_control_enabled", False))
    except Exception:
        control_ok = False
    try:
        probe = getattr(manager, "_last_rotation_error", None) or getattr(manager, "_error", None)
    except Exception:
        probe = None
    return {"kind": kind, "probe": str(probe or "")[:200], "control_ok": control_ok}


def _extract_current_hostname(text: str) -> str | None:
    """Last NordVPN hostname present in gluetun log text.

    gluetun's SIGUSR1 retry loop re-logs the SAME remote every ~11 s, so
    the last "[<host>] Peer Connection Initiated ..." line is the host
    currently in play. [audit 18/08] that M_INFO line is visible from the
    DEFAULT verbosity (1) — OPENVPN_VERBOSITY=4 is NOT required.
    None when no hostname is logged.
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
        self._shared: SharedRotationState | None = shared
        # [plan v10 §4 Lot 6] overrides PER-STATION : `ip_rotation.per_station`
        # = {«2»: {quota_per_ip: 500, country_offset: 3}} — fusionné PAR-DESSUS
        # la base pour CETTE instance uniquement (copie, jamais la référence
        # live partagée). Hot-reload : update_config ré-applique le merge.
        _per = cfg.get("per_station") if isinstance(cfg.get("per_station"), dict) else None
        _ovr = None
        if _per:
            _ovr = _per.get(str(station)) or _per.get(station)
        if isinstance(_ovr, dict) and _ovr:
            cfg = {**cfg, **_ovr}
            logging.getLogger(__name__).info(
                "[vpn] per_station overrides appliqués st%s: %s", station, sorted(_ovr)
            )
        self._config = cfg
        self._station = station
        self._mode = "docker"  # sole mode: compose-managed gluetun (free_ip_pool compat)

        self._enabled = cfg.get("enabled", False)
        self._proxy_mode = cfg.get("proxy_mode", "vpn")  # vpn | socks5 | direct
        self._quota_per_ip = cfg.get("quota_per_ip", 300)
        self._switch_delay = cfg.get("switch_delay", 5)
        # [plan 18/08 §1] N-station suffix: station 1 keeps the legacy key
        # names, stations 2+ read ``_N``-suffixed keys with derived defaults
        # (opencode-vpn-{N} / vpn-gluetun-{N} / socks5 1079+N / http 8887+N).
        # Stations 1 and 2 behave exactly as before.
        suffix = "" if station <= 1 else f"_{station}"
        self._docker_container = cfg.get(
            f"docker_container{suffix}",
            f"opencode-vpn-{station}" if station > 1 else "opencode-vpn",
        )
        self._docker_compose_file = cfg.get("docker_compose_file", "docker-compose.yml")
        # The compose SERVICE name (not container name) used in `docker
        # compose -f … up -d <service>` / `pull <service>` invocations.
        self._compose_service = cfg.get(
            f"compose_service{suffix}", f"vpn-gluetun-{station}" if station > 1 else "vpn-gluetun"
        )
        # Per-station state file (station N must not clobber another
        # station's IP history/circuit breaker, and vice versa).
        self._state_file = cfg.get(
            f"state_file{suffix}",
            os.path.join(
                ROOT, "logs", f"vpn_state{station}.json" if station > 1 else "vpn_state.json"
            ),
        )
        self._proxy_port = cfg.get(
            f"vpn_proxy_port{suffix}", 8887 + station if station > 1 else 8888
        )
        self._socks5_port = cfg.get(
            f"socks5_proxy_port{suffix}", 1079 + station if station > 1 else 1080
        )
        self._auth_file = cfg.get(
            "credentials_file", os.path.join(ROOT, "vpn_configs", "credentials.txt")
        )
        self._server_countries = cfg.get("server_countries", "Germany")
        self._server_provider = cfg.get("server_provider", "nordvpn")  # gluetun update -providers
        self._ip_check_url = cfg.get("ip_check_url", "https://api.ipify.org")
        # IP-check fallback chain ([plan] C): ordered endpoints probed through
        # the SAME SOCKS5 tunnel with a sticky index (the endpoint that works
        # is kept until its first failure). Legacy direct mutations of
        # _ip_check_url (tests) stay honored — _probe_urls() falls back to
        # [self._ip_check_url, defaults] when ip_check_urls is absent.
        _urls = cfg.get("ip_check_urls")
        self._ip_check_urls = (
            [str(u).strip() for u in _urls if str(u).strip()] if isinstance(_urls, list) else []
        )
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
        # [plan v10 v6 §3.4] écart structurel par station (0 = legacy)
        self._country_offset_stride = max(0, int(cfg.get("country_offset_stride", 0) or 0))
        self._current_country: str | None = None  # pinned country (control server)
        self._country_pinned_at: float | None = None
        # local cursor fallback (no shared state) — décalé par offset effectif
        self._country_index = (
            self._country_offset + self._country_offset_stride * (self._station - 1)
        ) % 1000000
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
            os.path.dirname(self._compose_file_path()), "vpn_configs", "wireguard.env"
        )
        if self._stack == "wireguard":
            self._stack_effective: str | None = "wireguard"
        elif self._stack == "openvpn":
            self._stack_effective = "openvpn"
        else:  # auto
            # [drift-fix 25/08] En auto, la stack effective au boot suit le
            # .env PERSISTÉ par station — la MÊME source que
            # reconcile_orphan_containers ([plan v10 §14.1.1]). L'ancienne
            # heuristique « clé WG présente ⇒ WG » faisait que chaque
            # redémarrage du proxy réimposait WireGuard via l'env explicite
            # des chemins de recovery (_compose_env), écrasant une flotte
            # OpenVPN saine : c'est LE moteur du bug « stations 2-3/4 »
            # persistant quand le chemin WG du fournisseur est muet.
            # La clé ne décide plus que de la PERMISSION d'un retour WG
            # ultérieur — retour désormais validé par le canari egress.
            try:
                _boot_env = os.path.join(
                    os.path.dirname(self._compose_file_path()), ".env"
                )
                self._stack_effective = _stack_from_env_file(_boot_env, self._station)
            except Exception:
                self._stack_effective = None
            if self._stack_effective is None:
                self._stack_effective = (
                    "wireguard" if os.path.exists(self._wg_key_file) else "openvpn"
                )
        # OpenVPN protocol fallback (udp/tcp) — gluetun OPENVPN_PROTOCOL, defaults udp.
        # WG is always UDP 51820, OV can be udp 1194 or tcp 443/8443. The protocol
        # is the remote-port dimension for the OV stack; all OV stations share it
        # (global .env OPENVPN_PROTOCOL) and the watchdog cycles udp->tcp when
        # OV UDP is dead (firewall that filters UDP but passes TCP 443).
        _proto = str(cfg.get("ovpn_protocol", cfg.get("openvpn_protocol", "udp"))).lower()
        if _proto not in ("udp", "tcp"):
            _proto = "udp"
        self._ovpn_protocol = _proto  # selected
        self._ovpn_protocol_effective = _proto if self._stack_effective == "openvpn" else "udp"
        # warm-avalanche Q6: endpoint port cycle 1194→443→8443
        _port_cfg = str(cfg.get("ovpn_endpoint_port", cfg.get("endpoint_port", "1194" if _proto == "udp" else "443"))).strip()
        try:
            _port_int = int(_port_cfg)
            if _port_int not in (1194, 443, 8443):
                _port_int = 1194 if _proto == "udp" else 443
        except Exception:
            _port_int = 1194 if _proto == "udp" else 443
        self._ovpn_endpoint_port = str(_port_int)
        self._ovpn_endpoint_port_effective = str(_port_int) if self._stack_effective == "openvpn" else ("1194" if _proto == "udp" else "443")
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
        # [canari WG 25/08] Preuve d'egress AVANT tout retour en WireGuard :
        # le fournisseur peut black-holer WG en silence (handshake local OK,
        # zéro trafic — DNS interne mort), et le retour automatique « OV sain
        # → WG préféré » se faisait À L'AVEUGLE après auto_ov_return_min → la
        # flotte flappait indéfiniment tant que le chemin WG était cassé.
        # Le canari (compose vpn-wg-test, SOCKS5 loopback 1090) valide le
        # chemin WG RÉEL ; verdict TTL-caché pour ne pas marteler docker.
        self._wg_canary_state: dict = {"ok": None, "at": None}
        # [plan 20/08] Gluetun healthcheck-restart churn: a marginal WG
        # tunnel makes gluetun's INTERNAL healthcheck restart the VPN every
        # ~12 s — invisible to the SOCKS5 probe (it samples the live
        # windows). _check_restart_churn counts the restart marker in the
        # container logs over a sliding window; above the threshold the
        # egress watchdog arms and the existing recovery chain plays
        # (fresh country/IP → restart → flip).
        self._restart_churn_threshold = max(1, int(cfg.get("restart_churn_threshold", 4)))
        self._restart_churn_window_min = max(2, int(cfg.get("restart_churn_window_min", 10)))
        # Armed cadence: while failures are pending the watchdog probes every
        # _egress_failure_tick_interval seconds (light probe only — the docker
        # CLI refresh would blow past the cadence). ip_probe_budget bounds the
        # probe's SOCKS5 CONNECT (an httpx timeout does not, am.10).
        self._egress_failure_tick_interval = max(
            0.5, float(cfg.get("egress_failure_tick_interval", 2.0))
        )
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
        self._rotation_op_event: asyncio.Event | None = None
        self._rotation_loop: asyncio.AbstractEventLoop | None = None
        # Pool-signal observability (am.23): the last real connection-
        # failure report (time + count) and the skipped-tick trace — per
        # ROTATION TASK, not a global flag (a flag that resets on any normal
        # tick loses the "lost pool wake on rotation X" trace; a per-task
        # pointer re-logs for each distinct rotation).
        self._skipped_rotation_task: asyncio.Task | None = None
        self._last_conn_failure_at: float | None = None
        self._conn_failure_signal_count = 0
        # [plan 18/08 §3c] Auto-mode reliability counters. Clock is
        # INJECTABLE (_now_fn) so tests can advance time without patching
        # time.monotonic globally (process-wide side effect); production uses
        # time.monotonic.
        self._now_fn = time.monotonic
        self._auth_failed_window: list[float] = []  # monotonic ts, 30-min sliding
        self._last_auto_flip_at: float | None = None  # cooldown (monotonic)
        self._stack_since: float | None = None  # when the effective stack took over
        self._auto_ov_fail_threshold = max(1, int(cfg.get("auto_ov_fail_threshold", 3)))
        self._auto_ov_return_min = max(1, int(cfg.get("auto_ov_return_min", 60)))
        # Cooldown 0 = instantané (utilisateur: pas d'attente, toute station doit être dispo immédiatement)
        self._auto_flip_cooldown_min = max(0, int(cfg.get("auto_flip_cooldown_min", 0)))
        self._flips: list[dict] = []  # journal [{time, from, to, reason}], cap 20
        self._pending_flip: tuple | None = None  # set inside the tick lock,
        # applied AFTER it (_apply_stack takes the lock — not reentrant).
        self._last_flip_blocked_reason: str | None = None  # [Bug #4] why flip was blocked
        # warm-avalanche: 401 control + SOCKS5 EOF observability
        self._control_last_401_at: float | None = None
        self._control_last_error: str | None = None
        self._socks5_eof_count: int = 0
        self._socks5_eof_window: list[float] = []  # 5-min sliding
        self._ovpn_ports = ["udp:1194", "tcp:443", "tcp:8443"]
        self._ovpn_port_idx = 0
        self._auto_hetero_boot = bool(cfg.get("auto_hetero_boot", False))

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
            self._identity_profiles_base, self._identity_diversity, self._identity_max_profiles
        )
        self._identity_index = 0  # restored/clamped by load_state()
        self._rotation_task: asyncio.Task | None = None  # single-flight rotation ([1]+[18])
        # [plan v10 §14.1.2] downscale : abandon COOPÉRATIF d'une rotation en
        # vol avant stop_container (le shield de connect_next détache sinon
        # l'implémentation, qui recréerait le conteneur condamné).
        self._rotation_cancel_requested = False

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
        self._update_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        # Docker-events wake-up for the watchdog ([plan] C): created in
        # start() — inside the running loop — so a container die/restart is
        # acted on immediately instead of at the next interval.
        self._watchdog_event: asyncio.Event | None = None
        self._startup_connect_task: asyncio.Task | None = None
        self._update_available = False
        self._update_current_digest: str | None = None
        self._update_new_digest: str | None = None
        self._update_old_image_id: str | None = None
        self._update_known_since: float | None = None
        self._update_checked_at: str | None = None
        self._update_applied_at: str | None = None
        self._update_last_error: str | None = None
        self._last_free_request_at: float | None = None

        # Runtime state
        self._status = VPNState.DISCONNECTED
        self._error: str | None = None
        self._current_ip: str | None = None
        self._current_server: dict | None = None
        self._connected_at: float | None = None
        self._auth_failed = False
        self._server_issue = False  # TLS negotiation failure (stale/crashed server)
        # [plan 20/08] Healthcheck-restart loop visible in the container
        # logs (set by the refresh scan; self-resolves by window slide).
        # _restart_churn_recovered_at is the wall-clock boundary of the last
        # successful recovery: markers older than it are stale (docker logs
        # span recoveries, and WG logs no per-attempt success marker to
        # bound against) — the scan window snaps to after it so a healed
        # tunnel is not re-flagged from its own pre-recovery history.
        self._restart_churn = False
        self._restart_churn_recovered_at: float | None = None
        # [v10 §14.1.10] cadence du scan churn (recovery → re-scan immédiat)
        self._churn_next_due = 0.0
        self._total_switches = 0
        self._ip_history: list[dict] = []
        self._lock = asyncio.Lock()
        # Fail-fast rotation cooldown (CRITIC(6)): after a total rotation
        # failure, refuse new rotations for rotation_fail_cooldown
        # (default 300 s, config ships 60) — covers every rotation path
        # (ensure_connected, switch_ip, on_quota_exhausted, manual) instead
        # of the old per-caller timer in free_ip_pool only.
        self._ROTATION_FAIL_COOLDOWN = max(5.0, float(cfg.get("rotation_fail_cooldown", 300)))
        self._last_rotation_failed_at: float | None = None
        # Last rotation failure detail (G7): exposed via get_status — a dead
        # rotation must be visible in the dashboard/debug.log, not silent.
        self._last_rotation_error: str | None = None
        # [37] Dashboard cache: /api/vpn-status polls every 10 s and each
        # full refresh costs 2 docker subprocess calls + a tunnel HTTP probe.
        # Within _STATUS_CACHE_SECONDS of a completed refresh, refresh_status
        # returns the live in-memory state instead of re-probing docker.
        self._STATUS_CACHE_SECONDS = max(0.5, float(cfg.get("status_cache_seconds", 10.0)))
        self._last_status_refresh_at: float | None = None
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
        self._watchdog_escalated_at: float | None = None  # escalation re-arm: 30 min

        self.load_state()
        # [P5.4 perf] debounced save_state — évite copy2+dump sur la boucle event loop
        self._save_state_debounce = 1.0
        self._save_state_last = 0.0
        self._save_state_task: asyncio.Task | None = None
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
        if value in ("vpn", "socks5", "direct"):
            self._proxy_mode = value

    @property
    def status(self) -> str:
        return self._status

    @property
    def current_ip(self) -> str | None:
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
        if (
            not self._identity_rotation_enabled
            or self._proxy_mode != "vpn"
            or not self._enabled
            or self._status != VPNState.CONNECTED
            or len(self._identity_profiles) <= 1
        ):
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
    def current_server(self) -> dict | None:
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
        return os.environ.get("VPN_DOCKER_COMPOSE_FILE") or os.path.join(
            ROOT, self._docker_compose_file
        )

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
        if self._enabled and self._proxy_mode == "vpn" and self._status != VPNState.CONNECTED:
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
                "retried on first free request / manual connect: %s",
                e,
            )

    async def stop(self) -> None:
        """Shutdown: persist state only. The tunnel is left running
        (it is compose-managed and survives proxy restarts).

        [Revue 19/08] ``_enabled`` is flipped to False: on the downscale
        path (stop() then stop_container()) a rotation still aimed at this
        manager — a shielded orphan, or a 429 that raced the pool-side
        retire — must refuse any docker work. ``_connect_next_impl`` raises
        RotationFailed when ``not self._enabled`` (VPN disabled gate), so
        this makes the existing guard effective for retiring managers. A
        fresh upscale builds NEW VPNManager instances (opencode
        ``_apply_station_count``), so the retired manager is never
        re-enabled."""
        if self._startup_connect_task:
            self._startup_connect_task.cancel()
            self._startup_connect_task = None
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        if self._update_task:
            self._update_task.cancel()
            self._update_task = None
        self._enabled = False
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
        result = await asyncio.to_thread(self._docker_run,
            ["compose", "-f", compose_file, "stop", self._compose_service],
            120,
            env=self._compose_env(),
        )
        if result.returncode != 0:
            logger.warning(
                "[vpn] compose stop failed (continuing to rm): %s",
                result.stderr.strip() or result.stdout.strip(),
            )
        # 2) actually delete the container (keep the volume). "No such
        #    container" is success — already removed by an earlier pass.
        rm = await asyncio.to_thread(self._docker_run, ["rm", "-f", self._docker_container], 120)
        if rm.returncode != 0 and "No such container" not in (rm.stderr or ""):
            raise RuntimeError(f"échec suppression docker : {rm.stderr.strip() or rm.stdout.strip()}")

    async def connect(self) -> None:
        """Bring the tunnel up: compose up + wait healthy + record IP."""
        async with self._lock:
            if self._status == VPNState.CONNECTED and self._current_ip:
                return
            if not VPNState.can_transition(self._status, VPNState.CONNECTING):
                raise RuntimeError(f"Transition impossible de {self._status} vers connecting")
            self._set_status(VPNState.CONNECTING)
            self._error = None
            try:
                await self._compose_up()
                started_at = await self._wait_healthy(timeout=120)
                if started_at is None:
                    raise RuntimeError("gluetun non sain dans les 120s")
                if await self._check_auth_failed(started_at):
                    self._auth_failed = True
                    self._current_ip = None  # stale IP must not be served ([5])
                    raise RuntimeError("AUTH_FAILED - identifiants NordVPN rejetés")
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
                    raise RuntimeError("impossible de finaliser une nouvelle IP via le tunnel")
                self._connected_at = time.monotonic()
                self._set_status(VPNState.CONNECTED)
                self._current_server = {
                    "name": self._docker_container,
                    "country": self._current_country or self._server_countries,
                }
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
                self._error = "connexion annulée"
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
                    f"cooldown rotation actif ({self._ROTATION_FAIL_COOLDOWN - int(since)}s restant)"
                )
        # Circuit breaker gate ([25]): no rotation while the breaker is open.
        if not self._circuit_breaker.is_available(self._docker_container):
            raise RotationFailed("circuit breaker ouvert — rotation ignorée")
        task = asyncio.create_task(self._connect_next_impl())
        self._rotation_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._rotation_task is task:
                self._rotation_task = None

    async def request_rotation_cancel(self, cap: float = 5.0) -> None:
        """[v10 §14.1.2] Appelé par le downscale AVANT stop_container : pose
        le flag coopératif (checké aux checkpoints de _connect_next_impl) puis
        attend la fin de la rotation en vol (cap `cap` s). Aucun CancelledError
        sauvage chez les callers 429 — la rotation sort par RotationFailed."""
        self._rotation_cancel_requested = True
        task = getattr(self, "_rotation_task", None)
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=cap)
            except Exception:
                pass  # timeout/RotationFailed/cancel : on rend la main au teardown
        self._rotation_cancel_requested = False

    async def _connect_next_impl(self) -> str:
        """Actual rotation: compose up if the container is absent,
        else restart it. Validates the new IP (different from current and
        not in the last 10) with 3 attempts + backoff.

        Returns the new IP on success; raises RotationFailed otherwise
        (CRITIC(5)).
        """
        async with self._lock:
            if not self._enabled:
                raise RotationFailed("VPN désactivé — rotation impossible")
            if not VPNState.can_transition(self._status, VPNState.CONNECTING):
                raise RuntimeError(f"Transition impossible de {self._status} vers connecting")

            old_ip = self._current_ip
            last_error: Exception | None = None
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
                # [plan v10 §14.1.2] checkpoint d'abandon coopératif : un
                # downscale en cours ne doit pas voir sa station recréée.
                if self._rotation_cancel_requested:
                    raise RotationFailed("rotation annulée (downscale en cours)")
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
                if self._rotation_cancel_requested:
                    raise RotationFailed("rotation annulée (downscale en cours)")
                if pinned is None:
                    await self._ensure_container()
                    started_at = await self._wait_healthy(timeout=120)
                    if self._rotation_cancel_requested:
                        raise RotationFailed("rotation annulée (downscale en cours)")
                    if started_at is None:
                        raise RuntimeError("gluetun non sain après redémarrage")
                    if await self._check_auth_failed(started_at):
                        self._auth_failed = True
                        self._current_ip = None  # stale IP must not be served ([5])
                        raise RuntimeError("AUTH_FAILED après redémarrage")
                    self._auth_failed = False
                else:
                    # The pin polled status:running inside the control
                    # server — the tunnel is up; no container action.
                    self._auth_failed = False

                # Let the new tunnel stabilize before probing the IP
                await asyncio.sleep(self._switch_delay * (random.uniform(0.8, 1.2) if not os.getenv("PYTEST_CURRENT_TEST") else 1.0))  # [P3.5] jitter ±20% healthy

                new_ip = await self.get_public_ip()
                if not new_ip:
                    raise RuntimeError("impossible de déterminer l'IP publique")
                if self._rotation_cancel_requested:
                    raise RotationFailed("rotation annulée (downscale en cours)")
                # [review 18/08 §C] the probe ANSWERED — the tunnel
                # lives. Clear the latch: a later "IP unchanged"/
                # "recently used" failure is the lottery, not death,
                # and must not arm behind an earlier dead probe (the
                # final WARN would overstate — attempt 1 did answer).
                self._rotation_probe_dead = False
                if old_ip and new_ip == old_ip:
                    raise RuntimeError(f"IP inchangée après redémarrage ({new_ip})")
                if self._ip_recent(new_ip) and attempt < 2:
                    raise RuntimeError(f"IP {new_ip} récemment utilisée")

                # Success — advance the identity BEFORE journalizing so
                # the history entry carries the NEW face ([plan] C.2 —
                # the old order logged the pre-advance identity).
                self._current_ip = new_ip
                self._current_server = {
                    "name": self._docker_container,
                    "country": self._current_country or self._server_countries,
                }
                self._connected_at = time.monotonic()
                self._set_status(VPNState.CONNECTED)
                self._total_switches += 1
                self._record_ip_change(new_ip)
                self._advance_identity()
                self._ip_history.append(
                    {
                        "ip": new_ip,
                        "server": self._docker_container,
                        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "identity": self._live_identity.get("impersonate") or "",
                        "identity_index": self._identity_index,
                    }
                )
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
                        last_error = last_error or RuntimeError("délai de rotation dépassé")
                        break
                    remaining = deadline - self._now_fn()
                    self._set_status(VPNState.CONNECTING)
                    self._error = None
                    try:
                        new_ip = await asyncio.wait_for(_attempt(), max(1.0, remaining))
                    except asyncio.CancelledError:
                        # Client disconnect mid-rotation must not leave the
                        # state stuck in CONNECTING ([17]).
                        self._set_status(VPNState.ERROR)
                        self._error = "rotation annulée par déconnexion client"
                        raise
                    except TimeoutError:
                        # The attempt burnt its budget. am.8: whatever
                        # docker op it left running is NOT finished
                        # (to_thread cannot be cancelled) — wait for its
                        # real end so the next attempt (or the repair rung)
                        # never races it. The loop-top check then decides:
                        # budget left → next attempt, else bail.
                        deadline_hit = True
                        last_error = RuntimeError("délai de rotation dépassé")
                        self._circuit_breaker.record_failure(self._docker_container)
                        self._backoff.record_failure()
                        logger.warning(
                            "[vpn] rotation attempt %d/3 hit the %.0f s wall — "
                            "draining in-flight docker op",
                            attempt + 1,
                            self._rotation_max_duration,
                        )
                        await self._await_rotation_ops_drained()
                    except Exception as e:
                        last_error = e
                        if "could not determine public IP" in str(e) or "impossible de déterminer l'IP publique" in str(e):
                            # The rotation died asking for a REAL IP through
                            # the tunnel — the tunnel itself is dead (the
                            # 445 s stall class, incident 18/08 S2). NOT set
                            # on "IP unchanged"/"recently used": those prove
                            # the tunnel answers.
                            self._rotation_probe_dead = True
                        self._circuit_breaker.record_failure(self._docker_container)
                        self._backoff.record_failure()
                        logger.warning("[vpn] rotation attempt %d/3 failed: %s", attempt + 1, e)
                        if attempt < 2:
                            # Cap the retry pause to the remaining wall so a
                            # late backoff (≤ 60 s) cannot push the rotation
                            # past its deadline. Recompute against the LIVE
                            # wall: the pre-try `remaining` predates this
                            # failed attempt and could overshoot it by up to
                            # the attempt's own duration (review 18/08).
                            remaining = max(0.0, deadline - self._now_fn())
                            await asyncio.sleep(min(self._backoff.delay, max(0.05, remaining)))
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
                    f"Rotation IP abandonnée après {self._rotation_max_duration:.0f}s"
                    f" (délai dépassé ; dernier : {last_error})"
                )
            else:
                self._error = f"Échec rotation IP après 3 tentatives (dernier : {last_error})"
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
                self._egress_failures = max(self._egress_failures, self._auto_wg_egress_ticks)
                if self._watchdog_event is not None:
                    self._watchdog_event.set()  # loop wait(timeout) returns → live tick
                logger.warning(
                    "[vpn] rotation died on a dead tunnel — egress watchdog "
                    "armed (real IP probe never answered)"
                )
            raise RotationFailed(self._error)

    # [P6] connect_wait supprimé : 0 appelant prod (audit 2026-08-26) —
    # le chemin externe passe par _wait_healthy / _finalize_ip.

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
        """Broadcast the VPN event to SSE subscribers. Fail-open:
        dashboard module import or publish errors must never crash a VPN
        state transition.

        [P4.4 perf] payload compact {station,status,ip,server} au lieu du
        `get_status()` complet — consommateur vérifié `app.js:2692-2718`
        garde `station||status||vpn_status` puis re-fetch, donc compact sûr."""
        try:
            from dashboard.events import get_event_manager

            compact = {
                "station": self._station,
                "status": str(self._status),
                "ip": self._current_ip,
                "server": self._current_server.get("name") if isinstance(self._current_server, dict) and self._current_server else None,
            }
            get_event_manager().publish("vpn_event", compact)
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
            # [P3 perf] compteur d'events depuis le dernier tick watchdog :
            # 0 → le tick sain saute le refresh docker lourd (inspect+logs),
            # la détection temps réel restant assurée par CE watcher.
            self._docker_events_since_tick = getattr(
                self, "_docker_events_since_tick", 0
            ) + 1
            if self._watchdog_event is not None:
                self._watchdog_event.set()  # not awaited -- simply wake the loop
            status = event.get("status")
            if status in ("die", "stop", "kill") and self._status == VPNState.CONNECTED:
                self._set_status(VPNState.ERROR)
                self._error = f"événement conteneur {status} -- VPN hors ligne"
        except Exception as e:
            logger.debug("[vpn] container event handling failed: %s", e)

    # ── Status & health ────────────────────────────────────────

    def _auth_check_supports_text(self) -> bool:
        """[v10 §14.1.10] True si ``_check_auth_failed`` accepte le kwarg
        ``text`` (fetch partagé). Mémoïsé par instance — les stubs de test
        sans le paramètre déclenchent le chemin historique automatiquement."""
        flag = getattr(self, "_auth_check_text_flag", None)
        if flag is None:
            import inspect as _inspect

            try:
                flag = "text" in _inspect.signature(self._check_auth_failed).parameters
            except (TypeError, ValueError):
                flag = False
            try:
                self._auth_check_text_flag = bool(flag)
            except Exception:
                pass
        return bool(flag)

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
        if (
            not force
            and self._last_status_refresh_at is not None
            and time.monotonic() - self._last_status_refresh_at < self._STATUS_CACHE_SECONDS
        ):
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
            self._error = "conteneur arrêté"
            return self.get_status()

        # [plan v10 §14.1.10] un SEUL docker logs pour auth+server_issue
        # (même fenêtre started_at) — budget subprocess watchdog ÷2.
        # Détection de signature : les sous-classes/stubs qui n'exposent pas
        # ``text`` retombent sur les appels séparés historiques.
        _started_at = info.get("started_at", "")
        if _started_at and self._auth_check_supports_text():
            _res = await asyncio.to_thread(self._docker_run, ["logs", "--since", _started_at, self._docker_container], 30
            )
            _shared_log = _res.stdout if _res.returncode == 0 else None
            auth_failed = await self._check_auth_failed(_started_at, text=_shared_log)
            server_issue = (
                await self._check_server_issue(_started_at, text=_shared_log)
                if not auth_failed
                else False
            )
        else:
            auth_failed = await self._check_auth_failed(_started_at)
            server_issue = (
                await self._check_server_issue(_started_at) if not auth_failed else False
            )
        if auth_failed or server_issue:
            self._auth_failed = auth_failed
            self._server_issue = server_issue
            self._set_status(VPNState.ERROR)
            self._error = (
                "AUTH_FAILED - identifiants NordVPN rejetés"
                if auth_failed
                else "Serveur VPN injoignable - échec négociation TLS (liste serveurs obsolète ?)"
            )
            self._current_ip = None  # stale IP must not be served ([5])
            logger.error("[vpn] %s", self._error)
            return self.get_status()
        self._auth_failed = False
        self._server_issue = False

        # [plan 20/08] Gluetun healthcheck-restart churn: the SOCKS5 egress
        # probe samples the LIVE windows of a marginal tunnel and stays
        # silent while the internal healthcheck loops restarts (~12 s).
        # The log scan sees the loop; arming the egress watchdog routes it
        # into the existing recovery chain (fresh IP/country → restart →
        # flip). Not an ERROR state: the tunnel may be answering right now.
        # [v10 §14.1.10] scan CADENCÉ à 60 s (le plus coûteux : full logs) —
        # un recovery force un re-scan immédiat via _churn_next_due=0.
        if self._now_fn() >= self._churn_next_due:
            churn = await self._check_restart_churn(self._restart_churn_window_min)
            self._churn_next_due = self._now_fn() + 60.0
        else:
            churn = self._restart_churn  # état courant conservé entre scans
        self._restart_churn = churn
        if churn:
            self.arm_egress_watchdog()

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
                        "country": self._current_country or self._server_countries,
                    }
                    return self.get_status()
            elif ctl is False:
                # gluetun itself reports the VPN stopped — honest error, no
                # SOCKS5 probe needed.
                self._set_status(VPNState.ERROR)
                self._error = "serveur de contrôle gluetun signale VPN arrêté"
                return self.get_status()
            # ctl is None (control server unreachable) → fall through to the
            # SOCKS5 probe below; "can't ask" must not read as "stopped".
            # warm-avalanche Q8 garde défensive: 401 récent (<30s) arme watchdog même si ctl is None
            if self._control_last_401_at and time.time() - self._control_last_401_at < 30:
                logger.debug("[vpn] refresh_status ctl None but 401 <30s → arm watchdog station %s", self._station)
                self.arm_egress_watchdog()
                try:
                    if self._watchdog_event is not None:
                        self._watchdog_event.set()
                except Exception:
                    pass

        ip = await self.get_public_ip()
        if ip:
            self._current_ip = ip
            self._connected_at = time.monotonic()
            self._set_status(VPNState.CONNECTED)
            self._error = None
            self._current_server = {
                "name": self._docker_container,
                "country": self._current_country or self._server_countries,
            }
        else:
            self._set_status(VPNState.ERROR)
            self._error = "conteneur actif mais tunnel sans réponse"
        return self.get_status()

    async def health_check(self) -> dict:
        """Probe the tunnel through SOCKS5 and measure latency."""
        result: dict[str, Any] = {"ok": False, "ip_changed": False, "latency_ms": None, "error": None}
        if self._status != VPNState.CONNECTED:
            result["error"] = "Non connecté"
            return result
        try:
            import httpx

            start = time.monotonic()
            new_ip = None
            elapsed_ms = None
            async with httpx.AsyncClient(timeout=15, proxy=self.socks5_url) as client:
                # [plan 18/08 §A] same stall class as get_public_ip: an
                # httpx timeout does NOT bound a stuck SOCKS5 CONNECT —
                # wait_for ip_probe_budget; the except below absorbs the
                # TimeoutError into the error result.
                # [plan v10 §14.3.4] même CHAÎNE sticky que get_public_ip
                # (fallback complet) au lieu d'un endpoint legacy unique qui
                # déclarait la station morte dès que CE endpoint tombait.
                urls = list(getattr(self, "_ip_check_urls", None) or [self._ip_check_url])
                for _url in urls:
                    try:
                        resp = await asyncio.wait_for(
                            client.get(_url), max(1.0, self._ip_probe_budget / len(urls))
                        )
                    except Exception:
                        continue
                    candidate = resp.text.strip()
                    if candidate:
                        new_ip = candidate
                        elapsed_ms = int((time.monotonic() - start) * 1000)
                        break
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
                        logger.warning(
                            "[vpn] health check: IP changed %s → %s", self._current_ip, new_ip
                        )
                        self._current_ip = new_ip
                        # The tunnel re-picked an IP outside a rotation — keep
                        # the shared registry and identity in sync so the next
                        # rotation never re-enters it and the history shows the
                        # new face ([plan] C.5).
                        self._record_ip_change(new_ip)
                        self._advance_identity()
                        self._ip_history.append(
                            {
                                "ip": new_ip,
                                "server": self._docker_container,
                                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "identity": self._live_identity.get("impersonate") or "",
                                "identity_index": self._identity_index,
                            }
                        )
                        self._ip_history = self._ip_history[-100:]
                        self.save_state()
            else:
                result["error"] = "Impossible de déterminer l'IP"
        except Exception as e:
            result["error"] = str(e)
            logger.error("[vpn] health check failed: %s", e)
        return result

    def _get_probe_client(self):
        """[P3 perf] httpx client RÉUTILISABLE par socks5_url pour les probes
        IP — l'ancien code recréait un AsyncClient (handshake SOCKS5+TLS) par
        GET. Caché par instance ; les vieux clients d'une URL remplacée sont
        fermés en fire-and-forget."""
        import httpx

        cache = getattr(self, "_probe_clients", None)
        if cache is None:
            cache = {}
            self._probe_clients = cache
        key = self.socks5_url or ""
        c = cache.get(key)
        if c is not None and getattr(c, "is_closed", False):
            c = None
        if c is None:
            c = httpx.AsyncClient(timeout=5, proxy=self.socks5_url or None)
            cache[key] = c
            # borne le cache (rotations multi-URL) — ferme les anciens
            while len(cache) > 4:
                old_url, old_c = next(iter(cache.items()))
                if old_url == key:
                    break
                try:
                    del cache[old_url]
                    asyncio.get_running_loop().create_task(old_c.aclose())
                except Exception:
                    break
        return c

    async def _probe_url(self, url: str, *, per_attempt: float = 2.0) -> str | None:
        """[plan 18/08 §A / prancy-unicorn §2] One bounded IP GET through the
        SOCKS5 tunnel — the parallel unit of get_public_ip. [P3 perf] client
        partagé réutilisable : plus de handshake SOCKS5+TLS par probe.
        [unicorn] per-probe wait_for(min(2.0, budget)) so a stuck SOCKS
        CONNECT (445 s stall) never leaks past httpx timeout=5.
        Returns the IP text (None on any failure)."""
        try:
            # [unicorn] httpx timeout alone does NOT bound SOCKS CONNECT
            # — wrap the GET in wait_for(per_attempt) (min(3.0, budget))
            per_attempt = max(0.5, min(3.0, float(per_attempt or 3.0)))
            client = self._get_probe_client()
            resp = await asyncio.wait_for(client.get(url), timeout=per_attempt)
            ip = resp.text.strip()
            if ip:
                logger.debug("[vpn] probe ok %s via %s", url, self.socks5_url)
            return ip or None
        except TimeoutError:
            logger.debug("[vpn] probe timeout %s (%.1fs) via %s", url, per_attempt, self.socks5_url)
            return None
        except Exception as e:
            logger.debug("[vpn] probe fail %s via %s: %s", url, self.socks5_url, e)
            return None

    async def get_public_ip(self) -> str | None:
        """Query the public IP through the SOCKS5 tunnel (127.0.0.1:1080).

        Tries the ordered ``ip_check_urls`` chain (``ip_check_url`` stays
        the legacy alias — entry 1 when no chain is configured), every
        endpoint through the SAME tunnel: resolving outside it would
        falsify rotation validation.
        [P3 perf] endpoint STICKY d'abord en séquentiel (1 seul GET dans le
        cas nominal) ; le sweep parallèle borné par ``ip_probe_budget`` ne
        sert qu'en fallback quand l'endpoint sticky échoue. Le sticky index
        reste la source d'ordre ; un échec complet réinitialise l'index.
        Returns None only when every endpoint failed — never "unknown" as
        a success value.
        """
        urls = self._ip_check_urls or [self._ip_check_url]
        base = self._ip_check_idx % len(urls)
        # 1) Endpoint sticky seul — le cas nominal (tunnel sain, endpoint up)
        # coûte exactement UN GET.
        ip = await self._probe_url(urls[base])
        if ip:
            return ip
        # 2) Fallback : sweep des AUTRES endpoints en parallèle, budget borné.
        others = [(base + i) % len(urls) for i in range(1, len(urls))]
        if not others:
            self._ip_check_idx = 0
            logger.error(
                "[vpn] public IP probe failed on %d endpoint(s) via SOCKS5", len(urls)
            )
            return None
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(self._probe_url(urls[j]) for j in others)),
                max(2.0, self._ip_probe_budget),
            )
        except TimeoutError:
            results = []  # sweep cancelled → total failure
        for k, j in enumerate(others):
            if k >= len(results):
                break  # cancelled sweep → the total-failure tail
            if results[k]:
                self._ip_check_idx = j
                return results[k]
        self._ip_check_idx = 0  # full sweep failed — restart at the top next call
        logger.error("[vpn] public IP probe failed on all %d endpoints via SOCKS5", len(urls))
        return None

    async def _probe_tunnel_light(self) -> bool:
        """[plan 18/08 §E1/am.10 / Axe 1.3] Light egress probe — SOCKS5
        handshake + CONNECT to the ip_check endpoint (same chain as
        get_public_ip). NO GET: dead/alive is all we need — _finalize_ip
        does the full check once the tunnel is back. Single authority for
        egress_dead on BOTH stacks: the incident OV tunnel was "connected"
        with no AUTH_FAILED/TLS marker, invisible without a probe. Never
        called without an explicit wait_for budget: an httpx timeout does
        NOT bound the SOCKS5 CONNECT (the 445 s stall bug class).

        [Axe 1.3] Two-phase: phase 1 = quick rotated sweep (per_attempt
        min(3.0, budget)) that distinguishes CONNECT-REFUSED (tunnel dead,
        definitive) from TIMEOUT (tunnel slow — may still be alive). On
        timeout-only failure, phase 2 = one grace attempt (remaining
        budget, only if > 0.5 s) on the sticky-first endpoint before
        declaring death — a slow-but-alive tunnel is no longer bad-marked
        as dead.
        """

        try:
            urls = self._ip_check_urls or [self._ip_check_url]
            # [review F2] the single-endpoint probe would false-death a
            # healthy tunnel when the ip_check endpoint itself is down.
            # Rotated sweep over ALL endpoints, sticky-first, bounded PER
            # attempt: `min(3.0, budget)` keeps the worst case under budget
            # across the sweep (n endpoints × per_attempt).
            per_attempt = min(3.0, self._ip_probe_budget)
            base = self._ip_check_idx
            timeout_seen = False
            for i in range(len(urls)):
                url = urls[(base + i) % len(urls)]
                verdict = await self._probe_connect(url, per_attempt=per_attempt)
                if verdict == "ok":
                    if i > 0:
                        self._ip_check_idx = (base + i) % len(urls)
                    return True
                if verdict == "timeout":
                    timeout_seen = True
                # "refused"/"error" are definitive — no grace phase for
                # them (full sweep continues; a later endpoint may be ok).
            if timeout_seen:
                # Grace: one attempt on the sticky-first endpoint with the
                # REMAINING budget (leftover > 0.5 s is worth a try). A
                # slow tunnel that answers here was never dead.
                used = len(urls) * per_attempt
                remaining = self._ip_probe_budget - used
                if remaining > 0.5:
                    verdict = await self._probe_connect(
                        urls[(base + 0) % len(urls)], per_attempt=remaining
                    )
                    if verdict == "ok":
                        return True
            return False
        except Exception:
            return False

    async def _probe_connect(self, url: str, *, per_attempt: float) -> str:
        """[review F2 + Axe 1.3] One bounded SOCKS5 handshake + CONNECT
        toward ``url``. NO GET: dead/alive is all we need — _finalize_ip
        does the full check once the tunnel is back. Never called without
        an explicit wait_for budget: an httpx timeout does NOT bound the
        SOCKS5 CONNECT (the 445 s stall bug class).

        Returns a VERDICT string instead of bool [Axe 1.3]:
        - "ok"      — CONNECT succeeded (tunnel alive)
        - "refused" — connection refused (definitive dead)
        - "timeout" — slow/unresponsive (may just be slow — grace applies)
        - "error"   — anything else (treated as dead, no grace: the
          classification is structural on type-name/cause-chain so the F2
          tests' fake httpx module (no httpx exception types) still works.
        """
        import httpx

        # warm-avalanche Etape2: GET via socks5 (laisse httpcore[socks] faire SOCKS handshake)
        # Ne jamais utiliser CONNECT via proxy=socks5:// (double sémantique HTTP proxy vs SOCKS)
        # Un seul wait_for (pas de double httpx.Timeout + asyncio.wait_for)
        try:
            # vérifie httpcore[socks] installé, sinon proxy=socks5 ouvre TCP direct (faux positif)
            import httpcore  # noqa: F401

            _has_socks = True
        except ImportError:
            _has_socks = False
            logger.debug("[vpn] httpcore[socks] non installé — probe via socks5 peut être direct")
        # [P2.6+P3.3 perf] réutilise le client poolé (_get_probe_client) — plus de handshake SOCKS5+TLS par probe
        # Le bornage par tentative passe déjà par wait_for(per_attempt) (outer), timeout du client reste 5 s.
        client = self._get_probe_client()
        try:
            # GET via SOCKS (httpcore fait SOCKS handshake + DNS via proxy) — compatible FakeHttpx.send
            resp = await asyncio.wait_for(client.send(httpx.Request("GET", url)), timeout=per_attempt)
            if getattr(resp, "status_code", 200) < 500:
                return "ok"
            return "error"
        except Exception as e:
            verdict = _classify_probe_exc(e)
            # filets supplémentaires pour tunnel mort DNS (second mode)
            _msg = str(e).lower()
            if "server misbehaving" in _msg or "i/o timeout" in _msg:
                verdict = "refused"
            if verdict == "refused" and ("eof" in _msg or "reading header" in _msg):
                try:
                    now = time.time()
                    self._socks5_eof_window.append(now)
                    self._socks5_eof_window = [t for t in self._socks5_eof_window if now - t < 300]
                    self._socks5_eof_count = len(self._socks5_eof_window)
                    if self._socks5_eof_count >= 4:
                        logger.warning("[vpn] socks5 EOF burst x%d station %s — egress dead", self._socks5_eof_count, self._station)
                        self.arm_egress_watchdog()
                        try:
                            if self._watchdog_event is not None:
                                self._watchdog_event.set()
                        except Exception:
                            pass
                except Exception:
                    pass
            return verdict

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
                    started_at = await self._wait_healthy(timeout=self._rotation_recovery_timeout)
                    if started_at:
                        auth = await self._check_auth_failed(started_at)
                        tls = await self._check_server_issue(started_at)
                        if auth or tls:
                            self._auth_failed = auth
                            self._server_issue = tls
                            logger.warning(
                                "[vpn] finalize recovery blocked: %s",
                                "AUTH_FAILED" if auth else "TLS negotiation timeout",
                            )
                    # The container action reset the country pool — re-pin so
                    # the recovery doesn't undo the rotation ([plan] A). The
                    # cursor always advances, so this picks a NEW country.
                    await self._pin_country_for_rotation()
                    await asyncio.sleep(self._switch_delay * (random.uniform(0.8, 1.2) if not os.getenv("PYTEST_CURRENT_TEST") else 1.0))  # [P3.5] jitter ±20% healthy
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
        self._ip_history.append(
            {
                "ip": new_ip,
                "server": self._docker_container,
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "identity": self._live_identity.get("impersonate") or "",
                "identity_index": self._identity_index,
            }
        )
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
            # [plan 19/08 §1/§2] free multi-attempt cap + exception ordering —
            # read from the config mirror (persisted selection, hot-reload).
            _free_attempts = _cfg_data.get("ip_rotation", {}).get("max_free_attempts", 2)
            _exc_fallback = _cfg_data.get("ip_rotation", {}).get(
                "free_exception_fallback", "station-first"
            )
            # [free_parallel] Stations free parallel routing (persisted)
            _free_parallel = _cfg_data.get("ip_rotation", {}).get("free_parallel", {})
            if not isinstance(_free_parallel, dict):
                _free_parallel = {}
            _on_429_action = str(_cfg_data.get("ip_rotation", {}).get("on_429_action", "both") or "both").strip().lower()
            if _on_429_action not in ("cooldown", "rotate", "both"):
                _on_429_action = "both"
            try:
                _bad_ttl = int(_cfg_data.get("ip_rotation", {}).get("bad_ttl", 60))
            except Exception:
                _bad_ttl = 60
            _bad_ttl = max(1, min(3600, _bad_ttl))
        except Exception:
            _dual = _strict = False
            _vpn_stack = "auto"
            _station_count = 2 if _dual else 1
            _free_attempts = 2
            _exc_fallback = "station-first"
            _free_parallel = {}
            _on_429_action = "both"
            _bad_ttl = 60
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
            "shared_rotation_file": self._config.get(
                "shared_rotation_file", "logs/shared_rotation.json"
            ),
            "dual_station": _dual,
            "strict_free": _strict,
            # [plan 18/08 §3d] stack selection (auto/wireguard/openvpn) —
            # read from the config mirror so the dashboard reflects what was
            # persisted, like dual_station above.
            "vpn_stack": _vpn_stack,
            "ovpn_protocol": getattr(self, "_ovpn_protocol", "udp"),
            "ovpn_protocol_effective": getattr(self, "_ovpn_protocol_effective", "udp"),
            # [plan 18/08 §1] parallel station count (1-10, resolved from
            # station_count / dual_station — same canonical value the
            # dropdown posts back).
            "station_count": _station_count,
            # [plan 19/08 §1/§2] free multi-attempt cap (1-3) + exception
            # ordering (station-first / direct) — same mirror pattern.
            "max_free_attempts": _free_attempts,
            "free_exception_fallback": _exc_fallback,
            # [free_parallel] Stations free parallel routing (persisted)
            "free_parallel": _free_parallel,
            "on_429_action": _on_429_action,
            "bad_ttl": _bad_ttl,
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
        if "proxy_mode" in updates and updates["proxy_mode"] in ("vpn", "socks5", "direct"):
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
        if "country_offset_stride" in updates:
            self._country_offset_stride = max(
                0, int(updates.get("country_offset_stride", 0) or 0)
            )
        # [v10 §4 Lot 6] per_station hot-reload : les overrides de CETTE
        # station sont re-fusionnés par-dessus les updates globaux.
        _per = updates.get("per_station") if isinstance(updates.get("per_station"), dict) else None
        if _per:
            _ovr = _per.get(str(self._station)) or _per.get(self._station)
            if isinstance(_ovr, dict):
                for k, v in _ovr.items():
                    self._config[k] = v
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
                "circuit_breaker_threshold", self._circuit_breaker._failure_threshold
            )
            recovery = updates.get("circuit_breaker_recovery", self._circuit_breaker._recovery_time)
            self._circuit_breaker = CircuitBreaker(
                failure_threshold=int(threshold), recovery_time=float(recovery)
            )
        if "backoff_max_delay" in updates:
            self._backoff = BackoffTimer(
                base_delay=self._switch_delay, max_delay=float(updates["backoff_max_delay"])
            )
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
            ("rotation_max_duration", float, 5.0),
        ):
            if _key not in updates:
                continue
            try:
                setattr(self, f"_{_key}", max(_floor, _conv(updates[_key])))
            except (TypeError, ValueError):
                logger.warning("[vpn] ignored invalid hot-reload %s=%r", _key, updates[_key])
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
            self._identity_profiles_base = _normalize_identity_profiles(
                updates["identity_profiles"]
            )
        if any(
            k in updates
            for k in ("identity_diversity", "identity_max_profiles", "identity_profiles")
        ):
            # Rebuild from the explicit base (seed), not from the current pool.
            self._identity_profiles = _build_identity_pool(
                self._identity_profiles_base,
                self._identity_diversity,
                self._identity_max_profiles,
            )
            if self._identity_profiles:
                self._identity_index %= len(self._identity_profiles)  # clamp (config may shrink)
        if "watchdog_backoff_base" in updates or "watchdog_backoff_max" in updates:
            _wbase = max(
                1.0, float(updates.get("watchdog_backoff_base", self._watchdog_backoff._base_delay))
            )
            _wmax = max(
                _wbase,
                float(updates.get("watchdog_backoff_max", self._watchdog_backoff._max_delay)),
            )
            self._watchdog_backoff = BackoffTimer(base_delay=_wbase, max_delay=_wmax)
        if "ovpn_protocol" in updates or "openvpn_protocol" in updates:
            _p = str(updates.get("ovpn_protocol", updates.get("openvpn_protocol", "udp"))).lower()
            if _p not in ("udp", "tcp"):
                _p = "udp"
            self._ovpn_protocol = _p
            # Effective follows selected when on OV, else stays udp (inert under WG)
            if self._stack_effective == "openvpn":
                self._ovpn_protocol_effective = _p
        if "ovpn_endpoint_port" in updates or "endpoint_port" in updates:
            _pe = str(updates.get("ovpn_endpoint_port", updates.get("endpoint_port", "1194"))).strip()
            try:
                _pei = int(_pe)
                if _pei not in (1194, 443, 8443):
                    _pei = 1194
            except Exception:
                _pei = 1194
            self._ovpn_endpoint_port = str(_pei)
            if self._stack_effective == "openvpn":
                self._ovpn_endpoint_port_effective = str(_pei)
        if "auto_hetero_boot" in updates:
            self._auto_hetero_boot = bool(updates["auto_hetero_boot"])
        if self._shared is not None and any(
            k in updates for k in ("recent_ip_window", "recent_ip_max_age", "shared_rotation_file")
        ):
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
            "error_detail": _classify_error_kind(self),
            "auth_failed": self._auth_failed,
            "last_rotation_error": self._last_rotation_error,
            "ovpn_protocol": getattr(self, "_ovpn_protocol", "udp"),
            "ovpn_protocol_effective": getattr(self, "_ovpn_protocol_effective", "udp"),
            "control_last_401_at": self._control_last_401_at,
            "control_last_error": self._control_last_error,
            "socks5_eof_count": self._socks5_eof_count,
            "socks5_eof_window": list(self._socks5_eof_window)[-10:],
            "rotation_failed_at": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._last_rotation_failed_at))
                if self._last_rotation_failed_at
                else None
            ),
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
                # [plan 20/08] healthcheck-restart churn flag: True while
                # the refresh scan counts fresh restart markers.
                "restart_churn": self._restart_churn,
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
                # [canari WG 25/08] dernier verdict egress WireGuard — None
                # = jamais testé ; âge en s vs l'horloge du manager.
                "wg_canary_ok": self._wg_canary_state["ok"],
                "wg_canary_age_s": (
                    None
                    if self._wg_canary_state["at"] is None
                    else max(0, int(self._now_fn() - self._wg_canary_state["at"]))
                ),
                # [Bug #4] flip observability — pending decision, blocked reason,
                # cooldown, auth window detail, key presence, stack age
                "pending_flip": self._pending_flip,
                "flip_blocked_reason": self._last_flip_blocked_reason,
                "cooldown_remaining_s": (
                    0
                    if self._last_auto_flip_at is None
                    else max(0, int(
                        self._auto_flip_cooldown_min * 60
                        - (self._now_fn() - self._last_auto_flip_at)
                    ))
                ),
                "auth_failed_window_len": len(self._auth_failed_window),
                "auth_failed_oldest_age_s": (
                    None
                    if not self._auth_failed_window
                    else max(0, int(self._now_fn() - self._auth_failed_window[0]))
                ),
                "wg_key_present": self._wg_key_present(),
                "wg_key_file": self._wg_key_file,
                "stack_effective": self._stack_effective,
                "stack_since_age_s": (
                    None
                    if self._stack_since is None
                    else max(0, int(self._now_fn() - self._stack_since))
                ),
                "egress_failures": self._egress_failures,
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

    def _docker_run(
        self, args: list[str], timeout: int = 30, env: dict | None = None
    ) -> subprocess.CompletedProcess:
        """Run a docker CLI command (blocking — call via asyncio.to_thread).

        ``env`` [plan 18/08 §2.1]: explicit child environment for `docker
        compose` invocations (see _compose_env). A child process's explicit
        env wins over BOTH the inherited parent env AND the .env file next
        to the compose file — the deterministic half of the 19/08 fix.

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
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
        except FileNotFoundError as _fnf:
            raise RuntimeError("CLI docker introuvable dans le PATH") from _fnf
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

    def _compose_env(self, *, stations: list | None = None, stack: str | None = None) -> dict:
        """Explicit environment for `docker compose` children.

        [plan 18/08 §2.1] The 19/08 root cause: settings.load_env_file()
        only fills os.environ when a key is ABSENT, so a parent env loaded
        at boot wins over the .env file for every compose child — a sed on
        the file was invisible until the process restarted. The fix here is
        deterministic: the child gets the FULL parent env PLUS the
        per-station VPN_TYPE_STATION{n} values, overriding whatever stale
        value the parent carries. The compose interpolation is
        ``${VPN_TYPE_STATION{n}:-openvpn}`` (docker-compose.yml) — this is
        the ONLY surface that decides a station's stack.

        ``stations`` defaults to this manager's own station; ``stack``
        defaults to the current effective stack (compose's own default when
        unknown). During a stack flip, _apply_stack passes the TARGET stack
        so the child recreates in the requested mode even if the parent env
        still carries the previous one.
        """
        env = dict(os.environ)
        if stack is None:
            stack = self._stack_effective or "openvpn"
        for s in stations or [self._station]:
            env[f"VPN_TYPE_STATION{s}"] = stack
            if stack == "openvpn":
                _proto = getattr(self, "_ovpn_protocol_effective", "udp")
                if _proto not in ("udp", "tcp"):
                    _proto = "udp"
                env[f"OPENVPN_PROTOCOL_STATION{s}"] = _proto
                env["OPENVPN_PROTOCOL"] = _proto
                # Q6: endpoint port diversification (custom ovpn only — nordvpn rejects custom port)
                if self._config.get("custom_ovpn_file"):
                    _port = getattr(self, "_ovpn_endpoint_port_effective", "1194" if _proto == "udp" else "443")
                    try:
                        _port = str(int(_port))
                        if _port not in ("1194", "443", "8443"):
                            _port = "1194" if _proto == "udp" else "443"
                    except Exception:
                        _port = "1194" if _proto == "udp" else "443"
                    env[f"OPENVPN_ENDPOINT_PORT_STATION{s}"] = _port
                    env["OPENVPN_ENDPOINT_PORT"] = _port
        # [fix 20/08][Axe 3.1] Custom .ovpn (dashboard upload): compose's
        # volume block bind-mounts vpn_configs/custom/ → /vpn-custom (ro)
        # and stanzas interpolate ${OPENVPN_CUSTOM_CONFIG:-}. The uploaded
        # path is persisted compose-ROOT-relative (upload endpoint) — resolve
        # it next to the compose file and point the env at its in-container
        # mirror. OpenVPN stack only: a stale custom path must never leak
        # into a WireGuard stanza, and gluetun ignores it under WG anyway.
        custom_ovpn = self._config.get("custom_ovpn_file")
        if stack == "openvpn" and custom_ovpn:
            cu_path = os.path.join(
                os.path.dirname(self._compose_file_path()), str(custom_ovpn).replace("/", os.sep)
            )
            if os.path.isfile(cu_path):
                env["OPENVPN_CUSTOM_CONFIG"] = f"/vpn-custom/{os.path.basename(cu_path)}"
        return env

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
            _op_ev = self._rotation_op_event
            if _op_ev is None:
                break  # boucle de rotation arrêtée : plus rien à attendre
            _op_ev.clear()
            if self._rotation_op_count > 0:
                await _op_ev.wait()

    async def _docker_inspect(self) -> dict:
        """Inspect the gluetun container. Returns {} if absent or docker unavailable."""
        try:
            result = await asyncio.to_thread(self._docker_run, ["inspect", self._docker_container], 15
            )
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

    async def restart(self) -> None:
        """[v10 §9.4] Redémarrage léger du conteneur — API publique utilisée
        par le dashboard et StationSupervisor.restart()."""
        await self._docker_restart()

    async def _docker_restart(self) -> None:
        """Restart the gluetun container to get a fresh IP.

        Note ([25]): this deliberately bypasses docker compose — a plain
        ``docker restart`` keeps the compose definition untouched (no drift
        of the declared service). Image updates go through ``apply_update``
        instead, which recreates the container from the new image.
        """
        result = await asyncio.to_thread(self._docker_run, ["restart", self._docker_container], 60)
        if result.returncode != 0:
            raise RuntimeError(
                f"échec redémarrage docker : {result.stderr.strip() or result.stdout.strip()}"
            )

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

    async def _control_exec(
        self, method: str, path: str, body: str | None = None, timeout: float = 10.0
    ) -> list[str]:
        """Run one wget call against gluetun's control server inside the
        container. Returns the decoded stdout lines on success, [] on any
        failure (non-zero exit, wget absent, control server down, timeout).
        Never logs the key. A 204 (no body) is still a success -> [].
        """
        key_var = "VPN_CONTROL_API_KEY"
        cmd = ["exec", self._docker_container]
        script = f'wget -q -O - -T {int(timeout)} --header="X-API-Key: ${key_var}"'
        if method != "GET":
            script += f" --method={method}"
            if body:
                script += f" --body-data={_sh_quote(body)}"
        script += f" http://127.0.0.1:8000{path}"
        cmd += ["sh", "-c", script]
        try:
            result = await asyncio.to_thread(self._docker_run, cmd, int(timeout) + 5)
        except RuntimeError as e:
            logger.debug("[vpn] control server call %s %s failed: %s", method, path, e)
            return []
        combined = (result.stderr or "") + (result.stdout or "")
        if result.returncode != 0:
            # warm-avalanche Q3+Q8: 401 = clé désynchronisée → resync + arm watchdog immédiat
            if "401" in combined or "Unauthorized" in combined:
                self._control_last_401_at = time.time()
                self._control_last_error = combined.strip()[:200]
                logger.warning(
                    "[vpn] control 401 — clé désynchronisée station %s, resync credentials.env (egress %d/%d)",
                    self._station,
                    self._egress_failures,
                    self._auto_wg_egress_ticks,
                )
                self.arm_egress_watchdog()
                try:
                    if self._watchdog_event is not None:
                        self._watchdog_event.set()
                except Exception:
                    pass
            logger.debug(
                "[vpn] control server %s %s rc=%d: %s",
                method,
                path,
                result.returncode,
                combined.strip()[:200],
            )
            return []
        # même en rc 0, wget peut avoir rendu un body 401 JSON
        if "401" in combined or "Unauthorized" in combined:
            self._control_last_401_at = time.time()
            self._control_last_error = combined.strip()[:200]
            logger.warning("[vpn] control 401 (body) station %s — arm watchdog", self._station)
            self.arm_egress_watchdog()
            try:
                if self._watchdog_event is not None:
                    self._watchdog_event.set()
            except Exception:
                pass
        out = (result.stdout or "").splitlines()
        return [ln.strip() for ln in out if ln.strip()]

    async def _control_status(self) -> bool | None:
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

    async def _control_public_ip(self) -> str | None:
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

    async def _control_pin_country(
        self, country: str, timeout: float = 60.0, catchup: float = 0.0
    ) -> bool:
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
            {"provider": {"server_selection": {"countries": [country]}}}, separators=(",", ":")
        )
        lines = await self._control_exec("PUT", "/v1/vpn/settings", body=payload, timeout=10)
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
                    hint = (
                        " (gluetun reported this country name as "
                        'invalid / "not in choices" — check '
                        "server_countries)"
                    )
                logger.warning("[vpn] control pin %s rejected: %s%s", country, lines[0][:200], hint)
                return False
        # Bound the failure scan to logs written AFTER the pin — a stale
        # AUTH_FAILED from an earlier reconnect in the same container must
        # not abort a healthy pin.
        since_pin = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        deadline = time.monotonic() + timeout
        stopped_warned = False  # [incident 17/08] one WARN per pin — 59/line spam

        async def _scan_auth_tls() -> bool:
            """[P3 perf] UN SEUL ``docker logs`` par itération de pin : le
            texte est partagé entre les scans AUTH_FAILED et TLS (l'ancien
            code lançait un subprocess par check, à chaque poll de 0,5 s).
            Stubs sans kwarg ``text`` → chemin historique (seam §14.1.10)."""
            if not self._auth_check_supports_text():
                if await self._check_auth_failed(since_pin):
                    return True
                return await self._check_server_issue(since_pin)
            result = await asyncio.to_thread(self._docker_run,
                ["logs", "--since", since_pin, self._docker_container],
                30,
            )
            if result.returncode != 0:
                return False
            text = result.stdout
            if await self._check_auth_failed(since_pin, text=text):
                return True
            return await self._check_server_issue(since_pin, text=text)

        while time.monotonic() < deadline:
            # Scan BEFORE leaning on the status reply: gluetun can report
            # "running" while openvpn is caught in an AUTH_FAILED retry
            # loop, so the auth scan is the decisive signal.
            if await _scan_auth_tls():
                logger.warning(
                    "[vpn] control pin %s: auth/TLS failure since pin start "
                    "— abandoning, the next country will be pinned "
                    "immediately",
                    country,
                )
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
                        if await _scan_auth_tls():
                            # [audit 18/08] same WARN as the pre-status scan —
                            # a TLS failure mid-catch-up was silent before
                            # (the incident was diagnosed through logs).
                            logger.warning(
                                "[vpn] control pin %s: auth/TLS failure "
                                "during catch-up — abandoning",
                                country,
                            )
                            return False
                        try:
                            if await self.get_public_ip() is not None:
                                return True  # a real IP through the tunnel
                        except Exception:
                            pass  # probe slipped — keep waiting
                        await asyncio.sleep(self._wait_healthy_poll)
                return True
            if status is False:
                if not stopped_warned:
                    logger.warning("[vpn] control pin %s: VPN reports stopped", country)
                    stopped_warned = True
            await asyncio.sleep(self._wait_healthy_poll)
        logger.warning("[vpn] control pin %s: not running after %.0fs", country, timeout)
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

    def _local_next_country(self, previous: str | None) -> str | None:
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

    def _countries_list_for_pool(self, forced_pool: set | None = None) -> list:
        """Countries list filtered to forced_pool when provided (P3 geo).

        Axe B: also intersects with geo_strict_union() when GEO strict
        policies exist and forced_pool is not explicitly provided — ensures
        background rotations never pin outside strict-allowed countries.
        """
        base = self._countries_list()
        if forced_pool is not None:
            # forced_pool is already normalized via _normalize_country
            return [c for c in base if c in forced_pool]
        # Axe B: when no forced_pool but GEO strict policies exist, narrow
        # to the union of all strict-allowed countries
        try:
            from config.settings import geo_strict_union

            _gsu = geo_strict_union()
            if _gsu:
                return [c for c in base if c in _gsu]
        except Exception:
            pass
        return base

    _geo_coalesce: dict = {}  # class-level: frozenset(allowed) -> Future

    async def ensure_geo_egress(self, allowed: set, timeout: float = 8.0) -> bool:
        """P2/P3 geo: ensure egress country is in allowed (budget ≤ min(8s, ip_probe_budget)).

        Isolation: does NOT advance SharedRotationState cursor; pins via
        forced_pool on this station only (bail court). Coalescing via
        frozenset(allowed) — N concurrent same-allowed share 1 pin. Queue
        max 8 → caller gets 503. Histogram + breaker (pays,station).
        """
        if not allowed:
            return False
        # [plan v10 §14.3.3] clé de coalescing AVEC la station : l'ancien
        # frozenset(allowed) seul faisait coalescer deux stations sur UN pin
        # exécuté par l'autre tunnel (résultat qui ne concerne pas sa propre
        # sortie géo). getattr défensif (instances de test via __new__).
        key = frozenset(allowed) | {f"__station_{getattr(self, '_station', 0)}"}
        # Coalescing: share in-flight pin for same allowed set
        existing = VPNManager._geo_coalesce.get(key)
        if existing is not None and not existing.done():
            try:
                return await asyncio.wait_for(existing, timeout=timeout + 1.0)
            except Exception:
                pass
        # Queue max guard
        if len(VPNManager._geo_coalesce) >= 8:
            raise RuntimeError("file d'attente geo pin saturée (503)")
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        VPNManager._geo_coalesce[key] = fut
        try:
            result = await self._ensure_geo_egress_inner(set(allowed), timeout)
            if not fut.done():
                fut.set_result(result)
            return result
        except Exception as e:
            if not fut.done():
                fut.set_exception(e)
            raise
        finally:
            VPNManager._geo_coalesce.pop(key, None)

    async def _ensure_geo_egress_inner(self, allowed: set, timeout: float) -> bool:
        if self._current_country and self._current_country in allowed:
            try:
                budget = min(float(timeout), float(getattr(self, "_ip_probe_budget", 8.0) or 8.0))
            except Exception:
                budget = 2.0
            probe_ok = (
                await asyncio.wait_for(self._probe_tunnel_light(), timeout=min(2.0, budget))
                if hasattr(self, "_probe_tunnel_light")
                else True
            )  # type: ignore
            if probe_ok:
                return True
        candidates = set(self._countries_list()) & set(allowed)
        # Axe B: intersect with geo_strict_union() when GEO strict policies
        # exist — prevents pinning to countries outside all strict-allowed sets
        try:
            from config.settings import geo_strict_union

            _gsu = geo_strict_union()
            if _gsu:
                candidates &= _gsu
        except Exception:
            pass
        # [plan v10 §14.3.34] _host_blacklisted appliqué aux PAYS = no-op
        # dangereux : la blacklist ne contient que des HOSTNAMES NordVPN
        # (_record_auth_failure), jamais des noms de pays. Filtre retiré.
        if not candidates:
            return False
        try:
            budget = min(float(timeout), float(getattr(self, "_ip_probe_budget", 8.0) or 8.0))
        except Exception:
            budget = 8.0
        max_tries = min(len(candidates), 3)
        for _ in range(max_tries):
            pick = None
            for c in sorted(candidates):
                pick = c
                break
            if pick is None:
                pick = sorted(candidates)[0]
            try:
                ok = await asyncio.wait_for(
                    self._control_pin_country(
                        pick, timeout=min(20, budget), catchup=min(25, budget)
                    ),
                    timeout=budget,
                )
                if ok:
                    self._current_country = pick
                    return True
            except TimeoutError:
                logger.debug("[vpn] ensure_geo_egress timeout for %s", pick)
                return False
            except Exception as e:
                logger.debug("[vpn] ensure_geo_egress pin %s failed: %s", pick, e)
            candidates.discard(pick)
            if not candidates:
                break
        return False

    async def pin_country(self, country: str, timeout: float = 30.0) -> bool:
        """[v10 §12.1.5] Pin MANUEL d'un pays pour cette station (dashboard).

        Réutilise _control_pin_country (PUT settings + scan auth/TLS fail-fast).
        Le pays est épinglé jusqu'à unpin_country() ou la prochaine rotation."""
        return await self._control_pin_country(country, timeout=timeout)

    async def unpin_country(self) -> bool:
        """[v10 §12.1.5] Restaure la sélection complète (SERVER_COUNTRIES) :
        met fin au pin manuel. Le curseur de rotation reprend son cours."""
        if not self._control_enabled:
            return False
        try:
            countries = [str(c) for c in (self._countries_list() or [])]
        except Exception:
            countries = []
        if not countries:
            logger.warning("[vpn] unpin: aucune liste de pays configurée")
            return False
        payload = json.dumps(
            {"provider": {"server_selection": {"countries": [_normalize_country(c) for c in countries]}}},
            separators=(",", ":"),
        )
        lines = await self._control_exec("PUT", "/v1/vpn/settings", body=payload, timeout=15)
        ok = bool(lines) and lines[0].strip().lower() == "running"
        if ok:
            self._current_country = None
            self._country_pinned_at = None
            logger.info("[vpn] unpin: sélection multi-pays restaurée (%d pays)", len(countries))
        return ok

    async def _pin_country_for_rotation(
        self,
        timeout: float | None = None,
        catchup: float | None = None,
        forced_pool: set | None = None,
    ) -> str | None:
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
        # Axe B: fallback to station-level geo constraint set by _open_via_pool
        # when forced_pool is not passed explicitly (background rotations from
        # on_quota_exhausted / on_disconnect_retry)
        if forced_pool is None:
            forced_pool = getattr(self, "_geo_forced_pool", None)
        countries = self._countries_list_for_pool(forced_pool)
        if len(countries) < 2:
            # forced_pool single-country pin still allowed
            if forced_pool and len(self._countries_list_for_pool(forced_pool)) == 1:
                nxt = self._countries_list_for_pool(forced_pool)[0]
                if nxt != self._current_country:
                    pin_timeout = self._control_pin_timeout if timeout is None else timeout
                    pin_catchup = self._control_pin_catchup if catchup is None else catchup
                    if await self._control_pin_country(
                        nxt, timeout=pin_timeout, catchup=pin_catchup
                    ):
                        self._current_country = nxt
                        self._country_pinned_at = time.monotonic()
                        return nxt
            return None
        idx = None
        if (
            forced_pool is None
            and self._shared is not None
            and hasattr(self._shared, "next_country")
        ):
            try:
                idx = self._shared.next_country(self._station, self._country_offset, len(countries))
            except Exception as e:
                logger.debug("[vpn] shared country cursor failed: %s", e)
        nxt = countries[idx] if idx is not None else self._local_next_country(self._current_country)
        # forced_pool: pick first not-current
        if forced_pool is not None and (nxt is None or nxt == self._current_country):
            for c in countries:
                if c != self._current_country:
                    nxt = c
                    break
        if nxt is None or nxt == self._current_country:
            return None
        pin_timeout = self._control_pin_timeout if timeout is None else timeout
        pin_catchup = self._control_pin_catchup if catchup is None else catchup
        if await self._control_pin_country(nxt, timeout=pin_timeout, catchup=pin_catchup):
            self._current_country = nxt
            self._country_pinned_at = time.monotonic()
            logger.info("[vpn] station %d pinned country %s", self._station, nxt)
            return nxt
        return None

    async def _current_hostname(self, since: str) -> str | None:
        """Hostname gluetun is currently trying — the LAST "[<host>] Peer
        Connection Initiated with [AF_INET]<ip>:1194" line (M_INFO, visible
        from the DEFAULT verbosity 1) in container logs written since
        ``since``. [audit 18/08] that IS the OpenVPN hostname line — never
        "Connecting to [...]". The SIGUSR1 retry loop re-logs the same
        remote every ~11 s, so the last occurrence is the host in play.
        None when the container is absent or no hostname is logged."""
        result = await asyncio.to_thread(self._docker_run, ["logs", "--since", since, self._docker_container], 30
        )
        if result.returncode != 0:
            return None
        return _extract_current_hostname(result.stdout)

    async def _fast_recover_via_control(
        self, max_skips: int = 3, timeout: float = 15.0, catchup: float = 15.0
    ) -> bool:
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
            nxt = await self._pin_country_for_rotation(timeout=timeout, catchup=catchup)
            if nxt is None:
                if await self._check_auth_failed(since) or await self._check_server_issue(since):
                    logger.warning(
                        "[vpn] fast-pin: still rejecting (attempt %d/%d) — "
                        "re-pinning a different country",
                        attempt + 1,
                        max_skips + 1,
                    )
                    continue  # dead host: the next country can work
                return False  # infra failure: compose path is the escalation
            host = await self._current_hostname(since)
            if host and self._host_blacklisted(host):
                logger.warning(
                    "[vpn] fast-pin: %s blacklisted — skipping it (attempt %d/%d)",
                    host,
                    attempt + 1,
                    max_skips + 1,
                )
                continue
            if await self._finalize_ip(allow_stale=False):
                self._watchdog_backoff.record_success()
                self._set_status(VPNState.CONNECTED)
                self._error = None
                # [plan 20/08] Mark the recovery boundary BEFORE the internal
                # refresh re-scans the churn window: docker logs span
                # recoveries, so pre-recovery churn markers are stale — the
                # scan snaps to after this instant.
                self._restart_churn_recovered_at = time.time()
                self._churn_next_due = 0.0  # [v10 §14.1.10] re-scan frais immédiat
                await self.refresh_status(force=True)
                self.save_state()
                logger.info("[vpn] fast-pin: recovered — tunnel healthy (IP %s)", self._current_ip)
                return True
            # Finalize failed to land a fresh IP — try the next country.
        return False

    def _next_country_preview(self) -> str | None:
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
                    self._station, self._country_offset, len(countries)
                )
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
        result = await asyncio.to_thread(self._docker_run, cmd, 120, env=self._compose_env())
        if result.returncode != 0:
            raise RuntimeError(
                f"échec docker compose up : {result.stderr.strip() or result.stdout.strip()}"
            )

    async def _ensure_container(self, force_recreate: bool | None = None) -> None:
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

    def _auto_flip_decision(self) -> tuple | None:
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
        if (
            self._last_auto_flip_at is not None
            and now - self._last_auto_flip_at < self._auto_flip_cooldown_min * 60
        ):
            return None  # cooldown — anti-flapping
        if self._stack_effective == "wireguard":
            if self._egress_failures >= self._auto_wg_egress_ticks:
                return ("openvpn", f"egress dead {self._egress_failures} ticks")
            # [churn→OV 25/08] la boucle healthcheck gluetun PREUVE que le
            # tunnel ne passe pas : le compteur egress peut ne jamais
            # atteindre le seuil (la sonde tombe parfois dans une fenêtre
            # vivante du cycle de restart) et la station restait coincée en
            # WG pour toujours. Un churn confirmé + un heal raté = sortie
            # déterministe vers OpenVPN (le canari protège le retour).
            if (
                getattr(self, "_restart_churn", False)
                and self._watchdog_backoff.consecutive_failures >= 1
            ):
                return (
                    "openvpn",
                    f"healthcheck restart loop x{self._restart_churn_threshold} + heal failed",
                )
            return None
        # Effective OpenVPN below.
        if self._stack_effective != "openvpn":
            return None
        # OV UDP dead -> TCP is cheaper than jumping back to WG (firewall blocks UDP)
        if self._egress_failures >= self._auto_wg_egress_ticks:
            if getattr(self, "_ovpn_protocol_effective", "udp") == "udp":
                return ("openvpn", f"egress dead {self._egress_failures} ticks UDP -> TCP")
            # UDP already tried, TCP also dead -> fall through to WG flip via auth/window or direct
            # If TCP dead persistently, the next egress tick will still be dead and the auth-free
            # path below will eventually flip to WG (preferred). Keep egress->WG as fallback
            # when protocol already tcp to avoid flip-flop loop.
            return ("wireguard", f"egress dead {self._egress_failures} ticks TCP")
        if not self._wg_key_present():
            # [Bug #4] log + blocked_reason pour observabilité dashboard
            reason = f"wireguard.env missing ({self._wg_key_file})"
            logger.warning("[vpn] auto-flip blocked: %s", reason)
            self._last_flip_blocked_reason = reason
            return None  # cannot flip to wireguard without the key
        # Path B : seuil franchi — flip immédiat (inchangé)
        if len(self._auth_failed_window) >= self._auto_ov_fail_threshold:
            return ("wireguard", f"{len(self._auth_failed_window)} AUTH_FAILED/30min")
        # Path C' : OV sain ≥ return_min ET fenêtre < seuil/2 — retour WG
        # (1 blip isolé ne bloque plus 60 min ; ancien: window strictement vide)
        if (
            self._stack_since is not None
            and now - self._stack_since >= self._auto_ov_return_min * 60
            and len(self._auth_failed_window) * 2 < self._auto_ov_fail_threshold
        ):
            return ("wireguard", f"OV healthy {self._auto_ov_return_min}min — return to WG")
        # Path C'' : OV bloqué ≥ (return_min+30) min — escape hatch
        if (
            self._stack_since is not None
            and now - self._stack_since >= (self._auto_ov_return_min + 30) * 60
        ):
            return ("wireguard", f"OV stuck {int((now - self._stack_since) / 60)}min — escape to WG")
        # Path D' : slow-burn 1-2 AUTH_FAILED persistés ≥10 min sur OV
        # (pas un bool, pas 5 min de flapping — old=oldest monotonic, déjà pruné 30 min)
        if self._auth_failed_window and self._stack_since is not None:
            oldest = self._auth_failed_window[0]
            if now - oldest >= 10 * 60 and now - self._stack_since >= 10 * 60:
                return ("wireguard", f"{len(self._auth_failed_window)} AUTH_FAILED/30min persisted 10min — slow-burn escape")
        return None

    def _emergency_flip_decision(self) -> tuple | None:
        """Emergency flip for manual stacks after persistent failure.

        Auto policy respects the user's choice (``_stack != 'auto'`` → never
        flip). In production all stations in the logs failed on WG with
        ``i/o timeout`` on every handshake, so a manual ``wireguard`` stack
        looped forever on the same recovery (re-pin → restart → compose).
        This decision allows a *manual* stack to heal itself after one
        failed recovery cycle: same thresholds as auto, but gated on
        ``watchdog_backoff ≥ 1`` (at least one heal attempt actually failed)
        and the same cooldown. Returns (mode, reason) or None.
        """

        if self._stack == "auto":
            return None
        now = self._now_fn()
        cutoff = now - 30 * 60
        self._auth_failed_window = [t for t in self._auth_failed_window if t >= cutoff]
        if (
            self._last_auto_flip_at is not None
            and now - self._last_auto_flip_at < self._auto_flip_cooldown_min * 60
        ):
            return None  # cooldown — anti-flapping (shared with auto)
        # Manual = stay in type, try all ports/protocols of that type only
        # WireGuard manual: never flip to OV, just keep healing WG (re-pin country,
        # blacklist, server refresh). The tunnel stays wireguard tout port (51820
        # + country rotation). Return None -> watchdog heals same stack.
        if self._stack_effective == "wireguard":
            # Port dimension for WG is server/country rotation (51820 fixe), so
            # no protocol flip — let the normal recovery (fast-pin, restart,
            # escalate refresh) do the work. No emergency stack flip in manual.
            return None
        if self._stack_effective == "openvpn":
            # OV manual: try all OV ports/protocols (udp 1194 -> tcp 443 -> tcp 8443
            # via OPENVPN_ENDPOINT_PORT if needed) but never flip to WG.
            if self._egress_failures >= self._auto_wg_egress_ticks:
                if self._watchdog_backoff.consecutive_failures >= 1 or self._restart_churn:
                    if getattr(self, "_ovpn_protocol_effective", "udp") == "udp":
                        return ("openvpn", "emergency OV UDP dead -> TCP, recovery failed")
                    else:
                        # TCP also dead -> stay in OV, escalate will refresh servers
                        # and watchdog will retry same TCP with new country.
                        return None
            if self._restart_churn and self._watchdog_backoff.consecutive_failures >= 1:
                if getattr(self, "_ovpn_protocol_effective", "udp") == "udp":
                    return ("openvpn", "emergency OV healthcheck loop UDP -> TCP, recovery failed")
                else:
                    return None
            # AUTH_FAILED in manual OV: stay in OV, just blacklist host (no WG flip)
            return None
        return None

    def _wg_key_present(self) -> bool:
        try:
            return os.path.isfile(self._wg_key_file)
        except Exception:
            return False

    # ── [canari WG 25/08] validation egress avant flip vers WireGuard ──

    _WG_CANARY_SERVICE = "vpn-wg-test"
    _WG_CANARY_TTL_S = 600  # verdict réutilisé 10 min (anti-marteau docker)
    _WG_CANARY_BOOT_TIMEOUT_S = 90
    _WG_CANARY_PORT = 1090  # SOCKS5 du canari (compose, loopback uniquement)
    _WG_CANARY_POLL_INTERVAL_S = 4.0

    async def _canary_probe_once(self) -> bool:
        """Une sonde GET bornée via SOCKS5 du canari. Même chaîne que
        _probe_connect (httpcore[socks] requis — sinon pas de faux positif).
        Injectable dans les tests."""
        import httpx

        try:
            import httpcore  # noqa: F401

            _has_socks = True
        except ImportError:
            return False
        if not _has_socks:
            return False
        try:
            client = httpx.AsyncClient(
                proxy=f"socks5://127.0.0.1:{self._WG_CANARY_PORT}",
                timeout=httpx.Timeout(4.0),
            )
        except Exception:
            return False
        try:
            resp = await asyncio.wait_for(
                client.send(httpx.Request("GET", self._ip_check_url)), timeout=5.0
            )
            return getattr(resp, "status_code", 500) < 500
        except Exception:
            return False
        finally:
            try:
                await client.aclose()
            except Exception:
                pass

    async def _wg_canary_alive(self, reason: str) -> bool:
        """True si le chemin WireGuard fait VRAIMENT passer du trafic.

        Bring-up compose du service vpn-wg-test (VPN_TYPE=wireguard épinglé,
        profil wg-test), sondes répétées jusqu'au budget, teardown
        systématique. Verdict mis en cache TTL (_WG_CANARY_TTL_S) : un
        fournisseur WG cassé coûte au plus UNE validation par TTL et par
        station — jamais de marteau docker, jamais de flip à l'aveugle.
        """
        now = self._now_fn()
        st = self._wg_canary_state
        if (
            st["ok"] is not None
            and st["at"] is not None
            and now - st["at"] < self._WG_CANARY_TTL_S
        ):
            return bool(st["ok"])
        logger.warning("[vpn-canary] validation egress WireGuard (%s)…", reason)
        compose_path = self._compose_file_path()
        ok = False
        t0 = self._now_fn()
        try:
            result = await asyncio.to_thread(self._docker_run,
                ["compose", "-f", compose_path, "up", "-d", self._WG_CANARY_SERVICE],
                240,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            deadline = t0 + self._WG_CANARY_BOOT_TIMEOUT_S
            # Borne d'itérations : un budget en secondes ne suffit pas si une
            # horloge est pathologique (tests) — jamais de boucle infinie.
            max_iters = (
                int(self._WG_CANARY_BOOT_TIMEOUT_S / max(self._WG_CANARY_POLL_INTERVAL_S, 0.001))
                + 2
            )
            iters = 0
            while self._now_fn() < deadline and iters < max_iters:
                iters += 1
                if await self._canary_probe_once():
                    ok = True
                    break
                await asyncio.sleep(self._WG_CANARY_POLL_INTERVAL_S)
        except Exception as e:
            logger.warning("[vpn-canary] bring-up échoué: %s", e)
        finally:
            try:
                await asyncio.to_thread(self._docker_run,
                    [
                        "compose",
                        "-f",
                        compose_path,
                        "rm",
                        "-sf",
                        self._WG_CANARY_SERVICE,
                    ],
                    120,
                )
            except Exception:
                pass
        st["ok"] = ok
        st["at"] = self._now_fn()
        logger.warning(
            "[vpn-canary] verdict: WireGuard %s (%.0fs)",
            "PASS" if ok else "SANS EGRESS",
            st["at"] - t0,
        )
        return ok

    async def _cancel_wg_flip_if_canary_dead(self) -> bool:
        """Verrou d'application : annule un flip wireguard non prouvé.

        Appelé JUSTE AVANT l'application de ``_pending_flip`` (hors lock,
        comme le reste du bloc). Retourne True quand un flip a été annulé —
        ``_pending_flip`` est alors None et la station RESTE sur son stack
        courant (la re-décision se fera au prochain tick, au rythme du TTL
        canari ; quand OV est sain c'est exactement le comportement voulu :
        ne jamais rejoindre un chemin WG mort « parce qu'il est préféré »).
        """
        pf = self._pending_flip
        if pf is None or pf[0] != "wireguard":
            return False
        if await self._wg_canary_alive(pf[1]):
            return False
        self._pending_flip = None
        logger.error(
            "[vpn-watchdog] flip →wireguard ANNULÉ — canari WG sans egress "
            "(%s) ; maintien sur %s jusqu'à preuve du chemin WG",
            pf[1],
            self._stack_effective,
        )
        return True

    async def _apply_stack(self, mode: str, reason: str = "manual", auto: bool = False, stations: list | None = None) -> bool:
        """Switch the effective stack (compose substitution) and record the flip.
        mode ∈ {"wireguard", "openvpn"} (auto is resolved by the caller).
        Refuses wireguard when vpn_configs/wireguard.env is missing.

        By default flips ALL ACTIVE stations (global, manual). When ``stations``
        is set (auto hétérogène per-station), only those stations are flipped
        and recreated — the fleet becomes heterogeneous (WG + OV UDP + OV TCP).

        Writes VPN_TYPE_STATION{1..N} for the target stations into the .env
        next to the compose file (read-modify-write + os.replace, atomic —
        same pattern as save_state) and PRUNES stale keys from downscaled
        stations, then `compose up -d --force-recreate` on the target
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
            logger.warning("[vpn] _apply_stack: refusing wireguard — %s missing", self._wg_key_file)
            return False
        # [plan 18/08 §1] Active stations: the live registry (set by the
        # lifespan / _apply_station_count). Fallback to the legacy station
        # 1/2 pair when there is no registry (standalone manager, unit
        # tests) so past dual-station behavior is unchanged.
        _managers = list(getattr(shared_state, "vpn_managers", None) or [])
        _is_global_flip = stations is None
        if stations is not None:
            # Per-station flip (auto hétérogène): explicit station list
            _map = {m._station: m._compose_service for m in _managers if m is not None}
            services = [_map.get(s, f"vpn-gluetun-{s}" if s > 1 else "vpn-gluetun") for s in stations]
            station_keys = {f"VPN_TYPE_STATION{s}" for s in stations}
        else:
            stations, services = [], []
            for _m in _managers:
                if _m is not None and _m._station not in stations:
                    stations.append(_m._station)
                    services.append(_m._compose_service)
            if not stations:
                stations = [1, 2] if self._station <= 2 else [self._station]
                services = (
                    ["vpn-gluetun", "vpn-gluetun-2"] if self._station <= 2 else [self._compose_service]
                )
            station_keys = {f"VPN_TYPE_STATION{s}" for s in stations}
        compose_path = self._compose_file_path()
        env_path = os.path.join(os.path.dirname(compose_path), ".env")
        _ENV_RW_LOCK.acquire()
        try:
            # Read-modify-write: NEVER touch the other .env keys (secrets
            # live there) — only the active stations' VPN_TYPE_STATION vars.
            data = ""
            try:
                with open(env_path, encoding="utf-8") as f:
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
                # Per-station flip (auto hétérogène) must NOT prune other stations.
                if _is_global_flip and key.startswith("VPN_TYPE_STATION") and re.fullmatch(r"VPN_TYPE_STATION\d+", key):
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
        finally:
            _ENV_RW_LOCK.release()
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
            cmd = ["compose", "-f", compose_path, "up", "-d", "--force-recreate"] + sorted(services)
            # Explicit env: the TARGET stack reaches the compose child even
            # when the parent env is stale (19/08 root cause — §2.1).
            result = await asyncio.to_thread(self._docker_run, cmd, 300, env=self._compose_env(stations=stations, stack=mode)
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        except Exception as e:
            logger.error("[vpn] _apply_stack: compose recreate failed: %s", e)
            return False
        previous = self._stack_effective
        self._stack_effective = mode
        self._stack_since = self._now_fn()
        self._last_flip_blocked_reason = None  # [Bug #4] clear on successful flip
        if auto:
            self._last_auto_flip_at = self._now_fn()
            # [Bug #2] prune window after auto OV→WG flip — avoid stale AUTH_FAILED
            # re-triggering D' on the next tick (window spans flips, not reset by stack change)
            if mode == "wireguard":
                self._auth_failed_window = []
        self._flips.append(
            {
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "from": previous,
                "to": mode,
                "reason": reason,
            }
        )
        self._flips = self._flips[-20:]
        logger.info("[vpn] stack: %s→%s (%s)", previous, mode, reason)
        # When flipping to OV, ensure protocol is tracked (default udp)
        if mode == "openvpn" and not hasattr(self, "_ovpn_protocol_effective"):
            self._ovpn_protocol_effective = getattr(self, "_ovpn_protocol", "udp")
        return True

    async def _apply_ovpn_protocol(self, protocol: str, reason: str = "protocol fallback") -> bool:
        """Flip OpenVPN protocol/port without changing VPN_TYPE.

        Cycle complet Q6: udp:1194 → tcp:443 → tcp:8443 → WG (via _auto_flip).
        Writes ``OPENVPN_PROTOCOL``+``OPENVPN_ENDPOINT_PORT`` for all active OV
        stations and recreates them. Logs proto:port à chaque transition.
        """

        if protocol not in ("udp", "tcp"):
            logger.error("[vpn] _apply_ovpn_protocol: invalid %r", protocol)
            return False
        if self._stack_effective != "openvpn":
            logger.warning("[vpn] _apply_ovpn_protocol: not on OV stack (%s)", self._stack_effective)
            return False
        cur_proto = getattr(self, "_ovpn_protocol_effective", "udp")
        cur_port = getattr(self, "_ovpn_endpoint_port_effective", "1194" if cur_proto == "udp" else "443")
        # Q6: cycle complet udp:1194→tcp:443→tcp:8443 (custom only, nordvpn ignore port)
        if self._config.get("custom_ovpn_file"):
            cur_key = f"{cur_proto}:{cur_port}"
            try:
                cur_idx = self._ovpn_ports.index(cur_key)
            except ValueError:
                cur_idx = 0 if cur_proto == "udp" else 1
                if cur_port == "8443":
                    cur_idx = 2
            nxt = None
            for offset in range(1, len(self._ovpn_ports) + 1):
                cand = self._ovpn_ports[(cur_idx + offset) % len(self._ovpn_ports)]
                cand_proto = cand.split(":")[0]
                if protocol != cur_proto and cand_proto != protocol:
                    continue
                nxt = cand
                break
            if nxt is None:
                nxt = self._ovpn_ports[(cur_idx + 1) % len(self._ovpn_ports)]
            target_proto, target_port_str = nxt.split(":")
            if target_proto == cur_proto and target_port_str == cur_port:
                logger.info("[vpn] ovpn endpoint already %s:%s — no-op", cur_proto, cur_port)
                return True
            protocol = target_proto
        else:
            # nordvpn: protocol toggle only (custom port not allowed)
            if protocol == cur_proto:
                logger.info("[vpn] ovpn protocol already %s — no-op", cur_proto)
                return True
            target_port_str = "1194" if protocol == "udp" else "443"
        # Per-station independent: only this station flips protocol (auto hétérogène)
        stations = [self._station]
        services = [self._compose_service]
        compose_path = self._compose_file_path()
        env_path = os.path.join(os.path.dirname(compose_path), ".env")
        _env_lock_held = False
        try:
            # [P4 race .env] même protection que _apply_stack : la réécriture
            # du .env est atomique (tmp+replace) mais le read-modify-write
            # concurrent avec un autre écrivain perdait des clés.
            _ENV_RW_LOCK.acquire()
            _env_lock_held = True
            data = ""
            try:
                with open(env_path, encoding="utf-8") as f:
                    data = f.read()
            except FileNotFoundError:
                pass
            lines = data.splitlines()
            out = []
            seen = False
            seen_port = False
            per_key = f"OPENVPN_PROTOCOL_STATION{self._station}"
            per_port_key = f"OPENVPN_ENDPOINT_PORT_STATION{self._station}"
            for ln in lines:
                stripped = ln.strip()
                if stripped.startswith(per_key + "="):
                    out.append(f"{per_key}={protocol}")
                    seen = True
                elif self._config.get("custom_ovpn_file") and stripped.startswith(per_port_key + "="):
                    out.append(f"{per_port_key}={target_port_str}")
                    seen_port = True
                elif stripped.startswith("OPENVPN_PROTOCOL=") and not stripped.startswith("OPENVPN_PROTOCOL_STATION"):
                    out.append(ln)
                elif self._config.get("custom_ovpn_file") and stripped.startswith("OPENVPN_ENDPOINT_PORT=") and not stripped.startswith("OPENVPN_ENDPOINT_PORT_STATION"):
                    out.append(ln)
                else:
                    out.append(ln)
            if not seen:
                out.append(f"{per_key}={protocol}")
            if self._config.get("custom_ovpn_file") and not seen_port:
                out.append(f"{per_port_key}={target_port_str}")
            # Ensure global fallback exists
            if not any(l.strip().startswith("OPENVPN_PROTOCOL=") for l in out):
                out.append(f"OPENVPN_PROTOCOL={protocol}")
            else:
                for i, l in enumerate(out):
                    if l.strip().startswith("OPENVPN_PROTOCOL=") and not l.strip().startswith("OPENVPN_PROTOCOL_STATION"):
                        out[i] = f"OPENVPN_PROTOCOL={protocol}"
                        break
            if self._config.get("custom_ovpn_file"):
                if not any(l.strip().startswith("OPENVPN_ENDPOINT_PORT=") for l in out):
                    out.append(f"OPENVPN_ENDPOINT_PORT={target_port_str}")
                else:
                    for i, l in enumerate(out):
                        if l.strip().startswith("OPENVPN_ENDPOINT_PORT=") and not l.strip().startswith("OPENVPN_ENDPOINT_PORT_STATION"):
                            out[i] = f"OPENVPN_ENDPOINT_PORT={target_port_str}"
                            break
            tmp = env_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(out) + "\n")
            os.replace(tmp, env_path)
        except OSError as e:
            logger.error("[vpn] _apply_ovpn_protocol: cannot write %s: %s", env_path, e)
            return False
        finally:
            if _env_lock_held:
                _env_lock_held = False
                _ENV_RW_LOCK.release()
        try:
            prev = getattr(self, "_ovpn_protocol_effective", "udp")
            prev_port = getattr(self, "_ovpn_endpoint_port_effective", "1194" if prev == "udp" else "443")
            self._ovpn_protocol_effective = protocol
            self._ovpn_protocol = protocol
            if self._config.get("custom_ovpn_file"):
                self._ovpn_endpoint_port_effective = target_port_str
                self._ovpn_endpoint_port = target_port_str
                try:
                    self._ovpn_port_idx = self._ovpn_ports.index(f"{protocol}:{target_port_str}")
                except ValueError:
                    pass
            cmd = ["compose", "-f", compose_path, "up", "-d", "--force-recreate"] + sorted(services)
            env = self._compose_env(stations=stations, stack="openvpn")
            result = await asyncio.to_thread(self._docker_run, cmd, 300, env=env)
            if result.returncode != 0:
                self._ovpn_protocol_effective = prev
                self._ovpn_protocol = prev
                if self._config.get("custom_ovpn_file"):
                    self._ovpn_endpoint_port_effective = prev_port
                    self._ovpn_endpoint_port = prev_port
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        except Exception as e:
            logger.error("[vpn] _apply_ovpn_protocol: compose failed: %s", e)
            return False
        # Per-station independent: no propagate to siblings (auto hétérogène)
        # Persist selected (global fallback)
        try:
            from config.settings import yaml_set

            yaml_set("ip_rotation", "ovpn_protocol", protocol)
            if self._config.get("custom_ovpn_file"):
                yaml_set("ip_rotation", "ovpn_endpoint_port", target_port_str)
        except Exception:
            pass
        if self._config.get("custom_ovpn_file"):
            self._flips.append(
                {
                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "from": f"openvpn-{prev}:{prev_port}",
                    "to": f"openvpn-{protocol}:{target_port_str}",
                    "reason": reason,
                }
            )
            self._flips = self._flips[-20:]
            logger.warning("[vpn] ovpn endpoint: %s:%s→%s:%s (%s)", prev, prev_port, protocol, target_port_str, reason)
        else:
            self._flips.append(
                {
                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "from": f"openvpn-{prev}",
                    "to": f"openvpn-{protocol}",
                    "reason": reason,
                }
            )
            self._flips = self._flips[-20:]
            logger.warning("[vpn] ovpn protocol: %s→%s (%s)", prev, protocol, reason)
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
            logger.info("[vpn] stack: auto (effective %s, watchdog resumes)", self._stack_effective)
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
        now = self._now_fn()
        return {
            "selected": self._stack,
            "effective": self._stack_effective,
            "ovpn_protocol": getattr(self, "_ovpn_protocol", "udp"),
            "ovpn_protocol_effective": getattr(self, "_ovpn_protocol_effective", "udp"),
            "ovpn_ports": getattr(self, "_ovpn_ports", ["udp:1194","tcp:443","tcp:8443"]),
            "ovpn_port_idx": getattr(self, "_ovpn_port_idx", 0),
            "auto_hetero_boot": getattr(self, "_auto_hetero_boot", False),
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
            "control_last_401_at": getattr(self, "_control_last_401_at", None),
            "control_last_error": getattr(self, "_control_last_error", None),
            "socks5_eof_count": getattr(self, "_socks5_eof_count", 0),
            # [Bug #4] flip observability — mirrors get_status for dashboard
            "pending_flip": self._pending_flip,
            "flip_blocked_reason": self._last_flip_blocked_reason,
            "cooldown_remaining_s": (
                0
                if self._last_auto_flip_at is None
                else max(0, int(
                    self._auto_flip_cooldown_min * 60
                    - (now - self._last_auto_flip_at)
                ))
            ),
            "auth_failed_oldest_age_s": (
                None
                if not self._auth_failed_window
                else max(0, int(now - self._auth_failed_window[0]))
            ),
            "wg_key_file": self._wg_key_file,
            "stack_since_age_s": (
                None
                if self._stack_since is None
                else max(0, int(now - self._stack_since))
            ),
            "wg_canary_ok": self._wg_canary_state["ok"],
            "wg_canary_age_s": (
                None
                if self._wg_canary_state["at"] is None
                else max(0, int(now - self._wg_canary_state["at"]))
            ),
        }

    async def _check_auth_failed(self, started_at: str = "", text: str | None = None) -> bool:
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
        [v10 §14.1.10] ``text`` pré-fetché par refresh_status → un SEUL docker
        logs par tick pour auth+server_issue (budget subprocess ÷2).
        """
        if text is None:
            since = started_at if started_at else "10m"
            result = await asyncio.to_thread(self._docker_run, ["logs", "--since", since, self._docker_container], 30
            )
            if result.returncode != 0:
                return False
            text = result.stdout
        if "AUTH_FAILED" not in text and "auth failed" not in text.lower():
            return False  # never auth-failed
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
            return  # no hostname in this text — nothing to blacklist
        now = time.time()
        entry = self._failed_hosts.get(host)
        if entry is None:
            entry = {"failures": 0, "first_failed_at": now, "bad_until": 0.0}
            self._failed_hosts[host] = entry
        entry["failures"] += 1
        entry["bad_until"] = now + _FAILED_HOST_TTL
        logger.warning(
            "[vpn] AUTH_FAILED on %s (failure #%d, TTL 24h) — fast-pin will skip it",
            host,
            entry["failures"],
        )

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

    async def _check_server_issue(self, started_at: str = "", text: str | None = None) -> bool:
        """Scan container logs for a TLS negotiation failure (stale server
        IP or crashed server — gluetun's 🔌 'server no longer valid'
        guidance). Detection: the raw openvpn line that guidance attaches to
        in internal/openvpn/logs.go. Same recovery-aware bounding as
        _check_auth_failed: a TLS failure superseded by a later successful
        connection is stale, not live. [v10 §14.1.10] ``text`` partagé."""
        if text is None:
            since = started_at if started_at else "10m"
            result = await asyncio.to_thread(self._docker_run, ["logs", "--since", since, self._docker_container], 30
            )
            if result.returncode != 0:
                return False
            text = result.stdout.lower()
        if "tls key negotiation failed" not in text:
            return False
        return text.rfind("initialization sequence completed") < text.rfind(
            "tls key negotiation failed"
        )

    async def _check_restart_churn(self, window_min: int = 10) -> bool:
        """Scan container logs for a gluetun healthcheck-restart LOOP.

        [plan 20/08] A marginal WG tunnel makes gluetun's internal healthcheck
        (HEALTH_RESTART_VPN=on) restart the VPN every ~12 s — the
        'restarting VPN because it failed to pass the healthcheck' marker —
        while the SOCKS5 egress probe samples the live windows and sees
        nothing. Unlike _check_auth_failed there is no per-attempt success
        marker to bound to (gluetun logs 'wireguard setup is complete' on
        EVERY attempt, even the ones killed 11 s later), so the bounding
        event is OUR last successful recovery (_restart_churn_recovered_at):
        the scan window snaps to after it, exactly like docker's own
        filtering — markers written BEFORE it are stale, new ones re-arm.

        Self-resolving: when the restarts stop, a scan sees no fresh markers
        and the flag clears — no separate 'resolved' event needed.
        """
        if self._restart_churn_recovered_at is not None:
            # Recovery boundary: only markers written after the last
            # successful recovery count (docker logs span recoveries).
            elapsed = max(1.0, time.time() - self._restart_churn_recovered_at)
            since = f"{int(elapsed)}s"
        else:
            since = f"{max(2, int(window_min))}m"
        result = await asyncio.to_thread(self._docker_run, ["logs", "--since", since, self._docker_container], 30
        )
        if result.returncode != 0:
            return False
        count = result.stdout.count("restarting VPN because it failed to pass the healthcheck")
        if count >= self._restart_churn_threshold:
            logger.warning(
                "[vpn] healthcheck-restart churn: %d restarts in the last "
                "%s (threshold %d) — arming egress watchdog",
                count,
                since,
                self._restart_churn_threshold,
            )
            return True
        return False

    async def _wait_healthy(self, timeout: float = 120.0) -> str | None:
        """Wait until the container runs AND the SOCKS5 tunnel answers.

        Returns the container's StartedAt on success — callers bind their
        AUTH_FAILED scan to it, so a pre-restart AUTH_FAILED still in the
        logs cannot flip state (docker logs span container restarts, [15]).
        Returns None on failure/timeout.
        [P3 perf] backoff exponentiel poll×1.5 plafonné à 2 s : un gluetun
        met typiquement 5-30 s à devenir sain ; sonder docker+IP toutes les
        500 ms pendant toute la fenêtre n'accélère rien et multiplie les
        subprocess ×4 stations. L'IP-sweep ne tourne déjà qu'une fois le
        conteneur ``running`` (court-circuit du and).
        """
        deadline = time.monotonic() + timeout
        empty_inspects = 0
        delay = max(0.1, float(self._wait_healthy_poll))
        while time.monotonic() < deadline:
            info = await self._docker_inspect()
            if info.get("running") and await self.get_public_ip():
                return info.get("started_at", "")
            # Fail fast: AUTH_FAILED (rejected credentials) and TLS negotiation
            # failures (dead server) never recover on their own — do not sit
            # out the timeout, report immediately (TLS fails in ~20 s).
            # [plan v10 §14.3.14] inspect VIDE (glitch CLI docker, daemon busy)
            # = transitoire : 3 tolérés avant de déclarer mort — l'ancien code
            # tuait la rotation sur un simple inspect raté.
            if not info:
                empty_inspects += 1
                if empty_inspects >= 3:
                    return None
                await asyncio.sleep(self._wait_healthy_poll)
                continue
            empty_inspects = 0
            if (
                not info.get("running")
                or await self._check_auth_failed(info.get("started_at", ""))
                or await self._check_server_issue(info.get("started_at", ""))
            ):
                return None
            await asyncio.sleep(delay)
            delay = min(2.0, delay * 1.5)  # [P3 perf] backoff exponentiel
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

    async def _docker_image_id(self, image: str) -> str | None:
        result = await asyncio.to_thread(self._docker_run, ["image", "inspect", image, "--format", "{{.Id}}"], 30
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    async def _docker_repo_digest(self, image: str) -> str | None:
        result = await asyncio.to_thread(self._docker_run,
            ["image", "inspect", image, "--format", "{{index .RepoDigests 0}}"],
            30,
        )
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
            result = await asyncio.to_thread(self._docker_run,
                ["compose", "-f", compose_file, "pull", self._compose_service],
                300,
                env=self._compose_env(),
            )
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
                return {"ok": False, "error": "aucune mise à jour disponible"}
            if check_opportune and not self._update_opportune():
                return {"ok": False, "error": "pas opportun (trafic actif)"}
            if check_opportune and self._active_free_streams > 0:
                return {"ok": False, "error": "streams gratuits actifs — mise à jour reportée"}
            if not self._acquire_update_lock():
                return {"ok": False, "error": "une autre instance applique déjà une mise à jour"}
            try:
                compose_file = self._compose_file_path()
                result = await asyncio.to_thread(self._docker_run,
                    [
                        "compose",
                        "-f",
                        compose_file,
                        "up",
                        "-d",
                        "--pull",
                        "never",
                        self._compose_service,
                    ],
                    120,
                    env=self._compose_env(),
                )
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip() or result.stdout.strip())
                started_at = await self._wait_healthy(timeout=120)
                if started_at is None:
                    raise RuntimeError("tunnel non sain après mise à jour")
                if await self._check_auth_failed(started_at):
                    self._auth_failed = True
                    raise RuntimeError("AUTH_FAILED après mise à jour")
                self._auth_failed = False
                # Fresh IP + NEW identity: the recreate re-picked a server, so
                # validate the IP is not recent on either station and advance
                # the identity ([plan] C.4). Failure rolls back below.
                if not await self._finalize_ip(allow_stale=False):
                    raise RuntimeError("impossible de finaliser une nouvelle IP après mise à jour")
                # force: container was just recreated — a cached status
                # would report the pre-update container ([37]).
                # The recreate IS a gluetun recovery: snap the churn scan
                # window AFTER it, or pre-update restart markers would
                # re-arm the egress watchdog for nothing (same anti
                # stale-marker pattern as the watchdog recovery).
                self._restart_churn_recovered_at = time.time()
                self._churn_next_due = 0.0  # [v10 §14.1.10] re-scan frais immédiat
                await self.refresh_status(force=True)
                self._update_available = False
                self._update_applied_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self._update_known_since = None
                self._update_old_image_id = None
                self.save_state()
                logger.info(
                    "[vpn-update] applied — container recreated with new image (IP %s)",
                    self._current_ip,
                )
                return {"ok": True, "ip": self._current_ip}
            except Exception as e:
                self._update_last_error = str(e)
                logger.error("[vpn-update] apply failed, rolling back: %s", e)
                await self._rollback_update(self._update_old_image_id)
                return {"ok": False, "error": str(e)}
            finally:
                self._release_update_lock()

    async def _rollback_update(self, old_image_id: str | None) -> None:
        """Best-effort rollback: re-tag the previous image and recreate."""
        if not old_image_id:
            logger.error("[vpn-update] no previous image ID — cannot roll back")
            return
        try:
            image = "qmcgaw/gluetun"
            await asyncio.to_thread(self._docker_run, ["tag", old_image_id, image], 30)
            compose_file = self._compose_file_path()
            await asyncio.to_thread(self._docker_run,
                [
                    "compose",
                    "-f",
                    compose_file,
                    "up",
                    "-d",
                    "--pull",
                    "never",
                    self._compose_service,
                ],
                120,
                env=self._compose_env(),
            )
            # force: container was just recreated — see _apply_update ([37]).
            # Same churn-scan snap as _apply_update (the recreate is a
            # recovery; pre-rollback markers must not re-arm the watchdog).
            self._restart_churn_recovered_at = time.time()
            self._churn_next_due = 0.0  # [v10 §14.1.10] re-scan frais immédiat
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
        if (
            self._update_known_since
            and time.monotonic() - self._update_known_since > self._update_max_defer_hours * 3600
        ):
            logger.info(
                "[vpn-update] deferring >%dh — applying anyway", self._update_max_defer_hours
            )
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
        if (
            self._last_free_request_at is None
            or time.monotonic() - self._last_free_request_at >= self._update_idle_minutes * 60
        ):
            return True
        return False

    async def _updater_loop(self) -> None:
        """Background loop: detect available updates and apply them at the
        most opportune moment. First iteration runs immediately, then every
        update_check_interval seconds.
        [P3 perf] stagger par index de station : les N managers ne tirent
        plus ``docker compose pull`` quasi simultanément toutes les 6 h
        (N× bande passante + N× charge docker daemon pour LA MÊME image) —
        le premier check est décalé de ``_station × 90 s``."""
        try:
            _idx = max(0, int(getattr(self, "_station", 1)) - 1)
            await asyncio.sleep(_idx * 90)
        except asyncio.CancelledError:
            raise
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
        self._egress_failures = max(self._egress_failures, self._auto_wg_egress_ticks)
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
        # [plan 20/08] Recovery boundary: pre-recovery churn markers are
        # stale (docker logs span recoveries) — set BEFORE the internal
        # refresh so its churn scan snaps to after this instant.
        self._restart_churn_recovered_at = time.time()
        self._churn_next_due = 0.0  # [v10 §14.1.10] re-scan frais immédiat
        await self.refresh_status(force=True)
        self.save_state()
        logger.info("[vpn-watchdog] recovered — tunnel healthy (IP %s)", self._current_ip)
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
        logger.info(
            "[vpn-watchdog] active — interval %ds, backoff %ds → %ds",
            self._watchdog_interval,
            self._watchdog_backoff._base_delay,
            self._watchdog_backoff._max_delay,
        )
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
            delay = (
                self._watchdog_backoff.delay
                if self._watchdog_backoff.consecutive_failures > 0
                else self._egress_failure_tick_interval
                if self._egress_failures > 0
                else self._watchdog_interval
            )
            # [P3.5 perf] jitter ±20% uniquement sur branche healthy (interval)
            # jamais sur backoff ni cadence armée — dual-wait docker intact
            # (désactivé sous pytest pour ne pas casser les assertions exactes)
            if delay == self._watchdog_interval and not os.getenv("PYTEST_CURRENT_TEST"):
                delay *= random.uniform(0.8, 1.2)
            # Sleep dually on the interval AND container events: every
            # die/stop/kill/start (docker events watcher sets
            # _watchdog_event) wakes the loop immediately, so recovery
            # starts within ms instead of on the next tick. None-guarded:
            # the watcher may be disabled. A stale set (event set while
            # wait_for raced its timeout) costs one extra tick — harmless.
            if self._watchdog_event is not None:
                try:
                    await asyncio.wait_for(self._watchdog_event.wait(), timeout=delay)
                    self._watchdog_event.clear()
                except TimeoutError:
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
                # [P3 perf] tick sain : le refresh docker lourd (inspect +
                # logs, ~2-4 subprocess ×4 stations) ne tourne que si des
                # events conteneur sont arrivés depuis le dernier tick OU si
                # le cache status est expiré. Le watcher docker_events réveille
                # déjà la boucle en ms sur die/stop/kill — le polling serré
                # n'apporte rien en régime stable.
                _events = getattr(self, "_docker_events_since_tick", 0)
                _status_fresh = (
                    self._last_status_refresh_at is not None
                    and time.monotonic() - self._last_status_refresh_at
                    < max(self._STATUS_CACHE_SECONDS, 30.0)
                )
                if _events > 0 or not _status_fresh:
                    await self.refresh_status(force=True)
                self._docker_events_since_tick = 0
            egress_dead = False
            # [plan 18/08 §E1] Light egress probe — SOCKS5 handshake +
            # CONNECT to the ip_check endpoint, bounded by the probe budget,
            # NO GET (dead/alive suffices; _finalize_ip does the full GET
            # when the tunnel returns). Stack-agnostic: the incident tunnel
            # was "connected" with no AUTH_FAILED/TLS marker — invisible
            # without a probe. Same connect chain as get_public_ip.
            try:
                tunnel_alive = await asyncio.wait_for(
                    self._probe_tunnel_light(), self._ip_probe_budget
                )
            except TimeoutError:
                # [plan 19/08] Half-open tunnel (TCP accepted, SOCKS5/CONNECT
                # never answers — gluetun healthcheck-restart signature): the
                # sweep worst case (n endpoints × probe + close) exceeds the
                # budget, and the wait_for timeout used to kill the tick
                # BEFORE the failure counter — the egress watchdog never
                # armed and the dead tunnel stayed dead forever (the
                # empty-message "loop error:" in the logs). Budget expiry =
                # failed probe: the counter increments and the restart path
                # below owns recovery.
                tunnel_alive = False
            if not tunnel_alive:
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
                    logger.warning(
                        "[vpn-watchdog] egress dead %d/%d ticks — waiting",
                        self._egress_failures,
                        self._auto_wg_egress_ticks,
                    )
                    return
            else:
                if self._egress_failures:
                    logger.info("[vpn-watchdog] egress OK — counter reset")
                self._egress_failures = 0
            # [plan 18/08 §3c] auto-mode policy — decided inside the lock on
            # fresh counters, applied AFTER it (_apply_stack takes the lock;
            # asyncio.Lock is not reentrant, calling it here would deadlock).
            self._pending_flip = self._auto_flip_decision()
            # Emergency flip for manual stacks: same thresholds but gated on
            # at least one failed heal attempt, so a transient blip is first
            # healed on the current stack before flipping.
            if self._pending_flip is None and self._stack != "auto":
                self._pending_flip = self._emergency_flip_decision()
            # [plan 20/08] _restart_churn: the healthcheck-restart LOOP is a
            # failing tunnel even while the SOCKS5 probe happens to land in a
            # live window — the flag alone routes the tick into the recovery
            # chain (the refresh scan that armed it keeps the flag fresh).
            if (
                not (self._auth_failed or self._server_issue or self._restart_churn)
                and not egress_dead
            ):
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
                    # [v6 P1-3] heal actif au lieu de return infini (Exited → 1/4 300s)
                    try:
                        self.arm_egress_watchdog()
                        if getattr(self, "_watchdog_event", None) is not None:
                            try:
                                self._watchdog_event.set()
                            except Exception:
                                pass
                        # container absent → schedule ensure outside lock (avoid deadlock)
                        if not info:
                            try:
                                asyncio.create_task(self._ensure_container())
                            except Exception:
                                pass
                    except Exception:
                        pass
                    return  # absent/stopped/restarting — other paths own the lifecycle (now with heal scheduled)
                if egress_dead:
                    kind = "egress dead"
                elif self._auth_failed:
                    kind = "AUTH_FAILED"
                elif self._restart_churn:
                    kind = "healthcheck restart loop"
                else:
                    kind = "TLS negotiation timeout"
                logger.warning(
                    "[vpn-watchdog] %s detected — restarting %s", kind, self._docker_container
                )
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
                        logger.warning(
                            "[vpn-watchdog] light restart failed: %s — escalating to compose", e
                        )
                        started_at = None
                    # [mypy] _wait_healthy -> str | None ; le court-circuit
                    # bool() protégeait déjà l'exécution, on l'explicite.
                    healed = False
                    if started_at:
                        healed = not (
                            await self._check_auth_failed(started_at)
                            or await self._check_server_issue(started_at)
                        )
                    if healed and await self._watchdog_recover_fresh_ip():
                        return
                    if not healed:
                        logger.info(
                            "[vpn-watchdog] restart did not clear %s — compose escalation", kind
                        )
                        try:
                            await self._ensure_container()
                            started_at = await self._wait_healthy(timeout=120)
                        except Exception as e:
                            logger.warning("[vpn-watchdog] compose escalation failed: %s", e)
                            started_at = None
                        healed = False
                        if started_at:
                            healed = not (
                                await self._check_auth_failed(started_at)
                                or await self._check_server_issue(started_at)
                            )
                        if healed and await self._watchdog_recover_fresh_ip():
                            return
                    # Still failing: keep the error state and back off. This
                    # tail is inside the try — with no exception it runs ONCE
                    # after both rungs failed to heal; with an exception the
                    # except below records the same single failure.
                    self._watchdog_backoff.record_failure()
                    escalate = self._watchdog_backoff.consecutive_failures >= 2
                    if self._pending_flip is None and self._stack != "auto":
                        self._pending_flip = self._emergency_flip_decision()
                    logger.error(
                        "[vpn-watchdog] restart did not recover — next "
                        "attempt in %ds (failure #%d)",
                        int(self._watchdog_backoff.delay),
                        self._watchdog_backoff.consecutive_failures,
                    )
                except Exception as e:
                    self._watchdog_backoff.record_failure()
                    escalate = self._watchdog_backoff.consecutive_failures >= 2
                    if self._pending_flip is None and self._stack != "auto":
                        self._pending_flip = self._emergency_flip_decision()
                    logger.error(
                        "[vpn-watchdog] restart failed: %s — next attempt in %ds",
                        e,
                        int(self._watchdog_backoff.delay),
                    )
        # [plan 18/08 §3c] auto flip (+ emergency manual flip) — applied OUTSIDE the lock (compose).
        # A successful flip supersedes the escalation: the tunnel was just
        # recreated in the other stack, no need to refresh servers/image too.
        # [canari WG 25/08] AUCUN flip vers wireguard sans preuve d'egress :
        # le canari valide le chemin WG réel ; sans lui, le retour « OV sain
        # → WG préféré » rejouait la panne en boucle (bug stations 2-3/4).
        if self._pending_flip is not None:
            await self._cancel_wg_flip_if_canary_dead()
        if self._pending_flip is not None:
            mode, reason = self._pending_flip
            self._pending_flip = None
            is_emergency = reason.startswith("emergency")
            # OV protocol flip (udp->tcp) is a lighter heal than full stack flip:
            # same VPN_TYPE=openvpn, only OPENVPN_PROTOCOL changes (1194->443).
            # Works for both auto and emergency (firewall blocks UDP but TCP 443 passes).
            if mode == "openvpn" and "UDP" in reason and "-> TCP" in reason:
                ok = await self._apply_ovpn_protocol("tcp", reason=reason)
                if ok:
                    return
                # Protocol flip failed -> fall through to stack flip as escalation
            if is_emergency:
                ok = await self._apply_stack(mode, reason=reason, auto=True)
                if ok:
                    # Propagate to sibling stations (registry sync) — the compose call recreated ALL
                    # active services, so every manager must reflect the new effective stack.
                    for _m in getattr(shared_state, "vpn_managers", []) or []:
                        if _m is not None and _m is not self:
                            _m._stack_effective = mode
                            _m._stack_since = self._stack_since
                            _m._last_auto_flip_at = self._last_auto_flip_at
                            if _m._stack != "auto":
                                _m._stack = mode
                    if self._stack != "auto":
                        prev = self._stack
                        self._stack = mode
                        try:
                            from config.settings import yaml_set  # persist selected (survives reboot)

                            yaml_set("ip_rotation", "vpn_stack", mode)
                        except Exception:
                            pass
                        logger.warning(
                            "[vpn-watchdog] emergency flip %s→%s applied (manual stack overridden, now %s)",
                            prev,
                            mode,
                            mode,
                        )
                    return
            else:
                # Auto hétérogène: per-station independent (each station heals itself)
                ok = await self._apply_stack(mode, reason="auto: " + reason, auto=True, stations=[self._station])
                if ok:
                    # No global propagate in per-station auto — each station keeps its own
                    # effective stack/protocol. The fleet becomes heterogeneous (WG + OV UDP + OV TCP)
                    # which is the resilience the user requested.
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
        if self._watchdog_escalated_at and time.monotonic() - self._watchdog_escalated_at < 1800:
            return
        self._watchdog_escalated_at = time.monotonic()
        logger.warning(
            "[vpn-watchdog] escalating: refreshing %s servers list + checking image update",
            self._server_provider,
        )
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
            if (
                m.get("Type") == "volume"
                and m.get("Destination", "").rstrip("/") == "/gluetun"
                and m.get("Name")
            ):
                return m["Name"]
        return "gluetun"

    async def _refresh_servers_list(self) -> None:
        """One-shot `gluetun update -providers <provider>` into the named
        volume backing /gluetun: rewrites the servers file with the current
        list, works without a running tunnel, and the local file takes
        precedence over the embedded list at next start. Fails soft."""
        try:
            volume = await self._resolve_gluetun_volume()
            result = await asyncio.to_thread(self._docker_run,
                [
                    "run",
                    "--rm",
                    "-v",
                    f"{volume}:/gluetun",
                    "qmcgaw/gluetun",
                    "update",
                    "-providers",
                    self._server_provider,
                ],
                180,
            )
            if result.returncode != 0:
                logger.error(
                    "[vpn-watchdog] servers refresh returned %d: %s",
                    result.returncode,
                    result.stdout[-500:],
                )
        except Exception as e:
            logger.error("[vpn-watchdog] servers refresh failed: %s", e)

    # ── State persistence ──────────────────────────────────────

    def _get_state_path(self) -> str:
        """Return path to the VPN state file (per-station: station 2 keeps
        its own history/circuit breaker in logs/vpn_state2.json)."""
        return self._state_file

    def _save_state_sync(self):
        """[P5.4] Coeur synchrone de save_state — I/O fichier pur (copy2+dump).

        L'écriture reste atomique tmp+replace ; seul ce coût est déporté
        via `to_thread` par le wrapper debouncé `save_state`."""
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
                "ovpn_protocol": getattr(self, "_ovpn_protocol", "udp"),
                "ovpn_protocol_effective": getattr(self, "_ovpn_protocol_effective", "udp"),
                "ovpn_endpoint_port": getattr(self, "_ovpn_endpoint_port", "1194"),
                "ovpn_endpoint_port_effective": getattr(self, "_ovpn_endpoint_port_effective", "1194"),
                "auto_hetero_boot": getattr(self, "_auto_hetero_boot", False),
                "flips": self._flips,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            state_path = self._get_state_path()
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            # [plan 18/08 §4.1] last-good copy BEFORE the overwrite.
            if os.path.exists(state_path):
                shutil.copy2(state_path, state_path + ".bak")
            tmp_path = state_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, state_path)
            logger.debug("[vpn] state saved to %s", state_path)
        except Exception as e:
            logger.debug("[vpn] failed to save state: %s", e, exc_info=True)

    async def _save_state_async(self):
        """[P5.4] Offload file I/O hors boucle via to_thread."""
        try:
            await asyncio.to_thread(self._save_state_sync)
            self._save_state_last = time.monotonic()
        except Exception:
            pass

    async def _save_state_debounced(self, delay: float):
        await asyncio.sleep(delay)
        await self._save_state_async()

    def save_state(self):
        """Persist IP history, stats, and circuit breaker state to disk.

        Atomic write ([20]): temp file + os.replace, so a crash mid-write
        can never leave a truncated state file. [plan 18/08 §4.1] the
        current on-disk state is copied to ``*.bak`` (last-good) BEFORE the
        overwrite, so a corrupted/empty write still leaves a recoverable
        previous snapshot. Failures are logged with traceback but never
        raised: saving must stay non-fatal for the runtime.

        [P5.4 perf] débouncé (1s) + offloadé `to_thread` — le coût boucle
        copy2+dump est déporté ; l'écriture reste atomique tmp+replace.
        Appels sans boucle (tests) → fallback sync immédiat.
        """
        # [tests] en pytest, le save doit être synchrone pour que les assertions sur le fichier passent
        if os.getenv("PYTEST_CURRENT_TEST"):
            self._save_state_sync()
            self._save_state_last = time.monotonic()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Hors boucle (tests, boot synchrone) — fallback sync direct
            self._save_state_sync()
            self._save_state_last = time.monotonic()
            return
        now = time.monotonic()
        # Coalesce si une sauvegarde est déjà en attente dans la fenêtre
        if self._save_state_task is not None and not self._save_state_task.done():
            return
        remaining = self._save_state_debounce - (now - self._save_state_last)
        if remaining > 0:
            self._save_state_task = loop.create_task(self._save_state_debounced(remaining))
        else:
            self._save_state_task = loop.create_task(self._save_state_async())

    def load_state(self):
        """Load persisted state from disk."""
        try:
            state_path = self._get_state_path()
            if not os.path.exists(state_path):
                return
            try:
                with open(state_path) as f:
                    state = json.load(f)
            except (json.JSONDecodeError, OSError) as _corrupt:
                # [plan v10 §14.3.7] le .bak last-good (écrit AVANT chaque
                # overwrite §4.1) n'était JAMAIS tenté au chargement : une
                # corruption du fichier principal persistait jusqu'au
                # prochain save réussi. Fallback last-good maintenant.
                bak = state_path + ".bak"
                logger.warning(
                    "[vpn] state corrupt (%s) — retry last-good %s", _corrupt, bak
                )
                with open(bak) as f:
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
                    k: {
                        "failures": v.get("failures", 0),
                        "opened_at": v.get("opened_at", 0),
                        "state": v.get("state", "closed"),
                    }
                    for k, v in cb.items()
                    if isinstance(v, dict)
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
                k: v
                for k, v in state.get("failed_hosts", {}).items()
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
            # OV protocol/port persistence (udp/tcp + 1194/443/8443)
            _rp = state.get("ovpn_protocol")
            if _rp in ("udp", "tcp"):
                self._ovpn_protocol = _rp
            _rpe = state.get("ovpn_protocol_effective")
            if _rpe in ("udp", "tcp"):
                self._ovpn_protocol_effective = _rpe
            _rpep = state.get("ovpn_endpoint_port")
            if _rpep in ("1194", "443", "8443", 1194, 443, 8443):
                self._ovpn_endpoint_port = str(_rpep)
            _rpepe = state.get("ovpn_endpoint_port_effective")
            if _rpepe in ("1194", "443", "8443", 1194, 443, 8443):
                self._ovpn_endpoint_port_effective = str(_rpepe)
            # hetero-boot flag
            _rhetero = state.get("auto_hetero_boot")
            if isinstance(_rhetero, bool):
                self._auto_hetero_boot = _rhetero
            restored_flips = state.get("flips")
            if isinstance(restored_flips, list):
                self._flips = [f for f in restored_flips if isinstance(f, dict)][-20:]
            logger.info(
                "[vpn] state loaded: %d IPs in history, %d total switches",
                len(self._ip_history),
                self._total_switches,
            )
        except Exception as e:
            logger.debug("[vpn] failed to load state: %s", e)


# ── Boot reconcile — orphan / stale-stack container cleanup (plan 18/08 §2.2) ──


def _docker_cli(
    args: list[str], timeout: int = 30, env: dict | None = None
) -> subprocess.CompletedProcess:
    """Blocking docker CLI call WITHOUT the rotation funnel.

    Boot-time only (reconcile_orphan_containers runs before any rotation
    exists), so there is no rotation-op accounting — a plain subprocess.run
    with the same flags as VPNManager._docker_run. RuntimeError on a missing
    docker CLI, same contract as the manager's helper.
    """
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except FileNotFoundError as _fnf:
        raise RuntimeError("CLI docker introuvable dans le PATH") from _fnf


def _env_value_from_inspect(info: dict, key: str) -> str | None:
    """Read one Config.Env entry from a docker inspect payload.

    Config.Env is a list of "KEY=VALUE" strings; the compose interpolation
    surfaces the stack exactly like that (e.g. "VPN_TYPE=wireguard").
    Returns None when the key is absent (old container, or a malformed
    payload) — the caller treats unknown as "keep" (conservative)."""
    try:
        env_list = info["Config"]["Env"]
    except (KeyError, TypeError):
        return None
    for entry in env_list or []:
        k, _, v = entry.partition("=")
        if k == key:
            return v or None
    return None


def _stack_from_env_file(env_path: str, station: int) -> str | None:
    """[plan v10 §14.1.1] stack RÉELLE de la station depuis le .env persisté
    (clé ``VPN_TYPE_STATION{n}`` écrite par chaque _apply_stack).

    L'ancienne heuristique boot (_stack_effective = wireguard si le fichier
    clé WG existe) faisait rm -f de TOUTE une flotte OpenVPN saine à chaque
    reboot dès que wireguard.env traînait encore sur disque. None = clé
    absente/fichier illisible → l'appelant retombe sur l'heuristique."""
    try:
        with open(env_path, encoding="utf-8") as f:
            prefix = f"VPN_TYPE_STATION{station}="
            for line in f:
                stripped = line.strip()
                if stripped.startswith(prefix):
                    val = stripped.split("=", 1)[1].strip().lower()
                    if val in ("wireguard", "openvpn"):
                        return val
                    return None
    except OSError:
        pass
    return None


async def reconcile_orphan_containers(managers: list, runner=None) -> list[str]:
    """[plan 18/08 §2.2] Remove fleet containers the boot must not keep.

    A crash (or a stack flip that died mid-way) leaves docker containers
    that do not match the manager registry: either a station retired by a
    downscale that never got its ``docker rm -f``, or a container booted on
    the stale stack (the 19/08 case) that survives into the new process.
    Called in lifespan BEFORE the start() gather, so the fleet comes up
    exactly as configured; start() then creates/repairs what is missing.

    Rules (fail-soft — a docker error is logged, never raised, so a broken
    docker daemon cannot block boot):
      * enumerate ``docker ps -a`` filtered on the fleet prefix (anchored
        name regex ^/opencode-vpn: excludes opencode-proxy and the
        opencode-wg-test canary, which do not share the prefix);
      * a name outside {m._docker_container for m in managers} is an orphan
        → ``docker rm -f`` (named volumes survive: rm without -v);
      * an expected name whose container runs VPN_TYPE != that station's
        _stack_effective is a stale-stack survivor → removed too (start()
        recreates it on the right stack — no wait for the watchdog);
      * "No such container" from rm is success (a compose teardown raced
        the boot — nothing to remove).

    ``runner`` is an injectable async callable (args, timeout, env) ->
    CompletedProcess for offline tests; the default is the module-level
    _docker_cli via asyncio.to_thread (no funnel — boot time).

    Returns the list of removed container names.
    """
    if not managers:
        return []
    run = runner or (
        lambda args, timeout=30, env=None: asyncio.to_thread(_docker_cli, args, timeout, env)
    )
    expected = {m._docker_container for m in managers}
    removed: list[str] = []

    async def _rm(name: str) -> None:
        try:
            res = await run(["rm", "-f", name], 30, None)
        except (RuntimeError, subprocess.SubprocessError) as e:
            logger.warning("[vpn] boot reconcile: rm %s failed — %s", name, e)
            return
        if res.returncode != 0 and "No such container" not in (res.stderr or ""):
            logger.warning(
                "[vpn] boot reconcile: rm %s failed rc=%d: %s",
                name,
                res.returncode,
                res.stderr.strip(),
            )

    try:
        result = await run(
            ["ps", "-a", "--filter", "name=^/opencode-vpn", "--format", "{{.Names}}"], 30, None
        )
    except (RuntimeError, subprocess.SubprocessError) as e:
        logger.warning("[vpn] boot reconcile: ps failed — %s", e)
        return []
    if result.returncode != 0:
        logger.warning(
            "[vpn] boot reconcile: ps failed rc=%d: %s", result.returncode, result.stderr.strip()
        )
        return []
    names = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    by_name = {m._docker_container: m for m in managers}

    for name in names:
        # inspect une seule fois : sert à la grâce ET au check de stack
        try:
            insp = await run(["inspect", name], 15, None)
        except (RuntimeError, subprocess.SubprocessError):
            insp = None
        info = None
        if insp is not None and insp.returncode == 0:
            try:
                info = json.loads(insp.stdout)[0]
            except (json.JSONDecodeError, IndexError, KeyError):
                info = None  # payload vide/malformé : traité comme inconnu

        # [plan v10 incident 25/08 matin] PÉRIODE DE GRÂCE : un conteneur
        # démarré depuis <120 s n'est JAMAIS un orphelin — les courses
        # (watchdog qui recrée vs reconcile qui purge) supprimaient des
        # stations fraîchement recréées, en boucle. Garde effective :
        # tout conteneur VIVANT absent du registre est conservé (plus bas).
        # [P6] le calcul mort `age_sec` (jamais lu) est supprimé.

        m = by_name.get(name)
        if name not in expected:
            # [incident 25/08 v2] conteneur VIVANT absent du registre = course
            # probable (watchdog qui recrée vs lifespan qui purge) → JAMAIS
            # supprimé tant qu'il tourne. Seuls les conteneurs arrêtés sortis
            # du registre sont purgés (vrais restes de downscale).
            running = bool((info or {}).get("State", {}).get("Running"))
            if running:
                logger.warning(
                    "[vpn] boot reconcile: %s running mais absent du registre — "
                    "conservé (course probable)",
                    name,
                )
                continue
            removed.append(name)
            await _rm(name)
            continue
        if m is None:
            continue
        vpn_type = _env_value_from_inspect(info or {}, "VPN_TYPE")
        # [plan v10 §14.1.1] stack attendue = .env PERSISTÉ par station, pas
        # l'heuristique fichier-clé recalculée au boot : une flotte basculée
        # OpenVPN avec wireguard.env encore présent voyait tous ses tunnels
        # sains rm -f puis recréés WG à chaque reboot.
        try:
            _env_path = os.path.join(os.path.dirname(m._compose_file_path()), ".env")
            expected_stack = _stack_from_env_file(_env_path, m._station)
        except Exception:
            expected_stack = None
        if expected_stack is None:
            expected_stack = m._stack_effective or "openvpn"  # fallback historique
        if vpn_type is not None and vpn_type != expected_stack:
            logger.warning(
                "[vpn] boot reconcile: %s stack=%s != attendu=%s (source .env) -> rm",
                name,
                vpn_type,
                expected_stack,
            )
            removed.append(name)
            await _rm(name)
    if removed:
        logger.warning(
            "[vpn] boot reconcile removed %d container(s): %s", len(removed), ", ".join(removed)
        )
    return removed
