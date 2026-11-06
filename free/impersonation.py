"""Runtime impersonation spec for the free leg.

``scripts/opencode_client_sync.py`` writes ``free/impersonation.json`` after
capturing the latest official client.  This module applies the volatile parts
(UA, auth fallback, tracing/session headers) to a header dict at request time.

The console credential, when present, still wins on Authorization: the spec is
the anonymous fallback and the format reference.
"""

from __future__ import annotations

import json
import secrets
import threading
from collections import OrderedDict
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parent / "impersonation.json"

#: Trace ID stable par session (comme le client 2.2.5) ; le span change à
#: chaque requête. LRU bornée pour ne pas fuir sur un proxy 24/7.
_TRACE_IDS: OrderedDict[str, str] = OrderedDict()
_TRACE_LOCK = threading.Lock()
_TRACE_MAX = 512


def load_spec() -> dict:
    try:
        return json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _trace_ids(session: str = "") -> tuple[str, str]:
    trace = ""
    sid = str(session or "").strip()
    if sid:
        with _TRACE_LOCK:
            trace = _TRACE_IDS.get(sid, "")
            if trace:
                _TRACE_IDS.move_to_end(sid)
            else:
                trace = secrets.token_hex(16)
                _TRACE_IDS[sid] = trace
                while len(_TRACE_IDS) > _TRACE_MAX:
                    _TRACE_IDS.popitem(last=False)
    span = secrets.token_hex(8)
    return trace or secrets.token_hex(16), span


def apply_spec(headers: dict, session: str) -> dict:
    spec = load_spec()
    if not spec:
        return headers
    user_agent = str(spec.get("user_agent") or "").strip()
    if user_agent:
        headers["User-Agent"] = user_agent
    client = str(spec.get("client") or "").strip()
    if client:
        headers["x-opencode-client"] = client
    project = str(spec.get("project") or "").strip()
    # The capture's project is only a template/base. Never inject the real
    # repository hash into an identity whose project is ``global`` or absent:
    # that would link every such station to the operator's repo. A real client
    # outside git sends exactly ``global``.
    current_project = str(headers.get("x-opencode-project") or "").strip()
    if project and not current_project:
        headers["x-opencode-project"] = "global"
    value = str(spec.get("authorization_value") or "").strip()
    if value:
        scheme = str(spec.get("authorization_scheme") or "Bearer").strip()
        headers["Authorization"] = f"{scheme} {value}"
    header_names = {str(name).lower() for name in spec.get("headers") or []}
    if "x-opencode-request" not in header_names:
        headers.pop("x-opencode-request", None)
    if "b3" in header_names or "traceparent" in header_names:
        trace, span = _trace_ids(session)
        if "b3" in header_names:
            headers["b3"] = f"{trace}-{span}-1-{secrets.token_hex(8)}"
        if "traceparent" in header_names:
            headers["traceparent"] = f"00-{trace}-{span}-01"
    if "x-session-affinity" in header_names:
        headers["x-session-affinity"] = session
    if "x-session-id" in header_names:
        headers["x-session-id"] = session
    if "x-opencode-session-id" in header_names:
        headers["x-opencode-session-id"] = session
    return headers
