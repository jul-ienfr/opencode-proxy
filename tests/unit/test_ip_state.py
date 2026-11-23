import pytest

from free import ip_state


def _store(tmp_path):
    return ip_state.IPStateStore(tmp_path / "registry.db")


def test_proven_requires_wire_fp(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.transition("nordvpn", "8.8.8.8", ip_state.PROVEN)


def test_proven_is_selectable_only_with_matching_fp(tmp_path):
    store = _store(tmp_path)
    st = store.transition("nordvpn", "8.8.8.8", ip_state.PROVEN, wire_fp="fp1")
    assert st.state == ip_state.PROVEN
    assert st.is_selectable(wire_fp="fp1")
    assert not st.is_selectable(wire_fp="fp2")


def test_spent_requires_reset_and_is_not_selectable(tmp_path):
    store = _store(tmp_path)
    store.transition("nordvpn", "8.8.8.8", ip_state.PROVEN, wire_fp="fp1")
    with pytest.raises(ValueError):
        store.transition("nordvpn", "8.8.8.8", ip_state.SPENT)
    st = store.transition(
        "nordvpn", "8.8.8.8", ip_state.SPENT, reset_at=1000.0, reason="quota"
    )
    assert st.state == ip_state.SPENT
    assert not st.is_selectable(now=2000.0)
    assert st.reset_elapsed(now=1000.0)


def test_burned_requires_24h_floor(tmp_path):
    store = _store(tmp_path)
    st = store.transition(
        "nordvpn", "8.8.8.8", ip_state.BURNED, wire_fp="bad", now=1000.0
    )
    assert st.burned_at == 1000.0
    assert not st.reset_elapsed(now=1000.0 + ip_state.BURN_SECONDS - 1)
    assert st.reset_elapsed(now=1000.0 + ip_state.BURN_SECONDS)


def test_terminal_state_can_only_return_to_unproven(tmp_path):
    store = _store(tmp_path)
    store.transition("nordvpn", "8.8.8.8", ip_state.PROVEN, wire_fp="fp1")
    store.transition("nordvpn", "8.8.8.8", ip_state.SPENT, reset_at=1000.0)
    with pytest.raises(ValueError):
        store.transition("nordvpn", "8.8.8.8", ip_state.PROVEN, wire_fp="fp1")
    st = store.transition("nordvpn", "8.8.8.8", ip_state.UNPROVEN)
    assert st.state == ip_state.UNPROVEN


def test_state_persists_across_store_instances(tmp_path):
    path = tmp_path / "registry.db"
    ip_state.IPStateStore(path).transition(
        "warp", "2a09:bac1:27c0:2060::1", ip_state.PROVEN, wire_fp="fp"
    )
    reloaded = ip_state.IPStateStore(path).get("warp", "2a09:bac1:27c0:2060::1")
    assert reloaded is not None and reloaded.state == ip_state.PROVEN


def test_counts_and_events(tmp_path):
    store = _store(tmp_path)
    store.transition("nordvpn", "8.8.8.8", ip_state.PROVEN, wire_fp="fp")
    counts = store.counts("nordvpn")
    assert counts[ip_state.PROVEN] == 1
    event_id = store.record_event(
        "429_format", provider="nordvpn", ip="8.8.8.8", wire_fp="bad", reason="burn"
    )
    assert event_id > 0
    events = store.events(limit=10)
    assert events and events[0]["kind"] == "429_format"


def test_false_format_burns_are_repaired_on_load(tmp_path):
    path = tmp_path / "registry.db"
    store = ip_state.IPStateStore(path)
    store.transition(
        "nordvpn",
        "8.8.8.8",
        ip_state.BURNED,
        reason="unclassified 429 on an unproven IP",
    )
    reloaded = ip_state.IPStateStore(path)
    state = reloaded.get("nordvpn", "8.8.8.8")
    assert state.state == ip_state.UNPROVEN
    assert state.burned_at is None
    assert "reclassified" in state.reason


def test_explicit_format_burn_is_not_repaired(tmp_path):
    path = tmp_path / "registry.db"
    store = ip_state.IPStateStore(path)
    store.transition(
        "nordvpn",
        "1.1.1.1",
        ip_state.BURNED,
        reason="explicit free-tier form rejection",
    )
    reloaded = ip_state.IPStateStore(path)
    assert reloaded.get("nordvpn", "1.1.1.1").state == ip_state.BURNED


def test_revive_if_elapsed_moves_burned_to_unproven_after_24h(tmp_path):
    store = _store(tmp_path)
    store.transition("nordvpn", "8.8.8.8", ip_state.BURNED, now=1000.0)
    assert store.get("nordvpn", "8.8.8.8").state == ip_state.BURNED
    revived = store.revive_if_elapsed(
        "nordvpn", "8.8.8.8", now=1000.0 + ip_state.BURN_SECONDS
    )
    assert revived is not None and revived.state == ip_state.UNPROVEN
    # Re-preuve désormais autorisée (terminal -> unproven -> proven).
    st = store.transition("nordvpn", "8.8.8.8", ip_state.PROVEN, wire_fp="fp")
    assert st.state == ip_state.PROVEN


def test_revive_if_elapsed_keeps_burned_before_floor(tmp_path):
    store = _store(tmp_path)
    store.transition("nordvpn", "8.8.8.8", ip_state.BURNED, now=1000.0)
    still = store.revive_if_elapsed("nordvpn", "8.8.8.8", now=1000.0 + 60)
    assert still is not None and still.state == ip_state.BURNED


def test_revive_if_elapsed_moves_spent_to_unproven_after_reset(tmp_path):
    store = _store(tmp_path)
    store.transition("nordvpn", "8.8.8.8", ip_state.SPENT, reset_at=2000.0)
    assert (
        store.revive_if_elapsed("nordvpn", "8.8.8.8", now=1999.0).state
        == ip_state.SPENT
    )
    assert (
        store.revive_if_elapsed("nordvpn", "8.8.8.8", now=2000.0).state
        == ip_state.UNPROVEN
    )


def test_revive_if_elapsed_returns_none_for_unknown(tmp_path):
    assert _store(tmp_path).revive_if_elapsed("nordvpn", "9.9.9.9") is None


def test_reburn_after_revive_restarts_the_24h_floor(tmp_path):
    """[fix 2026-11-21] Un re-burn heritait du `burned_at` precedent : le
    nouveau burn etait revivable immediatement (protection 24 h annulee)."""
    store = _store(tmp_path)
    store.transition("nordvpn", "8.8.8.8", ip_state.BURNED, now=1000.0)
    store.revive_if_elapsed(
        "nordvpn", "8.8.8.8", now=1000.0 + ip_state.BURN_SECONDS
    )
    reburn_at = 1000.0 + ip_state.BURN_SECONDS + 5
    st = store.transition("nordvpn", "8.8.8.8", ip_state.BURNED, now=reburn_at)
    assert st.burned_at == reburn_at
    assert not st.reset_elapsed(now=reburn_at + ip_state.BURN_SECONDS - 1)
    assert st.reset_elapsed(now=reburn_at + ip_state.BURN_SECONDS)


def test_reprove_refreshes_proven_at(tmp_path):
    """[fix 2026-11-21] Une preuve fraiche doit rafraichir `proven_at` ;
    sinon la fenetre OPENCODE_PROOF_MAX_AGE_S expire et force des sondes."""
    store = _store(tmp_path)
    store.transition("nordvpn", "8.8.8.8", ip_state.PROVEN, wire_fp="fp", now=1000.0)
    st = store.transition(
        "nordvpn", "8.8.8.8", ip_state.PROVEN, wire_fp="fp", now=2000.0
    )
    assert st.proven_at == 2000.0
