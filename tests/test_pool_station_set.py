"""test_pool_station_set.py — pool over N stations (plan 18/08 §4).

[plan 18/08 §4] The pool used to be hard-wired to the station 1/2 pair.
Now the active set is dynamic — ``set_stations()`` swaps it at runtime
(GUI dropdown 1-10 hot reload) and the single C4/C5 rotation worker must
survive the swap: queued entries for downscaled stations become no-ops.

Covered here (offline — the same _Station/_pool helpers as
test_pool_connection_failure.py, no docker, no loop tasks unless noted):
  * best-station over 3 stations: preference A (station 1), non-bad wins,
    walks down to the last standing; C1 never bad-marks the last standing
    station when 2 others are already bad.
  * set_stations: replaces the set sorted numerically (station 1 stays the
    preferred pass), prunes _per/_pending/_rotation_tasks of removed
    stations while PRESERVING the remaining stations' state, filters None.
  * downscale 4→2 with a station-3 entry already queued → the worker
    drains it as a no-op (single-flight C4/C5 intact, nothing rotated).
"""
import asyncio
import time

import pytest

from free_ip_pool import FreeIPPool


class _Station:
    """Minimal station double (same shape as test_pool_connection_failure,
    plus the slice get_status() reads)."""

    def __init__(self, sid, *, enabled=True, proxy_mode="vpn",
                 status="connected", current_ip=None, quota=15):
        self._station = sid
        self.enabled = enabled
        self.proxy_mode = proxy_mode
        self.status = status
        self.current_ip = current_ip
        self.current_server = None
        self._quota_per_ip = quota
        self.socks5_url = f"socks5://127.0.0.1:1{sid}080"
        self.armed = []

    def get_status(self):
        return {"station": self._station, "status": self.status}

    def arm_egress_watchdog(self):
        self.armed.append(self._station)


def _pool(st1, st2=None, *, bad_ttl=60, grace=20.0):
    pool = FreeIPPool(st1, st2)
    pool._bad_ttl = float(bad_ttl)
    pool._late_signal_grace = float(grace)
    pool.rotated = []
    pool._launch_rotation = lambda station: pool.rotated.append(station)
    return pool


def _stations3(p):
    """3 stations wired into the pool (the N-station constructor path)."""
    s1, s2, s3 = _Station(1), _Station(2), _Station(3)
    p.set_stations([s1, s2, s3])
    return s1, s2, s3


# ── best-station selection (double embrayage over N > 2) ─────────

def test_best_station_prefers_first_usable():
    s1, s2, s3 = _stations3(p := _pool(_Station(1)))
    assert p._best_station() is s1, "preference A unchanged with N=3"

def test_best_station_skips_bad_stations_down_to_the_last():
    s1, s2, s3 = _stations3(p := _pool(_Station(1)))
    p.notify_connection_failure(s1)
    assert p._best_station() is s2
    p.notify_connection_failure(s2)
    assert p._best_station() is s3, "a single surviving healthy station is found"

def test_best_station_none_when_all_bad():
    """C1 only protects the LAST STANDING station — a station that is
    merely disconnected (e.g. compose up failed) is not usable either,
    so with every station unusable the pool walks to None."""
    s1, s2, s3 = _stations3(p := _pool(_Station(1)))
    p.notify_connection_failure(s1)
    p.notify_connection_failure(s2)
    s3.status = "disconnected"          # not bad-marked, but not connected
    assert p._best_station() is None

def test_c1_last_standing_holds_with_3():
    """Never bad-mark the last standing station — with 3 stations the guard
    must see two bad ones AND refuse to mark the third."""
    s1, s2, s3 = _stations3(p := _pool(_Station(1)))
    p.notify_connection_failure(s1)
    p.notify_connection_failure(s2)
    p.notify_connection_failure(s3)     # last one standing
    assert p._per_station(s3)["bad_until"] is None, \
        "C1: the last usable station is never bad-marked"
    assert p._best_station() is s3


# ── set_stations (hot-reload swap) ───────────────────────────────

def test_set_stations_sorts_numerically():
    s1, s2, s3 = _Station(1), _Station(2), _Station(3)
    p = _pool(s1)
    p.set_stations([s3, s1, s2])        # out of order input
    assert [s._station for s in p._stations] == [1, 2, 3], \
        "station 1 stays the preferred pass"

def test_set_stations_filters_none():
    s1, s2 = _Station(1), _Station(2)
    p = _pool(s1)
    p.set_stations([s1, None, s2])
    assert [s._station for s in p._stations] == [1, 2]

def test_downscale_prunes_removed_stations_only():
    """4→2: the removed stations' per-state, pending flag and in-flight
    rotation slot are pruned; the survivors keep their counters."""
    s1, s2, s3, s4 = (_Station(1), _Station(2), _Station(3), _Station(4))
    p = _pool(s1)
    p.set_stations([s1, s2, s3, s4])
    p._per_station(s1)["request_count"] = 5
    p._per_station(s3)["request_count"] = 7
    p._pending.add(3)
    p._rotation_tasks[3] = object()     # dummy in-flight marker (sync test)
    p._rotation_tasks[4] = object()

    p.set_stations([s1, s2])

    assert [s._station for s in p._stations] == [1, 2]
    assert p._per_station(s1)["request_count"] == 5, "survivor state preserved"
    assert 3 not in p._per and 4 not in p._per
    assert 3 not in p._pending and 3 not in p._rotation_tasks \
        and 4 not in p._rotation_tasks

def test_get_status_after_downscale_only_lists_active():
    s1, s2, s3 = _Station(1), _Station(2), _Station(3)
    p = _pool(s1)
    p.set_stations([s1, s2, s3])
    p.set_stations([s1, s2])
    status = p.get_status()
    assert [st["station"] for st in status["stations"]] == [1, 2]


# ── worker guard: stale queue entries are no-ops ─────────────────

def test_queued_entry_for_downscaled_station_is_noop():
    """A station queued BEFORE the downscale must drain as a no-op: the
    worker survives the swap, nothing rotates, single-flight stays intact."""
    async def _go():
        s1, s2, s3 = _Station(1), _Station(2), _Station(3)
        p = _pool(s1)
        p.set_stations([s1, s2, s3])
        p._rotation_queue.put_nowait(s3)    # queued, then downscaled
        p.set_stations([s1, s2])
        task = asyncio.create_task(p._rotation_worker())
        await asyncio.sleep(0.05)           # let the worker drain the queue
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert p.rotated == [], "the stale entry must never launch a rotation"
        assert 3 not in p._pending, "the dequeued entry cleaned its pending flag"

    asyncio.run(_go())