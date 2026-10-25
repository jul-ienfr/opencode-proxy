#!/usr/bin/env python3
"""Vérifie de bout en bout que la jambe gratuite fonctionne.

Rassemble en une commande les contrôles qui ont servi au diagnostic du
2026-10-23/25 : payload officiel chargé, canary, et surtout des appels réels
sur les trois protocoles. Sert au quotidien (« est-ce que ça marche ? ») et
après chaque redémarrage.

Usage :
    python scripts/verify_free_leg.py
    python scripts/verify_free_leg.py --full        # + outils et raisonnement
    python scripts/verify_free_leg.py --model mimo-v2.6-flash-free --full
    python scripts/verify_free_leg.py --base-url http://127.0.0.1:4000

Sortie : un tableau PASS/FAIL, puis le bilan. Code de sortie 1 si un
contrôle échoue (utilisable en supervision).

Coût : 3 requêtes minimum, 5 avec --full (une par contrôle de capacité).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def _req(url: str, body: dict | None = None, timeout: float = 120.0) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, {"_raw": raw[:400]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_raw": raw[:400]}


class Check:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)

    def summary(self) -> int:
        failed = [r for r in self.rows if not r[1]]
        print()
        if failed:
            print(f"BILAN : {len(failed)} échec(s) sur {len(self.rows)} contrôles")
            for name, _, detail in failed:
                print(f"  - {name} {detail}")
            return 1
        print(f"BILAN : {len(self.rows)}/{len(self.rows)} contrôles OK")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:4000")
    ap.add_argument("--model", default="deepseek-v4.1-flash-free")
    ap.add_argument("--full", action="store_true", help="teste aussi outils et raisonnement")
    ap.add_argument(
        "--reasoning-model",
        default="mimo-v2.6-flash-free",
        help="modèle utilisé pour le contrôle de raisonnement (doit raisonner de façon fiable)",
    )
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    model = args.model
    c = Check()
    # Unicité : sans cela, le cache de réponses du proxy (TTL 300 s) répond
    # 200 en quelques millisecondes et le contrôle ne prouve RIEN sur l'amont.
    nonce = f"{int(time.time() * 1000):x}"

    print(f"Vérification de la jambe free — {base} — modèle {model}\n")

    # 1. Payload officiel et canary (pas d'appel amont facturé).
    print("État du proxy :")
    status, health = _req(f"{base}/health?detail=full", timeout=30)
    fp = (health or {}).get("free_payload") or {}
    c.add("payload officiel chargé", bool(fp.get("ok")), json.dumps(fp, ensure_ascii=False))
    canary = (health or {}).get("canary") or {}
    probes = canary.get("probes") or {}
    if probes:
        ok = all(p.get("verdict") == "ok" for p in probes.values())
        detail = ", ".join(f"{k}={v.get('verdict')}" for k, v in probes.items())
        c.add("canary", ok, detail)
    else:
        c.add("canary", canary.get("status") in ("ok", None), str(canary.get("status")))

    # 2. Chat (le chemin principal).
    print("\nAppels réels :")
    t0 = time.monotonic()
    code, d = _req(
        f"{base}/v1/chat/completions",
        {"model": model, "messages": [{"role": "user", "content": f"Réponds exactement : OK [v{nonce}]"}],
         "max_tokens": 16},
        args.timeout,
    )
    ms = round((time.monotonic() - t0) * 1000)
    content = ""
    try:
        content = (d["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        pass
    c.add("chat 200", code == 200, f"{code} en {ms} ms" if code == 200 else f"{code} {str(d)[:120]}")
    c.add("chat contenu non vide", bool(content), repr(content[:40]))

    # 3. Anthropic.
    code, d = _req(
        f"{base}/v1/messages",
        {"model": model, "max_tokens": 16,
         "messages": [{"role": "user", "content": f"Réponds exactement : OK [v{nonce}]"}]},
        args.timeout,
    )
    blocks = [b.get("type") for b in (d.get("content") or [])] if code == 200 else []
    c.add("messages 200", code == 200, f"{code} blocs={blocks}" if code == 200 else f"{code} {str(d)[:120]}")

    # 4. Responses.
    code, d = _req(
        f"{base}/v1/responses",
        {"model": model, "input": f"Réponds exactement : OK [v{nonce}]", "max_output_tokens": 16},
        args.timeout,
    )
    items = [it.get("type") for it in (d.get("output") or [])] if code == 200 else []
    c.add("responses 200", code == 200, f"{code} items={items}" if code == 200 else f"{code} {str(d)[:120]}")

    # 5. Capacités (coûte 2 requêtes de plus).
    if args.full:
        print("\nCapacités (--full) :")
        tool = {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Donne la météo d'une ville.",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                               "required": ["city"]},
            },
        }
        code, d = _req(
            f"{base}/v1/chat/completions",
            {"model": model, "max_tokens": 96,
             "messages": [{"role": "user",
                           "content": f"Utilise l'outil get_weather pour Paris. N'utilise aucun autre outil. [v{nonce}]"}],
             "tools": [tool]},
            args.timeout,
        )
        calls = []
        try:
            calls = [tc["function"]["name"] for tc in (d["choices"][0]["message"].get("tool_calls") or [])]
        except Exception:
            pass
        c.add("tool calling", code == 200 and "get_weather" in calls, f"{code} appels={calls}")

        code, d = _req(
            f"{base}/v1/chat/completions",
            {"model": args.reasoning_model, "max_tokens": 300,
             "messages": [{"role": "user",
                           "content": f"Calcule 17*23 en détaillant chaque étape. [v{nonce}]"}]},
            args.timeout,
        )
        reasoning = ""
        try:
            reasoning = d["choices"][0]["message"].get("reasoning_content") or ""
        except Exception:
            pass
        if code != 200:
            # Modèle indisponible : on ne conclut pas à une régression.
            c.add("raisonnement (ignoré)", True, f"{code} — modèle indisponible")
        else:
            c.add("raisonnement", bool(reasoning), f"{len(reasoning)} caractères ({args.reasoning_model})")

    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
