"""test_persistence_guard.py — plan 18/08 §4: persistance robuste.

§4.1 save_state (vpn_manager.py): the atomic tmp+os.replace write now copies
the current on-disk state to ``*.bak`` (last-good) BEFORE the overwrite, and
failures are logged with a traceback but never raised (saving stays
non-fatal). A corrupted/truncated write must never cost the previous good
snapshot.

Verified behaviours (real VPNManager logic, only os.replace faked — the
established offline FakeVPNManager pattern from test_vpn_freshness.py):
- first save: no .bak (nothing to back up yet) [20] intact.
- second save: the previous snapshot is copied to .bak BEFORE the new state
  overwrites the file — the .bak holds the OLD current_ip, the live file the
  NEW one.
- a failing write (os.replace raises, e.g. disk full) after the .bak copy:
  the exception is logged WITH traceback (exc_info), never raised — the live
  file keeps its previous content and the .bak remains recoverable.

§4.2 cooldown sweep (opencode.py): without a periodic sweep the
(model, IP) cooldown map grows unbounded — each rotation leaves the previous
IP's key behind (expiry only cleaned lazily on that exact key's next lookup).
The sweep drops expired entries; the len()>32 guard is now a soft-limit for
the gap between ticks (expired dropped, live keys untouched).
"""
import json
import logging
import time
from pathlib import Path

import vpn_manager as vm
import opencode as oc
from test_vpn_freshness import FakeVPNManager, _cfg


# ── §4.1 save_state: last-good .bak + non-fatal failures ─────────

def test_save_state_first_save_no_bak(tmp_path):
    """Fresh state file: nothing to back up yet → no .bak sidecar."""
    mgr = FakeVPNManager(_cfg(tmp_path), station=1, tmp_path=tmp_path)
    mgr._current_ip = "1.2.3.4"
    mgr.save_state()

    p = Path(mgr._get_state_path())
    bak = Path(str(p) + ".bak")
    assert p.exists(), "state file written"
    assert not bak.exists(), "no previous snapshot → no .bak"

    saved = json.loads(p.read_text())
    assert saved["current_ip"] == "1.2.3.4"


def test_save_state_bak_holds_last_good(tmp_path):
    """Second save copies the PREVIOUS snapshot to .bak before overwriting
    the live file — the .bak is the last-good, the live file the new state."""
    mgr = FakeVPNManager(_cfg(tmp_path), station=1, tmp_path=tmp_path)
    p = Path(mgr._get_state_path())
    bak = Path(str(p) + ".bak")

    mgr._current_ip = "1.2.3.4"
    mgr.save_state()
    mgr._current_ip = "5.6.7.8"
    mgr.save_state()

    assert bak.exists()
    assert json.loads(bak.read_text())["current_ip"] == "1.2.3.4", \
        ".bak holds the previous good snapshot"
    assert json.loads(p.read_text())["current_ip"] == "5.6.7.8", \
        "live file holds the newest state"


def test_save_state_write_failure_preserves_bak(tmp_path, caplog, monkeypatch):
    """A write that fails mid-save (os.replace raises — disk full) after the
    .bak copy must NOT raise out of save_state, must log WITH traceback, and
    must leave both the live file and the .bak intact (last-good)."""
    mgr = FakeVPNManager(_cfg(tmp_path), station=1, tmp_path=tmp_path)
    p = Path(mgr._get_state_path())
    bak = Path(str(p) + ".bak")

    mgr._current_ip = "1.2.3.4"
    mgr.save_state()
    assert json.loads(p.read_text())["current_ip"] == "1.2.3.4"

    # Fail the atomic replace on the NEXT save — after the .bak copy ran.
    def _boom(src, dst):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(vm.os, "replace", _boom)

    with caplog.at_level(logging.DEBUG, logger="vpn_manager"):
        mgr._current_ip = "5.6.7.8"
        mgr.save_state()          # must NOT raise

    # live file untouched (the replace failed) — still the last committed state
    assert json.loads(p.read_text())["current_ip"] == "1.2.3.4"
    # .bak was written before the failing replace → previous good snapshot
    assert json.loads(bak.read_text())["current_ip"] == "1.2.3.4"

    # failure logged WITH traceback (exc_info), not swallowed silently
    rec = next(r for r in caplog.records if r.getMessage().startswith("[vpn] failed to save state"))
    assert rec.exc_info is not None, "traceback attached (exc_info=True)"


# ── §4.2 cooldown sweep: bound the (model, IP) map ───────────────

def test_sweep_free_cooldowns_removes_expired_only(monkeypatch):
    monkeypatch.setattr(oc, "_free_model_cooldowns", {})
    now = time.monotonic()
    oc._free_model_cooldowns["expired|1.2.3.4"] = now - 1       # dead
    oc._free_model_cooldowns["fresh|5.6.7.8"] = now + 3600       # live

    n = oc._sweep_free_cooldowns()

    assert n == 1
    assert "expired|1.2.3.4" not in oc._free_model_cooldowns
    assert "fresh|5.6.7.8" in oc._free_model_cooldowns


def test_set_free_cooldown_soft_limit_sweeps_expired_not_fresh(monkeypatch):
    """The len()>32 guard is a SOFT limit: entering it sweeps expired entries
    (the periodic tick is the real bound) and never drops a live key."""
    monkeypatch.setattr(oc, "_free_model_cooldowns", {})
    now = time.monotonic()
    for i in range(32):                       # 32 expired → map > 32
        oc._free_model_cooldowns[f"exp|{i}"] = now - 10
    oc._free_model_cooldowns["live|key"] = now + 3600
    assert len(oc._free_model_cooldowns) == 33

    oc._set_free_cooldown("m", 60)            # triggers the soft-limit sweep

    assert "live|key" in oc._free_model_cooldowns, "live key survives the sweep"
    assert not [k for k in oc._free_model_cooldowns if k.startswith("exp|")], \
        "expired keys dropped (map ≤ 32 again — soft-limit, no forced eviction)"
    assert oc._free_model_cooldowns["live|key"] > now


def test_sweep_idempotent_empty(monkeypatch):
    monkeypatch.setattr(oc, "_free_model_cooldowns", {})
    assert oc._sweep_free_cooldowns() == 0
    assert oc._free_model_cooldowns == {}
