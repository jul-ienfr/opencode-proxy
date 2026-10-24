"""V8.6 — un verrou tenu doit dire QUI le tient et s'il sert encore quelque chose.

**Le cas vécu.** `FATAL: another opencode-proxy instance is already running (lock
held: logs/opencode-4000.lock)` — et rien sur le port 4000. Le porteur du verrou
était un processus qui ne servait plus rien : un zombie. Le message ne le disait
pas, donc l'utilisateur n'avait aucune action évidente : son proxy ne répondait
pas, et le redémarrage refusait de démarrer.

**Ce que ces tests exigent.** Le message FATAL distingue les deux situations :

* le port écoute → l'instance tourne, il suffit de l'utiliser ;
* le port n'écoute pas → zombie, et le PID à arrêter est écrit dans le fichier.

Le diagnostic ne doit **jamais** lever : il s'exécute sur le chemin où l'on
quitte le programme.
"""

from __future__ import annotations

import pathlib
import socket
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops.lock import diagnose_holder  # noqa: E402


def _free_port() -> int:
    """Un port fermé, sans supposition sur l'environnement."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture()
def lock_file(tmp_path):
    path = tmp_path / "opencode-4000.lock"
    path.write_text("pid=4242\n", encoding="ascii")
    return str(path)


class TestZombieHolder:
    def test_a_closed_port_is_reported_as_a_zombie(self, lock_file):
        lignes = diagnose_holder(lock_file, host="127.0.0.1", port=_free_port())
        texte = "\n".join(lignes)
        assert "zombie" in texte
        assert "nothing is listening" in texte

    def test_the_pid_to_kill_is_named(self, lock_file):
        lignes = diagnose_holder(lock_file, host="127.0.0.1", port=_free_port())
        assert any("4242" in ligne for ligne in lignes)

    def test_the_remedy_is_actionable(self, lock_file):
        lignes = diagnose_holder(lock_file, host="127.0.0.1", port=_free_port())
        texte = "\n".join(lignes)
        assert "kill 4242" in texte or "delete" in texte


class TestLiveHolder:
    def test_a_listening_port_says_the_instance_is_serving(self, lock_file):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            lignes = diagnose_holder(lock_file, host="127.0.0.1", port=port)
        finally:
            sock.close()
        texte = "\n".join(lignes)
        assert "IS serving" in texte
        assert "zombie" not in texte

    def test_the_wildcard_host_is_probed_on_loopback(self, lock_file):
        """Le proxy écoute sur 0.0.0.0 : sonder 0.0.0.0 ne prouve rien."""
        sock = socket.socket()
        sock.bind(("0.0.0.0", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            lignes = diagnose_holder(lock_file, host="0.0.0.0", port=port)
        finally:
            sock.close()
        assert "\n".join(lignes).count("IS serving") == 1


class TestRobustness:
    def test_without_host_or_port_only_the_pid_is_reported(self, lock_file):
        lignes = diagnose_holder(lock_file)
        texte = "\n".join(lignes)
        assert "4242" in texte
        assert "zombie" not in texte and "IS serving" not in texte

    def test_a_missing_lock_file_does_not_raise(self, tmp_path):
        lignes = diagnose_holder(str(tmp_path / "absent.lock"), host="127.0.0.1", port=_free_port())
        assert isinstance(lignes, list)

    def test_a_garbage_lock_file_does_not_raise(self, tmp_path):
        path = tmp_path / "garbage.lock"
        path.write_bytes(b"\xff\xfe\x00pas un pid")
        assert isinstance(diagnose_holder(str(path), host="127.0.0.1", port=_free_port()), list)

    def test_an_empty_lock_file_does_not_raise(self, tmp_path):
        path = tmp_path / "vide.lock"
        path.write_text("", encoding="ascii")
        assert isinstance(diagnose_holder(str(path)), list)

    def test_the_helper_never_raises_on_a_bad_port(self, lock_file):
        assert isinstance(diagnose_holder(lock_file, host="127.0.0.1", port=-1), list)
