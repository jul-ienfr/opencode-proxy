"""V8.2 — le canary compte le contenu, pas le statut.

**Le test qui aurait attrapé la panne du 2026-10-22.** `jev-1.13-free` répondait
`200` en 4 s sans rien produire. Un canary naïf (`status == 200 ?`) l'aurait
déclaré sain pendant des heures. Les tests ci-dessous verrouillent la règle
inverse : un verdict `ok` exige un contenu réel, vérifié par la sonde.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from observability import canary  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    canary.clear()
    yield
    canary.clear()


class TestVerdicts:
    def test_a_single_success_is_ok(self):
        canary.record("free.chat", True, latency_ms=1200)
        info = canary.snapshot()["probes"]["free.chat"]
        assert info["verdict"] == "ok"
        assert info["consecutive_failures"] == 0

    def test_one_failure_is_only_degraded(self):
        """Une jambe free a des ratés normaux : crier au loup à chaque hoquet
        rendrait l'alerte inutilisable."""
        canary.record("free.chat", False, detail="vide")
        assert canary.snapshot()["probes"]["free.chat"]["verdict"] == "degraded"

    def test_three_consecutive_failures_are_failing(self):
        for _ in range(canary.ALERT_AFTER):
            canary.record("free.chat", False, detail="refus")
        assert canary.snapshot()["probes"]["free.chat"]["verdict"] == "failing"

    def test_a_success_resets_the_counter(self):
        for _ in range(canary.ALERT_AFTER):
            canary.record("free.chat", False)
        canary.record("free.chat", True)
        info = canary.snapshot()["probes"]["free.chat"]
        assert info["consecutive_failures"] == 0
        assert info["verdict"] == "ok"

    def test_the_counter_only_counts_the_tail(self):
        canary.record("free.chat", False)
        canary.record("free.chat", True)
        canary.record("free.chat", False)
        canary.record("free.chat", False)
        info = canary.snapshot()["probes"]["free.chat"]
        assert info["consecutive_failures"] == 2
        assert info["verdict"] == "degraded"


class TestOverall:
    def test_no_probe_yet_is_unknown(self):
        """`unknown` et non `ok` : ne rien avoir mesuré n'est pas une bonne nouvelle."""
        payload = canary.snapshot()
        assert payload["status"] == "unknown"
        assert payload["never_run"] == list(canary.PROBES)

    def test_one_failing_probe_contaminates_the_verdict(self):
        canary.record("free.chat", True)
        for _ in range(canary.ALERT_AFTER):
            canary.record("free.messages", False)
        assert canary.snapshot()["status"] == "failing"

    def test_degraded_is_reported_when_nothing_is_failing(self):
        canary.record("free.chat", True)
        canary.record("free.messages", False)
        assert canary.snapshot()["status"] == "degraded"

    def test_all_green_is_ok(self):
        for probe in canary.PROBES:
            canary.record(probe, True)
        payload = canary.snapshot()
        assert payload["status"] == "ok"
        assert payload["never_run"] == []

    def test_an_undeclared_probe_is_surfaced(self):
        """Mieux vaut voir un nom inattendu que perdre la mesure en silence."""
        canary.record("paid.chat", True)
        assert canary.snapshot()["undeclared"] == ["paid.chat"]


class TestBounds:
    def test_history_is_bounded(self):
        for i in range(canary.HISTORY + 15):
            canary.record("free.chat", i % 2 == 0)
        assert canary.snapshot()["probes"]["free.chat"]["samples"] == canary.HISTORY

    def test_ok_rate_is_computed_on_the_window(self):
        canary.record("free.chat", True)
        canary.record("free.chat", False)
        assert canary.snapshot()["probes"]["free.chat"]["ok_rate"] == 0.5

    def test_garbage_does_not_raise(self):
        canary.record("", True)
        canary.record(None, False)
        canary.record("free.chat", False, latency_ms=None, detail=None)
        assert isinstance(canary.snapshot(), dict)

    def test_clear_resets_everything(self):
        canary.record("free.chat", True)
        canary.clear()
        assert canary.snapshot()["status"] == "unknown"


class TestTheLessonIsEncoded:
    def test_the_module_documents_that_status_is_not_enough(self):
        """Régression documentaire : la raison d'être du module doit rester lisible."""
        source = (ROOT / "observability" / "canary.py").read_text(encoding="utf-8")
        assert "200" in source
        assert "trame d'erreur" in source or "event: error" in source
        assert "contenu" in source.lower()


class TestTheProbeVerdict:
    """La règle qui distingue un vrai succès d'une trame d'erreur en 200.

    `_canary_verdict` est le seul endroit où cette décision se prend : si elle
    est fausse, le canary ment — et un canary qui ment est pire qu'aucun canary.
    """

    @pytest.fixture(autouse=True)
    def _import(self):
        import opencode  # noqa: F401 — importe la sonde réelle

        self.verdict = opencode._canary_verdict

    def test_text_delta_is_a_success(self):
        ok, _ = self.verdict('data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"OK"}}')
        assert ok is True

    def test_thinking_delta_is_a_success(self):
        """Un modèle qui ne renvoie que du raisonnement a bien répondu."""
        ok, _ = self.verdict('data: {"type":"content_block_delta","delta":{"type":"thinking_delta"}}')
        assert ok is True

    def test_an_error_frame_is_never_a_success(self):
        """Le cas `jev` exact : HTTP 200, flux ouvert, modèle refusé."""
        ok, raison = self.verdict('event: error\ndata: {"error":{"message":"Free model request failed: refusee"}}')
        assert ok is False
        assert "erreur" in raison

    def test_an_openai_error_payload_is_not_a_success(self):
        ok, _ = self.verdict('data: {"type":"error","error":{"type":"api_error"}}')
        assert ok is False

    def test_an_empty_stream_is_not_a_success(self):
        """Un flux qui se termine sans contenu : le piège du « 200 rapide »."""
        ok, raison = self.verdict("event: message_stop")
        assert ok is False
        assert "contenu" in raison

    def test_garbage_is_not_a_success(self):
        ok, _ = self.verdict("")
        assert ok is False
