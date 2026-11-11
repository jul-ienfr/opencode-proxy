"""Qualification router tests: registration and read-only payloads."""

from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers.qualification_routes import register_qualification_routes
from free.qualification.domain import Attempt, AttemptStatus, Candidate
from free.qualification.store import QualificationStore


def _app(db_path, **kwargs):
    app = FastAPI()
    register_qualification_routes(app, lambda: str(db_path), **kwargs)
    return app


def test_routes_are_registered():
    app = _app("unused.db")
    paths = {route.path for route in app.routes}
    assert "/api/egress/qualification/summary" in paths
    assert "/api/egress/qualification/queue" in paths
    assert "/api/egress/qualification/next" in paths
    assert "/api/egress/qualification/epochs" in paths
    assert "/api/egress/qualification/epochs/{epoch_id}" in paths
    assert "/api/egress/qualification/transports" in paths
    assert "/api/egress/qualification/quota" in paths
    assert "/api/egress/qualification/coverage" in paths
    assert "/api/egress/qualification/metrics" in paths
    assert "/api/egress/qualification/slo" in paths
    assert "/api/egress/qualification/rotations" in paths
    assert "/api/egress/qualification/export" in paths
    assert "/api/egress/qualification/rotate" in paths
    assert "/api/egress/qualification/watchdog" in paths
    assert "/api/egress/qualification/repairs" in paths


def test_summary_queue_and_next(tmp_path):
    db = tmp_path / "registry.db"
    now = time.time()
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                Candidate(
                    hostname="se1.nordvpn.com",
                    country="Sweden",
                    transports=("wireguard",),
                    potential_score=90.0,
                    first_seen_at=now,
                    last_seen_at=now,
                    updated_at=now,
                )
            ],
            now=now,
        )
    finally:
        store.close()
    client = TestClient(_app(db))
    summary = client.get("/api/egress/qualification/summary")
    assert summary.status_code == 200
    assert summary.json()["total"] == 1
    queue = client.get("/api/egress/qualification/queue", params={"status": "pending"})
    assert queue.status_code == 200
    assert queue.json()["items"][0]["hostname"] == "se1.nordvpn.com"
    nxt = client.get("/api/egress/qualification/next", params={"limit": 5})
    assert nxt.status_code == 200
    assert nxt.json()["items"][0]["hostname"] == "se1.nordvpn.com"
    metrics = client.get("/api/egress/qualification/metrics")
    assert metrics.status_code == 200
    assert metrics.json()["qualification_total"] == 1
    prometheus = client.get(
        "/api/egress/qualification/metrics", params={"format": "prometheus"}
    )
    assert prometheus.status_code == 200
    assert "qualification_total 1" in prometheus.text


def test_queue_filters_and_pagination(tmp_path):
    db = tmp_path / "registry.db"
    now = time.time()
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                Candidate(
                    hostname="se1.nordvpn.com",
                    country="Sweden",
                    transports=("wireguard",),
                    potential_score=90.0,
                    first_seen_at=now,
                    last_seen_at=now,
                    updated_at=now,
                ),
                Candidate(
                    hostname="se2.nordvpn.com",
                    country="Sweden",
                    transports=("openvpn-udp",),
                    potential_score=80.0,
                    first_seen_at=now,
                    last_seen_at=now,
                    updated_at=now,
                ),
                Candidate(
                    hostname="jp1.nordvpn.com",
                    country="Japan",
                    transports=("wireguard",),
                    potential_score=70.0,
                    first_seen_at=now,
                    last_seen_at=now,
                    updated_at=now,
                ),
            ],
            now=now,
        )
    finally:
        store.close()
    client = TestClient(_app(db))
    page = client.get(
        "/api/egress/qualification/queue",
        params={"country": "Sweden", "limit": 1, "offset": 1},
    ).json()
    assert page["total"] == 2
    assert len(page["items"]) == 1
    filtered = client.get(
        "/api/egress/qualification/queue",
        params={"transport": "wireguard", "limit": 10},
    ).json()
    assert {item["hostname"] for item in filtered["items"]} == {
        "se1.nordvpn.com",
        "jp1.nordvpn.com",
    }


def test_rotate_dry_run_plans_a_candidate(tmp_path):
    db = tmp_path / "registry.db"
    now = time.time()
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                Candidate(
                    hostname="se1.nordvpn.com",
                    country="Sweden",
                    transports=("wireguard",),
                    potential_score=90.0,
                    first_seen_at=now,
                    last_seen_at=now,
                    updated_at=now,
                )
            ],
            now=now,
        )
        store.upsert_station_state(
            1,
            state="degraded",
            client_usable=False,
            now=now,
        )
    finally:
        store.close()
    client = TestClient(_app(db))
    payload = client.post("/api/egress/qualification/rotate", json={"station": 1}).json()
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["decision"]["action"] == "rotate"
    assert payload["decision"]["candidate"] == "se1.nordvpn.com"


def test_rotate_apply_requires_live_mode(tmp_path):
    async def _unused(**kwargs):
        return {"ok": True}

    client = TestClient(
        _app(
            tmp_path / "registry.db",
            allow_live=False,
            rotate_apply=_unused,
        )
    )
    response = client.post(
        "/api/egress/qualification/rotate",
        json={"station": 1, "apply": True},
    )
    assert response.status_code == 409
    assert "shadow_mode" in response.json()["error"]


def test_rotate_apply_without_executor_in_live_mode(tmp_path):
    client = TestClient(_app(tmp_path / "registry.db", allow_live=True))
    response = client.post(
        "/api/egress/qualification/rotate",
        json={"station": 1, "apply": True},
    )
    assert response.status_code == 409
    assert "executor" in response.json()["error"]


def test_rotate_apply_executes_the_callback(tmp_path):
    calls = []

    async def _apply(*, station, body):
        calls.append((station, body))
        return {
            "ok": True,
            "dry_run": False,
            "station": station,
            "decision": {"action": "rotate"},
            "cutover": {"status": "committed"},
        }

    client = TestClient(
        _app(
            tmp_path / "registry.db",
            allow_live=True,
            rotate_apply=_apply,
        )
    )
    payload = client.post(
        "/api/egress/qualification/rotate",
        json={"station": 2, "apply": True, "quota_remaining": 1},
    ).json()
    assert payload["ok"] is True
    assert payload["dry_run"] is False
    assert calls == [(2, {"station": 2, "apply": True, "quota_remaining": 1})]


def test_operator_pause_and_resume_routes(tmp_path):
    db = tmp_path / "registry.db"
    now = time.time()
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                Candidate(
                    hostname="se1.nordvpn.com",
                    country="Sweden",
                    transports=("wireguard",),
                    potential_score=90.0,
                    first_seen_at=now,
                    last_seen_at=now,
                    updated_at=now,
                )
            ],
            now=now,
        )
    finally:
        store.close()
    client = TestClient(_app(db))
    assert client.get(
        "/api/egress/qualification/queue", params={"status": "pending"}
    ).json()["total"] == 1
    paused = client.post(
        "/api/egress/qualification/candidates/se1.nordvpn.com/pause"
    ).json()
    assert paused["changed"] is True
    queue = client.get(
        "/api/egress/qualification/queue", params={"status": "pending"}
    ).json()
    assert queue["total"] == 0
    resumed = client.post(
        "/api/egress/qualification/candidates/se1.nordvpn.com/resume"
    ).json()
    assert resumed["changed"] is True
    queue = client.get(
        "/api/egress/qualification/queue", params={"status": "pending"}
    ).json()
    assert queue["total"] == 1


def test_attempts_csv_export(tmp_path):
    db = tmp_path / "registry.db"
    now = time.time()
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                Candidate(
                    hostname="se1.nordvpn.com",
                    country="Sweden",
                    transports=("wireguard",),
                    potential_score=90.0,
                    first_seen_at=now,
                    last_seen_at=now,
                    updated_at=now,
                )
            ],
            now=now,
        )
        attempt = Attempt(
            hostname="se1.nordvpn.com",
            station=1,
            transport="wireguard",
            status=AttemptStatus.OK,
            egress_ip="1.2.3.4",
            rtt_ms=25.0,
            dns_ms=2.0,
            connect_ms=5.0,
            handshake_ms=8.0,
            endpoint_ip="1.2.3.4",
            started_at=now,
            finished_at=now,
        )
        store.record_attempt(attempt)
    finally:
        store.close()
    client = TestClient(_app(db))
    response = client.get(
        "/api/egress/qualification/attempts", params={"format": "csv"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "se1.nordvpn.com" in response.text
    assert "dns_ms" in response.text
    assert "1.2.3.4" in response.text


def test_rotate_headroom_config_changes_decision(tmp_path):
    db = tmp_path / "registry.db"
    now = time.time()
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                Candidate(
                    hostname="se1.nordvpn.com",
                    country="Sweden",
                    transports=("wireguard",),
                    potential_score=90.0,
                    first_seen_at=now,
                    last_seen_at=now,
                    updated_at=now,
                )
            ],
            now=now,
        )
        store.upsert_station_state(
            1,
            state="serving",
            current_ip="1.2.3.4",
            client_usable=True,
            now=now,
        )
    finally:
        store.close()
    strict = TestClient(
        _app(db, rotation_headroom_pct=50.0, last_station_protection=False)
    )
    decision = strict.post(
        "/api/egress/qualification/rotate",
        json={"station": 1, "quota_remaining": 10, "quota_estimate": 500},
    ).json()["decision"]
    assert decision["action"] == "rotate"
    relaxed = TestClient(
        _app(db, rotation_headroom_pct=1.0, last_station_protection=False)
    )
    decision = relaxed.post(
        "/api/egress/qualification/rotate",
        json={"station": 1, "quota_remaining": 10, "quota_estimate": 500},
    ).json()["decision"]
    assert decision["action"] == "keep"


def test_export_contains_all_projections(tmp_path):
    db = tmp_path / "registry.db"
    now = time.time()
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                Candidate(
                    hostname="se1.nordvpn.com",
                    country="Sweden",
                    transports=("wireguard",),
                    potential_score=90.0,
                    first_seen_at=now,
                    last_seen_at=now,
                    updated_at=now,
                )
            ],
            now=now,
        )
    finally:
        store.close()
    payload = TestClient(_app(db)).get("/api/egress/qualification/export").json()
    assert set(payload) == {
        "summary",
        "queue",
        "next",
        "epochs",
        "transports",
        "stations",
        "watchdog",
        "discovery",
        "repairs",
        "rotations",
        "quota",
        "slo",
    }
    assert payload["summary"]["total"] == 1


def test_rotate_standby_size_config_changes_decision(tmp_path):
    db = tmp_path / "registry.db"
    now = time.time()
    store = QualificationStore(db)
    try:
        store.upsert_candidates(
            [
                Candidate(
                    hostname="se1.nordvpn.com",
                    country="Sweden",
                    transports=("wireguard",),
                    potential_score=90.0,
                    first_seen_at=now,
                    last_seen_at=now,
                    updated_at=now,
                )
            ],
            now=now,
        )
        store.upsert_station_state(
            1,
            state="serving",
            current_ip="1.2.3.4",
            client_usable=True,
            now=now,
        )
        store.upsert_station_runtime(
            1,
            state="serving",
            standby_egress_ip="5.6.7.8",
            standby_option_key="standby.nordvpn.com",
            now=now,
        )
    finally:
        store.close()
    client = TestClient(
        _app(
            db,
            rotation_headroom_pct=50.0,
            last_station_protection=False,
            station_standby_size=0,
        )
    )
    decision = client.post(
        "/api/egress/qualification/rotate",
        json={"station": 1, "quota_remaining": 10, "quota_estimate": 500},
    ).json()["decision"]
    assert decision["action"] == "rotate"


def test_missing_db_is_fail_soft(tmp_path):
    client = TestClient(_app(tmp_path / "missing.db"))
    response = client.get("/api/egress/qualification/summary")
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    quota_response = client.get("/api/egress/qualification/quota")
    assert quota_response.status_code == 200
    assert quota_response.json()["enabled"] is False
    slo_response = client.get("/api/egress/qualification/slo")
    assert slo_response.status_code == 200
    assert slo_response.json()["enabled"] is False


class FakeProcess:
    def __init__(self, running=True):
        self.returncode = None if running else 0

    def poll(self):
        return self.returncode


def test_start_epoch_shadow_and_conflict(tmp_path):
    calls = []
    process = FakeProcess()

    def start_epoch(**kwargs):
        calls.append(kwargs)
        return {
            "pid": 1234,
            "mode": "shadow" if kwargs["shadow"] else "live",
            "station": kwargs["station"],
            "max_hosts": kwargs["max_hosts"],
            "started_at": 1.0,
            "log_path": "logs/test.log",
            "process": process,
        }

    client = TestClient(_app(tmp_path / "registry.db", start_epoch=start_epoch))
    response = client.post(
        "/api/egress/qualification/epochs",
        json={"station": 2, "max_hosts": 10},
    )
    assert response.status_code == 202
    assert response.json()["pid"] == 1234
    assert response.json()["mode"] == "shadow"
    assert "process" not in response.json()
    assert calls == [{"station": 2, "max_hosts": 10, "shadow": True}]

    conflict = client.post("/api/egress/qualification/epochs", json={})
    assert conflict.status_code == 409
    assert "déjà en cours" in conflict.json()["error"]

    process.returncode = 0
    again = client.post("/api/egress/qualification/epochs", json={})
    assert again.status_code == 202
    assert calls[-1] == {"station": 1, "max_hosts": 100, "shadow": True}


def test_start_epoch_live_and_validation(tmp_path):
    calls = []

    def start_epoch(**kwargs):
        calls.append(kwargs)
        return {"pid": 9, "process": FakeProcess(running=False)}

    blocked = TestClient(_app(tmp_path / "a.db", start_epoch=start_epoch))
    response = blocked.post(
        "/api/egress/qualification/epochs",
        json={"mode": "live"},
    )
    assert response.status_code == 409
    assert "shadow_mode" in response.json()["error"]
    assert calls == []

    allowed = TestClient(
        _app(
            tmp_path / "b.db",
            start_epoch=start_epoch,
            default_shadow=False,
            allow_live=True,
        )
    )
    assert allowed.post(
        "/api/egress/qualification/epochs",
        json={"mode": "auto"},
    ).status_code == 202
    assert calls[-1]["shadow"] is False
    assert allowed.post(
        "/api/egress/qualification/epochs",
        json={"mode": "shadow"},
    ).status_code == 202
    assert calls[-1]["shadow"] is True
    assert allowed.post(
        "/api/egress/qualification/epochs",
        json={"station": 0},
    ).status_code == 400
    assert allowed.post(
        "/api/egress/qualification/epochs",
        json={"mode": "nope"},
    ).status_code == 400


def test_start_epoch_requires_launcher_and_token(tmp_path):
    client = TestClient(_app(tmp_path / "registry.db"))
    response = client.post("/api/egress/qualification/epochs", json={})
    assert response.status_code == 503

    token_client = TestClient(
        _app(
            tmp_path / "registry.db",
            token_checker=lambda request: JSONResponse(
                {"error": "token"},
                status_code=401,
            ),
            start_epoch=lambda **kwargs: {"pid": 1, "process": FakeProcess()},
        )
    )
    assert token_client.post(
        "/api/egress/qualification/epochs",
        json={},
    ).status_code == 401

class FakeQualifierManager:
    def __init__(self, snapshot=None):
        self.applied: list[tuple[bool, int]] = []
        self._snapshot = snapshot or {
            "enabled": True,
            "count": 2,
            "manager": True,
            "slots": [{"station": 7, "state": "running"}],
        }

    def snapshot(self):
        return dict(self._snapshot)

    async def apply(self, *, enabled, count):
        self.applied.append((enabled, count))
        return {
            "enabled": enabled,
            "count": count,
            "manager": True,
            "slots": [],
        }


def test_qualifiers_get_uses_the_live_manager(tmp_path):
    manager = FakeQualifierManager()
    client = TestClient(
        _app(tmp_path / "a.db", qualifier_manager_getter=lambda: manager)
    )
    response = client.get("/api/egress/qualification/qualifiers")
    assert response.status_code == 200
    assert response.json()["manager"] is True
    assert response.json()["slots"] == [{"station": 7, "state": "running"}]


def test_qualifiers_get_falls_back_to_the_static_snapshot(tmp_path, monkeypatch):
    from free.qualification import qualifiers as qualifier_module

    monkeypatch.setattr(
        qualifier_module,
        "qualifier_config_snapshot",
        lambda: {"manager": False, "enabled": False, "count": 0, "slots": []},
    )
    client = TestClient(_app(tmp_path / "a.db"))
    response = client.get("/api/egress/qualification/qualifiers")
    assert response.status_code == 200
    assert response.json()["manager"] is False
    assert response.json()["count"] == 0


def test_qualifiers_post_applies_to_the_live_manager(tmp_path):
    manager = FakeQualifierManager()
    client = TestClient(
        _app(tmp_path / "a.db", qualifier_manager_getter=lambda: manager)
    )
    response = client.post(
        "/api/egress/qualification/qualifiers",
        json={"enabled": True, "count": 2},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["restart_required"] is False
    assert payload["count"] == 2
    assert manager.applied == [(True, 2)]


def test_qualifiers_post_without_manager_persists_and_requests_restart(
    tmp_path, monkeypatch
):
    from free.qualification import qualifiers as qualifier_module

    calls: list[dict] = []

    def fake_persist(*, enabled, count):
        calls.append({"enabled": enabled, "count": count})
        return {
            "manager": False,
            "enabled": enabled,
            "count": count,
            "slots": [],
        }

    monkeypatch.setattr(
        qualifier_module, "persist_qualifier_request", fake_persist
    )
    client = TestClient(_app(tmp_path / "a.db"))
    response = client.post(
        "/api/egress/qualification/qualifiers",
        json={"enabled": True, "count": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["restart_required"] is True
    assert payload["count"] == 1
    assert calls == [{"enabled": True, "count": 1}]


def test_qualifiers_post_validates_the_body(tmp_path):
    manager = FakeQualifierManager()
    client = TestClient(
        _app(tmp_path / "a.db", qualifier_manager_getter=lambda: manager)
    )
    assert client.post(
        "/api/egress/qualification/qualifiers", json={}
    ).status_code == 400
    assert client.post(
        "/api/egress/qualification/qualifiers", json={"count": "x"}
    ).status_code == 400
    assert client.post(
        "/api/egress/qualification/qualifiers",
        json={"enabled": True, "count": -1},
    ).status_code == 400
    assert client.post(
        "/api/egress/qualification/qualifiers",
        content=b"not-json",
        headers={"content-type": "application/json"},
    ).status_code == 400
    assert manager.applied == []


def test_qualifiers_post_requires_the_dashboard_token(tmp_path):
    manager = FakeQualifierManager()
    client = TestClient(
        _app(
            tmp_path / "a.db",
            token_checker=lambda request: JSONResponse(
                {"error": "token"}, status_code=401
            ),
            qualifier_manager_getter=lambda: manager,
        )
    )
    assert client.post(
        "/api/egress/qualification/qualifiers",
        json={"enabled": True, "count": 1},
    ).status_code == 401
    assert manager.applied == []


def test_qualifiers_post_reports_manager_failure(tmp_path):
    class BrokenManager(FakeQualifierManager):
        async def apply(self, *, enabled, count):
            raise RuntimeError("docker indisponible")

    client = TestClient(
        _app(
            tmp_path / "a.db",
            qualifier_manager_getter=lambda: BrokenManager(),
        )
    )
    response = client.post(
        "/api/egress/qualification/qualifiers",
        json={"enabled": True, "count": 1},
    )
    assert response.status_code == 500
    assert "docker indisponible" in response.json()["error"]
