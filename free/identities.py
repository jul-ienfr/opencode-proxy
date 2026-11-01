"""free.identities — vault d'identités OpenCode par IP (1 IP = 1 installation).

Chaque IP d'egress (station VPN ou direct) obtient sa propre identité complète
de client officiel, comme une installation OpenCode distincte sur une machine
distincte :

* version client (UA) : tirée du pool des versions MESURÉES à 200 sur la
  gateway (1.18.31 locale + 1.19.3 capture `_research/oc-1.19.3/capture.jsonl`).
  Le suffixe ai-sdk/Bun suit la version (4.0.23 chat / 4.0.40 responses,
  runtime/bun/1.3.14 dans les deux cas mesurés). La face TLS reste le replay
  Bun unique (même Bun 1.3.14 des deux côtés — varier le JA3 casserait la
  parité au lieu de la renforcer).
* identifiants : `x-opencode-client` (cli/desktop), `x-opencode-project`
  (dérivé par IP), `ses_` global + par conversation, `msg_` par envoi.

Lien machine unique cassé : deux IPs ne partagent JAMAIS un `ses_`, un
`msg_`, un `project` ou un UA. Le compteur temporel `id.ts` est isolé par
identité (pas de compteur global séquentiel inter-IP).

Déterministe sans fichier : version/client/project dérivés de
`sha256(ip)` — stables across restarts. Seul le `ses_` tournant (30 min,
comme `_free_session_id`) est persisté (`logs/identities/<ip>.json`).

Zéro import projet (pas de cycle opencode ↔ free).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Dossier du vault surchargeable en tests (OPENCODE_IDENTITIES_DIR) — prod :
# logs/identities (gitignoré via logs/_* comme les autres états runtime).


def _identities_dir() -> str:
    try:
        _ov = (os.getenv("OPENCODE_IDENTITIES_DIR", "") or "").strip()
        if _ov:
            return _ov
    except Exception:
        pass
    return os.path.join(ROOT, "logs", "identities")


# Versions client MESURÉES sur la gateway (captures mitm 2026-11-01).
# Chaque version porte sa face réseau exacte : UA + suffixe Bun + présence
# de `x-opencode-session-id`. Le corps (11 tools + système) est identique
# sur 1.18.31 / 1.19.0 / 1.19.3 / 1.19.5.
_JA3_BUN_113 = (
    "771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49161-49171"
    "-49162-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-21,"
    "29-23-24,0"
)
_JA3_BUN_114 = (
    "771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49161-49171"
    "-49162-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43,"
    "4588-29-23-24,0"
)
_SIGALGS_BUN_113 = (
    "ecdsa_secp256r1_sha256",
    "rsa_pss_rsae_sha256",
    "rsa_pkcs1_sha256",
    "ecdsa_secp384r1_sha384",
    "rsa_pss_rsae_sha384",
    "rsa_pkcs1_sha384",
    "rsa_pss_rsae_sha512",
    "rsa_pkcs1_sha512",
    "rsa_pkcs1_sha1",
)
_SIGALGS_BUN_114 = (
    "ecdsa_secp256r1_sha256",
    "rsa_pss_rsae_sha256",
    "rsa_pkcs1_sha256",
    "ecdsa_secp384r1_sha384",
    "rsa_pss_rsae_sha384",
    "rsa_pkcs1_sha384",
    "rsa_pss_rsae_sha512",
    "rsa_pkcs1_sha512",
    "ed25519",
    "ecdsa_secp521r1_sha512",
    "rsa_pkcs1_sha1",
)
VERSION_SPECS: dict[str, dict] = {
    "1.18.31": {
        "bun": "runtime/bun/1.3.14",
        "session_id": False,
        "verified": True,
        "ja3": _JA3_BUN_113,
        "ja3_md5": "d871d02cecbde59abbf8f4806134addf",
        "sigalgs": _SIGALGS_BUN_113,
        "tls_grease": False,
    },
    "1.18.35": {
        "bun": "runtime/bun/1.3.14",
        "session_id": False,
        "verified": False,
        "ja3": _JA3_BUN_113,
        "ja3_md5": "d871d02cecbde59abbf8f4806134addf",
        "sigalgs": _SIGALGS_BUN_113,
        "tls_grease": False,
    },
    "1.19.0": {
        "bun": "runtime/bun/1.3.14",
        "session_id": True,
        "verified": True,
        "ja3": _JA3_BUN_113,
        "ja3_md5": "d871d02cecbde59abbf8f4806134addf",
        "sigalgs": _SIGALGS_BUN_113,
        "tls_grease": False,
    },
    "1.19.3": {
        "bun": "runtime/bun/1.3.14",
        "session_id": True,
        "verified": True,
        "ja3": _JA3_BUN_113,
        "ja3_md5": "d871d02cecbde59abbf8f4806134addf",
        "sigalgs": _SIGALGS_BUN_113,
        "tls_grease": False,
    },
    "1.19.5": {
        "bun": "runtime/bun/1.4.4",
        "session_id": True,
        "verified": True,
        "ja3": _JA3_BUN_114,
        "ja3_md5": "1523504b38f0fae0d881d4b6554aac1b",
        "sigalgs": _SIGALGS_BUN_114,
        "tls_grease": False,
    },
}
KNOWN_CLIENT_VERSIONS = tuple(VERSION_SPECS)
_LEGACY_CLIENT_VERSIONS = ("1.18.31", "1.19.3")
_BUN_SUFFIX = "runtime/bun/1.3.14"
_BUN_BY_VERSION = {v: spec["bun"] for v, spec in VERSION_SPECS.items()}
#: Ordre de rotation des installations (versions vérifiées, la plus récente
#: d'abord). Le client reste `desktop` (seule valeur observée live sur
#: `opencode run`) ; `cli`/`app` restent générables explicitement.
ROTATION_VERSIONS = ("1.19.5", "1.19.3", "1.18.31", "1.19.0")
SUPPORTED_CLIENTS = ("desktop", "cli", "app")
#: Seul `desktop` a été observé sur le wire réel (`opencode run` 1.18.31,
#: 1.19.0, 1.19.3, 1.19.5). `cli`/`app` sont dans l'enum du binaire mais
#: n'ont pas de capture live : profils explicites uniquement.
VERIFIED_CLIENTS = ("desktop",)
# ai-sdk bundle diffère par endpoint (mesuré) : 4.0.23 chat / 4.0.40 responses.
_AI_CHAT = "ai-sdk/provider-utils/4.0.23"
_AI_RESP = "ai-sdk/provider-utils/4.0.40"

# Enum RÉELLE du binaire (`request.ts` : ["app","cli","desktop"] — 1.18.31).
# "app" n'a jamais été mesuré à 200 sur inference/v1 → pool restreint aux
# deux valeurs vérifiées live.
KNOWN_CLIENT_NAMES = ("desktop", "cli", "app")

_OC_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SES_ROTATE_S = 1800.0
_CONV_LRU_MAX = 128


def _stable_int(*parts: str) -> int:
    h = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(h[:8], "big")


def version_for(ip: str) -> str:
    """Version OpenCode stable pour cette IP (pool historique mesuré)."""
    if not ip:
        return _LEGACY_CLIENT_VERSIONS[0]
    return _LEGACY_CLIENT_VERSIONS[_stable_int("ver", ip) % len(_LEGACY_CLIENT_VERSIONS)]


def _version_for_gen(ip: str, generation: int) -> str:
    """Variante salée par génération (rotation sans IP) — pool mesuré."""
    try:
        g = int(generation or 0)
    except Exception:
        g = 0
    if not ip:
        return _LEGACY_CLIENT_VERSIONS[g % len(_LEGACY_CLIENT_VERSIONS)]
    return _LEGACY_CLIENT_VERSIONS[(_stable_int("ver", ip) + g) % len(_LEGACY_CLIENT_VERSIONS)]


def client_name_for(ip: str, forced: str = "") -> str:
    """Nom de client pour cette IP.

    `desktop` est la SEULE valeur observée live sur `opencode run`
    (1.18.31/1.19.0/1.19.3/1.19.5). `cli`/`app` restent disponibles via un
    profil explicite (générateur) mais ne sont pas le défaut du vault.
    """
    if forced in KNOWN_CLIENT_NAMES:
        return forced
    return "desktop"


def _client_for_gen(ip: str, generation: int, forced: str = "") -> str:
    """Variante salée par génération (rotation sans IP) — desktop par défaut."""
    if forced in KNOWN_CLIENT_NAMES:
        return forced
    return "desktop"


def project_for(ip: str, base: str, generation: int = 0) -> str:
    """Project ID par IP (et par génération) : deux installations distinctes
    ont des checkouts distincts. ``generation > 0`` donne un **nouveau**
    project à la même IP — sinon une rotation d'identité réutilisait le
    project et le provider pouvait relier les deux installations.
    """
    base = (base or "").strip()
    try:
        gen = int(generation or 0)
    except Exception:
        gen = 0
    if not base or base == "global":
        if gen <= 0:
            return "global"
        return hashlib.sha256(f"global|{ip}|gen{gen}".encode()).hexdigest()[:40]
    if not ip:
        return base
    # base = hash git 40 hex (ou contenu .git/opencode) → dérivé 40 hex.
    seed = f"{base}|{ip}|gen{gen}" if gen > 0 else f"{base}|{ip}"
    digest = hashlib.sha256(seed.encode()).hexdigest()
    if len(base) == 40 and all(c in "0123456789abcdefABCDEF" for c in base):
        return digest[:40]
    return digest[: len(base)] if len(base) <= 64 else digest


def ua_for(ip: str, endpoint: str = "", forced_version: str = "") -> str:
    """User-Agent officiel complet pour cette IP + endpoint."""
    ver = forced_version if forced_version in KNOWN_CLIENT_VERSIONS else version_for(ip)
    ai = _AI_RESP if "/responses" in (endpoint or "") else _AI_CHAT
    bun = _BUN_BY_VERSION.get(ver, _BUN_SUFFIX)
    return f"opencode/{ver} {ai} {bun}"


def _random_base62(n: int = 14) -> str:
    return "".join(_OC_ID_ALPHABET[b % 62] for b in os.urandom(n))


def _mint(prefix: str, direction: str, state: dict) -> str:
    """Port de `id.ts:create` avec compteur ISOLÉ par identité.

    `state` = {"last_ms": int, "counter": int} propre à l'identité + lock
    externe. Même format que le client (12 hex temporels + 14 base62).
    """
    now_ms = int(time.time() * 1000)
    with state["lock"]:
        if now_ms != state["last_ms"]:
            state["last_ms"] = now_ms
            state["counter"] = 0
        state["counter"] += 1
        n = now_ms * 0x1000 + state["counter"]
    if direction == "descending":
        n = -n - 1
    n48 = n & ((1 << 48) - 1)
    return f"{prefix}_{n48.to_bytes(6, 'big').hex()}" + _random_base62(14)


def _safe_filename(ip: str) -> str:
    # [audit-2026-10-24 Phase3] IPv6-safe : ':' -> '_' collisionnait 'a:b' et
    # 'a_b'. On garde lisible pour IPv4/hosts, hash court pour le reste.
    base = (ip or "direct").strip() or "direct"
    safe = "".join(c if (c.isalnum() or c in (".", "-", "_")) else "_" for c in base)[:48] or "direct"
    if ":" in base or len(safe) >= 48 or safe != base:
        digest = hashlib.sha256(base.encode()).hexdigest()[:12]
        return f"{safe[:32]}-{digest}"[:64]
    return safe[:64]


class IpIdentity:
    """Une installation OpenCode vue par la gateway (1 IP)."""

    __slots__ = (
        "ip",
        "version",
        "client",
        "project",
        "ses_global",
        "ses_ts",
        "conv",
        "generation",
        "_id_state",
        "_lock",
        "_file",
        "_persist_enabled",
    )

    def __init__(
        self,
        ip: str,
        base_project: str = "",
        forced_client: str = "",
        forced_version: str = "",
        generation: int = 0,
        persist: bool = True,
    ):
        self.ip = ip or "direct"
        try:
            self.generation = int(generation or 0)
        except Exception:
            self.generation = 0
        if forced_version in KNOWN_CLIENT_VERSIONS:
            self.version = forced_version
        elif self.generation:
            self.version = _version_for_gen(self.ip, self.generation)
        else:
            self.version = version_for(self.ip)
        self.client = _client_for_gen(self.ip, self.generation, forced_client)
        self.project = project_for(self.ip, base_project, self.generation)
        self.ses_global: str = ""
        self.ses_ts: float = 0.0
        self.conv: dict[str, str] = {}
        self._id_state = {"last_ms": 0, "counter": 0, "lock": threading.Lock()}
        self._lock = threading.Lock()
        self._persist_enabled = bool(persist)
        self._file = os.path.join(_identities_dir(), _safe_filename(self.ip) + ".json")
        if not self._persist_enabled:
            # Profil généré (CLI/tests) : aucune lecture/écriture disque, IDs
            # toujours frais.
            self.ses_global = _mint("ses", "descending", self._id_state)
            self.ses_ts = time.time()
        elif self.generation:
            # Rotation sans IP : nouvelle installation — on n'hérite ni de la
            # version figée ni du ses_ (c'est justement ce qu'on change).
            self.ses_global = _mint("ses", "descending", self._id_state)
            self.ses_ts = time.time()
            self._persist()
        else:
            self._load_or_mint()

    def _load_or_mint(self) -> None:
        now = time.time()
        try:
            with open(self._file, encoding="utf-8") as f:
                data = json.load(f)
            ses = str(data.get("ses") or "")
            ts = float(data.get("ts") or 0)
            # Le fichier fige version/client/project (stabilité) ; le ses
            # n'est réutilisé que s'il est frais (<30 min, comme le global).
            # [audit-2026-10-24 Phase3] migration douce : si le vault figé
            # porte une version/client obsolète (KNOWN_* a évolué) et aucun
            # forçage, on re-dérive depuis l'IP et on re-persiste — sinon une
            # maj de version ne s'applique jamais aux IP connues.
            stored_ver = str(data.get("version") or "")
            stored_cli = str(data.get("client") or "")
            if stored_ver in KNOWN_CLIENT_VERSIONS:
                self.version = stored_ver
            if stored_cli in KNOWN_CLIENT_NAMES:
                self.client = stored_cli
            if isinstance(data.get("project"), str) and data["project"]:
                self.project = str(data["project"])
            # Migration : dérivation canonique différente du figé → adopter.
            try:
                canon_ver = version_for(self.ip)
                canon_cli = client_name_for(self.ip)
                if not self._forced_version() and self.version != canon_ver and canon_ver in KNOWN_CLIENT_VERSIONS:
                    # Ne migrer que si le figé n'est plus canonique à cause
                    # d'un changement de pool (évite churn à chaque boot).
                    pass  # stabilité primauté : on garde le figé sauf forçage
                if not self._forced_client() and self.client != canon_cli and canon_cli in KNOWN_CLIENT_NAMES:
                    pass
            except Exception:
                pass
            if ses.startswith("ses_") and len(ses) == 30 and (now - ts) < _SES_ROTATE_S:
                self.ses_global, self.ses_ts = ses, ts
                return
        except Exception:
            pass
        self.ses_global = _mint("ses", "descending", self._id_state)
        self.ses_ts = now
        self._persist()

    def _forced_version(self) -> str:
        return ""

    def _forced_client(self) -> str:
        return ""

    def _persist(self) -> None:
        if not getattr(self, "_persist_enabled", True):
            return
        try:
            os.makedirs(os.path.dirname(self._file), exist_ok=True)
            tmp = self._file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "ip": self.ip,
                        "version": self.version,
                        "client": self.client,
                        "project": self.project,
                        "ses": self.ses_global,
                        "ts": self.ses_ts,
                    },
                    f,
                )
            os.replace(tmp, self._file)
        except Exception:
            pass

    def _rotate_if_stale(self) -> None:
        # [audit-2026-10-24] ne jamais faire d'I/O sous _lock en asyncio :
        # _persist (open/write/replace) bloquait la loop tant que le lock
        # était tenu + risque d'interblocage si un autre chemin prend les
        # locks dans l'ordre inverse. Mint sous lock, persist dehors.
        if (time.time() - self.ses_ts) >= _SES_ROTATE_S:
            _need_persist = False
            with self._lock:
                if (time.time() - self.ses_ts) >= _SES_ROTATE_S:
                    self.ses_global = _mint("ses", "descending", self._id_state)
                    self.ses_ts = time.time()
                    _need_persist = True
            if _need_persist:
                try:
                    self._persist()
                except Exception:
                    pass

    def session_for(self, conv_key: str | None) -> str:
        """`ses_` stable PAR CONVERSATION pour CETTE IP (parité request.ts :
        sticky routing + prefix-cache par conversation, scopé à l'instance)."""
        self._rotate_if_stale()
        if not conv_key or not isinstance(conv_key, str):
            return self.ses_global
        with self._lock:
            sid = self.conv.get(conv_key)
            if sid:
                # LRU : remonter en fin.
                del self.conv[conv_key]
                self.conv[conv_key] = sid
                return sid
            sid = _mint("ses", "descending", self._id_state)
            self.conv[conv_key] = sid
            while len(self.conv) > _CONV_LRU_MAX:
                self.conv.pop(next(iter(self.conv)))
            return sid

    def new_msg(self) -> str:
        """`msg_` frais par ENVOI egress (1 tentative = 1 msg).

        Diffère volontairement du client (qui réutilise le msg sur retry) :
        deux IPs qui émettent le même `msg_` sont trivialement liées ; deux
        `msg_` distincts de format valide passent le gate (le gate ne vérifie
        que le format, jamais la stabilité).
        """
        return _mint("msg", "ascending", self._id_state)

    def ua(self, endpoint: str = "") -> str:
        ai = _AI_RESP if "/responses" in (endpoint or "") else _AI_CHAT
        bun = _BUN_BY_VERSION.get(self.version, _BUN_SUFFIX)
        return f"opencode/{self.version} {ai} {bun}"

    def official_provider_headers(self, endpoint: str = "", conversation_key: str | None = None) -> dict:
        """Headers EXACTS du provider `opencode` pour cette installation.

        Capture mitm 2026-11-01 (1.18.31 / 1.19.0 / 1.19.3 / 1.19.5) :
        Authorization, Content-Type, UA, x-opencode-client, x-opencode-project,
        x-opencode-request, x-opencode-session, [x-opencode-session-id].
        `x-opencode-session-id` n'existe qu'à partir de 1.19.0.
        Aucun x-session-affinity / x-session-id / directory / workspace.
        """
        ses = self.session_for(conversation_key)
        msg = self.new_msg()
        out = {
            "Authorization": "Bearer public",
            "Content-Type": "application/json",
            "User-Agent": self.ua(endpoint),
            "x-opencode-client": self.client or "desktop",
            "x-opencode-project": self.project or "global",
            "x-opencode-request": msg,
            "x-opencode-session": ses,
        }
        if VERSION_SPECS.get(self.version, {}).get("session_id", True):
            out["x-opencode-session-id"] = ses
        return out

    def session_header_items(self, ses: str) -> list[tuple[str, str]]:
        """Headers d'identité de session pour CETTE version client (ordre wire).

        * 1.18.31 → quartet provider `opencode` sans `x-opencode-session-id`
          (capture live 2026-11-01) ;
        * 1.19.3 → trio historique `x-opencode-session-id` + duo (chemin
          custom-provider, conservé pour compatibilité) ;
        * 1.19.5 → quartet provider `opencode` + `x-opencode-session-id`
          (capture live 2026-11-01).
        """
        if self.version == "1.18.31":
            return [("x-opencode-session", ses)]
        if self.version == "1.19.5":
            return [("x-opencode-session", ses), ("x-opencode-session-id", ses)]
        return [
            ("x-opencode-session-id", ses),
            ("x-session-affinity", ses),
            ("x-session-id", ses),
        ]

    def snapshot(self) -> dict:
        return {
            "ip": self.ip,
            "version": self.version,
            "client": self.client,
            "project": self.project,
            "ses": self.ses_global,
            "generation": self.generation,
        }


class IdentityVault:
    """Registre des identités par IP (1 IP = 1 installation)."""

    #: [2026-10-25] Plafond mémoire/disque. Le vault créait une identité (et
    #: un fichier) par IP vue, à vie : 548 fichiers après quelques jours de
    #: rotation. `prune()` existait mais n'était appelé nulle part. On borne
    #: à l'insertion : les plus anciennes sont oubliées (fichier supprimé) ;
    #: une IP revue plus tard repart sur une identité neuve, ce qui est sans
    #: effet fonctionnel (le but est la distinction, pas la stabilité).
    MAX_IDS = 512

    def __init__(self, base_project: str = "", forced_client: str = "", forced_version: str = ""):
        self._lock = threading.Lock()
        self._ids: dict[str, IpIdentity] = {}
        self._gen: dict[str, int] = {}
        self._max_ids = self.MAX_IDS
        self._base_project = base_project or ""
        self._forced_client = forced_client if forced_client in KNOWN_CLIENT_NAMES else ""
        self._forced_version = forced_version if forced_version in KNOWN_CLIENT_VERSIONS else ""

    def configure(self, base_project: str = "", forced_client: str = "", forced_version: str = "") -> None:
        with self._lock:
            if base_project:
                self._base_project = base_project
            # [audit-2026-10-24 Phase3] forçage invalide = warning explicite
            # (avant : ignoré silencieusement, incohérence wire).
            if forced_client and forced_client not in KNOWN_CLIENT_NAMES:
                try:
                    import logging as _lg

                    _lg.getLogger("free.identities").warning(
                        "[identities] forced_client %r ignoré (attendu %s)", forced_client, KNOWN_CLIENT_NAMES
                    )
                except Exception:
                    pass
                forced_client = ""
            if forced_version and forced_version not in KNOWN_CLIENT_VERSIONS:
                try:
                    import logging as _lg

                    _lg.getLogger("free.identities").warning(
                        "[identities] forced_version %r ignorée (attendu %s)", forced_version, KNOWN_CLIENT_VERSIONS
                    )
                except Exception:
                    pass
                forced_version = ""
            self._forced_client = forced_client if forced_client in KNOWN_CLIENT_NAMES else ""
            self._forced_version = forced_version if forced_version in KNOWN_CLIENT_VERSIONS else ""

    def get(self, ip: str) -> IpIdentity:
        key = (ip or "direct").strip() or "direct"
        ident = self._ids.get(key)
        if ident is not None:
            return ident
        with self._lock:
            ident = self._ids.get(key)
            if ident is None:
                ident = IpIdentity(key, self._base_project, self._forced_client, self._forced_version)
                # Applique un re-forçage tardif (env lu après boot) + persiste
                # (avant : muté sans _persist → revert au restart).
                _dirty = False
                if self._forced_client and ident.client != self._forced_client:
                    ident.client = self._forced_client
                    _dirty = True
                if self._forced_version and ident.version != self._forced_version:
                    ident.version = self._forced_version
                    _dirty = True
                if _dirty:
                    try:
                        ident._persist()
                    except Exception:
                        pass
                self._ids[key] = ident
                self._enforce_cap_locked(keep=key)
            return ident

    def _forget(self, key: str) -> None:
        """Oublie une IP : entrée mémoire + fichier d'identité (fail-soft)."""
        ident = self._ids.pop(key, None)
        self._gen.pop(key, None)
        path = getattr(ident, "_file", "") if ident is not None else ""
        if not path:
            path = os.path.join(_identities_dir(), _safe_filename(key) + ".json")
        try:
            os.remove(path)
        except OSError:
            pass
        except Exception:
            pass

    def _enforce_cap_locked(self, keep: str = "") -> None:
        """Évince les identités les plus anciennes au-delà du plafond.

        Appelé sous `self._lock` (dict ordonné par insertion). `keep` n'est
        jamais évincé : c'est l'IP de la requête en cours.
        """
        try:
            surplus = len(self._ids) - int(self._max_ids)
        except Exception:
            return
        if surplus <= 0:
            return
        for old in list(self._ids.keys()):
            if surplus <= 0:
                break
            if old == keep:
                continue
            self._forget(old)
            surplus -= 1

    def rotate_installation(self, ip: str) -> dict:
        """Nouvelle installation sur la MÊME IP, SANS toucher au tunnel.

        Quota trial épuisé → changer d'impersonnalisation (version/UA,
        client, ses_) coûte zéro AUTH NordVPN, contrairement à une rotation
        d'IP. Si le bucket amont inclut la face client, la prochaine
        tentative passe ; sinon c'est la rotation d'IP (bras armé en
        parallèle) qui prend le relais. Pur effet de bord vault (persisté),
        jamais de raise.
        """
        key = (ip or "direct").strip() or "direct"
        try:
            with self._lock:
                old = self._ids.get(key)
                try:
                    base_gen = int(getattr(old, "generation", 0) or 0)
                except Exception:
                    base_gen = 0
                try:
                    remembered = int(self._gen.get(key, 0) or 0)
                except Exception:
                    remembered = 0
                gen = max(base_gen, remembered) + 1
                self._gen[key] = gen
                # [2026-11-01] Nouvelle installation = profil exact alterné
                # parmi les versions vérifiées (1.19.5 → 1.19.3 → 1.18.31 →
                # 1.19.0), client `desktop` (seule valeur observée live).
                # Chaque génération change aussi le project/ses_/msg_.
                version = self._forced_version or ROTATION_VERSIONS[(gen - 1) % len(ROTATION_VERSIONS)]
                client = self._forced_client or "desktop"
                ident = IpIdentity(
                    key,
                    self._base_project,
                    forced_client=client,
                    forced_version=version,
                    generation=gen,
                )
                ident._persist()
                self._ids[key] = ident
            try:
                return ident.snapshot()
            except Exception:
                return {"ip": key, "generation": gen}
        except Exception:
            return {"ip": key}

    def session_for(self, ip: str, conv_key: str | None) -> str:
        return self.get(ip).session_for(conv_key)

    def new_msg(self, ip: str) -> str:
        return self.get(ip).new_msg()

    def ua_for(self, ip: str, endpoint: str = "") -> str:
        return self.get(ip).ua(endpoint)

    def snapshot(self, ip: str) -> dict:
        try:
            return self.get(ip).snapshot()
        except Exception:
            return {"ip": ip or "direct"}

    def cleanup_stale_files(self, max_age_days: float = 14.0) -> int:
        """Supprime les fichiers d'identité plus vieux que N jours.

        [2026-10-25] Les fichiers des IP jamais revues ne sont jamais chargés
        en mémoire, donc `_enforce_cap_locked` ne peut pas les évincer : sans
        ce balayage, `logs/identities/` grossit indéfiniment (548 fichiers
        constatés). Une IP revue après le délai repart sur une identité
        neuve — sans effet fonctionnel.
        """
        removed = 0
        cutoff = time.time() - max(0.0, float(max_age_days)) * 86400.0
        try:
            base_dir = _identities_dir()
            for name in os.listdir(base_dir):
                if not name.endswith(".json"):
                    continue
                path = os.path.join(base_dir, name)
                try:
                    if os.path.getmtime(path) >= cutoff:
                        continue
                    os.remove(path)
                    removed += 1
                except OSError:
                    continue
        except Exception:
            pass
        return removed

    def prune(self, active_ips: set[str], max_ids: int = 512) -> int:
        """Évince les identités hors fenêtre (IPs plus vues).

        Supprime aussi le fichier correspondant : sinon le dossier
        `logs/identities/` grossit indéfiniment (548 fichiers constatés le
        2026-10-25). Rend le nombre d'entrées oubliées.
        """
        with self._lock:
            stale = [k for k in self._ids if k not in active_ips]
            for k in stale:
                self._forget(k)
            try:
                cap = max(64, int(max_ids))
            except Exception:
                cap = 512
            while len(self._ids) > cap:
                self._forget(next(iter(self._ids)))
            return len(stale)


_VAULT: IdentityVault | None = None
_VAULT_LOCK = threading.Lock()


def get_vault(base_project: str = "", forced_client: str = "", forced_version: str = "") -> IdentityVault:
    global _VAULT
    if _VAULT is not None:
        if base_project or forced_client or forced_version:
            _VAULT.configure(base_project, forced_client, forced_version)
        return _VAULT
    with _VAULT_LOCK:
        if _VAULT is None:
            _VAULT = IdentityVault(base_project, forced_client, forced_version)
            # [2026-10-25] Balayage unique au boot : les fichiers des IP
            # jamais revues ne sont jamais chargés, donc jamais évincés par
            # le plafond mémoire. Coût : un listdir + stat sur ~550 fichiers.
            try:
                _VAULT.cleanup_stale_files()
            except Exception:
                pass
        elif base_project or forced_client or forced_version:
            _VAULT.configure(base_project, forced_client, forced_version)
        return _VAULT


__all__ = [
    "KNOWN_CLIENT_VERSIONS",
    "KNOWN_CLIENT_NAMES",
    "IpIdentity",
    "IdentityVault",
    "get_vault",
    "version_for",
    "client_name_for",
    "project_for",
    "ua_for",
]
