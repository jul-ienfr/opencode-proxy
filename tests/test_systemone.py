"""POST /v1/systemone — passthrough TypeSafe SystemOne (Jev).

Contrats (docs : https://opencode.ai/docs/fr/zen/#jev + probes live) :
  * le corps {model, state, questions} est relayé TEL QUEL vers
    https://opencode.ai/zen/v1/systemone (jamais vers /chat/completions) ;
  * `jev-1.13` (payant) → clé Zen du pool (Bearer <clé>) ;
  * `jev-1.13-free` → `Bearer public` anonyme direct ;
  * validation : model connu + state non vide + questions typées ;
  * réponse amont relayée telle quelle, usage → compteurs.

Pattern établi : import module-level, jamais de boot réseau, jamais de
touch sur logs/requests.db live (cf. test_phase0_contracts.py).
"""

import json

import pytest
from fastapi.testclient import TestClient

import opencode as oc


class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {
            "model": "jev-1.13-free",
            "answers": {"is_urgent": {"type": "noul", "noul": 0.96}},
            "usage": {"input_tokens": 290, "output_tokens": 23},
        }
        raw = json.dumps(self._payload).encode()
        self.content = raw
        self.text = raw.decode()

    def json(self):
        return self._payload


class _FakeClient:
    """Remplace oc._ensure_http_client() : capture l'appel, rend 200."""

    def __init__(self):
        self.calls = []

    async def post(self, endpoint, content=None, headers=None):
        self.calls.append({"endpoint": endpoint, "content": content, "headers": headers})
        return _FakeResp()


@pytest.fixture()
def client(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(oc, "_ensure_http_client", lambda: fake)
    # Silence DB + logs (pas de touch requests.db live)
    monkeypatch.setattr(oc, "_save_and_log_request", _noop_save())
    monkeypatch.setattr(oc, "_log_and_save_error", _noop_err())
    monkeypatch.setattr(oc, "_update_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    c = TestClient(oc.app)
    c._systemone_fake = fake
    return c


def _noop_save():
    async def _save(*a, **k):
        return None

    return _save


def _noop_err():
    async def _err(*a, **k):
        return None

    return _err


def _body(model="jev-1.13-free"):
    return {
        "model": model,
        "state": "My payments have failed for three days. Please help now.",
        "questions": {
            "is_urgent": {"type": "noul", "instructions": "Does this request require urgent attention?"}
        },
    }


def test_systemone_free_passthrough_anonymous(client):
    """Free → endpoint systemone, Bearer public, corps relayé tel quel."""
    r = client.post("/v1/systemone", json=_body("jev-1.13-free"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["model"] == "jev-1.13-free"
    assert data["answers"]["is_urgent"]["noul"] == 0.96

    fake = client._systemone_fake
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["endpoint"] == "https://opencode.ai/zen/v1/systemone"
    assert call["headers"]["Authorization"] == "Bearer public"
    wire = json.loads(call["content"])
    assert wire["model"] == "jev-1.13-free"
    assert wire["state"].startswith("My payments")
    assert wire["questions"]["is_urgent"]["type"] == "noul"


def test_systemone_accepts_dict_state_passthrough(client):
    """state dict → accepté et relayé TEL QUEL (pas de 400 proxy, pas de
    stringification : l'amont tranche du format)."""
    payload = _body("jev-1.13-free")
    payload["state"] = {"task": "triage", "context": "payments failing"}
    r = client.post("/v1/systemone", json=payload)
    assert r.status_code == 200, r.text

    fake = client._systemone_fake
    assert len(fake.calls) == 1
    wire = json.loads(fake.calls[0]["content"])
    assert wire["state"] == {"task": "triage", "context": "payments failing"}


def test_systemone_rejects_empty_dict_state(client):
    """state dict vide → 400 (jamais relayé à l'amont)."""
    bad = _body()
    bad["state"] = {}
    r = client.post("/v1/systemone", json=bad)
    assert r.status_code == 400
    assert client._systemone_fake.calls == []


def test_systemone_rejects_chat_payload(client):
    """Pas de state/questions → 400 (jamais relayé à l'amont)."""
    r = client.post("/v1/systemone", json={"model": "jev-1.13-free", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400
    assert "state" in r.json()["error"]["message"]
    assert client._systemone_fake.calls == []


def test_systemone_rejects_bad_question_type(client):
    bad = _body()
    bad["questions"] = {"q": {"type": "generate", "instructions": "write a poem"}}
    r = client.post("/v1/systemone", json=bad)
    assert r.status_code == 400
    assert "noul" in r.json()["error"]["message"]
    assert client._systemone_fake.calls == []


def test_systemone_rejects_unknown_model(client):
    r = client.post("/v1/systemone", json=_body("nope-9000"))
    assert r.status_code == 404
    assert client._systemone_fake.calls == []


def test_systemone_rejects_non_systemone_model(client):
    """Un LLM de chat routé ici par erreur → 400 garde (pas de fuite
    {state, questions} vers /chat/completions)."""
    r = client.post("/v1/systemone", json=_body("glm-5.1"))
    assert r.status_code == 400
    assert "SystemOne" in r.json()["error"]["message"]
    assert client._systemone_fake.calls == []


def test_systemone_upstream_error_relayed(client):
    """Statut amont non-200 → relayé tel quel (ex. 402 solde vide)."""
    fake = client._systemone_fake

    async def _post_402(endpoint, content=None, headers=None):
        fake.calls.append({"endpoint": endpoint, "content": content, "headers": headers})
        return _FakeResp(
            status=402,
            payload={"error": {"type": "server_error", "message": "Upstream request failed: Insufficient account funds"}},
        )

    fake.post = _post_402
    r = client.post("/v1/systemone", json=_body("jev-1.13-free"))
    assert r.status_code == 402
    assert "funds" in r.json()["error"]["message"]


def test_systemone_config_endpoints():
    """config.yaml : les deux ids jev pointent sur /v1/systemone."""
    assert oc.get_model_config("jev-1.13")["endpoint"].endswith("/v1/systemone")
    assert oc.get_model_config("jev-1.13-free")["endpoint"].endswith("/v1/systemone")


def test_systemone_capabilities_are_decision_only():
    """Jev : pas de génération de texte ni de tool calls (doc TypeSafe)."""
    from config import get_model_capabilities

    caps = get_model_capabilities("jev-1.13")
    assert caps["toolcall"] is False
    assert caps["reasoning"] is False
    assert caps["output"] == ["structured"]


def _client_with_mocks(monkeypatch, *, free_fn, paid_fn=None, strict_free=False):
    """TestClient systemone avec jambe free/paid mockées + clé payante traçée."""
    import opencode as oc

    calls = {"free": [], "paid": [], "keys": []}

    async def _free(endpoint, body, headers, forced_pool=None, req_id=None):
        calls["free"].append({"endpoint": endpoint, "body": dict(body), "headers": dict(headers)})
        return await free_fn(endpoint, body, headers)

    async def _paid(endpoint, body, headers, proto):
        calls["paid"].append({"endpoint": endpoint, "body": dict(body)})
        if paid_fn is None:
            raise AssertionError("jambe paid appelée alors qu'elle ne devrait pas l'être")
        return await paid_fn(endpoint, body, headers, proto)

    def _key():
        calls["keys"].append(1)
        return {"api_key": "sk-test", "go_workspace_id": "", "go_auth_cookie": ""}

    monkeypatch.setattr(oc, "_systemone_free_via_pool", _free)
    monkeypatch.setattr(oc, "_do_request_with_retry", _paid)
    monkeypatch.setattr(oc, "get_next_api_key", _key)
    monkeypatch.setitem(oc.IP_ROTATION, "strict_free", strict_free)
    monkeypatch.setattr(oc, "_save_and_log_request", _noop_save())
    monkeypatch.setattr(oc, "_log_and_save_error", _noop_err())
    monkeypatch.setattr(oc, "_update_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    c = TestClient(oc.app)
    c._sysone_calls = calls
    return c


def test_systemone_paid_routes_free_first(monkeypatch):
    """jev-1.13 (payant) → free-first : succès free, ZÉRO clé payante consommée."""

    async def _free_ok(endpoint, body, headers):
        assert headers.get("Authorization") == "Bearer public"
        return _FakeResp(
            status=200,
            payload={
                "model": "jev-1.13-free",
                "answers": {"is_urgent": {"type": "noul", "noul": 0.91}},
                "usage": {"input_tokens": 285, "output_tokens": 23},
            },
        )

    c = _client_with_mocks(monkeypatch, free_fn=_free_ok, paid_fn=None, strict_free=False)
    r = c.post("/v1/systemone", json=_body("jev-1.13"))
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "jev-1.13-free"
    assert len(c._sysone_calls["free"]) == 1
    assert c._sysone_calls["free"][0]["body"]["model"] == "jev-1.13-free"
    assert c._sysone_calls["paid"] == []
    assert c._sysone_calls["keys"] == []


def test_systemone_paid_free_429_falls_back_to_paid(monkeypatch):
    """Free 429 + fallback-payant autorisé (strict_free OFF) → jambe paid."""

    async def _free_429(endpoint, body, headers):
        return _FakeResp(status=429, payload={"error": {"type": "rate_limit", "message": "quota exceeded"}})

    async def _paid_ok(endpoint, body, headers, proto):
        assert body["model"] == "jev-1.13"
        assert headers.get("Authorization", "").startswith("Bearer sk-test")
        return (
            _FakeResp(
                status=200,
                payload={
                    "model": "jev-1.13",
                    "answers": {"is_urgent": {"type": "noul", "noul": 0.5}},
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            ),
            headers,
        )

    c = _client_with_mocks(monkeypatch, free_fn=_free_429, paid_fn=_paid_ok, strict_free=False)
    r = c.post("/v1/systemone", json=_body("jev-1.13"))
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "jev-1.13"
    assert len(c._sysone_calls["free"]) == 1
    assert len(c._sysone_calls["paid"]) == 1


def test_systemone_paid_free_429_strict_free_refuses(monkeypatch):
    """Free 429 + strict_free ON → 429 relayé, ZÉRO jambe paid."""

    async def _free_429(endpoint, body, headers):
        return _FakeResp(status=429, payload={"error": {"type": "rate_limit", "message": "quota exceeded"}})

    c = _client_with_mocks(monkeypatch, free_fn=_free_429, paid_fn=None, strict_free=True)
    r = c.post("/v1/systemone", json=_body("jev-1.13"))
    assert r.status_code == 429, r.text
    assert len(c._sysone_calls["free"]) == 1
    assert c._sysone_calls["paid"] == []
    assert c._sysone_calls["keys"] == []


class _FakeStation:
    def __init__(self, num, ip):
        self._station = num
        self.current_ip = ip
        self.pid = None
        self.socks5_url = f"socks5://127.0.0.1:108{num}"


class _FakePool:
    enabled = True
    socks5_mode = False

    def __init__(self, stations):
        self._stations = stations

    async def on_request(self, forced_pool=None):
        return None, self._stations[0]

    def pick_candidates(self, forced_pool=None):
        return list(self._stations)


def _hedge_mocks(monkeypatch, *, hedge_resp, should_hedge=True):
    """Isole _systemone_free_via_pool : pool + hedge fakes, séquentiel interdit."""
    import opencode as oc

    calls = {"hedged": [], "sequential": []}

    async def _hedged(cands, body, headers, endpoint, forced_pool=None):
        calls["hedged"].append({"n": len(cands), "model": body.get("model")})
        return hedge_resp[0], hedge_resp[1]

    async def _no_sequential(*a, **k):
        calls["sequential"].append(1)
        raise AssertionError("boucle séquentielle appelée alors que le hedge a répondu")

    monkeypatch.setattr(oc, "_free_ip_pool", _FakePool([_FakeStation(1, "1.2.3.4"), _FakeStation(2, "5.6.7.8")]))
    monkeypatch.setattr(oc, "_free_proxy_mode", lambda: "vpn")
    monkeypatch.setattr(oc, "_free_parallel_should_hedge", lambda body, forced_pool=None: should_hedge)
    monkeypatch.setattr(oc, "_hedged_fetch", _hedged)
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", _no_sequential)
    monkeypatch.setattr(oc, "_mark_free_stations_429", lambda *a, **k: calls.setdefault("marked", []).append(1))
    monkeypatch.setattr(oc, "_log_free_model_usage", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    return calls


async def test_systemone_free_hedge_winner_200_no_sequential(monkeypatch):
    """Parité chat : hedge 200 → passthrough, boucle séquentielle sautée."""
    import opencode as oc

    winner = _FakeStation(2, "5.6.7.8")
    ok = _FakeResp(
        status=200,
        payload={
            "model": "jev-1.13-free",
            "answers": {"is_urgent": {"type": "noul", "noul": 0.88}},
            "usage": {"input_tokens": 285, "output_tokens": 23},
        },
    )
    calls = _hedge_mocks(monkeypatch, hedge_resp=(ok, winner))
    resp = await oc._systemone_free_via_pool(
        "https://opencode.ai/zen/v1/systemone",
        _body("jev-1.13-free"),
        {"Authorization": "Bearer public", "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert len(calls["hedged"]) == 1
    assert calls["hedged"][0]["model"] == "jev-1.13-free"
    assert calls["sequential"] == []


async def test_systemone_free_hedge_429_filet_relayed(monkeypatch):
    """Parité chat : hedge 429 (toutes stations) → 429 relayé, pas de retry."""
    import opencode as oc

    winner = _FakeStation(1, "1.2.3.4")
    rate = _FakeResp(status=429, payload={"error": {"type": "rate_limit", "message": "quota exceeded"}})
    calls = _hedge_mocks(monkeypatch, hedge_resp=(rate, winner))
    resp = await oc._systemone_free_via_pool(
        "https://opencode.ai/zen/v1/systemone",
        _body("jev-1.13-free"),
        {"Authorization": "Bearer public", "Content-Type": "application/json"},
    )
    assert resp.status_code == 429
    assert len(calls["hedged"]) == 1
    assert calls["sequential"] == []
