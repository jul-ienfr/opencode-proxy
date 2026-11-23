#!/usr/bin/env python3
"""Benchmarks proxy — 023 T068.

Mesure, contre un proxy **deja lance** (``python opencode.py``) et un upstream
direct optionnel :

* le surcout p50 de la mediation proxy sur des requetes non-stream
  (latence proxy - latence directe) ;
* le TTFB p50/p95 des flux SSE (premier evenement non vide) ;
* le RSS du process proxy si ``psutil`` est disponible (sinon ``null``) ;
* ``--streams`` flux concurrents (100 par defaut) et leur taux de succes.

Aucun upstream n'est demarre ici : sans proxy joignable le banc sort en erreur.

Usage :
  python scripts/bench_proxy.py
  python scripts/bench_proxy.py --base-url http://localhost:4000 --streams 100
  python scripts/bench_proxy.py --base-url http://localhost:4000 \\
      --direct-url http://localhost:4000 --requests 50 --concurrency 20
  python scripts/bench_proxy.py --json > logs/bench_proxy.json

Sortie : tableau lisible puis JSON (``--json`` = JSON seul). Les mesures sont
reelles ; les tests ne couvrent que les helpers purs (aucun reseau).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

DEFAULT_BASE_URL = "http://localhost:4000"
DEFAULT_PATH = "/v1/messages"
DEFAULT_MODEL = os.getenv("BENCH_MODEL", "deepseek-v4.1-flash")
DEFAULT_REQUESTS = 30
DEFAULT_CONCURRENCY = 10
DEFAULT_STREAMS = 100
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_TOKENS = 1


# ── helpers purs (testes sans reseau) ────────────────────────────────────


def percentile(values: list[float], pct: float) -> float:
    """Percentile nearest-rank (0..100). Liste vide -> 0.0."""
    if not isinstance(pct, (int, float)) or not 0.0 <= float(pct) <= 100.0:
        raise ValueError(f"pct hors [0, 100] : {pct!r}")
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    rank = math.ceil((float(pct) / 100.0) * len(ordered))
    index = min(len(ordered) - 1, max(0, rank - 1))
    return ordered[index]


def aggregate_latencies(values: list[float]) -> dict[str, Any]:
    """Agrege des latences en ms (p50/p90/p95/p99, min, moyenne, max).

    Liste vide -> ``count=0`` et tous les champs ``None`` (jamais un faux 0).
    """
    samples = [float(v) for v in values]
    if not samples:
        return {
            "count": 0,
            "min_ms": None,
            "mean_ms": None,
            "p50_ms": None,
            "p90_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
        }
    return {
        "count": len(samples),
        "min_ms": round(min(samples), 3),
        "mean_ms": round(statistics.fmean(samples), 3),
        "p50_ms": round(percentile(samples, 50), 3),
        "p90_ms": round(percentile(samples, 90), 3),
        "p95_ms": round(percentile(samples, 95), 3),
        "p99_ms": round(percentile(samples, 99), 3),
        "max_ms": round(max(samples), 3),
    }


def proxy_overhead(proxy_values: list[float], direct_values: list[float]) -> dict[str, Any]:
    """Surcout proxy = latence proxy - latence directe (p50 et p95).

    Sans mesure directe (ou proxy vide), le surcout vaut ``None`` : on ne
    compare pas une mesure a une absence.
    """
    proxy = [float(v) for v in proxy_values]
    direct = [float(v) for v in direct_values]
    result: dict[str, Any] = {
        "p50_proxy_ms": None,
        "p50_direct_ms": None,
        "p95_proxy_ms": None,
        "p95_direct_ms": None,
        "overhead_p50_ms": None,
        "overhead_p95_ms": None,
    }
    if proxy:
        result["p50_proxy_ms"] = round(percentile(proxy, 50), 3)
        result["p95_proxy_ms"] = round(percentile(proxy, 95), 3)
    if direct:
        result["p50_direct_ms"] = round(percentile(direct, 50), 3)
        result["p95_direct_ms"] = round(percentile(direct, 95), 3)
    if proxy and direct:
        result["overhead_p50_ms"] = round(
            result["p50_proxy_ms"] - result["p50_direct_ms"], 3
        )
        result["overhead_p95_ms"] = round(
            result["p95_proxy_ms"] - result["p95_direct_ms"], 3
        )
    return result


def success_rate(successes: int, total: int) -> float:
    """Taux de succes dans [0, 1] ; ``total=0`` -> 0.0."""
    if successes < 0 or total < 0:
        raise ValueError("successes/total negatifs")
    if successes > total:
        raise ValueError(f"successes ({successes}) > total ({total})")
    if total == 0:
        return 0.0
    return round(successes / total, 4)


def validate_url(url: str) -> str:
    """Valide une origine http(s) et la retourne sans espaces de bord."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL vide")
    value = url.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"URL non http(s) : {value!r}")
    if not parts.netloc:
        raise ValueError(f"URL sans hote : {value!r}")
    return value


def endpoint_url(base: str, path: str) -> str:
    """Concatene une origine et un chemin (idempotent si le chemin est deja la)."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"chemin invalide (doit commencer par '/') : {path!r}")
    origin = base.rstrip("/")
    if origin.endswith(path):
        return origin
    return origin + path


def url_port(url: str) -> int | None:
    """Port explicite ou implicite (80/443) d'une URL ; ``None`` si illisible."""
    try:
        parts = urlsplit(url)
        if parts.port:
            return int(parts.port)
    except (ValueError, AttributeError):
        return None
    if parts.scheme == "http":
        return 80
    if parts.scheme == "https":
        return 443
    return None


def build_payload(
    model: str, stream: bool, max_tokens: int = DEFAULT_MAX_TOKENS
) -> dict[str, Any]:
    """Payload minimal Anthropic (meme forme pour le proxy et l'upstream direct)."""
    return {
        "model": model,
        "max_tokens": max(1, int(max_tokens)),
        "stream": bool(stream),
        "messages": [{"role": "user", "content": "ping"}],
    }


def build_headers(api_key: str, stream: bool = False) -> dict[str, str]:
    """Entetes acceptes par le proxy Anthropic et par l'upstream direct."""
    return {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": api_key,
        "authorization": f"Bearer {api_key}",
        "accept": "text/event-stream" if stream else "application/json",
    }


def format_table(result: dict[str, Any]) -> str:
    """Rend le resultat en tableau lisible (valeurs absentes -> ``n/a``)."""

    def ms(value: Any) -> str:
        return "n/a" if value is None else f"{value:.1f} ms"

    def pct(value: Any) -> str:
        return "n/a" if value is None else f"{value * 100:.1f}%"

    non_stream = result.get("non_stream") or {}
    proxy = non_stream.get("proxy") or {}
    direct = non_stream.get("direct") or {}
    overhead = non_stream.get("overhead") or {}
    sse = result.get("sse") or {}
    streams = result.get("streams") or {}
    rss = result.get("rss") or {}
    proxy_lat = proxy.get("latency") or {}
    direct_lat = direct.get("latency") or {}
    ttfb = sse.get("ttfb") or {}
    burst_ttfb = streams.get("ttfb") or {}
    lines = [
        f"Proxy benchmark — {result.get('generated_at', 'n/a')}",
        f"  base_url    : {result.get('base_url', 'n/a')}",
        f"  direct_url  : {result.get('direct_url') or 'n/a'}",
        f"  path/model  : {result.get('path', 'n/a')} / {result.get('model', 'n/a')}",
        "",
        (
            "Non-stream "
            f"(n={proxy.get('attempted', 0)}, concurrency="
            f"{(result.get('config') or {}).get('concurrency', 'n/a')})"
        ),
        (
            f"  proxy  p50={ms(proxy_lat.get('p50_ms'))} "
            f"p95={ms(proxy_lat.get('p95_ms'))} "
            f"success={pct(proxy.get('success_rate'))}"
        ),
        (
            f"  direct p50={ms(direct_lat.get('p50_ms'))} "
            f"p95={ms(direct_lat.get('p95_ms'))} "
            f"success={pct(direct.get('success_rate'))}"
        ),
        (
            f"  surcout proxy p50={ms(overhead.get('overhead_p50_ms'))} "
            f"p95={ms(overhead.get('overhead_p95_ms'))}"
        ),
        "",
        f"SSE TTFB sequentiel (n={ttfb.get('count', 0)})",
        f"  p50={ms(ttfb.get('p50_ms'))} p95={ms(ttfb.get('p95_ms'))}",
        "",
        (
            f"Flux concurrents : {streams.get('success', 0)}/{streams.get('total', 0)} "
            f"ok ({pct(streams.get('success_rate'))})"
        ),
        (
            f"  TTFB rafale p50={ms(burst_ttfb.get('p50_ms'))} "
            f"p95={ms(burst_ttfb.get('p95_ms'))}"
        ),
        (
            f"RSS : {ms(rss.get('peak_mb')) if rss.get('available') else 'null'}"
            f" (pid {rss.get('pid') or 'n/a'})"
        ),
    ]
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmarks d'un proxy deja lance (023 T068)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="origine du proxy")
    parser.add_argument(
        "--direct-url",
        default=None,
        help="origine upstream directe optionnelle (surcout proxy mesure contre elle)",
    )
    parser.add_argument("--path", default=DEFAULT_PATH, help="chemin mesure")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="modele envoye")
    parser.add_argument(
        "--api-key",
        default=os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENCODE_API_KEY") or "public",
        help="cle envoyee au proxy/upstream (jamais ecrite dans la sortie)",
    )
    parser.add_argument(
        "--requests", type=int, default=DEFAULT_REQUESTS, help="requetes non-stream (et TTFB sequentiels)"
    )
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="parallelisme non-stream")
    parser.add_argument("--streams", type=int, default=DEFAULT_STREAMS, help="flux SSE concurrents")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="timeout par requete (s)")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="max_tokens des payloads")
    parser.add_argument(
        "--rss-pid",
        type=int,
        default=None,
        help="pid du proxy pour le RSS (defaut : decouverte via le port / opencode.py)",
    )
    parser.add_argument("--json", action="store_true", help="sortie JSON seule (machine)")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse et valide les arguments (erreurs -> SystemExit code 2)."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.requests < 1:
        parser.error("--requests doit etre >= 1")
    if args.concurrency < 1:
        parser.error("--concurrency doit etre >= 1")
    if args.streams < 1:
        parser.error("--streams doit etre >= 1")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout doit etre un nombre fini > 0")
    if args.max_tokens < 1:
        parser.error("--max-tokens doit etre >= 1")
    if args.rss_pid is not None and args.rss_pid <= 0:
        parser.error("--rss-pid doit etre > 0")
    try:
        args.base_url = validate_url(args.base_url)
        if args.direct_url:
            args.direct_url = validate_url(args.direct_url)
        endpoint_url(args.base_url, args.path)
        if args.direct_url:
            endpoint_url(args.direct_url, args.path)
    except ValueError as exc:
        parser.error(str(exc))
    return args


# ── mesures reseau (proxy lance requis) ─────────────────────────────────


async def _request_once(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], payload: dict[str, Any]
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = await client.post(url, headers=headers, json=payload)
        return {
            "latency_ms": (time.perf_counter() - started) * 1000.0,
            "status": response.status_code,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 — le banc enregistre l'echec, ne masque rien
        return {
            "latency_ms": None,
            "status": None,
            "error": f"{type(exc).__name__}: {exc}"[:200],
        }


async def _stream_once(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], payload: dict[str, Any]
) -> dict[str, Any]:
    """Un flux SSE : TTFB = premier evenement non vide, puis drain complet."""
    started = time.perf_counter()
    ttfb_ms: float | None = None
    try:
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            status = response.status_code
            async for line in response.aiter_lines():
                if line.strip():
                    if ttfb_ms is None:
                        ttfb_ms = (time.perf_counter() - started) * 1000.0
            return {
                "status": status,
                "ttfb_ms": ttfb_ms,
                "total_ms": (time.perf_counter() - started) * 1000.0,
                "error": None,
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": None,
            "ttfb_ms": None,
            "total_ms": None,
            "error": f"{type(exc).__name__}: {exc}"[:200],
        }


def _phase_summary(results: list[dict[str, Any]], attempted: int) -> dict[str, Any]:
    successes = [r for r in results if r["status"] is not None and 200 <= r["status"] < 300]
    responded = [r for r in results if r["status"] is not None]
    latencies = [r["latency_ms"] for r in successes if r["latency_ms"] is not None]
    status_counts: dict[str, int] = {}
    for item in responded:
        key = str(item["status"])
        status_counts[key] = status_counts.get(key, 0) + 1
    errors = [r["error"] for r in results if r["error"]]
    return {
        "attempted": attempted,
        "responses": len(responded),
        "success": len(successes),
        "success_rate": success_rate(len(successes), attempted),
        "errors": len(errors),
        "error_samples": errors[:5],
        "status_counts": status_counts,
        "latency": aggregate_latencies(latencies),
        # Echantillons bruts : permettent de recalculer le surcout sans
        # comparer des agregats differents (proxy_overhead).
        "latencies_ms": [round(value, 3) for value in latencies],
    }


async def _run_non_stream(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    requests: int,
    concurrency: int,
) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(concurrency)

    async def one() -> dict[str, Any]:
        async with semaphore:
            return await _request_once(client, url, headers, payload)

    results = await asyncio.gather(*(one() for _ in range(requests)))
    return _phase_summary(list(results), requests)


async def _run_ttfb_sequential(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    samples: int,
) -> dict[str, Any]:
    results = [await _stream_once(client, url, headers, payload) for _ in range(samples)]
    successes = [r for r in results if r["status"] == 200 and r["error"] is None]
    return {
        "attempted": samples,
        "success": len(successes),
        "success_rate": success_rate(len(successes), samples),
        "ttfb": aggregate_latencies(
            [r["ttfb_ms"] for r in successes if r["ttfb_ms"] is not None]
        ),
        "total": aggregate_latencies(
            [r["total_ms"] for r in successes if r["total_ms"] is not None]
        ),
        "errors": [r["error"] for r in results if r["error"]][:5],
    }


async def _run_stream_burst(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    streams: int,
) -> dict[str, Any]:
    results = await asyncio.gather(
        *(_stream_once(client, url, headers, payload) for _ in range(streams))
    )
    successes = [r for r in results if r["status"] == 200 and r["error"] is None]
    return {
        "total": streams,
        "success": len(successes),
        "success_rate": success_rate(len(successes), streams),
        "errors": len([r for r in results if r["error"]]),
        "error_samples": [r["error"] for r in results if r["error"]][:5],
        "ttfb": aggregate_latencies(
            [r["ttfb_ms"] for r in successes if r["ttfb_ms"] is not None]
        ),
        "total_latency": aggregate_latencies(
            [r["total_ms"] for r in successes if r["total_ms"] is not None]
        ),
    }


# ── RSS (psutil optionnel) ───────────────────────────────────────────────


def import_psutil() -> Any | None:
    """Retourne le module psutil, ou ``None`` s'il n'est pas installe."""
    try:
        import psutil  # noqa: PLC0415 — dependance optionnelle

        return psutil
    except Exception:  # noqa: BLE001
        return None


def find_proxy_pid(port: int | None, psutil_mod: Any) -> int | None:
    """Trouve le pid qui ecoute ``port``, sinon un process ``opencode.py``."""
    if psutil_mod is None:
        return None
    try:
        if port is not None:
            for proc in psutil_mod.process_iter(["pid"]):
                try:
                    for conn in proc.net_connections(kind="inet") or []:
                        laddr = getattr(conn, "laddr", None)
                        if (
                            str(getattr(conn, "status", "")).upper() == "LISTEN"
                            and laddr is not None
                            and int(getattr(laddr, "port", -1)) == int(port)
                        ):
                            return int(proc.pid)
                except Exception:  # noqa: BLE001 — process ephemere / acces refuse
                    continue
    except Exception:  # noqa: BLE001
        pass
    try:
        for proc in psutil_mod.process_iter(["pid", "cmdline"]):
            cmdline = proc.info.get("cmdline") or []
            if any("opencode.py" in str(part) for part in cmdline):
                return int(proc.pid)
    except Exception:  # noqa: BLE001
        return None
    return None


def read_rss_mb(psutil_mod: Any, pid: int | None) -> float | None:
    """RSS du process en Mo, ou ``None`` si indisponible."""
    if psutil_mod is None or not pid:
        return None
    try:
        return round(psutil_mod.Process(pid).memory_info().rss / (1024 * 1024), 2)
    except Exception:  # noqa: BLE001
        return None


async def _rss_sampler(
    psutil_mod: Any, pid: int, samples: list[float], interval: float = 0.2
) -> None:
    while True:
        value = await asyncio.to_thread(read_rss_mb, psutil_mod, pid)
        if value is not None:
            samples.append(value)
        await asyncio.sleep(interval)


async def _measure_phase_with_rss(
    coro_factory: Any, psutil_mod: Any, pid: int | None
) -> tuple[Any, dict[str, Any]]:
    """Execute une phase et echantillonne le RSS du proxy autour d'elle."""
    start = await asyncio.to_thread(read_rss_mb, psutil_mod, pid)
    samples: list[float] = []
    sampler: asyncio.Task | None = None
    if psutil_mod is not None and pid:
        sampler = asyncio.create_task(_rss_sampler(psutil_mod, pid, samples))
    try:
        payload = await coro_factory()
    finally:
        if sampler is not None:
            sampler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler
    end = await asyncio.to_thread(read_rss_mb, psutil_mod, pid)
    observed = [v for v in (start, *samples, end) if v is not None]
    return payload, {
        "available": psutil_mod is not None and bool(observed),
        "pid": pid,
        "start_mb": start,
        "peak_mb": round(max(observed), 2) if observed else None,
        "end_mb": end,
    }


# ── orchestration ────────────────────────────────────────────────────────


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    proxy_url = endpoint_url(args.base_url, args.path)
    direct_url = endpoint_url(args.direct_url, args.path) if args.direct_url else None
    headers_plain = build_headers(args.api_key, stream=False)
    headers_stream = build_headers(args.api_key, stream=True)
    payload_plain = build_payload(args.model, False, args.max_tokens)
    payload_stream = build_payload(args.model, True, args.max_tokens)
    max_connections = max(args.concurrency, args.streams) + 10
    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_connections,
    )
    timeout = httpx.Timeout(args.timeout)

    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        proxy_phase = await _run_non_stream(
            client, proxy_url, headers_plain, payload_plain, args.requests, args.concurrency
        )
        direct_phase = None
        if direct_url:
            direct_phase = await _run_non_stream(
                client,
                direct_url,
                headers_plain,
                payload_plain,
                args.requests,
                args.concurrency,
            )

        sse_phase = await _run_ttfb_sequential(
            client, proxy_url, headers_stream, payload_stream, args.requests
        )

        psutil_mod = import_psutil()
        rss_pid = args.rss_pid
        if rss_pid is None and psutil_mod is not None:
            rss_pid = find_proxy_pid(url_port(args.base_url), psutil_mod)
        streams_phase, rss = await _measure_phase_with_rss(
            lambda: _run_stream_burst(
                client, proxy_url, headers_stream, payload_stream, args.streams
            ),
            psutil_mod,
            rss_pid,
        )

    overhead = proxy_overhead(
        proxy_phase["latencies_ms"],
        (direct_phase or {}).get("latencies_ms", []),
    )

    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "base_url": args.base_url,
        "direct_url": args.direct_url,
        "path": args.path,
        "model": args.model,
        "config": {
            "requests": args.requests,
            "concurrency": args.concurrency,
            "streams": args.streams,
            "timeout_s": args.timeout,
            "max_tokens": args.max_tokens,
        },
        "proxy_reachable": proxy_phase["responses"] > 0,
        "non_stream": {
            "proxy": proxy_phase,
            "direct": direct_phase,
            "overhead": overhead,
        },
        "sse": sse_phase,
        "streams": streams_phase,
        "rss": rss,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(run_benchmark(args))
    except KeyboardInterrupt:
        return 130
    if not args.json:
        print(format_table(result))
        print()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result["proxy_reachable"]:
        return 0
    print(
        f"ERREUR: aucun HTTP recu de {args.base_url} — le proxy est-il lance ?",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
