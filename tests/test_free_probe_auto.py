"""Promotion automatique des modèles détectés par la sonde free-leg."""

from __future__ import annotations

import json
import time

import config.discovery as discovery
import opencode as oc
from config import settings as cs
from opencode import _free_probe_accepts


def test_positive_verdicts_promote_but_not_free_does_not():
    assert _free_probe_accepts("future-model", {"verdict": "alive"}) is True
    assert _free_probe_accepts("future-model", {"verdict": "limited"}) is True
    assert _free_probe_accepts("future-model", {"verdict": "not_free"}) is False
    assert _free_probe_accepts("future-model", {"verdict": "dead"}) is False
    assert _free_probe_accepts("future-model", {"verdict": "unknown"}) is False


def test_not_probeable_promotes_only_a_real_free_id():
    assert _free_probe_accepts("jev-1.13", {"verdict": "not_probeable"}) is False
    assert _free_probe_accepts("jev-1.13-free", {"verdict": "not_probeable"}) is True
    assert (
        _free_probe_accepts(
            "mapped-alias",
            {"verdict": "not_probeable"},
            map_targets={"mapped-alias"},
        )
        is True
    )
    assert (
        _free_probe_accepts(
            "big-pickle",
            {"verdict": "not_probeable"},
            models={"big-pickle": {"source": "free_verified"}},
        )
        is True
    )


def test_probed_cache_persists_only_accepted_verdicts(monkeypatch, tmp_path):
    target = tmp_path / "free_models_probed.json"
    monkeypatch.setattr(cs, "FREE_MODELS_PROBED_FILE", target)
    monkeypatch.setattr(cs, "_PROBED_FREE_MODELS", {})
    monkeypatch.setattr(cs, "_PROBED_FREE_IDS", set())

    written = cs.record_probed_free_models(
        {
            "future-free": {
                "verdict": "limited",
                "status": 429,
                "reason": "quota",
                "checked_at": "2026-10-28T00:00:00Z",
                "checked_epoch": 123.0,
            },
            "gpt-5.5": {"verdict": "not_free", "status": 401, "reason": "refus"},
            "unknown-model": {"verdict": "unknown", "status": 400, "reason": "shape"},
        }
    )

    assert written == 1
    data = json.loads(target.read_text(encoding="utf-8"))
    assert set(data["models"]) == {"future-free"}
    assert data["models"]["future-free"]["verdict"] == "limited"
    assert data["models"]["future-free"]["checked_epoch"] == 123.0
    assert cs._PROBED_FREE_IDS == {"future-free"}


def test_forget_probed_model_removes_it_from_cache(monkeypatch, tmp_path):
    target = tmp_path / "free_models_probed.json"
    monkeypatch.setattr(cs, "FREE_MODELS_PROBED_FILE", target)
    monkeypatch.setattr(cs, "_PROBED_FREE_MODELS", {})
    monkeypatch.setattr(cs, "_PROBED_FREE_IDS", set())
    monkeypatch.setattr(
        cs,
        "_FREE_DISCOVERY_STATE",
        {"probe": {"results": {"dead-free": {"verdict": "limited"}}}},
    )

    cs.record_probed_free_models({"dead-free": {"verdict": "limited", "status": 429}})
    removed = cs.forget_probed_free_models(["dead-free"])

    assert removed == 1
    assert cs._PROBED_FREE_IDS == set()
    assert json.loads(target.read_text(encoding="utf-8"))["models"] == {}
    assert cs._FREE_DISCOVERY_STATE["probe"]["results"] == {}


def test_worker_promotes_an_alive_candidate_and_persists_it(monkeypatch, tmp_path):
    target = tmp_path / "free_models_probed.json"
    monkeypatch.setattr(cs, "FREE_MODELS_PROBED_FILE", target)
    monkeypatch.setattr(cs, "FREE_MODELS", set())
    monkeypatch.setattr(cs, "FREE_MODEL_POOL", [])
    monkeypatch.setattr(cs, "_PROBED_FREE_MODELS", {})
    monkeypatch.setattr(cs, "_PROBED_FREE_IDS", set())
    monkeypatch.setattr(
        cs,
        "_FREE_DISCOVERY_STATE",
        {"probe": {"enabled": True, "results": {}}},
    )
    monkeypatch.setattr(
        discovery,
        "probe_cfg",
        lambda _raw: {
            "enabled": True,
            "timeout": 1.0,
            "concurrency": 1,
            "max_ids": 10,
            "dead_ttl_s": 86400.0,
            "recheck_interval_s": 1800.0,
        },
    )
    monkeypatch.setattr(
        discovery,
        "probe_free_models",
        lambda ids, **_kw: {
            "future-auto": {
                "verdict": "limited",
                "status": 429,
                "reason": "quota",
                "checked_at": "2026-10-28T00:00:00Z",
                "checked_epoch": 1.0,
            }
        },
    )

    oc._free_probe_new_worker(["future-auto"])

    assert cs.FREE_MODELS == {"future-auto"}
    assert cs.FREE_MODEL_POOL == ["future-auto"]
    assert cs._FREE_DISCOVERY_STATE["probe"]["summary"] == {"limited": 1}
    assert target.exists()
    assert "future-auto" in json.loads(target.read_text(encoding="utf-8"))["models"]


def _worker_cfg(monkeypatch, *, max_ids: int = 10, limited_recheck_s: float = 3600.0):
    monkeypatch.setattr(
        discovery,
        "probe_cfg",
        lambda _raw: {
            "enabled": True,
            "timeout": 1.0,
            "concurrency": 1,
            "max_ids": max_ids,
            "dead_ttl_s": 86400.0,
            "recheck_interval_s": 1800.0,
            "limited_recheck_s": limited_recheck_s,
        },
    )


def _worker_env(monkeypatch, tmp_path, free_models=None, results=None):
    monkeypatch.setattr(cs, "FREE_MODELS_PROBED_FILE", tmp_path / "free_models_probed.json")
    monkeypatch.setattr(cs, "FREE_MODELS", set(free_models or ()))
    monkeypatch.setattr(cs, "FREE_MODEL_POOL", [])
    monkeypatch.setattr(cs, "_PROBED_FREE_MODELS", {})
    monkeypatch.setattr(cs, "_PROBED_FREE_IDS", set())
    monkeypatch.setattr(
        cs,
        "_FREE_DISCOVERY_STATE",
        {"probe": {"enabled": True, "results": dict(results or {})}},
    )


def _fake_probe(monkeypatch, probed: list[str], verdict: str = "alive"):
    def fake(ids, **_kw):
        probed.extend(ids)
        return {
            mid: {
                "verdict": verdict,
                "status": 200 if verdict == "alive" else 429,
                "reason": "2xx" if verdict == "alive" else "quota",
                "checked_at": "2026-10-28T00:00:00Z",
                "checked_epoch": time.time(),
            }
            for mid in ids
        }

    monkeypatch.setattr(discovery, "probe_free_models", fake)


def test_worker_reprobe_un_limited_perime(monkeypatch, tmp_path):
    now = time.time()
    _worker_env(
        monkeypatch,
        tmp_path,
        free_models={"old-limited-free", "new-limited-free"},
        results={
            "old-limited-free": {"verdict": "limited", "checked_epoch": now - 10000.0},
            "new-limited-free": {"verdict": "limited", "checked_epoch": now},
        },
    )
    _worker_cfg(monkeypatch)
    probed: list[str] = []
    _fake_probe(monkeypatch, probed)

    oc._free_probe_new_worker(["old-limited-free", "new-limited-free"])

    assert probed == ["old-limited-free"]
    data = json.loads((tmp_path / "free_models_probed.json").read_text(encoding="utf-8"))
    assert data["models"]["old-limited-free"]["verdict"] == "alive"


def test_worker_borne_le_lot_au_budget(monkeypatch, tmp_path):
    _worker_env(monkeypatch, tmp_path, free_models={"aa-free", "bb-free", "cc-free"})
    _worker_cfg(monkeypatch, max_ids=2)
    probed: list[str] = []
    _fake_probe(monkeypatch, probed)

    oc._free_probe_new_worker(["aa-free", "bb-free", "cc-free"])

    assert len(probed) == 2


def test_load_probed_cache_conserve_l_age_du_verdict(monkeypatch, tmp_path):
    from datetime import datetime

    target = tmp_path / "free_models_probed.json"
    checked_at = "2026-10-28T13:10:50+00:00"
    target.write_text(
        json.dumps({"models": {"old-limited-free": {"verdict": "limited", "status": 429, "checked_at": checked_at}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cs, "FREE_MODELS_PROBED_FILE", target)
    monkeypatch.setattr(cs, "_PROBED_FREE_MODELS", {})
    monkeypatch.setattr(cs, "_PROBED_FREE_IDS", set())
    monkeypatch.setattr(cs, "FREE_MODELS", set())
    monkeypatch.setattr(cs, "FREE_MODEL_POOL", [])
    monkeypatch.setattr(cs, "_FREE_DISCOVERY_STATE", {})
    monkeypatch.setattr(cs, "GO_ONLY_IDS", set())
    monkeypatch.setattr(cs, "PURGED_IDS", set())

    assert cs.load_probed_free_models() == 1
    row = cs._FREE_DISCOVERY_STATE["probe"]["results"]["old-limited-free"]
    assert row["checked_epoch"] == datetime.fromisoformat(checked_at).timestamp()
