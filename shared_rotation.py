"""
Shared rotation state — cross-station IP registry + global identity cursor.

The two VPN stations each keep their own per-station IP history and
identity index; nothing was shared, so a station could re-enter an IP
recently used by the OTHER station, and both stations could serve the
same client fingerprint at the same time. This module is the single
shared registry:

- recent IPs of BOTH stations, persisted to logs/shared_rotation.json
- one absolute, monotone identity cursor that BOTH stations advance
  from, guaranteeing their live identities never collide as long as the
  profile pool has >= 2 entries

The whole module is synchronous — callers mutate then persist with no
``await`` in between, so inside the single asyncio loop (the proxy holds
a global one-process instance lock) each record/advance operation is
atomic. Persists are tmp+rename (same pattern as VPNManager.save_state).

Backward compatibility: all reads are fail-open (missing/corrupt file ->
empty state, local-only behavior downstream). When identity diversity is
off or the pool has a single profile, ``next_identity`` degenerates
gracefully — the existing len<=1 gate in VPNManager.current_identity
already pins everyone to profile[0].
"""

import os
import time
import json
import calendar
import logging
from typing import Optional

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.abspath(__file__))


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_utc(value: str) -> Optional[float]:
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
        self._file = (file_cfg if isinstance(file_cfg, str) and file_cfg.strip()
                      else os.path.join(ROOT, "logs", "shared_rotation.json"))
        if not os.path.isabs(self._file):
            self._file = os.path.join(ROOT, self._file)

        self._ip_events: list[dict] = []  # [{ip, station, time}]
        self._cursor: int = 0             # ABSOLUTE monotone identity counter
        self._last_index_by_station: dict[int, int] = {}
        self._country_cursor: int = 0     # ABSOLUTE monotone country counter
        self._last_country_by_station: dict[int, int] = {}
        self._saved_at: Optional[str] = None
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
        self._ip_events.append({
            "ip": ip,
            "station": int(station),
            "time": _now_utc(),
        })
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
        self._country_cursor += 1
        idx = (self._country_cursor + offset * (station - 1)) % n
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
        idx = ((self._country_cursor + 1) + offset * (station - 1)) % n
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
        window_new = self._ip_events[-self._recent_ip_window:]
        window_old = [e for e in self._ip_events[:-self._recent_ip_window]
                      if _fresh(e, cutoff)]
        self._ip_events = window_old + window_new
        if len(self._ip_events) > self._WINDOW_CAP:
            self._ip_events = self._ip_events[-self._WINDOW_CAP:]

    def _load(self) -> None:
        """Load persisted state from disk (fail-open)."""
        try:
            if not os.path.exists(self._file):
                return
            with open(self._file, "r") as f:
                state = json.load(f)
            events = state.get("ip_events")
            if isinstance(events, list):
                self._ip_events = [
                    {"ip": str(e.get("ip")), "station": int(e.get("station", 0)),
                     "time": str(e.get("time", ""))}
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
            logger.debug("[shared-rotation] state loaded from %s (%d IPs, cursor %d)",
                         self._file, len(self._ip_events), self._cursor)
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