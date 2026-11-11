#!/usr/bin/env python3
"""Synchronise l'impersonation free avec le dernier client OpenCode.

Pipeline :
  1. lit le feed de mise à jour du client desktop ;
  2. compare la version installée à la dernière publiée ;
  3. lance le client dans un profil TEMPORAIRE, sans compte ;
  4. capture la requête free réelle (en-têtes + corps) via un provider local ;
  5. compare au format de référence `free/impersonation.json` ;
  6. avec --apply, met à jour la référence et, avec --apply-body, le corps.

Aucun compte n'est utilisé : le profil est vierge et le provider `opencode`
est redirigé vers un serveur de capture local.

Exemples :
    python scripts/opencode_client_sync.py --check
    python scripts/opencode_client_sync.py --capture --diff
    python scripts/opencode_client_sync.py --capture --diff --apply
    python scripts/opencode_client_sync.py --watch --apply
    python scripts/opencode_client_sync.py --install-task
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "free" / "impersonation.json"
PAYLOAD_PATH = ROOT / "free" / "official_payload.json"
LOG_PATH = ROOT / "logs" / "opencode_client_sync.log"
FEED_URL = "https://opencode.ai/update/api/latest/desktop/opencode/"
DEFAULT_CLI = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Programs"
    / "@opencode-aidesktop"
    / "resources"
    / "opencode-cli.exe"
)
FREE_MODEL = "opencode/deepseek-v4.1-flash-free"

_CAPTURED: list[dict[str, Any]] = []
_CAPTURE_LOCK = threading.Lock()


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


# ── feed / version ───────────────────────────────────────────────────────────


def fetch_feed() -> dict[str, Any]:
    response = requests.get(FEED_URL + "latest.yml", timeout=30)
    response.raise_for_status()
    data = yaml.safe_load(response.content.decode("utf-8", "replace")) or {}
    if not isinstance(data, dict):
        return {}
    return data


def installed_version(cli: Path) -> str:
    try:
        out = subprocess.run(
            [str(cli), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        match = re.search(r"(\d+\.\d+\.\d+)", out.stdout or out.stderr or "")
        return match.group(1) if match else ""
    except Exception as exc:
        log(f"version read failed: {type(exc).__name__}: {exc}")
        return ""


def version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(value).split("."))
    except Exception:
        return (0,)


# ── capture server ───────────────────────────────────────────────────────────


class CaptureHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        parsed: Any = None
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        with _CAPTURE_LOCK:
            _CAPTURED.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "body_raw": raw.decode("utf-8", "replace"),
                    "body": parsed,
                    "at": time.time(),
                }
            )
        stream = bool(isinstance(parsed, dict) and parsed.get("stream"))
        if stream:
            chunk = {
                "id": "chatcmpl-capture",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": (parsed or {}).get("model") or "free",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
            }
            payload = (
                "data: "
                + json.dumps(chunk, ensure_ascii=False)
                + "\n\ndata: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = json.dumps(
            {
                "id": "chatcmpl-capture",
                "object": "chat.completion",
                "created": 0,
                "model": (parsed or {}).get("model") or "free",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: Any) -> None:
        return


def start_capture_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}/v1"


# ── profil temporaire et exécution ───────────────────────────────────────────


def write_profile_config(home: Path, base_url: str) -> None:
    config_dir = home / ".config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "opencode": {
                "options": {
                    "baseURL": base_url,
                }
            }
        },
    }
    (config_dir / "opencode.jsonc").write_text(
        json.dumps(config, indent=2), encoding="utf-8"
    )


def run_capture(cli: Path, timeout: float) -> list[dict[str, Any]]:
    _CAPTURED.clear()
    server, base_url = start_capture_server()
    home = Path(tempfile.mkdtemp(prefix="oc-format-"))
    try:
        write_profile_config(home, base_url)
        env = dict(os.environ)
        env.update(
            {
                "USERPROFILE": str(home),
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "APPDATA": str(home / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(home / "AppData" / "Local"),
            }
        )
        for key in (
            "OPENCODE_CONSOLE_TOKEN",
            "OPENCODE_CONSOLE_ORG",
            "OPENCODE_API_KEY",
            "OPENCODE_GO_AUTH_COOKIE",
        ):
            env.pop(key, None)
        command = [
            str(cli),
            "run",
            "-m",
            FREE_MODEL,
            "ping",
            "--standalone",
            "--format",
            "json",
        ]
        try:
            subprocess.run(
                command,
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            log(f"client timed out after {timeout}s")
        with _CAPTURE_LOCK:
            return list(_CAPTURED)
    finally:
        server.shutdown()
        shutil.rmtree(home, ignore_errors=True)


def pick_main_request(requests_list: list[dict[str, Any]]) -> dict[str, Any] | None:
    best = None
    best_score = -1
    for item in requests_list:
        body = item.get("body")
        if not isinstance(body, dict):
            continue
        score = len(body.get("tools") or []) * 1000 + len(item.get("body_raw") or "")
        if score > best_score:
            best = item
            best_score = score
    return best


# ── spec / diff ──────────────────────────────────────────────────────────────


def build_spec(request: dict[str, Any], version: str) -> dict[str, Any]:
    headers = {
        str(key).lower(): str(value) for key, value in request.get("headers", {}).items()
    }
    body = request.get("body") if isinstance(request.get("body"), dict) else {}
    auth = headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    raw = request.get("body_raw") or ""
    return {
        "version": version,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": request.get("path") or "",
        "authorization_scheme": scheme,
        "authorization_value": value,
        "user_agent": headers.get("user-agent", ""),
        "headers": sorted(headers),
        "header_count": len(headers),
        "body_keys": sorted(body),
        "tools_count": len(tools),
        "tool_names": [
            str(t.get("function", {}).get("name") or "")
            for t in tools
            if isinstance(t, dict)
        ],
        "max_completion_tokens": body.get("max_completion_tokens"),
        "stream": body.get("stream"),
        "stream_options": body.get("stream_options"),
        "body_bytes": len(raw.encode("utf-8")),
        "body_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "body_raw": raw,
    }


def load_spec() -> dict[str, Any]:
    try:
        return json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def comparable(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        key: spec.get(key)
        for key in (
            "user_agent",
            "authorization_scheme",
            "authorization_value",
            "headers",
            "body_keys",
            "tools_count",
            "tool_names",
            "max_completion_tokens",
            "stream",
            "stream_options",
            "endpoint",
        )
    }


def diff_specs(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    changes: list[str] = []
    left = comparable(old)
    right = comparable(new)
    for key in left:
        if left[key] != right.get(key):
            if key == "headers":
                added = sorted(set(right.get(key) or []) - set(left.get(key) or []))
                removed = sorted(set(left.get(key) or []) - set(right.get(key) or []))
                changes.append(f"headers +{added} -{removed}")
            elif key == "tool_names":
                added = sorted(set(right.get(key) or []) - set(left.get(key) or []))
                removed = sorted(set(left.get(key) or []) - set(right.get(key) or []))
                changes.append(f"tools +{added} -{removed}")
            else:
                changes.append(f"{key}: {left[key]!r} -> {right.get(key)!r}")
    return changes


def apply_spec(spec: dict[str, Any], apply_body: bool) -> None:
    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SPEC_PATH.exists():
        backup = SPEC_PATH.with_suffix(".json.bak")
        backup.write_text(SPEC_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    body_raw = spec.get("body_raw") or ""
    persisted = {key: value for key, value in spec.items() if key != "body_raw"}
    SPEC_PATH.write_text(
        json.dumps(persisted, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if apply_body and body_raw:
        try:
            body = json.loads(body_raw)
        except Exception as exc:
            log(f"body is not JSON, not applied: {type(exc).__name__}: {exc}")
            return
        if PAYLOAD_PATH.exists():
            shutil.copyfile(PAYLOAD_PATH, PAYLOAD_PATH.with_suffix(".json.bak"))
        payload = {
            "_source": f"opencode {spec.get('version')} capture",
            "_captured_at": spec.get("captured_at"),
            "_body_raw": body_raw,
            "_body_sha256": hashlib.sha256(body_raw.encode("utf-8")).hexdigest(),
            "body": body,
        }
        PAYLOAD_PATH.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        log(f"payload updated: {PAYLOAD_PATH}")
    log(f"spec updated: {SPEC_PATH}")


# ── update / scheduling ──────────────────────────────────────────────────────


def update_client(feed: dict[str, Any], install_dir: Path, assume_yes: bool) -> int:
    version = str(feed.get("version") or "")
    files = feed.get("files") if isinstance(feed.get("files"), list) else []
    target = next(
        (
            item
            for item in files
            if isinstance(item, dict)
            and "win-x64" in str(item.get("url") or "")
        ),
        None,
    )
    if not target:
        log("no win-x64 installer in feed")
        return 1
    if not assume_yes:
        log("refusing to install without --yes")
        return 2
    url = str(target.get("url") or "")
    expected = str(target.get("sha512") or "")
    install_dir.mkdir(parents=True, exist_ok=True)
    archive = install_dir / Path(url).name
    log(f"downloading {url}")
    response = requests.get(url, timeout=300, stream=True)
    response.raise_for_status()
    digest = hashlib.sha512()
    with archive.open("wb") as fh:
        for chunk in response.iter_content(1024 * 1024):
            fh.write(chunk)
            digest.update(chunk)
    import base64

    actual = base64.b64encode(digest.digest()).decode()
    if expected and actual != expected:
        log("sha512 mismatch, aborting")
        return 3
    log(f"installer ready: {archive}")
    log("run the installer manually (or use the app updater) to install version " + version)
    return 0


def install_task() -> int:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    script = Path(__file__).resolve()
    command = f'"{pythonw}" "{script}" --watch --capture --diff --apply'
    result = subprocess.run(
        [
            "schtasks",
            "/Create",
            "/F",
            "/SC",
            "DAILY",
            "/ST",
            "03:00",
            "/TN",
            "OpenCodeClientSync",
            "/TR",
            command,
        ],
        capture_output=True,
        text=True,
    )
    print(result.stdout or result.stderr)
    return result.returncode


# ── main ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", default=str(DEFAULT_CLI), help="client CLI path")
    parser.add_argument("--check", action="store_true", help="only compare versions")
    parser.add_argument("--capture", action="store_true", help="capture the free request")
    parser.add_argument("--diff", action="store_true", help="diff against the reference")
    parser.add_argument("--apply", action="store_true", help="write the new spec")
    parser.add_argument(
        "--apply-body", action="store_true", help="also replace official_payload.json"
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--watch", action="store_true", help="loop every 24 h")
    parser.add_argument("--install-task", action="store_true")
    parser.add_argument("--update", action="store_true", help="download the installer")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--install-dir", default=str(Path(tempfile.gettempdir()) / "opencode-update"))
    args = parser.parse_args(argv)

    if args.install_task:
        return install_task()

    cli = Path(args.cli)
    if not cli.exists():
        log(f"client not found: {cli}")
        return 1

    while True:
        try:
            feed = fetch_feed()
            latest = str(feed.get("version") or "")
            current = installed_version(cli)
            log(f"installed={current or '?'} latest={latest or '?'}")
            if latest and current and version_tuple(latest) > version_tuple(current):
                log("a newer client is published")
                if args.update:
                    update_client(feed, Path(args.install_dir), args.yes)
            if args.check and not args.capture:
                if not args.watch:
                    return 0
                time.sleep(24 * 3600)
                continue
            if args.capture or args.diff or args.apply:
                captured = run_capture(cli, args.timeout)
                if not captured:
                    log("no request captured")
                    return 2
                request = pick_main_request(captured)
                if request is None:
                    log("no JSON request captured")
                    return 2
                spec = build_spec(request, current or latest or "unknown")
                log(
                    "captured "
                    f"{spec['body_bytes']} bytes, {spec['tools_count']} tools, "
                    f"UA={spec['user_agent']}"
                )
                previous = load_spec()
                if args.diff or args.apply:
                    changes = diff_specs(previous, spec) if previous else ["no reference"]
                    if changes:
                        for change in changes:
                            log("diff: " + change)
                    else:
                        log("diff: format unchanged")
                    if args.apply and (changes or args.apply_body):
                        apply_spec(spec, args.apply_body)
            if not args.watch:
                return 0
        except Exception as exc:
            log(f"sync error: {type(exc).__name__}: {exc}")
            if not args.watch:
                return 1
        time.sleep(24 * 3600)


if __name__ == "__main__":
    raise SystemExit(main())
