"""Affinité de conversation sur le sélecteur SOCKS5."""

from __future__ import annotations

from types import SimpleNamespace

from free.pool import FreeIPPool, Socks5Endpoint


def _pool(count: int = 3) -> FreeIPPool:
    pool = object.__new__(FreeIPPool)
    pool._vpn = SimpleNamespace(enabled=True, proxy_mode="socks5")
    pool._stations = []
    pool._socks5_eps = [
        Socks5Endpoint("127.0.0.1", 11080 + i, label=f"warp-{i}", index=i)
        for i in range(count)
    ]
    pool._socks5_rr = 0
    pool._socks5_current = None
    pool._socks5_sticky = None
    pool._socks5_auto_rotate = True
    pool._socks5_affinity = {}
    pool._socks5_affinity_ttl = 3600.0
    pool._per = {}
    pool._free_parallel_enabled = True
    pool._free_parallel_routing = "round-robin"
    pool._free_parallel_mode = "load-balance"
    pool._socks5_usable = lambda endpoint, exclude_approaching=False, ignore_latency_cool=False: True
    return pool


def test_same_conversation_sticks_to_same_station():
    pool = _pool(3)
    first = pool._socks5_pick(conversation_key="conv-a")
    second = pool._socks5_pick(conversation_key="conv-a")
    other = pool._socks5_pick(conversation_key="conv-b")
    assert first is not None and second is not None and other is not None
    assert first is second
    assert other is not first
    assert pool._socks5_affinity["conv-a"][0] == first.pid


def test_affinity_failover_moves_and_updates_pin():
    pool = _pool(3)
    first = pool._socks5_pick(conversation_key="conv-a")
    assert first is not None
    pool._socks5_usable = lambda endpoint, exclude_approaching=False, ignore_latency_cool=False: endpoint is not first
    moved = pool._socks5_pick(conversation_key="conv-a")
    assert moved is not None
    assert moved is not first
    assert pool._socks5_affinity["conv-a"][0] == moved.pid


def test_new_conversations_round_robin():
    pool = _pool(3)
    picks = [pool._socks5_pick(conversation_key=f"conv-{i}") for i in range(3)]
    assert len({id(pick) for pick in picks}) == 3


def test_expired_pin_reuses_healthy_station():
    """Spec : TTL dépassé mais station saine → elle est réutilisée."""
    pool = _pool(2)
    first = pool._socks5_pick(conversation_key="conv-a")
    assert first is not None
    pid, _expires = pool._socks5_affinity["conv-a"]
    pool._socks5_affinity["conv-a"] = (pid, 0.0)
    second = pool._socks5_pick(conversation_key="conv-a")
    assert second is first
    assert pool._socks5_affinity["conv-a"][1] > 0.0  # TTL glissant réarmé


def test_affinity_ttl_expiry_reassigns_only_if_unusable():
    pool = _pool(2)
    first = pool._socks5_pick(conversation_key="conv-a")
    assert first is not None
    pool._socks5_affinity["conv-a"] = (first.pid, 0.0)
    pool._socks5_usable = lambda endpoint, exclude_approaching=False, ignore_latency_cool=False: endpoint is not first
    second = pool._socks5_pick(conversation_key="conv-a")
    assert second is not None
    assert second is not first
    assert pool._socks5_affinity["conv-a"][0] == second.pid


def test_failover_routing_is_sticky_for_new_conversations():
    """GUI routing=failover → pas de round-robin, la station courante reste."""
    pool = _pool(3)
    pool._free_parallel_routing = "failover"
    picks = [pool._socks5_pick(conversation_key=f"conv-{i}") for i in range(4)]
    assert len({id(pick) for pick in picks}) == 1


def test_failover_primary_ignores_pinned_conversation_elsewhere():
    """Une ancienne conversation épinglée ne déplace pas le primaire failover."""
    pool = _pool(3)
    pool._free_parallel_routing = "failover"
    primary = pool._socks5_pick(conversation_key="conv-a")
    assert primary is not None
    other = next(ep for ep in pool._socks5_eps if ep is not primary)
    pool._socks5_current = other  # ce que ferait un hit d'affinité ancien
    fresh = pool._socks5_pick(conversation_key="conv-b")
    assert fresh is primary


def test_free_parallel_disabled_is_sticky():
    pool = _pool(3)
    pool._free_parallel_enabled = False
    picks = [pool._socks5_pick(conversation_key=f"conv-{i}") for i in range(4)]
    assert len({id(pick) for pick in picks}) == 1


def test_socks5_auto_rotate_off_is_sticky_even_with_round_robin():
    pool = _pool(3)
    pool._socks5_auto_rotate = False
    picks = [pool._socks5_pick(conversation_key=f"conv-{i}") for i in range(4)]
    assert len({id(pick) for pick in picks}) == 1


def test_load_balance_picks_least_loaded():
    pool = _pool(2)
    first = pool._socks5_pick(conversation_key="conv-a")
    assert first is not None
    pool._per[first._station]["request_count"] = 10
    second = pool._socks5_pick(conversation_key="conv-b")
    assert second is not None
    assert second is not first


def test_strict_mode_uses_strict_counter():
    pool = _pool(3)
    pool._free_parallel_mode = "strict"
    picks = [pool._socks5_pick(conversation_key=f"conv-{i}") for i in range(3)]
    assert len({id(pick) for pick in picks}) == 3


def test_latency_hard_cooled_fleet_still_yields_candidate():
    """GUI latency_rotation : flotte hard-cooled → dernier passage, pas zéro."""

    pool = _pool(2)

    def _usable(endpoint, exclude_approaching=False, ignore_latency_cool=False):
        return bool(ignore_latency_cool)

    pool._socks5_usable = _usable
    picked = pool._socks5_pick(conversation_key="conv-a")
    assert picked is not None


def test_pick_candidates_exposes_socks5_for_gui_hedge():
    pool = _pool(3)
    cands = pool.pick_candidates()
    assert len(cands) == 3
    assert all(isinstance(cand, Socks5Endpoint) for cand in cands)


def test_pick_candidates_disabled_returns_single_endpoint():
    pool = _pool(3)
    pool._free_parallel_enabled = False
    cands = pool.pick_candidates()
    assert len(cands) == 1


def test_pick_candidates_failover_puts_current_first():
    pool = _pool(3)
    pool._free_parallel_routing = "failover"
    current = pool._socks5_pick(conversation_key="conv-a")
    assert current is not None
    cands = pool.pick_candidates()
    assert cands[0] is current


def test_pick_candidates_hedge_least_loaded_first():
    pool = _pool(3)
    pool._free_parallel_mode = "hedge"
    first = pool.pick_candidates()[0]
    pool._per[first._station]["request_count"] = 10
    assert pool.pick_candidates()[0] is not first


def test_pin_socks5_conversation_pins_hedge_winner():
    pool = _pool(3)
    winner = pool._socks5_eps[1]
    pool.pin_socks5_conversation("conv-hedge", winner)
    assert pool._socks5_pick(conversation_key="conv-hedge") is winner
    assert pool._socks5_affinity["conv-hedge"][0] == winner.pid


def test_pin_socks5_conversation_ignores_empty_key():
    pool = _pool(2)
    pool.pin_socks5_conversation("", pool._socks5_eps[0])
    assert pool._socks5_affinity == {}
