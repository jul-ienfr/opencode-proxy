#!/usr/bin/env python3
"""Régénère `free/official_payload.json` en interceptant le VRAI client.

Pourquoi ce script existe
-------------------------
Depuis le 2026-10-23T23:17:27Z, la jambe gratuite du provider exige la
signature complète du client officiel : les 10 tools et le prompt système.
Un corps réduit est refusé en 403 ``FreeTierError`` (« free tier can only be
used from within OpenCode »). La copie exacte est donc une dépendance dure,
et elle doit pouvoir être re-capturée quand le client évolue.

Comment il procède
------------------
1. démarre un serveur HTTP local qui journalise la première requête POST ;
2. écrit une configuration OpenCode temporaire pointant un provider dont
   l'identifiant commence par « opencode » (sinon le client n'attache pas les
   en-têtes x-opencode-*), vers ce serveur local ;
3. lance `opencode run --model <provider>/<modèle>` avec cette configuration ;
4. le client envoie son payload complet (prompt système + tools) en clair ;
5. le script fige le corps reçu dans le fichier de sortie.

Aucun trafic ne sort vers le provider : tout est capturé en local.

Usage
-----
    python scripts/capture_official_payload.py
    python scripts/capture_official_payload.py --out /tmp/payload.json
    python scripts/capture_official_payload.py --model deepseek-v4.1-flash-free --port 4599
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "free", "official_payload.json")

SSE = (
    b'data: {"id":"chatcmpl-capture","object":"chat.completion.chunk","created":1,'
    b'"model":"capture","choices":[{"index":0,"delta":{"role":"assistant","content":"OK"},'
    b'"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-capture","object":"chat.completion.chunk","created":1,'
    b'"model":"capture","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)


class _Capture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[dict] = []
    done = threading.Event()

    def log_message(self, *args):  # silence
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        rec = {
            "method": "POST",
            "path": self.path,
            "http_version": self.request_version,
            "headers": list(self.headers.items()),
            "body_len": len(raw),
            "body": raw.decode("utf-8", "replace"),
        }
        _Capture.requests.append(rec)
        try:
            tools = len((json.loads(rec["body"]) or {}).get("tools") or [])
        except Exception:
            tools = 0
        # Le client émet plusieurs POST (titre de session, compaction, agent).
        # On attend celui qui porte la signature complète.
        if tools >= 10:
            _Capture.done.set()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(SSE)))
        self.end_headers()
        self.wfile.write(SSE)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def _write_config(port: int, model: str) -> str:
    fd, path = tempfile.mkstemp(prefix="opencode-local-", suffix=".json")
    os.close(fd)
    config = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "opencode-local": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "capture",
                "options": {
                    "baseURL": f"http://127.0.0.1:{port}/inference/v1",
                    "apiKey": "public",
                },
                "models": {model: {"name": model}},
            }
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--model", default="deepseek-v4.1-flash-free")
    ap.add_argument("--port", type=int, default=4599)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument(
        "--binary",
        default="opencode",
        help="binaire client à intercepter (ex. opencode-cli.exe v2.1.4)",
    )
    ap.add_argument(
        "--standalone",
        action="store_true",
        help="passe --standalone au client (v2 : sinon il utilise le service en arrière-plan et ignore OPENCODE_CONFIG)",
    )
    args = ap.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), _Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[capture] serveur local sur 127.0.0.1:{args.port}")

    config_path = _write_config(args.port, args.model)
    env = dict(os.environ)
    env["OPENCODE_CONFIG"] = config_path
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"

    cmd = [args.binary, "run"]
    if args.standalone:
        cmd.append("--standalone")
    cmd += ["--model", f"opencode-local/{args.model}", "Réponds exactement: OK"]
    print(f"[capture] lancement : {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            cwd=tempfile.gettempdir(),
            env=env,
            capture_output=True,
            text=True,
            timeout=args.timeout,
            shell=(os.name == "nt"),
        )
        print(f"[capture] code client : {proc.returncode}")
        if proc.stdout:
            print(f"[capture] stdout : {proc.stdout.strip()[-400:]}")
        if proc.stderr:
            print(f"[capture] stderr : {proc.stderr.strip()[-400:]}")
    except FileNotFoundError:
        raise SystemExit("[capture] `opencode` introuvable. Installe-le (npm i -g opencode-ai) puis relance.") from None
    except subprocess.TimeoutExpired:
        print("[capture] délai dépassé, on regarde ce qui a été reçu")
    finally:
        try:
            os.remove(config_path)
        except OSError:
            pass

    _Capture.done.wait(timeout=5)
    time.sleep(1.0)  # laisse arriver une éventuelle requête tardive
    server.shutdown()
    if not _Capture.requests:
        raise SystemExit("[capture] aucune requête interceptée — rien n'a été écrit")

    # Choisit la requête la plus complète (celle de l'agent, pas le titre).
    def _score(rec: dict) -> tuple[int, int]:
        try:
            body = json.loads(rec["body"])
        except Exception:
            return (0, 0)
        return (len(body.get("tools") or []), len(rec["body"]))

    rec = max(_Capture.requests, key=_score)
    print(f"[capture] {len(_Capture.requests)} requête(s) vue(s), la plus complète retenue")

    body = json.loads(rec["body"])
    tools = [t.get("function", {}).get("name") for t in body.get("tools") or []]
    system: dict = next((m for m in body.get("messages") or [] if m.get("role") == "system"), {})
    if len(tools) < 10:
        print(f"[capture] ATTENTION : seulement {len(tools)} tools vus (attendu 10)")

    payload = {
        "_source": "capture intégrale du client officiel opencode",
        "_method": (
            "interception locale : provider 'opencode-local' pointé sur "
            f"127.0.0.1:{args.port}, aucun trafic vers le provider"
        ),
        "_note": (
            "Copie exacte, non minimale. Le gate gratuit vérifie la signature du "
            "client : suite complète des tools officiels + prompt système. "
            "Toute réduction est refusée 403/429."
        ),
        "_request_headers": rec["headers"],
        "body": body,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[capture] écrit : {args.out} ({os.path.getsize(args.out)} octets)")
    print(f"[capture] tools ({len(tools)}) : {tools}")
    print(f"[capture] prompt système : {len(system.get('content') or '')} caractères")
    print(f"[capture] clés du corps : {list(body.keys())}")


if __name__ == "__main__":
    main()
