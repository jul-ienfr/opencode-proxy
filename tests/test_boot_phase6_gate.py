"""test_boot_phase6_gate.py — modes dégradés + gate VPN (Phase 6 plan boot).

Contrats :

1. ``--no-docker`` / ``--no-vpn`` / ``--ready-timeout`` existent, sont parsés,
   et alimentent ``BOOT_OPTS`` (jamais un comportement caché) ;
2. un mode explicitement désactivé n'est PAS une dégradation : ``/readyz`` doit
   répondre ``ready:true`` (sinon un orchestrateur verrait un proxy
   éternellement non prêt alors qu'il tourne comme demandé) ;
3. ``--ready-timeout`` transforme les gates non résolues en ``degraded`` passé
   le budget, au lieu d'un warming sans fin ;
4. pendant le warmup VPN, une requête sur un modèle FREE reçoit un ``503``
   explicite + ``Retry-After`` (et NON l'erreur métier « no usable station »
   qui laisse croire à une panne) ;
5. le gate est INERTE hors warmup et pour les modèles payants — et surtout il
   ne casse pas le streaming SSE (middleware ASGI pur, jamais
   BaseHTTPMiddleware qui bufferise).

Offline : l'état est simulé via ``shared_state``, aucun docker/réseau.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import opencode as oc
import shared_state


@pytest.fixture(autouse=True)
def _restore_boot_state():
    """Restaure BOOT_OPTS et l'état partagé après chaque test."""
    saved = dict(oc.BOOT_OPTS)
    saved_fanout = getattr(shared_state, "boot_fanout_task", None)
    saved_docker = getattr(shared_state, "docker_ready", None)
    yield
    oc.BOOT_OPTS.clear()
    oc.BOOT_OPTS.update(saved)
    shared_state.boot_fanout_task = saved_fanout
    shared_state.docker_ready = saved_docker


class _PendingTask:
    """Tâche « en cours » : done() == False (warmup non terminé)."""

    def done(self) -> bool:
        return False


class _DoneTask:
    def done(self) -> bool:
        return True


# ── 1. Options CLI ───────────────────────────────────────────────────


def test_cli_flags_exist():
    """--no-docker / --no-vpn / --ready-timeout sont déclarés."""
    out = subprocess.run(
        [sys.executable, "opencode.py", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    ).stdout
    assert "--no-docker" in out
    assert "--no-vpn" in out
    assert "--ready-timeout" in out


def test_boot_opts_defaults_are_historical():
    """Sans argument, les options gardent le comportement historique complet."""
    import importlib

    saved = dict(oc.BOOT_OPTS)
    try:
        mod = importlib.reload(oc)
        assert mod.BOOT_OPTS["no_docker"] is False
        assert mod.BOOT_OPTS["no_vpn"] is False
        assert mod.BOOT_OPTS["ready_timeout"] == 0.0
    finally:
        oc.BOOT_OPTS.clear()
        oc.BOOT_OPTS.update(saved)


# ── 2/3. /readyz ─────────────────────────────────────────────────────


def _readyz(app, **state):
    """Appelle la coroutine /readyz et renvoie le dict décodé."""
    import asyncio

    handler = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/readyz")
    for k, v in state.items():
        setattr(shared_state, k, v)
    resp = asyncio.run(handler())
    return json.loads(resp.body)


def test_readyz_warming_when_docker_pending():
    """Comportement historique : docker non résolu → warming, pas ready."""
    app = oc.app
    saved_ready = getattr(app.state, "_boot_ready", None)
    app.state._boot_ready = True
    try:
        data = _readyz(app, docker_ready=None, boot_fanout_task=None)
        assert data["ready"] is False
        assert data["degraded"]["docker"] == "warming"
    finally:
        app.state._boot_ready = saved_ready


def test_no_docker_is_not_a_degradation():
    """--no-docker : le mode demandé ne doit PAS rendre /readyz non prêt.

    C'est le bug constaté en boot réel sur :4010 — /readyz répondait
    `ready:false, degraded:{docker:unavailable}` en boucle.
    """
    app = oc.app
    saved_ready = getattr(app.state, "_boot_ready", None)
    app.state._boot_ready = True
    oc.BOOT_OPTS["no_docker"] = True
    try:
        data = _readyz(app, docker_ready=False, boot_fanout_task=None)
        assert data["ready"] is True, f"readyz={data}"
        assert "docker" not in data["degraded"]
    finally:
        app.state._boot_ready = saved_ready


def test_no_vpn_is_not_a_degradation():
    app = oc.app
    saved_ready = getattr(app.state, "_boot_ready", None)
    app.state._boot_ready = True
    oc.BOOT_OPTS["no_vpn"] = True
    try:
        data = _readyz(app, docker_ready=True, boot_fanout_task=_PendingTask())
        assert data["ready"] is True, f"readyz={data}"
        assert "vpn" not in data["degraded"]
    finally:
        app.state._boot_ready = saved_ready


def test_ready_timeout_declares_degraded_after_budget():
    """Passé le budget, les gates non résolues deviennent « timeout »."""
    import time as _t

    app = oc.app
    saved_ready = getattr(app.state, "_boot_ready", None)
    saved_t0 = oc.BOOT_T0
    app.state._boot_ready = True
    oc.BOOT_OPTS["ready_timeout"] = 1.0
    oc.BOOT_T0 = _t.monotonic() - 10  # budget largement dépassé
    try:
        data = _readyz(app, docker_ready=None, boot_fanout_task=_PendingTask())
        assert data["ready"] is False
        assert "timeout" in data["degraded"]["docker"], data["degraded"]
    finally:
        oc.BOOT_T0 = saved_t0
        app.state._boot_ready = saved_ready


def test_readyz_publishes_opts():
    """Les options effectives sont observables (jamais un comportement caché)."""
    app = oc.app
    saved_ready = getattr(app.state, "_boot_ready", None)
    app.state._boot_ready = True
    oc.BOOT_OPTS["no_docker"] = True
    try:
        data = _readyz(app, docker_ready=False, boot_fanout_task=None)
        assert data["opts"]["no_docker"] is True
    finally:
        app.state._boot_ready = saved_ready


# ── 4/5. Gate VPN ────────────────────────────────────────────────────


def test_gate_inactive_without_fanout():
    """Pas de fan-out en cours (démarrage normal terminé) → gate inerte."""
    shared_state.boot_fanout_task = None
    oc.BOOT_OPTS["no_vpn"] = False
    assert oc._vpn_warmup_gate_active() is False


def test_gate_inactive_when_fanout_done():
    shared_state.boot_fanout_task = _DoneTask()
    assert oc._vpn_warmup_gate_active() is False


def test_gate_inactive_with_no_vpn_flag():
    shared_state.boot_fanout_task = _PendingTask()
    oc.BOOT_OPTS["no_vpn"] = True
    assert oc._vpn_warmup_gate_active() is False


def test_gate_active_during_warmup(monkeypatch):
    shared_state.boot_fanout_task = _PendingTask()
    oc.BOOT_OPTS["no_vpn"] = False
    monkeypatch.setitem(oc.IP_ROTATION, "enabled", True)
    assert oc._vpn_warmup_gate_active() is True


def test_gate_inactive_when_vpn_disabled(monkeypatch):
    shared_state.boot_fanout_task = _PendingTask()
    monkeypatch.setitem(oc.IP_ROTATION, "enabled", False)
    assert oc._vpn_warmup_gate_active() is False


def test_gate_fails_open_on_broken_state(monkeypatch):
    """Toute erreur d'introspection → gate inactif (jamais de blocage trafic)."""

    class _Boom:
        @property
        def done(self):
            raise RuntimeError("état cassé")

    shared_state.boot_fanout_task = _Boom()
    assert oc._vpn_warmup_gate_active() is False


def test_free_model_detection():
    assert oc._is_free_model_route("deepseek-v4-flash-free") is True
    assert oc._is_free_model_route("claude-opus-4") is False
    assert oc._is_free_model_route("") is False, "modèle illisible → ne pas bloquer"


def test_free_model_detection_uses_the_map(monkeypatch):
    monkeypatch.setitem(oc.FREE_MODEL_MAP, "muse-spark-1.3-contributor", "muse-spark-free")
    assert oc._is_free_model_route("muse-spark-1.3-contributor") is True


def test_gate_payload_matches_protocol():
    """Le corps d'erreur suit le protocole de la route (SDK-compatible)."""
    anthropic = oc._vpn_gate_payload("/v1/messages", 5)
    assert anthropic["type"] == "error"
    assert anthropic["error"]["type"] == "vpn_warming"
    assert anthropic["retry_after"] == 5

    openai = oc._vpn_gate_payload("/v1/chat/completions", 5)
    assert "type" not in openai, "format OpenAI : pas d'enveloppe Anthropic"
    assert openai["error"]["code"] == "vpn_warming"


class _CapturingApp:
    """App ASGI factice : note si elle est atteinte et le corps reçu."""

    def __init__(self):
        self.called = False
        self.body = b""

    async def __call__(self, scope, receive, send):
        self.called = True
        while True:
            msg = await receive()
            self.body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})


def _run_gate(body: bytes, path: str = "/v1/messages") -> tuple[int, dict, _CapturingApp]:
    """Traverse le gate avec une requête synthétique ; renvoie (status, headers, app)."""
    import asyncio

    inner = _CapturingApp()
    gate = oc.VpnWarmupGateMiddleware(inner)
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"content-type", b"application/json")],
    }
    sent: list[dict] = []
    sent_body = {"done": False}

    async def receive():
        if sent_body["done"]:
            return {"type": "http.disconnect"}
        sent_body["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(gate(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    return start["status"], headers, inner


def _arm_warmup(monkeypatch):
    shared_state.boot_fanout_task = _PendingTask()
    oc.BOOT_OPTS["no_vpn"] = False
    monkeypatch.setitem(oc.IP_ROTATION, "enabled", True)


def test_gate_returns_503_with_retry_after(monkeypatch):
    """Pendant le warmup, un POST sur un modèle FREE reçoit 503 + Retry-After.

    Testé au niveau du MIDDLEWARE (pas via TestClient) : sur l'app complète, un
    503 peut aussi venir de la logique métier (« aucune clé valide »), ce qui
    masquerait la responsabilité du gate.
    """
    _arm_warmup(monkeypatch)
    body = json.dumps({"model": "deepseek-v4-flash-free"}).encode()
    status, headers, inner = _run_gate(body)
    assert status == 503, status
    assert headers["retry-after"] == str(oc._VPN_GATE_RETRY_AFTER)
    assert headers["x-degraded"] == "vpn-warming"
    assert inner.called is False, "la requête ne doit PAS atteindre l'app"


def test_gate_lets_paid_model_through(monkeypatch):
    """Un modèle payant traverse le gate, corps REJOUÉ intact."""
    _arm_warmup(monkeypatch)
    body = json.dumps({"model": "claude-opus-4", "messages": []}).encode()
    status, headers, inner = _run_gate(body)
    assert status == 200, status
    assert inner.called is True, "l'app doit être atteinte"
    assert inner.body == body, "le corps doit être rejoué à l'identique"


def test_gate_replays_multi_chunk_body(monkeypatch):
    """Un corps envoyé en plusieurs chunks doit être intégralement rejoué."""
    import asyncio

    _arm_warmup(monkeypatch)
    inner = _CapturingApp()
    gate = oc.VpnWarmupGateMiddleware(inner)
    scope = {"type": "http", "method": "POST", "path": "/v1/messages", "headers": []}
    chunks = [b'{"model":"claude', b'-opus-4","messages"', b":[]}"]
    state = {"i": 0}

    async def receive():
        if state["i"] < len(chunks):
            i = state["i"]
            state["i"] += 1
            return {
                "type": "http.request",
                "body": chunks[i],
                "more_body": i < len(chunks) - 1,
            }
        return {"type": "http.disconnect"}

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    asyncio.run(gate(scope, receive, send))
    assert inner.called is True
    assert inner.body == b"".join(chunks), "corps multi-chunk altéré"


def test_gate_passes_through_when_inactive(monkeypatch):
    """Hors warmup, le gate est totalement transparent."""
    shared_state.boot_fanout_task = None
    body = json.dumps({"model": "deepseek-v4-flash-free"}).encode()
    status, _headers, inner = _run_gate(body)
    assert status == 200
    assert inner.called is True
    assert inner.body == body


def test_gate_ignores_get_requests(monkeypatch):
    """Seuls les POST porteurs d'un modèle sont inspectés."""
    import asyncio

    _arm_warmup(monkeypatch)
    inner = _CapturingApp()
    gate = oc.VpnWarmupGateMiddleware(inner)
    scope = {"type": "http", "method": "GET", "path": "/v1/messages", "headers": []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    asyncio.run(gate(scope, receive, send))
    assert inner.called is True, "un GET ne doit jamais être bloqué"


def test_gate_ignores_non_gated_paths(monkeypatch):
    _arm_warmup(monkeypatch)
    body = json.dumps({"model": "deepseek-v4-flash-free"}).encode()
    status, _h, inner = _run_gate(body, path="/api/quotas")
    assert status == 200
    assert inner.called is True


def test_gate_middleware_is_registered():
    """Le gate est bien monté (dernier ajouté = middleware le plus externe)."""
    names = [m.cls.__name__ for m in oc.app.user_middleware]
    assert "VpnWarmupGateMiddleware" in names
