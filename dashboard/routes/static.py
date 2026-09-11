"""dashboard.routes.static — assets statiques : pré-compression + cache (Phase 8).

Déplacement PUR depuis ``dashboard/api.py`` (module-level, stdlib +
starlette seuls, ``static_dir``/``precompressed`` en paramètres).
``dashboard/api.py`` ré-importe les mêmes objets (``api._precompress_static_assets``
reste valide — ``test_static_cache_asgi.py`` — et le montage middleware
dans ``register_dashboard`` est inchangé).

[Phase 5 plan boot] Compression déplacée au BUILD :
``scripts/precompress.py`` génère ``app.js.br`` / ``app.js.gz`` à côté des
sources. ``load_precompressed()`` sert ces fichiers s'ils sont présents ET
plus récents que la source (sinon compression à la volée, mode dev), et
``StaticCacheMiddleware`` négocie ``br > gzip > identité``. Le boot ne
compresse donc plus 250 Ko de JS en zlib à chaque démarrage (~17 ms mesurés),
et le client récupère en plus une charge brotli ~20 % plus légère.
"""

from __future__ import annotations

import os

from starlette.datastructures import MutableHeaders

# Encodages servis, du meilleur au moins bon (négociation par Accept-Encoding).
# La clé est le SUFFIXE DE FICHIER (`.gz`/`.br`), la valeur le nom d'encodage
# HTTP à poser dans Content-Encoding : `gzip`, jamais `gz` (erreur attrapée par
# test_static_cache_asgi — un client ne décompresserait pas « gz »).
_ENCODING_EXT = (("br", "br"), ("gz", "gzip"))
_ENCODING_NAMES = tuple(name for _, name in _ENCODING_EXT)

# Assets candidats à la pré-compression (js/css servis par /static/*).
_COMPRESSIBLE_EXT = (".js", ".css")


def _compress_gzip(raw: bytes) -> bytes:
    import zlib

    co = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    return co.compress(raw) + co.flush()


def _compress_brotli(raw: bytes) -> bytes | None:
    """brotli si disponible (requirements.txt), sinon None (dégradation)."""
    try:
        import brotli

        return brotli.compress(raw, quality=11)
    except Exception:
        return None


def _content_type(name: str) -> str:
    import mimetypes

    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _is_fresh(pre_path: str, src_path: str) -> bool:
    """Le fichier pré-compressé est-il utilisable (présent et non périmé) ?

    Périmé = plus vieux que la source : un asset édité à la main sans
    relancer le script de build doit être recompressé, jamais servi tel quel.
    """
    try:
        return os.path.getmtime(pre_path) >= os.path.getmtime(src_path)
    except OSError:
        return False


def load_precompressed(static_dir) -> dict[str, dict[str, tuple[bytes, str]]]:
    """Charge les assets pré-compressés : ``{path: {"br": (bytes, ctype), "gz": ...}}``.

    Pour chaque ``.js``/``.css`` : lit ``<nom>.br`` / ``<nom>.gz`` produits par
    ``scripts/precompress.py``. Si aucun des deux n'est présent ou frais, on
    compresse à la volée (comportement historique) — un checkout sans build
    reste pleinement fonctionnel, juste un peu plus lent au démarrage.

    Les deux encodages sont TOUJOURS présents dans le dict quand la source a
    pu être lue : ``gz`` est le fallback universel même si `.br` existe.
    """
    out: dict[str, dict[str, tuple[bytes, str]]] = {}
    try:
        names = sorted(os.listdir(static_dir))
    except Exception:
        return out
    for name in names:
        if not name.endswith(_COMPRESSIBLE_EXT):
            continue
        src_path = os.path.join(static_dir, name)
        try:
            with open(src_path, "rb") as f:
                raw = f.read()
        except Exception:
            continue
        ctype = _content_type(name)
        url = "/static/" + name
        entry: dict[str, tuple[bytes, str]] = {}
        # brotli (build) — pas de fallback à la volée : quality=11 coûte
        # ~150 ms/fichier, hors de question au démarrage.
        for ext in _ENCODING_NAMES:
            pre_path = f"{src_path}.{ext}"
            try:
                if _is_fresh(pre_path, src_path):
                    with open(pre_path, "rb") as f:
                        entry[ext] = (f.read(), ctype)
            except Exception:
                pass
        # gzip : build sinon à la volée (rapide, ~17 ms pour tout le dossier).
        if "gz" not in entry:
            try:
                gz = _compress_gzip(raw)
                if len(gz) < len(raw):
                    entry["gz"] = (gz, ctype)
            except Exception:
                pass
        if entry:
            out[url] = entry
    return out


def precompress_static_assets(static_dir) -> dict[str, tuple[bytes, str]]:
    """[P1.3 perf] Format HISTORIQUE ``{path: (gz_bytes, ctype)}``.

    Conservé tel quel (contrat ``test_static_cache_asgi.py``) : délègue à
    ``load_precompressed`` et n'expose que la variante gzip. ``register_dashboard``
    utilise désormais ``load_precompressed`` directement pour servir aussi
    brotli, mais l'API historique reste fonctionnelle pour tout appelant
    externe (dont ``scripts/bench_perf.py``).
    """
    out: dict[str, tuple[bytes, str]] = {}
    for url, variants in load_precompressed(static_dir).items():
        gz = variants.get("gz")
        if gz is not None:
            out[url] = gz
    return out


# Alias historique : `dashboard.routes.static._precompress_static_assets`
# (dashboard/api.py l'importe sous ce nom, scripts/bench_perf.py aussi).
_precompress_static_assets = precompress_static_assets


def _normalize_pre(precompressed) -> dict[str, dict[str, tuple[bytes, str]]]:
    """Accepte l'ancien format ``{path: (bytes, ctype)}`` et le nouveau.

    Les deux formes coexistent le temps de la transition : les tests et
    ``scripts/bench_perf.py`` passent encore l'ancienne (gzip implicite).
    """
    if not precompressed:
        return {}
    out: dict[str, dict[str, tuple[bytes, str]]] = {}
    for path, value in precompressed.items():
        if isinstance(value, tuple):
            out[path] = {"gz": value}
        elif isinstance(value, dict):
            out[path] = dict(value)
    return out


def _negotiate(accept: str, variants: dict[str, tuple[bytes, str]]):
    """Meilleure variante selon Accept-Encoding (br > gzip), sinon None.

    Retourne ``(nom_encodage_http, (octets, ctype))`` — le nom posé dans
    Content-Encoding (``gzip``, pas ``gz``).
    """
    if not accept:
        return None
    low = accept.lower()
    for ext, http_name in _ENCODING_EXT:
        if ext in variants and http_name in low:
            return http_name, variants[ext]
    return None


class StaticCacheMiddleware:
    """[P1.3 perf] Middleware statique PUR ASGI : Cache-Control sur /static/*
    + service direct des octets pré-compressés si le client accepte brotli ou
    gzip. Remplace la version BaseHTTPMiddleware (~1-8 ms/requête sur TOUTES
    les requêtes y compris SSE) par ~0.05 ms : pas de task enveloppe, pas de
    canaux recréés — un simple wrap du send ASGI."""

    def __init__(self, app, precompressed=None):
        self.app = app
        self._pre = _normalize_pre(precompressed)

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
            chosen = _negotiate(accept, hit)
            if chosen is not None:
                enc, (payload, ctype) = chosen
                is_head = scope.get("method") == "HEAD"
                body = b"" if is_head else payload
                headers = [
                    (b"content-type", ctype.encode("latin-1")),
                    (b"content-length", str(len(payload)).encode("latin-1")),
                    (b"content-encoding", enc.encode("latin-1")),
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
                resp_headers["vary"] = "Accept-Encoding"
            await send(message)

        await self.app(scope, receive, _send_cc)


# Alias historique (dashboard.api._StaticCacheMiddleware).
_StaticCacheMiddleware = StaticCacheMiddleware

__all__ = [
    "StaticCacheMiddleware",
    "_StaticCacheMiddleware",
    "_precompress_static_assets",
    "load_precompressed",
    "precompress_static_assets",
]
