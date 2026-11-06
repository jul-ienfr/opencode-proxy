"""Rotation automatique WARP — logique pure et endpoint.

Aucun docker réel : `_docker`, `_wait_healthy` et `_resolve_egress` sont
monkeypatchés.
"""

from __future__ import annotations

import time

from free.pool import Socks5Endpoint
from free.warp_rotation import WarpRotator, block_for_ip


class FakePool:
    def __init__(self) -> None:
        self.state = {}

    def _per_station(self, endpoint):
        return self.state.setdefault(endpoint._station, {"bad_until": 123.0, "consec_429": 3})


def _endpoint(**kwargs) -> Socks5Endpoint:
    defaults = {
        "host": "127.0.0.1",
        "port": 11103,
        "label": "warp-restore1",
        "ipv6": True,
        "egress_ip": "2a09:bac1:27a0:2060::214:7c",
        "managed": True,
        "container": "warp-restore1",
        "image": "caomingjun/warp:latest",
        "block": "2a09:bac1:27a0:2060::/64",
    }
    defaults.update(kwargs)
    return Socks5Endpoint(**defaults)


def test_docker_commands_suppress_console_window(monkeypatch):
    from types import SimpleNamespace

    import free.warp_rotation as wr

    calls = []
    monkeypatch.setattr(
        wr.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(kwargs)
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    WarpRotator()._docker(["ps"])
    assert calls and "creationflags" in calls[0]
    if wr.sys.platform == "win32":
        assert calls[0]["creationflags"] == wr.CREATE_NO_WINDOW == 0x08000000


def test_block_for_ip_uses_slash_64_and_slash_32():
    assert block_for_ip("2a09:bac1:27a0:2060::214:7c") == "2a09:bac1:27a0:2060::/64"
    assert block_for_ip("1.2.3.4") == "1.2.3.4/32"
    assert block_for_ip("not-an-ip") is None


def test_endpoint_carries_managed_fields():
    ep = _endpoint()
    assert ep.managed is True
    assert ep.container == "warp-restore1"
    assert ep.image == "caomingjun/warp:latest"
    assert ep.block == "2a09:bac1:27a0:2060::/64"


def test_rotate_restart_then_new_block(monkeypatch):
    rotator = WarpRotator()
    calls: list[list[str]] = []
    monkeypatch.setattr(rotator, "_docker", lambda args, timeout=60: calls.append(args) or "")
    monkeypatch.setattr(rotator, "_wait_healthy", lambda container: True)
    monkeypatch.setattr(rotator, "_resolve_egress", lambda port, ipv6: "2a09:bac1:2780:2060::216:7f")
    result = rotator.rotate(_endpoint())
    assert result.ok is True
    assert result.new_ip == "2a09:bac1:2780:2060::216:7f"
    assert result.block == "2a09:bac1:2780:2060::/64"
    assert calls[0] == ["restart", "warp-restore1"]


def test_rotate_recreates_when_block_is_same(monkeypatch):
    rotator = WarpRotator()
    calls: list[list[str]] = []
    monkeypatch.setattr(rotator, "_docker", lambda args, timeout=60: calls.append(args) or "")
    monkeypatch.setattr(rotator, "_wait_healthy", lambda container: True)
    responses = iter(["2a09:bac1:27a0:2060::214:7d", "2a09:bac1:27c0:2060::10:1"])
    monkeypatch.setattr(rotator, "_resolve_egress", lambda port, ipv6: next(responses))
    result = rotator.rotate(_endpoint())
    assert result.ok is True
    assert result.attempts == 2
    assert any(call[:2] == ["rm", "-f"] for call in calls)
    assert any(call[:2] == ["run", "-d"] for call in calls)


def test_rotate_retries_until_a_fresh_block(monkeypatch):
    rotator = WarpRotator()
    calls: list[list[str]] = []
    monkeypatch.setattr(rotator, "_docker", lambda args, timeout=60: calls.append(args) or "")
    monkeypatch.setattr(rotator, "_wait_healthy", lambda container: True)
    responses = iter(
        [
            "2a09:bac1:27a0:2060::214:7d",  # same block
            "2a09:bac1:27e0:2060::10:1",  # forbidden (ledger exhausted)
            "2a09:bac1:2780:2060::216:7f",  # fresh
        ]
    )
    monkeypatch.setattr(rotator, "_resolve_egress", lambda port, ipv6: next(responses))
    result = rotator.rotate(
        _endpoint(),
        forbidden_blocks={"2a09:bac1:27e0:2060::/64"},
    )
    assert result.ok is True
    assert result.attempts == 3
    assert result.block == "2a09:bac1:2780:2060::/64"
    assert sum(1 for call in calls if call[:2] == ["rm", "-f"]) == 2


def test_rotate_failure_keeps_last_resolved_egress(monkeypatch):
    rotator = WarpRotator()
    monkeypatch.setattr(rotator, "_docker", lambda args, timeout=60: "")
    monkeypatch.setattr(rotator, "_wait_healthy", lambda container: True)
    monkeypatch.setattr(
        rotator,
        "_resolve_egress",
        lambda port, ipv6: "2a09:bac1:27a0:2060::214:7d",
    )
    result = rotator.rotate(_endpoint())
    assert result.ok is False
    assert result.reason == "same_block"
    assert result.new_ip == "2a09:bac1:27a0:2060::214:7d"
    assert result.block == "2a09:bac1:27a0:2060::/64"


def test_apply_tracking_updates_rejected_egress(monkeypatch):
    rotator = WarpRotator()
    ep = _endpoint()
    pool = FakePool()
    persisted = []
    from free.warp_rotation import WarpRotationResult

    result = WarpRotationResult(
        label="warp-restore1",
        ok=False,
        old_ip=ep.egress_ip,
        new_ip="2a09:bac1:27a0:2060::214:7e",
        block="2a09:bac1:27a0:2060::/64",
        attempts=4,
        reason="same_block",
    )
    rotator._apply_tracking(
        ep,
        pool,
        lambda label, ip, block: persisted.append((label, ip, block)),
        result,
    )
    assert ep.egress_ip == "2a09:bac1:27a0:2060::214:7e"
    assert ep.current_ip == "2a09:bac1:27a0:2060::214:7e"
    assert pool.state[ep._station]["bad_cause"] == "rotation_rejected"
    assert pool.state[ep._station]["bad_until"] > time.monotonic()
    assert persisted == [("warp-restore1", "2a09:bac1:27a0:2060::214:7e", "2a09:bac1:27a0:2060::/64")]


def test_run_forbids_ledger_exhausted_blocks(monkeypatch):
    rotator = WarpRotator()
    captured = {}

    def fake_rotate(endpoint, *, forbidden_blocks=None, probe=None):
        captured["forbidden"] = set(forbidden_blocks or ())
        from free.warp_rotation import WarpRotationResult

        return WarpRotationResult(
            label=endpoint.label,
            ok=False,
            old_ip=endpoint.egress_ip,
            new_ip=None,
            block=None,
            attempts=1,
            reason="health_timeout",
        )

    class Ledger:
        def exhausted_ips(self):
            return {"2a09:bac1:27e0:2060", "104.28.198.203"}

    class Pool(FakePool):
        def _socks5_enabled_eps(self):
            return []

        def _get_ledger(self):
            return Ledger()

    monkeypatch.setattr(rotator, "rotate", fake_rotate)
    rotator._run(_endpoint(), Pool(), lambda *args: None)
    assert "2a09:bac1:27e0:2060::/64" in captured["forbidden"]
    assert "104.28.198.203/32" in captured["forbidden"]


def test_rotate_fails_cleanly(monkeypatch):
    rotator = WarpRotator()
    monkeypatch.setattr(rotator, "_docker", lambda args, timeout=60: "")
    monkeypatch.setattr(rotator, "_wait_healthy", lambda container: False)
    result = rotator.rotate(_endpoint())
    assert result.ok is False
    assert result.reason == "health_timeout"


def test_request_rotation_ignores_unmanaged():
    rotator = WarpRotator()
    ep = _endpoint(managed=False)
    assert rotator.request_rotation(ep, FakePool(), lambda *args: None) is False


def test_request_rotation_starts_managed_and_respects_cooldown(monkeypatch):
    import free.warp_rotation as wr

    monkeypatch.setattr(wr, "MIN_ROTATION_INTERVAL_S", 300.0)
    monkeypatch.setattr(wr, "INFLIGHT", set())
    monkeypatch.setattr(wr, "LAST_ROTATION", {})
    rotator = WarpRotator()
    monkeypatch.setattr(rotator, "_run", lambda *args: None)
    ep = _endpoint(label="warp-test-cooldown", container="warp-restore1")
    assert rotator.request_rotation(ep, FakePool(), lambda *args: None) is True
    assert rotator.request_rotation(ep, FakePool(), lambda *args: None) is False
    wr.LAST_ROTATION["warp-test-cooldown"] = time.monotonic()
    assert rotator.request_rotation(ep, FakePool(), lambda *args: None) is False


class ProactivePool:
    socks5_mode = True

    class _Vpn:
        proxy_mode = "socks5"
        enabled = True

    def __init__(self, endpoints):
        self._socks5_eps = endpoints
        self._vpn = self._Vpn()
        self.calls = []

    def _socks5_enabled_eps(self):
        return list(self._socks5_eps)

    def _get_ledger(self):
        class _Ledger:
            def is_exhausted(self, raw, model):
                return True

        return _Ledger()

    def _ledger_ip(self, endpoint):
        return endpoint.egress_ip or ""

    def _ledger_usable(self, model, endpoint):
        return False, "daily-bucket-exhausted"

    def _per_station(self, endpoint):
        return {}

    def _request_warp_rotation(self, endpoint, cause):
        self.calls.append((endpoint.label, cause))
        return True


def test_rotate_exhausted_managed_starts_managed_only():
    from free.pool import FreeIPPool

    managed = _endpoint(label="warp-restore1", container="warp-restore1")
    manual = _endpoint(label="warp-test", container=None, managed=False)
    pool = ProactivePool([managed, manual])
    count = FreeIPPool.rotate_exhausted_managed(pool)
    assert count == 1
    assert pool.calls == [("warp-restore1", "exhausted")]


def test_rotate_exhausted_managed_rotates_model_blocked_endpoint():
    from free.pool import FreeIPPool

    class ModelBadPool(ProactivePool):
        _model_ip_health = {("mimo-v2.6-flash-free", -1, "ip"): {}}

        def _get_ledger(self):
            class _Ledger:
                def is_exhausted(self, raw, model):
                    return False

            return _Ledger()

        def model_ip_bad(self, model, endpoint):
            return endpoint.label == "warp-restore1"

    managed = _endpoint(label="warp-restore1", container="warp-restore1")
    healthy = _endpoint(
        label="warp-restore3",
        container="warp-restore3",
        index=1,
        block="2a09:bac1:2780:2060::/64",
    )
    pool = ModelBadPool([managed, healthy])
    count = FreeIPPool.rotate_exhausted_managed(pool)
    assert count == 1
    assert pool.calls == [("warp-restore1", "model_ip")]


def test_rotate_exhausted_managed_retries_rejected_block():
    from free.pool import FreeIPPool

    class RejectedPool(ProactivePool):
        def _get_ledger(self):
            class _Ledger:
                def is_exhausted(self, raw, model):
                    return False

            return _Ledger()

        def _per_station(self, endpoint):
            return {"bad_cause": "rotation_rejected"}

    managed = _endpoint(label="warp-restore1", container="warp-restore1")
    pool = RejectedPool([managed])
    count = FreeIPPool.rotate_exhausted_managed(pool)
    assert count == 1
    assert pool.calls == [("warp-restore1", "exhausted")]


def test_rotate_exhausted_managed_rotates_trial_dead_endpoint():
    from free.pool import FreeIPPool

    class TrialDeadPool(ProactivePool):
        def _get_ledger(self):
            class _Ledger:
                def is_exhausted(self, raw, model):
                    return False

            return _Ledger()

    managed = _endpoint(label="warp-restore1", container="warp-restore1")
    healthy = _endpoint(
        label="warp-restore3",
        container="warp-restore3",
        index=1,
        block="2a09:bac1:2780:2060::/64",
    )
    pool = TrialDeadPool([managed, healthy])
    pool._trial_dead_labels_cb = lambda: {"warp-restore1"}
    count = FreeIPPool.rotate_exhausted_managed(pool)
    assert count == 1
    assert pool.calls == [("warp-restore1", "trial_dead")]


def test_rotate_exhausted_managed_rotates_duplicate_block():
    from free.pool import FreeIPPool

    class DuplicatePool(ProactivePool):
        def _get_ledger(self):
            class _Ledger:
                def is_exhausted(self, raw, model):
                    return False

            return _Ledger()

    shared_block = "2a09:bac1:27a0:2060::/64"
    first = _endpoint(label="warp-restore1", container="warp-restore1", block=shared_block)
    second = _endpoint(
        label="warp-restore3",
        container="warp-restore3",
        index=1,
        block=shared_block,
    )
    pool = DuplicatePool([first, second])
    count = FreeIPPool.rotate_exhausted_managed(pool)
    assert count == 1
    assert pool.calls == [("warp-restore3", "duplicate_block")]


def test_rotate_invokes_probe_with_candidate_egress(monkeypatch):
    rotator = WarpRotator()
    monkeypatch.setattr(rotator, "_docker", lambda args, timeout=60: "")
    monkeypatch.setattr(rotator, "_wait_healthy", lambda container: True)
    monkeypatch.setattr(
        rotator,
        "_resolve_egress",
        lambda port, ipv6: "2a09:bac1:2780:2060::216:7f",
    )
    probed = []

    def probe(endpoint):
        probed.append(endpoint.current_ip)
        return True

    result = rotator.rotate(_endpoint(), probe=probe)
    assert result.ok is True
    assert probed == ["2a09:bac1:2780:2060::216:7f"]


def test_rotate_retries_when_quota_probe_rejects(monkeypatch):
    rotator = WarpRotator()
    calls: list[list[str]] = []
    monkeypatch.setattr(rotator, "_docker", lambda args, timeout=60: calls.append(args) or "")
    monkeypatch.setattr(rotator, "_wait_healthy", lambda container: True)
    responses = iter(
        [
            "2a09:bac1:2780:2060::216:7f",
            "2a09:bac1:27e0:2060::10:2",
        ]
    )
    monkeypatch.setattr(rotator, "_resolve_egress", lambda port, ipv6: next(responses))
    verdicts = iter([False, True])

    result = rotator.rotate(_endpoint(), probe=lambda endpoint: next(verdicts))
    assert result.ok is True
    assert result.attempts == 2
    assert result.new_ip == "2a09:bac1:27e0:2060::10:2"
    assert sum(1 for call in calls if call[:2] == ["rm", "-f"]) == 1


def test_rotate_fails_after_probe_rejections(monkeypatch):
    rotator = WarpRotator()
    monkeypatch.setattr(rotator, "_docker", lambda args, timeout=60: "")
    monkeypatch.setattr(rotator, "_wait_healthy", lambda container: True)
    monkeypatch.setattr(
        rotator,
        "_resolve_egress",
        lambda port, ipv6: "2a09:bac1:2780:2060::216:7f",
    )
    result = rotator.rotate(_endpoint(), probe=lambda endpoint: False)
    assert result.ok is False
    assert result.reason == "quota_exhausted"
    assert result.new_ip == "2a09:bac1:2780:2060::216:7f"


def test_disabled_post_rotation_probe_is_noop(monkeypatch):
    import opencode as oc

    monkeypatch.setitem(oc.IP_ROTATION, "post_rotation_probe", {"enabled": False})
    assert oc._free_post_rotation_probe(None) is True


def test_post_rotation_probe_accepts_quota_as_egress_ok():
    import opencode as oc

    assert oc._post_rotation_probe_ok(200, "pass") is True
    assert oc._post_rotation_probe_ok(429, "quota") is True
    assert oc._post_rotation_probe_ok(403, "reject") is False
    assert oc._post_rotation_probe_ok(0, "") is False


def test_apply_updates_endpoint_and_pool():
    rotator = WarpRotator()
    ep = _endpoint()
    pool = FakePool()
    persisted = []
    from free.warp_rotation import WarpRotationResult

    result = WarpRotationResult(
        label="warp-restore1",
        ok=True,
        old_ip=ep.egress_ip,
        new_ip="2a09:bac1:2780:2060::216:7f",
        block="2a09:bac1:2780:2060::/64",
        attempts=1,
        reason="rotated",
    )
    rotator._apply(ep, pool, lambda label, ip, block: persisted.append((label, ip, block)), result)
    assert ep.egress_ip == "2a09:bac1:2780:2060::216:7f"
    assert ep.current_ip == "2a09:bac1:2780:2060::216:7f"
    assert ep.block == "2a09:bac1:2780:2060::/64"
    assert pool.state[ep._station]["bad_until"] == 0.0
    assert persisted == [("warp-restore1", "2a09:bac1:2780:2060::216:7f", "2a09:bac1:2780:2060::/64")]
