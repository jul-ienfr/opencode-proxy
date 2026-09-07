"""upstream.breaker — circuit-breakers per-endpoint + global 429 (Phase 3 refonte).

Déplacement PUR depuis ``opencode.py`` (§ « Circuit Breaker (per-endpoint) »
+ « Circuit breaker GLOBAL 429 »). AUCUN import du projet :

* seuils/timeouts injectés au constructeur (l'hôte les lit depuis
  ``config.yaml`` : ``circuit_breaker.failure_threshold/recovery_timeout``,
  ``global_429_*``) ;
* ``debug_fn`` injecté ;
* le flag hot-reload ``half_open_single_probe`` est résolu via le hook
  ``_probe_enabled()`` — l'hôte le surcharge (sous-classe
  ``opencode._CircuitBreaker``) pour lire son global
  ``_cb_half_open_probe_enabled()`` À L'APPEL (seam
  ``test_perf_lot3_regressions.py`` : monkeypatch + construction directe) ;
* le registre ``endpoint -> breaker`` RESTE la propriété de l'hôte
  (``oc._circuit_breakers`` est rebind par ``test_streaming_sse.py``) —
  l'hôte garde les wrappers ``_get_cb`` / ``_cb_*`` d'une ligne.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from fastapi.responses import JSONResponse

DEFAULT_FAILURE_THRESHOLD = 5  # = config.yaml circuit_breaker.failure_threshold
DEFAULT_RECOVERY_TIMEOUT = 60.0  # = config.yaml circuit_breaker.recovery_timeout
DEFAULT_GLOBAL_429_THRESHOLD = 10
DEFAULT_GLOBAL_429_WINDOW = 30.0
DEFAULT_GLOBAL_429_BACKOFF = 15.0


def _noop_debug(*args, **kwargs) -> None:
    return None


class CircuitOpenError(Exception):
    """Raised when the circuit breaker is open for an endpoint."""

    pass


class CircuitBreaker:
    """Per-endpoint circuit breaker: CLOSED → OPEN → HALF_OPEN → CLOSED.

    [plan Lot 2] En half_open, une SEULE requête sonde passe tant que la
    sonde n'a pas conclu — les requêtes concurrentes sont rejetées comme si
    le breaker était encore OPEN (CircuitOpenError → 503 côté appelant).
    ``half_open_in_flight`` = sonde en vol. Désactivable via
    ``circuit_breaker.half_open_single_probe: false`` (rollback)."""

    __slots__ = (
        "failures",
        "state",
        "opened_at",
        "total_requests",
        "total_failures",
        "last_failure_time",
        "created_at",
        "half_open_in_flight",
        "half_open_since",
        "_failure_threshold",
        "_recovery_timeout",
        "_debug_fn",
    )

    def __init__(
        self,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        recovery_timeout: float = DEFAULT_RECOVERY_TIMEOUT,
        *,
        debug_fn: Callable[..., None] = _noop_debug,
    ):
        self.failures = 0
        self.state = "closed"  # closed | open | half_open
        self.opened_at = 0.0
        self.total_requests = 0
        self.total_failures = 0
        self.last_failure_time = 0.0
        self.created_at = time.monotonic()
        self.half_open_in_flight = False
        self.half_open_since = 0.0
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._debug_fn = debug_fn

    def _probe_enabled(self) -> bool:
        """Flag half-open sonde unique — hook surchargé par l'hôte.

        Défaut True (= ``half_open_single_probe`` absent) ; l'hôte
        (``opencode._CircuitBreaker``) le résout via son global hot-reload
        ``_cb_half_open_probe_enabled()`` À L'APPEL."""
        return True

    def record_success(self):
        old_state = self.state
        self.failures = 0
        self.total_requests += 1
        self.state = "closed"
        self.half_open_in_flight = False
        if old_state != "closed":
            self._debug_fn(f"  [cb] state {old_state} → closed (success #{self.total_requests})")

    def record_failure(self):
        old_state = self.state
        self.failures += 1
        self.total_failures += 1
        self.total_requests += 1
        self.last_failure_time = time.monotonic()
        if self.state == "half_open":
            # Failure during half-open test → immediately reopen
            self.state = "open"
            self.half_open_in_flight = False
            self.opened_at = time.monotonic()
            self._debug_fn("  [cb] half_open → open (test request failed)")
        elif self.failures >= self._failure_threshold:
            self.state = "open"
            self.opened_at = time.monotonic()
            self._debug_fn(
                f"  [cb] {old_state} → open (failures={self.failures}/{self._failure_threshold})"
            )

    def should_allow(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.monotonic() - self.opened_at >= self._recovery_timeout:
                self.state = "half_open"
                # [Lot 2] le premier appelant devient la sonde.
                self.half_open_in_flight = self._probe_enabled()
                self.half_open_since = time.monotonic()
                self._debug_fn("  [cb] open → half_open (cooldown expired — sonde)")
                return True  # allow one test request
            remaining = self._recovery_timeout - (time.monotonic() - self.opened_at)
            self._debug_fn(f"  [cb] DENIED (state=open, cooldown={remaining:.0f}s remaining)")
            return False
        # half_open
        if self._probe_enabled():
            # Sonde unique : si une sonde est déjà en vol, rejeter (comme
            # open — CircuitOpenError → 503, comportement client existant).
            if self.half_open_in_flight:
                # Sonde présumée morte (requête annulée sans record_*) :
                # après 2×le cooldown, la sonde expire et peut être reprise.
                if time.monotonic() - self.half_open_since >= 2 * self._recovery_timeout:
                    self.half_open_since = time.monotonic()
                    self._debug_fn("  [cb] half_open probe expired — nouvelle sonde")
                    return True
                self._debug_fn("  [cb] DENIED (half_open probe in flight — reject as open)")
                return False
            # État half_open sans sonde marquée (transition héritée, hot
            # reload du flag, test) : prendre la sonde.
            self.half_open_in_flight = True
            self.half_open_since = time.monotonic()
            return True
        # Rollback : comportement historique « tout le monde passe ».
        return True

    def get_status(self) -> dict:
        uptime = time.monotonic() - self.created_at
        return {
            "state": self.state,
            "failures": self.failures,
            "total_requests": self.total_requests,
            "total_failures": self.total_failures,
            "last_failure_time": self.last_failure_time,
            "uptime_seconds": round(uptime, 1),
        }


# Alias historique (opencode._CircuitBreaker, test_proxy.py) — la sous-classe
# hôte porte le même nom ; cet alias désigne la base pure.
_CircuitBreaker = CircuitBreaker


class Global429State:
    """État du coupe-circuit global anti-thundering-herd 429.

    ([PLAN-corrections-429 E3/P14] — extrait pur : l'hôte possède UNE instance
    construite avec les valeurs ``config.yaml`` et expose les wrappers
    ``_record_global_429`` / ``_global_429_remaining`` d'une ligne.)"""

    __slots__ = ("threshold", "window", "backoff", "_hits", "_open_until", "_debug_fn")

    def __init__(
        self,
        threshold: int = DEFAULT_GLOBAL_429_THRESHOLD,
        window: float = DEFAULT_GLOBAL_429_WINDOW,
        backoff: float = DEFAULT_GLOBAL_429_BACKOFF,
        *,
        debug_fn: Callable[..., None] = _noop_debug,
    ):
        self.threshold = threshold
        self.window = window
        self.backoff = backoff
        self._hits: deque = deque()
        self._open_until = 0.0
        self._debug_fn = debug_fn

    def record(self) -> None:
        now = time.monotonic()
        hits = self._hits
        hits.append(now)
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.threshold:
            count = len(hits)
            self._open_until = now + self.backoff
            hits.clear()
            self._debug_fn(
                f"  [g429] breaker OPEN: {count} upstream 429s in {self.window:.0f}s "
                f"→ backoff {self.backoff:.0f}s"
            )

    def remaining(self) -> float:
        return max(0.0, self._open_until - time.monotonic())


class Global429BackoffMiddleware:
    """[PLAN-corrections-429 E3/P14] Coupe-circuit global anti-thundering-herd.

    Quand trop de 429 upstream viennent d'être observés (fenêtre glissante),
    les nouvelles requêtes sont rejetées en 503 + Retry-After au lieu
    d'aller saturer l'upstream. Raw ASGI, zéro copie.

    ``remaining_fn`` injecté (hôte : ``_global_429_remaining`` — état global
    429 possédé par l'hôte)."""

    _SKIP_PREFIXES = ("/api/", "/static/", "/health")

    def __init__(self, app, *, remaining_fn: Callable[[], float] | None = None):
        self.app = app
        self._remaining_fn = remaining_fn or (lambda: 0.0)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope.get("path", "").startswith(self._SKIP_PREFIXES):
            wait = self._remaining_fn()
            if wait > 0:
                resp = JSONResponse(
                    {
                        "error": {
                            "message": "upstream 429 storm — global backoff active",
                            "type": "rate_limit_error",
                        }
                    },
                    status_code=503,
                    headers={"Retry-After": str(int(wait) + 1)},
                )
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


__all__ = [
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_GLOBAL_429_BACKOFF",
    "DEFAULT_GLOBAL_429_THRESHOLD",
    "DEFAULT_GLOBAL_429_WINDOW",
    "DEFAULT_RECOVERY_TIMEOUT",
    "CircuitBreaker",
    "CircuitOpenError",
    "Global429BackoffMiddleware",
    "Global429State",
    "_CircuitBreaker",
]
