"""Declarative sources and parsers for the egress catalog.

Adding a public list is a configuration row, not a module.  Parsers are
pure functions over text/JSON; the only network call is :func:`fetch_text`,
which accepts an injected fetcher for tests.
"""

from __future__ import annotations

import ipaddress
import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SOURCE_TYPES = ("public_text", "public_json", "geonode_json", "local_text", "warp", "nordvpn")
ALLOWED_SCHEMES = ("http", "https", "socks5", "socks5h")


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    id: str
    type: str
    url: str = ""
    scheme: str = ""
    enabled: bool = True
    interval_s: int = 21_600
    priority: int = 0
    notes: str = ""
    tags: tuple[str, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, raw: dict[str, Any], *, default_interval: int = 21_600) -> SourceDefinition:
        data = raw if isinstance(raw, dict) else {}
        extra = data.get("config") if isinstance(data.get("config"), dict) else {}
        source_type = str(data.get("type") or "").strip().lower()
        if source_type not in SOURCE_TYPES:
            raise ValueError(f"type de source inconnu: {source_type!r}")
        source_id = str(data.get("id") or "").strip()
        if not source_id:
            raise ValueError("source sans id")
        url = str(data.get("url") or "").strip()
        if url and source_type != "local_text":
            url = validate_source_url(url)
        tags = data.get("tags", extra.get("tags", ()))
        if isinstance(tags, str):
            tags = (tags,)
        return cls(
            id=source_id,
            type=source_type,
            url=url,
            scheme=str(data.get("scheme") or "").strip().lower(),
            enabled=bool(data.get("enabled", True)),
            interval_s=max(60, int(data.get("interval_s") or default_interval)),
            priority=int(data.get("priority", extra.get("priority", 0)) or 0),
            notes=str(data.get("notes", extra.get("notes", "")) or ""),
            tags=tuple(str(tag) for tag in tags or ()),
            config=dict(extra),
        )


def parse_source_definitions(
    items: Iterable[dict[str, Any]] | None,
    *,
    default_interval: int = 21_600,
) -> list[SourceDefinition]:
    sources: list[SourceDefinition] = []
    seen: set[str] = set()
    for raw in items or ():
        try:
            source = SourceDefinition.from_config(raw, default_interval=default_interval)
        except (TypeError, ValueError):
            continue
        if source.id in seen:
            continue
        seen.add(source.id)
        sources.append(source)
    return sorted(sources, key=lambda source: source.priority, reverse=True)


def normalize_proxy(value: str, *, default_scheme: str = "http") -> dict[str, Any] | None:
    """Validate one proxy candidate and reject private/loopback targets."""

    text = str(value or "").strip()
    if not text or text.startswith("#"):
        return None
    if "://" not in text:
        # zloi/hideip.me: ``ip:port:Country``. Keep the first two fields.
        parts = text.split(":")
        if len(parts) == 3 and parts[1].isdigit() and not parts[2].isdigit():
            text = f"{parts[0]}:{parts[1]}"
        text = f"{default_scheme}://{text}"
    try:
        parsed = urllib.parse.urlsplit(text)
        scheme = parsed.scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            return None
        host = (parsed.hostname or "").strip()
        port = int(parsed.port or (443 if scheme == "https" else 80))
    except (TypeError, ValueError):
        return None
    if not host or not (1 <= port <= 65_535):
        return None
    if not _public_host(host):
        return None
    netloc = f"[{host}]" if ":" in host else host
    url = f"{scheme}://{netloc}:{port}"
    if parsed.username:
        user = urllib.parse.quote(parsed.username, safe="")
        password = urllib.parse.quote(parsed.password or "", safe="")
        url = f"{scheme}://{user}:{password}@{netloc}:{port}"
    return {
        "url": url,
        "scheme": scheme,
        "host": host,
        "port": port,
        "ipv6": ":" in host,
    }


def _public_host(host: str) -> bool:
    lowered = host.lower().rstrip(".")
    if lowered in ("localhost", "localhost.localdomain"):
        return False
    if lowered.endswith(".local") or lowered.endswith(".internal"):
        return False
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        # A DNS name is allowed; the worker still resolves it in isolation.
        return "." in lowered or lowered.startswith("xn--")
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def parse_public_text(text: str, *, default_scheme: str = "http") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in str(text or "").splitlines():
        candidate = normalize_proxy(line, default_scheme=default_scheme)
        if candidate:
            rows.append(candidate)
    return rows


def parse_public_json(text: str, *, default_scheme: str = "http") -> list[dict[str, Any]]:
    try:
        payload = json.loads(str(text or ""))
    except (TypeError, ValueError):
        return []
    return _json_candidates(payload, default_scheme=default_scheme)


def _json_candidates(payload: Any, *, default_scheme: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("data", "proxies", "results", "items"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in payload:
        candidate = _json_candidate(item, default_scheme=default_scheme)
        if candidate:
            rows.append(candidate)
    return rows


def _json_candidate(item: Any, *, default_scheme: str) -> dict[str, Any] | None:
    if isinstance(item, str):
        return normalize_proxy(item, default_scheme=default_scheme)
    if not isinstance(item, dict):
        return None
    raw = (
        item.get("proxy")
        or item.get("url")
        or item.get("address")
        or item.get("server")
        or ""
    )
    if raw:
        if "://" not in str(raw) and item.get("port"):
            raw = f"{raw}:{item['port']}"
        return normalize_proxy(str(raw), default_scheme=str(item.get("scheme") or default_scheme))
    host = str(item.get("ip") or item.get("host") or "").strip()
    port = item.get("port")
    if not host or not port:
        return None
    protocol = item.get("protocol") or item.get("scheme")
    protocols = item.get("protocols")
    if not protocol and isinstance(protocols, list) and protocols:
        protocol = protocols[0]
    if not protocol:
        # vakhov/fresh-proxy-list uses boolean-ish flags instead of a list.
        if _enabled_flag(item.get("socks5")):
            protocol = "socks5"
        elif _enabled_flag(item.get("ssl")):
            protocol = "https"
        elif _enabled_flag(item.get("http")):
            protocol = "http"
        elif _enabled_flag(item.get("socks4")):
            return None
    scheme = str(protocol or default_scheme).lower()
    if scheme == "socks5h":
        scheme = "socks5"
    return normalize_proxy(f"{scheme}://{host}:{int(port)}", default_scheme=default_scheme)


def _enabled_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    try:
        return int(value) == 1
    except (TypeError, ValueError):
        return str(value).strip().lower() in ("1", "true", "yes", "on")


def parse_geonode_json(text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(str(text or ""))
    except (TypeError, ValueError):
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        data = payload if isinstance(payload, list) else []
    rows: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        protocols = item.get("protocols") or []
        protocol = "socks5" if any(str(p).lower().startswith("socks5") for p in protocols) else "http"
        candidate = _json_candidate(
            {"ip": item.get("ip"), "port": item.get("port"), "protocol": protocol},
            default_scheme=protocol,
        )
        if candidate:
            rows.append(candidate)
    return rows


def parse_payload(source: SourceDefinition, text: str) -> list[dict[str, Any]]:
    if source.type in ("public_text", "local_text"):
        return parse_public_text(text, default_scheme=source.scheme or "http")
    if source.type == "public_json":
        return parse_public_json(text, default_scheme=source.scheme or "http")
    if source.type == "geonode_json":
        return parse_geonode_json(text)
    return []


def dedupe_candidates(
    candidates: Iterable[dict[str, Any]],
    *,
    limit: int = 20_000,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        key = (
            str(candidate.get("scheme") or ""),
            str(candidate.get("host") or "").lower(),
            int(candidate.get("port") or 0),
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(candidate)
        if len(rows) >= max(1, int(limit)):
            break
    return rows


def interleave_by_source(
    candidates: Iterable[dict[str, Any]],
    *,
    limit: int = 20_000,
) -> list[dict[str, Any]]:
    """Round-robin candidates across sources so a huge list cannot starve
    SOCKS5 (or any other) sources from the per-cycle window."""

    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        source_id = str(candidate.get("source_id") or "")
        if source_id not in groups:
            groups[source_id] = []
            order.append(source_id)
        groups[source_id].append(candidate)
    out: list[dict[str, Any]] = []
    index = 0
    cap = max(1, int(limit))
    while len(out) < cap:
        added = False
        for source_id in order:
            rows = groups[source_id]
            if index >= len(rows):
                continue
            out.append(rows[index])
            added = True
            if len(out) >= cap:
                return out
        if not added:
            break
        index += 1
    return out


def validate_source_url(url: str) -> str:
    """Reject non-HTTP and local/private source URLs (SSRF guard)."""

    value = str(url or "").strip()
    if not value:
        raise ValueError("url vide")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"schéma de source interdit: {parsed.scheme!r}")
    host = (parsed.hostname or "").strip()
    if not host or not _public_host(host):
        raise ValueError(f"hôte de source interdit: {host!r}")
    return value


def load_source_file(
    path: str | Path,
    *,
    default_interval: int = 21_600,
) -> list[SourceDefinition]:
    """Load the external source catalog (YAML/JSON) next to config.yaml."""

    target = Path(path)
    if not target.is_absolute():
        target = Path(__file__).resolve().parent.parent / target
    if not target.is_file():
        return []
    try:
        import yaml

        payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    rows = payload.get("sources") if isinstance(payload, dict) else payload
    return parse_source_definitions(rows if isinstance(rows, list) else [], default_interval=default_interval)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that would turn a source fetch into SSRF."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_source_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_text(
    url: str,
    *,
    timeout: float = 20.0,
    fetcher: Callable[[str, float], str] | None = None,
) -> str:
    target = validate_source_url(url)
    if fetcher is not None:
        return fetcher(target, timeout)
    request = urllib.request.Request(
        target,
        headers={"User-Agent": "opencode-proxy-egress-catalog/1.0"},
    )
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    with opener.open(request, timeout=max(1.0, float(timeout))) as response:  # noqa: S310
        validate_source_url(response.geturl())
        return response.read().decode("utf-8", "replace")


def read_source_text(
    source: SourceDefinition,
    *,
    timeout: float = 20.0,
    fetcher: Callable[[str, float], str] | None = None,
) -> str:
    if source.type == "local_text":
        target = Path(source.url)
        if not target.is_absolute():
            target = Path(__file__).resolve().parent.parent / target
        return target.read_text(encoding="utf-8", errors="replace")
    return fetch_text(source.url, timeout=timeout, fetcher=fetcher)


def source_signature(source: SourceDefinition) -> str:
    return f"{source.id}|{source.type}|{source.url}|{source.scheme}|{int(time.time()) // max(1, source.interval_s)}"


__all__ = [
    "ALLOWED_SCHEMES",
    "SOURCE_TYPES",
    "SourceDefinition",
    "dedupe_candidates",
    "fetch_text",
    "interleave_by_source",
    "load_source_file",
    "normalize_proxy",
    "parse_geonode_json",
    "parse_payload",
    "parse_public_json",
    "parse_public_text",
    "parse_source_definitions",
    "read_source_text",
    "source_signature",
    "validate_source_url",
]
