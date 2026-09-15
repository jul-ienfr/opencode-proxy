"""test_free_leg_effort_recompute.py — [TROU 7, volet survivant « effort »].

DÉFAUT MESURÉ (lecture + sonde sur le corps réellement transmis) — dans
``_try_free_model_first``, la branche « endpoint free = Chat » sur un corps **déjà
au format Chat** faisait ``free_body = dict(body)`` : le ``reasoning_effort`` posé
en amont du handler pour le modèle **payant** (``chat_completions`` →
``_resolve_effort(body, model_id)``) partait **tel quel** vers l'amont free. La
fonction ne contenait **aucun** site d'effort (0 occurrence sur ~790 lignes).

Conséquence : dès que le plafond d'effort du modèle free est plus bas que celui du
payant, l'amont free reçoit un niveau hors plafond.

Couple réel à plafonds divergents (``config.yaml:thinking.effort_caps``) :

* payant ``deepseek-v4-flash`` → plafond ``max``
* free ``deepseek-v4-flash-free`` → plafond ``high``   (entrée explicite)

Ces tests asserent la **valeur réellement envoyée à l'amont** (le corps capturé
dans le double amont ``_do_free_request_curl_cffi``), pas un code HTTP ni un état
interne. Le premier passe par ``TestClient`` sur ``oc.app`` : c'est le handler
``chat_completions`` réel qui pose la décision payante, puis la jambe free réelle
qui est exercée — le double n'est posé que sur la couche réseau.
"""

import json

import pytest

import opencode as oc

PAID_CHAT = "deepseek-v4-flash"
FREE_CHAT = "deepseek-v4-flash-free"


# ── harnais amont ──────────────────────────────────────────────────
class _Station:
    def __init__(self, n, ip, socks5_url):
        self._station = n
        self.status = "connected"
        self.current_ip = ip
        self.socks5_url = socks5_url
        self._quota_per_ip = 15
        self.current_identity = {
            "impersonate": "chrome131",
            "user_agent": None,
            "extra_headers": {},
        }


class _StubVpn:
    proxy_mode = "vpn"
    current_ip = "10.0.0.1"


class _Pool:
    enabled = True
    socks5_mode = False
    active_station = None

    def __init__(self, stations):
        self._stations = list(stations)

    async def on_request(self):
        best = self._best_station()
        return best.socks5_url if best else None, best

    def _best_station(self):
        for st_ in self._stations:
            if st_.status == "connected":
                return st_
        return None

    def _best_station_excluding_many(self, excluded, forced_pool=None):
        for st_ in self._stations:
            if st_ not in excluded and st_.status == "connected":
                return st_
        return None

    def _station_usable(self, st_, exclude_approaching=False, forced_pool=None):
        return st_.status == "connected"

    def pick_candidates(self, forced_pool=None):
        return [s for s in self._stations if s.status == "connected"]

    def on_quota_exhausted(self, station):
        pass

    def note_hedge_winner(self, winner, primary=None):
        pass


class _Resp:
    """Réponse amont free minimale mais exploitable par le handler (Chat 200)."""

    def __init__(self, payload, status=200):
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self._payload = payload
        self.content = json.dumps(payload).encode()
        self.text = self.content.decode()
        self._resp = self

    def json(self):
        return self._payload


def _chat_reply(model):
    return {
        "id": "chatcmpl-witness-effort",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
    }


class _Seam:
    """Capture le corps **réellement** remis à la couche réseau pour l'amont free."""

    def __init__(self):
        self.calls = []

    async def __call__(self, body, headers, proxy_url=None, station=None, endpoint=None, **kw):
        self.calls.append({"endpoint": endpoint, "body": json.loads(json.dumps(body))})
        return _Resp(_chat_reply(body.get("model") or FREE_CHAT))

    @property
    def wire(self):
        assert self.calls, (
            "le double amont n'a jamais été atteint : la jambe free n'a pas été exercée, "
            "le test ne prouve rien"
        )
        return self.calls[0]["body"]


@pytest.fixture
def free_env(monkeypatch):
    monkeypatch.setattr(oc, "_vpn_manager", _StubVpn())
    monkeypatch.setattr(oc, "_get_cached_public_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log_free_model_usage", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_free_ip_pool", _Pool([_Station(1, "1.1.1.1", "socks5://127.0.0.1:1080")]))
    saved = {
        k: oc.IP_ROTATION.get(k)
        for k in (
            "strict_free",
            "max_free_attempts",
            "auto_max_free_attempts",
            "station_count",
            "on_429_action",
            "free_exception_fallback",
            "free_parallel",
        )
    }
    oc.IP_ROTATION.update(
        {
            "strict_free": False,
            "max_free_attempts": 1,
            "auto_max_free_attempts": False,
            "station_count": 1,
            "on_429_action": "both",
            "free_exception_fallback": "station-first",
            "free_parallel": {
                "enabled": False,
                "routing": "round-robin",
                "mode": "load-balance",
                "hedge_delay_ms": 300,
                "hedge_max_attempts": 1,
            },
        }
    )
    oc._free_model_cooldowns.clear()
    yield oc
    for k, v in saved.items():
        if v is None:
            oc.IP_ROTATION.pop(k, None)
        else:
            oc.IP_ROTATION[k] = v


# ── 0. Le couple payant/free a bien des plafonds DIFFÉRENTS ────────
def test_witness_pair_has_really_divergent_effort_caps():
    """Sans écart de plafond, tout témoin sur l'effort ne prouverait rien."""
    if oc.FREE_MODEL_MAP.get(PAID_CHAT) != FREE_CHAT:
        pytest.skip(f"{PAID_CHAT} n'est plus routé vers {FREE_CHAT} dans cet environnement")

    from config.effort_caps import get_max_effort_for_model

    paid_cap = get_max_effort_for_model(PAID_CHAT)
    free_cap = get_max_effort_for_model(FREE_CHAT)
    assert paid_cap != free_cap, (
        f"plafonds identiques ({paid_cap!r}) pour {PAID_CHAT}/{FREE_CHAT} : le témoin ci-dessous "
        f"ne pourrait pas distinguer un recalcul d'un passthrough"
    )
    assert paid_cap == "max" and free_cap == "high", (paid_cap, free_cap)


# ── 1. E2E : l'amont free reçoit un effort PLAFONNÉ à son propre modèle ──
def test_e2e_p3_free_upstream_receives_capped_reasoning_effort(free_env, monkeypatch):
    """``/v1/chat/completions`` + ``effort=max`` → l'amont free voit ``high``.

    Le handler réel pose ``reasoning_effort`` pour le payant (plafond ``max``) ; la
    jambe free doit le recalculer pour ``deepseek-v4-flash-free`` (plafond ``high``).
    Sans le correctif, le fil porte ``max`` — mesuré avant correctif.
    """
    from fastapi.testclient import TestClient

    seam = _Seam()
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", seam)
    monkeypatch.setattr(oc, "_get_auth_headers", lambda *a, **k: {"Authorization": "Bearer test"})
    monkeypatch.setattr(oc, "_has_usable_paid_key", lambda: True)

    payload = {
        "model": PAID_CHAT,
        "messages": [{"role": "user", "content": "salut"}],
        "effort": "max",
        "max_tokens": 4096,
    }
    resp = TestClient(oc.app).post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200, resp.text[:500]

    wire = seam.wire
    assert wire.get("model") == FREE_CHAT, f"modèle free non ciblé : {wire.get('model')!r}"
    assert "/responses" not in (seam.calls[0]["endpoint"] or ""), "ce témoin vise l'endpoint free Chat"
    assert wire.get("reasoning_effort") == "high", (
        "l'amont free a reçu "
        f"{wire.get('reasoning_effort')!r} : le champ a été décidé pour le modèle PAYANT "
        f"({PAID_CHAT}, plafond max) et n'a pas été recalculé pour le modèle FREE "
        f"({FREE_CHAT}, plafond high)"
    )


# ── 2. Aucun effort inventé quand le client n'en demande pas ──────
@pytest.mark.asyncio
async def test_no_effort_invented_when_client_asks_none(free_env, monkeypatch):
    """Sans demande d'effort, le recalcul ne doit rien ajouter sur le fil."""
    monkeypatch.setattr(oc, "FREE_MODEL_MAP", {PAID_CHAT: FREE_CHAT})
    seam = _Seam()
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", seam)

    body = {"model": PAID_CHAT, "messages": [{"role": "user", "content": "salut"}], "max_tokens": 4096}
    decision = oc._resolve_effort(body, PAID_CHAT)
    assert not decision.wants, "prémisse : ce corps ne porte aucune demande d'effort"

    await oc._try_free_model_first(dict(body), {}, "openai", PAID_CHAT)
    assert "reasoning_effort" not in seam.wire, (
        "un effort a été inventé là où le client n'en demandait aucun"
    )


# ── 3. Un free à plafond PLUS HAUT ne relève pas le niveau demandé ──
@pytest.mark.asyncio
async def test_free_model_with_higher_cap_never_raises_the_level(free_env, monkeypatch):
    """``mimo-v2.5-free`` plafonne à ``max`` : la valeur payante doit rester inchangée."""
    monkeypatch.setattr(oc, "FREE_MODEL_MAP", {PAID_CHAT: "mimo-v2.5-free"})
    seam = _Seam()
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", seam)

    body = {
        "model": PAID_CHAT,
        "messages": [{"role": "user", "content": "salut"}],
        "reasoning_effort": "low",
        "max_tokens": 4096,
    }
    await oc._try_free_model_first(dict(body), {}, "openai", PAID_CHAT)
    assert seam.wire.get("reasoning_effort") == "low", (
        "le recalcul a relevé le niveau : il ne doit que rabaisser au plafond du modèle free"
    )


# ── 4. Garde anti-complicité : le vrai ``_try_free_model_first`` est exercé ──
def test_free_leg_is_the_real_function():
    """Si la suite remplace la jambe free par un stub global, les tests 2/3 ne prouvent rien."""
    import inspect

    src = inspect.getsource(oc._try_free_model_first)
    assert "free_body = dict(body)" in src, "la jambe free réelle n'est plus celle attendue"
    assert "_do_free_request_curl_cffi" in src, "le seam réseau attendu n'est plus appelé par la jambe free"
