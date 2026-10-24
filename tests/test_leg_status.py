"""Le statut par série de latence : « observé » ≠ « servi avec succès ».

**L'erreur que ces tests empêchent.** `jev-1.13-free` répondait en 4 s à chaque
appel — plus vite que tout le reste — et ne produisait **rien** : l'amont le
refusait (`api_usage_error`). Le proxy renvoyait `200` avec une trame d'erreur.
Sur la seule foi de la latence, on l'a recommandé comme « 3× plus rapide ». Il
était 3× plus rapide parce qu'il échouait.

**La règle qui en découle.** Une série est « servie » seulement si son **dernier**
statut est 2xx. La latence ne dit rien de la réussite ; le statut, si.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from observability import metrics  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    metrics.reset_leg_status()
    yield
    metrics.reset_leg_status()


class TestMarkAndRead:
    def test_a_successful_series_is_reported(self):
        key = metrics.leg_latency_key("free", "big-pickle")
        metrics.mark_leg_status(key, 200)
        assert "big-pickle" in metrics.leg_ok_models()

    def test_a_refused_series_is_not_reported(self):
        """Le cas `jev` : 200 côté client, 400 côté amont. Ici on enregistre 400."""
        key = metrics.leg_latency_key("free", "jev-1.13-free")
        metrics.mark_leg_status(key, 400)
        assert "jev-1.13-free" not in metrics.leg_ok_models()

    def test_the_last_status_wins(self):
        key = metrics.leg_latency_key("free", "modele-x")
        metrics.mark_leg_status(key, 400)
        metrics.mark_leg_status(key, 200)
        assert "modele-x" in metrics.leg_ok_models()
        metrics.mark_leg_status(key, 500)
        assert "modele-x" not in metrics.leg_ok_models()

    def test_a_2xx_family_is_a_success(self):
        for status in (200, 201, 204):
            metrics.reset_leg_status()
            metrics.mark_leg_status(metrics.leg_latency_key("free", "m"), status)
            assert "m" in metrics.leg_ok_models(), status

    def test_a_3xx_is_not_a_success(self):
        metrics.mark_leg_status(metrics.leg_latency_key("free", "m"), 302)
        assert "m" not in metrics.leg_ok_models()

    def test_endless_garbage_does_not_raise(self):
        for junk in (None, "", "abc", object()):
            metrics.mark_leg_status(metrics.leg_latency_key("free", "m"), junk)
        assert isinstance(metrics.leg_ok_models(), set)


class TestBounds:
    def test_the_status_table_is_bounded(self):
        """Un dictionnaire non borné finirait par manger la mémoire du proxy."""
        for i in range(metrics.LEG_LAT_MAX_KEYS + 40):
            metrics.mark_leg_status(metrics.leg_latency_key("free", f"m{i}"), 200)
        assert len(metrics._LEG_STATUS) <= metrics.LEG_LAT_MAX_KEYS

    def test_reset_clears_everything(self):
        metrics.mark_leg_status(metrics.leg_latency_key("free", "m"), 200)
        metrics.reset_leg_status()
        assert metrics.leg_ok_models() == set()


class TestKeyParsing:
    def test_a_key_without_a_model_is_ignored(self):
        metrics.mark_leg_status("leg_ms:free", 200)
        assert metrics.leg_ok_models() == set()

    def test_another_jambes_series_is_readable_too(self):
        """Le module ne connaît pas la notion de « jambe free » : il lit un préfixe."""
        key = metrics.leg_latency_key("paid", "claude-sonnet-5-0")
        metrics.mark_leg_status(key, 200)
        assert "claude-sonnet-5-0" in metrics.leg_ok_models()
