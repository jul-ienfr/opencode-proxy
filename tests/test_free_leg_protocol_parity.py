"""test_free_leg_protocol_parity.py — [lot L1/P1] parité de protocole, jambe free.

DÉFAUT MESURÉ (11/09, proxy :4000) — avant ce correctif, sur ``/v1/messages``
avec un modèle routé dont ``get_model_config()["protocol"] == "anthropic"`` **et**
dont l'équivalent free utilise ``/chat/completions`` (cas vivant : route ``haiku``
→ ``minimax-m2.5`` → ``mimo-v2.5-free``) :

* le corps Anthropic partait **verbatim** vers un endpoint Chat — un endpoint Chat
  ne lit pas le ``system`` top-level, donc le **system prompt était perdu**, et
  ``tools[].input_schema`` ne remplace pas ``tools[].function.parameters`` ;
* la réponse Chat (``choices``) était rendue **verbatim** au client Anthropic,
  sous un **HTTP 200** — ni ``content``, ni ``stop_reason``, ni
  ``usage.input_tokens``.

Repère de contrôle : la route ``opus`` (→ ``kimi-k2.6``, ``protocol: openai``)
utilise **le même** modèle free ``mimo-v2.5-free`` et le même endpoint, et elle
fonctionnait — c'est la déclaration de protocole du modèle payant, pas le modèle
free, qui déclenchait le défaut.

Ces tests verrouillent les deux sens de la conversion, hermétiquement (aucun
réseau) : le seam ``_do_free_request_curl_cffi`` est remplacé.
"""

import json

import pytest

import config.settings as st
import opencode as oc

PAID_ANTHROPIC = "claude-haiku-4-5"
FREE_CHAT = "mimo-v2.5-free"
FREE_RESPONSES = "muse-spark-1.2-contributor-free"

LONG_TOOL_NAME = "mcp__plugin_very_long_tool_name_exceeding_sixty_four_characters_witness"


# ── harnais ────────────────────────────────────────────────────────
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
        self.rotated = []
        self.requests = 0

    async def on_request(self):
        self.requests += 1
        best = self._best_station()
        return best.socks5_url if best else None, best

    def _best_station(self):
        for st_ in self._stations:
            if st_.status == "connected":
                return st_
        return None

    def _best_station_excluding_many(self, excluded, forced_pool=None):
        for st_ in self._stations:
            if st_ in excluded:
                continue
            if st_.status == "connected":
                return st_
        return None

    def _station_usable(self, st_, exclude_approaching=False, forced_pool=None):
        return st_.status == "connected"

    def pick_candidates(self, forced_pool=None):
        return [s for s in self._stations if s.status == "connected"]

    def on_quota_exhausted(self, station):
        self.rotated.append(station)

    def note_hedge_winner(self, winner, primary=None):
        pass


class _Resp:
    """Réponse fake : corps Chat 200, comme l'upstream free réel."""

    def __init__(self, payload, status=200):
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self._payload = payload
        self.content = json.dumps(payload).encode()
        self.text = self.content.decode()
        self._resp = self

    def json(self):
        return self._payload


def _chat_reply(tool_name=None, text="BONJOUR"):
    msg = {"role": "assistant", "content": text if not tool_name else None}
    if tool_name:
        msg["tool_calls"] = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": tool_name, "arguments": '{"a": 1}'},
            }
        ]
    return {
        "id": "chatcmpl-witness",
        "object": "chat.completion",
        "model": FREE_CHAT,
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_name else "stop", "message": msg}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
    }


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


def _anthropic_body(**over):
    body = {
        "model": PAID_ANTHROPIC,
        "max_tokens": 100,
        "system": "SYS-A-NE-PAS-PERDRE",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "salut"}]}],
        "tools": [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}],
    }
    body.update(over)
    return body


class _Seam:
    """Capture le corps réellement transmis et renvoie une réponse Chat."""

    def __init__(self, payload):
        self.payload = payload
        self.bodies = []

    async def __call__(self, body, headers, proxy_url=None, station=None, endpoint=None, **kw):
        self.bodies.append(json.loads(json.dumps(body)))
        return _Resp(self.payload)

    @property
    def wire(self):
        assert self.bodies, "le seam réseau n'a jamais été atteint — test non concluant"
        return self.bodies[0]


async def _run_free(monkeypatch, protocol, model_id, free_model, body, payload):
    oc.FREE_MODEL_MAP.clear()
    oc.FREE_MODEL_MAP[model_id] = free_model
    seam = _Seam(payload)
    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", seam)
    out = await oc._try_free_model_first(dict(body), {}, protocol, model_id)
    return seam, out


# ── 1. ALLER : le corps part en forme CHAT ─────────────────────────
@pytest.mark.asyncio
async def test_p1_body_is_converted_to_chat_shape(free_env, monkeypatch):
    """Le corps Anthropic ne doit JAMAIS partir verbatim vers un endpoint Chat."""
    seam, out = await _run_free(monkeypatch, "anthropic", PAID_ANTHROPIC, FREE_CHAT, _anthropic_body(), _chat_reply())
    wire = seam.wire

    assert "system" not in wire, (
        "le `system` top-level Anthropic est resté sur le fil : un endpoint Chat ne le lit "
        "pas, donc le system prompt serait perdu"
    )
    assert wire["messages"][0]["role"] == "system", "le system doit devenir un message system"
    assert "SYS-A-NE-PAS-PERDRE" in json.dumps(wire["messages"][0]), "texte du system perdu"
    assert isinstance(wire["messages"][1]["content"], str), (
        "content en blocs = forme Anthropic ; un endpoint Chat attend une chaîne"
    )
    assert "function" in wire["tools"][0] and "parameters" in wire["tools"][0]["function"], (
        "tools[] n'a pas été traduit en forme Chat (function.parameters)"
    )
    assert out is not None


# ── 2. RETOUR : la réponse revient en forme ANTHROPIC ──────────────
@pytest.mark.asyncio
async def test_p1_response_is_converted_back_to_anthropic(free_env, monkeypatch):
    """Un client Anthropic ne doit jamais recevoir `choices` sous un HTTP 200."""
    _seam, out = await _run_free(monkeypatch, "anthropic", PAID_ANTHROPIC, FREE_CHAT, _anthropic_body(), _chat_reply())
    assert out is not None
    resp = out[0]
    data = json.loads(resp.content)

    assert "choices" not in data, "forme Chat rendue à un client Anthropic"
    assert data.get("type") == "message" and data.get("role") == "assistant"
    assert data["content"][0]["type"] == "text"
    assert data["content"][0]["text"] == "BONJOUR"
    assert data["stop_reason"], "stop_reason absent de la réponse Anthropic"
    assert data["usage"]["input_tokens"] == 11, "usage Anthropic non renseigné"
    assert data["usage"]["output_tokens"] == 3


# ── 3. Les noms d'outils longs survivent au double trajet ──────────
@pytest.mark.asyncio
async def test_p1_long_tool_name_sanitized_on_wire_and_restored_to_client(free_env, monkeypatch):
    """Nom > 64 car. : raccourci sur le fil (contrat Chat), restauré pour le client."""
    body = _anthropic_body(
        tools=[
            {
                "name": LONG_TOOL_NAME,
                "description": "d",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]
    )
    # La réponse Chat référence le nom réellement envoyé : on le lit sur le fil,
    # ce qui prouve au passage que c'est bien ce nom qui circule.
    oc.FREE_MODEL_MAP.clear()
    oc.FREE_MODEL_MAP[PAID_ANTHROPIC] = FREE_CHAT
    seen = {}

    class _SeamTool:
        async def __call__(self, bod, headers, proxy_url=None, station=None, endpoint=None, **kw):
            seen["name"] = bod["tools"][0]["function"]["name"]
            return _Resp(_chat_reply(tool_name=seen["name"]))

    monkeypatch.setattr(oc, "_do_free_request_curl_cffi", _SeamTool())
    out = await oc._try_free_model_first(body, {}, "anthropic", PAID_ANTHROPIC)

    assert len(seen["name"]) <= 64, f"nom d'outil non raccourci sur le fil : {len(seen['name'])} car."
    assert seen["name"] != LONG_TOOL_NAME
    assert out is not None
    data = json.loads(out[0].content)
    blk = [b for b in data["content"] if b["type"] == "tool_use"][0]
    assert blk["name"] == LONG_TOOL_NAME, "le client reçoit le nom raccourci : il ne reconnaît pas son propre outil"


# ── 4. Témoin de non-régression : le client Chat reste intact ──────
@pytest.mark.asyncio
async def test_chat_client_path_is_unchanged(free_env, monkeypatch):
    """Un client Chat vers un endpoint Chat doit rester un passthrough (mesuré 200)."""
    chat_body = {"model": "mimo-v2.5", "messages": [{"role": "user", "content": "salut"}]}
    seam, out = await _run_free(monkeypatch, "openai", "mimo-v2.5", FREE_CHAT, chat_body, _chat_reply())
    assert seam.wire.get("messages"), "corps Chat altéré"
    assert "system" not in seam.wire
    assert out is not None
    data = json.loads(out[0].content)
    assert "choices" in data, (
        "un client Chat doit recevoir la forme Chat — c'est le témoin qui prouve que le "
        "correctif ne touche pas ce chemin"
    )


# ── 5. Témoin de portée : quels modèles sont réellement exposés ? ──
def test_scope_of_anthropic_protocol_models_with_chat_free_endpoint():
    """Documente la portée exacte du défaut mesuré, et la verrouille.

    Le défaut ne peut frapper qu'un modèle **à la fois** `protocol: anthropic` et
    présent dans ``FREE_MODEL_MAP`` avec un équivalent free hors ``/responses``.
    C'est ce qui explique le contrôle live : la route ``opus`` (``protocol: openai``)
    utilisait le **même** modèle free sans être touchée.
    """
    concerned = {
        paid: free
        for paid, free in oc.FREE_MODEL_MAP.items()
        if st.get_model_config(paid).get("protocol") == "anthropic" and "/responses" not in st._free_endpoint_for(free)
    }
    if not oc.FREE_MODEL_MAP:
        pytest.skip("FREE_MODEL_MAP vide dans cet environnement — portée non évaluable")
    # Cas vivant mesuré sur le proxy : route `haiku` → minimax-m2.5 → mimo-v2.5-free.
    if "minimax-m2.5" in oc.FREE_MODEL_MAP:
        assert "minimax-m2.5" in concerned, (
            "minimax-m2.5 (route haiku par défaut) n'est plus détecté comme exposé : "
            "soit la config a changé, soit la condition de portée a dérivé"
        )
        assert "/responses" not in st._free_endpoint_for(oc.FREE_MODEL_MAP["minimax-m2.5"])
    # Les modèles exposés doivent tous avoir un endpoint Chat (donc conversion requise).
    for paid, free in concerned.items():
        assert st._free_endpoint_for(free), f"{paid}: endpoint free introuvable"
