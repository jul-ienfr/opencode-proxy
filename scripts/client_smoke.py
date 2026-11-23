#!/usr/bin/env python3
"""Smoke test d'un client contre le proxy local (023 T143).

Usage:
    python scripts/client_smoke.py --client claude-code --stream
    python scripts/client_smoke.py --client codex --stream
    python scripts/client_smoke.py --client kilo-code --stream
    python scripts/client_smoke.py --client hermes-agent --stream
    python scripts/client_smoke.py --client deepseek
    python scripts/client_smoke.py --client aider
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:4000"
MODEL = "deepseek-v4.1-flash"
ROOT = Path(__file__).resolve().parent.parent


def _client_key() -> str:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"^(OPENCODE_API_KEY|API_KEY|PROXY_API_KEY)=(.+)$", line.strip())
            if match:
                return match.group(2).strip().strip('"').strip("'")
    return "public"


def _body(client: str, stream: bool) -> tuple[str, dict, dict]:
    key = _client_key()
    tools = [
        {
            "name": "Read",
            "description": "Read a file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]
    if client == "claude-code":
        payload = {
            "model": MODEL,
            "max_tokens": 32,
            "stream": stream,
            "system": [{"type": "text", "text": "You are Claude Code."}],
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            "tools": tools,
        }
        headers = {
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "accept": "text/event-stream" if stream else "application/json",
            "user-agent": "claude-cli/2.0.0",
        }
        return "/v1/messages", headers, payload
    if client == "codex":
        payload = {
            "model": MODEL,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Reply with the single word: ok"}],
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "read",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            "reasoning": {"effort": "low"},
            "max_output_tokens": 32,
            "stream": stream,
        }
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {key}",
            "accept": "text/event-stream" if stream else "application/json",
            "user-agent": "codex-cli/0.1",
        }
        return "/v1/responses", headers, payload
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a coding assistant."},
            {"role": "user", "content": "Reply with the single word: ok"},
        ],
        "max_tokens": 32,
        "stream": stream,
    }
    if client != "aider":
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
    if stream:
        payload["stream_options"] = {"include_usage": True}
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {key}",
        "accept": "text/event-stream" if stream else "application/json",
        "user-agent": f"{client}/smoke",
    }
    return "/v1/chat/completions", headers, payload


def _events(text: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r'"type"\s*:\s*"([a-z_]+)"', text):
        name = match.group(1)
        if name not in found:
            found.append(name)
    return found[:8]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test one client")
    parser.add_argument(
        "--client",
        default="claude-code",
        choices=[
            "claude-code",
            "codex",
            "kilo-code",
            "hermes-agent",
            "deepseek",
            "aider",
            "openai-sdk",
            "custom",
        ],
    )
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--fresh-ip", action="store_true", help="operator reminder only")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args(argv)
    if args.fresh_ip:
        print("note: a fresh IP must be provided operationally (one format per IP)")
    path, headers, payload = _body(args.client, bool(args.stream))
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    failures = 0
    for attempt in range(1, max(1, args.repeat) + 1):
        req = urllib.request.Request(API + path, data=data, headers=headers, method="POST")
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                blob = resp.read(8000).decode("utf-8", "replace")
                print(
                    f"[{attempt}] {args.client} HTTP {resp.status} "
                    f"in {time.time() - started:.1f}s events={_events(blob)}"
                )
        except urllib.error.HTTPError as exc:
            failures += 1
            raw = exc.read(600).decode("utf-8", "replace")
            print(
                f"[{attempt}] {args.client} HTTP {exc.code} "
                f"in {time.time() - started:.1f}s retry={exc.headers.get('retry-after')} "
                f"body={raw[:220]}"
            )
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[{attempt}] {args.client} ERR {type(exc).__name__}: {exc}")
        time.sleep(1)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
