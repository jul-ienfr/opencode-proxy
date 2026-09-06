"""
Free IP pool for rotating IP addresses on free model requests.

Each VPN session gives a fresh IP = fresh free model quota.
Routes free requests through the compose-managed gluetun tunnel (SOCKS5).

Multi-station ("double embrayage"): when the resolved ip_rotation
station count (resolved_station_count — station_count (1-10), legacy
`dual_station: true` ⇒ 2, absent ⇒ 1) is >= 2, N gluetun stations run
in parallel (stations 1..N) like a dual-clutch gearbox — one gear
always engaged. A request lands on the best available station; as soon
as a station is bad (recent 429), at quota, or its tunnel is down, the
NEXT best takes over immediately while the bad one rotates in the
background. A good IP is always available, so free quota is always
spent on a fresh (model, IP) cooldown key. The active set is
hot-swappable with set_stations (GUI 1-10 dropdown, no proxy restart),
the worker is never cancelled. The 429 rotation behavior itself is
unchanged ([0]/[42]): rotate in background + paid fallback, with the
strict-free GUI option refusing to pay when every station is
exhausted.
"""

import asyncio
import logging
import math
import random
import time
import urllib.parse
import weakref

from vpn_manager import RotationFailed, VPNManager

try:
    from config.settings import yaml_get as _yaml_get
except Exception:
    def _yaml_get(section, key, default=None):  # fallback
        return default

logger = logging.getLogger(__name__)


def _clamp_seconds(cfg: dict, key: str, lo: float, hi: float) -> float | None:
    """[plan 30/08 — règle transversale] Lit une clé de durée (secondes) avec
    bornes : valeur invalide → warning + None (l'appelant garde sa valeur
    courante) ; hors plage → clamp + warning.

    Retourne None quand la clé est absente ou invalide — jamais d'écrasement
    silencieux d'un réglage existant par un défaut."""
    if key not in cfg:
        return None
    raw = cfg[key]
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "[pool-config] %s=%r invalide — valeur courante conservée", key, raw
        )
        return None
    if math.isnan(val):
        logger.warning(
            "[pool-config] %s=%r invalide (NaN) — valeur courante conservée", key, raw
        )
        return None
    if val < lo or val > hi:
        clamped = max(lo, min(hi, val))
        logger.warning(
            "[pool-config] %s=%s hors bornes [%s, %s] — clamp à %s",
            key, raw, lo, hi, clamped,
        )
        return clamped
    return val


class Socks5Endpoint:
    """[Axe 3.1] One static SOCKS5 proxy, duck-typed to look like a VPNManager
    station to the pool's request/selection machinery.

    Lives OUTSIDE ``_stations``: its ``_station`` is a negative id
    (``-1 - index``) so it can never collide with docker stations 1..10, and
    ``_launch_rotation``'s ``_station_ids`` guard rejects it — a static proxy
    has no docker container to rotate. ``status`` is "connected" by
    definition (the URL is reachable until the request path says otherwise);
    ``current_ip``/``current_server`` stay None until the request path has
    actually been served through it. ``note_free_request`` and
    ``arm_egress_watchdog`` are no-ops (no docker activity, no watchdog to
    arm — the pool's bad-mark handles a dead proxy).
    """

    def __init__(self, host, port, username=None, password=None, enabled=True, index=0):
        self.host = str(host)
        self.port = int(port)
        self.username = username
        self.password = password
        self.enabled = bool(enabled)
        self._station = -1 - int(index)  # negative — never a docker station
        self.pid = f"{self.host}:{self.port}"
        self._quota_per_ip = 0  # filled by set_socks5_proxies / update_config
        self.status = "connected"
        self.current_ip = None
        self.current_server = None

    @property
    def has_password(self) -> bool:
        return bool(self.password)

    @property
    def socks5_url(self) -> str:
        """socks5://[user:pass@]host:port — auth URL-escaped."""
        if self.username or self.password:
            user = urllib.parse.quote(self.username or "", safe="")
            pwd = urllib.parse.quote(self.password or "", safe="")
            auth = f"{user}:{pwd}@"
        else:
            auth = ""
        return f"socks5://{auth}{self.host}:{self.port}"

    def note_free_request(self) -> None:
        pass  # nothing to account for on a static proxy

    def arm_egress_watchdog(self) -> None:
        pass  # no docker watchdog — the pool bad-mark owns recovery

    def get_status(self) -> dict:
        return {
            "pid": self.pid,
            "host": self.host,
            "port": self.port,
            "enabled": self.enabled,
            "has_password": self.has_password,
            "quota_per_ip": self._quota_per_ip,
        }


class FreeIPPool:
    """Manages IP rotation for free model requests.

    Routes free model requests through the gluetun SOCKS5 tunnel(s)
    (compose-managed Docker containers). With station_count >= 2, holds
    N stations [1..N] and picks the best one per request — never waits
    for a rotation. The active set is hot-swappable (set_stations).
    """

    _CONNECT_RETRY_INTERVAL = 300  # min seconds between docker reconnect attempts when down
    _BAD_TTL = 60  # seconds a 429 keeps a station out of rotation before it can be retried
    _on_429_action = "both"  # class fallback for object.__new__ hermetic pools ("cooldown"|"rotate"|"both")
    # [Axe 1.1] Rotation concurrency: how many stations may rotate at the
    # same time. Bounded — a single blocked rotation (budget up to
    # rotation_wait_timeout, plus docker ops) must not freeze the fleet.
    _ROTATION_CONCURRENCY = 2
    # [Axe 1.2] Consecutive post-commit-probe failures that trigger an
    # IMMEDIATE re-rotation. After the cap the watchdog owns recovery —
    # a persistently flapping tunnel must not hot-loop the docker stack.
    _POST_COMMIT_RETRY_MAX = 2

    def __init__(self, vpn_manager: VPNManager, vpn_manager_2: VPNManager | None = None):
        self._vpn = vpn_manager  # station 1 (legacy attribute — always present)
        self._stations = [vpn_manager]
        if vpn_manager_2 is not None:
            self._stations.append(vpn_manager_2)
        # [plan 18/08 §2.3] Station-number set mirroring ``_stations`` —
        # O(1) "is this station known?" check for _launch_rotation (a
        # request handler holding a RETIRED manager must not re-queue it
        # after a downscale).
        self._station_ids = {m._station for m in self._stations}
        # [v6 P1-4] pick+increment race — 2 on_request concurrentes sur même snapshot
        self._pool_pick_lock = asyncio.Lock()
        self._active_station: VPNManager | None = None  # last station used by on_request
        self._total_free_requests = 0
        # Per-station state (request counters, IP stats, 429-bad TTL...)
        self._per: dict[int, dict] = {}
        # Single-flight background rotation PER STATION: concurrent 429s
        # (stream + non-stream, 4 stream handlers) share ONE rotation per
        # station (and ``VPNManager.connect_next`` is itself single-flight
        # on top).
        self._rotation_tasks: dict[int, asyncio.Task] = {}
        # Timing attrs — seeded from the class defaults (= comportement
        # historique), overridden by update_config() (config.yaml
        # `ip_rotation`, hot-reloadable).
        # [plan 30/08 — constantes → config] clés dédiées (comportement
        # exact inchangé à défaut) :
        #   station_connect_retry_interval_s (défaut 300 s) remplace
        #     _CONNECT_RETRY_INTERVAL codé en dur — la clé historique
        #     ``connect_retry_interval`` reste lue tel quel plus bas.
        #   station_bad_ttl_s (défaut 60 s) remplace _BAD_TTL codé en dur
        #     (les 429 gardent une station hors rotation). On ne recycle PAS
        #     la clé ``bad_ttl`` : elle porte la sémantique blacklist fast-pin
        #     en MINUTES côté vpn_manager (lot A2) depuis le 30/08.
        self._connect_retry_interval = float(self._CONNECT_RETRY_INTERVAL)
        self._bad_ttl = float(self._BAD_TTL)
        try:
            _cfg_seed = getattr(vpn_manager, "_config", None) or {}
            for _key, _attr in (
                ("station_connect_retry_interval_s", "_connect_retry_interval"),
                ("station_bad_ttl_s", "_bad_ttl"),
            ):
                _v = _clamp_seconds(_cfg_seed, _key, 1.0, 3600.0)
                if _v is not None:
                    setattr(self, _attr, _v)
        except Exception:
            pass
        self._on_429_action = "both"
        # [PLAN-corrections-429 G2] miroir de IP_ROTATION["strict_free"] —
        # la pool ne peut pas importer opencode (cycle), elle reçoit la
        # valeur via update_config() (config.yaml au boot + hot-reload).
        self._strict_free = False
        # [PLAN-corrections-429 G4] callback épuisement-compte failover,
        # poussé par opencode.py (set_failover_exhausted_cb) — jamais importé.
        self._failover_exhausted_cb = None
        # [plan v10 §3.6 Lot 3] Moteur de rotation latence-adaptive — partagé
        # (singleton), config canonique `ip_rotation.latency_rotation`.
        try:
            from latency_rotation import get_engine

            self.latency_engine = get_engine()
        except Exception:
            self.latency_engine = None
        # [review 18/08 F1a] Late-signal absorption window — SHORT on
        # purpose (20 s, NOT bad_ttl): long enough for the dial queue of a
        # request launched before a rotation (connect timeout 10 s +
        # SOCKS5/TLS handshake) + the teardown recv of in-flight streams,
        # short enough that a genuinely dead freshly-rotated tunnel is
        # bad-marked within seconds (étage 0 must not re-strike it for a
        # full bad_ttl). Stragglers older than this are assumed real.
        self._late_signal_grace = 20.0
        self._rotation_stagger = 10  # C3: station N rotates (N-1)*stagger earlier
        # [17/08] Max seconds on_request waits for a background rotation
        # when the best station is over-threshold and no other station is
        # usable. Bounded: on timeout the request falls back to paid (None,
        # None) instead of serving the burned IP (guaranteed 429 → paid).
        # [P3.1-A perf] défaut 20 s → 5 s : le repli paid garanti existe déjà
        # (l.869-870) ; au-delà de 5 s d'attente d'IP fraîche, TTFB pire cas.
        self._rotation_wait_timeout = 5.0
        # [P3.1-B] hedge paid après rotation wait — 0 = désactivé, sinon ms
        self._paid_hedge_after_ms = 0.0
        # [Axe 1.1] Bounded rotation coordinator: up to _ROTATION_CONCURRENCY
        # workers drain the queue in parallel, so a blocked rotation on one
        # station (budget wait + docker ops) no longer freezes the fleet.
        # `_pending` dedups stations already queued (single-flight per
        # station, alongside `_rotation_tasks` for in-flight ones).
        self._rotation_queue: asyncio.Queue = asyncio.Queue()
        self._pending: set[int] = set()
        self._worker_tasks: list = []  # rotation workers, pruned lazily

        # [Axe 3.1] Static SOCKS5 proxies (socks5 mode — list of curated
        # proxies instead of docker stations; NO docker is ever touched).
        self._socks5_proxies: list = []  # validated config rows {host, port, ...}
        self._socks5_eps: list = []  # parsed Socks5Endpoint list (negative sids)
        self._socks5_rr = 0  # round-robin cursor (index into _socks5_eps)
        self._socks5_current = None  # last endpoint used by on_request
        # [Axe 3.1] True (default) = round-robin on every request; False =
        # stick to the current proxy while usable (rotation only via
        # bad-mark or the manual rotate endpoint).
        self._socks5_auto_rotate = True

        # [free_parallel] Deux routings découplés — (B) Stations free
        # enabled=false → pas de parallélisation (sticky), routing
        # round-robin = strict tour 1,2,3… (change à chaque requête), failover = sticky 1..N
        self._free_parallel_enabled = False
        self._free_parallel_routing = "round-robin"
        self._free_parallel_mode = "load-balance"
        self._free_parallel_hedge_delay_ms = 300
        self._free_parallel_hedge_max = 1
        self._rr_idx = 0  # strict round-robin compteur
        # observability hedge_wins counter
        self._hedge_wins = {"primary": 0, "hedge": 0, "total": 0}

    # ── Properties ─────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._vpn.enabled

    @property
    def proxy_mode(self) -> str:
        """Return the current proxy mode: vpn, socks5, or direct."""
        return self._vpn.proxy_mode

    @property
    def socks5_mode(self) -> bool:
        """True when free traffic routes through the static SOCKS5 list
        (proxy_mode == "socks5" and the pool is enabled)."""
        return self._vpn.enabled and self._vpn.proxy_mode == "socks5"

    @property
    def dual_station(self) -> bool:
        """True when a second station was supplied (dual-clutch active)."""
        return len(self._stations) > 1

    @property
    def proxy_url(self) -> str | None:
        """Return the SOCKS5 proxy URL of the best available station.

        Gluetun is the only backend (compose-managed Docker): free requests
        are routed via SOCKS5 (the HTTP proxy is not routed on Windows
        Docker Desktop). Returns None when not connected or disabled.

        [Axe 3.1] socks5 mode: the last-used static proxy (or the next
        round-robin one) — NO docker ever involved.
        """
        if not self._vpn.enabled:
            return None
        if self._vpn.proxy_mode == "socks5":
            ep = self._socks5_current or self._socks5_next()
            return ep.socks5_url if ep is not None else None
        if self._vpn.proxy_mode != "vpn":
            return None
        best = self._best_station()
        if best is None or best.status != "connected":
            return None
        return best.socks5_url

    @property
    def active_station(self) -> VPNManager | None:
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
                "last_confirmed_ip": None,  # [review F1b] repair anchor (current_ip)
                "ip_stats": {},
                "last_connect_attempt": None,
                "last_quota_per_ip": None,  # hot-reload detection (CRITIC(11))
                "bad_until": None,  # set by a 429 (double embrayage)
                # [Axe 1.2] Consecutive dead probes right after a rotation
                # commit (fresh tunnel committed but not egressing). Reset
                # on any alive probe; capped at _POST_COMMIT_RETRY_MAX
                # before the watchdog owns recovery (no docker hot-loop).
                "post_commit_retry_count": 0,
                # [PLAN-corrections-429 G4] failover cascade : 429 consécutifs
                # sur IPs différentes → épuisement compte (cooldown global).
                "consec_429": 0,
                "consec_429_ips": [],
            }
        return per

    def _rotation_threshold(self, station: VPNManager) -> int:
        """Quota-per-IP at which a station should rotate (C3).

        Station N rotates ``rotation_stagger * (N - 1)`` requests earlier
        than station 1, so the two stations' quota walls never coincide —
        station 2 always crosses its threshold first and rotates while
        station 1 keeps serving (and vice versa on the next cycle).

        [Revue 19/08] The floor is 1, NEVER 0: with a small quota
        (quota_per_ip 15, stagger 2 → stations 4-6 compute a negative
        headroom) the old ``max(0, ...)`` made every request cross the
        threshold and re-queue a rotation per request. The floor keeps the
        quota wall at a sane distance; the caller (``on_request``) routes a
        degenerate threshold (== 1) through the THROTTLED kick instead of
        the unthrottled dual-clutch launch — pacing rotations instead of
        hot-looping them."""
        return max(
            1, station._quota_per_ip - 10 - self._rotation_stagger * max(0, station._station - 1)
        )

    def _station_usable(
        self,
        station: VPNManager,
        *,
        exclude_approaching: bool,
        forced_pool=None,
        ignore_latency_cool: bool = False,
    ) -> bool:
        """A station is usable when its tunnel is up and it is not marked
        bad by a recent 429. ``exclude_approaching`` also rules out stations
        at (or beyond) their rotation threshold (preferred pass) — the
        second pass admits them so the request still gets a shot at the
        free tier instead of falling back to paid while a background
        rotation is in flight.
        [plan v10 §3.6.4 Lot 3] ``ignore_latency_cool`` = passe de secours
        LRU : admet une station dont l'IP courante est hard-cooled quand
        AUCUNE autre n'est disponible (jamais zéro candidat)."""
        if forced_pool is not None:
            cc = getattr(station, "_current_country", None) or getattr(
                station, "current_country", None
            )
            if cc is not None:
                try:
                    from vpn_manager import _normalize_country as _nc

                    if _nc(cc) not in forced_pool:
                        return False
                except Exception:
                    if cc not in forced_pool:
                        return False
        per = self._per_station(station)
        if per["bad_until"] and time.monotonic() < per["bad_until"]:
            return False
        if station.status != "connected":
            return False
        if not ignore_latency_cool:
            try:
                from latency_rotation import get_engine

                eng = getattr(self, "latency_engine", None) or get_engine()
                cur_ip = str(getattr(station, "current_ip", "") or "")
                # Garde cfg.enabled : un moteur latence désactivé (toggle GUI,
                # état importé…) ne filtre RIEN, même si des cooldowns ont
                # survécu à la purge de update_config.
                if cur_ip and eng.cfg.enabled and eng.ip_hard_cooled(int(station._station), cur_ip):
                    return False
            except Exception:
                pass
        if exclude_approaching and per["request_count"] >= self._rotation_threshold(station):
            return False
        return True

    def _non_routable_reason(self, station) -> str | None:
        """[fiabilisation 05/09 Lot 0] Pourquoi ``station`` n'est pas routable
        (base : mêmes branches et même ordre que ``_station_usable`` avec
        ``exclude_approaching=False``, sans ``forced_pool``). ``None`` =
        routable. Lecture seule : aucun effet sur la sélection."""
        try:
            per = self._per_station(station)
        except Exception:
            return "no-per-state"
        try:
            if per.get("bad_until") and time.monotonic() < per["bad_until"]:
                return f"bad_until {int(per['bad_until'] - time.monotonic())}s"
        except Exception:
            pass
        try:
            if station.status != "connected":
                return f"status={station.status}"
        except Exception:
            return "status=unknown"
        try:
            from latency_rotation import get_engine

            eng = getattr(self, "latency_engine", None) or get_engine()
            cur_ip = str(getattr(station, "current_ip", "") or "")
            if cur_ip and eng.cfg.enabled and eng.ip_hard_cooled(int(station._station), cur_ip):
                return "latency-hard-cool"
        except Exception:
            pass
        return None

    def usability_report(self) -> dict:
        """[fiabilisation 05/09 Lot 0] N/M routable + raison par station,
        pour le dashboard et le diagnostic « 1 seule station routable »."""
        report = {}
        for st in list(self._stations):
            try:
                sid = int(getattr(st, "_station", -1))
            except Exception:
                sid = -1
            report[sid] = self._non_routable_reason(st) or "routable"
        return report

    def _free_parallel_is_rr(self) -> bool:
        """True when free_parallel enabled + routing round-robin (tout mode sauf failover)."""
        return bool(self._free_parallel_enabled) and self._free_parallel_routing == "round-robin"

    def _best_station(self, forced_pool=None) -> VPNManager | None:
        """Best station for the next request.

        - enabled==false  → sticky station 1
        - enabled==true + failover → sticky 1..N (premier usable)
        - enabled==true + round-robin + load-balance → least-loaded (min request_count)
        - enabled==true + round-robin + strict → compteur strict 1,2,3… change à chaque requête
        - enabled==true + round-robin + hedge → primaire strict (hedge en course)
        Approaching quota NOT excluded here (dual-clutch géré dans on_request).
        """
        usable = [
            st for st in self._stations if self._station_usable(st, exclude_approaching=False, forced_pool=forced_pool)
        ]
        if not usable:
            # [fiabilisation 05/09] second pass ignorant le hard-cool latence
            # (miroir de pick_candidates §3.6.5) : sans ça, le chemin
            # on_request (qui passe par _best_station, pas pick_candidates)
            # rendait (None, None) alors qu'une station servable existait —
            # effondrement artificiel N→0/1 sur refroidissement seul.
            usable = [
                st for st in self._stations
                if self._station_usable(
                    st, exclude_approaching=False, forced_pool=forced_pool,
                    ignore_latency_cool=True,
                )
            ]
            if not usable:
                return None
        if not self._free_parallel_is_rr():
            return usable[0]
        # round-robin : selon mode
        if self._free_parallel_mode == "strict":
            usable.sort(key=lambda s: s._station)
            try:
                idx = self._rr_idx % len(usable)
                self._rr_idx = (self._rr_idx + 1) % 1000000
                return usable[idx]
            except Exception:
                return usable[0]
        if self._free_parallel_mode == "hedge":
            # hedge primaire = strict aussi (course)
            usable.sort(key=lambda s: s._station)
            try:
                idx = self._rr_idx % len(usable)
                self._rr_idx = (self._rr_idx + 1) % 1000000
                return usable[idx]
            except Exception:
                return usable[0]
        # load-balance (défaut) → least-loaded
        try:
            min_cnt = min(self._per_station(st)["request_count"] for st in usable)
            least = [st for st in usable if self._per_station(st)["request_count"] == min_cnt]
            if len(least) == 1:
                return least[0]
            return random.choice(least)
        except Exception:
            return usable[0]

    def _best_station_excluding(
        self, station: VPNManager, forced_pool=None
    ) -> VPNManager | None:
        """Best station other than ``station`` (for an immediate dual-clutch
        switch when the current one approaches quota)."""
        for exclude_approaching in (True, False):
            for st in self._stations:
                if st is station:
                    continue
                if self._station_usable(
                    st, exclude_approaching=exclude_approaching, forced_pool=forced_pool
                ):
                    return st
        return None

    def _best_station_excluding_many(self, excluded: set, forced_pool=None) -> VPNManager | None:
        """Best station NOT in ``excluded`` (cumulative retries: never
        re-strike an IP/bucket already used in this request's free attempts)."""
        for exclude_approaching in (True, False):
            for st in self._stations:
                if st in excluded:
                    continue
                if self._station_usable(
                    st, exclude_approaching=exclude_approaching, forced_pool=forced_pool
                ):
                    return st
        return None

    def _any_other_usable(self, station: VPNManager, forced_pool=None) -> bool:
        """True when at least one OTHER station is usable right now (C1 —
        the "never bad-mark the last standing station" guard)."""
        for st in self._stations:
            if st is station:
                continue
            if self._station_usable(st, exclude_approaching=False, forced_pool=forced_pool):
                return True
        return False

    def pick_candidates(self, forced_pool=None) -> list:
        """[free_parallel] Return up to N usable stations sorted by routing.

        - enabled==false or N==1 → 1 seule (pas de hedge)
        - round-robin → least-loaded d'abord (tri par request_count)
        - failover → ordre 1..N (sticky)
        Filtre bad_until/status/geo via _station_usable(exclude_approaching=False
        si hedge pour brûler 1 slot mais prendre le premier qui répond).
        Borne à N = len(_stations) (1..10). Si N>=2 et enabled+mode hedge,
        l'appelant peut hedger sur len(cands) candidates.
        """
        if not self._stations:
            return []
        # if disabled and single station, still return 1 usable (fallback seq)
        # collect usable with exclude_approaching=False (hedge veut même au seuil)
        usable = [st for st in self._stations if self._station_usable(st, exclude_approaching=False, forced_pool=forced_pool)]
        if not usable:
            # [plan v10 §3.6.5 GARANTIE LRU] toutes hard-cooled/indisponibles →
            # second pass ignorant le cooldown latence : on sert quand même la
            # moins mauvaise (jamais de fallback paid par refroidissement seul).
            usable = [
                st
                for st in self._stations
                if self._station_usable(
                    st,
                    exclude_approaching=False,
                    forced_pool=forced_pool,
                    ignore_latency_cool=True,
                )
            ]
            if not usable:
                return []
            try:
                from latency_rotation import get_engine

                eng = getattr(self, "latency_engine", None) or get_engine()
                usable.sort(
                    key=lambda st: (
                        eng.cooldown_kind(int(st._station), str(getattr(st, "current_ip", "") or ""))
                        == "hard",
                        st._station,
                    )
                )
            except Exception:
                pass
            return [usable[0]]
        # if free_parallel disabled, return only best (no hedge)
        if not self._free_parallel_enabled:
            return [usable[0]] if usable else []
        if len(usable) == 1:
            return usable
        # sort according to routing
        if self._free_parallel_is_rr():
            # least-loaded first
            try:
                usable.sort(key=lambda st: self._per_station(st)["request_count"])
            except Exception:
                pass
        else:
            # failover: already in _station order, keep sticky
            usable.sort(key=lambda st: st._station)
        # cap to N (all) — caller may hedge on all, but limit burst to 3 for N=10
        # plan: staggers évite burst simultané; on retourne toutes, l'appelant borne
        return usable

    # Back-compat alias for plan's pick_two (2 stations max)
    def pick_two(self, forced_pool=None) -> list:
        cands = self.pick_candidates(forced_pool)
        return cands[:2]

    # [Axe 3.1] Static SOCKS5 proxies (socks5 mode — NO docker is ever
    # touched). The proxy list is an alternative backend to the docker
    # stations: negative sids (-1 - index) keep them outside ``_stations``
    # and ``_launch_rotation``'s ``_station_ids`` guard, so the whole docker
    # rotation/watchdog machinery is inert in socks5 mode.

    def _socks5_enabled_eps(self) -> list:
        return [ep for ep in self._socks5_eps if ep.enabled]

    def _socks5_usable(self, ep, *, exclude_approaching: bool) -> bool:
        """Like ``_station_usable`` but for a static SOCKS5 endpoint — same
        counters and bad-until checks keyed by its negative sid.

        [Revue 19/08] A DISABLED proxy is never usable: ``_station_usable``
        has no notion of ``enabled`` (it was built for VPNManager), so the
        stick branch of ``on_request`` would otherwise keep serving a proxy
        the operator just toggled off (or re-resolve one disabled by a
        config hot-reload)."""
        if not ep.enabled:
            return False
        return self._station_usable(ep, exclude_approaching=exclude_approaching)

    def _socks5_next(self, excluded=None) -> Socks5Endpoint | None:
        """Next static proxy in round-robin order.

        Two passes (like ``_best_station_excluding``): the preferred pass
        skips proxies at/over their rotation threshold, the second pass
        admits them so a request still gets a shot at the free tier instead
        of falling back to paid while every proxy is mid-quota. Advances
        ``_socks5_rr`` to the chosen index so the following request rotates
        on.
        """
        eps = self._socks5_enabled_eps()
        if not eps:
            return None
        excluded = excluded or set()
        n = len(eps)
        start = (self._socks5_rr + 1) % n
        for exclude_approaching in (True, False):
            for i in range(n):
                idx = (start + i) % n
                ep = eps[idx]
                if ep in excluded:
                    continue
                if self._socks5_usable(ep, exclude_approaching=exclude_approaching):
                    self._socks5_rr = idx
                    return ep
        return None

    def _socks5_best_excluding(self, ep) -> Socks5Endpoint | None:
        """Next usable proxy that is NOT ``ep`` (for a dual-clutch switch
        when the current one approaches quota)."""
        return self._socks5_next(excluded={ep})

    def _socks5_any_other(self, ep) -> bool:
        """True when at least one OTHER enabled proxy is usable (C1 guard
        applied to the static list — never bad-mark the last proxy)."""
        for other in self._socks5_enabled_eps():
            if other is ep:
                continue
            if self._station_usable(other, exclude_approaching=False):
                return True
        return False

    def set_socks5_proxies(self, proxies: list) -> None:
        """[Axe 3.1] Replace the static SOCKS5 list from config.yaml
        ``ip_rotation.socks5_proxies``.

        Validates host/port, carries per-proxy request/bad state across the
        rebuild by pid (so a hot-reload keeps counters instead of resetting
        quota walls), prunes the state of removed proxies, resets the
        round-robin cursor and re-resolves the current endpoint by pid.
        """
        new_rows = []
        for row in proxies:
            host = str(row.get("host") or "").strip()
            try:
                port = int(row.get("port"))
            except (TypeError, ValueError):
                port = 0
            if not host or not (1 <= port <= 65535):
                logger.warning("[free-ip] skipping invalid socks5 proxy %r", row)
                continue
            new_rows.append(
                {
                    "host": host,
                    "port": port,
                    "username": row.get("username"),
                    "password": row.get("password"),
                    "enabled": bool(row.get("enabled", True)),
                }
            )
        old_by_pid = {}
        for ep in self._socks5_eps:
            old_by_pid[ep.pid] = (ep, self._per.get(ep._station))
        keep_pids = {f"{r['host']}:{r['port']}" for r in new_rows}
        # Prune per-proxy state of proxies being removed.
        for pid, (ep, per) in old_by_pid.items():
            if pid not in keep_pids and per is not None:
                self._per.pop(ep._station, None)
        self._socks5_proxies = new_rows
        eps = []
        for i, row in enumerate(new_rows):
            pid = f"{row['host']}:{row['port']}"
            ep = Socks5Endpoint(
                row["host"],
                row["port"],
                username=row.get("username"),
                password=row.get("password"),
                enabled=row.get("enabled", True),
                index=i,
            )
            ep._quota_per_ip = getattr(self._vpn, "_quota_per_ip", 0)
            old = old_by_pid.get(pid)
            if old is not None and old[1] is not None:
                # Carry counters across the rebuild (same proxy, new sid).
                self._per[ep._station] = old[1]
            eps.append(ep)
        self._socks5_eps = eps
        if self._socks5_rr >= len(eps) or not eps:
            self._socks5_rr = 0
        if self._socks5_current is not None:
            cur_pid = self._socks5_current.pid
            # [Revue 19/08] Re-resolve only to an ENABLED endpoint: a config
            # hot-reload may have toggled the current proxy off — keeping it
            # current would make the stick branch serve a disabled proxy.
            # When the pid disappeared or is disabled, advance round-robin
            # to the next enabled/usable one instead (None if every proxy
            # is disabled — the request falls back to paid).
            self._socks5_current = (
                next((e for e in eps if e.pid == cur_pid and e.enabled), None)
                or self._socks5_next()
            )

    def rotate_socks5_now(self):
        """[Axe 3.1] Manual rotate — pick the next usable proxy and make it
        current (used by the dashboard rotate endpoint and the GUI throttle).
        Returns the new endpoint, or None when no alternative is usable."""
        if self._socks5_auto_rotate:
            # Already round-robining — a manual turn just advances the cursor.
            self._socks5_current = self._socks5_next()
            return self._socks5_current
        cur = self._socks5_current
        ep = self._socks5_next(excluded={cur} if cur is not None else None)
        if ep is None:
            return None
        self._socks5_current = ep
        return ep

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
        if self._vpn.proxy_mode == "socks5":
            # [Axe 3.1] socks5 mode: the static proxies have no docker
            # container to reconnect — nothing to ensure (and any docker
            # kick here would violate the "NO docker in socks5 mode"
            # invariant).
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
        if (
            per["last_connect_attempt"]
            and now - per["last_connect_attempt"] < self._connect_retry_interval
        ):
            return
        per["last_connect_attempt"] = now
        self._launch_rotation(station)

    async def on_request(self, forced_pool=None) -> tuple[str | None, VPNManager | None]:
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
        if mode == "direct":
            # [proxy_mode] all free traffic goes direct (residential IP) —
            # uniform with muse-spark: no tunnel attempt. Debug-level to
            # avoid per-request spam.
            logger.debug("[free-ip] proxy_mode=direct → free served direct (no tunnel pick)")
        if mode == "socks5":
            # [Axe 3.1] Static SOCKS5 list — round-robin pick, NO docker.
            # The two-pass _socks5_next already prefers a fresh proxy; when
            # EVERY proxy is at quota the second pass admits one so the
            # request still gets its free shot (the 429 bad-mark then skips
            # to another). No rotation launch, no dual-clutch wait — the
            # whole docker/watchdog machinery is inert here.
            if self._socks5_auto_rotate:
                ep = self._socks5_next()
            else:
                # [Axe 3.1] Auto-rotate OFF → stick to the current proxy
                # while it stays usable; only a bad-mark (429/quota) or the
                # manual rotate endpoint forces a switch.
                ep = self._socks5_current
                if ep is None or not self._socks5_usable(ep, exclude_approaching=False):
                    ep = self._socks5_next()
            if ep is None:
                return None, None
            self._active_station = ep
            per = self._per_station(ep)
            # Hot-reload guard (CRITIC(11)): quota_per_ip changed via config
            # → the counter refers to the OLD quota — reset lazily.
            if (
                per["last_quota_per_ip"] is not None
                and per["last_quota_per_ip"] != ep._quota_per_ip
            ):
                logger.info(
                    "[free-ip] socks5 quota_per_ip changed %s → %s — resetting request counter",
                    per["last_quota_per_ip"],
                    ep._quota_per_ip,
                )
                per["request_count"] = 0
            per["last_quota_per_ip"] = ep._quota_per_ip
            async with self._pool_pick_lock:
                per["request_count"] += 1
            ep.note_free_request()
            ip = ep.current_ip or ep.pid
            if ip not in per["ip_stats"]:
                per["ip_stats"][ip] = {
                    "requests": 0,
                    "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "server": "socks5",
                }
            per["ip_stats"][ip]["requests"] += 1
            self._socks5_current = ep
            return ep.socks5_url, ep

        if mode == "vpn":
            # [v6 P1-4] pick+increment race — lock <50µs, pas sur ensure_connected
            async with self._pool_pick_lock:
                station = self._best_station(forced_pool)
            if station is None and forced_pool is not None:
                for st in self._stations:
                    if st.status != "connected":
                        continue
                    try:
                        # [P3.2] bail court — 2s au lieu de 10s, au-delà on sert direct/paid et le pin finit en background si possible
                        _geo_timeout = float(_yaml_get("geo", "pin_inline_timeout_s", 2) or 2)
                        ok = await st.ensure_geo_egress(forced_pool, timeout=_geo_timeout)
                        if ok:
                            station = self._best_station(forced_pool)
                            if station is not None:
                                self._active_station = station
                                per = self._per_station(station)
                                per["last_quota_per_ip"] = station._quota_per_ip
                                # [plan v10 §14.3.10] la branche geo incrémentait
                                # request_count SANS jamais consulter le seuil :
                                # une station au seuil servait indéfiniment sous
                                # geo-pool. Même logique que le chemin normal.
                                per["request_count"] += 1
                                if per["request_count"] >= self._rotation_threshold(station):
                                    logger.info(
                                        "[free-ip] st%d geo-branch over quota threshold (%d/%d) — rotation kick",
                                        station._station,
                                        per["request_count"],
                                        station._quota_per_ip,
                                    )
                                    if self._rotation_threshold(station) <= 1:
                                        self._kick_connect(station)
                                    else:
                                        self._launch_rotation(station)
                                station.note_free_request()
                                ip = station.current_ip or "unknown"
                                if ip not in per["ip_stats"]:
                                    per["ip_stats"][ip] = {
                                        "requests": 0,
                                        "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                        "server": station.current_server.get("name", "?")
                                        if station.current_server
                                        else "?",
                                    }
                                per["ip_stats"][ip]["requests"] += 1
                                return station.socks5_url, station
                            break
                    except Exception:
                        continue
                # [fiabilisation 05/09] refus geo explicite : quelles stations
                # existent et pourquoi aucune ne passe (évite le « 1 routable »
                # incompréhensible côté client — voir usability_report).
                try:
                    _states = ",".join(
                        f"{getattr(s, '_station', '?')}:{getattr(s, 'status', '?')}"
                        for s in self._stations
                    )
                    logger.warning(
                        "[free-ip] geo-mismatch forced_pool=%s stations=[%s] — aucune station compatible",
                        sorted(forced_pool) if forced_pool else None, _states,
                    )
                except Exception:
                    pass
                return None, None
            if station is None:
                return None, None
            self._active_station = station
            per = self._per_station(station)
            # Hot-reload guard (CRITIC(11)): when quota_per_ip changed via
            # config, the counter refers to the OLD quota — reset lazily so
            # the new quota applies from the next request on.
            if (
                per["last_quota_per_ip"] is not None
                and per["last_quota_per_ip"] != station._quota_per_ip
            ):
                logger.info(
                    "[free-ip] quota_per_ip changed %s → %s — resetting request counter",
                    per["last_quota_per_ip"],
                    station._quota_per_ip,
                )
                per["request_count"] = 0
            per["last_quota_per_ip"] = station._quota_per_ip

            # Approaching quota (C3 threshold — station 2 crosses first):
            # switch to the other station NOW (rotation of the departed one
            # runs in the background — zero wait, zero paid fallback). When
            # no other station is usable, keep serving on this one and
            # rotate it in the background — the request path never blocks
            # on a docker rotation (C6).
            if per["request_count"] >= self._rotation_threshold(station):
                other = self._best_station_excluding(station, forced_pool)
                if other is not None:
                    logger.info(
                        "[free-ip] station %d approaching quota (%d/%d) — "
                        "dual-clutch switch to station %d, rotating in background",
                        station._station,
                        per["request_count"],
                        station._quota_per_ip,
                        other._station,
                    )
                    # [Revue 19/08] Degenerate threshold (= 1, quota headroom
                    # collapsed — the floor in _rotation_threshold): every
                    # request crosses it, so an UNTHROTTLED _launch_rotation
                    # would re-queue a rotation per request (churn, docker
                    # hot-loop). The throttled kick (last_connect_attempt /
                    # _connect_retry_interval) paces rotation attempts; the
                    # request still passes to `other` with zero wait.
                    if self._rotation_threshold(station) <= 1:
                        self._kick_connect(station)
                    else:
                        self._launch_rotation(station)
                    station = other
                    self._active_station = station
                    per = self._per_station(station)
                    per["last_quota_per_ip"] = station._quota_per_ip
                else:
                    # [17/08] No other station usable and THIS one is over
                    # threshold. Serving its current IP would be a burned IP —
                    # the free request would 429 → paid on every request while
                    # the rotation crawls. Kick the rotation and WAIT for it
                    # (bounded by rotation_wait_timeout): serve the fresh IP
                    # if it lands (switch_ip resets the counter), else return
                    # (None, None) → paid now, never a guaranteed 429.
                    logger.info(
                        "[free-ip] station %d at threshold (%d/%d), "
                        "waiting ≤%.0fs for rotation (no other station usable)",
                        station._station,
                        per["request_count"],
                        station._quota_per_ip,
                        self._rotation_wait_timeout,
                    )
                    self._kick_connect(station)
                    # [P3.1-B] hedge opt-in — après paid_hedge_after_ms sans IP fraîche, rendre paid early
                    # [PLAN-corrections-429 G2] strict_free : ZÉRO jambe paid —
                    # paid early refusé (le caller _try_free_model_first refuse
                    # en local via FreeQuotaExhausted).
                    if getattr(self, "_strict_free", False):
                        return None, None
                    if getattr(self, "_paid_hedge_after_ms", 0) and self._paid_hedge_after_ms > 0:
                        try:
                            ok = await asyncio.wait_for(
                                self._await_rotation(station), timeout=self._paid_hedge_after_ms / 1000.0
                            )
                        except TimeoutError:
                            return None, None
                        if not ok:
                            return None, None
                    else:
                        if not await self._await_rotation(station):
                            return None, None
                    # Keep the station that just landed the fresh IP —
                    # switch_ip reset its counter, so this request counts
                    # as the first on the new (model, IP) cooldown key.
                    self._active_station = station
                    per = self._per_station(station)
                    per["last_quota_per_ip"] = station._quota_per_ip

            # Only count requests that actually went through the tunnel —
            # when the VPN is down, requests go direct on a residential IP
            # and must not advance the rotation counter ([5]).
            # [v6 P1-4] increment sous lock (évite sur-quota 429)
            async with self._pool_pick_lock:
                per["request_count"] += 1
            # Track activity for opportune update timing
            station.note_free_request()
            # Track stats for the current station IP
            ip = station.current_ip or "unknown"
            if ip not in per["ip_stats"]:
                per["ip_stats"][ip] = {
                    "requests": 0,
                    "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "server": station.current_server.get("name", "?")
                    if station.current_server
                    else "?",
                }
            per["ip_stats"][ip]["requests"] += 1

            return station.socks5_url, station

        return None, None

    async def switch_ip(self, station: VPNManager | None = None) -> str:
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
        if isinstance(station, Socks5Endpoint):
            # [Axe 3.1] Static proxies have no docker rotation — nothing to
            # switch. A stray call is a programming error, fail loudly.
            raise RotationFailed(f"proxy socks5 {station.pid} n'a pas de rotation docker")
        new_ip = await station.connect_next()
        if not new_ip:
            # Defensive: connect_next is typed to return str now, but a
            # None would silently reset counters below — fail loudly instead.
            raise RotationFailed("connect_next n'a retourné aucune IP")
        per = self._per_station(station)
        per["request_count"] = 0
        # [PLAN-corrections-429 G4] IP fraîche = nouveau bucket → reset
        # du compteur cascade (les 429 précédents portaient sur l'ancien).
        per["consec_429"] = 0
        per["consec_429_ips"] = []
        per["session_start"] = time.monotonic()
        # [review F1b] the rotation anchor: notify_connection_failure diffs
        # station.current_ip against this to detect a MANAGER repair (re-pin
        # with no pool rotation involved) and absorb its late-signal tail.
        per["last_confirmed_ip"] = new_ip
        # [plan v10 §3.6.5 Lot 3] warm-up post-rotation : reset consecutive_slow
        # du superviseur + note rotation au moteur (anti-flap/global_degraded).
        try:
            eng = getattr(self, "latency_engine", None)
            if eng is not None:
                eng._note_rotation(int(station._station))
            import shared_state as _ss

            for sup in getattr(_ss, "station_supervisors", None) or []:
                if sup.station == station._station:
                    sup.on_ip_finalized(new_ip)
                    break
        except Exception:
            pass
        return new_ip

    def on_quota_exhausted(
        self, station: VPNManager | None = None, forced_pool: set | None = None
    ):
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

        Axe B: ``forced_pool`` is propagated to the background rotation so
        the rotated station stays within geo-allowed countries.

        No-op when VPN is disabled or in direct mode.
        """
        from config.settings import normalize_429_action as _norm_429
        action = _norm_429(getattr(self, "_on_429_action", None))
        if not self._vpn.enabled:
            return
        if self._vpn.proxy_mode == "socks5":
            # [Axe 3.1] Static list, NO docker: bad-mark the current proxy
            # (C1-guarded) only for cooldown/both; rotate alone is no-op.
            ep = station if isinstance(station, Socks5Endpoint) else self._socks5_current
            if ep is None:
                return
            if action in ("cooldown", "both"):
                if self._socks5_any_other(ep):
                    self._per_station(ep)["bad_until"] = time.monotonic() + self._bad_ttl
            return
        if self._vpn.proxy_mode != "vpn":
            return
        station = station or self._active_station or self._vpn
        if action in ("cooldown", "both"):
            if self.dual_station:
                if self._any_other_usable(station):
                    self._per_station(station)["bad_until"] = time.monotonic() + self._bad_ttl
        if action in ("rotate", "both"):
            self._launch_rotation(station, forced_pool=forced_pool)
        # [PLAN-corrections-429 G4] failover cascade : en routing=failover,
        # la sticky station reçoit TOUT le trafic — ≥3 429 consécutifs sur
        # des IPs DIFFÉRENTES = épuisement au niveau COMPTE (pas per-IP).
        # → callback compte-épuisé (opencode.py l'enregistre via
        # set_failover_exhausted_cb : cooldown "sentinelle" couvrant toutes
        # les stations pour ce modèle). Pas d'import opencode ici (cycle) :
        # le callback est poussé, jamais tiré. Succès/rotation fraîche =
        # reset (voir note_hedge_winner + switch_ip) ; même IP répétée =
        # compteur inchangé (même bucket).
        try:
            if (
                not self._free_parallel_is_rr()
                and self._free_parallel_enabled
                and getattr(station, "_station", None) is not None
            ):
                per = self._per_station(station)
                ip = str(getattr(station, "current_ip", "") or "") or "unknown"
                ips = per.get("consec_429_ips") or []
                if ip not in ips:
                    ips.append(ip)
                    per["consec_429_ips"] = ips[-4:]
                    per["consec_429"] = int(per.get("consec_429") or 0) + 1
                if int(per.get("consec_429") or 0) >= 3:
                    per["consec_429"] = 0
                    per["consec_429_ips"] = []
                    cb = getattr(self, "_failover_exhausted_cb", None)
                    if callable(cb):
                        try:
                            cb(station)
                        except Exception:
                            pass
        except Exception:
            pass

    def set_failover_exhausted_cb(self, cb) -> None:
        """[PLAN-corrections-429 G4] Enregistre le callback "épuisement
        compte" appelé par on_quota_exhausted en cascade failover (≥3 429
        sur IPs distinctes). Poussé par opencode.py au boot (pas d'import
        inverse — cycle opencode↔pool interdit). ``None`` = désarme."""
        self._failover_exhausted_cb = cb if callable(cb) or cb is None else None

    async def on_disconnect_retry(
        self, failed: VPNManager | None = None, forced_pool=None
    ) -> tuple[str | None, VPNManager | None]:
        """Pick a DIFFERENT station for a retry after an upstream disconnect.

        The stream retry loop used to re-strike the SAME station/proxy that
        just died — under the per-IP quota model a dead IP is a guaranteed
        failure. It surfaced as a ✘ dashboard row ("Server disconnected
        without sending a response", 17/08 21:44). A retry is NOT a new
        request: no counter advance (the failed attempt already consumed its
        per-IP slot) — this is where it differs from `on_request`.

        Mirrors `on_quota_exhausted`:
          * no-op when disabled or not in vpn mode → (None, None)
          * the failed station is bad-marked (C1 guard: only when ANOTHER
            station is usable, never the last standing one) and rotated in
            the background — a fresh IP for it, ready for the next request
          * the retry lands on ``_best_station_excluding(failed)`` — a
            different station = a different, likely-fresh (model, IP) key
          * ``failed=None`` (no station known, e.g. a direct fallback) →
            best station across the pool

        Returns ``(proxy_url, station)``; ``(None, None)`` when no other
        station is usable right now — the caller falls back to direct/paid
        instead of burning another attempt on a dead IP.
        """
        if not self._vpn.enabled:
            return None, None
        if self._vpn.proxy_mode == "socks5":
            # [Axe 3.1] Static list, NO docker: bad-mark the failed proxy
            # (C1-guarded by _socks5_any_other) and pick the next one. No
            # _launch_rotation, no dual_station check.
            target = failed if isinstance(failed, Socks5Endpoint) else self._socks5_current
            if target is not None and self._socks5_any_other(target):
                self._per_station(target)["bad_until"] = time.monotonic() + self._bad_ttl
            st = self._socks5_best_excluding(target) if target is not None else self._socks5_next()
            if st is None:
                return None, None
            self._active_station = st
            self._socks5_current = st
            return st.socks5_url, st
        if self._vpn.proxy_mode != "vpn":
            return None, None
        await self.ensure_connected()
        if failed is not None:
            if self.dual_station:
                if self._any_other_usable(failed):
                    self._per_station(failed)["bad_until"] = time.monotonic() + self._bad_ttl
            self._launch_rotation(failed, forced_pool=forced_pool)
        st = (
            self._best_station_excluding(failed, forced_pool)
            if failed is not None
            else self._best_station(forced_pool)
        )
        if st is None:
            return None, None
        self._active_station = st
        return st.socks5_url, st

    def notify_connection_failure(self, station) -> None:
        """[plan 18/08 §1b] A real connection failure on ``station``'s tunnel
        (SOCKS5 dead — seen in the request path, invisible to the pool).

        [étage 0] bad-mark immediately → _station_usable refuses it → the
        next request switches to the other station instantly instead of
        re-striking a known-dead tunnel. C1 guard first: never bad-mark the
        last standing station (mono-station keeps serving; the request path
        adopts the 10 s connect timeout — am.21).

        [étage 1] the manager arms its egress watchdog and wakes it (~0-2 s)
        → the live tick probes and repairs. NO rotation kick here (am.7):
        a pool kick + the tick's fast-recover would race — the lock orders
        but does not cancel, and a queued rotation would pin a second time
        after the repair. The wake IS the repair.

        Late-signal guard (am.18): a request launched BEFORE a successful
        rotation may fail after it landed — its failure must not bad-mark a
        freshly rotated (healthy) station.
        """
        if self._vpn.proxy_mode == "socks5":
            # [Axe 3.1] Static list, NO docker: bad-mark the failing proxy
            # (C1-guarded by _socks5_any_other) so the next request skips
            # it. ``station`` may be None (a direct fallback) — fall back to
            # the current endpoint; do NOT call _per_station(station) first
            # (it would crash on None). No egress watchdog (no docker).
            target = station if isinstance(station, Socks5Endpoint) else self._socks5_current
            if target is None:
                return
            if self._socks5_any_other(target):
                self._per_station(target)["bad_until"] = time.monotonic() + self._bad_ttl
            return
        per = self._per_station(station)
        cur_ip = getattr(station, "current_ip", None)
        # [review F1b] the repair anchor. The manager's repair path re-pins a
        # FRESH IP without any pool rotation (session_start untouched) — a
        # late signal from a pre-repair request must not bad-mark the repaired
        # healthy tunnel. Two branches:
        #   (a) no baseline yet: record the current IP WITHOUT touching
        #       session_start — the FIRST genuine failure still bad-marks
        #       (étage 0 alive from day one, never silently absorbed by a
        #       baseline-establishing refresh);
        #   (b) baseline exists and the IP changed → a repair landed: refresh
        #       the anchor AND session_start so its late-signal tail is
        #       absorbed by the grace guard below.
        if per["last_confirmed_ip"] is None:
            if cur_ip is not None:
                per["last_confirmed_ip"] = cur_ip
        elif cur_ip != per["last_confirmed_ip"]:
            per["last_confirmed_ip"] = cur_ip
            per["session_start"] = time.monotonic()
        # [review F1a] the late-signal absorption window is the SHORT
        # `_late_signal_grace` (20 s), NOT the full bad_ttl (60 s): a signal
        # past the dial queue (connect timeout ~10-15 s + SOCKS5/TLS
        # handshake) + teardown recv is genuine — the dead freshly-rotated
        # tunnel is bad-marked NOW (étage 0) instead of being re-struck for a
        # full bad_ttl.
        if self._any_other_usable(station) and (
            per["session_start"] is None
            or time.monotonic() - per["session_start"] >= self._late_signal_grace
        ):
            per["bad_until"] = time.monotonic() + self._bad_ttl
        station.arm_egress_watchdog()

    # ── In-flight free stream registry (plan 18/08 §am.22) ──────

    def register_stream(self, station, task) -> None:
        """Record an in-flight free stream served over ``station``'s tunnel.

        [plan 18/08 §am.22] when the tick CONFIRMS egress death (``egress_dead``
        after the threshold), these streams are canceled — a client currently
        reading a dead tunnel would otherwise sit on the keepalive-bridged
        silence for up to the 600 s read timeout. Registration happens once
        the tunnel POST succeeded (the real fire-and-forget signal already
        covered the connect stage)."""
        if station is None or isinstance(station, Socks5Endpoint):
            return  # [Axe 3.1] socks5 mode registers nothing (no docker tunnel)
        self._per_station(station).setdefault("stream_tasks", set()).add(task)

    def unregister_stream(self, station, task) -> None:
        """Drop ``task`` from the registry once its stream is done."""
        if station is None or isinstance(station, Socks5Endpoint):
            return  # [Axe 3.1] socks5 mode never registered anything
        per = self._per_station(station)
        tasks = per.get("stream_tasks")
        if tasks:
            tasks.discard(task)

    def cancel_streams(self, station) -> None:
        """Cancel every in-flight free stream over ``station``'s tunnel.

        Called by the manager's tick at ``egress_dead`` — the tunnel is CONFIRMED
        dead by the light probe, so the streams on it cannot recover: giving the
        clients the error NOW (instead of after the keepalive/read timeout) is
        the whole point. The network-error classifier in opencode.py re-raises
        genuine client disconnects (uvicorn cancels the request task the same
        way); those task IDs were never registered here, so only tunnels that
        are truly mid-stream get a cancel.

        [piège 19] the retry loop must treat the watchdog cancel as a NETWORK
        failure — redirect into the failover (bad-mark → ``on_disconnect_retry``
        picks another station), never re-strike the dead one.

        C1 guard on the bad-mark (as in `notify_connection_failure`): never
        bad-mark the last standing station — but the CANCEL itself is
        unconditional (a confirmed-dead tunnel cannot carry a stream).
        """
        if station is None:
            return  # [Axe 3.1] socks5 mode has no docker tunnel to cancel
        per = self._per_station(station)
        tasks = per.get("stream_tasks")
        if not tasks:
            return
        # Bounded overwrite, not append: the cancelled set is per burst — a
        # stream of a PREVIOUS burst has either propagated already (re-raised
        # or retried to another station) or re-registered, so carrying stale
        # IDs forward would only risk misclassifying a genuine client cancel.
        # [plan v10 §14.3.9] WeakSet d'OBJETS et plus {id(t)} : un id réutilisé
        # par une nouvelle tâche post-GC était classé à tort "watchdog-cancelled".
        _cancelled_ws = weakref.WeakSet()
        _cancelled_ws.update(tasks)
        per["watchdog_cancelled"] = _cancelled_ws
        for t in list(tasks):
            t.cancel()

    def is_watchdog_cancelled(self, station, task) -> bool:
        """True when ``task`` was cancelled by ``cancel_streams`` (not by a
        genuine client disconnect). The marker lives OUTSIDE the live registry
        (``stream_tasks``): unregister runs during CancelledError propagation,
        BEFORE the handler's ``except asyncio.CancelledError`` inspects the
        task, so the decision must not depend on the task still being listed."""
        return any(
            task in (per.get("watchdog_cancelled") or set())
            for per in self._per.values()
            if per.get("watchdog_cancelled")
        )

    async def _await_rotation(self, station: VPNManager) -> bool:
        """Wait (bounded by ``rotation_wait_timeout``) for a background
        rotation to finish and land a NEW IP on ``station``.

        Network truth over self-report: the rotation succeeded only when
        ``station.current_ip`` CHANGED (connect_next's final step, resetting
        ``request_count`` to 0). A rotation that failed — or never started
        (still queued behind the other station's rotation) — ends this
        routine uncleanly. Returns False on timeout/failure; the caller then
        returns ``(None, None)`` → paid, never the burned IP.

        Concurrent callers share the same in-flight rotation task
        (``_rotation_tasks`` single-flight), so they all wake on its
        completion.
        """
        burned_ip = station.current_ip
        deadline = time.monotonic() + self._rotation_wait_timeout
        while time.monotonic() < deadline:
            task = self._rotation_tasks.get(station._station)
            if task is not None and not task.done():
                remaining = deadline - time.monotonic()
                try:
                    if remaining > 0:
                        await asyncio.wait_for(asyncio.shield(task), remaining)
                except TimeoutError:
                    return False
                except Exception as exc:
                    # [plan v10 §14.1.7] RotationFailed (breaker ouvert,
                    # fail-fast 300s, socks5 interdit…) sortait du handler en
                    # 500 au lieu du fallback paid promis. Fail-soft : False.
                    import logging as _lg

                    _lg.getLogger(__name__).debug(
                        "[pool] rotation task failed for st%s: %s", station._station, exc
                    )
                    return False
                # [Revue 19/08] The post-commit probe [Axe 1.2] can stamp a
                # bad_until on the FRESH IP BEFORE this waiter wakes (the
                # probe runs inside _rotate_station, right before the task
                # completes) — a committed-but-dead tunnel must not be
                # served as the result of a successful rotation. Re-check
                # usability (exclude_approaching=False: only bad_until +
                # status) so the request falls back to paid and the station
                # re-rotates instead of getting a guaranteed 429.
                return (
                    station.current_ip is not None
                    and station.current_ip != burned_ip
                    and station.status == "connected"
                    and self._station_usable(station, exclude_approaching=False)
                )
            # Not in flight yet — either queued behind another station's
            # rotation (bounded workers [Axe 1.1]) or about to be launched.
            await asyncio.sleep(0.05)
        return False

    def _ensure_workers(self) -> None:
        """[Axe 1.1] Prune finished workers and top the pool back up to
        ``_ROTATION_CONCURRENCY``. Workers are persistent (while-True queue
        drains), so this is near-no-op in steady state — it only matters
        after a worker died from a bug (guard in ``_rotation_worker``)."""
        self._worker_tasks = [t for t in self._worker_tasks if not t.done()]
        for _ in range(self._ROTATION_CONCURRENCY - len(self._worker_tasks)):
            self._worker_tasks.append(asyncio.create_task(self._rotation_worker()))

    def _launch_rotation(self, station: VPNManager, forced_pool: set | None = None) -> None:
        """Queue a background rotation for one station (C4/C5).

        Bounded concurrency [Axe 1.1]: up to ``_ROTATION_CONCURRENCY``
        workers drain the queue in parallel, so a rotation blocked on one
        station (budget wait + docker ops) no longer freezes the fleet.
        Dedup both ways: a station with a rotation already queued
        (``_pending``) or already in flight (``_rotation_tasks``) is never
        queued twice — concurrent 429s on the same station share one
        rotation.

        Axe B: ``forced_pool`` is stored on the station as
        ``_geo_forced_pool`` so the background rotation stays within
        geo-allowed countries."""
        sid = station._station
        if sid not in self._station_ids:
            # [plan 18/08 §2.3] A request handler can hold a manager the
            # downscale just retired (429 arrived mid-swap). The pool must
            # IGNORE it — no queue entry, no docker work on a container
            # that stop_container is deleting.
            return
        if sid in self._pending:
            return
        task = self._rotation_tasks.get(sid)
        if task and not task.done():
            return  # a rotation for this station is already running
        # Axe B: tag the station with geo constraint for the background
        # rotation so _pin_country_for_rotation filters correctly
        if forced_pool is not None:
            station._geo_forced_pool = forced_pool
        self._pending.add(sid)
        self._ensure_workers()
        self._rotation_queue.put_nowait(station)

    async def _rotation_worker(self) -> None:
        """Drain the rotation queue — up to ``_ROTATION_CONCURRENCY`` of
        these run in parallel [Axe 1.1], so N stations rotate concurrently
        (bounded) instead of serially. Per-station single-flight is kept
        by ``_rotation_tasks`` registration before the first await."""
        while True:
            station = await self._rotation_queue.get()
            self._pending.discard(station._station)
            if station not in self._stations:
                # [plan 18/08 §4] station downscaled while queued — no-op;
                # its per-station state was pruned by set_stations. The
                # single-flight guarantee stays intact: the entry was
                # already dequeued, nothing else touches this station.
                continue
            try:
                await self._rotate_station(station)
            except asyncio.CancelledError:
                # [plan 18/08 §2.3] A downscale cancelled this rotation
                # (cancel_rotations). Do NOT die: the remaining queue must
                # still drain (_ensure_workers would only re-pay the price
                # on the next launch). _rotate_station's finally already
                # popped its _rotation_tasks registration.
                # 3.12: the cancellation was delivered and consumed — clear
                # the pending-cancel marker so the worker leaves the
                # "cancelling" state (else `cancelled()`/`cancelling()`
                # stay sticky and shutdown-time cancels stack up).
                t = asyncio.current_task()
                if t is not None and hasattr(t, "uncancel"):
                    t.uncancel()
                continue
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
        # station's rotation queues behind it.
        self._rotation_tasks[sid] = asyncio.current_task()
        needs_retry = False  # [Axe 1.2] re-queue after finally-pop (dedup)
        try:
            await self.switch_ip(station=station)
            per = self._per_station(station)
            # Rotation succeeded: the station has a fresh IP (fresh
            # (model, IP) cooldown key) — make it eligible again now,
            # without waiting out the bad TTL.
            per["bad_until"] = None
            if station.proxy_mode == "vpn":
                # [Axe 1.2] Fresh probe AFTER the commit (direct call = not
                # the watchdog's tick, no status cache): a committed-but-dead
                # tunnel must re-rotate NOW instead of waiting for the
                # watchdog to notice on a later tick.
                try:
                    alive = await station._probe_tunnel_light()
                except Exception as e:
                    # A probe bug must not turn a good rotation into a
                    # failed one — log, assume dead (watchdog will confirm).
                    logger.warning(
                        "[free-ip] station %d post-commit probe error: %s", station._station, e
                    )
                    alive = False
                if alive:
                    per["post_commit_retry_count"] = 0
                else:
                    per["post_commit_retry_count"] += 1
                    logger.warning(
                        "[free-ip] station %d fresh IP not egressing "
                        "(post-commit probe dead, attempt %d/%d) — "
                        "re-rotating immediately",
                        station._station,
                        per["post_commit_retry_count"],
                        self._POST_COMMIT_RETRY_MAX,
                    )
                    # The watchdog must hear about the dead fresh tunnel on
                    # EVERY probe failure, cap or not — at the cap it owns
                    # the recovery escalation (no hot-loop of the stack).
                    station.arm_egress_watchdog()
                    if per["post_commit_retry_count"] < self._POST_COMMIT_RETRY_MAX:
                        # C1: never bad-mark the ONLY usable station.
                        if self._any_other_usable(station):
                            per["bad_until"] = time.monotonic() + self._bad_ttl
                        needs_retry = True
                    # At the cap: give up on immediate re-rotation — the
                    # watchdog owns recovery (escalation, not hot-loop).
        except Exception as e:
            # CRITIC(5): a failed background rotation must be logged, not
            # swallowed — the next free request will fall back to paid and
            # retry the rotation later (or not, if the fail-fast cooldown
            # is active in VPNManager).
            logger.warning(
                "[free-ip] station %d background rotation failed: %s", station._station, e
            )
        finally:
            self._rotation_tasks.pop(sid, None)
            # [plan v10 §14.1.8] le tag géo était posé par _launch_rotation et
            # JAMAIS nettoyé : après UN 429 géo, toutes les rotations futures
            # de la station restaient verrouillées sur ces pays jusqu'au
            # restart process. Le pin a consommé le tag — on le lève ici.
            try:
                if getattr(station, "_geo_forced_pool", None) is not None:
                    station._geo_forced_pool = None
                    logger.info("[free-ip] st%d _geo_forced_pool cleared after rotation", sid)
            except Exception:
                pass
        if needs_retry:
            # [Axe 1.2] Re-queue AFTER the finally-pop: _launch_rotation
            # dedups on _rotation_tasks, which is still registered until
            # here — queuing inside the try would be swallowed.
            self._launch_rotation(station)

        # ── Station set (hot-reload) ───────────────────────────────

    def set_stations(self, stations: list) -> None:
        """[plan 18/08 §4] Atomically swap the active station set (GUI
        dropdown 1-10, hot reload — no proxy restart, no worker stop).

        Effect: ``self._stations`` is replaced (sorted by station number,
        so station 1 stays the preferred pass); per-station state of
        removed stations is pruned (request counters + IP stats) so
        ``get_status()`` / ``ips_used`` stay honest; their queued or
        in-flight rotation entries are dropped from ``_pending`` /
        ``_rotation_tasks``.

        The worker is deliberately NOT cancelled: the single C4/C5 drain
        loop survives, stale queue entries become no-ops (guard inside
        ``_rotation_worker``), and a rotation already in flight finishes
        harmlessly on the retired manager — the single-flight guarantee
        is preserved without killing work mid-run.
        """
        stations = [s for s in stations if s is not None]
        removed = {s._station for s in self._stations} - {s._station for s in stations}
        self._stations = sorted(stations, key=lambda s: s._station)
        self._station_ids = {s._station for s in self._stations}
        for sid in removed:
            self._per.pop(sid, None)
            self._pending.discard(sid)
            self._rotation_tasks.pop(sid, None)
        # [prancy-unicorn Phase1 P1] prune ghost sids >N in shared_rotation
        try:
            import shared_state as _ss_prune

            _sr = getattr(_ss_prune, "shared_rotation", None)
            if _sr is not None and hasattr(_sr, "prune_stations"):
                # [plan v10 §14.3.12] passer le MAX des sids ACTIFS, pas le
                # count : un ensemble non contigu {1,3} avec count=2 ferait
                # prune_stations(2) supprimer l'état de la station 3 active.
                if self._station_ids:
                    _sr.prune_stations(max(self._station_ids))
        except Exception:
            pass
        # [plan 18/08 §2.3] Sweep any stale registrations whose sid is not in
        # the new set (belt-and-braces: an entry can outlive the removed-set
        # computation if a station object was swapped mid-reload) — unknown
        # stations are ignored, never acted on.
        for sid in list(self._pending):
            if sid not in self._station_ids:
                self._pending.discard(sid)
        for sid in list(self._rotation_tasks):
            if sid not in self._station_ids:
                self._rotation_tasks.pop(sid, None)

    async def cancel_rotations(self, sids) -> None:
        """[plan 18/08 §2.3] Cancel + await (5 s cap) the in-flight
        rotations of stations a downscale is about to remove.

        A rotation must NOT finish on a container that stop_container is
        about to delete (compose stop + docker rm -f) — it would resurrect
        the retired station or leave it half-rotated. Called BEFORE
        set_stations in the downscale branch of _apply_station_count.

        Task identity: _rotate_station registers the WORKER task
        (current_task()) as the rotation task, so cancelling it cancels the
        worker's current _rotate_station call — vpn_manager's rotation-op
        funnel (op count + generation) was designed for this, and
        _rotate_station's finally pops its own registration. The worker
        itself SURVIVES (it catches CancelledError and keeps draining the
        queue), so it never completes — awaiting it would always hit the
        cap. The real "rotation unwound" signal is the registration
        popping, hence the poll below instead of wait_for on the task.

        A rotation STUCK inside asyncio.to_thread cannot be killed (threads
        are not cancellable): the poll caps at 5 s and lets the downscale
        proceed anyway (best-effort — the op funnel guards the subsequent
        docker sequencing). Unknown station ids are ignored.

        [Revue 19/08] After cancel + await, the ids are RETIRED here —
        dropped from ``_station_ids`` / ``_stations`` / ``_pending`` /
        ``_rotation_tasks`` (+ per-station state pruned), so the pool's
        guards close IMMEDIATELY. Without this, the sids stayed live until
        the caller's later ``set_stations`` — and a 429 arriving DURING the
        stop_container teardown window (opencode ``_apply_station_count``:
        ``stop()`` + ``stop_container()`` awaiting docker, seconds) re-queued
        a rotation through ``_launch_rotation``'s sid guard, landing docker
        work on a container that was being deleted. ``set_stations`` stays
        idempotent on already-retired sids (its ``removed`` set computes
        from ``self._stations``, which is already shrunk)."""
        if not sids:
            return
        wanted = set(sids)
        tasks = [
            t
            for sid, t in self._rotation_tasks.items()
            if sid in wanted and t is not None and not t.done()
        ]
        if not tasks:
            # No in-flight rotation: still retire eagerly (a degenerate
            # threshold or a late 429 could re-queue otherwise).
            self._retire_stations(wanted)
            return
        for t in tasks:
            t.cancel()
        deadline = time.monotonic() + 5.0
        while self._rotation_tasks.keys() & wanted and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self._retire_stations(wanted)

    def _retire_stations(self, sids) -> None:
        """[Revue 19/08] Eager pool-side retirement of stations being
        downscaled — called by ``cancel_rotations`` so the teardown window
        (stop() + stop_container() awaiting docker) sees the sid guards
        closed. Mirrors set_stations' prune: state dropped, queued/in-flight
        rotation entries removed, no docker work accepted afterwards."""
        for sid in sids:
            self._station_ids.discard(sid)
            self._stations[:] = [s for s in self._stations if s._station != sid]
            self._pending.discard(sid)
            self._rotation_tasks.pop(sid, None)
            self._per.pop(sid, None)

    def update_config(self, cfg: dict) -> None:
        """Apply timing overrides from config.yaml ``ip_rotation`` (hot-reload,
        same body the dashboard sends to the VPN managers)."""
        if not isinstance(cfg, dict):
            return
        if "connect_retry_interval" in cfg:
            self._connect_retry_interval = max(5.0, float(cfg["connect_retry_interval"] or 0))
        if "on_429_action" in cfg:
            from config.settings import normalize_429_action as _norm_429
            self._on_429_action = _norm_429(cfg["on_429_action"])
        if "bad_ttl" in cfg:
            try:
                _bt = int(float(cfg["bad_ttl"] if cfg["bad_ttl"] is not None else 60))
            except (TypeError, ValueError):
                _bt = None  # invalid → ignore (keep current)
            if _bt is not None:
                self._bad_ttl = float(max(1, min(3600, _bt)))
        # [plan 30/08] clés dédiées (secondes, sans collision avec bad_ttl
        # minutes du fast-pin vpn_manager). Présentes → prioritaires ;
        # absentes/invalides → inchangé (défauts = comportement actuel).
        _v = _clamp_seconds(cfg, "station_connect_retry_interval_s", 5.0, 3600.0)
        if _v is not None:
            self._connect_retry_interval = _v
        _v = _clamp_seconds(cfg, "station_bad_ttl_s", 1.0, 3600.0)
        if _v is not None:
            self._bad_ttl = _v
        if "rotation_stagger" in cfg:
            self._rotation_stagger = max(0, int(cfg["rotation_stagger"] or 0))
        if "rotation_wait_timeout" in cfg:
            self._rotation_wait_timeout = max(1.0, float(cfg["rotation_wait_timeout"] or 0))
        if "paid_hedge_after_ms" in cfg:
            # [P3.1-B] opt-in hedge — 0 = désactivé, sinon ms
            self._paid_hedge_after_ms = max(0.0, float(cfg["paid_hedge_after_ms"] or 0))
        # [PLAN-corrections-429 G2] strict_free hot-reloadable (config.yaml
        # au boot + dashboard). Voir on_request : en strict, la jambe paid
        # early est refusée (None, None) → le caller refuse en local.
        if "strict_free" in cfg:
            self._strict_free = bool(cfg["strict_free"])
        # [Axe 1.1] Bounded rotation concurrency (config.yaml
        # `ip_rotation.rotation_concurrency`, hot-reloadable).
        if "rotation_concurrency" in cfg:
            self._ROTATION_CONCURRENCY = max(1, int(cfg["rotation_concurrency"] or 0))
        # [Axe 3.1] Static SOCKS5 list + quota refresh (config.yaml
        # `ip_rotation.socks5_proxies` / `quota_per_ip`, hot-reloadable).
        if "socks5_proxies" in cfg and isinstance(cfg["socks5_proxies"], list):
            self.set_socks5_proxies(cfg["socks5_proxies"])
        # [plan v10 §3.6.2 Lot 3] bloc canonique latency_rotation → moteur
        # (hot-reload, même corps que le dashboard envoie aux managers).
        if "latency_rotation" in cfg:
            eng = getattr(self, "latency_engine", None)
            if eng is not None:
                try:
                    eng.update_config(cfg["latency_rotation"] or {})
                except Exception:
                    pass
        # [Axe 3.1] Auto-rotate OFF = stick to the current proxy while
        # usable (rotation only via bad-mark or the rotate endpoint).
        if "socks5_auto_rotate" in cfg:
            self._socks5_auto_rotate = bool(cfg["socks5_auto_rotate"])
        if "quota_per_ip" in cfg:
            q = getattr(self._vpn, "_quota_per_ip", getattr(self._vpn, "quota_per_ip", 0))
            for ep in self._socks5_eps:
                ep._quota_per_ip = q
                # hot-reload guard: stale counters refer to the old quota
                per = self._per.get(ep._station)
                if per is not None and per["last_quota_per_ip"] != q:
                    per["request_count"] = 0
                    per["last_quota_per_ip"] = q
        # [free_parallel] hot-reload (nested dict + flat keys for dashboard compat)
        if "free_parallel" in cfg and isinstance(cfg["free_parallel"], dict):
            fp = cfg["free_parallel"]
            try:
                self._free_parallel_enabled = bool(fp.get("enabled", False))
            except Exception:
                self._free_parallel_enabled = False
            routing = str(fp.get("routing", "round-robin") or "round-robin").lower()
            self._free_parallel_routing = routing if routing in ("round-robin", "failover") else "round-robin"
            mode = str(fp.get("mode", "load-balance") or "load-balance").lower()
            self._free_parallel_mode = mode if mode in ("load-balance", "strict", "hedge") else "load-balance"
            try:
                self._free_parallel_hedge_delay_ms = max(0, min(2000, int(fp.get("hedge_delay_ms", 300))))
            except Exception:
                self._free_parallel_hedge_delay_ms = 300
            # [v10 Lot 5] hedge_delay per-model (§12.1.2 v6) — optionnel
            pmap = fp.get("hedge_delay_ms_per_model")
            self._free_parallel_hedge_delay_ms_per_model = (
                {str(k): float(v) for k, v in pmap.items()} if isinstance(pmap, dict) and pmap else None
            )
            try:
                self._free_parallel_hedge_max = max(1, min(3, int(fp.get("hedge_max_attempts", 1))))
            except Exception:
                self._free_parallel_hedge_max = 1
        # flat keys (dashboard sends free_parallel_routing plat)
        if "free_parallel_enabled" in cfg:
            try:
                self._free_parallel_enabled = bool(cfg["free_parallel_enabled"])
            except Exception:
                pass
        if "free_parallel_routing" in cfg:
            r = str(cfg["free_parallel_routing"] or "round-robin").lower()
            if r in ("round-robin", "failover"):
                self._free_parallel_routing = r
        if "free_parallel_mode" in cfg:
            m = str(cfg["free_parallel_mode"] or "load-balance").lower()
            if m in ("load-balance", "strict", "hedge"):
                self._free_parallel_mode = m
        if "free_parallel_hedge_delay_ms" in cfg:
            try:
                self._free_parallel_hedge_delay_ms = max(0, min(2000, int(cfg["free_parallel_hedge_delay_ms"])))
            except Exception:
                pass

    def note_hedge_winner(self, winner, primary=None) -> None:
        """Record hedge winner — compta winner-only (loser not counted).

        Increments per["request_count"], ip_stats, note_free_request(),
        and hedge_wins counter. Called AFTER _hedged_fetch picks winner.
        """
        if winner is None:
            return
        try:
            per = self._per_station(winner)
            # [PLAN-corrections-429 G4] un 200 = la station sert → reset du
            # compteur cascade (l'épuisement compte supposé est infirmé).
            per["consec_429"] = 0
            per["consec_429_ips"] = []
            # hot-reload guard like on_request
            if per["last_quota_per_ip"] is not None and per["last_quota_per_ip"] != getattr(winner, "_quota_per_ip", 0):
                per["request_count"] = 0
            per["last_quota_per_ip"] = getattr(winner, "_quota_per_ip", 0)
            per["request_count"] += 1
            try:
                winner.note_free_request()
            except Exception:
                pass
            ip = getattr(winner, "current_ip", None) or getattr(winner, "pid", "unknown") or "unknown"
            if ip not in per["ip_stats"]:
                per["ip_stats"][ip] = {
                    "requests": 0,
                    "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "server": getattr(winner, "current_server", {}).get("name", "?") if getattr(winner, "current_server", None) else "?",
                }
                # socks5 server
                if getattr(winner, "pid", None) and not getattr(winner, "current_server", None):
                    per["ip_stats"][ip]["server"] = "socks5"
            per["ip_stats"][ip]["requests"] += 1
            self._active_station = winner
            if getattr(winner, "pid", None) and isinstance(winner, Socks5Endpoint):
                self._socks5_current = winner
            # hedge observability
            try:
                self._hedge_wins["total"] += 1
                if primary is not None and winner is primary:
                    self._hedge_wins["primary"] += 1
                elif primary is not None:
                    self._hedge_wins["hedge"] += 1
                else:
                    self._hedge_wins["primary"] += 1
            except Exception:
                pass
        except Exception as e:
            logger.debug("[free-ip] note_hedge_winner failed: %s", e)

    def any_rotation_in_flight(self) -> bool:
        """[Axe 1.4] True while at least one station rotation is running.

        Called by ``_free_stations_exhausted`` (opencode.py) to decide
        whether the pool is genuinely exhausted: a rotation in flight is
        about to land a fresh (model, IP) key, so the request should WAIT
        for it instead of falling back to paid. No lock needed — asyncio
        runs this single-threaded and ``_rotation_tasks`` is mutated only
        synchronously (registered before the rotation's first await, popped
        in the same task's finally)."""
        return any(t is not None and not t.done() for t in self._rotation_tasks.values())

    def get_status(self) -> dict:
        """Return pool status for the dashboard (aggregated over stations).

        Legacy top-level fields (vpn_status, current_ip, ...) report
        station 1 for backward compatibility; the dashboard's dual-station
        view reads ``stations`` + ``active_station``.
        """
        stations = []
        for st in self._stations:
            per = self._per_station(st)
            stations.append(
                {
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
                    "bad_remaining": max(0, per["bad_until"] - time.monotonic())
                    if per["bad_until"]
                    else 0,
                    # [fiabilisation 05/09 Lot 0] raison lisible quand la
                    # station n'est pas servable (None = routable).
                    "non_routable_reason": self._non_routable_reason(st),
                    "last_rotation_error": getattr(st, "_last_rotation_error", None),
                    "vpn": st.get_status(),
                }
            )
        active = self._active_station or self._vpn
        # [plan v10 §14.3.8] stations[0] sans garde → IndexError (500 sur
        # /api/pool-status) dès que la registry est vide (retrait total).
        if not stations:
            s1 = {
                "station": None,
                "vpn_status": "error",
                "current_ip": None,
                "current_server": None,
                "requests_this_ip": 0,
                "quota_per_ip": 0,
                "rotation_threshold": 0,
                "remaining": 0,
                "bad_until": None,
                "bad_remaining": 0,
                "last_rotation_error": "no station configured",
                "vpn": {},
            }
        else:
            s1 = stations[0]
        # [prancy-unicorn Phase1] agrégat honnête N/M healthy — connected si une station l'est, error seulement si toutes error
        _healthy = sum(1 for _s in stations if _s.get("vpn_status") == "connected")
        _total = len(stations)
        # [v6 P1-0b] healthy_routable — connected - cooldown 429 (60s) - hard latency 1800s
        try:
            _eng = getattr(self, "latency_engine", None)
            _healthy_routable = 0
            for _s in stations:
                if _s.get("vpn_status") != "connected":
                    continue
                if _s.get("bad_remaining", 0) > 0:
                    continue
                _ip = _s.get("current_ip")
                if _ip and _eng and _eng.ip_hard_cooled(int(_s["station"]), str(_ip)):
                    continue
                _healthy_routable += 1
        except Exception:
            _healthy_routable = _healthy
        if _healthy > 0:
            _agg_status = "connected"
        elif all(_s.get("vpn_status") == "error" for _s in stations):
            _agg_status = "error"
        else:
            _agg_status = s1["vpn_status"]

        # [Axe 3.1] Static SOCKS5 view (socks5 mode — no docker stations).
        socks5_proxies = []
        for ep in self._socks5_eps:
            per = self._per.get(ep._station, {})
            bad_until = per.get("bad_until")
            socks5_proxies.append(
                {
                    "pid": ep.pid,
                    "host": ep.host,
                    "port": ep.port,
                    "username": ep.username,
                    "has_password": ep.has_password,
                    "enabled": ep.enabled,
                    "current": self._socks5_current is ep,
                    "requests_this_ip": per.get("request_count", 0),
                    "quota_per_ip": ep._quota_per_ip,
                    "bad_until": bad_until,
                    "bad_remaining": (max(0, bad_until - time.monotonic()) if bad_until else 0),
                }
            )

        return {
            "enabled": self._vpn.enabled,
            "proxy_mode": self._vpn.proxy_mode,
            "dual_station": self.dual_station,
            "active_station": active._station,
            "stations": stations,
            "healthy": _healthy,
            "healthy_routable": _healthy_routable,
            # [fiabilisation 05/09 Lot 0] alias top-level pour le dashboard N/M.
            "routable": _healthy_routable,
            "total": _total,
            "socks5_mode": self.socks5_mode,
            "socks5_current": (
                self._socks5_current.pid if self._socks5_current is not None else None
            ),
            "socks5_proxies": socks5_proxies,
            # Aggregated banner (N/M healthy) — legacy s1 fields kept for compat but vpn_status/status is aggregated
            "status": _agg_status,
            "vpn_status": _agg_status,
            "current_ip": s1["current_ip"],
            "current_server": s1["current_server"],
            "requests_this_ip": s1["requests_this_ip"],
            "quota_per_ip": s1["quota_per_ip"],
            "remaining": s1["remaining"],
            "total_free_requests": self._total_free_requests,
            # Timings/hot-reload state (dashboard panel + tests)
            "connect_retry_interval": self._connect_retry_interval,
            "bad_ttl": self._bad_ttl,
            "on_429_action": str(getattr(self, "_on_429_action", "both") or "both"),
            "rotation_stagger": self._rotation_stagger,
            "rotation_concurrency": self._ROTATION_CONCURRENCY,
            "rotate_pending": sorted(self._pending),
            "ips_used": len({ip for p in self._per.values() for ip in p["ip_stats"]}),
            "ip_stats": {ip: st for p in self._per.values() for ip, st in p["ip_stats"].items()},
            "vpn": self._vpn.get_status(),
            # [free_parallel] observability
            "free_parallel": {
                "enabled": self._free_parallel_enabled,
                "routing": self._free_parallel_routing,
                "mode": self._free_parallel_mode,
                "hedge_delay_ms": self._free_parallel_hedge_delay_ms,
                "hedge_max_attempts": self._free_parallel_hedge_max,
            },
            "hedge_wins": dict(self._hedge_wins),
        }
