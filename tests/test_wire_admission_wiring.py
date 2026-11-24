"""023 T220 — admission wire branchée sur le chemin free réel.

Deux niveaux :
* ``admit_ip`` (admission par IP, lit ``ip_state``) ;
* ``opencode._free_wire_head`` refuse en ``convert`` avant tout egress.
"""

from __future__ import annotations

import pytest

from free import ip_state
from free import wire_admission as wa
from free.wire_admission import Admission


def _store(tmp_path, rows):
    store = ip_state.IPStateStore(tmp_path / "registry.db")
    for provider, ip, state, fp in rows:
        store.transition(
            provider,
            ip,
            state,
            wire_fp=fp,
            reset_at=1000.0 if state == ip_state.SPENT else None,
            evidence_ref="proof" if state == ip_state.MISMATCH else "",
        )
    return store


def test_admit_ip_unknown_ip_is_unproven_and_routes(tmp_path):
    db = tmp_path / "registry.db"
    decision = wa.admit_ip("fp", "203.0.113.9", mode=wa.MODE_CONVERT, db_path=db)
    assert decision.allowed and decision.action == "route"


def test_admit_ip_proven_same_fingerprint_routes(tmp_path):
    _store(tmp_path, [("nordvpn", "203.0.113.9", ip_state.PROVEN, "fp")])
    decision = wa.admit_ip(
        "fp", "203.0.113.9", mode=wa.MODE_CONVERT, db_path=tmp_path / "registry.db"
    )
    assert decision.allowed and decision.action == "route"


def test_admit_ip_proven_different_fingerprint_refused(tmp_path):
    _store(tmp_path, [("nordvpn", "203.0.113.9", ip_state.PROVEN, "old")])
    decision = wa.admit_ip(
        "new", "203.0.113.9", mode=wa.MODE_CONVERT, db_path=tmp_path / "registry.db"
    )
    assert not decision.allowed and decision.action == "refuse"
    assert "mismatch" in decision.reason


def test_admit_ip_terminal_is_refused(tmp_path):
    _store(tmp_path, [("nordvpn", "203.0.113.9", ip_state.SPENT, "fp")])
    decision = wa.admit_ip(
        "fp", "203.0.113.9", mode=wa.MODE_CONVERT, db_path=tmp_path / "registry.db"
    )
    assert not decision.allowed and decision.action == "refuse"


def test_admit_ip_observe_never_blocks(tmp_path):
    _store(tmp_path, [("nordvpn", "203.0.113.9", ip_state.PROVEN, "old")])
    decision = wa.admit_ip(
        "new", "203.0.113.9", mode=wa.MODE_OBSERVE, db_path=tmp_path / "registry.db"
    )
    assert decision.allowed and decision.action == "observe"


def _patch_wire_head(monkeypatch, mode, admission):
    import opencode

    monkeypatch.setattr(opencode, "_station_identity_enabled", lambda: False)
    monkeypatch.setattr(opencode, "_per_ip_identity_enabled", lambda: False)
    monkeypatch.setattr(opencode, "_body_conversation_key", lambda body: "")
    monkeypatch.setattr(opencode, "_official_free_headers", lambda *a, **k: {})
    monkeypatch.setattr(opencode, "_get_free_fp_kwargs", lambda *a, **k: ("", "fp"))
    monkeypatch.setattr(
        opencode, "_free_wire_body", lambda body, force_stream=False, egress_ip=None: ({"model": "x"}, False)
    )
    monkeypatch.setattr(opencode, "_note_current_wire_fp", lambda wire: "wire-fp")
    monkeypatch.setattr(opencode, "_set_free_path_map", lambda *a, **k: None)
    monkeypatch.setattr(opencode._cfg_settings, "yaml_get", lambda *a, **k: mode)
    monkeypatch.setattr(wa, "admit_ip", lambda *a, **k: admission)
    return opencode


def test_free_wire_head_convert_refuses_locally(monkeypatch):
    opencode = _patch_wire_head(monkeypatch, "convert", Admission(False, "refuse", "mismatch"))
    from core.errors import FreeRefusal

    with pytest.raises(FreeRefusal) as exc:
        opencode._free_wire_head(
            {"model": "x", "messages": []},
            "https://example.invalid/v1/chat/completions",
            station=None,
            proxy_url=None,
            egress_ip="203.0.113.9",
            station_runtime=None,
        )
    assert exc.value.status == 422
    assert "wire admission" in exc.value.body


def test_free_wire_head_observe_still_builds_wire(monkeypatch):
    opencode = _patch_wire_head(
        monkeypatch, "observe", Admission(True, "observe", "wire_fp mismatch (observe mode)")
    )
    head = opencode._free_wire_head(
        {"model": "x", "messages": []},
        "https://example.invalid/v1/chat/completions",
        station=None,
        proxy_url=None,
        egress_ip="203.0.113.9",
        station_runtime=None,
    )
    assert head.wire_body == {"model": "x"}
