"""Le refus amont doit atteindre la métrique, malgré un HTTP 200 côté client.

**Le cas mesuré le 2026-10-22.** `jev-1.13-free` est refusé par l'amont
(`api_usage_error / Invalid request`) sur toutes les variantes. Le proxy répond
pourtant `200` : le flux SSE est déjà ouvert, il ne peut plus changer de code
HTTP. Conséquence : le logging d'usage enregistrait `status=200` et
`/api/free-health` affichait `jev` comme **servi**.

**La correction testée ici.** Le refus est déposé dans un ContextVar avant la
trame d'erreur ; le logging d'usage le lit et transmet le **statut amont** à la
métrique. Le client, lui, ne voit rien changer : il reçoit toujours sa trame
`event: error`, comme avant.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import opencode as oc  # noqa: E402
from observability import metrics  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    oc._current_free_refusal.set(None)
    metrics.reset_leg_status()
    yield
    oc._current_free_refusal.set(None)
    metrics.reset_leg_status()


def _log(free_model: str, status: int) -> None:
    oc._log_free_model_usage(
        "modele-client",
        free_model,
        "cle",
        "ws",
        status,
        tokens_in=0,
        tokens_out=0,
        duration_ms=1200,
        client_ip="127.0.0.1",
    )


class TestRefusalReachesTheMetric:
    def test_a_refusal_is_recorded_with_the_upstream_status(self):
        oc._current_free_refusal.set(("jev-1.13-free", 400))
        _log("jev-1.13-free", 200)
        assert "jev-1.13-free" not in metrics.leg_ok_models(), "un refus amont ne doit jamais apparaître comme servi"

    def test_a_genuine_success_is_recorded(self):
        _log("big-pickle", 200)
        assert "big-pickle" in metrics.leg_ok_models()

    def test_the_refusal_only_applies_to_its_own_model(self):
        """Un refus de `jev` ne doit pas salir `big-pickle` dans le même contexte."""
        oc._current_free_refusal.set(("jev-1.13-free", 400))
        _log("big-pickle", 200)
        assert "big-pickle" in metrics.leg_ok_models()

    def test_a_malformed_refusal_does_not_break_logging(self):
        oc._current_free_refusal.set(("jev-1.13-free",))  # tuple trop court
        _log("jev-1.13-free", 200)
        assert "jev-1.13-free" in metrics.leg_ok_models(), (
            "un ContextVar malformé ne doit pas empêcher le logging normal"
        )


class TestTheRefusalIsActuallySet:
    def test_the_context_var_exists_and_defaults_to_none(self):
        """Garde-fou de câblage : s'il disparaît, la trame d'erreur ne marque plus rien."""
        import contextvars

        assert isinstance(oc._current_free_refusal, contextvars.ContextVar)
        assert oc._current_free_refusal.get() is None
