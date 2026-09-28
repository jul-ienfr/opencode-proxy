"""Verrouille le WIRE COMPLET de la jambe free : 12 en-tetes comme le client.

Test de conformite mesure : le client officiel 1.18.31 envoie 12 en-tetes
(capture logs/_capture_real_provider.jsonl, 243 requetes, une seule signature
d'en-tetes sur toute la serie). On verifie que la jambe free les emet tous,
avec les bonnes valeurs, et que l'ordre hors Connection est identique.

Limite structurelle assumee : libcurl emet TOUJOURS Connection en dernier,
l'ordre exact du client (Connection en 8e) est inatteignable via curl_cffi.
Ce test verrouille donc « identical hors Connection », pas l'ordre brut.
"""

import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, ".")
import opencode as oc  # noqa: E402

# Ordre EXACT mesure sur le fil chez le client officiel (243 requetes).
CLIENT_WIRE_ORDER = [
    "Authorization",
    "Content-Type",
    "User-Agent",
    "x-opencode-client",
    "x-opencode-project",
    "x-opencode-request",
    "x-opencode-session",
    "Connection",
    "Accept",
    "Host",
    "Accept-Encoding",
    "Content-Length",
]

CLIENT_WIRE_VALUES = {
    "Authorization": "Bearer public",
    "Content-Type": "application/json",
    "User-Agent": "opencode/1.18.31 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14",
    "x-opencode-client": "cli",
    "Connection": "keep-alive",
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate, br, zstd",
}


def _capture_headers(extra_fp=None):
    """Envoie _official_free_headers via une vraie session curl_cffi vers un
    captureur local, et renvoie les en-tetes RECUS dans l'ordre du fil."""
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


@pytest.mark.skipif(
    not oc._ensure_curl_cffi(), reason="curl_cffi requis pour le wire reel"
)
def test_free_wire_has_all_client_headers():
    """Les 12 en-tetes du client sont emis (avant : 10 seulement)."""
    got = _capture_headers()
    assert got, "aucune requete capturee"
    names = [k for k, _ in got]
    manquants = [h for h in CLIENT_WIRE_ORDER if h not in names]
    assert not manquants, f"en-tetes du client absents du wire free : {manquants}"
    assert len(names) == len(CLIENT_WIRE_ORDER), (
        f"le client envoie {len(CLIENT_WIRE_ORDER)} en-tetes, le proxy {len(names)} : {names}"
    )


@pytest.mark.skipif(
    not oc._ensure_curl_cffi(), reason="curl_cffi requis pour le wire reel"
)
def test_free_wire_values_match_client():
    """Chaque valeur statique est identique a celle du client officiel."""
    got = {k.lower(): v for k, v in _capture_headers()}
    for name, expected in CLIENT_WIRE_VALUES.items():
        assert got.get(name.lower()) == expected, (
            f"{name}: attendu {expected!r}, recu {got.get(name.lower())!r}"
        )
    # Valeurs derivees : ID de projet conforme + IDs OC.
    assert got.get("x-opencode-project"), "x-opencode-project vide"
    import re

    assert re.fullmatch(r"(global|[0-9a-f]{40})", got["x-opencode-project"])
    assert re.fullmatch(r"msg_[0-9a-f]{12}[0-9A-Za-z]{14}", got["x-opencode-request"])
    assert re.fullmatch(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}", got["x-opencode-session"])


@pytest.mark.skipif(
    not oc._ensure_curl_cffi(), reason="curl_cffi requis pour le wire reel"
)
def test_free_wire_order_matches_client_except_connection():
    """Ordre identique a celui du client, a la position de Connection pres.

    Limite du moteur : libcurl emet toujours Connection en dernier. On
    verrouille donc l'ordre hors Connection — et on documente l'ecart.
    """
    got = [k for k, _ in _capture_headers()]
    got_sans = [h for h in got if h != "Connection"]
    cl_sans = [h for h in CLIENT_WIRE_ORDER if h != "Connection"]
    assert got_sans == cl_sans, f"ordre divergent :\n  proxy  {got_sans}\n  client {cl_sans}"
    # Connection est present, mais pas a la position du client (limite connue).
    assert "Connection" in got


def test_accept_encoding_announces_zstd_like_bun():
    """Le client annonce zstd ; on l'annonce aussi (decodage verifie)."""
    h = oc._official_free_headers("https://opencode.ai/zen/v1/chat/completions")
    assert h["Accept-Encoding"] == "gzip, deflate, br, zstd"
    assert oc._OPENCODE_ACCEPT_ENCODING == "gzip, deflate, br, zstd"


def test_full_headers_kill_switch(monkeypatch):
    """OPENCODE_FREE_WIRE_FULL_HEADERS=0 retire les 4 en-tetes de transport."""
    monkeypatch.setattr(oc, "_FREE_WIRE_FULL_HEADERS", False)
    h = oc._official_free_headers("https://opencode.ai/zen/v1/chat/completions")
    for hdr in ("Connection", "Accept", "Host", "Accept-Encoding"):
        assert hdr not in h, f"{hdr} devrait etre absent avec le kill-switch"
    assert len(h) == 7


def test_host_recomputed_per_endpoint():
    """Host suit l'endpoint (le client l'emet pour opencode.ai)."""
    a = oc._official_free_headers("https://opencode.ai/zen/v1/chat/completions")
    b = oc._official_free_headers("https://exemple.test/v1/chat/completions")
    assert a["Host"] == "opencode.ai"
    assert b["Host"] == "exemple.test"