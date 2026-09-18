"""test_official_client_parity.py — la jambe free recopie le client OpenCode officiel.

Référence (vérifiée 2026-09-18, sst/opencode == fork anomalyco/opencode) :
  * client  : packages/opencode/src/session/llm/request.ts (jeu des headers)
  * wire    : captures octet-exact du VRAI client 1.18.31 redirigé vers un
              captureur local (baseURL http://) : 7 headers dans l'ordre
              Authorization, Content-Type, User-Agent, x-opencode-client/
              project/request/session, puis transport Bun (Connection,
              Accept, Host, AE, Content-Length) ; ClientHello Bun rejoué
              (ciphers/order/sigalgs/ALPN http/1.1, cf. _free_fp_override)
  * gate    : bisection live 2026-09-18 — la jambe anonyme exige stream:true
              + tools[] contenant « bash » ET « read » (case-sensitive,
              schemas libres), sinon 403 FreeTierError
  * IDs     : packages/opencode/src/id/id.ts (algorithme ses_/msg_)
  * gateway : packages/console/app/src/routes/zen/util/handler.ts
              (lecture des 4 x-opencode-*, sticky routing sur la session,
              « public » → anonyme, quota trial PAR IP)

Règles verrouillées ici :
  1. Jeu exact : 7 headers, ni plus ni moins — AUCUN header navigateur
     (Accept-Language, sec-ch-ua, Cookie…) même si le profil d'identité en
     porte (la diversité navigateur ne sert que le chemin geo), et PAS
     d'Accept explicite (transport Bun l'ajoute en 9ᵉ ; explicite = trié
     premier = copie trahie).
  2. Ordre wire réel : Authorization, Content-Type, User-Agent,
     x-opencode-client/project/request/session (UA par endpoint : 4.0.23 en
     chat, 4.0.40 en responses — deux bundles ai-sdk mesurés).
  3. x-opencode-request STABLE par tâche (= par message logique) : deux
     envois d'une même tâche partagent le msg_, deux tâches ont des msg_
     différents (sémantique request.ts : l'ID du message utilisateur).
  4. Face réseau = replay Bun (preset porteur OCSP/SCT + ja3 + sigalgs x9 +
     tls_grease False + H1 + default_headers False, cf. _free_fp_override),
     PAS une face navigateur en rotation ; clé de pool `<proxy>|bun`.
  5. Grille body : stream forcé + tools bash/read ajoutés (shims, idempotent,
     entrée jamais mutée) ; prompt_cache_key=ses_ sur responses ; collectes
     SSE→JSON pour les jambes non-stream.
  6. IDs byte-exacts id.ts : 30 car., 12 hex + 14 base62, ascending monotone
     à timestamp extractible, descending inversé.

Hermétique : aucun réseau, logs/ redirigé vers tmp_path, curl_cffi faké.
"""

import asyncio
import json
import re

import pytest

import opencode as oc

MSG_RE = re.compile(r"msg_[0-9a-f]{12}[0-9A-Za-z]{14}")
SES_RE = re.compile(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}")

EXPECTED_ORDER = [
    "Authorization",
    "Content-Type",
    "User-Agent",
    "x-opencode-client",
    "x-opencode-project",
    "x-opencode-request",
    "x-opencode-session",
]


@pytest.fixture
def official_env(monkeypatch, tmp_path):
    """État officiel hermétique : session fichier → tmp, caches vidés."""
    monkeypatch.setattr(oc, "_FREE_SESSION_FILE", str(tmp_path / "_free_session_id"))
    oc._FREE_SESSION_CACHE = None
    oc._FREE_SESSION_TS = 0.0
    oc._free_msg_id.set(None)
    yield oc
    oc._free_msg_id.set(None)


def test_header_set_exact(official_env):
    """7 headers officiels, ni plus ni moins (aucun header navigateur,
    et PAS d'Accept explicite — le transport Bun l'ajoute en 9ᵉ position,
    un Accept explicite serait trié premier et trahirait la copie)."""
    h = oc._official_free_headers()
    assert list(h.keys()) == EXPECTED_ORDER
    lower = {k.lower() for k in h}
    for banned in (
        "accept",
        "accept-language",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "cookie",
        "x-api-key",
        "x-request-id",
        "anthropic-version",
        "x-stainless-arch",
        "x-parent-session-id",
    ):
        assert banned not in lower, f"browser/client header {banned!r} leaked to free"


def test_header_values_official(official_env):
    """Valeurs exactes du client officiel (captures desktop v1.18.31)."""
    h = oc._official_free_headers()
    assert h["User-Agent"] == oc._OPENCODE_OFFICIAL_UA
    assert h["User-Agent"] == "opencode/1.18.31 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14"
    assert h["Authorization"] == "Bearer public"
    assert h["x-opencode-client"] == "desktop"
    assert h["x-opencode-project"] == "global"
    assert h["Content-Type"] == "application/json"
    assert "Accept" not in h, "Accept must stay transport-added (9th), never explicit"
    assert MSG_RE.fullmatch(h["x-opencode-request"]), h["x-opencode-request"]
    assert SES_RE.fullmatch(h["x-opencode-session"]), h["x-opencode-session"]


def test_ua_per_endpoint(official_env):
    """Le client envoie provider-utils/4.0.40 sur /responses (mesuré)."""
    chat = oc._official_free_headers("https://opencode.ai/zen/v1/chat/completions")
    resp = oc._official_free_headers("https://opencode.ai/zen/v1/responses")
    assert chat["User-Agent"] == oc._OPENCODE_OFFICIAL_UA
    assert "4.0.23" in chat["User-Agent"]
    assert resp["User-Agent"] == oc._OPENCODE_OFFICIAL_UA_RESPONSES
    assert "4.0.40" in resp["User-Agent"]
    # Même jeu/ordre par ailleurs
    assert list(resp) == EXPECTED_ORDER


def test_msg_stable_same_task_new_after_reset(official_env):
    """Même tâche → même msg_ (retries/hedges d'un même message)."""
    h1 = oc._official_free_headers()
    h2 = oc._official_free_headers()
    assert h1["x-opencode-request"] == h2["x-opencode-request"]
    assert h1["x-opencode-session"] == h2["x-opencode-session"]
    # Reset (nouvelle requête) → nouveau msg_
    oc._free_msg_id.set(None)
    h3 = oc._official_free_headers()
    assert h3["x-opencode-request"] != h1["x-opencode-request"]


async def test_msg_distinct_across_tasks(official_env):
    """Deux tâches (= deux messages logiques) → deux msg_ distincts."""

    async def _send():
        return oc._official_free_headers()["x-opencode-request"]

    oc._free_msg_id.set(None)
    t1 = asyncio.create_task(_send())
    t2 = asyncio.create_task(_send())
    m1, m2 = await asyncio.gather(t1, t2)
    assert MSG_RE.fullmatch(m1) and MSG_RE.fullmatch(m2)
    assert m1 != m2


def test_hostile_profile_extras_never_reach_free(official_env):
    """Un profil diversité navigateur (Accept-Language, sec-ch-ua…) ne peut
    pas polluer la jambe free : le jeu officiel est fermé."""
    hostile = {
        "impersonate": "firefox144",
        "user_agent": None,
        "extra_headers": {
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
            "sec-ch-ua": '"Firefox";v="144", "Not/A)Brand";v="99"',
            "Cookie": "session=abc",
        },
    }
    h = oc._official_free_headers()
    lower = {k.lower(): v for k, v in h.items()}
    for k in hostile["extra_headers"]:
        assert k.lower() not in lower, f"profile extra {k!r} leaked to free"
    assert set(h.keys()) == set(EXPECTED_ORDER)


class _FakeCurlResp:
    status_code = 200
    headers = {}
    content = b'{"choices": [{"message": {"role": "assistant", "content": "echo"}}]}'

    async def aiter_lines(self):
        # [gate body] le non-stream force stream:true sur le wire : le fake
        # rejoue un mini-SSE que le collecteur reconstitue.
        yield ('data: {"id":"echo","object":"chat.completion.chunk","model":"m",'
               '"choices":[{"index":0,"delta":{"content":"echo"},'
               '"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1}}')
        yield "data: [DONE]"

    async def aclose(self):
        return None


class _FakeSlot:
    def __init__(self, post_log):
        self._post_log = post_log
        self.sess = self

    async def post(self, url, content=None, headers=None, timeout=None, **kwargs):
        self._post_log.append(
            {"url": url, "headers": dict(headers or {}), "content": content,
             "stream": kwargs.get("stream", False)}
        )
        return _FakeCurlResp()


class _FakePool:
    async def checkin(self, slot):
        return None

    async def evict(self, slot):
        return None


async def test_bun_replay_non_stream(official_env, monkeypatch):
    """Profil firefox144 + extras hostiles → la factory reçoit quand même le
    replay Bun (preset porteur + ja3 custom + sigalgs + H1), et les headers
    partent sans extras. Le corps non-stream est envoyé en stream forcé
    (grille) puis reconstitué : le statut reste 200."""
    seen = {}
    posts = []

    async def _fake_factory(proxy_url, impersonate, fp_override=None):
        seen["proxy_url"] = proxy_url
        seen["impersonate"] = impersonate
        seen["fp_override"] = fp_override
        return _FakePool(), _FakeSlot(posts)

    monkeypatch.setattr(oc, "_get_pooled_curl_session", _fake_factory)
    monkeypatch.setattr(
        oc,
        "_current_free_identity",
        lambda station=None: {
            "impersonate": "firefox144",
            "user_agent": None,
            "extra_headers": {"Accept-Language": "fr-FR,fr;q=0.9", "Cookie": "x=1"},
        },
    )
    monkeypatch.setattr(oc, "_ensure_curl_cffi", lambda: True)

    body = {"model": "mimo-v2.5-free", "messages": [{"role": "user", "content": "hi"}]}
    resp = await oc._do_free_request_curl_cffi(
        body, {"Authorization": "Bearer sk-ant-xxx"}, proxy_url=None, endpoint="http://127.0.0.1:9/free"
    )
    assert resp.status_code == 200
    # Replay Bun : la factory reçoit ("", override) — le preset chrome131 vit
    # DANS l'override comme simple porteur (émission OCSP/SCT), avec le ja3
    # Bun + sigalgs + H1 + headers navigateur neutralisés.
    assert seen["impersonate"] == "", seen
    fp = seen["fp_override"]
    assert fp is not None
    assert fp["impersonate"] == "chrome131"
    assert fp["ja3"] == oc._OPENCODE_JA3
    assert fp["ja3"].split(",")[0] == "771"
    assert fp["extra_fp"] == {
        "tls_signature_algorithms": list(oc._OPENCODE_SIG_ALGS),
        "tls_grease": False,
    }
    assert len(oc._OPENCODE_SIG_ALGS) == 9
    assert fp["http_version"] == "v1"  # Bun = HTTP/1.1 uniquement
    assert fp["default_headers"] is False
    assert len(posts) == 1
    # Corps non-stream → stream forcé sur le wire (grille)
    assert posts[0]["stream"] is True
    h = posts[0]["headers"]
    assert h["User-Agent"] == oc._OPENCODE_OFFICIAL_UA
    assert h["Authorization"] == "Bearer public"
    assert list(h.keys()) == EXPECTED_ORDER
    lower = {k.lower() for k in h}
    assert "accept-language" not in lower and "cookie" not in lower
    # Le SSE collecté donne un chat.completion avec le contenu du flux
    assert resp.json()["choices"][0]["message"]["content"] == "echo"


def test_kill_switch_preset(monkeypatch):
    """OPENCODE_TLS_IMPERSONATE posé → retour au preset (pas de ja3)."""
    monkeypatch.setattr(oc, "_OPENCODE_TLS_IMPERSONATE", "chrome131")
    imp, fp = oc._get_free_fp_kwargs()
    assert imp == "chrome131"
    assert fp is None


def test_free_fp_override_shape():
    """Contrat de la face Bun : preset porteur + ja3 SNI-first + sigalgs x9
    + H1 + pas de headers navigateur par défaut."""
    imp, fp = oc._get_free_fp_kwargs()
    assert imp == ""  # le preset vit DANS fp_override (clé pool = |bun)
    assert fp is not None
    assert fp["impersonate"] == "chrome131"  # porteur OCSP/SCT, headers neutralisés
    assert fp["default_headers"] is False
    ciphers, exts, curves, _formats = fp["ja3"].split(",")[1:]
    assert ciphers.split("-")[0] == "4865"  # TLS_AES_128_GCM_SHA256 en tête
    assert exts.split("-")[0] == "0"  # SNI en premier, comme Bun
    assert "16" in exts.split("-")  # ALPN présent (http/1.1 via http_version)
    assert curves == "29-23-24" and "4588" not in curves  # pas de GREASE
    assert fp["http_version"] == "v1"


def test_mint_oc_id_format():
    """30 car., 12 hex + 14 base62, dans les deux directions."""
    for prefix, direction in (("msg", "ascending"), ("ses", "descending")):
        v = oc._mint_oc_id(prefix, direction)
        assert len(v) == 30, v
        assert re.fullmatch(rf"{prefix}_[0-9a-f]{{12}}[0-9A-Za-z]{{14}}", v), v


def test_mint_oc_id_ascending_timestamp_roundtrip():
    """La partie hex code now_ms (extractible comme timestamp() côté client).

    Note fidèle à id.ts : seuls les 48 bits bas sont gardés (6 octets
    big-endian) — le round-trip exact ne vaut que pour les petits ms
    (comme dans les tests du client) ; les grands ms wrappent (test
    suivant), EXACTEMENT comme le TS d'origine.
    """
    ts = 5_000_000
    v = oc._mint_oc_id("msg", "ascending", ts_ms=ts)
    hexpart = v.split("_", 1)[1][:12]
    assert (int(hexpart, 16) - ts * 0x1000) & ((1 << 48) - 1) < 10000  # compteur/ms
    assert int(hexpart, 16) // 0x1000 == ts
    later = oc._mint_oc_id("msg", "ascending", ts_ms=ts + 1)
    assert later.split("_", 1)[1][:12] > hexpart


def test_mint_oc_id_large_ts_wraps_like_client():
    """Grands ms réels : wrap 48 bits + compteur — identique au TS."""
    ts = 1_700_000_000_123
    mask = (1 << 48) - 1
    a = oc._mint_oc_id("msg", "ascending", ts_ms=ts)
    b = oc._mint_oc_id("msg", "ascending", ts_ms=ts)
    ha, hb = int(a.split("_", 1)[1][:12], 16), int(b.split("_", 1)[1][:12], 16)
    assert hb - ha == 1  # compteur/ms partagé, même après wrap
    c = (ha - (ts * 0x1000 & mask)) & mask
    assert 1 <= c <= 10000, f"wrap inattendu: {a!r}"


def test_mint_oc_id_descending_inverted():
    """Sessions : plus récent = hex plus petit (tri descendant, id.ts)."""
    early = oc._mint_oc_id("ses", "descending", ts_ms=1_700_000_000_000)
    late = oc._mint_oc_id("ses", "descending", ts_ms=1_700_000_000_001)
    assert late.split("_", 1)[1][:12] < early.split("_", 1)[1][:12]


def test_oc_random_base62_charset():
    """14 car. base62 par défaut, longueurs respectées."""
    alphabet = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
    assert len(oc._oc_random_base62()) == 14
    assert len(oc._oc_random_base62(1)) == 1
    bulk = oc._oc_random_base62(500)
    assert set(bulk) <= alphabet
    # Non-dégénéré : plusieurs symboles distincts sur 500 tirages
    assert len(set(bulk)) > 10


def _chat_names(tools):
    return {t.get("function", {}).get("name") for t in tools if isinstance(t, dict)}


def _resp_names(tools):
    return {t.get("name") for t in tools if isinstance(t, dict)}


def test_free_wire_body_chat_appends_shim(official_env):
    """Corps chat sans tools → paire shim bash+read, stream forcé, sans
    muter l'entrée (idempotent au 2ᵉ passage)."""
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    wire, forced = oc._free_wire_body(body, force_stream=True)
    assert forced is True
    assert wire["stream"] is True
    assert _chat_names(wire["tools"]) == {"bash", "read"}
    assert "tools" not in body and body.get("stream") is not True  # entrée intacte
    wire2, forced2 = oc._free_wire_body(wire, force_stream=True)
    assert forced2 is False  # déjà conforme : inchangé
    assert _chat_names(wire2["tools"]) == {"bash", "read"}


def test_free_wire_body_chat_keeps_client_tools(official_env):
    """Tools client conservés + shim manquant ajouté (ex. Bash capitalisé
    côté Claude ne compte PAS : le gate est case-sensitive)."""
    mine = {"type": "function", "function": {"name": "Bash", "description": "x",
            "parameters": {"type": "object", "properties": {}}}}
    body = {"model": "m", "messages": [], "tools": [mine], "stream": True}
    wire, forced = oc._free_wire_body(body, force_stream=True)
    assert forced is False  # déjà stream:true
    names = _chat_names(wire["tools"])
    assert {"Bash", "bash", "read"} <= names
    assert wire["tools"][0] is mine  # ordre préservé, shims ajoutés en fin


def test_free_wire_body_responses_prompt_cache_key(official_env):
    """Corps responses : shim + prompt_cache_key = ses_ des headers."""
    body = {"model": "m", "input": [{"role": "user", "content": "hi"}]}
    wire, forced = oc._free_wire_body(body, force_stream=True)
    assert forced is True
    assert _resp_names(wire["tools"]) == {"bash", "read"}
    assert wire["prompt_cache_key"] == oc._free_session_id()
    assert wire["prompt_cache_key"].startswith("ses_")


def test_free_wire_body_passthrough():
    """Non-dict, ni-chat-ni-responses, ou déjà conforme → inchangé."""
    assert oc._free_wire_body(b"raw") == (b"raw", False)
    assert oc._free_wire_body({"model": "m"}) == ({"model": "m"}, False)
    full = {"model": "m", "messages": [],
            "tools": [{"type": "function", "function": {"name": n}} for n in ("bash", "read")],
            "stream": True}
    wire, forced = oc._free_wire_body(full, force_stream=True)
    assert forced is False and wire is full


def test_collect_chat_completion():
    """Chunks SSE chat → chat.completion JSON (contenu concaténé + usage)."""
    lines = [
        'data: {"choices":[{"delta":{"role":"assistant","content":"po"}}]}',
        'data: {"choices":[{"delta":{"content":"ng"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":2,"completion_tokens":1}}',
        "data: [DONE]",
        "",
        ": keep-alive",
    ]
    data = oc._collect_chat_completion(lines, "mimo-v2.5-free")
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "pong"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"] == {"prompt_tokens": 2, "completion_tokens": 1}
    assert oc._collect_chat_completion([], "m") is None
    assert oc._collect_chat_completion(["data: [DONE]"], "m") is None


def test_collect_responses_object():
    """Events SSE responses → completed.response repris tel quel, sinon
    synthèse depuis les deltas output_text."""
    full = [
        'data: {"type":"response.created","response":{"id":"r1"}}',
        'data: {"type":"response.output_text.delta","delta":"po"}',
        'data: {"type":"response.output_text.delta","delta":"ng"}',
        ('data: {"type":"response.completed","response":{"id":"r1","object":"response",'
         '"output":[{"type":"message","content":[{"type":"output_text","text":"pong"}]}]}}'),
    ]
    data = oc._collect_responses_object(full, "m")
    assert data["id"] == "r1"
    assert data["output"][0]["content"][0]["text"] == "pong"
    short = [
        'data: {"type":"response.output_text.delta","delta":"po"}',
        'data: {"type":"response.output_text.delta","delta":"ng"}',
    ]
    data2 = oc._collect_responses_object(short, "m")
    assert data2["output"][0]["content"][0]["text"] == "pong"
    assert oc._collect_responses_object([], "m") is None


class _FakeHttpxStreamResp:
    """Réponse httpx stream double : lignes SSE scriptées."""

    def __init__(self, status=200, lines=(), headers=None):
        self.status_code = status
        self.headers = dict(headers or {"content-type": "text/event-stream"})
        self._lines = list(lines)
        self.request = None

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b"".join(l.encode() + b"\n" for l in self._lines)


class _FakeHttpxStreamClient:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        resp = self.resp

        class _Ctx:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *a):
                return False

        return _Ctx()


CHAT_SSE = [
    'data: {"choices":[{"delta":{"content":"po"},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{"content":"ng"},"finish_reason":"stop"}]}',
    "data: [DONE]",
]


async def test_do_free_direct_request_collects(official_env, monkeypatch):
    """Jambe directe : stream forcé + collecte → httpx.Response 200 JSON,
    headers de requête renvoyés (contrat _do_request_with_retry)."""
    fake = _FakeHttpxStreamClient(_FakeHttpxStreamResp(200, CHAT_SSE))
    monkeypatch.setattr(oc, "_ensure_http_client", lambda: fake)
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    req_h = {"Authorization": "Bearer public"}
    resp, resp_headers = await oc._do_free_direct_request("https://h/zen/v1/chat/completions", body, req_h)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "pong"
    assert resp_headers == req_h
    # Corps wire : stream forcé + shim tools, entrée intacte
    sent = fake.calls[0][2]

    wire = json.loads(sent["content"].decode())
    assert wire["stream"] is True
    assert _chat_names(wire["tools"]) == {"bash", "read"}
    assert "tools" not in body


async def test_do_free_direct_request_passthrough_error(official_env, monkeypatch):
    """Non-200 amont → repassé tel quel (mapping 403/429 aval inchangé)."""
    fake = _FakeHttpxStreamClient(_FakeHttpxStreamResp(403, ['{"error": "x"}']))
    monkeypatch.setattr(oc, "_ensure_http_client", lambda: fake)
    resp, _rh = await oc._do_free_direct_request(
        "https://h/zen/v1/chat/completions", {"model": "m", "messages": []}, {}
    )
    assert resp.status_code == 403


async def test_do_free_direct_request_empty_raises(official_env, monkeypatch):
    """SSE vide → UpstreamError 502 (repli paid, comme avant)."""
    fake = _FakeHttpxStreamClient(_FakeHttpxStreamResp(200, ["data: [DONE]"]))
    monkeypatch.setattr(oc, "_ensure_http_client", lambda: fake)
    with pytest.raises(oc.UpstreamError):
        await oc._do_free_direct_request(
            "https://h/zen/v1/chat/completions", {"model": "m", "messages": []}, {}
        )
