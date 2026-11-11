#!/usr/bin/env python3
"""Preuves reproductibles - veille outillage d'ingenierie inverse (REA).

Regenere les chiffres cites dans
`docs/REA_STACK_COMPARATIF_2026-11-09.md` :

- metadonnees GitHub des depots du lot REA et de leurs alternatives ;
- activite du compte `morluto` (depots, dates de creation, etoiles) ;
- classement DecBench (instantane + taux par decompilateur).

Aucune cle d'API n'est requise. Le quota GitHub non authentifie est de 60
requetes/heure : une execution complete en consomme ~70. Chaque reponse est
mise en cache dans `<sortie>/cache/`, donc une seconde execution ne consomme
que les manquants. `--wait` attend le reset du quota au lieu d'echouer.

Exemples :
    python scripts/re_stack_evidence.py --wait
    python scripts/re_stack_evidence.py --refresh
    python scripts/re_stack_evidence.py --output docs/evidence/rea-2026-11-09
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "docs" / "evidence" / "rea-2026-11-09"
GH = "https://api.github.com"
HEADERS = {"Accept": "application/json", "User-Agent": "opencode-re-stack-evidence/1.0"}

REPOS = [
    # lot fourni par l'operateur
    "morluto/rea",
    "NationalSecurityAgency/ghidra",
    "LaurieWired/GhidraMCP",
    "mrexodia/ida-pro-mcp",
    "skylot/jadx",
    "iBotPeaches/Apktool",
    "frida/frida",
    "icsharpcode/ILSpy",
    "radareorg/radare2",
    "x64dbg/x64dbg",
    "WerWolv/ImHex",
    "angr/angr",
    "mitmproxy/mitmproxy",
    "dnSpyEx/dnSpy",
    "rizinorg/rizin",
    "EpicGames/raddebugger",
    "zhuyansen/awesome-ai-reverse-engineering",
    "morluto/awesome-reverse-engineering",
    # ajout 2026-11-09 : web/API -> client type (homonyme de REA, projet distinct)
    "kalil0321/reverse-api-engineer",
    "microsoft/playwright-mcp",
    "vercel-labs/agent-browser",
    # chaine web/API : capture -> spec -> client -> oracle
    "alufers/mitmproxy2swagger",
    "curlconverter/curlconverter",
    "OpenAPITools/openapi-generator",
    "schemathesis/schemathesis",
    "ChromeDevTools/chrome-devtools-mcp",
    "browser-use/browser-use",
    "browserbase/stagehand",
    "Skyvern-AI/skyvern",
    "mendableai/firecrawl",
    "unclecode/crawl4ai",
    "albertan017/LLM4Decompile",
    # alternatives et complements
    "Noelo-Lab/kuna",
    "Vector35/binaryninja-api",
    "rizinorg/cutter",
    "radareorg/radare2-mcp",
    "radareorg/r2ghidra",
    "fosdickio/binary_ninja_mcp",
    "jtang613/GhidrAssistMCP",
    "cyberkaida/reverse-engineering-assistant",
    "13bm/GhidraMCP",
    "LSPosed/LSPosed",
    "sensepost/objection",
    "pxb1988/dex2jar",
    "JonathanSalwan/Triton",
    "cea-sec/miasm",
    "eurecom-s3/symcc",
    "unicorn-engine/unicorn",
    "qilingframework/qiling",
    "klee/klee",
    "AFLplusplus/AFLplusplus",
    "revng/revng",
    "avast/retdec",
    "0xd4d/de4dot",
    "Washi1337/AsmResolver",
    "capstone-engine/capstone",
    "icedland/iced",
    "rr-debugger/rr",
    "zaproxy/zaproxy",
    "HypothesisWorks/hypothesis",
    "lief-project/LIEF",
    "mandiant/flare-floss",
    "mandiant/capa",
    "mandiant/speakeasy",
    "horsicq/Detect-It-Easy",
    "pwndbg/pwndbg",
    "hugsy/gef",
    "HyperDbg/hyperdbg",
    "osandov/drgn",
    "DynamoRIO/dynamorio",
    "quarkslab/QBDI",
    "trailofbits/manticore",
    "google/bindiff",
    "joxeankoret/diaphora",
    "VirusTotal/yara",
    "m4b/goblin",
    "google/atheris",
    "nowsecure/r2frida",
    "androguard/androguard",
    "MobSF/Mobile-Security-Framework-MobSF",
    "quark-engine/quark-engine",
]

# Cles `combos` DecBench utilisees dans le comparatif.
COMBOS = ["unoptimized|1", "optimized|0", "sample-set|0"]
PACE_S = 1.2  # sous le seuil de rate-limit secondaire de l'API


def _get(url: str, *, timeout: float = 30.0):
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8", "replace")), None
    except urllib.error.HTTPError as exc:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        detail = f"HTTP {exc.code}"
        if exc.code == 403 and "rate limit" in (exc.reason or "").lower():
            detail = "rate-limit"
        return None, (detail, retry_after)
    except (OSError, ValueError) as exc:
        return None, (f"{type(exc).__name__}: {exc}", None)


def quota() -> dict:
    data, _ = _get(f"{GH}/rate_limit")
    return (data or {}).get("resources", {}).get("core", {}) or {}


def wait_for_reset() -> None:
    info = quota()
    reset = info.get("reset")
    if not reset:
        time.sleep(60)
        return
    wait = max(0, int(reset - time.time())) + 5
    if wait > 0:
        print(f"quota epuise - attente de {wait}s (reset {datetime.fromtimestamp(reset, UTC).isoformat()})")
        time.sleep(min(wait, 3700))


def cached_fetch(url: str, cache_file: Path, *, refresh: bool):
    if cache_file.exists() and not refresh:
        try:
            return json.loads(cache_file.read_text(encoding="utf-8")), None
        except ValueError:
            pass
    data, error = _get(url)
    if error is None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data, error


def fetch_repos(out: Path, *, refresh: bool, wait: bool) -> list[dict]:
    cache = out / "cache"
    rows = []
    for name in REPOS:
        key = name.replace("/", "__")
        data, error = cached_fetch(f"{GH}/repos/{name}", cache / f"repo__{key}.json", refresh=refresh)
        if error and wait and error[0] == "rate-limit":
            wait_for_reset()
            data, error = cached_fetch(f"{GH}/repos/{name}", cache / f"repo__{key}.json", refresh=refresh)
        if error:
            rows.append({"repo": name, "error": error[0]})
            continue
        rows.append(
            {
                "repo": name,
                "stars": data.get("stargazers_count"),
                "forks": data.get("forks_count"),
                "open_issues": data.get("open_issues_count"),
                "created": (data.get("created_at") or "")[:10],
                "pushed": (data.get("pushed_at") or "")[:10],
                "archived": data.get("archived"),
                "license": (data.get("license") or {}).get("spdx_id") or "",
                "language": data.get("language") or "",
                "description": (data.get("description") or "")[:200],
            }
        )
        time.sleep(PACE_S)
    return rows


def fetch_morluto(out: Path, *, refresh: bool) -> dict:
    data, error = cached_fetch(
        f"{GH}/users/morluto/repos?per_page=100&sort=created",
        out / "cache" / "morluto__repos.json",
        refresh=refresh,
    )
    if error:
        return {"error": error[0]}
    created: dict[str, int] = {}
    for repo in data:
        day = (repo.get("created_at") or "")[:10]
        created[day] = created.get(day, 0) + 1
    stars = sorted((r.get("stargazers_count") or 0 for r in data), reverse=True)
    return {
        "count": len(data),
        "created_by_day": dict(sorted(created.items())),
        "repos_created_on_top_day": max(created.values()) if created else 0,
        "top_day": max(created, key=created.get) if created else "",
        "stars_total": sum(stars),
        "stars_top10": stars[:10],
        "repos_under_10_stars": sum(1 for s in stars if s < 10),
        "repos": [
            {
                "name": r.get("name"),
                "stars": r.get("stargazers_count"),
                "created": (r.get("created_at") or "")[:10],
                "pushed": (r.get("pushed_at") or "")[:10],
                "language": r.get("language"),
            }
            for r in data
        ],
    }


def fetch_decbench(out: Path, *, refresh: bool) -> dict:
    cache = out / "cache"
    index, error = cached_fetch("https://decbench.com/data/snapshots/index.json", cache / "decbench__index.json", refresh=refresh)
    if error:
        return {"error": error[0]}
    aggregates, agg_error = cached_fetch("https://decbench.com/data/aggregates.json", cache / "decbench__aggregates.json", refresh=refresh)
    result = {
        "latest_snapshot": index[0] if index else None,
        "snapshots": [
            {
                "date": s.get("date"),
                "label": s.get("label"),
                "functions": s.get("functions"),
                "binaries": s.get("binaries"),
                "preset": s.get("preset"),
                "versions": s.get("decompiler_versions"),
                "leaders": s.get("leaders"),
            }
            for s in index
        ],
        "aggregates_error": agg_error[0] if agg_error else None,
    }
    if agg_error:
        return result

    names = aggregates.get("decompiler_names", {})
    versions = aggregates.get("decompiler_versions", {})
    combos = aggregates.get("combos", {})
    extracted = {}
    for key in COMBOS:
        combo = combos.get(key)
        if not combo:
            continue
        rows = {}
        for dec, (perfect, measured) in (combo.get("overall") or {}).items():
            err = (combo.get("errors") or {}).get(dec) or [0, 0]
            comp = (combo.get("compile") or {}).get(dec) or [0, 0]
            rows[dec] = {
                "name": names.get(dec, dec),
                "version": versions.get(dec, ""),
                "perfect": perfect,
                "measured": measured,
                "pct": round(100.0 * perfect / measured, 2) if measured else None,
                "errors": err[0],
                "error_pct": round(100.0 * err[0] / err[1], 2) if err[1] else None,
                "compiles": comp[0],
                "compile_measured": comp[1],
                "compile_pct": round(100.0 * comp[0] / comp[1], 2) if comp[1] else None,
            }
        extracted[key] = {
            "functions": combo.get("functions"),
            "binaries": combo.get("binaries"),
            "decompilers": dict(sorted(rows.items(), key=lambda kv: -(kv[1]["pct"] or 0))),
        }
    result["extract"] = extracted
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--refresh", action="store_true", help="ignore le cache local")
    parser.add_argument("--wait", action="store_true", help="attend le reset du quota GitHub")
    args = parser.parse_args()

    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    repos = fetch_repos(out, refresh=args.refresh, wait=args.wait)
    (out / "repos.json").write_text(
        json.dumps({"generated_at": stamp, "source": "api.github.com", "repos": repos}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    with (out / "repos.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["repo", "stars", "forks", "open_issues", "created", "pushed", "archived", "license", "language", "error"]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(repos)

    morluto = fetch_morluto(out, refresh=args.refresh)
    (out / "morluto-repos.json").write_text(
        json.dumps({"generated_at": stamp, **morluto}, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    decbench = fetch_decbench(out, refresh=args.refresh)
    (out / "decbench.json").write_text(
        json.dumps({"generated_at": stamp, **decbench}, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    ok = sum(1 for r in repos if "error" not in r)
    print(f"{ok}/{len(repos)} depots recuperes")
    print(
        f"morluto: {morluto.get('count')} depots, top jour {morluto.get('top_day')} "
        f"({morluto.get('repos_created_on_top_day')}), moins de 10 etoiles: {morluto.get('repos_under_10_stars')}"
    )
    for key, combo in (decbench.get("extract") or {}).items():
        top = list(combo["decompilers"].items())[:3]
        print(f"{key}: " + ", ".join(f"{v['name']} {v['pct']}%" for _, v in top))
    print(f"preuves -> {out}")
    return 0 if ok == len(repos) else 1


if __name__ == "__main__":
    sys.exit(main())
