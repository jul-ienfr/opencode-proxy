"""[audit 2026-10-30] Une clé .env valide doit rester utilisable quand toutes
les clés listées sont en pause (has_usable_paid_key() la comptait déjà)."""

import threading

from core.keys import KeyPauser, select_next_key


def test_env_key_used_when_all_listed_keys_are_paused(tmp_path):
    pauser = KeyPauser()
    pauser._PAUSED_FILE = str(tmp_path / "paused.yaml")  # type: ignore[attr-defined]
    pauser.pause_key("sk-listed-1", 60, "test")
    pauser.pause_key("sk-listed-2", 60, "test")

    key, *_ = select_next_key(
        api_keys=[
            {"alias": "a", "api_key": "sk-listed-1"},
            {"alias": "b", "api_key": "sk-listed-2"},
        ],
        env_key="sk-env-valid",
        routing="round-robin",
        pauser=pauser,
        failover_index=0,
        cycle_keys=[],
        cycle_index=0,
        cycle_lock=threading.Lock(),
    )
    assert key["api_key"] == "sk-env-valid"


def test_all_paused_including_env_still_raises(tmp_path):
    pauser = KeyPauser()
    pauser._PAUSED_FILE = str(tmp_path / "paused.yaml")  # type: ignore[attr-defined]
    pauser.pause_key("sk-listed-1", 60, "test")
    pauser.pause_key("sk-env-valid", 60, "test")

    try:
        select_next_key(
            api_keys=[{"alias": "a", "api_key": "sk-listed-1"}],
            env_key="sk-env-valid",
            routing="round-robin",
            pauser=pauser,
            failover_index=0,
            cycle_keys=[],
            cycle_index=0,
            cycle_lock=threading.Lock(),
        )
    except Exception as exc:
        assert "paused" in type(exc).__name__.lower() or "paused" in str(exc).lower()
    else:
        raise AssertionError("toutes les clés sont pausées : une erreur est attendue")
