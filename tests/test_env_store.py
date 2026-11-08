"""Tests de l'écriture atomique et sérialisée du .env (011 T082)."""

from __future__ import annotations

import threading

from ops.env_store import read_env, update_env


def test_update_env_creates_and_preserves(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text("A=1\nB=2\n", encoding="utf-8")
    update_env(path, {"B": "20", "C": "3"})
    assert read_env(path) == {"A": "1", "B": "20", "C": "3"}
    assert not (tmp_path / ".env.lock").exists()


def test_concurrent_updates_do_not_lose_keys(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text("BASE=0\n", encoding="utf-8")
    threads = [
        threading.Thread(
            target=update_env,
            args=(path, {f"K{i}": str(i)}),
        )
        for i in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    values = read_env(path)
    assert values["BASE"] == "0"
    for i in range(20):
        assert values[f"K{i}"] == str(i)
