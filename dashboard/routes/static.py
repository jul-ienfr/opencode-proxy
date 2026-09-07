"""dashboard.routes.static — assets statiques : pré-compression + cache (Phase 8).

Déplacement PUR depuis ``dashboard/api.py`` (module-level, stdlib +
starlette seuls, ``static_dir``/``precompressed`` en paramètres).
``dashboard/api.py`` ré-importe les mêmes objets (``api._precompress_static_assets``
reste valide — ``test_static_cache_asgi.py`` — et le montage middleware
dans ``register_dashboard`` est inchangé).
"""

from __future__ import annotations

import os

from starlette.datastructures import MutableHeaders


def precompress_static_assets(static_dir) -> dict[str, tuple[bytes, str]]:
    """[P1.3 perf] Compresse UNE FOIS au demarrage les assets JS/CSS du
    dashboard (zlib niveau 6, format gzip). Le middleware statique sert ces
    octets directement si le client accepte gzip : zero compression ni I/O
    disque par requete. Retour {path_url: (gz_bytes, content_type)}."""
    import mimetypes
    import zlib

    out: dict[str, tuple[bytes, str]] = {}
    try:
        names = sorted(os.listdir(static_dir))
    except Exception:
        return out
    for name in names:
        if not name.endswith((".js", ".css")):
            continue
        try:
            with open(os.path.join(static_dir, name), "rb") as f:
                raw = f.read()
            co = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
            gz = co.compress(raw) + co.flush()
            if len(gz) < len(raw):
                ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                out["/static/" + name] = (gz, ctype)
        except Exception:
            continue
    return out


# Alias historique (dashboard.api._precompress_static_assets).
_precompress_static_assets = precompress_static_assets


class StaticCacheMiddleware:
    """[P1.3 perf] Middleware statique PUR ASGI : Cache-Control sur /static/*
    + service direct des octets .gz pré-compressés si le client accepte
    gzip. Remplace la version BaseHTTPMiddleware (~1-8 ms/requête sur TOUTES
    les requêtes y compris SSE) par ~0.05 ms : pas de task enveloppe, pas de
    canaux recréés — un simple wrap du send ASGI."""

    def __init__(self, app, precompressed=None):
        self.app = app
        self._pre = precompressed or {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        hit = self._pre.get(path)
        if hit is not None and scope.get("method") in ("GET", "HEAD"):
            accept = ""
            for k, v in scope.get("headers") or []:
                if k == b"accept-encoding":
                    accept = v.decode("latin-1", "ignore")
                    break
            if "gzip" in accept.lower():
                gz, ctype = hit
                is_head = scope.get("method") == "HEAD"
                body = b"" if is_head else gz
                headers = [
                    (b"content-type", ctype.encode("latin-1")),
                    (b"content-length", str(len(gz)).encode("latin-1")),
                    (b"content-encoding", b"gzip"),
                    (b"vary", b"Accept-Encoding"),
                    (b"cache-control", b"public, max-age=3600"),
                ]
                await send({"type": "http.response.start", "status": 200, "headers": headers})
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return
        if not path.startswith("/static/"):
            await self.app(scope, receive, send)
            return

        async def _send_cc(message):
            if message["type"] == "http.response.start":
                resp_headers = MutableHeaders(scope=message)
                resp_headers["cache-control"] = "public, max-age=3600"
            await send(message)

        await self.app(scope, receive, _send_cc)


# Alias historique (dashboard.api._StaticCacheMiddleware).
_StaticCacheMiddleware = StaticCacheMiddleware


__all__ = [
    "StaticCacheMiddleware",
    "_StaticCacheMiddleware",
    "_precompress_static_assets",
    "precompress_static_assets",
]
