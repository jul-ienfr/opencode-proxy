"""[plan v10 §14.0.3-4] Confiance réseau zéro-friction (décision v9).

Modèle : loopback toujours autorisé + CIDR LAN configurables hot-reloadables
(`dashboard_trust.lan_cidrs`) → aucune saisie jamais demandée en local/LAN.
Garde-fous conservés : validation Origin/Host anti-CSRF sur les méthodes
mutatrices, rate-limit des mutations, token optionnel pour exposition WAN,
toggle `client_auth.mode: none|lan|key` pour `/v1/*`.

ASGI pur (pas de BaseHTTPMiddleware — overhead SSE, cf. §14.3.19).
Typé pour `mypy --strict` (charte §3.7 sur nouveaux modules).
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Défauts §3.8 v9 — privés (RFC1918) + loopback v4/v6.
DEFAULT_LAN_CIDRS: tuple[str, ...] = (
    "127.0.0.0/8",
    "::1/128",
    "192.168.0.0/16",
    "10.0.0.0/8",
    "172.16.0.0/12",
)

MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# ── ASGI plumbing (types minimaux, évite la dépendance aux stubs starlette) ──

Receive = Callable[[], Any]
Send = Callable[[Any], Any]
MutableScope = dict[str, Any]

# Rate-limit mutations : 30/min/IP (garde-fou c §14.0.3).
_MUTATION_RATE_LIMIT: int = 30
_MUTATION_WINDOW_SEC: float = 60.0
_MUT_TABLE_MAX: int = 10_000


def _yaml_get(section: str, key: str, default: Any) -> Any:  # noqa: ANN401
    """Late import — config.settings peut ne pas être chargé dans certains tests."""
    try:
        from config.settings import yaml_get

        return yaml_get(section, key, default)
    except Exception:
        return default


def parse_cidrs(raw: Iterable[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse tolérant : CIDR invalide → warning + ignoré (jamais de crash au boot)."""
    out: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for c in raw:
        try:
            net = ipaddress.ip_network(str(c), strict=False)
        except ValueError:
            logger.warning("[trust] CIDR invalide ignoré: %r", c)
            continue
        out.append(net)
    return out


def lan_cidrs() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    raw = _yaml_get("dashboard_trust", "lan_cidrs", None)
    items = raw if isinstance(raw, (list, tuple)) and raw else DEFAULT_LAN_CIDRS
    return parse_cidrs([str(c) for c in items])


def is_loopback(ip: str) -> bool:
    # Sentinelle du TestClient starlette : client=("testclient", 50000).
    # Les tests HTTP doivent traverser le trust comme du trafic local.
    if ip == "testclient":
        return True
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def ip_trusted(ip: str) -> bool:
    """Décision réseau : loopback toujours True ; mode `open` → tout ;
    mode `lan` (défaut) → CIDR LAN ; mode `off` → loopback uniquement."""
    if not ip:
        return False
    if is_loopback(ip):
        return True
    mode = str(_yaml_get("dashboard_trust", "mode", "lan") or "lan").strip().lower()
    if mode == "open":
        return True
    if mode != "lan":
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in lan_cidrs())


def scope_client_ip(scope: Mapping[str, Any]) -> str:
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0] or "")
    return ""


def scope_header(scope: Mapping[str, Any], name: str) -> str:
    want = name.lower().encode("latin-1")
    for raw_key, raw_val in scope.get("headers") or ():
        # bytes(...) : retype explicite — scope["headers"] est typé Any côté ASGI
        key_b, val_b = bytes(raw_key), bytes(raw_val)
        if key_b.lower() == want:
            return val_b.decode("latin-1", errors="replace")
    return ""


def origin_host_matches(scope: Mapping[str, Any]) -> bool:
    """Anti-CSRF/DNS-rebinding : si Origin OU Referer est présent et que son
    host diffère du Host servi → False. Absents (curl, clients SDK) → True."""
    host_hdr = scope_header(scope, "host").lower()
    origin = scope_header(scope, "origin")
    if not origin:
        referer = scope_header(scope, "referer")
        if not referer:
            return True
        origin = referer
    try:
        origin_host = urlsplit(origin).hostname
    except ValueError:
        return False
    if not origin_host:
        return True
    try:
        served_host = urlsplit(f"//{host_hdr}").hostname if host_hdr else None
    except ValueError:
        served_host = None
    if not served_host:
        return True  # pas de Host à comparer — ne bloque pas (LAN local)
    return origin_host.lower() == served_host.lower()


class _MutationRateLimiter:
    """Fenêtre glissante par IP, bornée en mémoire."""

    def __init__(self, limit: int = _MUTATION_RATE_LIMIT, window: float = _MUTATION_WINDOW_SEC):
        self.limit = limit
        self.window = window
        self._hits: dict[str, list[float]] = {}

    def allow(self, ip: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits.get(ip)
        if hits is None:
            if len(self._hits) >= _MUT_TABLE_MAX:
                self._hits.clear()
            hits = self._hits.setdefault(ip, [])
        cutoff = now - self.window
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


class DashboardTrustMiddleware:
    """Garde `/api/*` : GET/HEAD → confiance réseau (+token sinon) ;
    mutations → confiance + Origin même-host + rate-limit.
    `require_token=true` (opt-in WAN) force le token hors loopback.
    [v10 §14.2.4] security headers posés sur TOUTES les réponses."""

    SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
        (
            b"content-security-policy",
            b"default-src 'self'; script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            b"style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
            b"object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
        ),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"same-origin"),
    )

    def __init__(
        self,
        app: Any,
        token_getter: Callable[[], str],
        require_token_getter: Callable[[], bool] | None = None,
        rate_limiter: _MutationRateLimiter | None = None,
    ) -> None:
        self.app = app
        self._token_getter = token_getter
        self._require_token_getter = require_token_getter or (lambda: False)
        self._limiter = rate_limiter or _MutationRateLimiter()

    async def __call__(self, scope: MutableScope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if not str(scope.get("path", "")).startswith("/api/"):
            # hors /api : pas de logique trust, mais security headers quand même
            await self.app(scope, receive, self._wrap_send_headers(send))
            return
        try:
            status_body = self._check(scope)
        except Exception:
            logger.exception("[trust] erreur middleware — fail-closed")
            status_body = (503, {"error": "trust_check_failed"})
        if status_body is not None:
            status, body = status_body
            await send_json(send, status, body)
            return
        await self.app(scope, receive, self._wrap_send_headers(send))

    def _wrap_send_headers(self, send: Send) -> Send:
        async def wrapped(message: Any) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                have = {bytes(k).lower() for k, _ in headers}
                for hk, hv in self.SECURITY_HEADERS:
                    if hk not in have:
                        headers.append((hk, hv))
                message["headers"] = headers
            await send(message)

        return wrapped

    def _check(self, scope: MutableScope) -> tuple[int, dict[str, str]] | None:
        method = str(scope.get("method", "GET")).upper()
        ip = scope_client_ip(scope)
        token = scope_header(scope, "x-dashboard-token")
        expected = self._token_getter() or ""
        tok_ok = bool(token and expected and hmac.compare_digest(token, expected))
        require_token = bool(self._require_token_getter()) and not is_loopback(ip)
        trusted = ip_trusted(ip)
        if require_token and not tok_ok:
            return (401, {"error": "unauthorized", "message": "X-Dashboard-Token requis."})
        if not trusted and not tok_ok:
            # Token configuré → 401 (sémantique historique _check_dashboard_token) ;
            # sinon 403 réseau non autorisé.
            if expected:
                return (401, {"error": "unauthorized", "message": "X-Dashboard-Token requis."})
            return (
                403,
                {
                    "error": "forbidden",
                    "message": "Réseau non autorisé (dashboard_trust.lan_cidrs).",
                },
            )
        if method in MUTATING_METHODS:
            if not origin_host_matches(scope):
                return (
                    403,
                    {"error": "forbidden", "message": "Origine cross-site refusée (anti-CSRF)."},
                )
            if not self._limiter.allow(ip or "?"):
                return (429, {"error": "rate_limited", "message": "Trop de mutations."})
        return None


class ClientAuthMiddleware:
    """Toggle `client_auth.mode: none|lan|key` pour `/v1/*` et les resets.
    Défaut `none` = comportement historique (zéro friction, bind 0.0.0.0 voulu).
    `key` sans clé configurée → retombe sur `lan` + warning une fois."""

    PROTECTED_PREFIXES: tuple[str, ...] = ("/v1/",)
    PROTECTED_PATHS: frozenset[str] = frozenset(
        {"/api/circuit-breakers/reset", "/api/key-pauses/reset"}
    )

    def __init__(
        self,
        app: Any,
        warn: Callable[[str], None] | None = None,
    ) -> None:
        self.app = app
        self._warn = warn or (lambda msg: logger.warning("[trust] %s", msg))
        self._warned_empty_key = False

    async def __call__(self, scope: MutableScope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http":
            path = str(scope.get("path", ""))
            if path.startswith(self.PROTECTED_PREFIXES) or path in self.PROTECTED_PATHS:
                status_body = self._check(scope, path)
                if status_body is not None:
                    status, body = status_body
                    await send_json(send, status, body)
                    return
        await self.app(scope, receive, send)

    def _mode(self) -> str:
        return str(_yaml_get("client_auth", "mode", "none") or "none").strip().lower()

    def _expected_key(self) -> str:
        return str(_yaml_get("client_auth", "key", "") or "")

    def _provided_key(self, scope: MutableScope) -> str:
        auth = scope_header(scope, "authorization")
        if auth[:7].lower() == "bearer ":
            return auth[7:].strip()
        for h in ("x-api-key", "x-client-key"):
            v = scope_header(scope, h)
            if v:
                return v.strip()
        return ""

    def _check(self, scope: MutableScope, path: str) -> tuple[int, dict[str, str]] | None:
        mode = self._mode()
        if mode in ("", "none"):
            return None
        ip = scope_client_ip(scope)
        if mode == "key":
            expected = self._expected_key()
            provided = self._provided_key(scope)
            if expected:
                if provided and hmac.compare_digest(provided, expected):
                    return None
                return (401, {"error": "unauthorized", "message": "Clé client requise."})
            if not self._warned_empty_key:
                self._warned_empty_key = True
                self._warn("client_auth.mode=key sans client_auth.key — repli sur mode 'lan'.")
            mode = "lan"
        # mode "lan"
        if ip_trusted(ip):
            return None
        return (
            403,
            {
                "error": "forbidden",
                "message": f"Accès {path} réservé au LAN (client_auth.mode=lan).",
            },
        )


async def send_json(send: Send, status: int, body: Mapping[str, str]) -> None:
    import json

    payload = json.dumps(body).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})
