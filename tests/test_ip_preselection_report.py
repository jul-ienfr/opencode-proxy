"""Contract tests for the IP preselection evidence report (offline)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _module():
    path = ROOT / "scripts" / "ip_preselection.py"
    spec = importlib.util.spec_from_file_location("ip_preselection_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ip_preselection_script"] = module
    spec.loader.exec_module(module)
    return module


def _db(path: Path, *, with_host: bool) -> None:
    conn = sqlite3.connect(path)
    columns = (
        "timestamp TEXT, success INTEGER, tokens_output INTEGER, duration_ms INTEGER, "
        "ttfb_ms INTEGER, free_model_ip TEXT"
    )
    if with_host:
        columns += ", server_host TEXT"
    conn.execute(f"CREATE TABLE requests ({columns})")
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if with_host:
        conn.execute(
            "INSERT INTO requests VALUES(?,1,100,1000,500,"
            "'203.0.113.7','nl1050.nordvpn.com')",
            (now,),
        )
        conn.execute(
            "INSERT INTO requests VALUES(?,1,100,1000,500,"
            "'203.0.113.8','nl1050.nordvpn.com')",
            (now,),
        )
    else:
        conn.execute(
            "INSERT INTO requests VALUES(?,1,100,1000,500,"
            "'203.0.113.7')",
            (now,),
        )
    conn.commit()
    conn.close()


def test_quality_supports_legacy_db_without_server_host(tmp_path, monkeypatch):
    module = _module()
    db = tmp_path / "requests.db"
    _db(db, with_host=False)
    monkeypatch.setattr(module, "DB", db)

    quality = module._quality(72)
    assert quality["hosts"] == {}
    assert quality["ips"]["203.0.113.7"]["n"] == 1


def test_quality_attributes_by_server_host(tmp_path, monkeypatch):
    module = _module()
    db = tmp_path / "requests.db"
    _db(db, with_host=True)
    monkeypatch.setattr(module, "DB", db)

    quality = module._quality(72)
    assert quality["hosts"]["nl1050.nordvpn.com"]["n"] == 2
    assert quality["hosts"]["nl1050.nordvpn.com"]["tps"] == pytest.approx(100.0)


def test_candidates_reports_api_failures_instead_of_empty_success(monkeypatch):
    module = _module()

    def _fail(url, timeout=20, attempts=3):
        raise TimeoutError("api down")

    monkeypatch.setattr(module, "_get_json", _fail)
    candidates, errors = module._candidates(["Albania"])
    assert candidates == []
    assert errors and "api down" in errors[0]


def test_json_report_carries_the_two_audit_annotations(monkeypatch, capsys):
    module = _module()
    monkeypatch.setattr(
        module,
        "_quality",
        lambda hours: {
            "hosts": {},
            "ips": {
                "203.0.113.7": {
                    "n": 1,
                    "tps": 42.0,
                    "ttfb": 500.0,
                    "last": "2026-11-02T08:00:00Z",
                }
            },
        },
    )
    monkeypatch.setattr(module, "_countries", lambda: ["Albania"])
    monkeypatch.setattr(module, "_candidates", lambda countries: ([], []))
    monkeypatch.setattr(sys, "argv", ["ip_preselection.py", "--hours", "72", "--json"])

    assert module.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["db"] == {"ips": 1, "hosts": 0, "requests": 1}
    assert report["matching"] == {"hostname": 0, "ip_legacy": 0, "candidates": 0}
    notes = " ".join(report["notes"])
    assert "ip_registry.enabled=false" in notes
    assert "no-adapter:nordcache_packed" in notes
    assert "server_host" in notes
