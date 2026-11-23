"""Profil wire officiel et empreinte — 023-client-usable-egress (T006).

La capture active (``free/captures/2.3.0.json``) définit le corps, l'UA,
l'ordre des 16 en-têtes, la grille des 13 outils et l'empreinte TLS. Ce module
expose un constructeur d'empreinte et un contrôle de conformité, sans jamais
composer un corps parallèle à ``opencode._free_wire_body``.

Constitution XVIII : un ``wire_fp`` différent interdit de réutiliser une IP
prouvée avec un autre format.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CAPTURE = ROOT / "free" / "captures" / "2.3.0.json"
DEFAULT_IMPERSONATION = ROOT / "free" / "impersonation.json"

#: Clés officielles du corps chat/completions 2.3.0 (ordre de capture).
OFFICIAL_BODY_KEYS: tuple[str, ...] = (
    "model",
    "messages",
    "tools",
    "stream",
    "stream_options",
    "max_completion_tokens",
)

#: Clés tolérées en plus (jamais dans la capture, mais acceptées par le gate).
TOLERATED_KEYS: frozenset[str] = frozenset({"tool_choice"})

#: Clés interdites au niveau racine d'un corps chat officiel.
FORBIDDEN_ROOT_KEYS: frozenset[str] = frozenset({"system", "input", "prompt"})

_LOCK = threading.RLock()
_CACHE: dict[tuple[str, float], WireProfile] = {}
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class WireProfile:
    version: str
    endpoint: str
    ua: str
    header_order: tuple[str, ...]
    body_keys: tuple[str, ...]
    tool_names: tuple[str, ...]
    body_sha256: str
    tls_ja3_md5: str
    header_count: int
    captured_at: str

    @property
    def official_keys(self) -> frozenset[str]:
        return frozenset(self.body_keys)


@dataclass(frozen=True)
class Conformance:
    ok: bool
    missing_keys: tuple[str, ...] = ()
    extra_keys: tuple[str, ...] = ()
    missing_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    forbidden_root: tuple[str, ...] = ()
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "missing_keys": list(self.missing_keys),
            "extra_keys": list(self.extra_keys),
            "missing_tools": list(self.missing_tools),
            "forbidden_tools": list(self.forbidden_tools),
            "forbidden_root": list(self.forbidden_root),
            "detail": self.detail,
        }


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def load_profile(path: str | Path | None = None) -> WireProfile:
    """Charge la capture active (cache par chemin + mtime)."""
    p = Path(path) if path else DEFAULT_CAPTURE
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0
    key = (str(p), mtime)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
    try:
        data = _load_json(p)
    except (OSError, ValueError) as exc:
        # Une capture manquante/corrompue est un défaut de configuration, pas un
        # repli anodin : on le journalise AVANT de propager. Cacher cette erreur
        # (p. ex. ``_note_current_wire_fp`` renvoie "") ferait perdre le
        # ``wire_fp`` qui distingue une brûlure de format d'un quota épuisé.
        _LOG.error("wire_profile: cannot load capture %s: %s", p, exc)
        raise
    imp = {}
    try:
        imp = _load_json(DEFAULT_IMPERSONATION)
    except Exception as exc:
        _LOG.warning(
            "wire_profile: impersonation.json unavailable (%s); "
            "falling back to capture/defaults",
            exc,
        )
        imp = {}
    headers = data.get("headers") or imp.get("headers") or []
    if isinstance(headers, dict):
        order = tuple(str(k) for k in headers.keys())
    else:
        order = tuple(str(h) for h in headers)
    if not order:
        _LOG.warning(
            "wire_profile: empty header order for %s; wire_fp will ignore headers",
            p,
        )
    body_raw = data.get("body_raw") or ""
    body_keys = data.get("body_keys") or []
    if not body_keys:
        try:
            body_keys = list(json.loads(body_raw).keys())
        except Exception as exc:
            _LOG.warning(
                "wire_profile: body_keys unparsable for %s (%s); "
                "falling back to OFFICIAL_BODY_KEYS",
                p,
                exc,
            )
            body_keys = list(OFFICIAL_BODY_KEYS)
    tool_names = data.get("tool_names") or []
    if not tool_names:
        try:
            tools = json.loads(body_raw).get("tools") or []
            tool_names = [
                str((t.get("function") or {}).get("name") or t.get("name") or "") for t in tools
            ]
        except Exception as exc:
            _LOG.warning(
                "wire_profile: tool_names unparsable for %s (%s); "
                "conformance will not require official tools",
                p,
                exc,
            )
            tool_names = []
    tls = data.get("tls") or imp.get("tls") or {}
    profile = WireProfile(
        version=str(data.get("version") or imp.get("version") or "2.3.0"),
        endpoint=str(data.get("endpoint") or imp.get("endpoint") or "/v1/chat/completions"),
        ua=str(data.get("user_agent") or imp.get("user_agent") or "opencode/latest/2.3.0/cli"),
        header_order=order,
        body_keys=tuple(str(k) for k in body_keys),
        tool_names=tuple(str(n) for n in tool_names if n),
        body_sha256=str(data.get("body_sha256") or ""),
        tls_ja3_md5=str(tls.get("ja3_md5") or tls.get("ja3") or ""),
        header_count=int(data.get("header_count") or len(order) or 0),
        captured_at=str(data.get("captured_at") or ""),
    )
    with _LOCK:
        _CACHE[key] = profile
    return profile


def reset_cache_for_tests() -> None:
    with _LOCK:
        _CACHE.clear()


def canonical_body(body: Any) -> str:
    """Sérialisation compacte, ordre des clés préservé."""
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8", "replace")
    if isinstance(body, str):
        return body
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def fingerprint(
    body: Any,
    *,
    ua: str = "",
    header_order: tuple[str, ...] | list[str] = (),
    tls_ja3_md5: str = "",
) -> str:
    """``wire_fp`` = sha256(corps canonique + UA + ordre d'en-têtes + JA3)."""
    h = hashlib.sha256()
    h.update(canonical_body(body).encode("utf-8"))
    h.update(b"\x1f")
    h.update(str(ua or "").encode("utf-8"))
    h.update(b"\x1f")
    h.update("|".join(str(x).lower() for x in header_order).encode("utf-8"))
    h.update(b"\x1f")
    h.update(str(tls_ja3_md5 or "").encode("utf-8"))
    return h.hexdigest()


def fingerprint_wire(wire: Any, profile: WireProfile | None = None) -> str:
    prof = profile or load_profile()
    return fingerprint(
        wire,
        ua=prof.ua,
        header_order=prof.header_order,
        tls_ja3_md5=prof.tls_ja3_md5,
    )


def _tool_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ""
    fn = tool.get("function")
    if isinstance(fn, dict):
        return str(fn.get("name") or "").strip()
    return str(tool.get("name") or "").strip()


def tool_names(wire: Any) -> list[str]:
    if not isinstance(wire, dict):
        return []
    tools = wire.get("tools")
    if not isinstance(tools, list):
        return []
    return [n for n in (_tool_name(t) for t in tools) if n]


def conformance(wire: Any, profile: WireProfile | None = None) -> Conformance:
    """Contrôle structurel d'un corps avant envoi sur une IP prouvée."""
    prof = profile or load_profile()
    if not isinstance(wire, dict):
        return Conformance(ok=False, detail="wire is not an object")
    keys = list(wire.keys())
    missing_keys = tuple(k for k in prof.body_keys if k not in wire)
    tolerated = set(prof.body_keys) | set(TOLERATED_KEYS)
    extra_keys = tuple(k for k in keys if k not in tolerated)
    forbidden_root = tuple(k for k in keys if k in FORBIDDEN_ROOT_KEYS)
    actual_tools = tool_names(wire)
    missing_tools = tuple(n for n in prof.tool_names if n not in actual_tools)
    forbidden_tools: tuple[str, ...] = ()
    if actual_tools:
        from free.clients.tool_policy import forbidden_names

        bad = set(forbidden_names())
        forbidden_tools = tuple(n for n in actual_tools if n.lower() in bad)
    ok = not (missing_keys or extra_keys or forbidden_root or missing_tools or forbidden_tools)
    detail = "" if ok else "wire differs from the official profile"
    return Conformance(
        ok=ok,
        missing_keys=missing_keys,
        extra_keys=extra_keys,
        missing_tools=missing_tools,
        forbidden_tools=forbidden_tools,
        forbidden_root=forbidden_root,
        detail=detail,
    )


def is_conformant(wire: Any, profile: WireProfile | None = None) -> bool:
    return conformance(wire, profile).ok
