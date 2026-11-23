"""[024 T102] `free_pool.json` ne doit plus contenir de stations fantômes.

Mesure du 2026-11-21 : 28 sids négatifs (-1..-28) et 58 clés de santé
fantômes, reliquats d'anciens boots. La persistance ne garde que les sid
réellement configurés (ou, flotte non résolue, les sid positifs).
"""

from __future__ import annotations

from types import SimpleNamespace

from free.pool import FreeIPPool, _state_sid_kept


def test_negative_sids_are_never_persisted():
    assert _state_sid_kept(-1, set()) is False
    assert _state_sid_kept("-28", {1, 2, 3}) is False
    assert _state_sid_kept(0, set()) is False


def test_unknown_positive_sid_is_dropped_when_the_fleet_is_known():
    assert _state_sid_kept(4, {1, 2, 3}) is False
    assert _state_sid_kept(2, {1, 2, 3}) is True


def test_empty_known_fleet_keeps_positive_sids_for_boot_compatibility():
    assert _state_sid_kept(7, set()) is True
    assert _state_sid_kept("7", set()) is True


def test_non_numeric_sid_is_dropped():
    assert _state_sid_kept("station", {1}) is False
    assert _state_sid_kept(None, {1}) is False


def test_known_station_sids_comes_from_the_live_stations():
    pool = object.__new__(FreeIPPool)
    pool._stations = [
        SimpleNamespace(_station=1),
        SimpleNamespace(_station=2),
        SimpleNamespace(_station=0),
    ]
    assert pool._known_station_sids() == {1, 2}
