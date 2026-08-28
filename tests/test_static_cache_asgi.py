"""[P1.3 perf] _StaticCacheMiddleware pur ASGI + gzip pré-compressé.

Contrats :
  - les octets .gz pré-calculés au démarrage sont servis directement
    (Content-Encoding: gzip, Vary: Accept-Encoding) sans repasser par
    StaticFiles ;
  - sans Accept-Encoding: gzip → fallback StaticFiles + Cache-Control
    posé sur TOUTES les réponses /static/* ;
  - les chemins non statiques traversent le middleware intacts ;
  - HEAD est servi sans corps mais avec les mêmes métadonnées.
"""

import gzip as _gzip
import sqlite3

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from dashboard.api import register_dashboard


@pytest.fixture
def static_app(tmp_path):
    (tmp_path / "app.js").write_bytes(b"console.log('hello'); " * 50)
    (tmp_path / "styles.css").write_bytes(b"body { color: red; } " * 50)
    (tmp_path / "notes.txt").write_bytes(b"plain text asset")
    (tmp_path / "index.html").write_text("<html><body>dash</body></html>")
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    fast = FastAPI()
    register_dashboard(fast, str(tmp_path), conn)
    return fast, tmp_path


def test_gzip_accept_serves_precompressed_bytes(static_app):
    fast, tmp_path = static_app
    with TestClient(fast) as client:
        r = client.get(
            "/static/app.js", headers={"accept-encoding": "gzip, deflate"}
        )
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["vary"] == "Accept-Encoding"
    assert r.headers["cache-control"] == "public, max-age=3600"
    # TestClient décompresse automatiquement : le contenu doit être IDENTIQUE
    # au fichier source après décompression gzip.
    assert r.content == (tmp_path / "app.js").read_bytes()


def test_no_gzip_falls_through_with_cache_control(static_app):
    fast, tmp_path = static_app
    with TestClient(fast) as client:
        r = client.get("/static/app.js", headers={"accept-encoding": "identity"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert r.headers["cache-control"] == "public, max-age=3600"
    assert r.content == (tmp_path / "app.js").read_bytes()


def test_head_request_has_headers_without_body(static_app):
    fast, _ = static_app
    with TestClient(fast) as client:
        r = client.head("/static/styles.css", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert r.content == b""


def test_non_static_paths_untouched(static_app):
    fast, tmp_path = static_app
    with TestClient(fast) as client:
        r = client.get("/", headers={"accept-encoding": "identity"})
    assert r.status_code == 200
    assert b"dash" in r.content
    assert "cache-control" not in r.headers, (
        "Cache-Control réservé aux assets /static/*"
    )


def test_precompressed_payload_is_smaller_and_valid(static_app, monkeypatch):
    import dashboard.api as api

    _, tmp_path = static_app
    pre = api._precompress_static_assets(str(tmp_path))
    assert "/static/app.js" in pre and "/static/styles.css" in pre
    assert "/static/notes.txt" not in pre, "seuls js/css sont pré-compressés"
    gz, ctype = pre["/static/app.js"]
    assert ctype == "text/javascript" or ctype == "application/javascript"
    assert len(gz) < (tmp_path / "app.js").stat().st_size
    assert _gzip.decompress(gz) == (tmp_path / "app.js").read_bytes()
