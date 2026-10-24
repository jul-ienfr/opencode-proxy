"""V8.8 — un « reset » qui ne survit pas au redémarrage n'est pas un reset.

**Le cas vécu, deux fois.** Un pause de clé `reason: "test"` maintenait
`/health` en `degraded` sans explication. `POST /api/key-pauses/reset` répondait
`{"reset": "all", "count": 1}` et `GET /api/key-pauses` renvoyait bien `{}` —
puis, au redémarrage suivant, le pause était **revenu** : l'endpoint vidait les
dicts en mémoire sans appeler `_save()`, donc le fichier de pauses conservait
l'entrée et le boot la rechargeait.

**Ce que ces tests exigent.** Après `unpause_all()` :

* la mémoire est vide ;
* le fichier est vide ;
* une **nouvelle instance** construite depuis ce même fichier ne voit aucun pause.

Le troisième point est le seul qui prouve la persistance : sans lui, un test qui
ne regarde que la mémoire passerait au vert sur le bug d'origine.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.keys import KeyPauser  # noqa: E402


def _pauser(tmp_path: pathlib.Path) -> KeyPauser:
    pauser = KeyPauser()
    pauser._PAUSED_FILE = str(tmp_path / "paused_keys.yaml")  # instance own path
    return pauser


def _fichier(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "paused_keys.yaml"


class TestPauseIsPersisted:
    def test_a_pause_is_written_to_disk(self, tmp_path):
        pauser = _pauser(tmp_path)
        pauser.pause_key("sk-test-1", 300, reason="test")
        pauser._save()
        contenu = yaml.safe_load(_fichier(tmp_path).read_text(encoding="utf-8")) or {}
        assert contenu.get("paused_keys"), "un pause doit être persisté"

    def test_a_reloaded_instance_still_sees_the_pause(self, tmp_path):
        """La preuve que le fichier est bien la source de vérité au boot."""
        pauser = _pauser(tmp_path)
        pauser.pause_key("sk-test-1", 300, reason="test")
        pauser._save()

        recharge = _pauser(tmp_path)
        recharge.load(["sk-test-1"])  # même fichier, instance neuve
        assert recharge.get_all_status(), "le boot doit recharger le pause"


class TestUnpauseAllReallyClearsEverything:
    def test_memory_is_cleared(self, tmp_path):
        pauser = _pauser(tmp_path)
        pauser.pause_key("sk-test-1", 300, reason="test")
        assert pauser.unpause_all() == 1
        assert pauser.get_all_status() == {}

    def test_the_file_is_cleared(self, tmp_path):
        pauser = _pauser(tmp_path)
        pauser.pause_key("sk-test-1", 300, reason="test")
        pauser.unpause_all()
        contenu = yaml.safe_load(_fichier(tmp_path).read_text(encoding="utf-8")) or {}
        assert not contenu.get("paused_keys"), "le fichier doit être vidé, pas seulement la mémoire"

    def test_a_restart_does_not_bring_the_pause_back(self, tmp_path):
        """LE test de non-régression : c'est exactement ce qui a échoué en vrai."""
        pauser = _pauser(tmp_path)
        pauser.pause_key("sk-test-1", 300, reason="test")
        pauser.unpause_all()

        apres_redemarrage = _pauser(tmp_path)
        apres_redemarrage.load(["sk-test-1"])
        assert apres_redemarrage.get_all_status() == {}, (
            "le pause est revenu au boot : unpause_all n'a pas persisté"
        )

    def test_unpause_all_on_an_empty_state_is_harmless(self, tmp_path):
        pauser = _pauser(tmp_path)
        assert pauser.unpause_all() == 0

    def test_unpause_all_returns_the_real_count(self, tmp_path):
        pauser = _pauser(tmp_path)
        pauser.pause_key("sk-test-1", 300, reason="a")
        pauser.pause_key("sk-test-2", 300, reason="b")
        assert pauser.unpause_all() == 2


class TestTheEndpointPersists:
    def test_reset_endpoint_clears_and_persists(self, tmp_path):
        from fastapi.testclient import TestClient

        import opencode as oc

        ancien = oc._key_pauser._PAUSED_FILE
        oc._key_pauser._PAUSED_FILE = str(_fichier(tmp_path))
        try:
            oc._key_pauser.pause_key("sk-test-endpoint", 300, reason="test")
            oc._key_pauser._save()
            client = TestClient(oc.app)

            reponse = client.post("/api/key-pauses/reset")
            assert reponse.status_code == 200
            assert reponse.json()["reset"] == "all"
            assert client.get("/api/key-pauses").json() == {}

            recharge = _pauser(tmp_path)
            recharge.load(["sk-test-endpoint"])
            assert recharge.get_all_status() == {}, (
                "l'endpoint n'a pas persisté : le pause reviendra au prochain boot"
            )
        finally:
            oc._key_pauser._PAUSED_FILE = ancien
            oc._key_pauser.unpause_all()
