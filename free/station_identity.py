"""Identité par station (P0) : project, trace, session, prompt, affinité.

Le provider ne doit pas pouvoir corréler deux stations comme la même
installation. Ce module dérive les identifiants d'un secret local et du label
de station — jamais de l'IP seule, du workspace réel ou du hash du dépôt.
Aucune I/O réseau ; le vault et l'affinité sont testables hors ligne.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import subprocess
import sys
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
ENV_PREFIX = "/workspace/oc-"
ALIAS_ROOT = "/workspace/"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
DEFAULT_SECRET_PATH = Path(__file__).resolve().parent.parent / "logs" / "identity_secret"

_ENV_RE = re.compile(r"<env>.*?</env>", re.DOTALL)
_DATE_RE = re.compile(r"Today's date:\s*[^\n]*")
_INSTRUCTIONS_RE = re.compile(
    r"Instructions from:\s*[^\n]*\n.*?(?=\nToday's date:|\n<env>|\Z)",
    re.DOTALL,
)
_WIN_USER_PATH_RE = re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\s`\"'<>|]+(?:[\\/][^\s`\"'<>|]+)*")
_ENV_CWD_RE = re.compile(r"<env>\s*Working directory:\s*([^\n<]+)")
#: Indices de cwd dans les messages : Claude Code 2.x ne met plus toujours
#: ``<env>`` dans le prompt système (mémoire/summary en rôle user), et sans
#: racine réelle le remap des tool_calls ne s'applique pas — le modèle continue
#: d'émettre ``/workspace/...``, inexistant chez un client Windows.
_CWD_HINT_RE = re.compile(
    r"(?:working\s+dir(?:ectory)?|actual\s+cwd|current\s+(?:working\s+)?dir(?:ectory)?|\bcwd\b)",
    re.IGNORECASE,
)
_QUOTED_PATH_RE = re.compile(
    r"[`\"']((?:[A-Za-z]:[\\/]|/[A-Za-z]/)[^`\"'\r\n]+)[`\"']"
)
_BARE_PATH_RE = re.compile(
    r"((?:[A-Za-z]:[\\/]|/[A-Za-z]/)[^`\"'\r\n<>|]+)"
)
_TRAILING_PROSE_RE = re.compile(
    r"\s+(?:and|then|but|which|where|while|because|so)\b",
    re.IGNORECASE,
)
_MEMORY_HOME_RE = re.compile(
    r"([A-Za-z]:[\\/]Users[\\/][^\\/\s`\"'<>|]+)[\\/]\.claude[\\/]projects[\\/]",
    re.IGNORECASE,
)
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


def _looks_windows(text: str) -> bool:
    low = str(text or "").lower()
    return (
        "c:\\users\\" in low
        or "git bash" in low
        or "windows powershell" in low
        or "<shell>powershell" in low
        or "platform: win32" in low
    )


def _windows_from_posix(path: str) -> str:
    """``/c/Users/ASUS/...`` (Git Bash) → ``C:\\Users\\ASUS\\...``."""
    match = re.match(r"^/([A-Za-z])/(.*)$", str(path or ""))
    if not match:
        return str(path or "")
    return f"{match.group(1).upper()}:\\" + match.group(2).replace("/", "\\")


def _trim_path_tail(candidate: str) -> str:
    """Retire la prose qui suit un chemin non quoté (« … gli and bash … »).

    Un dossier Windows peut contenir des espaces (``Savegare gli``) : on ne
    peut donc pas s'arrêter au premier espace. On coupe aux conjonctions et à
    la ponctuation de fin, puis aux points/espaces terminaux.
    """
    value = str(candidate or "").strip()
    value = _TRAILING_PROSE_RE.split(value, maxsplit=1)[0]
    value = re.split(r"[,;]", value, maxsplit=1)[0]
    return value.strip().rstrip(" .")


def _valid_path_candidate(candidate: str, windows: bool) -> str:
    """Filtre les faux chemins (fragments JSON type ``s:\\n``, ``/workspace``).

    On exige au moins un sous-dossier : une racine de workspace réelle en a
    toujours un (``C:\\Users\\ASUS\\Desktop\\...``), alors qu'un fragment
    d'échappement n'en a pas. Mieux vaut ne rien remapper que d'envoyer les
    outils vers ``s:\n-``.
    """
    value = _trim_path_tail(str(candidate or "").strip().strip("`\"'"))
    if not value or value.startswith("/workspace"):
        return ""
    if re.match(r"^[A-Za-z]:[\\/]", value):
        if not re.match(r"^[A-Za-z]:[\\/][^\\/\r\n]+[\\/]", value):
            return ""
        return value.replace("/", "\\")
    if re.match(r"^/[A-Za-z]/", value):
        if not windows:
            return ""
        if not re.match(r"^/[A-Za-z]/[^/\r\n]+/", value):
            return ""
        return _windows_from_posix(value)
    return ""


def workspace_root_from_messages(messages) -> str:
    """Extrait le vrai ``Working directory`` du prompt client.

    Le corps envoyé au provider est templaté (``/workspace/oc-<station>``) pour
    cacher le nom d'utilisateur. Pour que les tool_calls du modèle restent
    exécutables, le proxy doit connaître le chemin réel et le remapper au
    retour.

    Trois sources, dans cet ordre :
      1. bloc ``<env>Working directory:`` (n'importe quel rôle — Claude Code 2.x
         peut le placer dans un rappel utilisateur) ;
      2. indice explicite « working dir / cwd is … » dans les messages,
         notamment les summaries de compaction (``/c/Users/...`` converti en
         ``C:\\Users\\...`` pour un client Windows) ;
      3. dernier recours : dossier home déduit du chemin mémoire
         ``C:\\Users\\<user>\\.claude\\projects\\...``.
    """
    texts: list[str] = []
    system_texts: list[str] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        if not isinstance(content, str):
            continue
        texts.append(content)
        if message.get("role") == "system":
            system_texts.append(content)
    for content in (*system_texts, *texts):
        match = _ENV_CWD_RE.search(content)
        if match:
            value = match.group(1).strip()
            if value and not value.startswith(ENV_PREFIX):
                return value
    blob = "\n".join(texts)
    if not blob:
        return ""
    windows = _looks_windows(blob)
    for hint in _CWD_HINT_RE.finditer(blob):
        window = blob[hint.start() : hint.start() + 400]
        for match in _QUOTED_PATH_RE.finditer(window):
            candidate = _valid_path_candidate(match.group(1), windows)
            if candidate:
                return candidate
        for match in _BARE_PATH_RE.finditer(window):
            candidate = _valid_path_candidate(match.group(1), windows)
            if candidate:
                return candidate
    if windows:
        home = _MEMORY_HOME_RE.search(blob)
        if home:
            return home.group(1).replace("/", "\\")
    return ""


def translate_paths(
    text: str,
    mapping: dict[str, str],
    *,
    json_escape: bool = False,
) -> str:
    """Remplace les racines factices par les racines réelles.

    ``json_escape=True`` échappe les backslashes Windows pour que le résultat
    reste un fragment JSON valide dans ``tool_calls[].arguments``.
    """
    out = str(text or "")
    for fake, real in (mapping or {}).items():
        fake_s = str(fake or "")
        real_s = str(real or "")
        if not fake_s or not real_s:
            continue
        target = (
            json.dumps(real_s, ensure_ascii=False)[1:-1]
            if json_escape
            else real_s
        )
        out = out.replace(fake_s, target)
        # Racines factices HISTORIQUES : un tour précédent a pu utiliser un
        # autre suffixe de hash pour la même station (ou aucun suffixe). Toute
        # racine ``/workspace/oc-<station>`` est inventée par le proxy, on la
        # ramène donc aussi vers la racine réelle.
        if fake_s.startswith("/workspace"):
            out = re.sub(
                r"/workspace/oc-[A-Za-z0-9._-]+",
                lambda _match: target,
                out,
            )
        # La racine factice est en style Unix ; le suffixe doit prendre les
        # séparateurs Windows de la racine réelle (C:\...\file, pas C:\.../file).
        if re.match(r"^[A-Za-z]:[\\/]", real_s):
            sep = "\\\\" if json_escape else "\\"
            while target + "/" in out:
                out = out.replace(target + "/", target + sep)
    return out


def ensure_workspace_alias(
    fake_root: str,
    real_root: str,
    *,
    root: str | Path | None = None,
) -> bool:
    """Crée ``C:\\workspace\\oc-<label>`` comme jonction vers le vrai workspace.

    Le modèle reçoit ``/workspace/oc-<station>`` ; sur Windows, le client
    résout ce chemin en ``C:\\workspace\\oc-<station>``. Sans cette jonction,
    chaque ``read``/``edit`` échoue avec « C:\\workspace does not exist ».
    Le préfixe ``oc-`` évite les collisions avec un vrai dossier
    ``C:\\workspace\\<label>`` déjà présent sur la machine.
    """
    if os.name != "nt":
        return False
    fake = str(fake_root or "").strip().replace("\\", "/")
    if not fake.startswith(ALIAS_ROOT):
        return False
    segment = safe_label(fake[len(ALIAS_ROOT):].strip("/"))
    try:
        real = Path(str(real_root or "")).expanduser()
    except Exception:
        return False
    if not segment or not real.is_dir():
        return False
    base = Path(root) if root is not None else Path("C:/workspace")
    link = base / segment
    try:
        if link.exists():
            try:
                same = os.path.normcase(os.path.realpath(str(link))) == os.path.normcase(
                    os.path.realpath(str(real))
                )
            except Exception:
                same = False
            if same:
                return True
            # Le label a déjà servi un autre workspace : la jonction doit
            # suivre la requête courante, sinon le modèle lit les fichiers du
            # mauvais projet. On ne supprime qu'une jonction/symlink, jamais un
            # vrai dossier utilisateur.
            try:
                attrs = getattr(os.lstat(link), "st_file_attributes", 0)
            except Exception:
                attrs = 0
            if not (attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                return False
            try:
                os.rmdir(link)
            except OSError:
                return False
        base.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(real)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode == 0 and link.exists()
    except Exception:
        return False


def workspace_env_cwd(identity: StationIdentity, real_root: str) -> str:
    """Racine factice stable par (station, workspace réel).

    Le label seul ne suffit pas : deux projets ouverts sur la même station
    partageraient la même jonction ``C:\\workspace\\oc-...`` et le second
    écraserait la cible du premier. Le suffixe hash garde une cible distincte
    par workspace sans révéler le chemin réel.
    """
    base = str(getattr(identity, "env_cwd", "") or "")
    real = str(real_root or "").strip()
    if not base or not real:
        return base
    digest = hashlib.sha256(real.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


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
    real_root = workspace_root_from_messages(
        body.get("messages") if isinstance(body, dict) else None
    )
    env_cwd = workspace_env_cwd(identity, real_root)
    out = _sanitize_value(body, env_cwd)
    messages = out.get("messages") if isinstance(out, dict) else None
    if not isinstance(messages, list):
        return out
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "system":
            content = str(message.get("content") or "")
            if "<env>" in content or "Today's date:" in content or "Instructions from:" in content:
                message["content"] = identity.prompt(content, now=now, env_cwd=env_cwd)
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

    def prompt(
        self,
        base: str,
        *,
        now: datetime | None = None,
        env_cwd: str | None = None,
    ) -> str:
        return render_system_prompt(base, env_cwd or self.env_cwd, now=now)

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
    "ensure_workspace_alias",
    "load_or_create_secret",
    "render_system_prompt",
    "safe_label",
    "sanitize_text",
    "template_official_body",
    "translate_paths",
    "workspace_env_cwd",
    "workspace_root_from_messages",
]
