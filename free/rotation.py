"""free.rotation — registre partagé + moteur latence-adaptive (Phase 6).

[Phase 6 refonte — chantier 2] Domicile CANONIQUE (fusion byte-identique
de ``shared_rotation.py`` + ``latency_rotation.py`` — seuls ajustements :
en-tête, `from __future__` remonté, imports dédupliqués, loggers nommés
explicitement (noms historiques préservés), `ROOT` ré-ancré racine repo.
``shared_rotation.py`` et ``latency_rotation.py`` restent des shims de
re-export jusqu'à la Phase 9 ; le nouveau code importe ``free.rotation``.

Historicité — docstring shared_rotation.py :
>'''
>Shared rotation state — cross-station IP registry + global identity cursor.

>The two VPN stations each keep their own per-station IP history and
>identity index; nothing was shared, so a station could re-enter an IP
>recently used by the OTHER station, and both stations could serve the
>same client fingerprint at the same time. This module is the single
>shared registry:

>- recent IPs of BOTH stations, persisted to logs/shared_rotation.json
>- one absolute, monotone identity cursor that BOTH stations advance
>  from, guaranteeing their live identities never collide as long as the
>  profile pool has >= 2 entries

>The whole module is synchronous — callers mutate then persist with no
>``await`` in between, so inside the single asyncio loop (the proxy holds
>a global one-process instance lock) each record/advance operation is
>atomic. Persists are tmp+rename (same pattern as VPNManager.save_state).

>Backward compatibility: all reads are fail-open (missing/corrupt file ->
>empty state, local-only behavior downstream). When identity diversity is
>off or the pool has a single profile, ``next_identity`` degenerates
>gracefully — the existing len<=1 gate in VPNManager.current_identity
>already pins everyone to profile[0].
>'''

Historicité — docstring latency_rotation.py :
>'''[plan v10 §3.6 Lot 3] Moteur de rotation latence-adaptive.

>Décide soft_rotate/hard_rotate par (station, ip) d'après IpLatencyTracker,
>applique cooldowns soft(600s)/hard(1800s), anti-flapping (6/h/station),
>garde-fou global_degraded (≥50% stations tournées → pause 300s), toggle
>maintenance ``rotation_paused``, et GARANTIE LRU : jamais zéro candidat —
>au pire le moins mauvais est servi.

>Config canonique : ``ip_rotation.latency_rotation`` dans config.yaml
>(bloc §3.6.2 v5/v6), hot-reloadable via ``update_config()``.
>Typé pour ``mypy --strict`` (charte §3.7).
>'''
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ip_latency import IpLatencyTracker

logger = logging.getLogger("shared_rotation")
_lat_logger = logging.getLogger("latency_rotation")

# [Phase 6] ancré RACINE repo (ce fichier vit dans free/ — un niveau
# plus bas que l'original) : sans ce parent supplémentaire, le défaut
# logs/shared_rotation.json ne serait plus trouvé (état remis à zéro).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_utc(value: str) -> float | None:
    """Parse a UTC 'YYYY-mm-ddTHH:MM:SSZ' timestamp into epoch seconds."""
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


class SharedRotationState:
    """Cross-station shared state: recent-IP registry + identity cursor."""

    # Hard cap on _ip_events size (file-size guard under heavy rotation).
    _WINDOW_CAP = 100

    def __init__(self, cfg: dict):
        """Build the shared registry from the ip_rotation config section.

        Reads ``recent_ip_window`` (default 20, count), ``recent_ip_max_age``
        (default 1800 s) and ``shared_rotation_file`` (default
        ``logs/shared_rotation.json``). Fail-open: a missing/corrupt file
        simply starts with an empty registry.
        """
        self._cfg = cfg
        self._recent_ip_window = max(2, int(cfg.get("recent_ip_window", 20)))
        self._recent_ip_max_age = max(60.0, float(cfg.get("recent_ip_max_age", 1800)))
        file_cfg = cfg.get("shared_rotation_file")
        self._file = (
            file_cfg
            if isinstance(file_cfg, str) and file_cfg.strip()
            else os.path.join(ROOT, "logs", "shared_rotation.json")
        )
        if not os.path.isabs(self._file):
            self._file = os.path.join(ROOT, self._file)

        self._ip_events: list[dict] = []  # [{ip, station, time}]
        self._cursor: int = 0  # ABSOLUTE monotone identity counter
        self._last_index_by_station: dict[int, int] = {}
        self._country_cursor: int = 0  # ABSOLUTE monotone country counter
        self._last_country_by_station: dict[int, int] = {}
        self._saved_at: str | None = None
        self._load()

    # ── Registry access ─────────────────────────────────────────

    def recent_ips(self) -> list[str]:
        """All IPs recently used by either station (count or age window)."""
        return [e["ip"] for e in self._ip_events]

    def is_recent(self, ip: str) -> bool:
        """True when ``ip`` was used by either station inside the count
        window or the age window (the more conservative of the two — the
        trimmed ``_ip_events`` holds exactly those events)."""
        if not ip:
            return False
        return any(e["ip"] == ip for e in self._ip_events)

    def record_ip(self, ip: str, station: int) -> None:
        """Register ``ip`` as recently used by ``station``.

        Any prior event for the same IP is dropped (an IP is re-armed at
        its newest use), then the event is appended and persisted.
        """
        if not ip:
            return
        self._ip_events = [e for e in self._ip_events if e["ip"] != ip]
        self._ip_events.append(
            {
                "ip": ip,
                "station": int(station),
                "time": _now_utc(),
            }
        )
        self._trim()
        self._saved_at = _now_utc()
        self._persist()

    # ── Identity cursor ─────────────────────────────────────────

    def register_station(self, station: int, index: int) -> None:
        """Record a station's live identity index at boot (after its own
        state load) so ``next_identity`` immediately avoids it. Does NOT
        bump the absolute cursor — only next_identity advances it."""
        self._last_index_by_station[int(station)] = max(0, int(index))
        self._saved_at = _now_utc()
        self._persist()

    def next_identity(self, station: int, n: int) -> int:
        """Advance the shared identity cursor and return an index into a
        profile pool of size ``n`` that DIFFERS from the live index of every
        other station.

        Guaranteed for n >= 2: pass 1 skips the live slots of every OTHER
        station (cross-station uniqueness, the identity pair of a dual
        station never collides) AND this station's own last index (a fresh
        face on every IP change). When every slot is blocked — only possible
        when the pool is too small to express uniqueness, e.g. dual n==2
        ping-pong — pass 2 drops the uniqueness constraint but still
        guarantees a CHANGE. With n <= 1 the len<=1 gate in
        ``VPNManager.current_identity`` already pins everyone to profile[0],
        so the index is irrelevant — recorded as 0 for consistency.

        The cursor is absolute (restored on restart), so the identity
        sequence continues where it left off instead of resetting to
        chrome131 on every proxy boot.
        """
        station = int(station)
        if n <= 1:
            self._last_index_by_station[station] = 0
            self._saved_at = _now_utc()
            self._persist()
            return 0
        others = {idx for s, idx in self._last_index_by_station.items() if s != station}
        own = self._last_index_by_station.get(station)
        self._cursor += 1
        idx = self._cursor % n
        # Pass 1: an index free of every other station's live slot AND
        # different from this station's OWN last index — a new identity.
        # (The old code skipped `own`: with n==2 both stations froze on one
        # index each — the identity never changed when the IP changed.)
        for _ in range(n):
            if idx not in others and idx != own:
                break
            idx = (idx + 1) % n
        # Pass 2 (only reachable when every slot is taken, e.g. dual n==2):
        # dropping the own-index constraint still guarantees a CHANGE vs a
        # stale face; cross-station uniqueness is sacrificed only in pools
        # too small to express it (production pools are ~190 profiles).
        if idx == own or idx in others:
            for _ in range(n):
                if idx != own:
                    break
                idx = (idx + 1) % n
        self._last_index_by_station[station] = idx
        self._saved_at = _now_utc()
        self._persist()
        return idx

    # ── Country cursor ──────────────────────────────────────────

    def next_country(self, station: int, offset: int, n: int) -> int:
        """Advance the shared country cursor and return an index into a
        country list of size ``n`` that DIFFERS from the live country index
        of every other station (the two stations never serve the same
        country simultaneously) and from this station's OWN last country (a
        fresh country on every rotation).

        ``offset`` (default ``len(list)//2``) spreads the two stations'
        sequences apart on top of the pass-1 skip, so the country separation
        is structural; the same guarantees as ``next_identity`` apply
        (pass 1 uniqueness, pass 2 guaranteed change, n<=1 degenerate -> 0).
        """
        station = int(station)
        offset = max(1, int(offset))
        if n <= 1:
            self._last_country_by_station[station] = 0
            self._saved_at = _now_utc()
            self._persist()
            return 0
        others = {idx for s, idx in self._last_country_by_station.items() if s != station}
        own = self._last_country_by_station.get(station)
        # [plan v10 v6 §3.4 Lot 3] offset effectif par station :
        # offset + stride×(station-1) — sinon 2 stations tirent les mêmes
        # pays en boucle quand offset est petit. stride=0 → legacy exact.
        eff = offset + self._country_offset_stride() * (station - 1)
        self._country_cursor += 1
        idx = (self._country_cursor + eff) % n
        # Pass 1: a country free of every other station's live slot AND
        # different from this station's OWN last country — a new country.
        for _ in range(n):
            if idx not in others and idx != own:
                break
            idx = (idx + 1) % n
        # Pass 2 (only reachable when every slot is taken, e.g. n==2
        # ping-pong): dropping the own-country constraint still guarantees
        # a CHANGE; cross-station uniqueness is sacrificed only in lists too
        # small to express it (the production list has ~29 entries).
        if idx == own or idx in others:
            for _ in range(n):
                if idx != own:
                    break
                idx = (idx + 1) % n
        self._last_country_by_station[station] = idx
        self._saved_at = _now_utc()
        self._persist()
        return idx

    def _country_offset_stride(self) -> int:
        """[v6 §3.4] `ip_rotation.country_offset_stride` — écart structurel
        supplémentaire entre stations (0 = legacy offset×(station-1))."""
        try:
            return max(0, int((self._cfg or {}).get("country_offset_stride", 0) or 0))
        except Exception:
            return 0

    def peek_next_country(self, station: int, offset: int, n: int) -> int:
        """Preview the next country index for ``station`` WITHOUT advancing
        or persisting anything (dashboard 'next country' cell). Mirrors
        ``next_country``'s skip logic against the CURRENT live slots, so the
        preview is best-effort — another station's rotation may move the
        cursor before the real call."""
        station = int(station)
        offset = max(1, int(offset))
        if n <= 1:
            return 0
        others = {idx for s, idx in self._last_country_by_station.items() if s != station}
        own = self._last_country_by_station.get(station)
        eff = offset + self._country_offset_stride() * (station - 1)
        idx = ((self._country_cursor + 1) + eff) % n
        for _ in range(n):
            if idx not in others and idx != own:
                break
            idx = (idx + 1) % n
        if idx == own or idx in others:
            for _ in range(n):
                if idx != own:
                    break
                idx = (idx + 1) % n
        return idx

    # ── Config hot-reload / status / config ─────────────────────

    def prune_stations(self, max_station: int) -> None:
        """Drop ghost sids >N after a downscale (P1 melodic-pearl).

        Removes ip_events + last_index/country entries whose station > N so a
        later upscale does not resurrect stale IPs/identities. Called by
        FreeIPPool.set_stations() and opencode._apply_station_count().
        """
        try:
            max_station = int(max_station)
        except (TypeError, ValueError):
            return
        if max_station < 1:
            max_station = 1
        changed = False
        before = len(self._ip_events)
        self._ip_events = [e for e in self._ip_events if int(e.get("station", 0)) <= max_station]
        if len(self._ip_events) != before:
            changed = True
        for key in list(self._last_index_by_station.keys()):
            if int(key) > max_station:
                self._last_index_by_station.pop(key, None)
                changed = True
        for key in list(self._last_country_by_station.keys()):
            if int(key) > max_station:
                self._last_country_by_station.pop(key, None)
                changed = True
        if changed:
            self._saved_at = _now_utc()
            self._persist()
            logger.info("[shared-rotation] pruned ghost stations >%d (%d events removed)", max_station, before - len(self._ip_events))

    def set_window(self, cfg: dict) -> None:
        """Re-read recent_ip_window/recent_ip_max_age on config change
        and re-trim the registry to the new windows."""
        if not isinstance(cfg, dict):
            cfg = {}
        self._cfg = cfg
        try:
            self._recent_ip_window = max(2, int(cfg.get("recent_ip_window", 20)))
        except (TypeError, ValueError):
            self._recent_ip_window = 20
        try:
            self._recent_ip_max_age = max(60.0, float(cfg.get("recent_ip_max_age", 1800)))
        except (TypeError, ValueError):
            self._recent_ip_max_age = 1800.0
        # Re-trim against the new windows only if the registry could shrink
        # (a growing window never needs a trim).
        if len(self._ip_events) > self._recent_ip_window:
            self._trim()
            self._saved_at = _now_utc()
            self._persist()

    def get_status(self) -> dict:
        """Dashboard-facing snapshot of the shared state."""
        return {
            "cursor": self._cursor,
            "country_cursor": self._country_cursor,
            "recent_ip_window": self._recent_ip_window,
            "recent_ip_max_age": self._recent_ip_max_age,
            "file": self._file,
            "recent_ips": self.recent_ips(),
            "ip_events": list(self._ip_events),
            "last_index_by_station": dict(self._last_index_by_station),
            "last_country_by_station": dict(self._last_country_by_station),
            "saved_at": self._saved_at,
        }

    # ── Internal ────────────────────────────────────────────────

    def _trim(self) -> None:
        """Keep the newest recent_ip_window events plus any older event still
        younger than recent_ip_max_age (windows OR-ed — the conservative
        reading), hard-capped at _WINDOW_CAP.

        No early return: the cap is enforced on EVERY trim, so the true live
        bound is _WINDOW_CAP — the old ``len <= window`` early return let a
        window>cap configuration grow back past the cap between trims and
        skipped age-pruning entirely for under-window pools.
        """
        if not self._ip_events:
            return
        cutoff = time.time() - self._recent_ip_max_age
        window_new = self._ip_events[-self._recent_ip_window :]
        window_old = [e for e in self._ip_events[: -self._recent_ip_window] if _fresh(e, cutoff)]
        self._ip_events = window_old + window_new
        if len(self._ip_events) > self._WINDOW_CAP:
            self._ip_events = self._ip_events[-self._WINDOW_CAP :]

    def _load(self) -> None:
        """Load persisted state from disk (fail-open)."""
        try:
            if not os.path.exists(self._file):
                return
            with open(self._file) as f:
                state = json.load(f)
            events = state.get("ip_events")
            if isinstance(events, list):
                self._ip_events = [
                    {
                        "ip": str(e.get("ip")),
                        "station": int(e.get("station", 0)),
                        "time": str(e.get("time", "")),
                    }
                    for e in events
                    if isinstance(e, dict) and e.get("ip") and e.get("time")
                ]
            try:
                self._cursor = max(0, int(state.get("cursor", 0)))
            except (TypeError, ValueError):
                self._cursor = 0
            last = state.get("last_index_by_station") or {}
            if isinstance(last, dict):
                self._last_index_by_station = {}
                for k, v in last.items():
                    try:
                        self._last_index_by_station[int(k)] = max(0, int(v))
                    except (TypeError, ValueError):
                        continue
            try:
                self._country_cursor = max(0, int(state.get("country_cursor", 0)))
            except (TypeError, ValueError):
                self._country_cursor = 0
            last_c = state.get("last_country_by_station") or {}
            if isinstance(last_c, dict):
                self._last_country_by_station = {}
                for k, v in last_c.items():
                    try:
                        self._last_country_by_station[int(k)] = max(0, int(v))
                    except (TypeError, ValueError):
                        continue
            self._saved_at = state.get("saved_at")
            self._trim()
            logger.debug(
                "[shared-rotation] state loaded from %s (%d IPs, cursor %d)",
                self._file,
                len(self._ip_events),
                self._cursor,
            )
        except Exception as e:
            logger.debug("[shared-rotation] failed to load state: %s", e)
            self._ip_events = []
            self._cursor = 0
            self._last_index_by_station = {}
            self._country_cursor = 0
            self._last_country_by_station = {}
            self._saved_at = None

    def _persist(self) -> None:
        """Atomic write (temp file + os.replace), fail-open."""
        try:
            os.makedirs(os.path.dirname(self._file), exist_ok=True)
            state = {
                "ip_events": self._ip_events,
                "cursor": self._cursor,
                "last_index_by_station": self._last_index_by_station,
                "country_cursor": self._country_cursor,
                "last_country_by_station": self._last_country_by_station,
                "saved_at": self._saved_at,
            }
            tmp = self._file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, self._file)
        except Exception as e:
            logger.debug("[shared-rotation] failed to persist state: %s", e)


def _fresh(event: dict, cutoff: float) -> bool:
    """True when an event's timestamp is at or after ``cutoff`` (epoch s)."""
    ts = _parse_utc(str(event.get("time", "")))
    return ts is not None and ts >= cutoff


# ── Moteur latence-adaptive (ex-latency_rotation.py, inchangé hors logger) ──

COOLDOWN_SOFT: str = "soft"
COOLDOWN_HARD: str = "hard"


@dataclass
class EngineConfig:
    enabled: bool = True
    slow_threshold_ms: float = 8000.0
    ewma_threshold_ms: float = 6000.0
    p95_threshold_ms: float = 9000.0
    consecutive_slow: int = 3
    min_requests_before_eval: int = 5
    soft_cooldown_sec: float = 600.0
    hard_cooldown_sec: float = 1800.0
    ewma_alpha: float = 0.3
    window: int = 20
    per_model: dict[str, float] = field(
        default_factory=lambda: {"default": 8000.0}
    )
    floor_ms: float = 3000.0
    global_degraded_threshold: float = 0.5
    global_degraded_cooldown_sec: float = 300.0
    max_soft_rotates_per_hour: int = 6
    stream_metric: str = "ttfb"  # ttfb | total (v5 §3.6.1)
    prewarm_after_rotate: bool = True
    # [v10 §12.1.1] rotation prédictive : pente EWMA (%) sur 5 requêtes ;
    # 0 = désactivé.
    predictive_trend_pct: float = 30.0

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> EngineConfig:
        c = cls()
        if not isinstance(cfg, dict):
            return c
        g = cfg.get
        c.enabled = bool(g("enabled", True))
        c.slow_threshold_ms = float(g("slow_threshold_ms", c.slow_threshold_ms))
        c.ewma_threshold_ms = float(g("ewma_threshold_ms", c.ewma_threshold_ms))
        c.p95_threshold_ms = float(g("p95_threshold_ms", c.p95_threshold_ms))
        c.consecutive_slow = int(g("consecutive_slow", c.consecutive_slow))
        c.min_requests_before_eval = int(
            g("min_requests_before_eval", c.min_requests_before_eval)
        )
        c.soft_cooldown_sec = float(g("soft_cooldown_sec", c.soft_cooldown_sec))
        c.hard_cooldown_sec = float(g("hard_cooldown_sec", c.hard_cooldown_sec))
        c.ewma_alpha = float(g("ewma_alpha", c.ewma_alpha))
        c.window = int(g("window", c.window))
        pm_raw = g("slow_threshold_ms_per_model", None)
        if isinstance(pm_raw, dict) and pm_raw:
            c.per_model = {str(k): float(v) for k, v in pm_raw.items()}
        c.floor_ms = float(g("floor_ms", c.floor_ms))
        c.global_degraded_threshold = float(
            g("global_degraded_threshold", c.global_degraded_threshold)
        )
        c.global_degraded_cooldown_sec = float(
            g("global_degraded_cooldown_sec", c.global_degraded_cooldown_sec)
        )
        c.max_soft_rotates_per_hour = int(
            g("max_soft_rotates_per_hour", c.max_soft_rotates_per_hour)
        )
        sm = str(g("stream_metric", c.stream_metric)).lower()
        c.stream_metric = sm if sm in ("ttfb", "total") else "ttfb"
        c.prewarm_after_rotate = bool(g("prewarm_after_rotate", True))
        try:
            c.predictive_trend_pct = float(g("predictive_trend_pct", 30.0))
        except (TypeError, ValueError):
            c.predictive_trend_pct = 30.0
        return c

    def threshold_for(self, model: str) -> tuple[float, float]:
        """(seuil lent, seuil p95) pour un modèle — per-model sinon default."""
        base = self.per_model.get(model, self.per_model.get("default", self.slow_threshold_ms))
        return base, self.p95_threshold_ms


class LatencyRotationEngine:
    """État global du système de rotation latence-adaptive (singleton)."""

    def __init__(self) -> None:
        self.cfg = EngineConfig()
        self._trackers: dict[tuple[int, str], IpLatencyTracker] = {}
        # (sid, ip) -> (kind, until_monotonic)
        self._cooldowns: dict[tuple[int, str], tuple[str, float]] = {}
        # couples ayant DÉJÀ subi un soft — re-slow après expiration = hard
        self._soft_history: set[tuple[int, str]] = set()
        # anti-flap : timestamps (monotonic) des rotations déclenchées par sid
        self._soft_log: dict[int, deque[float]] = {}
        # fenêtre global_degraded : (mono, sid) des dernières rotations
        self._global_log: deque[tuple[float, int]] = deque(maxlen=64)
        self._global_paused_until: float = 0.0
        self.paused: bool = False
        # compteurs Prometheus §12.2.7
        self.total_soft: int = 0
        self.total_hard: int = 0
        self._now: Any = time.monotonic  # point d'injection tests

    # ── config ────────────────────────────────────────────────────────

    def update_config(self, cfg: dict[str, Any]) -> None:
        old_enabled = self.cfg.enabled
        old = (
            self.cfg.window,
            self.cfg.ewma_alpha,
            self.cfg.min_requests_before_eval,
            self.cfg.consecutive_slow,
        )
        self.cfg = EngineConfig.from_cfg(cfg)
        # [GUI toggle « Cooldown latence »] transition True→False : purge
        # immédiate de TOUS les cooldowns actifs (les stations exclues
        # redeviennent routables tout de suite) ET du _soft_history (sinon
        # une IP revenue après réactivation serait re-marquée hard d'office —
        # escalade fantôme). Les trackers sont conservés (sparkline/EWMA
        # restent affichés, sans effet). Ne purge PAS au boot ni en False→True.
        if old_enabled and not self.cfg.enabled:
            n = len(self._cooldowns)
            self._cooldowns.clear()
            self._soft_history.clear()
            _lat_logger.info("[latency] engine désactivé — %d cooldowns purgés", n)
        new = (
            self.cfg.window,
            self.cfg.ewma_alpha,
            self.cfg.min_requests_before_eval,
            self.cfg.consecutive_slow,
        )
        if old != new:
            for tr in self._trackers.values():
                tr.window = self.cfg.window
                tr.alpha = self.cfg.ewma_alpha
                tr.min_requests_before_eval = self.cfg.min_requests_before_eval
                tr.consecutive_slow_limit = self.cfg.consecutive_slow

    def on_rotation_done(self, sid: int, new_ip: str) -> None:
        """Rotation réussie : warm-up v6 — reset consecutive_slow de tous les
        trackers de la station (la nouvelle IP démarre vierge, et les anciennes
        aussi pour éviter un faux signal au retour par fallback LRU).
        Invalide aussi les entrées _soft_history de la station : sinon une IP
        revenue après rotation re-slow est re-marquée d'office en hard
        (escalade soft→hard systématique) et le pool tombe à 1/N routable."""
        for (s, _ip), tr in self._trackers.items():
            if s == int(sid):
                tr.reset_consecutive_slow()
        # purge anti hard-cascade : on ne peut escalader en hard que sur une
        # IP observée re-lente APRÈS l'expiration de SON soft, pas sur une IP
        # repassée par une rotation entre-temps.
        self._soft_history = {k for k in self._soft_history if int(k[0]) != int(sid)}
        self.tracker_for(int(sid), str(new_ip))

    def threshold_for(self, model: str) -> tuple[float, float]:
        return self.cfg.threshold_for(model)

    # ── trackers ──────────────────────────────────────────────────────

    def tracker_for(self, sid: int, ip: str) -> IpLatencyTracker:
        key = (int(sid), str(ip))
        tr = self._trackers.get(key)
        if tr is None:
            tr = IpLatencyTracker(
                station=int(sid),
                ip=str(ip),
                window=self.cfg.window,
                alpha=self.cfg.ewma_alpha,
                min_requests_before_eval=self.cfg.min_requests_before_eval,
                consecutive_slow_limit=self.cfg.consecutive_slow,
            )
            self._trackers[key] = tr
            while len(self._trackers) > 300:  # borne mémoire globale
                self._trackers.pop(next(iter(self._trackers)))
        return tr

    # ── cooldowns ─────────────────────────────────────────────────────

    def mark(self, sid: int, ip: str, kind: str) -> None:
        dur = self.cfg.soft_cooldown_sec if kind == COOLDOWN_SOFT else self.cfg.hard_cooldown_sec
        self._cooldowns[(int(sid), str(ip))] = (kind, self._now() + dur)
        # [v10 §12.2.7] compteur Prometheus par type
        if kind == COOLDOWN_HARD:
            self.total_hard += 1
        else:
            self.total_soft += 1
        _lat_logger.info("[latency] %s cooldown st%s ip=%s %.0fs", kind, sid, ip, dur)

    def cooldown_kind(self, sid: int, ip: str) -> str | None:
        entry = self._cooldowns.get((int(sid), str(ip)))
        if entry is None:
            return None
        kind, until = entry
        if self._now() >= until:
            del self._cooldowns[(int(sid), str(ip))]
            return None
        return kind

    def ip_hard_cooled(self, sid: int, ip: str) -> bool:
        return self.cooldown_kind(sid, ip) == COOLDOWN_HARD

    def lru_pick(self, candidates: list[tuple[int, str]]) -> tuple[int, str] | None:
        """GARANTIE LRU §3.6.5 : jamais zéro candidat — un non-refroidi gagne,
        sinon le plus proche d'expiration (least-recently-cooled)."""
        if not candidates:
            return None
        for cand in candidates:
            if self.cooldown_kind(int(cand[0]), str(cand[1])) is None:
                return cand
        best: tuple[int, str] | None = None
        best_until = float("inf")
        for cand in candidates:
            entry = self._cooldowns.get((int(cand[0]), str(cand[1])))
            until = entry[1] if entry else float("-inf")
            if until < best_until:
                best, best_until = cand, until
        return best

    # ── garde-fous ────────────────────────────────────────────────────

    def _note_rotation(self, sid: int) -> None:
        now = self._now()
        dq = self._soft_log.setdefault(int(sid), deque())
        dq.append(now)
        hour_ago = now - 3600.0
        while dq and dq[0] < hour_ago:
            dq.popleft()
        self._global_log.append((now, int(sid)))

    def can_soft_rotate(self, sid: int, total_stations: int) -> tuple[bool, str]:
        """Anti-flap + paused + global_degraded (§3.6.4/§3.6.5)."""
        if not self.cfg.enabled:
            return False, "disabled"
        if self.paused:
            return False, "rotation_paused"
        now = self._now()
        hour_ago = now - 3600.0
        dq = self._soft_log.get(int(sid))
        if dq is not None:
            # [fix v10] purge ICI : sinon len(dq) compte des notes sorties de
            # la fenêtre et le cap ne se lève jamais.
            while dq and dq[0] < hour_ago:
                dq.popleft()
        if dq and len(dq) >= self.cfg.max_soft_rotates_per_hour:
            return False, "anti_flapping"
        if now < self._global_paused_until:
            return False, "global_degraded_pause"
        if total_stations > 0 and self._global_log:
            recent_sids = {sid_t for ts, sid_t in self._global_log if ts >= now - 600.0}
            if len(recent_sids) / total_stations >= self.cfg.global_degraded_threshold:
                self._global_paused_until = self._now() + self.cfg.global_degraded_cooldown_sec
                _lat_logger.warning(
                    "[latency] global_degraded: %d/%d stations tournées <10min → pause %.0fs",
                    len(recent_sids),
                    total_stations,
                    self.cfg.global_degraded_cooldown_sec,
                )
                return False, "global_degraded"
        return True, ""

    # ── point d'entrée requête ────────────────────────────────────────

    def record_request(
        self,
        sid: int,
        ip: str,
        duration_ms: float,
        model: str,
        status_code: int | None = None,
    ) -> dict[str, Any]:
        """Mesure §3.6.1 : succès ET échec. Retourne la décision prise.

        Anti-re-mark : une fois un cooldown posé sur (sid, ip), les requêtes
        suivantes NE ré-escaladent pas tant que la rotation n'a pas changé
        l'IP (on_rotation_done) — sinon chaque requête lente post-mark
        escaladerait soft→hard dans la même rafale. Escalade hard réservée
        au cas : soft déjà subi par ce couple, expiré, et re-détection lente
        (l'IP est revenue en service via fallback LRU et reste mauvaise)."""
        if not self.cfg.enabled:
            return {"action": "none", "reason": "disabled"}
        key = (int(sid), str(ip))
        prev_kind = self.cooldown_kind(sid, ip)
        threshold_for, p95_threshold = self.threshold_for(model)
        effective_slow = max(threshold_for, self.cfg.floor_ms)
        tr = self.tracker_for(sid, ip)
        warmup_skip = tr.request_count == 0
        tr.record(duration_ms, effective_slow)
        action, reason = "none", ""
        if status_code is not None and status_code != 200:
            reason = f"http_{status_code}"
        elif warmup_skip:
            reason = "warmup_excluded"
        else:
            should = tr.should_soft_rotate(threshold_for, p95_threshold)
            # [v10 §12.1.1] rotation PRÉDICTIVE : pente EWMA > +X % sur les
            # 5 dernières requêtes — anticipe la lenteur avant les seuils.
            trend_hit = False
            if (
                not should
                and self.cfg.predictive_trend_pct > 0
                and prev_kind is None
                and tr.request_count >= self.cfg.min_requests_before_eval
            ):
                t = tr.trend_pct(5)
                trend_hit = t is not None and t >= self.cfg.predictive_trend_pct

            if should or trend_hit:
                if prev_kind is not None:
                    # déjà sous cooldown ACTIF → rien à faire, attendre la
                    # rotation (sinon ré-escalade intra-rafale)
                    action, reason = "none", f"{prev_kind}_active"
                elif key in self._soft_history:
                    # soft déjà subi par ce couple puis expiré et re-slow → hard
                    self.mark(sid, ip, COOLDOWN_HARD)
                    action, reason = "hard", "repeated_slow_after_soft"
                else:
                    self._soft_history.add(key)
                    self.mark(sid, ip, COOLDOWN_SOFT)
                    action = "soft"
                    reason = "predictive_trend" if trend_hit else "latency_thresholds"
            elif (
                status_code in (None, 200)
                and prev_kind is None
                and tr.consecutive_slow == max(1, self.cfg.consecutive_slow - 1)
            ):
                # [v10 §12.1.3] alerte proactive : une lente avant le seuil —
                # le front-end l'affiche en badge jaune (pas de rotation).
                action, reason = "warn", "approaching_slow"
        return {
            "action": action,
            "reason": reason,
            "ewma": tr.ewma_ms,
            "p95": tr.p95_ms(),
            "count": tr.request_count,
        }


_ENGINE: LatencyRotationEngine | None = None


def get_engine() -> LatencyRotationEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = LatencyRotationEngine()
        try:
            from config.settings import IP_ROTATION

            _ENGINE.update_config(dict(IP_ROTATION.get("latency_rotation") or {}))
        except Exception:
            pass
    return _ENGINE
