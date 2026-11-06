"""Générateur de profils d'impersonation OpenCode (free).

Chaque profil est une installation officielle complète, alignée sur les
captures mitm du 2026-11-01 :

* versions : 1.18.31, 1.19.0, 1.19.3, 1.19.5 (1.18.35 inférée) ;
* client : desktop (vérifié), cli/app (enum binaire, non capturés live) ;
* identité : project / session `ses_` / message `msg_` / UA / Bun ;
* headers exacts du provider `opencode` (aucun x-session-affinity,
  directory ou workspace) ;
* corps officiel 2.2.5 (13 outils) par défaut ; les corps 1.18.31/1.19.3 (10)
  et 1.19.5 (11) sont capturés séparément.

Le proxy utilise la même primitive (`IpIdentity.official_provider_headers`)
via `_official_free_headers` : un profil généré ici est donc rejouable
tel quel sur le wire.

Usage :
    python -m free.identity_generator --versions 1.18.31,1.19.3,1.19.5 \
        --clients desktop --count 5
    python -m free.identity_generator --version 1.19.5 --include-body
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from urllib.parse import urlparse

from free.identities import (
    SUPPORTED_CLIENTS,
    VERIFIED_CLIENTS,
    VERSION_SPECS,
    IpIdentity,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFFICIAL_PAYLOAD = os.path.join(ROOT, "free", "official_payload.json")
DEFAULT_ENDPOINT = "https://opencode.ai/inference/openai/v1/chat/completions"


def _official_body() -> dict:
    try:
        with open(OFFICIAL_PAYLOAD, encoding="utf-8") as f:
            return (json.load(f) or {}).get("body") or {}
    except Exception:
        return {}


def _transport_headers(endpoint: str) -> dict:
    # Ordre exact du client : Connection, Accept, Host, Accept-Encoding.
    out = {
        "Connection": "keep-alive",
        "Accept": "*/*",
    }
    try:
        host = urlparse(endpoint or "").netloc
    except Exception:
        host = ""
    if host:
        out["Host"] = host
    out["Accept-Encoding"] = "gzip, deflate, br, zstd"
    return out


def generate_profile(
    version: str = "1.19.5",
    client: str = "desktop",
    ip: str | None = None,
    project_base: str = "global",
    generation: int = 1,
    endpoint: str = DEFAULT_ENDPOINT,
    include_body: bool = False,
) -> dict:
    """Génère UN profil d'impersonation complet (sans I/O disque)."""
    version = str(version or "").strip()
    if version not in VERSION_SPECS:
        raise ValueError(f"version inconnue: {version!r} (connues: {sorted(VERSION_SPECS)})")
    client = str(client or "desktop").strip().lower()
    if client not in SUPPORTED_CLIENTS:
        raise ValueError(f"client inconnu: {client!r} (connus: {SUPPORTED_CLIENTS})")
    if not ip:
        ip = "gen-" + uuid.uuid4().hex[:12]

    ident = IpIdentity(
        ip,
        base_project=project_base,
        forced_client=client,
        forced_version=version,
        generation=max(0, int(generation or 0)),
        persist=False,
    )
    headers = ident.official_provider_headers(endpoint, None)
    headers.update(_transport_headers(endpoint))

    body = _official_body()
    tools = body.get("tools") or []
    system = ""
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "system":
            system = str(msg.get("content") or "")
            break

    profile = {
        "version": ident.version,
        "client": ident.client,
        "verified": bool(ident.version in VERSION_SPECS and VERSION_SPECS[ident.version].get("verified"))
        and client in VERIFIED_CLIENTS,
        "bun": VERSION_SPECS[ident.version]["bun"],
        "session_id_header": bool(VERSION_SPECS[ident.version]["session_id"]),
        "tls": {
            "ja3": VERSION_SPECS[ident.version]["ja3"],
            "ja3_md5": VERSION_SPECS[ident.version]["ja3_md5"],
            "sigalgs": list(VERSION_SPECS[ident.version]["sigalgs"]),
            "grease": bool(VERSION_SPECS[ident.version]["tls_grease"]),
        },
        "ip": ip,
        "generation": ident.generation,
        "project": ident.project,
        "session": ident.ses_global,
        "headers": headers,
        "body": {
            "model": body.get("model"),
            "tools_count": len(tools),
            "tools": [t.get("function", {}).get("name") for t in tools if isinstance(t, dict)],
            "system_chars": len(system),
        },
    }
    if include_body:
        profile["body"]["request"] = body
    return profile


def generate_many(
    count: int = 1,
    versions: list[str] | None = None,
    clients: list[str] | None = None,
    ip: str | None = None,
    project_base: str = "global",
    start_generation: int = 1,
    endpoint: str = DEFAULT_ENDPOINT,
    include_body: bool = False,
) -> list[dict]:
    """Génère ``count`` profils en alternant versions et clients."""
    versions = list(versions or ["1.19.5", "1.19.3", "1.18.31", "1.19.0"])
    clients = list(clients or ["desktop"])
    for v in versions:
        if v not in VERSION_SPECS:
            raise ValueError(f"version inconnue: {v!r}")
    for c in clients:
        if c not in SUPPORTED_CLIENTS:
            raise ValueError(f"client inconnu: {c!r}")
    out: list[dict] = []
    for i in range(max(0, int(count or 0))):
        out.append(
            generate_profile(
                version=versions[i % len(versions)],
                client=clients[i % len(clients)],
                ip=ip,
                project_base=project_base,
                generation=start_generation + i,
                endpoint=endpoint,
                include_body=include_body,
            )
        )
    return out


def _csv(value: str) -> list[str]:
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Générateur de profils d'impersonation OpenCode free")
    parser.add_argument("--version", default="1.19.5", help="version unique (défaut 1.19.5)")
    parser.add_argument("--versions", default="", help="liste CSV (ex: 1.18.31,1.19.3,1.19.5)")
    parser.add_argument("--client", default="desktop", help="client unique (desktop/cli/app)")
    parser.add_argument("--clients", default="", help="liste CSV (ex: desktop,cli)")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--ip", default="")
    parser.add_argument("--project-base", default="global")
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--include-body", action="store_true")
    parser.add_argument("--json", action="store_true", help="sortie JSON (défaut)")
    parser.add_argument("--out", default="", help="écrit la sortie dans ce fichier")
    args = parser.parse_args(argv)

    versions = _csv(args.versions) or [args.version]
    clients = _csv(args.clients) or [args.client]
    if args.count <= 1 and not args.versions and not args.clients:
        profiles = [
            generate_profile(
                version=args.version,
                client=args.client,
                ip=args.ip or None,
                project_base=args.project_base,
                generation=args.generation,
                endpoint=args.endpoint,
                include_body=args.include_body,
            )
        ]
    else:
        profiles = generate_many(
            count=args.count,
            versions=versions,
            clients=clients,
            ip=args.ip or None,
            project_base=args.project_base,
            start_generation=args.generation,
            endpoint=args.endpoint,
            include_body=args.include_body,
        )
    payload = json.dumps(profiles, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload + "\n")
    else:
        sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_ENDPOINT",
    "generate_many",
    "generate_profile",
    "main",
]
