"""Identité par station (P0) : project, trace, session, prompt, affinité.

Le provider ne doit pas pouvoir corréler deux stations comme la même
installation. Ce module dérive les identifiants d'un secret local et du label
de station — jamais de l'IP seule, du workspace réel ou du hash du dépôt.
Aucune I/O réseau ; le vault et l'affinité sont testables hors ligne.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from free.identities import _mint
from free.profile_registry import VerifiedProfile

logger = logging.getLogger(__name__)

PROJECT_LEN = 40
TRACE_LEN = 32
SECRET_LEN = 32
CONV_LRU_MAX = 128
ENV_PREFIX = "/workspace/"
DEFAULT_SECRET_PATH = Path(__file__).resolve().parent.parent / "logs" / "identity_secret"

_ENV_RE = re.compile(r"<env>.*?</env>", re.DOTALL)
_DATE_RE = re.compile(r"Today's date:\s*[^\n]*")
_INSTRUCTIONS_RE = re.compile(
    r"Instructions from:\s*[^\n]*\n.*?(?=\nToday's date:|\n<env>|\Z)",
    re.DOTALL,
)
_WIN_USER_PATH_RE = re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\s`\"'<>|]+(?:[\\/][^\s`\"'<>|]+)*")
_LABEL_RE = re.compile(r"[^A-Za-z0-9._-]+")
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


class IdentitySecretError(ValueError):
    """Secret absent, illisible ou trop court : le multi-station est refusé."""


def safe_label(label: str) -> str:
    cleaned = _LABEL_RE.sub("-", str(label or "")).strip("-._")
    return cleaned[:48] or "station"


def load_or_create_secret(path: str | Path | None = None) -> bytes:
    target = Path(path) if path is not None else DEFAULT_SECRET_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = target.read_text(encoding="ascii").strip()
        data = bytes.fromhex(raw)
        if len(data) < SECRET_LEN:
            raise IdentitySecretError(f"secret trop court: {len(data)} octets")
        return data[:SECRET_LEN]
    except FileNotFoundError:
        data = secrets.token_bytes(SECRET_LEN)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return load_or_create_secret(target)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(data.hex())
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return data
    except (OSError, ValueError) as exc:
        if isinstance(exc, IdentitySecretError):
            raise
        raise IdentitySecretError(f"secret illisible: {target}") from exc


def _hmac_hex(secret: bytes, purpose: str, label: str, length: int) -> str:
    digest = hmac.new(secret, f"{purpose}:{label}".encode(), hashlib.sha256).hexdigest()
    return digest[:length]


def _utc_date(now: datetime | None = None) -> str:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.strftime("%a %b %d %Y")


def sanitize_text(text: str, env_cwd: str) -> str:
    """Retire le bloc d'instructions locales et remplace les chemins réels."""
    out = _INSTRUCTIONS_RE.sub("", str(text or ""), count=1)
    return _WIN_USER_PATH_RE.sub(lambda _match: env_cwd, out)


def render_system_prompt(
    base: str,
    env_cwd: str,
    *,
    now: datetime | None = None,
) -> str:
    """Conserve le marqueur officiel, remplace la date et le bloc `<env>`."""
    if not str(env_cwd).startswith(ENV_PREFIX):
        raise ValueError(f"env_cwd doit commencer par {ENV_PREFIX!r}: {env_cwd!r}")
    text = sanitize_text(base, env_cwd)
    env_block = f"<env>\n  Working directory: {env_cwd}\n</env>"
    if _ENV_RE.search(text):
        text = _ENV_RE.sub(lambda _match: env_block, text, count=1)
    else:
        text = text.rstrip() + "\n\n" + env_block
    date_line = f"Today's date: {_utc_date(now)}"
    if _DATE_RE.search(text):
        text = _DATE_RE.sub(lambda _match: date_line, text, count=1)
    else:
        text = text.replace("<env>", date_line + "\n<env>", 1)
    return text


def audit_prompt(prompt: str, forbidden: Iterable[str]) -> list[str]:
    text = str(prompt or "")
    return [str(marker) for marker in forbidden if marker and str(marker) in text]


def _sanitize_value(value, env_cwd: str):
    if isinstance(value, str):
        return sanitize_text(value, env_cwd)
    if isinstance(value, list):
        return [_sanitize_value(item, env_cwd) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_value(item, env_cwd) for key, item in value.items()}
    return value


def template_official_body(
    body: dict,
    identity: StationIdentity,
    *,
    now: datetime | None = None,
) -> dict:
    """Copie le corps, nettoie tous les chemins et remplace le prompt système
    par sa version templatée. Les messages utilisateur/outils sont conservés
    après sanitation."""
    out = _sanitize_value(body, identity.env_cwd)
    messages = out.get("messages") if isinstance(out, dict) else None
    if not isinstance(messages, list):
        return out
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "system":
            content = str(message.get("content") or "")
            if "<env>" in content or "Today's date:" in content or "Instructions from:" in content:
                message["content"] = identity.prompt(content, now=now)
            break
    return out


@dataclass
class StationIdentity:
    label: str
    secret: bytes
    version: str = "2.2.5"
    client: str = "cli"
    project: str = field(init=False)
    trace: str = field(init=False)
    env_cwd: str = field(init=False)
    _id_state: dict = field(init=False, repr=False)
    _conv: OrderedDict[str, str] = field(init=False, repr=False)
    _lock: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if len(self.secret) < SECRET_LEN:
            raise IdentitySecretError("secret trop court")
        self.label = str(self.label or "").strip() or "station"
        self.project = _hmac_hex(self.secret, "project", self.label, PROJECT_LEN)
        self.trace = _hmac_hex(self.secret, "trace", self.label, TRACE_LEN)
        self.env_cwd = ENV_PREFIX + safe_label(self.label)
        self._id_state = {"last_ms": 0, "counter": 0, "lock": threading.Lock()}
        self._conv = OrderedDict()
        self._lock = threading.Lock()

    def session_for(self, conversation_key: str | None = None) -> str:
        if not conversation_key:
            return _mint("ses", "descending", self._id_state)
        with self._lock:
            sid = self._conv.get(str(conversation_key))
            if sid:
                self._conv.move_to_end(str(conversation_key))
                return sid
            sid = _mint("ses", "descending", self._id_state)
            self._conv[str(conversation_key)] = sid
            while len(self._conv) > CONV_LRU_MAX:
                self._conv.popitem(last=False)
            return sid

    def new_msg(self) -> str:
        return _mint("msg", "ascending", self._id_state)

    def prompt(self, base: str, *, now: datetime | None = None) -> str:
        return render_system_prompt(base, self.env_cwd, now=now)

    def headers(
        self,
        profile: VerifiedProfile | None = None,
        *,
        conversation_key: str | None = None,
        endpoint: str = "",
        include_transport: bool = False,
        wire_style: str = "auto",
    ) -> dict[str, str]:
        version = str(profile.version if profile is not None else self.version)
        client = str(profile.client if profile is not None else self.client)
        user_agent = str(profile.user_agent if profile is not None else "")
        if not user_agent:
            user_agent = "opencode/latest/2.2.5/cli"
        style = str(wire_style or "auto")
        if style == "auto":
            if version == "1.18.31":
                style = "legacy18"
            elif version in ("1.19.0", "1.19.3"):
                style = "legacy19"
            else:
                style = "official"
        session = self.session_for(conversation_key)
        out: dict[str, str] = {
            "Authorization": "Bearer public",
            "Content-Type": "application/json",
            "User-Agent": user_agent,
        }
        if style == "legacy18":
            out["x-session-affinity"] = session
            out["x-session-id"] = session
        elif style == "legacy19":
            out["x-opencode-session-id"] = session
            out["x-session-affinity"] = session
            out["x-session-id"] = session
        else:
            request = self.new_msg()
            out["x-opencode-client"] = client
            out["x-opencode-project"] = self.project
            out["x-opencode-request"] = request
            out["x-opencode-session"] = session
            out["x-opencode-session-id"] = session
            if version == "2.2.5":
                span = secrets.token_hex(8)
                parent = secrets.token_hex(8)
                out["b3"] = f"{self.trace}-{span}-1-{parent}"
                out["traceparent"] = f"00-{self.trace}-{span}-01"
                out["x-session-affinity"] = session
                out["x-session-id"] = session
        if include_transport:
            try:
                host = urlparse(endpoint or "").netloc
            except ValueError:
                host = ""
            out["Connection"] = "keep-alive"
            out["Accept"] = "*/*"
            if host:
                out["Host"] = host
            out["Accept-Encoding"] = "gzip, deflate, br, zstd"
        return out

    def snapshot(self) -> dict[str, str]:
        return {
            "label": self.label,
            "project": self.project,
            "trace": self.trace,
            "env_cwd": self.env_cwd,
            "version": self.version,
            "client": self.client,
        }


@dataclass(frozen=True, slots=True)
class IdentityAuditResult:
    status: str
    duplicates: list[str] = field(default_factory=list)
    leaks: list[dict[str, str]] = field(default_factory=list)
    checked: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "duplicates": list(self.duplicates),
            "leaks": list(self.leaks),
            "checked": self.checked,
        }


def audit_identity(identity: StationIdentity, prompt: str, forbidden: Iterable[str]) -> list[str]:
    reasons: list[str] = []
    if not _HEX40_RE.match(identity.project):
        reasons.append("invalid_project")
    if identity.project == "global":
        reasons.append("project_global")
    if not _HEX32_RE.match(identity.trace):
        reasons.append("invalid_trace")
    if not identity.env_cwd.startswith(ENV_PREFIX):
        reasons.append("invalid_env_cwd")
    for marker in audit_prompt(prompt, forbidden):
        reasons.append(f"prompt_leak:{marker}")
    return reasons


def audit_station_set(
    identities: Sequence[StationIdentity],
    base_prompt: str,
    forbidden: Iterable[str],
    *,
    now: datetime | None = None,
) -> IdentityAuditResult:
    duplicates: list[str] = []
    leaks: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    for identity in identities:
        for field_name, value in (
            ("project", identity.project),
            ("trace", identity.trace),
            ("env_cwd", identity.env_cwd),
        ):
            key = f"{field_name}:{value}"
            if key in seen and seen[key] != identity.label:
                duplicates.append(key)
            else:
                seen[key] = identity.label
        prompt = identity.prompt(base_prompt, now=now)
        for marker in audit_prompt(prompt, forbidden):
            leaks.append({"label": identity.label, "marker": marker})
    if duplicates and leaks:
        status = "duplicate_identity+prompt_leak"
    elif duplicates:
        status = "duplicate_identity"
    elif leaks:
        status = "prompt_leak"
    else:
        status = "ok"
    return IdentityAuditResult(
        status=status,
        duplicates=sorted(set(duplicates)),
        leaks=leaks,
        checked=len(identities),
    )


@dataclass
class ConversationAffinity:
    ttl_s: float = 3600.0
    _map: dict[str, tuple[str, float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, key: str, *, now: float | None = None) -> str | None:
        ts = float(now if now is not None else time.time())
        with self._lock:
            item = self._map.get(str(key))
            if item is None:
                return None
            label, expires_at = item
            if ts >= expires_at:
                self._map.pop(str(key), None)
                return None
            return label

    def set(self, key: str, label: str, *, now: float | None = None) -> None:
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._map[str(key)] = (str(label), ts + float(self.ttl_s))

    def forget(self, key: str) -> None:
        with self._lock:
            self._map.pop(str(key), None)

    def prune(self, *, now: float | None = None) -> int:
        ts = float(now if now is not None else time.time())
        with self._lock:
            expired = [key for key, (_label, expires_at) in self._map.items() if ts >= expires_at]
            for key in expired:
                self._map.pop(key, None)
            return len(expired)

    def failover(self, key: str, new_label: str, *, now: float | None = None) -> str:
        previous = self.get(key, now=now)
        self.set(key, new_label, now=now)
        logger.info("conversation_failover key=%s from=%s to=%s", key, previous, new_label)
        return new_label

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return {key: label for key, (label, _expires) in self._map.items()}


class StationIdentityVault:
    def __init__(
        self,
        secret: bytes | None = None,
        *,
        secret_path: str | Path | None = None,
    ) -> None:
        self.secret_path = Path(secret_path) if secret_path is not None else DEFAULT_SECRET_PATH
        self.secret = secret if secret is not None else load_or_create_secret(self.secret_path)
        if len(self.secret) < SECRET_LEN:
            raise IdentitySecretError("secret trop court")
        self._identities: dict[str, StationIdentity] = {}
        self._lock = threading.Lock()

    def identity_for(self, label: str, *, version: str = "2.2.5", client: str = "cli") -> StationIdentity:
        key = str(label or "").strip() or "station"
        with self._lock:
            existing = self._identities.get(key)
            if existing is not None:
                return existing
            identity = StationIdentity(key, self.secret, version=version, client=client)
            self._identities[key] = identity
            return identity

    def all(self) -> list[StationIdentity]:
        with self._lock:
            return list(self._identities.values())

    def reset(self) -> None:
        with self._lock:
            self._identities.clear()


__all__ = [
    "CONV_LRU_MAX",
    "ConversationAffinity",
    "DEFAULT_SECRET_PATH",
    "ENV_PREFIX",
    "IdentityAuditResult",
    "IdentitySecretError",
    "PROJECT_LEN",
    "SECRET_LEN",
    "StationIdentity",
    "StationIdentityVault",
    "TRACE_LEN",
    "audit_identity",
    "audit_prompt",
    "audit_station_set",
    "load_or_create_secret",
    "render_system_prompt",
    "safe_label",
    "sanitize_text",
    "template_official_body",
]
