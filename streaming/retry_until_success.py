"""RetryUntilSuccessMiddleware — retry transparent des erreurs recuperables.

**Le besoin.** Certains clients (agents) n'acceptent pas de voir un 429/502 :
ils doivent continuer a travailler. Ce middleware garde la requete ouverte,
rejoue la requete ENTIERE tant que l'echec est retryable, et ne rend la main
qu'au succes ou apres la fenetre de grace configuree.

**Ce qui est retryable, et ce qui ne l'est pas.**
  * retry : 408/409/429/5xx, flux 200 vide, premiere trame d'erreur de type
    rate_limit/overloaded/api_error/server_error/timeout ;
  * pas de retry : 400/401/403/404/405/413/422, erreurs de validation et de
    contexte trop long. Reessayer ne peut pas les corriger.

**Flux.** Une requete streaming qui echoue avant le premier contenu recoit un
`200 text/event-stream` + des commentaires `: keepalive` pendant les attentes.
Le client ne voit ni 429 ni 502, et ne declenche pas sa propre boucle de retry.
Des qu'une tentative produit du contenu, les octets sont relayes tels quels ;
un echec APRES le premier octet n'est jamais rejoue (duplication garantie).

**Securite.** Le corps de la requete est bufferise une fois puis rejoue a
chaque tentative. Le middleware abandonne des que le client se deconnecte.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from email.utils import parsedate_to_datetime
from typing import Any

from .empty_guard import looks_like_error_frame

__all__ = [
    "RetryPolicy",
    "RetryUntilSuccessMiddleware",
    "classify_error_frame",
    "classify_json_body",
    "is_retryable_status",
    "parse_retry_after",
]

_RETRY_PATHS = (
    "/v1/messages",
    "/anthropic/v1/messages",
    "/v1/chat/completions",
    "/v1/responses",
)
_SKIP_PREFIXES = (
    "/health",
    "/api/",
    "/static/",
    "/metrics",
    "/dashboard",
    "/favicon",
    "/ws",
)

#: Statuts qui peuvent changer entre deux tentatives.
DEFAULT_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})

#: Types d'erreur qui ne doivent JAMAIS etre rejoues.
_NON_RETRYABLE_TYPES = frozenset(
    {
        "invalid_request_error",
        "authentication_error",
        "permission_error",
        "not_found_error",
        "method_not_allowed",
        "request_too_large",
        "payload_too_large",
        "content_policy_violation",
        "context_length_exceeded",
        "billing_error",
    }
)

#: Types d'erreur transitoires.
_RETRYABLE_TYPES = frozenset(
    {
        "rate_limit_error",
        "overloaded_error",
        "api_error",
        "server_error",
        "internal_error",
        "timeout_error",
        "service_unavailable",
        "connection_error",
    }
)

_RETRYABLE_MARKERS = (
    "produced no output",
    "empty stream",
    "empty_response",
    "stream aborted",
    "rate limit",
    "too many requests",
    "quota",
    "usage limit",
    "overloaded",
    "temporarily unavailable",
    "try again",
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "upstream connect",
    "socket hang up",
)

#: Erreurs produites par le proxy lui-meme (rate limiter client) : les
#: rejouer ne fait que consommer un jeton de plus.
_LOCAL_RATE_LIMIT_MARKERS = (
    "rate limit exceeded. please retry shortly.",
    '"code":"rate_limit_exceeded"',
    '"code": "rate_limit_exceeded"',
)


def is_retryable_status(status: int, retry_statuses: frozenset[int] | set[int] | None = None) -> bool:
    """Le statut HTTP peut-il changer au prochain essai ?"""
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    allowed = retry_statuses or DEFAULT_RETRY_STATUSES
    return code in allowed or 500 <= code <= 599


def parse_retry_after(value: str | None) -> float:
    """Retry-After (secondes ou date HTTP) -> secondes ; 0 si illisible."""
    if not value:
        return 0.0
    raw = str(value).strip()
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return 0.0
        return max(0.0, dt.timestamp() - time.time())
    except Exception:
        return 0.0


def _header_value(headers: list[tuple[bytes, bytes]] | None, name: bytes) -> str:
    for key, value in headers or []:
        try:
            if bytes(key).lower() == name:
                return bytes(value).decode("latin-1", "replace")
        except Exception:
            continue
    return ""


def _content_type(headers: list[tuple[bytes, bytes]] | None) -> str:
    return _header_value(headers, b"content-type").lower()


def _parse_error_payload(payload: Any) -> tuple[str, str, str]:
    """Retourne (verdict, type, message) pour un objet d'erreur JSON."""
    if not isinstance(payload, dict):
        return "retry", "api_error", str(payload)[:300]
    err = payload.get("error")
    if isinstance(err, dict):
        etype = str(err.get("type") or err.get("code") or payload.get("type") or "")
        msg = str(err.get("message") or err.get("detail") or "")
    else:
        etype = str(payload.get("type") or payload.get("code") or "")
        msg = str(payload.get("message") or payload.get("detail") or err or "")
    low_type = etype.lower()
    low_msg = msg.lower()
    if low_type in _NON_RETRYABLE_TYPES:
        return "pass", etype or "invalid_request_error", msg or "non-retryable error"
    if any(
        marker in low_msg for marker in ("context length", "context too long", "too many tokens", "maximum context")
    ):
        return "pass", etype or "invalid_request_error", msg
    if low_type in _RETRYABLE_TYPES:
        return "retry", etype or "api_error", msg
    if any(marker in low_msg for marker in _RETRYABLE_MARKERS):
        return "retry", etype or "api_error", msg
    # Un type inconnu est rejoue : c'est le comportement officiel (l'inconnu
    # est traite comme transitoire), et le plafond de temps borne l'attente.
    return "retry", etype or "api_error", msg or "upstream error"


def _parse_sse_json(chunk: bytes) -> Any:
    """Extrait le premier objet JSON d'une trame SSE (ou None)."""
    try:
        text = bytes(chunk).decode("utf-8", "replace")
    except Exception:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            return json.loads(raw)
        except Exception:
            continue
    return None


def classify_error_frame(chunk: bytes | bytearray | memoryview | None) -> tuple[str, str, str]:
    """Une premiere trame SSE est-elle rejouable ? (content|retry|pass)"""
    if not chunk:
        return "content", "", ""
    try:
        raw = bytes(chunk)
    except Exception:
        return "content", "", ""
    if not looks_like_error_frame(raw):
        return "content", "", ""
    payload = _parse_sse_json(raw)
    if payload is None:
        # Trame d'erreur non parsable : on la considere transitoire, bornee
        # par le budget de temps.
        return "retry", "api_error", raw[:300].decode("utf-8", "replace")
    return _parse_error_payload(payload)


def classify_json_body(chunk: bytes | bytearray | memoryview | None) -> tuple[str, str, str]:
    """Un corps JSON 200 contient-il une erreur rejouable ?"""
    if not chunk:
        return "retry", "api_error", "empty response"
    try:
        payload = json.loads(bytes(chunk).decode("utf-8", "replace"))
    except Exception:
        return "content", "", ""
    if not isinstance(payload, dict) or "error" not in payload:
        return "content", "", ""
    return _parse_error_payload(payload)


def _looks_local_rate_limit(headers: list[tuple[bytes, bytes]] | None, chunk: bytes) -> bool:
    if "json" not in _content_type(headers):
        return False
    try:
        low = bytes(chunk).lower().decode("utf-8", "replace")
    except Exception:
        return False
    return any(marker in low for marker in _LOCAL_RATE_LIMIT_MARKERS)


def _is_stream_request(scope: dict, body: bytes) -> bool:
    """Le client attend-il un flux SSE ?"""
    try:
        payload = json.loads(body.decode("utf-8", "replace")) if body else {}
    except Exception:
        payload = {}
    if isinstance(payload, dict) and "stream" in payload:
        return bool(payload.get("stream"))
    accept = ""
    for key, value in scope.get("headers") or []:
        try:
            if bytes(key).lower() == b"accept":
                accept = bytes(value).decode("latin-1", "replace").lower()
                break
        except Exception:
            continue
    return "text/event-stream" in accept


def _is_json(headers: list[tuple[bytes, bytes]] | None) -> bool:
    return "json" in _content_type(headers)


def _content_length(scope: dict) -> int:
    for key, value in scope.get("headers") or []:
        try:
            if bytes(key).lower() == b"content-length":
                return int(bytes(value).decode("latin-1", "replace"))
        except (TypeError, ValueError):
            return 0
    return 0


def _parse_error_body(body: bytes) -> Any:
    """JSON, trame SSE, ou texte brut -> objet d'erreur exploitable."""
    if body:
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
        payload = _parse_sse_json(body)
        if payload is not None:
            return payload
        return {
            "error": {
                "type": "api_error",
                "message": body[:300].decode("utf-8", "replace"),
            }
        }
    return {"error": {"type": "api_error", "message": "empty upstream response"}}


def _sse_headers() -> list[tuple[bytes, bytes]]:
    return [
        (b"content-type", b"text/event-stream; charset=utf-8"),
        (b"cache-control", b"no-cache"),
        (b"connection", b"keep-alive"),
        (b"x-accel-buffering", b"no"),
    ]


def _error_payload(path: str, message: str, error_type: str = "api_error") -> bytes:
    if "/messages" in path or path.startswith("/anthropic"):
        payload = {"type": "error", "error": {"type": error_type, "message": message}}
    else:
        payload = {"error": {"message": message, "type": error_type}}
    return json.dumps(payload, ensure_ascii=False).encode()


def _sse_error(path: str, message: str, error_type: str = "api_error") -> bytes:
    payload = _error_payload(path, message, error_type)
    if "/messages" in path or path.startswith("/anthropic"):
        return b"event: error\ndata: " + payload + b"\n\n"
    return b"data: " + payload + b"\n\ndata: [DONE]\n\n"


class RetryPolicy:
    """Politique immuable d'une requete (lue depuis config.yaml)."""

    __slots__ = (
        "enabled",
        "max_wait_s",
        "base_delay_s",
        "max_delay_s",
        "keepalive_interval_s",
        "jitter",
        "buffer_max_bytes",
        "retry_statuses",
    )

    def __init__(
        self,
        *,
        enabled: bool = False,
        max_wait_s: float = 900.0,
        base_delay_s: float = 1.0,
        max_delay_s: float = 60.0,
        keepalive_interval_s: float = 10.0,
        jitter: float = 0.25,
        buffer_max_bytes: int = 10 * 1024 * 1024,
        retry_statuses: frozenset[int] | set[int] | None = None,
    ):
        self.enabled = bool(enabled)
        try:
            wait = float(max_wait_s)
        except (TypeError, ValueError):
            wait = 900.0
        # 0 = pas de plafond (choix explicite) ; une valeur negative est
        # traitee comme le defaut pour ne pas creer d'attente infinie par
        # accident.
        self.max_wait_s = max(0.0, min(86_400.0, wait)) if wait >= 0 else 900.0
        try:
            base = float(base_delay_s)
        except (TypeError, ValueError):
            base = 1.0
        self.base_delay_s = max(0.05, min(300.0, base))
        try:
            cap = float(max_delay_s)
        except (TypeError, ValueError):
            cap = 60.0
        self.max_delay_s = max(self.base_delay_s, min(3600.0, cap))
        try:
            keep = float(keepalive_interval_s)
        except (TypeError, ValueError):
            keep = 10.0
        self.keepalive_interval_s = max(1.0, min(120.0, keep))
        try:
            jit = float(jitter)
        except (TypeError, ValueError):
            jit = 0.25
        self.jitter = max(0.0, min(0.5, jit))
        try:
            self.buffer_max_bytes = max(1024, int(buffer_max_bytes))
        except (TypeError, ValueError):
            self.buffer_max_bytes = 10 * 1024 * 1024
        self.retry_statuses = frozenset(retry_statuses or DEFAULT_RETRY_STATUSES)

    @classmethod
    def from_mapping(cls, cfg: dict | None) -> RetryPolicy:
        cfg = cfg if isinstance(cfg, dict) else {}
        return cls(
            enabled=bool(cfg.get("enabled", False)),
            max_wait_s=cfg.get("max_wait_s", 900.0),
            base_delay_s=cfg.get("base_delay_s", 1.0),
            max_delay_s=cfg.get("max_delay_s", 60.0),
            keepalive_interval_s=cfg.get("keepalive_interval_s", 10.0),
            jitter=cfg.get("jitter", 0.25),
            buffer_max_bytes=cfg.get("buffer_max_bytes", 10 * 1024 * 1024),
            retry_statuses=cfg.get("retry_statuses"),
        )


class _Attempt:
    __slots__ = (
        "started",
        "status",
        "headers",
        "body",
        "forwarding",
        "forwarded",
        "retry",
        "content",
        "local_rate_limit",
        "start",
    )

    def __init__(self):
        self.started = False
        self.status = 0
        self.headers: list[tuple[bytes, bytes]] = []
        self.body = b""
        self.forwarding = False
        self.forwarded = False
        self.retry = False
        self.content = False
        self.local_rate_limit = False
        self.start: dict | None = None


def _clone_scope(scope: dict) -> dict:
    """Copie de scope avec un state neuf : aucune fuite entre tentatives."""
    clone = dict(scope)
    try:
        clone["state"] = dict(scope.get("state") or {})
    except Exception:
        clone["state"] = {}
    return clone


def _replay_receive(chunks: list[bytes], disconnected: asyncio.Event):
    index = 0

    async def receive() -> dict:
        nonlocal index
        if index < len(chunks):
            body = chunks[index]
            index += 1
            return {
                "type": "http.request",
                "body": body,
                "more_body": index < len(chunks),
            }
        await disconnected.wait()
        return {"type": "http.disconnect"}

    return receive


async def _wait_disconnect(receive, disconnected: asyncio.Event) -> None:
    try:
        while not disconnected.is_set():
            message = await receive()
            if message.get("type") == "http.disconnect":
                disconnected.set()
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        disconnected.set()


class RetryUntilSuccessMiddleware:
    """ASGI pur : rejoue une requete retryable sans exposer l'echec au client."""

    def __init__(self, app, config_getter=None, debug_fn=None, on_event=None):
        self.app = app
        self._config_getter = config_getter or (lambda: {})
        self._debug = debug_fn or (lambda *_a, **_k: None)
        self._on_event = on_event or (lambda *_a, **_k: None)

    def _policy(self) -> RetryPolicy:
        try:
            cfg = self._config_getter() or {}
        except Exception:
            cfg = {}
        return RetryPolicy.from_mapping(cfg)

    @staticmethod
    def _is_candidate(scope: dict) -> bool:
        if scope.get("type") != "http":
            return False
        if str(scope.get("method") or "").upper() != "POST":
            return False
        path = str(scope.get("path") or "")
        if any(path.startswith(prefix) for prefix in _SKIP_PREFIXES):
            return False
        return any(path.startswith(prefix) for prefix in _RETRY_PATHS)

    async def _read_body(self, receive) -> list[bytes] | None:
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return None
            if message.get("type") != "http.request":
                continue
            body = message.get("body") or b""
            if body:
                chunks.append(bytes(body))
            if not message.get("more_body"):
                break
        return chunks or [b""]

    @staticmethod
    def _delay(policy: RetryPolicy, attempt: int, last: _Attempt) -> float:
        retry_after = parse_retry_after(_header_value(last.headers, b"retry-after"))
        if retry_after > 0:
            delay = min(retry_after, policy.max_delay_s)
        else:
            delay = min(policy.max_delay_s, policy.base_delay_s * (2 ** max(0, attempt - 1)))
        if policy.jitter:
            delay *= 1.0 + random.uniform(-policy.jitter, policy.jitter)
        return max(0.05, delay)

    async def _sleep_with_keepalive(
        self,
        delay: float,
        send,
        *,
        committed: bool,
        is_stream: bool,
        policy: RetryPolicy,
        disconnected: asyncio.Event,
    ) -> None:
        remaining = max(0.0, delay)
        while remaining > 0:
            if disconnected.is_set():
                return
            step = remaining
            if committed and is_stream:
                step = min(step, policy.keepalive_interval_s)
            await asyncio.sleep(step)
            remaining -= step
            if committed and is_stream and remaining > 0:
                try:
                    await send({"type": "http.response.body", "body": b": keepalive\n\n", "more_body": True})
                except Exception:
                    return

    async def _send_start(self, send, attempt: _Attempt) -> None:
        if attempt.start is None:
            attempt.start = {
                "type": "http.response.start",
                "status": attempt.status or 200,
                "headers": attempt.headers,
            }
        await send(attempt.start)
        attempt.started = True

    async def _commit(self, send, attempt: _Attempt, message: dict) -> None:
        await self._send_start(send, attempt)
        await send(message)
        attempt.forwarding = True
        attempt.forwarded = True
        if message.get("body"):
            attempt.content = True

    async def _commit_buffered(self, send, attempt: _Attempt) -> None:
        await self._send_start(send, attempt)
        await send({"type": "http.response.body", "body": attempt.body, "more_body": False})
        attempt.forwarding = True
        attempt.forwarded = True
        attempt.content = bool(attempt.body)

    async def _finish_with_error(
        self,
        send,
        *,
        path: str,
        outer_committed: bool,
        last: _Attempt | None,
        policy: RetryPolicy,
        reason: str,
    ) -> None:
        message = ""
        error_type = "api_error"
        if last is not None and last.body:
            payload = _parse_sse_json(last.body)
            if payload is not None:
                _, etype, msg = _parse_error_payload(payload)
                error_type = etype or error_type
                message = msg or message
            else:
                try:
                    payload = json.loads(last.body.decode("utf-8", "replace"))
                    _, etype, msg = _parse_error_payload(payload)
                    error_type = etype or error_type
                    message = msg or message
                except Exception:
                    message = last.body[:300].decode("utf-8", "replace")
        if not message:
            message = f"upstream unavailable after retries ({reason})"
        if outer_committed:
            try:
                await send(
                    {"type": "http.response.body", "body": _sse_error(path, message, error_type), "more_body": False}
                )
            except Exception:
                pass
            return
        if last is not None and last.status:
            try:
                await self._send_start(send, last)
                await send({"type": "http.response.body", "body": last.body, "more_body": False})
                return
            except Exception:
                pass
        body = _error_payload(path, message, error_type)
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": 502,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body, "more_body": False})
        except Exception:
            pass

    async def __call__(self, scope, receive, send):
        if not self._is_candidate(scope):
            await self.app(scope, receive, send)
            return
        policy = self._policy()
        if not policy.enabled:
            await self.app(scope, receive, send)
            return
        # Ne jamais bufferiser un corps au-dela de la limite : on laisse
        # l'application (et son garde 413) traiter la requete en direct.
        length = _content_length(scope)
        if length > policy.buffer_max_bytes:
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        chunks = await self._read_body(receive)
        if chunks is None:
            return
        body_bytes = b"".join(chunks)
        is_stream = _is_stream_request(scope, body_bytes)

        disconnected = asyncio.Event()
        disconnect_task = asyncio.create_task(_wait_disconnect(receive, disconnected))
        outer = {"committed": False, "content": False}
        last: _Attempt | None = None
        attempt_no = 0
        started_at = time.monotonic()
        deadline = started_at + policy.max_wait_s if policy.max_wait_s > 0 else None

        async def send_capture(message: dict, at: _Attempt) -> None:
            mtype = message.get("type")
            if mtype == "http.response.start":
                at.start = message
                at.started = True
                at.status = int(message.get("status") or 200)
                at.headers = list(message.get("headers") or [])
                return

            if mtype == "http.response.body":
                if at.forwarding:
                    await send(message)
                    return
                body = bytes(message.get("body") or b"")
                more = bool(message.get("more_body"))
                if at.retry:
                    if body and len(at.body) < 65536:
                        at.body += body
                    return

                if outer["committed"]:
                    # En-tetes 200 deja partis (keepalive) : le statut n'est
                    # plus libre, mais le contenu peut encore etre remplace.
                    if at.status and at.status != 200:
                        if is_retryable_status(at.status, policy.retry_statuses):
                            at.retry = True
                            if body and len(at.body) < 65536:
                                at.body += body
                            return
                        payload = _parse_error_body(body)
                        _, etype, msg = _parse_error_payload(payload)
                        try:
                            await send(
                                {
                                    "type": "http.response.body",
                                    "body": _sse_error(path, msg or f"upstream HTTP {at.status}", etype),
                                    "more_body": False,
                                }
                            )
                        except Exception:
                            pass
                        at.forwarded = True
                        return
                    if _is_json(at.headers):
                        verdict, etype, msg = classify_json_body(body)
                        if verdict == "retry":
                            at.retry = True
                            if body and len(at.body) < 65536:
                                at.body += body
                            return
                        try:
                            await send(
                                {
                                    "type": "http.response.body",
                                    "body": _sse_error(
                                        path,
                                        msg or "upstream returned a non-stream response",
                                        etype or "api_error",
                                    ),
                                    "more_body": False,
                                }
                            )
                        except Exception:
                            pass
                        at.forwarded = True
                        return
                    if not body:
                        if not more:
                            at.retry = True
                        return
                    if looks_like_error_frame(body):
                        verdict, etype, msg = classify_error_frame(body)
                        if verdict == "retry":
                            at.retry = True
                            if len(at.body) < 65536:
                                at.body += body
                            return
                        if verdict == "pass":
                            try:
                                await send({"type": "http.response.body", "body": body, "more_body": True})
                            except Exception:
                                pass
                            at.forwarding = True
                            at.forwarded = True
                            return
                    if body.lstrip().startswith(b":"):
                        return
                    at.forwarding = True
                    at.forwarded = True
                    at.content = True
                    outer["content"] = True
                    await send(message)
                    return

                # Rien n'a encore ete envoye : le statut est encore libre.
                if is_retryable_status(at.status, policy.retry_statuses):
                    if at.status == 429 and _looks_local_rate_limit(at.headers, body):
                        await self._commit(send, at, message)
                        return
                    at.retry = True
                    if body and len(at.body) < 65536:
                        at.body += body
                    return

                if at.status == 200:
                    if is_stream:
                        if not body:
                            if not more:
                                at.retry = True
                            return
                        if looks_like_error_frame(body):
                            verdict, etype, msg = classify_error_frame(body)
                            if verdict == "retry":
                                at.retry = True
                                if len(at.body) < 65536:
                                    at.body += body
                                return
                            if verdict == "pass":
                                await self._commit(send, at, message)
                                return
                        if body.lstrip().startswith(b":"):
                            await self._commit(send, at, message)
                            return
                        await self._commit(send, at, message)
                        outer["content"] = True
                        return

                    at.body += body
                    if more:
                        return
                    if not at.body:
                        at.retry = True
                        return
                    if _is_json(at.headers):
                        verdict, etype, msg = classify_json_body(at.body)
                        if verdict == "retry":
                            at.retry = True
                            return
                    await self._commit_buffered(send, at)
                    outer["content"] = bool(at.body)
                    return

                await self._commit(send, at, message)
                return

            if mtype == "http.response.trailers":
                if at.forwarding:
                    await send(message)
                return
            if at.forwarding:
                await send(message)

        current_app: asyncio.Task | None = None
        try:
            while True:
                if disconnected.is_set():
                    return
                at = _Attempt()
                app_task = asyncio.create_task(
                    self.app(
                        _clone_scope(scope),
                        _replay_receive(chunks, disconnected),
                        lambda message, _at=at: send_capture(message, _at),
                    )
                )
                current_app = app_task
                done, _pending = await asyncio.wait({app_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED)
                if disconnect_task in done and disconnected.is_set():
                    app_task.cancel()
                    try:
                        await app_task
                    except BaseException:
                        pass
                    return
                try:
                    await app_task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if at.forwarded or outer["content"]:
                        raise
                    at.retry = True
                    if not at.body:
                        at.body = str(exc).encode("utf-8", "replace")[:500]

                if at.forwarded or outer["content"]:
                    self._on_event("success", attempt_no, at.status, len(at.body))
                    return
                if not at.retry:
                    # L'application n'a rien envoye du tout : on considere
                    # que c'est un echec transitoire, pas un succes silencieux.
                    if not at.started:
                        at.retry = True
                    else:
                        return

                last = at
                attempt_no += 1
                self._on_event("retry", attempt_no, at.status, len(at.body))
                self._debug(
                    f"  [retry-until-success] attempt {attempt_no} failed "
                    f"(status={at.status or '?'}, body={len(at.body)}B) -> retry"
                )
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    self._on_event("exhausted", attempt_no, at.status, len(at.body))
                    await self._finish_with_error(
                        send,
                        path=path,
                        outer_committed=outer["committed"],
                        last=last,
                        policy=policy,
                        reason=f"max_wait_s={policy.max_wait_s:.0f}",
                    )
                    return
                delay = self._delay(policy, attempt_no, at)
                if deadline is not None:
                    delay = min(delay, max(0.0, deadline - now))
                if is_stream and not outer["committed"]:
                    try:
                        await send({"type": "http.response.start", "status": 200, "headers": _sse_headers()})
                        await send({"type": "http.response.body", "body": b": keepalive\n\n", "more_body": True})
                        outer["committed"] = True
                    except Exception:
                        return
                await self._sleep_with_keepalive(
                    delay,
                    send,
                    committed=outer["committed"],
                    is_stream=is_stream,
                    policy=policy,
                    disconnected=disconnected,
                )
                if disconnected.is_set():
                    return
                if not outer["committed"]:
                    # Rien n'a encore ete envoye : la prochaine tentative peut
                    # encore choisir un statut propre.
                    pass
        finally:
            if current_app is not None and not current_app.done():
                current_app.cancel()
                try:
                    await current_app
                except BaseException:
                    pass
            disconnect_task.cancel()
            try:
                await disconnect_task
            except BaseException:
                pass
