"""Garde-fous pour l'egress via un proxy non fiable.

Un proxy public ne doit jamais recevoir de secret applicatif. Ce module
centralise la sanitisation des en-têtes free, le fail-closed si un credential
console est activé, et la détection d'une valeur secrète injectée sous un nom
d'en-tête inattendu.

Le contenu (corps, prompts) reste protégé par le TLS de bout en bout vers
``opencode.ai`` : ce module ne touche pas au corps et ne désactive jamais la
vérification du certificat d'origine.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Mapping
from typing import Any

PUBLIC_AUTH = "Bearer public"

# En-têtes qui ne doivent jamais partir vers un proxy non fiable.
SECRET_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-opencode-org-id",
        "openai-organization",
        "anthropic-api-key",
        "x-goog-api-key",
        "x-auth-token",
        "x-access-token",
        "x-session-token",
        "x-csrf-token",
    }
)

# Marqueurs de nom : couvre aussi les en-têtes custom du type
# ``x-my-api-token`` qu'une liste fermée laisserait passer.
SECRET_NAME_MARKERS = (
    "authorization",
    "cookie",
    "api-key",
    "apikey",
    "token",
    "secret",
    "credential",
    "password",
    "passwd",
    "csrf",
    "auth",
)

# Variables dont la valeur ne doit jamais apparaître dans un en-tête sortant.
SECRET_ENV_KEYS = (
    "OPENCODE_API_KEY",
    "OPENCODE_CONSOLE_TOKEN",
    "OPENCODE_GO_AUTH_COOKIE",
    "OPENCODE_PROXY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "DASHBOARD_TOKEN",
    "VPN_CONTROL_API_KEY",
)


class UntrustedProxySecretError(RuntimeError):
    """Un secret applicatif risquait de partir vers un proxy non fiable."""


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _console_enabled() -> bool:
    return _truthy(os.getenv("OPENCODE_ALLOW_CONSOLE_CREDENTIAL")) and bool(
        (os.getenv("OPENCODE_CONSOLE_TOKEN") or "").strip()
    )


def is_untrusted_proxy(station: Any = None, proxy_url: str | None = None) -> bool:
    """True si l'egress n'est pas un tunnel managé (WARP/NordVPN).

    Un endpoint statique (HTTPS public, SOCKS5 manuel) est non fiable. Un
    endpoint managé par le projet (WARP, gluetun) reste un tunnel de confiance,
    mais seulement si l'URL utilisée est bien celle de la station : une URL
    différente est traitée comme non fiable.
    """
    if not proxy_url:
        return False
    if station is None:
        return True
    provider = str(getattr(station, "provider", "") or "").strip().lower()
    if provider in ("public", "static", "proxies"):
        return True
    if str(getattr(station, "scheme", "") or "").strip().lower() == "https":
        return True
    station_url = str(getattr(station, "socks5_url", "") or "")
    managed = getattr(station, "managed", None)
    container = getattr(station, "_docker_container", None) or getattr(station, "container", None)
    if managed is True:
        # Un endpoint « managé » n'est de confiance que s'il vient d'un
        # provider connu (WARP/NordVPN) ou d'un conteneur du projet.
        if provider in ("warp", "nordvpn") or container:
            return bool(station_url and station_url != proxy_url)
        return True
    if managed is False:
        return True
    # VPNManager (gluetun) n'expose pas ``managed`` : son conteneur est la
    # marque d'un tunnel géré par le projet.
    if container:
        return bool(station_url and station_url != proxy_url)
    return True


def _is_secret_header_name(name: str) -> bool:
    lowered = str(name or "").strip().lower()
    return lowered in SECRET_HEADERS or any(marker in lowered for marker in SECRET_NAME_MARKERS)


def _secret_values() -> list[str]:
    values: list[str] = []
    for key in SECRET_ENV_KEYS:
        value = (os.getenv(key) or "").strip()
        if len(value) >= 8 and value.lower() not in ("public", "fake-key"):
            values.append(value)
    return values


def _contains_secret(value: str, secrets: list[str]) -> bool:
    text = str(value or "")
    return any(secret in text for secret in secrets)


def assert_untrusted_proxy_safe(headers: Mapping[str, str] | None = None) -> None:
    """Fail-closed avant tout envoi vers un proxy non fiable.

    - refuse si le credential console est activé ;
    - refuse si une valeur de secret d'environnement apparaît dans les
      en-têtes, même sous un nom custom.
    """
    if _console_enabled():
        raise UntrustedProxySecretError("credential console activé : envoi vers un proxy public refusé")
    secrets = _secret_values()
    if not secrets or not headers:
        return
    for name, value in headers.items():
        if _contains_secret(value, secrets):
            raise UntrustedProxySecretError(
                f"secret détecté dans l'en-tête {name!r} : envoi vers un proxy public refusé"
            )


def sanitize_free_headers(headers: Mapping[str, str], *, untrusted_proxy: bool) -> dict[str, str]:
    """Retourne une copie sûre des en-têtes free.

    Sur un proxy non fiable : supprime tous les en-têtes porteurs de secrets,
    force ``Authorization: Bearer public`` et vérifie qu'aucune valeur secrète
    ne subsiste. Sur un tunnel managé, conserve le comportement existant.
    """
    out = dict(headers or {})
    if not untrusted_proxy:
        # Jambe free de confiance : anonyme par défaut. Seul le diagnostic
        # console explicitement activé peut conserver Authorization + org-id.
        console = _console_enabled()
        if not console:
            out["Authorization"] = PUBLIC_AUTH
        else:
            out.setdefault("Authorization", PUBLIC_AUTH)
        keep = {"authorization"} | ({"x-opencode-org-id"} if console else set())
        for name in list(out):
            if name.lower() not in keep and _is_secret_header_name(name):
                out.pop(name, None)
        return out
    assert_untrusted_proxy_safe(out)
    for name in list(out):
        if _is_secret_header_name(name):
            out.pop(name, None)
    out["Authorization"] = PUBLIC_AUTH
    assert_untrusted_proxy_safe(out)
    return out


def assert_secure_endpoint(endpoint: str, *, untrusted_proxy: bool) -> None:
    """Refuse un endpoint non HTTPS ou hors ``opencode.ai`` sur un proxy non fiable.

    Sans HTTPS, le proxy verrait le corps et les en-têtes en clair. Un host
    hors ``opencode.ai`` sur un proxy public serait une erreur de routage
    (potentiellement une clé payante). C'est un fail-closed de configuration.
    """
    if not untrusted_proxy:
        return
    try:
        parsed = urllib.parse.urlsplit(str(endpoint or "").strip())
    except ValueError as exc:
        raise UntrustedProxySecretError("endpoint illisible via un proxy non fiable") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https":
        raise UntrustedProxySecretError("endpoint non HTTPS interdit via un proxy non fiable")
    if host != "opencode.ai" and not host.endswith(".opencode.ai"):
        raise UntrustedProxySecretError("host non autorisé via un proxy non fiable")


def redact_proxy_url(url: str | None) -> str:
    """Masque user/password d'une URL de proxy pour les logs."""
    value = str(url or "")
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return "***"
    if not (parsed.username or parsed.password):
        return value
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, f"***:***@{host}", parsed.path, parsed.query, parsed.fragment))


__all__ = [
    "PUBLIC_AUTH",
    "SECRET_HEADERS",
    "SECRET_ENV_KEYS",
    "UntrustedProxySecretError",
    "assert_secure_endpoint",
    "assert_untrusted_proxy_safe",
    "is_untrusted_proxy",
    "redact_proxy_url",
    "sanitize_free_headers",
]
