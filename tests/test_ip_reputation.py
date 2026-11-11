"""IP reputation: corroboration, local evidence, no contact with the target."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("ip_reputation", ROOT / "scripts" / "ip_reputation.py")
assert _SPEC and _SPEC.loader
rep = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = rep
_SPEC.loader.exec_module(rep)


def _fake(payloads):
    def _fetch(url, **kwargs):
        for marker, payload in payloads.items():
            if marker in url:
                return payload
        return None

    return _fetch


def test_local_open_proxy_candidate_is_suspicious_not_malicious(tmp_path):
    lists = tmp_path / "logs" / "proxy_lists"
    lists.mkdir(parents=True)
    (lists / "pool.jsonl").write_text('{"host": "103.26.10.23"}\n', encoding="utf-8")
    report = rep.check_ip("103.26.10.23", offline=True, root=tmp_path)
    assert report["verdict"] == "suspicious"
    assert "pool.jsonl" in report["local"]["lists"]


def test_two_independent_malicious_sources_are_required(tmp_path, monkeypatch):
    monkeypatch.setenv("VIRUSTOTAL_API_KEY", "vt")
    monkeypatch.setenv("ABUSEIPDB_API_KEY", "abuse")
    payloads = {
        "ipwho.is": {"success": True, "country": "RU", "connection": {"asn": 1, "org": "X"}},
        "otx.alienvault": {"pulse_info": {"count": 0}, "validation": []},
        "virustotal": {"data": {"attributes": {"last_analysis_stats": {"malicious": 5}}}},
        "abuseipdb": {"data": {"abuseConfidenceScore": 90, "totalReports": 10}},
    }
    report = rep.check_ip("203.0.113.9", fetcher=_fake(payloads), root=tmp_path)
    assert report["verdict"] == "malicious"
    assert len(report["reasons"]) >= 2


def test_a_single_minor_signal_is_suspicious(tmp_path):
    payloads = {
        "ipwho.is": {"success": True, "country": "US", "connection": {"asn": 2, "org": "Y"}},
        "otx.alienvault": {"pulse_info": {"count": 1}, "validation": []},
    }
    report = rep.check_ip("198.51.100.7", fetcher=_fake(payloads), root=tmp_path)
    assert report["verdict"] == "suspicious"


def test_unknown_without_signal(tmp_path):
    payloads = {
        "ipwho.is": {"success": True, "country": "FR", "connection": {"asn": 3, "org": "Z"}},
        "otx.alienvault": {"pulse_info": {"count": 0}, "validation": []},
    }
    report = rep.check_ip("192.0.2.10", fetcher=_fake(payloads), root=tmp_path)
    assert report["verdict"] == "unknown"
