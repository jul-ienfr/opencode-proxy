"""test_shared_rotation.py — SharedRotationState (shared_rotation.py).

The VPN plan's cross-station registry: one file shared by both stations
tracking (a) recently used IPs — reuse is forbidden across stations, not
just per-station — and (b) a global absolute identity cursor that keeps
the two live station indexes distinct even at modulus wrap.

Covered here (plan "Vérification" section 1, offline):
  * count window + age window, OR-ed conservatively (an event survives
    if it is among the newest N OR younger than max_age)
  * cross-station: is_recent() sees the OTHER station's IPs
  * next_identity: absolute cursor, per-station last index, sustained
    uniqueness at wrap for station pairs
  * register_station: live index known WITHOUT bumping the cursor
  * persistence: tmp+rename atomic write, fail-open read (missing file,
    corrupt file), reload round-trip
  * set_window: windows re-read

Never touches the live system: all state lives in tmp_path files; the
class is pure sync — mutate + persist in one call, no await.
"""
import json
import time

import pytest

from shared_rotation import SharedRotationState


def _state(tmp_path, *, recent_ip_window=20, recent_ip_max_age=1800):
    return SharedRotationState({
        "shared_rotation_file": str(tmp_path / "shared_rotation.json"),
        "recent_ip_window": recent_ip_window,
        "recent_ip_max_age": recent_ip_max_age,
    })


def _event(ip, station, age_sec):
    """An event dict as the registry persists it (UTC %Y-%m-%dT%H:%M:%SZ)."""
    ts = time.gmtime(time.time() - age_sec)
    return {"ip": ip, "station": station,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", ts)}


class TestCountWindow:
    def test_count_window_keeps_newest(self, tmp_path):
        """With an age window too small to protect the tail (events aged
        beyond max_age), exactly the newest N survive the trim."""
        s = _state(tmp_path, recent_ip_window=2, recent_ip_max_age=60)
        s._ip_events = [_event(f"1.1.1.{i}", 1, 100) for i in range(5)]
        s._trim()
        assert [e["ip"] for e in s._ip_events] == ["1.1.1.3", "1.1.1.4"]
        assert s.is_recent("1.1.1.3")                # inside count window
        assert s.is_recent("1.1.1.1") is False       # trimmed out
        assert len(s.recent_ips()) == 2

    def test_same_ip_replaces_prior_event(self, tmp_path):
        s = _state(tmp_path, recent_ip_window=10)
        s.record_ip("9.9.9.9", 1)
        s.record_ip("9.9.9.9", 2)                    # re-appears: no duplicate
        s.record_ip("9.9.9.9", 1)
        assert [e["ip"] for e in s._ip_events] == ["9.9.9.9"]
        assert s._ip_events[-1]["station"] == 1


class TestAgeWindow:
    def test_old_tail_dropped_by_age(self, tmp_path):
        """Events outside the newest-N that are older than max_age drop;
        the newest N survive regardless of age (count window protects them)."""
        s = _state(tmp_path, recent_ip_window=4, recent_ip_max_age=60)
        s._ip_events = [_event(f"10.0.0.{i}", 1, 800) for i in range(2)] + \
                       [_event(f"10.0.0.{i}", 1, 61) for i in range(2, 6)]
        s._trim()
        ips = [e["ip"] for e in s._ip_events]
        assert ips == ["10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5"]  # newest 4
        assert s.is_recent("10.0.0.0") is False      # 800 s old AND not in newest-N
        assert s.is_recent("10.0.0.4") is True       # count window protects it

    def test_count_and_age_or_conservative(self, tmp_path):
        """Fresh old events survive alongside the newest N (OR-semantics)."""
        s = _state(tmp_path, recent_ip_window=2, recent_ip_max_age=300)
        s._ip_events = [_event(f"11.0.0.{i}", 1, 100) for i in range(4)]
        s._trim()
        # window_new: last 2 (kept); window_old: first 2, still fresh (<300) → kept
        assert len(s._ip_events) == 4

    def test_hard_cap_bounds_memory(self, tmp_path):
        """The cap binds on EVERY trim (no count-window early return anymore),
        so the true live bound is _WINDOW_CAP: the running list never grows
        past it even with a window above the cap — 250 inserts stay at 100."""
        s = _state(tmp_path, recent_ip_window=150, recent_ip_max_age=10_000)
        for i in range(250):
            s.record_ip(f"12.0.0.{i}", 1)
            assert len(s._ip_events) <= SharedRotationState._WINDOW_CAP
        assert len(s._ip_events) == SharedRotationState._WINDOW_CAP
        assert s.recent_ips()[-1] == "12.0.0.249"             # newest survives

    def test_trim_result_clamped_to_cap(self, tmp_path):
        """A direct _trim() on an over-window registry clamps to _WINDOW_CAP."""
        s = _state(tmp_path, recent_ip_window=150, recent_ip_max_age=10_000)
        s._ip_events = [_event(f"14.0.0.{i}", 1, 100) for i in range(160)]
        s._trim()
        assert len(s._ip_events) == SharedRotationState._WINDOW_CAP
        assert s._ip_events[-1]["ip"] == "14.0.0.159"         # newest survives


class TestCrossStation:
    def test_recent_ips_shared_across_stations(self, tmp_path):
        s = _state(tmp_path)
        s.record_ip("1.2.3.4", 1)
        assert s.is_recent("1.2.3.4")                # same station trivially
        assert s.recent_ips() == ["1.2.3.4"]         # station 2 sees it too

    def test_station_attribution_recorded(self, tmp_path):
        s = _state(tmp_path)
        s.record_ip("1.2.3.4", 2)
        assert s._ip_events[0]["station"] == 2


# ── next_identity: global cursor + non-collision ─────────────────

class TestNextIdentity:
    def test_absolute_cursor_never_returns(self, tmp_path):
        s = _state(tmp_path)
        n = 4
        s.next_identity(1, n)
        idxs = [s.next_identity(1, n) for _ in range(20)]
        assert all(0 <= i < n for i in idxs)
        assert s._cursor == 21                        # monotone, never rewound
        assert s.get_status()["cursor"] == 21

    def test_live_indexes_never_collide_at_wrap(self, tmp_path):
        """Two stations: across many wraps of a 5-profile pool the pair
        (idx_1, idx_2) is never equal."""
        s = _state(tmp_path)
        s.register_station(1, 0)
        s.register_station(2, 0)
        pairs = set()
        for _ in range(200):                          # 40 wraps of the cursor
            i1 = s.next_identity(1, 5)
            i2 = s.next_identity(2, 5)
            assert i1 != i2, f"collision at wrap: {i1} == {i2}"
            pairs.add((i1, i2))
        assert len(pairs) > 1                         # not stuck on one pair
        st = s.get_status()
        assert st["last_index_by_station"][1] == i1
        assert st["last_index_by_station"][2] == i2

    def test_identity_skips_others_live_slot(self, tmp_path):
        """Station 2 takes a slot some distance from station 1 — never it."""
        s = _state(tmp_path)
        s.register_station(1, 2)
        for _ in range(50):
            idx = s.next_identity(2, 5)
            assert idx != 2, "next_identity aliased the other station's live index"

    def test_single_profile_no_collision_semantics(self, tmp_path):
        s = _state(tmp_path)
        assert s.next_identity(1, 1) == 0
        assert s.next_identity(2, 1) == 0             # n==1: uniqueness moot

    def test_register_station_does_not_bump_cursor(self, tmp_path):
        s = _state(tmp_path)
        s.register_station(1, 3)
        assert s._cursor == 0                         # registration is not a rotation


# ── persistence ──────────────────────────────────────────────────

class TestPersistence:
    def test_round_trip_reload(self, tmp_path):
        path = tmp_path / "shared_rotation.json"
        s = SharedRotationState({"shared_rotation_file": str(path)})
        s.record_ip("1.2.3.4", 1)
        s.next_identity(1, 8)
        s2 = SharedRotationState({"shared_rotation_file": str(path)})
        assert s2.recent_ips() == ["1.2.3.4"]
        assert s2._cursor == 1
        assert s2.get_status()["saved_at"]            # persisted timestamp

    def test_missing_file_fails_open(self, tmp_path):
        s = _state(tmp_path)                          # file does not exist
        assert s.recent_ips() == []
        assert s._cursor == 0

    def test_corrupt_file_fails_open(self, tmp_path):
        path = tmp_path / "shared_rotation.json"
        path.write_text("{ not json !!!")
        s = SharedRotationState({"shared_rotation_file": str(path)})
        assert s.recent_ips() == []                   # bad file → empty state, no raise

    def test_atomic_write_no_tmp_left(self, tmp_path):
        s = _state(tmp_path)
        s.record_ip("5.6.7.8", 1)
        assert not (tmp_path / "shared_rotation.json.tmp").exists()
        raw = json.loads((tmp_path / "shared_rotation.json").read_text())
        assert raw["ip_events"][0]["ip"] == "5.6.7.8"


# ── config hot-reload ────────────────────────────────────────────

class TestSetWindow:
    def test_set_window_re_reads_and_retrims(self, tmp_path):
        """Hot-reload re-reads both windows and re-trims ONLY when the
        registry could shrink; the OR-conservative merge keeps fresh events
        even outside the count window — only stale ones drop."""
        s = _state(tmp_path, recent_ip_window=20, recent_ip_max_age=60)
        # 3 stale + 2 fresh; all within the initial window → no trim at boot
        s._ip_events = [_event(f"13.0.0.{i}", 1, 200) for i in range(3)] + \
                       [_event(f"13.0.0.{i}", 1, 10) for i in range(3, 5)]
        assert len(s._ip_events) == 5
        # Widen the count window → re-read; nothing to drop (5 < 10).
        s.set_window({"recent_ip_window": 10, "recent_ip_max_age": 60})
        assert s._recent_ip_window == 10
        assert len(s._ip_events) == 5
        # Shrink the count window → re-trim: the 3 stale events drop,
        # the 2 fresh (age 10 < 60) survive even outside the window.
        s.set_window({"recent_ip_window": 2, "recent_ip_max_age": 60})
        assert len(s._ip_events) == 2
        assert [e["ip"] for e in s._ip_events] == ["13.0.0.3", "13.0.0.4"]
        # Windows are min-clamped on hot-reload: count ≥ 2, max_age ≥ 60.
        s.set_window({"recent_ip_window": 1, "recent_ip_max_age": 5})
        assert s._recent_ip_window == 2
        assert s._recent_ip_max_age == 60.0