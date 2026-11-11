#!/usr/bin/env python3
"""Teste un egress free OpenCode avec le wire 2.2.5 exact.

Aucun compte, aucun token : `Authorization: Bearer public`.

Exemples :
    python scripts/test_free_egress.py --station 3
    python scripts/test_free_egress.py --proxy socks5h://127.0.0.1:1082
    python scripts/test_free_egress.py --proxy http://user:pass@host:8080 --repeat 3
    python scripts/test_free_egress.py --proxy direct --model big-pickle,glm-5.3-flash-free

Sortie : un JSON par requête, puis un résumé 200/403/429.
"""

from __future__ import annotations

import argparse
import json
import secrets
import string
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from curl_cffi import requests  # noqa: E402
from curl_cffi.const import CurlOpt  # noqa: E402

FREE_URL = "https://opencode.ai/inference/openai/v1/chat/completions"
PAYLOAD = ROOT / "free" / "official_payload.json"
SPEC = ROOT / "free" / "impersonation.json"
CAPTURE = ROOT / "free" / "captures" / "2.2.5.json"
B62 = string.ascii_letters + string.digits


def canonical_session() -> str:
    # The free gate validates the 12-hex prefix as a descending timestamp.
    # A random prefix returns FreeUsageLimitError (429) even with a fresh IP.
    # Reuse the production minter (exact port of packages/schema/src/identifier.ts).
    try:
        import opencode as oc

        return oc._mint_oc_id("ses", "descending")
    except Exception:
        return "ses_" + secrets.token_hex(6) + "".join(secrets.choice(B62) for _ in range(14))


def load_payload() -> tuple[dict, str]:
    data = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    body = data.get("body") or {}
    raw = str(data.get("_body_raw") or "")
    if not raw:
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    return body, raw


def body_for(raw: str, model: str) -> bytes:
    try:
        old = json.loads(raw).get("model")
    except Exception:
        old = None
    if old and model != old:
        needle = f'"model":{json.dumps(old, ensure_ascii=False)}'
        replacement = f'"model":{json.dumps(model, ensure_ascii=False)}'
        if needle in raw:
            raw = raw.replace(needle, replacement, 1)
    return raw.encode("utf-8")


def load_capture(path: Path) -> dict | None:
    """Charge la capture-template 2.2.5 (corps + ordre des en-têtes)."""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"capture illisible: {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return None


def headers_for(proxy: str) -> dict:
    spec = json.loads(SPEC.read_text(encoding="utf-8")) if SPEC.exists() else {}
    session = canonical_session()
    trace = secrets.token_hex(16)
    span = secrets.token_hex(8)
    headers = {
        "Authorization": "Bearer public",
        "Content-Type": "application/json",
        "User-Agent": spec.get("user_agent") or "opencode/latest/2.2.5/cli",
        "b3": f"{trace}-{span}-1-{secrets.token_hex(8)}",
        "traceparent": f"00-{trace}-{span}-01",
        "x-opencode-client": spec.get("client") or "cli",
        "x-opencode-project": spec.get("project") or "global",
        "x-opencode-session": session,
        "x-opencode-session-id": session,
        "x-session-affinity": session,
        "x-session-id": session,
        "Accept": "*/*",
        "Connection": "keep-alive",
        "Accept-Encoding": "gzip, deflate, br, zstd",
    }
    return headers


def post_once(
    proxy: str,
    model: str,
    timeout: float,
    capture: dict | None = None,
    reuse_session: bool = False,
    client: str = "",
    project: str = "",
    dry_run: bool = False,
) -> dict:
    try:
        import opencode as oc

        impersonate, fp = oc._get_free_fp_kwargs("1.19.5")
    except Exception:
        impersonate, fp = "chrome131", None
    if capture:
        raw = str(capture.get("_body_raw") or "")
        if not raw:
            raw = json.dumps(capture.get("body") or {}, separators=(",", ":"), ensure_ascii=False)
        data = body_for(raw, model)
        headers = dict(capture.get("headers") or {})
        if reuse_session and capture.get("session"):
            session = str(capture["session"])
        else:
            session = canonical_session()
        for name in (
            "x-opencode-session",
            "x-opencode-session-id",
            "x-session-affinity",
            "x-session-id",
        ):
            headers[name] = session
        trace = secrets.token_hex(16)
        span = secrets.token_hex(8)
        headers["b3"] = f"{trace}-{span}-1-{secrets.token_hex(8)}"
        headers["traceparent"] = f"00-{trace}-{span}-01"
        headers["Content-Length"] = str(len(data))
        headers.setdefault("Authorization", "Bearer public")
        headers.setdefault("Content-Type", "application/json")
        if client == "desktop":
            headers["User-Agent"] = "opencode/latest/2.2.5/desktop"
            headers["x-opencode-client"] = "desktop"
        elif client == "cli":
            headers["User-Agent"] = "opencode/latest/2.2.5/cli"
            headers["x-opencode-client"] = "cli"
        if project:
            headers["x-opencode-project"] = project
    else:
        _, raw = load_payload()
        data = body_for(raw, model)
        headers = headers_for(proxy)
        session = str(headers.get("x-opencode-session") or "")
    if dry_run:
        try:
            parsed = json.loads(data)
        except Exception:
            parsed = {}
        tools = parsed.get("tools") if isinstance(parsed.get("tools"), list) else []
        return {
            "egress": proxy or "direct",
            "model": model,
            "session": session,
            "status": "dry",
            "headers": list(headers.items()),
            "header_order": [k for k, _ in headers.items()],
            "body_keys": list(parsed),
            "tool_names": [
                (t.get("function") or {}).get("name") if isinstance(t.get("function"), dict) else t.get("name")
                for t in tools
                if isinstance(t, dict)
            ],
            "body_bytes": len(data),
            "body_sha256": __import__("hashlib").sha256(data).hexdigest(),
        }
    kwargs = {
        "data": data,
        "headers": headers,
        "timeout": timeout,
        "stream": False,
    }
    if proxy and proxy != "direct":
        # curl_cffi gère mieux `proxy=` que le dict `proxies` pour le CONNECT
        # HTTP de Gluetun (le dict faisait aborter le tunnel).
        kwargs["proxy"] = proxy
    if impersonate:
        kwargs["impersonate"] = impersonate
    if isinstance(fp, dict):
        kwargs.update(fp)
    if proxy.startswith("https://"):
        kwargs["curl_options"] = {
            CurlOpt.PROXY_SSL_VERIFYPEER: 0,
            CurlOpt.PROXY_SSL_VERIFYHOST: 0,
        }
    started = time.time()
    try:
        response = requests.post(FREE_URL, **kwargs)
        latency = round((time.time() - started) * 1000, 1)
        retry_after = 0.0
        try:
            retry_after = float(response.headers.get("retry-after") or 0)
        except (TypeError, ValueError):
            pass
        text = response.text or ""
        return {
            "egress": proxy or "direct",
            "model": model,
            "session": session,
            "status": response.status_code,
            "retry_after": retry_after,
            "latency_ms": latency,
            "body": text[:220].replace("\n", " "),
        }
    except Exception as exc:
        return {
            "egress": proxy or "direct",
            "model": model,
            "session": session,
            "status": 0,
            "retry_after": 0.0,
            "latency_ms": round((time.time() - started) * 1000, 1),
            "error": f"{type(exc).__name__}: {exc}"[:220],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy", default="direct", help="socks5h://, http://, or direct")
    parser.add_argument("--station", type=int, default=0, help="shortcut: 1..6 -> socks5h")
    parser.add_argument("--model", default="deepseek-v4.1-flash-free")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=40.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--capture",
        default=str(CAPTURE),
        help="capture-template JSON (default: free/captures/2.2.5.json)",
    )
    parser.add_argument(
        "--no-capture",
        action="store_true",
        help="ignorer la capture et utiliser le builder historique",
    )
    parser.add_argument(
        "--reuse-session",
        action="store_true",
        help="réutiliser le ses_ de la capture au lieu d'en minter un par requête",
    )
    parser.add_argument(
        "--mint",
        action="store_true",
        help="re-mint la capture avant les tests",
    )
    parser.add_argument(
        "--client",
        choices=("capture", "cli", "desktop"),
        default="capture",
        help="profil client 2.2.5 : capture (défaut), cli ou desktop",
    )
    parser.add_argument(
        "--project",
        default="",
        help="valeur forcée de x-opencode-project (ex. global)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="construire la requête et afficher le profil sans envoyer",
    )
    args = parser.parse_args(argv)

    if args.mint:
        import subprocess

        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "mint_capture.py")],
            check=False,
        )
    capture = None if args.no_capture else load_capture(Path(args.capture))
    if capture is None and not args.no_capture:
        print(
            f"capture absente: {args.capture} — fallback builder historique",
            file=sys.stderr,
        )

    proxy = f"socks5h://127.0.0.1:{1079 + args.station}" if args.station else args.proxy
    models = [item.strip() for item in args.model.split(",") if item.strip()]
    results = []
    for _ in range(max(1, args.repeat)):
        for model in models:
            result = post_once(
                proxy,
                model,
                args.timeout,
                capture=capture,
                reuse_session=args.reuse_session,
                client="" if args.client == "capture" else args.client,
                project=args.project,
                dry_run=args.dry_run,
            )
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)

    counts = {200: 0, 403: 0, 429: 0, 0: 0}
    for result in results:
        counts[result["status"] if result["status"] in counts else 0] = counts.get(result["status"], 0) + 1
    print(
        json.dumps(
            {
                "summary": {
                    "total": len(results),
                    "ok": counts.get(200, 0),
                    "forbidden": counts.get(403, 0),
                    "quota": counts.get(429, 0),
                    "network": counts.get(0, 0),
                }
            }
        )
    )
    return 0 if counts.get(200, 0) else 2


if __name__ == "__main__":
    raise SystemExit(main())
