"""HTTP client for the Gluetun 016 control plane.

Design rules from ``contracts/orchestrator-integration.md``:

* ordered candidates are forwarded verbatim; the client never sends a score or
  a quota and never re-orders the list;
* every ``switch``/``rotate`` carries an ``Idempotency-Key`` so a client
  timeout followed by a retry cannot create a second job;
* only ``429``, ``502``, ``503``, ``504`` and connection/timeouts are retried;
* business ``4xx`` (``403``, ``409``, ``422``) are raised immediately;
* the API key is read from the environment and never rendered.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

import httpx

from .errors import (
    ActiveJob,
    ConfigError,
    ControlPlaneError,
    ControlPlaneTimeout,
    ControlPlaneUnavailable,
    LegacyFallback,
    UnknownTarget,
    VerificationFailed,
)
from .models import (
    Capabilities,
    Constraints,
    ControlEvent,
    DNSState,
    EgressVerification,
    EventsPage,
    ServerCandidate,
    SwitchJob,
    TransportState,
    TunnelState,
    TunnelTarget,
)

RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
FALLBACK_STATUS = frozenset({502, 503, 504})


def legacy_fallback_reason(error: ControlPlaneError) -> str:
    """Return a reason when the cold legacy path may run, else ``""``.

    Only transport-grade failures are eligible: a timeout, an unreachable
    fork, or a retryable 5xx exhausted after the retry budget.  Business
    rejections (403, 409, 422) and 429 quotas are deterministic and must never
    trigger a legacy cut.
    """

    if isinstance(error, ControlPlaneTimeout):
        return "timeout"
    if isinstance(error, ControlPlaneUnavailable):
        return error.code or "unavailable"
    status = int(error.status or 0)
    if status in FALLBACK_STATUS:
        return f"http_{status}"
    return ""


def legacy_fallback(error: ControlPlaneError) -> LegacyFallback:
    """Turn an eligible error into the explicit cold-path instruction."""

    reason = legacy_fallback_reason(error)
    if not reason:
        raise error
    return LegacyFallback(reason, cause=error)


TERMINAL_STATES = frozenset(
    {
        "committed",
        "rejected",
        "rolled_back",
        "failed_degraded",
        "exhausted",
        "cancelled",
        "interrupted",
    }
)


def _seconds(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _require_key(value: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise ValueError("idempotency_key must not be empty")
    return key


class GluetunControlClient:
    """Typed, idempotent, bounded-retry client for the control plane API."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        api_key_env: str = "GLUETUN_CONTROL_API_KEY",
        station: int | str | None = None,
        timeout_s: float = 10.0,
        switch_timeout_s: float = 60.0,
        rotate_timeout_s: float = 180.0,
        verify_timeout_s: float = 10.0,
        retry_max_attempts: int = 3,
        retry_backoff_s: float = 0.5,
        retry_max_backoff_s: float = 4.0,
        poll_interval_s: float = 1.0,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        template = str(base_url or "").strip().rstrip("/")
        if not template:
            raise ConfigError("base_url must not be empty")
        station_text = "" if station is None else str(station).strip()
        if "{station}" in template and not station_text:
            raise ConfigError("base_url contains {station} but no station was given")
        self._base_url = template.replace("{station}", station_text)
        self._station = station_text
        self._api_key = str(api_key or "").strip()
        self._api_key_env = str(api_key_env or "GLUETUN_CONTROL_API_KEY")
        self._environ = environ if environ is not None else os.environ
        self._timeout_s = max(0.1, float(timeout_s))
        self._switch_timeout_s = max(1.0, float(switch_timeout_s))
        self._rotate_timeout_s = max(1.0, float(rotate_timeout_s))
        self._verify_timeout_s = max(0.1, float(verify_timeout_s))
        self._retry_max_attempts = max(1, int(retry_max_attempts))
        self._retry_backoff_s = max(0.0, float(retry_backoff_s))
        self._retry_max_backoff_s = max(self._retry_backoff_s, float(retry_max_backoff_s))
        self._poll_interval_s = max(0.05, float(poll_interval_s))
        self._sleep = sleep
        self._now = now
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(follow_redirects=False)

    def __repr__(self) -> str:
        return f"GluetunControlClient(base_url={self._base_url!r}, station={self._station!r})"

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> GluetunControlClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── read methods ──────────────────────────────────────────────────
    def capabilities(self) -> Capabilities:
        return Capabilities.from_json(self._request("GET", "/v1/control/capabilities"))

    def state(self) -> TunnelState:
        return TunnelState.from_json(self._request("GET", "/v1/vpn/state"))

    def job(self, job_id: str) -> SwitchJob:
        job_id = str(job_id or "").strip()
        if not job_id:
            raise ValueError("job_id must not be empty")
        payload = self._request("GET", f"/v1/vpn/jobs/{quote(job_id, safe='')}")
        return SwitchJob.from_json(payload)

    def wait_for_active_job(
        self,
        error: ActiveJob,
        *,
        timeout_s: float | None = None,
    ) -> SwitchJob:
        """Follow the job named by a 409 instead of creating a second one."""

        job_id = str(error.active_job_id or "").strip()
        if not job_id:
            raise error
        budget = self._wait_timeout(timeout_s, self._switch_timeout_s)
        deadline = self._now() + budget
        while self._now() < deadline:
            self._sleep(self._poll_interval_s)
            try:
                job = self.job(job_id)
            except ControlPlaneUnavailable:
                continue
            if job.terminal:
                return job
        raise error

    def egress(self, *, fresh: bool = False) -> EgressVerification:
        params = {"fresh": "true"} if fresh else None
        payload = self._request(
            "GET",
            "/v1/egress",
            params=params,
            timeout=self._verify_timeout_s,
        )
        verification = EgressVerification.from_json(payload)
        if verification is None:
            raise VerificationFailed("egress endpoint returned no verification")
        return verification

    def dns_state(self) -> DNSState:
        return DNSState.from_json(self._request("GET", "/v1/dns/state"))

    def transport_state(self) -> TransportState:
        return TransportState.from_json(self._request("GET", "/v1/vpn/transport"))

    def transport_reset(self, transport: str | None = None) -> TransportState:
        body: dict[str, Any] = {}
        if transport:
            body["transport"] = str(transport)
        payload = self._request("POST", "/v1/vpn/transport/reset", body=body)
        try:
            return self.transport_state()
        except ControlPlaneError:
            return TransportState.from_json(payload)

    def events(self, since: int | None = None, limit: int = 100) -> list[ControlEvent]:
        return list(self.events_page(since=since, limit=limit).items)

    def events_page(self, since: int | None = None, limit: int = 100) -> EventsPage:
        params: dict[str, Any] = {"limit": int(limit)}
        if since is not None:
            params["since"] = int(since)
        return EventsPage.from_json(self._request("GET", "/v1/events", params=params))

    # ── write methods ─────────────────────────────────────────────────
    def switch(
        self,
        target: TunnelTarget,
        constraints: Constraints | None = None,
        *,
        idempotency_key: str,
        wait: bool = True,
        transport_policy: str | list[str] | None = None,
        timeout_s: float | None = None,
        dry_run: bool = False,
        drain: bool = False,
        drain_timeout_s: float | None = None,
    ) -> SwitchJob:
        key = _require_key(idempotency_key)
        options: dict[str, Any] = {
            "wait": bool(wait),
            "dry_run": bool(dry_run),
        }
        wait_timeout = self._wait_timeout(timeout_s, self._switch_timeout_s)
        if wait and not dry_run:
            options["timeout_s"] = int(wait_timeout)
        if transport_policy is not None:
            options["transport_policy"] = transport_policy
        if drain:
            drain_options: dict[str, Any] = {"enabled": True}
            if drain_timeout_s:
                drain_options["timeout_s"] = int(drain_timeout_s)
            options["drain"] = drain_options

        body = {
            "target": target.to_json(),
            "constraints": (constraints or Constraints()).to_switch_json(),
            "options": options,
            "idempotency_key": key,
        }
        payload = self._request(
            "POST",
            "/v1/vpn/switch",
            body=body,
            idempotency_key=key,
            timeout=self._http_timeout(wait, wait_timeout),
        )
        job = self._job_from_payload(payload)
        if wait and not dry_run:
            job = self._await_job(job, wait_timeout)
        return job

    def rotate(
        self,
        candidates: list[ServerCandidate],
        constraints: Constraints | None = None,
        *,
        idempotency_key: str,
        wait: bool = False,
        transport_policy: str | list[str] | None = None,
        timeout_s: float | None = None,
    ) -> SwitchJob:
        key = _require_key(idempotency_key)
        ordered = [candidate.to_json() for candidate in candidates if str(candidate.hostname or "").strip()]
        if not ordered:
            raise ValueError("rotate needs at least one candidate")
        options: dict[str, Any] = {"wait": bool(wait)}
        wait_timeout = self._wait_timeout(timeout_s, self._rotate_timeout_s)
        if wait:
            options["timeout_s"] = int(wait_timeout)
        if transport_policy is not None:
            options["transport_policy"] = transport_policy
        body = {
            "candidates": ordered,
            "constraints": (constraints or Constraints()).to_rotate_json(),
            "options": options,
            "idempotency_key": key,
        }
        payload = self._request(
            "POST",
            "/v1/vpn/rotate",
            body=body,
            idempotency_key=key,
            timeout=self._http_timeout(wait, wait_timeout),
        )
        job = self._job_from_payload(payload)
        if wait:
            job = self._await_job(job, wait_timeout)
        return job

    def rollback(
        self,
        *,
        wait: bool = True,
        idempotency_key: str = "",
        timeout_s: float | None = None,
    ) -> SwitchJob:
        key = str(idempotency_key or "").strip()
        if not key:
            committed = ""
            try:
                committed = self.state().last_committed.get("job_id", "")
            except ControlPlaneError:
                committed = ""
            key = f"rollback-{self._station or 'local'}-{committed or 'none'}"
        wait_timeout = self._wait_timeout(timeout_s, self._switch_timeout_s)
        body = {
            "to": "last_committed",
            "wait": bool(wait),
            "idempotency_key": key,
        }
        if wait:
            body["timeout_s"] = int(wait_timeout)
        payload = self._request(
            "POST",
            "/v1/vpn/rollback",
            body=body,
            idempotency_key=key,
            timeout=self._http_timeout(wait, wait_timeout),
        )
        job = self._job_from_payload(payload)
        if wait:
            job = self._await_job(job, wait_timeout)
        return job

    def cancel(self, job_id: str) -> SwitchJob:
        job_id = str(job_id or "").strip()
        if not job_id:
            raise ValueError("job_id must not be empty")
        payload = self._request(
            "POST",
            f"/v1/vpn/jobs/{quote(job_id, safe='')}/cancel",
            body={},
        )
        return SwitchJob.from_json(payload)

    # ── internals ─────────────────────────────────────────────────────
    def _key(self) -> str:
        return self._api_key or str(self._environ.get(self._api_key_env, "") or "").strip()

    def _headers(self, idempotency_key: str = "") -> dict[str, str]:
        headers = {"Accept": "application/json"}
        key = self._key()
        if key:
            headers["X-API-Key"] = key
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    @staticmethod
    def _http_timeout(wait: bool, wait_timeout: float) -> float:
        if not wait:
            return 0.0  # filled by the caller with the generic timeout
        return wait_timeout + 135.0

    def _wait_timeout(self, value: float | None, fallback: float) -> float:
        if value is None or value <= 0:
            return fallback
        return min(float(value), 600.0)

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self._base_url + path

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        url = self._url(path)
        headers = self._headers(idempotency_key)
        content = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        request_timeout = self._timeout_s if timeout is None or timeout <= 0 else timeout

        last_error: BaseException | None = None
        for attempt in range(self._retry_max_attempts):
            retry_after = 0.0
            try:
                response = self._http.request(
                    method,
                    url,
                    content=content,
                    headers=headers,
                    params=params,
                    timeout=request_timeout,
                )
            except httpx.TimeoutException as exc:
                last_error = ControlPlaneTimeout(f"{method} {path} timed out after {request_timeout:.0f}s")
                last_error.__cause__ = exc
            except httpx.HTTPError as exc:
                last_error = ControlPlaneUnavailable(f"{method} {path} connection failed: {type(exc).__name__}")
                last_error.__cause__ = exc
            else:
                status = response.status_code
                if status < 400:
                    if status == 204 or not response.content:
                        return {}
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise ControlPlaneError(
                            f"{method} {path} returned invalid JSON",
                            status=status,
                        ) from exc
                    return payload if isinstance(payload, dict) else {}
                error = self._error_for(response, method, path)
                if status not in RETRYABLE_STATUS:
                    raise error
                last_error = error
                retry_after = _seconds(response.headers.get("Retry-After"))
                if status == 429 and retry_after <= 0:
                    retry_after = 1.0

            if attempt + 1 >= self._retry_max_attempts:
                break
            delay = retry_after
            if delay <= 0:
                delay = min(
                    self._retry_backoff_s * (2**attempt),
                    self._retry_max_backoff_s,
                )
            else:
                delay = min(delay, self._retry_max_backoff_s or delay)
            self._sleep(delay)

        if isinstance(last_error, ControlPlaneError):
            raise last_error
        raise ControlPlaneUnavailable(
            f"{method} {path} failed after {self._retry_max_attempts} attempts"
        ) from last_error

    @staticmethod
    def _error_for(response: httpx.Response, method: str, path: str) -> ControlPlaneError:
        status = response.status_code
        payload: dict[str, Any] = {}
        try:
            decoded = response.json()
            if isinstance(decoded, dict):
                payload = decoded
        except ValueError:
            payload = {}
        detail = payload.get("error") if isinstance(payload, dict) else None
        detail = detail if isinstance(detail, dict) else {}
        code = str(detail.get("code", ""))
        message = str(detail.get("message", "") or f"{method} {path} returned HTTP {status}")
        details = detail.get("details")
        details = details if isinstance(details, dict) else {}

        if code == "active_job":
            job_id = details.get("active_job_id") or detail.get("job_id") or ""
            return ActiveJob(
                message,
                active_job_id=str(job_id),
                status=status,
                code=code or "active_job",
                details=details,
            )
        if code in ("unknown_target", "invalid_target", "endpoint_ip_mismatch"):
            return UnknownTarget(message, status=status, code=code, details=details)
        if code == "insufficient_sources":
            return VerificationFailed(
                message,
                verification=details.get("verification"),
                status=status,
                code=code,
                details=details,
            )
        return ControlPlaneError(message, status=status, code=code, details=details)

    def _job_from_payload(self, payload: dict[str, Any]) -> SwitchJob:
        job_payload = payload.get("job")
        if isinstance(job_payload, dict):
            return SwitchJob.from_json(job_payload)
        # Dry-run: {state: "planned", plan, would_cut_tunnel}; noop:
        # {state: "noop", job: null}; job responses are the job object itself.
        return SwitchJob.from_json(payload)

    def _await_job(self, job: SwitchJob, wait_timeout: float) -> SwitchJob:
        if job.terminal or not job.id:
            return job
        deadline = self._now() + wait_timeout + 5.0
        while self._now() < deadline:
            self._sleep(self._poll_interval_s)
            try:
                current = self.job(job.id)
            except ControlPlaneUnavailable:
                continue
            if current.terminal:
                return current
            job = current
        return job
