"""
Wireshark-like client traffic capture for the opencode proxy.

A pure-ASGI middleware that records every client request as a "frame":
raw request bytes (request line, headers, body), timestamps, timing
(TTFB, total duration, inter-arrival "tempo"), and abrupt client
disconnects (RST-like: ``http.disconnect`` before the response, or
``send()`` raising while streaming the response).

The capture is a bounded in-memory ring buffer — a rolling tcpdump
capture — with a per-frame body cap and a global byte budget. Bodies are
kept raw so the dashboard can render a true hex+ASCII dump. Frames are
appended when the request STARTS, so an in-flight request (a hanging
response, a stalled stream) is visible while it runs.

No project imports (stdlib + starlette only) so both ``opencode.py``
and ``dashboard/api.py`` can import it without circularity. Runs inside
the single asyncio loop (instance-lock pattern, like the rest of the
proxy): all mutations are synchronous between awaits, so no locks.
"""

from __future__ import annotations

import re
import time
from collections import deque

# Body stored per frame, above which the raw dump is truncated (bytes).
_DEF_BODY_CAP = 131072  # 128 KiB
_DEF_MAX_FRAMES = 500  # ring buffer length
_DEF_MAX_BYTES = 32 * 1024 * 1024  # global stored-body budget (32 MiB)

# Paths excluded from capture (dashboard self-noise / health probes).
_SKIP_PREFIXES = ("/static/", "/health", "/api/traffic")

# Redaction (finding i): frame headers/bodies/query are rendered raw in the
# dashboard — credential-shaped values must never be stored. Header values
# are replaced at capture time; bodies/query get a single regex pass at
# frame completion (values → "[REDACTED]", so the dump keeps its shape).
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "cookie",
        "set-cookie",
    }
)


def _header_is_sensitive(name: str) -> bool:
    """True when a header name carries a credential (redact its value)."""
    low = name.lower()
    if low in _SENSITIVE_HEADERS:
        return True
    return any(k in low for k in ("auth", "token", "key", "secret", "credential"))


_BODY_SECRET_RE = re.compile(
    r'(?i)("[a-z0-9_.-]*(?:api[_-]?key|auth|cookie|token|password|secret|credential)'
    r'[a-z0-9_.-]*"\s*:\s*")([^"\\]*(?:\\.[^"\\]*)*)(")'
)
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/\-=]+")
_FORM_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|go_auth_cookie|auth|token|password|secret|credential)=)[^&\s]+"
)


def _redact_text(text: str) -> str:
    """Masque credential-shaped JSON values / Bearer / form pairs (structure conservée)."""
    text = _BODY_SECRET_RE.sub(r"\1[REDACTED]\3", text)
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _FORM_SECRET_RE.sub(r"\1[REDACTED]", text)
    return text


def _redact_body(raw: bytes) -> bytes:
    """One pass over a stored frame body (see _redact_text).

    [plan v10 §14.1.18] binaire : décodage impossible → octets BRUTS rendus
    tels quels (l'ancien decode('utf-8','replace') corrompait les octets)."""
    if not raw:
        return raw
    try:
        text = raw.decode("utf-8")
    except Exception:
        return raw
    return _redact_text(text).encode("utf-8")


def _pct(sorted_vals: list[float], p: float) -> float:
    """Percentile of a pre-sorted ascending list (nearest-rank)."""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    idx = max(0, min(n - 1, int(round(p / 100.0 * (n - 1)))))
    return sorted_vals[idx]


class _Frame:
    """One captured client request (appended at request start, completed later)."""

    __slots__ = (
        "id",
        "ts",
        "delta_ms",
        "method",
        "path",
        "query",
        "version",
        "client_ip",
        "client_port",
        "headers",
        "body",
        "body_len",
        "truncated",
        "status",
        "ttfb_ms",
        "duration_ms",
        "aborted",
        "abort_reason",
        "binary",
        "tail",
    )

    def __init__(
        self,
        fid: int,
        ts: float,
        delta_ms: float,
        method: str,
        path: str,
        query: str,
        version: str,
        client_ip: str,
        client_port: int | None,
        headers: list[tuple[str, str]],
    ):
        self.id = fid
        self.ts = ts
        self.delta_ms = delta_ms
        self.method = method
        self.path = path
        self.query = query
        self.version = version
        self.client_ip = client_ip
        self.client_port = client_port
        self.headers = headers
        self.body: bytes | None = None  # set at completion
        self.body_len = 0  # full wire byte count
        self.truncated = False
        self.status: int | None = None  # None while in flight
        self.ttfb_ms: float | None = None
        self.duration_ms: float | None = None
        self.aborted = False
        self.abort_reason: str | None = None
        # [v10 §14.1.18] redaction incrémentale : binaire détecté / queue UTF-8
        self.binary = False
        self.tail: bytes = b""  # ≤3 octets multi-octets en attente de décodage

    # ── JSON projections (kept out of this module's hot path) ──
    @property
    def request_line(self) -> str:
        target = self.path
        if self.query:
            target = f"{target}?{self.query}"
        return f"{self.method} {target} HTTP/{self.version}"

    def meta_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.ts)),
            "time_ms": int(round((self.ts % 1.0) * 1000)),
            "delta_ms": round(self.delta_ms, 1),
            "method": self.method,
            "path": self.path,
            "query": self.query,
            "request_line": self.request_line,
            "client_ip": self.client_ip,
            "client_port": self.client_port,
            "body_len": self.body_len,
            "truncated": self.truncated,
            "status": self.status,
            "ttfb_ms": round(self.ttfb_ms, 1) if self.ttfb_ms is not None else None,
            "duration_ms": round(self.duration_ms, 1) if self.duration_ms is not None else None,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
        }


class TrafficCapture:
    """Bounded ring buffer of raw client request frames."""

    def __init__(
        self,
        max_frames: int | None = None,
        body_cap: int | None = None,
        max_bytes: int | None = None,
        enabled: bool | None = None,
    ):
        self.enabled = True
        self.max_frames = _DEF_MAX_FRAMES
        self.body_cap = _DEF_BODY_CAP
        self.max_bytes = _DEF_MAX_BYTES
        self._frames: deque[_Frame] = deque()
        self._by_id: dict[int, _Frame] = {}
        self._counter = 0
        self._bytes = 0
        self._last_ts: float | None = None
        if any(v is not None for v in (max_frames, body_cap, max_bytes, enabled)):
            self.configure(
                enabled=enabled, max_frames=max_frames, body_cap=body_cap, max_bytes=max_bytes
            )

    def configure(
        self,
        enabled: bool | None = None,
        max_frames: int | None = None,
        body_cap: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        if enabled is not None:
            self.enabled = bool(enabled)
        if max_frames is not None:
            self.max_frames = max(10, int(max_frames))
        if body_cap is not None:
            self.body_cap = max(1024, int(body_cap))
        if max_bytes is not None:
            self.max_bytes = max(self.body_cap, int(max_bytes))
            self._recount_bytes()  # budget may have shrunk below stored total

    @staticmethod
    def _skip(path: str) -> bool:
        return path == "/health" or any(path.startswith(p) for p in _SKIP_PREFIXES)

    # ── lifecycle (called by the middleware) ─────────────────────

    def _start(
        self,
        method: str,
        path: str,
        query: str,
        version: str,
        client_ip: str,
        client_port: int | None,
        headers: list[tuple[str, str]],
    ) -> _Frame:
        """Register a pending frame; evict when over the budgets."""
        self._counter += 1
        now = time.time()
        delta = 0.0
        if self._last_ts is not None:
            delta = (now - self._last_ts) * 1000.0
        self._last_ts = now
        # Query strings can carry credentials (?api_key=...) — redact.
        query = _FORM_SECRET_RE.sub(r"\1[REDACTED]", query)
        frame = _Frame(
            self._counter, now, delta, method, path, query, version, client_ip, client_port, headers
        )
        self._frames.append(frame)
        self._by_id[frame.id] = frame
        while len(self._frames) > self.max_frames:
            self._evict_oldest()
        return frame

    def _add_body(self, frame: _Frame, chunk: bytes) -> int:
        """Append a wire chunk to the frame body (capped). Returns stored bytes.

        [plan v10 §14.1.18] redaction INCRÉMENTALE : chaque chunk est masqué
        AVANT stockage — l'hexdump servi pendant le vol n'expose plus de
        secrets pendant toute la durée de vie de la requête (l'ancien code ne
        redactait qu'à _finish). Frontières UTF-8 multi-octets coupées entre
        deux chunks → ≤3 octets gardés en attente ; binaire détecté → octets
        bruts stockés, jamais re-redactés."""
        if not chunk:
            return 0
        if frame.body is None:
            frame.body = b""
        before = len(frame.body)
        if len(frame.body) < self.body_cap:
            take = chunk[: self.body_cap - len(frame.body)]
            data = (getattr(frame, "tail", b"") or b"") + take
            frame.tail = b""
            if getattr(frame, "binary", False):
                to_store = data  # binaire confirmé : brut
            else:
                try:
                    text = data.decode("utf-8")
                    to_store = _redact_text(text).encode("utf-8")
                except UnicodeDecodeError as e:
                    if e.start >= max(0, len(data) - 3):
                        # séquence multi-octets tronquée en fin → bufferiser
                        head = data[: e.start].decode("utf-8", "replace")
                        to_store = _redact_text(head).encode("utf-8")
                        frame.tail = data[e.start :]
                    else:
                        frame.binary = True  # vraie donnée binaire
                        to_store = data
            frame.body += to_store
            if len(take) < len(chunk):
                frame.truncated = True
        else:
            frame.truncated = True
        frame.body_len += len(chunk)
        self._bytes += len(frame.body) - before
        while self._bytes > self.max_bytes and len(self._frames) > 1:
            self._evict_oldest()
        return self._bytes

    def _finish(
        self,
        frame: _Frame,
        status: int | None,
        ttfb_ms: float | None,
        duration_ms: float,
        aborted: bool,
        abort_reason: str | None,
    ) -> None:
        """Complete a pending frame (called exactly once at the end)."""
        if frame.id not in self._by_id:
            return  # already evicted by the byte budget — drop
        frame.status = status
        frame.ttfb_ms = ttfb_ms
        frame.duration_ms = duration_ms
        frame.aborted = aborted
        frame.abort_reason = abort_reason
        if frame.body is None:
            frame.body = b""
        # Redaction pass: credential-shaped values in the stored body become
        # "[REDACTED]" (structure kept). Lengths change, so the global byte
        # budget must be recounted to stay honest (finding i).
        # [v10 §14.1.18] redaction déjà faite INCRÉMENTALEMENT dans _add_body ;
        # frames binaires : octets bruts, jamais décodés.
        if not getattr(frame, "binary", False):
            redacted = _redact_body(frame.body)
            if redacted != frame.body:
                frame.body = redacted
                self._recount_bytes()

    def _evict_oldest(self) -> None:
        if not self._frames:
            return
        old = self._frames.popleft()
        self._by_id.pop(old.id, None)
        if old.body:
            self._bytes -= len(old.body)

    def _recount_bytes(self) -> None:
        total = 0
        for f in self._frames:
            if f.body:
                total += len(f.body)
        self._bytes = total

    # ── control / read (called by the dashboard API) ─────────────

    def clear(self) -> int:
        n = len(self._frames)
        self._frames.clear()
        self._by_id.clear()
        self._bytes = 0
        self._last_ts = None
        return n

    def status(self) -> dict:
        newest = self._frames[-1] if self._frames else None
        oldest = self._frames[0] if self._frames else None
        return {
            "enabled": self.enabled,
            "max_frames": self.max_frames,
            "body_cap": self.body_cap,
            "max_bytes": self.max_bytes,
            "frames": len(self._frames),
            "bytes_stored": self._bytes,
            "counter": self._counter,
            "oldest_ts": oldest.ts if oldest else None,
            "newest_ts": newest.ts if newest else None,
        }

    def frames(
        self,
        limit: int = 200,
        offset: int = 0,
        method: str | None = None,
        path: str | None = None,
        status: int | None = None,
        aborted: bool | None = None,
        since: float | None = None,
    ) -> list[dict]:
        """Newest-first slice of frame metadata (no bodies)."""
        limit = max(1, min(limit, 1000))
        out: list[dict] = []
        for f in reversed(self._frames):
            if since is not None and f.ts < since:
                break
            if method and f.method != method:
                continue
            if path and path not in f.path:
                continue
            if status is not None and f.status != status:
                continue
            if aborted is not None and f.aborted != aborted:
                continue
            if offset > 0:
                offset -= 1
                continue
            out.append(f.meta_dict())
            if len(out) >= limit:
                break
        return out

    def frame_detail(self, fid: int) -> dict | None:
        f = self._by_id.get(fid)
        if f is None:
            return None
        meta = f.meta_dict()
        meta["headers"] = [{"name": n, "value": v} for n, v in f.headers]
        meta["body_hex"] = hex_dump(f.body or b"")
        return meta

    def stats(self, window: float = 60.0) -> dict:
        """Wireshark "Statistics"-style summary over the last ``window`` seconds."""
        now = time.time()
        frames: list[_Frame] = []
        for f in reversed(self._frames):
            if now - f.ts > window:
                break
            frames.append(f)
        frames.sort(key=lambda f: f.ts)  # chronological

        deltas = [f.delta_ms for f in frames if f.delta_ms is not None]
        durations = [f.duration_ms for f in frames if f.duration_ms is not None]
        ttfb = [f.ttfb_ms for f in frames if f.ttfb_ms is not None]
        aborted = [f for f in frames if f.aborted]
        in_flight = [f for f in frames if f.status is None]

        methods: dict[str, int] = {}
        paths: dict[str, int] = {}
        statuses: dict[str, int] = {}
        for f in frames:
            methods[f.method] = methods.get(f.method, 0) + 1
            p = f.path
            paths[p] = paths.get(p, 0) + 1
            s = str(f.status) if f.status is not None else "in-flight"
            statuses[s] = statuses.get(s, 0) + 1

        top_paths = sorted(paths.items(), key=lambda kv: (-kv[1], kv[0]))[:10]

        def _summ(vals: list[float]) -> dict | None:
            if not vals:
                return None
            s = sorted(vals)
            return {
                "n": len(s),
                "min": round(s[0], 1),
                "p50": round(_pct(s, 50), 1),
                "p95": round(_pct(s, 95), 1),
                "max": round(s[-1], 1),
            }

        return {
            "window": window,
            "frames": len(frames),
            "rps": round(len(frames) / window, 3) if window > 0 else 0.0,
            "aborted": len(aborted),
            "in_flight": len(in_flight),
            "bytes_wire": sum(f.body_len for f in frames),
            "tempo_delta_ms": _summ(deltas),
            "duration_ms": _summ(durations),
            "ttfb_ms": _summ(ttfb),
            "methods": methods,
            "paths_top": [{"path": p, "count": c} for p, c in top_paths],
            "statuses": statuses,
        }


def hex_dump(data: bytes, bytes_per_row: int = 16) -> list[dict]:
    """True Wireshark-style dump: offset | 8×2-byte hex groups | ASCII gutter."""
    rows: list[dict] = []
    n = len(data)
    for off in range(0, n, bytes_per_row):
        chunk = data[off : off + bytes_per_row]
        hex_parts = []
        for g in range(0, len(chunk), 2):
            pair = chunk[g : g + 2]
            hex_parts.append("".join(f"{b:02x}" for b in pair))
        ascii_gutter = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        hex_str = " ".join(hex_parts)
        if len(hex_str) < (bytes_per_row * 3 - 1):  # pad groups for alignment
            hex_str = hex_str.ljust(bytes_per_row * 3 - 1)
        rows.append({"offset": off, "hex": hex_str, "ascii": ascii_gutter, "len": len(chunk)})
    return rows


class TrafficCaptureMiddleware:
    """Pure-ASGI middleware that tees the client request into the capture.

    Not a ``BaseHTTPMiddleware``: reading the body must not starve the
    inner app, so ``receive`` is wrapped instead while the wire messages
    pass straight through to the handler.
    """

    def __init__(self, app, capture: TrafficCapture | None = None):
        self.app = app
        # [plan v10 §14.3.29] `capture or capture` était une auto-référence
        # (None si monté sans argument → AttributeError au premier hit) ;
        # retomber sur le singleton module-level `capture` (fin de fichier).
        self._capture = capture if capture is not None else globals()["capture"]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self._capture.enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if self._capture._skip(path):
            await self.app(scope, receive, send)
            return

        cap = self._capture
        start = time.monotonic()
        method = (scope.get("method") or "GET").upper()
        qs = (scope.get("query_string") or b"").decode("latin-1")
        version = scope.get("http_version", "1.1")
        client = scope.get("client") or (None, None)
        client_ip = client[0] or "?"
        client_port = client[1]

        headers: list[tuple[str, str]] = []
        try:
            for k, v in scope.get("headers") or []:
                name = k.decode("latin-1")
                value = v.decode("latin-1")
                if _header_is_sensitive(name):
                    value = "[REDACTED]"  # never store a raw credential (finding i)
                headers.append((name, value))
        except Exception:
            pass

        frame = cap._start(method, path, qs, version, client_ip, client_port, headers)

        early_disconnect = False
        response_started = False
        send_aborted = False

        async def receive_wrapper():
            nonlocal early_disconnect
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body") or b""
                if chunk:
                    cap._add_body(frame, chunk)
            elif message["type"] == "http.disconnect":
                early_disconnect = True
            return message

        async def send_wrapper(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                frame.status = message.get("status")
                frame.ttfb_ms = (time.monotonic() - start) * 1000.0
            await send(message)

        error: Exception | None = None
        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except Exception as e:  # noqa: BLE001 — record, then re-raise
            error = e
            send_aborted = True

        # Classify the abort signal (Wireshark "RST" proxy).
        aborted = frame.aborted
        reason = frame.abort_reason
        status = frame.status
        if early_disconnect and status is None:
            aborted, reason = True, "client closed mid-request (RST before response)"
        elif early_disconnect and status == 499:
            aborted, reason = True, "client disconnected (RST → 499)"
        elif send_aborted and response_started:
            aborted, reason = True, "client reset while response streaming (RST)"
        elif send_aborted:
            aborted, reason = (
                True,
                f"request aborted: {type(error).__name__}" if error else "request aborted",
            )

        cap._finish(
            frame, status, frame.ttfb_ms, (time.monotonic() - start) * 1000.0, aborted, reason
        )

        if error is not None:
            raise error


# Module-level singleton shared by opencode.py (middleware wiring) and
# dashboard/api.py (API endpoints).
capture = TrafficCapture()
