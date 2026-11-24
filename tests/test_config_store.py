"""[T027] Tests de l'écrivain unique config/store.py + contrats de câblage."""

from __future__ import annotations

import inspect
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from config import store


def test_atomic_write_then_noop(tmp_path):
    p = tmp_path / "cfg.yaml"
    assert store.atomic_write(str(p), "a: 1\n", reason="test") is True
    mtime = p.stat().st_mtime_ns
    assert store.atomic_write(str(p), "a: 1\n", reason="test") is False
    assert p.stat().st_mtime_ns == mtime


def test_atomic_write_changed(tmp_path):
    p = tmp_path / "cfg.yaml"
    store.atomic_write(str(p), "a: 1\n", reason="test")
    assert store.atomic_write(str(p), "a: 2\n", reason="test") is True
    assert p.read_text(encoding="utf-8") == "a: 2\n"


def test_write_yaml_noop(tmp_path):
    p = tmp_path / "cfg.yaml"
    data = {"ip_rotation": {"enabled": True}, "free_model_map": {"x": "y"}}
    assert store.write_yaml(str(p), data, reason="test") is True
    assert store.write_yaml(str(p), data, reason="test") is False
    assert yaml.safe_load(p.read_text(encoding="utf-8")) == data


def test_upsert_env_preserves_comments_and_order(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# header\n\nB=2\nA=1\n# tail\n", encoding="utf-8")
    assert store.upsert_env(str(p), {"A": "9", "C": "3"}, reason="test") is True
    assert p.read_text(encoding="utf-8") == "# header\n\nB=2\nA=9\n# tail\nC=3\n"


def test_upsert_env_duplicate_keys_all_replaced(tmp_path):
    p = tmp_path / ".env"
    p.write_text("K=old1\nK=old2\n", encoding="utf-8")
    store.upsert_env(str(p), {"K": "new"}, reason="test")
    assert p.read_text(encoding="utf-8") == "K=new\nK=new\n"


def test_upsert_env_rejects_multiline(tmp_path):
    p = tmp_path / ".env"
    with pytest.raises(ValueError):
        store.upsert_env(str(p), {"K": "a\nb"}, reason="test")


def test_upsert_env_noop(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\n", encoding="utf-8")
    assert store.upsert_env(str(p), {"A": "1"}, reason="test") is False


def test_concurrent_upsert_env_no_lost_keys(tmp_path):
    """[T027-fix] upsert_env doit lire ET écrire dans la même transaction.

    Régression : la lecture hors verrou faisait perdre toutes les clés des
    autres écrivains (chaque thread repartait du contenu initial). Appelants
    directs : settings.save_env, make_credentials_env, ops.env_store.
    """
    p = tmp_path / ".env"
    p.write_text("BASE=0\n", encoding="utf-8")
    workers = 16

    def bump(i):
        store.upsert_env(str(p), {f"K{i}": str(i)}, reason="concurrent")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(bump, range(workers)))
    text = p.read_text(encoding="utf-8")
    assert "BASE=0" in text
    for i in range(workers):
        assert f"K{i}={i}" in text, f"clé K{i} perdue par un écrivain concurrent"


def test_transaction_reentrant_no_deadlock(tmp_path):
    p = str(tmp_path / "cfg.yaml")
    done = []

    def worker():
        with store.transaction(p):
            store.write_yaml(p, {"n": 1}, reason="nested")
            with store.transaction(p):
                store.write_yaml(p, {"n": 2}, reason="nested2")
        done.append(True)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "deadlock sur transaction réentrante"
    assert done == [True]
    assert yaml.safe_load((tmp_path / "cfg.yaml").read_text(encoding="utf-8")) == {"n": 2}


def test_concurrent_writers_no_lost_update(tmp_path):
    p = str(tmp_path / "counter.yaml")
    store.write_yaml(p, {"n": 0}, reason="init")
    workers, per = 8, 25

    def bump(_):
        for _ in range(per):
            with store.transaction(p):
                data = store.read_yaml(p)
                data["n"] = int(data.get("n", 0)) + 1
                store.write_yaml(p, data, reason="bump")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(bump, range(workers)))
    assert store.read_yaml(p)["n"] == workers * per


def test_settings_save_env_routes_through_store():
    from config import settings

    src = inspect.getsource(settings.save_env)
    assert "upsert_env" in src
    assert 'open(ENV_PATH, "w"' not in src


def test_dashboard_writers_route_through_store():
    from dashboard import api

    for fn in (
        api._persist_vpn_config,
        api._persist_retry_until_success,
        api._persist_free_model_map,
    ):
        assert hasattr(fn, "__wrapped__"), "décorateur _config_transaction manquant"
    src = inspect.getsource(api._persist_free_model_map.__wrapped__)
    assert "write_yaml" in src
    assert 'open(config_path, "w"' not in src
    src_vpn = inspect.getsource(api._persist_vpn_config.__wrapped__)
    assert "write_yaml" in src_vpn
    assert "Fallback non-atomique" not in src_vpn


def test_cross_process_writers_no_lost_update(tmp_path):
    """Régression 2026-11-16 : portalocker.lock(timeout=...) levait TypeError,
    avalé par le except → écritures sans verrou cross-process. Invisible en
    mono-processus (le verrou thread suffisait) : ce test spawn des VRAIS
    processus ; sans verrou fichier, les updates sont perdus."""
    pytest.importorskip("portalocker")
    store_path = str(Path(store.__file__).resolve())
    target = str(tmp_path / "counter.yaml")
    store.write_yaml(target, {"n": 0}, reason="init")
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import importlib.util, sys, time\n"
        "spec = importlib.util.spec_from_file_location('cfg_store', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "for _ in range(int(sys.argv[3])):\n"
        "    with mod.transaction(sys.argv[2]):\n"
        "        data = mod.read_yaml(sys.argv[2])\n"
        "        data['n'] = int(data.get('n', 0)) + 1\n"
        "        time.sleep(0.005)\n"
        "        mod.write_yaml(sys.argv[2], data, reason='mp')\n",
        encoding="utf-8",
    )
    workers, per = 4, 15
    procs = [
        subprocess.Popen(
            [sys.executable, str(worker), store_path, target, str(per)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(workers)
    ]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode(errors="replace")
        assert b"indisponible" not in err, err.decode(errors="replace")
    assert store.read_yaml(target)["n"] == workers * per


def test_transaction_holds_lock_cross_process(tmp_path):
    """Preuve directe : pendant qu'un processus tient la transaction, un
    autre ne doit PAS pouvoir acquérir le même .lock (fail_when_locked)."""
    portalocker = pytest.importorskip("portalocker")
    store_path = str(Path(store.__file__).resolve())
    target = str(tmp_path / "held.yaml")
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    holder = tmp_path / "holder.py"
    holder.write_text(
        "import importlib.util, os, sys, time\n"
        "spec = importlib.util.spec_from_file_location('cfg_store', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "with mod.transaction(sys.argv[2]):\n"
        "    open(sys.argv[3], 'w').close()\n"
        "    deadline = time.time() + 30\n"
        "    while not os.path.exists(sys.argv[4]) and time.time() < deadline:\n"
        "        time.sleep(0.05)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(holder), store_path, target, str(ready), str(release)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 20
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "le holder n'a jamais atteint la transaction"
        probe = portalocker.Lock(
            target + ".lock", mode="a", timeout=0.5, fail_when_locked=True
        )
        try:
            with pytest.raises(portalocker.LockException):
                probe.acquire()
        finally:
            try:
                probe.release()
            except Exception:
                pass
    finally:
        release.write_text("go", encoding="utf-8")
        _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err.decode(errors="replace")


def test_make_credentials_env_routes_through_store():
    from scripts import make_credentials_env as mce

    src = inspect.getsource(mce._upsert_env_var)
    assert "upsert_env" in src
    assert 'open(path, "w"' not in src


def test_make_credentials_env_upsert_functional(tmp_path):
    from scripts import make_credentials_env as mce

    p = tmp_path / ".env"
    p.write_text("# c\nA=1\n", encoding="utf-8")
    mce._upsert_env_var(str(p), "A", "2")
    mce._upsert_env_var(str(p), "B", "3")
    assert p.read_text(encoding="utf-8") == "# c\nA=2\nB=3\n"


@pytest.mark.parametrize(
    "value",
    ["a\x0bb", "a\x0cb", "a\x1cb", "a\x1db", "a\x85b", "a b", "a b"],
)
def test_upsert_env_rejects_splitlines_separators(tmp_path, value):
    """[fix 2026-11-21] La relecture utilise splitlines() : ces separateurs
    passaient la garde \\n/\\r puis materialisaient une fausse ligne KEY=value."""
    path = tmp_path / ".env"
    with pytest.raises(ValueError, match="multi-ligne"):
        store.upsert_env(str(path), {"SAFE": value}, reason="test")
    assert not path.exists()


@pytest.mark.parametrize("key", ["BAD KEY", "1BAD", "A=B", "é"])
def test_upsert_env_rejects_invalid_keys(tmp_path, key):
    path = tmp_path / ".env"
    with pytest.raises(ValueError, match="variable invalide"):
        store.upsert_env(str(path), {key: "x"}, reason="test")
    assert not path.exists()


def test_upsert_env_still_allows_empty_values(tmp_path):
    path = tmp_path / ".env"
    store.upsert_env(str(path), {"EMPTY": ""}, reason="test")
    assert path.read_text(encoding="utf-8") == "EMPTY=\n"


def test_transaction_fails_closed_when_lock_acquisition_fails(tmp_path, monkeypatch):
    """[fix 2026-11-21] Une erreur d'acquisition du verrou cross-process ne
    doit pas poursuivre l'ecriture sans exclusion (lost update T027)."""
    portalocker = pytest.importorskip("portalocker")

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise OSError("lock unavailable")

    monkeypatch.setattr(portalocker, "Lock", _Boom)
    with pytest.raises(store.ConfigLockError, match="lock unavailable"):
        with store.transaction(str(tmp_path / "cfg.yaml")):
            pass
