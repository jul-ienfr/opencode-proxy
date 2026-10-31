"""Filet de securite de la jambe free : modele de PORTE, pas liste d'en-tetes.

Historique (a lire avant de « corriger » ce fichier)
----------------------------------------------------
La version precedente verrouillait une liste de 12 en-tetes mesuree au
mitmproxy sur le client 1.18.31. Ce test est devenu un piege : il figeait un
wire que le client n'emet plus (le quartet `x-opencode-client/project/
request/session` a disparu), et il aurait fait echouer tout alignement
legitime.

Ce qu'on verrouille desormais, c'est le **modele de la porte** mesure le
2026-10-20 (`docs/AUDIT_ALIGNEMENT_CLIENT_OFFICIEL_2026-10-20.md` §4.2) :

    Gate = EgressReputation(ip) ^ SessionIdentity(headers) ^ BodyShape(body)

  * `EgressReputation` n'est pas testable hors tunnel (on ne teste donc que
    la *forme* des invariants qui en dependent : tunnel obligatoire) ;
  * `SessionIdentity` : au moins un en-tete de session reconnu ;
  * `BodyShape` : `stream: true` ET un outil `read` realiste.

Et on verifie explicitement les **non-facteurs** mesures, pour qu'on ne les
reintroduise pas « par prudence » : la version d'UA ne discrimine pas, et le
quartet historique n'est pas exige.

Le wire de reference (binaire officiel 1.19.3, capture locale) :
`_research/oc-1.19.3/capture.jsonl` — procedure de re-mesure dans
`_research/oc-1.19.3/README.md`.
"""

import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, ".")
import opencode as oc  # noqa: E402
from config import free_gate  # noqa: E402

#: UA MESURE sur le binaire officiel 1.19.3 le 2026-10-20 (chemin ai-sdk,
#: provider `@ai-sdk/openai-compatible` = celui des deux jambes du proxy).
#: Si ce test echoue apres une release du client : re-capturer (voir
#: `_research/oc-1.19.3/README.md`) puis mettre a jour cette constante et la
#: valeur par defaut de `_OPENCODE_OFFICIAL_UA`. Ne jamais bumper a l'aveugle.
OFFICIAL_UA_1193 = "opencode/1.19.3 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14"

#: Forme attendue d'un UA ai-sdk : c'est elle qui trahit une recopie ratee.
UA_SHAPE = re.compile(r"^opencode/\d+\.\d+\.\d+ ai-sdk/provider-utils/\d+\.\d+\.\d+ runtime/bun/\d+\.\d+\.\d+$")

#: En-tetes de transport poses explicitement (ordre du client, sauf Connection).
TRANSPORT_HEADERS = ("Connection", "Accept", "Host", "Accept-Encoding")

ENDPOINT = "https://opencode.ai/inference/openai/v1/chat/completions"


def _capture_headers(style=None, monkeypatch=None):
    """Envoie _official_free_headers a un captureur local, renvoie le wire recu."""
    if style is not None:
        monkeypatch.setattr(oc, "_FREE_WIRE_STYLE", style)
    captured = []

    class Srv(HTTPServer):
        def handle_error(self, request, client_address):
            pass

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            captured.append(list(self.headers.items()))
            p = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(p)))
            self.end_headers()
            self.wfile.write(p)

    srv = Srv(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        from curl_cffi.requests import Session as CurlS

        url = f"http://127.0.0.1:{port}/v1/chat/completions"
        hdrs = oc._official_free_headers(url)
        _imp, fp = oc._get_free_fp_kwargs()
        kw = dict(fp)
        imp = kw.pop("impersonate", None)
        s = CurlS(impersonate=imp, **kw)
        s.post(
            url,
            content=oc._serialize_json_body(
                {"model": "m", "stream": True, "messages": [{"role": "user", "content": "x"}]}
            ),
            headers=hdrs,
            timeout=(5, 15),
        )
        s.close()
    finally:
        srv.shutdown()
    return captured[0] if captured else []


# ── 1. Le wire emis est celui du client 1.19.3 ───────────────────────────────


@pytest.mark.skipif(not oc._ensure_curl_cffi(), reason="curl_cffi requis")
def test_free_wire_uses_official_session_identity(monkeypatch):
    """Les trois noms de session du client 1.19.3, avec la MEME valeur."""
    got = {k.lower(): v for k, v in _capture_headers("official", monkeypatch)}
    for name in ("x-opencode-session-id", "x-session-affinity", "x-session-id"):
        assert got.get(name), f"{name} absent du wire free"
    valeurs = {got["x-opencode-session-id"], got["x-session-affinity"], got["x-session-id"]}
    assert len(valeurs) == 1, f"les trois en-tetes de session divergent : {valeurs}"
    assert re.fullmatch(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}", valeurs.pop())


@pytest.mark.skipif(not oc._ensure_curl_cffi(), reason="curl_cffi requis")
def test_free_wire_drops_legacy_quartet(monkeypatch):
    """Le quartet 1.18.x n'est PAS emis (mesure : inutile pour la porte)."""
    got = {k.lower() for k, _ in _capture_headers("official", monkeypatch)}
    presents = [h for h in free_gate.LEGACY_IDENTITY_HEADERS if h in got]
    assert not presents, f"quartet legacy reintroduit : {presents}"


@pytest.mark.skipif(not oc._ensure_curl_cffi(), reason="curl_cffi requis")
def test_free_wire_legacy_style_is_rollbackable(monkeypatch):
    """Le drapeau `legacy` restaure l'ancien wire, a chaud, sans redeploiement."""
    got = {k.lower() for k, _ in _capture_headers("legacy", monkeypatch)}
    for h in free_gate.LEGACY_IDENTITY_HEADERS:
        assert h in got, f"{h} absent du wire legacy"
    assert "x-opencode-session-id" not in got


@pytest.mark.skipif(not oc._ensure_curl_cffi(), reason="curl_cffi requis")
def test_free_wire_values_match_official_client(monkeypatch):
    """Valeurs statiques identiques a la capture du client officiel."""
    got = {k.lower(): v for k, v in _capture_headers("official", monkeypatch)}
    assert got.get("authorization") == "Bearer public", "la cle payante ne doit JAMAIS partir (A.0)"
    assert got.get("content-type") == "application/json"
    assert got.get("connection") == "keep-alive"
    assert got.get("accept") == "*/*"
    assert got.get("accept-encoding") == "gzip, deflate, br, zstd"
    assert got.get("host", "").startswith("127.0.0.1")


def test_user_agent_is_the_measured_one():
    """L'UA par defaut est celui du client 1.19.3, et il a la bonne forme."""
    assert oc._OPENCODE_OFFICIAL_UA == OFFICIAL_UA_1193, (
        "UA par defaut != capture du client officiel : re-mesurer avant de changer (voir _research/oc-1.19.3/README.md)"
    )
    assert UA_SHAPE.match(oc._OPENCODE_OFFICIAL_UA)


def test_ua_version_is_not_a_gate_factor():
    """NON-FACTEUR mesure : la porte ne discrimine pas la version d'UA.

    Si ce test casse un jour parce qu'une version d'UA est refusee en live,
    c'est un changement de la passerelle : le documenter dans l'audit AVANT
    de verrouiller une version (c'est cette croyance qui a gele le proxy sur
    1.18.31 pendant deux releases).
    """
    for version in ("1.18.31", "1.19.3", "2.0.32"):
        ua = f"opencode/{version} ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14"
        assert UA_SHAPE.match(ua), f"{ua} n'a pas la forme ai-sdk attendue"


# ── 2. Modele de porte (unitaire, hors reseau) ───────────────────────────────


def _read_tool(**over):
    tool = {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file or directory from the local filesystem. Returns up to 2000 lines.",
            "parameters": {
                "type": "object",
                "properties": {"filePath": {"type": "string"}},
                "required": ["filePath"],
            },
        },
    }
    tool["function"].update(over)
    return tool


def test_gate_identity_factor_needs_a_session_header():
    assert not free_gate.identity_ok({})
    assert not free_gate.identity_ok({"Authorization": "Bearer public"})
    assert free_gate.identity_ok({"x-session-affinity": "ses_" + "a" * 12 + "b" * 14})
    assert free_gate.identity_ok({"X-Session-Id": "ses_x"}), "casse insensible"
    assert not free_gate.identity_ok({"x-opencode-session-id": "   "}), "vide != present"


def test_gate_body_factor_requires_stream_and_realistic_read():
    ok = {"stream": True, "tools": [_read_tool()]}
    assert free_gate.body_ok(ok)

    sans_stream = {"stream": False, "tools": [_read_tool()]}
    assert not free_gate.body_ok(sans_stream), "le client force stream:true"

    sans_outil = {"stream": True, "messages": []}
    assert not free_gate.body_ok(sans_outil), "un flux sans outil est refuse (mesure)"

    bash_seul = {
        "stream": True,
        "tools": [_read_tool(name="bash")],
    }
    assert not free_gate.body_ok(bash_seul), "bash seul ne suffit pas (mesure)"

    read_stub = {
        "stream": True,
        "tools": [{"type": "function", "function": {"name": "read", "parameters": {}}}],
    }
    assert not free_gate.body_ok(read_stub), "un stub sans description n'est pas realiste"


def test_gate_diagnose_names_the_failing_factor():
    """Un refus doit toujours nommer le facteur : c'est tout l'objet du modele."""
    v = free_gate.diagnose({}, {}, egress="unknown")
    assert not v.passed
    assert v.failed_factors == ["identity", "body"]

    v2 = free_gate.diagnose(
        {"x-opencode-session-id": "ses_x"},
        {"stream": True, "tools": [_read_tool()]},
        egress="rejected",
    )
    assert not v2.passed
    assert v2.failed_factors == ["egress"], "un 403 IP doit etre attribue a l'egress"

    v3 = free_gate.diagnose({"x-opencode-session-id": "ses_x"}, {"stream": True, "tools": [_read_tool()]}, egress="ok")
    assert v3.passed
    assert v3.as_log()["gate"]["failed"] == []


def test_gate_legacy_quartet_is_not_required():
    """NON-FACTEUR mesure : le quartet legacy ne figure pas dans le modele.

    Nuance importante : `x-opencode-session` est membre du quartet *et* un nom
    d'identite de session valide (c'est le porteur d'identite du client
    1.18.x). Les trois autres membres (client/project/request) sont de la
    metadonnee pure : ils ne satisfont aucun facteur et ne sont donc jamais
    requis.
    """
    metadonnee = ("x-opencode-client", "x-opencode-project", "x-opencode-request")
    for h in metadonnee:
        assert h not in free_gate.SESSION_IDENTITY_HEADERS, f"{h} ne doit pas etre un porteur d'identite"
    v = free_gate.diagnose({"x-opencode-session-id": "ses_x"}, {"stream": True, "tools": [_read_tool()]}, egress="ok")
    assert v.passed, "le wire officiel (sans quartet) doit suffire"


# ── 3. Invariants de transport ───────────────────────────────────────────────


def test_accept_encoding_announces_zstd_like_bun():
    h = oc._official_free_headers(ENDPOINT)
    assert h["Accept-Encoding"] == "gzip, deflate, br, zstd"
    assert oc._OPENCODE_ACCEPT_ENCODING == "gzip, deflate, br, zstd"


def test_full_headers_kill_switch(monkeypatch):
    """OPENCODE_FREE_WIRE_FULL_HEADERS=0 retire les 4 en-tetes de transport."""
    monkeypatch.setattr(oc, "_FREE_WIRE_FULL_HEADERS", False)
    monkeypatch.setattr(oc, "_FREE_WIRE_STYLE", "official")
    h = oc._official_free_headers(ENDPOINT)
    for hdr in TRANSPORT_HEADERS:
        assert hdr not in h, f"{hdr} devrait etre absent avec le kill-switch"
    assert len(h) == 6, f"wire officiel sans transport = 6 en-tetes, recu {sorted(h)}"


def test_host_recomputed_per_endpoint():
    a = oc._official_free_headers("https://opencode.ai/inference/openai/v1/chat/completions")
    b = oc._official_free_headers("https://exemple.test/v1/chat/completions")
    assert a["Host"] == "opencode.ai"
    assert b["Host"] == "exemple.test"


def test_free_wire_never_carries_the_paid_key():
    """Invariant A.0 : la jambe free ne recopie JAMAIS la cle du client."""
    h = oc._official_free_headers(ENDPOINT)
    assert h["Authorization"] == "Bearer public"
    assert all("bearer public" == str(v).lower() for k, v in h.items() if k.lower() == "authorization")


# ── 4. Reasoning multi-tours sur la jambe free (P0-2) ────────────────────────


def _assistant(wire):
    return [m for m in wire["messages"] if m.get("role") == "assistant"]


def test_reasoning_content_present_is_preserved_verbatim():
    """Ce que le client fournit voyage TEL QUEL : aucune reecriture."""
    body = {
        "model": "deepseek-v4.1-flash-free",
        "max_tokens": 512,
        "stream": True,
        "messages": [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "r1", "reasoning_content": "REFLEXION"},
            {"role": "user", "content": "q2"},
        ],
    }
    wire, _ = oc._free_wire_body(body, "deepseek-v4.1-flash-free")
    assert _assistant(wire)[0]["reasoning_content"] == "REFLEXION"


def test_deepseek_reasoning_default_is_applied_on_the_free_leg():
    """P0-2 : DeepSeek exige `reasoning_content` sur CHAQUE message assistant.

    Sans ce remplissage, l'amont renvoie 400 « The `reasoning_content` in the
    thinking mode must be passed back to the API » (reproduit en production le
    2026-10-20, corps de 359 Ko, 24 outils). La regle existait dans
    `mapping.ensure_deepseek_reasoning` mais n'etait pas appliquee au corps de
    la jambe free : la conversion et le wire free avaient diverges.
    """
    body = {
        "model": "deepseek-v4.1-flash-free",
        "max_tokens": 512,
        "stream": True,
        "messages": [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "r1"},
            {"role": "user", "content": "q2"},
        ],
    }
    wire, _ = oc._free_wire_body(body, "deepseek-v4.1-flash-free")
    assert _assistant(wire)[0].get("reasoning_content") == " ", (
        "le defaut deepseek n'est pas pose sur le wire free : le 400 multi-tours revient"
    )


def test_non_deepseek_models_are_not_polluted():
    """Le remplissage est borne aux modeles deepseek : les autres restent intacts."""
    body = {
        "model": "mimo-v2.6-flash-free",
        "max_tokens": 512,
        "stream": True,
        "messages": [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "r1"},
            {"role": "user", "content": "q2"},
        ],
    }
    wire, _ = oc._free_wire_body(body, "mimo-v2.6-flash-free")
    assert "reasoning_content" not in _assistant(wire)[0]
