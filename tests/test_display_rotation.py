"""test_display_rotation.py — P2-13 : rotation cascade + debug_kv + writes concurrents."""

import threading

import dashboard.display as dd


def _setup(tmp_path, monkeypatch, *, max_size=1024, keep=3):
    monkeypatch.setattr(dd, "_DEBUG_MAX_SIZE", max_size)
    monkeypatch.setattr(dd, "_DEBUG_ROTATE_KEEP", keep)
    monkeypatch.setattr(dd, "_DEBUG_FLUSH_INTERVAL", 1)
    monkeypatch.setattr(dd._cfg_settings, "DEBUG", True)
    dd.set_debug_log_file(str(tmp_path / "debug.log"))
    return tmp_path / "debug.log"


def _teardown(monkeypatch):
    try:
        if dd._debug_file is not None:
            dd._debug_file.close()
    except Exception:
        pass
    monkeypatch.setattr(dd, "_debug_file", None)
    monkeypatch.setattr(dd, "_debug_file_path", None)


def test_rotation_cascade_keeps_generations(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, max_size=100, keep=3)
    try:
        for i in range(30):
            dd.debug("x" * 60 + f" {i}")
            dd._debug_file.flush()
        import os

        names = sorted(os.path.basename(f) for f in tmp_path.glob("debug.log*"))
        assert "debug.log" in names
        assert "debug.log.1" in names, names  # au moins 1 génération
        assert not any(n.endswith(".4") for n in names), names  # jamais au-delà de keep
    finally:
        _teardown(monkeypatch)


def test_rotate_keep_1_single_generation(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, max_size=100, keep=1)
    try:
        for i in range(20):
            dd.debug("y" * 60 + f" {i}")
            dd._debug_file.flush()
        import os

        names = sorted(os.path.basename(f) for f in tmp_path.glob("debug.log*"))
        assert "debug.log.2" not in names, names
    finally:
        _teardown(monkeypatch)


def test_debug_kv_format(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, max_size=10**9)
    try:
        dd.debug_kv("db writer", req_id="abc", batch=32)
        content = open(tmp_path / "debug.log", encoding="utf-8").read()
        assert "db writer req_id=abc batch=32" in content
    finally:
        _teardown(monkeypatch)


def test_concurrent_debug_writes_no_crash(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, max_size=10**9)
    errors = []

    def worker(n):
        try:
            for i in range(100):
                dd.debug(f"w{n} line {i} " + "z" * 40)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    try:
        ts = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in ts)
        assert errors == []
        lines = open(tmp_path / "debug.log", encoding="utf-8").read().splitlines()
        assert len(lines) == 800, f"aucune ligne perdue/entrelacée : {len(lines)}"
        assert all(l.startswith("[") and "] w" in l for l in lines)
    finally:
        _teardown(monkeypatch)
