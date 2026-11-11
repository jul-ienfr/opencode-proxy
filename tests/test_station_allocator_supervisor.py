"""Offline contracts for the all-station repair supervisor."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _module():
    path = ROOT / "scripts" / "fresh_chain_supervisor.py"
    spec = importlib.util.spec_from_file_location("fresh_chain_supervisor", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_effective_max_parallel_drops_to_one_after_auth_failure():
    """[P0 2026-11-11] Pause AUTH → une probe ; sinon le parallélisme revient."""
    module = _module()
    assert module._effective_max_parallel(
        {1: {"vpn": {"auth_fail_30min": 2}}}, 2
    ) == 2
    assert module._effective_max_parallel(
        {1: {"vpn": {"auth_fail_30min": 0}}}, 2
    ) == 2
    assert module._effective_max_parallel({}, 3) == 3
    # Pendant une pause AUTH, une seule probe est autorisée (comme le manager).
    assert module._effective_max_parallel({}, 2, auth_pause=366.0) == 1
    assert module._auth_recent({1: {"vpn": {"auth_fail_30min": 1}}}) is True
    assert module._auth_recent({1: {"vpn": {"auth_fail_30min": 0}}}) is False


def test_auth_pause_blocks_repairs():
    """[P0 2026-11-10] Une pause AUTH globale gèle les nouvelles chaînes."""
    module = _module()
    assert module._auth_pause_remaining(
        {1: {"vpn": {"auth_global_pause_remaining_s": 233.6}}}
    ) == 233.6
    assert module._auth_pause_remaining(
        {1: {"vpn": {}}, 2: {"vpn": {"auth_global_pause_remaining_s": 12}}}
    ) == 12.0
    assert module._auth_pause_remaining({1: {"vpn": {}}}) == 0.0
    assert module._auth_pause_remaining({}) == 0.0


def test_repair_holds_preserves_the_serving_reserve():
    """[P0 2026-11-10] Un refresh ne doit pas laisser la flotte à 0/6."""
    module = _module()
    # Refresh : garder >=3 stations utilisables.
    assert module._repair_holds(6, 0, refresh=True) is False
    assert module._repair_holds(6, 2, refresh=True) is False
    assert module._repair_holds(6, 3, refresh=True) is True
    assert module._repair_holds(4, 1, refresh=True) is True
    # Réparation d'une station déjà down : toujours autorisée, même si une
    # chaîne tourne ailleurs (elle ne consomme aucune réserve).
    assert module._repair_holds(1, 0, refresh=False) is False
    assert module._repair_holds(1, 1, refresh=False) is False
    # Un refresh de la dernière station serving reste retenu.
    assert module._repair_holds(1, 1, refresh=True) is True
    # Flotte déjà à zéro : toutes les réparations sont autorisées.
    assert module._repair_holds(0, 0, refresh=False) is False
    assert module._repair_holds(0, 2, refresh=True) is False


def test_active_serving_count_ignores_chains_on_down_stations():
    """[P0 2026-11-11] Une chaîne sur une station down ne consomme pas la réserve."""
    module = _module()
    stations = {
        1: {"model_usable": True},
        2: {"model_usable": False},
        3: {"model_usable": True},
    }
    running = {1: True, 2: True, 3: False}
    assert module._active_serving_count(stations, running, [1, 2, 3]) == 1
    assert module._active_serving_count(stations, {2: True}, [1, 2, 3]) == 0


def test_chain_expiry_releases_a_stuck_repair_slot(tmp_path):
    """[P0 2026-11-11] Une chaîne >20 min rend son slot de réparation."""
    import os

    module = _module()
    lock = tmp_path / "fresh_chain_1_auto.lock"
    lock.write_text('{"pid": 123}', encoding="utf-8")
    now = 10_000_000.0
    # Une chaîne fraîche n'expire pas.
    assert module._chain_expired(lock, now=now) is False
    # Juste sous le budget : encore vivante.
    os.utime(lock, (now - module.MAX_CHAIN_AGE_S + 1,) * 2)
    assert module._chain_expired(lock, now=now) is False
    # Au-delà du budget : le slot est libéré.
    os.utime(lock, (now - module.MAX_CHAIN_AGE_S,) * 2)
    assert module._chain_expired(lock, now=now) is True
    # Un lock disparu n'est jamais « expiré ».
    lock.unlink()
    assert module._chain_expired(lock, now=now) is False


def test_healthy_station_never_gets_a_chain():
    module = _module()
    assert module._needs_chain(
        {"station_usable": True, "model_usable": True, "current_ip": "1.2.3.4"}
    ) is False


def test_repair_request_for_a_previous_ip_is_stale():
    """[P0 2026-11-10] Après rotation, la demande de l'ancienne IP est obsolète."""
    module = _module()
    assert module._repair_request_is_stale(
        {"ip": "87.120.102.58"}, {"current_ip": "186.247.44.119"}
    ) is True
    assert module._repair_request_is_stale(
        {"ip": "87.120.102.58"}, {"current_ip": "87.120.102.58"}
    ) is False
    # IP inconnue (bind en cours) : on ne jette pas la demande.
    assert module._repair_request_is_stale({"ip": "1.2.3.4"}, {}) is False
    assert module._repair_request_is_stale({}, {"current_ip": "1.2.3.4"}) is False


def test_repair_request_is_dropped_when_the_model_is_already_usable():
    """[P0 2026-11-10] Une demande à IP vide ne re-rotate pas une station saine."""
    module = _module()
    station = {
        "station_usable": True,
        "model_usable": True,
        "model_usable_by_model": {
            "deepseek-v4.1-flash-free": {"usable": True},
            "glm-5.3-flash-free": {"usable": False},
        },
    }
    assert module._repair_request_satisfied(
        {"model": "deepseek-v4.1-flash-free", "ip": ""}, station
    ) is True
    # Un autre modèle en 429 n'est pas masqué par le verdict global.
    assert module._repair_request_satisfied(
        {"model": "glm-5.3-flash-free", "ip": ""}, station
    ) is False
    assert module._repair_request_satisfied({}, {}) is False


def test_allocator_event_is_jsonl_and_correlated(tmp_path, monkeypatch):
    import json

    module = _module()
    path = tmp_path / "allocator_events.jsonl"
    monkeypatch.setattr(module, "EVENTS_LOG", path)
    module.event("chain_started", station=2, pid=1234)
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["event"] == "chain_started"
    assert row["station"] == 2
    assert row["pid"] == 1234
    assert row["at"].endswith("Z")


def test_exhausted_bucket_rotates_but_transient_cooldown_does_not():
    module = _module()
    # [2026-11-03] The client model is the only probe bucket. A daily bucket
    # exhausted on the current IP is dead until the UTC reset, so protecting
    # the tunnel left the fleet at 2/6; the supervisor must rotate it.
    assert module._needs_chain(
        {
            "station_usable": True,
            "model_usable": False,
            "model_usable_reason": "daily-bucket-exhausted",
            "current_ip": "1.2.3.4",
            "non_routable_reason": "quota=draining",
        }
    ) is True
    assert module._needs_chain(
        {"station_usable": False, "vpn_status": "error", "current_ip": "1.2.3.4"}
    )
    assert module._needs_chain(
        {"station_usable": True, "model_usable": True, "current_ip": ""}
    )


def test_any_unusable_station_gets_a_fresh_ip():
    module = _module()
    # [2026-11-03] Operator policy: if the station cannot serve the client
    # model, its IP must be changed, whatever the reason.
    assert (
        module._needs_chain(
            {
                "station_usable": True,
                "model_usable": False,
                "model_usable_reason": "model-cooldown",
                "current_ip": "1.2.3.4",
            }
        )
        is True
    )


def test_exhaustion_pause_requires_sustained_zero_usable_polls():
    module = _module()
    assert module._exhaustion_due(2, 100, 40) is False
    assert module._exhaustion_due(3, 39, 40) is False
    assert module._exhaustion_due(3, 40, 40) is True
    assert module._exhaustion_due(10, 10**6, 40) is True


def test_scope_honours_station_count_and_operator_disabled(monkeypatch):
    module = _module()
    monkeypatch.setattr(
        module,
        "_live_ip_rotation",
        lambda: {"station_count": 4, "disabled_stations": [2]},
    )
    assert module._scope() == [1, 3, 4]


def test_nordvpn_selected_reads_live_config(monkeypatch):
    module = _module()
    monkeypatch.setattr(
        module,
        "_live_ip_rotation",
        lambda: {"egress_providers": ["warp"]},
    )
    assert module._nordvpn_selected() is False
    monkeypatch.setattr(
        module,
        "_live_ip_rotation",
        lambda: {"egress_providers": ["warp", "nordvpn"]},
    )
    assert module._nordvpn_selected() is True
    monkeypatch.setattr(
        module,
        "_live_ip_rotation",
        lambda: {"proxy_mode": "vpn"},
    )
    assert module._nordvpn_selected() is True


def test_manager_repair_enabled_reads_live_config(monkeypatch):
    module = _module()
    monkeypatch.setattr(
        module,
        "_live_ip_rotation",
        lambda: {"station_repair": {"enabled": False}},
    )
    assert module._manager_repair_enabled() is False
    monkeypatch.setattr(
        module,
        "_live_ip_rotation",
        lambda: {"station_repair": {"enabled": True}},
    )
    assert module._manager_repair_enabled() is True


def test_need_rank_prefers_tunnel_up_quota_station():
    module = _module()
    quota = {"station_usable": True, "model_usable": False, "vpn_status": "connected"}
    connected = {"station_usable": False, "model_usable": False, "vpn_status": "connected"}
    down = {"station_usable": False, "model_usable": False, "vpn_status": "error"}
    assert module._need_rank(quota) < module._need_rank(connected) < module._need_rank(down)


def test_manager_owned_repair_blocks_allocator(monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_manager_repair_enabled", lambda: True)
    station = {
        "current_ip": "1.2.3.4",
        "station_usable": False,
        "model_usable": False,
        "vpn_status": "error",
        "health": {"state": "repairing", "repairing_s": 30},
    }
    assert module._needs_chain(station) is False
    station["health"] = {"state": "repairing", "repairing_s": 601}
    assert module._needs_chain(station) is True
    station["health"] = {"state": "parked", "parked_remaining_s": 100}
    assert module._needs_chain(station) is False


def test_allocator_only_owner_repairs_after_short_grace(monkeypatch):
    """With the watchdog repair disabled, waiting 10 min leaves the station dead."""

    module = _module()
    monkeypatch.setattr(module, "_manager_repair_enabled", lambda: False)
    station = {
        "current_ip": "1.2.3.4",
        "station_usable": False,
        "model_usable": False,
        "vpn_status": "connected",
        "health": {"state": "repairing", "repairing_s": 10},
    }
    assert module._needs_chain(station) is False
    station["health"] = {"state": "repairing", "repairing_s": 31}
    assert module._needs_chain(station) is True


def test_restored_repairing_state_without_timestamp_is_repairable(monkeypatch):
    """A persisted ``repairing`` state has repairing_s=0: it cannot wait 30 s."""

    module = _module()
    monkeypatch.setattr(module, "_manager_repair_enabled", lambda: False)
    station = {
        "current_ip": "1.2.3.4",
        "station_usable": False,
        "model_usable": False,
        "vpn_status": "error",
        "health": {
            "state": "repairing",
            "repairing_s": 0.0,
            "last_ok_age_s": 900.0,
        },
    }
    assert module._needs_chain(station) is True
    # A repairing station that just probed well is left alone for the grace.
    station["vpn_status"] = "connected"
    station["health"]["last_ok_age_s"] = 5.0
    assert module._needs_chain(station) is False


def test_egress_park_is_repairable_but_auth_park_is_not():
    module = _module()
    base = {
        "current_ip": "1.2.3.4",
        "station_usable": False,
        "model_usable": False,
        "vpn_status": "error",
    }
    egress_park = dict(
        base,
        health={
            "state": "parked",
            "cause": "deadline 240s",
            "last_error": "egress",
            "parked_remaining_s": 1500,
        },
    )
    auth_park = dict(
        base,
        health={
            "state": "parked",
            "cause": "auth_failed",
            "last_error": "AUTH_FAILED",
            "parked_remaining_s": 1500,
        },
    )
    assert module._needs_chain(egress_park) is True
    assert module._needs_chain(auth_park) is False


def test_repairing_is_not_recovered_but_usable_is():
    module = _module()
    repairing = {
        "station_usable": False,
        "model_usable": False,
        "health": {"state": "repairing"},
    }
    usable = {
        "station_usable": True,
        "model_usable": True,
        "health": {"state": "usable"},
    }
    draining = {
        "station_usable": True,
        "model_usable": False,
        "model_usable_reason": "draining",
        "health": {"state": "usable"},
    }
    assert module._station_recovered(repairing) is False
    assert module._station_recovered(usable) is True
    # A per-model quota drain does not mean the tunnel is broken; the station
    # still serves other buckets and must not keep a repair chain alive.
    assert module._station_recovered(draining) is True


def test_hopeless_auth_park_stops_the_chain():
    module = _module()
    auth_park = {
        "health": {
            "state": "parked",
            "cause": "auth_failed",
            "last_error": "AUTH_FAILED",
        }
    }
    egress_park = {
        "health": {
            "state": "parked",
            "cause": "deadline 240s",
            "last_error": "egress",
        }
    }
    assert module._station_hopeless(auth_park) is True
    assert module._station_hopeless(egress_park) is False


def test_all_probe_buckets_exhausted_requests_a_new_chain():
    module = _module()
    station = {"station": 4, "current_ip": "1.2.3.4"}
    models = ["deepseek", "glm"]
    health = {
        "quota_leases": [
            {"station": 4, "ip": "1.2.3.4", "model": "deepseek", "state": "bad", "selectable": False},
            {"station": 4, "ip": "1.2.3.4", "model": "glm", "state": "exhausted", "selectable": False},
        ]
    }
    assert module._station_exhausted(station, health, models) is True
    # One usable bucket is enough to keep the current egress.
    health["quota_leases"][1].update({"state": "qualified", "selectable": True})
    assert module._station_exhausted(station, health, models) is False
    # An untried bucket is not proof of exhaustion.
    health["quota_leases"] = health["quota_leases"][:1]
    assert module._station_exhausted(station, health, models) is False


def test_winner_is_protected_only_when_station_is_usable_on_same_ip():
    module = _module()
    winner = {"host": "al75.nordvpn.com", "ip": "87.120.102.74"}
    station = {
        "current_ip": "87.120.102.74",
        "vpn_status": "connected",
        "egress_state": "tunnel_up_full",
        "station_usable": True,
    }
    assert module._winner_protected(station, winner) is True
    # A different IP is a real rotation: no protection.
    station["current_ip"] = "187.13.30.135"
    assert module._winner_protected(station, winner) is False
    # A dead tunnel is repairable even on the same IP.
    station["current_ip"] = "87.120.102.74"
    station["egress_state"] = "tunnel_down"
    assert module._winner_protected(station, winner) is False
    # An unusable station is no longer protected by its old winner.
    station["egress_state"] = "tunnel_up_full"
    station["station_usable"] = False
    assert module._winner_protected(station, winner) is False


def test_recent_real_200_protects_a_live_quota_ip():
    """A probe timeout must not rotate an IP that just served real traffic."""

    module = _module()
    now = 1_000_000.0
    health = {
        "quota_ledger": {
            "buckets": [
                # alive, served 30 s ago -> protected
                {
                    "ip": "9.9.9.9",
                    "exhausted": False,
                    "successes": 3,
                    "last_seen": now - 30.0,
                },
                # exhausted even though it has successes -> not protected
                {
                    "ip": "8.8.8.8",
                    "exhausted": True,
                    "successes": 10,
                    "last_seen": now - 5.0,
                },
                # alive but stale beyond the grace -> not protected
                {
                    "ip": "7.7.7.7",
                    "exhausted": False,
                    "successes": 3,
                    "last_seen": now - 600.0,
                },
                # alive but never served a real 200 -> not protected
                {
                    "ip": "6.6.6.6",
                    "exhausted": False,
                    "successes": 0,
                    "last_seen": now - 5.0,
                },
            ]
        }
    }
    proven = module._proven_live_quota_ips(health, now=now)
    assert proven == {"9.9.9.9"}
    station = {"current_ip": "9.9.9.9", "vpn_status": "connected"}
    assert module._proven_quota_station(station, proven) is True
    station["vpn_status"] = "error"
    assert module._proven_quota_station(station, proven) is False


def test_ledger_exhaustion_overrides_a_recovered_quota_lease():
    """The daily bucket is authoritative; a lease TTL must not hide a 429."""

    module = _module()
    station = {"station": 4, "current_ip": "1.2.3.4"}
    health = {
        "quota_leases": [
            {
                "station": 4,
                "ip": "1.2.3.4",
                "model": "deepseek",
                "state": "qualified",
                "selectable": True,
                "blocked_remaining_s": 0.0,
            }
        ],
        "quota_ledger": {
            "buckets": [
                {"ip": "1.2.3.4", "bucket": "default", "exhausted": True},
            ]
        },
    }
    assert module._ledger_ip_exhausted(health, "1.2.3.4", ["deepseek"]) is True
    assert module._station_primary_available(station, health, "deepseek") is False
    assert module._station_exhausted(station, health, ["deepseek"]) is True
    # A live default bucket is not exhaustion.
    health["quota_ledger"]["buckets"][0]["exhausted"] = False
    assert module._ledger_ip_exhausted(health, "1.2.3.4", ["deepseek"]) is False
    assert module._station_exhausted(station, health, ["deepseek"]) is False


def test_winner_cooldown_requires_the_same_ip():
    module = _module()
    winner = {"host": "al37.nordvpn.com", "ip": "87.120.102.22"}
    # A different live IP is a stale winner: repair now.
    assert module._winner_cooldown_applies("187.15.131.60", winner, 10, 300) is False
    # Same winner IP: keep the cooldown and do not repave a just-found winner.
    assert module._winner_cooldown_applies("87.120.102.22", winner, 10, 300) is True
    # A different IP in the same range is still a different egress.
    assert module._winner_cooldown_applies("87.120.102.93", winner, 10, 300) is False
    # No IP is not a reason to wait.
    assert module._winner_cooldown_applies("", winner, 10, 300) is False
    # Cooldown expired.
    assert module._winner_cooldown_applies("87.120.102.22", winner, 400, 300) is False
    # A duplicate egress must never wait: one of the two stations has to move.
    assert (
        module._winner_cooldown_applies(
            "87.120.102.22", winner, 10, 300, duplicate=True
        )
        is False
    )


def test_stale_control_state_pauses_the_scanner():
    module = _module()
    now = datetime.now(UTC)
    status = {
        "stations": [{"station": 1}],
        "refreshed_at": now.isoformat(),
    }
    health = {
        "generated_at": now.isoformat(),
        "quota_ledger": {"buckets": []},
    }
    assert module._control_state_stale(status, health, max_age_s=90) == (False, "")

    health["generated_at"] = (now - timedelta(seconds=120)).isoformat()
    stale, reason = module._control_state_stale(status, health, max_age_s=90)
    assert stale is True
    assert "free-health stale" in reason

    assert module._control_state_stale({}, health, max_age_s=90)[0] is True
    assert module._control_state_stale(status, {}, max_age_s=90)[0] is True

    # Numeric age fields are accepted too (vpn-status exposes status_age_s).
    assert module._payload_age_s({"status_age_s": 12.0}, ("status_age_s",)) == 12.0


def test_duplicate_egress_ip_is_not_protected_from_repair():
    module = _module()
    stations = {
        1: {"current_ip": "87.120.102.28"},
        2: {"current_ip": "87.120.102.28"},
        3: {"current_ip": "87.120.102.20"},
    }
    assert module._ip_shared_with_other(stations, 2, "87.120.102.28") is True
    assert module._ip_shared_with_other(stations, 1, "87.120.102.28") is True
    assert module._ip_shared_with_other(stations, 3, "87.120.102.20") is False
    assert module._ip_shared_with_other(stations, 2, "") is False


def test_open_scan_runs_are_reconciled_against_live_stations():
    module = _module()
    class _Audit:
        def __init__(self):
            self.finished = []
            self.interrupted = []

        def open_runs(self):
            return [
                {"run_id": "recovered", "station": 3, "started_at": 1.0},
                {"run_id": "old-4", "station": 4, "started_at": 1.0},
                {"run_id": "down", "station": 4, "started_at": 2.0},
                {"run_id": "still-running", "station": 5, "started_at": 1.0},
            ]

        def finish_run(self, run_id, outcome, **kwargs):
            self.finished.append((run_id, outcome, kwargs))

        def interrupt_attempts(self, run_id):
            self.interrupted.append(run_id)

    audit = _Audit()
    stations = {
        3: {
            "station_usable": True,
            "model_usable": True,
            "current_ip": "87.120.102.20",
            "vpn": {"current_host": "al37.nordvpn.com"},
        },
        4: {"station_usable": False, "model_usable": False},
        5: {
            "station_usable": True,
            "model_usable": True,
            "current_ip": "1.1.1.1",
        },
    }
    closed = module._reconcile_open_runs(audit, stations, {5})
    assert closed == 3
    assert audit.finished[0] == (
        "recovered",
        "recovered",
        {"winner_host": "al37.nordvpn.com", "winner_ip": "87.120.102.20"},
    )
    assert audit.finished[1] == ("old-4", "superseded", {})
    assert audit.finished[2] == ("down", "interrupted", {})
    assert "still-running" not in audit.interrupted
    assert "old-4" in audit.interrupted


def test_primary_quota_count_only_refreshes_at_true_fleet_exhaustion():
    module = _module()
    stations = {
        1: {"station": 1, "current_ip": "1.1.1.1"},
        2: {"station": 2, "current_ip": "2.2.2.2"},
    }
    health = {
        "quota_leases": [
            {
                "station": 1,
                "ip": "1.1.1.1",
                "model": "deepseek",
                "state": "exhausted",
                "selectable": False,
            },
            {
                "station": 2,
                "ip": "2.2.2.2",
                "model": "deepseek",
                "state": "draining",
                "selectable": False,
            },
        ],
        "model_ip_health": [],
    }
    assert module._primary_quota_count(health, stations, [1, 2], "deepseek") == 0
    health["quota_leases"][1].update({"state": "active", "selectable": True})
    assert module._primary_quota_count(health, stations, [1, 2], "deepseek") == 1
    # A missing lease but an observed 200 is still capacity.
    health["quota_leases"] = health["quota_leases"][:1]
    health["model_ip_health"] = [
        {"station": 2, "ip": "2.2.2.2", "model": "deepseek", "last_status": 200}
    ]
    assert module._primary_quota_count(health, stations, [1, 2], "deepseek") == 1
    # An untested IP is not declared exhausted.
    health["model_ip_health"] = []
    assert module._primary_quota_count(health, stations, [1, 2], "deepseek") == 1


def test_auth_throttling_switches_the_repair_to_wireguard():
    module = _module()
    auth = {"health": {"last_error": "auth", "cause": "auth_failed"}}
    egress = {"health": {"last_error": "egress"}}
    assert (
        module._preferred_transport(auth, "openvpn-udp", "wireguard")
        == "wireguard"
    )
    assert (
        module._preferred_transport(egress, "openvpn-udp", "wireguard")
        == "openvpn-udp"
    )
    assert (
        module._preferred_transport(auth, "wireguard", "wireguard")
        == "wireguard"
    )
    assert module._preferred_transport(auth, "openvpn-udp", "") == "openvpn-udp"
    # Auto keeps the tri-stack loop even when the station is auth-throttled.
    assert module._preferred_transport(auth, "auto", "wireguard") == "auto"
    assert module._preferred_transport(egress, "auto", "") == "auto"
