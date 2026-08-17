"""
Free IP pool for rotating IP addresses on free model requests.

Each VPN session gives a fresh IP = fresh free model quota.
Routes free requests through the compose-managed gluetun tunnel (SOCKS5).

Dual-station ("double embrayage"): when ip_rotation.dual_station is
enabled, TWO gluetun stations run in parallel (A = station 1, B =
station 2) like a dual-clutch gearbox — one gear always engaged. A
request lands on the best available station; as soon as a station is
bad (recent 429), at quota, or its tunnel is down, the OTHER takes over
immediately while the bad one rotates in the background. A good IP is
always available, so free quota is always spent on a fresh (model, IP)
cooldown key. The 429 rotation behavior itself is unchanged ([0]/[42]):
rotate in background + paid fallback, with the strict-free GUI option
refusing to pay when both stations are exhausted.
"""

import time
import logging
import asyncio
from typing import Optional

from vpn_manager import RotationFailed, VPNManager

logger = logging.getLogger(__name__)


class FreeIPPool:
    """Manages IP rotation for free model requests.

    Routes free model requests through the gluetun SOCKS5 tunnel(s)
    (compose-managed Docker containers). With dual_station, holds two
    stations [A, B] and picks the best one per request — never waits for
    a rotation.
    """

    _CONNECT_RETRY_INTERVAL = 300  # min seconds between docker reconnect attempts when down
    _BAD_TTL = 60  # seconds a 429 keeps a station out of rotation before it can be retried

    def __init__(self, vpn_manager: VPNManager,
                 vpn_manager_2: Optional[VPNManager] = None):
        self._vpn = vpn_manager  # station 1 (legacy attribute — always present)
        self._stations = [vpn_manager]
        if vpn_manager_2 is not None:
            self._stations.append(vpn_manager_2)
        self._active_station: Optional[VPNManager] = None  # last station used by on_request
        self._total_free_requests = 0
        # Per-station state (request counters, IP stats, 429-bad TTL...)
        self._per: dict[int, dict] = {}
        # Single-flight background rotation PER STATION: concurrent 429s
        # (stream + non-stream, 4 stream handlers) share ONE rotation per
        # station (and ``VPNManager.connect_next`` is itself single-flight
        # on top).
        self._rotation_tasks: dict[int, asyncio.Task] = {}
        # Timing attrs — seeded from the class defaults, overridden by
        # update_config() (config.yaml `ip_rotation`, hot-reloadable).
        self._connect_retry_interval = float(self._CONNECT_RETRY_INTERVAL)
        self._bad_ttl = float(self._BAD_TTL)
        self._rotation_stagger = 10  # C3: station N rotates (N-1)*stagger earlier
        # Serialized rotation coordinator (C4/C5): ONE worker drains the
        # queue, so two stations never run physical rotations at the same
        # time. `_pending` dedups stations already queued (single-flight
        # per station, alongside `_rotation_tasks` for in-flight ones).
        self._rotation_queue: asyncio.Queue = asyncio.Queue()
        self._pending: set[int] = set()
        self._worker_task: Optional[asyncio.Task] = None

    # ── Properties ─────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._vpn.enabled

    @property
    def proxy_mode(self) -> str:
        """Return the current proxy mode: vpn, or direct."""
        return self._vpn.proxy_mode

    @property
    def dual_station(self) -> bool:
        """True when a second station was supplied (dual-clutch active)."""
        return len(self._stations) > 1

    @property
    def proxy_url(self) -> Optional[str]:
        """Return the SOCKS5 proxy URL of the best available station.

        Gluetun is the only backend (compose-managed Docker): free requests
        are routed via SOCKS5 (the HTTP proxy is not routed on Windows
        Docker Desktop). Returns None when not connected or disabled.
        """
        if not self._vpn.enabled or self._vpn.proxy_mode != "vpn":
            return None
        best = self._best_station()
        if best is None or best.status != "connected":
            return None
        return best.socks5_url

    @property
    def active_station(self) -> Optional[VPNManager]:
        """The station used by the last ``on_request()`` call.

        ``None`` when no free request went through the pool yet, or the
        pool is disabled/direct — callers fall back to station 1
        (``self._vpn``) as before.
        """
        return self._active_station

    # ── Station selection (double embrayage) ───────────────────

    def _per_station(self, station: VPNManager) -> dict:
        """Per-station mutable state, created lazily."""
        sid = station._station
        per = self._per.get(sid)
        if per is None:
            per = self._per[sid] = {
                "request_count": 0,
                "session_start": None,
                "ip_stats": {},
                "last_connect_attempt": None,
                "last_quota_per_ip": None,  # hot-reload detection (CRITIC(11))
                "bad_until": None,          # set by a 429 (double embrayage)
            }
        return per

    def _rotation_threshold(self, station: VPNManager) -> int:
        """Quota-per-IP at which a station should rotate (C3).

        Station N rotates ``rotation_stagger * (N - 1)`` requests earlier
        than station 1, so the two stations' quota walls never coincide —
        station 2 always crosses its threshold first and rotates while
        station 1 keeps serving (and vice versa on the next cycle)."""
        return max(0, station._quota_per_ip - 10 -
                   self._rotation_stagger * max(0, station._station - 1))

    def _station_usable(self, station: VPNManager, *, exclude_approaching: bool) -> bool:
        """A station is usable when its tunnel is up and it is not marked
        bad by a recent 429. ``exclude_approaching`` also rules out stations
        at (or beyond) their rotation threshold (preferred pass) — the
        second pass admits them so the request still gets a shot at the
        free tier instead of falling back to paid while a background
        rotation is in flight."""
        per = self._per_station(station)
        if per["bad_until"] and time.monotonic() < per["bad_until"]:
            return False
        if station.status != "connected":
            return False
        if exclude_approaching and per["request_count"] >= self._rotation_threshold(station):
            return False
        return True

    def _best_station(self) -> Optional[VPNManager]:
        """Best station for the next request — preference A (station 1)
        unless it is bad (recent 429) or its tunnel is down.

        Approaching quota is NOT excluded here: the caller's threshold
        branch in ``on_request`` performs the dual-clutch switch (other
        station now, departed station rotating in the background).
        Excluding it here would return the other station directly and
        never launch the departed station's rotation — it would sit
        parked at quota until both stations were exhausted.
        """
        for st in self._stations:
            if self._station_usable(st, exclude_approaching=False):
                return st
        return None

    def _best_station_excluding(self, station: VPNManager) -> Optional[VPNManager]:
        """Best station other than ``station`` (for an immediate dual-clutch
        switch when the current one approaches quota)."""
        for exclude_approaching in (True, False):
            for st in self._stations:
                if st is station:
                    continue
                if self._station_usable(st, exclude_approaching=exclude_approaching):
                    return st
        return None

    def _any_other_usable(self, station: VPNManager) -> bool:
        """True when at least one OTHER station is usable right now (C1 —
        the "never bad-mark the last standing station" guard)."""
        for st in self._stations:
            if st is station:
                continue
            if self._station_usable(st, exclude_approaching=False):
                return True
        return False

    # ── Connection / rotation ──────────────────────────────────

    async def ensure_connected(self):
        """Ensure every station is connected (demand-driven, NON-blocking).

        (C6) The request path never awaits a docker operation: a station
        whose tunnel is down gets a BACKGROUND reconnect — throttled per
        station by ``connect_retry_interval`` and serialized through the
        same rotation queue (C4) — and requests fall back to paid
        immediately until it is back. ``connect_next`` can raise
        ``RotationFailed`` (CRITIC(5)): the background task logs it, it
        never propagates into the request path (fail-open by design).
        """
        if not self._vpn.enabled:
            return
        for st in self._stations:
            if st.proxy_mode == "vpn" and st.status != "connected":
                self._kick_connect(st)

    def _kick_connect(self, station: VPNManager) -> None:
        """Launch a background reconnect for a down station (C6).

        Throttled per station by ``connect_retry_interval`` (C2) and
        serialized through the same rotation queue as 429 rotations (C4) —
        a reconnect IS a rotation (fresh IP). Never awaited from the
        request path."""
        per = self._per_station(station)
        now = time.monotonic()
        if per["last_connect_attempt"] and \
                now - per["last_connect_attempt"] < self._connect_retry_interval:
            return
        per["last_connect_attempt"] = now
        self._launch_rotation(station)

    async def on_request(self) -> tuple[Optional[str], Optional[VPNManager]]:
        """Called before each free model request.

        Returns ``(proxy_url, station)`` — the SOCKS5 URL of the best
        station and that station itself (opencode.py keys the (model, IP)
        cooldown on the station's IP and marks THAT station on 429).
        ``(None, None)`` when disabled or no tunnel — caller falls back to
        direct/paid as before.

        Dual-clutch: never wait for a rotation. If the best station is
        approaching quota and another station is usable, switch to the
        other station IMMEDIATELY and let the departed station rotate in
        the background.
        """
        if not self._vpn.enabled:
            return None, None

        await self.ensure_connected()

        self._total_free_requests += 1

        mode = self._vpn.proxy_mode
        if mode == "vpn":
            station = self._best_station()
            if station is None:
                return None, None
            self._active_station = station
            per = self._per_station(station)
            # Hot-reload guard (CRITIC(11)): when quota_per_ip changed via
            # config, the counter refers to the OLD quota — reset lazily so
            # the new quota applies from the next request on.
            if per["last_quota_per_ip"] is not None and \
                    per["last_quota_per_ip"] != station._quota_per_ip:
                logger.info("[free-ip] quota_per_ip changed %s → %s — resetting request counter",
                            per["last_quota_per_ip"], station._quota_per_ip)
                per["request_count"] = 0
            per["last_quota_per_ip"] = station._quota_per_ip

            # Approaching quota (C3 threshold — station 2 crosses first):
            # switch to the other station NOW (rotation of the departed one
            # runs in the background — zero wait, zero paid fallback). When
            # no other station is usable, keep serving on this one and
            # rotate it in the background — the request path never blocks
            # on a docker rotation (C6).
            if per["request_count"] >= self._rotation_threshold(station):
                other = self._best_station_excluding(station)
                if other is not None:
                    logger.info("[free-ip] station %d approaching quota (%d/%d) — "
                                "dual-clutch switch to station %d, rotating in background",
                                station._station, per["request_count"],
                                station._quota_per_ip, other._station)
                    self._launch_rotation(station)
                    station = other
                    self._active_station = station
                    per = self._per_station(station)
                    per["last_quota_per_ip"] = station._quota_per_ip
                else:
                    logger.info("[free-ip] station %d at threshold (%d/%d), "
                                "rotating in background (no other station usable)",
                                station._station, per["request_count"], station._quota_per_ip)
                    self._kick_connect(station)

            # Only count requests that actually went through the tunnel —
            # when the VPN is down, requests go direct on a residential IP
            # and must not advance the rotation counter ([5]).
            per["request_count"] += 1
            # Track activity for opportune update timing
            station.note_free_request()
            # Track stats for the current station IP
            ip = station.current_ip or "unknown"
            if ip not in per["ip_stats"]:
                per["ip_stats"][ip] = {
                    "requests": 0,
                    "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "server": station.current_server.get("name", "?") if station.current_server else "?",
                }
            per["ip_stats"][ip]["requests"] += 1

            return station.socks5_url, station

        return None, None

    async def switch_ip(self, station: Optional[VPNManager] = None) -> str:
        """Switch the given station (default: the active/last used one) to
        a fresh VPN IP — honest single attempt (CRITIC(5)).

        The retry/backoff logic lives in ``VPNManager.connect_next`` (3
        attempts + exponential backoff + fail-fast cooldown after a total
        failure). A wrap-retry loop here would either duplicate that work or
        immediately hit the cooldown — so this is ONE call that either
        returns the new IP or raises ``RotationFailed``.

        Raises:
            RotationFailed: rotation failed or was gated (circuit breaker
                open, fail-fast cooldown, tunnel down). Never a silent
                success.
        """
        station = station or self._active_station or self._vpn
        new_ip = await station.connect_next()
        if not new_ip:
            # Defensive: connect_next is typed to return str now, but a
            # None would silently reset counters below — fail loudly instead.
            raise RotationFailed("connect_next returned no IP")
        per = self._per_station(station)
        per["request_count"] = 0
        per["session_start"] = time.monotonic()
        return new_ip

    def on_quota_exhausted(self, station: Optional[VPNManager] = None):
        """Free quota exhausted (429): mark the station bad and rotate it
        in the background — the next request lands on the other station.

        [0]/[42] restored: a 429 is answered with an IP rotation, NOT just a
        paid fallback. Fire-and-forget and single-flight — concurrent 429s
        share ONE rotation per station (``_rotation_tasks``, and
        ``VPNManager.connect_next`` is itself single-flight on top). The
        calling request falls back to paid immediately; the next free
        attempt lands on the fresh IP.

        The cooldown is keyed per (model, IP) ([4]), so the rotation gives
        the model a FRESH cooldown key — the new IP is not blocked by the
        429 that triggered this rotation.

        Double embrayage: the 429 also marks this station bad for
        `bad_ttl` seconds, so requests switch to the other station
        instead of burning attempts on a (model, IP) key that is still
        cooling down. When the background rotation succeeds, the station
        becomes eligible again immediately (fresh IP = fresh key).

        C1 — never bad-mark the last standing station: the bad-mark is only
        set when ANOTHER station is usable right now. On a sole station
        (single-station mode, or the other one already down) the mark is
        SKIPPED — it would remove the only usable station and free traffic
        would fall back DIRECT on the residential IP (violates [0]: quota
        resets on IP change). The background rotation still runs; the
        (model, IP) cooldown gate keeps repeat free requests off the
        cooling IP meanwhile.

        No-op when VPN is disabled or in direct mode.
        """
        if not self._vpn.enabled or self._vpn.proxy_mode != "vpn":
            return
        station = station or self._active_station or self._vpn
        if self.dual_station:
            if self._any_other_usable(station):
                self._per_station(station)["bad_until"] = (
                    time.monotonic() + self._bad_ttl)
        self._launch_rotation(station)

    def _launch_rotation(self, station: VPNManager) -> None:
        """Queue a background rotation for one station (C4/C5).

        Serialized: a single worker drains ``_rotation_queue``, so at most
        ONE physical rotation runs at any time (the invariant is about
        stations being down, not rotations racing). Dedup both ways: a
        station with a rotation already queued (``_pending``) or already in
        flight (``_rotation_tasks``) is never queued twice — concurrent 429s
        on the same station share one rotation."""
        sid = station._station
        if sid in self._pending:
            return
        task = self._rotation_tasks.get(sid)
        if task and not task.done():
            return  # a rotation for this station is already running
        self._pending.add(sid)
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._rotation_worker())
        self._rotation_queue.put_nowait(station)

    async def _rotation_worker(self) -> None:
        """Drain the rotation queue — one physical rotation at a time (C4),
        in enqueue order. Station 2's lower threshold (C3) makes it rotate
        first while station 1 keeps serving."""
        while True:
            station = await self._rotation_queue.get()
            self._pending.discard(station._station)
            try:
                await self._rotate_station(station)
            except Exception as e:
                # _rotate_station swallows its own errors; this guard keeps
                # the worker alive against any programming mistake.
                logger.warning("[free-ip] rotation worker error: %s", e)

    async def _rotate_station(self, station: VPNManager):
        sid = station._station
        # Register in-flight BEFORE the first await: the worker is the only
        # task that ever rotates, so ``current_task()`` is exactly the
        # "rotation running" marker `_launch_rotation` checks — a re-launch
        # of the same station while this one runs is deduped, another
        # station's rotation queues behind it (serialized).
        self._rotation_tasks[sid] = asyncio.current_task()
        try:
            await self.switch_ip(station=station)
            # Rotation succeeded: the station has a fresh IP (fresh
            # (model, IP) cooldown key) — make it eligible again now,
            # without waiting out the bad TTL.
            self._per_station(station)["bad_until"] = None
        except Exception as e:
            # CRITIC(5): a failed background rotation must be logged, not
            # swallowed — the next free request will fall back to paid and
            # retry the rotation later (or not, if the fail-fast cooldown
            # is active in VPNManager).
            logger.warning("[free-ip] station %d background rotation failed: %s",
                           station._station, e)
        finally:
            self._rotation_tasks.pop(sid, None)

    def update_config(self, cfg: dict) -> None:
        """Apply timing overrides from config.yaml ``ip_rotation`` (hot-reload,
        same body the dashboard sends to the VPN managers)."""
        if not isinstance(cfg, dict):
            return
        if "connect_retry_interval" in cfg:
            self._connect_retry_interval = max(
                5.0, float(cfg["connect_retry_interval"] or 0))
        if "bad_ttl" in cfg:
            self._bad_ttl = max(0.0, float(cfg["bad_ttl"] or 0))
        if "rotation_stagger" in cfg:
            self._rotation_stagger = max(0, int(cfg["rotation_stagger"] or 0))

    def get_status(self) -> dict:
        """Return pool status for the dashboard (aggregated over stations).

        Legacy top-level fields (vpn_status, current_ip, ...) report
        station 1 for backward compatibility; the dashboard's dual-station
        view reads ``stations`` + ``active_station``.
        """
        stations = []
        for st in self._stations:
            per = self._per_station(st)
            stations.append({
                "station": st._station,
                "vpn_status": st.status,
                "current_ip": st.current_ip,
                "current_server": st.current_server.get("name") if st.current_server else None,
                "requests_this_ip": per["request_count"],
                "quota_per_ip": st._quota_per_ip,
                "rotation_threshold": self._rotation_threshold(st),
                "remaining": max(0, st._quota_per_ip - per["request_count"]),
                "bad_until": per["bad_until"],
                # seconds left of the post-429 cooldown (dashboard display)
                "bad_remaining": max(0, per["bad_until"] - time.monotonic()) if per["bad_until"] else 0,
                "last_rotation_error": getattr(st, "_last_rotation_error", None),
                "vpn": st.get_status(),
            })
        active = self._active_station or self._vpn
        s1 = stations[0]

        return {
            "enabled": self._vpn.enabled,
            "proxy_mode": self._vpn.proxy_mode,
            "dual_station": self.dual_station,
            "active_station": active._station,
            "stations": stations,
            # Legacy top-level fields = station 1 (dashboard backward compat)
            "vpn_status": s1["vpn_status"],
            "current_ip": s1["current_ip"],
            "current_server": s1["current_server"],
            "requests_this_ip": s1["requests_this_ip"],
            "quota_per_ip": s1["quota_per_ip"],
            "remaining": s1["remaining"],
            "total_free_requests": self._total_free_requests,
            # Timings/hot-reload state (dashboard panel + tests)
            "connect_retry_interval": self._connect_retry_interval,
            "bad_ttl": self._bad_ttl,
            "rotation_stagger": self._rotation_stagger,
            "rotate_pending": sorted(self._pending),
            "ips_used": len({ip for p in self._per.values() for ip in p["ip_stats"]}),
            "ip_stats": {ip: st for p in self._per.values() for ip, st in p["ip_stats"].items()},
            "vpn": self._vpn.get_status(),
        }
