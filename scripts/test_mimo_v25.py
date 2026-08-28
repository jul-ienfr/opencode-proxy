#!/usr/bin/env python3
"""Test mimo-v2.5 via le proxy comme un client Claude Code (Anthropic /v1/messages).

Reproduit exactement le flux Claude Code:
  ANTHROPIC_BASE_URL=http://localhost:4000
  headers: x-api-key, anthropic-version, content-type
  body: {model, max_tokens, messages, stream}

Couvre:
  1. GET /v1/models (proxy up)
  2. POST /v1/messages non-stream mimo-v2.5
  3. POST /v1/messages stream mimo-v2.5 (SSE)
  4. POST /anthropic/v1/messages non-stream (variante Claude Code)
  5. POST /v1/messages/count_tokens

Usage:
  python scripts/test_mimo_v25.py [--port 4000] [--model mimo-v2.5]
  ANTHROPIC_BASE_URL=http://localhost:4000 python scripts/test_mimo_v25.py

Inspire de scripts/vpn_e2e_smoke.py (harness check + httpx).
"""

from __future__ import annotations

import argparse
import json
import sys

try:
    import httpx
except ImportError:
    print("httpx requis: pip install httpx", file=sys.stderr)
    sys.exit(2)

FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    tag = "PASS" if ok else "FAIL"
    line = f"[{tag}] {name}" + (f" - {detail}" if detail else "")
    print(line, flush=True)
    if not ok:
        FAILED += 1


def extract_text(content: list[dict]) -> str:
    """Concatene tous les blocks type=text (ignore thinking)."""
    parts = [b.get("text", "") for b in content if b.get("type") == "text"]
    return "".join(parts)


async def test_health(base: str) -> bool:
    async with httpx.AsyncClient(base_url=base, timeout=10) as c:
        try:
            r = await c.get("/v1/models")
        except Exception as e:
            check("GET /v1/models -> 200", False, str(e))
            return False
    ok = r.status_code == 200
    check("GET /v1/models -> 200", ok, f"HTTP {r.status_code}")
    return ok


async def test_non_stream(base: str, model: str) -> None:
    payload = {
        "model": model,
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "Dis pong et rien d'autre. Une seule ligne."}],
    }
    headers = {"x-api-key": "test", "anthropic-version": "2023-06-01"}
    async with httpx.AsyncClient(base_url=base, timeout=90) as c:
        try:
            r = await c.post("/v1/messages", json=payload, headers=headers)
        except Exception as e:
            check(f"non-stream {model} ->200", False, str(e))
            return
    body_text = r.text
    if r.status_code != 200:
        check(f"non-stream {model} ->200", False, f"HTTP {r.status_code}: {body_text[:400]}")
        return
    check(f"non-stream {model} ->200", True, f"HTTP 200")
    try:
        data = r.json()
    except Exception as e:
        check(f"non-stream {model} ->JSON Anthropic", False, str(e))
        return
    has_shape = data.get("type") == "message" and isinstance(data.get("content"), list)
    check(f"non-stream {model} ->shape Anthropic", has_shape, f"type={data.get('type')}")
    text = extract_text(data.get("content", []))
    check(f"non-stream {model} ->text non vide", bool(text.strip()), f"text={text[:80]!r}")
    usage = data.get("usage", {})
    check(f"non-stream {model} ->usage", bool(usage.get("output_tokens")), str(usage))
    check(f"non-stream {model} ->stop_reason", bool(data.get("stop_reason")), str(data.get("stop_reason")))
    # diag: model echo
    if data.get("model") != model:
        print(f"  [info] model echo: {data.get('model')!r} (attendu {model!r})")


async def test_stream(base: str, model: str) -> None:
    payload = {
        "model": model,
        "max_tokens": 128,
        "stream": True,
        "messages": [{"role": "user", "content": "Dis pong et rien d'autre."}],
    }
    headers = {"x-api-key": "test", "anthropic-version": "2023-06-01"}
    chunks = 0
    has_delta = False
    has_stop = False
    has_text = False
    async with httpx.AsyncClient(base_url=base, timeout=90) as c:
        try:
            async with c.stream("POST", "/v1/messages", json=payload, headers=headers) as r:
                ok_status = r.status_code == 200
                check(f"stream {model} ->200", ok_status, f"HTTP {r.status_code}")
                if not ok_status:
                    body = await r.aread()
                    print(f"  body: {body[:600]!r}")
                    return
                async for line in r.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            continue
                        chunks += 1
                        try:
                            evt = json.loads(data_str)
                        except Exception:
                            continue
                        t = evt.get("type", "")
                        if t == "content_block_delta":
                            has_delta = True
                            d = evt.get("delta", {})
                            if d.get("type") == "text_delta" and d.get("text"):
                                has_text = True
                        if t == "message_stop":
                            has_stop = True
        except Exception as e:
            check(f"stream {model} ->SSE", False, str(e))
            return
    check(f"stream {model} ->SSE chunks", chunks > 0, f"{chunks} events")
    check(f"stream {model} ->content_block_delta", has_delta, "")
    check(f"stream {model} ->text_delta non vide", has_text, "")
    check(f"stream {model} ->message_stop", has_stop, "")


async def test_anthropic_prefix(base: str, model: str) -> None:
    payload = {
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "Dis pong."}],
    }
    headers = {"x-api-key": "test", "anthropic-version": "2023-06-01"}
    async with httpx.AsyncClient(base_url=base, timeout=60) as c:
        try:
            r = await c.post("/anthropic/v1/messages", json=payload, headers=headers)
        except Exception as e:
            check(f"/anthropic/v1/messages {model} ->200", False, str(e))
            return
    if r.status_code != 200:
        check(f"/anthropic/v1/messages {model} ->200", False, f"HTTP {r.status_code}: {r.text[:300]}")
        return
    check(f"/anthropic/v1/messages {model} ->200", True, "")
    try:
        data = r.json()
        text = extract_text(data.get("content", []))
        check(f"/anthropic/v1/messages {model} ->text", bool(text.strip()), repr(text[:60]))
    except Exception as e:
        check(f"/anthropic/v1/messages {model} ->JSON", False, str(e))


async def test_count_tokens(base: str, model: str) -> None:
    payload = {"model": model, "messages": [{"role": "user", "content": "Hello world, count my tokens please."}]}
    headers = {"x-api-key": "test", "anthropic-version": "2023-06-01"}
    async with httpx.AsyncClient(base_url=base, timeout=30) as c:
        try:
            r = await c.post("/v1/messages/count_tokens", json=payload, headers=headers)
        except Exception as e:
            check(f"count_tokens {model} ->200", False, str(e))
            return
    ok = r.status_code == 200
    check(f"count_tokens {model} ->200", ok, f"HTTP {r.status_code}: {r.text[:200]}")
    if ok:
        try:
            data = r.json()
            check(f"count_tokens {model} ->input_tokens", "input_tokens" in data, str(data))
        except Exception as e:
            check(f"count_tokens {model} ->JSON", False, str(e))


async def main_async(args: argparse.Namespace) -> None:
    base = f"http://127.0.0.1:{args.port}"
    # ANTHROPIC_BASE_URL override si fourni
    import os

    env_base = os.environ.get("ANTHROPIC_BASE_URL", "").strip().rstrip("/")
    if env_base:
        base = env_base
        print(f"ANTHROPIC_BASE_URL override -> {base}")

    print(f"target: {base}  model: {args.model}")
    ok = await test_health(base)
    if not ok:
        print(f"\n{FAILED} FAILED — proxy injoignable")
        sys.exit(1)

    print(f"\n── non-stream /v1/messages ({args.model})")
    await test_non_stream(base, args.model)

    print(f"\n── stream /v1/messages ({args.model})")
    await test_stream(base, args.model)

    print(f"\n── /anthropic/v1/messages ({args.model})")
    await test_anthropic_prefix(base, args.model)

    print(f"\n── count_tokens ({args.model})")
    await test_count_tokens(base, args.model)

    # Optionnel: alias haiku en info (peut etre 503 si cles free epuisees — pas un echec bloquant)
    if args.with_haiku:
        print("\n── alias haiku (info, non bloquant)")
        payload = {"model": "haiku", "max_tokens": 32, "messages": [{"role": "user", "content": "Dis pong."}]}
        headers = {"x-api-key": "test", "anthropic-version": "2023-06-01"}
        async with httpx.AsyncClient(base_url=base, timeout=60) as c:
            try:
                r = await c.post("/v1/messages", json=payload, headers=headers)
                if r.status_code == 200:
                    check("alias haiku ->200 (via minimax-m2.5)", True, r.json().get("model", ""))
                else:
                    print(f"[INFO] alias haiku ->HTTP {r.status_code}: {r.text[:300]} (upstream/keys, pas un bug proxy)")
            except Exception as e:
                print(f"[INFO] alias haiku exception: {e}")

    print(f"\n{'ALL PASS' if FAILED == 0 else f'{FAILED} FAILED'}")
    sys.exit(0 if FAILED == 0 else 1)


def main() -> None:
    p = argparse.ArgumentParser(description="Test mimo-v2.5 comme client Claude Code")
    p.add_argument("--port", type=int, default=4000, help="port du proxy (defaut 4000)")
    p.add_argument("--model", type=str, default="mimo-v2.5", help="model a tester (defaut mimo-v2.5)")
    p.add_argument("--with-haiku", action="store_true", help="teste aussi l'alias haiku (info)")
    args = p.parse_args()
    import asyncio

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
