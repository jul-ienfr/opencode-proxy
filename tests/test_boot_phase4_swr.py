"""test_boot_phase4_swr.py — stale-while-revalidate des quotas (Phase 4).

Contrat du plan boot : la donnée de quota est TOUJOURS servie immédiatement
(jamais d'attente upstream sur le chemin de la requête), son âge est publié
dans les en-têtes `/api/quotas`, et un rafraîchissement part en tâche de fond
dès qu'elle dépasse max-age — en single-flight, pour ne pas lancer un fetch
par requête entrante.

Offline : le refresh de fond est monkeypatché, aucun appel réseau.
"""

from __future__ import annotations

import time

import pytest

import dashboard.quota as q


@pytest.fixture(autouse=True)
def _reset_swr_state(monkeypatch):
    """État SWR vierge avant/après chaque test (globals module)."""
    monkeypatch.setattr(q, "_last_refresh_ts", 0.0)
    monkeypatch.setattr(q, "_swr_inflight", False)
    yield


# ── Machine à états ──────────────────────────────────────────────────


def test_state_is_miss_before_any_successful_cycle():
    state, age = q.quota_cache_state()
    assert state == "miss"
    assert age == float("inf"), "âge inconnu avant le 1er cycle"


def test_state_fresh_right_after_a_cycle():
    q._mark_refreshed()
    state, age = q.quota_cache_state()
    assert state == "fresh"
    assert age < 1.0


def test_state_stale_past_max_age(monkeypatch):
    monkeypatch.setattr(q, "_last_refresh_ts", time.monotonic() - (q.SWR_MAX_AGE_S + 5))
    state, age = q.quota_cache_state()
    assert state == "stale"
    assert age >= q.SWR_MAX_AGE_S


def test_max_age_is_below_the_poll_interval():
    """max-age < intervalle du poller, sinon « stale » ne serait jamais atteint."""
    assert q.SWR_MAX_AGE_S < q.QUOTA_FETCH_INTERVAL
    assert q.SWR_MAX_AGE_S < q.SWR_STALE_S


# ── Déclenchement du refresh de fond ─────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_data_does_not_trigger_refresh(monkeypatch):
    calls = {"n": 0}

    async def _fake_refresh():
        calls["n"] += 1

    monkeypatch.setattr(q, "_refresh_all_now", _fake_refresh)
    q._mark_refreshed()
    await q.get_quota_snapshot()
    assert calls["n"] == 0, "donnée fraîche → aucun fetch"


@pytest.mark.asyncio
async def test_stale_data_triggers_background_refresh(monkeypatch):
    calls = {"n": 0}

    async def _fake_refresh():
        calls["n"] += 1

    monkeypatch.setattr(q, "_refresh_all_now", _fake_refresh)
    monkeypatch.setattr(q, "_last_refresh_ts", time.monotonic() - (q.SWR_MAX_AGE_S + 5))
    monkeypatch.setattr(q, "get_configured_workspaces", lambda: [{"go_workspace_id": "wrk_x"}])

    snapshot = await q.get_quota_snapshot()
    assert isinstance(snapshot, dict), "la réponse est immédiate, même périmée"
    await __import__("asyncio").sleep(0.05)  # laisse la tâche de fond tourner
    assert calls["n"] == 1, "donnée périmée → 1 refresh de fond"


@pytest.mark.asyncio
async def test_refresh_is_single_flight(monkeypatch):
    """N appels sur donnée périmée = UN seul refresh lancé."""
    calls = {"n": 0}

    async def _slow_refresh():
        calls["n"] += 1
        await __import__("asyncio").sleep(0.2)

    monkeypatch.setattr(q, "_refresh_all_now", _slow_refresh)
    monkeypatch.setattr(q, "_last_refresh_ts", time.monotonic() - (q.SWR_MAX_AGE_S + 5))
    monkeypatch.setattr(q, "get_configured_workspaces", lambda: [{"go_workspace_id": "wrk_x"}])

    for _ in range(5):
        await q.get_quota_snapshot()
    await __import__("asyncio").sleep(0.05)
    assert calls["n"] == 1, f"single-flight cassé : {calls['n']} refreshs lancés"


@pytest.mark.asyncio
async def test_no_workspace_means_no_refresh(monkeypatch):
    calls = {"n": 0}

    async def _fake_refresh():
        calls["n"] += 1

    monkeypatch.setattr(q, "_refresh_all_now", _fake_refresh)
    monkeypatch.setattr(q, "get_configured_workspaces", lambda: [])
    monkeypatch.setattr(q, "_last_refresh_ts", time.monotonic() - (q.SWR_MAX_AGE_S + 5))
    await q.get_quota_snapshot()
    await __import__("asyncio").sleep(0.05)
    assert calls["n"] == 0, "aucun workspace configuré → aucun fetch"


def test_refresh_outside_event_loop_is_a_noop(monkeypatch):
    """Hors boucle asyncio (appel sync/tests), le déclenchement ne doit pas lever."""
    monkeypatch.setattr(q, "_last_refresh_ts", time.monotonic() - (q.SWR_MAX_AGE_S + 5))
    monkeypatch.setattr(q, "get_configured_workspaces", lambda: [{"go_workspace_id": "wrk_x"}])
    q._trigger_background_refresh()  # ne doit rien lever


@pytest.mark.asyncio
async def test_refresh_failure_resets_inflight(monkeypatch):
    """Un refresh qui échoue doit libérer le single-flight (sinon plus jamais de refresh)."""

    async def _boom():
        raise RuntimeError("upstream down")

    monkeypatch.setattr(q, "_refresh_all_now", _boom)
    monkeypatch.setattr(q, "_last_refresh_ts", time.monotonic() - (q.SWR_MAX_AGE_S + 5))
    monkeypatch.setattr(q, "get_configured_workspaces", lambda: [{"go_workspace_id": "wrk_x"}])
    await q.get_quota_snapshot()
    await __import__("asyncio").sleep(0.05)
    assert q._swr_inflight is False, "le flag doit être libéré malgré l'erreur"


# ── Contrat HTTP ─────────────────────────────────────────────────────


def test_quota_route_publishes_cache_headers(monkeypatch):
    """/api/quotas publie X-Cache + Cache-Control SWR."""
    import sqlite3

    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from dashboard.api import register_dashboard

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    app = FastAPI()
    register_dashboard(app, "static", conn)

    monkeypatch.setattr(q, "_last_refresh_ts", time.monotonic() - (q.SWR_MAX_AGE_S + 5))
    monkeypatch.setattr(q, "get_configured_workspaces", lambda: [])

    with TestClient(app) as client:
        r = client.get("/api/quotas")
    assert r.status_code == 200
    assert r.headers["x-cache"] == "STALE"
    assert "stale-while-revalidate=300" in r.headers["cache-control"]


def test_quota_route_marks_fresh(monkeypatch):
    import sqlite3

    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from dashboard.api import register_dashboard

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    app = FastAPI()
    register_dashboard(app, "static", conn)

    monkeypatch.setattr(q, "_last_refresh_ts", time.monotonic())

    with TestClient(app) as client:
        r = client.get("/api/quotas")
    assert r.status_code == 200
    assert r.headers["x-cache"] == "HIT"
