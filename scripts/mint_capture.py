#!/usr/bin/env python3
"""Mint une capture de test free 2.2.5 avec un `ses_` temporel valide.

Le gate free décode les 12 premiers hex de `ses_` comme le résultat de
``descending()`` (``packages/schema/src/identifier.ts``). Un préfixe aléatoire
est rejeté avec ``FreeUsageLimitError`` + ``Retry-After`` jusqu'à 00:00 UTC,
même sur une IP avec du quota.

Ce script maintient ``free/captures/2.2.5.json`` : le corps et l'ordre des
en-têtes du client officiel 2.2.5, avec un `ses_` neuf à chaque exécution.
Le harnais de test lit cette capture et re-mint un `ses_` PAR REQUÊTE (les
quatre en-têtes de session pointent vers le même ID neuf).

Exemples :
    python scripts/mint_capture.py
    python scripts/mint_capture.py --verify
    python scripts/mint_capture.py --from-raw C:\\...\\3_req.bin --verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CAPTURE = ROOT / "free" / "captures" / "2.2.5.json"
ARCHIVE = ROOT / "free" / "captures" / "archive"
PAYLOAD = ROOT / "free" / "official_payload.json"
SPEC = ROOT / "free" / "impersonation.json"
URL = "https://opencode.ai/inference/openai/v1/chat/completions"
SESSION_HEADERS = (
    "x-opencode-session",
    "x-opencode-session-id",
    "x-session-affinity",
    "x-session-id",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mint_session() -> str:
    """Minter de production : `opencode._mint_oc_id('ses', 'descending')`."""
    import opencode as oc

    return oc._mint_oc_id("ses", "descending")


def trace_headers() -> dict[str, str]:
    trace = secrets.token_hex(16)
    span = secrets.token_hex(8)
    return {
        "b3": f"{trace}-{span}-1-{secrets.token_hex(8)}",
        "traceparent": f"00-{trace}-{span}-01",
    }


def build_headers(session: str, body_bytes: int, spec: dict) -> dict[str, str]:
    headers = {
        "Authorization": "Bearer public",
        "Content-Type": "application/json",
        "User-Agent": spec.get("user_agent") or "opencode/latest/2.2.5/cli",
        **trace_headers(),
        "x-opencode-client": spec.get("client") or "cli",
        "x-opencode-project": spec.get("project") or "global",
        "x-opencode-session": session,
        "x-opencode-session-id": session,
        "x-session-affinity": session,
        "x-session-id": session,
        "Connection": "keep-alive",
        "Accept": "*/*",
        "Host": "opencode.ai",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Content-Length": str(body_bytes),
    }
    return headers


def raw_from_payload() -> tuple[dict, str]:
    data = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    body = data.get("body") or {}
    raw = str(data.get("_body_raw") or "")
    if not raw:
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    return body, raw


def load_seed(from_raw: str | None) -> tuple[dict, str, dict | None, str | None]:
    """Retourne (body, raw, headers existants, captured_at)."""
    if from_raw:
        p = Path(from_raw)
        raw = p.read_text(encoding="utf-8")
        return json.loads(raw), raw, None, None
    if CAPTURE.exists():
        data = json.loads(CAPTURE.read_text(encoding="utf-8"))
        body = data.get("body") or {}
        raw = str(data.get("_body_raw") or "")
        if not raw:
            raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        return body, raw, data.get("headers"), data.get("captured_at")
    body, raw = raw_from_payload()
    return body, raw, None, json.loads(PAYLOAD.read_text(encoding="utf-8")).get("_captured_at")


def replace_model(raw: str, model: str | None) -> str:
    if not model:
        return raw
    try:
        old = json.loads(raw).get("model")
    except Exception:
        return raw
    if not old or old == model:
        return raw
    needle = f'"model":{json.dumps(old, ensure_ascii=False)}'
    replacement = f'"model":{json.dumps(model, ensure_ascii=False)}'
    if needle in raw:
        return raw.replace(needle, replacement, 1)
    body = json.loads(raw)
    body["model"] = model
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def archive_previous(no_archive: bool) -> None:
    if no_archive or not CAPTURE.exists():
        return
    try:
        old = json.loads(CAPTURE.read_text(encoding="utf-8"))
    except Exception:
        return
    stamp = str(old.get("minted_at") or old.get("captured_at") or "unknown").replace(":", "").replace("-", "")
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    target = ARCHIVE / f"2.2.5-{stamp}.json"
    try:
        shutil.copy2(CAPTURE, target)
    except OSError:
        pass


def write_capture(capture: dict) -> None:
    CAPTURE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CAPTURE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(capture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(CAPTURE)


def verify(capture: dict) -> int:
    try:
        import opencode as oc
        from curl_cffi import requests
    except Exception as exc:
        print(f"verify: import impossible: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    try:
        impersonate, fp = oc._get_free_fp_kwargs("1.19.5")
    except Exception:
        impersonate, fp = "chrome131", None
    kwargs = {
        "data": capture["_body_raw"].encode("utf-8"),
        "headers": capture["headers"],
        "timeout": 45,
        "stream": False,
    }
    if impersonate:
        kwargs["impersonate"] = impersonate
    if isinstance(fp, dict):
        kwargs.update(fp)
    try:
        response = requests.post(URL, **kwargs)
    except Exception as exc:
        print(f"verify: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    text = (response.text or "").replace("\n", " ")
    print(f"verify: status={response.status_code} retry_after={response.headers.get('retry-after')}")
    print(f"verify: body={text[:240]}")
    return 0 if response.status_code == 200 else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-raw", help="corps JSON brut pour amorcer une nouvelle capture")
    parser.add_argument("--model", help="remplacer le modèle dans le corps")
    parser.add_argument("--verify", action="store_true", help="envoyer la capture et vérifier un 200")
    parser.add_argument("--no-archive", action="store_true")
    args = parser.parse_args(argv)

    body, raw, existing_headers, captured_at = load_seed(args.from_raw)
    raw = replace_model(raw, args.model)
    body = json.loads(raw)
    body_bytes = len(raw.encode("utf-8"))
    session = mint_session()
    spec = json.loads(SPEC.read_text(encoding="utf-8")) if SPEC.exists() else {}

    if existing_headers:
        headers = dict(existing_headers)
    else:
        headers = build_headers(session, body_bytes, spec)
    for name in SESSION_HEADERS:
        headers[name] = session
    headers.update(trace_headers())
    headers["Content-Length"] = str(body_bytes)
    headers["Authorization"] = "Bearer public"
    headers["Content-Type"] = "application/json"
    headers.setdefault("User-Agent", spec.get("user_agent") or "opencode/latest/2.2.5/cli")
    headers.setdefault("x-opencode-client", spec.get("client") or "cli")
    headers.setdefault("x-opencode-project", spec.get("project") or "global")
    headers.setdefault("Connection", "keep-alive")
    headers.setdefault("Accept", "*/*")
    headers.setdefault("Host", "opencode.ai")
    headers.setdefault("Accept-Encoding", "gzip, deflate, br, zstd")

    capture = {
        "schema": "opencode-free-capture/v1",
        "version": "2.2.5",
        "source": (
            "official opencode v2.2.5 (release opencode-windows-x64.zip, "
            "sha256 2f3bfd6b3527c60c2a882557b20ddc9ba6bdff4b5a8fe61c853da8fcfb3076e8)"
        ),
        "captured_at": captured_at or now_iso(),
        "minted_at": now_iso(),
        "method": "POST",
        "url": URL,
        "session": session,
        "headers": headers,
        "body": body,
        "_body_raw": raw,
        "body_bytes": body_bytes,
        "_body_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "notes": (
            "Template only: test_free_egress.py re-mints a fresh ses_ per request "
            "and rewrites the four session headers. The ses_ prefix is a "
            "descending() timestamp; a random prefix is rejected as a false "
            "FreeUsageLimitError."
        ),
    }

    archive_previous(args.no_archive)
    write_capture(capture)
    print(f"capture: {CAPTURE}")
    print(f"session: {session}")
    print(f"minted_at: {capture['minted_at']}")
    print(f"body_bytes: {body_bytes}")
    print(f"body_sha256: {capture['_body_sha256']}")

    if args.verify:
        return verify(capture)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
