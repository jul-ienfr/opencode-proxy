#!/usr/bin/env python3
"""A/B de wire à IP neuve par variante — 023 T026 / SC-005 / principe XVIII.

Règle dure : les deux variantes ne partagent JAMAIS une IP. Chaque variante
utilise sa propre station (donc son propre egress frais) et un format unique.

Usage:
    python scripts/wire_ab.py --station-official 1 --station-converted 3 --samples 20
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from curl_cffi import requests as creq  # noqa: E402

from free.clients import respond, tool_policy  # noqa: E402

ENDPOINT = "https://opencode.ai/inference/openai/v1/chat/completions"
CAPTURE = ROOT / "free" / "captures" / "2.3.0.json"
EVIDENCE = ROOT / "docs" / "evidence"


def _official_body() -> tuple[str, dict]:
    data = json.loads(CAPTURE.read_text(encoding="utf-8"))
    raw = data["body_raw"]
    headers = {
        "authorization": "Bearer public",
        "content-type": "application/json",
        "user-agent": data.get("user_agent") or "opencode/latest/2.3.0/cli",
        "x-opencode-client": data.get("client") or "cli",
        "x-opencode-project": data.get("project") or "global",
    }
    hdrs = data.get("headers")
    # La capture stocke les en-tetes en LISTE de "Nom: valeur" (pas un dict) :
    # `isinstance(hdrs, dict)` etait toujours faux et les en-tetes de session
    # n'etaient jamais recopies. On normalise les deux formes.
    pairs: dict[str, str] = {}
    if isinstance(hdrs, dict):
        pairs = {str(k).lower(): str(v) for k, v in hdrs.items()}
    elif isinstance(hdrs, list):
        for entry in hdrs:
            if isinstance(entry, str) and ":" in entry:
                name, value = entry.split(":", 1)
                pairs[name.strip().lower()] = value.lstrip()
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                pairs[str(entry[0]).strip().lower()] = str(entry[1])
    for key in ("b3", "traceparent", "x-opencode-session", "x-opencode-session-id",
                "x-session-affinity", "x-session-id"):
        if pairs.get(key):
            headers[key] = pairs[key]
    return raw, headers


def _converted_body() -> str:
    sample = {
        "model": "deepseek-v4.1-flash",
        "max_tokens": 64,
        "system": [{"type": "text", "text": "You are Claude Code."}],
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "tools": [
            {
                "name": "Read",
                "description": "Read a file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ],
        "stream": True,
    }
    chat = respond.convert_request(sample, "anthropic_messages")
    grid = tool_policy.build_grid(chat.get("tools") or [])
    chat["tools"] = grid["tools"]
    return json.dumps(chat, ensure_ascii=False, separators=(",", ":"))


def _send(body: str, headers: dict, station: int) -> dict:
    socks = f"socks5h://127.0.0.1:{1079 + int(station)}"
    session = creq.Session(
        impersonate="chrome131",
        proxies={"http": socks, "https": socks},
        timeout=90,
    )
    try:
        resp = session.post(ENDPOINT, data=body.encode("utf-8"), headers=headers, stream=True)
        try:
            first = next(resp.iter_content(160)).decode("utf-8", "replace")
        except Exception:
            first = ""
        return {
            "status": int(resp.status_code),
            "retry_after": resp.headers.get("retry-after"),
            "head": first[:160],
        }
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wire A/B on distinct fresh IPs")
    parser.add_argument("--station-official", type=int, required=True)
    parser.add_argument("--station-converted", type=int, required=True)
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args(argv)
    if args.station_official == args.station_converted:
        print("refusing: both variants would share one IP (constitution XVIII)", file=sys.stderr)
        return 2
    official_raw, official_headers = _official_body()
    converted = _converted_body()
    converted_headers = dict(official_headers)
    samples = {"official": [], "converted": []}
    for index in range(max(1, args.samples)):
        samples["official"].append(_send(official_raw, official_headers, args.station_official))
        samples["converted"].append(_send(converted, converted_headers, args.station_converted))
    report = {
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "samples": args.samples,
        "stations": {
            "official": args.station_official,
            "converted": args.station_converted,
        },
        "official": samples["official"],
        "converted": samples["converted"],
        "summary": {
            "official_200": sum(1 for s in samples["official"] if s["status"] == 200),
            "converted_200": sum(1 for s in samples["converted"] if s["status"] == 200),
        },
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = EVIDENCE / f"wire-ab-{stamp}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(f"evidence: {path}")
    return 0 if report["summary"]["official_200"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
