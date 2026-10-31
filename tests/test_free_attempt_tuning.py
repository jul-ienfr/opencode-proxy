"""Réglages jambe free : gigue anti-corrélation + budget couvrant les stations.

* ``_free_retry_delay_seconds`` : bornes [0, 2.0] et base 0.5×attempt
  inchangées, gigue ±30 % en plus (hors tests → 0.0).
* ``effective_free_max_attempts`` (auto) : suit le nombre de stations
  configurées jusqu'au max architectural 10 (avant : 5 excluait
  systématiquement une station à 6 configurées).
"""

import opencode as oc


def test_retry_delay_jitter_bounds(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    vals = [oc._free_retry_delay_seconds(a) for a in (1, 2, 3, 4, 5, 10) for _ in range(40)]
    assert all(0.0 <= d <= 2.0 for d in vals)
    # attempt élevé reste plafonné malgré la gigue haute
    assert max(vals) <= 2.0
    # la base 0.5×attempt transparaît (attempt 1 < attempt 4 en moyenne)
    import statistics

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    m1 = statistics.mean(oc._free_retry_delay_seconds(1) for _ in range(60))
    m4 = statistics.mean(oc._free_retry_delay_seconds(4) for _ in range(60))
    assert 0.3 <= m1 <= 0.7
    assert 1.2 <= m4 <= 2.0


def test_retry_delay_multiplier_applied(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(oc.random, "uniform", lambda a, b: 1.3)
    assert oc._free_retry_delay_seconds(2) == min(2.0, 1.0 * 1.3)
    monkeypatch.setattr(oc.random, "uniform", lambda a, b: 0.7)
    assert oc._free_retry_delay_seconds(2) == 1.0 * 0.7


def test_retry_delay_zero_in_tests():
    # Sous pytest (PYTEST_CURRENT_TEST posé) : hermétique, instantané.
    assert oc._free_retry_delay_seconds(3) == 0.0


def test_auto_budget_covers_all_configured_stations(monkeypatch):
    monkeypatch.setitem(oc.IP_ROTATION, "auto_max_free_attempts", True)
    monkeypatch.setattr(oc, "_free_ip_pool", None)
    n = oc._cfg_settings.resolved_station_count(oc.IP_ROTATION)
    assert 1 <= n <= 10
    assert oc.effective_free_max_attempts() == max(1, min(n, 10))


class _UsableStation:
    status = "connected"
    current_ip = "10.0.0.1"


class _UsablePool:
    socks5_mode = False

    def __init__(self, n):
        self._stations = [_UsableStation() for _ in range(n)]

    def _station_usable(self, st, *, exclude_approaching=False, forced_pool=None):
        return True


def test_strict_free_manual_budget_tries_all_usable_stations(monkeypatch):
    """[2026-10-29] strict_free : 3 réglé mais 6 stations → 6 essais.

    Sinon 3 IP trial-dead suffisaient à rendre « quota épuisé » alors que
    3 stations fraîches n'avaient pas été essayées (mesuré en prod).
    """
    monkeypatch.setattr(oc, "_free_ip_pool", _UsablePool(6))
    monkeypatch.setattr(oc._cfg_settings, "resolved_station_count", lambda *_a, **_k: 6)
    monkeypatch.setitem(oc.IP_ROTATION, "auto_max_free_attempts", False)
    monkeypatch.setitem(oc.IP_ROTATION, "max_free_attempts", 3)

    monkeypatch.setitem(oc.IP_ROTATION, "strict_free", True)
    assert oc.effective_free_max_attempts() == 6

    monkeypatch.setitem(oc.IP_ROTATION, "strict_free", False)
    assert oc.effective_free_max_attempts() == 3, "hors strict_free, le budget manuel reste souverain"
