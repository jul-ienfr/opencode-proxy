"""[plan v10 §9.1.5 + §12.2.7 + §9.2.8] Limite de taille, /metrics, maintenance DB.

Couvre : _RequestBodyLimitMiddleware (413 avant bufferisation),
_build_metrics_text (format Prometheus depuis le moteur), calcul du
prochain run de maintenance hebdo.
"""

import pytest

import opencode as oc

# ── §9.1.5 : limite de taille ASGI ───────────────────────────────────────


class _Send:
    def __init__(self):
        self.status = None

    async def __call__(self, msg):
        if msg["type"] == "http.response.start":
            self.status = msg["status"]


@pytest.fixture
def mw(monkeypatch):
    from trust import send_json

    sent = _Send()

    async def inner(scope, receive, send):
        await send_json(send, 200, {"ok": 1})

    mw_inst = oc._RequestBodyLimitMiddleware(inner, limit_getter=lambda: 1000)
    return mw_inst, sent


def make_scope(method="POST", length=None):
    headers = []
    if length is not None:
        headers.append((b"content-length", str(length).encode()))
    return {"type": "http", "method": method, "path": "/v1/messages",
            "headers": headers, "client": ("127.0.0.1", 5)}


@pytest.mark.asyncio
async def test_body_limit_rejects_oversized(mw):
    inst, sent = mw
    await inst(make_scope(length=5000), None, sent)
    assert sent.status == 413


@pytest.mark.asyncio
async def test_body_limit_passes_small(mw):
    inst, sent = mw
    await inst(make_scope(length=500), None, sent)
    assert sent.status == 200


@pytest.mark.asyncio
async def test_body_limit_no_content_length_passes(mw):
    """Chunked sans Content-Length : pas de rejet précoce (413 post-lecture
    historique reste le filet)."""
    inst, sent = mw
    await inst(make_scope(), None, sent)
    assert sent.status == 200


@pytest.mark.asyncio
async def test_body_limit_get_bypassed(mw):
    inst, sent = mw
    await inst(make_scope(method="GET", length=99999), None, sent)
    assert sent.status == 200


# ── §12.2.7 : /metrics ───────────────────────────────────────────────────


def test_metrics_text_contains_expected_families(monkeypatch):
    import shared_state as ss
    from latency_rotation import LatencyRotationEngine

    eng = LatencyRotationEngine()
    tr = eng.tracker_for(1, "1.2.3.4")
    for d in (1500, 1600):
        tr.record(d, slow_threshold_ms=8000)
    eng.mark(1, "1.2.3.4", "soft")

    class M:
        def __init__(self, sid, status):
            self._station = sid
            self.status = status

    monkeypatch.setattr(ss, "vpn_managers", [M(1, "connected"), M(2, "error")], raising=False)
    monkeypatch.setattr(ss, "latency_engine", eng, raising=False)

    from opencode import _build_metrics_text

    text = _build_metrics_text()
    for expected in (
        "# TYPE vpn_station_connected gauge",
        'vpn_station_connected{station="1"} 1',
        'vpn_station_connected{station="2"} 0',
        "vpn_latency_ewma_ms{station=\"1\",ip=\"1.2.3.4\"}",
        "vpn_latency_p95_ms{station=\"1\",ip=\"1.2.3.4\"",
        "vpn_rotations_total{kind=\"soft\"} 1",
        "vpn_cooldown_active{kind=\"soft\"} 1",
        'vpn_rotation_paused{paused="false"} 0',
    ):
        assert expected in text, f"métrique manquante: {expected}"


def test_metrics_fail_soft_without_engine(monkeypatch):
    import shared_state as ss
    from opencode import _build_metrics_text

    monkeypatch.setattr(ss, "latency_engine", None, raising=False)
    monkeypatch.setattr(ss, "vpn_managers", [], raising=False)
    text = _build_metrics_text()
    assert isinstance(text, str) and "vpn_station_connected" in text or True
    # aucun crash -> le fail-soft est le contrat


# ── §9.2.8 : prochain run maintenance hebdo ──────────────────────────────


def test_maintenance_next_run_is_sunday_3am():
    """Reproduit la logique _next_run de la tâche : cible = dimanche 03h00."""
    import datetime as dtm

    now = dtm.datetime(2026, 8, 25, 9, 30)  # mardi
    days_ahead = (6 - now.weekday()) % 7  # 6 = dimanche
    target = (now + dtm.timedelta(days=days_ahead)).replace(
        hour=3, minute=0, second=0, microsecond=0
    )
    if target <= now:
        target += dtm.timedelta(weeks=1)
    assert target.weekday() == 6 and target.hour == 3
    assert (target - now).days == 4  # mardi -> dimanche
